"""
Diagnostic v13 - minimal, evidence-driven protocol comparison. Two
requests total, nothing more.

Finding that motivated this: re-examining the saved page config from
the ORIGINAL successful capture (partB_v7_chronological/002_....json)
shows the News Explorer tab has its own tabId:
  58734a12-d97c-40cb-8047-df76e660f23f
Both of the confirmed-working market-data endpoints
(risersFallersVolume, heatmap) include a tabId in their "parameters"
string (pattern: "indexname=X&tab=Y&tabId=Z"). Every News Explorer
request this project has made so far (v10, v11, v12) used only
"tab=news-explorer" - the tabId was never included. That is a genuine,
previously-unnoticed structural difference from the two endpoints that
work reliably, not a guess or a brute-forced parameter.

STEP 1 - minimal live config check (one GET, matching exactly what a
real page load does): re-fetch /api/v1/pages for News Explorer and
confirm whether the componentId/tabId/page id still match the old
baseline, or whether the configuration has genuinely changed.

STEP 2 - one POST, testing the SAME confirmed protocol pattern used by
the two working endpoints, applied to News Explorer for the first time
(tab=news-explorer AND tabId=<the tab's own id>, both from the current
live config fetched in step 1, not the old cached values - in case the
live tabId differs from the baseline).

No brute-forcing, no additional parameters invented, no bypassing of
any security control - this reproduces the exact pattern already
proven correct on two other endpoints, nothing more.

Run only in GitHub Actions - not usable from Claude's own sandbox,
which cannot reach any of these hosts at all.
"""
import json
import urllib.error
import urllib.request
from datetime import datetime, timezone

PAGES_URL = "https://api.londonstockexchange.com/api/v1/pages"
COMPONENTS_REFRESH_URL = "https://api.londonstockexchange.com/api/v1/components/refresh"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
}

# Baseline from the original successful capture, for comparison only -
# not assumed to still be correct.
BASELINE_PAGE_ID = "14"
BASELINE_NEWS_COMPONENT_ID = "block_content:431d02ac-09b8-40c9-aba6-04a72a4f2e49"
BASELINE_NEWS_TAB_ID = "58734a12-d97c-40cb-8047-df76e660f23f"


def find_news_explorer_tab(page_config):
    """Locates the News Explorer tab's own moduleId and tabId from the
    live page config - the same structural path used to find the
    baseline values, applied to whatever the live response actually
    contains."""
    try:
        for component in page_config.get("components", []):
            for content_item in component.get("content", []):
                value = content_item.get("value", {})
                for tab in value.get("contentTabNav", []):
                    if tab.get("label", "").lower() == "news explorer":
                        modules = tab.get("modules", [])
                        return {
                            "tabId": tab.get("tabId"),
                            "moduleId": modules[0].get("moduleId") if modules else None,
                            "pageId": page_config.get("id"),
                        }
    except Exception as e:
        print(f"Error walking page config structure: {type(e).__name__}: {e}")
    return None


def step1_fetch_current_config():
    print("=" * 70)
    print("STEP 1 - minimal live config check (one GET)")
    print("=" * 70)
    url = f"{PAGES_URL}?path=news&parameters=tab%253Dnews-explorer"
    req = urllib.request.Request(url, headers=HEADERS, method="GET")
    try:
        resp = urllib.request.urlopen(req, timeout=15)
        raw = resp.read().decode("utf-8", errors="replace")
        print(f"Status: {resp.status}, {len(raw)} bytes")
    except Exception as e:
        print(f"FAILED: {type(e).__name__}: {e}")
        return None

    with open("lse_v13_current_page_config.json", "w") as f:
        f.write(raw)

    try:
        parsed = json.loads(raw)
    except Exception as e:
        print(f"Could not parse response as JSON: {type(e).__name__}: {e}")
        return None

    current = find_news_explorer_tab(parsed)
    current_page_id = parsed.get("id")
    print(f"\nCurrent page id: {current_page_id}  (baseline: {BASELINE_PAGE_ID})  "
          f"{'MATCH' if str(current_page_id) == BASELINE_PAGE_ID else 'DIFFERENT'}")

    if current:
        print(f"Current News Explorer moduleId: {current['moduleId']}  "
              f"(baseline: {BASELINE_NEWS_COMPONENT_ID})  "
              f"{'MATCH' if current['moduleId'] == BASELINE_NEWS_COMPONENT_ID else 'DIFFERENT'}")
        print(f"Current News Explorer tabId: {current['tabId']}  "
              f"(baseline: {BASELINE_NEWS_TAB_ID})  "
              f"{'MATCH' if current['tabId'] == BASELINE_NEWS_TAB_ID else 'DIFFERENT'}")
    else:
        print("Could not locate the News Explorer tab in the current page config - "
              "structure may have changed beyond just IDs.")

    with open("lse_v13_config_comparison.json", "w") as f:
        json.dump({
            "currentPageId": current_page_id, "baselinePageId": BASELINE_PAGE_ID,
            "current": current,
            "baseline": {"moduleId": BASELINE_NEWS_COMPONENT_ID, "tabId": BASELINE_NEWS_TAB_ID,
                         "pageId": BASELINE_PAGE_ID},
        }, f, indent=2)

    return current


def step2_test_with_tabid(component_id, tab_id):
    print("\n" + "=" * 70)
    print("STEP 2 - one POST, testing the confirmed tab=X&tabId=Y pattern "
          "already proven correct on the two working endpoints")
    print("=" * 70)
    # componentId in the request body needs the "block_content:" -> URL
    # encoded colon form, matching the confirmed working pattern.
    component_id_encoded = component_id.replace(":", "%3A")
    parameters = f"tab%3Dnews-explorer%26tabId%3D{tab_id}"
    print(f"componentId: {component_id_encoded}")
    print(f"parameters:  {parameters}")

    body = json.dumps({
        "path": "news",
        "parameters": parameters,
        "components": [{"componentId": component_id_encoded, "parameters": None}],
    }).encode("utf-8")
    req = urllib.request.Request(
        COMPONENTS_REFRESH_URL, data=body, method="POST",
        headers={**HEADERS, "Content-Type": "application/json", "Accept": "application/json, text/plain, */*"},
    )
    try:
        resp = urllib.request.urlopen(req, timeout=15)
        raw = resp.read().decode("utf-8", errors="replace")
        print(f"Status: {resp.status}, {len(raw)} bytes")
        print(f"RAW BODY (repr, first 500 chars): {raw[:500]!r}")
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8", errors="replace")[:500]
        print(f"FAILED: HTTP {e.code} {e.reason} - {error_body}")
        return
    except Exception as e:
        print(f"FAILED: {type(e).__name__}: {e}")
        return

    with open("lse_v13_news_with_tabid_response.json", "w") as f:
        f.write(raw)

    try:
        parsed = json.loads(raw)
    except Exception as e:
        print(f"Could not parse response as JSON: {type(e).__name__}: {e}")
        return

    def find_newsexplorersearch_block(obj):
        if isinstance(obj, dict):
            if obj.get("name") == "newsexplorersearch" and isinstance(obj.get("value"), dict):
                return obj["value"]
            for v in obj.values():
                r = find_newsexplorersearch_block(v)
                if r is not None:
                    return r
        elif isinstance(obj, list):
            for item in obj:
                r = find_newsexplorersearch_block(item)
                if r is not None:
                    return r
        return None

    block = find_newsexplorersearch_block(parsed)
    if block:
        stories = block.get("content", [])
        print(f"\nnewsexplorersearch FOUND: {len(stories)} stories, "
              f"totalElements={block.get('totalElements')}, totalPages={block.get('totalPages')}")
        if stories:
            print(f"First story: {json.dumps(stories[0], default=str)[:400]}")
    else:
        print("\nnewsexplorersearch block: still NOT FOUND, even with tabId included")


if __name__ == "__main__":
    current = step1_fetch_current_config()
    if current and current.get("moduleId") and current.get("tabId"):
        step2_test_with_tabid(current["moduleId"], current["tabId"])
    else:
        print("\nSkipping step 2 - could not confirm current moduleId/tabId from live config, "
              "falling back to the known baseline values instead.")
        step2_test_with_tabid(BASELINE_NEWS_COMPONENT_ID, BASELINE_NEWS_TAB_ID)
