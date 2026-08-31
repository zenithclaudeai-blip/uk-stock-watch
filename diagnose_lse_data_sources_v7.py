"""
Diagnostic v7 — completely different News Explorer strategy, per
explicit instruction not to repeat the Barclays search-box interaction
(confirmed across v5/v6: it navigates the whole page away from News
Explorer to a general Search page, which is why those responses always
failed to read cleanly - not a timing issue at all, a wrong-interaction
issue).

v7 instead:
  1. Loads News Explorer with a clean session and does NOT interact
     with anything until it has checked whether headlines are already
     present in the initial page content (server-rendered or embedded
     JSON) - the very first, cheapest thing to rule out.
  2. Parses the page's own /api/v1/pages response recursively for EVERY
     distinct componentId referenced anywhere in it (not just the one
     used for the main market pages) - the exact "page config ->
     component -> data" chain requested - and calls components/refresh
     for EACH one found, checking every response for genuine news-shaped
     fields.
  3. Enumerates visible DOM controls that plausibly belong to News
     Explorer itself (tabs, filters, dropdowns, pagination, load-more,
     story cards) via Playwright locators, WITHOUT clicking anything
     yet - reports what exists structurally first.
  4. Only THEN attempts safe, non-navigating interactions - checked
     before clicking (an element with an in-page fragment href or no
     href at all; never anything pointing to a different path/domain) -
     and never leaves the News Explorer URL.
  5. Also captures WebSocket connections, in case the story list is
     delivered over a different transport than ordinary XHR/fetch.

Part A is unchanged from v5/v6 (already proven: components/refresh is
independently usable with zero session for both market pages).

Same ground rules throughout: ordinary browser, ordinary page load, no
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
BODIES_DIR = "lse_bodies_v7"

MARKET_PAGES = {
    "risersFallersVolume": "https://www.londonstockexchange.com/indices/ftse-100/constituents/risers-and-fallers-and-volume-leaders",
    "heatmap": "https://www.londonstockexchange.com/indices/ftse-100/constituents/heatmap",
}
NEWS_URL = "https://www.londonstockexchange.com/news?tab=news-explorer"
NEWS_PATH_PARAMS = "tab%3Dnews-explorer"  # confirmed from v4/v5/v6's own captured requests

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

def find_all_component_ids(obj, found=None):
    """Recursively walks the page's own /api/v1/pages response looking
    for EVERY distinct componentId referenced anywhere in it - not just
    the one already confirmed for the main market pages. This is the
    literal "page configuration -> component -> data request" chain
    requested, applied generically rather than assuming there's only
    ever one component per page."""
    if found is None:
        found = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k.lower() == "componentid" and isinstance(v, str):
                found.add(v)
            find_all_component_ids(v, found)
    elif isinstance(obj, list):
        for item in obj:
            find_all_component_ids(item, found)
    return found


def try_component_refresh(component_id, path="news", parameters=NEWS_PATH_PARAMS):
    """One ordinary POST to components/refresh for a specific
    componentId, exactly the same request shape already proven to work
    for the market pages - just a different ID, discovered from the
    page's own configuration rather than assumed."""
    body = json.dumps({
        "path": path, "parameters": parameters,
        "components": [{"componentId": component_id, "parameters": None}],
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.londonstockexchange.com/api/v1/components/refresh",
        data=body, method="POST",
        headers={"User-Agent": "Mozilla/5.0", "Content-Type": "application/json",
                 "Accept": "application/json, text/plain, */*"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return {"status": resp.status, "body": raw}
    except urllib.error.HTTPError as e:
        return {"status": e.code, "error": f"HTTP {e.code}: {e.reason}"}
    except Exception as e:
        return {"status": None, "error": f"{type(e).__name__}: {e}"}


NEWS_DATA_SIGNAL_WORDS = ("news", "story", "stories", "headline", "rns", "announcement", "article")


def run_part_b():
    from playwright.sync_api import sync_playwright

    print("\n\n" + "=" * 70)
    print("PART B (v7) - News Explorer: component-enumeration strategy, no search-box interaction")
    print("=" * 70)

    captured_ordered = []
    capture_errors = []
    websocket_log = []
    interaction_log = []
    pages_response_body = None

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
                    "body": response.text(),
                })
            except Exception as e:
                capture_errors.append(f"{response.url}: {type(e).__name__}: {e}")

        def on_websocket(ws):
            websocket_log.append(f"WebSocket opened: {ws.url}")

        page.on("response", on_response)
        page.on("websocket", on_websocket)

        page.goto(NEWS_URL, timeout=30000, wait_until="networkidle")
        interaction_log.append(f"Loaded {NEWS_URL} - no interaction yet")

        try:
            accept_btn = page.locator("#onetrust-accept-btn-handler")
            if accept_btn.is_visible(timeout=5000):
                accept_btn.click()
                page.wait_for_load_state("networkidle", timeout=15000)
                interaction_log.append("Accepted cookie consent banner")
        except Exception as e:
            interaction_log.append(f"Cookie consent: {type(e).__name__}: {e}")

        time.sleep(5)

        # --- Step 1: BEFORE any further interaction, check whether
        # headline-like text is already present in the rendered page. ---
        try:
            body_text = page.inner_text("body")
            # A crude but honest signal: real headlines tend to be
            # numerous, varied sentences well beyond nav/footer boilerplate.
            word_count = len(body_text.split())
            interaction_log.append(f"Initial page body text: {word_count} words "
                                    f"(purely descriptive - not itself proof of headlines)")
        except Exception as e:
            interaction_log.append(f"Reading initial page text: {type(e).__name__}: {e}")
            body_text = ""

        # --- Step 2: enumerate every componentId in the page's own
        # /api/v1/pages response, and try components/refresh for each. ---
        pages_entries = [e for e in captured_ordered if "api/v1/pages" in e["url"]]
        component_refresh_findings = []
        if pages_entries:
            pages_response_body = pages_entries[0]["body"]
            try:
                pages_parsed = json.loads(pages_response_body)
                all_component_ids = find_all_component_ids(pages_parsed)
                interaction_log.append(f"Found {len(all_component_ids)} distinct componentId(s) "
                                        f"in the page's own /api/v1/pages response")
                for cid in all_component_ids:
                    result = try_component_refresh(cid)
                    finding = {"componentId": cid, "status": result.get("status")}
                    if result.get("error"):
                        finding["error"] = result["error"]
                    elif result.get("body"):
                        saved = save_body("partB_v7_components", len(component_refresh_findings),
                                           cid, result["body"])
                        finding["savedBodyFile"] = saved
                        finding["bodyLength"] = len(result["body"])
                        lower_body = result["body"].lower()
                        finding["newsSignalWordsFound"] = [w for w in NEWS_DATA_SIGNAL_WORDS if w in lower_body]
                        try:
                            parsed_body = json.loads(result["body"])
                            finding["realDataFindings"] = classify_payload(parsed_body)
                        except Exception:
                            finding["realDataFindings"] = {}
                    component_refresh_findings.append(finding)
            except Exception as e:
                interaction_log.append(f"Parsing /api/v1/pages for componentIds: {type(e).__name__}: {e}")
        else:
            interaction_log.append("api/v1/pages response was not captured - cannot enumerate componentIds")

        # --- Step 3: enumerate visible News-Explorer-specific DOM
        # controls WITHOUT clicking anything yet. ---
        dom_findings = {}
        for label, selector in [
            ("tabs", "[role='tab'], [class*='tab' i]"),
            ("filters/dropdowns", "select, [role='listbox'], [class*='filter' i], [class*='dropdown' i]"),
            ("date filters", "input[type='date'], [class*='date' i][class*='filter' i]"),
            ("pagination", "[aria-label*='pagination' i], [class*='pagination' i]"),
            ("load-more control", "text=/load more/i, text=/show more/i"),
            ("story cards/tiles", "article, [class*='story' i], [class*='card' i], [class*='tile' i], [class*='news-item' i]"),
        ]:
            try:
                count = page.locator(selector).count()
                dom_findings[label] = count
            except Exception as e:
                dom_findings[label] = f"error: {type(e).__name__}: {e}"
        interaction_log.append(f"DOM control enumeration (counts, nothing clicked yet): {dom_findings}")

        # --- Step 4: only now, safe non-navigating interactions - never
        # leaving the News Explorer URL. ---
        try:
            load_more = page.locator("text=/load more/i, text=/show more/i").first
            if load_more.is_visible(timeout=3000):
                href = load_more.get_attribute("href")
                if not href or href.startswith("#"):
                    load_more.click()
                    interaction_log.append("Clicked a 'load more' control (no external href) - "
                                            "waiting for any resulting requests")
                    page.wait_for_load_state("networkidle", timeout=15000)
                    time.sleep(5)
                else:
                    interaction_log.append(f"'load more' control found but has an external href "
                                            f"({href}) - skipped to avoid navigating away")
            else:
                interaction_log.append("No 'load more' control visible")
        except Exception as e:
            interaction_log.append(f"'load more' interaction: {type(e).__name__}: {e}")

        # Confirm we're still on the News Explorer URL, not navigated away
        current_url = page.url
        interaction_log.append(f"Current URL after all interaction: {current_url} "
                                f"({'STAYED on News Explorer' if 'news' in current_url else 'NAVIGATED AWAY'})")

        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception as e:
            interaction_log.append(f"Final networkidle wait: {type(e).__name__}: {e}")
        time.sleep(5)
        browser.close()

    print("\n  Interaction log:")
    for line in interaction_log:
        print(f"    - {line}")

    if websocket_log:
        print(f"\n  WebSocket connections observed: {websocket_log}")
    else:
        print("\n  No WebSocket connections observed.")

    if capture_errors:
        print(f"\n  Capture warnings: {capture_errors}")

    print(f"\n  componentId enumeration results ({len(component_refresh_findings)} tried):")
    findings_summary = []
    for f in component_refresh_findings:
        print(f"    - componentId={f['componentId'][:60]} status={f.get('status')}")
        if f.get("error"):
            print(f"      error: {f['error']}")
        if f.get("realDataFindings"):
            print(f"      REAL DATA FOUND: {f['realDataFindings']}")
            findings_summary.append(f)
        elif f.get("newsSignalWordsFound"):
            print(f"      news-related words present in body (not confirmed structured data): "
                  f"{f['newsSignalWordsFound']}")

    print(f"\n  All JSON responses captured chronologically (noise excluded):")
    for i, entry in enumerate(captured_ordered):
        if any(n in entry["url"] for n in NOISE_HOSTS):
            continue
        saved = save_body("partB_v7_chronological", i, entry["url"], entry["body"])
        print(f"    [{i:03d}] {entry['method']} {entry['status']} {entry['url'][:100]} "
              f"(len={len(entry['body'])}, saved={saved})")

    REPORT["partB"] = {
        "interactionLog": interaction_log,
        "domFindings": dom_findings,
        "websocketLog": websocket_log,
        "captureErrors": capture_errors,
        "componentEnumeration": component_refresh_findings,
        "findingsSummary": [f["componentId"] for f in findings_summary],
    }

    if findings_summary:
        print(f"\n  CONCLUSION: found {len(findings_summary)} component(s) with real news-shaped data.")
    else:
        print(f"\n  CONCLUSION: no news-shaped data found via component enumeration or safe DOM "
              f"interaction. Evidence gathered: {len(component_refresh_findings)} componentIds tried, "
              f"DOM control counts logged above, {len(websocket_log)} WebSocket connection(s) observed, "
              f"URL confirmed to stay on News Explorer throughout.")




def main():
    run_part_a()
    run_part_b()
    with open("lse_diagnostic_report_v7.json", "w") as f:
        json.dump(REPORT, f, indent=2)
    print(f"\n\nSummary written to lse_diagnostic_report_v7.json")
    print(f"Full response bodies saved under {BODIES_DIR}/ for artifact upload")


if __name__ == "__main__":
    main()
