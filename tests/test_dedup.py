from decimal import Decimal

import psycopg

import db
from tests.conftest import TEST_DSN


def test_db_fixture_starts_empty(db_mod):
    assert db_mod.get_transaction_count() == 0


# ---------------------------------------------------------------------------
# Task 2: dedup key + type-family (pure)
# ---------------------------------------------------------------------------


def test_type_family_normalizes_pending_topup():
    assert db.type_family("pending_topup") == "topup"
    assert db.type_family("topup") == "topup"
    assert db.type_family("swap") == "swap"


def test_card_key_ignores_amount_and_description():
    k1 = db.make_dedup_key("8732", "card_spend", "2026-05-25T02:17:46+00:00", "187.16", "UEP*CHI CHICKEN")
    k2 = db.make_dedup_key("8732", "card_spend", "2026-05-25T02:17:46+00:00", "215.16", "UEP*CHI CHICKEN")
    assert k1 == k2  # amount drift does not change the key


def test_card_key_distinguishes_type():
    spend = db.make_dedup_key("8732", "card_spend", "2026-05-25T02:17:46+00:00", "10.00", "X")
    refund = db.make_dedup_key("8732", "card_refund", "2026-05-25T02:17:46+00:00", "10.00", "X")
    assert spend != refund


def test_funding_key_collapses_topup_family_same_second():
    pending = db.make_dedup_key("", "pending_topup", "2026-05-04T05:19:26+00:00", "1600.00", "USDT")
    final = db.make_dedup_key("", "topup", "2026-05-04T05:19:26+00:00", "1600.00", "USDT")
    assert pending == final  # same family + amount + desc + second


# ---------------------------------------------------------------------------
# Task 3: two-path upsert
# ---------------------------------------------------------------------------


def _txn(db, *, card, type_, ts, amount, desc, status):
    return {
        "timestamp": ts,
        "type": type_,
        "description": desc,
        "status": status,
        "amount_usd": Decimal(amount),
        "card": card,
        "card_holder": None,
        "original_amount": None,
        "original_currency": None,
        "cashback": None,
        "category": None,
        "dedup_key": db.make_dedup_key(card, type_, ts, amount, desc),
    }


def test_amount_drift_updates_same_row(db_mod):
    db = db_mod
    db.upsert_transaction(_txn(db, card="8732", type_="card_spend",
        ts="2026-05-25T02:17:46+00:00", amount="187.16", desc="UEP*CHI CHICKEN", status="PENDING"))
    db.upsert_transaction(_txn(db, card="8732", type_="card_spend",
        ts="2026-05-25T02:17:46+00:00", amount="215.16", desc="UEP*CHI CHICKEN", status="CLEARED"))
    rows = db.get_recent_transactions(10)
    assert len(rows) == 1
    assert rows[0]["status"] == "CLEARED"
    assert rows[0]["amount_usd"] == Decimal("215.16")


def test_description_drift_updates_same_row(db_mod):
    db = db_mod
    db.upsert_transaction(_txn(db, card="8732", type_="card_spend",
        ts="2026-05-17T03:05:29+00:00", amount="50.67", desc="UBER   *TRIP", status="PENDING"))
    db.upsert_transaction(_txn(db, card="8732", type_="card_spend",
        ts="2026-05-17T03:05:29+00:00", amount="50.67", desc="UBR* PENDING.UBER.COM", status="CLEARED"))
    rows = db.get_recent_transactions(10)
    assert len(rows) == 1
    assert rows[0]["description"] == "UBR* PENDING.UBER.COM"
    assert rows[0]["status"] == "CLEARED"


def test_topup_timestamp_drift_merges(db_mod):
    db = db_mod
    db.upsert_transaction(_txn(db, card="", type_="pending_topup",
        ts="2026-05-04T05:19:26+00:00", amount="1600.00", desc="USDT", status=""))
    db.upsert_transaction(_txn(db, card="", type_="topup",
        ts="2026-05-04T05:19:27+00:00", amount="1600.00", desc="USDT", status=""))
    rows = db.get_recent_transactions(10)
    assert len(rows) == 1
    assert rows[0]["type"] == "topup"  # upgraded to the more-final type


def test_distinct_funding_same_day_not_merged(db_mod):
    db = db_mod
    db.upsert_transaction(_txn(db, card="", type_="topup",
        ts="2026-05-04T05:19:26+00:00", amount="1600.00", desc="USDT", status=""))
    db.upsert_transaction(_txn(db, card="", type_="topup",
        ts="2026-05-04T09:30:00+00:00", amount="1600.00", desc="USDT", status=""))
    assert db.get_transaction_count() == 2  # >5s apart → separate


# ---------------------------------------------------------------------------
# Task 4: merge migration
# ---------------------------------------------------------------------------


def _raw_insert(txn):
    """Insert bypassing dedup logic, to simulate legacy duplicate rows."""
    cols = ("timestamp", "type", "description", "status", "amount_usd", "card",
            "original_amount", "cashback", "dedup_key")
    with psycopg.connect(TEST_DSN) as conn:
        conn.execute(
            f"INSERT INTO transactions ({','.join(cols)}) VALUES "
            "(%(timestamp)s,%(type)s,%(description)s,%(status)s,%(amount_usd)s,"
            "%(card)s,%(original_amount)s,%(cashback)s,%(dedup_key)s)",
            {**txn, "original_amount": None, "cashback": None},
        )


def test_merge_collapses_existing_card_duplicates(db_mod):
    db = db_mod
    _raw_insert({"timestamp": "2026-05-25T02:17:46+00:00", "type": "card_spend",
        "description": "UEP*CHI CHICKEN", "status": "PENDING", "amount_usd": Decimal("187.16"),
        "card": "8732", "dedup_key": "legacy_a"})
    _raw_insert({"timestamp": "2026-05-25T02:17:46+00:00", "type": "card_spend",
        "description": "UEP*CHI CHICKEN", "status": "CLEARED", "amount_usd": Decimal("215.16"),
        "card": "8732", "dedup_key": "legacy_b"})
    deleted = db.merge_duplicate_transactions()
    assert deleted == 1
    rows = db.get_recent_transactions(10)
    assert len(rows) == 1
    assert rows[0]["status"] == "CLEARED"          # CLEARED survives
    assert rows[0]["amount_usd"] == Decimal("215.16")


def test_merge_preserves_reported_at(db_mod):
    db = db_mod
    _raw_insert({"timestamp": "2026-05-25T02:17:46+00:00", "type": "card_spend",
        "description": "X", "status": "PENDING", "amount_usd": Decimal("10.00"),
        "card": "8732", "dedup_key": "leg_p"})
    with psycopg.connect(TEST_DSN) as conn:
        conn.execute("UPDATE transactions SET reported_at = NOW() WHERE dedup_key='leg_p'")
    _raw_insert({"timestamp": "2026-05-25T02:17:46+00:00", "type": "card_spend",
        "description": "X", "status": "CLEARED", "amount_usd": Decimal("12.00"),
        "card": "8732", "dedup_key": "leg_c"})
    db.merge_duplicate_transactions()
    rows = db.get_recent_transactions(10)
    assert len(rows) == 1
    assert rows[0]["reported_at"] is not None      # not re-reported later


def test_merge_collapses_funding_within_5s(db_mod):
    db = db_mod
    _raw_insert({"timestamp": "2026-05-04T05:19:26+00:00", "type": "pending_topup",
        "description": "USDT", "status": "", "amount_usd": Decimal("1600.00"),
        "card": "", "dedup_key": "leg_pt"})
    _raw_insert({"timestamp": "2026-05-04T05:19:27+00:00", "type": "topup",
        "description": "USDT", "status": "", "amount_usd": Decimal("1600.00"),
        "card": "", "dedup_key": "leg_t"})
    deleted = db.merge_duplicate_transactions()
    assert deleted == 1
    rows = db.get_recent_transactions(10)
    assert len(rows) == 1
    assert rows[0]["type"] == "topup"
