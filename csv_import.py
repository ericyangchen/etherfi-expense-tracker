"""Import transactions from an Ether.fi CSV export into the database."""

from __future__ import annotations

import csv
import re
import sys
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

import db


_JS_DATE_RE = re.compile(
    r"^[A-Z][a-z]{2}\s+"       # Day name
    r"([A-Z][a-z]{2}\s+\d{1,2}\s+\d{4})\s+"  # Mon DD YYYY
    r"(\d{2}:\d{2}:\d{2})\s+"  # HH:MM:SS
    r"GMT([+-]\d{4})"           # offset
)


def _normalize_timestamp(raw: str) -> str:
    """Normalize any timestamp to ISO 8601 truncated to whole seconds.

    Handles JS Date.toString() and ISO 8601 (with or without sub-second
    precision) so the dedup key is stable across different CSV formats.
    """
    m = _JS_DATE_RE.match(raw)
    if m:
        clean = f"{m.group(1)} {m.group(2)} {m.group(3)}"
        dt = datetime.strptime(clean, "%b %d %Y %H:%M:%S %z")
    else:
        try:
            dt = datetime.fromisoformat(raw)
        except ValueError:
            return raw
    return dt.replace(microsecond=0).isoformat()


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

            ts_normalized = _normalize_timestamp(timestamp)
            txn = {
                "timestamp": ts_normalized,
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
                "dedup_key": db.make_dedup_key(ts_normalized, amount_usd_raw, description),
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
