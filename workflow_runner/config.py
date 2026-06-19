from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class TaskConfig:
    name: str
    commands: List[str]
    depends_on: List[str] = field(default_factory=list)
    timeout: Optional[float] = None
    retries: int = 0
    retry_backoff: float = 1.0
    retry_backoff_factor: float = 2.0
    cwd: Optional[str] = None
    env: Dict[str, str] = field(default_factory=dict)
    inputs: List[str] = field(default_factory=list)
    outputs: List[str] = field(default_factory=list)
    condition: Optional[Dict[str, Any]] = None
    fail_fast: bool = True


@dataclass
class WorkflowConfig:
    version: str = "1"
    name: str = "workflow"
    max_concurrency: int = 1
    fail_fast: bool = True
    variables: Dict[str, Any] = field(default_factory=dict)
    env: Dict[str, str] = field(default_factory=dict)
    state_file: Optional[str] = None
    log_file: Optional[str] = None
    webhook: Optional[str] = None
    tasks: Dict[str, TaskConfig] = field(default_factory=dict)


class ConfigError(Exception):
    pass


def _load_raw(path: Path) -> Dict[str, Any]:
    suffix = path.suffix.lower()
    text = path.read_text(encoding="utf-8")
    if suffix == ".json":
        return json.loads(text)
    if suffix in (".toml", ".tml"):
        try:
            import tomllib
        except ImportError:
            try:
                import tomli as tomllib
            except ImportError:
                raise ConfigError(
                    "TOML config requires Python 3.11+ (built-in tomllib) or the 'tomli' package"
                )
        return tomllib.loads(text)
    raise ConfigError(f"Unsupported config format: {suffix}. Use .json or .toml")


def _parse_task(name: str, raw: Dict[str, Any]) -> TaskConfig:
    if "commands" not in raw and "command" not in raw:
        raise ConfigError(f"Task '{name}' must have 'commands' (list) or 'command' (string)")

    commands = raw.get("commands")
    if commands is None:
        commands = [raw["command"]]
    if isinstance(commands, str):
        commands = [commands]
    if not isinstance(commands, list):
        raise ConfigError(f"Task '{name}': commands must be a list of strings")

    depends_on = raw.get("depends_on", raw.get("deps", []))
    if isinstance(depends_on, str):
        depends_on = [depends_on]
    if not isinstance(depends_on, list):
        raise ConfigError(f"Task '{name}': depends_on must be a list")

    env = raw.get("env", {})
    if not isinstance(env, dict):
        raise ConfigError(f"Task '{name}': env must be a dict")

    inputs = raw.get("inputs", raw.get("input", []))
    if isinstance(inputs, str):
        inputs = [inputs]
    outputs = raw.get("outputs", raw.get("output", []))
    if isinstance(outputs, str):
        outputs = [outputs]

    condition = raw.get("condition", None)
    if condition is not None and not isinstance(condition, dict):
        raise ConfigError(f"Task '{name}': condition must be a dict")

    return TaskConfig(
        name=name,
        commands=[str(c) for c in commands],
        depends_on=[str(d) for d in depends_on],
        timeout=raw.get("timeout"),
        retries=int(raw.get("retries", 0)),
        retry_backoff=float(raw.get("retry_backoff", 1.0)),
        retry_backoff_factor=float(raw.get("retry_backoff_factor", 2.0)),
        cwd=raw.get("cwd"),
        env={str(k): str(v) for k, v in env.items()},
        inputs=[str(i) for i in inputs],
        outputs=[str(o) for o in outputs],
        condition=condition,
        fail_fast=bool(raw.get("fail_fast", True)),
    )


def load_config(path: str | os.PathLike) -> WorkflowConfig:
    p = Path(path).resolve()
    if not p.exists():
        raise ConfigError(f"Config file not found: {p}")

    raw = _load_raw(p)
    base_dir = p.parent

    tasks_raw = raw.get("tasks", {})
    if not isinstance(tasks_raw, dict):
        raise ConfigError("'tasks' must be a dict mapping task name -> task definition")

    tasks = {name: _parse_task(name, td) for name, td in tasks_raw.items()}

    variables = raw.get("variables", raw.get("vars", {}))
    if not isinstance(variables, dict):
        raise ConfigError("'variables' must be a dict")

    env = raw.get("env", {})
    if not isinstance(env, dict):
        raise ConfigError("'env' must be a dict")

    cfg = WorkflowConfig(
        version=str(raw.get("version", "1")),
        name=str(raw.get("name", p.stem)),
        max_concurrency=int(raw.get("max_concurrency", 1)),
        fail_fast=bool(raw.get("fail_fast", True)),
        variables={str(k): v for k, v in variables.items()},
        env={str(k): str(v) for k, v in env.items()},
        state_file=raw.get("state_file"),
        log_file=raw.get("log_file"),
        webhook=raw.get("webhook"),
        tasks=tasks,
    )

    for name, tc in tasks.items():
        for dep in tc.depends_on:
            if dep not in tasks:
                raise ConfigError(f"Task '{name}' depends on unknown task '{dep}'")
        if tc.cwd:
            resolved = (base_dir / tc.cwd).resolve() if not os.path.isabs(tc.cwd) else Path(tc.cwd)
            tc.cwd = str(resolved)

    return cfg
