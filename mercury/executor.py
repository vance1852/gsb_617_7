"""Task executor: runs commands with timeout, retry, env, cwd."""
from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from .config import TaskConfig


@dataclass
class TaskResult:
    name: str
    status: str  # "success" | "failed" | "skipped" | "cached"
    exit_code: Optional[int] = None
    stdout: str = ""
    stderr: str = ""
    attempts: int = 0
    duration: float = 0.0
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    reason: str = ""
    commands_executed: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict:
        return {
            "name": self.name,
            "status": self.status,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "attempts": self.attempts,
            "duration": self.duration,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "reason": self.reason,
        }


def _resolve_cwd(task: TaskConfig, base_dir: Path) -> Path:
    if task.cwd:
        cwd = Path(task.cwd)
        if not cwd.is_absolute():
            cwd = base_dir / cwd
        return cwd
    return base_dir


def _run_one_command(
    cmd: str,
    cwd: Path,
    env: Dict[str, str],
    timeout: Optional[float],
    capture: bool,
) -> subprocess.CompletedProcess:
    # shell=True for cross-platform shell command execution
    return subprocess.run(
        cmd,
        shell=True,
        cwd=str(cwd),
        env=env,
        capture_output=capture,
        text=True,
        timeout=timeout,
    )


def execute_task(
    task: TaskConfig,
    rendered_commands: List[str],
    rendered_env: Dict[str, str],
    base_dir: Path,
    logger,
) -> TaskResult:
    """Run a task: each command in sequence; retry the whole task on failure."""
    cwd = _resolve_cwd(task, base_dir)
    cwd.mkdir(parents=True, exist_ok=True)

    full_env = os.environ.copy()
    full_env.update(rendered_env)

    result = TaskResult(name=task.name, status="failed", commands_executed=rendered_commands)
    result.started_at = time.time()

    attempts = max(1, task.retry.max_attempts)
    backoff = task.retry.backoff_initial

    last_stdout = ""
    last_stderr = ""
    last_exit: Optional[int] = None

    for attempt in range(1, attempts + 1):
        result.attempts = attempt
        logger.info(
            f"[{task.name}] attempt {attempt}/{attempts} (cwd={cwd})"
        )
        attempt_failed = False
        attempt_stdout: List[str] = []
        attempt_stderr: List[str] = []

        for idx, cmd in enumerate(rendered_commands, start=1):
            logger.info(f"[{task.name}] $ {cmd}")
            try:
                cp = _run_one_command(
                    cmd, cwd, full_env, task.timeout, task.capture_output
                )
            except subprocess.TimeoutExpired as e:
                last_exit = -1
                msg = f"timeout after {task.timeout}s"
                logger.error(f"[{task.name}] {msg}")
                attempt_stderr.append(str(e))
                attempt_failed = True
                break
            except Exception as e:  # pragma: no cover
                last_exit = -2
                logger.error(f"[{task.name}] launch error: {e}")
                attempt_stderr.append(str(e))
                attempt_failed = True
                break

            last_exit = cp.returncode
            if task.capture_output:
                if cp.stdout:
                    attempt_stdout.append(cp.stdout)
                if cp.stderr:
                    attempt_stderr.append(cp.stderr)

            if cp.returncode != 0:
                logger.error(
                    f"[{task.name}] command #{idx} failed with exit code {cp.returncode}"
                )
                attempt_failed = True
                break

        last_stdout = "".join(attempt_stdout)
        last_stderr = "".join(attempt_stderr)

        if not attempt_failed:
            result.status = "success"
            result.exit_code = 0
            result.stdout = last_stdout
            result.stderr = last_stderr
            break

        if attempt < attempts:
            sleep_for = min(backoff, task.retry.backoff_max)
            logger.warning(
                f"[{task.name}] retry in {sleep_for:.1f}s "
                f"(attempt {attempt} failed)"
            )
            time.sleep(sleep_for)
            backoff *= task.retry.backoff_factor

    if result.status != "success":
        result.exit_code = last_exit
        result.stdout = last_stdout
        result.stderr = last_stderr
        result.reason = "exhausted retries"

    result.finished_at = time.time()
    result.duration = result.finished_at - result.started_at
    logger.info(
        f"[{task.name}] {result.status} in {result.duration:.2f}s "
        f"(exit={result.exit_code}, attempts={result.attempts})"
    )
    return result
