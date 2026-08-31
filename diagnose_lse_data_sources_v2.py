"""
Diagnostic v2: inspect the ACTUAL response bodies from LSE's first-party
api.londonstockexchange.com endpoint, and the Refinitiv SAML login
response, to determine what each one really contains.

Builds on diagnose_lse_data_sources.py's v1 finding: every target page
calls api.londonstockexchange.com/api/v1/pages?... AND
refinitiv-widgets.financial.com/auth/api/v1/sessions/samllogin. v1 only
logged metadata (status/content-type). This version parses the actual
JSON bodies to determine whether /api/v1/pages carries real market
data, page-layout config, or references to a further endpoint - and,
if it does contain an internal href/URL to another API, follows that
reference with an ordinary unauthenticated GET (never using any token
from the Refinitiv login, never attempting to bypass anything - if a
followed reference requires auth, that 401/403 IS the answer, reported
honestly, not worked around).

Also detects and accepts the OneTrust cookie-consent banner if present
(a completely normal, expected user action - many SPAs hold back
further widget-loading JS until consent is granted, which would fully
explain why v1's capture window closed before any deeper calls fired).

Run only in GitHub Actions (internet-enabled) - not usable from
Claude's own interactive sandbox, which cannot reach any of these
hosts at all.
"""
import json
import re
import time
import urllib.request
from datetime import datetime, timezone

REPORT = {
    "ranAt": datetime.now(timezone.utc).isoformat(),
    "pages": {},
}

TARGET_PAGES = {
    "risersFallersVolume": "https://www.londonstockexchange.com/indices/ftse-100/constituents/risers-and-fallers-and-volume-leaders",
    "heatmap": "https://www.londonstockexchange.com/indices/ftse-100/constituents/heatmap",
    "newsExplorer": "https://www.londonstockexchange.com/news?tab=news-explorer",
}

# Keys whose PRESENCE anywhere in the parsed JSON suggests real market/
# news content, vs keys suggesting pure page/layout configuration. Not
# exhaustive - just enough signal to make an honest, defensible call
# about what kind of payload this is.
DATA_SIGNAL_KEYS = (
    "price", "lastprice", "change", "percentchange", "volume", "marketcap",
    "sector", "bid", "ask", "high", "low", "ticker", "epic", "isin",
    "headline", "announcement", "rns", "publisheddate", "newsdate",
)
CONFIG_SIGNAL_KEYS = (
    "widget", "component", "template", "layout", "seo", "metatitle",
    "metadescription", "breadcrumb", "navigation", "slug",
)

URL_RE = re.compile(r'https?://[^\s"\'<>\\]+')


def find_signal_keys(obj, found=None):
    """Recursively collects every dict key found anywhere in the parsed
    JSON (lowercased), so it can be checked against the data/config
    signal lists above - a structural scan, not a guess."""
    if found is None:
        found = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            found.add(str(k).lower())
            find_signal_keys(v, found)
    elif isinstance(obj, list):
        for item in obj:
            find_signal_keys(item, found)
    return found


def extract_urls(text):
    """Pulls every http(s) URL literally present in a response body -
    the legitimate way to discover a referenced follow-up endpoint the
    page's own code already told us about, rather than guessing one."""
    return sorted(set(URL_RE.findall(text)))


def try_follow_reference(url):
    """A single, ordinary, unauthenticated GET on a URL the LSE
    response itself referenced. Never attaches any token or credential.
    A 401/403 here is itself the answer - reported as such, not
    retried with different auth."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return {"status": resp.status, "bodyPreview": body[:1000], "bodyLength": len(body)}
    except urllib.error.HTTPError as e:
        return {"status": e.code, "error": f"HTTP {e.code}"}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def inspect_page(name, url):
    from playwright.sync_api import sync_playwright

    result = {
        "url": url, "status": None, "error": None,
        "lsePagesEndpoint": None, "refinitivSamlLogin": None,
        "otherJsonEndpoints": [],
    }

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                )
            )
            captured = {}

            def on_response(response):
                try:
                    ctype = response.headers.get("content-type", "")
                    if "json" not in ctype.lower():
                        return
                    body = response.text()
                    captured[response.url] = {
                        "status": response.status, "contentType": ctype,
                        "body": body, "bodyLength": len(body),
                    }
                except Exception:
                    pass

            page.on("response", on_response)
            resp = page.goto(url, timeout=30000, wait_until="networkidle")
            result["status"] = resp.status if resp else None

            # Accept the OneTrust cookie banner if present - an ordinary
            # user action, not a bypass. Many SPAs hold back further
            # widget data-loading until consent is granted.
            try:
                accept_btn = page.locator("#onetrust-accept-btn-handler")
                if accept_btn.is_visible(timeout=5000):
                    accept_btn.click()
                    print(f"  [{name}] Cookie consent banner found and accepted")
                    page.wait_for_load_state("networkidle", timeout=15000)
                else:
                    print(f"  [{name}] No visible cookie consent banner")
            except Exception as e:
                print(f"  [{name}] Cookie consent handling: {type(e).__name__}: {e}")

            time.sleep(3)  # let any consent-gated calls finish firing

            browser.close()

            # Sort captured responses into the ones we care about
            for req_url, entry in captured.items():
                if "api.londonstockexchange.com/api/v1/pages" in req_url:
                    parsed = None
                    try:
                        parsed = json.loads(entry["body"])
                    except Exception as e:
                        entry["parseError"] = str(e)
                    signal_keys = find_signal_keys(parsed) if parsed is not None else set()
                    data_hits = [k for k in DATA_SIGNAL_KEYS if any(k in sk for sk in signal_keys)]
                    config_hits = [k for k in CONFIG_SIGNAL_KEYS if any(k in sk for sk in signal_keys)]
                    referenced_urls = extract_urls(entry["body"])
                    result["lsePagesEndpoint"] = {
                        "url": req_url, "status": entry["status"], "bodyLength": entry["bodyLength"],
                        "topLevelKeys": list(parsed.keys()) if isinstance(parsed, dict) else None,
                        "dataSignalKeysFound": data_hits, "configSignalKeysFound": config_hits,
                        "referencedUrls": referenced_urls[:20],
                        "bodyPreview": entry["body"][:3000],
                    }
                elif "samllogin" in req_url:
                    result["refinitivSamlLogin"] = {
                        "url": req_url, "status": entry["status"], "bodyLength": entry["bodyLength"],
                        "bodyPreview": entry["body"][:1000],
                    }
                elif not any(x in req_url for x in ("cookielaw", "onetrust", "demdex", "company-target")):
                    result["otherJsonEndpoints"].append({
                        "url": req_url, "status": entry["status"], "bodyLength": entry["bodyLength"],
                        "bodyPreview": entry["body"][:500],
                    })

            # Follow any referenced URLs found inside the pages endpoint's
            # own response body - the legitimate next step, not a guess.
            if result["lsePagesEndpoint"] and result["lsePagesEndpoint"]["referencedUrls"]:
                followed = []
                for ref_url in result["lsePagesEndpoint"]["referencedUrls"][:8]:
                    if any(x in ref_url for x in ("cookielaw", "onetrust", "demdex", "google", "adobe",
                                                   "w3.org", "schema.org", "fonts.")):
                        continue  # not a candidate data endpoint - skip noise
                    followed.append({"url": ref_url, "result": try_follow_reference(ref_url)})
                result["followedReferences"] = followed

    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"

    return result


def main():
    print("=" * 70)
    print("LSE DATA-SOURCE DIAGNOSTIC v2 - response body inspection")
    print("=" * 70)

    for name, url in TARGET_PAGES.items():
        print(f"\n{'=' * 50}\nInspecting: {name}\n{'=' * 50}")
        result = inspect_page(name, url)
        REPORT["pages"][name] = result

        if result.get("error"):
            print(f"  ERROR: {result['error']}")
            continue

        lp = result.get("lsePagesEndpoint")
        if lp:
            print(f"\n  /api/v1/pages endpoint:")
            print(f"    status: {lp['status']}, body length: {lp['bodyLength']}")
            print(f"    top-level keys: {lp['topLevelKeys']}")
            print(f"    data-signal keys found: {lp['dataSignalKeysFound']}")
            print(f"    config-signal keys found: {lp['configSignalKeysFound']}")
            print(f"    referenced URLs found in body: {len(lp['referencedUrls'])}")
            for u in lp['referencedUrls'][:10]:
                print(f"      -> {u}")
        else:
            print("  /api/v1/pages endpoint: NOT CAPTURED")

        rl = result.get("refinitivSamlLogin")
        if rl:
            print(f"\n  Refinitiv samllogin: status {rl['status']}, body length {rl['bodyLength']}")
            print(f"    preview: {rl['bodyPreview'][:300]}")

        others = result.get("otherJsonEndpoints", [])
        if others:
            print(f"\n  Other JSON endpoints captured ({len(others)}):")
            for o in others:
                print(f"    - {o['status']} {o['url'][:120]} (len={o['bodyLength']})")

        followed = result.get("followedReferences", [])
        if followed:
            print(f"\n  Followed references from pages-endpoint body:")
            for f in followed:
                print(f"    - {f['url'][:120]}")
                print(f"      -> {f['result']}")

    with open("lse_diagnostic_report_v2.json", "w") as f:
        json.dump(REPORT, f, indent=2)
    print("\n\nFull report written to lse_diagnostic_report_v2.json")
    print("=" * 70)


if __name__ == "__main__":
    main()
