"""Synthetic legacy data only; integration target is the guarded MySQL fixture."""
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from video_agent.config import DEFAULT_CONFIG_JSON
from video_agent.config_store import ensure_settings_schema
from video_agent.db import Database, SCHEMA
from video_agent.secrets import SnapshotCodec
from video_agent.settings_service import SettingsService


@pytest.fixture
def legacy(tmp_path):
    home = tmp_path / "synthetic-legacy"
    home.mkdir()
    config = json.loads(DEFAULT_CONFIG_JSON)
    config["duration_tiers"] = [5, 10]
    config["adapters"]["planner"] = {
        "adapter": "openai_chat", "base_url": "https://example.invalid/v1",
        "model": "original-model", "api_key": "synthetic-import-secret",
    }
    config_path = home / "selected.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    (home / "config.json").write_text(DEFAULT_CONFIG_JSON, encoding="utf-8")
    for relative in ("assets/input.png", "clips/s1.mp4", "artifacts/final.mp4"):
        path = home / relative
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(b"synthetic-media")
    provider = home / "provider-state"
    provider.mkdir()
    (provider / "seedance-tasks.json").write_text('{"provider-key":"external-1"}', encoding="utf-8")
    (provider / "wan-tasks.json").write_text('{"wan-key":{"task_id":"wan-external","width":720,"height":1280}}', encoding="utf-8")
    stamp = "2026-01-01T00:00:00+00:00"
    copied = json.dumps({"mode": "fake", "adapters": config["adapters"]})
    rows = {
        "assets": [dict(asset_id="asset-1", kind="image", filename="input.png", media_type="image/png", byte_size=15, relative_path="assets/input.png", created_at=stamp)],
        "tasks": [dict(task_id="task-1", status="generating", questions_json="[]", head_plan_version=1, confirmed_plan_version=1, generation_id="generation-1", product_info_json='{"name":"合成商品"}', user_prompt="合成数据", video_options_json="{}", audio_options_json="{}", image_asset_ids_json='["asset-1"]', first_frame_asset_id="asset-1", planning_basis_json="{}", created_at=stamp, updated_at=stamp)],
        "plan_versions": [dict(task_id="task-1", plan_version=1, document_json='{"first_frame_asset_id":"asset-1","shots":[{"shot_id":"s1"}]}', abandoned=0, created_at=stamp)],
        "generations": [dict(generation_id="generation-1", task_id="task-1", plan_version=1, kind="full", shot_ids_json='["s1"]', status="running", stage="video", snapshot_json='{"shots":[{"shot_id":"s1"}]}', copied_config_json=copied, auto_attempts_used=0, created_at=stamp, updated_at=stamp)],
        "generation_shots": [dict(generation_id="generation-1", shot_id="s1", ordinal=0, start_frame=0, end_frame=300, status="imported", provider_idempotency_key="provider-key", external_task_id="external-1", submission_state="submitted", clip_path="clips/s1.mp4")],
        "artifacts": [dict(artifact_id="artifact-1", task_id="task-1", generation_id="generation-1", plan_version=1, version=1, duration_seconds=10, aspect_ratio="9:16", width=1080, height=1920, relative_path="artifacts/final.mp4", quality_status="not_assessed", quality_json="{}", created_at=stamp)],
        "work_items": [dict(work_id="work-1", task_id="task-1", kind="generation", generation_id="generation-1", ready_at=stamp, copied_config_json=copied, step="poll_shots", done=0)],
        "idempotency": [dict(method="create_task", idempotency_key="request-key", request_hash="f" * 64, task_id="task-1", response_json='{"task_id":"task-1"}', created_at=stamp)],
    }
    with sqlite3.connect(home / "agent.sqlite") as connection:
        connection.executescript(SCHEMA)
        for table, items in rows.items():
            for row in items:
                columns = ",".join(row)
                connection.execute(f"INSERT INTO {table}({columns}) VALUES ({','.join('?' for _ in row)})", tuple(row.values()))
    return home, config_path, rows


def _api():
    from video_agent.migration import import_legacy, inspect_legacy
    return import_legacy, inspect_legacy


def _inputs(home, config_path):
    return {path: path.read_bytes() for path in home.rglob("*") if path.is_file()} | {config_path: config_path.read_bytes()}


@pytest.fixture
def sqlite_target(tmp_path, monkeypatch):
    monkeypatch.setenv("VIDEO_AGENT_SECRET_KEY", Fernet.generate_key().decode())
    database = Database(tmp_path / "target")
    ensure_settings_schema(database)
    # Worker metadata is not part of the legacy eight-table schema.
    from video_agent.provider_jobs import metadata
    metadata.create_all(database.engine)
    yield database
    database.close()


@pytest.fixture
def mysql_target(mysql_database, monkeypatch):
    from video_agent.config_store import metadata as settings_metadata, seed_default_settings
    from video_agent.provider_jobs import metadata as worker_metadata
    monkeypatch.setenv("VIDEO_AGENT_SECRET_KEY", Fernet.generate_key().decode())
    with mysql_database.transaction() as conn:
        for table in reversed(settings_metadata.sorted_tables):
            conn.execute(f"DELETE FROM {table.name}")
        for table in reversed(worker_metadata.sorted_tables):
            conn.execute(f"DELETE FROM {table.name}")
        seed_default_settings(conn)
    return mysql_database


def test_dry_run_changes_nothing(legacy, sqlite_target):
    home, config_path, _ = legacy
    before = _inputs(home, config_path)
    migrate, _ = _api()
    report = migrate(home, config_path=config_path, database=sqlite_target)
    assert report.counts == {"assets": 1, "tasks": 1, "plan_versions": 1, "generations": 1, "generation_shots": 1, "artifacts": 1, "work_items": 1, "idempotency": 1, "provider_jobs": 2}
    assert report.dry_run is True
    assert "synthetic-import-secret" not in repr(report)
    assert _inputs(home, config_path) == before
    with sqlite_target.transaction(write=False) as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"] == 0
        assert conn.execute("SELECT configuration_revision FROM app_settings").fetchone()["configuration_revision"] == 1


def _apply(database, home, config_path, backup):
    migrate, _ = _api()
    return migrate(home, config_path=config_path, database=database, dry_run=False, backup_dir=backup, target_home=home)


def _check_import(database, legacy):
    home, config_path, rows = legacy
    with database.transaction(write=False) as conn:
        for table, items in rows.items():
            assert conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"] == len(items)
        assert conn.execute("SELECT external_task_id,clip_path FROM generation_shots").fetchone() == {"external_task_id": "external-1", "clip_path": "clips/s1.mp4"}
        raw = conn.execute("SELECT copied_config_json FROM work_items").fetchone()["copied_config_json"]
        assert "synthetic-import-secret" not in raw
        restored = SnapshotCodec().decode(json.loads(raw))
        assert restored.adapters["planner"].field_map()["api_key"] == "synthetic-import-secret"
        assert conn.execute("SELECT COUNT(*) AS n FROM provider_jobs").fetchone()["n"] == 2
        for table in ("model_profiles", "model_profile_revisions"):
            assert "synthetic-import-secret" not in repr(conn.execute(f"SELECT * FROM {table}").fetchall())
    assert SettingsService(database).read_effective_config().adapters["planner"].field_map()["api_key"] == "synthetic-import-secret"


def test_import_preserves_ids_and_files(legacy, sqlite_target, tmp_path):
    home, config_path, _ = legacy
    before = _inputs(home, config_path)
    backup = tmp_path / "backup"
    report = _apply(sqlite_target, home, config_path, backup)
    assert report.imported["tasks"] == 1
    _check_import(sqlite_target, legacy)
    assert _inputs(home, config_path) == before
    with sqlite3.connect(backup / "agent.sqlite") as connection:
        assert connection.execute("SELECT task_id FROM tasks").fetchone()[0] == "task-1"
    assert (backup / "config.json").read_bytes() == config_path.read_bytes()
    assert (backup / "provider-state" / "wan-tasks.json").read_bytes() == (home / "provider-state" / "wan-tasks.json").read_bytes()


def test_same_import_skips_conflicting_import_rolls_back(legacy, sqlite_target, tmp_path):
    home, config_path, _ = legacy
    _apply(sqlite_target, home, config_path, tmp_path / "backup-1")
    revision = SettingsService(sqlite_target).read_settings().configuration_revision
    again = _apply(sqlite_target, home, config_path, tmp_path / "backup-2")
    assert again.skipped["tasks"] == 1
    assert again.imported.get("tasks", 0) == 0
    assert SettingsService(sqlite_target).read_settings().configuration_revision == revision
    with sqlite3.connect(home / "agent.sqlite") as connection:
        connection.execute("UPDATE tasks SET user_prompt='changed' WHERE task_id='task-1'")
        connection.execute("UPDATE assets SET filename='different.png' WHERE asset_id='asset-1'")
    with pytest.raises(Exception, match="冲突"):
        _apply(sqlite_target, home, config_path, tmp_path / "backup-3")
    with sqlite_target.transaction(write=False) as conn:
        assert conn.execute("SELECT filename FROM assets").fetchone()["filename"] == "input.png"
        assert conn.execute("SELECT user_prompt FROM tasks").fetchone()["user_prompt"] == "合成数据"


@pytest.mark.parametrize("location", ["source", "target", "worker"])
def test_active_worker_blocks_import(legacy, sqlite_target, tmp_path, location):
    home, config_path, _ = legacy
    future = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    if location == "source":
        with sqlite3.connect(home / "agent.sqlite") as connection:
            connection.execute("UPDATE work_items SET lease_owner='old-worker',lease_until=?", (future,))
    elif location == "worker":
        with sqlite_target.transaction() as conn:
            conn.execute("INSERT INTO worker_heartbeats(singleton_id,worker_owner,lease_until,last_seen_at,stopping) VALUES (1,'worker',:future,:future,0)", {"future": future})
    else:
        _apply(sqlite_target, home, config_path, tmp_path / "backup-prior")
        with sqlite_target.transaction() as conn:
            conn.execute("UPDATE work_items SET lease_owner='worker',lease_until=:future", {"future": future})
    with pytest.raises(Exception, match="租约|Worker"):
        _apply(sqlite_target, home, config_path, tmp_path / "blocked-backup")
    assert not (tmp_path / "blocked-backup").exists()


def test_explicit_config_source_wins(legacy):
    home, config_path, _ = legacy
    _, inspect = _api()
    assert inspect(home, config_path=config_path).counts["tasks"] == 1
    config_path.write_text("{}", encoding="utf-8")
    with pytest.raises(Exception):
        inspect(home, config_path=config_path)


@pytest.mark.parametrize("damage", ["schema", "association", "file", "escape", "provider"])
def test_unknown_schema_or_broken_inputs_rejected(legacy, damage):
    home, config_path, _ = legacy
    if damage == "provider":
        (home / "provider-state" / "wan-tasks.json").write_text('{"bad":{}}', encoding="utf-8")
    else:
        with sqlite3.connect(home / "agent.sqlite") as connection:
            if damage == "schema":
                connection.execute("ALTER TABLE tasks ADD COLUMN unknown TEXT")
            elif damage == "association":
                connection.execute("UPDATE generations SET task_id='unknown-task'")
            elif damage == "file":
                (home / "clips/s1.mp4").unlink()
            else:
                connection.execute("UPDATE assets SET relative_path='../outside.png'")
    _, inspect = _api()
    with pytest.raises(Exception):
        inspect(home, config_path=config_path)


def test_apply_requires_backup_and_never_overwrites(legacy, sqlite_target, tmp_path):
    home, config_path, _ = legacy
    migrate, _ = _api()
    with pytest.raises(Exception, match="备份"):
        migrate(home, config_path=config_path, database=sqlite_target, dry_run=False)
    backup = tmp_path / "existing-backup"
    backup.mkdir()
    (backup / "agent.sqlite").write_bytes(b"keep")
    with pytest.raises(Exception, match="备份"):
        _apply(sqlite_target, home, config_path, backup)
    assert (backup / "agent.sqlite").read_bytes() == b"keep"


@pytest.mark.parametrize("done", [0, 1])
def test_future_lease_blocks_import_even_without_owner(legacy, sqlite_target, tmp_path, done):
    home, config_path, _ = legacy
    future = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    with sqlite3.connect(home / "agent.sqlite") as conn:
        conn.execute("UPDATE work_items SET lease_until=?,lease_owner=NULL,done=?", (future, done))
    with pytest.raises(Exception, match="租约"):
        _apply(sqlite_target, home, config_path, tmp_path / "blocked-ownerless-backup")


def test_inspect_rejects_invalid_snapshot_before_writing_backup(legacy):
    home, config_path, _ = legacy
    with sqlite3.connect(home / "agent.sqlite") as conn:
        conn.execute("UPDATE work_items SET copied_config_json='[]'")
    _, inspect = _api()
    with pytest.raises(Exception, match="快照"):
        inspect(home, config_path=config_path)


def test_unknown_schema_with_missing_unique_constraint_is_rejected(legacy):
    home, config_path, rows = legacy
    with sqlite3.connect(home / "agent.sqlite") as conn:
        conn.execute("DROP TABLE artifacts")
        create = SCHEMA.split("CREATE TABLE artifacts (", 1)[1].split(";", 1)[0]
        create = create.replace(",\n  UNIQUE (task_id, version)", "")
        conn.execute("CREATE TABLE artifacts (" + create)
        row = rows["artifacts"][0]
        conn.execute(f"INSERT INTO artifacts({','.join(row)}) VALUES ({','.join('?' for _ in row)})", tuple(row.values()))
    _, inspect = _api()
    with pytest.raises(Exception, match="schema"):
        inspect(home, config_path=config_path)


def test_import_uses_explicit_key_for_encrypted_source_snapshots(legacy, sqlite_target, tmp_path):
    from video_agent.config import read_legacy_config
    home, config_path, _ = legacy
    key = Fernet.generate_key()
    encrypted = json.dumps(SnapshotCodec(key).encode(read_legacy_config(config_path)))
    with sqlite3.connect(home / "agent.sqlite") as conn:
        conn.execute("UPDATE work_items SET copied_config_json=?", (encrypted,))
        conn.execute("UPDATE generations SET copied_config_json=?", (encrypted,))
    migrate, _ = _api()
    report = migrate(home, config_path=config_path, database=sqlite_target, dry_run=False,
                     backup_dir=tmp_path / "explicit-key-backup", secret_key=key)
    assert report.imported["work_items"] == 1
    with sqlite_target.transaction(write=False) as conn:
        stored = json.loads(conn.execute("SELECT copied_config_json FROM work_items").fetchone()["copied_config_json"])
        assert SnapshotCodec(key).decode(stored).adapters["planner"].field_map()["api_key"] == "synthetic-import-secret"


def test_mysql_import_preserves_ids_and_encrypts_secrets(legacy, mysql_target, tmp_path):
    home, config_path, _ = legacy
    _apply(mysql_target, home, config_path, tmp_path / "mysql-backup")
    _check_import(mysql_target, legacy)
    again = _apply(mysql_target, home, config_path, tmp_path / "mysql-backup-again")
    assert again.skipped["generations"] == 1


def test_mysql_dry_run_and_late_conflict_roll_back_every_insert(legacy, mysql_target, tmp_path):
    home, config_path, _ = legacy
    migrate, _ = _api()
    report = migrate(home, config_path=config_path, database=mysql_target, target_home=home)
    assert report.dry_run
    with mysql_target.transaction(write=False) as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"] == 0
        assert conn.execute("SELECT COUNT(*) AS n FROM model_profiles").fetchone()["n"] == 0
    _apply(mysql_target, home, config_path, tmp_path / "first-backup")
    with sqlite3.connect(home / "agent.sqlite") as conn:
        conn.execute("INSERT INTO assets SELECT 'asset-new',kind,filename,media_type,byte_size,relative_path,created_at FROM assets")
        conn.execute("UPDATE tasks SET user_prompt='conflicting' WHERE task_id='task-1'")
    with pytest.raises(Exception, match="冲突"):
        _apply(mysql_target, home, config_path, tmp_path / "conflict-backup")
    with mysql_target.transaction(write=False) as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM assets WHERE asset_id='asset-new'").fetchone()["n"] == 0
        assert conn.execute("SELECT user_prompt FROM tasks").fetchone()["user_prompt"] == "合成数据"


@pytest.mark.parametrize("location", ["source", "target", "worker"])
def test_mysql_active_worker_blocks_import(legacy, mysql_target, tmp_path, location):
    test_active_worker_blocks_import(legacy, mysql_target, tmp_path, location)


def test_mysql_requires_explicit_matching_media_root(legacy, mysql_target, tmp_path):
    home, config_path, _ = legacy
    migrate, _ = _api()
    for target in (None, tmp_path / "different-root"):
        with pytest.raises(Exception, match="VIDEO_AGENT_HOME"):
            migrate(home, config_path=config_path, database=mysql_target, target_home=target)


def test_current_sqlite_schema_and_pending_work_resume_after_import(sqlite_target, tmp_path):
    import io
    from PIL import Image
    from video_agent.models import CreateTaskRequest
    from video_agent.service import VideoAgent

    home = tmp_path / "synthetic-current-source"
    old = VideoAgent(home)
    image = io.BytesIO()
    Image.new("RGB", (8, 8), "red").save(image, format="PNG")
    asset = old.register_asset(filename="input.png", kind="image", data=image.getvalue())
    task = old.create_task(CreateTaskRequest(images=[asset], first_frame_asset_id=asset, product_info={"name": "瓶子"}))
    old.db.close()
    _apply(sqlite_target, home, home / "config.json", tmp_path / "recovery-backup")
    resumed = VideoAgent(home, database=sqlite_target, settings_service=SettingsService(sqlite_target))
    assert resumed.get_task(task.task_id).status == "pending"
    resumed.recover()
    assert resumed.tick().did_work
    assert resumed.tick().did_work
    restored = resumed.get_task(task.task_id)
    assert restored.status == "awaiting_confirmation"
    assert restored.head_plan_version == 1
    assert resumed.get_plan(task.task_id, 1).document.first_frame_asset_id == asset


def test_cli_dry_run_is_readonly_and_apply_requires_backup(legacy, capsys):
    from video_agent.migration import main
    home, config_path, _ = legacy
    before = _inputs(home, config_path)
    assert main(["--source-home", str(home), "--config-path", str(config_path), "--dry-run"]) == 0
    output = capsys.readouterr().out
    assert json.loads(output)["counts"]["provider_jobs"] == 2
    assert "synthetic-import-secret" not in output
    assert str(home) not in output
    assert _inputs(home, config_path) == before
    with pytest.raises(SystemExit) as exc:
        main(["--source-home", str(home), "--config-path", str(config_path), "--apply"])
    assert exc.value.code == 2
