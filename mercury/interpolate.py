"""Variable interpolation for task commands and env values.

Supported forms inside strings:
  ${var.NAME}       -> global workflow variable NAME
  ${env.NAME}       -> os environment variable NAME
  ${task.NAME.stdout} -> captured stdout of upstream task NAME
  ${task.NAME.exit_code} -> exit code of upstream task NAME
"""
from __future__ import annotations

import os
import re
from typing import Any, Dict, Mapping

_PLACEHOLDER = re.compile(r"\$\{([^}]+)\}")


class InterpolationError(ValueError):
    pass


def interpolate(
    value: str,
    variables: Mapping[str, Any],
    task_results: Mapping[str, Mapping[str, Any]],
) -> str:
    if not isinstance(value, str):
        return value

    def replace(match: "re.Match[str]") -> str:
        expr = match.group(1).strip()
        parts = expr.split(".")
        if len(parts) >= 2 and parts[0] == "var":
            key = ".".join(parts[1:])
            if key not in variables:
                raise InterpolationError(f"Unknown variable: {key}")
            return str(variables[key])
        if len(parts) == 2 and parts[0] == "env":
            return os.environ.get(parts[1], "")
        if len(parts) >= 3 and parts[0] == "task":
            tname = parts[1]
            field = ".".join(parts[2:])
            if tname not in task_results:
                raise InterpolationError(
                    f"Reference to task '{tname}' that has not produced a result yet."
                )
            res = task_results[tname]
            if field not in res:
                raise InterpolationError(
                    f"Task '{tname}' has no field '{field}'."
                )
            return str(res[field])
        raise InterpolationError(f"Unsupported placeholder: ${{{expr}}}")

    return _PLACEHOLDER.sub(replace, value)


def interpolate_mapping(
    mapping: Mapping[str, str],
    variables: Mapping[str, Any],
    task_results: Mapping[str, Mapping[str, Any]],
) -> Dict[str, str]:
    return {k: interpolate(v, variables, task_results) for k, v in mapping.items()}
