from __future__ import annotations

import os
import re
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"
    UP_TO_DATE = "up_to_date"


_VAR_PATTERN = re.compile(
    r"\$\{("
    r"(?:vars|var)\.([a-zA-Z_][a-zA-Z0-9_.]*)"
    r"|env\.([a-zA-Z_][a-zA-Z0-9_]*)"
    r"|tasks\.([a-zA-Z_][a-zA-Z0-9_-]*)\.(output|exit_code|status)"
    r")\}"
)


class InterpolationError(Exception):
    pass


def interpolate(
    text: str,
    variables: Dict[str, Any],
    env: Dict[str, str] | None = None,
    task_results: Dict[str, Dict[str, Any]] | None = None,
) -> str:
    env = env or os.environ
    task_results = task_results or {}

    def _replace(m: re.Match) -> str:
        full = m.group(1)
        if m.group(2) is not None:
            key = m.group(2)
            return _lookup_var(variables, key, full)
        if m.group(3) is not None:
            key = m.group(3)
            val = env.get(key)
            if val is None:
                raise InterpolationError(f"Environment variable '{key}' not found (in ${{{full}}})")
            return val
        if m.group(4) is not None:
            tname = m.group(4)
            field = m.group(5)
            if tname not in task_results:
                raise InterpolationError(
                    f"Task '{tname}' result not available yet (in ${{{full}}})"
                )
            res = task_results[tname]
            if field == "output":
                return str(res.get("output", ""))
            if field == "exit_code":
                return str(res.get("exit_code", ""))
            if field == "status":
                return str(res.get("status", ""))
        return m.group(0)

    return _VAR_PATTERN.sub(_replace, text)


def _lookup_var(variables: Dict[str, Any], dotted: str, full: str) -> str:
    parts = dotted.split(".")
    cur: Any = variables
    for p in parts:
        if isinstance(cur, dict) and p in cur:
            cur = cur[p]
        else:
            raise InterpolationError(f"Variable '{dotted}' not found (in ${{{full}}})")
    return str(cur)


def interpolate_commands(
    commands: List[str],
    variables: Dict[str, Any],
    env: Dict[str, str] | None = None,
    task_results: Dict[str, Dict[str, Any]] | None = None,
) -> List[str]:
    return [interpolate(c, variables, env, task_results) for c in commands]


def evaluate_condition(
    condition: Dict[str, Any] | None,
    variables: Dict[str, Any],
    env: Dict[str, str] | None,
    task_results: Dict[str, Dict[str, Any]] | None,
    base_dir: str | None = None,
) -> bool:
    if condition is None:
        return True

    env = env or os.environ
    task_results = task_results or {}

    if "file_exists" in condition:
        path = interpolate(str(condition["file_exists"]), variables, env, task_results)
        if not os.path.isabs(path) and base_dir:
            path = os.path.join(base_dir, path)
        return Path(path).exists()

    if "file_not_exists" in condition:
        path = interpolate(str(condition["file_not_exists"]), variables, env, task_results)
        if not os.path.isabs(path) and base_dir:
            path = os.path.join(base_dir, path)
        return not Path(path).exists()

    if "var_equals" in condition:
        ve = condition["var_equals"]
        if not isinstance(ve, dict) or "name" not in ve or "value" not in ve:
            raise InterpolationError("condition.var_equals requires 'name' and 'value'")
        name = str(ve["name"])
        expected = str(ve["value"])
        try:
            actual = interpolate(f"${{var.{name}}}", variables, env, task_results)
        except InterpolationError:
            actual = ""
        return actual == expected

    if "env_equals" in condition:
        ee = condition["env_equals"]
        if not isinstance(ee, dict) or "name" not in ee or "value" not in ee:
            raise InterpolationError("condition.env_equals requires 'name' and 'value'")
        actual = env.get(str(ee["name"]), "")
        return actual == str(ee["value"])

    if "task_status" in condition:
        ts = condition["task_status"]
        if not isinstance(ts, dict) or "name" not in ts or "status" not in ts:
            raise InterpolationError("condition.task_status requires 'name' and 'status'")
        tname = str(ts["name"])
        expected_status = str(ts["status"])
        actual_status = str(task_results.get(tname, {}).get("status", TaskStatus.PENDING.value))
        return actual_status == expected_status

    if "task_succeeded" in condition:
        tname = str(condition["task_succeeded"])
        return str(task_results.get(tname, {}).get("status", "")) == TaskStatus.SUCCESS.value

    if "task_failed" in condition:
        tname = str(condition["task_failed"])
        return str(task_results.get(tname, {}).get("status", "")) == TaskStatus.FAILED.value

    if "all_of" in condition:
        items = condition["all_of"]
        if not isinstance(items, list):
            raise InterpolationError("condition.all_of must be a list")
        return all(evaluate_condition(it, variables, env, task_results, base_dir) for it in items)

    if "any_of" in condition:
        items = condition["any_of"]
        if not isinstance(items, list):
            raise InterpolationError("condition.any_of must be a list")
        return any(evaluate_condition(it, variables, env, task_results, base_dir) for it in items)

    if "not" in condition:
        return not evaluate_condition(condition["not"], variables, env, task_results, base_dir)

    raise InterpolationError(f"Unknown condition type: {list(condition.keys())}")
