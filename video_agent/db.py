import re
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from collections.abc import Mapping
from functools import wraps
import time

import sqlalchemy as sa

from video_agent.db_schema import metadata

SCHEMA = """
CREATE TABLE assets (
  asset_id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  filename TEXT NOT NULL,
  media_type TEXT NOT NULL,
  byte_size INTEGER NOT NULL,
  relative_path TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE tasks (
  task_id TEXT PRIMARY KEY,
  status TEXT NOT NULL,
  stage TEXT,
  stage_detail TEXT,
  failure_scope TEXT,
  failure_code TEXT,
  failure_message TEXT,
  failure_stage TEXT,
  failure_retryable INTEGER,
  failure_details_json TEXT,
  questions_json TEXT NOT NULL DEFAULT '[]',
  external_user_id TEXT,
  external_request_id TEXT,
  head_plan_version INTEGER,
  confirmed_plan_version INTEGER,
  generation_id TEXT,
  product_info_json TEXT NOT NULL,
  user_prompt TEXT NOT NULL DEFAULT '',
  video_options_json TEXT NOT NULL,
  audio_options_json TEXT NOT NULL,
  image_asset_ids_json TEXT NOT NULL,
  first_frame_asset_id TEXT,
  pending_instruction TEXT,
  planning_basis_json TEXT NOT NULL,
  completed_shots INTEGER,
  total_shots INTEGER,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE plan_versions (
  task_id TEXT NOT NULL,
  plan_version INTEGER NOT NULL,
  document_json TEXT NOT NULL,
  abandoned INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  PRIMARY KEY (task_id, plan_version)
);

CREATE TABLE generations (
  generation_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  plan_version INTEGER NOT NULL,
  kind TEXT NOT NULL,
  shot_ids_json TEXT NOT NULL,
  base_artifact_id TEXT,
  status TEXT NOT NULL,
  stage TEXT NOT NULL,
  stage_detail TEXT,
  completed_shots INTEGER,
  total_shots INTEGER,
  snapshot_json TEXT NOT NULL,
  copied_config_json TEXT NOT NULL,
  failure_code TEXT,
  failure_message TEXT,
  failure_stage TEXT,
  failure_retryable INTEGER,
  failure_details_json TEXT,
  auto_attempts_used INTEGER NOT NULL DEFAULT 0,
  next_attempt_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE generation_shots (
  generation_id TEXT NOT NULL,
  shot_id TEXT NOT NULL,
  ordinal INTEGER NOT NULL,
  start_frame INTEGER NOT NULL,
  end_frame INTEGER NOT NULL,
  status TEXT NOT NULL,
  provider_idempotency_key TEXT,
  external_task_id TEXT,
  submission_state TEXT,
  clip_path TEXT,
  PRIMARY KEY (generation_id, shot_id)
);

CREATE TABLE artifacts (
  artifact_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  generation_id TEXT NOT NULL,
  plan_version INTEGER NOT NULL,
  version INTEGER NOT NULL,
  duration_seconds INTEGER NOT NULL,
  aspect_ratio TEXT NOT NULL,
  width INTEGER NOT NULL,
  height INTEGER NOT NULL,
  relative_path TEXT NOT NULL,
  quality_status TEXT NOT NULL,
  quality_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE (task_id, version)
);

CREATE TABLE work_items (
  work_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  generation_id TEXT,
  ready_at TEXT NOT NULL,
  lease_owner TEXT,
  lease_until TEXT,
  copied_config_json TEXT NOT NULL,
  step TEXT NOT NULL,
  next_attempt_at TEXT,
  done INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE idempotency (
  method TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  request_hash TEXT NOT NULL,
  task_id TEXT,
  response_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (method, idempotency_key)
);
"""

_SCHEMA_TABLES = frozenset(re.findall(r"^CREATE TABLE (\w+)", SCHEMA, re.MULTILINE))


class MappingRow(dict):
    def __getitem__(self, key):
        # Temporary DB-API position access for the legacy pipeline only.
        return tuple(self.values())[key] if isinstance(key, int) else super().__getitem__(key)


class RowResult:
    """Small mapping-only result facade shared by both backends."""

    def __init__(self, result):
        self._result = result
        self.rowcount = result.rowcount
        self._mappings = result.mappings() if result.returns_rows else None
        self.description = [(name, None, None, None, None, None, None) for name in result.keys()] if result.returns_rows else None

    def fetchone(self):
        row = None if self._mappings is None else self._mappings.fetchone()
        return None if row is None else MappingRow(row)

    def fetchall(self):
        return [] if self._mappings is None else [MappingRow(row) for row in self._mappings.fetchall()]

    def __iter__(self):
        return iter(()) if self._mappings is None else (MappingRow(row) for row in self._mappings)


class Connection:
    def __init__(self, connection, *, write: bool):
        self._connection = connection
        self.dialect_name = connection.dialect.name
        self.write = write

    def for_update(self, sql: str, *, skip_locked: bool = False) -> str:
        if self.dialect_name == "mysql" and self.write:
            version = self._connection.dialect.server_version_info or ()
            # MySQL 5.7 uses a blocking current read. It still rechecks the
            # claim predicate after a competing transaction commits its lease.
            skip = skip_locked and version >= (8, 0, 1)
            return sql.rstrip().rstrip(";") + " FOR UPDATE" + (" SKIP LOCKED" if skip else "")
        return sql

    @property
    def in_transaction(self) -> bool:
        return self._connection.in_transaction()

    def execute(self, sql: str, parameters=None) -> RowResult:
        # Transitional support for worker/pipeline code, removed as those modules
        # adopt named SQL. Owned core modules always use explicit named mappings.
        if parameters is not None and not isinstance(parameters, Mapping):
            values = list(parameters)
            parts = sql.split("?")
            if len(parts) != len(values) + 1:
                raise ValueError("SQL parameter count mismatch")
            sql = "".join(part + (f":legacy_{index}" if index < len(values) else "") for index, part in enumerate(parts))
            parameters = {f"legacy_{index}": value for index, value in enumerate(values)}
        return RowResult(self._connection.execute(sa.text(sql), {} if parameters is None else parameters))


def is_retryable_transaction_error(exc: BaseException) -> bool:
    if not isinstance(exc, sa.exc.DBAPIError):
        return False
    args = getattr(exc.orig, "args", ())
    return bool(args and args[0] in {1205, 1213})


def retry_database_mutation(method):
    """Retry only rolled-back database-only service mutations, at most 3 times."""
    @wraps(method)
    def wrapped(*args, **kwargs):
        for attempt in range(3):
            try:
                return method(*args, **kwargs)
            except sa.exc.DBAPIError as exc:
                if attempt == 2 or not is_retryable_transaction_error(exc):
                    raise
                time.sleep(0.01 * (attempt + 1))
    return wrapped


def validate_mysql_version(version: str) -> None:
    """Apply the same supported-server check to runtime and schema migrations."""
    match = re.match(r"^(\d+)\.(\d+)(?:\.|$)", version)
    if ("mariadb" in version.lower() or match is None
            or tuple(int(part) for part in match.groups()) < (5, 7)):
        raise ValueError("Requires MySQL 5.7 or newer; MariaDB is not supported")


class Database:
    def __init__(self, home: Path) -> None:
        """Explicit SQLite backend retained for isolated legacy unit tests."""
        home = Path(home)
        home.mkdir(parents=True, exist_ok=True)
        self.path = home / "agent.sqlite"
        self.engine = sa.create_engine(sa.URL.create("sqlite", database=str(self.path)),
            connect_args={"check_same_thread": False, "timeout": 30}, hide_parameters=True)
        metadata.create_all(self.engine)
        self.dialect_name = "sqlite"

    @classmethod
    def from_settings(cls, settings) -> "Database":
        instance = cls.__new__(cls)
        instance.path = None
        url = sa.URL.create("mysql+pymysql", username=settings.db_user, password=settings.db_password,
            host=settings.db_host, port=settings.db_port, database=settings.db_name, query={"charset": "utf8mb4"})
        instance.engine = sa.create_engine(url, pool_pre_ping=True, pool_recycle=1800,
            isolation_level="READ COMMITTED", hide_parameters=True,
            connect_args={"connect_timeout": 5, "read_timeout": 30, "write_timeout": 30})
        instance.dialect_name = "mysql"
        try:
            with instance.engine.connect() as conn:
                version = str(conn.execute(sa.text("SELECT VERSION()")).scalar_one())
                validate_mysql_version(version)
        except BaseException:
            instance.engine.dispose()
            raise
        return instance

    @contextmanager
    def transaction(self, *, write: bool = True) -> Iterator[Connection]:
        with self.engine.connect() as raw:
            try:
                if self.dialect_name == "sqlite" and write:
                    raw.exec_driver_sql("BEGIN IMMEDIATE")
                else:
                    raw.begin()
                yield Connection(raw, write=write)
                raw.commit()
            except BaseException:
                raw.rollback()
                raise

    def run_transaction(self, callback, *, write: bool = True, attempts: int = 3):
        """Bounded replay of a DB-only callback; never wrap provider submissions."""
        if not 1 <= attempts <= 3:
            raise ValueError("attempts must be between 1 and 3")
        for attempt in range(attempts):
            try:
                with self.transaction(write=write) as conn:
                    return callback(conn)
            except sa.exc.DBAPIError as exc:
                if attempt + 1 == attempts or not is_retryable_transaction_error(exc):
                    raise
                time.sleep(0.01 * (attempt + 1))

    def close(self) -> None:
        self.engine.dispose()
