"""
Diagnostic v9 - pure visual reconnaissance, no interaction attempted.

v8's real run result: neither 'Index' nor 'Set news filters' was found
as visible text anywhere on the page after cookie consent. Rather than
guess a THIRD time at selectors from API response label strings, this
takes actual screenshots of the real, live News Explorer page and dumps
its visible text content and interactive-element structure - so the
next selector choice is based on what the page genuinely looks like,
not another inference from a JSON schema.

No interaction is attempted here beyond accepting the cookie consent
banner (already proven reliable across v4-v8). This run's only job is
to produce evidence a human (or Claude, reading the evidence) can look
at directly.

Run only in GitHub Actions - not usable from Claude's own sandbox,
which cannot reach any of these hosts at all.
"""
import json
import time
from datetime import datetime, timezone

NEWS_URL = "https://www.londonstockexchange.com/news?tab=news-explorer"


def main():
    from playwright.sync_api import sync_playwright

    print("=" * 70)
    print("DIAGNOSTIC v9 - visual reconnaissance of News Explorer")
    print("=" * 70)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(
            viewport={"width": 1400, "height": 1000},
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
        )

        page.goto(NEWS_URL, timeout=30000, wait_until="networkidle")
        print("Loaded page")

        try:
            accept_btn = page.locator("#onetrust-accept-btn-handler")
            if accept_btn.is_visible(timeout=5000):
                accept_btn.click()
                page.wait_for_load_state("networkidle", timeout=15000)
                print("Accepted cookie consent banner")
        except Exception as e:
            print(f"Cookie consent: {type(e).__name__}: {e}")

        page.wait_for_timeout(4000)

        # Screenshot 1: top of page, as first loaded.
        page.screenshot(path="v9_screenshot_01_top.png")
        print("Saved v9_screenshot_01_top.png")

        # Screenshot 2: full page, entire scrollable content.
        page.screenshot(path="v9_screenshot_02_fullpage.png", full_page=True)
        print("Saved v9_screenshot_02_fullpage.png")

        # Dump the visible text content of the whole page - cheap, and
        # lets a human/Claude search for "Index"/"Filter"/"Apply" etc
        # directly in real text rather than guessing.
        try:
            body_text = page.inner_text("body")
            with open("v9_visible_text.txt", "w") as f:
                f.write(body_text)
            print(f"Saved v9_visible_text.txt ({len(body_text)} chars)")
        except Exception as e:
            print(f"Could not dump visible text: {type(e).__name__}: {e}")

        # Dump every interactive element (button, input, select, [role]
        # elements) with its visible text / label / placeholder / aria-label
        # - a structural inventory, not a guess, of everything clickable.
        try:
            elements = page.eval_on_selector_all(
                "button, input, select, [role='button'], [role='tab'], "
                "[role='combobox'], [role='listbox'], a[href^='#'], [class*='filter' i], [class*='accordion' i]",
                """els => els.map(el => ({
                    tag: el.tagName,
                    text: (el.innerText || '').trim().slice(0, 80),
                    placeholder: el.placeholder || null,
                    ariaLabel: el.getAttribute('aria-label'),
                    role: el.getAttribute('role'),
                    className: (el.className || '').toString().slice(0, 100),
                    id: el.id || null,
                    visible: el.offsetParent !== null
                }))"""
            )
            with open("v9_interactive_elements.json", "w") as f:
                json.dump(elements, f, indent=2)
            print(f"Saved v9_interactive_elements.json ({len(elements)} elements found)")
            visible_count = sum(1 for e in elements if e.get("visible"))
            print(f"  ({visible_count} of them currently visible)")
        except Exception as e:
            print(f"Could not enumerate interactive elements: {type(e).__name__}: {e}")

        # Also try scrolling down once, in case the filter panel only
        # exists further down the page, then screenshot again.
        try:
            page.mouse.wheel(0, 1500)
            page.wait_for_timeout(2000)
            page.screenshot(path="v9_screenshot_03_after_scroll.png")
            print("Saved v9_screenshot_03_after_scroll.png (after scrolling down)")
        except Exception as e:
            print(f"Scroll + screenshot: {type(e).__name__}: {e}")

        browser.close()

    print("\nDone. All files saved to the working directory for artifact upload.")


if __name__ == "__main__":
    main()
