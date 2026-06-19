from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from .task import FailureStrategy, TaskCondition, TaskConfig, WorkflowConfig

try:
    import tomllib
except ImportError:
    import tomli as tomllib


class ConfigError(Exception):
    pass


def load_config(config_path: str) -> WorkflowConfig:
    path = Path(config_path)
    if not path.exists():
        raise ConfigError(f"Config file not found: {config_path}")

    suffix = path.suffix.lower()
    with open(path, "rb") as f:
        if suffix == ".json":
            data = json.load(f)
        elif suffix in (".toml", ".tml"):
            data = tomllib.load(f)
        else:
            content = f.read()
            try:
                data = json.loads(content)
            except (json.JSONDecodeError, UnicodeDecodeError):
                f.seek(0)
                data = tomllib.load(f)

    return parse_config(data, base_dir=path.parent)


def parse_config(data: Dict[str, Any], base_dir: Optional[Path] = None) -> WorkflowConfig:
    if base_dir is None:
        base_dir = Path.cwd()

    config = WorkflowConfig()

    if "name" in data:
        config.name = str(data["name"])

    if "variables" in data:
        config.variables = dict(data["variables"])

    if "max_concurrency" in data:
        config.max_concurrency = int(data["max_concurrency"])

    if "failure_strategy" in data:
        strategy_str = str(data["failure_strategy"]).lower()
        if strategy_str in ("fail_fast", "fast", "stop"):
            config.failure_strategy = FailureStrategy.FAIL_FAST
        elif strategy_str in ("continue", "continue_on_error", "keep_going"):
            config.failure_strategy = FailureStrategy.CONTINUE
        else:
            raise ConfigError(f"Unknown failure_strategy: {strategy_str}")

    if "state_file" in data:
        config.state_file = _resolve_path(str(data["state_file"]), base_dir)

    if "log_file" in data:
        config.log_file = _resolve_path(str(data["log_file"]), base_dir)

    if "webhook_url" in data:
        config.webhook_url = str(data["webhook_url"])

    if "tasks" not in data:
        raise ConfigError("Config must contain a 'tasks' section")

    tasks_data = data["tasks"]
    if not isinstance(tasks_data, dict):
        raise ConfigError("'tasks' must be a dict/mapping")

    for task_name, task_data in tasks_data.items():
        config.tasks[task_name] = _parse_task(task_name, task_data, base_dir)

    _validate_dependencies(config)
    return config


def _parse_task(name: str, data: Dict[str, Any], base_dir: Path) -> TaskConfig:
    if not isinstance(data, dict):
        raise ConfigError(f"Task '{name}' must be a dict/mapping")

    commands: List[str] = []
    if "command" in data:
        commands.append(str(data["command"]))
    if "commands" in data:
        cmds = data["commands"]
        if isinstance(cmds, str):
            commands.append(cmds)
        elif isinstance(cmds, list):
            commands.extend(str(c) for c in cmds)

    if not commands:
        raise ConfigError(f"Task '{name}' must have 'command' or 'commands'")

    depends_on: List[str] = []
    if "depends_on" in data:
        deps = data["depends_on"]
        if isinstance(deps, str):
            depends_on = [deps] if deps else []
        elif isinstance(deps, list):
            depends_on = [str(d) for d in deps if str(d)]

    cwd = None
    if "cwd" in data and data["cwd"] is not None:
        cwd = _resolve_path(str(data["cwd"]), base_dir)

    env: Dict[str, str] = {}
    if "env" in data and data["env"] is not None:
        if isinstance(data["env"], dict):
            env = {str(k): str(v) for k, v in data["env"].items()}

    timeout = None
    if "timeout" in data and data["timeout"] is not None:
        timeout = float(data["timeout"])

    retries = 0
    if "retries" in data and data["retries"] is not None:
        retries = int(data["retries"])

    retry_backoff = 1.0
    if "retry_backoff" in data and data["retry_backoff"] is not None:
        retry_backoff = float(data["retry_backoff"])

    retry_backoff_factor = 2.0
    if "retry_backoff_factor" in data and data["retry_backoff_factor"] is not None:
        retry_backoff_factor = float(data["retry_backoff_factor"])

    inputs: List[str] = []
    if "inputs" in data and data["inputs"] is not None:
        if isinstance(data["inputs"], str):
            inputs = [data["inputs"]]
        elif isinstance(data["inputs"], list):
            inputs = [str(p) for p in data["inputs"]]

    outputs: List[str] = []
    if "outputs" in data and data["outputs"] is not None:
        if isinstance(data["outputs"], str):
            outputs = [data["outputs"]]
        elif isinstance(data["outputs"], list):
            outputs = [str(p) for p in data["outputs"]]

    conditions: List[TaskCondition] = []
    if "if" in data or "condition" in data or "conditions" in data:
        cond_data = data.get("conditions", data.get("condition", data.get("if")))
        conditions = _parse_conditions(cond_data, name)

    run_on_failure = False
    if "run_on_failure" in data:
        run_on_failure = bool(data["run_on_failure"])

    description = None
    if "description" in data:
        description = str(data["description"])

    return TaskConfig(
        name=name,
        commands=commands,
        depends_on=depends_on,
        cwd=cwd,
        env=env,
        timeout=timeout,
        retries=retries,
        retry_backoff=retry_backoff,
        retry_backoff_factor=retry_backoff_factor,
        inputs=inputs,
        outputs=outputs,
        conditions=conditions,
        run_on_failure=run_on_failure,
        description=description,
    )


def _parse_conditions(cond_data: Any, task_name: str) -> List[TaskCondition]:
    if cond_data is None:
        return []

    if isinstance(cond_data, list):
        result = []
        for item in cond_data:
            result.extend(_parse_conditions(item, task_name))
        return result

    if isinstance(cond_data, str):
        return [TaskCondition(type="expression", params={"expr": cond_data})]

    if isinstance(cond_data, dict):
        if "file_exists" in cond_data:
            return [TaskCondition(type="file_exists", params={"path": str(cond_data["file_exists"])})]
        if "file_not_exists" in cond_data:
            return [TaskCondition(type="file_not_exists", params={"path": str(cond_data["file_not_exists"])})]
        if "var_equals" in cond_data:
            params = cond_data["var_equals"]
            if isinstance(params, dict):
                return [TaskCondition(type="var_equals", params={"name": str(params.get("name", "")), "value": params.get("value")})]
        if "env_equals" in cond_data:
            params = cond_data["env_equals"]
            if isinstance(params, dict):
                return [TaskCondition(type="env_equals", params={"name": str(params.get("name", "")), "value": str(params.get("value", ""))})]
        if "upstream_success" in cond_data:
            tasks = cond_data["upstream_success"]
            if isinstance(tasks, str):
                tasks = [tasks]
            return [TaskCondition(type="upstream_success", params={"tasks": [str(t) for t in tasks]})]
        if "upstream_failed" in cond_data:
            tasks = cond_data["upstream_failed"]
            if isinstance(tasks, str):
                tasks = [tasks]
            return [TaskCondition(type="upstream_failed", params={"tasks": [str(t) for t in tasks]})]
        if "expr" in cond_data:
            return [TaskCondition(type="expression", params={"expr": str(cond_data["expr"])})]

    raise ConfigError(f"Invalid condition in task '{task_name}': {cond_data}")


def _resolve_path(path_str: str, base_dir: Path) -> str:
    path = Path(path_str)
    if path.is_absolute():
        return str(path)
    return str((base_dir / path).resolve())


def _validate_dependencies(config: WorkflowConfig) -> None:
    task_names = set(config.tasks.keys())
    for name, task in config.tasks.items():
        for dep in task.depends_on:
            if dep not in task_names:
                raise ConfigError(f"Task '{name}' depends on unknown task '{dep}'")
            if dep == name:
                raise ConfigError(f"Task '{name}' cannot depend on itself")
