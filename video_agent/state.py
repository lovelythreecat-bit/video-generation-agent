import json
import sqlite3
import uuid

from video_agent.errors import VideoAgentError
from video_agent.models import AudioOptions, FailureView, ProductInfo, TaskView, VideoOptions


def dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False)


def has_plan(conn: sqlite3.Connection, task_id: str) -> bool:
    row = conn.execute("SELECT 1 AS ok FROM plan_versions WHERE task_id=? LIMIT 1", (task_id,)).fetchone()
    return row is not None


def assert_mutation_allowed(task: sqlite3.Row, method: str, *, has_plan: bool) -> None:
    status = task["status"]
    scope = task["failure_scope"]
    confirmed = task["confirmed_plan_version"] is not None
    head = task["head_plan_version"]
    head_unconfirmed = head is not None and head != task["confirmed_plan_version"]
    task_id = task["task_id"]

    def deny() -> None:
        raise VideoAgentError("STATE_CONFLICT", "当前状态不允许该操作", task_id=task_id)

    if method in {"regenerate", "discard_revision"} and not confirmed:
        deny()
    if status in {"pending", "planning", "generating"}:
        deny()
    if status == "awaiting_input":
        if method == "submit_inputs" or (method == "discard_revision" and confirmed):
            return
        deny()
    if status == "awaiting_confirmation":
        if method in {"revise_plan", "confirm"}:
            return
        if method in {"regenerate", "discard_revision"} and confirmed:
            return
        deny()
    if status == "succeeded":
        if method in {"revise_plan", "regenerate"}:
            return
        deny()
    if status == "failed" and scope == "planning":
        if method == "retry_planning":
            return
        if method == "submit_inputs" and not has_plan:
            return
        if method == "revise_plan" and head is not None:
            return
        if method == "discard_revision" and confirmed:
            return
        deny()
    if status == "failed" and scope == "generation":
        if method in {"retry_generation", "regenerate", "revise_plan"}:
            return
        if method in {"confirm", "discard_revision"} and head_unconfirmed:
            return
        deny()
    deny()


def task_row_to_view(row: sqlite3.Row) -> TaskView:
    status = row["status"]
    stage = row["stage"] if status in {"planning", "generating", "failed"} else None
    failure = None
    failure_scope = None
    if status == "failed":
        failure_scope = row["failure_scope"]
        if row["failure_code"]:
            failure = FailureView(
                code=row["failure_code"],
                message=row["failure_message"] or "",
                stage=row["failure_stage"],
                retryable=bool(row["failure_retryable"]),
            )
    product = json.loads(row["product_info_json"])
    return TaskView(
        task_id=row["task_id"],
        status=status,
        stage=stage,
        stage_detail=row["stage_detail"] if stage is not None else None,
        failure_scope=failure_scope,
        failure=failure,
        questions=json.loads(row["questions_json"]) if status == "awaiting_input" else [],
        external_user_id=row["external_user_id"],
        external_request_id=row["external_request_id"],
        head_plan_version=row["head_plan_version"],
        confirmed_plan_version=row["confirmed_plan_version"],
        generation_id=row["generation_id"],
        completed_shots=row["completed_shots"] if status == "generating" else None,
        total_shots=row["total_shots"] if status == "generating" else None,
        product_name=product.get("name"),
        product_info=ProductInfo.model_validate(product),
        user_prompt=row["user_prompt"],
        video_options=VideoOptions.model_validate_json(row["video_options_json"]),
        audio_options=AudioOptions.model_validate_json(row["audio_options_json"]),
        image_asset_ids=json.loads(row["image_asset_ids_json"]),
        first_frame_asset_id=row["first_frame_asset_id"],
        updated_at=row["updated_at"],
    )


def read_task_row(conn: sqlite3.Connection, task_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    if row is None:
        raise VideoAgentError("NOT_FOUND", "任务不存在", task_id=task_id)
    return row


def read_task_view(conn: sqlite3.Connection, task_id: str) -> TaskView:
    return task_row_to_view(read_task_row(conn, task_id))


def insert_work(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    kind: str,
    generation_id: str | None,
    ready_at: str,
    copied_config: dict,
    step: str,
) -> str:
    work_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO work_items (
            work_id, task_id, kind, generation_id, ready_at, lease_owner, lease_until,
            copied_config_json, step, next_attempt_at, done
        ) VALUES (?, ?, ?, ?, ?, NULL, NULL, ?, ?, NULL, 0)""",
        (work_id, task_id, kind, generation_id, ready_at, dumps(copied_config), step),
    )
    return work_id
