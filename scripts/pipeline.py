#!/usr/bin/env python3
"""
Trend Radar — sports-only trend monitoring pipeline for Polymarket.

Runs 4x/day via GitHub Actions. Pulls trends from X (v2 API), trends24.in
(free fallback + velocity), and Google Trends RSS; gates to sports-only via
one Claude (Haiku) call per cycle; suppresses obvious/saturated trends;
scores 0-100 with ZERO market input; then — and only then — matches
Polymarket markets as an optional, informational layer.

HARD RULE (audited): market presence/absence must never influence trend
selection, sub-league tagging, obviousness, early-signal, or score.
compute_score() takes no market data by design.
"""

import json
import os
import re
import sys
import time
import html as html_lib
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timezone

# ============================================================================
# CONFIG — everything you might want to change lives here
# ============================================================================

# Popularity ordering. Used for filter-chip order and as the tiebreaker for
# activity-aware section ordering. Reorder seasonally by editing this list.
SPORT_ORDER = [
    "NFL", "NBA", "MLB", "College Football", "Soccer", "NHL",
    "College Basketball", "MMA/Boxing", "Golf", "Tennis",
    "Motorsports", "Olympics", "Other Sports",
]

# Section ordering mode: "activity" = most-trending sport first each cycle
# (SPORT_ORDER breaks ties). "fixed" = always exactly SPORT_ORDER.
SECTION_ORDER_MODE = "activity"

TOP_N_PER_SECTION = int(os.environ.get("TREND_RADAR_TOP_N", "8"))

# --- X API budget guardrails (Pro tier: flat fee, 2,000,000 posts/month) ---
X_DAILY_TWEET_BUDGET = 15000        # posts pulled per UTC day, hard ceiling
MAX_DETAIL_QUERIES_PER_CYCLE = 25   # search/recent calls per cycle, hard cap
TWEETS_PER_DETAIL_QUERY = 50        # max_results per search call
EXAMPLE_TWEETS_PER_TREND = 3        # links shown on each dashboard card

WOEID_US = 23424977
MAX_CANDIDATES_TO_LLM = 80
ENRICH_TTL_HOURS = 6
HISTORY_RETENTION_DAYS = 7
GAMMA_PAGES = 5                     # x100 = ~500 markets in the catalog
LLM_MODEL = "claude-haiku-4-5-20251001"
LLM_TEMPERATURE = 0.2
LLM_MAX_TOKENS = 8000

# --- Slack digest (posted after each cycle if SLACK_WEBHOOK_URL is set) ---
# Mode "niche": only under-the-radar picks + fresh algorithmic movers —
# early=true trends, or brand-new-on-X trends with real velocity. Saturated
# mainstream trends stay on the dashboard and are NOT posted.
# Other modes: "all" (every kept trend), "top" (top N by score).
SLACK_INCLUDE_MODE = "all"
SLACK_MAX_TRENDS_PER_POST = 10
SLACK_REPOST_SCORE_JUMP = 15   # re-post an already-posted trend only if its
                               # score climbed by at least this much
SLACK_NEW_MOVER_MIN_SCORE = 40  # new-on-X trends need at least this score

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_PATH = os.path.join(REPO_ROOT, "data", "data.json")
HISTORY_PATH = os.path.join(REPO_ROOT, "data", "history.json")
FIXTURES_DIR = os.path.join(REPO_ROOT, "tests", "fixtures")

OFFLINE = os.environ.get("OFFLINE_FIXTURES") == "1"
MOCK_LLM = os.environ.get("MOCK_LLM") == "1"
X_BEARER = os.environ.get("X_BEARER_TOKEN", "").strip()
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# ============================================================================
# Keyword fallback classifier (used only if the LLM call fails/missing key).
# False negatives are worse than wrong sub-league tags: anything plausibly
# sports lands in a bucket ("Other Sports" if nothing more specific matches).
# ============================================================================

LEAGUE_KEYWORDS = {
    "NFL": ["nfl", "touchdown", "quarterback", " qb ", "super bowl", "chiefs",
            "bills", "eagles", "cowboys", "49ers", "niners", "ravens", "lions",
            "packers", "bengals", "jets", "giants", "steelers", "dolphins",
            "broncos", "chargers", "texans", "vikings", "seahawks", "rams",
            "buccaneers", "falcons", "saints", "commanders", "browns",
            "colts", "jaguars", "titans", "raiders", "cardinals nfl",
            "panthers", "patriots", "mahomes", "josh allen", "lamar jackson",
            "aaron rodgers", "training camp", "goodell", "kelce"],
    "NBA": ["nba", "lakers", "celtics", "warriors", "knicks", "nets", "bulls",
            "heat", "bucks", "nuggets", "suns", "mavericks", "mavs", "clippers",
            "sixers", "76ers", "thunder", "timberwolves", "wolves nba", "spurs",
            "grizzlies", "kings nba", "pelicans", "hawks", "raptors", "pacers",
            "cavaliers", "cavs", "pistons", "magic", "wizards", "hornets",
            "jazz", "rockets", "trail blazers", "lebron", "steph curry",
            "wembanyama", "giannis", "luka", "jokic", "durant", "wnba",
            "caitlin clark", "angel reese", "summer league"],
    "MLB": ["mlb", "yankees", "dodgers", "mets", "red sox", "cubs", "braves",
            "astros", "phillies", "padres", "orioles", "rangers mlb",
            "blue jays", "guardians", "mariners", "twins", "rays", "royals",
            "tigers", "diamondbacks", "giants mlb", "brewers", "cardinals",
            "reds", "pirates", "rockies", "marlins", "nationals", "white sox",
            "athletics", "angels", "ohtani", "judge", "home run", "walk-off",
            "walkoff", "no-hitter", "grand slam mlb", "all-star game"],
    "NHL": ["nhl", "stanley cup", "maple leafs", "bruins", "rangers nhl",
            "oilers", "mcdavid", "penguins", "crosby", "canadiens", "habs",
            "blackhawks", "bedard", "avalanche", "lightning", "panthers nhl",
            "golden knights", "canucks", "flames", "kraken", "devils",
            "islanders", "capitals", "ovechkin", "hat trick", "hurricanes nhl"],
    "Soccer": ["soccer", "football club", "premier league", "champions league",
               "world cup", "fifa", "uefa", "messi", "ronaldo", "mbappe",
               "haaland", "bellingham", "lamine", "yamal", "vinicius",
               "real madrid", "barcelona", "barca", "man city", "man united",
               "manchester", "arsenal", "chelsea", "liverpool", "tottenham",
               "spurs fc", "psg", "bayern", "juventus", "inter milan",
               "ac milan", "atletico", "usmnt", "uswnt", "mls", "la liga",
               "serie a", "bundesliga", "el clasico", "var ", "penalty kick",
               "own goal", "hat-trick", "copa", "concacaf", "golazo",
               "de bruyne", "lukaku", "griezmann", "kane", "salah", "pulisic"],
    "College Football": ["college football", "cfb", "ncaa football",
                         "heisman", "sec football", "big ten", "big 12",
                         "playoff cfp", "alabama football", "georgia bulldogs",
                         "ohio state", "michigan football", "texas longhorns",
                         "notre dame", "clemson", "oregon ducks", "usc football",
                         "lsu", "tailgate", "saban", "transfer portal"],
    "College Basketball": ["college basketball", "march madness", "final four",
                           "ncaa tournament", "cbb", "duke basketball",
                           "kentucky basketball", "kansas jayhawks", "uconn",
                           "gonzaga", "bracket", "cinderella", "sweet 16",
                           "elite eight", "buzzer beater ncaa"],
    "Golf": ["golf", "pga", "liv golf", "masters", "augusta", "ryder cup",
             "us open golf", "the open", "scheffler", "rory", "mcilroy",
             "tiger woods", "birdie", "eagle putt", "hole in one", "caddie"],
    "Tennis": ["tennis", "wimbledon", "us open tennis", "french open",
               "roland garros", "australian open", "atp", "wta", "djokovic",
               "alcaraz", "sinner", "gauff", "coco gauff", "sabalenka",
               "osaka", "swiatek", "rybakina", "medvedev", "zverev",
               "grand slam tennis", "tiebreak", "match point"],
    "MMA/Boxing": ["ufc", "mma", "boxing", "knockout", " ko ", "tko",
                   "heavyweight", "dana white", "jon jones", "mcgregor",
                   "canelo", "fury", "usyk", "joshua", "title fight",
                   "weigh-in", "octagon", "pay-per-view fight", "jake paul"],
    "Motorsports": ["f1", "formula 1", "formula one", "nascar", "indycar",
                    "verstappen", "hamilton", "leclerc", "norris", "grand prix",
                    "pole position", "pit stop", "daytona", "le mans",
                    "monaco gp", "red bull racing", "ferrari f1", "mclaren"],
    "Olympics": ["olympics", "olympic", "team usa", "gold medal",
                 "medal count", "ioc", "paralympic", "simone biles",
                 "katie ledecky", "opening ceremony"],
    "Other Sports": ["espn", "sportscenter", "athlete", "championship",
                     "playoffs", "coach", "referee", "umpire", "draft pick",
                     "trade deadline", "hall of fame", "rookie", "mvp",
                     "cricket", "rugby", "volleyball", "track and field",
                     "marathon", "surfing", "skateboarding", "lacrosse",
                     "pickleball", "darts", "snooker", "chess boxing",
                     "minor league", "aaa ", "g league", "practice squad"],
}

# Fallback-only obviousness patterns (LLM handles this normally).
OBVIOUS_PATTERNS = [
    r"^monday night football$", r"^sunday night football$",
    r"^thursday night football$", r"^#?mnf$", r"^#?snf$", r"^#?tnf$",
    r"^nfl sunday$", r"game today$", r"games today$", r"^#?gameday$",
    r"score$", r"schedule$", r"how to watch", r"live stream", r"start time$",
]

# True-noise patterns (both paths): zero-information filler.
NOISE_PATTERNS = [
    r"^#?monday ?motivation$", r"^#?tuesday ?thoughts$",
    r"^#?wednesday ?wisdom$", r"^#?throwback ?thursday$", r"^#?tbt$",
    r"^#?friday ?feeling$", r"^#?fri ?yay$", r"^#?saturday ?vibes$",
    r"^#?sunday ?funday$", r"^good ?morning$", r"^good ?night$",
    r"^happy ?(monday|tuesday|wednesday|thursday|friday|saturday|sunday)$",
    r"^#?ad$", r"^#?sponsored$",
]

# ============================================================================
# Small utilities
# ============================================================================

def log(msg):
    print(f"[trend-radar] {msg}", flush=True)


def now_utc():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def normalize_key(name):
    """lowercase, strip #/$ and punctuation, collapse whitespace."""
    s = name.lower().strip()
    s = re.sub(r"[#$@]", "", s)
    s = re.sub(r"[^\w\s]", " ", s, flags=re.UNICODE)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def http_get(url, headers=None, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def read_fixture(name):
    with open(os.path.join(FIXTURES_DIR, name), encoding="utf-8") as f:
        return f.read()


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)


def matches_any(patterns, key):
    return any(re.search(p, key) for p in patterns)


# ============================================================================
# Source status tracker
# ============================================================================

SOURCE_STATUS = {"x_api": "not attempted", "trends24": "not attempted",
                 "google_trends": "not attempted", "gamma_api": "not attempted",
                 "llm": "not attempted", "slack": "not attempted"}


# ============================================================================
# Ingestion: X API v2
# ============================================================================

def x_get(path, params):
    qs = urllib.parse.urlencode(params)
    url = f"https://api.x.com/2/{path}?{qs}"
    return json.loads(http_get(url, headers={"Authorization": f"Bearer {X_BEARER}"}))


def fetch_x_trends():
    """Ranked US trend names from v2 /trends/by/woeid. Returns list of names
    (rank = position) or None on failure. NOTE: the old v1.1 trends/place
    endpoint 403s on this tier — do not reintroduce it."""
    if OFFLINE:
        data = json.loads(read_fixture("x_trends.json"))
        SOURCE_STATUS["x_api"] = "ok (fixture)"
        return _dedupe([t["trend_name"] for t in data.get("data", [])])
    if not X_BEARER:
        SOURCE_STATUS["x_api"] = "no token — using trends24 fallback"
        return None
    try:
        data = x_get(f"trends/by/woeid/{WOEID_US}", {"max_trends": 50})
        names = _dedupe([t.get("trend_name", "") for t in data.get("data", []) if t.get("trend_name")])
        SOURCE_STATUS["x_api"] = f"ok ({len(names)} trends)"
        return names
    except Exception as e:
        SOURCE_STATUS["x_api"] = f"error: {str(e)[:80]} — using trends24 fallback"
        log(f"X trends failed: {e}")
        return None


def _dedupe(seq):
    seen, out = set(), []
    for s in seq:
        k = normalize_key(s)
        if k and k not in seen:
            seen.add(k)
            out.append(s)
    return out


def fetch_x_tweets_for(term, budget_state):
    """Top engagement tweets for a term via search/recent sort_order=relevancy.
    Respects the daily tweet budget. Returns list of tweet dicts or []."""
    today = now_utc().strftime("%Y-%m-%d")
    used = budget_state.setdefault(today, 0)
    if used + TWEETS_PER_DETAIL_QUERY > X_DAILY_TWEET_BUDGET:
        return []
    if OFFLINE:
        try:
            data = json.loads(read_fixture(f"x_search_{normalize_key(term).replace(' ', '_')}.json"))
        except OSError:
            data = json.loads(read_fixture("x_search_default.json"))
    else:
        if not X_BEARER:
            return []
        try:
            q = f'"{term}" -is:retweet -is:reply lang:en'
            data = x_get("tweets/search/recent", {
                "query": q, "max_results": TWEETS_PER_DETAIL_QUERY,
                "sort_order": "relevancy",
                "tweet.fields": "public_metrics,created_at,lang",
                "expansions": "author_id", "user.fields": "username",
            })
        except Exception as e:
            log(f"search/recent failed for '{term}': {e}")
            return []
    users = {u["id"]: u.get("username", "") for u in data.get("includes", {}).get("users", [])}
    tweets = []
    for t in data.get("data", []):
        pm = t.get("public_metrics", {})
        tweets.append({
            "id": t["id"],
            "text": t.get("text", "")[:280],
            "likes": pm.get("like_count", 0),
            "retweets": pm.get("retweet_count", 0),
            "username": users.get(t.get("author_id", ""), ""),
            "url": f"https://x.com/{users.get(t.get('author_id',''), 'i')}/status/{t['id']}",
        })
    budget_state[today] = used + len(tweets)
    tweets.sort(key=lambda t: t["likes"] + 2 * t["retweets"], reverse=True)
    return tweets


# ============================================================================
# Ingestion: trends24.in (free X-trend mirror; fallback + velocity source)
# ToS-gray third-party scraping — flagged in the dashboard footer; fail-soft.
# Verified live 2026-07: attributes are UNQUOTED (class=title,
# data-timestamp=1783373309.811 as float). Parser must not require quotes.
# ============================================================================

def fetch_trends24():
    """Returns list of snapshots, newest first:
    [{"ts": epoch_int, "trends": [name, ...rank order]}]. [] on failure."""
    try:
        raw = read_fixture("trends24.html") if OFFLINE else http_get(
            "https://trends24.in/united-states/")
    except Exception as e:
        SOURCE_STATUS["trends24"] = f"error: {str(e)[:80]}"
        log(f"trends24 fetch failed: {e}")
        return []
    snapshots = []
    # Quote-agnostic: match each h3 title w/ data-timestamp then its <ol> block
    pat = re.compile(
        r'<h3[^>]*\bdata-timestamp=["\']?([\d.]+)["\']?[^>]*>.*?'
        r'<ol[^>]*\btrend-card__list[^>]*>(.*?)</ol>',
        re.S)
    link_pat = re.compile(
        r'<a[^>]*href=["\']?https?://(?:twitter|x)\.com/search\?q=([^"\'\s&>]+)[^>]*>',
        re.S)
    for m in pat.finditer(raw):
        ts = int(float(m.group(1)))
        names = []
        for lm in link_pat.finditer(m.group(2)):
            name = html_lib.unescape(urllib.parse.unquote(lm.group(1)))
            if name:
                names.append(name)
        if names:
            snapshots.append({"ts": ts, "trends": names})
    snapshots.sort(key=lambda s: s["ts"], reverse=True)
    snapshots = snapshots[:2]  # 2 most recent → rank-over-time velocity
    SOURCE_STATUS["trends24"] = (
        f"ok ({len(snapshots)} snapshots)" if snapshots else "parsed 0 snapshots — markup may have drifted")
    return snapshots


# ============================================================================
# Ingestion: Google Trends RSS
# ============================================================================

def fetch_google_trends():
    """Returns [{"name", "traffic": int, "news": [titles], "pub": iso|None}].
    The old /trends/api/dailytrends JSON endpoint is dead — RSS only."""
    try:
        raw = read_fixture("google_trends.xml") if OFFLINE else http_get(
            "https://trends.google.com/trending/rss?geo=US")
    except Exception as e:
        SOURCE_STATUS["google_trends"] = f"error: {str(e)[:80]}"
        log(f"Google Trends fetch failed: {e}")
        return []
    items = []
    for im in re.finditer(r"<item>(.*?)</item>", raw, re.S):
        block = im.group(1)
        title = _xml_text(block, "title")
        if not title:
            continue
        traffic_s = _xml_text(block, "ht:approx_traffic") or "0"
        traffic = int(re.sub(r"[^\d]", "", traffic_s) or 0)
        news = re.findall(r"<ht:news_item_title>(.*?)</ht:news_item_title>", block, re.S)
        news = [html_lib.unescape(re.sub(r"<[^>]+>", "", n)).strip() for n in news][:3]
        items.append({"name": html_lib.unescape(title), "traffic": traffic,
                      "news": news, "pub": _xml_text(block, "pubDate")})
    SOURCE_STATUS["google_trends"] = f"ok ({len(items)} items)" if items else "parsed 0 items"
    return items


def _xml_text(block, tag):
    m = re.search(rf"<{tag}>(.*?)</{tag}>", block, re.S)
    return html_lib.unescape(m.group(1).strip()) if m else None


# ============================================================================
# Ingestion: Polymarket gamma-api market catalog (public, no key)
# Used ONLY after selection+scoring, as an optional suggestion layer.
# ============================================================================

def fetch_market_catalog():
    markets = []
    if OFFLINE:
        raws = [read_fixture("gamma_markets.json")]
    else:
        raws = []
        for page in range(GAMMA_PAGES):
            url = ("https://gamma-api.polymarket.com/markets?active=true&closed=false"
                   f"&order=volume24hr&ascending=false&limit=100&offset={page * 100}")
            try:
                raws.append(http_get(url))
            except Exception as e:
                log(f"gamma page {page} failed: {e}")
                break
    for raw in raws:
        try:
            batch = json.loads(raw)
        except json.JSONDecodeError:
            continue
        for m in batch:
            try:
                outcomes = json.loads(m.get("outcomes") or "[]")
                prices = json.loads(m.get("outcomePrices") or "[]")
            except json.JSONDecodeError:
                outcomes, prices = [], []
            ev = (m.get("events") or [{}])[0]
            markets.append({
                "id": str(m.get("id")),
                "question": m.get("question", ""),
                "slug": m.get("slug", ""),
                "event_slug": ev.get("slug", ""),
                "outcomes": outcomes,
                "prices": [float(p) for p in prices[:len(outcomes)]] if prices else [],
                "volume24hr": float(m.get("volume24hr") or 0),
            })
    SOURCE_STATUS["gamma_api"] = f"ok ({len(markets)} markets)" if markets else "error: 0 markets loaded"
    return markets


# ============================================================================
# Candidate merge across sources
# ============================================================================

def build_candidates(x_trends, t24_snapshots, g_trends):
    """Merge by normalized key with prefix fuzzy-matching. Returns dict
    key -> candidate with per-source signals."""
    cands = {}

    def upsert(name, source, **sig):
        key = normalize_key(name)
        if not key or len(key) < 2:
            return
        # prefix fuzzy-match: "chiefs vs" == "chiefs vs bills"
        target = key
        for k in list(cands):
            if k == key:
                target = k
                break
            if (k.startswith(key) or key.startswith(k)) and min(len(k), len(key)) >= 5:
                target = k
                break
        c = cands.setdefault(target, {
            "key": target, "name": name, "sources": [],
            "x_rank": None, "t24_rank": None, "t24_prev_rank": None,
            "google_traffic": 0, "news": [], "tweets": [],
        })
        if len(name) > len(c["name"]):
            c["name"] = name  # keep the longest/most specific surface form
        if source not in c["sources"]:
            c["sources"].append(source)
        for k, v in sig.items():
            if k == "news":
                c["news"] = (c["news"] + v)[:3]
            elif v is not None:
                c[k] = v

    for i, name in enumerate(x_trends or []):
        upsert(name, "x", x_rank=i + 1)
    if t24_snapshots:
        cur = t24_snapshots[0]["trends"]
        prev = t24_snapshots[1]["trends"] if len(t24_snapshots) > 1 else []
        prev_ranks = {normalize_key(n): i + 1 for i, n in enumerate(prev)}
        for i, name in enumerate(cur):
            upsert(name, "trends24", t24_rank=i + 1,
                   t24_prev_rank=prev_ranks.get(normalize_key(name)))
    for g in g_trends:
        upsert(g["name"], "google", google_traffic=g["traffic"], news=g["news"])

    # cheap true-noise pre-filter (zero-information filler only)
    return {k: c for k, c in cands.items() if not matches_any(NOISE_PATTERNS, k)}


# ============================================================================
# Sports-plausibility pre-scan — allocates the tweet-detail budget only.
# NOT a gate: candidates that fail this still go to the LLM for the real call.
# ============================================================================

def sports_plausibility(cand):
    text = " ".join([cand["key"]] + cand.get("news", [])).lower()
    padded = f" {text} "
    score = 0
    for league, words in LEAGUE_KEYWORDS.items():
        for w in words:
            if w.strip() and w in padded:
                score += 3 if league != "Other Sports" else 1
    return score


def pick_detail_candidates(cands, history):
    """Choose up to MAX_DETAIL_QUERIES_PER_CYCLE candidates for tweet pulls:
    plausibly-sports first, then by prominence. Hard-capped regardless of
    remaining budget so a bug can never cause unbounded calls."""
    def prominence(c):
        r = min([x for x in [c["x_rank"], c["t24_rank"]] if x] or [99])
        return -r + (c["google_traffic"] / 5000.0)
    ranked = sorted(cands.values(),
                    key=lambda c: (sports_plausibility(c) > 0, prominence(c)),
                    reverse=True)
    # Cached-and-fresh trends don't need re-pulls unless they gained a source
    out = []
    for c in ranked:
        if len(out) >= MAX_DETAIL_QUERIES_PER_CYCLE:
            break
        if sports_plausibility(c) <= 0 and len(out) >= MAX_DETAIL_QUERIES_PER_CYCLE // 2:
            continue
        out.append(c)
    return out


# ============================================================================
# LLM enrichment — ONE call per cycle (Haiku), strict JSON out
# ============================================================================

LLM_SYSTEM = """You are the classification engine inside Trend Radar, a sports trend monitor for a marketing team. You receive candidate trends with signals, and a Polymarket market catalog. You output ONLY a JSON array, no prose.

INDEPENDENCE RULE (highest priority): Market catalog matches must never influence whether something counts as trending, whether it is sports-related, its sub-league tag, its early-signal flag, its obviousness call, or anything about its priority. Decide all of those from the trend signals alone. Market matching is a separate, independent, optional FINAL step — do it last, and never let it feed back into any other field.

For each candidate, output an object with EXACTLY these fields:
- "key": the candidate key, copied verbatim
- "sports": boolean. true if sports OR sports-adjacent (memes, athlete personal drama, coach quotes, mascots, WAGs, broadcaster moments, betting culture, anything clearly riding a sports moment). Be generous: niche sports, minor leagues, third-string players, foreign leagues all count. false ONLY for content with zero sports connection.
- "league": one of NFL, NBA, MLB, NHL, Soccer, College Football, College Basketball, Golf, Tennis, MMA/Boxing, Motorsports, Olympics, Other Sports. (null if sports=false)
- "obvious": boolean. true = suppress as too-obvious/saturated: the mere existence of a scheduled event ("Monday Night Football" trending on Monday, "NFL Sunday", "X vs Y" pre-game with no specific storyline, "game today" searches, how-to-watch queries) or a plain final-score result with no angle. false = keep: there is a specific moment, quote, meme, beef, stat anomaly, injury, controversy, or culture-crossover story INSIDE the trend. When a big event has a specific viral moment, the moment is NOT obvious even if the event is.
- "early": boolean. true if this is a pre-mainstream signal most people have not picked up on yet (low ranks but climbing, single-platform, niche entity, no major-outlet news coverage). false if already saturated mainstream coverage.
- "entity": the exact specific entity at the center (person/team/moment name), max 60 chars.
- "why_now": ONE sentence, max 160 chars, naming the exact below-the-surface driver: the quote, the clip, the joke, the stat, the beef. NOT the scoreboard result everyone saw. Use the co-trending list and sample tweets to infer the driving event — clusters of related terms reveal it. If the driver is genuinely unclear, say what is observable, e.g. "Spiking on X alongside [terms]; driver unconfirmed". NEVER fabricate.
- "angle": ONE actionable sentence for the marketing team: the content hook. If early=true, say how to ride it before it peaks. Do not mention markets here.
- "duplicate_of": key of another candidate driven by the same underlying moment (use the most canonical one), else null.
- "market_id": AFTER deciding everything above — the id of a catalog market that directly relates to this trend, else null. Never force a match. A missing market is fine and must not change any other field.

Prioritize meme/culture depth: the joke itself, the personalities, the quotes — not game logistics. Discard-worthy (sports=false) should be RARE and reserved for genuinely non-sports noise."""


def llm_enrich(candidates, market_catalog, x_available):
    """One API call. Returns {key: enrichment} or None on failure."""
    if MOCK_LLM:
        SOURCE_STATUS["llm"] = "ok (mock)"
        return mock_llm(candidates)
    if not ANTHROPIC_KEY:
        SOURCE_STATUS["llm"] = "no ANTHROPIC_API_KEY — keyword fallback"
        return None
    co_trending = [c["name"] for c in candidates]
    payload_cands = []
    for c in candidates:
        payload_cands.append({
            "key": c["key"], "name": c["name"], "sources": c["sources"],
            "x_rank": c["x_rank"], "t24_rank": c["t24_rank"],
            "t24_prev_rank": c["t24_prev_rank"],
            "google_traffic": c["google_traffic"], "news_titles": c["news"],
            "sample_tweets": [
                {"text": t["text"][:200], "likes": t["likes"], "rts": t["retweets"]}
                for t in c["tweets"][:5]],
        })
    catalog = [{"id": m["id"], "q": m["question"]} for m in market_catalog]
    user_msg = json.dumps({
        "now_utc": iso(now_utc()),
        "x_api_available": x_available,
        "co_trending_full_list": co_trending,
        "candidates": payload_cands,
        "market_catalog": catalog,
    }, ensure_ascii=False)
    body = json.dumps({
        "model": LLM_MODEL, "max_tokens": LLM_MAX_TOKENS,
        "temperature": LLM_TEMPERATURE,
        "system": LLM_SYSTEM,
        "messages": [{"role": "user", "content": user_msg}],
    }).encode()
    try:
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages", data=body,
            headers={"x-api-key": ANTHROPIC_KEY,
                     "anthropic-version": "2023-06-01",
                     "content-type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as resp:
            out = json.loads(resp.read().decode())
        text = "".join(b.get("text", "") for b in out.get("content", []))
        arr = parse_llm_json(text)
        if arr is None:
            SOURCE_STATUS["llm"] = "bad JSON — keyword fallback"
            return None
        SOURCE_STATUS["llm"] = f"ok ({len(arr)} enriched)"
        return {o["key"]: o for o in arr if isinstance(o, dict) and "key" in o}
    except Exception as e:
        SOURCE_STATUS["llm"] = f"error: {str(e)[:80]} — keyword fallback"
        log(f"LLM call failed: {e}")
        return None


def parse_llm_json(text):
    """Defensive: strip fences, slice first '[' to last ']'."""
    text = re.sub(r"```(?:json)?", "", text)
    i, j = text.find("["), text.rfind("]")
    if i == -1 or j <= i:
        return None
    try:
        arr = json.loads(text[i:j + 1])
        return arr if isinstance(arr, list) else None
    except json.JSONDecodeError:
        return None


def keyword_fallback_enrich(cand):
    """Deterministic fallback when LLM unavailable. Defaults plausibly-sports
    content to a bucket rather than discarding — false negatives are worse."""
    text = " ".join([cand["key"]] + cand.get("news", [])
                    + [t["text"] for t in cand.get("tweets", [])[:3]]).lower()
    padded = f" {text} "
    best, best_hits = None, 0
    for league, words in LEAGUE_KEYWORDS.items():
        hits = sum(1 for w in words if w.strip() and w in padded)
        weight = hits * (2 if league != "Other Sports" else 1)
        if weight > best_hits:
            best, best_hits = league, weight
    sports = best is not None and best_hits > 0
    return {
        "key": cand["key"], "sports": sports, "league": best,
        "obvious": matches_any(OBVIOUS_PATTERNS, cand["key"]),
        "early": (cand["t24_rank"] or 99) > 20 and len(cand["sources"]) == 1,
        "entity": cand["name"][:60],
        "why_now": f"Spiking on {'/'.join(cand['sources'])}; driver unconfirmed (fallback classifier — no LLM key).",
        "angle": "Verify the driver manually before attaching content (fallback mode).",
        "duplicate_of": None, "market_id": None,
    }


def mock_llm(candidates):
    """Deterministic mock for tests: known fixture keys get scripted
    enrichment; everything else uses the keyword fallback."""
    scripted = {
        "monday night football": {"sports": True, "league": "NFL", "obvious": True,
            "early": False, "entity": "Monday Night Football",
            "why_now": "Weekly scheduled broadcast trending because it exists.",
            "angle": "n/a", "duplicate_of": None, "market_id": None},
        "butker fake injury celebration": {"sports": True, "league": "NFL",
            "obvious": False, "early": False, "entity": "Harrison Butker",
            "why_now": "Butker's fake-injury TD celebration clip is being memed as a soccer-flop crossover.",
            "angle": "Cut the clip against famous soccer flops; post before rights takedowns.",
            "duplicate_of": None, "market_id": "900001"},
        "sacramento river cats rally possum": {"sports": True,
            "league": "Other Sports", "obvious": False, "early": True,
            "entity": "River Cats rally possum",
            "why_now": "A possum delayed the AAA game and the broadcast crew named it; clip going viral.",
            "angle": "Early meme window: possum content peaks tomorrow; post tonight.",
            "duplicate_of": None, "market_id": None},
        "supreme court ruling": {"sports": False, "league": None,
            "obvious": False, "early": False, "entity": "Supreme Court",
            "why_now": "Major non-sports news event.", "angle": "n/a",
            "duplicate_of": None, "market_id": None},
        "chiefs vs bills": {"sports": True, "league": "NFL", "obvious": False,
            "early": False, "entity": "Chiefs vs Bills",
            "why_now": "Mahomes' no-look lateral in the 4th quarter is the clip everyone is quoting.",
            "angle": "Push the lateral clip with the rivalry framing.",
            "duplicate_of": None, "market_id": "900001"},
        "third string kicker moment": {"sports": True, "league": "NFL",
            "obvious": False, "early": True, "entity": "Practice-squad kicker",
            "why_now": "A practice-squad kicker drilled a 60-yarder in his debut and his mic'd-up reaction is viral.",
            "angle": "Underdog angle: post the mic'd-up clip before mainstream picks it up.",
            "duplicate_of": None, "market_id": None},
    }
    out = {}
    for c in candidates:
        if c["key"] in scripted:
            out[c["key"]] = {"key": c["key"], **scripted[c["key"]]}
        else:
            out[c["key"]] = keyword_fallback_enrich(c)
    return out


# ============================================================================
# Scoring — MARKET-BLIND BY CONSTRUCTION.
# This function receives no market data of any kind. Do not add any.
# ============================================================================

def compute_score(signals):
    """signals: x_rank, t24_rank, t24_prev_rank, google_traffic, sources,
    first_seen_hours_ago, new_on_x, engagement_now, engagement_prev, early.
    Returns (total 0-100, breakdown dict)."""
    b = {}

    # Velocity (max 35)
    v = 0.0
    if signals.get("new_on_x"):
        v += 15
    prev, cur = signals.get("t24_prev_rank"), signals.get("t24_rank")
    if prev and cur and prev > cur:
        v += min(12, (prev - cur) * 2)
    traffic = signals.get("google_traffic") or 0
    if traffic > 0:
        import math
        v += min(8, math.log10(max(traffic, 1)) * 2)
    b["velocity"] = round(min(v, 35), 1)

    # Cross-platform presence (max 25)
    srcs = set(signals.get("sources") or [])
    on_x = bool(srcs & {"x", "trends24"})
    on_g = "google" in srcs
    b["cross_platform"] = 25 if (on_x and on_g) else 8

    # Freshness (max 20)
    h = signals.get("first_seen_hours_ago", 999)
    b["freshness"] = 20 if h < 2 else 15 if h < 6 else 10 if h < 12 else 5 if h < 24 else 0

    # Engagement acceleration (max 20)
    e = 0.0
    now_e, prev_e = signals.get("engagement_now"), signals.get("engagement_prev")
    if now_e is not None and prev_e is not None and prev_e > 0 and now_e > prev_e:
        e += min(12, 12 * (now_e - prev_e) / prev_e)
    elif now_e is not None and prev_e in (None, 0) and now_e > 0:
        e += 6  # engagement exists, no baseline yet
    if prev and cur and prev > cur and (prev - cur) >= 3:
        e += 8  # sustained rank climb
    b["engagement"] = round(min(e, 20), 1)

    # Early-signal bonus (max 10)
    b["early_bonus"] = 10 if signals.get("early") else 0

    total = min(100, round(sum(b.values())))
    return total, b


# ============================================================================
# Market matching — runs strictly AFTER selection + scoring
# ============================================================================

def keyword_market_match(cand_name, entity, catalog):
    """Fallback matcher: word overlap between trend entity and market question."""
    words = set(w for w in normalize_key(f"{cand_name} {entity}").split() if len(w) > 3)
    best, best_score = None, 0
    for m in catalog:
        qwords = set(w for w in normalize_key(m["question"]).split() if len(w) > 3)
        overlap = len(words & qwords)
        if overlap >= 2 and overlap > best_score:
            best, best_score = m, overlap
    return best


def attach_market(trend, enrichment, catalog_by_id, catalog):
    """Purely additive: sets trend['market'] or trend['market_gap']=True.
    Never touches score/league/early/anything else."""
    m = None
    mid = enrichment.get("market_id")
    if mid is not None and str(mid) in catalog_by_id:
        m = catalog_by_id[str(mid)]
    if m is None:
        m = keyword_market_match(trend["name"], trend["entity"], catalog)
    if m:
        trend["market"] = {
            "question": m["question"],
            "url": f"https://polymarket.com/event/{m['event_slug'] or m['slug']}",
            "outcomes": [[o, p] for o, p in zip(m["outcomes"], m["prices"])],
            "volume24hr": m["volume24hr"],
        }
        trend["market_gap"] = False
    else:
        trend["market"] = None
        trend["market_gap"] = True


# ============================================================================
# Slack digest — push layer for the channel. Selection here happens AFTER
# the market-blind pipeline; it reads scores, never influences them.
# ============================================================================

def select_slack_trends(all_trends, history):
    """Pick 'niche things the algorithm is pushing' and dedupe against what
    was already posted. Returns list of trends, best first."""
    posted = history.setdefault("slack_posted", {})
    picks = []
    for t in all_trends:
        if SLACK_INCLUDE_MODE == "all":
            eligible = True
        elif SLACK_INCLUDE_MODE == "top":
            eligible = True  # capped below by score order
        else:  # "niche": under-the-radar, or brand new on X with velocity
            eligible = t["early"] or (
                t["new_on_x"] and t["score"] >= SLACK_NEW_MOVER_MIN_SCORE)
        if not eligible:
            continue
        prev = posted.get(t["key"])
        if prev and t["score"] - prev.get("score", 0) < SLACK_REPOST_SCORE_JUMP:
            continue  # already posted and hasn't jumped — stay quiet
        picks.append(t)
    picks.sort(key=lambda t: t["score"], reverse=True)
    return picks[:SLACK_MAX_TRENDS_PER_POST]


def build_slack_payload(picks, generated_at):
    """Block Kit payload: compact league-grouped cards. Pure function (tested
    offline). Returns None if there is nothing worth posting."""
    if not picks:
        return None
    dash = os.environ.get("DASHBOARD_URL", "").strip()
    blocks = [{
        "type": "header",
        "text": {"type": "plain_text", "emoji": True,
                 "text": f"📡 Trend Radar — {len(picks)} under-the-radar pick{'s' if len(picks) != 1 else ''}"},
    }]
    by_league = {}
    for t in picks:
        by_league.setdefault(t["league"], []).append(t)
    ordered = sorted(by_league, key=lambda lg: SPORT_ORDER.index(lg))
    for lg in ordered:
        lines = []
        for t in by_league[lg]:
            badges = []
            if t["early"]:
                badges.append("🟣 UNDER THE RADAR")
            if t["new_on_x"]:
                badges.append("🟢 NEW ON X")
            head = f"*[{t['score']}] {t['entity']}*" + (("  " + " ".join(badges)) if badges else "")
            body = t["why_now"]
            tw = (t.get("example_tweets") or [None])[0]
            tw_line = f"<{tw['url']}|top post @{tw['username']} · ♥{_k(tw['likes'])}>" if tw else None
            if t.get("market"):
                m = t["market"]
                odds = " / ".join(f"{o} {round(p * 100)}¢" for o, p in (m.get("outcomes") or [])[:2])
                mk = f"📈 <{m['url']}|{m['question']}> — {odds} (${_k(round(m['volume24hr']))} 24h)"
            else:
                mk = "▫️ MARKET GAP — no matching market (FYI markets team)"
            tail = f"💡 {t['angle']}"
            lines.append("\n".join(x for x in [head, body, tw_line, mk, tail] if x))
        blocks.append({"type": "section",
                       "text": {"type": "mrkdwn",
                                "text": f"*{lg}*\n\n" + "\n\n".join(lines)}})
    ctx = f"Cycle {generated_at} · niche/mover picks only — full board on the dashboard"
    if dash:
        ctx += f": <{dash}|open Trend Radar>"
    blocks.append({"type": "context",
                   "elements": [{"type": "mrkdwn", "text": ctx}]})
    return {"blocks": blocks, "text": f"Trend Radar: {len(picks)} under-the-radar sports picks"}


def post_to_slack(data, history):
    """Fail-soft: Slack problems must never break the data pipeline."""
    webhook = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
    if not webhook:
        SOURCE_STATUS["slack"] = "no SLACK_WEBHOOK_URL — skipped"
        return
    all_trends = [t for s in data["sections"] for t in s["trends"]]
    picks = select_slack_trends(all_trends, history)
    payload = build_slack_payload(picks, data["generated_at"])
    if payload is None:
        SOURCE_STATUS["slack"] = "ok (nothing new to post — stayed quiet)"
        return
    if OFFLINE:
        SOURCE_STATUS["slack"] = f"ok (offline — would post {len(picks)})"
    else:
        try:
            req = urllib.request.Request(
                webhook, data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                resp.read()
            SOURCE_STATUS["slack"] = f"ok (posted {len(picks)} trends)"
        except Exception as e:
            SOURCE_STATUS["slack"] = f"error: {str(e)[:80]}"
            log(f"Slack post failed (non-fatal): {e}")
            return
    posted = history.setdefault("slack_posted", {})
    for t in picks:
        posted[t["key"]] = {"score": t["score"], "at": data["generated_at"]}
    # prune posted-log alongside trend history
    if len(posted) > 500:
        for k in sorted(posted, key=lambda k: posted[k]["at"])[:len(posted) - 500]:
            del posted[k]


def _k(n):
    n = int(n)
    if n >= 1_000_000:
        return f"{n / 1e6:.1f}M"
    if n >= 1_000:
        return f"{n / 1e3:.1f}K"
    return str(n)


# ============================================================================
# Main pipeline
# ============================================================================

def run_pipeline():
    started = now_utc()
    history = load_json(HISTORY_PATH, {})
    history.setdefault("trends", {})
    history.setdefault("x_usage", {})
    history.setdefault("prev_x_trends", [])
    history.setdefault("suppressed", [])
    stats = {"newly_enriched": 0, "cache_hits": 0, "suppressed": 0,
             "discarded_non_sports": 0}

    # ---- 1. Ingest ----
    x_trends = fetch_x_trends()
    t24 = fetch_trends24()
    if x_trends is None and t24:
        x_trends = t24[0]["trends"]  # degrade to free mirror
    g_trends = fetch_google_trends()
    catalog = fetch_market_catalog()
    catalog_by_id = {m["id"]: m for m in catalog}

    cands = build_candidates(x_trends, t24, g_trends)
    log(f"{len(cands)} merged candidates")

    # new_on_x: in this cycle's X list but not the previous cycle's
    prev_x = {normalize_key(n) for n in history["prev_x_trends"]}
    for c in cands.values():
        c["new_on_x"] = (c["x_rank"] is not None) and (c["key"] not in prev_x)

    # ---- 2. Detail tweet pulls (budget-capped, hard-capped) ----
    for c in pick_detail_candidates(cands, history):
        c["tweets"] = fetch_x_tweets_for(c["name"], history["x_usage"])
    today = started.strftime("%Y-%m-%d")
    log(f"X budget used today: {history['x_usage'].get(today, 0)}/{X_DAILY_TWEET_BUDGET}")

    # ---- 3. Enrichment (cached, one LLM call for the uncached) ----
    to_enrich, cached = [], {}
    for c in sorted(cands.values(), key=lambda c: (
            c["x_rank"] or 99, c["t24_rank"] or 99, -c["google_traffic"]))[:MAX_CANDIDATES_TO_LLM]:
        h = history["trends"].get(c["key"], {})
        enr, enr_at = h.get("enrichment"), h.get("enriched_at")
        fresh = False
        if enr and enr_at:
            age_h = (started - parse_iso(enr_at)).total_seconds() / 3600
            gained_source = bool(set(c["sources"]) - set(h.get("sources_seen", [])))
            fresh = age_h < ENRICH_TTL_HOURS and not gained_source
        if fresh:
            cached[c["key"]] = enr
            stats["cache_hits"] += 1
        else:
            to_enrich.append(c)

    enriched = {}
    if to_enrich:
        result = llm_enrich(to_enrich, catalog, x_available=bool(X_BEARER) or OFFLINE)
        if result is None:
            result = {c["key"]: keyword_fallback_enrich(c) for c in to_enrich}
            if SOURCE_STATUS["llm"] == "not attempted":
                SOURCE_STATUS["llm"] = "keyword fallback"
        for c in to_enrich:
            enriched[c["key"]] = result.get(c["key"]) or keyword_fallback_enrich(c)
        stats["newly_enriched"] = len(to_enrich)
    else:
        SOURCE_STATUS["llm"] = f"ok (all {len(cached)} cached)"
    all_enr = {**cached, **enriched}

    # Cache enrichment for EVERY enriched candidate — including ones about to
    # be suppressed or discarded — so they don't hit the LLM again next cycle.
    for c in to_enrich:
        h = history["trends"].setdefault(c["key"], {})
        h.setdefault("first_seen", iso(started))
        h["last_seen"] = iso(started)
        h["sources_seen"] = sorted(set(h.get("sources_seen", [])) | set(c["sources"]))
        h["enrichment"] = enriched[c["key"]]
        h["enriched_at"] = iso(started)

    # ---- 4. Gate + suppression + duplicate merge ----
    kept = []
    for key, enr in all_enr.items():
        c = cands.get(key)
        if not c:
            continue
        if not enr.get("sports"):
            stats["discarded_non_sports"] += 1
            continue
        if enr.get("obvious"):
            stats["suppressed"] += 1
            history["suppressed"].append(
                {"key": key, "reason": enr.get("why_now", "obvious"), "at": iso(started)})
            continue
        if enr.get("duplicate_of") and enr["duplicate_of"] in all_enr \
                and enr["duplicate_of"] != key:
            continue
        kept.append((c, enr))
    history["suppressed"] = history["suppressed"][-200:]

    # ---- 5. Score (market-blind) + assemble ----
    trends_out = []
    for c, enr in kept:
        h = history["trends"].setdefault(c["key"], {})
        first_seen = h.get("first_seen") or iso(started)
        h["first_seen"] = first_seen
        engagement_now = None
        if c["tweets"]:
            top = c["tweets"][:EXAMPLE_TWEETS_PER_TREND]
            engagement_now = sum(t["likes"] + 2 * t["retweets"] for t in top) / len(top)
        score, breakdown = compute_score({
            "new_on_x": c["new_on_x"], "t24_rank": c["t24_rank"],
            "t24_prev_rank": c["t24_prev_rank"],
            "google_traffic": c["google_traffic"], "sources": c["sources"],
            "first_seen_hours_ago": (started - parse_iso(first_seen)).total_seconds() / 3600,
            "engagement_now": engagement_now,
            "engagement_prev": h.get("last_engagement"),
            "early": bool(enr.get("early")),
        })
        league = enr.get("league") if enr.get("league") in SPORT_ORDER else "Other Sports"
        trend = {
            "key": c["key"], "name": c["name"],
            "entity": (enr.get("entity") or c["name"])[:60],
            "league": league, "score": score, "score_breakdown": breakdown,
            "early": bool(enr.get("early")), "new_on_x": c["new_on_x"],
            "why_now": (enr.get("why_now") or "")[:200],
            "angle": (enr.get("angle") or "")[:300],
            "sources": c["sources"], "first_seen": first_seen,
            "example_tweets": [
                {"url": t["url"], "text": t["text"][:140],
                 "likes": t["likes"], "retweets": t["retweets"],
                 "username": t["username"]}
                for t in c["tweets"][:EXAMPLE_TWEETS_PER_TREND]],
        }
        # ---- 6. Market layer: strictly after scoring, purely additive ----
        attach_market(trend, enr, catalog_by_id, catalog)
        trends_out.append(trend)
        # persist history
        h["last_seen"] = iso(started)
        h["sources_seen"] = sorted(set(h.get("sources_seen", [])) | set(c["sources"]))
        h["enrichment"] = enr
        h.setdefault("enriched_at", iso(started))
        if c["key"] in enriched:
            h["enriched_at"] = iso(started)
        if engagement_now is not None:
            h["last_engagement"] = engagement_now

    # ---- 7. Sections ----
    by_league = {}
    for t in trends_out:
        by_league.setdefault(t["league"], []).append(t)
    sections = []
    for league, ts in by_league.items():
        ts.sort(key=lambda t: t["score"], reverse=True)
        sections.append({"league": league,
                         "activity": sum(t["score"] for t in ts),
                         "trends": ts[:TOP_N_PER_SECTION]})
    if SECTION_ORDER_MODE == "activity":
        sections.sort(key=lambda s: (-s["activity"], SPORT_ORDER.index(s["league"])))
    else:
        sections.sort(key=lambda s: SPORT_ORDER.index(s["league"]))

    # ---- 8. Write outputs ----
    data = {
        "schema_version": 2,
        "generated_at": iso(started),
        "sections": sections,
        "source_status": SOURCE_STATUS,
        "x_budget": {"used_today": history["x_usage"].get(today, 0),
                     "cap": X_DAILY_TWEET_BUDGET},
        "config": {"top_n": TOP_N_PER_SECTION, "sport_order": SPORT_ORDER,
                   "section_order_mode": SECTION_ORDER_MODE},
        "stats": stats,
    }
    # ---- 9. Slack digest (reads final data; never feeds back into it) ----
    post_to_slack(data, history)
    # prune history
    cutoff = started.timestamp() - HISTORY_RETENTION_DAYS * 86400
    history["trends"] = {k: v for k, v in history["trends"].items()
                         if parse_iso(v.get("last_seen", v.get("first_seen", iso(started)))).timestamp() > cutoff}
    history["x_usage"] = {d: n for d, n in history["x_usage"].items()
                          if d >= datetime.fromtimestamp(cutoff, timezone.utc).strftime("%Y-%m-%d")}
    if x_trends:
        history["prev_x_trends"] = x_trends
    save_json(DATA_PATH, data)
    save_json(HISTORY_PATH, history)
    log(f"wrote {len(trends_out)} trends across {len(sections)} sections "
        f"(suppressed {stats['suppressed']}, non-sports {stats['discarded_non_sports']}, "
        f"cache hits {stats['cache_hits']}, newly enriched {stats['newly_enriched']})")
    return data, history, stats


if __name__ == "__main__":
    try:
        run_pipeline()
    except Exception as e:
        log(f"FATAL: {e}")
        raise
