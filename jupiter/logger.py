from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from .models import TaskResult, TaskStatus


class Logger:
    def __init__(self, log_file: Optional[str] = None, verbose: bool = True):
        self.logger = logging.getLogger("jupiter")
        self.logger.setLevel(logging.DEBUG)
        self.logger.handlers.clear()

        fmt = logging.Formatter(
            "[%(asctime)s] %(levelname)-7s %(message)s",
            datefmt="%H:%M:%S",
        )

        console = logging.StreamHandler(sys.stdout)
        console.setLevel(logging.INFO if verbose else logging.WARNING)
        console.setFormatter(fmt)
        self.logger.addHandler(console)

        if log_file:
            Path(log_file).parent.mkdir(parents=True, exist_ok=True)
            fh = logging.FileHandler(log_file, encoding="utf-8", mode="a")
            fh.setLevel(logging.DEBUG)
            fh.setFormatter(fmt)
            self.logger.addHandler(fh)

    def info(self, msg: str) -> None:
        self.logger.info(msg)

    def debug(self, msg: str) -> None:
        self.logger.debug(msg)

    def warning(self, msg: str) -> None:
        self.logger.warning(msg)

    def error(self, msg: str) -> None:
        self.logger.error(msg)

    def task_start(self, name: str, attempt: int = 1) -> None:
        if attempt > 1:
            self.info(f"▶ {name} (attempt {attempt})")
        else:
            self.info(f"▶ {name}")

    def task_end(self, result: TaskResult) -> None:
        status = result.status
        tag = {
            TaskStatus.SUCCESS: "✓",
            TaskStatus.FAILED: "✗",
            TaskStatus.SKIPPED: "⊘",
            TaskStatus.UP_TO_DATE: "=",
        }.get(status, "?")
        dur = f"{result.duration:.2f}s" if result.duration else ""
        msg = f"{tag} {result.name}"
        parts = []
        if dur:
            parts.append(dur)
        if result.exit_code is not None and status == TaskStatus.FAILED:
            parts.append(f"exit={result.exit_code}")
        if result.skipped_reason:
            parts.append(result.skipped_reason)
        if parts:
            msg += " (" + ", ".join(parts) + ")"
        if status == TaskStatus.FAILED:
            self.error(msg)
        else:
            self.info(msg)

    def print_summary(self, results: dict[str, TaskResult], total_duration: float) -> None:
        counts = {s: 0 for s in TaskStatus}
        for r in results.values():
            counts[r.status] += 1

        self.info("")
        self.info("=" * 50)
        self.info(f"Summary ({total_duration:.2f}s total):")
        self.info(f"  Success:    {counts[TaskStatus.SUCCESS]}")
        self.info(f"  Up-to-date: {counts[TaskStatus.UP_TO_DATE]}")
        self.info(f"  Failed:     {counts[TaskStatus.FAILED]}")
        self.info(f"  Skipped:    {counts[TaskStatus.SKIPPED]}")
        self.info("=" * 50)

    def print_plan(self, layers: list[list[str]]) -> None:
        self.info("Execution plan (dry-run):")
        for i, layer in enumerate(layers, 1):
            self.info(f"  Layer {i} (parallel): {', '.join(layer)}")
        self.info("")
