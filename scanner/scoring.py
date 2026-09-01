"""
LSE Opportunity Scanner - scoring engine (v2).

THREE numbers, always kept separate, never conflated:

BUY SCORE (0-100): how favorable the AVAILABLE evidence looks,
calculated ONLY from components that genuinely have data, renormalized
across whatever's available. Missing data is NEVER treated as zero,
NEVER treated as neutral, NEVER silently dropped without a trace.

DATA CONFIDENCE (0-100): how much the SCORE should be trusted, given
BOTH how much evidence is present AND how fresh it actually is right
now. Two stocks with identical component coverage but different data
ages get different confidence - a same-day-refreshed analyst target
counts more than one that's a week old but still the best available.

DATA COVERAGE (%): a simpler, separate number - the raw proportion of
possible inputs that are genuinely present, regardless of freshness.
"88 / 82 confidence / 76% coverage" tells a reader something
qualitatively different from a single blended number ever could.

CORE vs ENHANCEMENT: components are explicitly classified. A stock
missing ONLY enhancement data (analyst target, news) is still
SCOREABLE and PARTIALLY_COVERED - never excluded from ranking. A stock
missing ALL core data is UNSCOREABLE. This project's own genuinely
available data currently supports three CORE components (momentum,
trend, liquidity) and two ENHANCEMENT components (analyst evidence,
news catalyst) - true fundamentals/growth/risk-model core dimensions
the wider spec envisions are an honest, stated gap, not faked.
"""
from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime, timezone

from data_model import AgeStatus, CoverageState


COMPONENT_WEIGHTS = {
    "momentum": 30,
    "trend": 25,
    "liquidity": 20,
    "analyst_evidence": 15,
    "news_catalyst": 10,
}
assert sum(COMPONENT_WEIGHTS.values()) == 100

CORE_COMPONENTS = {"momentum", "trend", "liquidity"}
ENHANCEMENT_COMPONENTS = {"analyst_evidence", "news_catalyst"}

# Explicitly named engines that DO NOT YET have a connected data
# source - per the explicit requirement, these are reported as
# UNAVAILABLE, visibly, on every stock's breakdown, rather than
# silently absent from the component list. They carry ZERO weight and
# are NEVER factored into COMPONENT_WEIGHTS/buy_score in any way -
# this dict exists purely to make the gap visible on the page, not to
# participate in scoring.
UNAVAILABLE_ENGINES = {
    "quality": "No connected fundamentals source (ROE/margins/balance sheet)",
    "growth": "No connected fundamentals source (revenue/EPS/FCF growth)",
    "valuation": "No connected fundamentals source (P/E vs sector/history)",
    "dividend": "No connected fundamentals source (payout ratio/cover/history)",
    "balance_sheet": "No connected fundamentals source (debt/equity/interest cover)",
    "profitability": "No connected fundamentals source (margins/ROIC)",
    "cash_flow": "No connected fundamentals source (operating/free cash flow)",
}
assert CORE_COMPONENTS | ENHANCEMENT_COMPONENTS == set(COMPONENT_WEIGHTS)

AGE_CONFIDENCE_MULTIPLIER = {
    AgeStatus.FRESH: 1.0,
    AgeStatus.RECENT: 0.85,
    AgeStatus.STALE: 0.6,
    AgeStatus.EXPIRED: 0.3,
    AgeStatus.UNKNOWN: 0.5,
}

COMPONENT_TTL_CATEGORY = {
    "momentum": "price", "trend": "price", "liquidity": "price",
    "analyst_evidence": "analyst", "news_catalyst": "news",
}

_CATEGORY_LABELS = {
    "momentum": "Price Momentum", "trend": "52-Week Trend", "liquidity": "Volume/Liquidity",
    "analyst_evidence": "Analyst Evidence", "news_catalyst": "News Catalyst",
}

MIN_CORE_WEIGHT_FOR_SCOREABLE = 30

# Evidence breadth thresholds - per the explicit requirement that a
# 95 built from one excellent engine must never look equivalent to a
# 95 built from five agreeing engines. Total engines currently
# implemented: 5 (momentum, trend, liquidity, analyst_evidence,
# news_catalyst) - thresholds are expressed as counts, not
# percentages, so they stay meaningful as more engines are added later
# (per the wider spec's ENGINE C-H) without needing to be re-tuned.
MIN_ENGINES_FOR_HIGH_QUALITY = 4
MIN_ENGINES_FOR_MEDIUM_QUALITY = 3


def _opportunity_quality(evidence_breadth: int) -> str:
    if evidence_breadth >= MIN_ENGINES_FOR_HIGH_QUALITY:
        return "HIGH"
    if evidence_breadth >= MIN_ENGINES_FOR_MEDIUM_QUALITY:
        return "MEDIUM"
    return "LOW"


@dataclass
class ComponentScore:
    name: str
    category: str
    raw_value: Optional[float]
    score_0_100: Optional[float]
    weight: int
    available: bool
    age_status: Optional[AgeStatus]
    explanation: str


@dataclass
class RiskFlag:
    code: str
    label: str
    severity: str  # "warning" or "info"


RISK_EXTENDED_MOMENTUM_THRESHOLD = 8.0   # % single-day move considered "extended"
RISK_HIGH_VOLATILITY_RANGE_PCT = 60.0    # 52wk (high-low)/low ratio considered "high volatility"
RISK_EARNINGS_IMMINENT_DAYS = 7          # days out considered "imminent"
RISK_LOW_LIQUIDITY_RATIO = 0.5           # volume/avg_volume below this = thin trading


RISK_FLAG_SEVERITY_POINTS = {
    "extended_momentum": 15, "high_volatility": 20, "low_liquidity": 25,
    "earnings_imminent": 15, "data_stale": 10,
}


def compute_risk_score(risk_flags: list) -> int:
    """
    A simple, transparent, additive 0-100 risk score built directly
    from the same risk_flags compute_risk_flags already produces -
    never a separate, opaque calculation. Each flag contributes a
    fixed, documented point value (RISK_FLAG_SEVERITY_POINTS above);
    zero flags = 0 risk score, never fabricated from nothing. Capped
    at 100 - multiple severe flags don't overflow the scale.
    """
    return min(100, sum(RISK_FLAG_SEVERITY_POINTS.get(f.code, 10) for f in risk_flags))


def compute_risk_flags(stock_record, now=None) -> list:
    """
    Descriptive, testable risk flags derived from data already
    available in this StockRecord - never a separate fabricated "risk
    score". Each flag only fires when the underlying data is genuinely
    available; missing data never produces a flag either way (absence
    of evidence is not evidence of risk).
    """
    now = now or datetime.now(timezone.utc)
    flags = []

    change_pct = stock_record.price.change_pct.value if stock_record.price.change_pct.is_available else None
    if change_pct is not None and abs(change_pct) >= RISK_EXTENDED_MOMENTUM_THRESHOLD:
        flags.append(RiskFlag("extended_momentum", f"⚠ Momentum extended ({change_pct:+.1f}% today)", "warning"))

    low = stock_record.price.fifty_two_week_low.value if stock_record.price.fifty_two_week_low.is_available else None
    high = stock_record.price.fifty_two_week_high.value if stock_record.price.fifty_two_week_high.is_available else None
    if low and high and low > 0:
        range_pct = ((high - low) / low) * 100
        if range_pct >= RISK_HIGH_VOLATILITY_RANGE_PCT:
            flags.append(RiskFlag("high_volatility", f"⚠ High volatility (52wk range {range_pct:.0f}%)", "warning"))

    volume = stock_record.volume.volume.value if stock_record.volume.volume.is_available else None
    avg_volume = stock_record.volume.average_volume.value if stock_record.volume.average_volume.is_available else None
    if volume is not None and avg_volume:
        ratio = volume / avg_volume
        if ratio < RISK_LOW_LIQUIDITY_RATIO:
            flags.append(RiskFlag("low_liquidity", f"⚠ Low liquidity (volume {ratio:.1f}x average)", "warning"))

    next_earnings = stock_record.earnings.next_earnings_date
    if next_earnings.is_available:
        try:
            earnings_dt = datetime.fromtimestamp(next_earnings.value, tz=timezone.utc)
            days_out = (earnings_dt - now).total_seconds() / 86400
            if 0 <= days_out <= RISK_EARNINGS_IMMINENT_DAYS:
                flags.append(RiskFlag("earnings_imminent", f"⚠ Earnings imminent (in {days_out:.0f} day(s))", "warning"))
        except (ValueError, OSError, OverflowError):
            pass  # malformed timestamp - never crash on a bad date, just skip the flag

    stale_sources = []
    for label, dp, category in [
        ("price", stock_record.price.change_pct, "price"),
        ("52wk range", stock_record.price.fifty_two_week_low, "price"),
        ("analyst target", stock_record.valuation.target_mean_price, "analyst"),
        ("average volume", stock_record.volume.average_volume, "price"),
    ]:
        if dp.is_available and dp.age_status(category) == AgeStatus.EXPIRED:
            stale_sources.append(label)
    if stale_sources:
        flags.append(RiskFlag("data_stale", f"⚠ Data stale: {', '.join(stale_sources)}", "warning"))

    return flags
    name: str
    category: str
    raw_value: Optional[float]
    score_0_100: Optional[float]
    weight: int
    available: bool
    age_status: Optional[AgeStatus]
    explanation: str


@dataclass
class ScoreBreakdown:
    ticker: str
    buy_score: Optional[float]
    data_confidence: float
    data_coverage_pct: float
    coverage_state: CoverageState
    evidence_breadth: int = 0       # count of genuinely available components/engines
    opportunity_quality: str = "LOW"  # LOW/MEDIUM/HIGH - how broad the evidence behind buy_score is
    components: list = field(default_factory=list)
    missing_categories: list = field(default_factory=list)

    def summary_line(self) -> str:
        if self.buy_score is None:
            return f"{self.ticker}: UNSCOREABLE (insufficient core data this run)"
        missing = f" — Missing: {', '.join(self.missing_categories)}" if self.missing_categories else ""
        quality_note = ""
        if self.buy_score >= 85 and self.opportunity_quality == "LOW":
            quality_note = " ⚠ HIGH SCORE / LIMITED EVIDENCE"
        return (f"{self.ticker}: BUY SCORE {round(self.buy_score)} · "
                f"DATA CONFIDENCE {round(self.data_confidence)} · "
                f"DATA COVERAGE {round(self.data_coverage_pct)}% · "
                f"OPPORTUNITY QUALITY {self.opportunity_quality} ({self.evidence_breadth} engines){quality_note}{missing}")


def _clamp(value, lo=0.0, hi=100.0):
    return max(lo, min(hi, value))


def _score_momentum(change_pct):
    if change_pct is None:
        return None, None, "No price-change data available this run."
    raw = 50 + (change_pct * 10)
    return change_pct, _clamp(raw), f"Today's change: {change_pct:+.2f}%"


def _score_trend(current_price, week52_low, week52_high):
    if current_price is None or week52_low is None or week52_high is None:
        return None, None, "52-week range not available for this stock this run."
    if week52_high == week52_low:
        return None, None, "52-week range data degenerate (high == low) - not usable."
    position_pct = ((current_price - week52_low) / (week52_high - week52_low)) * 100
    return position_pct, _clamp(position_pct), f"Currently {position_pct:.0f}% of the way through its 52-week range"


def _score_liquidity(volume, average_volume):
    if volume is None or average_volume is None or not average_volume:
        return None, None, "Volume/average-volume not available for this stock this run."
    ratio = volume / average_volume
    raw = 50 + ((ratio - 1) / 2) * 50
    return ratio, _clamp(raw), f"Volume is {ratio:.1f}x the average"


def _score_analyst_evidence(current_price, target_mean):
    if current_price is None or target_mean is None or not current_price:
        return None, None, "No broker consensus target available for this stock this run."
    upside_pct = ((target_mean - current_price) / current_price) * 100
    if upside_pct >= 0:
        raw = 50 + (upside_pct / 30) * 50
    else:
        raw = 50 + (upside_pct / 20) * 50
    return upside_pct, _clamp(raw), f"Broker target implies {upside_pct:+.1f}% vs current price"


def _score_news_catalyst(news_data):
    if news_data.recent_items.source is None:
        return None, None, "News was not checked for this stock this run."
    item_count_today = news_data.item_count_today
    if item_count_today == 0:
        return 0, 40, "No same-day news matched - genuinely quiet, not unscored."
    raw = 60 + min(item_count_today, 4) * 10
    return item_count_today, _clamp(raw), f"{item_count_today} matched same-day news item(s)"


def calculate_score(stock_record) -> ScoreBreakdown:
    """
    Each component's availability, raw score, and AGE are determined
    independently. BUY SCORE uses each available component's score
    AS-IS regardless of age (stale data isn't thrown away) - only DATA
    CONFIDENCE is discounted for staleness. DATA COVERAGE is a simple
    availability percentage, kept genuinely separate from confidence.
    """
    price = stock_record.price.last_price.value if stock_record.price.last_price.is_available else None
    change_pct = stock_record.price.change_pct.value if stock_record.price.change_pct.is_available else None
    week52_low = (stock_record.price.fifty_two_week_low.value
                  if stock_record.price.fifty_two_week_low.is_available else None)
    week52_high = (stock_record.price.fifty_two_week_high.value
                   if stock_record.price.fifty_two_week_high.is_available else None)
    volume = stock_record.volume.volume.value if stock_record.volume.volume.is_available else None
    avg_volume = (stock_record.volume.average_volume.value
                  if stock_record.volume.average_volume.is_available else None)
    target_mean = (stock_record.valuation.target_mean_price.value
                   if stock_record.valuation.target_mean_price.is_available else None)

    raw_fns = {
        "momentum": lambda: _score_momentum(change_pct),
        "trend": lambda: _score_trend(price, week52_low, week52_high),
        "liquidity": lambda: _score_liquidity(volume, avg_volume),
        "analyst_evidence": lambda: _score_analyst_evidence(price, target_mean),
        "news_catalyst": lambda: _score_news_catalyst(stock_record.news),
    }
    age_source_points = {
        "momentum": stock_record.price.change_pct,
        "trend": stock_record.price.fifty_two_week_low,
        "liquidity": stock_record.volume.average_volume,
        "analyst_evidence": stock_record.valuation.target_mean_price,
        "news_catalyst": stock_record.news.recent_items,
    }

    components = []
    for name, weight in COMPONENT_WEIGHTS.items():
        raw_value, score, explanation = raw_fns[name]()
        available = score is not None
        age_status = age_source_points[name].age_status(COMPONENT_TTL_CATEGORY[name]) if available else None
        category = "core" if name in CORE_COMPONENTS else "enhancement"
        components.append(ComponentScore(name, category, raw_value, score, weight, available, age_status, explanation))

    available_components = [c for c in components if c.available]
    missing = [c.name for c in components if not c.available]
    missing_labels = [_CATEGORY_LABELS.get(m, m) for m in missing]

    core_weight_available = sum(c.weight for c in available_components if c.category == "core")
    is_scoreable = core_weight_available >= MIN_CORE_WEIGHT_FOR_SCOREABLE

    data_coverage_pct = round(100 * sum(c.weight for c in available_components) / 100, 1)

    if not available_components or not is_scoreable:
        buy_score = None
        data_confidence = 0.0
        coverage_state = CoverageState.UNSCOREABLE
    else:
        total_weight = sum(c.weight for c in available_components)
        buy_score = sum(c.score_0_100 * c.weight for c in available_components) / total_weight

        confidence_weight = sum(
            c.weight * AGE_CONFIDENCE_MULTIPLIER.get(c.age_status, 0.5)
            for c in available_components
        )
        data_confidence = round(100 * confidence_weight / 100, 1)

        coverage_state = CoverageState.FULLY_COVERED if not missing else CoverageState.PARTIALLY_COVERED

    evidence_breadth = len(available_components)
    opportunity_quality = _opportunity_quality(evidence_breadth) if buy_score is not None else "LOW"

    return ScoreBreakdown(
        ticker=stock_record.identity.ticker,
        buy_score=round(buy_score, 1) if buy_score is not None else None,
        data_confidence=data_confidence,
        data_coverage_pct=data_coverage_pct,
        coverage_state=coverage_state,
        evidence_breadth=evidence_breadth,
        opportunity_quality=opportunity_quality,
        components=components,
        missing_categories=missing_labels,
    )


@dataclass
class EvidenceConflict:
    conflict_type: str
    description: str


def detect_evidence_conflicts(breakdown) -> list:
    """
    Genuine contradiction detection - checks specific, named component
    pairs for disagreement, using the SAME ComponentScore data already
    computed for the score itself. Never invents a conflict; only
    fires when both sides of a named pair are genuinely available and
    genuinely point in different directions.
    """
    conflicts = []
    by_name = {c.name: c for c in breakdown.components}

    momentum = by_name.get("momentum")
    analyst = by_name.get("analyst_evidence")
    if momentum and analyst and momentum.available and analyst.available:
        if momentum.score_0_100 >= 65 and analyst.score_0_100 <= 35:
            conflicts.append(EvidenceConflict(
                "momentum_vs_analyst",
                f"Strong price momentum (+{momentum.raw_value:.1f}% today) but weak broker-target evidence "
                f"(implies {analyst.raw_value:+.1f}% vs current price) — the market's short-term move and "
                f"analyst consensus are not agreeing.",
            ))
        elif momentum.score_0_100 <= 35 and analyst.score_0_100 >= 65:
            conflicts.append(EvidenceConflict(
                "momentum_vs_analyst",
                f"Weak price momentum ({momentum.raw_value:+.1f}% today) despite a strong broker-target "
                f"upside ({analyst.raw_value:+.1f}%) — the market isn't yet confirming the analyst view.",
            ))

    trend = by_name.get("trend")
    liquidity = by_name.get("liquidity")
    if trend and liquidity and trend.available and liquidity.available:
        if trend.score_0_100 >= 80 and liquidity.score_0_100 <= 35:
            conflicts.append(EvidenceConflict(
                "trend_vs_liquidity",
                f"Near its 52-week high ({trend.raw_value:.0f}% of the range) but on thin volume "
                f"({liquidity.raw_value:.1f}x average) — the move up isn't backed by strong participation.",
            ))

    if breakdown.buy_score is not None and breakdown.buy_score >= 85 and breakdown.data_confidence < 55:
        conflicts.append(EvidenceConflict(
            "high_score_low_confidence",
            f"BUY SCORE {breakdown.buy_score:.0f} looks strong, but DATA CONFIDENCE is only "
            f"{breakdown.data_confidence:.0f} — this score is built from limited or stale evidence, "
            f"not a comprehensively-confirmed picture.",
        ))

    return conflicts


def classify_opportunity_tier(score: Optional[float]) -> Optional[str]:
    """90+, 80-89, 70-79 tiers - flags EVERY stock that qualifies,
    never capped at a fixed top-N."""
    if score is None:
        return None
    if score >= 90:
        return "90+"
    if score >= 80:
        return "80-89"
    if score >= 70:
        return "70-79"
    return None
