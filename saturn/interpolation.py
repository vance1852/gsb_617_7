from __future__ import annotations

import os
import re
from typing import Any, Dict, Optional

from .task import TaskResult


class InterpolationError(Exception):
    pass


_PATTERN = re.compile(r"\$\{([^}]+)\}")


class InterpolationContext:
    def __init__(
        self,
        variables: Optional[Dict[str, Any]] = None,
        env: Optional[Dict[str, str]] = None,
        task_results: Optional[Dict[str, TaskResult]] = None,
    ):
        self.variables = variables or {}
        self.env = env or dict(os.environ)
        self.task_results = task_results or {}

    def get(self, key: str) -> str:
        parts = key.split(".")
        if not parts:
            raise InterpolationError(f"Empty interpolation key")

        namespace = parts[0]
        path = parts[1:]

        if namespace == "var" or namespace == "vars" or namespace == "variables":
            return self._lookup(self.variables, path, key)
        elif namespace == "env":
            if not path:
                raise InterpolationError(f"${{env}} requires a variable name: ${{env.VAR_NAME}}")
            var_name = ".".join(path)
            if var_name not in self.env:
                return ""
            return str(self.env[var_name])
        elif namespace == "task" or namespace == "tasks":
            if len(path) < 2:
                raise InterpolationError(f"${{task}} requires task name and field: ${{task.NAME.FIELD}}")
            task_name = path[0]
            field = ".".join(path[1:])
            return self._lookup_task(task_name, field, key)
        else:
            try:
                return self._lookup(self.variables, parts, key)
            except InterpolationError:
                if namespace in self.env:
                    if len(parts) == 1:
                        return str(self.env[namespace])
                raise InterpolationError(f"Unknown namespace in interpolation: {namespace} (in '${{{key}}}')")

    def _lookup(self, data: Dict[str, Any], path: list, full_key: str) -> str:
        current: Any = data
        for part in path:
            if isinstance(current, dict):
                if part not in current:
                    raise InterpolationError(f"Key not found: '{part}' in '${{{full_key}}}'")
                current = current[part]
            else:
                raise InterpolationError(f"Cannot traverse into non-dict at '{part}' in '${{{full_key}}}'")
        return str(current) if current is not None else ""

    def _lookup_task(self, task_name: str, field: str, full_key: str) -> str:
        if task_name not in self.task_results:
            return ""
        result = self.task_results[task_name]

        if field == "stdout":
            return result.stdout.strip()
        elif field == "stderr":
            return result.stderr.strip()
        elif field == "exit_code":
            return str(result.exit_code) if result.exit_code is not None else ""
        elif field == "status":
            return result.status.value
        elif field == "success":
            return "true" if result.success else "false"
        elif field == "output":
            return result.stdout.strip()
        elif field.startswith("outputs.") or field.startswith("output."):
            output_key = field.split(".", 1)[1] if "." in field else ""
            return result.outputs_captured.get(output_key, "")
        elif field in result.outputs_captured:
            return result.outputs_captured[field]
        else:
            raise InterpolationError(f"Unknown task field '{field}' in '${{{full_key}}}'. Valid fields: stdout, stderr, exit_code, status, success, outputs.NAME")


def interpolate_string(template: str, context: InterpolationContext) -> str:
    def replacer(match: re.Match) -> str:
        key = match.group(1).strip()
        try:
            return context.get(key)
        except InterpolationError:
            return match.group(0)

    return _PATTERN.sub(replacer, template)


def interpolate_dict(data: Dict[str, Any], context: InterpolationContext) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in data.items():
        new_key = interpolate_string(str(key), context) if isinstance(key, str) else key
        if isinstance(value, str):
            result[new_key] = interpolate_string(value, context)
        elif isinstance(value, dict):
            result[new_key] = interpolate_dict(value, context)
        elif isinstance(value, list):
            result[new_key] = [
                interpolate_string(v, context) if isinstance(v, str)
                else interpolate_dict(v, context) if isinstance(v, dict)
                else v
                for v in value
            ]
        else:
            result[new_key] = value
    return result
