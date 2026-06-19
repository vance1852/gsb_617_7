from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .models import (
    FailureStrategy,
    TaskCondition,
    TaskConfig,
    WorkflowConfig,
)


def _load_file(path: Path) -> dict[str, Any]:
    suffix = path.suffix.lower()
    text = path.read_text(encoding="utf-8-sig")
    if suffix == ".json":
        return json.loads(text)
    if suffix in (".toml", ".tml"):
        try:
            import tomllib
        except ImportError:
            import tomli as tomllib
        return tomllib.loads(text)
    raise ValueError(f"Unsupported config format: {suffix}")


def _parse_condition(raw: dict[str, Any] | None) -> TaskCondition | None:
    if not raw:
        return None
    return TaskCondition(
        file_exists=raw.get("file_exists"),
        file_not_exists=raw.get("file_not_exists"),
        var_eq=raw.get("var_eq"),
        var_ne=raw.get("var_ne"),
        upstream_status=raw.get("upstream_status"),
    )


def _parse_task(name: str, raw: dict[str, Any]) -> TaskConfig:
    commands = raw.get("commands", [])
    if isinstance(commands, str):
        commands = [commands]
    return TaskConfig(
        name=name,
        commands=commands,
        depends_on=raw.get("depends_on", []),
        cwd=raw.get("cwd"),
        env=raw.get("env", {}),
        timeout=raw.get("timeout"),
        retries=raw.get("retries", 0),
        retry_backoff=raw.get("retry_backoff", 1.0),
        retry_backoff_factor=raw.get("retry_backoff_factor", 2.0),
        inputs=raw.get("inputs", []),
        outputs=raw.get("outputs", []),
        condition=_parse_condition(raw.get("condition")),
    )


def load_config(path: str | os.PathLike) -> WorkflowConfig:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Config file not found: {p}")

    raw = _load_file(p)
    base_dir = p.parent.resolve()

    raw_tasks = raw.get("tasks", {})
    tasks: dict[str, TaskConfig] = {}
    for name, t_raw in raw_tasks.items():
        tasks[name] = _parse_task(name, t_raw)

    strategy_str = raw.get("failure_strategy", "fail_fast")
    strategy = FailureStrategy(strategy_str) if isinstance(strategy_str, str) else FailureStrategy.FAIL_FAST

    return WorkflowConfig(
        tasks=tasks,
        variables=raw.get("variables", {}),
        env=raw.get("env", {}),
        max_concurrency=raw.get("max_concurrency", 4),
        failure_strategy=strategy,
        state_file=raw.get("state_file", ".jupiter_state.json"),
        log_file=raw.get("log_file"),
        webhook_url=raw.get("webhook_url"),
    )
