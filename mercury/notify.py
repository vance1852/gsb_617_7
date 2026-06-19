"""Send workflow results to a configured webhook."""
from __future__ import annotations

import json
from typing import Any, Dict, Optional
from urllib import request, error


def post_json(url: str, payload: Dict[str, Any], timeout: float = 10.0) -> Optional[int]:
    data = json.dumps(payload).encode("utf-8")
    req = request.Request(
        url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            return resp.status
    except error.URLError:
        return None
    except Exception:  # pragma: no cover
        return None
