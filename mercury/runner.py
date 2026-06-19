"""Top-level workflow runner: ties together graph, executor, scheduler, state."""
from __future__ import annotations

import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from . import notify
from .conditions import evaluate as eval_condition
from .config import TaskConfig, WorkflowConfig
from .executor import TaskResult, execute_task
from .fingerprint import (
    Fingerprint,
    FingerprintCache,
    compute_fingerprint,
    is_up_to_date,
    outputs_exist,
)
from .graph import DependencyGraph
from .interpolate import interpolate, interpolate_mapping
from .logger import get_logger, setup_logger
from .scheduler import ScheduleSummary, Scheduler


class WorkflowRunner:
    def __init__(
        self,
        workflow: WorkflowConfig,
        base_dir: Path,
        include: Optional[List[str]] = None,
        exclude: Optional[List[str]] = None,
        dry_run: bool = False,
        resume: bool = False,
        force: bool = False,
    ):
        self.workflow = workflow
        self.base_dir = base_dir
        self.include = include or []
        self.exclude = exclude or []
        self.dry_run = dry_run
        self.resume = resume
        self.force = force

        log_path = (base_dir / workflow.log_file) if workflow.log_file else None
        self.logger = setup_logger(log_path)

        self.graph = DependencyGraph(workflow.tasks)

        self.cache = FingerprintCache(base_dir / workflow.cache_file)

        # State store: lazy import to avoid circular
        from .state import StateStore

        self.state = StateStore(base_dir / workflow.state_file)

        # In-memory task results keyed by task name (for interpolation refs)
        self._results_view: Dict[str, Dict[str, Any]] = {}
        self._results_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Planning
    # ------------------------------------------------------------------
    def plan(self) -> List[str]:
        order = self.graph.topological_order()
        selected = self.graph.filter(self.include, self.exclude)
        return [n for n in order if n in selected]

    # ------------------------------------------------------------------
    # Per-task pipeline (called by scheduler in worker threads)
    # ------------------------------------------------------------------
    def _build_results_snapshot(self) -> Dict[str, Dict[str, Any]]:
        with self._results_lock:
            return {k: dict(v) for k, v in self._results_view.items()}

    def _record_result(self, result: TaskResult) -> None:
        with self._results_lock:
            self._results_view[result.name] = {
                "status": result.status,
                "exit_code": "" if result.exit_code is None else result.exit_code,
                "stdout": (result.stdout or "").strip(),
                "stderr": (result.stderr or "").strip(),
                "duration": result.duration,
                "attempts": result.attempts,
            }
        fields = result.to_dict()
        fields.pop("name", None)
        self.state.update_task(result.name, **fields)

    def _run_task(self, task: TaskConfig) -> TaskResult:
        results_snapshot = self._build_results_snapshot()
        variables = self.workflow.variables

        # Condition check
        try:
            should_run = eval_condition(
                task.when, variables, results_snapshot, self.base_dir
            )
        except Exception as e:
            self.logger.error(f"[{task.name}] condition error: {e}")
            r = TaskResult(name=task.name, status="failed", reason=f"condition error: {e}")
            self._record_result(r)
            return r

        if not should_run:
            self.logger.info(f"[{task.name}] skipped (condition not met)")
            r = TaskResult(name=task.name, status="skipped", reason="condition not met")
            self._record_result(r)
            return r

        # Render commands & env via interpolation
        try:
            rendered_commands = [
                interpolate(c, variables, results_snapshot) for c in task.commands
            ]
            rendered_env = interpolate_mapping(task.env, variables, results_snapshot)
        except Exception as e:
            self.logger.error(f"[{task.name}] interpolation error: {e}")
            r = TaskResult(name=task.name, status="failed", reason=f"interpolation error: {e}")
            self._record_result(r)
            return r

        # Incremental check (up-to-date) using inputs+outputs fingerprint
        if not self.force and task.inputs and task.outputs:
            try:
                current_fp = compute_fingerprint(
                    task.inputs, rendered_commands, self.base_dir
                )
                cached_fp = self.cache.get(task.name)
                if is_up_to_date(current_fp, cached_fp, task.outputs, self.base_dir):
                    self.logger.info(
                        f"[{task.name}] up-to-date, skipping (cached)"
                    )
                    r = TaskResult(
                        name=task.name,
                        status="cached",
                        exit_code=0,
                        reason="inputs unchanged & outputs present",
                    )
                    self._record_result(r)
                    return r
            except Exception as e:
                self.logger.warning(
                    f"[{task.name}] fingerprint check failed: {e}; will run normally"
                )

        # Dry-run path: do not execute
        if self.dry_run:
            self.logger.info(
                f"[{task.name}] DRY-RUN — would execute {len(rendered_commands)} command(s)"
            )
            for c in rendered_commands:
                self.logger.info(f"[{task.name}]   $ {c}")
            r = TaskResult(
                name=task.name,
                status="success",
                exit_code=0,
                reason="dry-run",
                commands_executed=rendered_commands,
                attempts=0,
            )
            self._record_result(r)
            return r

        # Execute
        result = execute_task(
            task=task,
            rendered_commands=rendered_commands,
            rendered_env=rendered_env,
            base_dir=self.base_dir,
            logger=self.logger,
        )

        # Update fingerprint cache on success (only if inputs/outputs declared)
        if result.status == "success" and task.inputs and task.outputs:
            if outputs_exist(task.outputs, self.base_dir):
                fp = compute_fingerprint(task.inputs, rendered_commands, self.base_dir)
                self.cache.set(task.name, fp)
                self.cache.save()

        self._record_result(result)
        return result

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------
    def run(self) -> ScheduleSummary:
        plan = self.plan()
        self.logger.info(
            f"Workflow '{self.workflow.name}' plan ({len(plan)} tasks): "
            + " -> ".join(plan)
        )
        if self.dry_run:
            self.logger.info("DRY-RUN mode: no commands will be executed.")

        self.state.initialize(self.workflow.name)

        already_done: Set[str] = set()
        if self.resume and not self.dry_run:
            for name, st in self.state.all_tasks().items():
                if st.get("status") in ("success", "cached") and name in plan:
                    already_done.add(name)
                    # also pre-populate results view so downstream interpolation works
                    with self._results_lock:
                        self._results_view[name] = {
                            "status": st.get("status"),
                            "exit_code": st.get("exit_code", 0),
                            "stdout": (st.get("stdout") or "").strip(),
                            "stderr": (st.get("stderr") or "").strip(),
                            "duration": st.get("duration", 0.0),
                            "attempts": st.get("attempts", 0),
                        }
            if already_done:
                self.logger.info(
                    f"Resuming: skipping {len(already_done)} previously completed task(s): "
                    + ", ".join(sorted(already_done))
                )

        if not self.resume and not self.dry_run:
            # Fresh run: clear previous state (but keep cache for incremental)
            self.state.reset()
            self.state.initialize(self.workflow.name)

        scheduler = Scheduler(
            workflow=self.workflow,
            tasks_to_run=set(plan),
            parents=self.graph.parents,
            children=self.graph.children,
            runner=self._run_task,
            logger=self.logger,
            already_done=already_done,
        )

        summary = scheduler.run()
        self._print_summary(summary)
        self._maybe_notify(summary)
        return summary

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------
    def _print_summary(self, summary: ScheduleSummary) -> None:
        self.logger.info("=" * 60)
        self.logger.info(f"Workflow '{self.workflow.name}' summary")
        self.logger.info(f"  success:   {len(summary.success)} {summary.success}")
        self.logger.info(f"  cached:    {len(summary.cached)} {summary.cached}")
        self.logger.info(f"  skipped:   {len(summary.skipped)} {summary.skipped}")
        self.logger.info(f"  failed:    {len(summary.failed)} {summary.failed}")
        self.logger.info(f"  cancelled: {len(summary.cancelled)} {summary.cancelled}")
        self.logger.info(f"  total time: {summary.total_duration:.2f}s")
        self.logger.info("=" * 60)

    def _maybe_notify(self, summary: ScheduleSummary) -> None:
        if not self.workflow.webhook:
            return
        payload = {
            "workflow": self.workflow.name,
            "success": summary.success,
            "cached": summary.cached,
            "skipped": summary.skipped,
            "failed": summary.failed,
            "cancelled": summary.cancelled,
            "total_duration": summary.total_duration,
            "ok": len(summary.failed) == 0 and len(summary.cancelled) == 0,
            "tasks": {n: r.to_dict() for n, r in self._collect_results().items()},
        }
        self.logger.info(f"Posting result to webhook: {self.workflow.webhook}")
        status = notify.post_json(self.workflow.webhook, payload)
        if status is None:
            self.logger.warning("Webhook delivery failed.")
        else:
            self.logger.info(f"Webhook delivered (HTTP {status}).")

    def _collect_results(self) -> Dict[str, TaskResult]:
        out: Dict[str, TaskResult] = {}
        for name, st in self.state.all_tasks().items():
            out[name] = TaskResult(
                name=name,
                status=st.get("status", "unknown"),
                exit_code=st.get("exit_code"),
                stdout=st.get("stdout", ""),
                stderr=st.get("stderr", ""),
                attempts=st.get("attempts", 0),
                duration=st.get("duration", 0.0),
                started_at=st.get("started_at"),
                finished_at=st.get("finished_at"),
                reason=st.get("reason", ""),
            )
        return out
