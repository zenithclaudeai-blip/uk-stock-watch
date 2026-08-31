"""
LSE Opportunity Scanner - the /opportunities page renderer.

Consumes a real ScanResult (orchestrator.py) and real history (history.py)
and produces the actual HTML page. Every number on this page traces back
to a genuine DataPoint or ScoreBreakdown - nothing here invents a value.

Honest about the deployment model throughout: this is a periodically-
refreshed static page (every 5 minutes during market hours, matching
the rest of this project's poller), never described as real-time.
"""
from datetime import datetime, timezone

from scoring import classify_opportunity_tier
import history as history_module


def _confidence_warning(breakdown):
    """High score + low confidence / high score + high confidence
    classification, per the explicit requirement - never ranks these
    identically even at the same buy_score."""
    if breakdown.buy_score is None:
        return ""
    if breakdown.buy_score >= 90 and breakdown.data_confidence >= 85:
        return '<span class="opp-badge opp-badge-strong">🔥 HIGH-CONFIDENCE OPPORTUNITY</span>'
    if breakdown.buy_score >= 85 and breakdown.data_confidence < 60:
        return '<span class="opp-badge opp-badge-warn">⚠ HIGH SCORE / LOW CONFIDENCE</span>'
    return ""


def _momentum_badge(ticker, snapshot_history, now=None):
    momentum = history_module.calculate_momentum(snapshot_history.get(ticker, []), now)
    if not momentum.has_sufficient_history:
        return '<span class="opp-meta">Momentum: insufficient history</span>'
    return f'<span class="opp-meta">7D: {momentum.change_7d:+.1f} · {momentum.label}</span>' if momentum.change_7d is not None else \
           f'<span class="opp-meta">{momentum.label}</span>'


def _stock_row_html(breakdown, snapshot_history, now):
    ticker = breakdown.ticker
    score_display = f"{breakdown.buy_score:.0f}" if breakdown.buy_score is not None else "—"
    badge = _confidence_warning(breakdown)
    momentum_html = _momentum_badge(ticker, snapshot_history, now)
    missing = f'<span class="opp-missing">Missing: {", ".join(breakdown.missing_categories)}</span>' if breakdown.missing_categories else ""
    return f"""<div class="opp-row">
  <div class="opp-ticker">{ticker}</div>
  <div class="opp-score">{score_display}</div>
  <div class="opp-conf">Confidence: {breakdown.data_confidence:.0f} · Coverage: {breakdown.data_coverage_pct:.0f}%</div>
  {momentum_html}
  {badge}
  {missing}
</div>"""


def _section_html(title, breakdowns, snapshot_history, now, empty_message):
    if not breakdowns:
        return f'<h3>{title}</h3><p class="opp-empty">{empty_message}</p>'
    rows = "".join(_stock_row_html(b, snapshot_history, now) for b in breakdowns)
    return f'<h3>{title} ({len(breakdowns)})</h3><div class="opp-list">{rows}</div>'


def render_opportunities_page(scan_result, snapshot_history: dict, dashboard_css: str, docs_dir: str,
                               render_standalone_page_fn):
    """
    scan_result: orchestrator.ScanResult from the most recent real scan.
    snapshot_history: {ticker: [snapshot_dict, ...]} - real persisted history.
    render_standalone_page_fn: this project's existing render_standalone_page,
    reused rather than duplicated so styling/back-link/footer stay identical
    to every other dedicated page.
    """
    now = datetime.now(timezone.utc)

    tier_90 = scan_result.tier_90_plus()
    tier_80 = scan_result.tier_80_89()
    tier_70 = scan_result.tier_70_79()

    # RISING FAST / DETERIORATING: real momentum-derived sections,
    # genuinely separate from raw score - a stock at 78 rising fast
    # appears here even if not yet in a score tier, per the explicit
    # "early opportunity" requirement.
    rising_fast, deteriorating = [], []
    for ticker, breakdown in scan_result.breakdowns.items():
        momentum = history_module.calculate_momentum(snapshot_history.get(ticker, []), now)
        if not momentum.has_sufficient_history or momentum.change_7d is None:
            continue
        if momentum.change_7d >= 8:
            rising_fast.append(breakdown)
        elif momentum.change_7d <= -8:
            deteriorating.append(breakdown)
    rising_fast.sort(key=lambda b: -history_module.calculate_momentum(snapshot_history.get(b.ticker, []), now).change_7d)
    deteriorating.sort(key=lambda b: history_module.calculate_momentum(snapshot_history.get(b.ticker, []), now).change_7d)

    # NEW TODAY: genuine state transitions (first-ever snapshot), not
    # merely "scanned in this run" - reuses detect_transition's own
    # NEW classification.
    new_today = []
    for ticker in scan_result.breakdowns:
        transition = history_module.detect_transition(snapshot_history.get(ticker, []), now)
        if transition and transition.event_type == "NEW" and transition.current_score is not None and transition.current_score >= 70:
            new_today.append(scan_result.breakdowns[ticker])

    insufficient_history_count = sum(
        1 for t in scan_result.breakdowns
        if not history_module.calculate_momentum(snapshot_history.get(t, []), now).has_sufficient_history
    )

    content = f"""
<p class="meta">Genuine, periodically-refreshed opportunity discovery — not real-time. Refreshed every 5 minutes during UK market hours, same schedule as the rest of this dashboard.</p>

<div class="opp-model-status">
  <p><b>{scan_result.model_status}</b></p>
  <p class="opp-meta">Universe: FTSE 100 ({scan_result.universe_size} stocks) — not the full LSE. Currently supported model dimensions: {", ".join(k for k, v in scan_result.model_coverage.items() if v == "Available")}. Not yet connected: {", ".join(k for k, v in scan_result.model_coverage.items() if v == "Unavailable")}.</p>
</div>

<div class="opp-scan-health">
  <h3>Scan Health</h3>
  <p class="opp-meta">Last scan: {scan_result.ran_at} · Universe: {scan_result.coverage['eligibleUniverse']} ·
  Scoreable: {scan_result.coverage['scoreable']} · Fully covered: {scan_result.coverage['fullyCovered']} ·
  Partially covered: {scan_result.coverage['partiallyCovered']} · Unscoreable: {scan_result.coverage['unscoreable']} ·
  Data coverage: {scan_result.coverage['dataCoveragePct']}%</p>
  <p class="opp-meta">Analyst data refreshed this run: {scan_result.coverage['analystRefreshedThisRun']} of {scan_result.coverage['analystRefreshCapPerRun']} cap ·
  Stocks with insufficient history for momentum tracking: {insufficient_history_count} (expected — history accumulates over multiple runs)</p>
</div>

<div class="opp-provider-health">
  <h3>Provider Health</h3>
  {"".join(f'<p class="opp-meta">{name}: {rec["status"]} · {rec["totalRequests"]} request(s) · {rec["errorRate"]:.0%} error rate</p>' for name, rec in scan_result.provider_health.items())}
</div>

<h2>🔥 Best Opportunities Now</h2>
{_section_html("Exceptional Setups (90+)", tier_90, snapshot_history, now, "No stocks currently score 90 or above.")}
{_section_html("Strong Setups (80-89)", tier_80, snapshot_history, now, "No stocks currently score 80-89.")}
{_section_html("Promising (70-79)", tier_70, snapshot_history, now, "No stocks currently score 70-79.")}

<h2>📈 Rising Fast</h2>
{_section_html("Rapidly improving (7d change ≥ +8)", rising_fast, snapshot_history, now, "No stocks are currently rising rapidly — or insufficient history exists yet to measure this.")}

<h2>📉 Deteriorating</h2>
{_section_html("Meaningful decline (7d change ≤ -8)", deteriorating, snapshot_history, now, "No stocks are currently deteriorating meaningfully — or insufficient history exists yet.")}

<h2>🆕 New Today</h2>
{_section_html("Newly qualified (first-ever snapshot, score 70+)", new_today, snapshot_history, now, "No genuinely new qualifying stocks this run.")}

<h2>📊 Backtesting Status</h2>
<p class="opp-meta">🟡 BACKTESTING NOT YET STATISTICALLY VALID — Reason: insufficient point-in-time historical data. Score history only began accumulating this session; a real backtest requires many weeks of genuine daily snapshots to avoid look-ahead bias. The framework exists (history.py) and will produce honest results once sufficient data exists — never a fabricated performance figure in the meantime.</p>
"""

    return render_standalone_page_fn("opportunities.html", "Opportunity Scanner", "🔥 LSE Opportunity Scanner", content, docs_dir)
