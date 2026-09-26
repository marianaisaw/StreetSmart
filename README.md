# StreetSmart

Real-time urban risk-aware routing that uses Apify X scrapers to find and recommend safer routes.

A door-to-door night navigator for San Francisco. For the demo trip (Mission & 16th St -> 9th Ave & Irving, 11pm) it shows a live safety heatmap, lets you pick a budget ($3 / $15 / $50, editable), and has Claude Opus 5.5 choose the safest mix of Muni, BART, walking and Uber that fits, with a one-sentence reason and photo-based "where to stand" tips for each stop.

## Architecture

1. **Apify scrapes X + news** (`xquik/x-tweet-scraper`, `data_xplorer/google-news-scraper-fast`), plus SFPD open data (DataSF `wg3w-h783`) and Google Maps places/photos/hours (`compass/crawler-google-places`) -> `data/raw/`.
2. **Opus extracts incidents**: keyword pre-filter, then batches of 50 at effort `low` with a strict `save_incidents` tool (DataSF rows skip the LLM) -> `data/incidents.json`.
3. **H3 scores cells, then the Opus agent plans the trip**: res-9 risk = sum(severity x confidence x source weight x e^(-h/72)), x1.5 at night / (1 + 0.1 x places open late); one Opus call writes each risky cell's reason. The agent loops over `get_routes`, `score_route`, `get_fares`, `estimate_rideshare`, `submit_plan`.

## Run

```bash
git clone https://github.com/marianaisaw/StreetSmart && cd StreetSmart
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env   # then paste your ANTHROPIC_API_KEY and APIFY_TOKEN into .env
```

The repo ships with the demo dataset (`data/*.json`), so the app runs straight away:

```bash
.venv/bin/uvicorn server:app --port 8765
```

To rebuild the data from scratch (about $2 Apify + $0.60 Opus):

```bash
.venv/bin/python scrape.py && .venv/bin/python extract.py && .venv/bin/python score.py
.venv/bin/python warm_demo.py   # caches the 3 demo plans + stop captions
```

Open http://localhost:8765 (live agent) or http://localhost:8765/?demo=1 (cached plans, instant). `python build_routes.py` rebuilds the candidate route geometry (OSRM); `python llm.py` prints total tokens and $ spent from `data/usage.log`.

## Settings

The gear button opens an iOS-style settings sheet, saved in the browser: trip times, budget buttons, safety-vs-speed priority, max walking, allowed modes, rideshare surge, scoring (recency, night multiplier, band cutoffs, trust in SFPD / news / X), map look (dark, light or auto, accent colour, heatmap strength), auto-captions, Walk-with-me name and ETA, agent effort, demo mode and agent trace. Scoring changes re-score cells on the server without calling Opus.

## Keys and secrets

- Keys live only in `.env` (git-ignored, `chmod 600`) and are loaded with python-dotenv. `.env.example` shows the format.
- `data/raw/` (raw scraped tweets and the full Maps dump) is git-ignored; the app reads the slim `data/places.json` instead.

## Notes

- Fares are current Clipper adult fares: Muni $2.85 (sfmta.com), BART 16th St -> Civic Center $2.55 (bart.gov fare API), and a free Muni transfer from BART within 2h via the $2.85 inter-agency discount. Rideshare prices are a labelled formula estimate, not a quote.
- Cells score incidents and conditions (reported events, places open late), never whole neighborhoods, and every cell shows its reason.
- The model is `claude-opus-5-5`. Thinking is always on, depth is set by `output_config.effort`, and tools use `strict: true` with `tool_choice: auto` (forced tool choice is not supported).

## Demo numbers (this dataset)

- Scraped: 75 tweets, 45 news articles, 687 SFPD reports (last 7 days), 258 places. Apify cost: $2.06.
- 101 of 120 posts/articles passed the keyword filter; Opus kept 8 located incidents; DataSF added 468.
- 114 cells: 17 unsafe, 17 kind of safe. Opus total so far is about $0.58 (see `python llm.py`), including three plans at about $0.07 each (8 tool calls, cached system prompt and tools).
