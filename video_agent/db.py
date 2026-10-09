import re
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

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


class Database:
    def __init__(self, home: Path) -> None:
        home.mkdir(parents=True, exist_ok=True)
        self.path = home / "agent.sqlite"
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        existing = {
            row["name"]
            for row in self._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if not _SCHEMA_TABLES <= existing:
            self._conn.executescript(SCHEMA)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self._lock.acquire()
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        finally:
            self._lock.release()
