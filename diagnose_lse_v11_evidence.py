"""
Diagnostic v11 - evidence gathering only, per explicit instruction not
to change any production code until the facts are established.

Two independent investigations:

PART A - Heatmap body capture. Makes ONE isolated request, using the
EXACT SAME constants (URL, componentId, path, parameters, headers)
copied directly from production poll.py, and saves the COMPLETE
response body + its SHA-256 hash, so it can be directly diffed against
the already-saved successful diagnostic Heatmap response from earlier
in this project (partA_heatmap/000_....json) - byte-for-byte, not
just "same size".

PART B - News Explorer isolation. This script itself performs ONLY
ONE News Explorer request per invocation (no Screener, no Heatmap, no
Yahoo, no browser, no cookies) - true process-level isolation across
attempts is achieved by the WORKFLOW invoking this script multiple
times as separate steps (separate OS processes), not by looping
inside one process, matching "completely fresh Python process" for
each attempt exactly as asked. Each invocation is told which attempt
number it is via a command-line argument, and appends its own
timestamped result to a shared JSON file.

Nothing here changes any production code. This is read-only evidence
gathering against the real LSE endpoint.

Run only in GitHub Actions - not usable from Claude's own sandbox,
which cannot reach any of these hosts at all.
"""
import hashlib
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

# Exact constants copied directly from production poll.py - not
# reproduced from memory, not approximated. Any request-shape
# difference between this and production would itself be a real
# finding, so these are deliberately copy-pasted verbatim.
LSE_COMPONENTS_REFRESH_URL = "https://api.londonstockexchange.com/api/v1/components/refresh"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
}
HEATMAP_PARAMETERS = "indexname%3Dftse-100%26tab%3Dheatmap%26tabId%3Ddcd47cbd-346e-4bd0-bf77-039301c7d329"
HEATMAP_COMPONENT_ID = "block_content%3A72d8cb8c-5ef6-41a9-9bb9-49db0a064214"
NEWS_PATH = "news"
NEWS_PARAMETERS = "tab%3Dnews-explorer"
NEWS_COMPONENT_ID = "block_content%3A431d02ac-09b8-40c9-aba6-04a72a4f2e49"


def make_request(path, parameters, component_id, timeout=15):
    """Exact same request construction as production's
    _fetch_lse_components_refresh_raw - reproduced here rather than
    imported, since this diagnostic must work standalone, but built to
    be byte-identical in shape (verified by comparing the actual body
    string below, not just trusting this comment)."""
    body = json.dumps({
        "path": path,
        "parameters": parameters,
        "components": [{"componentId": component_id, "parameters": None}],
    }).encode("utf-8")
    req = urllib.request.Request(
        LSE_COMPONENTS_REFRESH_URL, data=body, method="POST",
        headers={**HEADERS, "Content-Type": "application/json", "Accept": "application/json, text/plain, */*"},
    )
    result = {
        "requestUrl": LSE_COMPONENTS_REFRESH_URL, "requestMethod": "POST",
        "requestBody": body.decode("utf-8"), "requestHeaders": req.headers,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        raw = resp.read()
        result.update({
            "status": resp.status, "contentType": resp.headers.get("content-type"),
            "bodyBytes": len(raw), "bodySha256": hashlib.sha256(raw).hexdigest(),
            "bodyText": raw.decode("utf-8", errors="replace"),
        })
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8", errors="replace")[:500]
        result.update({"status": e.code, "error": f"HTTP {e.code} {e.reason}", "errorBody": error_body})
    except Exception as e:
        result.update({"status": None, "error": f"{type(e).__name__}: {e}"})
    return result


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


def collect_top_level_shape(obj, depth=0, max_depth=4):
    """A shallow structural summary - top-level keys/types at each
    level, down to max_depth - for a genuine structural comparison,
    not just a byte-size comparison."""
    if depth >= max_depth:
        return "..."
    if isinstance(obj, dict):
        return {k: collect_top_level_shape(v, depth + 1, max_depth) for k, v in list(obj.items())[:20]}
    elif isinstance(obj, list):
        return [f"<list of {len(obj)}>"] + ([collect_top_level_shape(obj[0], depth + 1, max_depth)] if obj else [])
    else:
        return type(obj).__name__


def run_heatmap():
    print("=" * 70)
    print("PART A - Heatmap: isolated request, full body capture")
    print("=" * 70)
    result = make_request("ftse-constituents", HEATMAP_PARAMETERS, HEATMAP_COMPONENT_ID)
    print(f"Status: {result.get('status')}")
    print(f"Body bytes: {result.get('bodyBytes')}")
    print(f"Body SHA-256: {result.get('bodySha256')}")
    if result.get("error"):
        print(f"ERROR: {result['error']}")

    with open("lse_heatmap_production_shape_response.json", "w") as f:
        f.write(result.get("bodyText", ""))
    print("Full body saved to lse_heatmap_production_shape_response.json")

    if result.get("bodyText"):
        try:
            parsed = json.loads(result["bodyText"])
            shape = collect_top_level_shape(parsed)
            print("\nStructural shape (keys/types, 4 levels deep):")
            print(json.dumps(shape, indent=2, default=str)[:4000])

            # Search for the actual instrument-bearing arrays and report
            # exactly what's there, or that nothing is, rather than
            # asserting either.
            def find_instrument_like_lists(o, path=""):
                found = []
                if isinstance(o, dict):
                    for k, v in o.items():
                        if k in ("values", "content") and isinstance(v, list):
                            found.append((f"{path}.{k}", len(v), v[0] if v else None))
                        found.extend(find_instrument_like_lists(v, f"{path}.{k}"))
                elif isinstance(o, list):
                    for i, item in enumerate(o):
                        found.extend(find_instrument_like_lists(item, f"{path}[{i}]"))
                return found

            lists_found = find_instrument_like_lists(parsed)
            print(f"\n'values'/'content' arrays found in the response ({len(lists_found)}):")
            for path, length, sample in lists_found:
                print(f"  {path}: length={length}")
                if sample is not None:
                    print(f"    sample item: {json.dumps(sample, default=str)[:500]}")

            with open("lse_heatmap_structural_shape.json", "w") as f:
                json.dump({"shape": shape, "instrumentLikeLists": [
                    {"path": p, "length": l, "sample": s} for p, l, s in lists_found]}, f, indent=2, default=str)
        except Exception as e:
            print(f"Could not parse/analyze body: {type(e).__name__}: {e}")

    with open("lse_heatmap_full_result.json", "w") as f:
        json.dump({k: v for k, v in result.items() if k != "bodyText"}, f, indent=2, default=str)


def run_news_isolated(attempt_num):
    print("=" * 70)
    print(f"PART B - News Explorer: ISOLATED attempt {attempt_num} (fresh process)")
    print("=" * 70)
    result = make_request(NEWS_PATH, NEWS_PARAMETERS, NEWS_COMPONENT_ID)
    print(f"Timestamp: {result['timestamp']}")
    print(f"Status: {result.get('status')}")
    print(f"Body bytes: {result.get('bodyBytes')}")
    print(f"Body SHA-256: {result.get('bodySha256')}")
    if result.get("error"):
        print(f"ERROR: {result['error']}")

    attempt_summary = {
        "attempt": attempt_num, "timestamp": result["timestamp"], "status": result.get("status"),
        "bodyBytes": result.get("bodyBytes"), "bodySha256": result.get("bodySha256"),
        "error": result.get("error"),
    }

    if result.get("bodyText"):
        with open(f"lse_news_isolated_attempt_{attempt_num}_response.json", "w") as f:
            f.write(result["bodyText"])
        try:
            parsed = json.loads(result["bodyText"])
            block = find_newsexplorersearch_block(parsed)
            if block:
                attempt_summary["newsexplorersearchFound"] = True
                attempt_summary["storyCount"] = len(block.get("content", []))
                attempt_summary["totalElements"] = block.get("totalElements")
                attempt_summary["totalPages"] = block.get("totalPages")
                print(f"newsexplorersearch found: {attempt_summary['storyCount']} stories, "
                      f"totalElements={attempt_summary['totalElements']}, totalPages={attempt_summary['totalPages']}")
            else:
                attempt_summary["newsexplorersearchFound"] = False
                print("newsexplorersearch block: NOT FOUND in this response")
        except Exception as e:
            attempt_summary["parseError"] = f"{type(e).__name__}: {e}"
            print(f"Could not parse body as JSON: {type(e).__name__}: {e}")
    else:
        attempt_summary["newsexplorersearchFound"] = False

    # Append this attempt's summary to the shared results file (each
    # invocation is a separate process, so this file accumulates across
    # the workflow's repeated steps).
    results_file = "lse_news_isolated_all_attempts.json"
    try:
        with open(results_file) as f:
            all_results = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        all_results = []
    all_results.append(attempt_summary)
    with open(results_file, "w") as f:
        json.dump(all_results, f, indent=2, default=str)
    print(f"\nAppended to {results_file} ({len(all_results)} attempt(s) recorded so far)")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "heatmap"
    if mode == "heatmap":
        run_heatmap()
    elif mode == "news":
        attempt = int(sys.argv[2]) if len(sys.argv) > 2 else 1
        run_news_isolated(attempt)
    else:
        print(f"Unknown mode: {mode}. Use 'heatmap' or 'news <attempt_number>'.")
        sys.exit(1)
