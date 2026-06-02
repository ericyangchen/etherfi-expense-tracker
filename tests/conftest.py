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
