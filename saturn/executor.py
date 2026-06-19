from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, Future
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set, Tuple

from .conditions import evaluate_conditions
from .incremental import collect_fingerprints, is_up_to_date
from .interpolation import InterpolationContext, interpolate_string
from .runner import CommandRunner
from .state import StateStore
from .task import FailureStrategy, TaskConfig, TaskResult, TaskStatus, WorkflowConfig


class CyclicDependencyError(Exception):
    pass


class ExecutorError(Exception):
    pass


@dataclass
class ExecutionStats:
    total: int = 0
    success: int = 0
    failed: int = 0
    skipped: int = 0
    up_to_date: int = 0
    total_duration: float = 0.0

    @property
    def ok(self) -> bool:
        return self.failed == 0


@dataclass
class ExecutionPlan:
    tasks_in_order: List[str] = field(default_factory=list)
    task_deps: Dict[str, Set[str]] = field(default_factory=dict)
    task_rdeps: Dict[str, Set[str]] = field(default_factory=dict)
    roots: List[str] = field(default_factory=list)
    selected_tasks: Set[str] = field(default_factory=set)


def detect_cycle(tasks: Dict[str, TaskConfig]) -> Optional[List[str]]:
    WHITE, GRAY, BLACK = 0, 1, 2
    color: Dict[str, int] = {name: WHITE for name in tasks}
    parent: Dict[str, Optional[str]] = {name: None for name in tasks}
    cycle_path: List[str] = []

    def dfs(node: str) -> bool:
        color[node] = GRAY
        for dep in tasks[node].depends_on:
            if color.get(dep) == GRAY:
                cycle_path.append(dep)
                cur = node
                while cur is not None and cur != dep:
                    cycle_path.append(cur)
                    cur = parent.get(cur)
                cycle_path.append(dep)
                cycle_path.reverse()
                return True
            if color.get(dep) == WHITE:
                parent[dep] = node
                if dfs(dep):
                    return True
        color[node] = BLACK
        return False

    for name in tasks:
        if color[name] == WHITE:
            if dfs(name):
                return cycle_path
    return None


def topological_sort(tasks: Dict[str, TaskConfig]) -> List[str]:
    in_degree: Dict[str, int] = {name: 0 for name in tasks}
    for name, task in tasks.items():
        for dep in task.depends_on:
            in_degree[name] = in_degree.get(name, 0) + 1

    from collections import deque
    queue = deque([name for name, deg in in_degree.items() if deg == 0])
    result: List[str] = []

    while queue:
        node = queue.popleft()
        result.append(node)
        for name, task in tasks.items():
            if node in task.depends_on:
                in_degree[name] -= 1
                if in_degree[name] == 0:
                    queue.append(name)

    if len(result) != len(tasks):
        cycle = detect_cycle(tasks)
        if cycle:
            raise CyclicDependencyError(f"Cyclic dependency detected: {' -> '.join(cycle)}")
        raise CyclicDependencyError("Cyclic dependency detected in workflow")

    return result


def build_execution_plan(
    config: WorkflowConfig,
    only_tasks: Optional[List[str]] = None,
    exclude_tasks: Optional[List[str]] = None,
) -> ExecutionPlan:
    tasks = config.tasks
    cycle = detect_cycle(tasks)
    if cycle:
        raise CyclicDependencyError(f"Cyclic dependency detected: {' -> '.join(cycle)}")

    topo_order = topological_sort(tasks)

    task_deps: Dict[str, Set[str]] = {}
    task_rdeps: Dict[str, Set[str]] = {name: set() for name in tasks}

    for name, task in tasks.items():
        task_deps[name] = set(task.depends_on)
        for dep in task.depends_on:
            task_rdeps[dep].add(name)

    selected: Set[str] = set(tasks.keys())
    if exclude_tasks:
        for ex in exclude_tasks:
            if ex in selected:
                to_remove: Set[str] = set()
                stack = [ex]
                while stack:
                    n = stack.pop()
                    if n in to_remove:
                        continue
                    to_remove.add(n)
                    for rdep in task_rdeps.get(n, set()):
                        stack.append(rdep)
                selected -= to_remove

    if only_tasks:
        needed: Set[str] = set()
        stack = list(only_tasks)
        while stack:
            n = stack.pop()
            if n in needed:
                continue
            if n not in tasks:
                raise ExecutorError(f"Task not found: {n}")
            needed.add(n)
            for dep in task_deps.get(n, set()):
                stack.append(dep)
        selected &= needed

    selected_ordered = [t for t in topo_order if t in selected]
    roots = [t for t in selected_ordered if not (task_deps[t] & selected)]

    return ExecutionPlan(
        tasks_in_order=selected_ordered,
        task_deps={n: task_deps[n] & selected for n in selected},
        task_rdeps={n: task_rdeps[n] & selected for n in selected},
        roots=roots,
        selected_tasks=selected,
    )


class WorkflowExecutor:
    def __init__(
        self,
        config: WorkflowConfig,
        base_dir: str,
        state_store: Optional[StateStore] = None,
        dry_run: bool = False,
        resume: bool = False,
        only_tasks: Optional[List[str]] = None,
        exclude_tasks: Optional[List[str]] = None,
        log_callback: Optional[Callable[[str, str], None]] = None,
    ):
        self.config = config
        self.base_dir = base_dir
        self.state_store = state_store or StateStore(config.state_file)
        self.dry_run = dry_run
        self.resume = resume
        self.log_callback = log_callback
        self.only_tasks = only_tasks
        self.exclude_tasks = exclude_tasks

        self.task_results: Dict[str, TaskResult] = {}
        self.completed_count = 0
        self.failed_count = 0
        self.has_failure = False
        self.aborted = False
        self.runner = CommandRunner(base_dir, log_callback=self._log)
        self.logger = logging.getLogger("saturn.executor")

    def _log(self, task_name: str, message: str) -> None:
        if self.log_callback:
            self.log_callback(task_name, message)

    def _build_interpolation_context(self) -> InterpolationContext:
        return InterpolationContext(
            variables=self.config.variables,
            task_results=self.task_results,
        )

    def run(self) -> ExecutionStats:
        start_time = time.time()

        pre_completed: Set[str] = set()

        if not self.dry_run:
            self.state_store.load()
            self.state_store.reset_failed_tasks()
            self.state_store.initialize_run()

        if self.resume:
            for name, state in self.state_store.completed_tasks.items():
                if name in self.config.tasks:
                    result = TaskResult(
                        name=name,
                        status=TaskStatus(state.get("status", "success")),
                        exit_code=0,
                        duration=state.get("duration", 0),
                        attempts=state.get("attempts", 1),
                        stdout=state.get("stdout", ""),
                        outputs_captured=state.get("outputs_captured", {}),
                    )
                    self.task_results[name] = result
                    pre_completed.add(name)
                    self._log(name, f"Resumed (already completed in previous run)")

        plan = build_execution_plan(
            self.config,
            only_tasks=self.only_tasks,
            exclude_tasks=self.exclude_tasks,
        )

        pending: Set[str] = set(plan.tasks_in_order) - pre_completed
        stats_total = len(plan.selected_tasks)
        stats = ExecutionStats(total=stats_total)

        for name in pre_completed:
            if name in plan.selected_tasks:
                r = self.task_results[name]
                if r.status == TaskStatus.UP_TO_DATE:
                    stats.up_to_date += 1
                    stats.success += 1
                elif r.status == TaskStatus.SKIPPED:
                    stats.skipped += 1
                elif r.success:
                    stats.success += 1

        if self.dry_run:
            self._print_dry_run_plan(plan)
            stats.total_duration = time.time() - start_time
            return stats

        if not plan.tasks_in_order:
            self._log("", "No tasks to execute")
            stats.total_duration = time.time() - start_time
            return stats

        tasks_to_run = len(pending)
        if tasks_to_run == 0:
            self._log("", "All tasks already completed, nothing to do")
        else:
            resumed_count = len(pre_completed & plan.selected_tasks)
            if resumed_count > 0:
                self._log("", f"Resuming: {resumed_count} already completed, {tasks_to_run} remaining")
            self._log("", f"Executing {tasks_to_run} tasks with concurrency={self.config.max_concurrency}")

        running: Dict[str, Future] = {}
        completed_deps: Dict[str, Set[str]] = {n: set() for n in plan.tasks_in_order}
        for name in pre_completed:
            if name in plan.selected_tasks:
                completed_deps[name] = plan.task_deps.get(name, set())

        with ThreadPoolExecutor(max_workers=max(1, self.config.max_concurrency)) as pool:
            while pending or running:
                ready: List[str] = []
                for name in list(pending):
                    deps = plan.task_deps.get(name, set())
                    dep_satisfied = True
                    for dep in deps:
                        if dep in self.task_results:
                            dep_result = self.task_results[dep]
                            if dep_result.status == TaskStatus.FAILED:
                                task = self.config.tasks[name]
                                if not task.run_on_failure:
                                    task_result = TaskResult(
                                        name=name,
                                        status=TaskStatus.SKIPPED,
                                        skipped_reason=f"Dependency '{dep}' failed",
                                    )
                                    self.task_results[name] = task_result
                                    self._record_result(name, task_result)
                                    pending.discard(name)
                                    dep_satisfied = False
                                    break
                            elif dep_result.status == TaskStatus.SKIPPED:
                                task_result = TaskResult(
                                    name=name,
                                    status=TaskStatus.SKIPPED,
                                    skipped_reason=f"Dependency '{dep}' was skipped",
                                )
                                self.task_results[name] = task_result
                                self._record_result(name, task_result)
                                pending.discard(name)
                                dep_satisfied = False
                                break
                            else:
                                completed_deps[name].add(dep)
                        else:
                            dep_satisfied = False
                            break
                    if dep_satisfied and name in pending:
                        ready.append(name)

                for name in ready:
                    pending.discard(name)
                    future = pool.submit(self._execute_task, name)
                    running[name] = future

                if not running:
                    if pending and self.has_failure and self.config.failure_strategy == FailureStrategy.FAIL_FAST:
                        for name in pending:
                            task_result = TaskResult(
                                name=name,
                                status=TaskStatus.SKIPPED,
                                skipped_reason="Fail-fast: aborting due to earlier failure",
                            )
                            self.task_results[name] = task_result
                            self._record_result(name, task_result)
                        pending.clear()
                        break
                    if pending:
                        break
                    continue

                done_names = []
                for name, future in list(running.items()):
                    if future.done():
                        try:
                            result = future.result()
                        except Exception as e:
                            result = TaskResult(
                                name=name,
                                status=TaskStatus.FAILED,
                                stderr=f"Executor error: {e}",
                            )
                        self.task_results[name] = result
                        self._record_result(name, result)
                        done_names.append(name)
                        if result.status == TaskStatus.FAILED:
                            self.has_failure = True
                            self.failed_count += 1
                            if self.config.failure_strategy == FailureStrategy.FAIL_FAST:
                                self.aborted = True
                        elif result.status == TaskStatus.SKIPPED:
                            stats.skipped += 1
                        elif result.status == TaskStatus.UP_TO_DATE:
                            stats.up_to_date += 1
                            stats.success += 1
                        elif result.status == TaskStatus.SUCCESS:
                            stats.success += 1
                        self.completed_count += 1

                for name in done_names:
                    del running[name]

                if self.aborted and self.config.failure_strategy == FailureStrategy.FAIL_FAST:
                    for fut in running.values():
                        fut.cancel()
                    running.clear()
                    for name in pending:
                        task_result = TaskResult(
                            name=name,
                            status=TaskStatus.SKIPPED,
                            skipped_reason="Fail-fast: aborting due to task failure",
                        )
                        self.task_results[name] = task_result
                        self._record_result(name, task_result)
                    pending.clear()
                    break

                if not ready and running:
                    time.sleep(0.05)

        stats.total_duration = time.time() - start_time
        stats.failed = self.failed_count
        return stats

    def _execute_task(self, name: str) -> TaskResult:
        task = self.config.tasks[name]
        context = self._build_interpolation_context()

        resolved_cwd = task.cwd or self.base_dir
        if task.cwd:
            resolved_cwd = interpolate_string(task.cwd, context)

        resolved_inputs = [interpolate_string(p, context) for p in task.inputs]
        resolved_outputs = [interpolate_string(p, context) for p in task.outputs]

        stored_task_state = self.state_store.get_task_state(name)
        stored_fingerprints = stored_task_state.get("input_fingerprints") if stored_task_state else None
        if resolved_inputs or resolved_outputs:
            if is_up_to_date(name, resolved_inputs, resolved_outputs, resolved_cwd, stored_fingerprints):
                self._log(name, "Up-to-date, skipping")
                prev_outputs = stored_task_state.get("outputs_captured", {}) if stored_task_state else {}
                result = TaskResult(
                    name=name,
                    status=TaskStatus.UP_TO_DATE,
                    exit_code=0,
                    duration=0,
                    attempts=stored_task_state.get("attempts", 1) if stored_task_state else 1,
                    outputs_captured=prev_outputs,
                )
                return result

        if task.conditions:
            ok, reason = evaluate_conditions(
                task.conditions, context, self.task_results, resolved_cwd
            )
            if not ok:
                self._log(name, f"Skipped: {reason}")
                result = TaskResult(
                    name=name,
                    status=TaskStatus.SKIPPED,
                    skipped_reason=reason,
                )
                return result

        resolved_commands = [interpolate_string(cmd, context) for cmd in task.commands]
        import copy
        task_to_run = copy.copy(task)
        task_to_run.commands = resolved_commands
        task_to_run.cwd = resolved_cwd
        task_to_run.inputs = resolved_inputs
        task_to_run.outputs = resolved_outputs

        result = self.runner.run_task(task_to_run)

        if result.status in (TaskStatus.SUCCESS, TaskStatus.UP_TO_DATE):
            fingerprints = collect_fingerprints(resolved_inputs, resolved_cwd) if result.status == TaskStatus.SUCCESS else {}
            fp_dict = {p: fp.to_dict() for p, fp in fingerprints.items()}
            self.state_store.set_task_state(name, {
                "status": result.status.value,
                "exit_code": result.exit_code,
                "duration": result.duration,
                "attempts": result.attempts,
                "input_fingerprints": fp_dict,
                "outputs_captured": dict(result.outputs_captured),
            })
        else:
            self.state_store.set_task_state(name, {
                "status": result.status.value,
                "exit_code": result.exit_code,
                "duration": result.duration,
                "attempts": result.attempts,
            })

        self.state_store.save()
        return result

    def _record_result(self, name: str, result: TaskResult) -> None:
        if result.status in (TaskStatus.SKIPPED, TaskStatus.FAILED):
            self.state_store.set_task_state(name, {
                "status": result.status.value,
                "exit_code": result.exit_code,
                "duration": result.duration,
                "attempts": result.attempts,
                "skipped_reason": result.skipped_reason,
            })
            self.state_store.save()

    def _print_dry_run_plan(self, plan: ExecutionPlan) -> None:
        self._log("", "=== DRY RUN - Execution Plan ===")
        self._log("", f"Workflow: {self.config.name}")
        self._log("", f"Total tasks: {len(plan.tasks_in_order)}")
        self._log("", f"Max concurrency: {self.config.max_concurrency}")
        self._log("", f"Failure strategy: {self.config.failure_strategy.value}")
        self._log("", "")

        level: Dict[str, int] = {}
        for name in plan.tasks_in_order:
            deps = plan.task_deps.get(name, set())
            if not deps:
                level[name] = 0
            else:
                level[name] = max(level.get(d, 0) for d in deps) + 1

        for name in plan.tasks_in_order:
            task = self.config.tasks[name]
            indent = "  " * level.get(name, 0)
            dep_str = f" (depends on: {', '.join(task.depends_on)})" if task.depends_on else ""
            self._log("", f"{indent}[ ] {name}{dep_str}")
            for cmd in task.commands:
                self._log("", f"{indent}    $ {cmd}")
            if task.inputs:
                self._log("", f"{indent}    inputs: {', '.join(task.inputs)}")
            if task.outputs:
                self._log("", f"{indent}    outputs: {', '.join(task.outputs)}")
            if task.timeout:
                self._log("", f"{indent}    timeout: {task.timeout}s")
            if task.retries:
                self._log("", f"{indent}    retries: {task.retries}")
            if task.cwd:
                self._log("", f"{indent}    cwd: {task.cwd}")
        self._log("", "")
        self._log("", f"Execution order: {' -> '.join(plan.tasks_in_order)}")
