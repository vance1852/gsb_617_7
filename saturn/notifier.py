from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional
from urllib import request, error

from .executor import ExecutionStats
from .task import TaskResult


def send_webhook_notification(
    url: str,
    workflow_name: str,
    stats: ExecutionStats,
    task_results: Dict[str, TaskResult],
    extra_data: Optional[Dict[str, Any]] = None,
    timeout: float = 10.0,
) -> bool:
    logger = logging.getLogger("saturn.notifier")

    tasks_data: Dict[str, Any] = {}
    for name, result in task_results.items():
        tasks_data[name] = {
            "status": result.status.value,
            "exit_code": result.exit_code,
            "duration": round(result.duration, 3),
            "attempts": result.attempts,
            "skipped_reason": result.skipped_reason,
        }

    payload: Dict[str, Any] = {
        "workflow": workflow_name,
        "success": stats.ok,
        "stats": {
            "total": stats.total,
            "success": stats.success,
            "failed": stats.failed,
            "skipped": stats.skipped,
            "up_to_date": stats.up_to_date,
            "total_duration": round(stats.total_duration, 3),
        },
        "tasks": tasks_data,
    }

    if extra_data:
        payload.update(extra_data)

    try:
        data = json.dumps(payload).encode("utf-8")
        req = request.Request(
            url,
            data=data,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "Saturn-Workflow/0.1.0",
            },
            method="POST",
        )
        with request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            logger.info(f"Webhook notification sent, status={status}")
            return 200 <= status < 300
    except error.URLError as e:
        logger.warning(f"Failed to send webhook notification: {e}")
        return False
    except Exception as e:
        logger.warning(f"Webhook notification error: {e}")
        return False
