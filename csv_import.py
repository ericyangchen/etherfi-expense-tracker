"""Import transactions from an Ether.fi CSV export into the database."""

from __future__ import annotations

import csv
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

import db


def _parse_decimal(value: str) -> Decimal | None:
    try:
        return Decimal(value.strip()) if value.strip() else None
    except InvalidOperation:
        return None


def parse_csv(filepath: str | Path) -> list[dict]:
    rows: list[dict] = []
    with open(filepath, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            description = row["description"].strip()
            timestamp = row["timestamp"].strip()
            amount_usd_raw = row["amount USD"].strip()
            status = row["status"].strip()

            amount_usd = _parse_decimal(amount_usd_raw)
            if amount_usd is None:
                continue

            txn = {
                "timestamp": timestamp,
                "type": row["type"].strip(),
                "description": description,
                "status": status,
                "amount_usd": amount_usd,
                "card": row["card"].strip(),
                "card_holder": row.get("card holder name", "").strip() or None,
                "original_amount": _parse_decimal(row.get("original amount", "")),
                "original_currency": row.get("original currency", "").strip() or None,
                "cashback": _parse_decimal(row.get("cashback earned", "")),
                "category": row.get("category", "").strip() or None,
                "dedup_key": db.make_dedup_key(timestamp, amount_usd_raw, description),
            }
            rows.append(txn)
    return rows


def import_csv(filepath: str | Path) -> int:
    """Parse CSV and upsert all rows. Returns number of rows affected."""
    txns = parse_csv(filepath)
    count = db.upsert_transactions(txns)
    return count


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python csv_import.py <csv_file>")
        sys.exit(1)
    db.init_db()
    filepath = sys.argv[1]
    affected = import_csv(filepath)
    total = len(parse_csv(filepath))
    print(f"Imported {total} transactions ({affected} new/updated)")
