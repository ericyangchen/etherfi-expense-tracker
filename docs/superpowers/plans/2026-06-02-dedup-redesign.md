# Dedup Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a transaction's dedup identity stable across the PENDING → CLEARED lifecycle so status updates land on the same row instead of creating duplicates.

**Architecture:** Discriminate by whether a row has a card. Card-bearing rows use a deterministic key `SHA256(card | type | ts_to_second)` and treat amount/description/status as mutable (atomic `ON CONFLICT`). Card-less funding rows merge via a ±5s tolerance lookup on `(type_family, amount, description)`. A one-time migration merges existing duplicates and recomputes keys.

**Tech Stack:** Python 3.11, psycopg 3, PostgreSQL (Docker), pytest.

---

## File Structure

- `db.py` (modify) — dedup-key construction, two-path upsert, merge migration, recompute
- `csv_import.py` (modify) — `_build_txn` computes the new dedup key
- `main.py` (modify) — new `dedup-migrate` CLI command; scrape/import call the new merge
- `tests/conftest.py` (create) — pytest fixture providing an isolated `etherfi_test` DB
- `tests/test_dedup.py` (create) — unit + DB-backed tests for the new dedup behavior
- `pyproject.toml` (modify) — add `pytest` dev dependency

---

## Task 1: Test infrastructure (isolated test DB)

**Files:**
- Modify: `pyproject.toml`
- Create: `tests/conftest.py`
- Create: `tests/__init__.py` (empty)

- [ ] **Step 1: Add pytest dev dependency and install**

Add to `pyproject.toml` after the `dependencies = [...]` block:

```toml
[dependency-groups]
dev = ["pytest"]
```

Run: `.venv/bin/pip install pytest`
Expected: pytest installs successfully.

- [ ] **Step 2: Create the test DB once**

Run:
```bash
docker exec personal-expense-postgres-1 psql -U etherfi -d postgres -c "CREATE DATABASE etherfi_test" 2>&1 | grep -v "already exists" || true
```
Expected: creates `etherfi_test` (or no-op if it exists).

- [ ] **Step 3: Write `tests/__init__.py` (empty) and `tests/conftest.py`**

`tests/__init__.py`: empty file.

`tests/conftest.py`:
```python
import os

import psycopg
import pytest

TEST_DSN = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://etherfi:etherfi_local@localhost:5432/etherfi_test",
)


@pytest.fixture()
def db_mod(monkeypatch):
    """Point the db module at the isolated test database, with a clean schema."""
    import config
    import db

    monkeypatch.setattr(config, "DATABASE_URL", TEST_DSN)
    db.init_db()
    with psycopg.connect(TEST_DSN) as conn:
        conn.execute(
            "TRUNCATE transactions, cards, card_categories, categories RESTART IDENTITY CASCADE"
        )
    return db
```

- [ ] **Step 4: Verify the fixture wiring with a smoke test**

Append to `tests/test_dedup.py` (create it):
```python
def test_db_fixture_starts_empty(db_mod):
    assert db_mod.get_transaction_count() == 0
```

Run: `.venv/bin/pytest tests/test_dedup.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml tests/__init__.py tests/conftest.py tests/test_dedup.py
git commit -m "test: add pytest + isolated test-db fixture"
```

---

## Task 2: Type-family helpers + new dedup key

**Files:**
- Modify: `db.py` (the dedup-key section, ~lines 282-295)
- Test: `tests/test_dedup.py`

- [ ] **Step 1: Write failing unit tests**

Append to `tests/test_dedup.py`:
```python
import db


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
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_dedup.py -k "type_family or card_key or funding_key" -v`
Expected: FAIL (`type_family` not defined; `make_dedup_key` signature mismatch).

- [ ] **Step 3: Implement in `db.py`**

Replace the existing dedup-key section (the `make_dedup_key` definition, ~lines 287-295) with:

```python
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
    from datetime import datetime, timezone

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
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/test_dedup.py -k "type_family or card_key or funding_key" -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add db.py tests/test_dedup.py
git commit -m "feat: stable dedup key from card+type / funding-family"
```

---

## Task 3: Two-path upsert (card ON CONFLICT + funding ±5s)

**Files:**
- Modify: `db.py` (`_UPSERT_SQL` ~lines 302-319, `upsert_transaction`/`upsert_transactions` ~lines 322-342)
- Modify: `csv_import.py` (`_build_txn` line 115)
- Test: `tests/test_dedup.py`

- [ ] **Step 1: Write failing DB tests for all four drift scenarios**

Append to `tests/test_dedup.py`:
```python
from decimal import Decimal


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
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_dedup.py -k "drift or funding" -v`
Expected: FAIL (amount/description drift create 2 rows; topup drift creates 2 rows).

- [ ] **Step 3: Update `_UPSERT_SQL` to also update description**

Replace `_UPSERT_SQL` (~lines 302-319) with:
```python
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
```

- [ ] **Step 4: Replace `upsert_transaction` / `upsert_transactions` with the two-path version**

Replace both functions (~lines 322-342) with:
```python
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
```

- [ ] **Step 5: Update `csv_import._build_txn` to compute the new key**

In `csv_import.py`, replace line 115:
```python
        "dedup_key": db.make_dedup_key(ts_normalized, amount_raw_str, description),
```
with:
```python
        "dedup_key": db.make_dedup_key(
            row_norm.get("card", "").strip(),
            row_norm.get("type", "").strip(),
            ts_normalized,
            amount_raw_str,
            description,
        ),
```

- [ ] **Step 6: Run to verify pass**

Run: `.venv/bin/pytest tests/test_dedup.py -v`
Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git add db.py csv_import.py tests/test_dedup.py
git commit -m "feat: two-path upsert (card ON CONFLICT + funding ±5s merge)"
```

---

## Task 4: Merge migration + recompute keys

**Files:**
- Modify: `db.py` (`recompute_dedup_keys` ~lines 557-577; replace `deduplicate_transactions` ~lines 580-605)
- Test: `tests/test_dedup.py`

- [ ] **Step 1: Write failing test for the merge migration**

Append to `tests/test_dedup.py`:
```python
import psycopg
from tests.conftest import TEST_DSN


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
    # Legacy rows with OLD-style distinct keys (amount drift)
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
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_dedup.py -k merge -v`
Expected: FAIL (`merge_duplicate_transactions` not defined / old behavior).

- [ ] **Step 3: Replace `deduplicate_transactions` with `merge_duplicate_transactions`**

Replace the whole `deduplicate_transactions` function (~lines 580-605) with:
```python
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
```

- [ ] **Step 4: Update `recompute_dedup_keys` to the new signature**

In `recompute_dedup_keys` (~lines 557-577), change the SELECT and the key call:
- Change the SELECT to also fetch `card` and `type`:
```python
        rows = conn.execute(
            "SELECT id, card, type, timestamp, amount_usd, description FROM transactions"
        ).fetchall()
```
- Replace the `new_key = make_dedup_key(...)` line with:
```python
            new_key = make_dedup_key(row["card"], row["type"], ts, amt, row["description"])
```

- [ ] **Step 5: Run to verify pass**

Run: `.venv/bin/pytest tests/test_dedup.py -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add db.py tests/test_dedup.py
git commit -m "feat: merge_duplicate_transactions migration + new recompute keys"
```

---

## Task 5: CLI wiring + replace old dedup calls

**Files:**
- Modify: `main.py` (`cmd_scrape` line 53, `cmd_import` line 71, new `cmd_dedup_migrate`, parser, dispatch)

- [ ] **Step 1: Replace `deduplicate_transactions()` calls in scrape/import**

In `main.py`, change line 53 (`cmd_scrape`) and line 71 (`cmd_import`):
```python
    dupes = db.deduplicate_transactions()
```
→
```python
    dupes = db.merge_duplicate_transactions()
```

- [ ] **Step 2: Add `cmd_dedup_migrate` with backup + dry-run**

Add this function in `main.py` after `cmd_card`:
```python
def cmd_dedup_migrate(args: argparse.Namespace) -> None:
    _init()
    import db

    with db.get_conn() as conn:
        if not args.execute:
            rows = conn.execute("SELECT * FROM transactions ORDER BY id").fetchall()
            print(f"[dedup-migrate] DRY RUN over {len(rows)} rows.")
            print("[dedup-migrate] Re-run with --execute to back up and apply.")
            return

        conn.execute("DROP TABLE IF EXISTS transactions_backup")
        conn.execute("CREATE TABLE transactions_backup AS SELECT * FROM transactions")
        print("[dedup-migrate] Backup written to table transactions_backup.")

    deleted = db.merge_duplicate_transactions()
    rekeyed = db.recompute_dedup_keys()
    print(f"[dedup-migrate] Merged/deleted {deleted} duplicate row(s); rekeyed {rekeyed}.")
```

- [ ] **Step 3: Register the subcommand in `build_parser`**

In `build_parser`, before `return parser`:
```python
    # dedup-migrate
    p_migrate = sub.add_parser("dedup-migrate", help="One-time: merge legacy duplicates + rekey")
    p_migrate.add_argument("--execute", action="store_true",
                           help="Back up + apply (default is dry-run)")
```

And add to the `dispatch` dict in `cli()`:
```python
        "dedup-migrate": cmd_dedup_migrate,
```

- [ ] **Step 4: Verify CLI parses (dry run, no DB writes)**

Run: `DATABASE_URL=postgresql://etherfi:etherfi_local@localhost:5432/etherfi_test .venv/bin/python main.py dedup-migrate`
Expected: prints a DRY RUN line, no errors.

- [ ] **Step 5: Run the full test suite**

Run: `.venv/bin/pytest -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add main.py
git commit -m "feat: dedup-migrate CLI command; scrape/import use merge"
```

---

## Task 6: Run the migration on the live database (gated)

**Files:** none (operational step)

- [ ] **Step 1: Dry run against the live DB and review counts**

Run: `.venv/bin/python main.py dedup-migrate`
Expected: DRY RUN summary. Review with the owner.

- [ ] **Step 2: Capture pre-migration duplicate evidence**

Run the May duplicate-cluster query (from the investigation) and save the output, so the result is verifiable afterward.

- [ ] **Step 3: Execute with backup (requires owner go-ahead)**

Run: `.venv/bin/python main.py dedup-migrate --execute`
Expected: "Backup written…", then merged/deleted + rekeyed counts. For the current data this should remove the 4 known May duplicates (ids 31609, 26913, 29957, 22053) plus any historical ones.

- [ ] **Step 4: Verify no duplicates remain for May and totals look right**

Run the self-join + group-by queries for May again.
Expected: zero PENDING/CLEARED duplicate pairs (the two genuinely-pending 05-31 rows remain untouched).

- [ ] **Step 5: Verify the backup row count vs. live count difference equals deletions**

Run:
```bash
docker exec personal-expense-postgres-1 psql -U etherfi -d etherfi -c "
SELECT (SELECT COUNT(*) FROM transactions_backup) AS before,
       (SELECT COUNT(*) FROM transactions) AS after;"
```
Expected: `before - after` equals the deleted count reported in Step 3.

---

## Self-Review Notes

- **Spec coverage:** identity rules (Task 2), two-path upsert + description update (Task 3),
  ±5s funding merge (Task 3 + Task 4), migration with backup/dry-run (Task 5/6), reported_at
  preservation (Task 4), test infra (Task 1). All spec sections mapped.
- **Type consistency:** `type_family`, `type_family_members`, `more_final_type`,
  `merge_duplicate_transactions`, `_pick_survivor`, `make_dedup_key(card, type_, timestamp,
  amount_usd, description)` used consistently across tasks and call sites (`csv_import`,
  `recompute_dedup_keys`).
- **Rollback:** `transactions_backup` table is the recovery path if the live migration misbehaves.
