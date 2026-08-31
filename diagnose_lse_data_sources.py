"""
Diagnostic: discover how londonstockexchange.com actually supplies its
FTSE 100 risers/fallers/volume-leaders, heatmap, and news-explorer data.

WHY THIS SCRIPT EXISTS, RUN HERE, RUN BY YOU:
Claude's own interactive sandbox is network-restricted and cannot reach
londonstockexchange.com at all (confirmed directly - the sandbox's own
egress proxy blocks the connection before it ever reaches LSE's
servers). GitHub Actions runners have normal outbound internet access.
This script has to be run there, not by Claude directly - Claude has no
ability to trigger or view GitHub Actions runs itself.

WHAT THIS DOES:
Uses a real, ordinary headless browser (Playwright/Chromium) to load
each target page exactly as a normal visitor's browser would, and
records every network response made during that normal page load. This
is the legitimate way to discover which request actually supplies the
displayed data - it's the same information visible in any browser's
own DevTools Network tab, just captured programmatically. No
authentication, anti-bot, CAPTCHA, or access-control mechanism is
touched or bypassed - if the page requires something Claude/this
script can't get past normally, that itself is a valid, useful result
to report back honestly.

ALSO tests the already-confirmed-working baseline sources this project
already uses elsewhere (yfiua FTSE100 constituent JSON, Yahoo Finance
quote endpoint) as a sanity check, so this run produces something
concrete regardless of whether the LSE discovery succeeds.

OUTPUT: prints a clearly structured report to stdout (visible in the
GitHub Actions log) and also writes it to lse_diagnostic_report.json
so it can be inspected as a workflow artifact.
"""
import json
import sys
import time
import urllib.request
from datetime import datetime, timezone

REPORT = {
    "ranAt": datetime.now(timezone.utc).isoformat(),
    "sandboxNote": (
        "Run from GitHub Actions (internet-enabled), not Claude's own "
        "interactive sandbox, which cannot reach any of these hosts."
    ),
    "lsePages": {},
    "baselineSources": {},
}

TARGET_PAGES = {
    "risersFallersVolume": "https://www.londonstockexchange.com/indices/ftse-100/constituents/risers-and-fallers-and-volume-leaders",
    "heatmap": "https://www.londonstockexchange.com/indices/ftse-100/constituents/heatmap",
    "newsExplorer": "https://www.londonstockexchange.com/news?tab=news-explorer",
}

# Content-types worth recording in full detail - these are the ones
# likely to actually carry the page's real data, as opposed to images,
# fonts, analytics beacons, etc.
DATA_CONTENT_TYPES = ("json", "graphql")


def inspect_page(name, url, page_module):
    """Loads one page in a real browser, records every network response
    whose content-type suggests it's carrying structured data (JSON/
    GraphQL), and captures a snippet of each so a human can identify
    which one is the real data source."""
    from playwright.sync_api import sync_playwright

    result = {"url": url, "status": None, "dataRequests": [], "error": None}

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                )
            )
            captured = []

            def on_response(response):
                try:
                    ctype = response.headers.get("content-type", "")
                    if any(t in ctype.lower() for t in DATA_CONTENT_TYPES):
                        entry = {
                            "requestUrl": response.url,
                            "status": response.status,
                            "contentType": ctype,
                        }
                        try:
                            body = response.text()
                            entry["bodyPreview"] = body[:1500]
                            entry["bodyLength"] = len(body)
                        except Exception as e:
                            entry["bodyError"] = str(e)
                        captured.append(entry)
                except Exception:
                    pass  # a single response failing to log shouldn't kill the whole run

            page.on("response", on_response)
            resp = page.goto(url, timeout=30000, wait_until="networkidle")
            result["status"] = resp.status if resp else None
            time.sleep(2)  # let any late XHR calls finish
            result["dataRequests"] = captured
            result["pageTitle"] = page.title()
            browser.close()
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"

    return result


def test_baseline_sources():
    """Sanity-check the data sources this project ALREADY uses elsewhere
    (already working in production, per the existing codebase) - gives
    this run something concrete to report even if LSE discovery fails
    outright, and confirms the runner's own network access is genuinely
    working (rules out 'nothing works from here' as an explanation)."""
    baseline = {}

    # Already used by fetch_ftse100_constituents() in poll.py
    try:
        req = urllib.request.Request(
            "https://yfiua.github.io/index-constituents/constituents-ftse100.json",
            headers={"User-Agent": "Mozilla/5.0"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8")
            data = json.loads(body)
            baseline["yfiuaFtse100Json"] = {
                "status": resp.status,
                "rowCount": len(data) if isinstance(data, list) else None,
                "samplRow": data[0] if isinstance(data, list) and data else None,
            }
    except Exception as e:
        baseline["yfiuaFtse100Json"] = {"error": f"{type(e).__name__}: {e}"}

    # Already used by fetch_yahoo_quote() in poll.py
    try:
        req = urllib.request.Request(
            "https://query1.finance.yahoo.com/v8/finance/chart/BARC.L?interval=1d&range=1d",
            headers={"User-Agent": "Mozilla/5.0"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8")
            data = json.loads(body)
            has_price = bool(
                data.get("chart", {}).get("result", [{}])[0].get("meta", {}).get("regularMarketPrice")
            )
            baseline["yahooChartQuote"] = {"status": resp.status, "hasPriceData": has_price}
    except Exception as e:
        baseline["yahooChartQuote"] = {"error": f"{type(e).__name__}: {e}"}

    return baseline


def main():
    print("=" * 70)
    print("LSE DATA-SOURCE DIAGNOSTIC")
    print("=" * 70)

    print("\n--- Baseline: sources this project already uses elsewhere ---")
    REPORT["baselineSources"] = test_baseline_sources()
    print(json.dumps(REPORT["baselineSources"], indent=2)[:3000])

    print("\n--- LSE pages: real network inspection via headless browser ---")
    for name, url in TARGET_PAGES.items():
        print(f"\nInspecting: {name} ({url})")
        result = inspect_page(name, url, None)
        REPORT["lsePages"][name] = result
        if result.get("error"):
            print(f"  ERROR: {result['error']}")
        else:
            print(f"  HTTP status: {result['status']}")
            print(f"  Page title: {result.get('pageTitle')}")
            print(f"  Data-bearing (JSON/GraphQL) requests found: {len(result['dataRequests'])}")
            for req in result["dataRequests"][:10]:
                print(f"    - {req['status']} {req['contentType']}  {req['requestUrl'][:120]}")

    with open("lse_diagnostic_report.json", "w") as f:
        json.dump(REPORT, f, indent=2)
    print("\nFull report written to lse_diagnostic_report.json")
    print("=" * 70)


if __name__ == "__main__":
    main()
