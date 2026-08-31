"""
Diagnostic v3: full-body inspection of api.londonstockexchange.com's
/api/v1/components/refresh endpoint - the strongest candidate found in
v2 for where LSE's actual widget data lives (same first-party domain
as /api/v1/pages, but its response size varies meaningfully per page,
unlike /api/v1/pages which was identical across all three targets).

Maps the actual chain: page -> /api/v1/pages -> /api/v1/components/
refresh -> [next endpoint, if that's also just config] -> real data OR
a confirmed Refinitiv/LSEG requirement.

Same ground rules as v1/v2: ordinary browser, ordinary page load, no
auth bypass, no credential extraction, no anti-bot circumvention. Any
endpoint that legitimately requires a session the public page itself
established is reported as such, never worked around.

Run only in GitHub Actions - not usable from Claude's own sandbox.
"""
import json
import re
import time
from datetime import datetime, timezone

REPORT = {"ranAt": datetime.now(timezone.utc).isoformat(), "pages": {}}

TARGET_PAGES = {
    "risersFallersVolume": "https://www.londonstockexchange.com/indices/ftse-100/constituents/risers-and-fallers-and-volume-leaders",
    "heatmap": "https://www.londonstockexchange.com/indices/ftse-100/constituents/heatmap",
    "newsExplorer": "https://www.londonstockexchange.com/news?tab=news-explorer",
}

# What would actually prove real market/news content is present -
# checked as ACTUAL KEY NAMES in the parsed structure (never a
# substring match against arbitrary body text, which is what produced
# v2's false-positive "change"/"bid" hits against UI label config).
REAL_DATA_KEY_PATTERNS = {
    "constituents/tickers": re.compile(r"^(ticker|symbol|epic|isin|constituent)s?$", re.I),
    "price fields": re.compile(r"^(lastprice|last_price|closeprice|previousclose|bidprice|askprice)$", re.I),
    "change fields": re.compile(r"^(percentchange|pctchange|netchange|changepercent|change1d)$", re.I),
    "volume": re.compile(r"^(volume|tradedvolume|dayvolume)$", re.I),
    "market cap": re.compile(r"^(marketcap|market_cap)$", re.I),
    "sector": re.compile(r"^(sector|industry|gics)$", re.I),
    "news/RNS": re.compile(r"^(headline|rns|announcement|newsdate|publisheddate|storyid)$", re.I),
}

NOISE_HOSTS = ("cookielaw", "onetrust", "demdex", "company-target", "google", "adobe",
               "w3.org", "schema.org", "fonts.", "twitter.com", "doubleclick")


def collect_keys_with_sample_values(obj, out=None, path=""):
    """Recursively walks the parsed JSON, recording every key alongside
    ONE example value - lets us check real key names against
    REAL_DATA_KEY_PATTERNS precisely, and show a human a concrete
    example rather than just a key list."""
    if out is None:
        out = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            key_path = f"{path}.{k}" if path else k
            if not isinstance(v, (dict, list)):
                out[key_path] = v
            collect_keys_with_sample_values(v, out, key_path)
    elif isinstance(obj, list) and obj:
        collect_keys_with_sample_values(obj[0], out, path + "[0]")
    return out


def classify_payload(parsed):
    """Checks actual key names (last path segment) against the real-data
    patterns - returns which categories of genuine data, if any, are
    present, each with one concrete example value."""
    if parsed is None:
        return {}
    kv = collect_keys_with_sample_values(parsed)
    findings = {}
    for label, pattern in REAL_DATA_KEY_PATTERNS.items():
        for full_key, value in kv.items():
            leaf = full_key.rsplit(".", 1)[-1].split("[")[0]
            if pattern.match(leaf):
                findings[label] = {"key": full_key, "exampleValue": value}
                break
    return findings


def extract_urls(text):
    return sorted(set(re.findall(r'https?://[^\s"\'<>\\]+', text)))


def inspect_page(name, url):
    from playwright.sync_api import sync_playwright

    result = {
        "url": url, "status": None, "error": None,
        "componentsRefresh": None, "allJsonEndpoints": [],
    }

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(
                user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
            )
            captured = {}

            def on_response(response):
                try:
                    ctype = response.headers.get("content-type", "")
                    if "json" not in ctype.lower():
                        return
                    req = response.request
                    captured[response.url] = {
                        "status": response.status,
                        "method": req.method,
                        "requestPostData": req.post_data,
                        "responseHeaders": dict(response.headers()),
                        "body": response.text(),
                    }
                except Exception:
                    pass

            page.on("response", on_response)
            resp = page.goto(url, timeout=30000, wait_until="networkidle")
            result["status"] = resp.status if resp else None

            try:
                accept_btn = page.locator("#onetrust-accept-btn-handler")
                if accept_btn.is_visible(timeout=5000):
                    accept_btn.click()
                    page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass

            time.sleep(3)
            browser.close()

            # Record EVERY json endpoint seen (minus obvious tracking/consent
            # noise) - broadens News Explorer coverage specifically, in case
            # its real data comes from something not named "components/refresh"
            # at all.
            for req_url, entry in captured.items():
                if any(n in req_url for n in NOISE_HOSTS):
                    continue
                result["allJsonEndpoints"].append({
                    "url": req_url, "status": entry["status"], "method": entry["method"],
                    "bodyLength": len(entry["body"]),
                })

            refresh_entries = [(u, e) for u, e in captured.items() if "components/refresh" in u]
            if refresh_entries:
                req_url, entry = refresh_entries[0]
                parsed = None
                try:
                    parsed = json.loads(entry["body"])
                except Exception as e:
                    result["componentsRefresh"] = {"parseError": str(e), "bodyPreview": entry["body"][:2000]}
                if parsed is not None:
                    findings = classify_payload(parsed)
                    referenced = extract_urls(entry["body"])
                    top_keys = list(parsed.keys()) if isinstance(parsed, dict) else (
                        f"<list of {len(parsed)}>" if isinstance(parsed, list) else str(type(parsed)))
                    result["componentsRefresh"] = {
                        "url": req_url, "method": entry["method"], "status": entry["status"],
                        "requestPostData": entry["requestPostData"],
                        "responseHeaders": {k: v for k, v in entry["responseHeaders"].items()
                                            if k.lower() in ("content-type", "cache-control", "x-provider",
                                                              "x-data-source", "content-length")},
                        "bodyLength": len(entry["body"]), "topLevelStructure": top_keys,
                        "realDataFindings": findings,
                        "referencedUrls": [u for u in referenced if not any(n in u for n in NOISE_HOSTS)][:20],
                        "bodyFull": entry["body"],  # full body, as required
                    }

    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"

    return result


def main():
    print("=" * 70)
    print("LSE DATA-SOURCE DIAGNOSTIC v3 - components/refresh full-body inspection")
    print("=" * 70)

    for name, url in TARGET_PAGES.items():
        print(f"\n{'=' * 50}\nInspecting: {name}\n{'=' * 50}")
        result = inspect_page(name, url)
        REPORT["pages"][name] = result

        if result.get("error"):
            print(f"  ERROR: {result['error']}")
            continue

        print(f"\n  All JSON endpoints seen (excluding tracking/consent noise):")
        for e in result["allJsonEndpoints"]:
            print(f"    - {e['method']} {e['status']} {e['url'][:110]} (len={e['bodyLength']})")

        cr = result.get("componentsRefresh")
        if not cr:
            print("\n  components/refresh: NOT CAPTURED on this page")
            continue

        if cr.get("parseError"):
            print(f"\n  components/refresh: FAILED TO PARSE AS JSON: {cr['parseError']}")
            print(f"    body preview: {cr['bodyPreview'][:500]}")
            continue

        print(f"\n  components/refresh:")
        print(f"    method: {cr['method']}, status: {cr['status']}, body length: {cr['bodyLength']}")
        print(f"    request post data: {cr['requestPostData']}")
        print(f"    relevant response headers: {cr['responseHeaders']}")
        print(f"    top-level structure: {cr['topLevelStructure']}")
        print(f"    REAL DATA FINDINGS (actual key match, not substring search):")
        if cr["realDataFindings"]:
            for label, info in cr["realDataFindings"].items():
                print(f"      -> {label}: key='{info['key']}' example={info['exampleValue']!r}")
        else:
            print("      -> NONE FOUND. This response does not contain recognizable market/news data fields.")
        print(f"    referenced URLs found in body ({len(cr['referencedUrls'])}):")
        for u in cr["referencedUrls"][:15]:
            print(f"      -> {u}")

    with open("lse_diagnostic_report_v3.json", "w") as f:
        json.dump(REPORT, f, indent=2)
    print("\n\nFull report (including complete response bodies) written to lse_diagnostic_report_v3.json")
    print("=" * 70)


if __name__ == "__main__":
    main()
