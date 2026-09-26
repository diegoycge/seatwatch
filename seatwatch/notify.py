"""Mac notifications (osascript) and phone push via ntfy."""
from __future__ import annotations

import json
import subprocess
import urllib.request

from . import db


def mac(title: str, body: str) -> None:
    script = ['-e', 'on run argv', '-e',
              'display notification (item 2 of argv) with title (item 1 of argv) sound name "Glass"',
              '-e', 'end run']
    try:
        subprocess.run(["osascript", *script, title, body], timeout=10, capture_output=True)
    except (OSError, subprocess.SubprocessError):
        pass


def ntfy(settings: dict, title: str, body: str, link: str = "", priority: int = 4) -> str:
    payload = {"topic": settings["ntfy_topic"], "title": title, "message": body,
               "tags": ["airplane"], "priority": priority}
    if link:
        payload["click"] = link
    req = urllib.request.Request(settings["ntfy_server"].rstrip("/") + "/", data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return f"ntfy {r.status}"
    except Exception as e:  # never let a push failure break a job
        return f"ntfy failed: {e}"


def send(conn, title: str, body: str, link: str = "", kind: str = "alert") -> None:
    s = db.get_settings(conn)
    if s.get("mac_notify"):
        mac(title, body)
    result = ""
    if s.get("ntfy_enabled") and s.get("ntfy_topic"):
        result = ntfy(s, title, body, link)
    db.log_event(conn, kind, title, body + (f"\n[{result}]" if result.startswith("ntfy failed") else ""), link)
