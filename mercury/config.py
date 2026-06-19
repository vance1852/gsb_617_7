"""Configuration loading and data models for workflows."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

try:
    import tomllib  # Python 3.11+
except ImportError:  # pragma: no cover
    try:
        import tomli as tomllib  # type: ignore
    except ImportError:
        tomllib = None  # type: ignore


@dataclass
class RetryPolicy:
    max_attempts: int = 1
    backoff_initial: float = 1.0
    backoff_factor: float = 2.0
    backoff_max: float = 60.0


@dataclass
class TaskConfig:
    name: str
    commands: List[str]
    depends_on: List[str] = field(default_factory=list)
    cwd: Optional[str] = None
    env: Dict[str, str] = field(default_factory=dict)
    timeout: Optional[float] = None
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    inputs: List[str] = field(default_factory=list)
    outputs: List[str] = field(default_factory=list)
    when: Optional[Dict[str, Any]] = None
    capture_output: bool = True
    description: str = ""


@dataclass
class WorkflowConfig:
    name: str = "workflow"
    variables: Dict[str, Any] = field(default_factory=dict)
    tasks: List[TaskConfig] = field(default_factory=list)
    max_parallel: int = 4
    fail_strategy: str = "fail-fast"  # or "continue-on-error"
    state_file: str = ".mercury/state.json"
    log_file: str = ".mercury/run.log"
    cache_file: str = ".mercury/cache.json"
    webhook: Optional[str] = None
    workdir: Optional[str] = None


class ConfigError(ValueError):
    pass


def _load_raw(path: Path) -> Dict[str, Any]:
    suffix = path.suffix.lower()
    text = path.read_text(encoding="utf-8")
    if suffix in (".json",):
        return json.loads(text)
    if suffix in (".toml",):
        if tomllib is None:
            raise ConfigError(
                "TOML support requires Python 3.11+ or the 'tomli' package."
            )
        return tomllib.loads(text)
    raise ConfigError(f"Unsupported config format: {suffix}")


def _build_retry(data: Any) -> RetryPolicy:
    if data is None:
        return RetryPolicy()
    if isinstance(data, int):
        return RetryPolicy(max_attempts=max(1, data))
    if isinstance(data, dict):
        return RetryPolicy(
            max_attempts=int(data.get("max_attempts", data.get("attempts", 1))),
            backoff_initial=float(data.get("backoff_initial", 1.0)),
            backoff_factor=float(data.get("backoff_factor", 2.0)),
            backoff_max=float(data.get("backoff_max", 60.0)),
        )
    raise ConfigError(f"Invalid retry value: {data!r}")


def _build_task(name: str, raw: Dict[str, Any]) -> TaskConfig:
    if "commands" in raw:
        commands = raw["commands"]
    elif "command" in raw:
        commands = raw["command"]
    elif "run" in raw:
        commands = raw["run"]
    else:
        raise ConfigError(f"Task '{name}' must define 'commands' / 'command' / 'run'.")

    if isinstance(commands, str):
        commands = [commands]
    if not isinstance(commands, list) or not all(isinstance(c, str) for c in commands):
        raise ConfigError(f"Task '{name}': commands must be a string or list of strings.")

    depends_on = raw.get("depends_on", raw.get("deps", []))
    if isinstance(depends_on, str):
        depends_on = [depends_on]

    env = raw.get("env", {}) or {}
    if not isinstance(env, dict):
        raise ConfigError(f"Task '{name}': env must be a mapping.")

    inputs = raw.get("inputs", []) or []
    outputs = raw.get("outputs", []) or []
    if isinstance(inputs, str):
        inputs = [inputs]
    if isinstance(outputs, str):
        outputs = [outputs]

    timeout = raw.get("timeout")
    if timeout is not None:
        timeout = float(timeout)

    return TaskConfig(
        name=name,
        commands=list(commands),
        depends_on=list(depends_on),
        cwd=raw.get("cwd"),
        env={str(k): str(v) for k, v in env.items()},
        timeout=timeout,
        retry=_build_retry(raw.get("retry")),
        inputs=list(inputs),
        outputs=list(outputs),
        when=raw.get("when"),
        capture_output=bool(raw.get("capture_output", True)),
        description=str(raw.get("description", "")),
    )


def load_workflow(path: Union[str, Path]) -> WorkflowConfig:
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")
    raw = _load_raw(path)

    tasks_raw = raw.get("tasks", {})
    tasks: List[TaskConfig] = []
    if isinstance(tasks_raw, dict):
        for name, t in tasks_raw.items():
            if not isinstance(t, dict):
                raise ConfigError(f"Task '{name}' definition must be a mapping.")
            tasks.append(_build_task(name, t))
    elif isinstance(tasks_raw, list):
        for t in tasks_raw:
            if not isinstance(t, dict) or "name" not in t:
                raise ConfigError("Each task entry must be a mapping with 'name'.")
            tasks.append(_build_task(t["name"], t))
    else:
        raise ConfigError("'tasks' must be a mapping or a list.")

    if not tasks:
        raise ConfigError("Workflow has no tasks defined.")

    seen = set()
    for t in tasks:
        if t.name in seen:
            raise ConfigError(f"Duplicate task name: {t.name}")
        seen.add(t.name)

    wf = WorkflowConfig(
        name=str(raw.get("name", path.stem)),
        variables=dict(raw.get("variables", {}) or {}),
        tasks=tasks,
        max_parallel=int(raw.get("max_parallel", 4)),
        fail_strategy=str(raw.get("fail_strategy", "fail-fast")),
        state_file=str(raw.get("state_file", ".mercury/state.json")),
        log_file=str(raw.get("log_file", ".mercury/run.log")),
        cache_file=str(raw.get("cache_file", ".mercury/cache.json")),
        webhook=raw.get("webhook"),
        workdir=raw.get("workdir"),
    )

    if wf.fail_strategy not in ("fail-fast", "continue-on-error"):
        raise ConfigError(f"Invalid fail_strategy: {wf.fail_strategy}")
    if wf.max_parallel < 1:
        raise ConfigError("max_parallel must be >= 1")

    return wf
