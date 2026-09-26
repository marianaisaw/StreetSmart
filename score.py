"""Step 3: incidents + places -> H3 res-9 cells with risk, band and a one-line reason.

We score incidents and conditions per ~150m hex, never whole neighborhoods,
and every cell carries a human-readable reason.
"""
import json
import math
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import h3

from llm import call, tool_input

DATA = Path(__file__).parent / "data"
RES = 9
SOURCE_W = {"datasf": 1.0, "news": 0.8, "x": 0.4}
DEMO_HOUR = 23  # trip is at 11pm
DEMO_DAY = datetime.now().strftime("%A")

# Same corridor as scrape.py, as (lat, lng) for h3
CORRIDOR = [(37.7610, -122.4235), (37.7610, -122.4150), (37.7805, -122.4085), (37.7835, -122.4175),
            (37.7720, -122.4380), (37.7675, -122.4720), (37.7605, -122.4720), (37.7615, -122.4400)]


def parse_time(s):
    if not s:
        return None
    s = str(s).strip()
    for fmt in ("%a %b %d %H:%M:%S %z %Y",):  # X's createdAt format
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _to_hour(tok):
    m = re.match(r"(\d{1,2})(?::(\d{2}))?\s*([AP]M)?", tok.strip(), re.I)
    if not m:
        return None
    h = int(m.group(1)) % 12 + (int(m.group(2) or 0) / 60)
    ap = (m.group(3) or "").upper()
    return h + 12 if ap == "PM" else h


def open_late(place, day=DEMO_DAY, hour=DEMO_HOUR):
    """True if the place is open at `hour` on `day` (rough parse of Google's hours strings)."""
    for oh in place.get("openingHours") or []:
        if oh.get("day", "").lower() != day.lower():
            continue
        hrs = (oh.get("hours") or "").replace(" ", " ").replace(" ", " ")
        if "24 hours" in hrs.lower():
            return True
        for span in hrs.split(","):
            parts = re.split(r"\s*[–-]\s*|\s+to\s+", span.strip())
            if len(parts) != 2:
                continue
            start, end = _to_hour(parts[0]), _to_hour(parts[1])
            if start is None or end is None:
                continue
            if "M" not in parts[0].upper() and "PM" in parts[1].upper():
                start += 12 if start < 12 and start + 12 < end else 0
            if end <= start:  # closes after midnight
                end += 24
            if start <= hour < end:
                return True
    return False


def place_cells(day=DEMO_DAY, hour=DEMO_HOUR):
    p = DATA / "places.json" if (DATA / "places.json").exists() else DATA / "raw" / "maps.json"
    places = json.loads(p.read_text()) if p.exists() else []
    late = {}
    for pl in places:
        loc = pl.get("location") or {}
        if loc.get("lat") and open_late(pl, day, hour):
            c = h3.latlng_to_cell(loc["lat"], loc["lng"], RES)
            late.setdefault(c, []).append(pl.get("title", ""))
    return late


REASON_TOOL = {
    "name": "save_reasons",
    "description": "Save one short reason line per cell.",
    "strict": True,
    "input_schema": {
        "type": "object", "additionalProperties": False, "required": ["reasons"],
        "properties": {"reasons": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["cell", "reason"],
            "properties": {"cell": {"type": "string"}, "reason": {"type": "string"}},
        }}},
    },
}


def write_reasons(cells):
    todo = [c for c in cells if c["band"] != "very safe"]  # one reason line per risky cell
    if not todo:
        return {}
    brief = [{"cell": c["cell"], "band": c["band"], "risk": c["risk"], "incidents": c["incidents"],
              "hours_ago_newest": c["newest_hours"], "open_late_places": c["open_late"][:3],
              "types": c["types"], "summaries": [t["summary"] for t in c["top"]]} for c in todo]
    resp = call("score:reasons", max_tokens=16000, output_config={"effort": "low"}, tools=[REASON_TOOL],
                tool_choice={"type": "auto"},
                system="You write one-line safety reasons for map cells in a night-time navigation app. "
                       "Each reason is <= 14 words, factual, uses ' · ' to join 2-3 facts, mentions counts and recency "
                       "(e.g. '2 incidents reported in last 48h · poorly lit after 9pm' or '1 phone theft 3 days ago · "
                       "4 places open late'). Describe incidents and conditions, never people or neighborhoods. "
                       "Always finish by calling save_reasons once with every cell.",
                messages=[{"role": "user", "content": json.dumps(brief)}])
    if resp.stop_reason == "refusal":
        return {}
    got = tool_input(resp, "save_reasons") or {"reasons": []}
    return {r["cell"]: r["reason"] for r in got["reasons"]}


DEFAULTS = {"weights": dict(SOURCE_W), "halflife_hours": 72.0, "night": True, "hour": DEMO_HOUR, "day": DEMO_DAY,
            "night_mult": 1.5, "thresholds": [2.0, 6.0]}


def is_night(hour):
    return hour >= 21 or hour < 6


def compute_cells(incidents, late=None, params=None, reasons=None):
    """Pure scoring (no LLM): risk per H3 cell. Params override DEFAULTS; used live by the server."""
    p = {**DEFAULTS, **(params or {})}
    p["weights"] = {**SOURCE_W, **(p.get("weights") or {})}
    late = place_cells(p["day"], p["hour"]) if late is None else late
    now = datetime.now(timezone.utc)
    corridor = set(h3.polygon_to_cells(h3.LatLngPoly(CORRIDOR), RES))
    cells = {c: [] for c in corridor}
    for inc in incidents:
        if p["weights"].get(inc["source"], 0.4) <= 0:
            continue
        cells.setdefault(h3.latlng_to_cell(inc["lat"], inc["lng"], RES), []).append(inc)
    lo, hi = p["thresholds"]
    out = []
    for c, incs in cells.items():
        base, newest = 0.0, None
        for i in incs:
            t = parse_time(i.get("occurred_at"))
            hrs = max(0.0, (now - t).total_seconds() / 3600) if t else 24.0
            newest = hrs if newest is None else min(newest, hrs)
            base += i["severity"] * i["confidence"] * p["weights"].get(i["source"], 0.4) * math.exp(-hrs / p["halflife_hours"])
        n_open = len(late.get(c, []))
        risk = base
        if p["night"] and is_night(p["hour"]):  # night bump, softened by places open late (eyes on the street)
            risk = base * p["night_mult"] / (1 + 0.1 * n_open)
        band = "very safe" if risk < lo else "kind of safe" if risk <= hi else "unsafe"
        top = [{"summary": i["summary"], "source": i["source"], "url": i.get("url", "")}
               for i in sorted(incs, key=lambda i: (-i["severity"] * i["confidence"], i["source"] == "datasf"))[:3]]
        cell = {"cell": c, "risk": round(risk, 2), "band": band, "incidents": len(incs),
                "newest_hours": round(newest, 1) if newest is not None else None,
                "open_late": late.get(c, []), "top": top, "in_corridor": c in corridor,
                "sources": sorted({i["source"] for i in incs}),
                "types": dict(Counter(i["incident_type"] for i in incs).most_common(3))}
        if reasons and c in reasons and incs:
            cell["reason"] = reasons[c]
        elif incs:
            cell["reason"] = f"{len(incs)} report{'s' if len(incs) > 1 else ''} in the last week · " + (top[0]["summary"] if top else "")
        else:
            cell["reason"] = "No incidents reported in the last 2 weeks" + (
                f" · {n_open} places open late" if n_open else "")
        out.append(cell)
    out.sort(key=lambda c: -c["risk"])
    return out


def main():
    incidents = json.loads((DATA / "incidents.json").read_text())
    out = compute_cells(incidents)
    reasons = write_reasons(out)  # one batched Opus call
    out = compute_cells(incidents, reasons=reasons)
    (DATA / "cells.json").write_text(json.dumps(out, indent=1))
    (DATA / "reasons.json").write_text(json.dumps(reasons, indent=1))
    bands = {b: sum(c["band"] == b for c in out) for b in ("very safe", "kind of safe", "unsafe")}
    print(f"{len(out)} cells ({sum(c['in_corridor'] for c in out)} in corridor) -> data/cells.json  {bands}")


if __name__ == "__main__":
    main()
