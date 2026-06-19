from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .models import TaskResult, TaskStatus


class StateStore:
    def __init__(self, path: str | os.PathLike):
        self.path = Path(path)
        self._data: dict[str, Any] = {
            "version": 1,
            "results": {},
            "fingerprints": {},
        }
        self.load()

    def load(self) -> None:
        if self.path.exists():
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    self._data = json.load(f)
            except (json.JSONDecodeError, OSError):
                pass

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2, default=str)
        except OSError:
            pass

    def reset(self) -> None:
        self._data = {
            "version": 1,
            "results": {},
            "fingerprints": {},
        }
        if self.path.exists():
            try:
                self.path.unlink()
            except OSError:
                pass

    def get_task_result(self, name: str) -> TaskResult | None:
        raw = self._data.get("results", {}).get(name)
        if not raw:
            return None
        status = TaskStatus(raw.get("status", "pending"))
        return TaskResult(
            name=name,
            status=status,
            exit_code=raw.get("exit_code"),
            stdout=raw.get("stdout", ""),
            stderr=raw.get("stderr", ""),
            duration=raw.get("duration", 0.0),
            attempts=raw.get("attempts", 0),
            skipped_reason=raw.get("skipped_reason"),
            outputs=raw.get("outputs", {}),
        )

    def set_task_result(self, result: TaskResult) -> None:
        self._data.setdefault("results", {})[result.name] = {
            "status": result.status.value,
            "exit_code": result.exit_code,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "duration": result.duration,
            "attempts": result.attempts,
            "skipped_reason": result.skipped_reason,
            "outputs": result.outputs,
        }

    def get_fingerprint(self, name: str) -> dict[str, tuple[str, float]] | None:
        raw = self._data.get("fingerprints", {}).get(name)
        if not raw:
            return None
        return {k: (v[0], v[1]) for k, v in raw.items()}

    def set_fingerprint(self, name: str, fp: dict[str, tuple[str, float]]) -> None:
        self._data.setdefault("fingerprints", {})[name] = fp

    def get_successful_tasks(self) -> set[str]:
        return {
            name
            for name, raw in self._data.get("results", {}).items()
            if raw.get("status") == TaskStatus.SUCCESS.value
            or raw.get("status") == TaskStatus.UP_TO_DATE.value
        }

    def get_results_map(self) -> dict[str, TaskResult]:
        results = {}
        for name in self._data.get("results", {}):
            r = self.get_task_result(name)
            if r:
                results[name] = r
        return results
