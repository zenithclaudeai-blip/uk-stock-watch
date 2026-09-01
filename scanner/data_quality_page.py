"""
LSE Opportunity Scanner - the /data-quality page renderer.

Every number here comes directly from the same ScanResult already
produced by orchestrator.run_scan() - this page adds no new
computation, only a dedicated, focused presentation of data already
gathered, per the explicit "make the system auditable" requirement.
"""
from datetime import datetime, timezone


def render_data_quality_page(scan_result, ai_status: dict, docs_dir: str, render_standalone_page_fn):
    ai_status = ai_status or {}

    # Field-level coverage breakdown - counted directly from real
    # StockRecord data across the whole scanned universe, not estimated.
    with_52wk = sum(1 for r in scan_result.records.values() if r.price.fifty_two_week_low.is_available)
    with_volume = sum(1 for r in scan_result.records.values() if r.volume.volume.is_available)
    with_avg_volume = sum(1 for r in scan_result.records.values() if r.volume.average_volume.is_available)
    with_analyst = scan_result.coverage.get("withAnalystData", 0)
    with_news = scan_result.coverage.get("withNewsChecked", 0)
    with_fundamentals = sum(1 for r in scan_result.records.values() if r.fundamentals.market_cap.is_available)
    universe = scan_result.universe_size or 1

    # Freshness breakdown - real AgeStatus counts across every
    # component of every scored stock, not a single aggregate guess.
    from data_model import AgeStatus
    freshness_counts = {"fresh": 0, "recent": 0, "stale": 0, "expired": 0, "unknown": 0}
    for breakdown in scan_result.breakdowns.values():
        for c in breakdown.components:
            if not c.available:
                continue
            key = c.age_status.value if c.age_status else "unknown"
            freshness_counts[key] = freshness_counts.get(key, 0) + 1

    content = f"""
<p class="meta">Every number on this page comes directly from the same scan that produced the Opportunity Scanner results — nothing here is separately estimated. This page exists to make the system's real limitations visible, not to make it look more complete than it is.</p>

<h2>Universe & Model Coverage</h2>
<table>
<tr><th>Metric</th><th>Value</th></tr>
<tr><td>Current live universe</td><td>{scan_result.universe_size} stocks (FTSE 100)</td></tr>
<tr><td>Universe Coverage</td><td>{scan_result.coverage.get('universeCoveragePct', 0)}% (every eligible stock scanned)</td></tr>
<tr><td>Model Data Coverage</td><td>{scan_result.coverage.get('modelDataCoveragePct', 0)}% (average completeness of scoring evidence)</td></tr>
<tr><td>Fully covered</td><td>{scan_result.coverage.get('fullyCovered', 0)}</td></tr>
<tr><td>Partially covered</td><td>{scan_result.coverage.get('partiallyCovered', 0)}</td></tr>
<tr><td>Unscoreable</td><td>{scan_result.coverage.get('unscoreable', 0)}</td></tr>
</table>

<h2>Field-Level Coverage</h2>
<table>
<tr><th>Field</th><th>Coverage</th></tr>
<tr><td>Live price</td><td>{scan_result.coverage.get('withLivePrice', 0)}/{universe} ({100*scan_result.coverage.get('withLivePrice', 0)/universe:.0f}%)</td></tr>
<tr><td>52-week range</td><td>{with_52wk}/{universe} ({100*with_52wk/universe:.0f}%)</td></tr>
<tr><td>Current volume</td><td>{with_volume}/{universe} ({100*with_volume/universe:.0f}%)</td></tr>
<tr><td>Average volume</td><td>{with_avg_volume}/{universe} ({100*with_avg_volume/universe:.0f}%)</td></tr>
<tr><td>Analyst/broker target</td><td>{with_analyst}/{universe} ({100*with_analyst/universe:.0f}%)</td></tr>
<tr><td>Same-day news checked</td><td>{with_news}/{universe} ({100*with_news/universe:.0f}%)</td></tr>
<tr><td>Fundamentals (market cap)</td><td>{with_fundamentals}/{universe} ({100*with_fundamentals/universe:.0f}%)</td></tr>
<tr><td>Quality / Growth / Valuation / Timing</td><td>0/{universe} — <b>no connected data source</b> (see STATUS.md)</td></tr>
</table>

<h2>Data Freshness (across all available component values)</h2>
<table>
<tr><th>Status</th><th>Count</th></tr>
<tr><td>🟢 Fresh</td><td>{freshness_counts.get('fresh', 0)}</td></tr>
<tr><td>🟢 Recent</td><td>{freshness_counts.get('recent', 0)}</td></tr>
<tr><td>🟡 Stale</td><td>{freshness_counts.get('stale', 0)}</td></tr>
<tr><td>🔴 Expired</td><td>{freshness_counts.get('expired', 0)}</td></tr>
</table>

<h2>Provider Health</h2>
<table>
<tr><th>Provider</th><th>Status</th><th>Requests</th><th>Error Rate</th><th>Last Success</th><th>Last Failure</th></tr>
{"".join(f'<tr><td>{name}</td><td>{rec["status"]}</td><td>{rec["totalRequests"]}</td><td>{rec["errorRate"]:.0%}</td><td>{rec.get("lastSuccessAt") or "—"}</td><td>{rec.get("lastFailureAt") or "—"}</td></tr>' for name, rec in scan_result.provider_health.items())}
</table>

<h2>AI Layer Status</h2>
<table>
<tr><th>Metric</th><th>Value</th></tr>
<tr><td>API key configured</td><td>{"Yes" if ai_status.get("hasApiKey") else "No"}</td></tr>
<tr><td>AI Evidence Analysis candidates this run</td><td>{ai_status.get("evidenceCandidates", 0)}</td></tr>
<tr><td>AI Evidence Analysis succeeded</td><td>{ai_status.get("evidenceSucceeded", 0)}</td></tr>
<tr><td>Bear Agent candidates this run (80+ scores)</td><td>{ai_status.get("bearCandidates", 0)}</td></tr>
<tr><td>Bear Agent succeeded</td><td>{ai_status.get("bearSucceeded", 0)}</td></tr>
{f'<tr><td>Step error</td><td>{ai_status["stepError"]}</td></tr>' if ai_status.get("stepError") else ""}
</table>

<h2>Daily Self-Audit</h2>
<p class="meta">Last scan: {scan_result.ran_at}</p>
<ul>
<li>{scan_result.coverage.get('scoreable', 0)} of {universe} stocks scoreable</li>
<li>{len(scan_result.tier_90_plus())} opportunities at 90+, {len(scan_result.tier_80_89())} at 80-89, {len(scan_result.tier_70_79())} at 70-79</li>
<li>Analyst data refreshed for {scan_result.coverage.get('analystRefreshedThisRun', 0)} of {scan_result.coverage.get('analystRefreshCapPerRun', 0)} cap this run</li>
</ul>
"""
    return render_standalone_page_fn("data-quality.html", "Data Quality", "🔍 Scanner Data Quality", content, docs_dir)
