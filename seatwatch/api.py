"""seats.aero partner API client with quota accounting.

Every call is recorded in api_calls together with the X-RateLimit headers, so the
scheduler always budgets from the quota seats.aero reports rather than a guess.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from . import config


class ApiError(Exception):
    pass


class BudgetExhausted(ApiError):
    pass


def _next_utc_midnight(now: float) -> float:
    d = datetime.fromtimestamp(now, timezone.utc).date() + timedelta(days=1)
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp()


def budget(conn) -> dict:
    """Current quota state: remaining calls, reset time, calls used this quota day."""
    now = time.time()
    last = conn.execute(
        "SELECT remaining, reset_at, ts FROM api_calls WHERE remaining IS NOT NULL ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    if last and last["reset_at"] and now < last["reset_at"]:
        remaining, reset_at = last["remaining"], last["reset_at"]
    else:
        remaining, reset_at = config.DAILY_LIMIT, _next_utc_midnight(now)
    day_start = reset_at - 86400
    used = conn.execute("SELECT COUNT(*) FROM api_calls WHERE ts >= ? AND status = 200", (day_start,)).fetchone()[0]
    by_purpose = {r[0]: r[1] for r in conn.execute(
        "SELECT substr(purpose, 1, instr(purpose || ':', ':') - 1), COUNT(*) FROM api_calls "
        "WHERE ts >= ? AND status = 200 GROUP BY 1", (day_start,))}
    return {"limit": config.DAILY_LIMIT, "remaining": remaining, "reset_at": reset_at,
            "seconds_to_reset": max(0, reset_at - now), "used": used, "by_purpose": by_purpose}


def _key() -> str:
    try:
        return config.KEY_PATH.read_text().strip()
    except OSError as e:
        raise ApiError(f"cannot read API key at {config.KEY_PATH}: {e}")


def get(conn, path: str, params: dict, purpose: str, floor: int = 0) -> dict:
    """GET an endpoint. Refuses to call if the known remaining quota is <= floor."""
    b = budget(conn)
    if b["remaining"] <= floor:
        raise BudgetExhausted(f"{b['remaining']} calls left (floor {floor}); resets in {int(b['seconds_to_reset'] // 60)} min")
    q = {k: ("true" if v is True else "false" if v is False else v) for k, v in params.items() if v not in (None, "")}
    url = config.API_BASE + path + ("?" + urllib.parse.urlencode(q) if q else "")
    req = urllib.request.Request(url, headers={
        "Partner-Authorization": _key(), "accept": "application/json", "User-Agent": "seatwatch/1.0 (personal use)"})
    t0 = time.time()
    status, remaining, reset_at, body, err = 0, None, None, None, None
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            status = resp.status
            hdr = resp.headers
            body = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        status, hdr = e.code, e.headers
        err = e.read().decode(errors="replace")[:500]
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        hdr, err = None, f"network: {e}"
    if hdr is not None:
        try:
            remaining = int(hdr.get("x-ratelimit-remaining"))
            reset_at = t0 + int(hdr.get("x-ratelimit-reset"))
        except (TypeError, ValueError):
            pass
    rows = len(body.get("data") or []) if isinstance(body, dict) else None
    conn.execute(
        "INSERT INTO api_calls(ts, endpoint, purpose, status, remaining, reset_at, rows, ms, error) VALUES(?,?,?,?,?,?,?,?,?)",
        (t0, path.split("/")[0], purpose, status, remaining, reset_at, rows, int((time.time() - t0) * 1000), err))
    if status == 429:
        raise BudgetExhausted("seats.aero reports the daily quota is used up (HTTP 429)")
    if status != 200:
        raise ApiError(f"HTTP {status} from {path}: {err}")
    return body


def paged(conn, path: str, params: dict, purpose: str, max_pages: int, floor: int = 0):
    """Fetch up to max_pages pages. Returns (rows deduped by ID, calls made, truncated)."""
    seen, out, calls = set(), [], 0
    cursor, skip = None, 0
    while calls < max_pages:
        p = dict(params, take=1000)
        if cursor is not None:
            p.update(cursor=cursor, skip=skip)
        body = get(conn, path, p, purpose, floor)
        calls += 1
        data = body.get("data") or []
        for r in data:
            if r.get("ID") not in seen:
                seen.add(r.get("ID"))
                out.append(r)
        skip += len(data)
        if cursor is None:
            cursor = body.get("cursor")
        if not body.get("hasMore") or not data:
            return out, calls, False
    return out, calls, True
