"""
Diagnostic v10 - final, minimal standalone confirmation. No browser
needed at all for this one.

Full explanation of how we got here: the real News Explorer story data
was found sitting inside a file already captured back in the v7 run -
the SAME components/refresh response (componentId
block_content:431d02ac-09b8-40c9-aba6-04a72a4f2e49, path "news",
parameters "tab%3Dnews-explorer") that every version from v4 through v8
already fetched naturally on every single page load. It was never a
separate request triggered by a filter - the default, no-filter load
returns a "newsexplorersearch" block containing real story data (title,
source, companycode, companyname, datetime, rnsnumber, lastprice,
percentualchange) AND pagination (totalElements, totalPages) in the
exact same response as the filter-configuration data. Confirmed
directly from the saved file: 14 real stories, matching real headlines
visible in a v9 screenshot of the live page (Ignitis Group, Sydbank
A/S, Jyske Bank A/S, Amundi Physical Metals plc, etc).

This script's only job is the one thing not yet actually proven: does
this exact request work from a completely fresh process, zero cookies,
zero browser, zero session - the same standalone-independence test
already proven for both market pages in Part A. Uses the exact same
request shape (URL, method, body) already confirmed to work in the
browser, replayed via plain urllib.

Run only in GitHub Actions - not usable from Claude's own sandbox,
which cannot reach any of these hosts at all.
"""
import json
import urllib.request
import urllib.error
from datetime import datetime, timezone

COMPONENTS_REFRESH_URL = "https://api.londonstockexchange.com/api/v1/components/refresh"
# Exact values confirmed from the actual captured request/response -
# not guessed.
NEWS_COMPONENT_ID = "block_content%3A431d02ac-09b8-40c9-aba6-04a72a4f2e49"
NEWS_PATH = "news"
NEWS_PARAMETERS = "tab%3Dnews-explorer"

KNOWN_STRINGS = ["Ignitis", "Sydbank", "Jyske Bank", "Amundi Physical Metals"]


def find_newsexplorersearch(obj):
    if isinstance(obj, dict):
        if obj.get("name") == "newsexplorersearch":
            return obj
        for v in obj.values():
            result = find_newsexplorersearch(v)
            if result:
                return result
    elif isinstance(obj, list):
        for item in obj:
            result = find_newsexplorersearch(item)
            if result:
                return result
    return None


def main():
    print("=" * 70)
    print("DIAGNOSTIC v10 - standalone test of the identified News Explorer request")
    print("=" * 70)

    body = json.dumps({
        "path": NEWS_PATH,
        "parameters": NEWS_PARAMETERS,
        "components": [{"componentId": NEWS_COMPONENT_ID, "parameters": None}],
    }).encode("utf-8")

    print(f"\nRequest: POST {COMPONENTS_REFRESH_URL}")
    print(f"Body: {body.decode('utf-8')}")
    print(f"Headers: zero cookies, zero session, zero Refinitiv auth - plain fresh urllib request")

    req = urllib.request.Request(
        COMPONENTS_REFRESH_URL, data=body, method="POST",
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
        },
    )

    result = {"ranAt": datetime.now(timezone.utc).isoformat()}
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            print(f"\nSTATUS: {resp.status}")
            print(f"RESPONSE SIZE: {len(raw)} bytes")
            result["status"] = resp.status
            result["bodyLength"] = len(raw)

            with open("lse_news_standalone_response.json", "w") as f:
                f.write(raw)
            print("Full response saved to lse_news_standalone_response.json")

            parsed = json.loads(raw)
            news_block = find_newsexplorersearch(parsed)
            if news_block is None:
                print("\nCONCLUSION: request succeeded but no newsexplorersearch block found "
                      "in this response - structure may have changed since it was last captured.")
                result["conclusion"] = "no_news_block_found"
            else:
                stories = news_block["value"].get("content", [])
                total_elements = news_block["value"].get("totalElements")
                total_pages = news_block["value"].get("totalPages")
                print(f"\nnewsexplorersearch block found:")
                print(f"  stories in this response: {len(stories)}")
                print(f"  totalElements: {total_elements}")
                print(f"  totalPages: {total_pages}")

                matched = [s for s in KNOWN_STRINGS if s in raw]
                print(f"  known strings from the earlier screenshot matched here too: {matched}")
                print(f"  (an exact match isn't expected every run - this is a live, changing "
                      f"feed - what matters is that REAL story content is present at all)")

                if stories:
                    print(f"\n  Sample story (first in this response):")
                    s = stories[0]
                    for field in ("title", "source", "companycode", "companyname", "datetime",
                                  "rnsnumber", "lastprice", "percentualchange"):
                        print(f"    {field}: {s.get(field)!r}")

                result["conclusion"] = "SUCCESS - standalone request works independently"
                result["storyCount"] = len(stories)
                result["totalElements"] = total_elements
                result["totalPages"] = total_pages
                result["sampleStory"] = stories[0] if stories else None
                result["matchedKnownStrings"] = matched

    except urllib.error.HTTPError as e:
        print(f"\nSTANDALONE REQUEST FAILED: HTTP {e.code}: {e.reason}")
        try:
            error_body = e.read().decode("utf-8", errors="replace")[:1000]
            print(f"Error body: {error_body}")
            result["errorBody"] = error_body
        except Exception:
            pass
        result["conclusion"] = f"FAILED - HTTP {e.code}"
        result["status"] = e.code
    except Exception as e:
        print(f"\nSTANDALONE REQUEST FAILED: {type(e).__name__}: {e}")
        result["conclusion"] = f"FAILED - {type(e).__name__}: {e}"

    with open("lse_diagnostic_report_v10.json", "w") as f:
        json.dump(result, f, indent=2, default=str)
    print("\n\nSummary written to lse_diagnostic_report_v10.json")


if __name__ == "__main__":
    main()
