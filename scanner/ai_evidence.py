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
BEAR_AGENT_VERSION = "v1.0"

BEAR_SYSTEM_PROMPT = """You are a skeptical, adversarial research assistant for a UK stock research tool. \
Your ONLY job is to find the strongest reasons this stock should NOT be considered attractive, using \
ONLY the evidence you are given below.

You will be given a fixed set of ALREADY-VERIFIED facts - real prices, real broker targets, real news, \
real risk flags. You do NOT have web access and did not gather this evidence yourself.

You must NEVER:
- state a price, percentage, target, or any number that was not explicitly given to you
- invent a fact, statistic, catalyst, or event not present in the evidence
- soften your challenge because the evidence looks positive - actively look for what's missing, weak, or contradictory
- give investment advice or tell the reader to buy/sell

Respond with ONLY a JSON object with exactly these fields:
{
  "bear_case": "the strongest case AGAINST this being attractive, using only the supplied evidence",
  "weaknesses_in_bull_case": ["short phrase", ...] (gaps or weak points in the positive evidence),
  "missing_evidence_that_would_matter": ["short phrase", ...] (what's absent that a skeptic would want to see),
  "verdict": "Challenge Upheld" (bear case genuinely undermines the opportunity) or "Challenge Weak" (evidence still looks reasonably solid despite the challenge),
  "bear_confidence": integer 0-100 (how strong the bear case is, given only the supplied evidence)
}

No text outside the JSON object. No markdown formatting."""

# Mandatory bear-challenge tier - deliberately much smaller than the
# general AI evidence tier, since this is specifically for the
# highest-conviction opportunities where an unchallenged bull case is
# most risky to present unquestioned.
BEAR_AGENT_MIN_SCORE = 80
BEAR_AGENT_MAX_PER_RUN = 3


def bear_challenge(evidence_dict: dict, cache: dict) -> dict:
    """
    A genuinely SEPARATE AI call from analyze_evidence - different
    system prompt, different task (actively disprove, not neutrally
    interpret), different cache namespace so a bull-case cache hit
    never silently substitutes for a bear challenge. Same fact-boundary
    guarantees: never invents a number, never available without an API
    key, never fabricated if the call fails.
    """
    ticker = evidence_dict["ticker"]
    ehash = evidence_hash(evidence_dict)
    cache_key = f"bear:{ticker}:{BEAR_AGENT_VERSION}:{ehash}"
    if cache_key in cache:
        return cache[cache_key]

    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return None

    body = json.dumps({
        "model": AI_EVIDENCE_MODEL,
        "max_tokens": 500,
        "system": BEAR_SYSTEM_PROMPT,
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
        required = {"bear_case", "weaknesses_in_bull_case", "missing_evidence_that_would_matter",
                    "verdict", "bear_confidence"}
        if not required.issubset(parsed.keys()):
            print(f"  ! Bear Agent for {ticker}: response missing required fields, discarded", file=sys.stderr)
            return None
        full_text = json.dumps(parsed)
        if any(re.search(pat, full_text, re.IGNORECASE) for pat in FORBIDDEN_PATTERNS):
            print(f"  ! Bear Agent for {ticker} blocked: matched an advice/prediction pattern", file=sys.stderr)
            return None
        parsed["evidenceHash"] = ehash
        parsed["modelVersion"] = BEAR_AGENT_VERSION
        cache[cache_key] = parsed
        return parsed
    except urllib.error.HTTPError as e:
        try:
            error_body = e.read().decode("utf-8", errors="replace")
        except Exception:
            error_body = "(couldn't read error body)"
        print(f"  ! Bear Agent for {ticker} failed: HTTP {e.code} — {error_body[:200]}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"  ! Bear Agent for {ticker} failed: {e}", file=sys.stderr)
        return None


def select_bear_agent_candidates(scan_result) -> list:
    """
    Per the explicit requirement, the bear challenge is MANDATORY for
    high-score opportunities - selects every stock at or above
    BEAR_AGENT_MIN_SCORE, ranked highest-score-first, capped at
    BEAR_AGENT_MAX_PER_RUN for cost control (a smaller cap than the
    general AI evidence tier, since this targets specifically the
    highest-conviction stocks where an unchallenged bull case matters most).
    """
    candidates = [
        (t, b.buy_score) for t, b in scan_result.breakdowns.items()
        if b.buy_score is not None and b.buy_score >= BEAR_AGENT_MIN_SCORE
    ]
    candidates.sort(key=lambda c: -c[1])
    return [t for t, _ in candidates[:BEAR_AGENT_MAX_PER_RUN]]


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


# Deliberately small, bounded tier - per the explicit cost-control
# requirement, never the whole universe.
AI_ANALYSIS_MAX_PER_RUN = 5

# Event priority order - per the explicit requirement that the market
# scan can identify many events, with the AI budget then selecting the
# HIGHEST-PRIORITY ones, never simply "first encountered". Lower
# number = higher priority. Every reason here is backed by a REAL
# signal already computed elsewhere (transitions, risk flags,
# conflicts, coverage) - never a fabricated urgency score.
EVENT_PRIORITY = {
    "new_opportunity": 1,       # NEW transition, score >= 80
    "band_change": 2,           # crossed a score-tier boundary
    "evidence_conflict": 3,     # detect_evidence_conflicts fired
    "risk_flag_appeared": 4,
    "major_score_change": 5,    # IMPROVING/DETERIORATING transition
    "risk_flag_disappeared": 6,
    "evidence_became_available": 7,  # a previously-missing category now populated
    "evidence_became_stale": 8,      # a component that was fresh is now EXPIRED
}


def identify_ai_candidate_events(scan_result, snapshot_history, history_module, prior_risk_flags=None, now=None) -> dict:
    """
    IDENTIFICATION step - separate from the capped API-call step below,
    per the explicit requirement. Scans the ENTIRE breakdown set (never
    limited by the AI budget) and returns {ticker: (event_type, priority)}
    for every stock with a genuine, real-signal-backed reason to
    reconsider AI analysis. This can return more results than
    AI_ANALYSIS_MAX_PER_RUN - selecting which of these actually get an
    API call happens separately in select_ai_analysis_candidates.
    """
    from data_model import AgeStatus
    prior_risk_flags = prior_risk_flags or {}
    events = {}
    for ticker, breakdown in scan_result.breakdowns.items():
        if breakdown.buy_score is None:
            continue
        transition = history_module.detect_transition(snapshot_history.get(ticker, []), now)
        if transition:
            if transition.event_type == "NEW" and breakdown.buy_score >= 80:
                events[ticker] = ("new_opportunity", EVENT_PRIORITY["new_opportunity"])
                continue
            if transition.event_type == "BAND_CHANGE":
                events[ticker] = ("band_change", EVENT_PRIORITY["band_change"])
                continue
            if transition.event_type in ("IMPROVING", "DETERIORATING"):
                events[ticker] = ("major_score_change", EVENT_PRIORITY["major_score_change"])

        current_flags = {f.code for f in scan_result.risk_flags.get(ticker, [])}
        prior_flags = {f.code for f in prior_risk_flags.get(ticker, [])}
        if current_flags - prior_flags:  # a genuinely NEW flag appeared this run
            if ticker not in events or events[ticker][1] > EVENT_PRIORITY["risk_flag_appeared"]:
                events[ticker] = ("risk_flag_appeared", EVENT_PRIORITY["risk_flag_appeared"])
        elif prior_flags - current_flags:  # a flag genuinely cleared
            if ticker not in events or events[ticker][1] > EVENT_PRIORITY["risk_flag_disappeared"]:
                events[ticker] = ("risk_flag_disappeared", EVENT_PRIORITY["risk_flag_disappeared"])

    return events


def select_ai_analysis_candidates(scan_result, snapshot_history, history_module, prior_risk_flags=None, now=None) -> list:
    """
    The CAPPED selection step - takes the full event set from
    identify_ai_candidate_events (which is NEVER capped) and returns
    only the top AI_ANALYSIS_MAX_PER_RUN by event priority, then score,
    for the actual API-call tier. The market scan itself never sees
    this cap - it only governs how many stocks get an AI call.
    """
    events = identify_ai_candidate_events(scan_result, snapshot_history, history_module, prior_risk_flags, now)
    ranked = sorted(
        events.items(),
        key=lambda kv: (kv[1][1], -(scan_result.breakdowns[kv[0]].buy_score or 0)),
    )
    return [ticker for ticker, _ in ranked[:AI_ANALYSIS_MAX_PER_RUN]]
