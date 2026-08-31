"""
LSE Opportunity Scanner - normalized data model.

Every meaningful value in this system is wrapped in a DataPoint, which
tracks not just the value but WHERE it came from, WHEN it was obtained,
and how confident the system is in it. This is what makes "scoring with
partial data" honest rather than guessed: a score component that used
real data looks different, in the data structure itself, from one that
had nothing to work with - there is no code path where a missing field
silently becomes a zero or a fabricated number.

This module has NO network calls and NO dependency on poll_live.py -
it's pure data structure, imported by providers.py (which does the
actual fetching) and scoring.py (which consumes it).
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional, Any


class DataFreshness(Enum):
    """How current a value genuinely is - never inferred, always set
    explicitly by whichever provider supplied the value, based on what
    that provider actually told us (a live quote vs an end-of-day
    close vs a delayed feed vs a stale cache)."""
    REAL_TIME = "real_time"
    DELAYED = "delayed"          # provider explicitly delayed (e.g. 15-min)
    END_OF_DAY = "end_of_day"
    STALE_CACHE = "stale_cache"  # persisted value, provider unavailable this run
    UNKNOWN = "unknown"          # provider gave no freshness signal at all


class AgeStatus(Enum):
    """
    A SEPARATE concept from DataFreshness above: DataFreshness is what
    the SOURCE claims about itself (real-time vs delayed vs EOD).
    AgeStatus is how OLD a specific value actually is right now,
    relative to a sensible time-to-live for its data category (see
    DATA_TYPE_TTL_SECONDS below) - a real-time price from 8 minutes ago
    is STALE for a price (TTL: minutes), while an analyst target from 8
    hours ago is perfectly FRESH for that category (TTL: hours/day).
    Never conflated: a value can be DataFreshness.REAL_TIME in origin
    and still be AgeStatus.STALE right now if it simply hasn't been
    refreshed recently enough for its own category's TTL.
    """
    FRESH = "fresh"      # well within TTL
    RECENT = "recent"    # within TTL but past its "ideal" midpoint
    STALE = "stale"      # past TTL, but still the best available - carried forward, not discarded
    EXPIRED = "expired"  # far enough past TTL that it should be treated as effectively unavailable
    UNKNOWN = "unknown"  # no timestamp at all to judge age from


# Sensible per-category time-to-live, in seconds - determines how a
# DataPoint's age is judged (see AgeStatus above and
# DataPoint.age_status below). Examples from the spec, translated into
# concrete numbers this codebase can actually apply:
DATA_TYPE_TTL_SECONDS = {
    "price": 15 * 60,             # PRICE/VOLUME: minutes
    "volume": 15 * 60,
    "technical": 2 * 3600,        # TECHNICALS: minutes/hours
    "news": 6 * 3600,             # NEWS: hours
    "analyst": 24 * 3600,         # ANALYST ESTIMATES: hours/day
    "fundamental": 7 * 24 * 3600,  # FUNDAMENTALS: days/weeks
    "profile": 30 * 24 * 3600,    # COMPANY PROFILE: weeks/months
}


class CoverageState(Enum):
    """
    Two genuinely separate questions, per the explicit correction:
    "is there enough CORE data to produce a meaningful score at all"
    is a DIFFERENT question from "how much of the FULL evidence
    picture, core AND enhancement, is present". A stock missing only
    enhancement data (e.g. analyst target) is scoreable and
    PARTIALLY_COVERED - it must never be excluded from ranking just
    because one enhancement category is missing. is_scoreable is True
    for both FULLY_COVERED and PARTIALLY_COVERED; only UNSCOREABLE
    means no meaningful score could be produced at all.
    """
    FULLY_COVERED = "fully_covered"          # scoreable AND all core+enhancement present
    PARTIALLY_COVERED = "partially_covered"  # scoreable, but some evidence (core or enhancement) missing
    UNSCOREABLE = "unscoreable"              # insufficient CORE data - no meaningful score possible

    @property
    def is_scoreable(self) -> bool:
        return self != CoverageState.UNSCOREABLE


class ProviderStatus(Enum):
    """A provider's own observed health, tracked persistently across
    runs (see providers.py's ProviderHealthMonitor) - not a static
    label, an actual measured state."""
    AVAILABLE = "available"
    DEGRADED = "degraded"        # working, but recent errors above baseline
    RATE_LIMITED = "rate_limited"
    FAILED = "failed"
    STALE = "stale"              # hasn't been successfully queried recently
    DISABLED = "disabled"        # deliberately turned off (e.g. no API key)


class FieldStatus(Enum):
    """
    The explicit distinction requested: a field's CURRENT status is
    genuinely different information from its age or its value. Two
    fields can both be "available" but one was REFRESHED this exact
    run while the other survived via persistence despite a
    REFRESH_FAILED this run - the UI and the score explanation should
    be able to say which, never collapsing them into one ambiguous
    "available" flag.
    """
    AVAILABLE = "available"            # has a valid value, refresh status not otherwise distinguished
    REFRESHED = "refreshed"            # genuinely fetched fresh THIS run
    STALE = "stale"                    # valid, but past its TTL midpoint - carried forward
    EXPIRED = "expired"                # valid but far past TTL - used only for lack of an alternative
    REFRESH_FAILED = "refresh_failed"  # an attempt was made this run and failed - but a prior valid value survives
    MISSING = "missing"                # never successfully obtained at all - no prior value exists
    UNSUPPORTED = "unsupported"        # no provider currently exists for this field at all


@dataclass
class DataPoint:
    """
    The atomic unit of this data model. Every field on every stock is
    one of these, never a bare value - so "we don't know" and "this
    genuinely is zero" are never confusable, and every number displayed
    anywhere can be traced back to exactly which provider supplied it
    and when.
    """
    value: Optional[Any] = None
    source: Optional[str] = None          # provider name, e.g. "LSE", "Yahoo Finance"
    timestamp: Optional[datetime] = None  # when the SOURCE says this value is as-of
    fetched_at: Optional[datetime] = None  # when WE retrieved it (may differ from timestamp)
    freshness: DataFreshness = DataFreshness.UNKNOWN
    confidence: float = 0.0  # 0.0-1.0 - how much this specific value should be trusted
    status: FieldStatus = FieldStatus.MISSING
    # Set ONLY when a refresh was genuinely attempted this run and
    # failed - kept separate from timestamp (which is the value's OWN
    # as-of time, unaffected by a failed attempt to update it) so
    # "last successful refresh" and "most recent attempt" never get
    # confused with each other.
    last_refresh_attempt_failed_at: Optional[datetime] = None
    last_refresh_error: Optional[str] = None

    @property
    def is_available(self) -> bool:
        """The one check every consumer should use before touching
        .value - never assume a DataPoint has real data just because
        the object exists."""
        return self.value is not None

    def age_seconds(self) -> Optional[float]:
        if self.timestamp is None:
            return None
        return (datetime.now(timezone.utc) - self.timestamp).total_seconds()

    def age_status(self, category: str) -> AgeStatus:
        """category is a key into DATA_TYPE_TTL_SECONDS (e.g. "price",
        "analyst", "fundamental"). Returns FRESH/RECENT/STALE/EXPIRED
        based on this value's actual age against that category's TTL -
        never based on which provider it came from or how it's
        labeled, purely on measured elapsed time."""
        age = self.age_seconds()
        if age is None:
            return AgeStatus.UNKNOWN
        ttl = DATA_TYPE_TTL_SECONDS.get(category, 24 * 3600)
        if age <= ttl * 0.5:
            return AgeStatus.FRESH
        if age <= ttl:
            return AgeStatus.RECENT
        if age <= ttl * 3:
            return AgeStatus.STALE
        return AgeStatus.EXPIRED

    def age_human(self) -> str:
        """A short, human-readable age string - 'Updated 18 hours ago'
        style, for direct display."""
        age = self.age_seconds()
        if age is None:
            return "unknown age"
        if age < 90:
            return "just now"
        if age < 3600:
            return f"{int(age // 60)} minute(s) ago"
        if age < 86400:
            return f"{int(age // 3600)} hour(s) ago"
        return f"{int(age // 86400)} day(s) ago"

    def compute_status(self, category: str, just_refreshed: bool = False) -> FieldStatus:
        """
        Derives the correct FieldStatus from this DataPoint's actual
        state - never manually guessed by a caller. The key
        distinction this enforces: REFRESH_FAILED requires BOTH a
        genuinely failed attempt AND a prior valid value to fall back
        on; a field that has NEVER succeeded is MISSING even if a
        refresh was just attempted and failed, since there is no
        "last known good" state to preserve for it.
        """
        if not self.is_available:
            return FieldStatus.MISSING
        if just_refreshed and self.last_refresh_attempt_failed_at is None:
            return FieldStatus.REFRESHED
        if self.last_refresh_attempt_failed_at is not None:
            # A failed attempt happened - but since is_available is True
            # here, a prior valid value genuinely survives it.
            return FieldStatus.REFRESH_FAILED
        age_status = self.age_status(category)
        if age_status == AgeStatus.EXPIRED:
            return FieldStatus.EXPIRED
        if age_status in (AgeStatus.STALE,):
            return FieldStatus.STALE
        return FieldStatus.AVAILABLE

    def status_badge(self, category: str, just_refreshed: bool = False) -> str:
        """A short emoji+label for direct UI display, matching the
        exact style specified: '🟢 RECENT', '🟡 STALE', '🔴 EXPIRED'."""
        status = self.compute_status(category, just_refreshed)
        age_status = self.age_status(category) if self.is_available else AgeStatus.UNKNOWN
        badges = {
            FieldStatus.REFRESHED: "🟢 FRESH", FieldStatus.AVAILABLE: "🟢 RECENT",
            FieldStatus.STALE: "🟡 STALE", FieldStatus.EXPIRED: "🔴 EXPIRED",
            FieldStatus.REFRESH_FAILED: "🟠 REFRESH FAILED (using last known good)",
            FieldStatus.MISSING: "⚪ MISSING", FieldStatus.UNSUPPORTED: "⚪ UNSUPPORTED",
        }
        return badges.get(status, "⚪ UNKNOWN")


def missing_point(source: Optional[str] = None) -> DataPoint:
    """Explicit constructor for 'we tried, this field genuinely isn't
    available' - used instead of DataPoint() bare so every call site
    that couldn't get a value says so intentionally, not by omission."""
    return DataPoint(value=None, source=source, confidence=0.0, freshness=DataFreshness.UNKNOWN)


@dataclass
class StockIdentity:
    ticker: str                       # LSE/Yahoo-style ticker, e.g. "BARC"
    isin: Optional[str] = None        # stable identifier - preferred over name for dedup
    tidm: Optional[str] = None        # LSE's own mnemonic, when isin isn't available
    name: Optional[str] = None
    exchange: str = "LSE"

    def stable_key(self) -> str:
        """The identifier used for deduplication across universe
        sources - ISIN when genuinely available (a real stable
        identifier), falling back to TIDM, falling back to ticker only
        as a last resort. Never company name alone (per the explicit
        requirement) - two different share classes or a renamed company
        can share a name but not a real identifier."""
        return self.isin or self.tidm or self.ticker


@dataclass
class PriceData:
    last_price: DataPoint = field(default_factory=DataPoint)
    change_pct: DataPoint = field(default_factory=DataPoint)
    net_change: DataPoint = field(default_factory=DataPoint)
    day_high: DataPoint = field(default_factory=DataPoint)
    day_low: DataPoint = field(default_factory=DataPoint)
    fifty_two_week_high: DataPoint = field(default_factory=DataPoint)
    fifty_two_week_low: DataPoint = field(default_factory=DataPoint)


@dataclass
class VolumeData:
    volume: DataPoint = field(default_factory=DataPoint)
    average_volume: DataPoint = field(default_factory=DataPoint)

    def volume_ratio(self) -> Optional[float]:
        if not (self.volume.is_available and self.average_volume.is_available):
            return None
        if not self.average_volume.value:
            return None
        return self.volume.value / self.average_volume.value


@dataclass
class HistoricalData:
    """Closing-price series, used for technical indicators. Stored as
    a plain list of (date, close) - RSI/moving-average calculation
    lives in scoring.py, not here; this is just the raw series with
    its own provenance."""
    closes: DataPoint = field(default_factory=DataPoint)  # value: list[(date, float)]
    days_available: int = 0


@dataclass
class FundamentalData:
    market_cap: DataPoint = field(default_factory=DataPoint)
    pe_ratio: DataPoint = field(default_factory=DataPoint)
    dividend_yield: DataPoint = field(default_factory=DataPoint)
    debt_to_equity: DataPoint = field(default_factory=DataPoint)
    profit_margin: DataPoint = field(default_factory=DataPoint)


@dataclass
class ValuationData:
    target_mean_price: DataPoint = field(default_factory=DataPoint)
    target_high_price: DataPoint = field(default_factory=DataPoint)
    target_low_price: DataPoint = field(default_factory=DataPoint)
    number_of_analyst_opinions: DataPoint = field(default_factory=DataPoint)
    recommendation_key: DataPoint = field(default_factory=DataPoint)

    def upside_pct(self, current_price: Optional[float]) -> Optional[float]:
        if current_price is None or not self.target_mean_price.is_available:
            return None
        if not current_price:
            return None
        return ((self.target_mean_price.value - current_price) / current_price) * 100


@dataclass
class EarningsData:
    next_earnings_date: DataPoint = field(default_factory=DataPoint)
    last_eps: DataPoint = field(default_factory=DataPoint)
    eps_estimate: DataPoint = field(default_factory=DataPoint)


@dataclass
class AnalystData:
    """Upgrade/downgrade/target-change history - distinct from
    ValuationData's current consensus snapshot; this is the recent
    ACTIVITY feed, reusing the exact same structured source this
    project already has (Yahoo's analyst history)."""
    recent_actions: DataPoint = field(default_factory=DataPoint)  # value: list[dict]


@dataclass
class NewsData:
    """Reuses this project's own already-built, already-deduplicated
    news pipeline (multi-source, near-duplicate-filtered, same-day
    filtered) rather than re-fetching - see providers.py's
    ExistingPipelineNewsProvider."""
    recent_items: DataPoint = field(default_factory=DataPoint)  # value: list[dict]
    item_count_today: int = 0


@dataclass
class CorporateActionData:
    ex_dividend_date: DataPoint = field(default_factory=DataPoint)
    dividend_amount: DataPoint = field(default_factory=DataPoint)


@dataclass
class RiskData:
    """Purely descriptive risk facts already computable from other
    fields - never a judgment call, never an invented 'risk score' from
    nothing. Populated by scoring.py from the OTHER already-fetched
    data, not from a separate provider."""
    volatility_flag: Optional[str] = None      # e.g. "high 5-day range" - descriptive only
    contradiction_flags: list = field(default_factory=list)  # reuses existing detect_contradictions


@dataclass
class DataQuality:
    """Summarizes, for ONE stock, how complete its data actually is -
    consumed directly by the scoring engine to compute Data Confidence
    and Data Coverage separately from the Buy Score itself (per the
    explicit requirement: missing data must never silently become a
    zero, and BUY SCORE / DATA CONFIDENCE / DATA COVERAGE are three
    genuinely separate numbers, never conflated)."""
    fields_available: int = 0
    fields_total: int = 0
    missing_categories: list = field(default_factory=list)  # e.g. ["Analyst Evidence"]
    coverage_state: "CoverageState" = None  # set by scoring.py once computed

    @property
    def completeness_pct(self) -> float:
        if self.fields_total == 0:
            return 0.0
        return round(100 * self.fields_available / self.fields_total, 1)


@dataclass
class StockRecord:
    """The complete normalized picture of one stock - what the scoring
    engine actually consumes. Never knows or cares which provider
    supplied which field; that provenance lives inside each DataPoint,
    not in this record's own structure."""
    identity: StockIdentity
    price: PriceData = field(default_factory=PriceData)
    volume: VolumeData = field(default_factory=VolumeData)
    historical: HistoricalData = field(default_factory=HistoricalData)
    fundamentals: FundamentalData = field(default_factory=FundamentalData)
    valuation: ValuationData = field(default_factory=ValuationData)
    earnings: EarningsData = field(default_factory=EarningsData)
    analyst: AnalystData = field(default_factory=AnalystData)
    news: NewsData = field(default_factory=NewsData)
    corporate_actions: CorporateActionData = field(default_factory=CorporateActionData)
    risk: RiskData = field(default_factory=RiskData)
    quality: DataQuality = field(default_factory=DataQuality)
