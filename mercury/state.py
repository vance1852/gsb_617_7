"""State persistence for resume support."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from threading import Lock
from typing import Any, Dict, Optional


class StateStore:
    """Thread-safe JSON state store keyed by task name."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = Lock()
        self._data: Dict[str, Any] = {
            "workflow": "",
            "started_at": None,
            "tasks": {},
        }
        if path.exists():
            try:
                self._data = json.loads(path.read_text(encoding="utf-8"))
                if "tasks" not in self._data:
                    self._data["tasks"] = {}
            except (OSError, json.JSONDecodeError):
                pass

    def initialize(self, workflow_name: str) -> None:
        with self._lock:
            if not self._data.get("workflow"):
                self._data["workflow"] = workflow_name
            if not self._data.get("started_at"):
                self._data["started_at"] = time.time()

    def get_task(self, name: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self._data["tasks"].get(name)

    def update_task(self, name: str, **fields: Any) -> None:
        with self._lock:
            cur = self._data["tasks"].setdefault(name, {})
            cur.update(fields)
            self._flush_locked()

    def all_tasks(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            return dict(self._data["tasks"])

    def reset(self) -> None:
        with self._lock:
            self._data = {"workflow": "", "started_at": None, "tasks": {}}
            self._flush_locked()

    def _flush_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)
