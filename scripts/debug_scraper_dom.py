#!/usr/bin/env python3
"""Run inside Docker to debug transaction-history page DOM. No DB needed."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

from playwright.sync_api import sync_playwright

AUTH = config.AUTH_STATE_PATH
URL = "https://www.ether.fi/app/cash/transaction-history"

if not os.path.isfile(AUTH):
    print("No auth_state.json - run login first")
    sys.exit(1)

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    context = browser.new_context(storage_state=AUTH)
    page = context.new_page()

    print("Navigating...")
    page.goto(URL, wait_until="load", timeout=60_000)
    page.wait_for_timeout(6000)

    # Dismiss popups
    for sel in ['button:has-text("OK")', 'button:has-text("Accept")']:
        btn = page.query_selector(sel)
        if btn and btn.is_visible():
            btn.click()
            page.wait_for_timeout(2000)
            break

    page.wait_for_timeout(3000)

    # Check download button selectors
    selectors = [
        'button:has(svg.lucide-arrow-down-to-line)',
        'button:has(svg[class*="arrow-down-to-line"])',
        'button:has(svg[class*="arrow-down"])',
    ]
    print("\n--- Selector check ---")
    for sel in selectors:
        el = page.query_selector(sel)
        print(f"  {sel}: found={el is not None}, visible={el.is_visible() if el else 'N/A'}")

    # Dump buttons in the filter bar area (where download usually is)
    info = page.evaluate("""
        () => {
            const buttons = document.querySelectorAll('button');
            const results = [];
            buttons.forEach((b, i) => {
                const svg = b.querySelector('svg');
                const svgClass = svg ? svg.className.baseVal || svg.getAttribute('class') || '' : '';
                if (svgClass.includes('arrow') || svgClass.includes('down') || b.closest('[class*="flex"]')) {
                    results.push({
                        i, text: b.textContent?.trim().substring(0, 30),
                        aria: b.getAttribute('aria-label'),
                        svgClass: svgClass.substring(0, 80),
                        html: b.outerHTML.substring(0, 300)
                    });
                }
            });
            return results.slice(0, 20);
        }
    """)
    print("\n--- Buttons with arrow/down or in flex ---")
    import json
    for r in info:
        print(json.dumps(r, indent=2, default=str))

    # User agent
    ua = page.evaluate("() => navigator.userAgent")
    print("\n--- User-Agent ---")
    print(ua[:150])

    browser.close()
    print("\nDone.")
