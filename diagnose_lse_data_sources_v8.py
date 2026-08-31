"""
Diagnostic v8 - captures a REAL, populated News Explorer query, using
the actual UI, not the global search box (confirmed in v5/v6 to
navigate away from News Explorer entirely) and not guessed parameters
(explicitly instructed against).

Grounded directly in what v7's own captured evidence proved:
  - the News Explorer component is block_content:431d02ac-...
  - its filter config confirms an "Index" filter with real values
    (UKX = FTSE 100, MCX = FTSE 250, etc.) and an "Apply filters" button
  - a genuine results schema exists, named "newsexplorersearch", with
    fields title/source/date/time/provider/companycode/totalPages/
    totalElements - i.e. real story data, just not returned until a
    filter is actually submitted

v8 uses Playwright to: open the Index filter (expanding the "Set news
filters" accordion first if needed), type and select "FTSE 100", click
"Apply filters", then checks every response captured during that
specific interaction against the confirmed newsexplorersearch field
shape - not a generic keyword scan. If real results are found, the
exact request is replayed standalone (fresh process, no cookies) to
test independent usability, exactly the same proven method as Part A.
A second filter (a sector) is tried the same way afterward, to confirm
this is the general search mechanism, not a one-off special case.

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
BODIES_DIR = "lse_bodies_v8"

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


def find_all_component_ids_v8(obj, found=None):
    """Fixed from v7's bug: the real page config uses 'id' (inside
    components/sharedComponents lists) and 'moduleId' (nested inside
    contentTabNav[].modules[]) - confirmed by directly inspecting the
    real captured page config from the v7 run, not 'componentId' as a
    single combined key, which v7 wrongly assumed."""
    if found is None:
        found = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in ("id", "moduleId") and isinstance(v, str) and v.startswith("block_content:"):
                found.add(v)
            find_all_component_ids_v8(v, found)
    elif isinstance(obj, list):
        for item in obj:
            find_all_component_ids_v8(item, found)
    return found


# The exact confirmed real UI label text from the v7 run's own captured
# News Explorer filter-config response - used as text-based Playwright
# selectors, which are robust to underlying markup/class changes,
# rather than guessed CSS classes.
LABEL_ACCORDION = "Set news filters"
LABEL_INDEX_FILTER = "Index"
LABEL_APPLY = "Apply filters"
LABEL_SELECT_OR_SEARCH = "Select or search for an option"

NEWSEXPLORERSEARCH_FIELDS = re.compile(
    r"^(title|source|date|time|provider|companycode|totalpages|totalelements|number|size)$", re.I)


def classify_news_results(parsed):
    """A DEDICATED, precise classifier for the confirmed newsexplorersearch
    schema specifically (title/source/date/time/provider/companycode/
    totalPages/totalElements) - not the generic market-data classifier,
    since a genuine news response has a structurally different shape."""
    if parsed is None:
        return {}
    kv = collect_keys_with_sample_values(parsed)
    findings = {}
    for full_key, value in kv.items():
        leaf = full_key.rsplit(".", 1)[-1].split("[")[0]
        if NEWSEXPLORERSEARCH_FIELDS.match(leaf):
            findings[leaf] = {"key": full_key, "exampleValue": value}
    return findings


def select_index_filter(page, log, filter_text, filter_label=LABEL_INDEX_FILTER):
    """Attempts to open the named filter (default: 'Index') and select
    filter_text (e.g. 'FTSE 100') via the real UI - the 'Select or
    search for an option' label confirms this is a searchable dropdown,
    not a plain <select>, so this types into whatever becomes focused
    and clicks the first matching suggestion. Every step logged
    individually so a failure is precisely located, never a generic
    'didn't work'."""
    try:
        # The filter panel may be behind an accordion - only expand it
        # if the Index label isn't already visible.
        index_label_locator = page.locator(f"text='{filter_label}'").first
        if not index_label_locator.is_visible(timeout=3000):
            accordion = page.locator(f"text='{LABEL_ACCORDION}'").first
            if accordion.is_visible(timeout=3000):
                accordion.click()
                log.append(f"Clicked accordion '{LABEL_ACCORDION}' to reveal filters")
                page.wait_for_timeout(1500)
            else:
                log.append(f"Neither '{filter_label}' filter nor '{LABEL_ACCORDION}' "
                            f"accordion is visible - cannot proceed with this filter")
                return False
    except Exception as e:
        log.append(f"Checking filter panel visibility: {type(e).__name__}: {e}")

    try:
        index_label_locator = page.locator(f"text='{filter_label}'").first
        index_label_locator.scroll_into_view_if_needed(timeout=5000)
        log.append(f"'{filter_label}' filter label is visible")
    except Exception as e:
        log.append(f"Could not find/scroll to '{filter_label}' filter label: {type(e).__name__}: {e}")
        return False

    # The searchable input is most plausibly a sibling/nearby element -
    # try a few structurally reasonable candidates near the label.
    input_candidates = [
        f"text='{filter_label}' >> xpath=following::input[1]",
        f"text='{filter_label}' >> xpath=following::*[@role='combobox'][1]",
        f"text='{filter_label}' >> xpath=ancestor::*[position()<=3]//input",
    ]
    opened = False
    for candidate in input_candidates:
        try:
            el = page.locator(candidate).first
            if el.is_visible(timeout=2000):
                el.click()
                el.fill(filter_text)
                log.append(f"Typed '{filter_text}' into the '{filter_label}' filter input "
                           f"(selector: {candidate})")
                opened = True
                break
        except Exception:
            continue
    if not opened:
        log.append(f"Could not locate a searchable input near the '{filter_label}' label "
                    f"via any of {len(input_candidates)} candidate selectors")
        return False

    page.wait_for_timeout(1500)
    # Click the first suggestion that contains the filter text.
    try:
        suggestion = page.locator(
            f"[role='option']:has-text('{filter_text}'), li:has-text('{filter_text}')"
        ).first
        if suggestion.is_visible(timeout=3000):
            suggestion.click()
            log.append(f"Selected the '{filter_text}' suggestion from the dropdown")
            return True
        else:
            log.append(f"No visible suggestion matching '{filter_text}' appeared after typing")
            return False
    except Exception as e:
        log.append(f"Selecting the '{filter_text}' suggestion: {type(e).__name__}: {e}")
        return False


def click_apply_filters(page, log):
    try:
        apply_btn = page.locator(f"text='{LABEL_APPLY}'").first
        if apply_btn.is_visible(timeout=3000):
            apply_btn.click()
            log.append(f"Clicked '{LABEL_APPLY}'")
            return True
        log.append(f"'{LABEL_APPLY}' button not visible")
        return False
    except Exception as e:
        log.append(f"Clicking '{LABEL_APPLY}': {type(e).__name__}: {e}")
        return False


def run_one_filter_attempt(page, filter_text, capture_ref, log):
    """One full attempt: select the Index filter to filter_text, apply
    it, wait extensively, and check every response captured DURING this
    attempt (not the whole session) for the confirmed newsexplorersearch
    field shape. capture_ref is a mutable list the caller's on_response
    handler appends to - sliced by length before/after so results are
    attributed to the correct filter attempt, not mixed with a later one."""
    start_len = len(capture_ref)
    ok = select_index_filter(page, log, filter_text)
    if not ok:
        return {"filterText": filter_text, "selectionSucceeded": False, "results": []}
    click_apply_filters(page, log)
    try:
        page.wait_for_load_state("networkidle", timeout=20000)
    except Exception as e:
        log.append(f"networkidle wait after applying '{filter_text}': {type(e).__name__}: {e}")
    page.wait_for_timeout(8000)

    new_entries = capture_ref[start_len:]
    findings_for_this_attempt = []
    for i, entry in enumerate(new_entries):
        if any(n in entry["url"] for n in NOISE_HOSTS):
            continue
        try:
            parsed = json.loads(entry["body"])
        except Exception:
            continue
        news_findings = classify_news_results(parsed)
        if news_findings:
            saved = save_body(f"partB_v8_filter_{filter_text.replace(' ', '_')}",
                               i, entry["url"], entry["body"])
            findings_for_this_attempt.append({
                "url": entry["url"], "method": entry["method"], "status": entry["status"],
                "bodyLength": len(entry["body"]), "savedBodyFile": saved,
                "newsFields": news_findings,
            })
    return {"filterText": filter_text, "selectionSucceeded": True, "results": findings_for_this_attempt}


def run_part_b():
    from playwright.sync_api import sync_playwright

    print("\n\n" + "=" * 70)
    print("PART B (v8) - News Explorer: real UI filter interaction (Index -> FTSE 100)")
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
        interaction_log.append(f"Loaded {NEWS_URL}")

        try:
            accept_btn = page.locator("#onetrust-accept-btn-handler")
            if accept_btn.is_visible(timeout=5000):
                accept_btn.click()
                page.wait_for_load_state("networkidle", timeout=15000)
                interaction_log.append("Accepted cookie consent banner")
        except Exception as e:
            interaction_log.append(f"Cookie consent: {type(e).__name__}: {e}")

        page.wait_for_timeout(3000)

        attempt1 = run_one_filter_attempt(page, "FTSE 100", captured_ordered, interaction_log)
        current_url_1 = page.url
        interaction_log.append(f"URL after FTSE 100 filter attempt: {current_url_1} "
                                f"({'STAYED on News Explorer' if 'news' in current_url_1 else 'NAVIGATED AWAY'})")

        attempt2 = {"filterText": None, "selectionSucceeded": False, "results": []}
        if "news" in current_url_1:
            attempt2 = run_one_filter_attempt(page, "Banks", captured_ordered, interaction_log)
            current_url_2 = page.url
            interaction_log.append(f"URL after second (Banks sector) filter attempt: {current_url_2}")
        else:
            interaction_log.append("Skipping second filter attempt - already navigated away from News Explorer")

        # If either attempt found real results, capture the exact request
        # shape for the standalone test.
        standalone_test = None
        real_result_entry = None
        for attempt in (attempt1, attempt2):
            if attempt["results"]:
                real_result_entry = attempt["results"][0]
                break
        if real_result_entry:
            matching = [e for e in captured_ordered if e["url"] == real_result_entry["url"]]
            if matching:
                shape = matching[-1]
                standalone_test = {
                    "url": shape["url"], "method": shape["method"], "postData": shape["postData"],
                }

        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        page.wait_for_timeout(3000)
        browser.close()

    print("\n  Interaction log:")
    for line in interaction_log:
        print(f"    - {line}")
    if capture_errors:
        print(f"\n  Capture warnings: {capture_errors}")

    for label, attempt in [("FTSE 100 (Index filter)", attempt1), ("Banks (sector filter)", attempt2)]:
        print(f"\n  Filter attempt: {label}")
        print(f"    selection succeeded: {attempt['selectionSucceeded']}")
        if attempt["results"]:
            for r in attempt["results"]:
                print(f"    REAL NEWS RESULT FOUND: {r['method']} {r['status']} {r['url'][:100]}")
                print(f"      fields found: {list(r['newsFields'].keys())}")
                for fname, finfo in r["newsFields"].items():
                    print(f"        -> {fname}: {finfo['exampleValue']!r}")
        else:
            print("    No news-shaped results found for this filter attempt")

    standalone_result = None
    if standalone_test:
        print(f"\n  Testing the discovered results request standalone (fresh process, no session):")
        print(f"    {standalone_test['method']} {standalone_test['url']}")
        headers = {"User-Agent": "Mozilla/5.0", "Content-Type": "application/json",
                   "Accept": "application/json, text/plain, */*"}
        data = standalone_test["postData"].encode("utf-8") if standalone_test["postData"] else None
        req = urllib.request.Request(standalone_test["url"], data=data,
                                      method=standalone_test["method"], headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                body = resp.read().decode("utf-8", errors="replace")
                parsed = json.loads(body)
                findings = classify_news_results(parsed)
                standalone_result = {"status": resp.status, "bodyLength": len(body), "newsFields": list(findings.keys())}
                print(f"    STANDALONE result: status {resp.status}, {len(body)} bytes, "
                      f"news fields found: {list(findings.keys())}")
        except Exception as e:
            standalone_result = {"error": f"{type(e).__name__}: {e}"}
            print(f"    STANDALONE result: FAILED - {e}")
    else:
        print("\n  No populated news results were found in either filter attempt - "
              "standalone test skipped (nothing to test).")

    REPORT["partB"] = {
        "interactionLog": interaction_log,
        "captureErrors": capture_errors,
        "attempt1_ftse100": {k: v for k, v in attempt1.items()},
        "attempt2_sector": {k: v for k, v in attempt2.items()},
        "standaloneTest": standalone_test,
        "standaloneResult": standalone_result,
    }

    if real_result_entry:
        print(f"\n  CONCLUSION: genuine News Explorer story data FOUND and captured.")
    else:
        print(f"\n  CONCLUSION: no populated news results found via real UI filter interaction. "
              f"See interaction log above for exactly which step did not behave as expected.")


def main():
    run_part_a()
    run_part_b()
    with open("lse_diagnostic_report_v8.json", "w") as f:
        json.dump(REPORT, f, indent=2)
    print(f"\n\nSummary written to lse_diagnostic_report_v8.json")
    print(f"Full response bodies saved under {BODIES_DIR}/ for artifact upload")


if __name__ == "__main__":
    main()
