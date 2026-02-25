#!/usr/bin/env python3
"""Ether.fi Expense Tracker — CLI orchestrator."""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone


def _init() -> None:
    import db

    db.init_db()
    db.migrate_seed_cards()


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


def cmd_login(_args: argparse.Namespace) -> None:
    _init()
    import scraper

    scraper.login()


def cmd_scrape(args: argparse.Namespace) -> None:
    _init()
    import db, scraper, analytics, notify

    print("[scrape] Starting headless scrape...")
    try:
        txns = scraper.scrape()
    except RuntimeError as e:
        msg = f"[scrape] ERROR: {e}"
        print(msg)
        try:
            notify.send(f"Ether.fi Tracker Error:\n{e}")
        except Exception:
            pass
        sys.exit(1)

    affected = 0
    if txns:
        affected = db.upsert_transactions(txns)
        print(f"[scrape] Upserted {len(txns)} transactions ({affected} new/updated)")
    else:
        print("[scrape] No transactions scraped (selectors may need updating)")

    db.update_last_fetch_at()
    print(f"[scrape] Done. {affected} new/updated.")


def cmd_import(args: argparse.Namespace) -> None:
    _init()
    import csv_import
    import db

    filepath = args.file
    print(f"[import] Importing {filepath}...")
    txns = csv_import.parse_csv(filepath)
    affected = db.upsert_transactions(txns)
    print(f"[import] Processed {len(txns)} transactions ({affected} new/updated)")


def cmd_report(args: argparse.Namespace) -> None:
    _init()
    import db, analytics, notify

    report_type = args.report_type

    if report_type == "latest":
        txns = db.get_unreported_transactions()
        report = analytics.format_daily_report(txns, title="Ether.fi Latest Report")
        if txns and not args.no_send:
            db.mark_as_reported([t["id"] for t in txns])
    elif report_type == "daily":
        txns = db.get_today_transactions()
        today = datetime.now(timezone.utc).strftime("%Y/%m/%d")
        report = analytics.format_daily_report(
            txns, title=f"Ether.fi Daily Report - {today}"
        )
    elif report_type == "monthly":
        summary = analytics.get_monthly_summary(args.year, args.month)
        report = analytics.format_monthly_report(summary)
    else:
        summary = analytics.get_monthly_summary()
        report = analytics.format_monthly_report(summary)

    print(report)
    if not args.no_send:
        notify.send(report)


def cmd_bot(_args: argparse.Namespace) -> None:
    _init()
    import bot

    bot.run_bot()


def cmd_gui(_args: argparse.Namespace) -> None:
    import shutil
    import subprocess

    st_bin = shutil.which("streamlit")
    if not st_bin:
        print("Streamlit not installed. Run:  pip install streamlit")
        sys.exit(1)

    gui_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gui.py")
    subprocess.run([st_bin, "run", gui_path, "--server.headless", "true"])


def cmd_config(args: argparse.Namespace) -> None:
    _init()
    import db

    if args.config_action == "list":
        rows = db.get_all_config()
        for row in rows:
            print(f"  {row['key']}: {row['value']}")
    elif args.config_action == "set":
        db.set_config(args.key, args.value)
        print(f"  {args.key} = {args.value}")
    elif args.config_action == "get":
        try:
            val = db.get_config(args.key)
            print(f"  {args.key}: {val}")
        except KeyError:
            print(f"  Key not found: {args.key}")
            sys.exit(1)


def cmd_card(args: argparse.Namespace) -> None:
    _init()
    import db

    if args.card_action == "list":
        cards = db.get_all_cards()
        if not cards:
            print("  No cards registered. Cards are auto-discovered on import/fetch.")
            return
        for c in cards:
            nick = c.get("nickname") or "(none)"
            cats = db.get_card_categories(c["card"])
            cat_str = ", ".join(cats) if cats else "—"
            print(f"  {c['card']}  {nick:<20s}  [{cat_str}]")

    elif args.card_action == "set":
        db.upsert_card(args.card_number, args.nickname)
        display = db.get_card_display(args.card_number)
        print(f"  Updated: {display}")

    elif args.card_action == "remove":
        db.delete_card(args.card_number)
        print(f"  Removed card {args.card_number}")


# ---------------------------------------------------------------------------
# CLI parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="etherfi",
        description="Ether.fi Cash Expense Tracker",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # login
    sub.add_parser("login", help="Open headed browser for wallet login")

    # scrape
    sub.add_parser("scrape", help="Headless scrape + store data")

    # import
    p_import = sub.add_parser("import", help="Import CSV file into DB")
    p_import.add_argument("file", help="Path to Ether.fi CSV export")

    # report
    p_report = sub.add_parser("report", help="Generate reports")
    p_report.add_argument(
        "report_type",
        choices=["latest", "daily", "monthly"],
        help="latest=unreported txns, daily=today's txns, monthly=month summary",
    )
    p_report.add_argument("--year", type=int, default=None)
    p_report.add_argument("--month", type=int, default=None)
    p_report.add_argument("--no-send", action="store_true", help="Skip notifications")

    # bot
    sub.add_parser("bot", help="Start Discord bot (long-running)")

    # gui
    sub.add_parser("gui", help="Launch configuration dashboard (Streamlit)")

    # config
    p_config = sub.add_parser("config", help="View/set config stored in DB")
    config_sub = p_config.add_subparsers(dest="config_action", required=True)
    config_sub.add_parser("list", help="List all config values")
    p_get = config_sub.add_parser("get", help="Get a config value")
    p_get.add_argument("key")
    p_set = config_sub.add_parser("set", help="Set a config value")
    p_set.add_argument("key")
    p_set.add_argument("value")

    # card
    p_card = sub.add_parser("card", help="Manage card nicknames")
    card_sub = p_card.add_subparsers(dest="card_action", required=True)
    card_sub.add_parser("list", help="List all cards with categories")
    p_card_set = card_sub.add_parser("set", help="Add/update a card nickname")
    p_card_set.add_argument("card_number", help="Card last 4 digits")
    p_card_set.add_argument("--nickname", "-n", default=None, help="Display name")
    p_card_rm = card_sub.add_parser("remove", help="Remove a card")
    p_card_rm.add_argument("card_number", help="Card last 4 digits")

    return parser


def cli() -> None:
    parser = build_parser()
    args = parser.parse_args()

    dispatch = {
        "login": cmd_login,
        "scrape": cmd_scrape,
        "import": cmd_import,
        "report": cmd_report,
        "bot": cmd_bot,
        "gui": cmd_gui,
        "config": cmd_config,
        "card": cmd_card,
    }
    dispatch[args.command](args)


if __name__ == "__main__":
    cli()
