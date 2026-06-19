from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

from .task import TaskConfig, TaskResult, TaskStatus


class CommandRunner:
    def __init__(
        self,
        base_dir: str,
        log_callback: Optional[Callable[[str, str], None]] = None,
    ):
        self.base_dir = base_dir
        self.log_callback = log_callback

    def run_task(self, task: TaskConfig, env: Optional[Dict[str, str]] = None) -> TaskResult:
        start_time = time.time()
        attempts = 0
        max_attempts = task.retries + 1
        last_result: Optional[TaskResult] = None

        merged_env = os.environ.copy()
        if env:
            merged_env.update({k: str(v) for k, v in env.items()})
        if task.env:
            for k, v in task.env.items():
                merged_env[k] = str(v)

        cwd = task.cwd or self.base_dir
        Path(cwd).mkdir(parents=True, exist_ok=True)

        while attempts < max_attempts:
            attempts += 1
            attempt_start = time.time()

            if self.log_callback:
                self.log_callback(task.name, f"Starting (attempt {attempts}/{max_attempts})")

            stdout_parts: List[str] = []
            stderr_parts: List[str] = []
            exit_code: Optional[int] = None
            timed_out = False
            failed_cmd: Optional[str] = None

            for cmd in task.commands:
                if self.log_callback:
                    self.log_callback(task.name, f"  $ {cmd}")

                try:
                    proc = subprocess.Popen(
                        cmd,
                        shell=True,
                        cwd=cwd,
                        env=merged_env,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        bufsize=1,
                    )
                except Exception as e:
                    stderr_parts.append(f"Failed to start process: {e}")
                    exit_code = -1
                    failed_cmd = cmd
                    break

                try:
                    stdout, stderr = proc.communicate(timeout=task.timeout)
                    if stdout:
                        stdout_parts.append(stdout)
                        if self.log_callback:
                            for line in stdout.splitlines():
                                self.log_callback(task.name, f"  | {line}")
                    if stderr:
                        stderr_parts.append(stderr)
                        if self.log_callback:
                            for line in stderr.splitlines():
                                self.log_callback(task.name, f"  | {line}")
                    exit_code = proc.returncode
                except subprocess.TimeoutExpired:
                    proc.kill()
                    try:
                        stdout, stderr = proc.communicate(timeout=5)
                        if stdout:
                            stdout_parts.append(stdout)
                        if stderr:
                            stderr_parts.append(stderr)
                    except Exception:
                        pass
                    timed_out = True
                    exit_code = -1
                    stderr_parts.append(f"Command timed out after {task.timeout}s: {cmd}")
                    failed_cmd = cmd
                    break

                if exit_code != 0:
                    failed_cmd = cmd
                    break

            attempt_duration = time.time() - attempt_start
            full_stdout = "".join(stdout_parts)
            full_stderr = "".join(stderr_parts)

            if timed_out:
                status = TaskStatus.FAILED
                if self.log_callback:
                    self.log_callback(task.name, f"Attempt {attempts} timed out after {attempt_duration:.2f}s")
            elif exit_code == 0:
                total_duration = time.time() - start_time
                result = TaskResult(
                    name=task.name,
                    status=TaskStatus.SUCCESS,
                    exit_code=0,
                    duration=total_duration,
                    attempts=attempts,
                    stdout=full_stdout,
                    stderr=full_stderr,
                )
                if self.log_callback:
                    self.log_callback(task.name, f"Succeeded in {attempt_duration:.2f}s (attempt {attempts})")
                return self._extract_outputs(result)
            else:
                status = TaskStatus.FAILED
                if self.log_callback:
                    self.log_callback(
                        task.name,
                        f"Attempt {attempts} failed with exit code {exit_code} after {attempt_duration:.2f}s"
                        + (f" (command: {failed_cmd})" if failed_cmd else "")
                    )

            last_result = TaskResult(
                name=task.name,
                status=TaskStatus.FAILED,
                exit_code=exit_code,
                duration=time.time() - start_time,
                attempts=attempts,
                stdout=full_stdout,
                stderr=full_stderr,
            )

            if attempts < max_attempts:
                backoff = task.retry_backoff * (task.retry_backoff_factor ** (attempts - 1))
                if self.log_callback:
                    self.log_callback(task.name, f"Retrying in {backoff:.1f}s...")
                time.sleep(backoff)

        return self._extract_outputs(last_result)

    def _extract_outputs(self, result: TaskResult) -> TaskResult:
        import re
        pattern = re.compile(r"^##saturn:output\s+(\w+)\s*=\s*(.+)$", re.MULTILINE)
        for match in pattern.finditer(result.stdout):
            key = match.group(1)
            value = match.group(2).strip()
            result.outputs_captured[key] = value
        result.stdout = pattern.sub("", result.stdout).strip()
        return result
