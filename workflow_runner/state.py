from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

from .executor import TaskResult


@dataclass
class TaskState:
    name: str
    status: str = "pending"
    exit_code: Optional[int] = None
    output: str = ""
    duration: float = 0.0
    attempts: int = 0
    timed_out: bool = False
    error: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    input_fingerprints: Dict[str, list] = field(default_factory=dict)
    output_fingerprints: Dict[str, list] = field(default_factory=dict)

    @classmethod
    def from_result(cls, result: TaskResult) -> "TaskState":
        return cls(
            name=result.name,
            status=result.status,
            exit_code=result.exit_code,
            output=result.output,
            duration=result.duration,
            attempts=result.attempts,
            timed_out=result.timed_out,
            error=result.error,
            started_at=result.started_at,
            finished_at=result.finished_at,
        )


@dataclass
class WorkflowState:
    version: str = "1"
    workflow_name: str = ""
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    status: str = "pending"
    tasks: Dict[str, TaskState] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "workflow_name": self.workflow_name,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "status": self.status,
            "tasks": {name: asdict(ts) for name, ts in self.tasks.items()},
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "WorkflowState":
        ws = cls(
            version=d.get("version", "1"),
            workflow_name=d.get("workflow_name", ""),
            started_at=d.get("started_at"),
            finished_at=d.get("finished_at"),
            status=d.get("status", "pending"),
        )
        for name, td in d.get("tasks", {}).items():
            ws.tasks[name] = TaskState(
                name=td.get("name", name),
                status=td.get("status", "pending"),
                exit_code=td.get("exit_code"),
                output=td.get("output", ""),
                duration=td.get("duration", 0.0),
                attempts=td.get("attempts", 0),
                timed_out=td.get("timed_out", False),
                error=td.get("error"),
                started_at=td.get("started_at"),
                finished_at=td.get("finished_at"),
                input_fingerprints=td.get("input_fingerprints", {}),
                output_fingerprints=td.get("output_fingerprints", {}),
            )
        return ws

    def get_result_dict(self, name: str) -> Dict[str, Any]:
        ts = self.tasks.get(name)
        if ts is None:
            return {}
        return {
            "status": ts.status,
            "exit_code": ts.exit_code,
            "output": ts.output,
            "duration": ts.duration,
            "attempts": ts.attempts,
        }

    def all_results(self) -> Dict[str, Dict[str, Any]]:
        return {n: self.get_result_dict(n) for n in self.tasks}


class StateStore:
    def __init__(self, path: Optional[str]):
        if path is None:
            self.path: Optional[Path] = None
        else:
            p = Path(path)
            if not p.is_absolute():
                p = Path.cwd() / p
            self.path = p

    def exists(self) -> bool:
        return self.path is not None and self.path.exists()

    def load(self) -> WorkflowState:
        if self.path is None or not self.path.exists():
            return WorkflowState()
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return WorkflowState.from_dict(data)
        except (json.JSONDecodeError, OSError):
            return WorkflowState()

    def save(self, state: WorkflowState) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = state.to_dict()
        fd, tmp = tempfile.mkstemp(
            prefix=".wfstate_",
            dir=str(self.path.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            os.replace(tmp, self.path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def reset(self) -> None:
        if self.path is not None and self.path.exists():
            try:
                self.path.unlink()
            except OSError:
                pass
