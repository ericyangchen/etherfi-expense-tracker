# Dedup Redesign — Stable Identity Across PENDING → CLEARED

**Date:** 2026-06-02
**Status:** Approved (design)

## Problem

Transactions are deduplicated with `dedup_key = SHA256(timestamp | amount | description)`,
and the whole pipeline relies on `INSERT ... ON CONFLICT (dedup_key) DO UPDATE` to sync a
transaction's status as it moves PENDING → CLEARED. But **all three** fields in the key are
mutable during settlement:

1. **amount** changes when a tip / final settlement differs from the auth hold
2. **description** changes when the merchant descriptor is finalized
3. **timestamp** drifts ~1s for topups (`pending_topup` vs `topup`)

When any field changes, the recomputed key no longer matches the existing row, so the upsert
INSERTs a new row instead of UPDATEing — leaving a stale PENDING row *and* a CLEARED row for the
same real-world transaction. Both are counted in reports (reports only exclude `CANCELLED`), and
the transaction can be reported twice.

### Evidence (May 2026)

| Cluster | Stale row (PENDING) | Final row | Drifted field |
|---|---|---|---|
| USDT topup $1600 | 31609 `pending_topup` 05:19:26 | 22100 `topup` 05:19:27 | timestamp +1s |
| UEP*CHI CHICKEN | 26913 `PENDING $187.16` | 28431 `CLEARED $215.16` | amount (tip) |
| YOON HAEUNDAE GALBI | 29957 `PENDING $125.21` | 31521 `CLEARED $145.91` | amount (tip) |
| UBER | 22053 `UBER *TRIP $50.67` | 23977 `UBR* PENDING.UBER.COM $50.67` | description |

The prior fix (commit `20c1183`) only normalized timestamp formatting / sub-second drift; it could
not catch amount or description drift, nor ≥1s timestamp drift.

## Design

Discriminate by **whether the row has a card** (robust to new `type` values).

### Identity rules

**Card-bearing rows** (`card_spend`, `card_refund`, `physical_card_refund` — anything with a card):
- Deterministic key = `SHA256(card | type | timestamp_truncated_to_second)`
- `type` does not drift for card txns, so including it prevents a same-second spend/refund collision
- **Mutable fields** (updated on conflict): `amount_usd`, `description`, `status`,
  `original_amount`, `cashback`
- Accepted risk: two genuinely distinct charges on the same card in the same second merge into one
  (extremely rare; explicitly accepted by the owner)

**Card-less rows** (`topup`, `pending_topup`, `swap`, `physical_card_order`):
- Type-family normalization: `{topup, pending_topup} → topup`; others unchanged
- Merge via **±5s tolerance lookup**: before insert, find an existing row with same
  `type_family` + same `amount_usd` + same `description` + `|Δtimestamp| ≤ 5s`; if found, UPDATE it
  (upgrade `type` to the more-final value, refresh `status`/`updated_at`), else INSERT
- A deterministic placeholder key (`SHA256(type_family | amount | description | ts_to_second)`)
  is still stored to satisfy `UNIQUE NOT NULL` and block exact-duplicate inserts

### Code changes

- **`db.py`**
  - `make_dedup_key(...)` rewritten to use only stable fields, with a `_TYPE_FAMILY` map + helper
  - `_UPSERT_SQL`: add `description = EXCLUDED.description` to the `DO UPDATE SET` clause; widen the
    `WHERE` guard to cover description changes
  - `upsert_transaction` / `upsert_transactions`: two paths — card rows use the atomic
    `ON CONFLICT` path; card-less rows use the ±5s SELECT-then-UPDATE/INSERT path
  - `merge_duplicate_transactions()`: one-time migration that merges existing duplicates under the
    new rules (replaces `deduplicate_transactions`)
- **`csv_import.py`**
  - `_build_txn`: compute `dedup_key` via the new signature (pass card/type/timestamp)

### Migration of existing data

1. **Back up** first: `CREATE TABLE transactions_backup AS SELECT * FROM transactions`
2. **Dry-run** the merge: print which rows will be deleted and which survive; owner confirms
3. **Merge**:
   - Card rows: group by `(card, type, ts_to_second)`; keep the most-final row (`CLEARED` >
     `PENDING`; tie-break on newer `updated_at` / lower `id`), preserve any non-null `reported_at`,
     delete the rest
   - Card-less rows: cluster by `(type_family, amount, description)` within ≤5s; keep the more-final
     row (`topup` > `pending_topup`)
4. **Recompute** keys with the new scheme; survivors no longer collide

Single-threaded scraper/import means the SELECT-then-write funding path has no concurrency race.

### Side effect

Fixes double-reporting: once status updates land on the same row, `reported_at` is preserved and a
settled transaction is not reported a second time.

## Testing

- New dev dependency: `pytest`
- Tests run against a dedicated `etherfi_test` database (never the live DB)
- Pure-function unit tests: `make_dedup_key` stability, type-family normalization
- DB-backed tests reproducing all four drift scenarios (amount, description, timestamp, type),
  asserting one surviving row with final values after re-upsert
- A test for `merge_duplicate_transactions()` seeding duplicates and asserting the merge result

## Out of scope

- Changing the report queries themselves
- Any change to the scraper's browser automation
