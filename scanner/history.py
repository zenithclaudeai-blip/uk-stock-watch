"""
LSE Opportunity Scanner - historical snapshots and change detection.

Every function here that computes a TREND (momentum, velocity, band
transitions, rank change) explicitly checks whether enough real
snapshots exist FIRST, and returns an honest "insufficient history"
result rather than computing something misleading from 0 or 1 data
points. This project has ZERO historical snapshots before this
session's own scoring work - these functions will correctly report
that emptiness rather than papering over it, and only become
meaningful once genuine history accumulates run over run.

MODEL_VERSION is stamped onto every snapshot. If the scoring formula
in scoring.py changes later, bump this constant - existing stored
snapshots keep their ORIGINAL version forever; nothing here ever
silently recalculates history under a new formula.
"""
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from typing import Optional

MODEL_VERSION = "v1.0-provisional"

# Score-band boundaries, named per the spec - used for both tier
# flagging (already in scoring.py) and band-transition detection here.
SCORE_BANDS = [
    (90, 101, "Exceptional Setup"),
    (80, 90, "Strong Setup"),
    (70, 80, "Promising"),
    (0, 70, "Below Threshold"),
]


def score_band(score: Optional[float]) -> Optional[str]:
    if score is None:
        return None
    for lo, hi, label in SCORE_BANDS:
        if lo <= score < hi:
            return label
    return None


@dataclass
class ScoreSnapshot:
    """One stock's complete, reproducible score at one point in time -
    per the explicit requirement, stores enough to answer 'why did this
    stock score 82 on that date' without recalculating from today's
    data. component_scores is the same structure ScoreBreakdown.components
    produces, serialized plainly."""
    ticker: str
    timestamp: str  # ISO
    model_version: str
    buy_score: Optional[float]
    data_confidence: float
    data_coverage_pct: float
    coverage_state: str
    component_scores: dict  # {name: {"score": x, "weight": y, "available": bool, "explanation": str}}
    missing_categories: list

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_breakdown(cls, breakdown, timestamp=None):
        ts = (timestamp or datetime.now(timezone.utc)).isoformat()
        components = {
            c.name: {"score": c.score_0_100, "weight": c.weight, "available": c.available,
                      "category": c.category, "explanation": c.explanation}
            for c in breakdown.components
        }
        return cls(
            ticker=breakdown.ticker, timestamp=ts, model_version=MODEL_VERSION,
            buy_score=breakdown.buy_score, data_confidence=breakdown.data_confidence,
            data_coverage_pct=breakdown.data_coverage_pct, coverage_state=breakdown.coverage_state.value,
            component_scores=components, missing_categories=list(breakdown.missing_categories),
        )

    @classmethod
    def from_dict(cls, d):
        return cls(**d)


def append_snapshot(history: dict, snapshot: ScoreSnapshot, max_snapshots_per_ticker: int = 500) -> dict:
    """history is {ticker: [snapshot_dict, ...]}, newest last. Capped
    per ticker (a real, stated bound - not unlimited growth in a
    git-committed JSON file) - old snapshots beyond the cap are
    dropped from the OLD end, never the recent end."""
    history = dict(history)
    existing = list(history.get(snapshot.ticker, []))
    existing.append(snapshot.to_dict())
    if len(existing) > max_snapshots_per_ticker:
        existing = existing[-max_snapshots_per_ticker:]
    history[snapshot.ticker] = existing
    return history


def _snapshot_at_or_before(snapshots: list, cutoff: datetime) -> Optional[dict]:
    """The most recent REAL snapshot at or before cutoff - never
    interpolated, never fabricated. None if genuinely no snapshot
    exists that old."""
    candidates = [s for s in snapshots if datetime.fromisoformat(s["timestamp"]) <= cutoff]
    if not candidates:
        return None
    return max(candidates, key=lambda s: s["timestamp"])


@dataclass
class MomentumResult:
    """Explicitly distinguishes 'we calculated a real trend' from 'not
    enough history exists yet' - callers must check has_sufficient_history
    before trusting change_1d/3d/7d or label."""
    has_sufficient_history: bool
    change_1d: Optional[float] = None
    change_3d: Optional[float] = None
    change_7d: Optional[float] = None
    label: Optional[str] = None
    reason_if_insufficient: Optional[str] = None


MOMENTUM_LABELS = [
    (10, "🔥 RAPIDLY IMPROVING"),
    (4, "📈 IMPROVING"),
    (-4, "STABLE"),
    (-10, "📉 DETERIORATING"),
    (float("-inf"), "📉 RAPIDLY DETERIORATING"),
]


def calculate_momentum(history_for_ticker: list, now: datetime = None) -> MomentumResult:
    """
    Computes 1/3/7-day score change from REAL stored snapshots only.
    Requires at least 2 genuine snapshots spanning some real time gap -
    a single snapshot (this project's actual current state, before any
    history accumulates) correctly returns has_sufficient_history=False
    rather than inventing a trend from nothing.
    """
    now = now or datetime.now(timezone.utc)
    if not history_for_ticker or len(history_for_ticker) < 2:
        return MomentumResult(False, reason_if_insufficient=(
            "Only 0-1 snapshot(s) exist for this stock - momentum requires at least 2 "
            "genuine historical scores to compare, spanning real elapsed time."
        ))

    latest = max(history_for_ticker, key=lambda s: s["timestamp"])
    if latest.get("buy_score") is None:
        return MomentumResult(False, reason_if_insufficient="Latest snapshot is itself unscoreable.")

    def delta_at(days):
        cutoff = now - timedelta(days=days)
        prior = _snapshot_at_or_before(history_for_ticker, cutoff)
        if prior is None or prior.get("buy_score") is None:
            return None
        return round(latest["buy_score"] - prior["buy_score"], 1)

    change_1d = delta_at(1)
    change_3d = delta_at(3)
    change_7d = delta_at(7)

    # Label from whichever real window has the most data - prefer 7d,
    # fall back to shorter windows, never fabricate a window with no
    # real prior snapshot to compare against.
    reference_change = change_7d if change_7d is not None else (change_3d if change_3d is not None else change_1d)
    if reference_change is None:
        return MomentumResult(False, reason_if_insufficient=(
            "No prior snapshot exists within the last 7 days to compare against."
        ))

    label = next(lbl for threshold, lbl in MOMENTUM_LABELS if reference_change >= threshold)
    return MomentumResult(True, change_1d, change_3d, change_7d, label)


@dataclass
class LifecycleTransition:
    """A genuine state-transition event, derived from two real
    snapshots - never manually assigned. event_type is one of NEW,
    IMPROVING, STABLE, DETERIORATING, LOST, BAND_CHANGE."""
    ticker: str
    event_type: str
    previous_score: Optional[float]
    current_score: Optional[float]
    previous_band: Optional[str]
    current_band: Optional[str]
    priority: str  # CRITICAL, HIGH, MEDIUM, LOW
    description: str


# Minimum genuine score change to be considered meaningful - avoids
# alerting on 82.1 -> 82.3 rounding noise, per the explicit instruction.
MEANINGFUL_SCORE_CHANGE_THRESHOLD = 3.0


def component_deltas(history_for_ticker: list) -> Optional[dict]:
    """
    "What Changed" - compares the two most recent REAL snapshots'
    component_scores (already stored per-snapshot in ScoreSnapshot)
    and returns the actual per-component point changes. Never
    generates text that "sounds plausible" - every delta traces
    directly back to the two real stored snapshots being compared.
    Returns None if fewer than 2 snapshots exist.
    """
    if not history_for_ticker or len(history_for_ticker) < 2:
        return None
    sorted_snaps = sorted(history_for_ticker, key=lambda s: s["timestamp"])
    previous, current = sorted_snaps[-2], sorted_snaps[-1]
    prev_components = previous.get("component_scores", {})
    curr_components = current.get("component_scores", {})

    deltas = {}
    all_names = set(prev_components) | set(curr_components)
    for name in all_names:
        prev_c = prev_components.get(name, {})
        curr_c = curr_components.get(name, {})
        prev_score = prev_c.get("score") if prev_c.get("available") else None
        curr_score = curr_c.get("score") if curr_c.get("available") else None
        if prev_score is not None and curr_score is not None:
            deltas[name] = round(curr_score - prev_score, 1)
        elif prev_score is None and curr_score is not None:
            deltas[name] = f"newly available ({curr_score:.0f})"
        elif prev_score is not None and curr_score is None:
            deltas[name] = "became unavailable"
        # both None - no change worth reporting, omitted entirely

    return {
        "previousScore": previous.get("buy_score"), "currentScore": current.get("buy_score"),
        "previousTimestamp": previous["timestamp"], "currentTimestamp": current["timestamp"],
        "componentDeltas": deltas,
    }


def format_what_changed(deltas: dict) -> str:
    """Human-readable rendering of component_deltas' output - built
    entirely from the real numbers already computed, never independently
    generated text."""
    if deltas is None:
        return "Insufficient history to show what changed."
    prev, curr = deltas["previousScore"], deltas["currentScore"]
    if prev is None or curr is None:
        return "Score coverage changed between snapshots (became scoreable or unscoreable)."
    change = round(curr - prev, 1)
    parts = [f"{prev:.0f} → {curr:.0f} ({change:+.1f})"]
    for name, delta in sorted(deltas["componentDeltas"].items(),
                               key=lambda kv: -abs(kv[1]) if isinstance(kv[1], (int, float)) else 0):
        label = _CATEGORY_LABELS_FOR_CHANGE.get(name, name)
        if isinstance(delta, (int, float)):
            if abs(delta) < 0.5:
                continue  # genuine noise, not worth reporting
            parts.append(f"{label} {delta:+.1f}")
        else:
            parts.append(f"{label}: {delta}")
    return " · ".join(parts)


_CATEGORY_LABELS_FOR_CHANGE = {
    "momentum": "Momentum", "trend": "52W Trend", "liquidity": "Liquidity",
    "analyst_evidence": "Analyst", "news_catalyst": "Catalyst",
}


def detect_transition(history_for_ticker: list, now: datetime = None) -> Optional[LifecycleTransition]:
    """
    Compares the two most recent REAL snapshots (never today's data
    against itself) and returns a genuine transition event, or None if
    the change is too small to be meaningful (per
    MEANINGFUL_SCORE_CHANGE_THRESHOLD) or insufficient history exists.
    """
    if not history_for_ticker:
        return None
    sorted_snaps = sorted(history_for_ticker, key=lambda s: s["timestamp"])
    if len(sorted_snaps) < 2:
        # A stock with exactly ONE snapshot ever is genuinely NEW -
        # this is the only case a single snapshot is meaningful.
        latest = sorted_snaps[-1]
        if latest.get("buy_score") is None:
            return None
        return LifecycleTransition(
            ticker=latest["ticker"], event_type="NEW",
            previous_score=None, current_score=latest["buy_score"],
            previous_band=None, current_band=score_band(latest["buy_score"]),
            priority="MEDIUM",
            description=f"First scored: {latest['buy_score']} ({score_band(latest['buy_score'])})",
        )

    previous, current = sorted_snaps[-2], sorted_snaps[-1]
    prev_score, curr_score = previous.get("buy_score"), current.get("buy_score")
    prev_band, curr_band = score_band(prev_score), score_band(curr_score)
    ticker = current["ticker"]

    if prev_score is None and curr_score is not None:
        return LifecycleTransition(ticker, "NEW", None, curr_score, None, curr_band, "MEDIUM",
                                    f"Became scoreable: {curr_score} ({curr_band})")
    if prev_score is not None and curr_score is None:
        return LifecycleTransition(ticker, "LOST", prev_score, None, prev_band, None, "HIGH",
                                    f"No longer scoreable (was {prev_score}, {prev_band})")
    if prev_score is None and curr_score is None:
        return None

    change = round(curr_score - prev_score, 1)
    band_changed = prev_band != curr_band

    # A band crossing is ALWAYS meaningful by definition (per the exact
    # spec examples: 79 -> 81 = entered Strong Setup, a 2-point change
    # well under the general noise threshold, but genuinely significant
    # because it crosses a real boundary). The noise threshold only
    # applies to movement WITHIN the same band.
    if not band_changed and abs(change) < MEANINGFUL_SCORE_CHANGE_THRESHOLD:
        return None  # genuine noise, not a real event - no alert

    if band_changed:
        event_type = "BAND_CHANGE"
        direction = "entered" if _band_rank(curr_band) > _band_rank(prev_band) else "dropped out of"
        description = f"{prev_score} → {curr_score}: {direction} {curr_band}"
        priority = "HIGH"
    elif change > 0:
        event_type = "IMPROVING"
        description = f"{prev_score} → {curr_score} (+{change})"
        priority = "HIGH" if change >= 8 else "MEDIUM"
    else:
        event_type = "DETERIORATING"
        description = f"{prev_score} → {curr_score} ({change})"
        priority = "HIGH" if change <= -8 else "MEDIUM"

    return LifecycleTransition(ticker, event_type, prev_score, curr_score, prev_band, curr_band,
                                priority, description)


def _band_rank(band_label: Optional[str]) -> int:
    order = ["Below Threshold", "Promising", "Strong Setup", "Exceptional Setup"]
    return order.index(band_label) if band_label in order else -1


def deduplicate_transition(transition: LifecycleTransition, sent_event_ids: set) -> Optional[str]:
    """
    Returns a unique event ID if this transition should genuinely be
    alerted (not seen before), or None if it's a duplicate of an
    already-sent alert - per the explicit requirement: stock + event
    type + state transition + snapshot identity, so the SAME transition
    is never alerted twice just because the scanner ran again.
    """
    event_id = (f"{transition.ticker}|{transition.event_type}|"
                f"{transition.previous_score}->{transition.current_score}|"
                f"{transition.previous_band}->{transition.current_band}")
    if event_id in sent_event_ids:
        return None
    return event_id
