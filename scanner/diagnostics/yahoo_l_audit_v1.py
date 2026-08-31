"""
Yahoo Finance .L capability audit - v1.

Tests real Yahoo quoteSummary modules against real LSE (.L) securities
spanning multiple sectors, and saves the RAW responses for inspection.
This exists because this session's sandbox has no network access to
Yahoo Finance, and web_fetch is restricted to pre-surfaced URLs -
the only honest way to answer "which fields does Yahoo actually
provide for .L securities" is to genuinely fetch and look, not guess
from general knowledge of Yahoo's US-market schema (which may not
hold for LSE-listed names).

Run via GitHub Actions (same runner that already has working Yahoo
access, confirmed throughout this session's poller runs) - not
locally, not from this sandbox.

Tests a representative ticker per sector, and a WIDE set of modules
(going beyond what fetch_yahoo_analyst currently requests) to
discover fields not yet extracted, per the explicit "do not conclude
Yahoo cannot provide X until tested" instruction.
"""
import json
import urllib.request
import urllib.parse
import time
import sys
from datetime import datetime, timezone

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
}

# One representative ticker per sector, spanning market-cap tiers -
# not exhaustive, but enough to see whether coverage genuinely differs
# by sector/size for non-US securities.
TEST_TICKERS = {
    "Banking (large-cap)": "BARC.L",
    "Mining (large-cap)": "AAL.L",
    "Consumer staples (large-cap)": "ULVR.L",
    "Insurance (large-cap)": "PRU.L",
    "Utilities (large-cap)": "SSE.L",
    "Retail (mid-cap)": "TSCO.L",
    "Pharma (large-cap)": "GSK.L",
}

# Every module worth testing - deliberately wider than what
# fetch_yahoo_analyst currently requests, to discover untapped fields.
MODULES = [
    "price", "summaryDetail", "defaultKeyStatistics", "financialData",
    "incomeStatementHistory", "incomeStatementHistoryQuarterly",
    "balanceSheetHistory", "balanceSheetHistoryQuarterly",
    "cashflowStatementHistory", "cashflowStatementHistoryQuarterly",
    "earnings", "earningsHistory", "earningsTrend",
    "recommendationTrend", "upgradeDowngradeHistory",
    "calendarEvents", "assetProfile", "esgScores",
]


def get_crumb():
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
    req = urllib.request.Request("https://fc.yahoo.com/", headers=HEADERS)
    try:
        opener.open(req, timeout=10)
    except Exception:
        pass
    req2 = urllib.request.Request("https://query1.finance.yahoo.com/v1/test/getcrumb", headers=HEADERS)
    try:
        with opener.open(req2, timeout=10) as resp:
            crumb = resp.read().decode().strip()
            return crumb, opener
    except Exception as e:
        print(f"  ! crumb fetch failed: {e}", file=sys.stderr)
        return None, opener


def fetch_modules(opener, crumb, ticker, modules):
    url = (
        f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{urllib.parse.quote(ticker)}"
        f"?modules={','.join(modules)}"
    )
    if crumb:
        url += f"&crumb={urllib.parse.quote(crumb)}"
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with opener.open(req, timeout=15) as resp:
            status = resp.status
            body = resp.read()
            return status, body
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:
        return None, str(e).encode()


def main():
    crumb, opener = get_crumb()
    print(f"Crumb obtained: {bool(crumb)}")
    results = {}
    for sector, ticker in TEST_TICKERS.items():
        print(f"\nFetching {ticker} ({sector})...")
        status, body = fetch_modules(opener, crumb, ticker, MODULES)
        print(f"  HTTP {status}, {len(body)} bytes")
        try:
            parsed = json.loads(body)
            result = (((parsed.get("quoteSummary") or {}).get("result")) or [None])[0]
            if result:
                modules_present = sorted(result.keys())
                print(f"  Modules present in response: {modules_present}")
            error = (parsed.get("quoteSummary") or {}).get("error")
            if error:
                print(f"  ERROR in response: {error}")
        except Exception as e:
            print(f"  Could not parse as JSON: {e}")
        results[ticker] = {"sector": sector, "status": status, "body": body.decode(errors="replace")}
        time.sleep(2)  # stagger, same principle as the rest of this project's Yahoo calls

    out_path = f"yahoo_l_audit_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nSaved raw responses to {out_path}")


if __name__ == "__main__":
    main()
