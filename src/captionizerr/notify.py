"""ntfy notifications (JSON publish to the server's base URL)."""
from __future__ import annotations

import json
import logging
import urllib.request

log = logging.getLogger("captionizerr.notify")


def ntfy(url: str, topic: str, title: str, message: str, priority: int = 3,
         tags: list[str] | None = None, click: str | None = None) -> bool:
    if not url or not topic:
        return False
    body = {"topic": topic, "title": title, "message": message[:3800], "priority": priority, "tags": tags or []}
    if click:
        body["click"] = click
    req = urllib.request.Request(url.rstrip("/"), data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return 200 <= r.status < 300
    except Exception as e:   # notifications never break a job
        log.warning("ntfy publish failed: %s", e)
        return False
