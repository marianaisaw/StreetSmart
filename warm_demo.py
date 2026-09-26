"""Run the live agent on the demo trip for all three budgets (fills data/demo_cache.json) and caption every stop photo."""
import json
import os
import sys

os.environ["STREETSMART_NO_REFRESH"] = "1"
import server  # noqa: E402

for b in (3, 15, 50):
    req = server.PlanReq(budget=b)
    plan, trace = server.run_agent(req, server.DEMO)
    cache = server.jload("demo_cache.json", {})
    cache[server.cache_key(req)] = {"plan": plan, "trace": trace}
    server.jsave("demo_cache.json", cache)
    print(f"${b}: {plan['recommended_route_id']:7s} {plan.get('cost_label')}  {plan['total_minutes']} min | {plan['why']}")

if "--no-captions" not in sys.argv:
    for sid, st in server.DEMO["stops"].items():
        card = server.stop_card(st)
        print(f"{card['name']}: {server.caption_image(card['photo'], card['name']) if card.get('photo') else '(no photo)'}")

print(json.dumps(server.usage_summary(), indent=1))
