# Trend Radar — Sports

A self-updating dashboard that shows what the sports world is talking about *right now* — the memes, quotes, beefs, personalities, and below-the-surface moments, not the scoreboard results everyone already saw. Refreshes itself 4x/day with zero manual steps. Where a Polymarket market matches a trend, it's shown as a suggestion with live odds; where none exists, the trend is flagged MARKET GAP for the markets team.

**Core rule, enforced in code and tests:** whether a Polymarket market exists never affects what counts as trending, how it's tagged, or how it scores. Trends are selected and scored from trend signals alone; markets are attached afterward as a purely informational layer.

This README is the complete manual — setup, what every file does, how to read the output, costs, and every knob you can turn. You don't need any other document.

---

## What's in this folder (every file, what it does, whether you touch it)

| File | What it is | Do you ever touch it? |
|---|---|---|
| `README.md` | This manual | Read it |
| `scripts/pipeline.py` | The entire engine: pulls trends, classifies, scores, matches markets, posts to Slack | Only to change settings (constants at the top) |
| `site/index.html` | The whole dashboard (one file, no build step) | No |
| `site/config.js` | Tells the dashboard which repo to read data from | No — fills itself in automatically on the first run |
| `site/vercel.json` | Stops Vercel from redeploying on every data refresh (protects the free tier) | No |
| `.github/workflows/refresh.yml` | The schedule: runs the engine 4x/day | Only to change run times |
| `WORKFLOW-COPY-ME.txt` | Exact copy of the workflow file, used during setup because the hidden `.github` folder doesn't upload by drag-and-drop | Copy-paste from it once, in Part 4 |
| `data/data.json` | The live trend data the dashboard reads (placeholder until the first run) | No — regenerated every cycle |
| `data/history.json` | The engine's memory: first-seen times, enrichment cache, X budget used, what's been posted to Slack | No — regenerated every cycle |
| `tests/` | Offline test suite + fixtures (30 checks) | Optional: `python3 tests/test_pipeline.py` |

## Before you start — have these 4 things ready

1. A GitHub account (free — sign up at github.com)
2. A Vercel account (free — you'll sign in WITH your GitHub account at vercel.com, so create GitHub first)
3. Your **X API Bearer Token** — from https://developer.x.com → your Project → your App → "Keys and tokens" tab → Bearer Token (Regenerate if you can't see it; regenerating invalidates the old one)
4. Your **Anthropic API key** — from https://console.anthropic.com → Settings → API keys → Create Key (starts with `sk-ant-`)

The Slack webhook is created during Part 3 below — you don't need it in advance, just access to your Slack workspace.

---

## Setup guide (browser only — no terminal, no git, ~15 minutes)

### Part 1 — Create the repo

1. Go to **https://github.com/new**
2. In "Repository name" type exactly: `trend-radar`
3. Select **Public** (required — public repos get unlimited free Actions minutes)
4. Check the box **"Add a README file"** (required — it creates the main branch so you can upload files)
5. Click the green **Create repository** button
   → *You should now see: an almost-empty repo page with a README.md in it.*

### Part 2 — Upload the files (skip the hidden .github folder!)

6. On your repo page, click **Add file → Upload files**
7. Drag in these folders/files from this project — **but NOT the `.github` folder** (it's hidden and GitHub's uploader mishandles it; we create the workflow by hand in Part 4):
   - the `scripts` folder
   - the `site` folder
   - the `data` folder
   - the `tests` folder
   - `WORKFLOW-COPY-ME.txt`
   - `README.md` (choose "replace" if asked)
8. Click **Commit changes**
   → *You should now see: scripts/, site/, data/, tests/, and WORKFLOW-COPY-ME.txt listed on your repo's main page.*

### Part 3 — Add secrets (do this BEFORE creating the workflow, or the first run fails)

9. On your repo page click **Settings** (top tab) → in the left sidebar: **Secrets and variables → Actions**
10. Click the green **New repository secret** button
11. Name: `X_BEARER_TOKEN` — Secret: paste your X API Bearer Token exactly as X gave it to you (including any %-characters). Click **Add secret**
12. Click **New repository secret** again. Name: `ANTHROPIC_API_KEY` — Secret: paste your Anthropic API key. Click **Add secret**
12b. **Slack digest (recommended):** in a new browser tab go to **https://api.slack.com/apps** → click **Create New App → From scratch** → name it `Trend Radar`, pick your workspace, click **Create App** → in the left sidebar click **Incoming Webhooks** → flip the toggle to **On** → click **Add New Webhook to Workspace** (bottom of page) → choose the channel the digests should land in → click **Allow** → copy the webhook URL (starts with `https://hooks.slack.com/services/`)
12c. Back in GitHub: **New repository secret** again. Name: `SLACK_WEBHOOK_URL` — Secret: paste the webhook URL. Click **Add secret**
   → *You should now see: three secret names listed under "Repository secrets".*

### Part 4 — Create the workflow file

13. Go back to your repo's main page (click the repo name at top). Click **Add file → Create new file**
14. In the filename box type exactly: `.github/workflows/refresh.yml` (typing the `/` characters creates the folders)
15. Open `WORKFLOW-COPY-ME.txt` from your repo (or from this project on your computer), copy ALL of its contents, and paste into the big text box
16. Click **Commit changes**
   → *You should now see: the file at .github/workflows/refresh.yml. Because the workflow triggers on workflow-file changes, the first run starts automatically.*
17. Click the **Actions** tab. You should see "Trend Radar refresh" running (yellow dot) or finished (green check). If it's not there, click **Trend Radar refresh** in the left list → **Run workflow → Run workflow**.
   → *You should now see: a green check within ~2 minutes. The run commits fresh data/data.json and fills in site/config.js with your repo name.*

### Part 5 — Deploy to Vercel

18. Go to **https://vercel.com/new** and sign in with your GitHub account
19. Find `trend-radar` in the repo list and click **Import**
20. **THE STEP EVERYONE MISSES:** expand the **Root Directory** setting, click **Edit**, and set it to `site` — not the repo root. If you skip this, you'll deploy a blank page.
21. Leave everything else at defaults. Click **Deploy**
   → *You should now see: confetti and a live URL like `trend-radar-xyz.vercel.app`. Open it — trends should load within a few seconds.*
22. Share that URL with the team. No logins needed. Done.
23. **Optional, makes Slack posts link to the dashboard:** back in GitHub → Settings → Secrets and variables → Actions → click the **Variables** tab → **New repository variable** → Name: `DASHBOARD_URL` — Value: your Vercel URL (e.g. `https://trend-radar-xyz.vercel.app`). Click **Add variable**.

### Did it work? checklist

- [ ] Repo Actions tab shows a green "Trend Radar refresh" run
- [ ] `data/data.json` in the repo shows a recent `generated_at` timestamp
- [ ] `site/config.js` in the repo shows YOUR repo name, not OWNER/REPO
- [ ] The Vercel URL loads with sports sections and score cards
- [ ] Freshness dot in the header is green
- [ ] Footer shows `x_api: ok`, `google_trends: ok`, `gamma_api: ok`, `llm: ok`
- [ ] Slack channel got a "📡 Trend Radar" digest (or footer shows `slack: ok (nothing new to post…)` on a quiet cycle)

### Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Actions run is red | Secrets added after the workflow, or typo'd names | Check both secrets exist with EXACT names above, then Actions → Run workflow |
| Dashboard says "Could not load data" | First Actions run hasn't finished, or config.js still has placeholder | Wait for a green run; check site/config.js shows your repo |
| Blank white page on Vercel | Root Directory not set to `site` | Vercel project → Settings → Build & Deployment → Root Directory → `site`, then redeploy |
| Footer says `llm: keyword fallback` | ANTHROPIC_API_KEY missing/invalid | Fix the secret; cards will say "driver unconfirmed" until then |
| Footer says X error, trends still appear | X key hit a limit or expired | Pipeline auto-fell back to trends24.in; fix the key when convenient |
| Data goes stale (red dot) | GitHub disables cron on repos with no commits for 60 days | Actions tab → enable/re-run; any tiny commit resets the clock |
| Every trend says MARKET GAP | gamma-api unreachable that cycle | Usually transient; check footer next cycle |
| No Slack posts arriving | Webhook secret missing/typo'd, or all cycles quiet | Footer shows the reason under `slack:`; "nothing new to post" is healthy dedupe, not a bug |
| Slack posts feel too sparse/too noisy | Selection mode | Edit `SLACK_INCLUDE_MODE` in scripts/pipeline.py: `"niche"` (default), `"top"`, or `"all"` |

---

## How to read the dashboard

Each card is one trend inside a sport section. Sections are ordered by live activity (most-trending sport first). The **score (0–100)** is the likelihood the trend keeps growing over the next 12–24h — tap the score pill to see the math (velocity, cross-platform, freshness, engagement, early bonus). **UNDER THE RADAR** = pre-mainstream, you're early; the Angle line tells you how to ride it before it peaks. **NEW ON X** = first appeared this cycle. The gray quoted lines are the actual viral posts driving the trend, with like/RT counts — tap to open. The blue chip is a matching Polymarket market with live odds and 24h volume; **MARKET GAP** (dashed) means no market exists — that's a heads-up for the markets team, never a knock on the trend.

**Trust rule:** suppression of "obvious" trends (e.g. Monday Night Football trending on a Monday) is intentional. If a big game had a real viral moment, the *moment* gets its own card.

## What's covered / not covered

Covered: X/Twitter trends (v2 API, your Pro key) with per-trend top-tweet pulls; trends24.in as free fallback + rank-velocity source (third-party mirror, ToS-gray, fail-soft — noted in footer); Google Trends RSS; Polymarket gamma-api for the market layer. Not covered: TikTok (Creative Center API requires TikTok for Business approval) and Instagram/Reels (no public trend source exists) — both noted in the dashboard footer.

## Costs

GitHub Actions: $0 (public repo). Vercel: $0 (static page; data commits don't trigger deploys thanks to site/vercel.json's ignoreCommand). X API: your existing Pro subscription — this tool uses ≤15,000 posts/day (~450k/month), leaving ~75% of the 2M/month cap for other team use; usage is tracked per UTC day in data/history.json and shown in the footer. Anthropic API: 4 Haiku calls/day with ~6h caching — typically well under $5/month.

## Changing things later (all in the GitHub web editor — open the file, click the pencil, commit)

**Refresh times:** `.github/workflows/refresh.yml`, the `cron:` line. Currently `0 3,13,17,22 * * *` (UTC ≈ 11pm/9am/1pm/6pm ET; shifts one hour when US DST ends). Add/remove hours as needed — also update `STALE_HOURS` in `site/index.html` if you change spacing significantly.

**Sport ordering:** `scripts/pipeline.py`, the `SPORT_ORDER` list at the top. This drives filter-chip order and tiebreaks. Sections auto-order by live activity by default; to force your fixed order instead, set `SECTION_ORDER_MODE = "fixed"` right below it.

**Trends per section:** default 8. Either edit `TOP_N_PER_SECTION` in `scripts/pipeline.py`, or set env `TREND_RADAR_TOP_N` in the workflow's "Run pipeline" step.

**X budget:** `X_DAILY_TWEET_BUDGET` (posts/day ceiling), `MAX_DETAIL_QUERIES_PER_CYCLE` (hard cap on per-trend tweet pulls, bug-proofing), `TWEETS_PER_DETAIL_QUERY` — all constants at the top of `scripts/pipeline.py`.

**Slack digest:** constants at the top of `scripts/pipeline.py`. `SLACK_INCLUDE_MODE` — `"niche"` (default: only UNDER THE RADAR picks and fresh new-on-X movers; saturated mainstream trends stay dashboard-only), `"top"` or `"all"` for fuller digests. `SLACK_MAX_TRENDS_PER_POST` (default 10), `SLACK_REPOST_SCORE_JUMP` (a trend already posted is only re-posted if its score climbed this much; default 15), `SLACK_NEW_MOVER_MIN_SCORE` (default 40). Quiet cycles post nothing by design. To change the target channel, create a new webhook for the other channel (README step 12b) and replace the `SLACK_WEBHOOK_URL` secret.

## Running the tests (optional, needs a computer with Python 3)

`python3 tests/test_pipeline.py` — fully offline (fixtures + mocked LLM), validates the data schema and the behavioral guarantees: noise discarded, niche sports kept, non-sports discarded, obvious trends suppressed while moments-inside survive, market matching works, MARKET GAP carries zero score penalty, market presence cannot change a score, and enrichment caching means a repeat run makes zero new LLM enrichments.
