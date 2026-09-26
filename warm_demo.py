"""Run the live agent for all three budgets (fills data/demo_cache.json) and caption every stop photo."""
import json
import sys

import server

for b in (3, 15, 50):
    req = server.PlanReq(budget=b)
    plan, trace = server.run_agent(req)
    cache = server.jload("demo_cache.json", {})
    cache[server.cache_key(req)] = {"plan": plan, "trace": trace}
    server.jsave("demo_cache.json", cache)
    print(f"${b}: {plan['recommended_route_id']:7s} {plan.get('cost_label')}  {plan['total_minutes']} min | {plan['why']}")

if "--no-captions" not in sys.argv:
    for sid in server.ROUTES["stops"]:
        card = server.stop_card(sid)
        if card.get("photo"):
            print(f"{card['name']}: {server.caption_image(card['photo'], card['name'])}")
        else:
            print(f"{card['name']}: (no photo)")

print(json.dumps(server.usage_summary(), indent=1))
