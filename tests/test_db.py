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


def test_second_open_reads_written_row(tmp_path: Path):
    first = Database(tmp_path)
    with first.transaction() as conn:
        conn.execute(
            "INSERT INTO assets (asset_id, kind, filename, media_type, byte_size, relative_path, created_at) VALUES ('a', 'image', 'a.png', 'image/png', 1, 'assets/a/a.png', 't')"
        )
    second = Database(tmp_path)
    with second.transaction() as conn:
        row = conn.execute("SELECT asset_id, filename FROM assets WHERE asset_id='a'").fetchone()
    assert row["asset_id"] == "a"
    assert row["filename"] == "a.png"


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


def test_injected_database_and_named_mapping_rows(tmp_path: Path):
    from video_agent import VideoAgent
    from video_agent.models import CreateTaskRequest, ProductInfo

    database = Database(tmp_path / "database")
    agent = VideoAgent(tmp_path / "files", database=database, planner=object())
    created = agent.create_task(CreateTaskRequest(product_info=ProductInfo(name="中文瓶子")))
    with database.transaction(write=False) as conn:
        row = conn.execute("SELECT task_id FROM tasks WHERE task_id=:task_id", {"task_id": created.task_id}).fetchone()
    assert dict(row) == {"task_id": created.task_id}
    page = agent.list_tasks_page(limit=1, offset=0)
    assert page.total == 1
    assert page.items[0].product_name == "中文瓶子"
    assert page.limit == 1
    database.close()


def test_runtime_secrets_are_hidden_and_tokens_validated(monkeypatch):
    from video_agent.runtime import RuntimeSettings

    for name, value in {
        "VIDEO_AGENT_DB_PASSWORD": "password-private", "VIDEO_AGENT_BUSINESS_TOKEN": "business-private",
        "VIDEO_AGENT_ADMIN_TOKEN": "admin-private", "VIDEO_AGENT_SECRET_KEY": "key-private",
    }.items():
        monkeypatch.setenv(name, value)
    settings = RuntimeSettings.from_env()
    assert "private" not in repr(settings)
    monkeypatch.setenv("VIDEO_AGENT_ADMIN_TOKEN", "business-private")
    with pytest.raises(ValueError, match="different|不同"):
        RuntimeSettings.from_env()


def test_failed_idempotent_mutation_does_not_reserve_key(tmp_path: Path):
    from video_agent import VideoAgent, VideoAgentError
    from video_agent.models import CreateTaskRequest

    agent = VideoAgent(tmp_path, planner=object())
    with pytest.raises(VideoAgentError):
        agent.create_task(CreateTaskRequest(images=["missing"]), idempotency_key="rollback-key")
    created = agent.create_task(CreateTaskRequest(), idempotency_key="rollback-key")
    assert agent.create_task(CreateTaskRequest(), idempotency_key="rollback-key").task_id == created.task_id


def test_settings_injection_uses_no_legacy_config_file(tmp_path: Path):
    from video_agent import VideoAgent
    from video_agent.config import AppConfig, DEFAULT_CONFIG_JSON
    from video_agent.models import CreateTaskRequest

    class Settings:
        def read_effective_config(self, *, conn=None):
            return AppConfig.model_validate_json(DEFAULT_CONFIG_JSON)

        def snapshot(self, *, conn=None):
            assert conn is not None
            return {"snapshot_marker": "database"}

    agent = VideoAgent(tmp_path, database=Database(tmp_path), settings_service=Settings(), planner=object())
    agent.create_task(CreateTaskRequest())
    assert not (tmp_path / "config.json").exists()


def test_database_error_cleans_uploaded_asset(tmp_path: Path):
    from contextlib import contextmanager
    from video_agent import VideoAgent
    from tests.test_tasks import png_bytes

    database = Database(tmp_path)
    agent = VideoAgent(tmp_path, database=database, planner=object())

    @contextmanager
    def unavailable(*, write=True):
        raise RuntimeError("database unavailable")
        yield

    database.transaction = unavailable
    with pytest.raises(RuntimeError):
        agent.register_asset(filename="upload.png", kind="image", data=png_bytes())
    assert list((tmp_path / "assets").rglob("*.png")) == []
