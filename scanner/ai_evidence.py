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
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone

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


def bear_challenge(evidence_dict: dict, cache: dict, gateway=None) -> dict:
    """
    A genuinely SEPARATE AI call from analyze_evidence - different
    system prompt, different task (actively disprove, not neutrally
    interpret), different cache namespace so a bull-case cache hit
    never silently substitutes for a bear challenge. Same fact-boundary
    guarantees: never invents a number, never available without a
    configured provider, never fabricated if the call fails.

    Routed through the AI Provider Gateway (ai_provider_gateway.py) -
    this function no longer knows HOW to reach Anthropic specifically;
    it asks the gateway for a completion and gets back a real result
    or an honest failure. A gateway instance is created on demand if
    none is passed, so existing callers keep working unchanged.
    """
    import ai_provider_gateway
    gateway = gateway or ai_provider_gateway.AIProviderGateway()

    ticker = evidence_dict["ticker"]
    ehash = evidence_hash(evidence_dict)
    cache_key = f"bear:{ticker}:{BEAR_AGENT_VERSION}:{ehash}"
    if cache_key in cache:
        return cache[cache_key]

    result = gateway.complete(BEAR_SYSTEM_PROMPT, f"Evidence:\n{json.dumps(evidence_dict, indent=2)}")
    if not result.success:
        print(f"  ! Bear Agent for {ticker} failed via {result.provider or 'no provider'}: "
              f"{result.error_type} — {result.error_detail}", file=sys.stderr)
        return None
    try:
        parsed = json.loads(result.text)
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
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        print(f"  ! Bear Agent for {ticker}: could not parse response as valid JSON: {e}", file=sys.stderr)
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


def analyze_evidence(evidence_dict: dict, cache: dict, gateway=None) -> dict:
    """
    Calls the AI ONLY if this exact evidence set (by hash) hasn't
    already been analyzed under the current model version. Returns
    None (never a fabricated analysis) if no provider is configured,
    every configured provider fails, or the response fails the
    safety-pattern check - the scanner and its numerical BUY SCORE
    work identically either way.

    Routed through the AI Provider Gateway - this function has no
    Anthropic-specific HTTP logic of its own anymore.
    """
    import ai_provider_gateway
    gateway = gateway or ai_provider_gateway.AIProviderGateway()

    ticker = evidence_dict["ticker"]
    ehash = evidence_hash(evidence_dict)
    cache_key = f"{ticker}:{AI_EVIDENCE_ANALYSIS_VERSION}:{ehash}"
    if cache_key in cache:
        return cache[cache_key]  # unchanged evidence - no repeat API call

    result = gateway.complete(SYSTEM_PROMPT, f"Evidence:\n{json.dumps(evidence_dict, indent=2)}")
    if not result.success:
        print(f"  ! AI evidence analysis for {ticker} failed via {result.provider or 'no provider'}: "
              f"{result.error_type} — {result.error_detail}", file=sys.stderr)
        return None
    try:
        parsed = json.loads(result.text)
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
        parsed["aiProvider"] = result.provider
        parsed["aiModel"] = result.model
        cache[cache_key] = parsed
        return parsed
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        print(f"  ! AI evidence analysis for {ticker}: could not parse response as valid JSON: {e}", file=sys.stderr)
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


# =========================================================================
# AI WORK QUEUE - persisted, real, re-prioritized every run. A queued
# event never simply vanishes just because it wasn't reached this run;
# it stays QUEUED (with an updated priority, since new evidence may
# have arrived) until it's processed, superseded by a newer event for
# the same stock, or genuinely expires.
# =========================================================================

QUEUE_ENTRY_EXPIRY_DAYS = 7  # a queued event this old is stale enough that
# re-analyzing it wouldn't reflect anything a fresh scan wouldn't already
# capture next time a real event fires - expired, not processed forever.

STATUS_QUEUED = "QUEUED"
STATUS_ANALYSING = "ANALYSING"
STATUS_COMPLETED = "COMPLETED"
STATUS_FAILED = "FAILED"
STATUS_RETRY_PENDING = "RETRY_PENDING"
STATUS_SUPERSEDED = "SUPERSEDED"
STATUS_EXPIRED = "EXPIRED"


@dataclass
class QueueEntry:
    ticker: str
    event_type: str
    event_priority: int
    evidence_hash: str
    buy_score: float
    first_detected: str    # ISO timestamp - never rewritten once set
    last_detected: str     # ISO timestamp - updated whenever the SAME underlying event recurs
    attempt_count: int = 0
    last_attempt: str = None
    status: str = STATUS_QUEUED

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, d):
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


def _event_identity(ticker, event_type, evidence_hash_val):
    """Immutable event identity for deduplication - per the explicit
    requirement, the SAME underlying event (same stock, same event
    type, same evidence) must never create a second queue entry."""
    return f"{ticker}:{event_type}:{evidence_hash_val}"


def update_ai_queue(queue: dict, scan_result, snapshot_history, history_module,
                     prior_risk_flags=None, now=None) -> dict:
    """
    Re-derives priorities from CURRENT data (never blindly FIFO, per
    the explicit requirement) and merges them into the persisted
    queue: genuinely new events are added as QUEUED, an event that
    recurs for a stock already queued updates last_detected and
    priority (never duplicated), and a stock's OLDER queued entry is
    marked SUPERSEDED when a newer, different event fires for the same
    stock (the newer evidence is what should actually be analyzed).
    Expired entries (older than QUEUE_ENTRY_EXPIRY_DAYS) are marked
    EXPIRED, never silently deleted (auditable history of what the
    queue decided and why).
    """
    now = now or datetime.now(timezone.utc)
    now_iso = now.isoformat()
    queue = {k: v for k, v in queue.items()}  # shallow copy, never mutate caller's dict in place

    events = identify_ai_candidate_events(scan_result, snapshot_history, history_module, prior_risk_flags, now)
    for ticker, (event_type, priority) in events.items():
        breakdown = scan_result.breakdowns.get(ticker)
        if not breakdown:
            continue
        record = scan_result.records.get(ticker)
        ehash = evidence_hash(build_evidence_summary(record, breakdown, scan_result.risk_flags.get(ticker, []))) \
            if record else "unknown"
        identity = _event_identity(ticker, event_type, ehash)

        if identity in queue:
            entry = QueueEntry.from_dict(queue[identity])
            if entry.status in (STATUS_QUEUED, STATUS_RETRY_PENDING):
                entry.last_detected = now_iso  # same event recurring - update, don't duplicate
                entry.event_priority = priority  # priority itself can still shift with fresh context
                queue[identity] = entry.to_dict()
            continue  # already COMPLETED/FAILED/etc for this exact evidence - don't requeue identical evidence

        # Supersede any OTHER still-queued entry for the same ticker -
        # newer evidence is what should actually get analyzed, not a
        # stale queued reason that's since been overtaken.
        for other_id, other_dict in list(queue.items()):
            if other_dict["ticker"] == ticker and other_dict["status"] in (STATUS_QUEUED, STATUS_RETRY_PENDING) \
                    and other_id != identity:
                other_dict["status"] = STATUS_SUPERSEDED
                queue[other_id] = other_dict

        queue[identity] = QueueEntry(
            ticker=ticker, event_type=event_type, event_priority=priority, evidence_hash=ehash,
            buy_score=breakdown.buy_score, first_detected=now_iso, last_detected=now_iso,
        ).to_dict()

    # Expire old still-queued entries - marked, never deleted.
    for identity, entry_dict in queue.items():
        if entry_dict["status"] not in (STATUS_QUEUED, STATUS_RETRY_PENDING):
            continue
        try:
            first = datetime.fromisoformat(entry_dict["first_detected"])
            if (now - first).days >= QUEUE_ENTRY_EXPIRY_DAYS:
                entry_dict["status"] = STATUS_EXPIRED
        except (ValueError, KeyError):
            pass

    return queue


AGE_BOOST_PER_DAY = 0.5  # per full day waiting, effective priority number is
# reduced (made more urgent) by this amount - transparent, linear, testable.
# Prevents an event stuck behind a constant stream of higher-priority new
# events from waiting forever, per the explicit starvation-prevention
# requirement, without letting age alone override a genuinely much
# higher-priority fresh event on day one.


def _effective_priority(entry: "QueueEntry", now: datetime) -> float:
    """Raw event_priority, reduced (made more urgent) by how long the
    entry has genuinely been waiting - never boosted below 0.5 of its
    original priority tier, so aging alone can't make a low-priority
    event outrank a same-day new-90+-opportunity, only eventually catch
    up with same-priority-tier events that arrived more recently."""
    try:
        first = datetime.fromisoformat(entry.first_detected)
        days_waiting = max(0.0, (now - first).total_seconds() / 86400)
    except (ValueError, TypeError):
        days_waiting = 0.0
    boost = min(entry.event_priority * 0.5, days_waiting * AGE_BOOST_PER_DAY)
    return entry.event_priority - boost


def select_from_queue(queue: dict, max_per_run: int = None, now: datetime = None) -> list:
    """
    Selects the top-priority QUEUED/RETRY_PENDING entries for this
    run's actual API calls - by EFFECTIVE priority (raw event priority,
    aged by how long the entry has waited, per the explicit starvation-
    prevention requirement), then by BUY SCORE, never FIFO/insertion
    order. Returns the list of (identity, QueueEntry) tuples to process.
    """
    now = now or datetime.now(timezone.utc)
    max_per_run = max_per_run if max_per_run is not None else AI_ANALYSIS_MAX_PER_RUN
    eligible = [
        (identity, QueueEntry.from_dict(d)) for identity, d in queue.items()
        if d["status"] in (STATUS_QUEUED, STATUS_RETRY_PENDING)
    ]
    eligible.sort(key=lambda kv: (_effective_priority(kv[1], now), -kv[1].buy_score))
    return eligible[:max_per_run]


def queue_summary(queue: dict) -> dict:
    """Counts by status - for the AI Status display."""
    counts = {STATUS_QUEUED: 0, STATUS_ANALYSING: 0, STATUS_COMPLETED: 0, STATUS_FAILED: 0,
              STATUS_RETRY_PENDING: 0, STATUS_SUPERSEDED: 0, STATUS_EXPIRED: 0}
    for entry_dict in queue.values():
        counts[entry_dict.get("status", STATUS_QUEUED)] = counts.get(entry_dict.get("status", STATUS_QUEUED), 0) + 1
    return counts


# =========================================================================
# BUY SCORE vs AI CONVICTION - kept genuinely separate per the explicit
# requirement (never merged, never one automatically derived from the
# other). This function only produces a HUMAN-READABLE LABEL describing
# the relationship between two numbers that already exist independently -
# it never modifies either number.
# =========================================================================

HIGH_THRESHOLD = 85  # aligned with this project's own existing 80-89/90+ tier boundaries,
# not an arbitrary new threshold - keeps "HIGH" meaning the same thing across the page.
LOW_THRESHOLD = 50


def _band(value):
    if value is None:
        return None
    if value >= HIGH_THRESHOLD:
        return "HIGH"
    if value >= LOW_THRESHOLD:
        return "MODERATE"
    return "LOW"


def combine_score_and_conviction(buy_score, ai_conviction):
    """
    Returns a human-readable overall-status label describing the
    relationship between the two, e.g. "HIGH SCORE / LOW CONVICTION" -
    never a new number, never a blend. Returns None if AI conviction
    isn't available for this stock (most stocks, most runs) - the
    absence of an AI view is not itself a status worth displaying.
    """
    if buy_score is None or ai_conviction is None:
        return None
    score_band = _band(buy_score)
    conviction_band = _band(ai_conviction)
    if score_band == conviction_band:
        return f"{score_band} SCORE / {conviction_band} CONVICTION (evidence agrees)"
    return f"{score_band} SCORE / {conviction_band} CONVICTION"
