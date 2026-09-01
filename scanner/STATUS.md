# LSE Opportunity Scanner — Consolidated Status

*One accurate picture: what's real and verified, what's provisional, what's not started.*

---

## ✅ REAL AND VERIFIED (running in production, confirmed via actual GitHub Actions logs)

| Component | What it does | Verified how |
|---|---|---|
| **Universe** | FTSE 100, 100 stocks, real LSE Heatmap data | Confirmed live in production log |
| **Price/Volume/52wk range** | Real LSE first-party data | Confirmed live |
| **Momentum engine** | Today's price change → 0-100 score | Tested, matches spec examples |
| **Liquidity engine** | Volume vs average → 0-100 score | Tested |
| **Trend engine** | 52-week range position → 0-100 score | Tested |
| **Analyst Evidence engine** | Broker target upside → 0-100 score (Yahoo, 20-stock/run rotation) | Tested |
| **News Catalyst engine** | Same-day news presence (existing pipeline, deduplicated) | Tested |
| **BUY SCORE** (labeled PROVISIONAL) | Weighted average of available engines, renormalized when data missing | Tested — missing data never becomes zero |
| **DATA CONFIDENCE** | Freshness-adjusted, separate from score | Tested — identical score, different confidence, proven with stale-vs-fresh data |
| **MODEL DATA COVERAGE** | Separate from Universe Coverage — no longer conflated | Tested — 100% universe / 72% model data, genuinely different numbers |
| **Evidence Breadth / Opportunity Quality** | LOW/MEDIUM/HIGH based on how many engines contributed | Tested against exact spec example (99 score, 2 engines → LOW quality, flagged) |
| **Risk engine** | 5 flags: extended momentum, high volatility, low liquidity, earnings imminent, data staleness | Tested against risky and quiet stocks |
| **Numeric Risk Score** | Additive, transparent, built from the same risk flags | Tested |
| **Rank & Percentile** | Real position in the scored universe | Tested against exact spec math (14/100 → 86th percentile) |
| **Cross-run persistence** | Prior valid data survives provider failure | Tested against 6 real failure modes (timeout, HTTP error, rate limit, malformed, empty, partial) |
| **FieldStatus tracking** | AVAILABLE/REFRESHED/STALE/EXPIRED/REFRESH_FAILED/MISSING/UNSUPPORTED | Tested |
| **Historical snapshots** | Real, model-versioned, append-only | Confirmed live — first real snapshot created |
| **Momentum/change detection** | 1d/3d/7d, band crossings, NEW/IMPROVING/DETERIORATING | Tested against exact spec examples, including a real bug caught and fixed (band crossings vs. noise threshold) |
| **Alert deduplication** | Same transition never re-fires | Tested |
| **AI Evidence Analysis** | Bull case/bear case/catalysts/risks — interpretation only, never invents facts | Tested: evidence-hash caching confirmed (no repeat API call), safety-pattern guard confirmed (blocked an advice-shaped response) |
| **Bear Agent** | Mandatory adversarial challenge for 80+ scores, genuinely separate AI call | Tested: confirmed 2 separate API calls (bull + bear) for the same evidence, not one shared result |
| **RNS classifier** | Deterministic, rule-based (profit warning, contract win, dividend, etc.) | Tested against real captured headlines + 8 illustrative cases |
| **`/opportunities` page** | Model status, scan health, provider health, tiers, rising/deteriorating, all-opportunities list, AI/Bear sections | Confirmed live |
| **Dashboard integration** | Nav link + live summary badge | Confirmed live |
| **News deduplication (main dashboard)** | Cross-source near-duplicate detection | Tested, deployed, live |

**Full regression: 95+ scanner/integration tests, 1500+ main dashboard tests, all passing.**

---

## 🟡 PROVISIONAL / PARTIAL (real, working, but incomplete)

- **Model coverage**: 4 of 8 intended dimensions (Momentum, Liquidity/Trend as "Market Confirmation", Analyst Evidence, News as "Catalyst"). Quality, Growth, Valuation, Timing have **no connected data source**.
- **AI layers**: real, tested, guardrailed — but cost-capped to 5 (evidence) + 3 (bear) stocks/run. Most of the universe never gets AI analysis, by design.
- **Universe**: FTSE 100 only. FTSE 250 names are technically available (Wikipedia scrape, already used elsewhere) but have no live pricing source — expanding would mean ~150 more Yahoo requests/run, not yet built or cost-justified.

---

## ❌ NOT STARTED (stated plainly, not stubbed)

- Quality/Growth/Valuation/Macro agents — blocked on a real fundamentals source (Companies House path researched, requires genuine iXBRL parsing, not built)
- Prediction ledger evaluation ("do 90+ outperform 80-89?") — needs weeks of real history that doesn't exist yet; only the storage layer (`history.py`) is built
- Opportunity episodes / lifecycle IDs
- AI memory of prior conclusions ("what changed since last analysis?")
- Sector-aware / relative scoring (vs sector, market, own history)
- Automated research reports
- `/data-quality` page
- FCA short interest
- Companies House filings pipeline
- Model-version-change isolation in change detection (a v1.0→v1.1 score jump would currently look like a real market move — this is a known, unaddressed gap)

---

## Genuine external blockers (not effort — actual dependencies)

1. **Fundamentals data source** — Companies House is real but needs substantial iXBRL parsing work; no paid provider purchased yet, per your instruction
2. **Time** — real history for prediction-ledger evaluation only accumulates one day at a time; there's no way to shortcut this honestly
3. **ANTHROPIC_API_KEY credit balance** — confirmed out of credits in the last production log; AI Evidence/Bear layers will silently no-op until resolved

---

*This document reflects the actual, tested state of the system as of this message — not aspiration.*
