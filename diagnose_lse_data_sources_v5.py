"""
Diagnostic v5, two parts:

PART A - Standalone components/refresh test (Risers/Fallers/Volume,
Heatmap): captures the EXACT POST request the real browser sends to
api.londonstockexchange.com/api/v1/components/refresh (URL, method,
body), then replays that exact request via a completely fresh Python
urllib call - no cookies, no browser context, no Refinitiv/SAML
session, nothing carried over. This is the real test of whether our
backend could call this endpoint independently, not an inference from
request ordering.

PART B - News Explorer deep investigation: v4's capture window closed
before any story-list request fired. v5 waits substantially longer,
scrolls the page, and tries (safely, wrapped so a missing element never
crashes the run) to interact with pagination/filter/search controls if
present, capturing every JSON response chronologically throughout.

Same ground rules as v1-v4: ordinary browser, ordinary page load, no
auth bypass, no credential extraction, no anti-bot circumvention. A
cookie-less request that fails with 401/403 is not retried with any
extracted token - that result IS the answer, reported as such.

Run only in GitHub Actions - not usable from Claude's own sandbox,
which cannot reach any of these hosts at all.
"""
import json
import os
import re
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone

REPORT = {"ranAt": datetime.now(timezone.utc).isoformat(), "partA": {}, "partB": {}}
BODIES_DIR = "lse_bodies_v5"

MARKET_PAGES = {
    "risersFallersVolume": "https://www.londonstockexchange.com/indices/ftse-100/constituents/risers-and-fallers-and-volume-leaders",
    "heatmap": "https://www.londonstockexchange.com/indices/ftse-100/constituents/heatmap",
}
NEWS_URL = "https://www.londonstockexchange.com/news?tab=news-explorer"

REAL_DATA_KEY_PATTERNS = {
    "constituents/tickers": re.compile(r"^(ticker|symbol|epic|isin|constituent)s?$", re.I),
    "price fields": re.compile(r"^(lastprice|last_price|closeprice|previousclose|bidprice|askprice)$", re.I),
    "change fields": re.compile(r"^(percentchange|pctchange|netchange|changepercent|change1d)$", re.I),
    "volume": re.compile(r"^(volume|tradedvolume|dayvolume)$", re.I),
    "market cap": re.compile(r"^(marketcap|market_cap)$", re.I),
    "sector": re.compile(r"^(sector|industry|gics)$", re.I),
    "news/RNS": re.compile(r"^(headline|rns|announcement|newsdate|publisheddate|storyid|story_id)$", re.I),
    "news source/url": re.compile(r"^(source|articleurl|article_url|link)$", re.I),
    "pagination/count": re.compile(r"^(totalcount|total_count|totalresults|total_results|pagenumber|pagesize)$", re.I),
}
NOISE_HOSTS = ("cookielaw", "onetrust", "demdex", "company-target", "google", "adobe",
               "w3.org", "schema.org", "fonts.", "twitter.com", "doubleclick")


def collect_keys_with_sample_values(obj, out=None, path=""):
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


def save_body(subdir, seq, url, body):
    safe_host = re.sub(r'[^a-zA-Z0-9._-]', '_', url.split("//", 1)[-1][:80])
    dir_path = os.path.join(BODIES_DIR, subdir)
    os.makedirs(dir_path, exist_ok=True)
    file_path = os.path.join(dir_path, f"{seq:03d}_{safe_host}.json")
    with open(file_path, "w") as f:
        f.write(body)
    return file_path


# =========================================================================
# PART A - capture the real request shape, then replay it with zero session
# =========================================================================

def capture_real_request_shape(page_name, url):
    """Loads the page with a real browser ONE time, purely to record the
    EXACT components/refresh request the page itself sends (method, full
    URL, POST body, and the request headers Playwright itself set) - not
    guessed, not assumed."""
    from playwright.sync_api import sync_playwright

    captured = None
    capture_errors = []

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
        )

        def on_response(response):
            nonlocal captured
            try:
                if "components/refresh" not in response.url:
                    return
                req = response.request
                captured = {
                    "url": response.url, "method": req.method,
                    "postData": req.post_data,
                    "requestHeaders": dict(req.headers),
                    "responseStatus": response.status,
                    "responseBody": response.text(),
                }
            except Exception as e:
                capture_errors.append(f"{type(e).__name__}: {e}")

        page.on("response", on_response)
        page.goto(url, timeout=30000, wait_until="networkidle")
        try:
            accept_btn = page.locator("#onetrust-accept-btn-handler")
            if accept_btn.is_visible(timeout=5000):
                accept_btn.click()
                page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        time.sleep(3)
        browser.close()

    return captured, capture_errors


def make_standalone_request(shape):
    """A completely fresh urllib call - brand-new process-level state,
    no cookie jar, no browser, no Refinitiv/SAML session, nothing
    carried over from the Playwright capture above. Only the request
    SHAPE (URL/method/body) is reused; headers are a minimal, ordinary
    set - explicitly NO cookie header, NO Authorization/token header.
    This is the actual test of backend-independent usability."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Content-Type": "application/json",
        "Accept": "application/json",
        # Deliberately NOT copying any Cookie / Authorization / x-*-token
        # header from the captured browser request - that's the whole
        # point of this test.
    }
    data = shape["postData"].encode("utf-8") if shape["postData"] else None
    req = urllib.request.Request(shape["url"], data=data, method=shape["method"], headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return {"status": resp.status, "body": body, "bodyLength": len(body), "error": None}
    except urllib.error.HTTPError as e:
        error_body = ""
        try:
            error_body = e.read().decode("utf-8", errors="replace")[:1000]
        except Exception:
            pass
        return {"status": e.code, "body": None, "error": f"HTTP {e.code}: {e.reason}", "errorBody": error_body}
    except Exception as e:
        return {"status": None, "body": None, "error": f"{type(e).__name__}: {e}"}


def run_part_a():
    print("=" * 70)
    print("PART A - Standalone components/refresh test (no session, no cookies)")
    print("=" * 70)

    for name, url in MARKET_PAGES.items():
        print(f"\n{'-' * 50}\n{name}\n{'-' * 50}")
        shape, cap_errors = capture_real_request_shape(name, url)
        if cap_errors:
            print(f"  Capture warnings: {cap_errors}")
        if not shape:
            print("  Could not capture the real request shape on this page - skipping standalone test.")
            REPORT["partA"][name] = {"error": "components/refresh never fired during capture"}
            continue

        print(f"  Captured real request: {shape['method']} {shape['url']}")
        print(f"  Real POST body: {shape['postData']}")
        print(f"  Real request headers used by the browser: "
              f"{ {k: v for k, v in shape['requestHeaders'].items() if k.lower() in ('content-type', 'accept', 'cookie', 'authorization')} }")
        print(f"  (Browser's own request succeeded: status {shape['responseStatus']}, "
              f"{len(shape['responseBody'])} bytes)")

        standalone = make_standalone_request(shape)
        print(f"\n  STANDALONE request (fresh process, zero cookies, zero session):")
        print(f"    status: {standalone['status']}")
        if standalone["error"]:
            print(f"    error: {standalone['error']}")
            if standalone.get("errorBody"):
                print(f"    error body: {standalone['errorBody']}")
            conclusion = "Requires session/auth - standalone call failed"
        else:
            parsed = None
            try:
                parsed = json.loads(standalone["body"])
            except Exception as e:
                print(f"    response not valid JSON: {e}")
            findings = classify_payload(parsed) if parsed is not None else {}
            print(f"    response size: {standalone['bodyLength']} bytes")
            print(f"    REAL DATA FOUND (standalone, no session):")
            if findings:
                for label, info in findings.items():
                    print(f"      -> {label}: key='{info['key']}' example={info['exampleValue']!r}")
                conclusion = "OUTCOME A - independently usable, no session required"
            else:
                print("      -> none found")
                conclusion = "Request succeeded but no real data found - inconclusive"
            saved = save_body(f"partA_{name}", 0, shape["url"], standalone["body"])
            print(f"    full standalone response saved: {saved}")

        print(f"\n  CONCLUSION for {name}: {conclusion}")
        REPORT["partA"][name] = {
            "capturedShape": {k: v for k, v in shape.items() if k != "responseBody"},
            "standaloneResult": {k: v for k, v in standalone.items() if k != "body"},
            "conclusion": conclusion,
        }


# =========================================================================
# PART B - News Explorer deep interaction
# =========================================================================

def run_part_b():
    from playwright.sync_api import sync_playwright

    print("\n\n" + "=" * 70)
    print("PART B - News Explorer deep interaction capture")
    print("=" * 70)

    captured_ordered = []
    capture_errors = []
    interaction_log = []

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
        )

        def on_response(response):
            try:
                ctype = response.headers.get("content-type", "")
                if "json" not in ctype.lower():
                    return
                req = response.request
                captured_ordered.append({
                    "url": response.url, "status": response.status, "method": req.method,
                    "postData": req.post_data, "body": response.text(),
                })
            except Exception as e:
                capture_errors.append(f"{response.url}: {type(e).__name__}: {e}")

        page.on("response", on_response)
        page.goto(NEWS_URL, timeout=30000, wait_until="networkidle")

        try:
            accept_btn = page.locator("#onetrust-accept-btn-handler")
            if accept_btn.is_visible(timeout=5000):
                accept_btn.click()
                page.wait_for_load_state("networkidle", timeout=15000)
                interaction_log.append("Accepted cookie consent banner")
        except Exception as e:
            interaction_log.append(f"Cookie consent: {type(e).__name__}: {e}")

        print("  Waiting substantially longer than v4's capture window...")
        time.sleep(8)
        interaction_log.append("Waited 8s after initial load")

        try:
            page.mouse.wheel(0, 2000)
            time.sleep(2)
            page.mouse.wheel(0, 2000)
            time.sleep(2)
            interaction_log.append("Scrolled down twice")
        except Exception as e:
            interaction_log.append(f"Scroll: {type(e).__name__}: {e}")

        for label, selector in [
            ("next-page button", "text=/next/i"),
            ("pagination control", "[aria-label*='pagination' i], [class*='pagination' i]"),
            ("search/filter input", "input[type='search'], input[placeholder*='search' i], input[placeholder*='compan' i]"),
        ]:
            try:
                el = page.locator(selector).first
                if el.is_visible(timeout=3000):
                    if "input" in selector:
                        el.fill("Barclays")
                        page.keyboard.press("Enter")
                        interaction_log.append(f"Found and used {label}: searched 'Barclays'")
                    else:
                        el.click()
                        interaction_log.append(f"Found and clicked {label}")
                    time.sleep(3)
                else:
                    interaction_log.append(f"{label}: not visible on this page")
            except Exception as e:
                interaction_log.append(f"{label}: not found or not interactable ({type(e).__name__})")

        time.sleep(3)
        browser.close()

    print("\n  Interaction log:")
    for line in interaction_log:
        print(f"    - {line}")

    if capture_errors:
        print(f"\n  Capture warnings: {capture_errors}")

    print(f"\n  All JSON responses captured during this session (chronological, noise excluded):")
    findings_summary = []
    for i, entry in enumerate(captured_ordered):
        if any(n in entry["url"] for n in NOISE_HOSTS):
            continue
        saved = save_body("partB_newsExplorer", i, entry["url"], entry["body"])
        parsed = None
        try:
            parsed = json.loads(entry["body"])
        except Exception:
            pass
        findings = classify_payload(parsed) if parsed is not None else {}
        print(f"    [{i:03d}] {entry['method']} {entry['status']} {entry['url'][:100]} "
              f"(len={len(entry['body'])}, saved={saved})")
        if findings:
            print(f"          REAL DATA FOUND:")
            for label, info in findings.items():
                print(f"            -> {label}: key='{info['key']}' example={info['exampleValue']!r}")
        if findings:
            findings_summary.append({"url": entry["url"], "findings": findings})

    REPORT["partB"] = {
        "interactionLog": interaction_log,
        "captureErrors": capture_errors,
        "responseCount": len(captured_ordered),
        "findingsSummary": findings_summary,
    }

    if findings_summary:
        print(f"\n  CONCLUSION: found {len(findings_summary)} response(s) with real news-shaped data.")
    else:
        print(f"\n  CONCLUSION: no news-shaped data found even after extended interaction. "
              f"The News Explorer story list may require a different trigger not attempted here, "
              f"or may not exist as a discoverable first-party JSON endpoint.")


def main():
    run_part_a()
    run_part_b()
    with open("lse_diagnostic_report_v5.json", "w") as f:
        json.dump(REPORT, f, indent=2)
    print(f"\n\nSummary written to lse_diagnostic_report_v5.json")
    print(f"Full response bodies saved under {BODIES_DIR}/ for artifact upload")


if __name__ == "__main__":
    main()
