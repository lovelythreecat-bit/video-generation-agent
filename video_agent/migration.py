"""Explicit, offline import of the current legacy SQLite/JSON format.

Media stays in source_home. Production VIDEO_AGENT_HOME must name that same
directory; this tool never discovers a repository config or starts a Worker.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import sqlalchemy as sa

from video_agent.config import (
    ADAPTER_ROLES, DEFAULT_CONFIG_JSON, PUBLIC_SETTING_FIELDS, _is_secret,
    config_to_dict, read_legacy_config, validate_config,
)
from video_agent.db import SCHEMA, Database
from video_agent.db_schema import metadata as core_metadata
from video_agent.errors import VideoAgentError
from video_agent.runtime import RuntimeSettings
from video_agent.secrets import SnapshotCodec
from video_agent.settings_service import SettingsService

TABLES = tuple(core_metadata.tables)
PRIMARY_KEYS = {name: tuple(column.name for column in table.primary_key) for name, table in core_metadata.tables.items()}


@dataclass
class MigrationReport:
    dry_run: bool = True
    counts: dict[str, int] = field(default_factory=dict)
    imported: dict[str, int] = field(default_factory=dict)
    skipped: dict[str, int] = field(default_factory=dict)
    conflicts: list[str] = field(default_factory=list)
    sources: dict[str, str] = field(default_factory=lambda: {
        "database": "<source-home>/agent.sqlite", "config": "<explicit-config>",
        "provider_state": "<explicit-source-home>/provider-state",
        "media_root": "<source-home> (must also be VIDEO_AGENT_HOME)",
    })
    files_checked: int = 0


def _fail(message: str, *, code="MIGRATION_ERROR"):
    raise VideoAgentError(code, message)


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _parse(raw, label):
    try:
        return json.loads(raw)
    except (TypeError, ValueError, UnicodeError):
        _fail(f"旧数据的 {label} JSON 无效")


def _signature(connection, table):
    def affinity(kind):
        kind = kind.upper()
        return "integer" if "INT" in kind else "text" if any(item in kind for item in ("CHAR", "TEXT", "CLOB")) else kind
    return [(row[1], affinity(row[2]), bool(row[3]) or bool(row[5]), row[5])
            for row in connection.execute(f'PRAGMA table_info("{table}")')]


def _check_source_schema(connection):
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
    if tables != set(TABLES):
        _fail("旧 SQLite schema 不受支持：必须是当前八张业务表")
    with sqlite3.connect(":memory:") as reference:
        reference.executescript(SCHEMA)
        for table in TABLES:
            actual, expected = _signature(connection, table), _signature(reference, table)
            # The new explicit SQLite test backend permits an idempotency reservation
            # before its response is known. All other schema columns stay identical.
            actual = [(name, kind, True if table == "idempotency" and name == "response_json" else required, pk)
                      for name, kind, required, pk in actual]
            if actual != expected:
                _fail(f"旧 SQLite schema 不受支持：{table} 列、类型或主键不匹配")
            def unique_keys(database):
                return {tuple(item[0] for item in database.execute("SELECT name FROM pragma_index_info(?) ORDER BY seqno", (index[1],)))
                        for index in database.execute(f'PRAGMA index_list("{table}")') if index[2]}
            if unique_keys(connection) != unique_keys(reference):
                _fail(f"旧 SQLite schema 不受支持：{table} 唯一约束不匹配")


def _read_sources(home: Path, config_path: Path, provider_state_dir: Path | None, *, codec=None):
    home = Path(home).resolve()
    path = home / "agent.sqlite"
    if not path.is_file():
        _fail("指定旧 home 内没有 agent.sqlite")
    config_path = Path(config_path).resolve()
    config = read_legacy_config(config_path)
    config_bytes = config_path.read_bytes()
    if config_to_dict(validate_config(_parse(config_bytes, "config"))) != config_to_dict(config):
        _fail("读取期间配置发生变化，请停止旧程序后重试")
    provider = (home / "provider-state") if provider_state_dir is None else Path(provider_state_dir).resolve()
    provider_files, jobs = {}, []
    if provider.exists():
        if not provider.is_dir():
            _fail("指定 provider-state 必须是目录")
        unknown = [item for item in provider.iterdir() if item.name not in {"seedance-tasks.json", "wan-tasks.json"}]
        if unknown:
            _fail("provider-state 含不支持的文件；请显式选择当前格式的目录")
        for adapter, name in (("seedance", "seedance-tasks.json"), ("wan", "wan-tasks.json")):
            item = provider / name
            if not item.exists():
                continue
            raw = item.read_bytes()
            values = _parse(raw, "provider-state")
            if not isinstance(values, dict):
                _fail("provider-state 必须是 JSON 对象")
            provider_files[name] = raw
            for key, value in values.items():
                if not isinstance(key, str) or not key or len(key) > 128:
                    _fail("provider-state 幂等键无效")
                if adapter == "seedance":
                    external, download = value, {}
                else:
                    if not isinstance(value, dict) or set(value) != {"task_id", "width", "height"}:
                        _fail("Wan provider-state 记录格式无效")
                    external = value["task_id"]
                    download = {name: value[name] for name in ("width", "height")}
                    if any(type(number) is not int or number <= 0 for number in download.values()):
                        _fail("Wan provider-state 尺寸无效")
                if not isinstance(external, str) or not external or len(external) > 255:
                    _fail("provider-state 外部任务号无效")
                jobs.append(dict(adapter=adapter, key=key, external_task_id=external,
                                 submission_state="submitted", request_metadata_json="{}", download_metadata_json=_json(download)))
    try:
        connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN")
        _check_source_schema(connection)
        rows = {table: [dict(row) for row in connection.execute(f"SELECT * FROM {table}")] for table in TABLES}
        files_checked = _check_rows(home, rows, codec=codec or SnapshotCodec())
    except BaseException:
        if "connection" in locals():
            connection.close()
        raise
    return connection, config, config_bytes, provider_files, jobs, rows, files_checked


def _check_rows(home, rows, *, codec):
    assets = {row["asset_id"]: row for row in rows["assets"]}
    tasks = {row["task_id"]: row for row in rows["tasks"]}
    plans = {(row["task_id"], row["plan_version"]): row for row in rows["plan_versions"]}
    generations = {row["generation_id"]: row for row in rows["generations"]}
    artifacts = {row["artifact_id"]: row for row in rows["artifacts"]}
    checked = set()

    def require(condition):
        if not condition:
            _fail("旧数据关联 ID 不完整或跨任务不一致")

    def file(relative):
        if relative is None:
            return
        if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
            _fail("旧媒体路径必须是 home 内的相对路径")
        resolved = (home / relative).resolve()
        if not resolved.is_relative_to(home) or not resolved.is_file():
            _fail("旧媒体文件缺失或路径越界")
        checked.add(resolved)

    def asset_references(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key in {"asset_id", "first_frame_asset_id", "music_asset_id"} and child:
                    require(isinstance(child, str) and child in assets)
                else:
                    asset_references(child)
        elif isinstance(value, list):
            for child in value:
                asset_references(child)

    for table, items in rows.items():
        for row in items:
            for name, value in row.items():
                if name.endswith("_json") and value is not None:
                    parsed = _parse(value, table)
                    if name == "copied_config_json":
                        _snapshot_config(value, codec)
                    if name in {"snapshot_json", "document_json"} and not isinstance(parsed, dict):
                        _fail("旧方案或执行快照必须是 JSON 对象")
                    asset_references(parsed)
            if table in {"assets", "artifacts"}:
                file(row["relative_path"])
            if table == "generation_shots":
                file(row["clip_path"])
    for row in rows["tasks"]:
        image_ids = _parse(row["image_asset_ids_json"], "image IDs")
        require(isinstance(image_ids, list) and all(isinstance(item, str) and item in assets for item in image_ids))
        if row["first_frame_asset_id"]:
            require(row["first_frame_asset_id"] in assets)
        for name in ("head_plan_version", "confirmed_plan_version"):
            if row[name] is not None:
                require((row["task_id"], row[name]) in plans)
        if row["generation_id"]:
            require(row["generation_id"] in generations and generations[row["generation_id"]]["task_id"] == row["task_id"])
    for row in rows["plan_versions"]:
        require(row["task_id"] in tasks)
    for row in rows["generations"]:
        require(row["task_id"] in tasks and (row["task_id"], row["plan_version"]) in plans)
        if row["base_artifact_id"]:
            require(row["base_artifact_id"] in artifacts and artifacts[row["base_artifact_id"]]["task_id"] == row["task_id"])
    for row in rows["generation_shots"]:
        require(row["generation_id"] in generations)
        snapshot = _parse(generations[row["generation_id"]]["snapshot_json"], "generation snapshot")
        require(row["shot_id"] in {item.get("shot_id") for item in snapshot.get("shots", []) if isinstance(item, dict)})
    for row in rows["artifacts"]:
        generation = generations.get(row["generation_id"])
        require(generation is not None and generation["task_id"] == row["task_id"] and generation["plan_version"] == row["plan_version"])
    for row in rows["work_items"]:
        require(row["task_id"] in tasks)
        if row["generation_id"]:
            require(row["generation_id"] in generations and generations[row["generation_id"]]["task_id"] == row["task_id"])
    for row in rows["idempotency"]:
        if row["task_id"]:
            require(row["task_id"] in tasks)
    return len(checked)


def inspect_legacy(home: Path, *, config_path: Path, provider_state_dir: Path | None = None) -> MigrationReport:
    connection, _, _, _, jobs, rows, files = _read_sources(home, config_path, provider_state_dir)
    try:
        return MigrationReport(counts={**{table: len(items) for table, items in rows.items()}, "provider_jobs": len(jobs)}, files_checked=files)
    finally:
        connection.close()


def _lease_active(owner, until):
    if not until:
        if owner:
            _fail("租约记录缺少到期时间，不能安全导入")
        return False
    try:
        stamp = datetime.fromisoformat(until)
        if stamp.tzinfo is None:
            raise ValueError
        return stamp > datetime.now(timezone.utc)
    except (TypeError, ValueError):
        _fail("租约到期时间无效，不能安全导入")


def _offline(conn, rows, *, lock=False):
    if lock:
        statement = "INSERT INTO worker_heartbeats(singleton_id) VALUES (1)"
        statement += " ON DUPLICATE KEY UPDATE singleton_id=singleton_id" if conn.dialect_name == "mysql" else " ON CONFLICT(singleton_id) DO NOTHING"
        conn.execute(statement)
    sql = "SELECT * FROM worker_heartbeats WHERE singleton_id=1"
    heartbeat = conn.execute(conn.for_update(sql) if lock else sql).fetchone()
    if heartbeat and _lease_active(heartbeat["worker_owner"], heartbeat["lease_until"]):
        _fail("Worker 仍持有有效租约；停止 Worker 并等待租约释放后导入")
    target = conn.execute("SELECT lease_owner,lease_until FROM work_items").fetchall()
    if any(_lease_active(row["lease_owner"], row["lease_until"]) for row in [*rows["work_items"], *target]):
        _fail("工作项仍有有效租约；停止旧 Worker 并等待租约过期后导入")


def _snapshot_config(raw, codec):
    value = _parse(raw, "copied_config")
    if isinstance(value, dict) and "format_version" in value:
        return codec.decode(value)
    if not isinstance(value, dict):
        _fail("旧工作配置快照无效")
    merged = json.loads(DEFAULT_CONFIG_JSON)
    merged.update(value)
    return validate_config(merged)


def _canonical_row(row, codec):
    result = dict(row)
    for name, value in result.items():
        if name == "copied_config_json":
            result[name] = config_to_dict(_snapshot_config(value, codec))
        elif name.endswith("_json") and value is not None:
            result[name] = _parse(value, "目标或旧数据")
    return result


def _import_rows(conn, table, items, primary_keys, codec, report, *, apply):
    for original in items:
        where = " AND ".join(f"`{key}`=:{key}" for key in primary_keys)
        parameters = {key: original[key] for key in primary_keys}
        existing = conn.execute(f"SELECT * FROM {table} WHERE {where}", parameters).fetchone()
        if existing is not None:
            if _canonical_row(existing, codec) != _canonical_row(original, codec):
                _fail(f"目标已有不同内容：{table} 主键冲突", code="CONFLICT")
            report.skipped[table] = report.skipped.get(table, 0) + 1
            continue
        row = dict(original)
        if "copied_config_json" in row:
            row["copied_config_json"] = _json(codec.encode(_snapshot_config(row["copied_config_json"], codec)))
        if apply:
            columns = ",".join(f"`{key}`" for key in row)
            conn.execute(f"INSERT INTO {table}({columns}) VALUES ({','.join(':' + key for key in row)})", row)
        report.imported[table] = report.imported.get(table, 0) + 1


def _contains_secret(value):
    if isinstance(value, dict):
        return any(_is_secret(str(key)) or _contains_secret(child) for key, child in value.items())
    if isinstance(value, list):
        return any(_contains_secret(child) for child in value)
    return False


def _import_config(conn, config, settings, *, apply):
    current = settings.read_effective_config(conn=conn)
    if config_to_dict(current) == config_to_dict(config):
        return
    row = conn.execute(conn.for_update("SELECT * FROM app_settings WHERE singleton_id=1")).fetchone()
    pristine = row["configuration_revision"] == 1 and config_to_dict(current) == json.loads(DEFAULT_CONFIG_JSON)
    pristine = pristine and conn.execute("SELECT COUNT(*) AS n FROM model_profiles").fetchone()["n"] == 0
    if not pristine:
        _fail("目标配置与明确选定的旧配置冲突", code="CONFLICT")
    # Validate and encrypt even during dry-run, without issuing writes.
    body = config_to_dict(config)
    profiles = []
    for role in ADAPTER_ROLES:
        adapter = dict(body["adapters"][role])
        name = adapter.pop("adapter")
        public, secrets = {}, {}
        for key, value in adapter.items():
            (secrets if _is_secret(key) or _contains_secret(value) else public)[key] = value
        encrypted = settings.codec.cipher.encrypt(secrets)
        profile = {"name": f"Imported {role}", "role": role, "adapter": name, "enabled": True,
                   "parameters": {key: value for key, value in public.items() if key not in {"base_url", "model"}},
                   "secret_presence": {key: True for key in secrets},
                   **{key: public[key] for key in ("base_url", "model") if key in public}}
        if name not in {"fake", "none"}:
            settings._validate_profile({**profile, "secrets": secrets})
        profiles.append((role, name, adapter, profile, encrypted))
    if not apply:
        return
    stamp = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    for role, name, adapter, profile, encrypted in profiles:
        profile_id = str(uuid.uuid4()) if adapter or name not in {"fake", "none"} else None
        if profile_id:
            params = dict(id=profile_id, role=role, adapter=name, body=_json(profile), encrypted=encrypted, stamp=stamp)
            conn.execute("INSERT INTO model_profiles(profile_id,role,adapter,enabled,version,body_json,encrypted_secrets,created_at,updated_at) VALUES (:id,:role,:adapter,1,1,:body,:encrypted,:stamp,:stamp)", params)
            conn.execute("INSERT INTO model_profile_revisions(profile_id,version,body_json,encrypted_secrets,created_at) VALUES (:id,1,:body,:encrypted,:stamp)", params)
        conn.execute("UPDATE adapter_bindings SET adapter=:adapter,profile_id=:id WHERE role=:role", {"adapter": name, "id": profile_id, "role": role})
    conn.execute("UPDATE app_settings SET settings_json=:body,configuration_revision=configuration_revision+1 WHERE singleton_id=1", {"body": _json({key: body[key] for key in PUBLIC_SETTING_FIELDS})})


def _backup(connection, home, backup_dir, config_bytes, provider_files):
    backup = Path(backup_dir).resolve()
    home = Path(home).resolve()
    if backup.is_relative_to(home) or home.is_relative_to(backup) or backup.exists():
        _fail("备份目录必须是源目录之外的全新独立目录，不覆盖已有内容")
    backup.mkdir(parents=True, exist_ok=False)
    with sqlite3.connect(backup / "agent.sqlite") as target:
        connection.backup(target)
    (backup / "config.json").write_bytes(config_bytes)
    if provider_files:
        (backup / "provider-state").mkdir()
        for name, raw in provider_files.items():
            (backup / "provider-state" / name).write_bytes(raw)


def import_legacy(home: Path, *, config_path: Path, database: Database, dry_run: bool = True,
                  backup_dir: Path | None = None, provider_state_dir: Path | None = None,
                  target_home: Path | None = None, secret_key: str | bytes | None = None) -> MigrationReport:
    if not dry_run and backup_dir is None:
        _fail("正式导入必须显式提供备份目录")
    if database.dialect_name == "mysql":
        if target_home is None or Path(target_home).resolve() != Path(home).resolve():
            _fail("目标 VIDEO_AGENT_HOME 必须与 source-home 相同，以保留媒体路径")
        if sa.inspect(database.engine).get_table_names() and "alembic_version" in sa.inspect(database.engine).get_table_names():
            with database.transaction(write=False) as conn:
                versions = [row["version_num"] for row in conn.execute("SELECT version_num FROM alembic_version")]
            if versions != ["20261014_03"]:
                _fail("目标数据库必须先升级到当前 schema 20261014_03")
    required = set(TABLES) | {"app_settings", "adapter_bindings", "model_profiles", "model_profile_revisions", "provider_jobs", "worker_heartbeats"}
    if not required.issubset(sa.inspect(database.engine).get_table_names()):
        _fail("目标数据库 schema 尚未升级；迁移工具不会自动建表")
    source, config, config_bytes, provider_files, jobs, rows, files = _read_sources(home, config_path, provider_state_dir, codec=SnapshotCodec(secret_key))
    try:
        report = MigrationReport(dry_run=dry_run, counts={**{name: len(items) for name, items in rows.items()}, "provider_jobs": len(jobs)}, files_checked=files)
        settings = SettingsService(database, secret_key)
        with database.transaction(write=False) as conn:
            _offline(conn, rows)
        if not dry_run:
            _backup(source, home, backup_dir, config_bytes, provider_files)
        with database.transaction(write=not dry_run) as conn:
            _offline(conn, rows, lock=not dry_run)
            _import_config(conn, config, settings, apply=not dry_run)
            for name in TABLES:
                _import_rows(conn, name, rows[name], PRIMARY_KEYS[name], settings.codec, report, apply=not dry_run)
            _import_rows(conn, "provider_jobs", jobs, ("adapter", "key"), settings.codec, report, apply=not dry_run)
        return report
    finally:
        source.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="显式导入旧 SQLite/JSON；媒体保留在 source-home")
    parser.add_argument("--source-home", required=True, type=Path)
    parser.add_argument("--config-path", required=True, type=Path)
    parser.add_argument("--provider-state-dir", type=Path, help="默认仅检查明确 source-home 下的 provider-state")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--dry-run", action="store_true")
    action.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-dir", type=Path)
    args = parser.parse_args(argv)
    if args.apply and args.backup_dir is None:
        parser.error("--apply 必须提供 --backup-dir")
    database = None
    try:
        if args.dry_run:
            report = inspect_legacy(args.source_home, config_path=args.config_path, provider_state_dir=args.provider_state_dir)
        else:
            runtime = RuntimeSettings.from_env(validate_tokens=False)
            database = Database.from_settings(runtime)
            report = import_legacy(args.source_home, config_path=args.config_path, database=database, dry_run=False,
                                   backup_dir=args.backup_dir, provider_state_dir=args.provider_state_dir,
                                   target_home=runtime.home, secret_key=runtime.secret_key)
        print(_json(asdict(report)))
        return 0
    except (VideoAgentError, OSError, sqlite3.Error, sa.exc.SQLAlchemyError, ValueError):
        # Upstream exceptions may include absolute paths, connection credentials,
        # provider payloads or configuration values. Never echo them from a CLI.
        print(_json({"error": "迁移失败：请检查 schema、关联、文件、配置、目标根目录、冲突及有效租约；源数据保留"}))
        return 1
    finally:
        if database is not None:
            database.close()


if __name__ == "__main__":
    raise SystemExit(main())
