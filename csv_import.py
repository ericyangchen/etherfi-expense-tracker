"""Import transactions from an Ether.fi CSV export into the database."""

from __future__ import annotations

import csv
import logging
import re
import sys
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from dateutil import parser as dtparser

import db

_log = logging.getLogger(__name__)

# JS Date.toString() — dateutil misreads "GMT+0800" (treats GMT as tz, drops offset)
_JS_DATE_RE = re.compile(
    r"^[A-Z][a-z]{2}\s+"
    r"([A-Z][a-z]{2}\s+\d{1,2}\s+\d{4})\s+"
    r"(\d{2}:\d{2}:\d{2})\s+"
    r"GMT([+-]\d{4})"
)


def _normalize_timestamp(raw: str) -> str:
    """Parse any reasonable datetime string, convert to UTC, truncate to whole seconds.

    Handles JS Date.toString() explicitly (dateutil misparses its GMT±HHMM
    offset), then falls back to dateutil.parser for everything else.
    Always returns a canonical ISO 8601 UTC string so the dedup key is
    format-independent.
    """
    m = _JS_DATE_RE.match(raw)
    if m:
        clean = f"{m.group(1)} {m.group(2)} {m.group(3)}"
        dt = datetime.strptime(clean, "%b %d %Y %H:%M:%S %z")
    else:
        try:
            dt = dtparser.parse(raw)
        except (ValueError, OverflowError):
            _log.warning("Unparseable timestamp, using raw: %r", raw)
            return raw
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _parse_decimal(value) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, (int, float, Decimal)):
        try:
            return Decimal(str(value))
        except InvalidOperation:
            return None
    s = str(value).strip()
    if not s:
        return None
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


def _norm_key(s: str) -> str:
    return s.strip().lower().replace("_", " ")


def _require(row_norm: dict, key: str, headers: list[str]) -> str:
    if key not in row_norm:
        raise KeyError(
            f"Export is missing required column {key!r}. "
            f"Headers found: {headers}"
        )
    return row_norm[key]


def _build_txn(row_norm: dict, headers: list[str]) -> dict | None:
    description = _require(row_norm, "description", headers).strip()
    timestamp = _require(row_norm, "timestamp", headers).strip()
    # New XLSX format has `amount` + `currency`; legacy CSV had `amount usd`.
    if "amount usd" in row_norm:
        amount_raw = row_norm["amount usd"]
        currency = "USD"
    else:
        amount_raw = row_norm.get("amount", "")
        currency = row_norm.get("currency", "USD").strip().upper() or "USD"
    amount_raw_str = str(amount_raw).strip() if amount_raw is not None else ""

    amount_usd = _parse_decimal(amount_raw_str)
    if amount_usd is None:
        return None
    # We only track USD-denominated amounts in `amount_usd`; skip foreign-currency
    # card spends rather than store mixed currencies under one column.
    if currency != "USD":
        _log.info("Skipping non-USD txn (%s %s): %s", amount_raw_str, currency, description)
        return None

    ts_normalized = _normalize_timestamp(timestamp)
    return {
        "timestamp": ts_normalized,
        "type": row_norm.get("type", "").strip(),
        "description": description,
        "status": row_norm.get("status", "").strip(),
        "amount_usd": amount_usd,
        "card": row_norm.get("card", "").strip(),
        "card_holder": row_norm.get("card holder name", "").strip() or None,
        "original_amount": _parse_decimal(row_norm.get("original amount", "")),
        "original_currency": row_norm.get("original currency", "").strip() or None,
        "cashback": _parse_decimal(row_norm.get("cashback earned", "")),
        "category": row_norm.get("category", "").strip() or None,
        "dedup_key": db.make_dedup_key(
            row_norm.get("card", "").strip(),
            row_norm.get("type", "").strip(),
            ts_normalized,
            amount_raw_str,
            description,
        ),
    }


def _is_xlsx(filepath: str | Path) -> bool:
    with open(filepath, "rb") as f:
        return f.read(4) == b"PK\x03\x04"


def _parse_xlsx(filepath: str | Path) -> list[dict]:
    """Parse the Ether.fi XLSX export. Header row is auto-detected by looking
    for a row that contains both 'timestamp' and 'description' cells."""
    import io
    from openpyxl import load_workbook

    # Wrap in BytesIO — openpyxl rejects .csv extensions by name even when the
    # file content is a valid xlsx (Ether.fi mislabels their download).
    buf = io.BytesIO(Path(filepath).read_bytes())
    wb = load_workbook(buf, read_only=True, data_only=True)
    sheet_name = "All Transactions" if "All Transactions" in wb.sheetnames else wb.sheetnames[0]
    ws = wb[sheet_name]

    headers: list[str] | None = None
    rows: list[dict] = []
    for raw_row in ws.iter_rows(values_only=True):
        if headers is None:
            normalized = [_norm_key(str(c)) if c is not None else "" for c in raw_row]
            if "timestamp" in normalized and "description" in normalized:
                headers = normalized
            continue

        row_norm: dict = {}
        for h, v in zip(headers, raw_row):
            if not h:
                continue
            if v is None:
                row_norm[h] = ""
            elif isinstance(v, datetime):
                row_norm[h] = v.isoformat() if v.tzinfo else v.replace(tzinfo=timezone.utc).isoformat()
            else:
                row_norm[h] = v if isinstance(v, (int, float, Decimal)) else str(v)

        if not any(str(v).strip() for v in row_norm.values()):
            continue
        txn = _build_txn(row_norm, headers)
        if txn:
            rows.append(txn)

    if headers is None:
        raise KeyError(
            f"XLSX has no recognizable header row in sheet {sheet_name!r}. "
            f"Expected 'timestamp' and 'description' columns."
        )
    return rows


def _parse_csv_text(filepath: str | Path) -> list[dict]:
    rows: list[dict] = []
    with open(filepath, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        headers = [_norm_key(h) for h in (reader.fieldnames or [])]
        for row in reader:
            row_norm = {_norm_key(k): (v or "") for k, v in row.items() if k is not None}
            txn = _build_txn(row_norm, headers)
            if txn:
                rows.append(txn)
    return rows


def parse_csv(filepath: str | Path) -> list[dict]:
    """Parse an Ether.fi transaction export (legacy CSV or new XLSX)."""
    if _is_xlsx(filepath):
        return _parse_xlsx(filepath)
    return _parse_csv_text(filepath)


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
