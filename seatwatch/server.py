"""Local web UI + JSON API on 127.0.0.1, plus the background scheduler thread."""
from __future__ import annotations

import json
import re
import sys
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import threading

from . import api, config, datasync, db, jobs, matching, notify, ops
from .scheduler import Scheduler

SCHED: Scheduler = None
ALLOWED_HOSTS = {f"127.0.0.1:{config.PORT}", f"localhost:{config.PORT}"}


def state(conn) -> dict:
    settings = db.get_settings(conn)
    balances = db.get_balances(conn)
    table = ops.job_table(conn)
    watches = db.rows(conn, "SELECT * FROM watches ORDER BY enabled DESC, depart_start")
    windows = db.rows(conn, "SELECT * FROM windows ORDER BY enabled DESC, start_date")
    for w in watches:
        js = [j for j in table if j["kind"] == "watch" and j["ref_id"] == w["id"]]
        w["jobs"] = js
        w["active_matches"] = conn.execute(
            "SELECT COUNT(*) FROM matches WHERE owner LIKE ? AND active = 1", (f"watch:{w['id']}:%",)).fetchone()[0]
    for w in windows:
        js = [j for j in table if j["kind"] in ("window", "explore") and j["ref_id"] == w["id"]]
        w["jobs"] = [j for j in js if j["kind"] == "window"]
        w["explore_jobs"] = len([j for j in js if j["kind"] == "explore"])
        w["explore_done"] = len([j for j in js if j["kind"] == "explore" and j["last_run"]])
    return {
        "now": time.time(), "settings": settings, "balances": balances,
        "accounts": {"bank": config.BANK_ACCOUNTS, "airline": config.AIRLINE_ACCOUNTS, "other": config.OTHER_ACCOUNTS},
        "short": config.SHORT_NAMES, "regions": config.REGIONS, "metros": config.METROS, "bands": config.DISTANCE_BANDS,
        "buying_power": ops.buying_power(balances), "watches": watches, "windows": windows,
        "pacing": ops.pacing(conn, settings), "scheduler": SCHED.status if SCHED else None,
        "jobs": {"total": len(table), "never_run": len([j for j in table if not j["last_run"]]),
                 "errors": [j for j in table if j["error"]][:10]},
        "events": db.rows(conn, "SELECT * FROM events ORDER BY id DESC LIMIT 40"),
        "cloud": {"engine": settings.get("engine"), "remote": datasync.sync_settings().get("remote"),
                  "last_job": conn.execute("SELECT max(last_run) FROM job_state").fetchone()[0],
                  "pending": conn.execute("SELECT COUNT(*) FROM pending").fetchone()[0],
                  "sync": (SCHED.status.get("cloud") if SCHED else None)},
    }


def calls(conn) -> dict:
    b = api.budget(conn)
    day_start = b["reset_at"] - 86400
    hourly = db.rows(conn, "SELECT CAST((ts - ?) / 3600 AS INTEGER) AS h, "
                           "SUM(purpose LIKE 'bg:%') AS bg, SUM(purpose LIKE 'ondemand:%') AS od "
                           "FROM api_calls WHERE ts >= ? AND status = 200 GROUP BY h", (day_start, day_start))
    kinds = db.rows(conn, "SELECT CASE WHEN purpose LIKE 'ondemand:%' THEN 'on demand' "
                          "ELSE substr(purpose, 4, instr(substr(purpose, 4), ':') - 1) END AS kind, COUNT(*) AS n "
                          "FROM api_calls WHERE ts >= ? GROUP BY kind ORDER BY n DESC", (day_start,))
    recent = db.rows(conn, "SELECT ts, endpoint, purpose, status, remaining, rows, ms, error FROM api_calls ORDER BY id DESC LIMIT 60")
    return {"day_start": day_start, "hourly": hourly, "kinds": kinds, "recent": recent, "jobs": ops.job_table(conn)}


_push_lock = threading.Lock()


def config_changed() -> None:
    """In cloud mode, push config edits to the data branch in the background."""
    def work():
        with _push_lock:
            conn = db.connect()
            try:
                if db.get_settings(conn).get("engine") == "cloud" and datasync.enabled():
                    datasync.publish_config(conn)
                    db.log_event(conn, "sync", "Config pushed to the cloud")
            except Exception as e:
                db.log_event(conn, "sync", "Couldn't push config to the cloud", str(e)[:300])
            finally:
                conn.close()
    threading.Thread(target=work, daemon=True).start()


class Handler(BaseHTTPRequestHandler):
    server_version = "seatwatch"

    def log_message(self, fmt, *args):  # quiet
        pass

    def _send(self, code: int, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(data)

    def _guard(self, write: bool) -> bool:
        # Block DNS-rebinding and cross-site requests: exact Host, and a custom header on writes
        # (browsers can't add custom headers cross-origin without a CORS preflight we never allow).
        if self.headers.get("Host") not in ALLOWED_HOSTS:
            self._send(403, {"error": "bad host"})
            return False
        if write and self.headers.get("X-Seatwatch") != "1":
            self._send(403, {"error": "missing X-Seatwatch header"})
            return False
        return True

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def _dispatch(self, method: str):
        if not self._guard(method != "GET"):
            return
        url = urlparse(self.path)
        path, qs = url.path, {k: v[0] for k, v in parse_qs(url.query).items()}
        conn = db.connect()
        try:
            if method == "GET" and path in ("/", "/index.html"):
                return self._send(200, (config.PKG_DIR / "web/index.html").read_bytes(), "text/html; charset=utf-8")
            code, body = 200, self.route(conn, method, path, qs)
            if body is None:
                code, body = 404, {"error": "not found"}
            elif method in ("POST", "DELETE") and path.startswith(("/api/balances", "/api/settings", "/api/watches", "/api/windows")) \
                    and not path.endswith("/run"):
                config_changed()
            self._send(code, body)
        except (ValueError, api.ApiError) as e:
            self._send(400 if isinstance(e, ValueError) else 502, {"error": str(e)})
        except Exception as e:
            traceback.print_exc(file=sys.stderr)
            self._send(500, {"error": f"{type(e).__name__}: {e}"})
        finally:
            conn.close()

    def route(self, conn, method: str, path: str, qs: dict):
        if method == "GET":
            if path == "/api/state":
                return state(conn)
            if path == "/api/deals":
                return jobs.deals_view(conn, int(qs["window"]) if qs.get("window") else None)
            if path == "/api/calls":
                return calls(conn)
            m = re.fullmatch(r"/api/watches/(\d+)/matches", path)
            if m:
                return jobs.watch_view(conn, int(m.group(1)))
            m = re.fullmatch(r"/api/trips/([A-Za-z0-9]+)", path)
            if m:
                return ops.trips(conn, m.group(1))
            return None
        body = self._body() if method == "POST" else {}
        if method == "POST" and path == "/api/balances":
            db.set_balances(conn, {k: int(str(v or 0).replace(",", "")) for k, v in body.items()})
            return {"ok": True}
        if method == "POST" and path == "/api/settings":
            if "home_airports" in body:
                body["home_airports"] = ops._validate_places(body["home_airports"], "home airports", False) or ""
            db.set_settings(conn, body)
            return {"ok": True}
        if method == "POST" and path == "/api/search":
            return ops.adhoc_search(conn, body.get("origins"), body.get("destinations"), body.get("start"),
                                    body.get("end"), body.get("cabins") or "business", body.get("direct"),
                                    body.get("max_miles"), body.get("pax") or 1, body.get("sources") or "")
        if method == "POST" and path == "/api/notify-test":
            notify.send(conn, "✈ seatwatch test", "Notifications are working.", "", kind="test")
            return {"ok": True}
        for table, kind in (("watches", "watch"), ("windows", "window")):
            save = ops.save_watch if kind == "watch" else ops.save_window
            if method == "POST" and path == f"/api/{table}":
                new_id = save(conn, body)
                run = ops.run_now(conn, kind, new_id) if body.get("run_now", True) else []
                return {"id": new_id, "run": run}
            m = re.fullmatch(rf"/api/{table}/(\d+)(/run)?", path)
            if m:
                row_id = int(m.group(1))
                if method == "DELETE":
                    ops.delete(conn, table, row_id)
                    return {"ok": True}
                if m.group(2):
                    return {"run": ops.run_now(conn, kind, row_id)}
                save(conn, body, row_id)
                return {"id": row_id}
        return None


def serve(with_scheduler: bool = True) -> None:
    global SCHED
    db.connect().close()
    if with_scheduler:
        SCHED = Scheduler()
        SCHED.start()
    httpd = ThreadingHTTPServer((config.HOST, config.PORT), Handler)
    print(f"seatwatch UI on http://{config.HOST}:{config.PORT}", flush=True)
    try:
        httpd.serve_forever()
    finally:
        if SCHED:
            SCHED.stop_event.set()
