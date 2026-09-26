"""Step 2: raw posts -> data/incidents.json.

Keyword pre-filter first (cheap), then Opus 5.5 at effort=low reads batches of 50
and returns structured incidents via a strict tool. DataSF rows are already
structured, so they skip the LLM entirely.
"""
import json
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from llm import call, tool_input

DATA = Path(__file__).parent / "data"
RAW = DATA / "raw"
BATCH = 50

INCIDENT_RE = re.compile(
    r"\b(rob|robbed|robbery|mugg|fight|fought|assault|attack|stab|shoot|shot|shooting|gun|knife|"
    r"police|cops?|sfpd|arrest|broken (street)?light|dark|unsafe|sketchy|dangerous|harass|followed|"
    r"yell|threat|theft|stole|stolen|break[- ]?in|smash|needle|overdose|fire|crash|hit[- ]and[- ]run|"
    r"scary|sketch|safe|safety|crime)\w*", re.I)

TYPES = ["violent", "theft", "harassment", "hazard", "police_activity", "other"]

SAVE_TOOL = {
    "name": "save_incidents",
    "description": "Save every concrete, located safety incident found in this batch of posts. Call exactly once per batch; pass an empty list if there are none.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["incidents"],
        "properties": {"incidents": {"type": "array", "items": {
            "type": "object",
            "additionalProperties": False,
            "required": ["item_id", "source", "location_text", "lat", "lng", "incident_type", "severity",
                         "occurred_at", "confidence", "summary"],
            "properties": {
                "item_id": {"type": "string", "description": "the [id] of the item"},
                "source": {"type": "string", "enum": ["x", "news"]},
                "location_text": {"type": "string", "description": "Place as written, e.g. '16th & Mission BART plaza'"},
                "lat": {"type": "number", "description": "0 if you can't place it"},
                "lng": {"type": "number", "description": "0 if you can't place it"},
                "incident_type": {"type": "string", "enum": TYPES},
                "severity": {"type": "integer", "description": "1 minor hazard .. 5 serious violence"},
                "occurred_at": {"type": "string", "description": "ISO 8601; use the post time if not stated"},
                "confidence": {"type": "number", "description": "0-1: how sure the incident happened at that exact spot"},
                "summary": {"type": "string", "description": "<= 12 words, factual, no names"},
            },
        }}},
    },
}

SYSTEM = """You extract street-safety incidents in San Francisco from X posts and local news items for a navigation app.

Rules:
- Only real, specific events at a specific place (an intersection, station, block, or landmark). Skip opinions, general complaints ("SF is unsafe"), jokes, news about other cities, and posts with no place.
- Give lat/lng only for places you can recognize in SF (intersections, BART/Muni stations, landmarks). If you can't place it precisely, use your best guess with confidence <= 0.4, or 0/0.
- confidence reflects both whether it happened and whether the location is right.
- Severity: 5 shooting/stabbing, 4 assault/armed robbery, 3 robbery/fight/harassment, 2 theft/break-in/police presence, 1 hazards (broken streetlight, dark stretch, needles).
- Summaries are short, factual, and never name or describe individuals.
Always finish by calling save_incidents exactly once (with an empty list if nothing qualifies)."""


def load(name):
    p = RAW / f"{name}.json"
    return json.loads(p.read_text()) if p.exists() else []


def normalize():
    items = []
    for t in load("x"):
        items.append({"item_id": f"x:{t.get('id')}", "source": "x", "created_at": t.get("createdAt", ""),
                      "text": t.get("text") or "", "url": t.get("url", "")})
    for n in load("news"):
        text = f"{n.get('title') or ''} ({n.get('source') or ''})\n{n.get('description') or ''}".strip()
        items.append({"item_id": f"news:{n.get('url')}", "source": "news", "created_at": n.get("publishedAt") or "",
                      "text": text, "url": n.get("url", "")})
    seen, out = set(), []
    for p in items:
        if p["item_id"] in seen or not p["text"]:
            continue
        seen.add(p["item_id"])
        out.append(p)
    return out


def extract_batch(i, batch):
    lines = [f"[{p['item_id']}] ({p['source']}, {p['created_at']}) {p['text'][:700]}" for p in batch]
    resp = call(f"extract:batch{i}", max_tokens=16000, system=SYSTEM, tools=[SAVE_TOOL],
                tool_choice={"type": "auto"}, output_config={"effort": "low"},
                messages=[{"role": "user", "content": "Items:\n\n" + "\n\n".join(lines)}])
    if resp.stop_reason == "refusal":
        print(f"  batch {i}: refusal, skipped")
        return []
    got = tool_input(resp, "save_incidents")
    if got is None:
        print(f"  batch {i}: no tool call (stop={resp.stop_reason}), skipped")
        return []
    return got["incidents"]


# DataSF categories -> (type, severity). Structured data: no LLM needed.
SF_MAP = {
    "Assault": ("violent", 4), "Robbery": ("violent", 4), "Homicide": ("violent", 5),
    "Weapons Offense": ("violent", 4), "Weapons Carrying Etc": ("violent", 3),
    "Larceny Theft": ("theft", 2), "Burglary": ("theft", 2), "Motor Vehicle Theft": ("theft", 2),
    "Malicious Mischief": ("hazard", 1), "Vandalism": ("hazard", 1), "Disorderly Conduct": ("harassment", 2),
    "Drug Offense": ("police_activity", 1), "Warrant": ("police_activity", 1),
}


def datasf_incidents():
    out = []
    for r in load("datasf"):
        cat = r.get("incident_category") or ""
        if cat not in SF_MAP or not r.get("latitude"):
            continue
        typ, sev = SF_MAP[cat]
        out.append({"item_id": f"datasf:{r.get('incident_id') or r.get('row_id')}", "source": "datasf",
                    "location_text": r.get("intersection") or "", "lat": float(r["latitude"]),
                    "lng": float(r["longitude"]), "incident_type": typ, "severity": sev,
                    "occurred_at": (r.get("incident_datetime") or "")[:19] + "-07:00",  # SFPD times are local (PDT)
                    "confidence": 0.95, "url": "https://data.sf.gov/d/wg3w-h783",
                    "summary": f"SFPD report: {(r.get('incident_subcategory') or cat).lower()}"})
    return out


def main():
    posts = normalize()
    relevant = [p for p in posts if INCIDENT_RE.search(p["text"])]
    by = lambda xs, s: sum(p["source"] == s for p in xs)
    print(f"items: {len(posts)} scraped (x {by(posts, 'x')}, news {by(posts, 'news')}) -> {len(relevant)} after keyword "
          f"pre-filter (x {by(relevant, 'x')}, news {by(relevant, 'news')}); {len(posts) - len(relevant)} never sent to Opus")
    batches = [relevant[i:i + BATCH] for i in range(0, len(relevant), BATCH)]
    with ThreadPoolExecutor(4) as ex:
        results = list(ex.map(lambda a: extract_batch(*a), enumerate(batches)))
    src = {p["item_id"]: p for p in relevant}
    incidents = []
    for inc in (i for r in results for i in r):
        p = src.get(inc["item_id"])
        if inc["confidence"] < 0.5 or not inc["lat"] or not (37.70 < inc["lat"] < 37.82 and -122.52 < inc["lng"] < -122.35):
            continue
        inc["source"] = p["source"] if p else inc["item_id"].split(":")[0]
        inc["url"] = p["url"] if p else ""
        incidents.append(inc)
    sf = datasf_incidents()
    print(f"Opus kept {len(incidents)} located incidents (conf >= 0.5); DataSF adds {len(sf)}")
    (DATA / "incidents.json").write_text(json.dumps(incidents + sf, indent=1))
    print(f"saved {len(incidents) + len(sf)} -> data/incidents.json")


if __name__ == "__main__":
    main()
