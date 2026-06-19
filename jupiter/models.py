from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Optional


class TaskStatus(enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"
    UP_TO_DATE = "up_to_date"


class FailureStrategy(enum.Enum):
    FAIL_FAST = "fail_fast"
    CONTINUE = "continue"


@dataclass
class TaskCondition:
    file_exists: Optional[str] = None
    file_not_exists: Optional[str] = None
    var_eq: Optional[dict[str, Any]] = None
    var_ne: Optional[dict[str, Any]] = None
    upstream_status: Optional[dict[str, str]] = None


@dataclass
class TaskConfig:
    name: str
    commands: list[str]
    depends_on: list[str] = field(default_factory=list)
    cwd: Optional[str] = None
    env: dict[str, str] = field(default_factory=dict)
    timeout: Optional[float] = None
    retries: int = 0
    retry_backoff: float = 1.0
    retry_backoff_factor: float = 2.0
    inputs: list[str] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)
    condition: Optional[TaskCondition] = None


@dataclass
class TaskResult:
    name: str
    status: TaskStatus
    exit_code: Optional[int] = None
    stdout: str = ""
    stderr: str = ""
    duration: float = 0.0
    attempts: int = 0
    skipped_reason: Optional[str] = None
    outputs: dict[str, Any] = field(default_factory=dict)


@dataclass
class WorkflowConfig:
    tasks: dict[str, TaskConfig]
    variables: dict[str, Any] = field(default_factory=dict)
    env: dict[str, str] = field(default_factory=dict)
    max_concurrency: int = 4
    failure_strategy: FailureStrategy = FailureStrategy.FAIL_FAST
    state_file: str = ".jupiter_state.json"
    log_file: Optional[str] = None
    webhook_url: Optional[str] = None
