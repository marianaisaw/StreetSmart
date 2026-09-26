"""Step 4: FastAPI server + Opus 5.5 trip agent.

GET  /               index.html
GET  /api/config     default settings, fares, rideshare formula
GET  /api/cells      H3 safety cells, re-scored live from ?p=<json scoring params>
GET  /api/routes     candidate route geometry + stops (with Maps photos)
POST /api/plan       PlanReq -> agent plan   (?demo=1 uses data/demo_cache.json)
POST /api/caption    {image_url, stop_name} -> Opus vision caption (cached)
POST /api/live       Opus + Apify remote MCP: pull the newest X posts, merge new incidents
GET  /api/usage      token + $ totals from data/usage.log
"""
import base64
import hashlib
import json
import os
import threading
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional

import h3
import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

import score
from llm import call, text_of, usage_summary

ROOT = Path(__file__).parent
DATA = ROOT / "data"
app = FastAPI(title="Streetsmart")
_lock = threading.Lock()


def jload(name, default):
    p = DATA / name
    return json.loads(p.read_text()) if p.exists() else default


def jsave(name, obj):
    with _lock:
        (DATA / name).write_text(json.dumps(obj, indent=1))


ROUTES = jload("routes.json", {})

# Clipper adult fares, checked 2026-09-26: sfmta.com/getting-around/muni/fares ($2.85 Clipper/MuniMobile,
# $3.00 cash, 120-min transfer window; $2.85 inter-agency discount on transfer to Muni within 2h) and the
# bart.gov fare API (16th St Mission -> Civic Center, Clipper $2.55).
FARES = {
    "muni": {"usd": 2.85, "label": "Muni adult, Clipper (120 min of transfers)"},
    "muni_cash": {"usd": 3.00, "label": "Muni adult, cash"},
    "bart_16th_civic": {"usd": 2.55, "label": "BART 16th St Mission -> Civic Center, Clipper"},
    "muni_transfer_from_bart": {"usd": 0.00, "label": "Muni after BART within 2h: $2.85 inter-agency Clipper discount"},
}
# UberX-style estimate for SF late night; clearly an estimate, not a quote.
RIDESHARE = {"base": 2.50, "per_mile": 1.80, "per_minute": 0.45, "booking_fee": 3.00, "minimum": 10.0,
             "low_mult": 0.9, "high_mult": 1.4}


# ---------------------------------------------------------------- settings
class Scoring(BaseModel):
    weights: Dict[str, float] = Field(default_factory=lambda: dict(score.SOURCE_W))
    halflife_hours: float = 72
    night: bool = True
    night_mult: float = 1.5
    thresholds: List[float] = Field(default_factory=lambda: [2.0, 6.0])


class PlanReq(BaseModel):
    origin: str = "Mission St & 16th St"
    destination: str = "9th Ave & Irving St"
    budget: float = 3
    depart: str = "23:00"
    deadline: str = "23:59"
    priority: int = 30            # 0 = safety only ... 100 = speed only
    max_walk_min: int = 15
    modes: List[str] = Field(default_factory=lambda: ["walk", "muni", "bart", "uber"])
    surge: float = 1.0            # multiplier on rideshare estimates
    effort: str = "medium"        # agent effort: low | medium | high
    scoring: Scoring = Field(default_factory=Scoring)


def _hour(hhmm):
    try:
        return int(hhmm.split(":")[0]) % 24
    except (ValueError, AttributeError):
        return 23


@lru_cache(maxsize=48)
def _late(day, hour):
    return score.place_cells(day, hour)


@lru_cache(maxsize=1)
def _incidents():
    return jload("incidents.json", [])


def cells_for(scoring: Scoring, depart="23:00"):
    hour = _hour(depart)
    params = {**scoring.dict(), "hour": hour, "day": score.DEMO_DAY}
    return score.compute_cells(_incidents(), late=_late(score.DEMO_DAY, hour), params=params,
                               reasons=jload("reasons.json", {}))


# ---------------------------------------------------------------- agent tools
def walk_minutes(route):
    return sum(l["minutes"] for l in route["legs"] if l["mode"] == "walk")


def allowed_routes(req: PlanReq):
    ok = []
    for r in ROUTES["routes"]:
        modes = {l["mode"] for l in r["legs"]}
        if modes <= set(req.modes) and walk_minutes(r) <= req.max_walk_min:
            ok.append(r)
    return ok


class Tools:
    """Local tools bound to one request's settings."""

    def __init__(self, req: PlanReq):
        self.req = req
        self.cells = {c["cell"]: c for c in cells_for(req.scoring, req.depart)}

    def get_routes(self, origin, destination):
        out = []
        for r in allowed_routes(self.req):
            out.append({"route_id": r["id"], "name": r["name"], "modes": r["modes"], "total_minutes": r["minutes"],
                        "includes_wait_minutes": r["wait_minutes"], "fare_keys": r["fare_keys"],
                        "rideshare_leg": r["rideshare"],
                        "legs": [{"mode": l["mode"], "label": l["label"], "minutes": l["minutes"], "miles": l["miles"]} for l in r["legs"]],
                        "stops": [ROUTES["stops"][s]["name"] for s in r["stops"]]})
        excluded = [r["id"] for r in ROUTES["routes"] if r not in allowed_routes(self.req)]
        return {"origin": ROUTES["origin"]["name"], "destination": ROUTES["destination"]["name"],
                "depart": self.req.depart, "note": "Demo trip; candidate routes are precomputed.",
                "excluded_by_rider_settings": excluded, "routes": out}

    def score_route(self, route_id):
        route = next((r for r in ROUTES["routes"] if r["id"] == route_id), None)
        if not route:
            return {"error": f"unknown route_id {route_id}"}
        exposed, passing = route_exposure(route)

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
                "note": "Risk only counts where you're on foot or waiting (BART stations count the whole station area); in-vehicle stretches are listed separately."}

    def get_fares(self):
        return {"fares": FARES, "source": "sfmta.com Muni fares page + bart.gov fare API, checked 2026-09-26"}

    def estimate_rideshare(self, miles, minutes):
        p, s = RIDESHARE, self.req.surge
        mid = max(p["minimum"], p["base"] + p["booking_fee"] + p["per_mile"] * miles + p["per_minute"] * minutes) * s
        return {"low_usd": round(mid * p["low_mult"], 2), "typical_usd": round(mid, 2), "high_usd": round(mid * p["high_mult"], 2),
                "label": "estimate" + (f" incl. {s:g}x surge" if s != 1 else "") + " (late night; surge can exceed the high end)",
                "formula": f"max(${p['minimum']}, ${p['base']} + ${p['booking_fee']} fee + ${p['per_mile']}/mi + ${p['per_minute']}/min) x {s:g}"}


def _sample(coords, step_m=40):
    pts = []
    for (a, b) in zip(coords, coords[1:]):
        d = h3.great_circle_distance(tuple(a), tuple(b), unit="m")
        n = max(1, int(d / step_m))
        pts += [(a[0] + (b[0] - a[0]) * i / n, a[1] + (b[1] - a[1]) * i / n) for i in range(n)]
    return pts + [tuple(coords[-1])]


def route_exposure(route):
    """Where the rider is exposed (on foot or waiting) vs. just passing through in a vehicle.
    Returns ({key: (where, [cells])}, {cell: where}); a BART station counts its surrounding cells too."""
    exposed, passing = {}, {}

    def spot(lat, lng, where, station=False):
        c = h3.latlng_to_cell(lat, lng, 9)
        group = list(h3.grid_disk(c, 1)) if station else [c]
        if c not in exposed:
            exposed[c] = (where, group)

    o = ROUTES["origin"]
    spot(o["lat"], o["lng"], "waiting at Mission & 16th", station="bart16" in route["stops"])
    for s in route["stops"]:
        st = ROUTES["stops"][s]
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


def S(props, required=None):
    return {"type": "object", "additionalProperties": False, "properties": props,
            "required": list(props) if required is None else required}


TOOLS = [
    {"name": "get_routes", "strict": True,
     "description": "Candidate routes (modes, legs, minutes incl. waiting, stops) for the trip, already filtered by the rider's allowed modes and max walking.",
     "input_schema": S({"origin": {"type": "string"}, "destination": {"type": "string"}})},
    {"name": "score_route", "strict": True,
     "description": "Safety score for one route from live H3 incident cells: minutes on foot/waiting, night_exposure, avg/max risk where the rider walks or waits, and flagged cells with reasons.",
     "input_schema": S({"route_id": {"type": "string"}})},
    {"name": "get_fares", "strict": True, "description": "Current Muni and BART Clipper fares.",
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
         "total_cost": {"type": "number", "description": "USD; rideshare legs at their typical estimate"},
         "cost_label": {"type": "string", "description": "SHORT, max 14 chars, e.g. '$2.85' or '~$13 est.'"},
         "total_minutes": {"type": "integer"},
         "safety_summary": {"type": "string", "description": "<= 20 words"},
         "why": {"type": "string", "description": "ONE sentence, names the specific unsafe stretch avoided"},
         "stops": {"type": "array", "items": {"type": "string"}, "description": "stop names on the recommended route"},
     })},
]

SYSTEM = [{"type": "text", "cache_control": {"type": "ephemeral"}, "text": """You are Streetsmart, a door-to-door night navigator for San Francisco.

Pick the best way for the rider to get there given their budget, deadline and preferences:
- Budget is a hard cap: total_cost (fares + the typical rideshare estimate) must be <= budget. Quote rideshare as a range.
- The rider's priority slider runs from 0 (safety only) to 100 (speed only). Near 0, pick the route with the lowest night_exposure from score_route (minutes on foot or waiting, weighted by the risk of the cells there); near 100, pick the fastest arrival; in between, trade them off proportionally. Less time standing on a street at night is real safety.
- Only routes returned by get_routes are allowed (the rider's mode and walking settings are already applied). If none fit the budget, recommend the closest one and say so plainly.
- Use tools for every fact: get_routes, score_route for each candidate, get_fares, estimate_rideshare for rideshare legs. Never invent fares or incidents.
- The "why" is one plain sentence that names the specific unsafe stretch or stop avoided (from the score_route reasons), or says none was flagged.
- Describe places and incidents, never people or whole neighborhoods.
Always finish by calling submit_plan exactly once."""}]


def run_agent(req: PlanReq):
    tools = Tools(req)
    local = {"get_routes": lambda a: tools.get_routes(**a), "score_route": lambda a: tools.score_route(**a),
             "get_fares": lambda a: tools.get_fares(), "estimate_rideshare": lambda a: tools.estimate_rideshare(**a)}
    msgs = [{"role": "user", "content": (
        f"Trip: {req.origin} -> {req.destination}, leaving {req.depart} Saturday. Budget ${req.budget:g}. "
        f"Must arrive by {req.deadline}. Priority slider: {req.priority} (0 safety .. 100 speed). "
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


def enrich(plan):
    """Attach geometry + stop photos so the page can draw everything from one response."""
    route = next(r for r in ROUTES["routes"] if r["id"] == plan["recommended_route_id"])
    plan["route"] = route
    plan["stop_cards"] = [stop_card(s) for s in route["stops"]]
    return plan


# ---------------------------------------------------------------- stops / photos
def _photos(p):
    return list(dict.fromkeys(u for u in [p.get("imageUrl")] + (p.get("imageUrls") or []) if u))


def stop_card(stop_id):
    st = dict(ROUTES["stops"][stop_id])
    best, bd = None, 1e9
    for p in jload("places.json", None) or jload("raw/maps.json", []):
        loc = p.get("location") or {}
        if not loc.get("lat") or not _photos(p):
            continue
        cat = " ".join([p.get("categoryName") or ""] + (p.get("categories") or [])).lower()
        transit = any(k in cat for k in ("transit", "bus", "station", "subway", "train", "light rail", "tram"))
        d = h3.great_circle_distance((st["lat"], st["lng"]), (loc["lat"], loc["lng"]), unit="m") * (1 if transit else 2.5)
        if d < bd:
            best, bd = p, d
    if best and bd < 400:
        st["photos"] = _photos(best)[:3]
        st["photo"] = st["photos"][0]
        st["place_title"] = best.get("title")
        st["caption"] = jload("captions.json", {}).get(st["photo"])
    return st


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
                                 "here alone at 11pm. In ONE sentence of at most 14 words, tell them where to stand, "
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
    base = PlanReq(budget=req.budget, origin=req.origin, destination=req.destination).dict()
    if d == base:
        return f"{req.budget:g}"
    return f"{req.budget:g}:" + hashlib.sha1(json.dumps(d, sort_keys=True).encode()).hexdigest()[:10]


# ---------------------------------------------------------------- routes
@app.get("/")
def index():
    return FileResponse(ROOT / "index.html")


@app.get("/api/config")
def config():
    return {"defaults": PlanReq().dict(), "fares": FARES, "rideshare": RIDESHARE,
            "trip": {"origin": ROUTES["origin"], "destination": ROUTES["destination"]}}


@app.get("/api/cells")
def cells(p: Optional[str] = None, depart: str = "23:00"):
    scoring = Scoring(**json.loads(p)) if p else Scoring()
    return cells_for(scoring, depart)


@app.get("/api/routes")
def routes():
    return {**ROUTES, "stop_cards": {s: stop_card(s) for s in ROUTES["stops"]}}


@app.post("/api/plan")
def plan(req: PlanReq, demo: int = 0):
    key = cache_key(req)
    cache = jload("demo_cache.json", {})
    use_saved = bool(demo) or not os.environ.get("ANTHROPIC_API_KEY")
    hit = cache.get(key) or (cache.get(f"{req.budget:g}") if use_saved else None)
    if use_saved and hit:
        return {**enrich(dict(hit["plan"])), "cached": True, "trace": hit["trace"]}
    if use_saved:
        raise HTTPException(503, "No saved plan for this trip, and there is no Anthropic key to plan a new one.")
    p, trace = run_agent(req)
    cache = jload("demo_cache.json", {})
    cache[key] = {"plan": p, "trace": trace}
    jsave("demo_cache.json", cache)
    return {**enrich(dict(p)), "cached": False, "trace": trace}


class CapReq(BaseModel):
    image_url: str
    stop_name: str = "the stop"


@app.post("/api/caption")
def caption(req: CapReq):
    try:
        return {"caption": caption_image(req.image_url, req.stop_name)}
    except httpx.HTTPError as e:
        raise HTTPException(502, f"could not fetch image: {e}")


@app.post("/api/live")
def live_refresh():
    """Scrape fresh X posts and news with Apify. No Claude call."""
    token = os.environ.get("APIFY_TOKEN")
    if not token:
        raise HTTPException(400, "APIFY_TOKEN is missing from .env")
    from apify_client import ApifyClient
    import scrape
    client = ApifyClient(token)
    tweets = scrape.scrape_x(client)
    articles = scrape.scrape_news(client)
    scrape.save("x", tweets)
    scrape.save("news", articles)
    return {
        "scraped": len(tweets) + len(articles),
        "tweets": len(tweets),
        "articles": len(articles),
        "added": 0,
        "note": "Apify scrape saved. Placing new posts on the map still needs an Anthropic key.",
    }


@app.get("/api/usage")
def usage():
    return usage_summary()
