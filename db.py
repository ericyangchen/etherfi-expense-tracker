from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Generator

import psycopg
from psycopg.rows import dict_row

import config

# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------


@contextmanager
def get_conn() -> Generator[psycopg.Connection, None, None]:
    with psycopg.connect(config.DATABASE_URL, row_factory=dict_row) as conn:
        yield conn


def _execute(sql: str, params: dict | tuple | None = None) -> None:
    with get_conn() as conn:
        conn.execute(sql, params)


# ---------------------------------------------------------------------------
# Schema bootstrap
# ---------------------------------------------------------------------------

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS transactions (
    id SERIAL PRIMARY KEY,
    timestamp TIMESTAMPTZ NOT NULL,
    type TEXT NOT NULL,
    description TEXT NOT NULL,
    status TEXT NOT NULL,
    amount_usd NUMERIC(12,2) NOT NULL,
    card TEXT NOT NULL,
    card_holder TEXT,
    original_amount NUMERIC(12,2),
    original_currency TEXT,
    cashback NUMERIC(10,4),
    category TEXT,
    dedup_key TEXT UNIQUE NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    reported_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_txn_card_ts ON transactions(card, timestamp);
CREATE INDEX IF NOT EXISTS idx_txn_status ON transactions(status);

CREATE TABLE IF NOT EXISTS cards (
    card TEXT PRIMARY KEY,
    nickname TEXT
);

CREATE TABLE IF NOT EXISTS categories (
    name TEXT PRIMARY KEY,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS card_categories (
    card TEXT NOT NULL,
    category TEXT NOT NULL,
    PRIMARY KEY (card, category)
);

CREATE TABLE IF NOT EXISTS config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

INSERT INTO config (key, value) VALUES
    ('fetch_interval_hours', '24'),
    ('daily_report_hour', '0'),
    ('monthly_report_day', '1'),
    ('notify_channels', '[]'),
    ('etherfi_url', 'https://www.ether.fi/app/cash/safe'),
    ('last_fetch_at', '1970-01-01T00:00:00Z')
ON CONFLICT (key) DO NOTHING;
"""

_MIGRATION_SQL = """
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS reported_at TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS idx_txn_reported ON transactions(reported_at);

CREATE TABLE IF NOT EXISTS categories (
    name TEXT PRIMARY KEY,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS card_categories (
    card TEXT NOT NULL,
    category TEXT NOT NULL,
    PRIMARY KEY (card, category)
);

DO $$ BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'cards' AND column_name = 'category'
    ) THEN
        INSERT INTO categories (name)
        SELECT DISTINCT category FROM cards WHERE category IS NOT NULL
        ON CONFLICT (name) DO NOTHING;

        INSERT INTO card_categories (card, category)
        SELECT card, category FROM cards WHERE category IS NOT NULL
        ON CONFLICT (card, category) DO NOTHING;

        ALTER TABLE cards DROP COLUMN category;
    END IF;
END $$;
"""


def init_db() -> None:
    with get_conn() as conn:
        conn.execute(_SCHEMA_SQL)
        conn.execute(_MIGRATION_SQL)


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def get_config(key: str) -> str:
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM config WHERE key = %s", (key,)).fetchone()
    if row is None:
        raise KeyError(f"Config key not found: {key}")
    return row["value"]


def set_config(key: str, value: str) -> None:
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO config (key, value, updated_at)
               VALUES (%s, %s, NOW())
               ON CONFLICT (key) DO UPDATE
               SET value = EXCLUDED.value, updated_at = NOW()""",
            (key, value),
        )


def get_all_config() -> list[dict[str, Any]]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT key, value, updated_at FROM config ORDER BY key"
        ).fetchall()


def get_last_fetch_at() -> datetime:
    raw = get_config("last_fetch_at")
    return datetime.fromisoformat(raw)


def update_last_fetch_at() -> None:
    set_config("last_fetch_at", datetime.now(timezone.utc).isoformat())


def get_fetch_interval_hours() -> float:
    return float(get_config("fetch_interval_hours"))


# ---------------------------------------------------------------------------
# Card management
# ---------------------------------------------------------------------------


def get_card(card: str) -> dict | None:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM cards WHERE card = %s", (card,)).fetchone()


def get_all_cards() -> list[dict]:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM cards ORDER BY card").fetchall()


def upsert_card(card: str, nickname: str | None) -> None:
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO cards (card, nickname)
               VALUES (%s, %s)
               ON CONFLICT (card) DO UPDATE SET nickname = EXCLUDED.nickname""",
            (card, nickname),
        )


def delete_card(card: str) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM card_categories WHERE card = %s", (card,))
        conn.execute("DELETE FROM cards WHERE card = %s", (card,))


def get_card_display(card: str) -> str:
    info = get_card(card)
    if info and info.get("nickname"):
        return f"{info['nickname']} ({card})"
    return card


# ---------------------------------------------------------------------------
# Category management (many-to-many)
# ---------------------------------------------------------------------------


def create_category(name: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO categories (name) VALUES (%s) ON CONFLICT (name) DO NOTHING",
            (name,),
        )


def delete_category(name: str) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM card_categories WHERE category = %s", (name,))
        conn.execute("DELETE FROM categories WHERE name = %s", (name,))


def get_all_categories() -> list[dict]:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM categories ORDER BY name").fetchall()


def get_card_categories(card: str) -> list[str]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT category FROM card_categories WHERE card = %s ORDER BY category",
            (card,),
        ).fetchall()
    return [r["category"] for r in rows]


def get_cards_in_category(category: str) -> list[dict]:
    with get_conn() as conn:
        return conn.execute(
            """SELECT c.* FROM cards c
               JOIN card_categories cc ON c.card = cc.card
               WHERE cc.category = %s
               ORDER BY c.card""",
            (category,),
        ).fetchall()


def add_card_to_category(card: str, category: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO card_categories (card, category) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (card, category),
        )


def remove_card_from_category(card: str, category: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM card_categories WHERE card = %s AND category = %s",
            (card, category),
        )


def set_category_cards(category: str, cards: list[str]) -> None:
    """Replace all card assignments for a category."""
    with get_conn() as conn:
        conn.execute("DELETE FROM card_categories WHERE category = %s", (category,))
        for card in cards:
            conn.execute(
                "INSERT INTO card_categories (card, category) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (card, category),
            )


# ---------------------------------------------------------------------------
# Dedup key
# ---------------------------------------------------------------------------


# Funding types that share one logical identity across their lifecycle.
_TYPE_FAMILY = {"pending_topup": "topup"}

# Higher = more final; used to choose the surviving type when merging funding rows.
_TYPE_FINALITY = {"pending_topup": 0, "topup": 1}


def type_family(type_: str) -> str:
    return _TYPE_FAMILY.get(type_, type_)


def type_family_members(type_: str) -> list[str]:
    fam = type_family(type_)
    members = {fam}
    members.update(t for t, f in _TYPE_FAMILY.items() if f == fam)
    return sorted(members)


def more_final_type(a: str, b: str) -> str:
    return a if _TYPE_FINALITY.get(a, 1) >= _TYPE_FINALITY.get(b, 1) else b


def _norm_amount(amount_usd: str) -> str:
    from decimal import Decimal, InvalidOperation

    try:
        return f"{Decimal(str(amount_usd).strip()):.2f}"
    except (InvalidOperation, AttributeError):
        return str(amount_usd).strip()


def _ts_to_second(timestamp: str) -> str:
    """Canonical UTC ISO truncated to whole seconds. Input is already a
    normalized ISO string from csv_import._normalize_timestamp; pass through
    but defensively re-truncate if microseconds slipped in."""
    try:
        dt = datetime.fromisoformat(timestamp)
    except (ValueError, TypeError):
        return timestamp
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def make_dedup_key(
    card: str | None,
    type_: str,
    timestamp: str,
    amount_usd: str,
    description: str,
) -> str:
    ts = _ts_to_second(timestamp)
    if card and str(card).strip():
        raw = f"{str(card).strip()}|{type_}|{ts}"
    else:
        raw = f"{type_family(type_)}|{_norm_amount(amount_usd)}|{description.strip()}|{ts}"
    return hashlib.sha256(raw.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Transaction upsert
# ---------------------------------------------------------------------------

_UPSERT_SQL = """
INSERT INTO transactions
    (timestamp, type, description, status, amount_usd,
     card, card_holder, original_amount, original_currency,
     cashback, category, dedup_key)
VALUES
    (%(timestamp)s, %(type)s, %(description)s, %(status)s, %(amount_usd)s,
     %(card)s, %(card_holder)s, %(original_amount)s, %(original_currency)s,
     %(cashback)s, %(category)s, %(dedup_key)s)
ON CONFLICT (dedup_key) DO UPDATE SET
    status = EXCLUDED.status,
    description = EXCLUDED.description,
    amount_usd = EXCLUDED.amount_usd,
    original_amount = EXCLUDED.original_amount,
    cashback = EXCLUDED.cashback,
    updated_at = NOW()
WHERE transactions.status IS DISTINCT FROM EXCLUDED.status
   OR transactions.amount_usd IS DISTINCT FROM EXCLUDED.amount_usd
   OR transactions.description IS DISTINCT FROM EXCLUDED.description;
"""


def _upsert_one(conn, txn: dict[str, Any]) -> int:
    card = (txn.get("card") or "").strip()
    if card:
        conn.execute(
            "INSERT INTO cards (card) VALUES (%s) ON CONFLICT (card) DO NOTHING",
            (card,),
        )
        return conn.execute(_UPSERT_SQL, txn).rowcount
    return _upsert_funding(conn, txn)


def _upsert_funding(conn, txn: dict[str, Any]) -> int:
    members = type_family_members(txn["type"])
    existing = conn.execute(
        """SELECT id, type FROM transactions
           WHERE type = ANY(%s)
             AND amount_usd = %s
             AND description = %s
             AND abs(EXTRACT(EPOCH FROM (timestamp - %s::timestamptz))) <= 5
           ORDER BY id
           LIMIT 1""",
        (members, txn["amount_usd"], txn["description"], txn["timestamp"]),
    ).fetchone()
    if existing:
        conn.execute(
            """UPDATE transactions
               SET type = %s, status = %s, updated_at = NOW()
               WHERE id = %s""",
            (more_final_type(existing["type"], txn["type"]), txn["status"], existing["id"]),
        )
        return 1
    conn.execute(_UPSERT_SQL, txn)
    return 1


def upsert_transaction(txn: dict[str, Any]) -> None:
    with get_conn() as conn:
        _upsert_one(conn, txn)


def upsert_transactions(txns: list[dict[str, Any]]) -> int:
    """Upsert a batch of transactions. Auto-registers new cards."""
    count = 0
    with get_conn() as conn:
        for txn in txns:
            count += _upsert_one(conn, txn)
    return count


# ---------------------------------------------------------------------------
# Unreported / daily queries
# ---------------------------------------------------------------------------


def get_unreported_transactions() -> list[dict[str, Any]]:
    with get_conn() as conn:
        return conn.execute(
            """SELECT * FROM transactions
               WHERE reported_at IS NULL AND status NOT IN ('CANCELLED', 'DECLINED')
               ORDER BY timestamp DESC"""
        ).fetchall()


def mark_as_reported(txn_ids: list[int]) -> None:
    if not txn_ids:
        return
    with get_conn() as conn:
        conn.execute(
            "UPDATE transactions SET reported_at = NOW() WHERE id = ANY(%s)",
            (txn_ids,),
        )


def get_today_transactions() -> list[dict[str, Any]]:
    now = datetime.now().astimezone()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    with get_conn() as conn:
        return conn.execute(
            """SELECT * FROM transactions
               WHERE timestamp >= %s AND timestamp < %s
                 AND status NOT IN ('CANCELLED', 'DECLINED')
               ORDER BY timestamp DESC""",
            (start, end),
        ).fetchall()


def get_transactions_for_date(
    year: int, month: int, day: int
) -> list[dict[str, Any]]:
    """Transactions for a given calendar day (server local time)."""
    start = datetime(year, month, day).astimezone()
    end = start + timedelta(days=1)
    with get_conn() as conn:
        return conn.execute(
            """SELECT * FROM transactions
               WHERE timestamp >= %s AND timestamp < %s
                 AND status NOT IN ('CANCELLED', 'DECLINED')
               ORDER BY timestamp DESC""",
            (start, end),
        ).fetchall()


# ---------------------------------------------------------------------------
# Analytics queries
# ---------------------------------------------------------------------------


def get_transaction_count() -> int:
    with get_conn() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM transactions").fetchone()
    return row["n"] if row else 0


def get_recent_transactions(limit: int = 20) -> list[dict[str, Any]]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM transactions ORDER BY timestamp DESC LIMIT %s",
            (limit,),
        ).fetchall()


def get_monthly_totals_by_card(year: int, month: int) -> list[dict[str, Any]]:
    sql = """
        SELECT t.card,
               COALESCE(c.nickname, t.card) AS display_name,
               SUM(t.amount_usd) AS total,
               COUNT(*) AS txn_count
        FROM transactions t
        LEFT JOIN cards c ON t.card = c.card
        WHERE EXTRACT(YEAR FROM t.timestamp) = %s
          AND EXTRACT(MONTH FROM t.timestamp) = %s
          AND t.status NOT IN ('CANCELLED', 'DECLINED')
          AND t.type IN ('card_spend', 'card_refund', 'physical_card_refund')
        GROUP BY t.card, c.nickname
        ORDER BY total DESC
    """
    with get_conn() as conn:
        return conn.execute(sql, (year, month)).fetchall()


_FUNDING_TYPES = ('topup', 'swap', 'physical_card_order')


def get_monthly_funding(year: int, month: int) -> list[dict[str, Any]]:
    """Non-spend activity (topups, swaps, etc.) for a given month."""
    sql = """
        SELECT type, SUM(amount_usd) AS total, COUNT(*) AS txn_count
        FROM transactions
        WHERE EXTRACT(YEAR FROM timestamp) = %s
          AND EXTRACT(MONTH FROM timestamp) = %s
          AND status NOT IN ('CANCELLED', 'DECLINED')
          AND type = ANY(%s)
        GROUP BY type
        ORDER BY total DESC
    """
    with get_conn() as conn:
        return conn.execute(sql, (year, month, list(_FUNDING_TYPES))).fetchall()


def get_monthly_funding_transactions(year: int, month: int) -> list[dict[str, Any]]:
    """Individual funding transactions for a given month."""
    sql = """
        SELECT *
        FROM transactions
        WHERE EXTRACT(YEAR FROM timestamp) = %s
          AND EXTRACT(MONTH FROM timestamp) = %s
          AND status NOT IN ('CANCELLED', 'DECLINED')
          AND type = ANY(%s)
        ORDER BY timestamp DESC
    """
    with get_conn() as conn:
        return conn.execute(sql, (year, month, list(_FUNDING_TYPES))).fetchall()


def get_top_merchants(
    year: int,
    month: int,
    *,
    card: str | None = None,
    category: str | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    conditions = [
        "EXTRACT(YEAR FROM t.timestamp) = %(year)s",
        "EXTRACT(MONTH FROM t.timestamp) = %(month)s",
        "t.status NOT IN ('CANCELLED', 'DECLINED')",
        "t.type IN ('card_spend', 'card_refund', 'physical_card_refund')",
    ]
    params: dict[str, Any] = {"year": year, "month": month, "limit": limit}
    joins = ""

    if card is not None:
        conditions.append("t.card = %(card)s")
        params["card"] = card

    if category is not None:
        joins = (
            "JOIN card_categories cc ON t.card = cc.card AND cc.category = %(category)s"
        )
        params["category"] = category

    where = " AND ".join(conditions)
    sql = f"""
        SELECT TRIM(t.description) AS merchant, SUM(t.amount_usd) AS total
        FROM transactions t
        {joins}
        WHERE {where}
        GROUP BY TRIM(t.description)
        ORDER BY total DESC
        LIMIT %(limit)s
    """
    with get_conn() as conn:
        return conn.execute(sql, params).fetchall()


# ---------------------------------------------------------------------------
# Migration helpers
# ---------------------------------------------------------------------------


def migrate_seed_cards() -> None:
    """Seed cards table from existing transaction data if empty."""
    with get_conn() as conn:
        count = conn.execute("SELECT COUNT(*) AS n FROM cards").fetchone()
        if count and count["n"] > 0:
            return

        conn.execute(
            """
            INSERT INTO cards (card)
            SELECT DISTINCT card FROM transactions
            ON CONFLICT (card) DO NOTHING
        """
        )

        try:
            raw = conn.execute(
                "SELECT value FROM config WHERE key = 'business_cards'"
            ).fetchone()
            if raw:
                biz_cards = json.loads(raw["value"])
                conn.execute(
                    "INSERT INTO categories (name) VALUES ('Business') ON CONFLICT DO NOTHING"
                )
                for bc in biz_cards:
                    conn.execute(
                        "INSERT INTO card_categories (card, category) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                        (bc, "Business"),
                    )
                conn.execute("DELETE FROM config WHERE key = 'business_cards'")
        except Exception:
            pass


def migrate_mark_existing_reported() -> None:
    """Mark all existing transactions as reported so daily reports start fresh."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE transactions SET reported_at = NOW() WHERE reported_at IS NULL"
        )


def recompute_dedup_keys() -> int:
    """Recompute all dedup keys from stored DB values.

    Ensures consistency after changes to the dedup key algorithm.
    Returns number of rows updated.
    """
    updated = 0
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, card, type, timestamp, amount_usd, description FROM transactions"
        ).fetchall()
        for row in rows:
            ts = row["timestamp"].astimezone(timezone.utc).replace(microsecond=0).isoformat()
            amt = f"{row['amount_usd']:.2f}"
            new_key = make_dedup_key(row["card"], row["type"], ts, amt, row["description"])
            result = conn.execute(
                "UPDATE transactions SET dedup_key = %s WHERE id = %s AND dedup_key IS DISTINCT FROM %s",
                (new_key, row["id"], new_key),
            )
            updated += result.rowcount
    return updated


def merge_duplicate_transactions() -> int:
    """Merge existing duplicate rows under the stable-identity rules.

    Card rows: group by (card, type, second); keep the most-final row
    (CLEARED > others; tie-break newer updated_at, then lower id).
    Funding rows: cluster by (type_family, amount, description) within 5s;
    keep the more-final type. Any non-null reported_at in a group is preserved
    on the survivor so settled transactions are not reported again.
    Returns the number of rows deleted.
    """
    from collections import defaultdict

    deleted = 0
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM transactions ORDER BY id").fetchall()

        card_groups: dict[tuple, list[dict]] = defaultdict(list)
        funding_rows: list[dict] = []
        for r in rows:
            if (r["card"] or "").strip():
                key = (r["card"], r["type"], r["timestamp"].replace(microsecond=0))
                card_groups[key].append(r)
            else:
                funding_rows.append(r)

        groups: list[list[dict]] = [g for g in card_groups.values() if len(g) > 1]

        # Funding: cluster by (family, amount, description), then split on >5s gaps.
        fund_buckets: dict[tuple, list[dict]] = defaultdict(list)
        for r in funding_rows:
            fund_buckets[(type_family(r["type"]), r["amount_usd"], r["description"])].append(r)
        for bucket in fund_buckets.values():
            bucket.sort(key=lambda r: r["timestamp"])
            cluster = [bucket[0]]
            for r in bucket[1:]:
                if (r["timestamp"] - cluster[-1]["timestamp"]).total_seconds() <= 5:
                    cluster.append(r)
                else:
                    if len(cluster) > 1:
                        groups.append(cluster)
                    cluster = [r]
            if len(cluster) > 1:
                groups.append(cluster)

        for group in groups:
            survivor = _pick_survivor(group)
            reported = next((g["reported_at"] for g in group if g["reported_at"]), None)
            loser_ids = [g["id"] for g in group if g["id"] != survivor["id"]]
            if reported and not survivor["reported_at"]:
                conn.execute(
                    "UPDATE transactions SET reported_at = %s WHERE id = %s",
                    (reported, survivor["id"]),
                )
            conn.execute("DELETE FROM transactions WHERE id = ANY(%s)", (loser_ids,))
            deleted += len(loser_ids)
    return deleted


def _pick_survivor(group: list[dict]) -> dict:
    def rank(r: dict) -> tuple:
        cleared = 1 if r["status"] == "CLEARED" else 0
        final_type = _TYPE_FINALITY.get(r["type"], 1)
        updated = r["updated_at"].timestamp() if r["updated_at"] else 0
        return (cleared, final_type, updated, r["id"])

    return max(group, key=rank)
