from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, List

from .interpolation import InterpolationContext, interpolate_string
from .task import TaskCondition, TaskResult, TaskStatus


class ConditionError(Exception):
    pass


def evaluate_conditions(
    conditions: List[TaskCondition],
    context: InterpolationContext,
    task_results: Dict[str, TaskResult],
    base_dir: str,
) -> tuple[bool, str]:
    for cond in conditions:
        try:
            ok, reason = _evaluate_single(cond, context, task_results, base_dir)
            if not ok:
                return False, reason
        except Exception as e:
            return False, f"Condition error ({cond.type}): {e}"
    return True, ""


def _evaluate_single(
    cond: TaskCondition,
    context: InterpolationContext,
    task_results: Dict[str, TaskResult],
    base_dir: str,
) -> tuple[bool, str]:
    cond_type = cond.type
    params = cond.params

    if cond_type == "file_exists":
        path_str = interpolate_string(params.get("path", ""), context)
        path = Path(path_str)
        if not path.is_absolute():
            path = Path(base_dir) / path
        if not path.exists():
            return False, f"File not found: {path}"
        return True, ""

    elif cond_type == "file_not_exists":
        path_str = interpolate_string(params.get("path", ""), context)
        path = Path(path_str)
        if not path.is_absolute():
            path = Path(base_dir) / path
        if path.exists():
            return False, f"File exists: {path}"
        return True, ""

    elif cond_type == "var_equals":
        name = params.get("name", "")
        expected = params.get("value")
        try:
            actual = context.get(f"var.{name}")
        except Exception:
            actual = ""
        expected_str = str(expected)
        if actual != expected_str:
            return False, f"Variable '{name}' is '{actual}', expected '{expected_str}'"
        return True, ""

    elif cond_type == "env_equals":
        name = params.get("name", "")
        expected = str(params.get("value", ""))
        actual = os.environ.get(name, "")
        if actual != expected:
            return False, f"Env '{name}' is '{actual}', expected '{expected}'"
        return True, ""

    elif cond_type == "upstream_success":
        tasks = params.get("tasks", [])
        for t in tasks:
            t_name = interpolate_string(str(t), context)
            if t_name not in task_results:
                return False, f"Upstream task '{t_name}' has not run"
            result = task_results[t_name]
            if not result.success:
                return False, f"Upstream task '{t_name}' did not succeed (status: {result.status.value})"
        return True, ""

    elif cond_type == "upstream_failed":
        tasks = params.get("tasks", [])
        any_failed = False
        for t in tasks:
            t_name = interpolate_string(str(t), context)
            if t_name in task_results and task_results[t_name].status == TaskStatus.FAILED:
                any_failed = True
                break
        if not any_failed:
            return False, f"None of the upstream tasks {tasks} failed"
        return True, ""

    elif cond_type == "expression":
        expr = interpolate_string(params.get("expr", ""), context)
        if not _eval_expression(expr, context):
            return False, f"Expression evaluated to false: {expr}"
        return True, ""

    else:
        raise ConditionError(f"Unknown condition type: {cond_type}")


def _eval_expression(expr: str, context: InterpolationContext) -> bool:
    expr = expr.strip()
    if not expr:
        return True

    lower_expr = expr.lower()
    if lower_expr in ("true", "1", "yes", "on"):
        return True
    if lower_expr in ("false", "0", "no", "off", ""):
        return False

    for op in ("==", "!=", ">=", "<=", ">", "<"):
        if op in expr:
            parts = expr.split(op, 1)
            if len(parts) == 2:
                left = parts[0].strip().strip("'\"")
                right = parts[1].strip().strip("'\"")
                try:
                    left_val = context.get(left)
                except Exception:
                    left_val = left
                try:
                    right_val = context.get(right)
                except Exception:
                    right_val = right
                return _compare(left_val, op, right_val)

    try:
        val = context.get(expr)
        return bool(val) and val.lower() not in ("false", "0", "", "no", "off")
    except Exception:
        return bool(expr)


def _compare(left: str, op: str, right: str) -> bool:
    try:
        l_num = float(left)
        r_num = float(right)
        if op == "==":
            return l_num == r_num
        if op == "!=":
            return l_num != r_num
        if op == ">":
            return l_num > r_num
        if op == "<":
            return l_num < r_num
        if op == ">=":
            return l_num >= r_num
        if op == "<=":
            return l_num <= r_num
    except (ValueError, TypeError):
        pass

    if op == "==":
        return left == right
    if op == "!=":
        return left != right
    return False
