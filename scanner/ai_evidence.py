"""
LSE Opportunity Scanner - AI evidence analysis layer.

STRICT BOUNDARY: the AI is given already-verified facts and asked to
INTERPRET them - it never generates a number that feeds the score, a
price, a broker target, or any other fact. The deterministic scoring
engines (scoring.py) remain fully authoritative for the numerical BUY
SCORE; this module produces a SEPARATE, clearly-labeled "AI Evidence
View" that a reader can take or leave without it ever having touched
the reproducible score.

COST CONTROL: only called for a bounded, deliberately small tier of
stocks each run (new 80+/90+, large score changes) - never the whole
universe. Cached by a hash of the underlying evidence + model version,
so unchanged evidence never triggers a repeat API call.
"""
import hashlib
import json
import os
import re
import urllib.request
import urllib.error
import sys

AI_EVIDENCE_MODEL = "claude-haiku-4-5-20251001"  # cheap/fast tier - bounded structured task, not open-ended
AI_EVIDENCE_ANALYSIS_VERSION = "v1.0"

SYSTEM_PROMPT = """You are an evidence-interpretation assistant for a UK stock research tool.

You will be given a fixed set of ALREADY-VERIFIED facts about one stock - real prices, \
real broker targets, real news, real risk flags. You do NOT have web access and did not \
gather this evidence yourself.

Your ONLY job is to interpret the evidence you are given. You must NEVER:
- state a price, percentage, target, or any number that was not explicitly given to you
- invent a fact, statistic, catalyst, or event not present in the evidence
- predict a future price or claim certainty about future performance
- give investment advice or tell the reader to buy/sell

Respond with ONLY a JSON object with exactly these fields:
{
  "bull_case": "one or two sentences citing only the supplied evidence",
  "bear_case": "one or two sentences citing only the supplied evidence",
  "key_catalysts": ["short phrase", ...] (from the supplied evidence only, empty list if none),
  "key_risks": ["short phrase", ...] (from the supplied risk flags/evidence only),
  "evidence_conflicts": "a short description of any contradiction in the evidence, or empty string if none",
  "what_would_change_the_view": "one sentence - what NEW evidence would change this analysis",
  "outlook": "Bullish" or "Neutral" or "Bearish" (directional read of the SUPPLIED evidence only, never a price prediction),
  "analysis_confidence": integer 0-100 (how much of the evidence set is actually populated - low if most fields are missing)
}

No text outside the JSON object. No markdown formatting."""

# Guards against advice-shaped or fact-inventing language slipping
# through despite the system prompt - same defense-in-depth principle
# as this project's existing AI digest feature.
FORBIDDEN_PATTERNS = [
    r"\bI (?:recommend|suggest|believe you should)\b",
    r"\byou should (?:buy|sell|invest)\b",
    r"\bwill (?:rise|fall|reach|hit) £",
    r"\bguaranteed\b",
    r"\bcertain(?:ly)? (?:to|will)\b",
]


def evidence_hash(evidence_dict: dict) -> str:
    """Deterministic hash of the evidence actually shown to the AI -
    used for caching. Same evidence -> same hash -> no repeat call."""
    canonical = json.dumps(evidence_dict, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def build_evidence_summary(stock_record, breakdown, risk_flags) -> dict:
    """
    Extracts ONLY already-verified facts from the StockRecord/
    ScoreBreakdown/risk_flags already computed elsewhere - this
    function invents nothing, it just selects what to show the AI.
    Missing fields are simply omitted, never filled with a placeholder.
    """
    evidence = {"ticker": stock_record.identity.ticker}
    if stock_record.price.last_price.is_available:
        evidence["price"] = stock_record.price.last_price.value
    if stock_record.price.change_pct.is_available:
        evidence["changePctToday"] = stock_record.price.change_pct.value
    if stock_record.price.fifty_two_week_low.is_available:
        evidence["fiftyTwoWeekLow"] = stock_record.price.fifty_two_week_low.value
    if stock_record.price.fifty_two_week_high.is_available:
        evidence["fiftyTwoWeekHigh"] = stock_record.price.fifty_two_week_high.value
    if stock_record.valuation.target_mean_price.is_available:
        evidence["brokerTargetMean"] = stock_record.valuation.target_mean_price.value
    if stock_record.valuation.number_of_analyst_opinions.is_available:
        evidence["numberOfAnalysts"] = stock_record.valuation.number_of_analyst_opinions.value
    if stock_record.volume.volume.is_available and stock_record.volume.average_volume.is_available:
        evidence["volumeVsAverage"] = round(
            stock_record.volume.volume.value / stock_record.volume.average_volume.value, 2
        ) if stock_record.volume.average_volume.value else None
    if stock_record.news.item_count_today:
        evidence["sameDayNewsCount"] = stock_record.news.item_count_today
    evidence["riskFlags"] = [f.label for f in (risk_flags or [])]
    evidence["provisionalBuyScore"] = breakdown.buy_score
    evidence["dataConfidence"] = breakdown.data_confidence
    evidence["modelDataCoveragePct"] = breakdown.data_coverage_pct
    evidence["missingCategories"] = breakdown.missing_categories
    return evidence


def analyze_evidence(evidence_dict: dict, cache: dict) -> dict:
    """
    Calls the AI ONLY if this exact evidence set (by hash) hasn't
    already been analyzed under the current model version. Returns
    None (never a fabricated analysis) if no API key is configured,
    the call fails, or the response fails the safety-pattern check -
    the scanner and its numerical BUY SCORE work identically either
    way, exactly as this project's existing AI digest feature does.
    """
    ticker = evidence_dict["ticker"]
    ehash = evidence_hash(evidence_dict)
    cache_key = f"{ticker}:{AI_EVIDENCE_ANALYSIS_VERSION}:{ehash}"
    if cache_key in cache:
        return cache[cache_key]  # unchanged evidence - no repeat API call

    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return None

    body = json.dumps({
        "model": AI_EVIDENCE_MODEL,
        "max_tokens": 500,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": f"Evidence:\n{json.dumps(evidence_dict, indent=2)}"}],
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body,
        headers={"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
        method="POST",
    )
    try:
        resp_data = json.loads(urllib.request.urlopen(req, timeout=30).read())
        text = "".join(b.get("text", "") for b in resp_data.get("content", []) if b.get("type") == "text").strip()
        if not text:
            return None
        parsed = json.loads(text)
        required = {"bull_case", "bear_case", "key_catalysts", "key_risks", "evidence_conflicts",
                    "what_would_change_the_view", "outlook", "analysis_confidence"}
        if not required.issubset(parsed.keys()):
            print(f"  ! AI evidence analysis for {ticker}: response missing required fields, discarded", file=sys.stderr)
            return None
        full_text = json.dumps(parsed)
        if any(re.search(pat, full_text, re.IGNORECASE) for pat in FORBIDDEN_PATTERNS):
            print(f"  ! AI evidence analysis for {ticker} blocked: matched an advice/prediction pattern", file=sys.stderr)
            return None
        parsed["evidenceHash"] = ehash
        parsed["modelVersion"] = AI_EVIDENCE_ANALYSIS_VERSION
        cache[cache_key] = parsed
        return parsed
    except urllib.error.HTTPError as e:
        try:
            error_body = e.read().decode("utf-8", errors="replace")
        except Exception:
            error_body = "(couldn't read error body)"
        print(f"  ! AI evidence analysis for {ticker} failed: HTTP {e.code} — {error_body[:200]}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"  ! AI evidence analysis for {ticker} failed: {e}", file=sys.stderr)
        return None


# Deliberately small, bounded tier - per the explicit cost-control
# requirement, never the whole universe.
AI_ANALYSIS_MAX_PER_RUN = 5


def select_ai_analysis_candidates(scan_result, snapshot_history, history_module, now=None) -> list:
    """
    Selects which tickers get AI evidence analysis this run - new
    80+/90+ entrants and large score changes, per the explicit cost
    tier. Capped at AI_ANALYSIS_MAX_PER_RUN regardless of how many
    genuinely qualify, to keep API cost bounded and predictable.
    """
    candidates = []
    for ticker, breakdown in scan_result.breakdowns.items():
        if breakdown.buy_score is None:
            continue
        transition = history_module.detect_transition(snapshot_history.get(ticker, []), now)
        if transition and transition.event_type in ("NEW", "BAND_CHANGE") and breakdown.buy_score >= 80:
            candidates.append((ticker, breakdown.buy_score))
        elif transition and transition.event_type in ("IMPROVING", "DETERIORATING"):
            candidates.append((ticker, breakdown.buy_score))
    candidates.sort(key=lambda c: -c[1])
    return [t for t, _ in candidates[:AI_ANALYSIS_MAX_PER_RUN]]
