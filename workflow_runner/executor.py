from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, Future
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .config import TaskConfig
from .logger import get_logger


@dataclass
class TaskResult:
    name: str
    status: str
    exit_code: Optional[int] = None
    output: str = ""
    stdout: str = ""
    stderr: str = ""
    duration: float = 0.0
    attempts: int = 0
    timed_out: bool = False
    error: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    env: Dict[str, str] = field(default_factory=dict)


def _kill_process_tree(proc: subprocess.Popen) -> None:
    try:
        if os.name == "nt":
            try:
                subprocess.call(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=3,
                )
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        else:
            try:
                pgid = os.getpgid(proc.pid)
                os.killpg(pgid, signal.SIGKILL)
            except OSError:
                try:
                    proc.kill()
                except Exception:
                    pass
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _run_single_command(
    cmd: str,
    cwd: Optional[str],
    env: Dict[str, str],
    timeout: Optional[float],
    shell_executable: Optional[str] = None,
) -> tuple[int, str, str, bool]:
    merged_env = os.environ.copy()
    merged_env.update(env)

    creationflags = 0
    preexec_fn = None
    if os.name != "nt":
        preexec_fn = getattr(os, "setsid", None)
    else:
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP

    try:
        proc = subprocess.Popen(
            cmd,
            shell=True,
            cwd=cwd,
            env=merged_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            executable=shell_executable,
            preexec_fn=preexec_fn,
            creationflags=creationflags,
            text=True,
        )
    except Exception as e:
        return -1, "", str(e), False

    timed_out = False
    try:
        out, err = proc.communicate(timeout=timeout)
        rc = proc.returncode
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_process_tree(proc)
        try:
            out, err = proc.communicate(timeout=5)
        except Exception:
            out, err = "", ""
        rc = proc.returncode if proc.returncode is not None else -1
    return rc, out or "", err or "", timed_out


def run_task(
    tc: TaskConfig,
    commands: List[str],
    extra_env: Optional[Dict[str, str]] = None,
    base_cwd: Optional[str] = None,
    on_log: Optional[Callable[[str, str], None]] = None,
) -> TaskResult:
    log = get_logger()
    cwd = tc.cwd or base_cwd

    env = dict(extra_env or {})
    env.update(tc.env)

    result = TaskResult(
        name=tc.name,
        status="pending",
        started_at=time.strftime("%Y-%m-%d %H:%M:%S"),
    )

    max_attempts = tc.retries + 1
    backoff = tc.retry_backoff

    for attempt in range(1, max_attempts + 1):
        result.attempts = attempt
        all_stdout: List[str] = []
        all_stderr: List[str] = []
        combined_output: List[str] = []
        attempt_ok = True
        attempt_timed_out = False
        attempt_rc: Optional[int] = None
        attempt_error: Optional[str] = None
        t0 = time.monotonic()

        if on_log:
            on_log(tc.name, f"attempt {attempt}/{max_attempts} starting")

        for idx, cmd in enumerate(commands, 1):
            if on_log:
                on_log(tc.name, f"  [{idx}/{len(commands)}] $ {cmd}")
            log.debug("[%s] $ %s", tc.name, cmd)

            rc, out, err, timed_out = _run_single_command(
                cmd, cwd=cwd, env=env, timeout=tc.timeout
            )

            if out:
                all_stdout.append(out)
                combined_output.append(out)
                for line in out.rstrip().splitlines():
                    log.debug("[%s|out] %s", tc.name, line)
            if err:
                all_stderr.append(err)
                combined_output.append(err)
                for line in err.rstrip().splitlines():
                    log.debug("[%s|err] %s", tc.name, line)

            if timed_out:
                attempt_timed_out = True
                attempt_ok = False
                attempt_rc = rc
                msg = f"command timed out after {tc.timeout}s: {cmd}"
                attempt_error = msg
                if on_log:
                    on_log(tc.name, f"  TIMEOUT: {msg}")
                log.error("[%s] %s", tc.name, msg)
                break

            if rc != 0:
                attempt_ok = False
                attempt_rc = rc
                msg = f"command exited with code {rc}: {cmd}"
                attempt_error = msg
                if on_log:
                    on_log(tc.name, f"  FAILED (exit {rc}): {cmd}")
                log.error("[%s] %s", tc.name, msg)
                break

        duration = time.monotonic() - t0
        result.duration = duration
        result.env = env
        result.exit_code = attempt_rc
        result.timed_out = attempt_timed_out
        result.stdout = "\n".join(all_stdout)
        result.stderr = "\n".join(all_stderr)
        result.output = "\n".join(combined_output).strip()

        if attempt_ok:
            result.status = "success"
            result.finished_at = time.strftime("%Y-%m-%d %H:%M:%S")
            result.error = None
            if on_log:
                on_log(tc.name, f"attempt {attempt} succeeded in {duration:.2f}s")
            return result

        result.status = "failed"
        result.error = attempt_error

        if attempt < max_attempts:
            wait = backoff
            if on_log:
                on_log(tc.name, f"retrying in {wait:.2f}s (backoff)...")
            log.warning("[%s] attempt %d failed, retrying in %.2fs", tc.name, attempt, wait)
            time.sleep(wait)
            backoff *= tc.retry_backoff_factor

    result.finished_at = time.strftime("%Y-%m-%d %H:%M:%S")
    return result


class Executor:
    def __init__(self, max_concurrency: int = 1):
        self.max_concurrency = max(1, max_concurrency)
        self._pool = ThreadPoolExecutor(max_workers=self.max_concurrency)
        self._shutdown = False

    def submit(self, fn: Callable[..., TaskResult], *args, **kwargs) -> Future:
        return self._pool.submit(fn, *args, **kwargs)

    def shutdown(self, wait: bool = True) -> None:
        if not self._shutdown:
            self._pool.shutdown(wait=wait)
            self._shutdown = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.shutdown(wait=True)
        return False
