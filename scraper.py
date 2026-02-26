"""Playwright scraper for Ether.fi Cash transaction history."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright, Page

import config
import db
from csv_import import parse_csv


def _auth_state_exists() -> bool:
    return os.path.isfile(config.AUTH_STATE_PATH)


def _ensure_data_dir() -> None:
    Path(config.AUTH_STATE_PATH).parent.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Login flow (headed browser, manual wallet connect)
# ---------------------------------------------------------------------------

def login() -> None:
    """Launch headed browser for manual wallet login, then save session."""
    _ensure_data_dir()
    etherfi_url = db.get_config("etherfi_url")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        page.goto(etherfi_url)

        print(f"Browser opened at {etherfi_url}")
        print("Please connect your wallet and sign in.")
        input("Press ENTER here after you are fully logged in...")

        context.storage_state(path=config.AUTH_STATE_PATH)
        print(f"Session saved to {config.AUTH_STATE_PATH}")

        browser.close()


# ---------------------------------------------------------------------------
# Scrape flow: go to transaction-history, dismiss popups, download CSV
# ---------------------------------------------------------------------------

TRANSACTION_HISTORY_URL = "https://www.ether.fi/app/cash/transaction-history"

# Popup dismiss selectors (try in order; some promotions end and popups disappear)
_POPUP_DISMISS_SELECTORS = [
    'button:has-text("OK")',
    'button:has-text("Accept")',
    'button:has-text("Accept All")',
    '[aria-label="Close"]',
    'button[aria-label="Close"]',
    '[data-testid="close"]',
    'button:has-text("Dismiss")',
    'button:has-text("Got it")',
    'button:has-text("Close")',
]


def _dismiss_popups(page: Page) -> None:
    """Try to dismiss any modal/popup. Flexible — popups may or may not exist."""
    page.wait_for_timeout(2000)
    for selector in _POPUP_DISMISS_SELECTORS:
        btn = page.query_selector(selector)
        if btn and btn.is_visible():
            try:
                btn.click()
                page.wait_for_timeout(1500)
                break
            except Exception:
                pass


def _is_session_expired(page: Page) -> bool:
    """Check if we got redirected to a login/connect-wallet page."""
    current = page.url.lower()
    if "connect" in current or "login" in current or "sign" in current:
        return True
    btn = page.query_selector('button:has-text("Connect"), button:has-text("Sign in")')
    return btn is not None and btn.is_visible()


def scrape() -> list[dict]:
    """
    Run a headless scrape using saved session state.
    Navigates to transaction-history, dismisses popups, clicks download CSV.
    Returns list of transaction dicts ready for DB upsert.
    Raises RuntimeError if session is expired.
    """
    if not _auth_state_exists():
        raise RuntimeError(
            f"No saved session at {config.AUTH_STATE_PATH}. "
            "Run 'python main.py login' first."
        )

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(storage_state=config.AUTH_STATE_PATH)
        page = context.new_page()

        # Go directly to transaction history page
        page.goto(TRANSACTION_HISTORY_URL, wait_until="load", timeout=60_000)
        page.wait_for_timeout(5000)

        if _is_session_expired(page):
            browser.close()
            raise RuntimeError(
                "Session expired. Run 'python main.py login' to re-authenticate."
            )

        _dismiss_popups(page)

        # Wait for page to stabilize (transaction list + download button render)
        page.wait_for_selector("h2:has-text('Transactions')", timeout=15_000)
        page.wait_for_timeout(3000)

        # Download button selectors (Ether.fi may minify class names in prod)
        download_selectors = [
            'button:has(svg.lucide-arrow-down-to-line)',
            'button:has(svg[class*="arrow-down-to-line"])',
            'button:has(svg[class*="arrow-down"])',
            'button[aria-label*="download" i]',
            '[aria-label*="download" i] button',
        ]
        download_btn = None
        for sel in download_selectors:
            try:
                download_btn = page.wait_for_selector(sel, state="visible", timeout=10_000)
                if download_btn:
                    break
            except Exception:
                continue

        if not download_btn:
            browser.close()
            raise RuntimeError(
                "Could not find download button on transaction-history page. "
                "Page may have changed or a popup may be blocking it."
            )

        download_btn.scroll_into_view_if_needed()
        page.wait_for_timeout(500)

        with page.expect_download(timeout=30_000) as download_info:
            download_btn.click()

        download = download_info.value
        with tempfile.NamedTemporaryFile(
            suffix=".csv", delete=False
        ) as f:
            tmp_path = f.name
        download.save_as(tmp_path)
        try:
            txns = parse_csv(tmp_path)
        finally:
            os.unlink(tmp_path)

        # Save updated session state
        context.storage_state(path=config.AUTH_STATE_PATH)
        browser.close()

    return txns
