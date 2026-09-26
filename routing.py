"""Live trip building for any origin/destination in San Francisco.

- Nominatim (OpenStreetMap) geocoding + reverse geocoding
- Transitous (MOTIS, GTFS + GTFS-realtime) transit itineraries for Muni, BART, etc.
- OSRM walking + driving geometry (for walk-only and Uber legs)
- BART fare API (public key published by BART) + Muni Clipper fare
All free, no keys. Returns the same route shape the agent and UI already use.
"""
import json
import math
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import httpx

DATA = Path(__file__).parent / "data"
SF_TZ = ZoneInfo("America/Los_Angeles")
UA = {"User-Agent": "StreetSmart/1.0 (Claude Opus 5.5 build day demo)"}
SF_VIEWBOX = "-122.5155,37.8120,-122.3550,37.7080"  # lon1,lat1,lon2,lat2
BART_KEY = "MW9S-E7SL-26DU-VV8V"  # BART's published public API key (api.bart.gov/docs/overview/index.aspx)
MUNI_FARE = 2.85
CABLE_CAR_FARE = 9.00
TRANSFER_DISCOUNT = 2.85  # Clipper inter-agency transfer discount (within 2h)
_cache, _lock = {}, threading.Lock()


def fetch_json(url, params=None, timeout=25):
    """httpx first; the macOS system Python's LibreSSL can't handshake with some hosts, so fall back to curl."""
    full = url + ("?" + urlencode(params) if params else "")
    try:
        r = httpx.get(full, headers=UA, timeout=timeout, follow_redirects=True)
        r.raise_for_status()
        return r.json()
    except (httpx.ConnectError, httpx.RemoteProtocolError):
        out = subprocess.run(["curl", "-sfL", "-m", str(timeout), "-A", UA["User-Agent"], full],
                             capture_output=True, check=True).stdout
        return json.loads(out)


def cached(key, ttl, fn):
    with _lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
    val = fn()
    with _lock:
        _cache[key] = (time.time(), val)
    return val


def dist_m(a, b):
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * 6371000 * math.asin(math.sqrt(h))


# ---------------------------------------------------------------- geocoding
def _short(d):
    a = d.get("address") or {}
    name = d.get("name") or ""
    road = a.get("road") or a.get("pedestrian") or ""
    num = a.get("house_number")
    hood = a.get("neighbourhood") or a.get("suburb") or a.get("quarter") or ""
    main = name or (f"{num} {road}" if num else road) or d.get("display_name", "").split(",")[0]
    sub = ", ".join(x for x in [road if name and road != name else "", hood] if x)
    return main, sub


def geocode(q, limit=6):
    q = q.strip()
    if len(q) < 2:
        return []

    def go():
        res = fetch_json("https://nominatim.openstreetmap.org/search", {
            "q": q if "san francisco" in q.lower() else f"{q}, San Francisco", "format": "jsonv2",
            "addressdetails": 1, "limit": limit, "viewbox": SF_VIEWBOX, "bounded": 1})
        out = []
        for d in res:
            main, sub = _short(d)
            out.append({"name": main, "sub": sub, "lat": float(d["lat"]), "lng": float(d["lon"])})
        return out
    return cached(("geo", q.lower()), 3600, go)


def reverse(lat, lng):
    def go():
        d = fetch_json("https://nominatim.openstreetmap.org/reverse", {
            "lat": lat, "lon": lng, "format": "jsonv2", "addressdetails": 1, "zoom": 18})
        a = d.get("address") or {}
        road, num = a.get("road") or "", a.get("house_number")
        main = (d.get("name") or (f"{num} {road}" if num else road) or "Dropped pin")
        return {"name": main, "sub": a.get("neighbourhood") or a.get("suburb") or "", "lat": lat, "lng": lng}
    return cached(("rev", round(lat, 5), round(lng, 5)), 3600, go)


# ---------------------------------------------------------------- geometry
def decode_polyline(s, precision=6):
    coords, idx, lat, lng, f = [], 0, 0, 0, 10 ** precision
    while idx < len(s):
        for which in (0, 1):
            shift = result = 0
            while True:
                b = ord(s[idx]) - 63
                idx += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            d = ~(result >> 1) if result & 1 else result >> 1
            if which == 0:
                lat += d
            else:
                lng += d
        coords.append([round(lat / f, 6), round(lng / f, 6)])
    return coords


def osrm(profile, a, b):
    base = ("https://routing.openstreetmap.de/routed-foot/route/v1/foot" if profile == "foot"
            else "https://router.project-osrm.org/route/v1/driving")

    def go():
        rt = fetch_json(f"{base}/{a[1]},{a[0]};{b[1]},{b[0]}", {"overview": "full", "geometries": "geojson"})["routes"][0]
        return {"coords": [[round(c[1], 6), round(c[0], 6)] for c in rt["geometry"]["coordinates"]],
                "miles": rt["distance"] / 1609.34, "minutes": rt["duration"] / 60}
    return cached(("osrm", profile, tuple(map(lambda x: round(x, 5), a)), tuple(map(lambda x: round(x, 5), b))), 3600, go)


# ---------------------------------------------------------------- fares
def bart_stations():
    p = DATA / "bart_stations.json"
    if p.exists():
        return json.loads(p.read_text())
    st = fetch_json("https://api.bart.gov/api/stn.aspx", {"cmd": "stns", "key": BART_KEY, "json": "y"})["root"]["stations"]["station"]
    out = [{"abbr": s["abbr"], "name": s["name"], "lat": float(s["gtfs_latitude"]), "lng": float(s["gtfs_longitude"])} for s in st]
    p.write_text(json.dumps(out, indent=1))
    return out


def bart_abbr(lat, lng):
    return min(bart_stations(), key=lambda s: dist_m((lat, lng), (s["lat"], s["lng"])))["abbr"]


def bart_fare(a_abbr, b_abbr):
    def go():
        d = fetch_json("https://api.bart.gov/api/sched.aspx", {"cmd": "fare", "orig": a_abbr, "dest": b_abbr,
                                                               "key": BART_KEY, "json": "y"})
        fares = d["root"]["fares"]["fare"]
        return float(next(f["@amount"] for f in fares if f["@class"] == "clipper"))
    return cached(("bart", a_abbr, b_abbr), 86400, go)


def transit_fare(legs):
    """Clipper adult fare for the transit legs: Muni once per 120 min, BART by station pair, and the
    $2.85 inter-agency discount on the next agency within 2 hours."""
    total, parts, agencies_paid = 0.0, [], []
    for l in legs:
        ag = l["agency"]
        if ag == "muni" and "muni" in agencies_paid and l.get("line") not in ("PH", "PM", "C"):
            continue
        if ag == "muni":
            fare = CABLE_CAR_FARE if l.get("line") in ("PH", "PM", "C") else MUNI_FARE
            label = f"Muni {l.get('line') or ''}".strip()
        elif ag == "bart":
            try:
                fare = bart_fare(l["from_abbr"], l["to_abbr"])
            except Exception:
                fare = 3.50
            label = f"BART {l['from_abbr']}-{l['to_abbr']}"
        else:
            fare, label = 3.00, f"{l.get('agency_name') or 'Transit'} (est.)"
        if agencies_paid and agencies_paid[-1] != ag:
            fare = max(0.0, fare - TRANSFER_DISCOUNT)
            label += " (transfer discount)"
        agencies_paid.append(ag)
        total += fare
        parts.append({"label": label, "usd": round(fare, 2)})
    return round(total, 2), parts


# ---------------------------------------------------------------- Transitous
MODE_OF = {"BUS": "muni", "TRAM": "muni", "CABLE_CAR": "muni", "FUNICULAR": "muni", "TROLLEYBUS": "muni",
           "SUBWAY": "bart", "METRO": "bart", "RAIL": "rail", "REGIONAL_RAIL": "rail", "FERRY": "rail"}


def _agency(leg):
    name = (leg.get("agencyName") or "").lower()
    if "municipal" in name or "sfmta" in name or "muni" in name:
        return "muni"
    if "bay area rapid" in name or "bart" in name:
        return "bart"
    return "other"


def _t(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def transitous(a, b, when, arrive_by=False, n=4):
    def go():
        return fetch_json("https://api.transitous.org/api/v5/plan", {
            "fromPlace": f"{a[0]},{a[1]}", "toPlace": f"{b[0]},{b[1]}",
            "time": when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "arriveBy": str(arrive_by).lower(), "numItineraries": n}, timeout=40)
    key = ("tr", round(a[0], 4), round(a[1], 4), round(b[0], 4), round(b[1], 4), when.strftime("%Y%m%d%H%M")[:-1], arrive_by)
    return cached(key, 120, go)


def itinerary_to_route(it, rid, depart):
    legs, stops, transit_legs = [], [], []
    first_board = None
    for l in it["legs"]:
        mins = max(1, round(l["duration"] / 60))
        coords = decode_polyline(l["legGeometry"]["points"], l["legGeometry"].get("precision", 6)) if l.get("legGeometry") else \
            [[l["from"]["lat"], l["from"]["lon"]], [l["to"]["lat"], l["to"]["lon"]]]
        if l["mode"] == "WALK":
            to = l["to"]["name"]
            legs.append({"mode": "walk", "label": f"Walk to {to}" if to not in ("END", "") else "Walk to destination",
                         "line": None, "minutes": mins, "miles": round((l.get("distance") or 0) / 1609.34, 2) or None, "coords": coords})
            continue
        ag = _agency(l)
        mode = "bart" if ag == "bart" else "muni" if ag == "muni" else MODE_OF.get(l["mode"], "rail")
        line = l.get("routeShortName") or l.get("routeLongName") or ""
        dep = _t(l["startTime"]).astimezone(SF_TZ).strftime("%-I:%M")
        tl = {"agency": ag, "agency_name": l.get("agencyName"), "line": line}
        for end, key in ((l["from"], "from"), (l["to"], "to")):
            sid = end.get("stopId") or f"{end['lat']:.5f},{end['lon']:.5f}"
            kind = "BART station" if ag == "bart" else "Muni stop" if ag == "muni" else "stop"
            stops.append({"id": sid, "name": end["name"], "lat": end["lat"], "lng": end["lon"], "kind": kind})
            if ag == "bart":
                tl[f"{key}_abbr"] = bart_abbr(end["lat"], end["lon"])
        transit_legs.append(tl)
        if first_board is None:
            first_board = _t(l["startTime"])
        legs.append({"mode": mode, "label": f"{line} to {l['to']['name']}".strip(), "line": line[:4] or None,
                     "minutes": mins, "miles": None, "coords": coords, "departs": dep, "headsign": l.get("headsign"),
                     "realtime": bool(l.get("realTime")), "agency": ag})
    # waiting = transfer gaps + a 3 min buffer at the first stop
    gaps = 0
    for x, y in zip(it["legs"], it["legs"][1:]):
        gaps += max(0, (_t(y["startTime"]) - _t(x["endTime"])).total_seconds() / 60)
    fare, parts = transit_fare(transit_legs)
    start = _t(it["startTime"])
    lines = [t["line"] for t in transit_legs if t["line"]]
    uniq = []
    for s in stops:
        if s["id"] not in [u["id"] for u in uniq]:
            uniq.append(s)
    modes = list(dict.fromkeys(l["mode"] for l in legs))
    return {"id": rid, "name": " + ".join(lines) if lines else "Transit",
            "modes": modes, "legs": legs, "stops": uniq, "wait_minutes": round(gaps + (3 if transit_legs else 0)),
            "fare": {"usd": fare, "parts": parts}, "rideshare": None,
            "leave_at": start.astimezone(SF_TZ).strftime("%H:%M"),
            "arrive_at": _t(it["endTime"]).astimezone(SF_TZ).strftime("%H:%M"),
            "minutes": round((_t(it["endTime"]) - depart).total_seconds() / 60),
            "realtime": any(l.get("realtime") for l in legs), "_it": it, "_transit_legs": transit_legs}


def build_trip(origin, dest, depart=None, arrive_by=None):
    """origin/dest: {name, lat, lng}. depart: aware datetime (default now, SF time)."""
    depart = depart or datetime.now(SF_TZ)
    a, b = (origin["lat"], origin["lng"]), (dest["lat"], dest["lng"])
    with ThreadPoolExecutor(4) as ex:
        f_tr = ex.submit(transitous, a, b, depart)
        f_car = ex.submit(osrm, "car", a, b)
        f_walk = ex.submit(osrm, "foot", a, b)
    routes, errors = [], []
    try:
        its = f_tr.result().get("itineraries") or []
    except Exception as e:
        its, _ = [], errors.append(f"transit: {e}")
    seen = set()
    for it in its:
        sig = tuple((l["mode"], l.get("routeShortName")) for l in it["legs"] if l["mode"] != "WALK")
        if not sig or sig in seen:
            continue
        seen.add(sig)
        try:
            routes.append(itinerary_to_route(it, f"transit{len(routes) + 1}", depart))
        except Exception as e:
            errors.append(f"itinerary: {e}")
        if len(routes) >= 3:
            break

    # Uber door to door
    try:
        car = f_car.result()
        routes.append({"id": "uber", "name": "Uber door to door", "modes": ["uber"], "stops": [], "wait_minutes": 5,
                       "legs": [{"mode": "uber", "label": "Uber to destination", "line": None,
                                 "minutes": round(car["minutes"] * 1.15 + 1), "miles": round(car["miles"], 2), "coords": car["coords"]}],
                       "fare": {"usd": 0.0, "parts": []},
                       "rideshare": {"miles": round(car["miles"], 2), "minutes": round(car["minutes"] * 1.15 + 1)},
                       "realtime": False})
    except Exception as e:
        errors.append(f"drive: {e}")

    # Walk only (if it's a reasonable walk)
    try:
        w = f_walk.result()
        if w["minutes"] <= 45:
            routes.append({"id": "walk", "name": "Walk the whole way", "modes": ["walk"], "stops": [], "wait_minutes": 0,
                           "legs": [{"mode": "walk", "label": "Walk to destination", "line": None, "minutes": round(w["minutes"]),
                                     "miles": round(w["miles"], 2), "coords": w["coords"]}],
                           "fare": {"usd": 0.0, "parts": []}, "rideshare": None, "realtime": False})
    except Exception as e:
        errors.append(f"walk: {e}")

    # Hybrids built on the best transit itinerary
    best = next((r for r in routes if r["id"].startswith("transit")), None)
    if best:
        tl = [i for i, l in enumerate(best["legs"]) if l["mode"] not in ("walk",)]
        first, last = tl[0], tl[-1]
        board = best["legs"][first]["coords"][0]
        # 1) short Uber to the first boarding stop (skips the first walk and the wait at that corner)
        if first > 0 and best["legs"][0]["minutes"] >= 6:
            try:
                car = osrm("car", a, board)
                rest = best["legs"][first:]
                routes.append({"id": "uber_transit", "name": f"Short Uber, then {best['name']}", "modes": ["uber"] + [m for m in best["modes"] if m != "uber"],
                               "stops": best["stops"], "wait_minutes": max(3, best["wait_minutes"] - 2),
                               "legs": [{"mode": "uber", "label": f"Uber to {best['stops'][0]['name']}", "line": None,
                                         "minutes": round(car["minutes"] * 1.15 + 1), "miles": round(car["miles"], 2), "coords": car["coords"]}] + rest,
                               "fare": best["fare"], "rideshare": {"miles": round(car["miles"], 2), "minutes": round(car["minutes"] * 1.15 + 1)},
                               "realtime": best["realtime"]})
            except Exception as e:
                errors.append(f"hybrid1: {e}")
        # 2) transit for the first ride, then Uber for the last stretch
        if len(tl) > 1 or (len(best["legs"]) - 1 - last) > 0 and best["legs"][-1]["minutes"] >= 6:
            try:
                alight = best["legs"][first]["coords"][-1]
                car = osrm("car", alight, b)
                head = best["legs"][:first + 1]
                first_ride = best["_transit_legs"][:1]
                fare, parts = transit_fare(first_ride)
                routes.append({"id": "transit_uber", "name": f"{best['legs'][first]['line'] or 'Transit'}, then short Uber",
                               "modes": list(dict.fromkeys([l["mode"] for l in head] + ["uber"])),
                               "stops": best["stops"][:2], "wait_minutes": 3 + 4,
                               "legs": head + [{"mode": "uber", "label": "Uber for the last stretch", "line": None,
                                                "minutes": round(car["minutes"] * 1.15 + 1), "miles": round(car["miles"], 2), "coords": car["coords"]}],
                               "fare": {"usd": fare, "parts": parts},
                               "rideshare": {"miles": round(car["miles"], 2), "minutes": round(car["minutes"] * 1.15 + 1)},
                               "realtime": best["realtime"]})
            except Exception as e:
                errors.append(f"hybrid2: {e}")

    for r in routes:
        r.setdefault("minutes", r["wait_minutes"] + sum(l["minutes"] for l in r["legs"]))
        if r["id"] in ("uber_transit",) and best:
            r["minutes"] = max(r["wait_minutes"] + sum(l["minutes"] for l in r["legs"]), best["minutes"] - 3)
        r.setdefault("arrive_at", (depart + timedelta(minutes=r["minutes"])).strftime("%H:%M"))
        r.setdefault("leave_at", depart.strftime("%H:%M"))
        r.pop("_it", None)
        r.pop("_transit_legs", None)
    stops = {s["id"]: s for r in routes for s in r["stops"]}
    for r in routes:
        r["stops"] = [s["id"] for s in r["stops"]]
    return {"origin": origin, "destination": dest, "depart": depart.strftime("%H:%M"),
            "depart_iso": depart.isoformat(), "live": True, "stops": stops, "routes": routes, "errors": errors}


if __name__ == "__main__":
    t0 = time.time()
    trip = build_trip({"name": "Mission & 16th", "lat": 37.76506, "lng": -122.41963},
                      {"name": "9th & Irving", "lat": 37.7641, "lng": -122.46652})
    for r in trip["routes"]:
        print(f"{r['id']:13s} {r['minutes']:3d} min  ${r['fare']['usd']:.2f}  wait {r['wait_minutes']:2d}  {r['name']}  | "
              + " > ".join(f"{l['mode']}:{l['label'][:28]}" for l in r["legs"]))
    print("stops:", [s["name"] for s in trip["stops"].values()])
    print("errors:", trip["errors"], f"{time.time() - t0:.1f}s")
    print(geocode("ferry building")[:2])
