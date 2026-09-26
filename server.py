"""StreetSmart server: FastAPI + Claude Opus 5.5 trip agent, live for any trip in San Francisco.

GET  /                     React app (frontend/dist), /classic = single-file Leaflet app
GET  /api/config           defaults, fares, rideshare formula, demo trip, preset places
GET  /api/status           live-data status (SFPD refresh time, counts)
GET  /api/geocode?q=       place search (OpenStreetMap Nominatim, SF only)
GET  /api/reverse?lat&lng  name for a dropped pin / current location
GET  /api/cells            H3 safety cells citywide, re-scored live from ?p=<json scoring params>&depart=
POST /api/trip             live candidate routes (Transitous real-time transit + OSRM walk/drive) + stop cards
POST /api/plan             PlanReq -> Opus 5.5 agent plan   (?demo=1 uses data/demo_cache.json for the demo trip)
POST /api/stop_photo       photo for any stop (local Maps data, else a small Apify Google Maps run)
POST /api/caption          {image_url, stop_name} -> Opus vision caption (cached)
POST /api/refresh          pull the newest SFPD reports now
POST /api/live             Opus + Apify remote MCP: newest X posts along the route
POST /api/share            start a live "Walk with me" session; POST /api/share/{id}/pos; GET /api/share/{id}
GET  /api/usage            token + $ totals from data/usage.log
"""
import base64
import hashlib
import json
import os
import secrets
import threading
import time
from datetime import datetime, timedelta
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional

import h3
import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

import routing
import score
from llm import call, text_of, usage_summary

ROOT = Path(__file__).parent
DATA = ROOT / "data"
app = FastAPI(title="StreetSmart")
_lock = threading.Lock()


def jload(name, default):
    p = DATA / name
    try:
        return json.loads(p.read_text()) if p.exists() else default
    except json.JSONDecodeError:
        return default


def jsave(name, obj):
    with _lock:
        tmp = DATA / (name + ".tmp")
        tmp.write_text(json.dumps(obj, indent=1))
        tmp.replace(DATA / name)


# Clipper adult fares, checked 2026-09-26: sfmta.com/getting-around/muni/fares ($2.85 Clipper/MuniMobile,
# $3.00 cash, 120-min transfer window; $2.85 inter-agency discount on transfer within 2h) and the
# bart.gov fare API (16th St Mission -> Civic Center, Clipper $2.55). Live trips price BART per station pair.
FARES = {
    "muni": {"usd": 2.85, "label": "Muni adult, Clipper (120 min of transfers)"},
    "muni_cash": {"usd": 3.00, "label": "Muni adult, cash"},
    "bart_16th_civic": {"usd": 2.55, "label": "BART 16th St Mission -> Civic Center, Clipper"},
    "muni_transfer_from_bart": {"usd": 0.00, "label": "Muni after BART within 2h: $2.85 inter-agency Clipper discount"},
}
# UberX-style estimate for SF; clearly an estimate, not a quote.
RIDESHARE = {"base": 2.50, "per_mile": 1.80, "per_minute": 0.45, "booking_fee": 3.00, "minimum": 10.0,
             "low_mult": 0.9, "high_mult": 1.4}

# The precomputed demo trip (11pm, Mission & 16th -> 9th & Irving); used with ?demo=1 for instant answers.
DEMO = jload("routes.json", {})
for _r in DEMO.get("routes", []):
    _r["fare"] = {"usd": round(sum(FARES[k]["usd"] for k in _r.get("fare_keys", [])), 2),
                  "parts": [{"label": FARES[k]["label"], "usd": FARES[k]["usd"]} for k in _r.get("fare_keys", [])]}
    _r["realtime"] = False
DEMO["live"] = False

PRESETS = [
    {"name": "Mission & 16th St", "lat": 37.76506, "lng": -122.41963},
    {"name": "9th Ave & Irving St", "lat": 37.76410, "lng": -122.46652},
    {"name": "Ferry Building", "lat": 37.79555, "lng": -122.39347},
    {"name": "Union Square", "lat": 37.78797, "lng": -122.40748},
    {"name": "Castro & Market", "lat": 37.76257, "lng": -122.43518},
    {"name": "Dolores Park", "lat": 37.75964, "lng": -122.42695},
    {"name": "Haight & Ashbury", "lat": 37.77004, "lng": -122.44692},
    {"name": "Chase Center", "lat": 37.76802, "lng": -122.38766},
    {"name": "Oracle Park", "lat": 37.77858, "lng": -122.38929},
    {"name": "Civic Center", "lat": 37.77962, "lng": -122.41388},
    {"name": "Golden Gate Park (de Young)", "lat": 37.77147, "lng": -122.46868},
    {"name": "SF State", "lat": 37.72410, "lng": -122.47950},
]


# ---------------------------------------------------------------- settings
class Place(BaseModel):
    name: str
    lat: float
    lng: float


class Scoring(BaseModel):
    weights: Dict[str, float] = Field(default_factory=lambda: dict(score.SOURCE_W))
    halflife_hours: float = 72
    night: bool = True
    night_mult: float = 1.5
    thresholds: List[float] = Field(default_factory=lambda: [2.0, 6.0])


class PlanReq(BaseModel):
    origin: Place = Field(default_factory=lambda: Place(**{**PRESETS[0], "name": "Mission St & 16th St"}))
    destination: Place = Field(default_factory=lambda: Place(**{**PRESETS[1], "name": "9th Ave & Irving St"}))
    budget: float = 3
    depart: str = "23:00"         # "now" or "HH:MM" (SF time)
    deadline: str = ""            # optional "HH:MM"
    priority: int = 30            # 0 = safety only ... 100 = speed only
    max_walk_min: int = 15
    modes: List[str] = Field(default_factory=lambda: ["walk", "muni", "bart", "uber"])
    surge: float = 1.0
    effort: str = "medium"
    scoring: Scoring = Field(default_factory=Scoring)


def depart_dt(depart: str):
    now = datetime.now(routing.SF_TZ)
    if not depart or depart == "now":
        return now
    try:
        h, m = map(int, depart.split(":"))
    except ValueError:
        return now
    t = now.replace(hour=h, minute=m, second=0, microsecond=0)
    return t + timedelta(days=1) if t < now - timedelta(minutes=30) else t


def is_demo_trip(req: PlanReq):
    return (routing.dist_m((req.origin.lat, req.origin.lng), (DEMO["origin"]["lat"], DEMO["origin"]["lng"])) < 120 and
            routing.dist_m((req.destination.lat, req.destination.lng), (DEMO["destination"]["lat"], DEMO["destination"]["lng"])) < 120)


def get_trip(req: PlanReq, demo=False, fresh=False):
    if demo and is_demo_trip(req):
        return DEMO
    dep = depart_dt(req.depart)
    key = ("trip", round(req.origin.lat, 5), round(req.origin.lng, 5), round(req.destination.lat, 5),
           round(req.destination.lng, 5), dep.strftime("%Y%m%d%H%M"))
    if fresh:
        routing._cache.pop(key, None)
    return routing.cached(key, 90, lambda: routing.build_trip(req.origin.dict(), req.destination.dict(), dep))


# ---------------------------------------------------------------- safety cells (live)
STATUS = {"datasf_updated": None, "datasf_rows": 0, "incidents": 0, "refreshing": False, "error": None}


def _hour(hhmm):
    if not hhmm or hhmm == "now":
        return datetime.now(routing.SF_TZ).hour
    try:
        return int(hhmm.split(":")[0]) % 24
    except (ValueError, AttributeError):
        return 23


@lru_cache(maxsize=48)
def _late(day, hour):
    return score.place_cells(day, hour)


_INC = {"list": None}


def incidents():
    if _INC["list"] is None:
        _INC["list"] = jload("incidents.json", [])
    return _INC["list"]


def cells_for(scoring: Scoring, depart="23:00", base_cells=()):
    hour = _hour(depart)
    day = depart_dt(depart).strftime("%A")
    params = {**scoring.dict(), "hour": hour, "day": day}
    return score.compute_cells(incidents(), late=_late(day, hour), params=params,
                               reasons=jload("reasons.json", {}), base_cells=list(base_cells))


def refresh_datasf(write_reasons=True):
    """Pull the last 7 days of SFPD incident reports citywide and rebuild incidents + reasons."""
    import extract
    if STATUS["refreshing"]:
        return STATUS
    STATUS["refreshing"] = True
    try:
        since = (datetime.now(routing.SF_TZ) - timedelta(days=7)).strftime("%Y-%m-%dT00:00:00")
        rows = routing.fetch_json("https://data.sf.gov/resource/wg3w-h783.json", {
            "$select": "incident_id,incident_datetime,incident_category,incident_subcategory,incident_description,"
                       "latitude,longitude,intersection",
            "$where": f"incident_datetime > '{since}' AND latitude IS NOT NULL", "$limit": 20000,
            "$order": "incident_datetime DESC"}, timeout=60)
        sf = extract.datasf_incidents(rows)
        others = [i for i in jload("incidents.json", []) if i.get("source") != "datasf"]
        allinc = others + sf
        jsave("incidents.json", allinc)
        _INC["list"] = allinc
        STATUS.update(datasf_updated=datetime.now(routing.SF_TZ).isoformat(), datasf_rows=len(rows),
                      incidents=len(allinc), error=None)
        if write_reasons:
            _reasons_for_new_cells()
    except Exception as e:
        STATUS["error"] = f"{type(e).__name__}: {e}"
    finally:
        STATUS["refreshing"] = False
    return STATUS


def _reasons_for_new_cells(cap=40):
    """One batched Opus call (effort low) for risky cells that don't have a reason yet."""
    have = jload("reasons.json", {})
    cells = cells_for(Scoring(), "23:00")
    todo = [c for c in cells if c["band"] != "very safe" and c["cell"] not in have][:cap]
    if not todo:
        return 0
    new = score.write_reasons(todo)
    have.update(new)
    jsave("reasons.json", have)
    return len(new)


def _refresher():
    time.sleep(5)
    while True:
        refresh_datasf()
        time.sleep(600)  # every 10 minutes


@app.on_event("startup")
def _start():
    if os.environ.get("STREETSMART_NO_REFRESH") != "1":
        threading.Thread(target=_refresher, daemon=True).start()


# ---------------------------------------------------------------- agent tools
def walk_minutes(route):
    return sum(l["minutes"] for l in route["legs"] if l["mode"] == "walk")


def allowed_routes(trip, req: PlanReq):
    ok = []
    for r in trip["routes"]:
        modes = {("muni" if l["mode"] == "rail" else l["mode"]) for l in r["legs"]}
        if modes <= set(req.modes) and walk_minutes(r) <= max(req.max_walk_min, 3 if "walk" in req.modes else 0):
            ok.append(r)
    return ok


def _sample(coords, step_m=40):
    pts = []
    for (a, b) in zip(coords, coords[1:]):
        d = h3.great_circle_distance(tuple(a), tuple(b), unit="m")
        n = max(1, int(d / step_m))
        pts += [(a[0] + (b[0] - a[0]) * i / n, a[1] + (b[1] - a[1]) * i / n) for i in range(n)]
    return pts + [tuple(coords[-1])] if coords else pts


def route_exposure(route, trip):
    """Where the rider is exposed (on foot or waiting) vs. just passing through in a vehicle.
    Returns ({key: (where, [cells])}, {cell: where}); a BART station counts its surrounding cells too."""
    exposed, passing = {}, {}

    def spot(lat, lng, where, station=False):
        c = h3.latlng_to_cell(lat, lng, 9)
        if c not in exposed:
            exposed[c] = (where, list(h3.grid_disk(c, 1)) if station else [c])

    o = trip["origin"]
    first_station = route["stops"] and trip["stops"][route["stops"][0]]["kind"] == "BART station"
    spot(o["lat"], o["lng"], f"waiting at {o['name']}", station=bool(first_station) and route["legs"][0]["mode"] != "uber")
    for s in route["stops"]:
        st = trip["stops"][s]
        spot(st["lat"], st["lng"], f"waiting at {st['name']}" + (" (station area)" if st["kind"] == "BART station" else ""),
             station=st["kind"] == "BART station")
    for leg in route["legs"]:
        for lat, lng in _sample(leg["coords"]):
            c = h3.latlng_to_cell(lat, lng, 9)
            if leg["mode"] == "walk":
                if c not in exposed:
                    exposed[c] = (leg["label"].lower(), [c])
            else:
                passing.setdefault(c, leg["label"])
    return exposed, passing


def route_cells(trip):
    out = set()
    for r in trip["routes"]:
        for l in r["legs"]:
            for lat, lng in _sample(l["coords"], 60):
                out.add(h3.latlng_to_cell(lat, lng, 9))
    return out


class Tools:
    """Local tools bound to one request's trip and settings."""

    def __init__(self, req: PlanReq, trip):
        self.req, self.trip = req, trip
        self.cells = {c["cell"]: c for c in cells_for(req.scoring, req.depart, base_cells=())}

    def get_routes(self, origin, destination):
        ok = allowed_routes(self.trip, self.req)
        out = []
        for r in ok:
            out.append({"route_id": r["id"], "name": r["name"], "modes": r["modes"], "total_minutes": r["minutes"],
                        "arrive_at": r.get("arrive_at"), "includes_wait_minutes": r["wait_minutes"],
                        "transit_fare_usd": r["fare"]["usd"], "transit_fare_parts": r["fare"]["parts"],
                        "rideshare_leg": r["rideshare"], "realtime_departures": r.get("realtime", False),
                        "legs": [{"mode": l["mode"], "label": l["label"], "minutes": l["minutes"], "miles": l["miles"],
                                  **({"departs": l["departs"]} if l.get("departs") else {})} for l in r["legs"]],
                        "stops": [self.trip["stops"][s]["name"] for s in r["stops"]]})
        excluded = [r["id"] for r in self.trip["routes"] if r not in ok]
        return {"origin": self.trip["origin"]["name"], "destination": self.trip["destination"]["name"],
                "depart": self.trip["depart"],
                "source": "Transitous real-time transit + OSRM" if self.trip.get("live") else "precomputed demo trip",
                "excluded_by_rider_settings": excluded, "routes": out}

    def score_route(self, route_id):
        route = next((r for r in self.trip["routes"] if r["id"] == route_id), None)
        if not route:
            return {"error": f"unknown route_id {route_id}"}
        exposed, passing = route_exposure(route, self.trip)

        def worst(group):  # a station area = several cells; count its riskiest one
            hits = [self.cells[c] for c in group if c in self.cells]
            return max(hits, key=lambda x: x["risk"]) if hits else None
        ex = [(key, where, worst(group)) for key, (where, group) in exposed.items()]
        risks = [x["risk"] if x else 0.0 for _, _, x in ex]
        exposed_min = route["wait_minutes"] + walk_minutes(route)
        flagged = [{"where": w, "band": x["band"], "risk": x["risk"], "reason": x["reason"]}
                   for _, w, x in ex if x and x["band"] != "very safe"]
        exposed_cells = {c for _, group in exposed.values() for c in group}
        passed = [{"where": w, "band": self.cells[c]["band"], "reason": self.cells[c]["reason"]}
                  for c, w in passing.items() if c in self.cells and self.cells[c]["band"] == "unsafe" and c not in exposed_cells]
        return {"route_id": route_id,
                "minutes_on_foot_or_waiting": exposed_min,
                "night_exposure": round(exposed_min * (1 + sum(risks) / max(1, len(risks))), 1),
                "exposed_spots": len(ex), "exposed_avg_risk": round(sum(risks) / max(1, len(risks)), 2),
                "exposed_max_risk": round(max(risks or [0]), 2),
                "flagged_while_walking_or_waiting": sorted(flagged, key=lambda f: -f["risk"])[:6],
                "unsafe_cells_passed_in_vehicle": passed[:4],
                "note": "Risk only counts where you're on foot or waiting (BART stations count the whole station area)."}

    def get_fares(self):
        return {"fares": FARES, "note": "Live routes already include transit_fare_usd per route (BART priced per station pair).",
                "source": "sfmta.com Muni fares page + bart.gov fare API, checked 2026-09-26"}

    def estimate_rideshare(self, miles, minutes):
        p, s = RIDESHARE, self.req.surge
        mid = max(p["minimum"], p["base"] + p["booking_fee"] + p["per_mile"] * miles + p["per_minute"] * minutes) * s
        return {"low_usd": round(mid * p["low_mult"], 2), "typical_usd": round(mid, 2), "high_usd": round(mid * p["high_mult"], 2),
                "label": "estimate" + (f" incl. {s:g}x surge" if s != 1 else "") + " (surge can exceed the high end)",
                "formula": f"max(${p['minimum']}, ${p['base']} + ${p['booking_fee']} fee + ${p['per_mile']}/mi + ${p['per_minute']}/min) x {s:g}"}


def S(props, required=None):
    return {"type": "object", "additionalProperties": False, "properties": props,
            "required": list(props) if required is None else required}


TOOLS = [
    {"name": "get_routes", "strict": True,
     "description": "Candidate routes for the trip (modes, legs with live departure times, minutes incl. waiting, transit fare, stops), already filtered by the rider's allowed modes and max walking.",
     "input_schema": S({"origin": {"type": "string"}, "destination": {"type": "string"}})},
    {"name": "score_route", "strict": True,
     "description": "Safety score for one route from live H3 incident cells: minutes on foot/waiting, night_exposure, avg/max risk where the rider walks or waits, and flagged cells with reasons.",
     "input_schema": S({"route_id": {"type": "string"}})},
    {"name": "get_fares", "strict": True, "description": "Muni and BART Clipper fare rules.",
     "input_schema": S({})},
    {"name": "estimate_rideshare", "strict": True,
     "description": "Low/typical/high price ESTIMATE for an Uber/Lyft leg.",
     "input_schema": S({"miles": {"type": "number"}, "minutes": {"type": "number"}})},
    {"name": "submit_plan", "strict": True,
     "description": "Submit the final plan. Ends the task.",
     "cache_control": {"type": "ephemeral"},
     "input_schema": S({
         "recommended_route_id": {"type": "string"},
         "alternatives": {"type": "array", "items": S({"route_id": {"type": "string"}, "why_not": {"type": "string"}})},
         "total_cost": {"type": "number", "description": "USD; transit fares + rideshare legs at their typical estimate"},
         "cost_label": {"type": "string", "description": "SHORT, max 14 chars, e.g. '$2.85' or '~$13 est.'"},
         "total_minutes": {"type": "integer"},
         "safety_summary": {"type": "string", "description": "<= 20 words"},
         "why": {"type": "string", "description": "ONE sentence, names the specific unsafe stretch avoided"},
         "stops": {"type": "array", "items": {"type": "string"}, "description": "stop names on the recommended route"},
     })},
]

SYSTEM = [{"type": "text", "cache_control": {"type": "ephemeral"}, "text": """You are StreetSmart, a door-to-door navigator for San Francisco that puts safety first at night.

Pick the best way for the rider to get there given their budget, deadline and preferences:
- Budget is a hard cap: total_cost (transit_fare_usd + the typical rideshare estimate for any rideshare leg) must be <= budget. Quote rideshare as a range.
- The rider's priority slider runs from 0 (safety only) to 100 (speed only). Near 0, pick the route with the lowest night_exposure from score_route (minutes on foot or waiting, weighted by the risk of the cells there); near 100, pick the earliest arrival; in between, trade them off proportionally. Less time standing on a street is real safety, especially at night.
- Only routes returned by get_routes are allowed (the rider's mode and walking settings are already applied). If none fit the budget, recommend the closest one and say so plainly. If a deadline is given, prefer routes that arrive by it.
- Use tools for every fact: get_routes, score_route for each candidate, estimate_rideshare for rideshare legs. Never invent fares, departures or incidents.
- The "why" is one plain sentence that names the specific unsafe stretch or stop avoided (from the score_route reasons), or says none was flagged. Mention live departure times when they matter.
- Describe places and incidents, never people or whole neighborhoods.
Always finish by calling submit_plan exactly once."""}]


def run_agent(req: PlanReq, trip):
    tools = Tools(req, trip)
    local = {"get_routes": lambda a: tools.get_routes(**a), "score_route": lambda a: tools.score_route(**a),
             "get_fares": lambda a: tools.get_fares(), "estimate_rideshare": lambda a: tools.estimate_rideshare(**a)}
    when = "now" if req.depart == "now" else f"at {req.depart}"
    msgs = [{"role": "user", "content": (
        f"Trip: {trip['origin']['name']} -> {trip['destination']['name']}, leaving {when} "
        f"({datetime.fromisoformat(trip['depart_iso']).strftime('%A %H:%M') if trip.get('depart_iso') else 'Saturday 23:00'}). "
        f"Budget ${req.budget:g}." + (f" Must arrive by {req.deadline}." if req.deadline else "") +
        f" Priority slider: {req.priority} (0 safety .. 100 speed). "
        f"Allowed modes: {', '.join(req.modes)}. Max walking: {req.max_walk_min} min.")}]
    trace = []
    effort = req.effort if req.effort in ("low", "medium", "high") else "medium"
    for turn in range(10):
        resp = call(f"plan:${req.budget:g}:t{turn}", max_tokens=16000, system=SYSTEM, tools=TOOLS,
                    tool_choice={"type": "auto"}, output_config={"effort": effort}, messages=msgs)
        if resp.stop_reason == "refusal":
            raise HTTPException(502, "model declined this request")
        msgs.append({"role": "assistant", "content": resp.content})  # unmodified, thinking blocks included
        uses = [b for b in resp.content if b.type == "tool_use"]
        plan = next((b.input for b in uses if b.name == "submit_plan"), None)
        if plan:
            if not any(r["id"] == plan["recommended_route_id"] for r in trip["routes"]):
                msgs.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": b.id, "is_error": True,
                              "content": "Unknown route_id; pick one from get_routes."} for b in uses]})
                continue
            return plan, trace
        if not uses:
            msgs.append({"role": "user", "content": "Please finish by calling submit_plan."})
            continue
        results = []
        for b in uses:
            try:
                out = local[b.name](b.input)
            except Exception as e:
                out = {"error": str(e)}
            trace.append({"tool": b.name, "input": b.input})
            results.append({"type": "tool_result", "tool_use_id": b.id, "content": json.dumps(out)})
        msgs.append({"role": "user", "content": results})
    raise HTTPException(504, "agent did not finish")


def local_plan(req: PlanReq, trip):
    """Token-free recommendation: same tools, deterministic choice (budget cap, then safety/speed blend)."""
    tools = Tools(req, trip)
    cands = []
    for r in allowed_routes(trip, req):
        sc = tools.score_route(r["id"])
        cost, lo, hi = r["fare"]["usd"], r["fare"]["usd"], r["fare"]["usd"]
        if r["rideshare"]:
            e = tools.estimate_rideshare(**r["rideshare"])
            cost, lo, hi = cost + e["typical_usd"], lo + e["low_usd"], hi + e["high_usd"]
        cands.append({"r": r, "s": sc, "cost": round(cost, 2), "lo": lo, "hi": hi})
    if not cands:
        raise HTTPException(404, "no routes match your mode and walking settings")
    fit = [c for c in cands if c["cost"] <= req.budget] or sorted(cands, key=lambda c: c["cost"])[:1]
    mx_e = max(c["s"]["night_exposure"] for c in fit) or 1
    mx_m = max(c["r"]["minutes"] for c in fit) or 1
    p = req.priority / 100
    for c in fit:
        c["k"] = (1 - p) * c["s"]["night_exposure"] / mx_e + p * c["r"]["minutes"] / mx_m
    best = min(fit, key=lambda c: c["k"])
    mine = {f["where"] for f in best["s"]["flagged_while_walking_or_waiting"]}
    avoided = [f for c in cands if c is not best for f in c["s"]["flagged_while_walking_or_waiting"] if f["where"] not in mine]
    avoided.sort(key=lambda f: -f["risk"])
    over = best["cost"] > req.budget
    lead = ("Nothing fits your budget, so this is the cheapest option" if over else
            "Fastest option in your budget" if p > .6 else "Least time on foot or waiting in your budget")
    why = lead + (f"; it skips {avoided[0]['where'].replace('waiting at ', 'the wait at ')} ({avoided[0]['reason'].split(' · ')[0]})." if avoided
                  else "; nothing risky was flagged on the alternatives either.")
    alts = []
    for c in cands:
        if c is best:
            continue
        if c["cost"] > req.budget:
            wn = f"Over budget (~${c['cost']:.0f})"
        elif c["s"]["night_exposure"] > best["s"]["night_exposure"]:
            wn = f"{c['s']['minutes_on_foot_or_waiting']} min on foot or waiting" + (
                f", incl. {c['s']['flagged_while_walking_or_waiting'][0]['where']}" if c["s"]["flagged_while_walking_or_waiting"] else "")
        else:
            wn = f"Arrives later ({c['r']['minutes']} min)" if c["r"]["minutes"] > best["r"]["minutes"] else "Similar; ranked lower on your safety/speed setting"
        alts.append({"route_id": c["r"]["id"], "why_not": wn})
    fl = best["s"]["flagged_while_walking_or_waiting"]
    r = best["r"]
    return {"recommended_route_id": r["id"], "alternatives": alts, "total_cost": best["cost"],
            "cost_label": f"~${best['cost']:.0f} est." if r["rideshare"] else f"${best['cost']:.2f}",
            "total_minutes": r["minutes"],
            "safety_summary": f"{best['s']['minutes_on_foot_or_waiting']} min on foot or waiting; " +
                              (f"{fl[0]['where']} is {fl[0]['band']}." if fl else "nothing flagged where you walk or wait."),
            "why": why, "stops": [trip["stops"][s]["name"] for s in r["stops"]], "source": "local"}


PLAN_CACHE = {}


def plan_key(req: PlanReq, mode):
    d = req.dict()
    if d["depart"] == "now":
        d["depart"] = datetime.now(routing.SF_TZ).strftime("now-%H") + str(datetime.now().minute // 10)
    return hashlib.sha1(json.dumps([d, mode], sort_keys=True).encode()).hexdigest()


# ---------------------------------------------------------------- stops / photos
def _photos(p):
    return list(dict.fromkeys(u for u in [p.get("imageUrl")] + (p.get("imageUrls") or []) if u))


def _places():
    return jload("places.json", None) or jload("raw/maps.json", [])


def find_photo(st, max_m=400):
    best, bd = None, 1e9
    for p in _places():
        loc = p.get("location") or {}
        if not loc.get("lat") or not _photos(p):
            continue
        cat = " ".join([p.get("categoryName") or ""] + (p.get("categories") or [])).lower()
        transit = any(k in cat for k in ("transit", "bus", "station", "subway", "train", "light rail", "tram"))
        d = routing.dist_m((st["lat"], st["lng"]), (loc["lat"], loc["lng"])) * (1 if transit else 2.5)
        if d < bd:
            best, bd = p, d
    return (best, bd) if best and bd < max_m else (None, None)


def stop_card(st, max_m=400):
    st = dict(st)
    best, _ = find_photo(st, max_m)
    if best:
        st["photos"] = _photos(best)[:3]
        st["photo"] = st["photos"][0]
        st["place_title"] = best.get("title")
        st["caption"] = jload("captions.json", {}).get(st["photo"])
    return st


def trip_payload(trip):
    live = trip.get("live")
    cards = {sid: stop_card(s, 150 if live else 400) for sid, s in trip["stops"].items()}
    return {**{k: v for k, v in trip.items() if k != "stops"}, "stops": trip["stops"], "stop_cards": cards,
            "route_cells": sorted(route_cells(trip))}


def apify_stop_photo(st):
    """Small Google Maps run around one stop (about 1-2 cents, ~30-60 s); result appended to places.json."""
    from apify_client import ApifyClient
    from scrape import log_apify
    client = ApifyClient(os.environ["APIFY_TOKEN"])
    term = "BART station" if st.get("kind") == "BART station" else "bus stop"
    run = client.actor("compass/crawler-google-places").call(run_input={
        "searchStringsArray": [term], "language": "en", "maxImages": 3, "maxReviews": 0,
        "maxCrawledPlacesPerSearch": 4, "scrapePlaceDetailPage": False,
        "customGeolocation": {"type": "Point", "coordinates": [st["lng"], st["lat"]], "radiusKm": 0.25},
    }, max_items=4, max_total_charge_usd=Decimal("0.08"), timeout_secs=150, logger=None)
    run = client.run(run["id"]).get() or run
    log_apify(f"stop_photo:{st['name'][:20]}", run)
    items = list(client.dataset(run["defaultDatasetId"]).iterate_items())
    keep = ("placeId", "title", "location", "categoryName", "categories", "openingHours", "imageUrl", "imageUrls",
            "address", "url", "totalScore")
    new = [{k: p.get(k) for k in keep} for p in items if (p.get("location") or {}).get("lat")]
    if new:
        places = _places()
        ids = {p.get("placeId") for p in places}
        jsave("places.json", places + [p for p in new if p.get("placeId") not in ids])
    return len(new)


def caption_image(image_url, stop_name):
    cache = jload("captions.json", {})
    if image_url in cache:
        return cache[image_url]
    r = httpx.get(image_url, timeout=20, follow_redirects=True)
    r.raise_for_status()
    mt = r.headers.get("content-type", "image/jpeg").split(";")[0]
    resp = call("caption", max_tokens=8000, output_config={"effort": "low"}, messages=[{"role": "user", "content": [
        {"type": "image", "source": {"type": "base64", "media_type": mt, "data": base64.b64encode(r.content).decode()}},
        {"type": "text", "text": f"This is a photo near the transit stop '{stop_name}' in San Francisco. Someone is waiting "
                                 "here alone at night. In ONE sentence of at most 14 words, tell them where to stand, "
                                 "using visible landmarks (signs, awnings, shelters, lights), e.g. 'Stand on the north "
                                 "side, by the green awning.' If the photo shows no useful landmark, describe the best "
                                 "lit spot you can see. Reply with the sentence only."}]}])
    if resp.stop_reason == "refusal":
        return None
    cap = text_of(resp).strip().strip('"')
    cache = jload("captions.json", {})
    cache[image_url] = cap
    jsave("captions.json", cache)
    return cap


def cache_key(req: PlanReq):
    """Default settings -> plain budget key (what ?demo=1 falls back to); anything else gets a hash."""
    d = req.dict()
    base = PlanReq(budget=req.budget).dict()
    if d == base:
        return f"{req.budget:g}"
    return f"{req.budget:g}:" + hashlib.sha1(json.dumps(d, sort_keys=True).encode()).hexdigest()[:10]


# ---------------------------------------------------------------- live "Walk with me"
SHARES = jload("shares.json", {})


class ShareReq(BaseModel):
    name: str = ""
    route: dict
    eta: str = ""
    origin: Optional[dict] = None
    destination: Optional[dict] = None


class Pos(BaseModel):
    lat: float
    lng: float
    simulated: bool = False


# ---------------------------------------------------------------- endpoints
DIST = ROOT / "frontend" / "dist"
if (DIST / "assets").exists():
    from fastapi.staticfiles import StaticFiles
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")


@app.get("/")
def index():
    """The React + Mapbox/MapLibre app (frontend/, `npm run build`); falls back to the single-file Leaflet app."""
    return FileResponse(DIST / "index.html" if (DIST / "index.html").exists() else ROOT / "index.html",
                        headers={"Cache-Control": "no-cache"})


@app.get("/classic")
def classic():
    return FileResponse(ROOT / "index.html")


@app.get("/api/config")
def config():
    return {"defaults": PlanReq().dict(), "fares": FARES, "rideshare": RIDESHARE, "presets": PRESETS,
            "demo_trip": {"origin": DEMO["origin"], "destination": DEMO["destination"], "depart": DEMO["depart"]}}


@app.get("/api/status")
def status():
    return {**STATUS, "now": datetime.now(routing.SF_TZ).isoformat(),
            "transit": "Transitous (GTFS + GTFS-realtime: Muni, BART)", "geocoder": "OpenStreetMap Nominatim"}


@app.get("/api/geocode")
def geocode(q: str):
    try:
        return routing.geocode(q)
    except Exception as e:
        raise HTTPException(502, f"geocoder unavailable: {e}")


@app.get("/api/reverse")
def reverse(lat: float, lng: float):
    try:
        return routing.reverse(lat, lng)
    except Exception:
        return {"name": f"{lat:.4f}, {lng:.4f}", "sub": "", "lat": lat, "lng": lng}


@app.get("/api/cells")
def cells(p: Optional[str] = None, depart: str = "23:00"):
    scoring = Scoring(**json.loads(p)) if p else Scoring()
    return [c for c in cells_for(scoring, depart) if c["incidents"]]


class TripReq(BaseModel):
    origin: Place
    destination: Place
    depart: str = "now"


@app.post("/api/trip")
def trip(req: TripReq, demo: int = 0, fresh: int = 0):
    preq = PlanReq(origin=req.origin, destination=req.destination, depart=req.depart)
    try:
        t = get_trip(preq, demo=bool(demo) and req.depart == "23:00", fresh=bool(fresh))
    except Exception as e:
        raise HTTPException(502, f"routing failed: {e}")
    if not t["routes"]:
        raise HTTPException(404, "no routes found for this trip")
    return trip_payload(t)


@app.post("/api/plan")
def plan(req: PlanReq, demo: int = 0, mode: str = "opus", force: int = 0, fresh: int = 0):
    """mode=opus: Opus 5.5 agent (cached 10 min per trip+settings unless force=1). mode=local: free, no tokens."""
    use_demo = bool(demo) and is_demo_trip(req) and req.depart == "23:00"
    t = get_trip(req, demo=use_demo, fresh=bool(fresh))
    if mode == "local":
        return {**local_plan(req, t), "cached": False, "trace": [], "trip": trip_payload(t)}
    if use_demo and not force:
        cache = jload("demo_cache.json", {})
        hit = cache.get(cache_key(req)) or cache.get(f"{req.budget:g}")
        if hit:
            return {**hit["plan"], "cached": True, "trace": hit["trace"], "trip": trip_payload(t)}
    k = plan_key(req, mode)
    hit = PLAN_CACHE.get(k)
    if hit and not force and time.time() - hit["at"] < 600 and any(r["id"] == hit["plan"]["recommended_route_id"] for r in t["routes"]):
        return {**hit["plan"], "cached": True, "trace": hit["trace"], "trip": trip_payload(t)}
    p, trace = run_agent(req, t)
    PLAN_CACHE[k] = {"plan": p, "trace": trace, "at": time.time()}
    if is_demo_trip(req) and req.depart == "23:00":
        cache = jload("demo_cache.json", {})
        cache[cache_key(req)] = {"plan": p, "trace": trace}
        jsave("demo_cache.json", cache)
    return {**p, "cached": False, "trace": trace, "trip": trip_payload(t)}


@app.post("/api/scan")
def scan(req: TripReq):
    """Fetch data we haven't searched yet: X + news around this trip's places (Apify), read by Opus (effort low)."""
    import extract
    from apify_client import ApifyClient
    from scrape import run_actor
    t = get_trip(PlanReq(origin=req.origin, destination=req.destination, depart=req.depart))
    names = [req.origin.name, req.destination.name] + [s["name"] for s in list(t["stops"].values())[:4]]
    names = [n for n in dict.fromkeys(names) if n and n not in ("Current location", "Dropped pin")][:5]
    client = ApifyClient(os.environ["APIFY_TOKEN"])
    since = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    queries = [f'"{n.split(",")[0]}" (robbery OR assault OR stabbing OR police OR unsafe OR shooting) (sf OR "san francisco")' for n in names]
    items = []
    try:
        tw = run_actor(client, "xquik/x-tweet-scraper", {"mode": "search", "searchTerms": queries, "queryType": "Latest",
                       "lang": "en", "since": since, "maxItems": 80, "maxItemsPerTarget": 20}, 80, 0.1, "scan:x", quiet=True)
        items += [{"item_id": f"x:{x.get('id')}", "source": "x", "created_at": x.get("createdAt", ""),
                   "text": x.get("text") or x.get("fullText") or "", "url": x.get("url", "")} for x in tw]
    except Exception as e:
        print("scan x failed", e)
    try:
        nw = run_actor(client, "data_xplorer/google-news-scraper-fast", {"keywords": [f"{n.split(',')[0]} San Francisco crime" for n in names[:3]],
                       "maxArticles": 8, "timeframe": "7d", "region_language": "US:en", "decodeUrls": True,
                       "extractDescriptions": True, "extractImages": False}, 24, 0.15, "scan:news", quiet=True)
        items += [{"item_id": f"news:{a.get('url')}", "source": "news", "created_at": a.get("publishedAt") or "",
                   "text": f"{a.get('title') or ''}\n{a.get('description') or ''}", "url": a.get("url", "")} for a in nw]
    except Exception as e:
        print("scan news failed", e)
    known = {i.get("item_id") for i in incidents()}
    fresh_items = [i for i in items if i["text"] and i["item_id"] not in known and extract.INCIDENT_RE.search(i["text"])]
    found = []
    for b in range(0, len(fresh_items), 50):
        found += extract.extract_batch(f"scan{b}", fresh_items[b:b + 50])
    src = {i["item_id"]: i for i in fresh_items}
    new = []
    for inc in found:
        if inc["confidence"] < 0.5 or not (37.70 < inc["lat"] < 37.83) or inc["item_id"] in known:
            continue
        inc["url"] = src.get(inc["item_id"], {}).get("url", "")
        new.append(inc)
    if new:
        allinc = jload("incidents.json", []) + new
        jsave("incidents.json", allinc)
        _INC["list"] = allinc
        threading.Thread(target=_reasons_for_new_cells, daemon=True).start()
    return {"searched": names, "items": len(items), "new_items": len(fresh_items), "added": len(new), "incidents": new}


class StopReq(BaseModel):
    id: str
    name: str
    lat: float
    lng: float
    kind: str = "stop"


@app.post("/api/stop_photo")
def stop_photo(req: StopReq, fetch: int = 1):
    st = req.dict()
    card = stop_card(st, 150)
    if card.get("photo") or not fetch:
        return card
    try:
        apify_stop_photo(st)
    except Exception as e:
        return {**card, "error": f"Apify photo lookup failed: {e}"}
    return stop_card(st, 250)


class CapReq(BaseModel):
    image_url: str
    stop_name: str = "the stop"


@app.post("/api/caption")
def caption(req: CapReq):
    try:
        return {"caption": caption_image(req.image_url, req.stop_name)}
    except httpx.HTTPError as e:
        raise HTTPException(502, f"could not fetch image: {e}")


@app.post("/api/refresh")
def refresh():
    threading.Thread(target=refresh_datasf, daemon=True).start()
    return {"started": True, **STATUS}


@app.post("/api/live")
def live_refresh():
    """Opus 5.5 + Apify remote MCP: scrape the newest X posts along the route and merge new incidents."""
    import live
    out = live.refresh()
    added = live.merge(out["new"]) if out["new"] else 0
    _INC["list"] = None
    return {"added": added, "incidents": out["new"], "mcp_tools_used": out.get("mcp_tools_used", []), "note": out.get("note")}


@app.post("/api/share")
def share_start(req: ShareReq):
    sid = secrets.token_urlsafe(6)
    SHARES[sid] = {**req.dict(), "created": time.time(), "pos": None, "trail": [], "updated": None}
    jsave("shares.json", SHARES)
    return {"id": sid}


@app.post("/api/share/{sid}/pos")
def share_pos(sid: str, pos: Pos):
    s = SHARES.get(sid)
    if not s:
        raise HTTPException(404, "unknown share")
    s["pos"] = pos.dict()
    s["updated"] = time.time()
    s["trail"] = (s["trail"] + [[pos.lat, pos.lng]])[-300:]
    if len(s["trail"]) % 10 == 0:
        jsave("shares.json", SHARES)
    return {"ok": True}


@app.get("/api/share/{sid}")
def share_get(sid: str):
    s = SHARES.get(sid)
    if not s:
        raise HTTPException(404, "unknown share")
    return {**s, "age_s": round(time.time() - s["updated"]) if s["updated"] else None}


@app.get("/api/usage")
def usage():
    return usage_summary()
