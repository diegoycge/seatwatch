"""Operations shared by the web UI and the CLI."""
from __future__ import annotations

import re
import time
from datetime import date

from . import api, config, db, jobs, matching
from .matching import Criteria, cabin_codes, parse_places

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

WATCH_FIELDS = {"name": str, "description": str, "origins": str, "destinations": str, "depart_start": str,
                "depart_end": str, "return_start": str, "return_end": str, "cabins": str, "max_miles": int,
                "pax": int, "direct_only": bool, "sources": str, "priority": int, "interval_min": int,
                "enabled": bool, "notify": bool}
WINDOW_FIELDS = {"name": str, "start_date": str, "end_date": str, "origins": str, "destinations": str,
                 "cabins": str, "pax": int, "direct_only": bool, "roundtrip": bool, "explore": bool,
                 "min_score": float, "notify": bool, "enabled": bool}


def _date(v, field, required=True):
    if not v:
        if required:
            raise ValueError(f"{field} is required (YYYY-MM-DD)")
        return None
    v = str(v).strip()
    if not DATE_RE.match(v):
        raise ValueError(f"{field} must be YYYY-MM-DD, got '{v}'")
    date.fromisoformat(v)
    return v


def _clean(d: dict, fields: dict) -> dict:
    out = {}
    for k, typ in fields.items():
        if k not in d:
            continue
        v = d[k]
        if v in ("", None):
            out[k] = None
        elif typ is bool:
            out[k] = 1 if v in (True, 1, "1", "true", "on", "yes") else 0
        elif typ in (int, float):
            out[k] = typ(str(v).replace(",", "").replace("k", "000"))
        else:
            out[k] = str(v).strip()
    return out


def _validate_places(v, field, required=True):
    if not v:
        if required:
            raise ValueError(f"{field} is required")
        return None
    a, r = parse_places(v)
    if not a and not r:
        raise ValueError(f"{field} is empty")
    return ",".join(sorted(a) + sorted(r))


def normalize_watch(d: dict, existing: dict = None) -> dict:
    w = dict(existing or {})
    w.update(_clean(d, WATCH_FIELDS))
    w["origins"] = _validate_places(w.get("origins"), "origins")
    w["destinations"] = _validate_places(w.get("destinations"), "destinations")
    w["depart_start"] = _date(w.get("depart_start"), "depart_start")
    w["depart_end"] = _date(w.get("depart_end"), "depart_end")
    w["return_start"] = _date(w.get("return_start"), "return_start", False)
    w["return_end"] = _date(w.get("return_end"), "return_end", False)
    if bool(w["return_start"]) != bool(w["return_end"]):
        raise ValueError("give both return_start and return_end, or neither")
    if w["depart_end"] < w["depart_start"] or (w["return_end"] and w["return_end"] < w["return_start"]):
        raise ValueError("end date is before start date")
    w["cabins"] = ",".join(config.CABIN_NAMES[c] for c in cabin_codes(w.get("cabins") or "business"))
    for s in (w.get("sources") or "").split(","):
        if s.strip() and s.strip() not in config.SOURCE_NAMES:
            raise ValueError(f"unknown program '{s.strip()}' (use seats.aero source codes like united, aeroplan)")
    w.setdefault("pax", 1)
    w["pax"] = max(1, int(w.get("pax") or 1))
    w["priority"] = min(5, max(1, int(w.get("priority") or 3)))
    w["interval_min"] = max(15, int(w.get("interval_min") or 45))
    if not w.get("name"):
        w["name"] = f"{w['origins']} → {w['destinations']}"
    for k in ("direct_only",):
        w[k] = int(w.get(k) or 0)
    for k in ("enabled", "notify"):
        w[k] = 1 if w.get(k) is None else int(w[k])
    return w


def normalize_window(d: dict, existing: dict = None) -> dict:
    w = dict(existing or {})
    w.update(_clean(d, WINDOW_FIELDS))
    w["start_date"] = _date(w.get("start_date"), "start_date")
    w["end_date"] = _date(w.get("end_date"), "end_date")
    if w["end_date"] < w["start_date"]:
        raise ValueError("end date is before start date")
    w["destinations"] = _validate_places(w.get("destinations"), "destinations")
    w["origins"] = _validate_places(w.get("origins"), "origins", False)
    w["cabins"] = ",".join(config.CABIN_NAMES[c] for c in cabin_codes(w.get("cabins") or "business"))
    w["pax"] = max(1, int(w.get("pax") or 1))
    w["min_score"] = float(w.get("min_score") or 1.0)
    if not w.get("name"):
        w["name"] = f"{w['destinations']} {w['start_date']}"
    for k, default in (("direct_only", 0), ("roundtrip", 1), ("explore", 1), ("notify", 1), ("enabled", 1)):
        w[k] = default if w.get(k) is None else int(w[k])
    return w


def _upsert(conn, table: str, row: dict, row_id=None) -> int:
    row = {k: v for k, v in row.items() if k not in ("id", "created_at")}
    if row_id:
        conn.execute(f"UPDATE {table} SET {', '.join(k + ' = ?' for k in row)} WHERE id = ?", (*row.values(), row_id))
        return row_id
    row["created_at"] = time.time()
    cur = conn.execute(f"INSERT INTO {table}({', '.join(row)}) VALUES({', '.join('?' * len(row))})", tuple(row.values()))
    return cur.lastrowid


def save_watch(conn, d: dict, watch_id=None) -> int:
    existing = None
    if watch_id:
        existing = conn.execute("SELECT * FROM watches WHERE id = ?", (watch_id,)).fetchone()
        if not existing:
            raise ValueError(f"no watch #{watch_id}")
        existing = dict(existing)
    return _upsert(conn, "watches", normalize_watch(d, existing), watch_id)


def save_window(conn, d: dict, window_id=None) -> int:
    existing = None
    if window_id:
        existing = conn.execute("SELECT * FROM windows WHERE id = ?", (window_id,)).fetchone()
        if not existing:
            raise ValueError(f"no travel window #{window_id}")
        existing = dict(existing)
    return _upsert(conn, "windows", normalize_window(d, existing), window_id)


def delete(conn, table: str, row_id: int) -> None:
    prefix = {"watches": "watch", "windows": "window"}[table]
    conn.execute(f"DELETE FROM {table} WHERE id = ?", (row_id,))
    for p in ([f"{prefix}:{row_id}:%"] + ([f"explore:{row_id}:%"] if prefix == "window" else [])):
        conn.execute("DELETE FROM matches WHERE owner LIKE ?", (p,))
        conn.execute("DELETE FROM job_state WHERE key LIKE ?", (p,))


def pacing(conn, settings=None) -> dict:
    settings = settings or db.get_settings(conn)
    b = api.budget(conn)
    reserve = float(settings["ondemand_reserve"]) * min(1.0, b["seconds_to_reset"] / 86400)
    spendable = max(0.0, b["remaining"] - reserve - config.HARD_FLOOR)
    rate = spendable / max(600.0, b["seconds_to_reset"])
    return dict(b, reserve=int(reserve), spendable=int(spendable), rate_per_hour=round(rate * 3600, 1),
                bg_floor=int(reserve) + config.HARD_FLOOR, paused=bool(settings.get("paused")))


def run_now(conn, kind: str, row_id: int) -> list:
    """Run a watch's or window's search jobs immediately (spends from the on-demand reserve)."""
    if kind == "watch":
        js = jobs.watch_jobs(conn, row_id)
    else:
        js = [j for j in jobs.window_jobs(conn, db.get_settings(conn), db.get_balances(conn))
              if j.ref_id == row_id and j.kind == "window"]
    out = []
    persist = db.get_settings(conn).get("engine") != "cloud"  # in cloud mode the routine owns alerting state
    for j in js:
        r = jobs.run_job(conn, j, floor=config.HARD_FLOOR, on_demand=True, persist=persist)
        r.pop("offers", None)
        out.append(r)
    return out


def adhoc_search(conn, origins, destinations, start, end, cabins="business", direct=False, max_miles=None,
                 pax=1, sources="") -> list:
    start, end = _date(start, "start"), _date(end, "end")
    cab = cabin_codes(cabins)
    oa, orr = parse_places(origins)
    da, dr = parse_places(destinations)
    crit = Criteria(origins=oa, origin_regions=orr, dests=da, dest_regions=dr, date_from=start, date_to=end,
                    cabins=cab, max_miles=int(max_miles) if max_miles else None, pax=int(pax or 1),
                    direct_only=bool(direct), sources=set(filter(None, (sources or "").split(","))))
    job = jobs.Job(key="adhoc", kind="adhoc", label="search", endpoint="search", crit=crit, priority=0,
                   interval=1, min_interval=0, max_pages=2, mode="watch", notify=False, pax=int(pax or 1),
                   params={"origin_airport": matching.query_airports(origins),
                           "destination_airport": matching.query_airports(destinations),
                           "start_date": start, "end_date": end,
                           "cabins": ",".join(config.CABIN_NAMES[c] for c in cab),
                           "only_direct_flights": True if direct else None, "sources": sources or None})
    r = jobs.run_job(conn, job, floor=config.HARD_FLOOR, on_demand=True, persist=False)
    if r.get("error"):
        raise api.ApiError(r["error"])
    offers = r.get("offers", [])
    for o in offers:
        o["link"] = jobs.seats_link(o)
    return sorted(offers, key=lambda o: (o["cost"], o["date"]))


def trips(conn, avail_id: str) -> list:
    body = api.get(conn, f"trips/{avail_id}", {}, "ondemand:trips", config.HARD_FLOOR)
    out = []
    for t in body.get("data") or []:
        out.append({
            "cabin": t.get("Cabin"), "miles": t.get("MileageCost"), "taxes": (t.get("TotalTaxes") or 0) / 100,
            "currency": t.get("TaxesCurrency") or "USD", "seats": t.get("RemainingSeats"), "stops": t.get("Stops"),
            "flights": t.get("FlightNumbers"), "carriers": t.get("Carriers"), "departs": t.get("DepartsAt"),
            "arrives": t.get("ArrivesAt"), "duration_min": t.get("TotalDuration"), "source": t.get("Source"),
            "segments": [{"flight": s.get("FlightNumber"), "from": s.get("OriginAirport"), "to": s.get("DestinationAirport"),
                          "departs": s.get("DepartsAt"), "arrives": s.get("ArrivesAt"), "aircraft": s.get("AircraftName"),
                          "fare_class": s.get("FareClass")} for s in sorted(t.get("AvailabilitySegments") or [], key=lambda s: s.get("Order", 0))],
        })
    return sorted(out, key=lambda t: (t["miles"] or 0, t["duration_min"] or 0))


def job_table(conn) -> list:
    states = jobs.job_states(conn)
    now = time.time()
    out = []
    for j in jobs.all_jobs(conn):
        st = states.get(j.key) or {}
        last = st.get("last_run")
        out.append({"key": j.key, "kind": j.kind, "label": j.label, "ref_id": j.ref_id, "leg": j.leg,
                    "priority": j.priority, "interval_min": int(j.interval / 60), "last_run": last,
                    "age_min": int((now - last) / 60) if last else None, "calls": st.get("last_calls"),
                    "rows": st.get("last_rows"), "matches": st.get("last_matches"), "error": st.get("last_error"),
                    "truncated": bool(st.get("truncated"))})
    return out


def buying_power(balances: dict) -> list:
    """Miles you could have in each searchable program (own balance + best transfers)."""
    out = []
    for src, name in config.AIRLINE_ACCOUNTS:
        f = matching.funding(src, 10 ** 9, balances)
        routes = [{"from": b, "name": config.ACCOUNT_NAMES[b], "ratio": r, "balance": balances.get(b, 0)}
                  for b, r in matching.partners().get(src, [])]
        out.append({"source": src, "name": name, "capacity": f["capacity"], "own": balances.get(src, 0),
                    "transfer_from": routes})
    return sorted(out, key=lambda x: -x["capacity"])
