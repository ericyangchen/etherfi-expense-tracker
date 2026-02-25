"""Playwright scraper for Ether.fi Cash transaction history."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright, Page, BrowserContext

import config
import db


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
# Scrape flow (headless, uses saved session)
# ---------------------------------------------------------------------------

def _parse_transaction_row(row_data: dict) -> dict | None:
    """Convert a raw scraped row dict into the DB-ready format."""
    try:
        timestamp = row_data.get("timestamp", "").strip()
        description = row_data.get("description", "").strip()
        amount_usd_raw = row_data.get("amount_usd", "0").strip()
        amount_usd = float(amount_usd_raw)

        return {
            "timestamp": timestamp,
            "type": row_data.get("type", "card_spend").strip(),
            "description": description,
            "status": row_data.get("status", "").strip(),
            "amount_usd": amount_usd,
            "card": row_data.get("card", "").strip(),
            "card_holder": row_data.get("card_holder", "").strip() or None,
            "original_amount": float(row_data["original_amount"]) if row_data.get("original_amount") else None,
            "original_currency": row_data.get("original_currency", "").strip() or None,
            "cashback": float(row_data["cashback"]) if row_data.get("cashback") else None,
            "category": row_data.get("category", "").strip() or None,
            "dedup_key": db.make_dedup_key(timestamp, amount_usd_raw, description),
        }
    except (ValueError, KeyError):
        return None


def _scrape_card_transactions(page: Page) -> list[dict]:
    """
    Scrape visible transaction rows from the current card view.

    NOTE: This function's selectors are placeholders. They must be updated
    to match the actual Ether.fi Cash DOM structure once the real page is
    inspected. The general strategy:
    1. Wait for the transaction list container to appear.
    2. Scroll to load all transactions (if lazy-loaded).
    3. Extract each row's data attributes or text content.
    """
    page.wait_for_timeout(3000)

    # Try to find a CSV download/export button first (more reliable than DOM scraping)
    # Fallback: scrape transaction rows from the DOM

    # Placeholder: extract transactions via page.evaluate()
    # The actual JS extraction logic depends on the Ether.fi DOM structure
    raw_rows = page.evaluate("""
        () => {
            // This JS must be adapted to the actual Ether.fi Cash page DOM.
            // Example: look for a transaction list table or repeated elements.
            const rows = [];
            // document.querySelectorAll('[data-testid="transaction-row"]').forEach(el => {
            //     rows.push({
            //         timestamp: el.querySelector('.timestamp')?.textContent,
            //         description: el.querySelector('.merchant')?.textContent,
            //         amount_usd: el.querySelector('.amount')?.textContent?.replace('$',''),
            //         status: el.querySelector('.status')?.textContent,
            //         card: el.querySelector('.card')?.textContent,
            //         type: 'card_spend',
            //     });
            // });
            return rows;
        }
    """)

    txns = []
    for raw in raw_rows:
        parsed = _parse_transaction_row(raw)
        if parsed:
            txns.append(parsed)
    return txns


def _is_session_expired(page: Page, etherfi_url: str) -> bool:
    """Check if we got redirected to a login/connect-wallet page."""
    current = page.url.lower()
    # If the URL changed significantly from what we expected, session is likely expired
    if "connect" in current or "login" in current or "sign" in current:
        return True
    # Also check for a connect-wallet button as a signal
    btn = page.query_selector('button:has-text("Connect"), button:has-text("Sign in")')
    return btn is not None


def scrape() -> list[dict]:
    """
    Run a headless scrape using saved session state.
    Returns list of transaction dicts ready for DB upsert.
    Raises RuntimeError if session is expired.
    """
    if not _auth_state_exists():
        raise RuntimeError(
            f"No saved session at {config.AUTH_STATE_PATH}. "
            "Run 'python main.py login' first."
        )

    etherfi_url = db.get_config("etherfi_url")
    all_txns: list[dict] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(storage_state=config.AUTH_STATE_PATH)
        page = context.new_page()

        page.goto(etherfi_url, wait_until="networkidle")

        if _is_session_expired(page, etherfi_url):
            browser.close()
            raise RuntimeError(
                "Session expired. Run 'python main.py login' to re-authenticate."
            )

        # Find all card selector tabs/buttons
        # NOTE: selectors are placeholders, must be adapted to actual DOM
        card_tabs = page.query_selector_all('[data-testid="card-tab"], .card-selector button')

        if not card_tabs:
            # Single card or no tabs visible — scrape current view
            txns = _scrape_card_transactions(page)
            all_txns.extend(txns)
        else:
            for tab in card_tabs:
                tab.click()
                page.wait_for_timeout(2000)
                txns = _scrape_card_transactions(page)
                all_txns.extend(txns)

        # Save updated session state
        context.storage_state(path=config.AUTH_STATE_PATH)
        browser.close()

    return all_txns
