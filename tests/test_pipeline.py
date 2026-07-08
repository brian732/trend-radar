#!/usr/bin/env python3
"""
Offline test harness for Trend Radar. No network, no keys.
Run:  python3 tests/test_pipeline.py
"""
import copy
import inspect
import json
import os
import sys
import tempfile

os.environ["OFFLINE_FIXTURES"] = "1"
os.environ["MOCK_LLM"] = "1"
os.environ["SLACK_WEBHOOK_URL"] = "https://hooks.slack.com/services/TEST/OFFLINE"
os.environ.pop("X_BEARER_TOKEN", None)
os.environ.pop("ANTHROPIC_API_KEY", None)

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import pipeline  # noqa: E402

# isolate outputs in a temp dir
TMP = tempfile.mkdtemp(prefix="trendradar_test_")
pipeline.DATA_PATH = os.path.join(TMP, "data.json")
pipeline.HISTORY_PATH = os.path.join(TMP, "history.json")

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not cond else ""))


def all_trends(data):
    return [t for s in data["sections"] for t in s["trends"]]


def find(data, key):
    return next((t for t in all_trends(data) if t["key"] == key), None)


print("== run 1 ==")
data, history, stats = pipeline.run_pipeline()
trends = all_trends(data)
keys = {t["key"] for t in trends}

# ---- schema validation ----
req_top = {"schema_version", "generated_at", "sections", "source_status", "x_budget", "config"}
check("data.json has required top-level keys", req_top <= set(data))
req_trend = {"key", "name", "entity", "league", "score", "score_breakdown", "early",
             "new_on_x", "why_now", "angle", "sources", "first_seen",
             "example_tweets", "market", "market_gap"}
check("every trend has required fields",
      all(req_trend <= set(t) for t in trends), str(trends and (req_trend - set(trends[0]))))
check("every league is a valid bucket",
      all(t["league"] in pipeline.SPORT_ORDER for t in trends))
check("scores within 0-100", all(0 <= t["score"] <= 100 for t in trends))
check("breakdown sums to score (pre-cap)",
      all(abs(min(100, round(sum(t["score_breakdown"].values()))) - t["score"]) < 1 for t in trends))

# ---- (a) true noise discarded ----
check("(a) #MondayMotivation discarded as noise", "mondaymotivation" not in keys)

# ---- (b) niche sports trend survives ----
possum = find(data, "sacramento river cats rally possum")
check("(b) niche minor-league trend survives", possum is not None)
check("(b) niche trend flagged early w/ badge bonus",
      bool(possum and possum["early"] and possum["score_breakdown"]["early_bonus"] == 10))

# ---- (c) non-sports trend discarded even with news value ----
check("(c) Supreme Court ruling discarded (non-sports)", "supreme court ruling" not in keys)

# ---- (obviousness) MNF suppressed, moment-inside survives ----
check("(h) 'Monday Night Football' suppressed as obvious", "monday night football" not in keys)
check("(h) suppression logged for audit",
      any(s["key"] == "monday night football" for s in history["suppressed"]))
butker = find(data, "butker fake injury celebration")
check("(h) specific moment INSIDE the event survives", butker is not None)

# ---- (d) fixture trend matches expected market ----
chiefs = find(data, "chiefs vs bills")
check("(d) Chiefs vs Bills matched to its market",
      bool(chiefs and chiefs["market"] and "Chiefs beat the Bills" in chiefs["market"]["question"]))
check("(d) market url uses events[0].slug",
      bool(chiefs and chiefs["market"] and chiefs["market"]["url"].endswith("/event/nfl-kc-buf-2026-01-26")))
check("(d) prefix fuzzy-merge worked (google 'chiefs vs' merged in)",
      bool(chiefs and "google" in chiefs["sources"] and "x" in chiefs["sources"]))

# ---- (e) no market -> MARKET GAP, zero penalty ----
kicker = find(data, "third string kicker moment")
check("(e) no-market trend kept and flagged MARKET GAP",
      bool(kicker and kicker["market_gap"] and kicker["market"] is None))

# ---- (f) market presence/absence cannot change score ----
src = inspect.getsource(pipeline.compute_score)
check("(f) compute_score references no market data", "market" not in src.lower())
sig = {"new_on_x": True, "t24_rank": 3, "t24_prev_rank": 9, "google_traffic": 20000,
       "sources": ["x", "google"], "first_seen_hours_ago": 1.0,
       "engagement_now": 500, "engagement_prev": 100, "early": True}
s1, _ = pipeline.compute_score(dict(sig))
s2, _ = pipeline.compute_score(dict(sig))  # identical trend, market irrelevant by construction
check("(f) identical signals -> identical score", s1 == s2)
if chiefs and kicker:
    t = copy.deepcopy(kicker)
    before = (t["score"], t["score_breakdown"], t["league"], t["early"])
    pipeline.attach_market(t, {"market_id": "900001"},
                           {"900001": {"id": "900001", "question": "Will the Chiefs beat the Bills on January 26?",
                                       "slug": "chiefs-bills-2026", "event_slug": "nfl-kc-buf-2026-01-26",
                                       "outcomes": ["Yes", "No"], "prices": [0.55, 0.45], "volume24hr": 1.0}}, [])
    after = (t["score"], t["score_breakdown"], t["league"], t["early"])
    check("(f) attach_market mutates nothing but the market fields", before == after)

# ---- section ordering sanity ----
if pipeline.SECTION_ORDER_MODE == "activity":
    acts = [s["activity"] for s in data["sections"]]
    check("sections ordered by activity desc", acts == sorted(acts, reverse=True))

# ---- example tweets attached ----
check("example tweets attached with engagement counts",
      any(t["example_tweets"] and "likes" in t["example_tweets"][0] for t in trends))

# ---- Slack digest layer ----
check("(s) slack: offline post recorded in status",
      data["source_status"].get("slack", "").startswith("ok (offline"),
      data["source_status"].get("slack", ""))
check("(s) slack: niche picks include the early minor-league trend",
      "sacramento river cats rally possum" in history.get("slack_posted", {}))
saturated = {"key": "saturated thing", "entity": "Saturated", "league": "NFL",
             "score": 95, "early": False, "new_on_x": False, "why_now": "w",
             "angle": "a", "example_tweets": [], "market": None,
             "market_gap": True, "score_breakdown": {}, "sources": ["x"],
             "first_seen": data["generated_at"]}
check("(s) slack: saturated mainstream trend excluded in niche mode",
      pipeline.select_slack_trends([saturated], {"slack_posted": {}}) == [])
check("(s) slack: empty picks -> no payload (stays quiet)",
      pipeline.build_slack_payload([], data["generated_at"]) is None)
pl = pipeline.build_slack_payload([dict(saturated, early=True)], data["generated_at"])
check("(s) slack: payload is valid Block Kit with gap line",
      bool(pl and pl["blocks"][0]["type"] == "header"
           and "MARKET GAP" in json.dumps(pl)))

# ---- (g) cache stability: second run enriches zero ----
print("== run 2 ==")
data2, history2, stats2 = pipeline.run_pipeline()
check("(g) second run enriches zero new candidates", stats2["newly_enriched"] == 0,
      f"newly_enriched={stats2['newly_enriched']}")
check("(g) second run output still valid", len(all_trends(data2)) == len(trends))
check("(s) slack: second run posts nothing (dedupe holds)",
      "nothing new" in data2["source_status"].get("slack", ""),
      data2["source_status"].get("slack", ""))

# ---- X budget accounting ----
check("x budget tracked per UTC day", data2["x_budget"]["used_today"] > 0
      and data2["x_budget"]["cap"] == pipeline.X_DAILY_TWEET_BUDGET)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED:", *FAIL, sep="\n  - ")
    sys.exit(1)
print("ALL TESTS PASSED")
