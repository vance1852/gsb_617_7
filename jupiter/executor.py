from __future__ import annotations

import asyncio
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from . import fingerprint as fp_mod
from . import interpolation
from .graph import TaskGraph
from .logger import Logger
from .models import (
    FailureStrategy,
    TaskConfig,
    TaskResult,
    TaskStatus,
    WorkflowConfig,
)
from .state import StateStore


class TaskExecutor:
    def __init__(
        self,
        workflow: WorkflowConfig,
        graph: TaskGraph,
        state: StateStore,
        logger: Logger,
        selected: set[str] | None = None,
        resume: bool = False,
    ):
        self.workflow = workflow
        self.graph = graph
        self.state = state
        self.logger = logger
        self.selected = selected or set(workflow.tasks.keys())
        self.resume = resume

        self.results: dict[str, TaskResult] = {}
        self._completed: set[str] = set()
        self._running: set[str] = set()
        self._lock = asyncio.Lock()
        self._sem: asyncio.Semaphore | None = None
        self._failed = False

    async def _run_commands_sync(
        self,
        task: TaskConfig,
        commands: list[str],
        env: dict[str, str],
        cwd: str | None,
        timeout: float | None,
    ) -> tuple[int, str, str]:
        shell = os.name == "nt"
        merged_env = os.environ.copy()
        merged_env.update(env)

        stdout_parts: list[str] = []
        stderr_parts: list[str] = []
        exit_code = 0

        for cmd in commands:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=merged_env,
                cwd=cwd,
                shell=True,
            )
            try:
                out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
                stdout_parts.append(out.decode("utf-8", errors="replace"))
                stderr_parts.append(err.decode("utf-8", errors="replace"))
                exit_code = proc.returncode if proc.returncode is not None else -1
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                    await proc.wait()
                except Exception:
                    pass
                return -1, "".join(stdout_parts), f"Timeout after {timeout}s"
            if exit_code != 0:
                break

        return exit_code, "".join(stdout_parts), "".join(stderr_parts)

    async def _execute_single(self, task: TaskConfig) -> TaskResult:
        name = task.name
        t0 = time.time()

        results_so_far = self.state.get_results_map()
        results_so_far.update(self.results)

        env = dict(self.workflow.env)
        env.update(task.env)
        env = {k: interpolation.interpolate(v, self.workflow.variables, env, results_so_far) for k, v in env.items()}

        cwd = task.cwd
        if cwd:
            cwd = interpolation.interpolate(cwd, self.workflow.variables, env, results_so_far)

        should_run_ok, skip_reason = interpolation.should_run(
            task.condition, self.workflow.variables, env, results_so_far
        )
        if not should_run_ok:
            return TaskResult(
                name=name,
                status=TaskStatus.SKIPPED,
                duration=time.time() - t0,
                skipped_reason=skip_reason,
            )

        if self.resume:
            prev = self.state.get_task_result(name)
            if prev and prev.status in (TaskStatus.SUCCESS, TaskStatus.UP_TO_DATE):
                prev.duration = 0.0
                return prev

        base_dir = Path(cwd) if cwd else Path.cwd()
        old_fp = self.state.get_fingerprint(name)
        if task.outputs and old_fp is not None:
            if fp_mod.is_up_to_date(task.inputs, task.outputs, old_fp, base_dir):
                return TaskResult(
                    name=name,
                    status=TaskStatus.UP_TO_DATE,
                    duration=0.0,
                    skipped_reason="up-to-date",
                )

        commands = interpolation.interpolate_all(
            task.commands, self.workflow.variables, env, results_so_far
        )

        attempts = 0
        exit_code = -1
        out = ""
        err = ""
        max_attempts = max(1, task.retries + 1)
        backoff = task.retry_backoff

        for attempt in range(1, max_attempts + 1):
            attempts = attempt
            self.logger.task_start(name, attempt)
            exit_code, out, err = await self._run_commands_sync(task, commands, env, cwd, task.timeout)
            if exit_code == 0:
                break
            if attempt < max_attempts:
                await asyncio.sleep(backoff)
                backoff *= task.retry_backoff_factor

        duration = time.time() - t0

        if exit_code == 0:
            new_fp = fp_mod.compute_fingerprint(task.inputs, base_dir)
            self.state.set_fingerprint(name, new_fp)
            result = TaskResult(
                name=name,
                status=TaskStatus.SUCCESS,
                exit_code=0,
                stdout=out,
                stderr=err,
                duration=duration,
                attempts=attempts,
            )
        else:
            result = TaskResult(
                name=name,
                status=TaskStatus.FAILED,
                exit_code=exit_code,
                stdout=out,
                stderr=err,
                duration=duration,
                attempts=attempts,
            )

        return result

    async def _run_task(self, name: str) -> None:
        async with self._sem:
            task = self.workflow.tasks[name]
            try:
                result = await self._execute_single(task)
            except Exception as e:
                result = TaskResult(
                    name=name,
                    status=TaskStatus.FAILED,
                    stderr=f"Executor error: {e}",
                )

            async with self._lock:
                self.results[name] = result
                self._running.discard(name)
                self._completed.add(name)
                self.state.set_task_result(result)
                self.state.save()
                self.logger.task_end(result)
                if result.status == TaskStatus.FAILED:
                    self._failed = True

    def _get_tasks_whose_deps_failed(self) -> set[str]:
        skipped: set[str] = set()
        for name in self.selected:
            if name in self._completed:
                continue
            for dep in self.workflow.tasks[name].depends_on:
                dep_result = self.results.get(dep) or self.state.get_task_result(dep)
                if dep_result and dep_result.status == TaskStatus.FAILED:
                    skipped.add(name)
                    break
        return skipped

    async def run(self) -> dict[str, TaskResult]:
        self._sem = asyncio.Semaphore(self.workflow.max_concurrency)
        tasks_by_layer = self.graph.get_execution_layers(self.selected)

        for layer in tasks_by_layer:
            if self._failed and self.workflow.failure_strategy == FailureStrategy.FAIL_FAST:
                for name in layer:
                    result = TaskResult(
                        name=name,
                        status=TaskStatus.SKIPPED,
                        skipped_reason="dependency failed",
                    )
                    self.results[name] = result
                    self._completed.add(name)
                    self.state.set_task_result(result)
                    self.logger.task_end(result)
                continue

            to_run = []
            for name in layer:
                if name in self._completed or name in self._running:
                    continue
                deps_failed = False
                for dep in self.workflow.tasks[name].depends_on:
                    dep_result = self.results.get(dep)
                    if dep_result and dep_result.status == TaskStatus.FAILED:
                        deps_failed = True
                        break
                if deps_failed and self.workflow.failure_strategy == FailureStrategy.FAIL_FAST:
                    result = TaskResult(
                        name=name,
                        status=TaskStatus.SKIPPED,
                        skipped_reason="dependency failed",
                    )
                    self.results[name] = result
                    self._completed.add(name)
                    self.state.set_task_result(result)
                    self.logger.task_end(result)
                    continue
                to_run.append(name)

            if not to_run:
                continue

            self._running.update(to_run)
            await asyncio.gather(*[self._run_task(n) for n in to_run])

        self.state.save()
        return self.results
