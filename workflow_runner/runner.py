from __future__ import annotations

import json
import threading
import time
import urllib.request
import urllib.error
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set

from .config import WorkflowConfig, TaskConfig
from .executor import Executor, TaskResult, run_task
from .graph import TaskGraph, build_graph
from .incremental import fingerprint_files, fingerprint_outputs, is_up_to_date
from .logger import get_logger
from .state import StateStore, TaskState, WorkflowState
from .task import (
    TaskStatus,
    evaluate_condition,
    interpolate,
    interpolate_commands,
)


@dataclass
class RunOptions:
    dry_run: bool = False
    resume: bool = False
    only_tasks: Optional[Set[str]] = None
    exclude_tasks: Optional[Set[str]] = None
    fail_fast: Optional[bool] = None
    force: bool = False
    variables: Optional[Dict[str, Any]] = None


class WorkflowRunner:
    def __init__(
        self,
        config: WorkflowConfig,
        state_store: StateStore,
        base_dir: str,
        options: Optional[RunOptions] = None,
    ):
        self.config = config
        self.state_store = state_store
        self.base_dir = base_dir
        self.opts = options or RunOptions()
        self.log = get_logger()
        self._state = WorkflowState(workflow_name=config.name)
        self._lock = threading.Lock()
        self._fail_fast = self.opts.fail_fast if self.opts.fail_fast is not None else config.fail_fast
        self._aborted = threading.Event()
        self._start_time: float = 0.0
        self._end_time: float = 0.0
        self._prev_state: WorkflowState = WorkflowState()

    def _load_state(self) -> WorkflowState:
        if self.state_store.exists():
            state = self.state_store.load()
            if self.opts.resume:
                self.log.info("Resuming from previous state: %s", self.state_store.path)
            return state
        return WorkflowState(workflow_name=self.config.name)

    def _build_graph(self) -> TaskGraph:
        g = build_graph(self.config.tasks)
        cycle = g.detect_cycle()
        if cycle:
            from .graph import CyclicDependencyError
            raise CyclicDependencyError(cycle)
        if self.opts.only_tasks or self.opts.exclude_tasks:
            g = g.filtered_subgraph(
                include=self.opts.only_tasks,
                exclude=self.opts.exclude_tasks,
            )
        return g

    def _variables(self) -> Dict[str, Any]:
        vars_copy = dict(self.config.variables)
        if self.opts.variables:
            vars_copy.update(self.opts.variables)
        return vars_copy

    def _env(self) -> Dict[str, str]:
        return dict(self.config.env)

    def _print_plan(self, levels: List[List[str]], graph: TaskGraph) -> None:
        print("=== DRY RUN: Execution Plan ===")
        print(f"Workflow: {self.config.name}")
        print(f"Max concurrency: {self.config.max_concurrency}")
        print(f"Fail fast: {self._fail_fast}")
        print()
        for lvl_idx, level in enumerate(levels):
            print(f"Level {lvl_idx + 1} (parallel group):")
            for name in sorted(level):
                tc = self.config.tasks[name]
                deps = sorted(graph.dependencies(name))
                tags = []
                if tc.timeout:
                    tags.append(f"timeout={tc.timeout}s")
                if tc.retries:
                    tags.append(f"retries={tc.retries}")
                if tc.inputs:
                    tags.append(f"inputs={len(tc.inputs)}")
                if tc.outputs:
                    tags.append(f"outputs={len(tc.outputs)}")
                if tc.condition:
                    tags.append("conditional")
                tag_str = f" [{', '.join(tags)}]" if tags else ""
                dep_str = f" (depends on: {', '.join(deps)})" if deps else ""
                print(f"  - {name}{tag_str}{dep_str}")
                for cmd in tc.commands:
                    print(f"      $ {cmd}")
            print()
        total = sum(len(l) for l in levels)
        print(f"Total tasks to execute: {total}")

    def _save_state_locked(self) -> None:
        self.state_store.save(self._state)

    def _record_result(self, result: TaskResult, task_state: TaskState) -> None:
        with self._lock:
            self._state.tasks[result.name] = task_state
            self._save_state_locked()

    def _task_results_for_interpolation(self) -> Dict[str, Dict[str, Any]]:
        return self._state.all_results()

    def _resolve_cwd(self, tc: TaskConfig) -> str:
        if tc.cwd:
            return tc.cwd
        return self.base_dir

    def _is_up_to_date(self, tc: TaskConfig) -> bool:
        if self.opts.force:
            return False
        if not tc.outputs:
            return False
        cwd = self._resolve_cwd(tc)
        in_fps = fingerprint_files(tc.inputs, cwd)
        out_fps = fingerprint_outputs(tc.outputs, cwd)
        prev = self._prev_state.tasks.get(tc.name)
        if not prev or prev.status != TaskStatus.SUCCESS.value:
            return False
        prev_in_converted = {k: tuple(v) for k, v in prev.input_fingerprints.items()} if prev.input_fingerprints else {}
        prev_out_converted = {k: tuple(v) for k, v in prev.output_fingerprints.items()} if prev.output_fingerprints else None
        return is_up_to_date(in_fps, out_fps, prev_in_converted, prev_out_converted, tc.outputs, cwd)

    def _on_task_log(self, task_name: str, msg: str) -> None:
        self.log.info("[%s] %s", task_name, msg)

    def _execute_task(self, tc: TaskConfig) -> TaskResult:
        variables = self._variables()
        env = self._env()
        with self._lock:
            results = self._task_results_for_interpolation()

        try:
            resolved_env = {}
            for k, v in env.items():
                resolved_env[k] = interpolate(v, variables, env, results)
            for k, v in tc.env.items():
                resolved_env[k] = interpolate(v, variables, env, results)
        except Exception as e:
            r = TaskResult(
                name=tc.name,
                status=TaskStatus.FAILED.value,
                error=f"Variable interpolation failed (env): {e}",
                duration=0.0,
                attempts=0,
                started_at=time.strftime("%Y-%m-%d %H:%M:%S"),
                finished_at=time.strftime("%Y-%m-%d %H:%M:%S"),
            )
            return r

        try:
            commands = interpolate_commands(tc.commands, variables, resolved_env, results)
        except Exception as e:
            r = TaskResult(
                name=tc.name,
                status=TaskStatus.FAILED.value,
                error=f"Variable interpolation failed (commands): {e}",
                duration=0.0,
                attempts=0,
                started_at=time.strftime("%Y-%m-%d %H:%M:%S"),
                finished_at=time.strftime("%Y-%m-%d %H:%M:%S"),
            )
            return r

        cwd = self._resolve_cwd(tc)
        return run_task(
            tc,
            commands=commands,
            extra_env=resolved_env,
            base_cwd=cwd,
            on_log=self._on_task_log,
        )

    def _handle_task_completion(
        self, tc: TaskConfig, result: TaskResult
    ) -> TaskState:
        cwd = self._resolve_cwd(tc)
        ts = TaskState.from_result(result)

        if tc.inputs or tc.outputs:
            in_fps = fingerprint_files(tc.inputs, cwd)
            out_fps = fingerprint_outputs(tc.outputs, cwd)
            ts.input_fingerprints = {k: list(v) for k, v in in_fps.items()}
            ts.output_fingerprints = {k: list(v) for k, v in out_fps.items()}

        return ts

    def _skip_task(self, tc: TaskConfig, reason: str) -> TaskResult:
        self.log.info("[%s] skipped: %s", tc.name, reason)
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        return TaskResult(
            name=tc.name,
            status=TaskStatus.SKIPPED.value,
            duration=0.0,
            attempts=0,
            error=reason,
            started_at=now,
            finished_at=now,
        )

    def _up_to_date_task(self, tc: TaskConfig) -> TaskResult:
        self.log.info("[%s] up-to-date, skipping", tc.name)
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        prev = self._state.tasks.get(tc.name)
        dur = prev.duration if prev else 0.0
        return TaskResult(
            name=tc.name,
            status=TaskStatus.UP_TO_DATE.value,
            exit_code=0,
            output=prev.output if prev else "",
            duration=dur,
            attempts=prev.attempts if prev else 0,
            started_at=prev.started_at if prev else now,
            finished_at=now,
        )

    def _any_dep_failed(self, name: str, graph: TaskGraph) -> bool:
        for dep in graph.dependencies(name):
            dep_state = self._state.tasks.get(dep)
            if dep_state and dep_state.status in (TaskStatus.FAILED.value, TaskStatus.SKIPPED.value):
                dep_tc = self.config.tasks.get(dep)
                if dep_tc and dep_tc.condition:
                    continue
                return True
        return False

    def _run_level(
        self,
        level: List[str],
        graph: TaskGraph,
        executor: Executor,
    ) -> Dict[str, TaskResult]:
        futures: Dict[str, Future] = {}
        results: Dict[str, TaskResult] = {}
        variables = self._variables()
        env = self._env()

        for name in level:
            tc = self.config.tasks[name]

            if self._aborted.is_set():
                results[name] = self._skip_task(tc, "aborted due to earlier failure (fail-fast)")
                continue

            with self._lock:
                current_results = self._task_results_for_interpolation()

            if self.opts.resume:
                prev = self._state.tasks.get(name)
                if prev and prev.status == TaskStatus.SUCCESS.value:
                    results[name] = TaskResult(
                        name=name,
                        status=TaskStatus.SUCCESS.value,
                        exit_code=prev.exit_code,
                        output=prev.output,
                        duration=prev.duration,
                        attempts=prev.attempts,
                        started_at=prev.started_at,
                        finished_at=prev.finished_at,
                    )
                    self.log.info("[%s] already succeeded in previous run, skipping (resume)", name)
                    continue

            if self._any_dep_failed(name, graph):
                results[name] = self._skip_task(tc, "dependency failed or was skipped")
                continue

            try:
                cond_result = evaluate_condition(
                    tc.condition, variables, env, current_results, self._resolve_cwd(tc)
                )
            except Exception as e:
                self.log.error("[%s] condition evaluation error: %s", name, e)
                results[name] = self._skip_task(tc, f"condition error: {e}")
                continue

            if not cond_result:
                results[name] = self._skip_task(tc, "condition evaluated to false")
                continue

            if not self.opts.force and tc.outputs and self._is_up_to_date(tc):
                results[name] = self._up_to_date_task(tc)
                continue

            self.log.info("[%s] starting...", name)
            futures[name] = executor.submit(self._execute_task, tc)

        for name, fut in futures.items():
            try:
                result = fut.result()
            except Exception as e:
                now = time.strftime("%Y-%m-%d %H:%M:%S")
                result = TaskResult(
                    name=name,
                    status=TaskStatus.FAILED.value,
                    error=f"executor exception: {e}",
                    duration=0.0,
                    attempts=0,
                    started_at=now,
                    finished_at=now,
                )
            results[name] = result

            ts = self._handle_task_completion(self.config.tasks[name], result)
            self._record_result(result, ts)

            if result.status == TaskStatus.SUCCESS.value:
                self.log.info(
                    "[%s] succeeded in %.2fs (attempts: %d)",
                    name, result.duration, result.attempts,
                )
            elif result.status == TaskStatus.FAILED.value:
                self.log.error(
                    "[%s] FAILED in %.2fs after %d attempt(s): %s",
                    name, result.duration, result.attempts, result.error or "exit code " + str(result.exit_code),
                )
                if self._fail_fast:
                    self._aborted.set()
                    self.log.warning("fail-fast enabled: aborting remaining tasks")

        for name, result in results.items():
            if name not in futures:
                ts = TaskState.from_result(result)
                tc = self.config.tasks.get(name)
                if tc and (tc.inputs or tc.outputs):
                    cwd = self._resolve_cwd(tc)
                    prev = self._prev_state.tasks.get(name)
                    if prev and (prev.input_fingerprints or prev.output_fingerprints):
                        ts.input_fingerprints = prev.input_fingerprints
                        ts.output_fingerprints = prev.output_fingerprints
                    else:
                        ts.input_fingerprints = {k: list(v) for k, v in fingerprint_files(tc.inputs, cwd).items()}
                        ts.output_fingerprints = {k: list(v) for k, v in fingerprint_outputs(tc.outputs, cwd).items()}
                self._record_result(result, ts)

        return results

    def _print_summary(self) -> None:
        counts: Dict[str, int] = {s.value: 0 for s in TaskStatus}
        total_duration = 0.0
        for ts in self._state.tasks.values():
            counts[ts.status] = counts.get(ts.status, 0) + 1
            total_duration += ts.duration

        wall = self._end_time - self._start_time if self._end_time else 0.0

        print()
        print("=" * 60)
        print(f"Workflow '{self.config.name}' finished: {self._state.status}")
        print(f"  Success   : {counts.get(TaskStatus.SUCCESS.value, 0)}")
        print(f"  Up-to-date: {counts.get(TaskStatus.UP_TO_DATE.value, 0)}")
        print(f"  Failed    : {counts.get(TaskStatus.FAILED.value, 0)}")
        print(f"  Skipped   : {counts.get(TaskStatus.SKIPPED.value, 0)}")
        print(f"  Wall time : {wall:.2f}s")
        print(f"  CPU/Exec  : {total_duration:.2f}s (sum of all task durations)")
        print("=" * 60)

        failed = [name for name, ts in self._state.tasks.items() if ts.status == TaskStatus.FAILED.value]
        if failed:
            print("Failed tasks:")
            for n in failed:
                ts = self._state.tasks[n]
                print(f"  - {n}: {ts.error or 'exit code ' + str(ts.exit_code)}")

    def _send_webhook(self) -> None:
        if not self.config.webhook:
            return
        url = self.config.webhook
        payload = {
            "workflow": self.config.name,
            "status": self._state.status,
            "started_at": self._state.started_at,
            "finished_at": self._state.finished_at,
            "wall_time": self._end_time - self._start_time if self._end_time else 0.0,
            "tasks": {
                name: {
                    "status": ts.status,
                    "exit_code": ts.exit_code,
                    "duration": ts.duration,
                    "attempts": ts.attempts,
                    "error": ts.error,
                }
                for name, ts in self._state.tasks.items()
            },
        }
        try:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(
                url,
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                resp.read()
            self.log.info("Webhook notification sent to %s", url)
        except (urllib.error.URLError, OSError) as e:
            self.log.warning("Failed to send webhook to %s: %s", url, e)

    def run(self) -> int:
        graph = self._build_graph()
        levels = graph.topological_levels()

        if self.opts.dry_run:
            self._print_plan(levels, graph)
            return 0

        prev = self._load_state()
        self._prev_state = prev

        if self.opts.resume:
            self._state = prev
        else:
            self._state = WorkflowState(workflow_name=self.config.name)

        self._state.workflow_name = self.config.name
        self._state.started_at = time.strftime("%Y-%m-%d %H:%M:%S")
        self._state.status = TaskStatus.RUNNING.value
        self.state_store.save(self._state)

        self.log.info("Starting workflow '%s' (%d tasks, max concurrency=%d)",
                       self.config.name, sum(len(l) for l in levels), self.config.max_concurrency)

        self._start_time = time.monotonic()

        with Executor(max_concurrency=self.config.max_concurrency) as executor:
            for lvl_idx, level in enumerate(levels):
                self.log.info("--- Level %d/%d (%d tasks) ---",
                              lvl_idx + 1, len(levels), len(level))
                self._run_level(level, graph, executor)
                if self._aborted.is_set():
                    break

        self._end_time = time.monotonic()
        self._state.finished_at = time.strftime("%Y-%m-%d %H:%M:%S")

        has_failed = any(ts.status == TaskStatus.FAILED.value for ts in self._state.tasks.values())
        self._state.status = TaskStatus.FAILED.value if has_failed else TaskStatus.SUCCESS.value
        self.state_store.save(self._state)

        self._print_summary()
        self._send_webhook()

        return 1 if has_failed else 0
