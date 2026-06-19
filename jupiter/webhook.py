from __future__ import annotations

import json
import urllib.request
import urllib.error
from typing import Any

from .logger import Logger
from .models import TaskResult, TaskStatus


def send_webhook(
    url: str,
    results: dict[str, TaskResult],
    total_duration: float,
    logger: Logger,
) -> None:
    counts = {s.value: 0 for s in TaskStatus}
    for r in results.values():
        counts[r.status.value] += 1

    payload: dict[str, Any] = {
        "summary": {
            "total_duration": round(total_duration, 3),
            "success": counts["success"],
            "failed": counts["failed"],
            "skipped": counts["skipped"],
            "up_to_date": counts["up_to_date"],
        },
        "tasks": {
            name: {
                "status": r.status.value,
                "exit_code": r.exit_code,
                "duration": round(r.duration, 3),
                "attempts": r.attempts,
                "stdout": r.stdout[-2000:] if r.stdout else "",
                "stderr": r.stderr[-2000:] if r.stderr else "",
            }
            for name, r in results.items()
        },
    }

    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            logger.debug(f"Webhook delivered, status={resp.status}")
    except Exception as e:
        logger.warning(f"Webhook delivery failed: {e}")
