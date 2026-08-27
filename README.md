# UK Stock Watch — Cloud Edition (works on iOS)

Same idea as the browser extension, but runs on GitHub's free servers instead of your
computer — so it works from your iPhone with no desktop required. Pushes alerts to WhatsApp
and publishes a live dashboard page you can bookmark on your Home Screen.

## What this is — and isn't

Same as before, worth repeating: this surfaces public news and price data faster. It does
not predict prices, guarantee timing, or place trades. You make every decision.

## Setup (about 10 minutes, all free)

### 1. Get a free GitHub account
[github.com/signup](https://github.com/signup) if you don't have one.

### 2. Create a new repository
- New repository → name it anything (e.g. `uk-stock-watch`) → **Public** (required for free
  GitHub Pages) → Create.
- Upload every file from this folder to it (drag-and-drop on the GitHub web UI works, or use
  `git push` if you're comfortable with that).

### 3. Set up free WhatsApp alerts (CallMeBot)
1. Save **+34 644 51 95 23** as a contact on your phone.
2. WhatsApp it: `I allow callmebot to send me messages`.
3. It replies with an API key.
4. In your GitHub repo: Settings → Secrets and variables → Actions → **New repository secret**:
   - Name: `WEBHOOK_URL`
     Value: `https://api.callmebot.com/whatsapp.php?phone=YOURNUMBER&apikey=YOURKEY`
   - Name: `WEBHOOK_TEMPLATE`
     Value: `callmebot`

(Alternatives: `ntfy` for free phone push, or `generic` to POST JSON to your own webhook —
same idea as the browser extension's settings.)

### 4. Turn on the schedule
- Repo → **Actions** tab → you'll see "UK Stock Watch Poller" → click **Enable workflow** if
  prompted.
- Click **Run workflow** once manually to test it immediately rather than waiting 5 minutes.
- Check your WhatsApp — you should get alerts if there are any live upgrades/downgrades on
  your watchlist right now (there may be none, that's normal).

### 5. Turn on the live dashboard (GitHub Pages)
- Repo → Settings → Pages → Source: **Deploy from a branch** → Branch: `main`, folder: `/docs`
  → Save.
- After a minute, your dashboard is live at `https://YOURUSERNAME.github.io/uk-stock-watch/`
- On your iPhone: open that link in Safari → Share → **Add to Home Screen**. Now it behaves
  like an app icon.

### 6. Edit your watchlist
Edit `watchlist.json` in the repo (GitHub's web editor works fine — click the file, pencil
icon, edit, commit). Same LSE-ticker format as the extension:
```json
[{ "ticker": "LLOY", "name": "Lloyds Banking Group" }]
```

## Market-wide broker alerts (all LSE companies, not just your watchlist)

Your watchlist stays small (prices, dedicated news search per stock) — but broker
upgrade/downgrade **alerts** now cover the entire LSE, not just those few stocks. Adding all
~1,900 LSE-listed companies to `watchlist.json` directly isn't feasible: this design does
several requests *per watchlist stock*, so 1,900 entries would take hours per cycle and get
rate-limited long before finishing. Instead, one extra market-wide search (not scoped to any
company name) plus the existing Investing.com ratings feed are scanned unfiltered for
upgrade/downgrade language — covering any LSE company, for the cost of one extra request per
cycle rather than thousands. Shown as its own "Market-wide Broker Alerts" section at the top
of the dashboard, separate from your per-stock feed below it.

## LSE Screener (Volume / Gainers / Losers)

Every run (~5 minutes), the poller pulls three market-wide rankings via Yahoo Finance's free
screener endpoint, scoped to `region: gb` — Top 10 Volume, Top 10 Gainers, Top 10 Losers —
and pushes them as a WhatsApp message plus a 3-column table at the top of the dashboard.
Not limited to `watchlist.json`; it's there to catch unusual activity elsewhere on the LSE.
**Finviz was considered and left out on purpose**: its free screener is US-market only
(NYSE/NASDAQ/AMEX), and its robots.txt/terms restrict automated scraping — Yahoo's
GB-scoped screener is both more relevant here and doesn't raise that conflict.

## Already Moving Today (descriptive volatility flag)

The poller flags any watchlist stock that has already moved ±5% or more today, pushed to
WhatsApp and shown on the dashboard. **This is a fact about what has already happened, not a
prediction of what happens next** — it doesn't identify entry/exit points or forecast further
movement. No free (or honestly, most paid) source can reliably predict which stocks will move
a given amount in advance; this shows what's already moving so you can look closer yourself.

## Reuters & Bloomberg coverage

Neither offers a free public feed anymore (Reuters dropped RSS years ago; Bloomberg is
subscription-walled), so this scopes the same free Google News search specifically to
`site:reuters.com` and `site:bloomberg.com` — legitimate, no scraping either site directly.
Full Bloomberg articles may still require their subscription to read past the headline.

## What actually pushes a WhatsApp alert

Not everything found gets pushed — that would be constant noise. What does:
- **Upgrades/downgrades** — any broker rating change, watchlist or market-wide
- **Watchlist event news** — mergers, takeovers, trading updates, profit warnings,
  results, for stocks specifically on your watchlist (genuinely price-moving, and scoped
  so it isn't noise about companies you don't track)

What stays dashboard-only (visible, but doesn't ping WhatsApp): general "news" category
items and price-target-only mentions — usually too frequent and low-signal to push
individually. Everything is still visible in full on the dashboard regardless.

## Optional: AI daily digest (the only paid feature — opt-in)

Everything else in this tool is free. This one feature isn't: an optional AI-generated
plain-English summary of what the last few hours' data actually showed, sent as its own
WhatsApp message every ~6 hours. It costs real money per call (a cheap model, roughly a
few cents per digest) — nothing else changes or breaks if you skip this section entirely.

**What the AI is allowed to do:** restate facts already gathered elsewhere in this
tool — which broker rated what, what moved and by how much, what news broke. **What it is
never allowed to do:** recommend buying/selling/holding, predict future price movement, or
use advisory language. This is enforced twice: a system prompt instructing it strictly, and
a second, independent check afterward that scans the output for advice-shaped phrases
("you should," "good opportunity," "could rise," "I recommend," etc.) and **blocks the
message from ever sending** if anything matches — the prompt isn't trusted alone.

**To turn it on:**
1. Get an API key from [console.anthropic.com](https://console.anthropic.com)
2. Add repo secret: Name `ANTHROPIC_API_KEY`, Value your key
3. That's it — it activates automatically on the next run. Leave the secret unset and this
   feature simply never runs (checked first, before anything else happens).

**To turn it off again:** delete the `ANTHROPIC_API_KEY` secret, or reduce/increase how often
it runs by changing `AI_DIGEST_INTERVAL_SECONDS` near the top of `poll.py` (default 6 hours).

## Why doesn't the dashboard update in real time?

Two separate things, worth knowing both:

1. **The underlying data updates every ~5 minutes** — the poll cycle, by design (checking
   faster risks rate-limiting, as covered earlier in this README).
2. **The page now auto-refreshes itself every 90 seconds** (`<meta http-equiv="refresh">`)
   so you don't have to manually reload to see new data land — this didn't exist in earlier
   versions, where the page you had open just sat static until you refreshed it yourself.

One more honest caveat: after the poller commits new data, **GitHub Pages itself takes a
short while (often 30-90 seconds) to actually redeploy** the updated page — so a refresh
immediately after a run finishes might still briefly show the previous version. This isn't
something the tool controls; it's GitHub's own deployment pipeline.

## Time source and "today" scoping

No external time-server call (e.g. `time.windows.com`) is used, and that's intentional
rather than a gap: GitHub Actions runners are cloud VMs with system clocks already
NTP-synced automatically — the same underlying mechanism a dedicated time server provides,
just built into the OS. Reading the system clock is already accurate.

News and broker items are now scoped two ways: a 21-day sanity cap (catches obviously stale
reposts), plus a stricter same-**calendar-day** filter using real Europe/London time (via
Python's `zoneinfo`, so it correctly handles the GMT/BST switch rather than a fixed UTC
offset). Toggle with `NEWS_SAME_LONDON_DAY_ONLY` near the top of `poll.py` if this proves
too strict and cuts out relevant items from yesterday evening.

## Data quality fixes: liquidity filtering and news recency

Two real issues surfaced from actual use, both fixed at the source:

- **Illiquid penny-stock noise in gainers/losers.** The `.L`-suffix filter removed GDR/IOB
  junk, but thousands of genuinely `.L`-listed micro-caps still swing 100%+ on almost no
  volume (a move from 0.1p to 1p shows as "900%" and means nothing). Gainers/losers now
  require real volume (500,000+ shares) and a non-penny price (20p+) — Top Volume is
  unaffected, since that ranking is about volume itself.
- **Stale news resurfacing.** Google News search returns "most relevant," not "most
  recent" — old syndicated articles (sometimes years old) could appear alongside current
  ones. All news/broker items are now filtered to the last 21 days before ever reaching
  the feed, alerts, or dashboard.

## Analyst ratings history (structured, not just keyword-matched)

The poller also pulls each stock's **actual analyst ratings history** from Yahoo Finance's
quoteSummary endpoint — real firm name, exact action, and date — rather than relying only on
news-headline keyword matching. More reliable than news-based classification specifically for
"did a broker change their rating." Also surfaces consensus rating and average analyst price
target next to each stock's price on the dashboard. Alerts only fire on ratings from the last
48 hours — older history shows for context without flooding WhatsApp on first run.

## Heat Map

A color-coded grid on the dashboard, built entirely from the gainers/losers screener data
already flowing in — no additional source. Green/red by direction, darker/more saturated by
size of the move (capped visually at ±10%). Built as a self-rendered alternative after
checking the LSE's own official heat map: it's powered by the same `/lsecws/` backend their
`robots.txt` explicitly disallows for automated access, same as their risers-and-fallers page,
so it wasn't scraped.

## Always checking, always reporting — how that's actually guaranteed

Three specific mechanisms, not just an assumption:

1. **Self-sustaining schedule.** GitHub auto-disables a repo's scheduled workflows after
   60 days with no repository activity. Every single run commits an updated `state/data.json`
   (the timestamp alone guarantees a diff), so as long as it's running at all, it keeps the
   repo "active" and the schedule alive — no manual intervention needed.
2. **Heartbeat.** Every 6 hours, you get a "✅ still checking" WhatsApp message even if
   nothing newsworthy happened — so silence is never your only signal that something's wrong.
3. **Failure reporting, two layers deep.** If the poller crashes mid-run, it tries to send you
   a WhatsApp message saying so before re-raising the error (so the GitHub Actions tab also
   shows it failed). If Python fails so badly it can't even reach that error handler, a
   separate workflow-level step sends a backstop notification directly. That backstop only
   works cleanly with the CallMeBot webhook format — if you're using `ntfy` or `generic`, the
   in-Python failure handler is still your main protection.

Nothing here is "guaranteed" in the way a paid monitoring service might promise (GitHub itself
could have an outage), but going dark without any notice is the specific failure mode this is
built to avoid.

## Optional: FTSE 350 job for deeper tracking (hourly)

A second, separate scheduled job (`.github/workflows/poll-ftse350.yml`) runs against a
bigger watchlist (`watchlist-ftse350.json`) — live price, consensus rating, and dedicated
news search per company, same as the main watchlist, just for more companies. It runs
**hourly, not every 5 minutes** — a larger list does several requests per company, so it
can't run on the fast schedule without getting rate-limited. It writes to entirely separate
state (`state-ftse350/`) and its own dashboard page (`docs/ftse350.html`), and skips the
market-wide search/screener/heat map entirely (`SKIP_MARKET_WIDE=true`) since the 5-minute
job already covers those — running them twice would send duplicate WhatsApp alerts.

**Important honesty note on the starter list:** `watchlist-ftse350.json` ships with ~50
verified, well-known large-cap tickers (banks, miners, oil majors, retailers, pharma) as a
starting point — it is **not** the full, current FTSE 350. Fetching an accurate, up-to-date
350-company list programmatically wasn't reliable to do automatically, and shipping a
guessed list risked wrong tickers breaking the polling silently. To get the real current
list: visit the LSE's own public FTSE 100/250 constituents pages yourself, copy the
ticker/name pairs, and add them to `watchlist-ftse350.json` in the same
`{ "ticker": "X", "name": "Y" }` format as the existing entries. Same rule as the main
watchlist: if a ticker returns no price data, double-check it against a current source —
index constituents change quarterly.

To turn this job on: it's enabled automatically alongside the main workflow — no extra
secrets needed, it reuses the same `WEBHOOK_URL`/`WEBHOOK_TEMPLATE`. Just edit
`watchlist-ftse350.json` to add more companies, commit, and it picks them up next hour (or
trigger it manually from the Actions tab: **UK Stock Watch Poller — FTSE 350 (hourly)** →
**Run workflow**).

## Important limitations of the cloud version

- **5-minute cycle, not 30 seconds.** GitHub's free scheduler won't go faster, and even then
  can run a few minutes late during busy periods. This is the tradeoff for not needing a
  computer running.
- **GitHub disables schedules after 60 days of repo inactivity.** If you stop getting alerts,
  go to the Actions tab and click "Run workflow" once to re-enable it — a `workflow_dispatch`
  or any commit resets the clock.
- Everything else (sources, classification, broker tagging, disclaimers) works identically to
  the browser extension.

## TradingView + Pine Script

Unchanged from before — the TradingView iOS app is fully featured, so `strategy.pine` works
exactly the same way on your phone: Pine Editor → paste → Add to Chart → Strategy Tester to
backtest → set an alert with the same webhook URl to land in the same WhatsApp thread as
everything else.
