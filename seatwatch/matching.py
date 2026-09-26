"""Turning availability rows into offers: filtering, deal scoring, and paying with your points."""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Optional

from . import config

_PARTNERS = None
AVIOS = ("qatar", "finnair")  # Avios move 1:1 between these (and BA/Iberia/Aer Lingus)


def partners() -> dict:
    """source -> [(bank_account, airline miles per bank point)], best ratio first."""
    global _PARTNERS
    if _PARTNERS is None:
        raw = json.loads((config.PKG_DIR / "data/partners.json").read_text())
        best: dict = {}
        for bank, spec in raw["currencies"].items():
            for p in spec["partners"]:
                targets = list(AVIOS) if p.get("avios") else [p.get("source")]
                for src in filter(None, targets):
                    best[(src, bank)] = max(best.get((src, bank), 0.0), float(p["ratio"]))
        out: dict = {}
        for (src, bank), ratio in best.items():
            out.setdefault(src, []).append((bank, ratio))
        for v in out.values():
            v.sort(key=lambda x: -x[1])
        _PARTNERS = out
    return _PARTNERS


def parse_places(text) -> tuple:
    """'NYC, TYO, Europe' -> ({'JFK','EWR','LGA','NRT','HND'}, {'Europe'})."""
    airports, regions = set(), set()
    region_lc = {r.lower(): r for r in config.REGIONS}
    for tok in str(text or "").replace(";", ",").split(","):
        t = tok.strip()
        if not t:
            continue
        if t.lower() in region_lc:
            regions.add(region_lc[t.lower()])
            continue
        t = t.upper()
        metro = config.METROS.get(t) or config.METROS.get(t + "*")
        if metro:
            airports.update(metro.split(","))
        elif len(t) == 3 and t.isalpha():
            airports.add(t)
        else:
            raise ValueError(f"'{tok.strip()}' is not an airport code, metro code, or region")
    return airports, regions


def query_airports(text) -> str:
    """Airport list for Cached Search: explicit airports plus each named region's major airports."""
    airports, regions = parse_places(text)
    for r in regions:
        airports.update(config.REGION_AIRPORTS[r].split(","))
    return ",".join(sorted(airports))


def cabin_codes(text) -> list:
    out = []
    for tok in str(text or "").replace(";", ",").split(","):
        t = tok.strip().lower()
        if not t:
            continue
        code = config.CABINS.get(t) or (t.upper() if t.upper() in "YWJF" else None)
        if t in ("prem", "premium economy", "premium_economy"):
            code = "W"
        if not code:
            raise ValueError(f"unknown cabin '{tok.strip()}' (use economy, premium, business, first)")
        if code not in out:
            out.append(code)
    return out or ["J"]


def _int(v) -> int:
    try:
        return int(float(v or 0))
    except (TypeError, ValueError):
        return 0


def compact(r: dict) -> dict:
    route = r.get("Route") or {}
    cab = {}
    for c in "YWJF":
        cost = _int(r.get(f"{c}MileageCost"))
        if not r.get(f"{c}Available") or cost <= 0:
            continue
        cab[c] = {
            "cost": cost, "taxes": _int(r.get(f"{c}TotalTaxes")), "seats": _int(r.get(f"{c}RemainingSeats")),
            "airlines": r.get(f"{c}Airlines") or "", "direct": bool(r.get(f"{c}Direct")),
            "dcost": _int(r.get(f"{c}DirectMileageCost")), "dtaxes": _int(r.get(f"{c}DirectTotalTaxes")),
            "dseats": _int(r.get(f"{c}DirectRemainingSeats")), "dairlines": r.get(f"{c}DirectAirlines") or "",
        }
    return {
        "id": r.get("ID"), "source": r.get("Source") or route.get("Source"),
        "origin": route.get("OriginAirport"), "dest": route.get("DestinationAirport"),
        "origin_region": route.get("OriginRegion"), "dest_region": route.get("DestinationRegion"),
        "distance": _int(route.get("Distance")), "date": (r.get("Date") or "")[:10],
        "cabins": cab, "currency": r.get("TaxesCurrency") or "", "updated_at": r.get("UpdatedAt"),
    }


@dataclass
class Criteria:
    origins: set = field(default_factory=set)        # empty = any
    origin_regions: set = field(default_factory=set)
    dests: set = field(default_factory=set)
    dest_regions: set = field(default_factory=set)
    date_from: str = ""
    date_to: str = "9999-12-31"
    cabins: list = field(default_factory=lambda: ["J"])
    max_miles: Optional[int] = None
    pax: int = 1
    direct_only: bool = False
    sources: set = field(default_factory=set)

    @staticmethod
    def _place_ok(ap, region, airports, regions) -> bool:
        if not airports and not regions:
            return True
        return ap in airports or (region in regions) or (config.AIRPORT_REGION.get(ap) in regions)

    def offers(self, row: dict) -> list:
        if not (self.date_from <= row["date"] <= self.date_to):
            return []
        if self.sources and row["source"] not in self.sources:
            return []
        if not self._place_ok(row["origin"], row["origin_region"], self.origins, self.origin_regions):
            return []
        if not self._place_ok(row["dest"], row["dest_region"], self.dests, self.dest_regions):
            return []
        out = []
        for c in self.cabins:
            d = row["cabins"].get(c)
            if not d:
                continue
            if self.direct_only:
                if not d["direct"]:
                    continue
                cost, taxes = d["dcost"] or d["cost"], d["dtaxes"] or d["taxes"]
                seats, airlines = d["dseats"] or d["seats"], d["dairlines"] or d["airlines"]
            else:
                cost, taxes, seats, airlines = d["cost"], d["taxes"], d["seats"], d["airlines"]
            if self.max_miles and cost > self.max_miles:
                continue
            if self.pax > 1 and 0 < seats < self.pax:
                continue
            out.append({
                "avail_id": row["id"], "source": row["source"], "origin": row["origin"], "dest": row["dest"],
                "date": row["date"], "distance": row["distance"], "cabin": c, "cost": cost, "taxes": taxes,
                "currency": row["currency"], "seats": seats, "airlines": airlines,
                "direct": d["direct"] if not self.direct_only else True,
                "updated_at": row.get("updated_at"),
            })
        return out


def threshold(settings: dict, cabin: str, distance: int) -> int:
    table = settings["thresholds"][cabin]
    for i, upper in enumerate(config.DISTANCE_BANDS):
        if distance <= upper:
            return table[i]
    return table[-1]


def score(offer: dict, settings: dict) -> dict:
    thr = threshold(settings, offer["cabin"], offer["distance"] or 0)
    penalty = 0.0
    if offer["currency"] in ("USD", "") and offer["taxes"]:
        taxes_usd = offer["taxes"] / 100
        penalty = max(0.0, taxes_usd - float(settings["tax_allowance_usd"])) / (float(settings["cpp"]) / 100)
    eff = offer["cost"] + penalty
    s = thr / eff if eff else 0
    return {"score": round(s, 3), "threshold": thr, "effective": int(eff),
            "rating": "great" if s >= settings["great_score"] else "good" if s >= 1 else "meh"}


def funding(source: str, needed: int, balances: dict) -> dict:
    """How to pay `needed` miles in `source` from the airline balance plus bank transfers."""
    plan, remaining = [], needed
    own = sum(balances.get(a, 0) for a in AVIOS) if source in AVIOS else balances.get(source, 0)
    if own and remaining > 0:
        use = min(own, remaining)
        plan.append({"from": source, "points": use, "miles": use, "ratio": 1.0})
        remaining -= use
    capacity = own
    for bank, ratio in partners().get(source, []):
        bal = balances.get(bank, 0)
        capacity += int(bal * ratio)
        if remaining <= 0 or bal <= 0:
            continue
        want = int(math.ceil(math.ceil(remaining / ratio) / 1000.0) * 1000)  # transfers go in 1,000s
        pts = min(want, bal - bal % 1000)
        if pts <= 0:
            continue
        miles = int(pts * ratio)
        plan.append({"from": bank, "points": pts, "miles": miles, "ratio": ratio})
        remaining -= miles
    return {"ok": remaining <= 0, "short": max(0, remaining), "capacity": capacity, "plan": plan}


def short_k(n: int) -> str:
    return f"{n / 1000:.1f}".rstrip("0").rstrip(".") + "k" if n >= 1000 else str(n)


def describe_plan(f: dict) -> str:
    parts = []
    for p in f["plan"]:
        name = config.SHORT_NAMES.get(p["from"], p["from"])
        ratio = "" if p["ratio"] == 1.0 else f" at {p['ratio']:g}x"
        parts.append(f"{short_k(p['points'])} {name}{ratio}")
    s = " + ".join(parts)
    if not f["ok"]:
        s = (s + "; " if s else "") + f"short {short_k(f['short'])}"
    return s


def enrich(offer: dict, settings: dict, balances: dict, pax: int = 1) -> dict:
    o = dict(offer)
    o.update(score(offer, settings))
    o["total_miles"] = offer["cost"] * pax
    if balances:
        f = funding(offer["source"], o["total_miles"], balances)
        o["affordable"], o["pay_with"] = f["ok"], describe_plan(f)
    else:
        o["affordable"], o["pay_with"] = None, ""
    return o
