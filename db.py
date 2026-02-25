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


def make_dedup_key(timestamp: str, amount_usd: str, description: str) -> str:
    raw = f"{timestamp}|{amount_usd}|{description.strip()}"
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
    amount_usd = EXCLUDED.amount_usd,
    original_amount = EXCLUDED.original_amount,
    cashback = EXCLUDED.cashback,
    updated_at = NOW()
WHERE transactions.status IS DISTINCT FROM EXCLUDED.status
   OR transactions.amount_usd IS DISTINCT FROM EXCLUDED.amount_usd;
"""


def upsert_transaction(txn: dict[str, Any]) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO cards (card) VALUES (%s) ON CONFLICT (card) DO NOTHING",
            (txn["card"],),
        )
        conn.execute(_UPSERT_SQL, txn)


def upsert_transactions(txns: list[dict[str, Any]]) -> int:
    """Upsert a batch of transactions. Auto-registers new cards."""
    count = 0
    with get_conn() as conn:
        for txn in txns:
            conn.execute(
                "INSERT INTO cards (card) VALUES (%s) ON CONFLICT (card) DO NOTHING",
                (txn["card"],),
            )
            result = conn.execute(_UPSERT_SQL, txn)
            count += result.rowcount
    return count


# ---------------------------------------------------------------------------
# Unreported / daily queries
# ---------------------------------------------------------------------------


def get_unreported_transactions() -> list[dict[str, Any]]:
    with get_conn() as conn:
        return conn.execute(
            """SELECT * FROM transactions
               WHERE reported_at IS NULL AND status != 'CANCELLED'
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
               WHERE timestamp >= %s AND timestamp < %s AND status != 'CANCELLED'
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
               WHERE timestamp >= %s AND timestamp < %s AND status != 'CANCELLED'
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
          AND t.status != 'CANCELLED'
        GROUP BY t.card, c.nickname
        ORDER BY total DESC
    """
    with get_conn() as conn:
        return conn.execute(sql, (year, month)).fetchall()


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
        "t.status != 'CANCELLED'",
        "t.type = 'card_spend'",
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
