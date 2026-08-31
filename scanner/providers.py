"""
LSE Opportunity Scanner - provider abstraction layer.

The scoring engine never talks to LSE or Yahoo directly - it asks the
ProviderManager for data, and the manager decides which registered
provider (or fallback chain) actually supplies it. Adding a new data
source later means implementing one of the interfaces below and
registering it - never touching the scoring engine, the universe
registry, or anything downstream.

Every concrete provider here wraps functions ALREADY PROVEN in
poll_live.py this session (the LSE fetches, the Yahoo fetches, the news
pipeline) - this module adds no new network-request logic of its own,
only the abstraction and health-tracking around calls that already work.
"""
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Optional

from data_model import DataPoint, DataFreshness, ProviderStatus, StockIdentity


@dataclass
class ProviderCapabilities:
    name: str
    supported_exchanges: list
    supported_instruments: str
    available_fields: list
    update_frequency_seconds: Optional[int]
    historical_depth_days: Optional[int]
    typical_freshness: DataFreshness
    rate_limit_note: str


@dataclass
class ProviderHealthRecord:
    status: ProviderStatus = ProviderStatus.AVAILABLE
    last_success_at: Optional[str] = None
    last_failure_at: Optional[str] = None
    last_error: Optional[str] = None
    consecutive_failures: int = 0
    total_requests: int = 0
    total_failures: int = 0
    last_latency_seconds: Optional[float] = None

    @property
    def error_rate(self) -> float:
        if self.total_requests == 0:
            return 0.0
        return round(self.total_failures / self.total_requests, 3)

    def to_dict(self):
        return {
            "status": self.status.value, "lastSuccessAt": self.last_success_at,
            "lastFailureAt": self.last_failure_at, "lastError": self.last_error,
            "consecutiveFailures": self.consecutive_failures, "totalRequests": self.total_requests,
            "totalFailures": self.total_failures, "errorRate": self.error_rate,
            "lastLatencySeconds": self.last_latency_seconds,
        }

    @classmethod
    def from_dict(cls, d):
        if not d:
            return cls()
        rec = cls()
        try:
            rec.status = ProviderStatus(d.get("status", "available"))
        except ValueError:
            rec.status = ProviderStatus.AVAILABLE
        rec.last_success_at = d.get("lastSuccessAt")
        rec.last_failure_at = d.get("lastFailureAt")
        rec.last_error = d.get("lastError")
        rec.consecutive_failures = d.get("consecutiveFailures", 0)
        rec.total_requests = d.get("totalRequests", 0)
        rec.total_failures = d.get("totalFailures", 0)
        rec.last_latency_seconds = d.get("lastLatencySeconds")
        return rec


class ProviderHealthMonitor:
    """Tracks every provider's real, observed health across runs -
    persisted as a plain dict so history survives between the
    stateless 5-minute script runs."""

    def __init__(self, initial_state=None):
        self._records = {}
        for name, d in (initial_state or {}).items():
            self._records[name] = ProviderHealthRecord.from_dict(d)

    def get(self, provider_name) -> ProviderHealthRecord:
        if provider_name not in self._records:
            self._records[provider_name] = ProviderHealthRecord()
        return self._records[provider_name]

    def record_success(self, provider_name, latency_seconds=None):
        rec = self.get(provider_name)
        rec.total_requests += 1
        rec.consecutive_failures = 0
        rec.last_success_at = datetime.now(timezone.utc).isoformat()
        rec.last_latency_seconds = latency_seconds
        rec.status = ProviderStatus.AVAILABLE

    def record_failure(self, provider_name, error_message, rate_limited=False):
        rec = self.get(provider_name)
        rec.total_requests += 1
        rec.total_failures += 1
        rec.consecutive_failures += 1
        rec.last_failure_at = datetime.now(timezone.utc).isoformat()
        rec.last_error = str(error_message)[:300]
        if rate_limited:
            rec.status = ProviderStatus.RATE_LIMITED
        elif rec.consecutive_failures >= 3:
            rec.status = ProviderStatus.FAILED
        else:
            rec.status = ProviderStatus.DEGRADED

    def to_dict(self):
        return {name: rec.to_dict() for name, rec in self._records.items()}


class UniverseProvider(ABC):
    @abstractmethod
    def capabilities(self) -> ProviderCapabilities: ...
    @abstractmethod
    def fetch_universe(self) -> list: ...


class PriceProvider(ABC):
    @abstractmethod
    def capabilities(self) -> ProviderCapabilities: ...
    @abstractmethod
    def covers(self, identity: StockIdentity) -> bool: ...


class FundamentalProvider(ABC):
    @abstractmethod
    def capabilities(self) -> ProviderCapabilities: ...
    @abstractmethod
    def fetch_fundamentals(self, identity: StockIdentity): ...


class AnalystProvider(ABC):
    @abstractmethod
    def capabilities(self) -> ProviderCapabilities: ...
    @abstractmethod
    def fetch_analyst_data(self, identity: StockIdentity): ...


class NewsProvider(ABC):
    @abstractmethod
    def capabilities(self) -> ProviderCapabilities: ...
    @abstractmethod
    def fetch_news(self, identity: StockIdentity): ...


class EstimateProvider(ABC):
    """Analyst EPS/revenue estimates and revisions - distinct from
    AnalystProvider's current price-target snapshot. No provider is
    currently connected for this category (see the source audit) -
    the interface exists so one can be plugged in without touching the
    scoring engine, per the explicit provider-independence requirement."""
    @abstractmethod
    def capabilities(self) -> ProviderCapabilities: ...
    @abstractmethod
    def fetch_estimates(self, identity: StockIdentity): ...


class CorporateActionsProvider(ABC):
    """Splits, consolidations, dividends, rights issues, takeovers,
    demergers, ticker changes, suspensions - no provider currently
    connected. Genuinely important for historical price integrity
    (per the explicit point-in-time requirement) but building this
    without a real source would mean guessing at corporate action
    data, which this project does not do."""
    @abstractmethod
    def capabilities(self) -> ProviderCapabilities: ...
    @abstractmethod
    def fetch_corporate_actions(self, identity: StockIdentity, since=None): ...


class UnsupportedProvider:
    """
    A genuine, honest 'no provider connected' implementation - not a
    silent no-op. Every call raises NotImplementedError with a clear
    message identifying exactly what's missing, so a caller that
    forgets to check capabilities() first fails loudly rather than
    silently returning empty data that looks like 'genuinely checked,
    found nothing' (which would be dishonest - MISSING and UNSUPPORTED
    are different FieldStatus values for exactly this reason).
    """
    def __init__(self, category_name: str, what_would_unlock_it: str):
        self.category_name = category_name
        self.what_would_unlock_it = what_would_unlock_it

    def capabilities(self):
        return ProviderCapabilities(
            name=f"{self.category_name} (UNSUPPORTED - no provider connected)",
            supported_exchanges=[], supported_instruments="none - no provider connected",
            available_fields=[], update_frequency_seconds=None, historical_depth_days=None,
            typical_freshness=DataFreshness.UNKNOWN,
            rate_limit_note=f"N/A - not connected. To unlock: {self.what_would_unlock_it}",
        )

    def __getattr__(self, name):
        def _raise(*args, **kwargs):
            raise NotImplementedError(
                f"{self.category_name}: no provider is currently connected. "
                f"To unlock this category: {self.what_would_unlock_it}"
            )
        return _raise


class LSEHeatmapUniverseProvider(UniverseProvider):
    """The ONLY confirmed, working broad-universe source in this
    project: the FTSE 100, via the same LSE Heatmap fetch already
    proven to return 100 real instruments this session."""

    def __init__(self, poll_live_module, health: ProviderHealthMonitor):
        self._pl = poll_live_module
        self._health = health

    def capabilities(self):
        return ProviderCapabilities(
            name="LSE Heatmap (FTSE 100)",
            supported_exchanges=["LSE"],
            supported_instruments="FTSE 100 constituents (~100 stocks)",
            available_fields=["ticker", "isin_or_tidm", "name", "price", "changePct", "volume"],
            update_frequency_seconds=300,
            historical_depth_days=None,
            typical_freshness=DataFreshness.REAL_TIME,
            rate_limit_note="Undocumented by LSE - this project already spaces multiple "
                             "requests within a run to reduce risk, confirmed necessary "
                             "earlier this session.",
        )

    def fetch_universe(self) -> list:
        start = time.time()
        try:
            result = self._pl.fetch_lse_ftse100_market_data("heatmap")
        except Exception as e:
            self._health.record_failure("LSE Heatmap (FTSE 100)", e)
            return []
        if result.get("status") != "ok":
            self._health.record_failure("LSE Heatmap (FTSE 100)", result.get("error", "unknown"))
            return []
        self._health.record_success("LSE Heatmap (FTSE 100)", time.time() - start)
        out = []
        for row in result["instruments"]:
            if not row.get("symbol"):
                continue
            out.append(StockIdentity(
                ticker=row["symbol"], isin=row.get("isin"), tidm=row.get("tidm"),
                name=row.get("name"), exchange="LSE",
            ))
        return out

    def fetch_universe_with_prices(self):
        """Returns the raw instrument rows too (not just identities) -
        so callers can build price data from this SAME response,
        rather than fetching LSE a second time for the same data."""
        start = time.time()
        try:
            result = self._pl.fetch_lse_ftse100_market_data("heatmap")
        except Exception as e:
            self._health.record_failure("LSE Heatmap (FTSE 100)", e)
            return [], {"status": "failed", "error": str(e)}
        if result.get("status") != "ok":
            self._health.record_failure("LSE Heatmap (FTSE 100)", result.get("error", "unknown"))
            return [], result
        self._health.record_success("LSE Heatmap (FTSE 100)", time.time() - start)
        return result["instruments"], result


class YahooAnalystProvider(FundamentalProvider, AnalystProvider):
    """Wraps this project's existing, already-tested
    fetch_yahoo_broker_target for valuation/analyst fields. Fundamentals
    proper (P/E, margins, debt) are an honest gap - not yet wired to any
    source; fetch_fundamentals returns an explicitly empty record so the
    coverage engine reports it as missing rather than fabricating it."""

    def __init__(self, poll_live_module, health: ProviderHealthMonitor):
        self._pl = poll_live_module
        self._health = health

    def capabilities(self):
        return ProviderCapabilities(
            name="Yahoo Finance (enrichment)",
            supported_exchanges=["LSE"], supported_instruments="Any ticker Yahoo covers",
            available_fields=["targetMeanPrice", "targetHighPrice", "targetLowPrice",
                               "numberOfAnalystOpinions", "recommendationKey"],
            update_frequency_seconds=None,
            historical_depth_days=None, typical_freshness=DataFreshness.DELAYED,
            rate_limit_note="No confirmed batch endpoint - one request per symbol; not "
                             "suitable for the full universe every run without an explicit "
                             "bounded cap (see scanner's own refresh-cadence design).",
        )

    def fetch_analyst_data(self, identity: StockIdentity):
        """Returns (ValuationData, average_volume_or_None) - average
        volume comes from the SAME underlying request (summaryDetail
        module, fetched alongside financialData) rather than a second
        call, so callers get both without any extra cost.

        Returns (ValuationData, volume_extra_dict, attempt_failed) -
        attempt_failed distinguishes a genuine failure (network error,
        timeout, malformed response) from a request that succeeded but
        legitimately found nothing (e.g. a stock with no analyst
        coverage at all) - the orchestrator uses this to correctly mark
        last_refresh_attempt_failed_at only in the genuine-failure case,
        never for an honestly-empty-but-successful response."""
        from data_model import ValuationData
        start = time.time()
        try:
            result = self._pl.fetch_yahoo_broker_target(identity.ticker)
        except Exception as e:
            self._health.record_failure("Yahoo Finance (enrichment)", e)
            return ValuationData(), {}, True
        self._health.record_success("Yahoo Finance (enrichment)", time.time() - start)
        vd = ValuationData()
        if not result:
            return vd, {}, False
        now = datetime.now(timezone.utc)
        if result.get("targetMeanPrice") is not None:
            vd.target_mean_price = DataPoint(result.get("targetMeanPrice"), "Yahoo Finance", now, now,
                                              DataFreshness.DELAYED, 0.7)
            vd.target_high_price = DataPoint(result.get("targetHighPrice"), "Yahoo Finance", now, now,
                                              DataFreshness.DELAYED, 0.7)
            vd.target_low_price = DataPoint(result.get("targetLowPrice"), "Yahoo Finance", now, now,
                                             DataFreshness.DELAYED, 0.7)
            vd.number_of_analyst_opinions = DataPoint(result.get("numberOfAnalystOpinions"), "Yahoo Finance",
                                                       now, now, DataFreshness.DELAYED, 0.7)
            vd.recommendation_key = DataPoint(result.get("recommendationKey"), "Yahoo Finance", now, now,
                                               DataFreshness.DELAYED, 0.7)
        return vd, {"averageVolume": result.get("averageVolume"), "currentVolume": result.get("currentVolume"),
                     "nextEarningsDate": result.get("nextEarningsDate")}, False

    def fetch_fundamentals(self, identity: StockIdentity):
        from data_model import FundamentalData
        return FundamentalData()  # honest gap - see docstring


class ExistingPipelineNewsProvider(NewsProvider):
    """Reuses this project's own news pipeline as-is (multi-source,
    same-day filtered, near-duplicate deduplicated) rather than
    building a separate fetch for the scanner."""

    def __init__(self, poll_live_module, health: ProviderHealthMonitor):
        self._pl = poll_live_module
        self._health = health

    def capabilities(self):
        return ProviderCapabilities(
            name="Existing news pipeline (Google/FT/Yahoo/Reuters-Bloomberg)",
            supported_exchanges=["LSE"], supported_instruments="Any ticker with a resolvable company name",
            available_fields=["headline", "source", "pubDate", "category"],
            update_frequency_seconds=300, historical_depth_days=1,
            typical_freshness=DataFreshness.REAL_TIME,
            rate_limit_note="Per-symbol, no batch API - see this session's own capped "
                             "expansion design for bounding request volume on broad lists.",
        )

    def fetch_news(self, identity: StockIdentity, items_by_ticker: dict = None):
        from data_model import NewsData
        nd = NewsData()
        items = (items_by_ticker or {}).get(identity.ticker, [])
        now = datetime.now(timezone.utc)
        nd.recent_items = DataPoint(items if items else None, "Existing news pipeline", now, now,
                                     DataFreshness.REAL_TIME if items else DataFreshness.UNKNOWN,
                                     0.9 if items else 0.0)
        nd.item_count_today = len(items)
        return nd
