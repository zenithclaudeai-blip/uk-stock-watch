#!/usr/bin/env python3
"""
UK Stock Watch — cloud poller.

Runs on a GitHub Actions schedule instead of in a browser. Pulls the same free
sources as the browser extension (Google News, Yahoo News, Yahoo Finance quotes,
Investing.com UK's analyst-ratings feed), classifies items, pushes new
upgrade/downgrade alerts to a webhook (WhatsApp via CallMeBot, ntfy, etc.), and
writes a static dashboard page for GitHub Pages.

Stdlib only — no pip installs needed.
"""

import http.cookiejar
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

STATE_DIR = os.environ.get("STATE_DIR", "state")
DOCS_DIR = os.environ.get("DOCS_DIR", "docs")
DOCS_FILENAME = os.environ.get("DOCS_FILENAME", "index.html")
WATCHLIST_FILE = os.environ.get("WATCHLIST_FILE", "watchlist.json")
SEEN_FILE = os.path.join(STATE_DIR, "seen.json")
DATA_FILE = os.path.join(STATE_DIR, "data.json")
# When true, this run skips the market-wide broker search, LSE screener, and heat map —
# used by the large-watchlist hourly job so it doesn't duplicate what the fast 5-minute
# job already covers (which would mean duplicate WhatsApp alerts from two separate runs).
SKIP_MARKET_WIDE = os.environ.get("SKIP_MARKET_WIDE", "false").lower() == "true"
MAX_ITEMS_PER_TICKER = 60
MAX_SEEN = 3000

NEWS_MAX_AGE_DAYS = 21  # news/broker items older than this are filtered from the live feed
# When true, additionally restricts to items published "today" in the real London
# calendar (handles the GMT/BST switch correctly) — set False if this proves too
# strict and cuts out genuinely relevant items from yesterday evening.
NEWS_SAME_LONDON_DAY_ONLY = True

UPGRADE_WORDS = [
    "upgrade", "raises rating", "buy rating",
    "raised to buy", "initiates.*buy",
    r"upgrades?\s+\S+.{0,25}\s+to\s+['\"‘’]?(buy|overweight|outperform|add|accumulate)",
    r"raises?\s+\S+.{0,25}\s+to\s+['\"‘’]?(buy|overweight|outperform)",
    r"\bups\b\s+\S+.{0,25}\s+to\s+['\"‘’]?(buy|overweight|outperform)",
    r"moves?\s+\S+.{0,25}\s+to\s+['\"‘’]?(buy|overweight|outperform)",
]
DOWNGRADE_WORDS = [
    "downgrade", "cuts rating", "sell rating",
    "cut to sell", "initiates.*sell",
    r"cuts?\s+\S+.{0,25}\s+to\s+['\"‘’]?(sell|underweight|underperform|reduce|hold)",
    r"downgrades?\s+\S+.{0,25}\s+to\s+['\"‘’]?(sell|hold|underweight|underperform)",
    r"moves?\s+\S+.{0,25}\s+to\s+['\"‘’]?(sell|underweight|underperform)",
]
TARGET_WORDS = ["price target", "target price", "pt raised", "pt cut"]

# =========================================================================
# NORMALIZED ACTION VOCABULARY (per user's structured broker-monitor spec)
# =========================================================================
# Distinguishes a RATING change (upgrade/downgrade) from a TARGET-only change
# (target raised/cut with rating unchanged) — the two are different facts.
# Example: "BUY maintained, target £500->£550" = TARGET_RAISE, not UPGRADE.
# Checked in priority order inside classify(): a genuine rating change always
# wins over a target-only mention, since it's the more significant fact.
INITIATION_WORDS = [
    "initiates coverage", "initiated coverage", "starts coverage", "started coverage",
    "resumes coverage", "resumed coverage", "begins coverage", "initiate coverage",
]
REITERATION_WORDS = [
    "reiterates", "reiterated", "reaffirms", "reaffirmed", "maintains rating",
    "keeps rating", "retains rating", "sticks with",
]
TARGET_RAISE_WORDS = [
    r"hik(e|es|ed)\s+\S*.{0,30}(price target|target price)",
    r"rais(e|es|ed)\s+\S*.{0,30}(price target|target price)",
    r"lift(s|ed)?\s+\S*.{0,30}(price target|target price)",
    r"\bups\b\s+\S*.{0,30}(price target|target price)",
    r"increas(e|es|ed)\s+\S*.{0,30}(price target|target price)",
    r"boost(s|ed)?\s+\S*.{0,30}(price target|target price)",
    r"price target.{0,20}(raised|hiked|lifted|increased)",
    r"target price.{0,20}(raised|hiked|lifted|increased)",
]
TARGET_CUT_WORDS = [
    r"\bcuts?\b\s+\S*.{0,30}(price target|target price)",
    r"lower(s|ed)?\s+\S*.{0,30}(price target|target price)",
    r"trim(s|med)?\s+\S*.{0,30}(price target|target price)",
    r"slash(es|ed)?\s+\S*.{0,30}(price target|target price)",
    r"reduc(e|es|ed)\s+\S*.{0,30}(price target|target price)",
    r"price target.{0,20}(cut|lowered|trimmed|reduced|slashed)",
    r"target price.{0,20}(cut|lowered|trimmed|reduced|slashed)",
]

# Rating-text -> BULLISH/NEUTRAL/BEARISH bucket, for a plain-English "what
# does this rating mean" signal without inventing a prediction — purely a
# classification of the label itself, same as the user's spec.
BULLISH_RATING_TERMS = {
    "buy", "strong buy", "outperform", "overweight", "add", "accumulate",
    "top pick", "speculative buy", "house stock",
}
NEUTRAL_RATING_TERMS = {
    "hold", "neutral", "market perform", "equal weight", "equal-weight",
    "sector perform", "in-line", "no recommendation", "coverage pending",
}
BEARISH_RATING_TERMS = {
    "sell", "strong sell", "underperform", "underweight", "reduce", "trading sell",
}


def normalize_rating_bucket(raw_rating):
    """Maps any broker rating text to BULLISH/NEUTRAL/BEARISH, or None if
    unrecognized. Purely descriptive classification of the label itself —
    never a recommendation, never predicts anything."""
    if not raw_rating:
        return None
    r = raw_rating.strip().lower().replace("_", " ")
    if r in BULLISH_RATING_TERMS:
        return "BULLISH"
    if r in NEUTRAL_RATING_TERMS:
        return "NEUTRAL"
    if r in BEARISH_RATING_TERMS:
        return "BEARISH"
    return None


def normalize_action_from_grades(from_grade, to_grade, yahoo_action_code=None):
    """
    Returns one of: UPGRADE, DOWNGRADE, REITERATION, INITIATION, RATING_CHANGE,
    NO_CHANGE. Prefers Yahoo's own action code (most reliable, since Yahoo
    already knows the real event type); falls back to comparing bullish/
    neutral/bearish buckets of from/to grade when no action code is given.
    """
    if yahoo_action_code == "init":
        return "INITIATION"
    if yahoo_action_code == "up":
        return "UPGRADE"
    if yahoo_action_code == "down":
        return "DOWNGRADE"
    if yahoo_action_code == "reit":
        return "REITERATION"
    from_bucket = normalize_rating_bucket(from_grade)
    to_bucket = normalize_rating_bucket(to_grade)
    if from_bucket and to_bucket:
        order = {"BEARISH": 0, "NEUTRAL": 1, "BULLISH": 2}
        if from_bucket == to_bucket:
            return "NO_CHANGE"
        return "UPGRADE" if order[to_bucket] > order[from_bucket] else "DOWNGRADE"
    if from_grade and to_grade and from_grade != to_grade:
        return "RATING_CHANGE"
    return "NO_CHANGE"


def normalize_headline_action(title):
    """
    Classifies a NEWS HEADLINE (not structured Yahoo data) into the same
    normalized vocabulary. Checked in priority order: a genuine rating
    change always wins over a target-only mention, since a headline saying
    "Citi upgrades X, raises target" is fundamentally an UPGRADE, not a
    TARGET_RAISE, even though it mentions the target too.
    """
    t = title.lower()
    if any(re.search(w, t) for w in UPGRADE_WORDS):
        return "UPGRADE"
    if any(re.search(w, t) for w in DOWNGRADE_WORDS):
        return "DOWNGRADE"
    if any(w in t for w in INITIATION_WORDS):
        return "INITIATION"
    if any(re.search(w, t) for w in TARGET_RAISE_WORDS):
        return "TARGET_RAISE"
    if any(re.search(w, t) for w in TARGET_CUT_WORDS):
        return "TARGET_CUT"
    if any(w in t for w in REITERATION_WORDS):
        return "REITERATION"
    return None  # not a broker-action headline at all — classify() handles the rest

# Maps classify()'s lowercase category (used for CSS badge classes) to the
# uppercase normalized vocabulary word (used in the structured data /
# alert labels) — keeps the two representations consistent everywhere.
CATEGORY_TO_NORMALIZED_ACTION = {
    "upgrade": "UPGRADE", "downgrade": "DOWNGRADE", "initiation": "INITIATION",
    "target_raise": "TARGET_RAISE", "target_cut": "TARGET_CUT", "reiteration": "REITERATION",
    "target": "RATING_CHANGE", "director_dealing": None, "event": None, "news": None,
}

# Director/PDMR (Persons Discharging Managerial Responsibilities) dealings — a standard
# RNS announcement type disclosing when a company director has bought or sold shares.
# Real, published, factual disclosure — not something this tool infers or predicts.
DIRECTOR_DEALING_WORDS = [
    "pdmr", "director/pdmr shareholding", "director shareholding",
    "notification of transactions by persons discharging managerial responsibilities",
    "director dealing", "directors' dealing", "holding(s) in company",
]
EVENT_WORDS = [
    "trading update", "results", "interim report", "final results", "agm",
    "dividend", "profit warning", "acquisition", "placing", "earnings", "guidance",
    "merger", "merges", "merging", "takeover", "bid for", "bids for", "acquire",
    "acquires", "acquiring", "buyout", "to be bought", "in talks to", "offer for",
]
BROKER_NAMES = [
    "Barclays", "Deutsche Bank", "JPMorgan", "JP Morgan", "HSBC", "UBS", "Citi",
    "Citigroup", "Goldman Sachs", "Morgan Stanley", "RBC", "RBC Capital Markets",
    "Jefferies", "Peel Hunt", "Numis", "Berenberg", "Panmure", "Panmure Liberum",
    "Shore Capital", "Investec", "Canaccord", "Canaccord Genuity", "Liberum", "BofA",
    "Bank of America", "Societe Generale", "BNP Paribas", "Credit Suisse", "Stifel",
    "Redburn", "Zeus Capital", "Cenkos", "finnCap", "Singer Capital Markets",
    "N+1 Singer", "WH Ireland", "Edison", "Equity Development", "Cantor Fitzgerald",
    "Exane BNP Paribas", "Kepler Cheuvreux", "Mediobanca", "Davy", "Goodbody",
    "Third Bridge", "Marex", "Turner Pope", "Arden Partners", "Charles Stanley",
    "AJ Bell", "Hargreaves Lansdown", "Killik", "Interactive Investor", "Shard Capital",
    "Oberon Capital", "Whitman Howard", "Progressive Equity Research", "Hardman & Co",
]
ANALYST_RATINGS_FEED_URL = "https://uk.investing.com/rss/news_1061.rss"
GENERAL_MARKET_NEWS_FEED_URL = "https://uk.investing.com/rss/news_25.rss"  # confirmed via their own official RSS listing page
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
}

# Yahoo's screener and quoteSummary endpoints reject anonymous requests from cloud/
# datacenter IPs (GitHub Actions runners included) with a 401 unless a session cookie
# + "crumb" token — Yahoo's own web frontend's real auth handshake — are attached.
# The basic chart/quote endpoint is more lenient and doesn't need this. This uses a
# shared cookie jar across all Yahoo requests so the session persists once established.
_yahoo_cookiejar = http.cookiejar.CookieJar()
_yahoo_opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_yahoo_cookiejar))
_yahoo_crumb = None


def _yahoo_opener_get(url, timeout=15):
    req = urllib.request.Request(url, headers=HEADERS)
    with _yahoo_opener.open(req, timeout=timeout) as resp:
        return resp.read()


def get_yahoo_crumb():
    """Fetch (and cache) a Yahoo auth crumb, establishing the session cookie first."""
    global _yahoo_crumb
    if _yahoo_crumb:
        return _yahoo_crumb
    try:
        _yahoo_opener_get("https://fc.yahoo.com")  # sets the initial session cookie
    except Exception as e:
        print(f"  ! yahoo cookie handshake failed: {e}", file=sys.stderr)
    try:
        crumb = _yahoo_opener_get("https://query2.finance.yahoo.com/v1/test/getcrumb").decode("utf-8").strip()
        if crumb and "<html" not in crumb.lower():
            _yahoo_crumb = crumb
            return _yahoo_crumb
    except Exception as e:
        print(f"  ! yahoo crumb fetch failed: {e}", file=sys.stderr)
    return None


def http_get(url, timeout=15):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def classify(title):
    t = title.lower()
    # Downgrade checked before upgrade: a headline like "downgraded X to Y from
    # outperform" would otherwise risk matching an upgrade-side pattern first —
    # a genuine downgrade always contains "downgrade"/"cuts"-style language
    # that's a stronger, less ambiguous signal than any upgrade-side term.
    if any(re.search(w, t) for w in DOWNGRADE_WORDS):
        return "downgrade"
    if any(re.search(w, t) for w in UPGRADE_WORDS):
        return "upgrade"
    if any(w in t for w in INITIATION_WORDS):
        return "initiation"
    if any(re.search(w, t) for w in TARGET_RAISE_WORDS):
        return "target_raise"
    if any(re.search(w, t) for w in TARGET_CUT_WORDS):
        return "target_cut"
    if any(w in t for w in REITERATION_WORDS):
        return "reiteration"
    if any(w in t for w in TARGET_WORDS):
        return "target"
    if any(w in t for w in DIRECTOR_DEALING_WORDS):
        return "director_dealing"
    if any(w in t for w in EVENT_WORDS):
        return "event"
    return "news"


def detect_broker(title):
    t = title.lower()
    for b in BROKER_NAMES:
        if b.lower() in t:
            return b
    return ""


def parse_rss(xml_bytes):
    items = []
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return items
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub_date = (item.findtext("pubDate") or "").strip()
        source_el = item.find("source")
        source = source_el.text.strip() if source_el is not None and source_el.text else ""
        if not source and link:
            try:
                source = urllib.parse.urlparse(link).hostname.replace("www.", "")
            except Exception:
                source = ""
        if title and link:
            items.append({"title": title, "link": link, "pubDate": pub_date, "source": source})
    return items


def fetch_yahoo_analyst(ticker):
    """
    Structured analyst ratings history via Yahoo Finance's quoteSummary endpoint —
    real firm name, exact action (up/down/init/main/reit), and date, straight from
    Yahoo's own aggregation of broker calls. Requires the crumb-authenticated opener;
    falls back to trying without a crumb if one couldn't be obtained.
    Also pulls calendarEvents (next earnings date, next ex-dividend date) in the SAME
    request — quoteSummary accepts multiple modules at once, so this adds real data
    without adding a new network call.
    """
    symbol = yahoo_symbol(ticker)
    crumb = get_yahoo_crumb()
    url = (
        f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{urllib.parse.quote(symbol)}"
        "?modules=upgradeDowngradeHistory,recommendationTrend,financialData,calendarEvents,summaryDetail,defaultKeyStatistics,price,assetProfile"
    )
    if crumb:
        url += f"&crumb={urllib.parse.quote(crumb)}"
    try:
        data = json.loads(_yahoo_opener_get(url))
        result = (((data.get("quoteSummary") or {}).get("result")) or [None])[0]
        if not result:
            return None
        history = ((result.get("upgradeDowngradeHistory") or {}).get("history")) or []
        fin = result.get("financialData") or {}
        cal = result.get("calendarEvents") or {}
        earnings_dates = ((cal.get("earnings") or {}).get("earningsDate")) or []
        next_earnings = earnings_dates[0].get("raw") if earnings_dates else None
        ex_div = (cal.get("exDividendDate") or {}).get("raw")
        summary = result.get("summaryDetail") or {}
        div_rate = (summary.get("dividendRate") or {}).get("raw")  # currency amount per share per year
        div_yield_raw = (summary.get("dividendYield") or {}).get("raw")  # Yahoo returns this as a fraction, e.g. 0.045 = 4.5%
        div_yield_pct = div_yield_raw * 100 if div_yield_raw is not None else None
        stats = result.get("defaultKeyStatistics") or {}
        price_mod = result.get("price") or {}
        trailing_pe = (summary.get("trailingPE") or {}).get("raw")
        eps = (stats.get("trailingEps") or {}).get("raw")
        market_cap = (price_mod.get("marketCap") or {}).get("raw")
        fifty_two_low = (summary.get("fiftyTwoWeekLow") or {}).get("raw")
        fifty_two_high = (summary.get("fiftyTwoWeekHigh") or {}).get("raw")
        held_insiders_raw = (stats.get("heldPercentInsiders") or {}).get("raw")  # fraction, e.g. 0.12 = 12%
        held_insiders_pct = held_insiders_raw * 100 if held_insiders_raw is not None else None
        profile = result.get("assetProfile") or {}
        sector = profile.get("sector")
        industry = profile.get("industry")
        business_summary = profile.get("longBusinessSummary")
        if business_summary and len(business_summary) > 220:
            business_summary = business_summary[:217].rsplit(" ", 1)[0] + "..."  # trim at a word boundary
        return {
            "history": history,
            "targetMeanPrice": (fin.get("targetMeanPrice") or {}).get("raw"),
            "recommendationKey": fin.get("recommendationKey"),
            "nextEarningsDate": next_earnings,  # unix epoch seconds, or None
            "exDividendDate": ex_div,  # unix epoch seconds, or None
            "dividendRate": div_rate,  # per-share currency amount, or None
            "dividendYieldPct": div_yield_pct,  # already converted to a percentage, or None
            "trailingPE": trailing_pe,
            "trailingEps": eps,
            "marketCap": market_cap,
            "fiftyTwoWeekLow": fifty_two_low,
            "fiftyTwoWeekHigh": fifty_two_high,
            "heldPercentInsidersPct": held_insiders_pct,
            "sector": sector,
            "industry": industry,
            "businessSummary": business_summary,
        }
    except Exception as e:
        print(f"  ! yahoo analyst history failed: {ticker} ({e})", file=sys.stderr)
        return None


FCA_SHORT_INTEREST_URL = "https://www.fca.org.uk/publication/data/short-positions-daily-update.xlsx"


def fetch_short_interest():
    """
    FCA's daily Aggregate Net Short Position file — free, official, no auth.
    T+2 lag (today's figures reflect positions from two working days ago).
    Returns dict: COMPANY NAME (upper, stripped) -> {"pct": float, "position_date": str}
    Fails soft (returns {}) — same pattern as every other fetch_* here.
    """
    try:
        import openpyxl
    except ImportError:
        print("  ! short interest skipped: openpyxl not installed (add to requirements.txt)", file=sys.stderr)
        return {}
    try:
        import io
        raw = http_get(FCA_SHORT_INTEREST_URL, timeout=20)
        wb = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
        ws = wb.active

        header_row_idx, headers = None, {}
        for i, row in enumerate(ws.iter_rows(min_row=1, max_row=10, values_only=True)):
            for cell in row:
                if cell and "issuer" in str(cell).lower():
                    header_row_idx = i + 1
                    headers = {str(h).strip().lower(): k for k, h in enumerate(row) if h}
                    break
            if header_row_idx:
                break
        if header_row_idx is None:
            print("  ! short interest: header row not found — FCA file format may have changed", file=sys.stderr)
            return {}

        name_col = next((v for k, v in headers.items() if "issuer" in k), None)
        pct_col = next((v for k, v in headers.items() if "%" in k or "percent" in k), None)
        date_col = next((v for k, v in headers.items() if "date" in k), None)
        if name_col is None or pct_col is None:
            print(f"  ! short interest: required columns not found (saw: {list(headers.keys())})", file=sys.stderr)
            return {}

        result = {}
        for row in ws.iter_rows(min_row=header_row_idx + 1, values_only=True):
            if not row or row[name_col] is None:
                continue
            name = str(row[name_col]).strip().upper()
            try:
                pct = float(row[pct_col])
            except (TypeError, ValueError):
                continue
            result[name] = {
                "pct": pct,
                "position_date": str(row[date_col]) if date_col is not None else None,
            }
        print(f"  short interest: loaded {len(result)} companies from FCA")
        return result
    except Exception as e:
        print(f"  ! short interest fetch failed: {e}", file=sys.stderr)
        return {}


def match_short_interest(company_name, short_interest_map):
    """Loose name match — FCA issuer names rarely match Yahoo's/watchlist's naming exactly."""
    if not company_name or not short_interest_map:
        return None
    clean = company_name.upper().replace(" PLC", "").replace(" ORD", "").strip()
    for fca_name, data in short_interest_map.items():
        clean_fca = fca_name.replace(" PLC", "").replace(" ORD", "").strip()
        if clean in clean_fca or clean_fca in clean:
            return data
    return None


ACTION_CATEGORY = {"up": "upgrade", "down": "downgrade", "init": "initiation", "main": "news", "reit": "reiteration"}
RECENT_WINDOW_SECONDS = 2 * 24 * 3600  # only alert on ratings from the last 48h, not backfilled history


def format_normalized_at(iso_string):
    """Formats a normalizedAt ISO timestamp (when this item's classification was
    computed) as a plain London date/time, same dual-timezone convention as the
    rest of the dashboard — distinct from pubDate (the article/event's own date),
    since a backfilled Yahoo history item can be re-classified on a later run
    than when the underlying event actually happened."""
    if not iso_string:
        return None
    try:
        dt = datetime.fromisoformat(iso_string)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return format_london_and_utc(dt)
    except Exception:
        return None


def analyst_history_to_items(ticker, analyst):
    items = []
    if not analyst:
        return items
    symbol = yahoo_symbol(ticker)
    now = time.time()
    normalized_at = datetime.now(timezone.utc).isoformat()  # when THIS classification ran —
                                                              # distinct from the event's own date,
                                                              # since backfilled history can be
                                                              # re-classified on a later run than
                                                              # when the actual rating happened.
    for h in analyst.get("history", [])[:15]:
        firm = h.get("firm", "")
        to_grade = h.get("toGrade", "")
        from_grade = h.get("fromGrade", "")
        action = h.get("action", "")
        epoch = h.get("epochGradeDate")
        if not firm or not to_grade or not epoch:
            continue
        pub_date = datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
        title = f"{firm} {'moves' if action=='main' else 'rates'} {ticker} to {to_grade}" + (
            f" (from {from_grade})" if from_grade and from_grade != to_grade else ""
        )
        items.append({
            "title": title,
            "link": f"https://finance.yahoo.com/quote/{symbol}/analysis#{firm}-{epoch}".replace(" ", "-"),
            "pubDate": pub_date,
            "source": "Yahoo Finance Analyst History",
            "category": ACTION_CATEGORY.get(action, "news"),
            "broker": firm,
            "_recent": (now - epoch) <= RECENT_WINDOW_SECONDS,
            # Normalized structured fields (per the broker-monitor spec) — kept
            # alongside the original wording (fromGrade/toGrade preserved as-is).
            "normalizedAction": normalize_action_from_grades(from_grade, to_grade, action),
            "fromRating": from_grade or None,
            "toRating": to_grade,
            "fromRatingBucket": normalize_rating_bucket(from_grade),
            "toRatingBucket": normalize_rating_bucket(to_grade),
            "normalizedAt": normalized_at,  # when this classification was computed (ISO UTC)
        })
    return items



import email.utils as _email_utils


LONDON_TZ = ZoneInfo("Europe/London")


def _parse_pub_date(pub_date_str):
    """
    Shared date parser — tries RFC 822 (standard RSS: "Wed, 31 Jul 2024 07:00:00 GMT")
    then Investing.com's plain "YYYY-MM-DD HH:MM:SS" format. Returns a tz-aware
    datetime, or None if genuinely unparseable.
    """
    if not pub_date_str:
        return None
    dt = None
    try:
        dt = _email_utils.parsedate_to_datetime(pub_date_str)
    except Exception:
        pass
    if dt is None:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(pub_date_str.strip(), fmt)
                break
            except Exception:
                continue
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def is_recent_enough(pub_date_str, max_age_days=NEWS_MAX_AGE_DAYS):
    """
    Returns True if pub_date_str parses to within max_age_days of now, OR if it can't
    be parsed at all (fail open — an unparseable date shouldn't silently hide an item,
    that's a display bug, not a staleness signal).
    """
    dt = _parse_pub_date(pub_date_str)
    if dt is None:
        return True
    age = datetime.now(timezone.utc) - dt
    return age.days <= max_age_days


def is_today_in_london(pub_date_str):
    """
    Stricter same-day filter, using the real Europe/London calendar date (correctly
    handles the GMT/BST switch via zoneinfo, not just a fixed UTC offset) — 'today'
    means today in London, not just 'within the last N days'. Fails open on an
    unparseable date, same reasoning as is_recent_enough.
    """
    dt = _parse_pub_date(pub_date_str)
    if dt is None:
        return True
    return dt.astimezone(LONDON_TZ).date() == datetime.now(timezone.utc).astimezone(LONDON_TZ).date()


def passes_news_filters(pub_date_str):
    """Combined recency check applied to every news/broker item before it can enter
    the feed, alerts, or dashboard."""
    if not is_recent_enough(pub_date_str):
        return False
    if NEWS_SAME_LONDON_DAY_ONLY and not is_today_in_london(pub_date_str):
        return False
    return True


def item_sort_key(it):
    """Sort key for ordering news/alert items latest-first. NEVER sort on the raw
    pubDate string directly — RFC 822 dates start with the day name ("Thu, 27 Aug...",
    "Fri, 28 Aug..."), so a plain string sort groups by day-of-week alphabetically
    ("Fri" < "Thu") rather than by actual chronological time, silently scrambling the
    real order. Parse to a real datetime first; fall back to detectedAt (stored as
    Python's .isoformat(), a different format _parse_pub_date doesn't cover — handled
    separately here), then epoch 0 (sorts last) for anything genuinely unparseable."""
    dt = _parse_pub_date(it.get("pubDate"))
    if dt is None and it.get("detectedAt"):
        try:
            dt = datetime.fromisoformat(it["detectedAt"])
        except Exception:
            dt = None
    if dt is None:
        return datetime.fromtimestamp(0, tz=timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def fetch_feed(url):
    try:
        return parse_rss(http_get(url)), False
    except Exception as e:
        print(f"  ! feed fetch failed: {url} ({e})", file=sys.stderr)
        return [], True


def clean_company_name(name):
    """
    Yahoo's screener returns full share-listing names, e.g. "MCBRIDE PLC ORD 10P",
    "GEIGER COUNTER LIMITED ORD NPV", "SMITH & NEPHEW PLC ORD USD0.20" — the "ORD ..."
    part is share-class jargon (par value, currency) that never appears in actual news
    articles. Searching Google News for that literal string returns almost nothing,
    because real headlines just say "McBride" — confirmed against real data: McBride's
    genuine same-day news (a Vestacy partnership announcement) was being missed
    entirely because "ORD 10P" polluted the search query. Trimming everything from
    " ORD " onward gives a name that actually matches how news outlets refer to the
    company. Names that don't contain " ORD " (e.g. watchlist names you supplied
    yourself, like "Vodafone Group") pass through unchanged.
    """
    if not name:
        return name
    cleaned = re.split(r"\bORD\b", name, maxsplit=1, flags=re.IGNORECASE)[0].strip()
    return cleaned or name  # never return an empty string


def google_news_url(company_name):
    q = f'{company_name} (LSE OR "London Stock Exchange") (upgrade OR downgrade OR "price target" OR rating)'
    return "https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {"q": q, "hl": "en-GB", "gl": "GB", "ceid": "GB:en"}
    )


def general_news_url(company_name):
    """Broader than google_news_url — not restricted to rating/broker keywords, so it
    catches general company news (earnings, M&A, trading updates) for stocks that show
    up in the screener but aren't on the watchlist."""
    q = f'{company_name} (LSE OR "London Stock Exchange")'
    return "https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {"q": q, "hl": "en-GB", "gl": "GB", "ceid": "GB:en"}
    )


def reuters_bloomberg_url(company_name):
    # Reuters dropped public RSS years ago and Bloomberg is subscription-walled, so
    # neither offers a free feed directly. This scopes the same free Google News search
    # to just those two publishers (site: filter) — legitimate, no scraping either site,
    # same mechanism already used for the general news search.
    q = f'{company_name} (LSE OR "London Stock Exchange") (site:reuters.com OR site:bloomberg.com)'
    return "https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {"q": q, "hl": "en-GB", "gl": "GB", "ceid": "GB:en"}
    )


def market_wide_broker_news_url():
    # NOT scoped to a company name — this is what makes it market-wide rather than
    # watchlist-only. One request per poll cycle covers every LSE-listed company's
    # broker rating news, instead of the (infeasible) approach of adding all ~1,900
    # LSE companies to the watchlist and polling each individually.
    q = '(LSE OR "London Stock Exchange") (upgrade OR downgrade OR "price target" OR "rating") (broker OR analyst OR bank)'
    return "https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {"q": q, "hl": "en-GB", "gl": "GB", "ceid": "GB:en"}
    )


def yahoo_symbol(ticker):
    t = ticker.strip().upper()
    return t if t.endswith(".L") else f"{t.rstrip('.')}.L"


def yahoo_news_url(ticker):
    return "https://feeds.finance.yahoo.com/rss/2.0/headline?" + urllib.parse.urlencode(
        {"s": yahoo_symbol(ticker), "region": "UK", "lang": "en-GB"}
    )


def fetch_ftse100():
    """The overall market headline every major finance site leads with — a single call
    once per cycle (not per-stock), for the ^FTSE index itself."""
    url = "https://query1.finance.yahoo.com/v8/finance/chart/%5EFTSE?interval=1d&range=5d"
    try:
        data = json.loads(http_get(url))
        result = (data.get("chart") or {}).get("result") or [None]
        if not result[0]:
            return None
        meta = result[0].get("meta", {})
        price = meta.get("regularMarketPrice")
        prev_close = meta.get("chartPreviousClose") or meta.get("previousClose")
        if price is None or not prev_close:
            return None
        change = price - prev_close
        change_pct = change / prev_close * 100
        return {"price": price, "change": change, "changePct": change_pct}
    except Exception as e:
        print(f"  ! FTSE 100 fetch failed: {e}", file=sys.stderr)
        return None


def fetch_yahoo_quote(ticker):
    symbol = yahoo_symbol(ticker)
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}?interval=1m&range=1d"
    try:
        data = json.loads(http_get(url))
        result = (data.get("chart") or {}).get("result") or [None]
        meta = (result[0] or {}).get("meta") if result[0] else None
        if not meta or "regularMarketPrice" not in meta:
            return None
        price = meta["regularMarketPrice"]
        prev_close = meta.get("previousClose") or meta.get("chartPreviousClose")
        change = price - prev_close if prev_close else None
        change_pct = (change / prev_close * 100) if (prev_close and change is not None) else None
        day_high = meta.get("regularMarketDayHigh")
        day_low = meta.get("regularMarketDayLow")
        # Intraday range as % of previous close — a purely descriptive volatility
        # measure (how much the stock has already swung today), not a prediction.
        range_pct = ((day_high - day_low) / prev_close * 100) if (day_high and day_low and prev_close) else None
        return {
            "price": price,
            "currency": meta.get("currency", "GBp"),
            "change": change,
            "changePct": change_pct,
            "dayHigh": day_high,
            "dayLow": day_low,
            "rangePct": range_pct,
            "asOf": meta.get("regularMarketTime", int(time.time())) * 1000,
        }
    except Exception as e:
        print(f"  ! yahoo quote failed: {ticker} ({e})", file=sys.stderr)
        return None


def compute_rsi(closes, period=14):
    """Standard RSI (relative strength index) — a raw computed statistic from real
    closing prices, shown as a number, not labeled 'overbought'/'oversold' or turned
    into a recommendation. That labeling is exactly the kind of implied-advice framing
    this tool avoids; the number itself is just arithmetic on public price history."""
    if len(closes) < period + 1:
        return None
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    recent = deltas[-period:]
    gains = [d for d in recent if d > 0]
    losses = [-d for d in recent if d < 0]
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def fetch_price_technicals(ticker):
    """Real, already-happened price history — 5-day % change, RSI(14), and 20-day
    moving average, all computed from the same single chart fetch (a longer range than
    strictly needed for the 5-day figure, so RSI/MA come along for free rather than
    requiring a second network call). Facts about the past, not predictions."""
    symbol = yahoo_symbol(ticker)
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}?interval=1d&range=3mo"
    try:
        data = json.loads(http_get(url))
        result = (data.get("chart") or {}).get("result") or [None]
        if not result[0]:
            return None
        closes = (result[0].get("indicators", {}).get("quote", [{}])[0] or {}).get("close") or []
        closes = [c for c in closes if c is not None]  # some days can come back null (holidays etc.)
        if len(closes) < 6:
            return None  # not enough real trading days to compute even the 5-day change
        latest = closes[-1]
        five_days_ago = closes[-6]
        change_pct = (latest - five_days_ago) / five_days_ago * 100 if five_days_ago else None
        rsi14 = compute_rsi(closes, 14)
        ma20 = sum(closes[-20:]) / len(closes[-20:]) if len(closes) >= 20 else None
        return {
            "changePct5d": change_pct,
            "price": latest,
            "rsi14": rsi14,
            "ma20": ma20,
            "aboveMA20": (latest > ma20) if ma20 else None,
        }
    except Exception as e:
        print(f"  ! price technicals fetch failed: {ticker} ({e})", file=sys.stderr)
        return None


BIG_MOVER_THRESHOLD_PCT = 5.0  # purely descriptive flag: "this has already moved a lot today"
UPTREND_5DAY_THRESHOLD_PCT = 5.0  # flagged as "5-day uptrend" once risen at least this much
# Gainers/losers liquidity filter — without this, illiquid penny stocks with meaningless
# tiny-absolute-value swings (0.1p -> 1p = "900%") dominate the list.
MIN_VOLUME_FOR_MOVERS = 500_000
MIN_PRICE_PENCE_FOR_MOVERS = 20  # excludes sub-20p penny stocks specifically from gainers/losers
# News/broker items older than this are considered stale and filtered from the live feed —
# Google News search returns "most relevant," not "most recent," so old syndicated pieces
# (sometimes years old) can otherwise resurface in results.


def fetch_gb_screener(sort_field, sort_type="DESC", count=10):
    """
    Generic LSE-scoped screener via Yahoo Finance's free public endpoint — same source
    already used for top-volume, generalized to any sortable field (dayvolume,
    percentchange, etc). Requires the crumb-authenticated opener; Yahoo 401s this
    endpoint for anonymous requests from cloud/datacenter IPs.

    Yahoo's "region: gb" tag catches thousands of obscure GDRs/depositary receipts
    (symbols ending ".IL" etc) alongside genuine LSE-listed shares — these are thinly
    traded, so their %-change numbers are noise (a move from 0.1p to 1p shows as
    "900%"). Filtered here to only ".L"-suffixed symbols, the standard LSE ticker
    format, and over-fetched (4x) since a chunk gets filtered out.
    """
    crumb = get_yahoo_crumb()
    url = "https://query1.finance.yahoo.com/v1/finance/screener"
    if crumb:
        url += f"?crumb={urllib.parse.quote(crumb)}"
    # Illiquid penny stocks dominate the top of a raw %-change sort by definition —
    # a small over-fetch (e.g. 4x) means genuinely liquid mid/large-caps can be buried
    # past the fetch window entirely, leaving nothing after the liquidity filter runs.
    # Gainers/losers need a much bigger pool to filter from than Volume does.
    fetch_size = count * 50 if sort_field == "percentchange" else count * 4
    fetch_size = min(fetch_size, 250)  # stay within what the endpoint reliably accepts
    body = json.dumps({
        "size": fetch_size,
        "offset": 0,
        "sortField": sort_field,
        "sortType": sort_type,
        "quoteType": "EQUITY",
        "query": {"operator": "eq", "operands": ["region", "gb"]},
    }).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={**HEADERS, "Content-Type": "application/json"},
    )
    try:
        data = json.loads(_yahoo_opener.open(req, timeout=15).read())
        quotes = (((data.get("finance") or {}).get("result") or [{}])[0]).get("quotes", [])
        out = []
        for q in quotes:
            symbol = q.get("symbol", "")
            if not symbol.upper().endswith(".L"):
                continue  # skip GDRs/IOB instruments etc — keep genuine LSE-listed shares only
            # LSE International Order Book tickers (depositary receipts for FOREIGN
            # companies, not genuine UK-listed businesses) conventionally start with a
            # digit — e.g. "0R9U.L" is PayPal Holdings, confirmed by its news being
            # entirely US-market coverage. Excluding these keeps results to genuine LSE
            # primary listings, matching what "LSE-listed stocks only" actually promises.
            ticker_part = symbol.upper().rsplit(".L", 1)[0]
            if ticker_part[:1].isdigit():
                continue
            volume = q.get("regularMarketVolume")
            price = q.get("regularMarketPrice")
            if sort_field == "dayvolume" and not volume:
                continue  # skip zero/missing-volume noise from the volume ranking specifically
            if sort_field == "percentchange":
                # Gainers/losers dominated by illiquid penny stocks: a move from 0.1p to
                # 1p shows as "900%" and is meaningless. Require real liquidity and a
                # non-penny price so the list reflects stocks actually worth noticing.
                if not volume or volume < MIN_VOLUME_FOR_MOVERS:
                    continue
                if not price or price < MIN_PRICE_PENCE_FOR_MOVERS:
                    continue
            out.append({
                "symbol": symbol,
                "name": q.get("shortName", symbol),
                "volume": volume,
                "price": price,
                "changePct": q.get("regularMarketChangePercent"),
            })
            if len(out) >= count:
                break
        return out
    except Exception as e:
        print(f"  ! screener failed (sortField={sort_field}): {e}", file=sys.stderr)
        return []


def fetch_lse_screener():
    return {
        "volume": fetch_gb_screener("dayvolume", "DESC", 10),
        "gainers": fetch_gb_screener("percentchange", "DESC", 10),
        "losers": fetch_gb_screener("percentchange", "ASC", 10),
    }


def now_stamp():
    """Human-readable London-time stamp for every outgoing message and the dashboard —
    'this is live and this is exactly when' rather than leaving it implicit. Shows
    London time first (what matters for a UK tool) with the London/BST or GMT label
    zoneinfo resolves automatically, plus UTC alongside for technical clarity."""
    now_utc = datetime.now(timezone.utc)
    now_london = now_utc.astimezone(LONDON_TZ)
    return f'{now_london.strftime("%a %d %b %Y, %H:%M")} {now_london.strftime("%Z")} ({now_utc.strftime("%H:%M")} UTC)'


def format_london_and_utc(dt_utc):
    """Same dual London/UTC format as now_stamp(), for a specific stored UTC datetime
    (e.g. lastPoll) rather than 'right now'."""
    if dt_utc.tzinfo is None:
        dt_utc = dt_utc.replace(tzinfo=timezone.utc)
    dt_london = dt_utc.astimezone(LONDON_TZ)
    return f'{dt_london.strftime("%a %d %b %Y, %H:%M")} {dt_london.strftime("%Z")} ({dt_utc.strftime("%H:%M")} UTC)'


def format_screener_sections(screener):
    """
    Returns a list of separate messages (one per section: Volume, Gainers, Losers)
    instead of one combined string. CallMeBot/WhatsApp truncates long messages, and
    all three sections combined easily exceeds that — sending separately means each
    one arrives complete rather than the later sections getting cut off silently.
    """
    def section(title, rows, show_pct=True):
        lines = [f"{title}  🕐 {now_stamp()}"]
        for i, q in enumerate(rows, 1):
            chg = q.get("changePct")
            chg_str = f'{"▲" if (chg or 0) >= 0 else "▼"}{abs(chg or 0):.2f}%' if chg is not None else ""
            vol = q.get("volume")
            vol_str = f"{vol:,}" if isinstance(vol, (int, float)) else "?"
            bit = f"vol {vol_str}" if not show_pct else chg_str
            name = q.get("name") or q.get("symbol", "")
            lines.append(f'{i}. {q.get("symbol","")} ({name}) — {bit}')
        return "\n".join(lines)

    messages = []
    if screener.get("volume"):
        messages.append(section("📊 TOP 10 BY VOLUME (LSE):", screener["volume"], show_pct=False))
    if screener.get("gainers"):
        messages.append(section("📈 TOP 10 GAINERS (LSE):", screener["gainers"]))
    else:
        messages.append(f"📈 TOP 10 GAINERS (LSE):  🕐 {now_stamp()}\nNo liquid (500k+ vol, 20p+) gainers found this run.")
    if screener.get("losers"):
        messages.append(section("📉 TOP 10 LOSERS (LSE):", screener["losers"]))
    else:
        messages.append(f"📉 TOP 10 LOSERS (LSE):  🕐 {now_stamp()}\nNo liquid (500k+ vol, 20p+) losers found this run.")
    return messages


_RATE_LIMIT_WINDOW_SECONDS = 240 * 60  # matches CallMeBot's free-tier window
_RATE_LIMIT_MAX_SENDS = 14  # stay under CallMeBot's 16/240min cap with a safety margin
_recent_send_times = []  # populated from seen_state at the start of each run, see main()


def _current_template():
    return (os.environ.get("WEBHOOK_TEMPLATE", "").strip() or "callmebot")


def _rate_limit_ok():
    """Global send budget shared across every message type this run (and recent prior
    runs) — the per-category throttles above help, but this is the actual backstop that
    guarantees the WhatsApp free-tier cap is never exceeded regardless of which
    combination of screener/alert/heartbeat messages happens to fire in a given hour.
    Only applies to CallMeBot specifically — ntfy and generic webhooks don't share that
    limit, so they're not artificially held back by a cap that doesn't apply to them."""
    if _current_template() != "callmebot":
        return True
    now = time.time()
    while _recent_send_times and now - _recent_send_times[0] > _RATE_LIMIT_WINDOW_SECONDS:
        _recent_send_times.pop(0)
    return len(_recent_send_times) < _RATE_LIMIT_MAX_SENDS


def send_webhook(message):
    if not _rate_limit_ok():
        print(f"  ! send skipped: WhatsApp rate-limit budget exhausted ({_RATE_LIMIT_MAX_SENDS}/{_RATE_LIMIT_WINDOW_SECONDS//60}min) — still on dashboard.", file=sys.stderr)
        return
    webhook_url = os.environ.get("WEBHOOK_URL", "").strip()
    # GitHub passes a missing secret through as an EMPTY STRING env var, not an absent
    # one — so os.environ.get(..., "callmebot") never actually triggers its default in
    # that case. Explicitly fall back after stripping, regardless of why it's empty.
    template = (os.environ.get("WEBHOOK_TEMPLATE", "").strip() or "callmebot")
    if not webhook_url:
        print("  ! WEBHOOK_URL is empty — check the secret exists and is named exactly WEBHOOK_URL", file=sys.stderr)
        return
    print(f"  > sending webhook via template='{template}'")
    _recent_send_times.append(time.time())  # count the attempt itself, regardless of outcome —
                                              # matches CallMeBot's own "message queued" behavior
    try:
        if template == "callmebot":
            sep = "&" if "?" in webhook_url else "?"
            url = f"{webhook_url}{sep}text={urllib.parse.quote(message)}"
            resp = urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS), timeout=15)
            body = resp.read().decode("utf-8", errors="replace")
            print(f"  > callmebot response ({resp.status}): {body[:200]}")
        elif template == "ntfy":
            req = urllib.request.Request(webhook_url, data=message.encode("utf-8"), method="POST", headers=HEADERS)
            resp = urllib.request.urlopen(req, timeout=15)
            print(f"  > ntfy response: {resp.status}")
        else:
            body = json.dumps({"text": message, "message": message}).encode("utf-8")
            req = urllib.request.Request(
                webhook_url, data=body, method="POST",
                headers={**HEADERS, "Content-Type": "application/json"},
            )
            resp = urllib.request.urlopen(req, timeout=15)
            print(f"  > generic webhook response: {resp.status}")
    except Exception as e:
        print(f"  ! webhook send failed: {e}", file=sys.stderr)


def load_json(path, default):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def format_market_cap(value):
    """Readable market cap — e.g. 1_234_000_000 -> '£1.23bn'. Yahoo returns this in the
    stock's own currency (GBp for most LSE stocks, so this is already pence-scale — but
    marketCap itself is typically reported in full currency units, not pence, by Yahoo)."""
    if value is None:
        return None
    if value >= 1_000_000_000:
        return f"£{value / 1_000_000_000:.2f}bn"
    if value >= 1_000_000:
        return f"£{value / 1_000_000:.1f}m"
    return f"£{value:,.0f}"


def scale_bar_html(label, value_display, position_pct, lo_label, hi_label, color_lo, color_hi, zone_lo=33, zone_hi=66):
    """
    A compact positional scale bar: shows WHERE a number sits on a labelled range,
    never whether that's "good" or "bad" — no buy/sell framing, no green-means-go
    styling. The marker's position is the only signal; colour is purely to distinguish
    the two ends of the scale (e.g. small-cap vs large-cap), not a verdict.
    position_pct: 0-100, already clamped by the caller.
    zone_lo/zone_hi: where the colour bands split (default even thirds, middle band neutral grey).
    """
    pos = max(0, min(100, position_pct))
    return (
        f'<div style="margin:6px 0 3px;max-width:320px;">'
        f'<div style="display:flex;justify-content:space-between;font-size:14px;color:#9aa0a6;margin-bottom:3px;">'
        f'<span>{esc_safe(label)}</span><span style="color:#e8eaed;font-weight:700;">{esc_safe(value_display)}</span></div>'
        f'<div style="position:relative;height:7px;border-radius:4px;'
        f'background:linear-gradient(to right, {color_lo} 0%, {color_lo} {zone_lo}%, '
        f'#2a2e37 {zone_lo}%, #2a2e37 {zone_hi}%, {color_hi} {zone_hi}%, {color_hi} 100%);">'
        f'<div style="position:absolute;left:{pos:.0f}%;top:-3px;width:3px;height:13px;'
        f'background:#e8eaed;border-radius:1px;"></div></div>'
        f'<div style="display:flex;justify-content:space-between;font-size:12px;color:#6b7078;margin-top:2px;">'
        f'<span>{esc_safe(lo_label)}</span><span>{esc_safe(hi_label)}</span></div></div>'
    )


def esc_safe(s):
    """Small standalone escaper — scale_bar_html is called from inside render_dashboard's
    nested functions where the main esc() closure isn't in scope, so this avoids relying
    on closure capture across function boundaries."""
    return (str(s) if s is not None else "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def pe_scale(pe):
    """P/E scale: 0-30 range, no 'good/bad' framing — a P/E can be low because a stock
    is cheap OR because something's wrong; can be high because of growth optimism OR
    overpricing. The bar shows POSITION only, never a verdict."""
    if pe is None or pe < 0:
        return ""
    pos = min(pe / 30 * 100, 100)
    return scale_bar_html("P/E", f"{pe:.1f}", pos, "low", "high", "#7fb3ff", "#e0d267")


def rsi_scale(rsi):
    """RSI is already 0-100 natively — no transform needed. Labels describe the fact
    (risen/fallen fast) never a recommendation (never 'overbought'/'oversold')."""
    if rsi is None:
        return ""
    return scale_bar_html("RSI (14)", f"{rsi:.1f}", rsi, "0 (fallen fast)", "100 (risen fast)", "#7fb3ff", "#e0d267")


def mktcap_scale(mcap_value):
    """Log-scale position: £1m -> 0%, £10bn -> 100%. Purely a size classification
    (micro/mid/large-cap), not a quality judgement — small isn't 'bad', it's smaller."""
    if not mcap_value or mcap_value <= 0:
        return ""
    import math
    log_val = math.log10(max(mcap_value, 1))
    pos = (log_val - 6) / (10 - 6) * 100  # 10^6 (£1m) to 10^10 (£10bn)
    pos = max(0, min(100, pos))
    display = format_market_cap(mcap_value) or ""
    return scale_bar_html("Mkt cap", display, pos, "micro-cap", "large-cap", "#f0997b", "#5dcaa5")


def eps_scale(eps):
    """EPS scale: -£0.50 to £2.00 — a company can be a legitimate early-stage business
    with negative EPS, so 'loss-making' is a factual label, not a red flag icon."""
    if eps is None:
        return ""
    pos = (eps - (-0.5)) / (2.0 - (-0.5)) * 100
    pos = max(0, min(100, pos))
    return scale_bar_html("EPS", f"£{eps:.2f}", pos, "loss-making", "strong profit/share", "#f0997b", "#5dcaa5")


def format_epoch_date(epoch_seconds):
    """Formats a Yahoo epoch timestamp (earnings/ex-dividend dates) as a plain London
    date. IMPORTANT: only returns a value if the date is today or in the future — Yahoo
    sometimes returns a stale PAST date for "next earnings"/"ex-dividend" when it has no
    genuine upcoming one (confirmed against real data: a "next earnings" date that had
    already passed, an "ex-dividend" date from 1996). Showing a past date labeled "next"
    would be actively misleading, so it's treated the same as missing data — omitted."""
    if not epoch_seconds:
        return None
    try:
        dt = datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).astimezone(LONDON_TZ)
        if dt.date() < datetime.now(timezone.utc).astimezone(LONDON_TZ).date():
            return None  # stale/past date — not genuinely "next" anything
        return dt.strftime("%d %b %Y")
    except Exception:
        return None


def render_dashboard(data, watchlist):
    items_by_ticker = data.get("items", {})
    quotes = data.get("quotes", {})
    screener = data.get("screener", {})
    ftse100 = data.get("ftse100")
    screener_news = data.get("screenerNews", {})
    uptrend_stocks = data.get("uptrendStocks", [])
    big_movers = data.get("bigMovers", [])
    market_wide = data.get("marketWide", [])
    market_research = data.get("marketResearch", {})
    last_poll_raw = data.get("lastPoll")
    if last_poll_raw:
        try:
            last_poll = format_london_and_utc(datetime.strptime(last_poll_raw, "%Y-%m-%d %H:%M:%S"))
        except Exception:
            last_poll = last_poll_raw  # fall back to raw string if format ever changes
    else:
        last_poll = "never"
    all_items = []
    for ticker, its in items_by_ticker.items():
        all_items.extend(its)
    all_items.sort(key=item_sort_key, reverse=True)

    def esc(s):
        return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    ftse_html = ""
    if ftse100:
        chg = ftse100.get("changePct") or 0
        cls = "up" if chg >= 0 else "down"
        arrow = "▲" if chg >= 0 else "▼"
        ftse_html = (
            f'<p style="font-size:14px;margin:0 0 10px;">FTSE 100: '
            f'<span class="{cls}">{ftse100.get("price",""):,.2f} {arrow}{abs(chg):.2f}%</span></p>'
        )

    def quote_div(t, q):
        chg_cls = "up" if (q.get("change") or 0) >= 0 else "down"
        chg_arrow = "▲" if (q.get("change") or 0) >= 0 else "▼"
        target = q.get("targetMeanPrice")
        rec = q.get("recommendationKey")
        extra = ""
        if target or rec:
            extra = f'<div class="meta">consensus: {esc(rec or "?")} · target {target if target else "?"}</div>'
        return (
            f'<div class="q"><b>{esc(t)}</b> '
            f'<span class="{chg_cls}">{q.get("price", "?")}{"p" if q.get("currency") == "GBp" else ""} '
            f'{chg_arrow}{abs(q.get("changePct") or 0):.2f}%</span>{extra}</div>'
        )

    quote_rows = "".join(quote_div(t, q) for t, q in quotes.items())

    def item_div(it):
        # Defense in depth: only render a broker badge for genuine rating/target items,
        # even if something upstream ever slipped through — a "news" item mentioning a
        # bank's name is not the same as that bank issuing a rating.
        show_broker = it.get("broker") and it.get("category") in ("upgrade", "downgrade", "target", "target_raise", "target_cut", "initiation", "reiteration")
        broker_html = f'<span class="broker">{esc(it["broker"])}</span>' if show_broker else ""
        ticker_label = "LSE" if it.get("ticker") == "MARKET" else esc(it.get("ticker", ""))
        # Shows WHEN this item's classification was computed — separate from pubDate
        # (the article/event's own date), since a backfilled Yahoo history item can
        # be re-classified on a later run than when the actual rating happened.
        normalized_at_display = format_normalized_at(it.get("normalizedAt"))
        classified_html = f'<br/><span class="meta">Classified: {esc(normalized_at_display)}</span>' if normalized_at_display else ""
        return (
            f'<div class="item"><span class="badge {it.get("category","news")}">{it.get("category","news").upper()}</span> '
            f'<b>{ticker_label}</b> '
            f'{broker_html} '
            f'<span class="meta">{esc(it.get("source",""))} · {esc(it.get("pubDate",""))}</span>{classified_html}<br/>'
            f'<a href="{esc(it.get("link","#"))}" target="_blank">{esc(it.get("title",""))}</a></div>'
        )

    item_rows = "".join(item_div(it) for it in all_items[:150])
    market_wide_rows = "".join(item_div(it) for it in market_wide[:60])

    def screener_table(rows, show_pct=True):
        def row(i, q):
            vol = q.get("volume")
            vol_str = f"{vol:,}" if isinstance(vol, (int, float)) else "?"
            chg = q.get("changePct") or 0
            chg_cls = "up" if chg >= 0 else "down"
            chg_str = f'{"▲" if chg >= 0 else "▼"}{abs(chg):.2f}%'
            last_col = chg_str if show_pct else vol_str
            last_cls = f' class="{chg_cls}"' if show_pct else ""
            symbol = q.get("symbol", "")
            name = esc(q.get("name") or symbol)
            # Inline news link — the same lookup already built for the "News on Today's
            # Top Movers" section, shown right where you'd actually look for it: next to
            # the stock itself, not just in a separate section further down the page.
            news_for_symbol = screener_news.get(symbol) or []
            if news_for_symbol:
                top = news_for_symbol[0]
                extra = f" (+{len(news_for_symbol)-1} more)" if len(news_for_symbol) > 1 else ""
                news_html = (
                    f'<br/><a href="{esc(top.get("link","#"))}" target="_blank" '
                    f'style="color:#7fb3ff;font-size:12px;font-weight:600;">📰 {esc(top.get("title",""))}</a>'
                    f'<span class="meta">{extra}</span>'
                )
            else:
                news_html = ""
            target = q.get("targetMeanPrice")
            rec = q.get("recommendationKey")
            target_html = (
                f'<br/><span class="meta">🎯 target <span class="val">{target:.2f}</span>{f" · {esc(rec)}" if rec else ""}</span>'
                if target else ""
            )
            earnings_date = format_epoch_date(q.get("nextEarningsDate"))
            ex_div_date = format_epoch_date(q.get("exDividendDate"))
            div_rate = q.get("dividendRate")
            div_yield = q.get("dividendYieldPct")
            calendar_html = ""
            if earnings_date:
                calendar_html += f'<br/><span class="meta">📅 next earnings: <span class="val">{earnings_date}</span></span>'
            if ex_div_date:
                calendar_html += f'<br/><span class="meta">💰 ex-dividend: <span class="val">{ex_div_date}</span></span>'
            if div_rate is not None or div_yield is not None:
                rate_str = f'<span class="val">{div_rate:.2f}</span>/share' if div_rate is not None else ''
                yield_str = f'<span class="val">{div_yield:.2f}%</span> yield' if div_yield is not None else ''
                joined = " · ".join(s for s in (rate_str, yield_str) if s)
                calendar_html += f'<br/><span class="meta">💷 dividend: {joined}</span>'
            pe = q.get("trailingPE")
            eps = q.get("trailingEps")
            mcap = format_market_cap(q.get("marketCap"))
            fundamentals_parts = []
            if pe is not None:
                fundamentals_parts.append(f'P/E <span class="val">{pe:.1f}</span>')
            if eps is not None:
                fundamentals_parts.append(f'EPS <span class="val">{eps:.2f}</span>')
            if mcap:
                fundamentals_parts.append(f'mkt cap <span class="val">{mcap}</span>')
            if fundamentals_parts:
                calendar_html += f'<br/><span class="meta">📊 {" · ".join(fundamentals_parts)}</span>'
            scales_html = pe_scale(pe) + mktcap_scale(q.get("marketCap")) + eps_scale(eps)
            if scales_html:
                calendar_html += f'<div style="margin-top:4px;">{scales_html}</div>'
            wk_low = q.get("fiftyTwoWeekLow")
            wk_high = q.get("fiftyTwoWeekHigh")
            if wk_low is not None and wk_high is not None:
                calendar_html += f'<br/><span class="meta">📏 52-wk range: <span class="val">{wk_low:.2f}</span> – <span class="val">{wk_high:.2f}</span></span>'
            insiders = q.get("heldPercentInsidersPct")
            if insiders is not None:
                calendar_html += f'<br/><span class="meta">🧑‍💼 insider ownership: <span class="val">{insiders:.1f}%</span></span>'
            short_pct = q.get("shortInterestPct")
            if short_pct is not None:
                calendar_html += f'<br/><span class="meta">📉 short interest: <span class="val">{short_pct:.2f}%</span> (FCA)</span>'
            sector = q.get("sector")
            industry = q.get("industry")
            if sector or industry:
                sector_bit = " · ".join(s for s in (sector, industry) if s)
                calendar_html += f'<br/><span class="meta">🏷️ <span class="val">{esc(sector_bit)}</span></span>'
            biz_summary = q.get("businessSummary")
            if biz_summary:
                calendar_html += f'<br/><span class="meta" style="display:block;max-width:520px;">ℹ️ {esc(biz_summary)}</span>'
            rsi14 = q.get("rsi14")
            above_ma = q.get("aboveMA20")
            technicals_html = ""
            if rsi14 is not None:
                technicals_html += f'<br/><span class="meta">RSI(14): <span class="val">{rsi14:.1f}</span></span>'
                technicals_html += f'<div style="margin-top:4px;">{rsi_scale(rsi14)}</div>'
            if above_ma is not None:
                technicals_html += f'<br/><span class="meta">Price is <span class="val">{"above" if above_ma else "below"}</span> its 20-day average</span>'
            return (
                f'<tr><td>{i+1}</td><td><b style="font-size:14px;">{esc(symbol)}</b><br/>'
                f'<span class="meta">{name}</span>{news_html}{target_html}{calendar_html}{technicals_html}</td><td{last_cls}>{last_col}</td></tr>'
            )
        return "".join(row(i, q) for i, q in enumerate(rows)) or '<tr><td colspan="3" class="meta">No data yet</td></tr>'

    vol_rows = screener_table(screener.get("volume", []), show_pct=False)
    gain_rows = screener_table(screener.get("gainers", []))
    lose_rows = screener_table(screener.get("losers", []))

    def screener_news_item(symbol, it):
        broker_html = f'<span class="broker">{esc(it["broker"])}</span>' if it.get("broker") and it.get("category") in ("upgrade", "downgrade", "target", "target_raise", "target_cut", "initiation", "reiteration") else ""
        normalized_at_display = format_normalized_at(it.get("normalizedAt"))
        classified_html = f'<br/><span class="meta">Classified: {esc(normalized_at_display)}</span>' if normalized_at_display else ""
        return (
            f'<div class="item"><span class="badge {it.get("category","news")}">{it.get("category","news").upper()}</span> '
            f'<b>{esc(symbol)}</b> {broker_html} '
            f'<span class="meta">{esc(it.get("source",""))} · {esc(it.get("pubDate",""))}</span>{classified_html}<br/>'
            f'<a href="{esc(it.get("link","#"))}" target="_blank">{esc(it.get("title",""))}</a></div>'
        )

    screener_news_rows = "".join(
        screener_news_item(symbol, it)
        for symbol, items in screener_news.items()
        for it in items
    )

    uptrend_rows = "".join(
        f'<div class="quote-row"><b>{esc(s["symbol"])}</b> ({esc(s["name"])}) — '
        f'<span style="color:#2fbf71">▲{s["changePct5d"]:.1f}%</span> over 5 sessions</div>'
        for s in sorted(uptrend_stocks, key=lambda x: -x["changePct5d"])
    )

    # Every screener-ranked stock (Volume/Gainers/Losers) that has a real broker target
    # price attached — pulled together here as its own scannable list, in addition to
    # already showing inline under each screener row. ALSO includes watchlist stocks'
    # targets (via `quotes`), so this is genuinely every target the tool has, in one
    # place, not just the screener subset — deduped so a stock in both pools (e.g. KOO)
    # only appears once.
    all_screener_rows = (
        screener.get("volume", []) + screener.get("gainers", []) + screener.get("losers", [])
    )
    seen_target_symbols = set()
    target_price_rows = ""
    for q in all_screener_rows:
        symbol = q.get("symbol", "")
        target = q.get("targetMeanPrice")
        if not target or symbol in seen_target_symbols:
            continue
        seen_target_symbols.add(symbol)
        rec = q.get("recommendationKey")
        rec_html = f' · <span class="meta">{esc(rec)}</span>' if rec else ""
        target_price_rows += (
            f'<div class="quote-row"><b>{esc(symbol)}</b> ({esc(q.get("name") or symbol)}) — '
            f'🎯 target {target:.2f}{rec_html}</div>'
        )
    for stock in watchlist:
        ticker, name = stock["ticker"], stock["name"]
        yahoo_sym = yahoo_symbol(ticker)  # watchlist tickers are stored plain (e.g. "LLOY"),
                                           # screener symbols are Yahoo-format (e.g. "LLOY.L") —
                                           # normalize so the seen_target_symbols dedupe actually matches
        if yahoo_sym in seen_target_symbols:
            continue
        q = quotes.get(ticker, {})
        target = q.get("targetMeanPrice")
        if not target:
            continue
        seen_target_symbols.add(yahoo_sym)
        rec = q.get("recommendationKey")
        rec_html = f' · <span class="meta">{esc(rec)}</span>' if rec else ""
        target_price_rows += (
            f'<div class="quote-row"><b>{esc(ticker)}</b> ({esc(name)}) — '
            f'🎯 target {target:.2f}{rec_html}</div>'
        )

    def heatmap_cell(q):
        chg = q.get("changePct") or 0
        symbol = esc(q.get("symbol", ""))
        name = esc(q.get("name") or symbol)
        # Colour by direction, intensity (lightness) by magnitude of the move —
        # bigger swings render darker/more saturated, capped at a 10% move.
        magnitude = min(abs(chg), 10) / 10
        lightness = 55 - (magnitude * 30)  # 55% (small move) down to 25% (big move)
        hue = 142 if chg >= 0 else 4  # green / red
        bg = f"hsl({hue}, 55%, {lightness:.0f}%)"
        return (
            f'<div class="heat-cell" style="background:{bg}" title="{name}">'
            f'<div class="heat-symbol">{symbol}</div>'
            f'<div class="heat-pct">{"▲" if chg >= 0 else "▼"}{abs(chg):.1f}%</div>'
            f"</div>"
        )

    heatmap_pool = (screener.get("gainers", []) + screener.get("losers", []))
    heatmap_pool.sort(key=lambda q: abs(q.get("changePct") or 0), reverse=True)
    heatmap_cells = "".join(heatmap_cell(q) for q in heatmap_pool[:20])

    def research_card(stock):
        ticker, name = stock["ticker"], stock["name"]
        entry = market_research.get(ticker)

        # Free, always-on facts layer — same data already gathered for News Feed/
        # Watchlist, just pulled together per-stock. No API key needed for this part.
        quote = quotes.get(ticker, {})
        facts_parts = []
        target = quote.get("targetMeanPrice")
        rec = quote.get("recommendationKey")
        if target:
            facts_parts.append(f'🎯 Broker consensus target: <span class="val">{target}</span>' + (f' · {esc(rec)}' if rec else ''))
        recent_items = (items_by_ticker.get(ticker) or [])[:5]
        headlines_html = ""
        if recent_items:
            headlines_html = '<ul style="margin:6px 0 0;padding-left:18px;font-size:12px;line-height:1.7;">' + "".join(
                f'<li><span class="meta">{esc(it.get("pubDate",""))} — {esc(it.get("source",""))}</span><br/>'
                f'<a href="{esc(it.get("link","#"))}" target="_blank" style="color:#e8eaed;">{esc(it.get("title",""))}</a></li>'
                for it in recent_items
            ) + '</ul>'
        facts_html = ""
        if facts_parts:
            facts_html += f'<p class="meta" style="margin:6px 0 0;">{" · ".join(facts_parts)}</p>'
        facts_html += headlines_html
        if not facts_html:
            facts_html = '<p class="meta" style="margin:6px 0 0;">No recent news/broker data gathered for this stock yet.</p>'

        # Optional AI-written paragraph on top, only if a key is configured and this
        # stock's write-up has actually been generated.
        ai_html = ""
        if entry and entry.get("text"):
            ai_html = (
                f'<p style="margin:10px 0 0;font-size:13px;line-height:1.6;border-top:1px solid #2a2e37;padding-top:8px;">'
                f'🤖 {esc(entry["text"])}</p>'
                f'<span class="meta">AI summary generated {esc(entry.get("generatedAt","?"))} UTC from the facts above — not fresh web research, not a recommendation.</span>'
            )

        return (
            f'<div class="item"><b>{esc(ticker)}</b> <span class="meta">({esc(name)})</span>'
            f'{facts_html}{ai_html}</div>'
        )

    research_rows = "".join(research_card(s) for s in watchlist)

    mover_rows = "".join(
        f'<div class="q"><b>{esc(m["ticker"])}</b> '
        f'<span class="{"up" if (m.get("changePct") or 0) >= 0 else "down"}">'
        f'{"▲" if (m.get("changePct") or 0) >= 0 else "▼"}{abs(m.get("changePct") or 0):.2f}%</span></div>'
        for m in big_movers
    )

    html = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
<meta http-equiv="refresh" content="90">
<title>UK Stock Watch</title>
<style>
body{{background:#0f1115;color:#e8eaed;font-family:-apple-system,sans-serif;margin:0;padding:12px;font-size:17px;line-height:1.6}}
h1{{font-size:26px;margin:4px 0;font-weight:800}}
h2{{font-size:21px;margin:26px 0 10px;font-weight:800;border-left:4px solid #7fb3ff;padding-left:10px}}
h3{{font-size:16px;margin:0 0 8px;color:#c2c7d0;font-weight:700}}
.screener-grid{{display:grid;grid-template-columns:1fr;gap:10px}}
@media(min-width:600px){{.screener-grid{{grid-template-columns:1fr 1fr 1fr}}}}
.heatmap-grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:4px;margin-bottom:16px}}
@media(min-width:600px){{.heatmap-grid{{grid-template-columns:repeat(8,1fr)}}}}
.heat-cell{{border-radius:4px;padding:8px 4px;text-align:center;color:#fff}}
.heat-symbol{{font-size:14px;font-weight:700}}
.heat-pct{{font-size:13px;opacity:0.9}}
.disclaimer{{background:#1c2b25;border:1px solid #274235;color:#9aa0a6;border-radius:6px;padding:10px;font-size:14px;margin-bottom:10px}}
.quotes{{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:14px}}
.q{{background:#171a21;border:1px solid #2a2e37;border-radius:6px;padding:8px 12px;font-size:15px}}
.up{{color:#50dc96;font-weight:800;font-size:17px}} .down{{color:#ff6b6b;font-weight:800;font-size:17px}}
table{{width:100%;border-collapse:collapse;margin-bottom:16px;font-size:16px}}
table td, table th{{padding:9px 10px;border-bottom:1px solid #2a2e37;text-align:left}}
.item{{background:#171a21;border:1px solid #2a2e37;border-radius:8px;padding:12px;margin-bottom:8px}}
.item a{{color:#e8eaed;text-decoration:none;font-size:17px;font-weight:600}}
.item a:hover{{text-decoration:underline}}
.meta{{color:#9aa0a6;font-size:15px;line-height:2.0}}
.val{{color:#e8eaed;font-weight:700}}
.badge{{border-radius:4px;padding:2px 8px;font-size:12px;font-weight:700;margin-right:4px}}
.badge.upgrade{{background:#163a2a;color:#50dc96}}
.badge.downgrade{{background:#3a1919;color:#ff6b6b}}
.badge.target{{background:#2a2a17;color:#e0d267}}
.badge.target_raise{{background:#1c3a1c;color:#7bd97b}}
.badge.target_cut{{background:#3a2317;color:#e0977f}}
.badge.initiation{{background:#1f2a3a;color:#8fb8ff}}
.badge.reiteration{{background:#2a2532;color:#b8a0d9}}
.badge.director_dealing{{background:#1a2a3a;color:#7fb3ff}}
.badge.event{{background:#1c2a3a;color:#6ab6ff}}
.badge.news{{background:#22262f;color:#9aa0a6}}
.broker{{background:#2a1c3a;color:#c69bf0;border-radius:4px;padding:2px 8px;font-size:12px;font-weight:700;margin-right:4px}}
.lastpoll{{color:#9aa0a6;font-size:11px;text-align:right}}
</style></head>
<body>
<h1>UK Stock Watch — Live Feed</h1>
<p class="lastpoll" style="text-align:left;font-size:13px;color:#50dc96;margin:0 0 10px;">🕐 Live as of {esc(str(last_poll))} — this page auto-refreshes every 90s (data itself updates every ~5 min)</p>
{ftse_html}
<p class="disclaimer">LSE-listed stocks only. Informational only — not investment advice, not a guarantee of any outcome.</p>

<nav style="margin:14px 0;padding:12px;background:#161920;border-radius:6px;font-size:16px;line-height:2.4;">
<b style="color:#9aa0a6;margin-right:8px;">Jump to:</b>
<a href="#heatmap" style="color:#7fb3ff;margin-right:14px;text-decoration:none;">🗺️ Heat Map</a>
<a href="#screener" style="color:#7fb3ff;margin-right:14px;text-decoration:none;">📊 Screener</a>
<a href="#mover-news" style="color:#7fb3ff;margin-right:14px;text-decoration:none;">📰 Mover News</a>
<a href="#uptrend" style="color:#7fb3ff;margin-right:14px;text-decoration:none;">📈 5-Day Uptrend</a>
<a href="#targets" style="color:#7fb3ff;margin-right:14px;text-decoration:none;">🎯 Target Prices</a>
<a href="#movers-today" style="color:#7fb3ff;margin-right:14px;text-decoration:none;">🔥 Moving Today</a>
<a href="#watchlist" style="color:#7fb3ff;margin-right:14px;text-decoration:none;">👀 Watchlist</a>
<a href="#market-research" style="color:#7fb3ff;margin-right:14px;text-decoration:none;">🔎 Market Research</a>
<a href="#broker-alerts" style="color:#7fb3ff;margin-right:14px;text-decoration:none;">⬆⬇🎯 Broker Alerts</a>
<a href="#news-feed" style="color:#7fb3ff;text-decoration:none;">📰 News Feed</a>
</nav>

<h2 id="heatmap">🗺️ Heat Map (top movers, by size of move)</h2>
<div class="heatmap-grid">{heatmap_cells or '<span class="meta">No data yet</span>'}</div>

<h2 id="screener">📊 LSE Screener (Volume / Gainers / Losers)</h2>
<div class="screener-grid">
  <div><h3>Top Volume</h3><table><tr><th>#</th><th>Symbol</th><th>Volume</th></tr>{vol_rows}</table></div>
  <div><h3>Top Gainers</h3><table><tr><th>#</th><th>Symbol</th><th>Chg%</th></tr>{gain_rows}</table></div>
  <div><h3>Top Losers</h3><table><tr><th>#</th><th>Symbol</th><th>Chg%</th></tr>{lose_rows}</table></div>
</div>

<h2 id="mover-news">📰 News on Today's Top Movers</h2>
<p class="meta">Real, dated-today news for any stock currently in Volume/Gainers/Losers above — not limited to your watchlist.</p>
<div>{screener_news_rows or '<span class="meta">No same-day news found for today&#39;s ranked stocks yet.</span>'}</div>

<h2 id="uptrend">📈 5-Day Uptrend ({UPTREND_5DAY_THRESHOLD_PCT:.0f}%+, screener + watchlist)</h2>
<p class="meta">Real closing-price history over the last 5 trading days — a fact about the past, not a forecast of what happens next.</p>
<div class="quotes">{uptrend_rows or '<span class="meta">Nothing has met the 5-day threshold right now</span>'}</div>

<h2 id="targets">🎯 Broker Target Prices</h2>
<p class="meta">Real, already-published broker consensus targets from Yahoo's aggregation — not generated by this tool. Covers both your watchlist and today's screener-ranked stocks (Volume/Gainers/Losers).</p>
<div class="quotes">{target_price_rows or '<span class="meta">No target price data available yet.</span>'}</div>

<h2 id="movers-today">🔥 Already Moving Today (watchlist, ±{BIG_MOVER_THRESHOLD_PCT:.0f}%+)</h2>
<p class="meta">A fact about what already happened today — not a forecast of what happens next.</p>
<div class="quotes">{mover_rows or '<span class="meta">Nothing past the threshold right now</span>'}</div>

<h2 id="watchlist">👀 Your Watchlist</h2>
<div class="quotes">{quote_rows or '<span class="meta">No quotes yet</span>'}</div>

<h2 id="market-research">🔎 Market Research</h2>
<p class="meta">Real broker targets, recent news, and consensus ratings, already gathered by this tool — free, always live, no AI or API cost involved. If an ANTHROPIC_API_KEY is configured, a short AI-written summary appears too (🤖), synthesised only from these same facts — never fresh web research, never a recommendation. Always cross-check anything here against primary sources before acting on it.</p>
{research_rows or '<p class="meta">No watchlist stocks to show yet.</p>'}

<h2 id="broker-alerts">⬆⬇🎯 Market-wide Broker Alerts (all LSE, not just watchlist)</h2>
<p class="meta">Upgrades/downgrades from anywhere on the LSE, not limited to your watchlist below.</p>
{market_wide_rows or '<p class="meta">No market-wide alerts yet.</p>'}

<h2 id="news-feed">📰 News &amp; Broker Feed (watchlist)</h2>
{item_rows or '<p class="meta">No items yet — first run may still be in progress.</p>'}
<p class="lastpoll">Last checked: {esc(str(last_poll))}</p>
</body></html>"""
    os.makedirs(DOCS_DIR, exist_ok=True)
    with open(os.path.join(DOCS_DIR, DOCS_FILENAME), "w", encoding="utf-8") as f:
        f.write(html)


HEARTBEAT_INTERVAL_SECONDS = 6 * 3600  # confirmation ping every 6h, not every 5 min — a signal, not spam


AI_DIGEST_INTERVAL_SECONDS = 6 * 3600  # throttled like the heartbeat — a paid API call, not free

# CallMeBot's free WhatsApp tier allows ~16 messages per 240 minutes (~4/hour sustained).
# Sending the screener report every 5-minute cycle would blow through that in half an hour
# regardless of anything else — throttle the WhatsApp SEND specifically (the dashboard/
# data itself still refreshes every cycle; only the push notification is rationed).
SCREENER_MESSAGE_INTERVAL_SECONDS = 60 * 60  # once per hour
MAX_ALERTS_PER_RUN = 5  # cap a burst of alerts in one run; rest are still on the dashboard
# A stock is flagged as "getting attention today" purely on mention COUNT — how many
# already-published, real items exist about it today. This is not a signal about where
# the price might go; it's a fact about today's coverage volume, same category as the
# screener's volume ranking.
ATTENTION_MENTION_THRESHOLD = 3


def format_uptrend_message(uptrend_stocks):
    """Purely descriptive: stocks that have genuinely risen UPTREND_5DAY_THRESHOLD_PCT%+
    over the last 5 real trading days, computed from actual closing prices. This is a
    fact about the past — explicitly not a prediction the rise continues."""
    if not uptrend_stocks:
        return None
    flagged = sorted(uptrend_stocks, key=lambda x: -x["changePct5d"])
    lines = [
        f"📈 5-DAY UPTREND ({UPTREND_5DAY_THRESHOLD_PCT:.0f}%+, screener + watchlist)  🕐 {now_stamp()}",
        "Real closing-price history over the last 5 trading days — not a forecast of what happens next.",
    ]
    for s in flagged[:15]:
        lines.append(f'{s["symbol"]} ({s["name"]}) — ▲{s["changePct5d"]:.1f}% over 5 sessions')
    return "\n".join(lines)


def format_screener_news_message(screener_news):
    """Real, dated-today news for stocks currently ranked in Volume/Gainers/Losers —
    not limited to the watchlist. Capped per message to stay a reasonable length."""
    lines = []
    for symbol, items in screener_news.items():
        for it in items[:2]:  # at most 2 headlines per stock in the message (full list is on the dashboard)
            lines.append(f'{symbol}: {it["title"]}')
    if not lines:
        return None
    header = f"📰 NEWS ON TODAY'S TOP MOVERS  🕐 {now_stamp()}"
    return header + "\n" + "\n".join(lines[:15])  # overall cap so one message can't run away


def format_attention_message(mention_counts):
    """Purely descriptive: which watchlist stocks have unusually high real news/broker
    mention counts today. Never says why that might matter for price — just the count."""
    flagged = sorted(
        ((t, d) for t, d in mention_counts.items() if d["count"] >= ATTENTION_MENTION_THRESHOLD),
        key=lambda x: -x[1]["count"],
    )
    if not flagged:
        return None
    lines = [f"📢 GETTING ATTENTION TODAY (watchlist, {ATTENTION_MENTION_THRESHOLD}+ mentions)  🕐 {now_stamp()}"]
    lines.append("A count of today's coverage — not a signal about where price might go.")
    for ticker, d in flagged:
        lines.append(f"{ticker} ({d['name']}) — {d['count']} mentions today")
    return "\n".join(lines)
AI_DIGEST_MODEL = "claude-haiku-4-5-20251001"  # cheap/fast model, appropriate for a short summary

AI_DIGEST_SYSTEM_PROMPT = """You are a strictly factual summarizer for a UK stock market news digest sent to one person's WhatsApp.

CRITICAL RULES — violating any of these makes your output unusable:
- NEVER recommend buying, selling, or holding any stock, in any form or phrasing.
- NEVER predict future price movements or say what "might" or "could" happen next.
- NEVER use directive/advisory language: "should", "consider", "opportunity", "worth watching for a play", "good time to", etc.
- ONLY describe what has already happened: which broker rated which stock how, what news broke, what moved and by how much, per the data given to you.
- Attribute every claim to its source (the broker name, or "news reports") — never state something as your own conclusion.
- Do not add analysis, speculation, or connect-the-dots reasoning between separate facts.
- Keep it under 120 words, plain prose, no markdown headers.

If you cannot summarize something factually without it reading like advice, state the raw fact more plainly instead of omitting it or softening it into a suggestion."""

# Safety net beyond the system prompt — if the model drifts into advice-shaped language
# despite the instructions, this blocks the message from ever being sent rather than
# trusting the prompt alone.
FORBIDDEN_DIGEST_PATTERNS = [
    r"\byou should\b", r"\bconsider (buying|selling|holding)\b", r"\brecommend(s|ed|ing)?\b",
    r"\bworth (a )?(buy|look|watch)\b", r"\bgood (time|opportunity)\b", r"\bi (suggest|advise)\b",
    r"\bmight (rise|fall|climb|drop)\b", r"\bcould (rise|fall|climb|drop|see)\b",
    r"\bexpect(ed)? to (rise|fall|climb|drop)\b",
]


def generate_ai_digest(context_text):
    """
    Optional, opt-in, paid: summarizes already-gathered facts into a short WhatsApp
    digest. Returns None (silently, no error) if no API key is configured — the rest
    of the tool works identically either way. Never asked to judge or predict, only to
    restate what was already found; output is scanned before sending as a second layer
    of protection against advice-shaped language slipping through.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return None
    body = json.dumps({
        "model": AI_DIGEST_MODEL,
        "max_tokens": 300,
        "system": AI_DIGEST_SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": f"Today's gathered data:\n{context_text}\n\nWrite the factual summary."}],
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
        method="POST",
    )
    try:
        resp_data = json.loads(urllib.request.urlopen(req, timeout=30).read())
        text = "".join(b.get("text", "") for b in resp_data.get("content", []) if b.get("type") == "text").strip()
        if not text:
            return None
        if any(re.search(pat, text, re.IGNORECASE) for pat in FORBIDDEN_DIGEST_PATTERNS):
            print("  ! AI digest blocked: output matched an advice-shaped pattern, not sent.", file=sys.stderr)
            return None
        return text
    except urllib.error.HTTPError as e:
        try:
            error_body = e.read().decode("utf-8", errors="replace")
        except Exception:
            error_body = "(couldn't read error body)"
        print(f"  ! AI digest generation failed: HTTP {e.code} — {error_body[:300]}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"  ! AI digest generation failed: {e}", file=sys.stderr)
        return None


def build_digest_context(screener, market_wide_enriched, big_movers, watchlist_alerts):
    """Plain-text summary of this run's facts, handed to the AI as its only input —
    it never sees anything beyond what's already been gathered and shown elsewhere."""
    lines = []
    if screener.get("gainers"):
        lines.append("Top gainers: " + ", ".join(f"{q['symbol']} +{q.get('changePct',0):.1f}%" for q in screener["gainers"][:5]))
    if screener.get("losers"):
        lines.append("Top losers: " + ", ".join(f"{q['symbol']} {q.get('changePct',0):.1f}%" for q in screener["losers"][:5]))
    if market_wide_enriched:
        lines.append("Market-wide broker calls: " + "; ".join(
            f'{it.get("broker","?")} {it["category"]} on {it["title"][:80]}' for it in market_wide_enriched[:8]
        ))
    if big_movers:
        lines.append("Watchlist stocks already moving today: " + ", ".join(
            f"{m['ticker']} {m.get('changePct',0):+.1f}%" for m in big_movers
        ))
    if watchlist_alerts:
        lines.append("Watchlist news/broker alerts: " + "; ".join(
            f'{a.get("ticker","")} {a["category"]}: {a["title"][:80]}' for a in watchlist_alerts[:8]
        ))
    return "\n".join(lines) if lines else "No notable items this cycle."


def maybe_send_ai_digest(seen_state, screener, market_wide_enriched, big_movers, watchlist_alerts):
    if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
        return False  # feature is off — no key configured, no cost, no message
    last_digest = seen_state.get("lastAiDigest", 0)
    if time.time() - last_digest < AI_DIGEST_INTERVAL_SECONDS:
        return False
    context = build_digest_context(screener, market_wide_enriched, big_movers, watchlist_alerts)
    digest = generate_ai_digest(context)
    if digest:
        msg = f"🤖 AI DAILY DIGEST (factual summary only, not advice)  🕐 {now_stamp()}\n\n{digest}"
        print(msg)
        send_webhook(msg)
    seen_state["lastAiDigest"] = time.time()
    return True


def maybe_send_heartbeat(seen_state, watchlist, alert_count, mover_count):
    last_heartbeat = seen_state.get("lastHeartbeat", 0)
    now = time.time()
    if now - last_heartbeat < HEARTBEAT_INTERVAL_SECONDS:
        return False
    ts = datetime.now(timezone.utc).strftime("%H:%M UTC")
    msg = (
        f"✅ UK Stock Watch — still checking. {ts}, watching {len(watchlist)} stocks. "
        f"{alert_count} broker alert(s) and {mover_count} big mover(s) since last heartbeat."
    )
    print(msg)
    send_webhook(msg)
    seen_state["lastHeartbeat"] = now
    return True


# =========================================================================
# MARKET RESEARCH — per-stock factual synthesis of ALREADY-GATHERED data
# =========================================================================
# IMPORTANT — what this is and isn't:
# This does NOT do fresh web research (no search capability exists in this
# script). It synthesizes facts poll.py has ALREADY collected this run —
# recent news/broker items, price, target price, consensus label — into a
# short factual write-up per stock. Same safety pattern as the existing AI
# digest: strict factual-only system prompt, output scanned against
# FORBIDDEN_DIGEST_PATTERNS before use, opt-in (requires ANTHROPIC_API_KEY).
#
# "Live" here means: refreshed automatically on a rolling basis, always
# current within ~1 day, WITHOUT refreshing all watchlist stocks every run —
# doing that would mean one paid API call per stock every 5 minutes, which
# doesn't scale to a 40+ stock watchlist. A capped number of the STALEST
# entries get refreshed each run, cycling through the whole list over time.

MARKET_RESEARCH_REFRESH_SECONDS = 24 * 3600  # each stock's write-up refreshes at most once/day
MARKET_RESEARCH_MAX_PER_RUN = 3  # cost/runtime cap — NOT a technical limit, a deliberate budget

MARKET_RESEARCH_SYSTEM_PROMPT = """You are writing a short "Market Research" note for one UK-listed stock, shown on a personal dashboard.

CRITICAL RULES — violating any of these makes your output unusable:
- Base EVERY sentence ONLY on the facts provided below. Do not draw on any outside knowledge of this company beyond what's given — if you know more about it from training, ignore that; use only the supplied facts.
- NEVER recommend buying, selling, or holding, in any form or phrasing.
- NEVER predict future price movements or say what "might" or "could" happen next.
- NEVER use directive/advisory language: "should", "consider", "opportunity", "worth watching", "good time to", etc.
- If broker ratings or targets are in the data, state them as attributed facts ("Broker X rates it Y with a target of Z") — never as your own conclusion or endorsement.
- If the provided facts are thin or contradictory, say so plainly rather than filling gaps with speculation.
- Do not explain what the "strong_buy"/"buy"/"hold" label conceptually means — that reasoning isn't in the data you're given, so don't invent it.
- Keep it under 100 words, plain prose, no markdown headers.

If you cannot write something factual from the given data, write "Not enough recent data gathered for a research note yet" instead of inventing content."""


def build_stock_research_context(ticker, name, quote, items):
    lines = [f"Stock: {name} ({ticker})"]
    if quote:
        price = quote.get("price")
        chg = quote.get("changePct")
        if price is not None:
            lines.append(f"Current price: {price} ({chg:+.2f}% today)" if chg is not None else f"Current price: {price}")
        target = quote.get("targetMeanPrice")
        rec = quote.get("recommendationKey")
        if target:
            lines.append(f"Broker consensus target price: {target}" + (f", consensus rating: {rec}" if rec else ""))
    if items:
        lines.append("Recent news/broker items (most recent first):")
        for it in items[:8]:
            broker_bit = f" [{it['broker']}]" if it.get("broker") else ""
            lines.append(f"- ({it.get('category','news')}){broker_bit} {it.get('title','')} — {it.get('source','')}, {it.get('pubDate','')}")
    return "\n".join(lines)


def generate_stock_research(ticker, name, quote, items):
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return None
    context = build_stock_research_context(ticker, name, quote, items)
    body = json.dumps({
        "model": AI_DIGEST_MODEL,
        "max_tokens": 250,
        "system": MARKET_RESEARCH_SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": f"Facts gathered for this stock:\n{context}\n\nWrite the research note."}],
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
        method="POST",
    )
    try:
        resp_data = json.loads(urllib.request.urlopen(req, timeout=30).read())
        text = "".join(b.get("text", "") for b in resp_data.get("content", []) if b.get("type") == "text").strip()
        if not text:
            return None
        if any(re.search(pat, text, re.IGNORECASE) for pat in FORBIDDEN_DIGEST_PATTERNS):
            print(f"  ! market research blocked for {ticker}: output matched an advice-shaped pattern", file=sys.stderr)
            return None
        return text
    except urllib.error.HTTPError as e:
        try:
            error_body = e.read().decode("utf-8", errors="replace")
        except Exception:
            error_body = "(couldn't read error body)"
        print(f"  ! market research generation failed for {ticker}: HTTP {e.code} — {error_body[:300]}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"  ! market research generation failed for {ticker}: {e}", file=sys.stderr)
        return None


def update_market_research(watchlist, quotes, items_by_ticker, existing_research):
    """
    Refreshes MARKET_RESEARCH_MAX_PER_RUN of the stalest watchlist entries per run.
    Returns the updated research dict (unchanged if no API key configured — the
    dashboard section still renders, just shows nothing generated yet).
    """
    if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
        print("  market research: skipped, no ANTHROPIC_API_KEY configured", file=sys.stderr)
        return existing_research  # feature off — no key, no cost, section shows "not enabled"

    now = time.time()

    def staleness(ticker):
        entry = existing_research.get(ticker)
        if not entry or not entry.get("generatedAtEpoch"):
            return float("inf")  # never generated — highest priority
        return now - entry["generatedAtEpoch"]

    candidates = sorted(watchlist, key=lambda s: -staleness(s["ticker"]))
    due = [s for s in candidates if staleness(s["ticker"]) >= MARKET_RESEARCH_REFRESH_SECONDS]
    refresh_list = due[:MARKET_RESEARCH_MAX_PER_RUN]
    print(f"  market research: {len(due)} stock(s) due, refreshing {len(refresh_list)} this run")

    for stock in refresh_list:
        ticker, name = stock["ticker"], stock["name"]
        text = generate_stock_research(ticker, name, quotes.get(ticker), items_by_ticker.get(ticker, []))
        if text:
            existing_research[ticker] = {
                "text": text,
                "generatedAtEpoch": now,
                "generatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            }
            print(f"  market research refreshed: {ticker}")
        time.sleep(1)  # be polite between successive API calls in the same run

    return existing_research


def main():
    watchlist = load_json(WATCHLIST_FILE, [])
    if not watchlist:
        print("watchlist.json is empty — nothing to do.")
        return

    seen_state = load_json(SEEN_FILE, {"seen": []})
    seen = set(seen_state.get("seen", []))
    global _recent_send_times
    _recent_send_times = list(seen_state.get("recentSendTimes", []))
    data = load_json(DATA_FILE, {"items": {}, "quotes": {}, "lastPoll": None})
    items_by_ticker = data.get("items", {})
    quotes = data.get("quotes", {})
    market_research = data.get("marketResearch", {})

    yahoo_crumb = get_yahoo_crumb()
    print(f"Yahoo auth crumb: {'obtained' if yahoo_crumb else 'FAILED — screener/analyst-history will likely 401'}")

    new_alerts = []
    mention_counts = {}  # ticker -> {"name": ..., "count": ...} — today's mention volume, purely descriptive
    big_movers = []
    market_wide_enriched = []
    screener = {}
    ratings_items = []
    screener_news = {}
    uptrend_stocks = []
    screener_targets = {}
    ftse100 = fetch_ftse100()

    if not SKIP_MARKET_WIDE:
        ratings_items, _ = fetch_feed(ANALYST_RATINGS_FEED_URL)
        market_wide_items, _ = fetch_feed(market_wide_broker_news_url())
        screener = fetch_lse_screener()

        # News for every stock ranked in Volume/Gainers/Losers, not just the watchlist —
        # deduped by symbol (a stock can appear in more than one list), one query each,
        # staggered to avoid the burst-triggered 503s seen earlier in this project.
        screener_news = {}
        ranked_stocks = {}
        for section in ("volume", "gainers", "losers"):
            for row in screener.get(section, []):
                ranked_stocks[row["symbol"]] = row.get("name", row["symbol"])
        print(f"Fetching news for {len(ranked_stocks)} screener-ranked stocks (volume/gainers/losers)...")
        for symbol, name in ranked_stocks.items():
            # Search using a cleaned name (see clean_company_name docstring) — the
            # dashboard still displays the full "name" as-is, only the search query
            # uses the cleaned version, since that's what actually matches real news.
            items, _ = fetch_feed(general_news_url(clean_company_name(name)))
            items = [it for it in items if passes_news_filters(it.get("pubDate"))]
            if items:
                now_iso_sc = datetime.now(timezone.utc).isoformat()
                enriched_items = []
                for it in items[:5]:  # cap per-stock to keep dashboard/message size sane
                    category = classify(it["title"])
                    broker = detect_broker(it["title"]) if category in ("upgrade", "downgrade", "target", "target_raise", "target_cut", "initiation", "reiteration") else None
                    enriched_items.append({**it, "ticker": symbol, "company": name, "category": category, "broker": broker,
                                            "detectedAt": now_iso_sc, "normalizedAt": now_iso_sc,
                                            "normalizedAction": CATEGORY_TO_NORMALIZED_ACTION.get(category)})
                screener_news[symbol] = enriched_items
            time.sleep(1)  # stagger — this is the change most likely to trip Google's rate limiting if rushed

        # 5-day uptrend: real closing-price history for the same deduped stock set (no
        # extra tickers beyond what's already being fetched news for) plus the watchlist.
        uptrend_stocks = []
        uptrend_targets = dict(ranked_stocks)
        for stock in watchlist:
            uptrend_targets.setdefault(stock["ticker"], stock["name"])
        print(f"Checking price technicals, targets, earnings/dividend dates for {len(uptrend_targets)} stocks...")
        short_interest_map = fetch_short_interest()  # one fetch per run, not per ticker
        screener_targets = {}  # symbol -> {targetMeanPrice, recommendationKey, nextEarningsDate, exDividendDate, rsi14, ma20, aboveMA20}
        for symbol, name in uptrend_targets.items():
            hist = fetch_price_technicals(symbol)
            if hist and hist.get("changePct5d") is not None and hist["changePct5d"] >= UPTREND_5DAY_THRESHOLD_PCT:
                uptrend_stocks.append({"symbol": symbol, "name": name, **hist})
            time.sleep(0.3)  # lighter stagger — this hits Yahoo, not Google, different rate-limit budget

            # Real broker price targets + earnings/dividend calendar dates — all from
            # Yahoo's own published data, in the SAME request as before (calendarEvents
            # was added to the existing modules list, not a new call).
            analyst = fetch_yahoo_analyst(symbol)
            if analyst:
                entry = {}
                if analyst.get("targetMeanPrice"):
                    entry["targetMeanPrice"] = analyst["targetMeanPrice"]
                    entry["recommendationKey"] = analyst.get("recommendationKey")
                if analyst.get("nextEarningsDate"):
                    entry["nextEarningsDate"] = analyst["nextEarningsDate"]
                if analyst.get("exDividendDate"):
                    entry["exDividendDate"] = analyst["exDividendDate"]
                if analyst.get("dividendRate") is not None:
                    entry["dividendRate"] = analyst["dividendRate"]
                if analyst.get("dividendYieldPct") is not None:
                    entry["dividendYieldPct"] = analyst["dividendYieldPct"]
                if analyst.get("trailingPE") is not None:
                    entry["trailingPE"] = analyst["trailingPE"]
                if analyst.get("trailingEps") is not None:
                    entry["trailingEps"] = analyst["trailingEps"]
                if analyst.get("marketCap") is not None:
                    entry["marketCap"] = analyst["marketCap"]
                if analyst.get("fiftyTwoWeekLow") is not None:
                    entry["fiftyTwoWeekLow"] = analyst["fiftyTwoWeekLow"]
                if analyst.get("fiftyTwoWeekHigh") is not None:
                    entry["fiftyTwoWeekHigh"] = analyst["fiftyTwoWeekHigh"]
                if analyst.get("heldPercentInsidersPct") is not None:
                    entry["heldPercentInsidersPct"] = analyst["heldPercentInsidersPct"]
                if analyst.get("sector"):
                    entry["sector"] = analyst["sector"]
                if analyst.get("industry"):
                    entry["industry"] = analyst["industry"]
                if analyst.get("businessSummary"):
                    entry["businessSummary"] = analyst["businessSummary"]
                if hist:
                    entry["rsi14"] = hist.get("rsi14")
                    entry["ma20"] = hist.get("ma20")
                    entry["aboveMA20"] = hist.get("aboveMA20")
                si = match_short_interest(name, short_interest_map)
                if si:
                    entry["shortInterestPct"] = si["pct"]
                    entry["shortInterestDate"] = si["position_date"]
                if entry:
                    screener_targets[symbol] = entry
            time.sleep(0.3)

        # Attach the target-price data directly onto each screener row so it displays
        # with the row itself — no separate lookup needed on the dashboard/message side.
        for section in ("volume", "gainers", "losers"):
            for row in screener.get(section, []):
                extra = screener_targets.get(row["symbol"])
                if extra:
                    row.update(extra)  # attach whichever fields were found: target, recommendation, earnings/ex-div dates, RSI/MA

        # Market-wide broker alerts: covers ALL LSE companies for upgrade/downgrade news,
        # not just the watchlist — this is what "all LSE companies" actually needs, without
        # trying to poll ~1,900 individual tickers (which would take hours per cycle and
        # get rate-limited). Combines the unfiltered ratings feed + the market-wide search.
        # general_market_items (investing.com's general "Stock Market News" feed) was
        # removed from this pool entirely, and its fetch removed too — despite being on
        # the uk.investing.com subdomain, real data proved it's not actually UK-scoped
        # (surfaced US broker actions on Workday, Ulta Beauty, Elastic, Autodesk as if
        # they were "all LSE" alerts). Not reused elsewhere either, so keeping the fetch
        # around would just be a wasted request for data that's no longer used anywhere.
        market_wide_pool = ratings_items + market_wide_items
        market_wide_pool = [it for it in market_wide_pool if passes_news_filters(it.get("pubDate"))]
        now_iso_mw = datetime.now(timezone.utc).isoformat()
        for it in market_wide_pool:
            category = classify(it["title"])
            # "target" = a broker raising/cutting their price target — a genuine, already-
            # published broker action, same category of fact as an upgrade/downgrade, so it
            # belongs in the same alert stream rather than being silently dropped.
            if category not in ("upgrade", "downgrade", "target", "target_raise", "target_cut", "initiation", "reiteration"):
                continue
            market_wide_enriched.append({
                **it,
                "ticker": "MARKET",
                "company": "",
                "category": category,
                "broker": detect_broker(it["title"]),
                "detectedAt": now_iso_mw,
                "normalizedAt": now_iso_mw,
                "normalizedAction": CATEGORY_TO_NORMALIZED_ACTION.get(category),
            })
        market_wide_dedup = {}
        for it in market_wide_enriched:
            market_wide_dedup[it["link"]] = it  # de-dupe within this run (same story can appear in both feeds)
        market_wide_enriched = list(market_wide_dedup.values())

        for it in market_wide_enriched:
            if it["link"] in seen:
                continue
            seen.add(it["link"])
            new_alerts.append(it)
    else:
        print("SKIP_MARKET_WIDE set — skipping market-wide search/screener/heatmap (covered by the other job).")

    for stock in watchlist:
        ticker, name = stock["ticker"], stock["name"]
        print(f"Polling {ticker} ({name})...")

        g_items, _ = fetch_feed(google_news_url(name))
        y_items, _ = fetch_feed(yahoo_news_url(ticker))
        rb_items, _ = fetch_feed(reuters_bloomberg_url(name))
        matched_ratings = [it for it in ratings_items if name.lower() in it["title"].lower()]
        combined = g_items + y_items + rb_items + matched_ratings
        combined = [it for it in combined if passes_news_filters(it.get("pubDate"))]
        # Purely a count of real, already-published items mentioning this stock today
        # (deduped by link) — a fact about today's coverage volume, not a prediction of
        # anything. NEWS_SAME_LONDON_DAY_ONLY already restricts `combined` to today.
        mention_links = {it["link"] for it in combined}
        mention_counts[ticker] = {"name": name, "count": len(mention_links)}

        quote = fetch_yahoo_quote(ticker)
        if quote:
            quotes[ticker] = quote
            # Purely descriptive: this stock has already moved a lot today.
            # Not a prediction of further movement, just a factual flag.
            if abs(quote.get("changePct") or 0) >= BIG_MOVER_THRESHOLD_PCT:
                big_movers.append({**quote, "ticker": ticker})

        analyst = fetch_yahoo_analyst(ticker)
        analyst_items = analyst_history_to_items(ticker, analyst)
        if analyst and analyst.get("targetMeanPrice") and ticker in quotes:
            quotes[ticker]["targetMeanPrice"] = analyst["targetMeanPrice"]
            quotes[ticker]["recommendationKey"] = analyst.get("recommendationKey")

        now_iso = datetime.now(timezone.utc).isoformat()
        enriched = []
        for it in combined:
            category = classify(it["title"])
            # Only tag a broker when the item is actually a rating/target call — otherwise
            # a story that merely mentions a bank's name (e.g. a personnel/legal story
            # about "Barclays") gets mislabeled as if that bank issued the rating.
            broker = detect_broker(it["title"]) if category in ("upgrade", "downgrade", "target", "target_raise", "target_cut", "initiation", "reiteration") else None
            enriched.append({
                **it,
                "ticker": ticker,
                "company": name,
                "category": category,
                "broker": broker,
                "detectedAt": now_iso,
                "normalizedAt": now_iso,
                "normalizedAction": CATEGORY_TO_NORMALIZED_ACTION.get(category),
            })
        # Analyst history items already carry structured category/broker/pubDate —
        # merge as-is rather than re-running keyword classification on them.
        for it in analyst_items:
            enriched.append({**it, "ticker": ticker, "company": name, "detectedAt": now_iso})

        existing = items_by_ticker.get(ticker, [])
        merged = enriched + existing
        seen_links = set()
        deduped = []
        for it in merged:
            if it["link"] in seen_links:
                continue
            seen_links.add(it["link"])
            deduped.append(it)
        deduped.sort(key=item_sort_key, reverse=True)
        items_by_ticker[ticker] = deduped[:MAX_ITEMS_PER_TICKER]

        for it in enriched:
            if it["link"] in seen:
                continue
            seen.add(it["link"])
            # Analyst-history items are backfilled (up to 15 past ratings) so first-run
            # dashboard context isn't empty — but only alert on genuinely recent ones,
            # not old history that happens to be new to our "seen" set. For your
            # watchlist specifically, event-category news (mergers, trading updates,
            # profit warnings, results) is genuinely price-moving and worth pushing —
            # unlike the market-wide stream, this is scoped to stocks you actually track.
            if it["category"] in ("upgrade", "downgrade", "target", "target_raise", "target_cut", "initiation", "director_dealing", "event") and it.get("_recent", True):
                new_alerts.append(it)

        time.sleep(0.5)  # be polite to free sources

    # Merge this run's market-wide items with previously stored ones (same pattern as
    # the per-ticker feed) so the dashboard shows recent history, not just this cycle.
    existing_market_wide = data.get("marketWide", [])
    merged_market_wide = market_wide_enriched + existing_market_wide
    seen_links_mw = set()
    deduped_market_wide = []
    for it in merged_market_wide:
        if it["link"] in seen_links_mw:
            continue
        seen_links_mw.add(it["link"])
        deduped_market_wide.append(it)
    deduped_market_wide.sort(key=item_sort_key, reverse=True)
    deduped_market_wide = deduped_market_wide[:MAX_ITEMS_PER_TICKER]

    # Refresh a capped batch of the stalest Market Research write-ups (see function
    # docstring for why this doesn't refresh everyone every run) using this run's
    # freshly-gathered items_by_ticker/quotes as the factual source.
    market_research = update_market_research(watchlist, quotes, items_by_ticker, market_research)

    data = {
        "items": items_by_ticker,
        "quotes": quotes,
        "screener": screener,
        "ftse100": ftse100,
        "screenerNews": screener_news,
        "uptrendStocks": uptrend_stocks,
        "bigMovers": big_movers,
        "marketWide": deduped_market_wide,
        "marketResearch": market_research,
        "lastPoll": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
    }
    save_json(DATA_FILE, data)
    render_dashboard(data, watchlist)

    screener_last_sent = seen_state.get("lastScreenerMessage", 0)
    screener_interval = SCREENER_MESSAGE_INTERVAL_SECONDS if _current_template() == "callmebot" else 0
    if time.time() - screener_last_sent >= screener_interval:
        screener_messages = format_screener_sections(screener)
        for i, msg in enumerate(screener_messages):
            print(msg)
            send_webhook(msg)
            if i < len(screener_messages) - 1:
                time.sleep(2)  # small gap so rapid-fire messages don't collapse/drop
        if screener_messages:
            seen_state["lastScreenerMessage"] = time.time()
    else:
        print("Screener WhatsApp send throttled (sent within the last hour) — dashboard/data still updated above.")

    screener_news_msg = format_screener_news_message(screener_news)
    if screener_news_msg:
        print(screener_news_msg)
        send_webhook(screener_news_msg)

    uptrend_msg = format_uptrend_message(uptrend_stocks)
    if uptrend_msg:
        print(uptrend_msg)
        send_webhook(uptrend_msg)

    attention_msg = format_attention_message(mention_counts)
    if attention_msg:
        print(attention_msg)
        send_webhook(attention_msg)

    if big_movers:
        lines = [f"🔥 ALREADY MOVING TODAY (±{BIG_MOVER_THRESHOLD_PCT:.0f}%+, watchlist)  🕐 {now_stamp()} — facts about today, not a forecast:"]
        for m in big_movers:
            arrow = "▲" if (m.get("changePct") or 0) >= 0 else "▼"
            lines.append(f'{m["ticker"]} {arrow}{abs(m.get("changePct") or 0):.2f}%')
        movers_msg = "\n".join(lines)
        print(movers_msg)
        send_webhook(movers_msg)

    ALERT_LABELS = {"upgrade": "⬆ UPGRADE", "downgrade": "⬇ DOWNGRADE", "target": "🎯 PRICE TARGET", "target_raise": "🎯⬆ TARGET RAISED", "target_cut": "🎯⬇ TARGET CUT", "initiation": "🆕 INITIATED", "reiteration": "🔁 REITERATED", "director_dealing": "🧑‍💼 DIRECTOR DEALING", "event": "📰 NEWS"}
    alert_cap = MAX_ALERTS_PER_RUN if _current_template() == "callmebot" else len(new_alerts)
    alerts_to_send = new_alerts[:alert_cap]
    skipped_count = len(new_alerts) - len(alerts_to_send)
    for alert in alerts_to_send:
        label = ALERT_LABELS.get(alert["category"], "📰 NEWS")
        broker_tag = f' ({alert["broker"]})' if alert["broker"] else ""
        ticker_bit = "LSE (market-wide)" if alert["ticker"] == "MARKET" else alert["ticker"]
        article_date = alert.get("pubDate") or ""
        msg = f'{label}: {ticker_bit}{broker_tag}\n{alert["title"]}\n{alert["link"]}\n📅 {article_date}  🕐 seen {now_stamp()}'
        print(f"Alert: {msg}")
        send_webhook(msg)
        time.sleep(2)  # CallMeBot can silently drop messages sent in rapid succession
                        # even while still returning 200 — space every send out.
    if skipped_count > 0:
        skip_msg = f"ℹ️ {skipped_count} more alert(s) this run — see the full list on the dashboard (capped to avoid WhatsApp rate limits)."
        print(skip_msg)
        send_webhook(skip_msg)

    print(f"Done. {len(new_alerts)} new alert(s) found, {len(alerts_to_send)} sent to WhatsApp.")

    if new_alerts:
        time.sleep(2)  # gap before heartbeat/digest, same reasoning as between alerts

    heartbeat_sent = maybe_send_heartbeat(seen_state, watchlist, len(new_alerts), len(big_movers))
    digest_sent = maybe_send_ai_digest(seen_state, screener, market_wide_enriched, big_movers, new_alerts)

    # Always persist here (not just when heartbeat/digest fired) — this is the only place
    # lastScreenerMessage's updated value actually gets written to disk.
    save_json(SEEN_FILE, {
        "seen": list(seen)[-MAX_SEEN:],
        "lastHeartbeat": seen_state.get("lastHeartbeat", 0),
        "lastAiDigest": seen_state.get("lastAiDigest", 0),
        "lastScreenerMessage": seen_state.get("lastScreenerMessage", 0),
        "recentSendTimes": _recent_send_times,  # carries the rate-limit budget forward to the next run
    })



# =========================================================================
# BROKER-EVENTS PIPELINE (steps 1-3) — target extraction, ticker
# resolution, normalization, and cross-source deduplication/merging.
#
# SEPARATE, ADDITIVE subsystem — nothing here is called from main(), the
# dashboard, or the existing news/alert pipeline yet. Pure functions
# operating on in-memory lists only; no filesystem I/O. This is what makes
# idempotency achievable once a persistent store is added in a later step:
# calling this pipeline twice on the same raw input always produces the
# same output, since every ID is deterministic (hash-based or built from
# the data itself, never a random/incrementing value or a timestamp of
# when the pipeline ran).
#
# Pipeline shape, exactly as specified:
#   raw_events -> normalized_events -> candidate_matches -> merged_events
# Nothing is ever discarded: every input event ends up in exactly one of
# merged_events / conflicts / unmatched_events in the final output.
# =========================================================================

import hashlib

# -------------------------------------------------------------------
# STEP 1 — Investing.com target-price extraction
# -------------------------------------------------------------------
# Only extracts a target when the headline gives EXPLICIT numbers with an
# unambiguous currency marker (£/GBP prefix, or p/pence suffix). A bare
# number with neither marker is never guessed at — real UK financial
# headlines always mark pence explicitly ("450p") or pounds explicitly
# ("£4.50"); a marker-less number is genuinely ambiguous and is left as
# no-extraction rather than assumed.

_MONEY_TOKEN_RE = r"(?:GBP\s*)?(?:£\s*(\d{1,3}(?:,\d{3})*(?:\.\d+)?)|(\d{1,3}(?:,\d{3})*(?:\.\d+)?)\s*p(?:ence)?\b)"
# group(1) = pounds-marked number (£ prefix), group(2) = pence-marked number (p suffix)
# — exactly one is populated per match, since the alternation is exclusive.


def _parse_money_match(m):
    """Given a regex match against _MONEY_TOKEN_RE, returns (value_in_pounds, currency)."""
    pounds_str, pence_str = m.group(1), m.group(2)
    if pounds_str is not None:
        return float(pounds_str.replace(",", "")), "GBP"
    if pence_str is not None:
        return float(pence_str.replace(",", "")) / 100.0, "GBP"
    return None, None  # should not happen given the alternation, but never guess


def extract_target_from_headline(title):
    """
    Returns (old_target, new_target, currency) — all None if no explicit,
    unambiguous target-price change is stated in the headline. old_target
    may be None even when new_target is found (e.g. "cuts price target to
    £3.80" states only the new value) — that's a valid partial extraction,
    not a failure; old_target simply stays unknown, never invented.

    Only searches within a window around the word "target" (or "price
    target"/"pt") — avoids accidentally grabbing an unrelated number
    elsewhere in a long headline (e.g. a percentage, a share count, a date).
    """
    t = title.lower()
    target_idx = None
    for kw in ("price target", "target price", " pt ", "target"):
        idx = t.find(kw)
        if idx != -1:
            target_idx = idx
            break
    if target_idx is None:
        return None, None, None

    window_start = max(0, target_idx - 15)
    window_end = min(len(title), target_idx + 70)
    window = title[window_start:window_end]

    money_re = re.compile(_MONEY_TOKEN_RE, re.IGNORECASE)
    matches = list(money_re.finditer(window))
    if not matches:
        return None, None, None

    if len(matches) == 1:
        value, currency = _parse_money_match(matches[0])
        return None, value, currency

    if len(matches) == 2:
        v1, c1 = _parse_money_match(matches[0])
        v2, c2 = _parse_money_match(matches[1])
        if v1 is None or v2 is None:
            return None, None, None
        currency = c1 or c2
        # "from X to Y" => old=X, new=Y. Anything else ("to Y from X",
        # "raises ... to Y from X") => the token right after "from" is old,
        # the other is new. Checked via the word immediately preceding the
        # FIRST token in reading order.
        before_first = window[:matches[0].start()].strip().split()
        word_before_first = before_first[-1].lower() if before_first else ""
        if word_before_first == "from":
            return v1, v2, currency
        return v2, v1, currency

    # 3+ money-like tokens in the window is ambiguous — a malformed or
    # unusually-structured headline. Never guess which pair is the target.
    return None, None, None


def compute_target_change_pct(old_target, new_target):
    """(new - old) / old * 100 — only when both values are present and
    old_target is non-zero. Never estimated otherwise."""
    if old_target is None or new_target is None or old_target == 0:
        return None
    return (new_target - old_target) / old_target * 100.0


# -------------------------------------------------------------------
# STEP 2 — Name -> LSE ticker resolution
# -------------------------------------------------------------------

def build_name_ticker_lookup(watchlist, screener_rows):
    """
    Builds a company-name -> ticker lookup from data ALREADY available in
    this run (watchlist + screener pool) — never invented, never fetched
    from a separate source. Keys are lowercased and cleaned via the
    existing clean_company_name() (strips "ORD 10P"-style jargon), so
    lookups tolerate the same naming noise Yahoo's screener returns.
    """
    lookup = {}
    for stock in watchlist:
        name = clean_company_name(stock["name"]).strip().lower()
        if name:
            lookup[name] = stock["ticker"].upper()
    for row in screener_rows:
        name = clean_company_name(row.get("name", "")).strip().lower()
        symbol = row.get("symbol", "")
        ticker = symbol.upper().rsplit(".L", 1)[0] if symbol.upper().endswith(".L") else symbol.upper()
        if name and ticker:
            lookup.setdefault(name, ticker)  # watchlist takes priority if both define the same name
    return lookup


def resolve_ticker_by_substring(headline, lookup):
    """
    The Investing.com RSS has no ticker field, only a headline — so this
    checks whether any KNOWN company name (from the lookup, i.e. from this
    run's own watchlist/screener) appears as a substring of the headline.
    A company outside that pool genuinely can't be resolved from data this
    run has, and stays ticker=None rather than guessed.
    """
    if not headline:
        return None
    t_lower = headline.lower()
    for name, ticker in lookup.items():
        if name and name in t_lower:
            return ticker
    return None


# -------------------------------------------------------------------
# STEP 3 — Normalization + cross-source deduplication/merging
# -------------------------------------------------------------------

CONFIDENCE_SINGLE_STRUCTURED = "SINGLE_SOURCE_STRUCTURED"
CONFIDENCE_SINGLE_PARSED = "SINGLE_SOURCE_PARSED"
CONFIDENCE_MERGED_HIGH = "MERGED_HIGH"
CONFIDENCE_MERGED_PARTIAL = "MERGED_PARTIAL"
CONFIDENCE_CONFLICT = "CONFLICT"


def make_source_event_id(source, **fields):
    """Deterministic per-source ID — identical inputs always produce the
    identical ID, which is what makes re-polling idempotent at this layer."""
    raw = source + "|" + "|".join(f"{k}={fields[k]}" for k in sorted(fields))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def make_canonical_key(ticker, broker_canonical, date_str, old_rating, new_rating, old_target, new_target):
    """
    Cross-source canonical key — deliberately excludes the source name,
    since its purpose is letting independently-observed records of the
    SAME real-world event collide and merge. Ticker is mandatory: without
    it, two events can't be safely confirmed as the same company, so no
    canonical key is produced.

    Target values are represented as integer pence in the key (not the
    float pounds stored in the event itself) to avoid floating-point
    representation differences producing different keys for the same
    real value.
    """
    if not ticker:
        return None
    old_r = (old_rating or "NA").strip().upper()
    new_r = (new_rating or "NA").strip().upper()
    old_t = str(round(old_target * 100)) if old_target is not None else "NA"
    new_t = str(round(new_target * 100)) if new_target is not None else "NA"
    return f"LSE|{ticker.upper()}|{broker_canonical.upper()}|{date_str}|{old_r}>{new_r}|{old_t}>{new_t}"


def _canonical_broker(name):
    """Resolves a broker name to its canonical form from the existing
    BROKER_NAMES list (e.g. so 'Barclays' from two sources always matches)."""
    if not name:
        return None
    for b in BROKER_NAMES:
        if b.lower() == name.lower() or b.lower() in name.lower():
            return b
    return name


def normalize_yahoo_event(ticker, company, firm, from_grade, to_grade, action_code, epoch, link):
    """
    One Yahoo upgradeDowngradeHistory entry -> a normalized event. Always
    SINGLE_SOURCE_STRUCTURED confidence — Yahoo never carries target
    history (verified in the forensic pass), so old_target/new_target are
    always None here; a later merge with an Investing.com event may fill
    them in.
    """
    dt_london = datetime.fromtimestamp(epoch, tz=timezone.utc).astimezone(LONDON_TZ)
    date_str = dt_london.strftime("%Y-%m-%d")
    action = normalize_action_from_grades(from_grade, to_grade, action_code)
    return {
        "source": "yahoo",
        "source_event_id": make_source_event_id("yahoo", ticker=ticker, firm=firm, epoch=epoch),
        "source_url": link,
        "timestamp": dt_london.astimezone(timezone.utc).isoformat(),
        "date": date_str,
        "company": company,
        "ticker": ticker.upper(),
        "exchange": "LSE",
        "broker": _canonical_broker(firm),
        "action": action,
        "old_rating": from_grade or None,
        "new_rating": to_grade,
        "old_rating_bucket": normalize_rating_bucket(from_grade),
        "new_rating_bucket": normalize_rating_bucket(to_grade),
        "old_target": None,
        "new_target": None,
        "target_currency": None,
        "target_change_pct": None,
        "confidence": CONFIDENCE_SINGLE_STRUCTURED,
    }


def normalize_investing_event(title, link, pub_date, ticker_lookup):
    """
    One Investing.com RSS item -> a normalized event. Broker/action come
    from the existing, already-tested classify()/detect_broker()/
    normalize_headline_action(). Target comes from Step 1's regex
    extractor. Ticker comes from Step 2's substring lookup.
    """
    category = classify(title)
    broker = detect_broker(title)
    old_target, new_target, currency = extract_target_from_headline(title)
    dt = _parse_pub_date(pub_date)
    if dt is None:
        date_str, timestamp = None, None
    else:
        dt_london = dt.astimezone(LONDON_TZ)
        date_str = dt_london.strftime("%Y-%m-%d")
        timestamp = dt.astimezone(timezone.utc).isoformat()

    ticker = resolve_ticker_by_substring(title, ticker_lookup)

    return {
        "source": "investing_com",
        "source_event_id": make_source_event_id("investing_com", link=link),
        "source_url": link,
        "timestamp": timestamp,
        "date": date_str,
        "company": None,  # not a structured field from this source — never invented
        "ticker": ticker,  # may be None — genuinely unresolvable for out-of-pool companies
        "exchange": "LSE" if ticker else None,
        "broker": _canonical_broker(broker) if broker else None,
        "action": normalize_headline_action(title) or category.upper(),
        "old_rating": None,  # headlines don't state explicit old/new rating text structurally
        "new_rating": None,  # — only Yahoo gives literal fromGrade/toGrade
        "old_rating_bucket": None,
        "new_rating_bucket": None,
        "old_target": old_target,
        "new_target": new_target,
        "target_currency": currency,
        "target_change_pct": compute_target_change_pct(old_target, new_target),
        "confidence": CONFIDENCE_SINGLE_PARSED,
    }


def _events_conflict(e1, e2):
    """
    True if two same-ticker/same-broker/same-day events actively
    contradict each other and must NOT be merged. Compatible (mergeable):
    one side has no rating info and the other does (a target-only fragment
    naturally complementing a rating-only fragment); one has no target and
    the other does. Conflicting: both state a rating transition and they
    differ; both state a target value and it differs.
    """
    if e1["new_rating"] and e2["new_rating"]:
        if (e1.get("old_rating") or "").lower() != (e2.get("old_rating") or "").lower():
            return True
        if e1["new_rating"].lower() != e2["new_rating"].lower():
            return True
    if e1["new_target"] is not None and e2["new_target"] is not None:
        if round(e1["new_target"], 2) != round(e2["new_target"], 2):
            return True
    if e1["old_target"] is not None and e2["old_target"] is not None:
        if round(e1["old_target"], 2) != round(e2["old_target"], 2):
            return True
    return False


def _merge_pair(e1, e2, canonical_key):
    """Combines two compatible (non-conflicting) same-event records.
    Never overwrites — fills gaps, always keeps both sources' IDs/URLs."""
    def pick(a, b):
        return a if a is not None else b
    timestamps = [t for t in (e1.get("timestamp"), e2.get("timestamp")) if t]
    merged = {
        "event_id": canonical_key,
        "timestamp": min(timestamps) if timestamps else None,
        "date": e1.get("date") or e2.get("date"),
        "company": pick(e1.get("company"), e2.get("company")),
        "ticker": e1.get("ticker") or e2.get("ticker"),
        "exchange": "LSE",
        "broker": e1.get("broker") or e2.get("broker"),
        "action": e1.get("action") if e1.get("action") not in (None, "NO_CHANGE") else e2.get("action"),
        "old_rating": pick(e1.get("old_rating"), e2.get("old_rating")),
        "new_rating": pick(e1.get("new_rating"), e2.get("new_rating")),
        "old_rating_bucket": pick(e1.get("old_rating_bucket"), e2.get("old_rating_bucket")),
        "new_rating_bucket": pick(e1.get("new_rating_bucket"), e2.get("new_rating_bucket")),
        "old_target": pick(e1.get("old_target"), e2.get("old_target")),
        "new_target": pick(e1.get("new_target"), e2.get("new_target")),
        "target_currency": pick(e1.get("target_currency"), e2.get("target_currency")),
        "source": sorted(set([e1["source"], e2["source"]])),
        "source_url": [e1["source_url"], e2["source_url"]],
        "source_event_ids": [e1["source_event_id"], e2["source_event_id"]],
        "confidence": CONFIDENCE_MERGED_HIGH,
    }
    merged["target_change_pct"] = compute_target_change_pct(merged["old_target"], merged["new_target"])
    both_sides_gave_target = e1.get("new_target") is not None and e2.get("new_target") is not None
    if not both_sides_gave_target:
        # Only one side contributed target data — genuinely a partial
        # merge, not the strong two-source agreement MERGED_HIGH implies.
        merged["confidence"] = CONFIDENCE_MERGED_PARTIAL
    return merged


def finalize_unmatched_event(e):
    """Gives an unmatched (single-source) normalized event its own
    deterministic event_id — canonical key when ticker/broker/date are all
    known, otherwise the source-specific ID (never invented, never guessed)."""
    canonical = None
    if e.get("ticker") and e.get("broker") and e.get("date"):
        canonical = make_canonical_key(
            e["ticker"], e["broker"], e["date"],
            e.get("old_rating"), e.get("new_rating"),
            e.get("old_target"), e.get("new_target"),
        )
    out = dict(e)
    out["event_id"] = canonical or e["source_event_id"]
    out["source"] = [e["source"]]
    out["source_url"] = [e["source_url"]]
    out["source_event_ids"] = [e["source_event_id"]]
    return out


def deduplicate_and_merge(normalized_events):
    """
    normalized_events -> candidate_matches -> merged_events, per the
    required pipeline shape. Returns (merged_events, conflicts,
    unmatched_events) — every input event ends up in exactly one of these
    three, never silently dropped.

    Matching evidence, exactly as specified: ticker + canonical broker +
    calendar day are REQUIRED to even consider two events a candidate
    match — same ticker+broker+day alone is NOT assumed to mean the same
    event; the actual rating/target transitions are then checked for
    compatibility (_events_conflict) before merging. A broker making two
    genuinely distinct actions on the same stock on the same day is
    handled by evaluating every pair independently within a group, rather
    than assuming the whole group is one event.
    """
    groups = {}
    unmatched = []
    for e in normalized_events:
        if not (e.get("ticker") and e.get("broker") and e.get("date")):
            unmatched.append(finalize_unmatched_event(e))
            continue
        key = (e["ticker"], e["broker"], e["date"])
        groups.setdefault(key, []).append(e)

    merged_events = []
    conflicts = []
    for key, group in groups.items():
        if len(group) == 1:
            unmatched.append(finalize_unmatched_event(group[0]))
            continue
        merged_indices = set()
        for i in range(len(group)):
            if i in merged_indices:
                continue
            for j in range(i + 1, len(group)):
                if j in merged_indices:
                    continue
                e1, e2 = group[i], group[j]
                if _events_conflict(e1, e2):
                    conflicts.append({
                        "reason": "rating_or_target_mismatch",
                        "ticker": key[0], "broker": key[1], "date": key[2],
                        "events": [e1, e2],
                    })
                    continue
                canonical_key = make_canonical_key(
                    e1["ticker"], e1["broker"], e1["date"],
                    e1.get("old_rating") or e2.get("old_rating"),
                    e1.get("new_rating") or e2.get("new_rating"),
                    e1.get("old_target") if e1.get("old_target") is not None else e2.get("old_target"),
                    e1.get("new_target") if e1.get("new_target") is not None else e2.get("new_target"),
                )
                merged_events.append(_merge_pair(e1, e2, canonical_key))
                merged_indices.add(i)
                merged_indices.add(j)
        for i in range(len(group)):
            if i not in merged_indices:
                unmatched.append(finalize_unmatched_event(group[i]))

    return merged_events, conflicts, unmatched




# =========================================================================
# PERSISTENT EVENT STORE — state/events.json
#
# SEPARATE from state/data.json entirely — different file, different
# functions, never touches the existing data.json load/save calls. This
# layer is additive: nothing here is called from main() yet (verified by
# the same "not wired in" check used for the pipeline itself).
#
# Design principles, per spec:
# - Append-only: existing entries are NEVER modified or removed by normal
#   operation. Only explicit user action (not built here) would ever
#   remove an event.
# - Idempotent: running the exact same poll cycle twice produces byte-
#   identical file content the second time (0 events added, same sorted
#   order) — this is verified directly in the tests below.
# - Atomic writes: temp file + os.replace(), so a crash or interruption
#   mid-write can never leave a truncated/corrupt events.json in place —
#   either the old file survives intact, or the new one lands complete.
# - A CORRUPT existing file is never silently overwritten. If the file
#   can't be parsed as the expected {version, events} shape, persistence
#   is skipped for that cycle entirely and the corrupt file is left
#   exactly as found — overwriting it with a "fresh" store would destroy
#   whatever real history it might still contain in partially-recoverable
#   form, which is a worse outcome than just skipping a cycle.
# =========================================================================

import tempfile

EVENTS_STATE_FILE = os.path.join(STATE_DIR, "events.json")
EVENTS_STORE_VERSION = 1


class EventsStoreCorruptError(Exception):
    """Raised when state/events.json exists but doesn't parse as valid
    JSON, or doesn't match the expected {version, events} shape. The
    caller must NOT write a fresh store in response to this — see the
    module docstring above."""
    pass


def load_events_store(path=None):
    """
    Loads the events store. A MISSING file is the only case that
    legitimately produces a fresh empty store (first run ever) — a file
    that EXISTS but fails to parse raises EventsStoreCorruptError instead
    of silently returning empty, since those are very different
    situations: one has no history yet, the other has history that must
    not be destroyed.
    """
    path = path or EVENTS_STATE_FILE
    if not os.path.exists(path):
        return {"version": EVENTS_STORE_VERSION, "events": []}
    with open(path, "r", encoding="utf-8") as f:
        raw = f.read()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise EventsStoreCorruptError(f"{path} contains invalid JSON: {e}") from e
    if not isinstance(data, dict) or not isinstance(data.get("events"), list):
        raise EventsStoreCorruptError(f"{path} does not match the expected {{version, events}} shape")
    return data


def atomic_write_json(path, data):
    """
    Writes JSON atomically: content goes to a temp file in the SAME
    directory first (so the final os.replace is a same-filesystem rename,
    guaranteed atomic on POSIX and Windows), then replaces the target in
    one step. An interrupted write leaves the ORIGINAL file completely
    intact — there is no window where a half-written file sits at the
    real path.
    """
    dir_name = os.path.dirname(path) or "."
    os.makedirs(dir_name, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=dir_name, prefix=".events_tmp_", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


def merge_events_into_store(store, new_events):
    """
    Append-only, idempotent merge. An event whose event_id is already
    present is left COMPLETELY untouched — "if the same event is
    encountered again, do nothing" is implemented literally, including
    not enriching an existing single-source record even if this run's
    merge could now add a second source to it. This is the simplest rule
    that can never corrupt or silently rewrite something already stored;
    a known, accepted limitation (not a bug) is that a previously-stored
    single-source event won't retroactively gain a second source later —
    documented here rather than hidden.

    Returns (updated_store, added_count). added_count == 0 on a repeated/
    duplicate run is the CORRECT outcome, not a failure.

    Events are kept sorted by event_id after every merge — this makes the
    file's byte content (not just its logical content) identical across
    runs that produce the same event set, so idempotency can be verified
    at the file level directly, not just by re-parsing and comparing.
    """
    existing_ids = {e["event_id"] for e in store["events"]}
    added = 0
    for e in new_events:
        if e["event_id"] in existing_ids:
            continue
        store["events"].append(e)
        existing_ids.add(e["event_id"])
        added += 1
    store["events"].sort(key=lambda e: e["event_id"])
    return store, added


def fetch_yahoo_broker_events(watchlist):
    """
    Fetches and normalizes Yahoo upgradeDowngradeHistory events for every
    watchlist ticker. Fails soft per-ticker AND overall — one ticker's
    fetch failing never blocks the rest (same pattern as every other
    fetch_* in this file), and if Yahoo is down entirely this returns []
    rather than raising, so the caller can proceed with Investing.com
    alone.
    """
    events = []
    for stock in watchlist:
        ticker, name = stock["ticker"], stock["name"]
        try:
            analyst = fetch_yahoo_analyst(ticker)
            if not analyst:
                continue
            for h in analyst.get("history", [])[:15]:
                firm = h.get("firm", "")
                to_grade = h.get("toGrade", "")
                epoch = h.get("epochGradeDate")
                if not firm or not to_grade or not epoch:
                    continue
                link = f"https://finance.yahoo.com/quote/{yahoo_symbol(ticker)}/analysis#{firm}-{epoch}".replace(" ", "-")
                events.append(normalize_yahoo_event(
                    ticker=ticker, company=name, firm=firm,
                    from_grade=h.get("fromGrade"), to_grade=to_grade,
                    action_code=h.get("action", ""), epoch=epoch, link=link,
                ))
        except Exception as e:
            print(f"  ! events pipeline: yahoo fetch failed for {ticker}: {e}", file=sys.stderr)
            continue
    return events


def fetch_investing_broker_events(ticker_lookup):
    """Fetches and normalizes Investing.com analyst-ratings RSS events.
    Fails soft — returns [] on any error, never raises, so the caller can
    proceed with Yahoo's events alone."""
    try:
        items, had_error = fetch_feed(ANALYST_RATINGS_FEED_URL)
        if had_error:
            return []
        return [
            normalize_investing_event(
                title=it["title"], link=it["link"], pub_date=it.get("pubDate"),
                ticker_lookup=ticker_lookup,
            )
            for it in items
        ]
    except Exception as e:
        print(f"  ! events pipeline: investing.com fetch failed: {e}", file=sys.stderr)
        return []


def collect_and_persist_broker_events(watchlist, screener_rows=None, dry_run=False, path=None):
    """
    ONE full poll-cycle pass: fetch both sources -> normalize -> dedupe/
    merge -> load existing store -> add genuinely new events -> write
    atomically. This is the single function a future main() would call;
    it is NOT called from main() yet (see the module-load check in the
    test suite).

    Failure handling, exactly as specified:
    - Yahoo fails -> proceeds using Investing.com's events only.
    - Investing.com fails -> proceeds using Yahoo's events only.
    - Both fail -> new_events is empty; the existing store is loaded and
      re-saved (0 added) rather than skipped — safe because
      merge_events_into_store only ever appends, so re-saving unchanged
      content can never truncate or lose data.
    - Existing store is CORRUPT -> persistence is skipped entirely this
      cycle; the corrupt file is left exactly as-is, loudly logged.
    - Write itself fails (disk full, permissions, etc) -> reported, the
      PREVIOUS file on disk is untouched (atomic_write_json guarantees
      this structurally, not just by convention).

    Conflicting pairs (from deduplicate_and_merge) are never merged into
    one event, but neither underlying single-source record is discarded —
    both are individually persisted (each keeping its own original
    confidence and event_id) via finalize_unmatched_event, exactly as
    "conflicting events remain separate" requires. The conflict itself
    (the fact that two records disagreed) is returned in the result dict
    for logging/inspection, not stored as its own event-shaped record.

    dry_run=True runs the full pipeline and returns the result WITHOUT
    writing anything to disk.
    """
    path = path or EVENTS_STATE_FILE
    screener_rows = screener_rows or []

    yahoo_events = fetch_yahoo_broker_events(watchlist)
    ticker_lookup = build_name_ticker_lookup(watchlist, screener_rows)
    investing_events = fetch_investing_broker_events(ticker_lookup)

    all_normalized = yahoo_events + investing_events
    merged, conflicts, unmatched = deduplicate_and_merge(all_normalized)

    new_events = merged + unmatched
    for c in conflicts:
        for e in c["events"]:
            new_events.append(finalize_unmatched_event(e))

    try:
        store = load_events_store(path)
    except EventsStoreCorruptError as e:
        print(f"  ! events store CORRUPT — skipping persistence this cycle to avoid data loss: {e}", file=sys.stderr)
        return {
            "written": False, "reason": "existing_store_corrupt",
            "new_events_found": len(new_events), "added": 0,
            "conflicts": conflicts, "store": None,
        }

    updated_store, added = merge_events_into_store(store, new_events)

    if dry_run:
        return {
            "written": False, "reason": "dry_run",
            "new_events_found": len(new_events), "added": added,
            "conflicts": conflicts, "store": updated_store,
        }

    try:
        atomic_write_json(path, updated_store)
    except Exception as e:
        print(f"  ! events store write FAILED — previous file left untouched: {e}", file=sys.stderr)
        return {
            "written": False, "reason": "write_failed",
            "new_events_found": len(new_events), "added": 0,
            "conflicts": conflicts, "store": None,
        }

    return {
        "written": True, "reason": "ok",
        "new_events_found": len(new_events), "added": added,
        "conflicts": conflicts, "store": updated_store,
    }



if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        # A totally unhandled crash (vs the per-source try/excepts already inside main)
        # should still tell you something's wrong, not just go silent.
        err_msg = f"⚠️ UK Stock Watch poller crashed: {type(e).__name__}: {e}\nWill retry next scheduled run."
        print(err_msg, file=sys.stderr)
        try:
            send_webhook(err_msg)
        except Exception:
            pass  # if even the failure notification fails, there's nothing more we can do here
        raise  # re-raise so the GitHub Actions run shows red/failed in the Actions tab too
