from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Union


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
    type: str
    params: Dict[str, Any] = field(default_factory=dict)


@dataclass
class TaskConfig:
    name: str
    commands: List[str]
    depends_on: List[str] = field(default_factory=list)
    cwd: Optional[str] = None
    env: Dict[str, str] = field(default_factory=dict)
    timeout: Optional[float] = None
    retries: int = 0
    retry_backoff: float = 1.0
    retry_backoff_factor: float = 2.0
    inputs: List[str] = field(default_factory=list)
    outputs: List[str] = field(default_factory=list)
    conditions: List[TaskCondition] = field(default_factory=list)
    run_on_failure: bool = False
    description: Optional[str] = None


@dataclass
class TaskResult:
    name: str
    status: TaskStatus
    exit_code: Optional[int] = None
    duration: float = 0.0
    attempts: int = 0
    stdout: str = ""
    stderr: str = ""
    skipped_reason: Optional[str] = None
    outputs_captured: Dict[str, str] = field(default_factory=dict)

    @property
    def success(self) -> bool:
        return self.status in (TaskStatus.SUCCESS, TaskStatus.UP_TO_DATE)


@dataclass
class WorkflowConfig:
    name: str = "workflow"
    variables: Dict[str, Any] = field(default_factory=dict)
    max_concurrency: int = 4
    failure_strategy: FailureStrategy = FailureStrategy.FAIL_FAST
    state_file: Optional[str] = None
    log_file: Optional[str] = None
    webhook_url: Optional[str] = None
    tasks: Dict[str, TaskConfig] = field(default_factory=dict)
