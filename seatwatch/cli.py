"""seatwatch command line. Run `seatwatch -h` or `seatwatch <command> -h`."""
from __future__ import annotations

import argparse
import json
import os
import plistlib
import shutil
import subprocess
import sys
import time
from datetime import datetime

from . import api, config, db, jobs, notify, ops

LABEL = "com.seatwatch.agent"
PLIST = os.path.expanduser(f"~/Library/LaunchAgents/{LABEL}.plist")
APP_DIR = config.DATA_DIR / "app"


def out(obj, as_json: bool, human=None):
    if as_json or human is None:
        print(json.dumps(obj, indent=2, default=str))
    else:
        human(obj)


def _range(s: str, name: str):
    if not s:
        return None, None
    parts = s.split(":")
    if len(parts) != 2:
        raise SystemExit(f"--{name} must look like 2026-11-01:2026-11-30")
    return parts[0], parts[1]


def _ago(ts):
    if not ts:
        return "never"
    m = int((time.time() - ts) / 60)
    return f"{m}m ago" if m < 120 else f"{m // 60}h ago"


def _offer_rows(offers, limit):
    for o in offers[:limit]:
        if o.get("verdict"):
            print(f"  [{o['verdict'].upper()}] {o.get('reason', '')}")
        extra = []
        if "score" in o:
            extra.append(f"score {o['score']:.2f}")
        if o.get("pay_with"):
            extra.append(o["pay_with"])
        if o.get("positioning"):
            extra.append("needs positioning")
        print("  " + jobs.offer_line(o) + (f"  [{'; '.join(extra)}]" if extra else "") + f"  id={o['avail_id']}")
    if len(offers) > limit:
        print(f"  … {len(offers) - limit} more (use --limit or --json)")


# ------------------------------------------------------------------ commands

def cmd_status(a, conn):
    p = ops.pacing(conn)
    table = ops.job_table(conn)
    data = {"pacing": p, "jobs": table, "service_running": _service_running()}
    def human(d):
        reset = datetime.fromtimestamp(p["reset_at"]).strftime("%-I:%M %p")
        print(f"Service: {'running' if d['service_running'] else 'NOT running'}   UI: http://{config.HOST}:{config.PORT}")
        print(f"Quota: {p['remaining']}/{p['limit']} left, resets {reset} local · used today {p['used']} "
              f"({', '.join(f'{k} {v}' for k, v in p['by_purpose'].items()) or 'none'})")
        print(f"Pacing: {p['rate_per_hour']}/hour background · on-demand reserve {p['reserve']}"
              + (" · PAUSED" if p["paused"] else ""))
        print(f"Jobs: {len(table)} ({len([j for j in table if not j['last_run']])} never run)")
        for j in table:
            if j["kind"] == "explore":
                continue
            err = f"  ERROR: {j['error']}" if j["error"] else ""
            print(f"  [{j['kind']}] {j['label']}: last {_ago(j['last_run'])}, {j['matches'] or 0} matches{err}")
        ex = [j for j in table if j["kind"] == "explore"]
        if ex:
            print(f"  [explore] {len(ex)} program/region scans, {len([j for j in ex if j['last_run']])} run so far")
    out(data, a.json, human)


def _watch_payload(a) -> dict:
    d = {}
    ds, de = _range(a.depart, "depart")
    rs, re_ = _range(a.ret, "return")
    for k, v in (("name", a.name), ("description", a.description), ("origins", a.origins),
                 ("destinations", a.destinations), ("depart_start", ds), ("depart_end", de),
                 ("return_start", rs), ("return_end", re_), ("cabins", a.cabins), ("max_miles", a.max_miles),
                 ("pax", a.pax), ("sources", a.sources), ("priority", a.priority), ("interval_min", a.interval)):
        if v is not None:
            d[k] = v
    if a.direct is not None:
        d["direct_only"] = a.direct
    if a.picks_only is not None:
        d["picks_only"] = a.picks_only
    if getattr(a, "no_return", False):
        d["return_start"] = d["return_end"] = None
    return d


def cmd_watch(a, conn):
    if a.action == "add":
        wid = ops.save_watch(conn, _watch_payload(a))
        res = [] if a.no_run else ops.run_now(conn, "watch", wid)
        w = dict(conn.execute("SELECT * FROM watches WHERE id = ?", (wid,)).fetchone())
        offers = jobs.watch_view(conn, wid)
        def human(_):
            print(f"Added watch #{wid}: {w['name']} ({w['origins']} → {w['destinations']}, {w['cabins']}, "
                  f"{w['depart_start']}..{w['depart_end']}" + (f", return {w['return_start']}..{w['return_end']}" if w['return_start'] else "") + ")")
            for r in res:
                print(f"  checked now: {r['label']} — {r['matches']} matches, {r['calls']} call(s)" + (f", ERROR {r['error']}" if r.get("error") else ""))
            _offer_rows(offers, a.limit)
        return out({"watch": w, "run": res, "matches": offers}, a.json, human)
    if a.action == "list":
        ws = db.rows(conn, "SELECT * FROM watches ORDER BY enabled DESC, id")
        def human(ws):
            if not ws:
                print("No watches yet.")
            for w in ws:
                print(f"#{w['id']} {'' if w['enabled'] else '(paused) '}{w['name']}: {w['origins']} → {w['destinations']} · "
                      f"{w['cabins']} · {w['depart_start']}..{w['depart_end']}"
                      + (f" · return {w['return_start']}..{w['return_end']}" if w['return_start'] else "")
                      + (f" · ≤{w['max_miles']:,}" if w['max_miles'] else "") + f" · {w['pax']} pax"
                      + (" · nonstop" if w['direct_only'] else ""))
        return out(ws, a.json, human)
    if a.id is None:
        raise SystemExit("this action needs a watch id")
    if a.action == "show":
        offers = jobs.watch_view(conn, a.id)
        return out(offers, a.json, lambda o: _offer_rows(o, a.limit) if o else print("No current matches in the cache."))
    if a.action == "rm":
        ops.delete(conn, "watches", a.id)
        return print(f"Deleted watch #{a.id}")
    if a.action in ("pause", "resume"):
        conn.execute("UPDATE watches SET enabled = ? WHERE id = ?", (1 if a.action == "resume" else 0, a.id))
        return print(f"Watch #{a.id} {a.action}d")
    if a.action == "edit":
        ops.save_watch(conn, _watch_payload(a), a.id)
        return print(f"Updated watch #{a.id}")
    if a.action == "run":
        res = ops.run_now(conn, "watch", a.id)
        offers = jobs.watch_view(conn, a.id)
        def human(_):
            for r in res:
                print(f"{r['label']}: {r['matches']} matches ({r['new']} new), {r['calls']} call(s)" + (f", ERROR {r['error']}" if r.get("error") else ""))
            _offer_rows(offers, a.limit)
        return out({"run": res, "matches": offers}, a.json, human)


def _window_payload(a) -> dict:
    d = {}
    s, e = _range(a.dates, "dates")
    for k, v in (("name", a.name), ("start_date", s), ("end_date", e), ("origins", a.origins),
                 ("destinations", a.destinations), ("cabins", a.cabins), ("pax", a.pax), ("min_score", a.min_score)):
        if v is not None:
            d[k] = v
    for k, v in (("direct_only", a.direct), ("roundtrip", a.roundtrip), ("explore", a.explore)):
        if v is not None:
            d[k] = v
    return d


def cmd_window(a, conn):
    if a.action == "add":
        wid = ops.save_window(conn, _window_payload(a))
        res = [] if a.no_run else ops.run_now(conn, "window", wid)
        offers = jobs.deals_view(conn, wid)
        def human(_):
            w = conn.execute("SELECT * FROM windows WHERE id = ?", (wid,)).fetchone()
            print(f"Added travel window #{wid}: {w['name']} ({w['start_date']}..{w['end_date']} → {w['destinations']}, {w['cabins']})")
            if not (w["origins"] or db.get_settings(conn)["home_airports"]):
                print("  NOTE: no origins and no home airports set — only region-wide explore scans will run. "
                      "Set home airports: seatwatch settings set home_airports=JFK,EWR")
            for r in res:
                print(f"  checked now: {r['label']} — {r['matches']} deals, {r['calls']} call(s)" + (f", ERROR {r['error']}" if r.get("error") else ""))
            _offer_rows(offers, a.limit)
        return out({"id": wid, "run": res, "deals": offers}, a.json, human)
    if a.action == "list":
        ws = db.rows(conn, "SELECT * FROM windows ORDER BY enabled DESC, id")
        def human(ws):
            if not ws:
                print("No travel windows yet.")
            for w in ws:
                print(f"#{w['id']} {'' if w['enabled'] else '(paused) '}{w['name']}: {w['start_date']}..{w['end_date']} · "
                      f"{w['origins'] or 'home'} → {w['destinations']} · {w['cabins']} · {w['pax']} pax"
                      + (" · round trip" if w['roundtrip'] else " · one way") + (" · +explore" if w['explore'] else ""))
        return out(ws, a.json, human)
    if a.id is None:
        raise SystemExit("this action needs a window id")
    if a.action == "rm":
        ops.delete(conn, "windows", a.id)
        return print(f"Deleted travel window #{a.id}")
    if a.action in ("pause", "resume"):
        conn.execute("UPDATE windows SET enabled = ? WHERE id = ?", (1 if a.action == "resume" else 0, a.id))
        return print(f"Window #{a.id} {a.action}d")
    if a.action == "edit":
        ops.save_window(conn, _window_payload(a), a.id)
        return print(f"Updated window #{a.id}")
    if a.action == "run":
        res = ops.run_now(conn, "window", a.id)
        return out(res, a.json, lambda rs: [print(f"{r['label']}: {r['matches']} deals ({r['new']} new), {r['calls']} call(s)") for r in rs])


def cmd_deals(a, conn):
    offers = jobs.deals_view(conn, a.window)
    if a.home_only:
        offers = [o for o in offers if not o["positioning"]]
    if db.get_settings(conn).get("engine") == "cloud" and not a.all:
        rank = {"great": 0, "good": 1}
        offers = sorted([o for o in offers if o.get("verdict") in rank], key=lambda o: (rank[o["verdict"]], -o["score"]))
    out(offers[: a.limit] if a.json else offers, a.json, lambda o: _offer_rows(o, a.limit) if o else print("No deals in the cache yet."))


def cmd_search(a, conn):
    s, e = _range(a.dates, "dates")
    offers = ops.adhoc_search(conn, a.origins, a.destinations, s, e, a.cabins or "business", a.direct,
                              a.max_miles, a.pax or 1, a.sources or "")
    out(offers[: a.limit] if a.json else offers, a.json, lambda o: _offer_rows(o, a.limit) if o else print("Nothing found."))


def cmd_trips(a, conn):
    ts = ops.trips(conn, a.avail_id)
    def human(ts):
        for t in ts[: a.limit]:
            print(f"{t['cabin']:9} {t['miles']:>7,} + {t['taxes']:,.0f} {t['currency']} · {t['flights']} · "
                  f"{t['departs'][:16].replace('T', ' ')} → {t['arrives'][:16].replace('T', ' ')} · "
                  f"{t['stops']} stop(s) · {t['seats']} seats")
    out(ts, a.json, human)


def cmd_balance(a, conn):
    if a.action == "set":
        upd = {}
        for kv in a.pairs:
            k, _, v = kv.partition("=")
            upd[k.strip()] = int(v.replace(",", "").replace("k", "000"))
        db.set_balances(conn, upd)
    bal = db.get_balances(conn)
    def human(b):
        for acct, name in config.BANK_ACCOUNTS + config.AIRLINE_ACCOUNTS + config.OTHER_ACCOUNTS:
            if b.get(acct):
                print(f"  {acct:15} {b[acct]:>10,}  {name}")
        if not any(b.values()):
            print("No balances set. Example: seatwatch balance set amex_mr=120000 united=45000")
        print("Account ids:", ", ".join(k for k, _ in config.BANK_ACCOUNTS + config.AIRLINE_ACCOUNTS + config.OTHER_ACCOUNTS))
    out(bal, a.json, human)


def cmd_settings(a, conn):
    if a.action == "set":
        upd = {}
        for kv in a.pairs:
            k, _, v = kv.partition("=")
            try:
                upd[k] = json.loads(v)
            except json.JSONDecodeError:
                upd[k] = v
            if k not in config.DEFAULT_SETTINGS:
                raise SystemExit(f"unknown setting '{k}'")
        db.set_settings(conn, upd)
    out(db.get_settings(conn), True)


def cmd_notify_test(a, conn):
    notify.send(conn, "✈ seatwatch test", "Notifications are working.", "", kind="test")
    print("Sent.")


def cmd_cloud(a, conn):
    from . import cloud, datasync
    if a.action == "poll":
        conn = cloud.prepare()
        return out(cloud.poll(conn), True)
    if a.action == "finish":
        conn = db.connect()
        return out(cloud.finish(conn, a.verdicts), True)
    if a.action == "setup":
        if not a.remote:
            raise SystemExit("--remote is required, e.g. git@github.com:you/seatwatch.git")
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        datasync.SYNC_FILE.write_text(json.dumps({"remote": a.remote}))
        db.set_settings(conn, {"engine": "cloud"})
        datasync.ensure_clone(a.remote)
        datasync.publish_config(conn)
        print(f"Config pushed to the '{datasync.BRANCH}' branch of {a.remote}. This Mac now only syncs; "
              "background polling happens in the cloud routine.")
        return
    if a.action == "add-key":
        # Run by you (not Claude): uploads your seats.aero key to the private repo's data branch
        # so the cloud routine can use it.
        key = config.KEY_PATH.read_text().strip()
        if not key:
            raise SystemExit(f"no key in {config.KEY_PATH}")

        def writer(path):
            (path / "secrets").mkdir(exist_ok=True)
            (path / "secrets/seats_aero_key").write_text(key + "\n")
        datasync.ensure_clone()
        pushed = datasync.push(writer, "add seats.aero key")
        return print("Key uploaded to the data branch." if pushed else "Key already there.")
    if a.action == "disable":
        db.set_settings(conn, {"engine": "local"})
        return print("Engine set to local: this Mac polls again (remember to pause the cloud routine).")
    if a.action == "sync":
        pushed = datasync.publish_config(conn)
        st = datasync.pull_state(conn)
        return out({"config_pushed": pushed, **st}, a.json, lambda d: print(
            f"config {'pushed' if pushed else 'unchanged'}; cloud state {'pulled' if d.get('state') else 'not available yet'}"))
    if a.action == "status":
        last = conn.execute("SELECT max(last_run) FROM job_state").fetchone()[0]
        pend = conn.execute("SELECT COUNT(*) FROM pending").fetchone()[0]
        v = {r["verdict"]: r["n"] for r in db.rows(conn, "SELECT verdict, COUNT(*) AS n FROM verdicts GROUP BY verdict")}
        d = {"engine": db.get_settings(conn)["engine"], "sync": datasync.sync_settings(), "last_cloud_job": last,
             "pending": pend, "verdicts": v}
        return out(d, a.json, lambda d: print(f"engine {d['engine']} · remote {d['sync'].get('remote', '-')} · "
                                              f"last cloud job {_ago(last)} · {pend} waiting for Claude · verdicts {v or 'none yet'}"))


def _config_changed(conn):
    """In cloud mode, push config edits to the data branch so the next cloud run sees them."""
    from . import datasync
    if db.get_settings(conn).get("engine") != "cloud" or not datasync.enabled():
        return
    try:
        datasync.publish_config(conn)
    except datasync.SyncError as e:
        print(f"warning: couldn't push config to the cloud yet ({e}); run `seatwatch cloud sync` later", file=sys.stderr)


def _service_running() -> bool:
    r = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{LABEL}"], capture_output=True, text=True)
    return r.returncode == 0 and "state = running" in r.stdout


def cmd_install(a, conn):
    """Copy the code to Application Support (background agents can't read ~/Documents) and load launchd."""
    APP_DIR.mkdir(parents=True, exist_ok=True)
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    dest = APP_DIR / "seatwatch"
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(config.PKG_DIR, dest, ignore=shutil.ignore_patterns("__pycache__"))
    plist = {
        "Label": LABEL,
        "ProgramArguments": [sys.executable, "-m", "seatwatch.cli", "serve"],
        "WorkingDirectory": str(APP_DIR),
        "EnvironmentVariables": {"PYTHONPATH": str(APP_DIR), "PYTHONUNBUFFERED": "1"},
        "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 30,
        "StandardOutPath": str(config.LOG_DIR / "service.log"),
        "StandardErrorPath": str(config.LOG_DIR / "service.log"),
    }
    os.makedirs(os.path.dirname(PLIST), exist_ok=True)
    with open(PLIST, "wb") as f:
        plistlib.dump(plist, f)
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", f"{domain}/{LABEL}"], capture_output=True)
    time.sleep(1)
    r = subprocess.run(["launchctl", "bootstrap", domain, PLIST], capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"launchctl bootstrap failed: {r.stderr.strip()}")
    print(f"Installed. Service runs from {APP_DIR}, logs in {config.LOG_DIR}/service.log")
    print(f"Dashboard: http://{config.HOST}:{config.PORT}")


def cmd_uninstall(a, conn):
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], capture_output=True)
    if os.path.exists(PLIST):
        os.remove(PLIST)
    print(f"Service stopped and removed. Data kept in {config.DATA_DIR}")


def cmd_serve(a, conn):
    from . import server
    server.serve(with_scheduler=not a.no_scheduler)


# ------------------------------------------------------------------ parser

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="seatwatch", description="seats.aero award alerts and deal finder")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="quota, pacing, and job freshness")

    places = "airports, metro codes (NYC, TYO, LON, BAY…) or regions (Europe, Asia…), comma separated"
    w = sub.add_parser("watch", help="route watches: alert on any matching availability")
    w.add_argument("action", choices=["add", "list", "show", "run", "edit", "pause", "resume", "rm"])
    w.add_argument("id", nargs="?", type=int)
    w.add_argument("--name")
    w.add_argument("--description", help="the request in your own words")
    w.add_argument("--from", dest="origins", help=places)
    w.add_argument("--to", dest="destinations", help=places)
    w.add_argument("--depart", help="outbound date range START:END")
    w.add_argument("--return", dest="ret", help="return date range START:END (optional)")
    w.add_argument("--no-return", action="store_true", help="(edit) remove the return leg")
    w.add_argument("--cabins", help="economy,premium,business,first (default business)")
    w.add_argument("--max-miles", type=int, help="max miles per person, one way")
    w.add_argument("--pax", type=int)
    w.add_argument("--direct", action="store_const", const=True, default=None, help="nonstop only")
    w.add_argument("--any-stops", dest="direct", action="store_const", const=False)
    w.add_argument("--sources", help="limit to programs, e.g. aeroplan,united")
    w.add_argument("--picks-only", dest="picks_only", action="store_const", const=True, default=None,
                   help="(cloud mode) only notify when Claude rates a match good or great")
    w.add_argument("--all-matches", dest="picks_only", action="store_const", const=False,
                   help="(cloud mode) notify on every new match, with Claude's take")
    w.add_argument("--priority", type=int, help="1-5 (default 3)")
    w.add_argument("--interval", type=int, help="target minutes between checks (default 45)")
    w.add_argument("--no-run", action="store_true", help="don't check immediately after adding")
    w.add_argument("--limit", type=int, default=15)

    t = sub.add_parser("window", help="travel windows: find good deals you can afford")
    t.add_argument("action", choices=["add", "list", "run", "edit", "pause", "resume", "rm"])
    t.add_argument("id", nargs="?", type=int)
    t.add_argument("--name")
    t.add_argument("--dates", help="START:END of the travel period")
    t.add_argument("--from", dest="origins", help=places + " (default: home airports)")
    t.add_argument("--to", dest="destinations", help=places)
    t.add_argument("--cabins")
    t.add_argument("--pax", type=int)
    t.add_argument("--min-score", type=float, help="1.0 = at threshold; higher is stricter")
    t.add_argument("--direct", action="store_const", const=True, default=None)
    t.add_argument("--oneway", dest="roundtrip", action="store_const", const=False, default=None)
    t.add_argument("--roundtrip", dest="roundtrip", action="store_const", const=True)
    t.add_argument("--no-explore", dest="explore", action="store_const", const=False, default=None)
    t.add_argument("--explore", dest="explore", action="store_const", const=True)
    t.add_argument("--no-run", action="store_true")
    t.add_argument("--limit", type=int, default=15)

    d = sub.add_parser("deals", help="best deals from the cache (no API calls)")
    d.add_argument("--window", type=int)
    d.add_argument("--home-only", action="store_true", help="hide deals that need a positioning flight")
    d.add_argument("--all", action="store_true", help="(cloud mode) include candidates Claude passed on or hasn't judged")
    d.add_argument("--limit", type=int, default=20)

    s = sub.add_parser("search", help="one-off search right now (uses the on-demand reserve)")
    s.add_argument("--from", dest="origins", required=True, help=places)
    s.add_argument("--to", dest="destinations", required=True, help=places)
    s.add_argument("--dates", required=True, help="START:END")
    s.add_argument("--cabins")
    s.add_argument("--direct", action="store_true")
    s.add_argument("--max-miles", type=int)
    s.add_argument("--pax", type=int)
    s.add_argument("--sources")
    s.add_argument("--limit", type=int, default=20)

    tr = sub.add_parser("trips", help="flight-level detail for an availability id (1 call)")
    tr.add_argument("avail_id")
    tr.add_argument("--limit", type=int, default=15)

    b = sub.add_parser("balance", help="points balances")
    b.add_argument("action", choices=["list", "set"])
    b.add_argument("pairs", nargs="*", help="account=amount, e.g. amex_mr=120000")

    st = sub.add_parser("settings", help="view or change settings")
    st.add_argument("action", choices=["get", "set"])
    st.add_argument("pairs", nargs="*", help="key=value (JSON values allowed)")

    c = sub.add_parser("cloud", help="cloud routine: setup/sync/status on the Mac; poll/finish in the routine")
    c.add_argument("action", choices=["setup", "sync", "status", "add-key", "disable", "poll", "finish"])
    c.add_argument("--remote", help="(setup) git remote of the private repo")
    c.add_argument("--verdicts", help="(finish) path to Claude's verdicts.json")

    sub.add_parser("notify-test", help="send a test notification")
    sub.add_parser("install", help="install/refresh the background service (launchd)")
    sub.add_parser("uninstall", help="stop and remove the background service")
    sv = sub.add_parser("serve", help="run the service in the foreground")
    sv.add_argument("--no-scheduler", action="store_true")
    return p


def main(argv=None):
    a = build_parser().parse_args(argv)
    conn = db.connect()
    fn = globals()["cmd_" + a.cmd.replace("-", "_")]
    writes = (a.cmd in ("watch", "window") and a.action in ("add", "edit", "pause", "resume", "rm")) or \
             (a.cmd in ("balance", "settings") and a.action == "set")
    try:
        fn(a, conn)
        if writes:
            _config_changed(conn)
    except (ValueError, api.ApiError) as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        from . import datasync
        if isinstance(e, datasync.SyncError):
            print(f"error: {e}", file=sys.stderr)
            sys.exit(1)
        raise


if __name__ == "__main__":
    main()
