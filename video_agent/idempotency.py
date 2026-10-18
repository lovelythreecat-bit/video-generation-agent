import hashlib
import json
import re
from datetime import datetime, timezone
from video_agent.db import Connection

from video_agent.errors import VideoAgentError
from video_agent.models import (
    CreateTaskRequest,
    InputPatch,
    PlanChange,
    RegenerateRequest,
    TaskView,
)

_KEY = re.compile(r"^[\x21-\x7e]{1,128}$")


def validate_idempotency_key(key: str | None) -> None:
    if key is None:
        return
    if _KEY.fullmatch(key) is None:
        raise VideoAgentError(
            "VALIDATION_ERROR",
            "幂等键必须是 1 到 128 个可见 ASCII 字符",
            fields=["idempotency_key"],
        )


def request_hash(payload: dict) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def create_task_payload(request: CreateTaskRequest) -> dict:
    return request.model_dump(mode="json")


def submit_inputs_payload(task_id: str, patch: InputPatch) -> dict:
    return {"task_id": task_id, "patch": patch.model_dump(mode="json")}


def revise_plan_payload(task_id: str, change: PlanChange) -> dict:
    return {"task_id": task_id, "change": change.model_dump(mode="json")}


def discard_revision_payload(task_id: str) -> dict:
    return {"task_id": task_id}


def confirm_payload(task_id: str, plan_version: int) -> dict:
    return {"task_id": task_id, "plan_version": plan_version}


def regenerate_payload(task_id: str, request: RegenerateRequest) -> dict:
    return {"task_id": task_id, "request": request.model_dump(mode="json")}


def retry_planning_payload(task_id: str) -> dict:
    return {"task_id": task_id}


def retry_generation_payload(generation_id: str) -> dict:
    return {"generation_id": generation_id}


def lookup_replay(
    conn: Connection,
    method: str,
    key: str | None,
    payload: dict,
) -> TaskView | None:
    if key is None:
        return None
    # Reserve under the unique key before performing any business mutation.
    # A competing insert waits for commit, then observes the persisted response.
    reserve = """INSERT INTO idempotency
        (method, idempotency_key, request_hash, response_json, created_at)
        VALUES (:method, :key, :request_hash, NULL, :created_at)"""
    reserve += (" ON DUPLICATE KEY UPDATE idempotency_key=idempotency_key" if conn.dialect_name == "mysql"
                else " ON CONFLICT(method, idempotency_key) DO NOTHING")
    conn.execute(reserve, {"method": method, "key": key, "request_hash": request_hash(payload),
                           "created_at": datetime.now(timezone.utc).isoformat()})
    row = conn.execute(
        conn.for_update('SELECT request_hash, response_json FROM idempotency WHERE method=:method AND idempotency_key=:key'),
        {'method': method, 'key': key},
    ).fetchone()
    if row is None:
        return None
    if row["request_hash"] != request_hash(payload):
        raise VideoAgentError("IDEMPOTENCY_CONFLICT", "同一方法的幂等键已被不同参数使用")
    return None if row["response_json"] is None else TaskView.model_validate_json(row["response_json"])


def store_replay(
    conn: Connection,
    *,
    method: str,
    key: str | None,
    payload: dict,
    task_id: str | None,
    view: TaskView,
    now: str,
) -> None:
    if key is None:
        return
    conn.execute(
        """UPDATE idempotency SET task_id=:task_id, response_json=:view_json, created_at=:now
           WHERE method=:method AND idempotency_key=:key AND request_hash=:request_hash_payload""",
        {'method': method, 'key': key, 'request_hash_payload': request_hash(payload), 'task_id': task_id, 'view_json': view.model_dump_json(), 'now': now},
    )
