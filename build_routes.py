"""Build the 4 hardcoded candidate routes for the demo trip -> data/routes.json.

Walk and car geometry comes from public OSRM servers; rail legs (BART under
Mission St, N-Judah via Market subway / Duboce / Sunset Tunnel / Carl / Irving)
are hand-drawn. Coordinates are stored as [lat, lng].
"""
import json
from pathlib import Path

import subprocess

DATA = Path(__file__).parent / "data"

P = {  # [lat, lng]
    "origin": [37.76506, -122.41963],          # Mission & 16th St
    "bart16": [37.76506, -122.41969],          # 16th St Mission BART
    "church16": [37.76442, -122.42860],        # Church St & 16th St (J-Church)
    "churchduboce": [37.76941, -122.42925],    # Church St & Duboce Ave (J/N transfer)
    "civic": [37.77962, -122.41388],           # Civic Center / UN Plaza BART + Muni Metro
    "dest": [37.76410, -122.46652],            # 9th Ave & Irving St
}

BART_16_CIVIC = [P["bart16"], [37.76970, -122.42030], [37.77290, -122.41780], [37.77650, -122.41400], P["civic"]]
N_CIVIC_DUBOCE = [P["civic"], [37.77507, -122.41925], [37.77030, -122.42650], P["churchduboce"]]
N_DUBOCE_IRVING = [P["churchduboce"], [37.76910, -122.43350], [37.76580, -122.44980], [37.76610, -122.45280],
                   [37.76590, -122.45810], [37.76440, -122.45800], P["dest"]]
J_16_DUBOCE = [P["church16"], [37.76763, -122.42905], P["churchduboce"]]


def osrm(profile, a, b):
    base = ("https://routing.openstreetmap.de/routed-foot/route/v1/foot" if profile == "foot"
            else "https://router.project-osrm.org/route/v1/driving")
    # curl, not httpx: the system Python's LibreSSL can't handshake with these hosts
    url = f"{base}/{a[1]},{a[0]};{b[1]},{b[0]}?overview=full&geometries=geojson"
    rt = json.loads(subprocess.run(["curl", "-sf", "-m", "20", url], capture_output=True, check=True).stdout)["routes"][0]
    return [[c[1], c[0]] for c in rt["geometry"]["coordinates"]], rt["distance"] / 1609.34, rt["duration"] / 60


def leg(mode, label, coords, minutes, miles=None, line=None):
    return {"mode": mode, "label": label, "line": line, "minutes": round(minutes), "miles": round(miles, 2) if miles else None,
            "coords": [[round(y, 6), round(x, 6)] for y, x in coords]}


def main():
    walk16, walk16_mi, walk16_min = osrm("foot", P["origin"], P["church16"])
    car_short, short_mi, short_min = osrm("car", P["origin"], P["churchduboce"])
    car_full, full_mi, full_min = osrm("car", P["origin"], P["dest"])

    stops = {
        "bart16": {"id": "bart16", "name": "16th St Mission BART", "lat": P["bart16"][0], "lng": P["bart16"][1], "kind": "BART station"},
        "civic": {"id": "civic", "name": "Civic Center / UN Plaza", "lat": P["civic"][0], "lng": P["civic"][1], "kind": "BART station"},
        "church16": {"id": "church16", "name": "Church St & 16th St (J)", "lat": P["church16"][0], "lng": P["church16"][1], "kind": "bus stop"},
        "churchduboce": {"id": "churchduboce", "name": "Church St & Duboce Ave (N)", "lat": P["churchduboce"][0], "lng": P["churchduboce"][1], "kind": "bus stop"},
        "irving9": {"id": "irving9", "name": "Irving St & 9th Ave (N)", "lat": P["dest"][0], "lng": P["dest"][1], "kind": "bus stop"},
    }

    routes = [
        {"id": "muni", "name": "All-Muni: walk, J-Church, N-Judah", "modes": ["walk", "muni"],
         "fare_keys": ["muni"], "rideshare": None, "wait_minutes": 16,
         "stops": ["church16", "churchduboce", "irving9"],
         "legs": [leg("walk", "Walk 16th St to Church", walk16, walk16_min, walk16_mi),
                  leg("muni", "J-Church to Duboce", J_16_DUBOCE, 4, line="J"),
                  leg("muni", "N-Judah to 9th & Irving", N_DUBOCE_IRVING, 13, line="N")]},
        {"id": "bart_n", "name": "BART to Civic Center, then N-Judah", "modes": ["bart", "muni"],
         "fare_keys": ["bart_16th_civic", "muni_transfer_from_bart"], "rideshare": None, "wait_minutes": 18,
         "stops": ["bart16", "civic", "irving9"],
         "legs": [leg("bart", "BART 16th St to Civic Center", BART_16_CIVIC, 5, line="BART"),
                  leg("muni", "N-Judah Civic Center to 9th & Irving", N_CIVIC_DUBOCE + N_DUBOCE_IRVING[1:], 22, line="N")]},
        {"id": "uber_n", "name": "Short Uber to Church & Duboce, then N-Judah", "modes": ["uber", "muni"],
         "fare_keys": ["muni"], "rideshare": {"miles": round(short_mi, 2), "minutes": round(short_min + 2)}, "wait_minutes": 12,
         "stops": ["churchduboce", "irving9"],
         "legs": [leg("uber", "Uber to Church & Duboce", car_short, short_min + 2, short_mi),
                  leg("muni", "N-Judah to 9th & Irving", N_DUBOCE_IRVING, 13, line="N")]},
        {"id": "uber", "name": "Uber door to door", "modes": ["uber"],
         "fare_keys": [], "rideshare": {"miles": round(full_mi, 2), "minutes": round(full_min + 4)}, "wait_minutes": 5,
         "stops": [],
         "legs": [leg("uber", "Uber to 9th & Irving", car_full, full_min + 4, full_mi)]},
    ]
    for r in routes:
        r["minutes"] = r["wait_minutes"] + sum(l["minutes"] for l in r["legs"])
    out = {"origin": {"name": "Mission St & 16th St", "lat": P["origin"][0], "lng": P["origin"][1]},
           "destination": {"name": "9th Ave & Irving St", "lat": P["dest"][0], "lng": P["dest"][1]},
           "depart": "23:00", "stops": stops, "routes": routes}
    (DATA / "routes.json").write_text(json.dumps(out, indent=1))
    for r in routes:
        print(f"{r['id']:7s} {r['minutes']:3d} min  {r['name']}")


if __name__ == "__main__":
    main()
