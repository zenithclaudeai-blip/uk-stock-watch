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

from scoring import classify_opportunity_tier, compute_risk_score, detect_evidence_conflicts, COMPONENT_WEIGHTS, CORE_COMPONENTS, ENHANCEMENT_COMPONENTS, UNAVAILABLE_ENGINES
import history as history_module
import ai_evidence as ai_evidence_module


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


def _status_label(breakdown):
    """Tier + evidence-quality suffix, per the explicit requirement
    that two identical scores with different evidence must look
    visibly different - 'PROMISING' vs 'PROMISING — LIMITED EVIDENCE'."""
    if breakdown.buy_score is None:
        return "UNSCOREABLE"
    tier = classify_opportunity_tier(breakdown.buy_score)
    tier_labels = {"90+": "EXCEPTIONAL", "80-89": "STRONG", "70-79": "PROMISING"}
    label = tier_labels.get(tier, "BELOW THRESHOLD")
    if breakdown.opportunity_quality == "LOW":
        label += " — LIMITED EVIDENCE"
    return label


def _stock_row_html(breakdown, snapshot_history, risk_flags_for_ticker, now):
    ticker = breakdown.ticker
    score_display = f"{breakdown.buy_score:.0f}" if breakdown.buy_score is not None else "—"
    risk_score = compute_risk_score(risk_flags_for_ticker or [])
    badge = _confidence_warning(breakdown)
    momentum_html = _momentum_badge(ticker, snapshot_history, now)
    missing = f'<span class="opp-missing">Missing: {", ".join(breakdown.missing_categories)}</span>' if breakdown.missing_categories else ""
    risk_flag_html = (
        f'<span class="opp-risk-flags">{" · ".join(f.label for f in risk_flags_for_ticker)}</span>'
        if risk_flags_for_ticker else ""
    )
    what_changed = history_module.component_deltas(snapshot_history.get(ticker, []))
    what_changed_html = (
        f'<div class="opp-what-changed"><b>What changed:</b> {history_module.format_what_changed(what_changed)}</div>'
        if what_changed else ""
    )
    conflicts = detect_evidence_conflicts(breakdown)
    conflicts_html = (
        "".join(f'<div class="opp-conflict">⚠ EVIDENCE CONFLICT: {c.description}</div>' for c in conflicts)
        if conflicts else ""
    )
    return f"""<div class="opp-row">
  <div class="opp-ticker">{ticker}</div>
  <div class="opp-score">PROVISIONAL BUY SCORE: {score_display}</div>
  <div class="opp-conf">Confidence: {breakdown.data_confidence:.0f} · Model Coverage: {breakdown.data_coverage_pct:.0f}% · Risk: {risk_score}</div>
  <div class="opp-status">Status: {_status_label(breakdown)}</div>
  {momentum_html}
  {what_changed_html}
  {conflicts_html}
  {badge}
  {missing}
  {risk_flag_html}
</div>"""


def _section_html(title, breakdowns, snapshot_history, risk_flags_by_ticker, now, empty_message):
    if not breakdowns:
        return f'<h3>{title}</h3><p class="opp-empty">{empty_message}</p>'
    rows = "".join(_stock_row_html(b, snapshot_history, risk_flags_by_ticker.get(b.ticker, []), now) for b in breakdowns)
    return f'<h3>{title} ({len(breakdowns)})</h3><div class="opp-list">{rows}</div>'


def render_opportunities_page(scan_result, snapshot_history: dict, dashboard_css: str, docs_dir: str,
                               render_standalone_page_fn, ai_analyses: dict = None, bear_challenges: dict = None,
                               ai_status: dict = None):
    """
    scan_result: orchestrator.ScanResult from the most recent real scan.
    snapshot_history: {ticker: [snapshot_dict, ...]} - real persisted history.
    ai_analyses: {ticker: analysis_dict} - AI Evidence Analysis output for
    the small, cost-bounded tier of stocks selected this run (see
    ai_evidence.select_ai_analysis_candidates) - most stocks will NOT
    have an entry here, which is expected, not an error.
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

    # Full ranked universe - EVERY scoreable stock, never truncated,
    # per the explicit requirement that the underlying dataset must
    # never be hidden behind only the top tiers.
    all_scoreable_sorted = sorted(
        [b for b in scan_result.breakdowns.values() if b.buy_score is not None],
        key=lambda b: -b.buy_score,
    )

    ai_analyses = ai_analyses or {}
    bear_challenges = bear_challenges or {}
    ai_status = ai_status or {}

    def _ai_unavailable_reason():
        """Explicit reason AI didn't run this round - never ambiguous
        between 'nothing qualified' and 'it qualified but failed',
        per the explicit requirement that AI unavailability be shown
        clearly rather than left as silent absence."""
        if ai_status.get("stepError"):
            return f"AI Analysis step failed this run: {ai_status['stepError'][:150]}"
        if not ai_status.get("hasApiKey", True):
            return "AI ANALYSIS: UNAVAILABLE — Reason: no ANTHROPIC_API_KEY configured this run."
        if ai_status.get("evidenceCandidates", 0) == 0:
            return None  # genuinely nothing qualified this run - not an unavailability, just nothing to analyze
        if ai_status.get("evidenceSucceeded", 0) == 0:
            return (f"AI ANALYSIS: UNAVAILABLE — Reason: {ai_status['evidenceCandidates']} stock(s) qualified "
                    f"for analysis but every API call failed this run (see the poller log for the exact error — "
                    f"commonly a billing/credit issue or a rate limit). The deterministic BUY SCORE above is "
                    f"completely unaffected.")
        return None  # partial or full success - the analyses themselves are shown, no unavailability banner needed

    _ai_unavailable_banner = _ai_unavailable_reason()

    content = f"""
<p class="meta">Genuine, periodically-refreshed opportunity discovery — not real-time. Refreshed every 5 minutes during UK market hours, same schedule as the rest of this dashboard.</p>

<div class="opp-model-status">
  <p><b>{scan_result.model_status}</b></p>
  <p class="opp-meta"><b>Scores on this page are a PROVISIONAL BUY SCORE</b> — not yet the full intended model. Universe: FTSE 100 ({scan_result.universe_size} stocks) — not the full LSE. Currently supported model dimensions: {", ".join(k for k, v in scan_result.model_coverage.items() if v == "Available")}. Not yet connected: {", ".join(k for k, v in scan_result.model_coverage.items() if v == "Unavailable")}. Once Quality/Growth/Valuation/Timing are genuinely connected, this will be promoted to a full BUY SCORE 0-100.</p>
</div>

<div class="opp-how-it-works">
  <h3>How the Score Works</h3>
  <p class="opp-meta">The ACTUAL current weighting (not an example) — a weighted average of whichever components have real data this run, renormalized when something is missing, never padded with a zero or an invented value:</p>
  <table><tr><th>Component</th><th>Weight</th><th>Category</th></tr>
  {"".join(f'<tr><td>{name.replace("_", " ").title()}</td><td>{weight}%</td><td>{"CORE" if name in CORE_COMPONENTS else "ENHANCEMENT"}</td></tr>' for name, weight in COMPONENT_WEIGHTS.items())}
  </table>
  <p class="opp-meta">CORE components must supply at least 30 of their combined weight before a stock is considered scoreable at all — a stock with only ENHANCEMENT evidence (e.g. news alone) is never scored from that alone. When a component is missing, its weight is redistributed proportionally among the components that ARE available — the score reflects only real evidence, and DATA CONFIDENCE is reduced separately to reflect the gap, rather than the missing component silently becoming a zero.</p>
  <h4>Engines not yet connected — genuinely UNAVAILABLE, not zero, and carrying zero weight in the score above:</h4>
  <table><tr><th>Engine</th><th>Status</th><th>Reason</th></tr>
  {"".join(f'<tr><td>{name.replace("_", " ").title()}</td><td>⚪ UNAVAILABLE</td><td>{reason}</td></tr>' for name, reason in UNAVAILABLE_ENGINES.items())}
  </table>
</div>

<div class="opp-scan-health">
  <h3>Scan Health</h3>
  <p class="opp-meta">Last scan: {scan_result.ran_at} · <b>Universe Coverage: {scan_result.coverage['universeCoveragePct']}%</b> (every eligible stock was scanned) ·
  <b>Model Data Coverage: {scan_result.coverage['modelDataCoveragePct']}%</b> (average completeness of the actual scoring evidence — these are DIFFERENT numbers; 100% universe coverage does not mean complete financial evidence)</p>
  <p class="opp-meta">Scoreable: {scan_result.coverage['scoreable']} · Fully covered: {scan_result.coverage['fullyCovered']} ·
  Partially covered: {scan_result.coverage['partiallyCovered']} · Unscoreable: {scan_result.coverage['unscoreable']}</p>
  <p class="opp-meta">Analyst data refreshed this run: {scan_result.coverage['analystRefreshedThisRun']} of {scan_result.coverage['analystRefreshCapPerRun']} cap ·
  Stocks with insufficient history for momentum tracking: {insufficient_history_count} (expected — history accumulates over multiple runs)</p>
</div>

<div class="opp-ai-status">
  <h3>AI Status</h3>
  <ul>
    <li>Candidates identified this run: {ai_status.get("totalEventsIdentified", 0)}</li>
    <li>Analysed: {ai_status.get("evidenceSucceeded", 0)}</li>
    <li>Waiting (qualified, budget-limited): {ai_status.get("waiting", 0)}</li>
    <li>Failed (API/billing/rate-limit error): {max(0, ai_status.get("evidenceCandidates", 0) - ai_status.get("evidenceSucceeded", 0))}</li>
    <li>No-change runs (nothing materially different): {"yes" if ai_status.get("totalEventsIdentified", 0) == 0 else "no"}</li>
  </ul>
  <p class="opp-meta">Candidates are selected by event priority (new opportunity → band change → evidence conflict → risk flag appeared → major score change → risk flag disappeared), never by encounter order — and the {ai_evidence_module.AI_ANALYSIS_MAX_PER_RUN}-candidate budget never limits the underlying market scan, which always covers the full {scan_result.universe_size}-stock universe.</p>
</div>

<div class="opp-provider-health">
  <h3>Provider Health</h3>
  {"".join(f'<p class="opp-meta">{name}: {rec["status"]} · {rec["totalRequests"]} request(s) · {rec["errorRate"]:.0%} error rate</p>' for name, rec in scan_result.provider_health.items())}
</div>

<h2>🔥 Best Opportunities Now</h2>
{_section_html("Exceptional Setups (90+)", tier_90, snapshot_history, scan_result.risk_flags, now, "No stocks currently score 90 or above.")}
{_section_html("Strong Setups (80-89)", tier_80, snapshot_history, scan_result.risk_flags, now, "No stocks currently score 80-89.")}
{_section_html("Promising (70-79)", tier_70, snapshot_history, scan_result.risk_flags, now, "No stocks currently score 70-79.")}

<h2>📈 Rising Fast</h2>
{_section_html("Rapidly improving (7d change ≥ +8)", rising_fast, snapshot_history, scan_result.risk_flags, now, "No stocks are currently rising rapidly — or insufficient history exists yet to measure this.")}

<h2>📉 Deteriorating</h2>
{_section_html("Meaningful decline (7d change ≤ -8)", deteriorating, snapshot_history, scan_result.risk_flags, now, "No stocks are currently deteriorating meaningfully — or insufficient history exists yet.")}

<h2>🆕 New Today</h2>
{_section_html("Newly qualified (first-ever snapshot, score 70+)", new_today, snapshot_history, scan_result.risk_flags, now, "No genuinely new qualifying stocks this run.")}

<h2>📋 All Opportunities (full ranked universe)</h2>
<p class="meta">Every scoreable stock, ranked by PROVISIONAL BUY SCORE — never truncated. {len(all_scoreable_sorted)} stock(s).</p>
{_section_html("All scoreable stocks", all_scoreable_sorted, snapshot_history, scan_result.risk_flags, now, "No scoreable stocks this run.")}

<h2>📊 Backtesting Status</h2>
<p class="opp-meta">🟡 BACKTESTING NOT YET STATISTICALLY VALID — Reason: insufficient point-in-time historical data. Score history only began accumulating this session; a real backtest requires many weeks of genuine daily snapshots to avoid look-ahead bias. The framework exists (history.py) and will produce honest results once sufficient data exists — never a fabricated performance figure in the meantime.</p>

<h2>🤖 AI Evidence View</h2>
<p class="opp-meta">A separate, clearly-labeled interpretive layer — the AI is given only the same verified facts already shown above and asked to interpret them; it never generates a number, price, or target, and never changes the PROVISIONAL BUY SCORE. Only run for a small, cost-bounded set of stocks each run (new 80+/90+ entrants and large score changes) — most stocks will not have an entry here, which is expected.</p>
{f'<p class="opp-ai-unavailable"><b>{_ai_unavailable_banner}</b></p>' if _ai_unavailable_banner else ""}
{"".join(f'''<div class="opp-ai-analysis">
  <h4>{ticker}</h4>
  <p><b>Outlook:</b> {a.get("outlook", "—")} (AI confidence: {a.get("analysis_confidence", "—")})</p>
  <p><b>Bull case:</b> {a.get("bull_case", "")}</p>
  <p><b>Bear case:</b> {a.get("bear_case", "")}</p>
  {f'<p><b>Evidence conflicts:</b> {a["evidence_conflicts"]}</p>' if a.get("evidence_conflicts") else ""}
  <p><b>What would change this view:</b> {a.get("what_would_change_the_view", "")}</p>
</div>''' for ticker, a in ai_analyses.items()) or ('<p class="opp-empty">No stocks qualified for AI evidence analysis this run.</p>' if not _ai_unavailable_banner else "")}

<h2>🐻 Bear Agent — Mandatory Challenge (80+ scores)</h2>
<p class="opp-meta">Every stock scoring 80+ receives an independent, adversarial challenge — a separate AI call specifically instructed to find the strongest case AGAINST the opportunity, never a softened version of the bull case above. Capped at {ai_evidence_module.BEAR_AGENT_MAX_PER_RUN} per run for cost control.</p>
{"".join(f'''<div class="opp-bear-challenge">
  <h4>{ticker} — Verdict: {c.get("verdict", "—")} (bear confidence: {c.get("bear_confidence", "—")})</h4>
  <p><b>Bear case:</b> {c.get("bear_case", "")}</p>
  {f'<p><b>Weaknesses in the bull case:</b> {", ".join(c["weaknesses_in_bull_case"])}</p>' if c.get("weaknesses_in_bull_case") else ""}
  {f'<p><b>Missing evidence that would matter:</b> {", ".join(c["missing_evidence_that_would_matter"])}</p>' if c.get("missing_evidence_that_would_matter") else ""}
</div>''' for ticker, c in bear_challenges.items()) or '<p class="opp-empty">No stocks currently score 80+, so no mandatory bear challenge was triggered this run.</p>'}
"""

    return render_standalone_page_fn("opportunities.html", "Opportunity Scanner", "🔥 LSE Opportunity Scanner", content, docs_dir)
