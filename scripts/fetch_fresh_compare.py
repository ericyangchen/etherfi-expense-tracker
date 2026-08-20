"""One-off diagnostic: pull the CURRENT Ether.fi export and diff May/June
against what's in the DB. READ-ONLY — never upserts, never mutates rows.

Saves the raw export to data/debug/fresh_<ts>.csv for ground-truth inspection,
then reports:
  1. Rows present at source but missing in DB (by stable dedup_key)
  2. Rows in DB but absent from the current source (stale / removed upstream)
  3. Every non-CLEARED card_spend (PENDING/DECLINED/CANCELLED) at source,
     with any same-merchant CLEARED sibling — i.e. status-driven duplicates
  4. Whether the 6 June pending auths have now settled, and if the settled
     timestamp drifted from the pending one (would defeat card dedup)
"""
from __future__ import annotations

import os
import sys
import tempfile
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import db
from csv_import import parse_csv
from scraper import (
    TRANSACTION_HISTORY_URL,
    _auth_state_exists,
    _dismiss_popups,
    _is_session_expired,
    _watch_auth_failures,
)
from playwright.sync_api import sync_playwright


def download_export() -> str:
    """Download the current CSV/XLSX export to a kept path. Returns the path."""
    if not _auth_state_exists():
        raise RuntimeError("No saved session; run `python main.py login` first.")

    debug_dir = Path(config.AUTH_STATE_PATH).parent / "debug"
    debug_dir.mkdir(parents=True, exist_ok=True)
    out = debug_dir / f"fresh_{datetime.now():%Y%m%d_%H%M%S}.csv"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(storage_state=config.AUTH_STATE_PATH)
        page = context.new_page()
        auth_failures = _watch_auth_failures(page)
        page.goto(TRANSACTION_HISTORY_URL, wait_until="load", timeout=60_000)
        page.wait_for_timeout(5000)
        if _is_session_expired(page, auth_failures):
            browser.close()
            raise RuntimeError("Session expired; run `python main.py login`.")
        _dismiss_popups(page)
        try:
            page.wait_for_selector("h2:has-text('Transactions')", timeout=15_000)
        except Exception:
            pass
        page.wait_for_timeout(3000)

        download_selectors = [
            'button:has(svg.lucide-arrow-down-to-line)',
            'button:has(svg[class*="arrow-down-to-line"])',
            'button:has(svg[class*="arrow-down"])',
            'button[aria-label*="download" i]',
            '[aria-label*="download" i] button',
        ]
        btn = None
        for sel in download_selectors:
            try:
                btn = page.wait_for_selector(sel, state="visible", timeout=10_000)
                if btn:
                    break
            except Exception:
                continue
        if not btn:
            browser.close()
            raise RuntimeError("Download button not found.")
        btn.scroll_into_view_if_needed()
        page.wait_for_timeout(500)
        with page.expect_download(timeout=30_000) as info:
            btn.click()
        info.value.save_as(str(out))
        context.storage_state(path=config.AUTH_STATE_PATH)
        browser.close()
    return str(out)


def in_may_june(ts: str) -> bool:
    return ts >= "2026-05-01" and ts < "2026-07-01"


def main() -> None:
    raw = download_export()
    print(f"[fresh] saved raw export -> {raw}")
    fresh = parse_csv(raw)
    print(f"[fresh] parsed {len(fresh)} rows total")

    # --- Load DB May/June rows ---
    with db.get_conn() as conn:
        db_rows = conn.execute(
            """SELECT id, dedup_key, card, type, status, amount_usd, timestamp,
                      TRIM(description) AS descr
               FROM transactions
               WHERE timestamp >= '2026-05-01' AND timestamp < '2026-07-01'
               ORDER BY timestamp"""
        ).fetchall()

    fresh_mj = [t for t in fresh if in_may_june(t["timestamp"])]
    db_by_key = {r["dedup_key"]: r for r in db_rows}
    fresh_by_key = {t["dedup_key"]: t for t in fresh_mj}

    print(f"\n[scope] May+June: {len(fresh_mj)} source rows vs {len(db_rows)} DB rows")

    # 1. At source, missing from DB
    missing = [t for k, t in fresh_by_key.items() if k not in db_by_key]
    print(f"\n=== (1) AT SOURCE but MISSING in DB: {len(missing)} ===")
    for t in sorted(missing, key=lambda x: x["timestamp"]):
        print(f"  {t['timestamp']}  {t['card'] or '----':>4}  {t['type']:<20} "
              f"{t['status']:<9} {t['amount_usd']:>9}  {t['description'][:30]}")

    # 2. In DB, gone from source
    gone = [r for k, r in db_by_key.items() if k not in fresh_by_key]
    print(f"\n=== (2) IN DB but NOT at source: {len(gone)} ===")
    for r in sorted(gone, key=lambda x: x["timestamp"]):
        print(f"  id={r['id']} {r['timestamp']}  {r['card'] or '----':>4}  "
              f"{r['type']:<20} {r['status']:<9} {r['amount_usd']:>9}  {r['descr'][:30]}")

    # 3. Non-CLEARED card_spend at source + CLEARED siblings (same card+merchant)
    print("\n=== (3) NON-CLEARED card rows at source (status-driven dup risk) ===")
    by_merchant: dict[tuple, list[dict]] = defaultdict(list)
    for t in fresh_mj:
        if t["type"] in ("card_spend", "card_refund", "physical_card_refund"):
            by_merchant[(t["card"], t["description"].strip())].append(t)
    for (card, merch), rows in sorted(by_merchant.items()):
        statuses = {r["status"] for r in rows}
        if statuses - {"CLEARED"}:  # has a non-CLEARED row
            print(f"  card {card} | {merch[:34]}")
            for r in sorted(rows, key=lambda x: x["timestamp"]):
                print(f"      {r['timestamp']}  {r['status']:<9} {r['amount_usd']:>9}")

    # 4. June pending auths: settled now? timestamp drift?
    print("\n=== (4) June PENDING auths in DB — current status at source ===")
    june_pending = [r for r in db_rows
                    if r["status"] == "PENDING" and str(r["timestamp"]) >= "2026-06"]
    for r in sorted(june_pending, key=lambda x: x["timestamp"]):
        merch = r["descr"].strip()
        cands = [t for t in fresh
                 if t["card"] == r["card"] and t["description"].strip() == merch]
        print(f"  DB id={r['id']} {r['timestamp']} PENDING {r['amount_usd']} {merch[:28]}")
        for t in sorted(cands, key=lambda x: x["timestamp"]):
            drift = "  <-- same dedup_key" if t["dedup_key"] == r["dedup_key"] else "  <-- DIFFERENT key"
            print(f"      src {t['timestamp']}  {t['status']:<9} {t['amount_usd']:>9}{drift}")


if __name__ == "__main__":
    main()
