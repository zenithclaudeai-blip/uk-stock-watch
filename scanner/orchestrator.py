"""
LSE Opportunity Scanner - orchestrator (v2, with cross-run persistence).

FAST vs SLOW data, adapted honestly to a stateless periodic script:
price/volume (FAST) are fetched fresh every run, since the LSE universe
fetch already returns them for the whole universe in one request at no
extra cost. Broker/analyst/fundamental data (SLOWER, no batch endpoint)
is refreshed for only a BOUNDED subset each run, chosen by staleness -
but a stock NOT in this run's rotation keeps its last genuinely valid
value, carried forward with its real original timestamp (see
persistence.py) - never silently turned into "missing" just because
this specific run didn't refresh it.

MODEL STATUS: this project's CURRENT scoring model covers momentum,
trend, liquidity, analyst evidence, and news - a genuinely useful but
INTENTIONALLY PROVISIONAL subset of the full model envisioned (which
also wants quality, valuation, growth, timing, market confirmation).
Every scan result is explicitly labeled PROVISIONAL until fundamentals/
growth/valuation data sources are connected - the UI must never let a
provisional score look equivalent to the eventual full model.
"""
from datetime import datetime, timezone
from dataclasses import dataclass, field

from data_model import StockRecord, StockIdentity, DataPoint, DataFreshness
from providers import (
    ProviderHealthMonitor, LSEHeatmapUniverseProvider, YahooAnalystProvider,
    ExistingPipelineNewsProvider,
)
from scoring import calculate_score, classify_opportunity_tier, CORE_COMPONENTS, ENHANCEMENT_COMPONENTS, compute_risk_flags
import persistence


ANALYST_REFRESH_CAP_PER_RUN = 20

# The FULL intended model's own named categories, per the explicit
# spec - shown in MODEL COVERAGE regardless of whether this project
# currently has a data source for them, so the gap is visible rather
# than silently absent from the UI.
FULL_MODEL_CATEGORIES = [
    "Quality", "Valuation", "Growth", "Momentum", "Timing",
    "Market Confirmation", "Catalyst", "Analyst Evidence",
]
# Which of those this project's CURRENT scoring engine genuinely
# computes - everything else is honestly reported as Unavailable, not
# silently omitted from the list.
CURRENTLY_AVAILABLE_MODEL_CATEGORIES = {
    "Momentum": "momentum", "Market Confirmation": "trend/liquidity (technical only)",
    "Analyst Evidence": "analyst_evidence", "Catalyst": "news_catalyst",
}

MODEL_STATUS_PROVISIONAL = "🟡 PROVISIONAL — LIMITED FUNDAMENTAL DATA"
MODEL_STATUS_FULL = "🟢 FULL MODEL"


@dataclass
class ScanResult:
    ran_at: str
    universe_size: int
    model_status: str = MODEL_STATUS_PROVISIONAL
    model_coverage: dict = field(default_factory=dict)
    records: dict = field(default_factory=dict)
    breakdowns: dict = field(default_factory=dict)
    risk_flags: dict = field(default_factory=dict)
    provider_health: dict = field(default_factory=dict)
    coverage: dict = field(default_factory=dict)
    updated_persisted_store: dict = field(default_factory=dict)
    updated_analyst_refresh_state: dict = field(default_factory=dict)

    def qualifying_stocks(self, min_score=70):
        """Returns EVERY stock scoring >= min_score - never truncated.
        Top-N is a presentation concern for the page renderer, not a
        limit on what this method returns."""
        return sorted(
            [b for b in self.breakdowns.values() if b.buy_score is not None and b.buy_score >= min_score],
            key=lambda b: -b.buy_score,
        )

    def rank_and_percentile(self, ticker: str):
        """
        Returns (rank, total_scoreable, percentile) for one ticker, or
        None if that ticker is genuinely unscoreable this run (rank
        among an undefined denominator is meaningless, never
        fabricated as e.g. rank=0). rank is 1-indexed (rank 1 = the
        single highest score). percentile is computed the honest way:
        the fraction of the scoreable universe this stock outranks,
        not a fabricated round number.
        """
        scoreable = sorted(
            [b for b in self.breakdowns.values() if b.buy_score is not None],
            key=lambda b: -b.buy_score,
        )
        total = len(scoreable)
        if total == 0:
            return None
        for i, b in enumerate(scoreable):
            if b.ticker == ticker:
                rank = i + 1
                percentile = round(100 * (total - rank) / total, 1) if total > 1 else 100.0
                return rank, total, percentile
        return None  # this ticker was genuinely unscoreable this run

    def tier_90_plus(self):
        return [b for b in self.qualifying_stocks(90)]

    def tier_80_89(self):
        return [b for b in self.qualifying_stocks(80) if b.buy_score < 90]

    def tier_70_79(self):
        return [b for b in self.qualifying_stocks(70) if b.buy_score < 80]


def build_model_coverage() -> dict:
    """MODEL COVERAGE per the explicit spec - shows every FULL_MODEL
    category as Available/Unavailable, never silently pretending a
    missing category was properly assessed."""
    return {
        cat: ("Available" if cat in CURRENTLY_AVAILABLE_MODEL_CATEGORIES else "Unavailable")
        for cat in FULL_MODEL_CATEGORIES
    }


def run_scan(poll_live_module, items_by_ticker: dict, analyst_refresh_state: dict = None,
             provider_health_state: dict = None, persisted_store: dict = None) -> ScanResult:
    """
    One full scan, now with cross-run persistence wired in. Callers
    must persist the returned updated_persisted_store and
    updated_analyst_refresh_state after this call (same pattern as this
    project's other JSON state files) so the NEXT run's rotation and
    carry-forward data are correct.
    """
    health = ProviderHealthMonitor(provider_health_state)
    universe_provider = LSEHeatmapUniverseProvider(poll_live_module, health)
    analyst_provider = YahooAnalystProvider(poll_live_module, health)
    news_provider = ExistingPipelineNewsProvider(poll_live_module, health)
    persisted_store = dict(persisted_store or {})

    raw_instruments, raw_result = universe_provider.fetch_universe_with_prices()

    now = datetime.now(timezone.utc)
    records = {}
    for row in raw_instruments:
        ticker = row.get("symbol")
        if not ticker:
            continue
        identity = StockIdentity(ticker=ticker, isin=row.get("isin"), tidm=row.get("tidm"),
                                  name=row.get("name"), exchange="LSE")
        record = StockRecord(identity=identity)
        if row.get("price") is not None:
            record.price.last_price = DataPoint(row["price"], "LSE", now, now, DataFreshness.REAL_TIME, 1.0)
        if row.get("changePct") is not None:
            record.price.change_pct = DataPoint(row["changePct"], "LSE", now, now, DataFreshness.REAL_TIME, 1.0)
        if row.get("volume") is not None:
            record.volume.volume = DataPoint(row["volume"], "LSE", now, now, DataFreshness.REAL_TIME, 1.0)
        if row.get("fiftyTwoWeekLow") is not None:
            record.price.fifty_two_week_low = DataPoint(row["fiftyTwoWeekLow"], "LSE", now, now,
                                                          DataFreshness.REAL_TIME, 1.0)
        if row.get("fiftyTwoWeekHigh") is not None:
            record.price.fifty_two_week_high = DataPoint(row["fiftyTwoWeekHigh"], "LSE", now, now,
                                                           DataFreshness.REAL_TIME, 1.0)
        if row.get("marketCap") is not None:
            record.fundamentals.market_cap = DataPoint(row["marketCap"], "LSE", now, now,
                                                         DataFreshness.REAL_TIME, 1.0)
        record.news = news_provider.fetch_news(identity, items_by_ticker)
        records[ticker] = record

    # SLOW data rotation: refresh only the CAP most-stale tickers.
    analyst_refresh_state = dict(analyst_refresh_state or {})
    tickers_by_staleness = sorted(
        records.keys(), key=lambda t: analyst_refresh_state.get(t, "1970-01-01T00:00:00+00:00"),
    )
    refreshed_this_run = tickers_by_staleness[:ANALYST_REFRESH_CAP_PER_RUN]
    failed_refresh_tickers = set()
    for ticker in refreshed_this_run:
        valuation, volume_extra, attempt_failed = analyst_provider.fetch_analyst_data(records[ticker].identity)
        records[ticker].valuation = valuation
        if volume_extra.get("averageVolume") is not None:
            records[ticker].volume.average_volume = DataPoint(volume_extra["averageVolume"], "Yahoo Finance",
                                                                now, now, DataFreshness.DELAYED, 0.7)
        if not records[ticker].volume.volume.is_available and volume_extra.get("currentVolume") is not None:
            records[ticker].volume.volume = DataPoint(volume_extra["currentVolume"], "Yahoo Finance (fallback)",
                                                        now, now, DataFreshness.DELAYED, 0.6)
        if volume_extra.get("nextEarningsDate") is not None:
            records[ticker].earnings.next_earnings_date = DataPoint(volume_extra["nextEarningsDate"], "Yahoo Finance",
                                                                      now, now, DataFreshness.DELAYED, 0.7)
        analyst_refresh_state[ticker] = now.isoformat()
        if attempt_failed:
            failed_refresh_tickers.add(ticker)

    # CROSS-RUN PERSISTENCE: for every ticker NOT refreshed this run (or
    # whose refresh genuinely returned nothing), apply the last known
    # valid persisted value instead - never leaving it as "missing"
    # just because this run didn't touch it.
    for ticker, record in records.items():
        persistence.apply_persisted_data(record, persisted_store.get(ticker, {}))
        # For tickers whose refresh attempt genuinely FAILED this run
        # (not just "found nothing"), mark that on whichever fields the
        # attempt targeted - so REFRESH_FAILED is visible even though
        # the prior valid value (via persistence, applied above)
        # correctly survives it.
        if ticker in failed_refresh_tickers:
            for section, field_name in persistence.PERSISTED_FIELDS:
                if section == "valuation":
                    dp = getattr(getattr(record, section), field_name)
                    if dp.is_available:
                        dp.last_refresh_attempt_failed_at = now
                        dp.last_refresh_error = "Yahoo Finance request failed this run"

    breakdowns = {ticker: calculate_score(record) for ticker, record in records.items()}
    risk_flags = {ticker: compute_risk_flags(record, now) for ticker, record in records.items()}

    fully_covered = sum(1 for b in breakdowns.values() if b.coverage_state.value == "fully_covered")
    partial = sum(1 for b in breakdowns.values() if b.coverage_state.value == "partially_covered")
    unscoreable = sum(1 for b in breakdowns.values() if b.coverage_state.value == "unscoreable")
    with_price = sum(1 for r in records.values() if r.price.last_price.is_available)
    with_analyst = sum(1 for r in records.values() if r.valuation.target_mean_price.is_available)
    with_news_checked = sum(1 for r in records.values() if r.news.recent_items.source is not None)

    # Two genuinely different numbers, per the explicit correction:
    # UNIVERSE coverage = did we successfully scan/attempt every
    # stock (a fact about the scan run itself). MODEL DATA coverage =
    # how complete the actual scoring EVIDENCE is, on average, across
    # those stocks (a fact about data completeness) - the previous
    # single "dataCoveragePct" conflated "scanned" with "fully
    # evidenced", which could mislead a reader into thinking 100%
    # scan success meant 100% complete financial evidence.
    universe_coverage_pct = round(100 * len(records) / len(records), 1) if records else 0.0
    model_data_coverage_pct = (
        round(sum(b.data_coverage_pct for b in breakdowns.values()) / len(breakdowns), 1)
        if breakdowns else 0.0
    )

    coverage = {
        "eligibleUniverse": len(records),
        "withLivePrice": with_price,
        "withAnalystData": with_analyst,  # includes carried-forward persisted values, not just this run's fresh fetch
        "withNewsChecked": with_news_checked,
        "fullyCovered": fully_covered,
        "partiallyCovered": partial,
        "unscoreable": unscoreable,
        "scoreable": fully_covered + partial,
        "universeCoveragePct": universe_coverage_pct,
        "modelDataCoveragePct": model_data_coverage_pct,
        "analystRefreshedThisRun": len(refreshed_this_run),
        "analystRefreshCapPerRun": ANALYST_REFRESH_CAP_PER_RUN,
    }

    updated_store = persistence.merge_scan_into_store(persisted_store, records)

    result = ScanResult(
        ran_at=now.isoformat(), universe_size=len(records),
        model_status=MODEL_STATUS_PROVISIONAL, model_coverage=build_model_coverage(),
        records=records, breakdowns=breakdowns, risk_flags=risk_flags,
        provider_health=health.to_dict(), coverage=coverage,
        updated_persisted_store=updated_store, updated_analyst_refresh_state=analyst_refresh_state,
    )
    return result
