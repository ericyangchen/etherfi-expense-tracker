"""Playwright scraper for Ether.fi Cash transaction history."""
from __future__ import annotations

import logging
import os
import shutil
import tempfile
from datetime import datetime
from pathlib import Path

_log = logging.getLogger(__name__)

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

# Identity providers refuse to sign you in from a browser that advertises
# automation. Playwright's bundled Chromium reports navigator.webdriver=true
# behind a "HeadlessChrome" user agent, and Google's sign-in page answers
# "This browser or app may not be secure." The real Chrome binary with these
# two switches stripped reports webdriver=false and a plain "Chrome/152" UA,
# which the sign-in flow accepts.
_ANTI_AUTOMATION_ARGS = ["--disable-blink-features=AutomationControlled"]
_AUTOMATION_DEFAULT_ARGS = ["--enable-automation"]


def _launch_login_browser(p):
    """Headed browser for manual sign-in, preferring the real Chrome install.

    Falls back to the bundled Chromium if Chrome is not present — sign-in may
    be rejected there, but a blocked login beats no browser at all.
    """
    try:
        return p.chromium.launch(
            headless=False,
            channel="chrome",
            args=_ANTI_AUTOMATION_ARGS,
            ignore_default_args=_AUTOMATION_DEFAULT_ARGS,
        )
    except Exception as e:
        _log.warning(
            "Could not launch the real Chrome install (%s); falling back to "
            "bundled Chromium. Google may reject it as an unsafe browser.", e
        )
        return p.chromium.launch(headless=False)


def login() -> None:
    """Launch headed browser for manual wallet login, then save session."""
    _ensure_data_dir()
    etherfi_url = db.get_config("etherfi_url")

    with sync_playwright() as p:
        browser = _launch_login_browser(p)
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


# Only an authenticated session gets a 2xx out of the cash API; a dead one 401s.
_CASH_API_FRAGMENT = "/app/cash/api/"
_AUTH_FAIL_STATUSES = (401, 403)

# Text ether.fi renders only in its public (signed-out) shell. Keep these
# distinctive: a bare "sign in" is a substring of merchant names like "DESIGN INC".
_LOGGED_OUT_MARKERS = ("become a member", "connect wallet")


def _watch_auth_failures(page: Page) -> list[str]:
    """Record cash-API responses proving the session is no longer authenticated.

    Install before navigating — the 401s arrive during page load. The returned
    list fills in as responses come back.
    """
    failures: list[str] = []

    def _on_response(response) -> None:
        if (
            _CASH_API_FRAGMENT in response.url
            and response.status in _AUTH_FAIL_STATUSES
        ):
            failures.append(f"{response.status} {response.url}")

    page.on("response", _on_response)
    return failures


def _is_session_expired(page: Page, auth_failures: list[str] | None = None) -> bool:
    """Check whether the saved session is dead.

    Ether.fi is a SPA, so an expired session neither redirects nor renders a
    "Connect"/"Sign in" button: it 401s on the cash API and quietly paints the
    public shell ("Become a Member" / "No transactions yet"). The URL and button
    checks alone therefore let dead sessions straight through, which is why the
    API response is the primary signal here.
    """
    if auth_failures:
        _log.warning("Cash API rejected the session: %s", auth_failures[0])
        return True

    current = page.url.lower()
    if "connect" in current or "login" in current or "sign" in current:
        return True

    btn = page.query_selector('button:has-text("Connect"), button:has-text("Sign in")')
    if btn is not None and btn.is_visible():
        return True

    try:
        body = page.inner_text("body").lower()
    except Exception:
        return False
    return any(marker in body for marker in _LOGGED_OUT_MARKERS)


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
        auth_failures = _watch_auth_failures(page)

        # Go directly to transaction history page
        page.goto(TRANSACTION_HISTORY_URL, wait_until="load", timeout=60_000)
        page.wait_for_timeout(5000)

        if _is_session_expired(page, auth_failures):
            browser.close()
            raise RuntimeError(
                "Session expired. Run 'python main.py login' to re-authenticate."
            )

        _dismiss_popups(page)

        # Let the page settle. The heading renders in the signed-out shell too,
        # so it proves nothing about auth and must not be fatal — the session
        # check and the download button below are the real gates.
        try:
            page.wait_for_selector("h2:has-text('Transactions')", timeout=15_000)
        except Exception:
            _log.warning("Transactions heading never rendered; continuing anyway")
        page.wait_for_timeout(3000)

        # Auth failures can land after the first check (late XHRs, slow hydration).
        if _is_session_expired(page, auth_failures):
            browser.close()
            raise RuntimeError(
                "Session expired. Run 'python main.py login' to re-authenticate."
            )

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
        except Exception as e:
            debug_dir = Path(config.AUTH_STATE_PATH).parent / "debug"
            debug_dir.mkdir(parents=True, exist_ok=True)
            debug_path = debug_dir / f"failed_{datetime.now():%Y%m%d_%H%M%S}.csv"
            shutil.copyfile(tmp_path, debug_path)
            _log.error("CSV parse failed (%s); saved raw to %s", e, debug_path)
            raise RuntimeError(f"CSV parse failed: {e}. Raw CSV at {debug_path}")
        finally:
            os.unlink(tmp_path)

        # Save updated session state
        context.storage_state(path=config.AUTH_STATE_PATH)
        browser.close()

    return txns
