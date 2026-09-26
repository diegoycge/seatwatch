"""Background jobs: what to fetch, how often, and what to alert on.

Three job kinds, all derived from the watches/windows tables on every tick:
  watch   - Cached Search for one leg of a route watch (priority 3, every ~45 min)
  window  - Cached Search from your home airports to a travel window's destinations (priority 2)
  explore - Bulk Availability for one program + region pair, catches deals from nearby
            origins (priority 1). These soak up whatever quota is left over.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import date, datetime

from . import api, config, db, matching, notify
from .matching import Criteria, cabin_codes, parse_places, query_airports

FALLBACK_SOURCES = ["united", "american", "delta", "alaska", "aeroplan", "flyingblue", "virginatlantic",
                    "qatar", "jetblue", "lifemiles", "singapore", "turkish"]
MIN = 60


@dataclass
class Job:
    key: str
    kind: str
    label: str
    endpoint: str
    params: dict
    crit: Criteria
    priority: float
    interval: float
    min_interval: float
    max_pages: int
    mode: str               # "watch" (alert on any match) or "deal" (alert on good, affordable deals)
    ref_id: int = 0
    notify: bool = True
    pax: int = 1
    min_score: float = 1.0
    leg: str = "out"
    scope: dict = field(default_factory=dict)


def _split(s) -> list:
    return [x.strip() for x in str(s or "").split(",") if x.strip()]


def _clip(start: str, end: str):
    start = max(start or "", date.today().isoformat())
    return (start, end) if end and start <= end else None


def _search_job(key, kind, label, o_text, d_text, rng, cabins, crit_kw, **kw) -> Job:
    oa, orr = parse_places(o_text)
    da, dr = parse_places(d_text)
    q_o, q_d = query_airports(o_text), query_airports(d_text)
    crit = Criteria(origins=oa, origin_regions=orr, dests=da, dest_regions=dr,
                    date_from=rng[0], date_to=rng[1], cabins=cabins, **crit_kw)
    params = {"origin_airport": q_o, "destination_airport": q_d, "start_date": rng[0], "end_date": rng[1],
              "cabins": ",".join(config.CABIN_NAMES[c] for c in cabins),
              "only_direct_flights": True if crit.direct_only else None,
              "sources": ",".join(sorted(crit.sources)) or None}
    scope = {"origins": q_o.split(","), "dests": q_d.split(","), "sources": sorted(crit.sources),
             "date_from": rng[0], "date_to": rng[1], "cabins": cabins, "skip": crit.direct_only}
    return Job(key=key, kind=kind, label=label, endpoint="search", params=params, crit=crit, scope=scope, **kw)


def watch_jobs(conn, watch_id=None) -> list:
    """Jobs for enabled watches, or for one watch (enabled or not) when watch_id is given."""
    out = []
    q = "SELECT * FROM watches WHERE " + ("id = ?" if watch_id else "enabled = 1")
    for w in db.rows(conn, q, (watch_id,) if watch_id else ()):
        legs = [("out", w["origins"], w["destinations"], w["depart_start"], w["depart_end"])]
        if w["return_start"] and w["return_end"]:
            legs.append(("ret", w["destinations"], w["origins"], w["return_start"], w["return_end"]))
        for leg, o, d, s, e in legs:
            rng = _clip(s, e)
            if not rng:
                continue
            try:
                out.append(_search_job(
                    f"watch:{w['id']}:{leg}", "watch", w["name"] + (" (return)" if leg == "ret" else ""),
                    o, d, rng, cabin_codes(w["cabins"]),
                    dict(max_miles=w["max_miles"], pax=w["pax"], direct_only=bool(w["direct_only"]),
                         sources=set(_split(w["sources"]))),
                    priority=w["priority"], interval=w["interval_min"] * MIN, min_interval=15 * MIN,
                    max_pages=3, mode="watch", ref_id=w["id"], notify=bool(w["notify"]), pax=w["pax"], leg=leg))
            except ValueError:
                continue
    return out


def funded_sources(balances: dict) -> list:
    if not any(balances.values()):
        return FALLBACK_SOURCES
    return [s for s in config.SOURCE_NAMES if matching.funding(s, 1, balances)["capacity"] > 0]


def window_jobs(conn, settings: dict, balances: dict) -> list:
    out = []
    for w in db.rows(conn, "SELECT * FROM windows WHERE enabled = 1"):
        rng = _clip(w["start_date"], w["end_date"])
        if not rng:
            continue
        try:
            cabins = cabin_codes(w["cabins"])
            da, dr = parse_places(w["destinations"])
            o_text = w["origins"] or settings["home_airports"]
            oa, orr = parse_places(o_text)
        except ValueError:
            continue
        min_score = min(w["min_score"], settings["judge_min_score"]) if settings.get("engine") == "cloud" else w["min_score"]
        common = dict(mode="deal", ref_id=w["id"], notify=bool(w["notify"]), pax=w["pax"], min_score=min_score)
        ckw = dict(pax=w["pax"], direct_only=bool(w["direct_only"]))
        legs = [("out", o_text, w["destinations"])] + ([("ret", w["destinations"], o_text)] if w["roundtrip"] else [])
        if o_text:
            for leg, o, d in legs:
                out.append(_search_job(f"window:{w['id']}:{leg}", "window",
                                       w["name"] + (" (return)" if leg == "ret" else ""), o, d, rng, cabins, ckw,
                                       priority=2, interval=90 * MIN, min_interval=30 * MIN, max_pages=4,
                                       leg=leg, **common))
        if not w["explore"]:
            continue
        o_regions = sorted(orr | {config.AIRPORT_REGION[a] for a in oa if a in config.AIRPORT_REGION}) or ["North America"]
        d_regions = sorted(dr | {config.AIRPORT_REGION[a] for a in da if a in config.AIRPORT_REGION})
        cabin_param = config.CABIN_NAMES[cabins[0]] if len(cabins) == 1 else None
        for src in funded_sources(balances):
            for o_reg in o_regions:
                for d_reg in d_regions:
                    pairs = [("out", o_reg, d_reg)] + ([("ret", d_reg, o_reg)] if w["roundtrip"] else [])
                    for leg, a, b in pairs:
                        if leg == "out":
                            crit = Criteria(origin_regions={a}, dests=da, dest_regions=dr, **ckw)
                        else:
                            crit = Criteria(origins=da, origin_regions=dr, dest_regions={b}, **ckw)
                        crit.date_from, crit.date_to, crit.cabins, crit.sources = rng[0], rng[1], cabins, {src}
                        params = {"source": src, "cabin": cabin_param, "start_date": rng[0], "end_date": rng[1],
                                  "origin_region": a, "destination_region": b}
                        scope = {"sources": [src], "origin_region": a, "dest_region": b, "date_from": rng[0],
                                 "date_to": rng[1], "cabins": cabins, "skip": crit.direct_only}
                        out.append(Job(key=f"explore:{w['id']}:{src}:{a}>{b}", kind="explore",
                                       label=f"{w['name']} · {config.SOURCE_NAMES.get(src, src)} {a}→{b}",
                                       endpoint="availability", params=params, crit=crit, scope=scope,
                                       priority=1, interval=6 * 60 * MIN, min_interval=2 * 60 * MIN,
                                       max_pages=3, leg=leg, **common))
    return out


def all_jobs(conn) -> list:
    return watch_jobs(conn) + window_jobs(conn, db.get_settings(conn), db.get_balances(conn))


def job_states(conn) -> dict:
    return {r["key"]: r for r in db.rows(conn, "SELECT * FROM job_state")}


def pick_job(conn, now: float, jobs=None):
    """Most overdue job, weighted by priority. Never-run jobs go first."""
    jobs = all_jobs(conn) if jobs is None else jobs
    states = job_states(conn)
    best = None
    for j in jobs:
        st = states.get(j.key)
        if st and st["last_run"]:
            age = now - st["last_run"]
            if age < j.min_interval or (st["last_error"] and age < 30 * MIN):
                continue
            sc = j.priority * age / j.interval
        else:
            sc = 1e6 * j.priority
        if best is None or sc > best[0]:
            best = (sc, j)
    return best[1] if best else None


# ---------------------------------------------------------------- running a job

def _store(conn, rows: list, t0: float) -> None:
    conn.execute("BEGIN")
    conn.executemany(
        "INSERT OR REPLACE INTO availability(id, source, origin, dest, origin_region, dest_region, distance, date,"
        " cabins, currency, updated_at, fetched_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        [(r["id"], r["source"], r["origin"], r["dest"], r["origin_region"], r["dest_region"], r["distance"],
          r["date"], json.dumps(r["cabins"]), r["currency"], r["updated_at"], t0) for r in rows if r["id"]])
    conn.execute("COMMIT")


def _cleanup_stale(conn, job: Job, t0: float) -> None:
    """Rows this query covered but didn't return are gone: drop the queried cabins from them."""
    sc = job.scope
    if sc.get("skip"):
        return
    sql, args = "SELECT id, cabins FROM availability WHERE fetched_at < ? AND date BETWEEN ? AND ?", [t0, sc["date_from"], sc["date_to"]]
    for col, key in (("origin", "origins"), ("dest", "dests"), ("source", "sources")):
        if sc.get(key):
            sql += f" AND {col} IN ({','.join('?' * len(sc[key]))})"
            args += sc[key]
    for col in ("origin_region", "dest_region"):
        if sc.get(col):
            sql += f" AND {col} = ?"
            args.append(sc[col])
    conn.execute("BEGIN")
    for r in conn.execute(sql, args).fetchall():
        cab = json.loads(r["cabins"])
        for c in sc["cabins"]:
            cab.pop(c, None)
        if cab:
            conn.execute("UPDATE availability SET cabins = ? WHERE id = ?", (json.dumps(cab), r["id"]))
        else:
            conn.execute("DELETE FROM availability WHERE id = ?", (r["id"],))
    conn.execute("COMMIT")


def evaluate(job: Job, rows: list, settings: dict, balances: dict) -> list:
    seen, out = set(), []
    for r in rows:
        for o in job.crit.offers(r):
            k = (o["avail_id"], o["cabin"])
            if k in seen:
                continue
            seen.add(k)
            o = matching.enrich(o, settings, balances, job.pax)
            if job.mode == "deal" and (o["score"] < job.min_score or o["affordable"] is False):
                continue
            o["leg"] = job.leg
            out.append(o)
    return out


def _diff(conn, job: Job, offers: list, truncated: bool, now: float) -> list:
    existing = {(r["avail_id"], r["cabin"]): r for r in db.rows(conn, "SELECT * FROM matches WHERE owner = ?", (job.key,))}
    new, current = [], set()
    conn.execute("BEGIN")
    for o in offers:
        k = (o["avail_id"], o["cabin"])
        current.add(k)
        ex = existing.get(k)
        if ex is None or not ex["active"]:
            o["why"] = "new" if ex is None else "back"
            new.append(o)
        elif o["cost"] < ex["cost"] * 0.95:
            o["why"] = f"was {ex['cost']:,}"
            new.append(o)
        conn.execute("INSERT OR REPLACE INTO matches(owner, avail_id, cabin, cost, active, first_seen, last_seen)"
                     " VALUES(?,?,?,?,1,?,?)", (job.key, k[0], k[1], o["cost"], ex["first_seen"] if ex else now, now))
    if not truncated:
        for k, ex in existing.items():
            if k not in current and ex["active"]:
                conn.execute("UPDATE matches SET active = 0 WHERE owner = ? AND avail_id = ? AND cabin = ?", (job.key, *k))
    conn.execute("COMMIT")
    return new


def fmt_k(n: int) -> str:
    return f"{n / 1000:.1f}".rstrip("0").rstrip(".") + "k"


def seats_link(o: dict) -> str:
    return f"https://seats.aero/search?origins={o['origin']}&destinations={o['dest']}&date={o['date']}"


def offer_line(o: dict) -> str:
    d = datetime.strptime(o["date"], "%Y-%m-%d").strftime("%a %b %-d")
    bits = [f"{d} {o['origin']}→{o['dest']} {config.CABIN_LABELS[o['cabin']]} {fmt_k(o['cost'])}",
            config.SHORT_NAMES.get(o["source"], o["source"])]
    if o["direct"]:
        bits.append("nonstop")
    if o["seats"]:
        bits.append(f"{o['seats']} seat{'s' if o['seats'] > 1 else ''}")
    if o["taxes"] and o["currency"] in ("USD", ""):
        bits.append(f"${o['taxes'] / 100:,.0f} tax")
    return " · ".join(bits)


def _alert(conn, job: Job, new: list, now: float) -> None:
    fresh = []
    for o in new:
        n = conn.execute("SELECT cost, at FROM notified WHERE avail_id = ? AND cabin = ?", (o["avail_id"], o["cabin"])).fetchone()
        if n and now - n["at"] < 12 * 3600 and o["cost"] >= n["cost"] * 0.95:
            continue
        fresh.append(o)
    if not fresh:
        return
    if job.mode == "deal":
        fresh.sort(key=lambda o: -o["score"])
    else:
        fresh.sort(key=lambda o: (o["date"], o["cost"]))
    conn.execute("BEGIN")
    for o in fresh:
        conn.execute("INSERT OR REPLACE INTO notified(avail_id, cabin, cost, at) VALUES(?,?,?,?)",
                     (o["avail_id"], o["cabin"], o["cost"], now))
    conn.execute("COMMIT")
    top = fresh[0]
    what = "deal" if job.mode == "deal" else "match"
    if len(fresh) == 1:
        o = top
        title = f"✈ {o['origin']}→{o['dest']} {config.CABIN_LABELS[o['cabin']]} {fmt_k(o['cost'])}"
        if o.get("rating") == "great" and job.mode == "deal":
            title += " · great deal"
        body = offer_line(o) + (f"\nPay: {o['pay_with']}" if o.get("pay_with") else "") + f"\n{job.label}"
    else:
        title = f"✈ {len(fresh)} new {what}es · {job.label}"
        body = "\n".join(offer_line(o) for o in fresh[:4])
        if len(fresh) > 4:
            body += f"\n+{len(fresh) - 4} more on the dashboard"
    notify.send(conn, title, body, seats_link(top), kind=job.kind)


def _summary(conn, job: Job, offers: list) -> None:
    best = sorted(offers, key=lambda o: -o["score"] if job.mode == "deal" else o["cost"])[:3]
    what = "deals" if job.mode == "deal" else "options"
    notify.send(conn, f"✈ Now watching: {job.label}", f"{len(offers)} {what} available right now. Best:\n"
                + "\n".join(offer_line(o) for o in best), seats_link(best[0]), kind=job.kind)


def _queue(conn, job: Job, offers: list, now: float) -> int:
    """Queue candidates for Claude. Skips ones already judged at the same price."""
    n = 0
    conn.execute("BEGIN")
    for o in offers:
        v = conn.execute("SELECT cost FROM verdicts WHERE avail_id = ? AND cabin = ?", (o["avail_id"], o["cabin"])).fetchone()
        if v and v["cost"] == o["cost"]:
            continue
        payload = dict(o, owner_label=job.label, owner_kind=job.kind, ref_id=job.ref_id)
        conn.execute("INSERT OR REPLACE INTO pending(avail_id, cabin, owner, cost, kind, payload, created) VALUES(?,?,?,?,?,?,?)",
                     (o["avail_id"], o["cabin"], job.key, o["cost"], "watch" if job.mode == "watch" else "deal",
                      json.dumps(payload, default=str), now))
        n += 1
    conn.execute("COMMIT")
    return n


def run_job(conn, job: Job, floor: int, on_demand: bool = False, persist: bool = True, judge: bool = False) -> dict:
    t0 = time.time()
    purpose = ("ondemand:" if on_demand else "bg:") + job.key
    result = {"key": job.key, "label": job.label, "calls": 0, "rows": 0, "matches": 0, "new": 0, "truncated": False}
    try:
        raw, calls, truncated = api.paged(conn, job.endpoint, job.params, purpose, job.max_pages, floor)
    except api.BudgetExhausted:
        raise
    except (api.ApiError, ValueError) as e:
        calls = conn.execute("SELECT COUNT(*) FROM api_calls WHERE purpose = ? AND ts >= ?", (purpose, t0)).fetchone()[0]
        if persist:
            conn.execute("INSERT OR REPLACE INTO job_state(key, last_run, last_calls, last_rows, last_matches, last_error, truncated)"
                         " VALUES(?,?,?,0,0,?,0)", (job.key, t0, calls, str(e)[:300]))
        result.update(calls=calls, error=str(e))
        return result
    rows = [matching.compact(r) for r in raw]
    _store(conn, rows, t0)
    if not truncated:
        _cleanup_stale(conn, job, t0)
    offers = evaluate(job, rows, db.get_settings(conn), db.get_balances(conn))
    result.update(calls=calls, rows=len(rows), matches=len(offers), truncated=truncated, offers=offers)
    if persist:
        first = conn.execute("SELECT 1 FROM job_state WHERE key = ?", (job.key,)).fetchone() is None
        new = _diff(conn, job, offers, truncated, t0)
        result["new"] = len(new)
        if judge:
            # Claude decides what's worth an alert. A watch's first run is a silent baseline (you saw
            # the results when you added it); a window's first run queues the deals available today.
            if job.notify and not (first and job.mode == "watch"):
                result["queued"] = _queue(conn, job, offers if first else new, t0)
        elif first:
            # Baseline run: everything is "new". Don't fire an alert per item; when this ran in the
            # background (nobody saw the results), send one summary instead.
            if offers and job.notify and not on_demand and job.kind != "explore":
                _summary(conn, job, offers)
        elif new and job.notify:
            _alert(conn, job, new, t0)
        conn.execute("INSERT OR REPLACE INTO job_state(key, last_run, last_calls, last_rows, last_matches, last_error, truncated)"
                     " VALUES(?,?,?,?,?,NULL,?)", (job.key, t0, calls, len(rows), len(offers), int(truncated)))
    return result


# ---------------------------------------------------------------- views over the local cache

def cached_offers(conn, job: Job, settings: dict, balances: dict) -> list:
    sql, args = "SELECT * FROM availability WHERE date BETWEEN ? AND ?", [job.crit.date_from, job.crit.date_to]
    if job.crit.sources:
        sql += f" AND source IN ({','.join('?' * len(job.crit.sources))})"
        args += sorted(job.crit.sources)
    rows = []
    for r in db.rows(conn, sql, args):
        r["cabins"] = json.loads(r["cabins"])
        rows.append(r)
    offers = evaluate(job, rows, settings, balances)
    fetched = {r["id"]: r["fetched_at"] for r in rows}
    verdicts = {(v["avail_id"], v["cabin"]): v for v in db.rows(conn, "SELECT * FROM verdicts")}
    for o in offers:
        o["fetched_at"] = fetched.get(o["avail_id"])
        o["link"] = seats_link(o)
        v = verdicts.get((o["avail_id"], o["cabin"]))
        if v:
            o["verdict"], o["reason"] = v["verdict"], v["reason"]
            o["verdict_stale"] = v["cost"] != o["cost"]
    return offers


def watch_view(conn, watch_id: int) -> list:
    settings, balances = db.get_settings(conn), db.get_balances(conn)
    out = []
    for j in watch_jobs(conn, watch_id):
        out += cached_offers(conn, j, settings, balances)
    uniq = {(o["avail_id"], o["cabin"]): o for o in out}
    return sorted(uniq.values(), key=lambda o: (o["leg"], o["date"], o["cost"]))


def deals_view(conn, window_id=None) -> list:
    settings, balances = db.get_settings(conn), db.get_balances(conn)
    home = set()
    out = {}
    for j in window_jobs(conn, settings, balances):
        if window_id and j.ref_id != window_id:
            continue
        w = conn.execute("SELECT origins FROM windows WHERE id = ?", (j.ref_id,)).fetchone()
        home = parse_places(w["origins"] or settings["home_airports"])[0]
        for o in cached_offers(conn, j, settings, balances):
            k = (o["avail_id"], o["cabin"])
            if k in out:
                continue
            o["window_id"] = j.ref_id
            o["positioning"] = bool(home) and (o["origin"] if o["leg"] == "out" else o["dest"]) not in home
            out[k] = o
    return sorted(out.values(), key=lambda o: -o["score"])


def housekeeping(conn) -> None:
    today = date.today().isoformat()
    now = time.time()
    conn.execute("DELETE FROM availability WHERE date < ?", (today,))
    conn.execute("DELETE FROM matches WHERE last_seen < ?", (now - 14 * 86400,))
    conn.execute("DELETE FROM notified WHERE at < ?", (now - 30 * 86400,))
    conn.execute("DELETE FROM api_calls WHERE ts < ?", (now - 60 * 86400,))
    conn.execute("DELETE FROM events WHERE ts < ?", (now - 60 * 86400,))
    conn.execute("UPDATE watches SET enabled = 0 WHERE enabled = 1 AND max(depart_end, coalesce(return_end, '')) < ?", (today,))
    conn.execute("UPDATE windows SET enabled = 0 WHERE enabled = 1 AND end_date < ?", (today,))
    live = {j.key for j in all_jobs(conn)}
    for k in [r["key"] for r in db.rows(conn, "SELECT key FROM job_state")]:
        if k not in live:
            conn.execute("DELETE FROM job_state WHERE key = ?", (k,))
