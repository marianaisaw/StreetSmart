"""Step 1: one-shot scrape of the demo dataset via Apify + DataSF.

Writes data/raw/{x,news,datasf,maps}.json. Every Apify run has maxItems and a max
total charge, and its real cost is appended to data/usage.log.
Run one source again with e.g. `python scrape.py news`.
"""
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
from apify_client import ApifyClient
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env", override=True)
DATA = Path(__file__).parent / "data"
RAW = DATA / "raw"
RAW.mkdir(parents=True, exist_ok=True)

# Corridor: Mission/16th -> Civic Center (BART) -> Market/Church/Duboce -> Cole Valley -> 9th & Irving.
# GeoJSON order is [lng, lat].
CORRIDOR = {
    "type": "Polygon",
    "coordinates": [[
        [-122.4235, 37.7610],
        [-122.4150, 37.7610],
        [-122.4085, 37.7805],
        [-122.4175, 37.7835],
        [-122.4380, 37.7720],
        [-122.4720, 37.7675],
        [-122.4720, 37.7605],
        [-122.4400, 37.7615],
        [-122.4235, 37.7610],
    ]],
}
# bbox around the corridor (a little wider, so Tenderloin-edge reports near Civic Center count)
BBOX = dict(south=37.7590, north=37.7870, west=-122.4730, east=-122.4060)


def save(name, items):
    path = RAW / f"{name}.json"
    path.write_text(json.dumps(items, indent=1, default=str))
    print(f"  saved {len(items)} items -> data/raw/{name}.json")


def _field(run, *names, default=None):
    for name in names:
        value = run.get(name) if isinstance(run, dict) else getattr(run, name, None)
        if value is not None:
            return value
    return default


def log_apify(tag, run):
    usd = float(_field(run, "usage_total_usd", "usageTotalUsd", default=0) or 0)
    with (DATA / "usage.log").open("a") as f:
        f.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "tag": f"apify:{tag}", "model": "apify",
                            "input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "usd": round(usd, 5),
                            "secs": 0, "stop": _field(run, "status")}) + "\n")


def run_actor(client, actor, run_input, max_items, max_usd, name, quiet=False):
    if not quiet:
        print(f"[{name}] running {actor} (maxItems={max_items}, max ${max_usd})")
    run = client.actor(actor).call(run_input=run_input, max_items=max_items,
                                   max_total_charge_usd=Decimal(str(max_usd)), logger=None)
    if not run:
        raise RuntimeError("run returned nothing")
    run = client.run(_field(run, "id")).get() or run
    log_apify(name, run)
    items = list(client.dataset(_field(run, "default_dataset_id", "defaultDatasetId")).iterate_items())
    status = _field(run, "status")
    usd = float(_field(run, "usage_total_usd", "usageTotalUsd", default=0) or 0)
    print(f"  [{name}] status={status} items={len(items)} cost=${usd:.3f}")
    if status != "SUCCEEDED" and not items:
        raise RuntimeError(f"run {status}")
    return items


# ---------------------------------------------------------------- X
X_QUERIES = [
    '(bart OR muni) (robbery OR fight OR assault OR stabbing OR police) "san francisco"',
    '(tenderloin OR "civic center" OR "mission st" OR "16th st") (robbery OR assault OR stabbing OR police OR unsafe)',
    '("inner sunset" OR "n judah" OR "16th and mission" OR "broken streetlight") (sf OR "san francisco")',
]


def norm_tweet(t):
    return {"id": str(t.get("id") or t.get("id_str") or t.get("tweetId") or ""),
            "text": t.get("text") or t.get("fullText") or t.get("full_text") or "",
            "createdAt": t.get("createdAt") or t.get("created_at") or "",
            "url": t.get("url") or t.get("twitterUrl") or t.get("tweetUrl") or "",
            "place": t.get("place") or t.get("geo") or t.get("location") or None}


def scrape_x(client):
    since = (datetime.now() - timedelta(days=14)).strftime("%Y-%m-%d")
    try:
        items = run_actor(client, "xquik/x-tweet-scraper", {
            "mode": "search", "searchTerms": X_QUERIES, "queryType": "Latest", "lang": "en",
            "since": since, "maxItems": 300, "maxItemsPerTarget": 100, "outputVariant": "compact", "includeSearchTerms": True,
        }, 300, 0.3, "x")
        if not [t for t in items if norm_tweet(t)["text"]]:
            raise RuntimeError("no tweets with text")
    except Exception as e:
        print(f"  xquik failed ({e}); falling back to apidojo/tweet-scraper")
        items = run_actor(client, "apidojo/tweet-scraper", {
            "searchTerms": [q + " lang:en" for q in X_QUERIES], "sort": "Latest", "tweetLanguage": "en",
            "start": since, "maxItems": 300,
        }, 300, 0.5, "x-fallback")
    tweets = [norm_tweet(t) for t in items]
    return [t for t in tweets if t["text"] and t["id"]]


# ---------------------------------------------------------------- news
NEWS_KEYWORDS = [
    "San Francisco robbery", "San Francisco shooting OR stabbing", "Mission District police", "SFPD arrest",
    "BART station police San Francisco", "Tenderloin assault", "Inner Sunset crime",
]


def scrape_news(client, timeframe="7d"):
    items = run_actor(client, "data_xplorer/google-news-scraper-fast", {
        "keywords": NEWS_KEYWORDS, "maxArticles": 10, "timeframe": timeframe, "region_language": "US:en",
        "decodeUrls": True, "extractDescriptions": True, "extractImages": False,
    }, 10 * len(NEWS_KEYWORDS), 0.5, "news")
    seen, out = set(), []
    for a in items:
        url = a.get("url") or a.get("link")
        if not url or url in seen:
            continue
        seen.add(url)
        out.append({"id": url, "title": a.get("title"), "source": a.get("source"), "url": url,
                    "description": a.get("description"), "publishedAt": a.get("publishedAt") or a.get("date"),
                    "keyword": (a.get("metadata") or {}).get("keyword")})
    if len(out) < 15 and timeframe == "7d":
        print(f"  only {len(out)} articles in 7d; retrying with 30d")
        return scrape_news(client, "30d")
    return out


# ---------------------------------------------------------------- DataSF (SFPD incident reports)
DATASF_URL = "https://data.sf.gov/resource/wg3w-h783.json"  # "Police Department Incident Reports: 2018 to Present"


def scrape_datasf():
    since = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%dT00:00:00")
    params = {
        "$select": "incident_id,incident_datetime,incident_category,incident_subcategory,incident_description,"
                   "latitude,longitude,intersection",
        "$where": (f"incident_datetime > '{since}' AND latitude between {BBOX['south']} and {BBOX['north']}"
                   f" AND longitude between {BBOX['west']} and {BBOX['east']}"),
        "$limit": 5000, "$order": "incident_datetime DESC",
    }
    print(f"[datasf] SFPD incident reports since {since[:10]} in corridor bbox")
    try:
        r = httpx.get(DATASF_URL, params=params, timeout=40, follow_redirects=True)
        r.raise_for_status()
        return r.json()
    except httpx.HTTPError:  # the system Python's LibreSSL can't talk to some hosts; curl can
        url = str(httpx.URL(DATASF_URL, params=params))
        return json.loads(subprocess.run(["curl", "-sfL", "-m", "60", url], capture_output=True, check=True).stdout)


# ---------------------------------------------------------------- Google Maps places + photos
def scrape_maps(client):
    items = run_actor(client, "compass/crawler-google-places", {
        "searchStringsArray": ["bus stop", "BART station", "cafe", "convenience store", "bar"],
        "customGeolocation": CORRIDOR,
        "maxCrawledPlacesPerSearch": 60,
        "language": "en",
        "maxImages": 3,
        "maxReviews": 0,
        "scrapePlaceDetailPage": False,
        "scrapeContacts": False,
    }, 300, 2.5, "maps")
    keep = ("placeId", "title", "location", "categoryName", "categories", "openingHours", "imageUrl", "imageUrls",
            "address", "url", "totalScore")
    return [{k: p.get(k) for k in keep} for p in items]


def main():
    token = os.environ.get("APIFY_TOKEN")
    if not token:
        sys.exit("APIFY_TOKEN missing from .env")
    client = ApifyClient(token)
    only = set(sys.argv[1:])
    jobs = [("x", lambda: scrape_x(client)), ("news", lambda: scrape_news(client)),
            ("datasf", scrape_datasf), ("maps", lambda: scrape_maps(client))]
    jobs = [(n, f) for n, f in jobs if not only or n in only]

    def run(job):
        name, fn = job
        try:
            items = fn()
            save(name, items)
            if name == "maps":  # slim copy (no raw dump) that the app reads; safe to commit
                (DATA / "places.json").write_text(json.dumps(items, indent=1))
            return name, len(items)
        except Exception as e:  # fallback: keep going with the other sources
            print(f"  [{name}] FAILED: {type(e).__name__}: {e}")
            return name, None
    with ThreadPoolExecutor(len(jobs)) as ex:
        results = dict(ex.map(run, jobs))
    print("summary:", results)
    if "maps" in results and results["maps"] is None:
        print("  Maps failed: stop cards will use the hand-picked photos in data/stops.json")


if __name__ == "__main__":
    main()
