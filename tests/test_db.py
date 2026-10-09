import sqlite3
import threading
from pathlib import Path

import pytest

from video_agent.db import Database


EXPECTED = {
    "assets",
    "tasks",
    "plan_versions",
    "generations",
    "generation_shots",
    "artifacts",
    "work_items",
    "idempotency",
}


def test_database_creates_contract_tables(tmp_path: Path):
    database = Database(tmp_path)
    assert database.path == tmp_path / "agent.sqlite"
    with database.transaction() as conn:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    assert EXPECTED <= {row["name"] for row in rows}


def test_transaction_rolls_back(tmp_path: Path):
    database = Database(tmp_path)
    with pytest.raises(RuntimeError):
        with database.transaction() as conn:
            conn.execute(
                "INSERT INTO assets (asset_id, kind, filename, media_type, byte_size, relative_path, created_at) VALUES ('a', 'image', 'a.png', 'image/png', 1, 'assets/a/a.png', 't')"
            )
            raise RuntimeError("boom")
    with database.transaction() as conn:
        count = conn.execute("SELECT COUNT(*) AS c FROM assets").fetchone()["c"]
    assert count == 0


def test_another_thread_can_write(tmp_path: Path):
    database = Database(tmp_path)
    errors: list[BaseException] = []

    def write() -> None:
        try:
            with database.transaction() as conn:
                conn.execute(
                    "INSERT INTO assets (asset_id, kind, filename, media_type, byte_size, relative_path, created_at) VALUES ('b', 'image', 'b.png', 'image/png', 1, 'assets/b/b.png', 't')"
                )
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=write)
    thread.start()
    thread.join()
    assert errors == []
    with database.transaction() as conn:
        count = conn.execute("SELECT COUNT(*) AS c FROM assets").fetchone()["c"]
    assert count == 1
    assert sqlite3.connect(database.path).execute("PRAGMA query_only").fetchone() is not None
