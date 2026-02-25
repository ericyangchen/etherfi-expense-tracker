"""Analytics engine with many-to-many category support and multiple report formats."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import db


@dataclass
class CardSummary:
    card: str
    display_name: str
    categories: list[str]
    total: Decimal
    txn_count: int
    top_merchants: list[dict]


@dataclass
class CategorySummary:
    name: str
    total: Decimal
    cards: list[CardSummary]


@dataclass
class MonthlySummary:
    year: int
    month: int
    grand_total: Decimal
    categories: list[CategorySummary]
    uncategorized_cards: list[CardSummary]
    overall_top_merchants: list[dict]


# ---------------------------------------------------------------------------
# Monthly summary
# ---------------------------------------------------------------------------

def get_monthly_summary(
    year: int | None = None,
    month: int | None = None,
    top_limit: int = 10,
) -> MonthlySummary:
    now = datetime.now()
    if year is None:
        year = now.year
    if month is None:
        month = now.month

    card_totals = db.get_monthly_totals_by_card(year, month)

    card_summaries: dict[str, CardSummary] = {}
    for row in card_totals:
        categories = db.get_card_categories(row["card"])
        top = db.get_top_merchants(
            year, month, card=row["card"], limit=top_limit
        )
        cs = CardSummary(
            card=row["card"],
            display_name=row.get("display_name") or row["card"],
            categories=categories,
            total=row["total"],
            txn_count=row["txn_count"],
            top_merchants=top,
        )
        card_summaries[row["card"]] = cs

    all_categories = db.get_all_categories()
    cat_summaries: list[CategorySummary] = []
    categorized_cards: set[str] = set()

    for cat in all_categories:
        cards_in_cat = [
            cs for cs in card_summaries.values()
            if cat["name"] in cs.categories
        ]
        if cards_in_cat:
            cat_total = sum((c.total for c in cards_in_cat), Decimal("0"))
            cat_summaries.append(
                CategorySummary(name=cat["name"], total=cat_total, cards=cards_in_cat)
            )
            categorized_cards.update(c.card for c in cards_in_cat)

    uncategorized = [
        cs for cs in card_summaries.values()
        if cs.card not in categorized_cards
    ]

    grand_total = sum((cs.total for cs in card_summaries.values()), Decimal("0"))
    overall_top = db.get_top_merchants(year, month, limit=top_limit)

    return MonthlySummary(
        year=year,
        month=month,
        grand_total=grand_total,
        categories=cat_summaries,
        uncategorized_cards=uncategorized,
        overall_top_merchants=overall_top,
    )


def format_monthly_report(summary: MonthlySummary) -> str:
    lines: list[str] = []
    header = f"Ether.fi Monthly Summary - {summary.year}/{summary.month:02d}"
    lines.append(header)
    lines.append("=" * len(header))
    lines.append(f"Grand Total: ${summary.grand_total:,.2f}")
    lines.append("")

    for cat in summary.categories:
        lines.append(f"{cat.name} (${cat.total:,.2f}):")
        for cs in cat.cards:
            card_label = f"{cs.display_name} ({cs.card})" if cs.display_name != cs.card else cs.card
            other_cats = [c for c in cs.categories if c != cat.name]
            also_in = f"  (also in: {', '.join(other_cats)})" if other_cats else ""
            lines.append(f"  {card_label}: ${cs.total:,.2f} ({cs.txn_count} txns){also_in}")
            if cs.top_merchants:
                lines.append("    Top:")
                for m in cs.top_merchants:
                    lines.append(f"      - {m['merchant']}: ${m['total']:,.2f}")
        lines.append("")

    if summary.uncategorized_cards:
        uncat_total = sum((c.total for c in summary.uncategorized_cards), Decimal("0"))
        lines.append(f"Uncategorized (${uncat_total:,.2f}):")
        for cs in summary.uncategorized_cards:
            card_label = f"{cs.display_name} ({cs.card})" if cs.display_name != cs.card else cs.card
            lines.append(f"  {card_label}: ${cs.total:,.2f} ({cs.txn_count} txns)")
            if cs.top_merchants:
                lines.append("    Top:")
                for m in cs.top_merchants:
                    lines.append(f"      - {m['merchant']}: ${m['total']:,.2f}")
        lines.append("")

    if summary.overall_top_merchants:
        lines.append("Top Merchants (All):")
        for m in summary.overall_top_merchants:
            lines.append(f"  {m['merchant']}: ${m['total']:,.2f}")

    return "\n".join(lines).rstrip()


# ---------------------------------------------------------------------------
# Daily / latest report
# ---------------------------------------------------------------------------

def format_daily_report(txns: list[dict[str, Any]], title: str | None = None) -> str:
    if not txns:
        return "No new transactions to report."

    if title is None:
        today = datetime.now().strftime("%Y/%m/%d")
        title = f"Ether.fi Daily Report - {today}"

    lines: list[str] = []
    lines.append(title)
    lines.append("=" * len(title))
    lines.append(f"{len(txns)} transaction(s)")
    lines.append("")

    card_map: dict[str, list[dict]] = defaultdict(list)
    for txn in txns:
        card_map[txn["card"]].append(txn)

    for card_id in sorted(card_map.keys()):
        card_txns = card_map[card_id]
        display = db.get_card_display(card_id)
        categories = db.get_card_categories(card_id)
        cat_str = f" [{', '.join(categories)}]" if categories else ""
        lines.append(f"{display}{cat_str}:")
        for t in card_txns:
            desc = t["description"].strip()
            amt = t["amount_usd"]
            status = t["status"].capitalize()
            lines.append(f"  {desc:<30s} ${amt:>10,.2f}  ({status})")
        lines.append("")

    return "\n".join(lines).rstrip()
