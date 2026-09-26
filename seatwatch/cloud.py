"""Hourly cloud run: poll seats.aero, hand new candidates to Claude, send its picks, push state.

Called by the Claude routine through bin/seatwatch-cloud:
  poll    pull the data branch, rebuild the DB, spend this hour's share of the quota,
          write pending.json (what Claude must judge)
  finish  read Claude's verdicts.json, send notifications, push a state snapshot
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
import subprocess
import time
from datetime import date, datetime
from pathlib import Path

from . import api, config, datasync, db, jobs, matching, notify, ops

VERDICTS = ("great", "good", "pass")
ICON = {"great": "★", "good": "✓", "pass": "·"}
MAX_RUNTIME = 15 * 60


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def prepare() -> sqlite3.Connection:
    remote = subprocess.run(["git", "-C", str(_repo_root()), "remote", "get-url", "origin"],
                            capture_output=True, text=True).stdout.strip()
    path = datasync.ensure_clone(remote)
    if not datasync.pull(path):
        raise datasync.SyncError("the 'data' branch doesn't exist yet; run `seatwatch cloud setup` on the Mac")
    if not os.environ.get("SEATS_AERO_KEY"):
        config.KEY_PATH = path / "secrets/seats_aero_key"
    if config.DB_PATH.exists():
        config.DB_PATH.unlink()
    for suffix in ("-wal", "-shm"):
        Path(str(config.DB_PATH) + suffix).unlink(missing_ok=True)
    snap = path / "state/seatwatch.db"
    if snap.exists():
        src = sqlite3.connect(str(snap))
        dst = sqlite3.connect(str(config.DB_PATH))
        src.backup(dst)
        src.close()
        dst.close()
    conn = db.connect()
    datasync.import_config(conn, path)
    db.set_settings(conn, {"engine": "cloud"})
    return conn


def _context(conn, o: dict) -> dict:
    """Cheapest alternatives on the same route/cabin in the cache, so Claude can judge relative price."""
    rows = conn.execute("SELECT source, cabins, date FROM availability WHERE origin = ? AND dest = ?",
                        (o["origin"], o["dest"])).fetchall()
    costs = []
    for r in rows:
        c = json.loads(r["cabins"]).get(o["cabin"])
        if c:
            costs.append((c["cost"], r["source"], r["date"]))
    costs.sort()
    if not costs:
        return {}
    mid = costs[len(costs) // 2][0]
    return {"route_options_in_cache": len(costs), "route_cheapest": {"miles": costs[0][0], "program": costs[0][1], "date": costs[0][2]},
            "route_median_miles": mid}


def connectivity() -> dict:
    """Reachability of seats.aero and ntfy from this sandbox. Uses no quota (no API key is sent)."""
    import urllib.error
    import urllib.request
    out = {"key_present": bool(os.environ.get("SEATS_AERO_KEY")) or Path(config.KEY_PATH).exists()}
    for name, url in (("seats.aero", "https://seats.aero/partnerapi/search?origin_airport=SFO&destination_airport=NRT"),
                      ("ntfy", "https://ntfy.sh/v1/health")):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "seatwatch/1.0"}), timeout=20) as r:
                out[name] = f"ok ({r.status})"
        except urllib.error.HTTPError as e:  # the server answered, so the network path works
            out[name] = f"ok ({e.code})"
        except Exception as e:
            out[name] = f"BLOCKED: {e}. Allow {name.split('.')[0] if name == 'ntfy' else name}" \
                        f"{'.sh' if name == 'ntfy' else ''} in the cloud environment's network settings."
    return out


def poll(conn) -> dict:
    t_start = time.time()
    net = connectivity()
    if not str(net.get("seats.aero", "")).startswith("ok") or not net["key_present"]:
        return {"error": "cannot poll", "connectivity": net, "to_judge": 0}
    s = db.get_settings(conn)
    p = ops.pacing(conn, s)
    runs_left = max(1, math.ceil(p["seconds_to_reset"] / 3600))
    allowance = min(int(p["spendable"] / runs_left), int(os.environ.get("SEATWATCH_MAX_CALLS", 10 ** 9)))
    used, results, first_call = 0, [], True
    jobs.housekeeping(conn)
    while used < allowance and time.time() - t_start < MAX_RUNTIME and not s.get("paused"):
        job = jobs.pick_job(conn, time.time())
        if not job:
            break
        try:
            r = jobs.run_job(conn, job, floor=p["bg_floor"], judge=True)
        except api.BudgetExhausted as e:
            results.append({"key": job.key, "error": str(e)})
            break
        r.pop("offers", None)
        results.append(r)
        used += max(1, r["calls"])
        if first_call:  # now we know the real remaining quota (the Mac may have spent some)
            first_call = False
            p = ops.pacing(conn, s)
            allowance = min(allowance, used + int(p["spendable"] / runs_left))
    out = write_pending(conn, s)
    out.update(connectivity=net, calls=used, allowance=allowance, jobs_run=len(results),
               errors=[r for r in results if r.get("error")], remaining=ops.pacing(conn, s)["remaining"])
    return out


def _short_date(d: str) -> str:
    return datetime.strptime(d, "%Y-%m-%d").strftime("%b %-d")


def write_pending(conn, s: dict) -> dict:
    """Group identical fares (owner, route, cabin, program, price) and write the batch Claude judges."""
    conn.execute("DELETE FROM pending WHERE created < ?", (time.time() - 48 * 3600,))
    conn.execute("DELETE FROM pending WHERE avail_id NOT IN (SELECT id FROM availability)")
    groups: dict = {}
    for r in db.rows(conn, "SELECT * FROM pending"):
        o = json.loads(r["payload"])
        key = (r["owner"].split(":")[0] + ":" + str(o.get("ref_id")), r["kind"], o["origin"], o["dest"], o["cabin"], o["source"], o["cost"])
        g = groups.setdefault(key, {"kind": r["kind"], "offers": []})
        if not any(x["avail_id"] == o["avail_id"] and x["cabin"] == o["cabin"] for x in g["offers"]):
            g["offers"].append(o)
    ordered = sorted(groups.values(), key=lambda g: (g["kind"] != "watch", -max(o.get("score", 0) for o in g["offers"])))
    batch, members = [], {}
    for n, g in enumerate(ordered[: int(s["judge_batch"])], 1):
        offs = sorted(g["offers"], key=lambda o: o["date"])
        o = offs[0]
        gid = f"g{n}"
        members[gid] = [[x["avail_id"], x["cabin"]] for x in offs]
        taxes = sorted({x["taxes"] for x in offs})
        seats = [x["seats"] for x in offs if x["seats"]]
        batch.append({
            "id": gid, "for": f"{'watch' if g['kind'] == 'watch' else 'travel window'} '{o['owner_label']}'",
            "kind": g["kind"], "dates": [x["date"] for x in offs][:12], "date_count": len(offs),
            "route": f"{o['origin']}-{o['dest']}", "distance_mi": o["distance"],
            "cabin": config.CABIN_LABELS[o["cabin"]], "program": config.SOURCE_NAMES.get(o["source"], o["source"]),
            "miles_per_person": o["cost"], "passengers": o.get("pax") or 1, "total_miles": o.get("total_miles"),
            "taxes": (f"{o['currency'] or 'USD'} {taxes[0] / 100:,.0f}" + (f"-{taxes[-1] / 100:,.0f}" if len(taxes) > 1 else "")
                      if taxes[-1] else "unknown/none"),
            "seats_left": (f"{min(seats)}-{max(seats)}" if seats and min(seats) != max(seats) else (seats[0] if seats else "unknown")),
            "nonstop": any(x["direct"] for x in offs), "airlines": o["airlines"],
            "rule_score": o.get("score"), "rule_threshold_miles": o.get("threshold"),
            "affordable": o.get("affordable"), "pay_with": o.get("pay_with"),
            "needs_positioning_flight": o.get("positioning", False),
            "change": sorted({x.get("why", "new") for x in offs}),
            **_context(conn, o),
        })
    balances = {k: v for k, v in db.get_balances(conn).items() if v}
    doc = {
        "instructions": "Judge each candidate (a fare, possibly on several dates). Write verdicts.json as a JSON list of "
                        "{\"id\", \"verdict\": great|good|pass, \"reason\"} (reason: one plain sentence, <= 20 words).",
        "user": {"notes": s.get("judge_notes") or "(none given)", "home_airports": s.get("home_airports"),
                 "balances": balances, "point_value_cents": s["cpp"]},
        "candidates": batch, "waiting_after_this_batch": max(0, len(ordered) - len(batch)),
    }
    path = config.DATA_DIR / "pending.json"
    path.write_text(json.dumps(doc, indent=1, default=str))
    (config.DATA_DIR / "pending_map.json").write_text(json.dumps(members))
    return {"pending_file": str(path), "to_judge": len(batch), "queued_fares": len(ordered),
            "queued_offers": sum(len(g["offers"]) for g in ordered)}


def _judged_title(o: dict, verdict: str) -> str:
    word = {"great": "Great deal", "good": "Good deal", "pass": "Available"}[verdict]
    return f"✈ {word} · {o['origin']}→{o['dest']} {config.CABIN_LABELS[o['cabin']]} {jobs.fmt_k(o['cost'])}"


def _group_line(offs: list, verdict: str, reason: str) -> str:
    o = offs[0]
    dates = ", ".join(_short_date(x["date"]) for x in offs[:3]) + (f" +{len(offs) - 3} more" if len(offs) > 3 else "")
    bits = [f"{ICON[verdict]} {o['origin']}→{o['dest']} {config.CABIN_LABELS[o['cabin']]} {jobs.fmt_k(o['cost'])}",
            config.SHORT_NAMES.get(o["source"], o["source"]), dates]
    return " · ".join(bits) + (f" — {reason}" if reason else "")


def finish(conn, verdicts_path: str = None) -> dict:
    now = time.time()
    verdicts = []
    if verdicts_path and Path(verdicts_path).exists():
        verdicts = json.loads(Path(verdicts_path).read_text())
        if not isinstance(verdicts, list):
            raise ValueError("verdicts.json must be a JSON list")
    map_file = config.DATA_DIR / "pending_map.json"
    members = json.loads(map_file.read_text()) if map_file.exists() else {}
    by_owner: dict = {}
    applied = 0
    for v in verdicts:
        verdict = str(v.get("verdict", "")).lower()
        gid = str(v.get("id", ""))
        if verdict not in VERDICTS or gid not in members:
            continue
        reason = str(v.get("reason", ""))[:200]
        group = []
        for avail_id, cabin in members[gid]:
            rows = db.rows(conn, "SELECT * FROM pending WHERE avail_id = ? AND cabin = ?", (avail_id, cabin))
            if not rows:
                continue
            conn.execute("INSERT OR REPLACE INTO verdicts(avail_id, cabin, cost, verdict, reason, at) VALUES(?,?,?,?,?,?)",
                         (avail_id, cabin, rows[0]["cost"], verdict, reason, now))
            conn.execute("DELETE FROM pending WHERE avail_id = ? AND cabin = ?", (avail_id, cabin))
            applied += 1
            o = json.loads(rows[0]["payload"])
            n = conn.execute("SELECT cost, at FROM notified WHERE avail_id = ? AND cabin = ?", (avail_id, cabin)).fetchone()
            if n and now - n["at"] < 12 * 3600 and o["cost"] >= n["cost"] * 0.95:
                continue
            group.append(o)
        if not group or (group[0].get("owner_kind") != "watch" and verdict == "pass"):
            continue
        by_owner.setdefault(group[0]["owner_label"], []).append((verdict, reason, sorted(group, key=lambda o: o["date"])))
    sent = 0
    order = {"great": 0, "good": 1, "pass": 2}
    for label, gs in by_owner.items():
        gs.sort(key=lambda g: (order[g[0]], g[2][0]["cost"]))
        for verdict, _, offs in gs:
            for o in offs:
                conn.execute("INSERT OR REPLACE INTO notified(avail_id, cabin, cost, at) VALUES(?,?,?,?)",
                             (o["avail_id"], o["cabin"], o["cost"], now))
        verdict, reason, offs = gs[0]
        if len(gs) == 1:
            title = _judged_title(offs[0], verdict)
            body = f"{reason}\n{_group_line(offs, verdict, '')}" + (f"\nPay: {offs[0]['pay_with']}" if offs[0].get("pay_with") else "") + f"\n{label}"
        else:
            picks = sum(1 for g in gs if g[0] != "pass")
            title = (f"✈ {picks} pick{'s' if picks != 1 else ''} · {label}" if picks else f"✈ {len(gs)} new fares · {label}")
            body = "\n".join(_group_line(g[2], g[0], g[1]) for g in gs[:4])
            if len(gs) > 4:
                body += f"\n+{len(gs) - 4} more on the dashboard"
        notify.send(conn, title, body, jobs.seats_link(offs[0]), kind="judged")
        sent += 1
    pushed = push_state(conn)
    return {"verdicts_applied": applied, "notifications": sent, "state_pushed": pushed,
            "still_pending": conn.execute("SELECT COUNT(*) FROM pending").fetchone()[0]}


def push_state(conn) -> bool:
    conn.execute("DELETE FROM availability WHERE date < ?", (date.today().isoformat(),))
    conn.execute("DELETE FROM verdicts WHERE at < ?", (time.time() - 30 * 86400,))
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    snap = config.DATA_DIR / "snapshot.db"
    snap.unlink(missing_ok=True)
    dst = sqlite3.connect(str(snap))
    conn.backup(dst)
    dst.execute("VACUUM")
    dst.close()

    def writer(path: Path):
        (path / "state").mkdir(exist_ok=True)
        (path / "state/seatwatch.db").write_bytes(snap.read_bytes())
        (path / "state/last_run.json").write_text(json.dumps({"at": time.time(), "pacing": ops.pacing(conn)}, default=str))

    return datasync.push(writer, "state from cloud run")
