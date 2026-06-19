from __future__ import annotations

import os
import re
from typing import Any

from .models import TaskResult, TaskStatus

_PATTERN = re.compile(r"\{\{\s*([^}]+?)\s*\}\}")


def _resolve_key(
    key: str,
    variables: dict[str, Any],
    env: dict[str, str],
    results: dict[str, TaskResult],
) -> Any:
    key = key.strip()

    if key.startswith("env."):
        var_name = key[4:]
        return env.get(var_name, os.environ.get(var_name, ""))

    if key.startswith("tasks."):
        parts = key[6:].split(".", 1)
        task_name = parts[0]
        field = parts[1] if len(parts) > 1 else "stdout"
        result = results.get(task_name)
        if result is None:
            return ""
        if field == "stdout":
            return result.stdout.strip()
        if field == "stderr":
            return result.stderr.strip()
        if field == "exit_code":
            return result.exit_code if result.exit_code is not None else ""
        if field == "status":
            return result.status.value
        if field == "output":
            return result.outputs.get("value", "")
        if field.startswith("outputs."):
            output_key = field[8:]
            return result.outputs.get(output_key, "")
        return ""

    return variables.get(key, "")


def interpolate(
    text: str,
    variables: dict[str, Any],
    env: dict[str, str],
    results: dict[str, TaskResult],
) -> str:
    def replace(match: re.Match[str]) -> str:
        expr = match.group(1)
        try:
            value = _resolve_key(expr, variables, env, results)
            return str(value)
        except Exception:
            return match.group(0)

    return _PATTERN.sub(replace, text)


def interpolate_all(
    items: list[str],
    variables: dict[str, Any],
    env: dict[str, str],
    results: dict[str, TaskResult],
) -> list[str]:
    return [interpolate(s, variables, env, results) for s in items]


def _eval_condition(
    cond: Any,
    variables: dict[str, Any],
    env: dict[str, str],
    results: dict[str, TaskResult],
) -> bool:
    if cond is None:
        return True

    from .models import TaskCondition

    if not isinstance(cond, TaskCondition):
        return True

    import glob
    from pathlib import Path

    if cond.file_exists:
        pattern = interpolate(cond.file_exists, variables, env, results)
        matches = glob.glob(pattern)
        if not matches:
            return False

    if cond.file_not_exists:
        pattern = interpolate(cond.file_not_exists, variables, env, results)
        matches = glob.glob(pattern)
        if matches:
            return False

    if cond.var_eq:
        for k, expected in cond.var_eq.items():
            k_interp = interpolate(k, variables, env, results)
            actual = _resolve_key(k_interp, variables, env, results)
            if str(actual) != str(expected):
                return False

    if cond.var_ne:
        for k, expected in cond.var_ne.items():
            k_interp = interpolate(k, variables, env, results)
            actual = _resolve_key(k_interp, variables, env, results)
            if str(actual) == str(expected):
                return False

    if cond.upstream_status:
        for task_name, expected_status in cond.upstream_status.items():
            result = results.get(task_name)
            actual_status = result.status.value if result else TaskStatus.PENDING.value
            if actual_status != expected_status:
                return False

    return True


def should_run(
    condition: Any,
    variables: dict[str, Any],
    env: dict[str, str],
    results: dict[str, TaskResult],
) -> tuple[bool, str | None]:
    if condition is None:
        return True, None
    ok = _eval_condition(condition, variables, env, results)
    if ok:
        return True, None
    return False, "condition not met"
