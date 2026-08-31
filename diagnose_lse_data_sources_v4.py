"""
Diagnostic v4: full production trace of api.londonstockexchange.com's
data flow, chronologically ordered, with multi-hop chain-following,
and every JSON body saved as its own artifact file for direct
inspection (not just a printed preview).

Builds on v3 (which had a real, now-fixed bug: response.headers was
called as a function instead of accessed as the property it actually
is in this Playwright version, and a bare except swallowed the
resulting TypeError on every single response, producing a falsely
empty result). v4 keeps v3's fix (loud errors, never silently
swallowed) and adds:
  - every response tracked in chronological arrival order, so the
    requests immediately before/after components/refresh are visible,
    not just components/refresh in isolation
  - each captured JSON body written to its own file under
    lse_bodies/<page>/<NNN>_<hostname>.json for artifact upload -
    "full response saved" is a real file, not a truncated string
  - multi-hop reference following: if components/refresh (or anything
    it leads to) is itself config pointing at another endpoint, that
    endpoint is followed too, up to MAX_CHAIN_DEPTH hops, always via
    an ordinary unauthenticated GET, never retried past a 401/403

Same ground rules throughout: ordinary browser, ordinary page load, no
auth bypass, no credential extraction, no anti-bot circumvention. Any
endpoint that legitimately requires a session the public page itself
established is reported as such, never worked around.

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

REPORT = {"ranAt": datetime.now(timezone.utc).isoformat(), "pages": {}}
BODIES_DIR = "lse_bodies"
MAX_CHAIN_DEPTH = 2

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


def save_body(page_name, seq, url, body):
    """Writes one captured JSON body to its own file for artifact
    upload - a real, complete saved file, not a truncated preview
    string embedded in a log line."""
    safe_host = re.sub(r'[^a-zA-Z0-9._-]', '_', url.split("//", 1)[-1][:80])
    dir_path = os.path.join(BODIES_DIR, page_name)
    os.makedirs(dir_path, exist_ok=True)
    file_path = os.path.join(dir_path, f"{seq:03d}_{safe_host}.json")
    with open(file_path, "w") as f:
        f.write(body)
    return file_path


def try_follow_reference(url):
    """One ordinary, unauthenticated GET on a URL a captured response
    itself referenced. Never attaches any token or credential. A
    401/403 here is itself the answer - reported as such, never
    retried with different auth."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return {"status": resp.status, "body": body, "bodyLength": len(body)}
    except urllib.error.HTTPError as e:
        return {"status": e.code, "error": f"HTTP {e.code} - likely requires authentication/licensing"}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def inspect_page(name, url):
    from playwright.sync_api import sync_playwright

    result = {
        "url": url, "status": None, "error": None,
        "chronologicalJsonResponses": [],  # every JSON response, IN ORDER, minus noise
        "componentsRefreshChain": [],       # the full hop-by-hop chain starting at components/refresh
        "captureErrors": [],
    }

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(
                user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
            )
            captured_ordered = []  # list, not dict - preserves arrival order and duplicates
            capture_errors = []

            def on_response(response):
                try:
                    ctype = response.headers.get("content-type", "")
                    if "json" not in ctype.lower():
                        return
                    req = response.request
                    captured_ordered.append({
                        "url": response.url, "status": response.status, "method": req.method,
                        "requestPostData": req.post_data, "responseHeaders": dict(response.headers),
                        "body": response.text(),
                    })
                except Exception as e:
                    # Loud, not swallowed: a v3 predecessor hid a real bug
                    # behind a bare "except: pass" here, producing a
                    # misleadingly empty (not honestly failed) result.
                    capture_errors.append(f"{response.url}: {type(e).__name__}: {e}")

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
            result["captureErrors"] = capture_errors

            # Chronological record of every genuine (non-noise) JSON response,
            # each with its saved-body file path - this is what lets us show
            # "N responses immediately before/after components/refresh".
            for i, entry in enumerate(captured_ordered):
                if any(n in entry["url"] for n in NOISE_HOSTS):
                    continue
                file_path = save_body(name, i, entry["url"], entry["body"])
                parsed = None
                try:
                    parsed = json.loads(entry["body"])
                except Exception:
                    pass
                findings = classify_payload(parsed) if parsed is not None else {}
                result["chronologicalJsonResponses"].append({
                    "sequence": i, "url": entry["url"], "status": entry["status"],
                    "method": entry["method"], "bodyLength": len(entry["body"]),
                    "savedBodyFile": file_path, "realDataFindings": findings,
                })

            # Multi-hop chain starting from components/refresh: follow any
            # referenced endpoint found in its body, then in THAT response's
            # body, up to MAX_CHAIN_DEPTH hops - via ordinary unauthenticated
            # GETs only, never using any session/token, never retried past a
            # 401/403.
            refresh_matches = [e for e in result["chronologicalJsonResponses"] if "components/refresh" in e["url"]]
            if refresh_matches:
                start = refresh_matches[0]
                start_idx = start["sequence"]
                # The requests immediately surrounding components/refresh,
                # explicitly called out as requested.
                all_seqs = result["chronologicalJsonResponses"]
                pos = next(i for i, e in enumerate(all_seqs) if e["sequence"] == start_idx)
                result["surroundingComponentsRefresh"] = {
                    "before": all_seqs[max(0, pos - 2):pos],
                    "componentsRefresh": start,
                    "after": all_seqs[pos + 1:pos + 3],
                }

                with open(start["savedBodyFile"]) as f:
                    hop_body = f.read()
                hop_url = start["url"]
                visited = {hop_url}
                for depth in range(MAX_CHAIN_DEPTH + 1):
                    try:
                        hop_parsed = json.loads(hop_body)
                    except Exception as e:
                        result["componentsRefreshChain"].append(
                            {"depth": depth, "url": hop_url, "parseError": str(e)})
                        break
                    findings = classify_payload(hop_parsed)
                    referenced = [u for u in extract_urls(hop_body) if not any(n in u for n in NOISE_HOSTS)]
                    result["componentsRefreshChain"].append({
                        "depth": depth, "url": hop_url, "realDataFindings": findings,
                        "referencedUrls": referenced[:15],
                    })
                    if findings or depth >= MAX_CHAIN_DEPTH or not referenced:
                        break
                    next_url = next((u for u in referenced if u not in visited), None)
                    if not next_url:
                        break
                    visited.add(next_url)
                    followed = try_follow_reference(next_url)
                    if "error" in followed:
                        result["componentsRefreshChain"].append(
                            {"depth": depth + 1, "url": next_url, "followError": followed["error"]})
                        break
                    hop_body = followed["body"]
                    hop_url = next_url
            else:
                result["surroundingComponentsRefresh"] = None

    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"

    return result


def main():
    print("=" * 70)
    print("LSE DATA-SOURCE DIAGNOSTIC v4 - chronological trace, saved bodies, multi-hop chain")
    print("=" * 70)

    for name, url in TARGET_PAGES.items():
        print(f"\n{'=' * 50}\nInspecting: {name}\n{'=' * 50}")
        result = inspect_page(name, url)
        REPORT["pages"][name] = result

        if result.get("error"):
            print(f"  ERROR: {result['error']}")
            continue

        if result.get("captureErrors"):
            print(f"  WARNING: {len(result['captureErrors'])} response(s) failed to capture "
                  f"(shown here instead of silently hidden):")
            for ce in result["captureErrors"][:10]:
                print(f"    - {ce}")

        print(f"\n  Chronological JSON responses (in arrival order, tracking/consent noise excluded):")
        for e in result["chronologicalJsonResponses"]:
            marker = " <-- components/refresh" if "components/refresh" in e["url"] else ""
            print(f"    [{e['sequence']:03d}] {e['method']} {e['status']} {e['url'][:100]} "
                  f"(len={e['bodyLength']}, saved={e['savedBodyFile']}){marker}")
            if e["realDataFindings"]:
                for label, info in e["realDataFindings"].items():
                    print(f"          -> REAL DATA: {label}: key='{info['key']}' example={info['exampleValue']!r}")

        surround = result.get("surroundingComponentsRefresh")
        if surround is None:
            print("\n  components/refresh: NOT CAPTURED on this page")
            continue

        print(f"\n  Requests immediately BEFORE components/refresh:")
        for e in surround["before"]:
            print(f"    [{e['sequence']:03d}] {e['url'][:100]}")
        print(f"  >>> components/refresh itself: [{surround['componentsRefresh']['sequence']:03d}] "
              f"{surround['componentsRefresh']['url'][:100]}")
        print(f"  Requests immediately AFTER components/refresh:")
        for e in surround["after"]:
            print(f"    [{e['sequence']:03d}] {e['url'][:100]}")

        print(f"\n  Full chain trace starting from components/refresh (up to {MAX_CHAIN_DEPTH} hops):")
        for hop in result["componentsRefreshChain"]:
            if "followError" in hop:
                print(f"    depth {hop['depth']}: FOLLOW FAILED for {hop['url'][:100]} -> {hop['followError']}")
                continue
            if "parseError" in hop:
                print(f"    depth {hop['depth']}: {hop['url'][:100]} FAILED TO PARSE AS JSON: {hop['parseError']}")
                continue
            print(f"    depth {hop['depth']}: {hop['url'][:100]}")
            if hop["realDataFindings"]:
                print(f"      REAL DATA FOUND:")
                for label, info in hop["realDataFindings"].items():
                    print(f"        -> {label}: key='{info['key']}' example={info['exampleValue']!r}")
            else:
                print(f"      No recognizable market/news data fields at this depth.")
            if hop.get("referencedUrls"):
                print(f"      References found here ({len(hop['referencedUrls'])}): {hop['referencedUrls'][:5]}")

    with open("lse_diagnostic_report_v4.json", "w") as f:
        json.dump(REPORT, f, indent=2)
    print(f"\n\nSummary report written to lse_diagnostic_report_v4.json")
    print(f"Complete individual response bodies saved under {BODIES_DIR}/<page>/ for artifact upload")
    print("=" * 70)


if __name__ == "__main__":
    main()
