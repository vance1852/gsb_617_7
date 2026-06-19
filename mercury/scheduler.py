"""Workflow scheduler: parallel execution honoring dependency graph."""
from __future__ import annotations

import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set

from .config import TaskConfig, WorkflowConfig
from .executor import TaskResult


@dataclass
class ScheduleSummary:
    success: List[str] = field(default_factory=list)
    failed: List[str] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)
    cached: List[str] = field(default_factory=list)
    cancelled: List[str] = field(default_factory=list)
    total_duration: float = 0.0


# Function signature passed in: (task) -> TaskResult
TaskRunner = Callable[[TaskConfig], TaskResult]


class Scheduler:
    def __init__(
        self,
        workflow: WorkflowConfig,
        tasks_to_run: Set[str],
        parents: Dict[str, Set[str]],
        children: Dict[str, Set[str]],
        runner: TaskRunner,
        logger,
        already_done: Optional[Set[str]] = None,
    ):
        self.workflow = workflow
        self.tasks_to_run = set(tasks_to_run)
        self.parents = {k: set(v) for k, v in parents.items()}
        self.children = {k: set(v) for k, v in children.items()}
        self.task_map: Dict[str, TaskConfig] = {t.name: t for t in workflow.tasks}
        self.runner = runner
        self.logger = logger

        self.already_done: Set[str] = set(already_done or set())
        self.results: Dict[str, TaskResult] = {}
        self._lock = threading.Lock()
        self._stop = False  # set when fail-fast triggers

    def _ready_tasks(self, pending: Set[str], running: Set[str]) -> List[str]:
        ready: List[str] = []
        for n in sorted(pending):
            deps = self.parents.get(n, set()) & self.tasks_to_run
            unmet = deps - self.already_done - {
                d for d, r in self.results.items() if r.status in ("success", "cached", "skipped")
            }
            if not unmet:
                ready.append(n)
        return ready

    def _cancel_descendants(self, name: str, pending: Set[str]) -> List[str]:
        """Mark descendants of a failed task as cancelled (only meaningful in continue-on-error)."""
        cancelled: List[str] = []
        stack = list(self.children.get(name, set()))
        seen: Set[str] = set()
        while stack:
            n = stack.pop()
            if n in seen:
                continue
            seen.add(n)
            if n in pending:
                pending.discard(n)
                cancelled.append(n)
                stack.extend(self.children.get(n, set()))
        return cancelled

    def run(self) -> ScheduleSummary:
        start_time = time.time()
        summary = ScheduleSummary()
        pending: Set[str] = set(self.tasks_to_run) - self.already_done
        running: Set[str] = set()
        cancelled: Set[str] = set()

        max_workers = max(1, self.workflow.max_parallel)
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures: Dict[Future, str] = {}

            while pending or running:
                # Submit ready tasks unless stopped
                if not self._stop:
                    for name in self._ready_tasks(pending, running):
                        if len(running) >= max_workers:
                            break
                        pending.discard(name)
                        running.add(name)
                        fut = pool.submit(self._safe_run, self.task_map[name])
                        futures[fut] = name

                if not running:
                    # Nothing running and we were stopped: cancel rest
                    if self._stop and pending:
                        for n in list(pending):
                            cancelled.add(n)
                        pending.clear()
                    break

                # Wait for any future to complete
                done_futs = []
                while not done_futs:
                    for fut in list(futures):
                        if fut.done():
                            done_futs.append(fut)
                    if not done_futs:
                        time.sleep(0.05)

                for fut in done_futs:
                    name = futures.pop(fut)
                    running.discard(name)
                    try:
                        result = fut.result()
                    except Exception as e:  # pragma: no cover
                        self.logger.exception(f"[{name}] unexpected error: {e}")
                        result = TaskResult(
                            name=name, status="failed", reason=str(e), exit_code=-3
                        )
                    with self._lock:
                        self.results[name] = result

                    if result.status == "failed":
                        if self.workflow.fail_strategy == "fail-fast":
                            self._stop = True
                            self.logger.error(
                                f"[{name}] failed; fail-fast: cancelling remaining tasks"
                            )
                        else:
                            cancelled_now = self._cancel_descendants(name, pending)
                            for c in cancelled_now:
                                cancelled.add(c)
                                self.logger.warning(
                                    f"[{c}] cancelled because upstream '{name}' failed"
                                )

        # If we stopped and there are still pending items, they are cancelled
        for n in pending:
            cancelled.add(n)

        # Build summary
        for name, r in self.results.items():
            if r.status == "success":
                summary.success.append(name)
            elif r.status == "failed":
                summary.failed.append(name)
            elif r.status == "skipped":
                summary.skipped.append(name)
            elif r.status == "cached":
                summary.cached.append(name)
        for n in self.already_done:
            if n not in self.results:
                summary.success.append(n)
        summary.cancelled = sorted(cancelled)
        summary.total_duration = time.time() - start_time
        return summary

    def _safe_run(self, task: TaskConfig) -> TaskResult:
        try:
            return self.runner(task)
        except Exception as e:  # pragma: no cover
            self.logger.exception(f"[{task.name}] runner exception: {e}")
            return TaskResult(name=task.name, status="failed", reason=str(e), exit_code=-4)
