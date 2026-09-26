"""Background loop that spreads the daily quota evenly over the time left until reset.

Each tick earns "credit" at rate = spendable_calls / seconds_until_reset, where spendable
is the live remaining quota minus a shrinking on-demand reserve. Unused quota raises the
rate for the rest of the day, so the budget gets used up by the reset.
"""
from __future__ import annotations

import sys
import threading
import time
import traceback

from . import api, datasync, db, jobs, notify, ops

TICK = 30
CLOUD_PULL_EVERY = 300  # in cloud mode, fetch the routine's results this often
CREDIT_CAP = 8
MAX_JOBS_PER_TICK = 6


class Scheduler(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True, name="scheduler")
        self.stop_event = threading.Event()
        self.status = {"started": time.time(), "last_tick": None, "credit": 0.0, "last_job": None, "idle_reason": ""}

    def run(self) -> None:
        conn = db.connect()
        credit, last, last_hk, last_pull = 1.0, time.time(), 0.0, 0.0
        seen_events = time.time()  # only mirror cloud alerts that arrive after startup
        while not self.stop_event.is_set():
            now = time.time()
            if db.get_settings(conn).get("engine") == "cloud":
                # The hourly cloud routine polls and judges; this Mac only mirrors its results.
                if now - last_pull > CLOUD_PULL_EVERY:
                    try:
                        self.status["cloud"] = datasync.pull_state(conn)
                        if db.get_settings(conn).get("mac_notify"):
                            for ev in db.rows(conn, "SELECT * FROM events WHERE kind = 'judged' AND ts > ? ORDER BY ts", (seen_events,)):
                                notify.mac(ev["title"], ev["body"])
                        seen_events = max([seen_events] + [r[0] for r in conn.execute("SELECT max(ts) FROM events") if r[0]])
                    except Exception as e:
                        self.status["cloud"] = {"error": str(e)[:300], "at": now}
                    last_pull = now
                self.status.update(last_tick=now, idle_reason="cloud mode: polling runs in the Claude routine")
                last = now
                self.stop_event.wait(TICK)
                continue
            try:
                if now - last_hk > 3600:
                    jobs.housekeeping(conn)
                    last_hk = now
                p = ops.pacing(conn)
                credit = min(CREDIT_CAP, credit + p["spendable"] / max(600.0, p["seconds_to_reset"]) * (now - last))
                last = now
                self.status.update(last_tick=now, credit=round(credit, 2), idle_reason="")
                ran = 0
                while not p["paused"] and credit >= 1 and ran < MAX_JOBS_PER_TICK and not self.stop_event.is_set():
                    job = jobs.pick_job(conn, time.time())
                    if not job:
                        self.status["idle_reason"] = "every job is fresh"
                        break
                    try:
                        r = jobs.run_job(conn, job, floor=p["bg_floor"])
                    except api.BudgetExhausted as e:
                        self.status["idle_reason"] = str(e)
                        break
                    r.pop("offers", None)
                    self.status["last_job"] = dict(r, at=time.time())
                    credit -= max(1, r["calls"])
                    ran += 1
                    p = ops.pacing(conn)
                if p["paused"]:
                    self.status["idle_reason"] = "paused"
            except Exception:
                traceback.print_exc(file=sys.stderr)
                sys.stderr.flush()
            self.stop_event.wait(TICK)
