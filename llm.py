"""Shared Opus 5.5 client + usage logging (every call appends a line to data/usage.log)."""
import json
import os
import threading
import time
from pathlib import Path

import anthropic
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env", override=True)
MODEL = "claude-opus-5-5"
DATA = Path(__file__).parent / "data"
DATA.mkdir(exist_ok=True)
USAGE_LOG = DATA / "usage.log"
# $ per million tokens (Opus 5.5)
PRICE = {"input": 4.0, "output": 20.0, "cache_read": 0.20, "cache_write": 5.0}

# The desktop shell may export ANTHROPIC_BASE_URL for its own proxy; talk to the real API.
client = anthropic.Anthropic(
    api_key=os.environ.get("ANTHROPIC_API_KEY"),
    base_url=os.environ.get("STREETSMART_API_BASE", "https://api.anthropic.com"),
    max_retries=3,
)
_lock = threading.Lock()


def _log(tag, resp, secs):
    u = resp.usage
    inp = u.input_tokens or 0
    out = u.output_tokens or 0
    cr = getattr(u, "cache_read_input_tokens", 0) or 0
    cw = getattr(u, "cache_creation_input_tokens", 0) or 0
    cost = (inp * PRICE["input"] + out * PRICE["output"] + cr * PRICE["cache_read"] + cw * PRICE["cache_write"]) / 1e6
    row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "tag": tag, "model": MODEL, "input": inp, "output": out,
           "cache_read": cr, "cache_write": cw, "usd": round(cost, 5), "secs": round(secs, 1),
           "stop": resp.stop_reason}
    with _lock, USAGE_LOG.open("a") as f:
        f.write(json.dumps(row) + "\n")
    return row


def call(tag, beta=False, **kw):
    """messages.create with model + usage logging. Caller checks stop_reason (incl. 'refusal')."""
    kw.setdefault("model", MODEL)
    t = time.time()
    api = client.beta.messages if beta else client.messages
    resp = api.create(**kw)
    _log(tag, resp, time.time() - t)
    return resp


def tool_input(resp, name):
    """First tool_use block with this name (select by .type, never by index)."""
    for b in resp.content:
        if b.type == "tool_use" and b.name == name:
            return b.input
    return None


def text_of(resp):
    return "".join(b.text for b in resp.content if b.type == "text").strip()


def usage_summary():
    rows = [json.loads(l) for l in USAGE_LOG.read_text().splitlines() if l.strip()] if USAGE_LOG.exists() else []
    opus = [r for r in rows if r["model"] != "apify"]
    apify = [r for r in rows if r["model"] == "apify"]
    tot = {k: sum(r[k] for r in opus) for k in ("input", "output", "cache_read", "cache_write")}
    by = {}
    for r in rows:
        t = r["tag"].split(":")[0]
        by.setdefault(t, {"calls": 0, "usd": 0.0})
        by[t]["calls"] += 1
        by[t]["usd"] = round(by[t]["usd"] + r["usd"], 5)
    opus_usd, apify_usd = sum(r["usd"] for r in opus), sum(r["usd"] for r in apify)
    return {"calls": len(opus), **tot, "opus_usd": round(opus_usd, 4), "apify_runs": len(apify),
            "apify_usd": round(apify_usd, 4), "usd": round(opus_usd + apify_usd, 4), "by_tag": by}


if __name__ == "__main__":
    print(json.dumps(usage_summary(), indent=2))
