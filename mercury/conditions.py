"""Conditional execution evaluator for task `when` clauses.

Supported `when` shapes (all optional, combined with logical AND):

  {"file_exists": "path/to/file"}
  {"file_missing": "path/to/file"}
  {"var_equals": {"name": "X", "value": "Y"}}
  {"upstream_succeeded": ["task_a", "task_b"]}
  {"upstream_failed": ["task_a"]}
  {"all": [<cond>, <cond>, ...]}
  {"any": [<cond>, <cond>, ...]}
  {"not": <cond>}
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Optional


class ConditionError(ValueError):
    pass


def evaluate(
    cond: Optional[Mapping[str, Any]],
    variables: Mapping[str, Any],
    task_results: Mapping[str, Mapping[str, Any]],
    base_dir: Path,
) -> bool:
    if cond is None:
        return True
    if not isinstance(cond, Mapping):
        raise ConditionError(f"Condition must be a mapping, got {type(cond).__name__}")

    # Combinators
    if "all" in cond:
        return all(evaluate(c, variables, task_results, base_dir) for c in cond["all"])
    if "any" in cond:
        return any(evaluate(c, variables, task_results, base_dir) for c in cond["any"])
    if "not" in cond:
        return not evaluate(cond["not"], variables, task_results, base_dir)

    result = True

    if "file_exists" in cond:
        p = (base_dir / str(cond["file_exists"])).resolve()
        result = result and p.exists()
    if "file_missing" in cond:
        p = (base_dir / str(cond["file_missing"])).resolve()
        result = result and not p.exists()
    if "var_equals" in cond:
        spec = cond["var_equals"]
        if not isinstance(spec, Mapping) or "name" not in spec or "value" not in spec:
            raise ConditionError("var_equals requires 'name' and 'value'.")
        actual = variables.get(spec["name"])
        result = result and (str(actual) == str(spec["value"]))
    if "upstream_succeeded" in cond:
        names = cond["upstream_succeeded"]
        if isinstance(names, str):
            names = [names]
        for n in names:
            r = task_results.get(n)
            result = result and bool(r and r.get("status") == "success")
    if "upstream_failed" in cond:
        names = cond["upstream_failed"]
        if isinstance(names, str):
            names = [names]
        for n in names:
            r = task_results.get(n)
            result = result and bool(r and r.get("status") == "failed")

    return result
