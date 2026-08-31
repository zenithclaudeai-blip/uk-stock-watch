"""
LSE Opportunity Scanner - cross-run persistence.

The core rule this module exists to enforce: a failed or skipped
refresh NEVER overwrites a field with None/empty. Only a genuinely new,
valid value updates a field's stored state. Everything else - a
timeout, an empty response, a rate-limit, simply not being in this
run's rotation - leaves the LAST VALID value exactly as it was, with
its own real timestamp intact, so its age is computed honestly rather
than reset.

Persisted as a plain JSON-serializable dict, one entry per ticker, same
pattern this project already uses for its other state files (e.g.
radar_history.json) - no new persistence mechanism invented here.
"""
from dataclasses import fields as dataclass_fields
from datetime import datetime, timezone

from data_model import DataPoint, DataFreshness


def _datapoint_to_dict(dp: DataPoint) -> dict:
    return {
        "value": dp.value, "source": dp.source,
        "timestamp": dp.timestamp.isoformat() if dp.timestamp else None,
        "fetchedAt": dp.fetched_at.isoformat() if dp.fetched_at else None,
        "freshness": dp.freshness.value if dp.freshness else None,
        "confidence": dp.confidence,
    }


def _datapoint_from_dict(d: dict) -> DataPoint:
    if not d or d.get("value") is None:
        return DataPoint()
    ts = datetime.fromisoformat(d["timestamp"]) if d.get("timestamp") else None
    fa = datetime.fromisoformat(d["fetchedAt"]) if d.get("fetchedAt") else None
    try:
        freshness = DataFreshness(d.get("freshness")) if d.get("freshness") else DataFreshness.UNKNOWN
    except ValueError:
        freshness = DataFreshness.UNKNOWN
    return DataPoint(value=d["value"], source=d.get("source"), timestamp=ts, fetched_at=fa,
                      freshness=freshness, confidence=d.get("confidence", 0.0))


# Which (section, field) pairs on a StockRecord are persisted across
# runs - the SLOW-moving fields (analyst/fundamentals/volume-average)
# that the bounded rotation doesn't refresh every run. FAST fields
# (price/changePct/volume itself) are deliberately NOT persisted here -
# they come fresh from LSE every single run at no extra cost, so
# carrying forward a stale price would be actively wrong, not helpful.
PERSISTED_FIELDS = [
    ("valuation", "target_mean_price"),
    ("valuation", "target_high_price"),
    ("valuation", "target_low_price"),
    ("valuation", "number_of_analyst_opinions"),
    ("valuation", "recommendation_key"),
    ("volume", "average_volume"),
    # "volume.volume" (current volume) is ALSO persisted, even though
    # it's conceptually a "fast" field - because LSE's Heatmap universe
    # source (this scanner's ONLY current universe provider) has NO
    # volume field at all. Current volume only ever arrives via the
    # Yahoo fallback, which only fires during the bounded rotation -
    # so for THIS specific universe source it behaves as a slow field,
    # not a genuinely-fresh-every-run one. apply_persisted_data's own
    # "fresh always wins" rule still protects this correctly for any
    # FUTURE universe provider that does supply live volume from LSE
    # directly (e.g. risersFallersVolume) - a real LSE value this run
    # is never overwritten by a stale persisted one.
    ("volume", "volume"),
    ("fundamentals", "market_cap"),
    ("fundamentals", "pe_ratio"),
    ("fundamentals", "dividend_yield"),
    ("earnings", "next_earnings_date"),
]


def record_to_persisted_dict(stock_record) -> dict:
    """Extracts just the PERSISTED_FIELDS from a StockRecord, as a
    plain JSON-serializable dict, for writing to the store."""
    out = {}
    for section, field_name in PERSISTED_FIELDS:
        dp = getattr(getattr(stock_record, section), field_name)
        if dp.is_available:  # only ever persist a genuinely valid value
            out[f"{section}.{field_name}"] = _datapoint_to_dict(dp)
    return out


def apply_persisted_data(stock_record, persisted_dict: dict):
    """
    For each PERSISTED_FIELDS entry: if the fresh stock_record already
    has a genuinely available value for it (this run's own fetch
    succeeded), leave it alone - fresh always wins over persisted. If
    the fresh record does NOT have it (this run didn't refresh it, or
    the refresh failed), fall back to whatever's in persisted_dict,
    carrying its REAL original timestamp forward so age is computed
    honestly rather than reset to "now".

    This is the one function that enforces the cross-run rule: a
    failed/skipped refresh can never turn "available" into "missing".
    """
    if not persisted_dict:
        return stock_record
    for section, field_name in PERSISTED_FIELDS:
        current_dp = getattr(getattr(stock_record, section), field_name)
        if current_dp.is_available:
            continue  # fresh data this run - never overwritten by stale persisted data
        key = f"{section}.{field_name}"
        if key in persisted_dict:
            restored = _datapoint_from_dict(persisted_dict[key])
            if restored.is_available:
                setattr(getattr(stock_record, section), field_name, restored)
    return stock_record


def merge_scan_into_store(store: dict, records: dict) -> dict:
    """
    After a scan, updates the persisted store: for each ticker, any
    field that has a genuinely fresh value this run overwrites the
    stored one (with its real new timestamp); everything else in the
    store is left untouched (so tickers NOT scanned this run, or
    fields that failed to refresh, keep their last known valid state).
    """
    store = dict(store)
    for ticker, record in records.items():
        existing = store.get(ticker, {})
        fresh = record_to_persisted_dict(record)
        merged = dict(existing)
        merged.update(fresh)  # fresh values overwrite; fields not in `fresh` are left as-is
        store[ticker] = merged
    return store
