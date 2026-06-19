from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional


class StateStore:
    def __init__(self, state_file: Optional[str] = None):
        self.state_file = state_file
        self._data: Dict[str, Any] = {
            "version": 1,
            "created_at": None,
            "updated_at": None,
            "tasks": {},
        }
        self._loaded = False

    def load(self) -> bool:
        if not self.state_file:
            return False
        path = Path(self.state_file)
        if not path.exists():
            return False
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                self._data.update(data)
                self._loaded = True
                return True
        except (json.JSONDecodeError, OSError, IOError):
            pass
        return False

    def save(self) -> None:
        if not self.state_file:
            return
        self._data["updated_at"] = time.time()
        path = Path(self.state_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(".tmp")
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2, ensure_ascii=False)
            os.replace(tmp_path, path)
        except Exception:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
            raise

    def initialize_run(self) -> None:
        if not self._data.get("created_at"):
            self._data["created_at"] = time.time()
        self._data["updated_at"] = time.time()

    def get_task_state(self, task_name: str) -> Optional[Dict]:
        return self._data["tasks"].get(task_name)

    def set_task_state(self, task_name: str, state: Dict) -> None:
        self._data["tasks"][task_name] = state
        self._data["tasks"][task_name]["updated_at"] = time.time()

    def is_task_completed(self, task_name: str) -> bool:
        task_state = self.get_task_state(task_name)
        if not task_state:
            return False
        status = task_state.get("status")
        return status in ("success", "up_to_date")

    def get_task_fingerprints(self, task_name: str) -> Optional[Dict]:
        task_state = self.get_task_state(task_name)
        if not task_state:
            return None
        return task_state.get("input_fingerprints")

    def reset_failed_tasks(self) -> None:
        to_remove = []
        for name, state in self._data["tasks"].items():
            if state.get("status") in ("failed", "running", None):
                to_remove.append(name)
        for name in to_remove:
            del self._data["tasks"][name]

    def clear(self) -> None:
        self._data = {
            "version": 1,
            "created_at": time.time(),
            "updated_at": time.time(),
            "tasks": {},
        }
        self.save()

    @property
    def completed_tasks(self) -> Dict[str, Dict]:
        return {
            name: state
            for name, state in self._data["tasks"].items()
            if state.get("status") in ("success", "up_to_date", "skipped")
        }

    @property
    def failed_tasks(self) -> Dict[str, Dict]:
        return {
            name: state
            for name, state in self._data["tasks"].items()
            if state.get("status") == "failed"
        }
