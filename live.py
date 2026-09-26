"""Live refresh via Apify's remote MCP server: Opus 5.5 runs the X scraper itself (server-side MCP tool),
then hands back new located incidents through a local strict tool. `python live.py` or POST /api/live."""
import json
import os
from pathlib import Path

from extract import SAVE_TOOL
from llm import call

DATA = Path(__file__).parent / "data"
APIFY_MCP = "https://mcp.apify.com?tools=xquik/x-tweet-scraper"

SYSTEM = """You keep a San Francisco night-navigation safety map fresh.
Use the Apify X scraper tool once (mode "search", queryType "Latest", maxItems 40, lang "en") to find posts from the
last 24 hours about incidents at or near these places: 16th St Mission BART, Mission St & 16th St, Civic Center / UN
Plaza, Church St & Duboce Ave, the N-Judah, 9th Ave & Irving St.
Then call save_incidents exactly once with only real, specific, located incidents (source "x", item_id "x:<tweet id>");
skip opinions, jokes, delays without an incident, and anything without a specific place. Pass an empty list if none.
Always finish by calling save_incidents."""


def refresh():
    known = {i.get("item_id") for i in json.loads((DATA / "incidents.json").read_text())}
    msgs = [{"role": "user", "content": "Check for new reports along tonight's route."}]
    for turn in range(4):
        resp = call(f"live:mcp:t{turn}", beta=True, betas=["mcp-client-2025-11-20"], max_tokens=16000,
                    output_config={"effort": "low"}, system=SYSTEM, messages=msgs,
                    mcp_servers=[{"type": "url", "url": APIFY_MCP, "name": "apify",
                                  "authorization_token": os.environ["APIFY_TOKEN"]}],
                    tools=[{"type": "mcp_toolset", "mcp_server_name": "apify"}, SAVE_TOOL])
        if resp.stop_reason == "refusal":
            return {"new": [], "note": "model declined"}
        mcp_calls = [b.name for b in resp.content if b.type == "mcp_tool_use"]
        got = next((b for b in resp.content if b.type == "tool_use" and b.name == "save_incidents"), None)
        if got:
            new = [i for i in got.input["incidents"]
                   if i["item_id"] not in known and i["confidence"] >= 0.5 and 37.70 < i["lat"] < 37.82]
            return {"new": new, "mcp_tools_used": mcp_calls}
        msgs.append({"role": "assistant", "content": resp.content})
        if resp.stop_reason == "pause_turn":  # long server-side tool run; let it continue
            continue
        msgs.append({"role": "user", "content": "Please finish by calling save_incidents."})
    return {"new": [], "note": "no result"}


def merge(new):
    path = DATA / "incidents.json"
    inc = json.loads(path.read_text())
    for i in new:
        i["source"] = "x"
        i["url"] = f"https://x.com/i/status/{i['item_id'].split(':', 1)[-1]}"
    path.write_text(json.dumps(inc + new, indent=1))
    return len(new)


if __name__ == "__main__":
    out = refresh()
    print(json.dumps(out, indent=1)[:2000])
