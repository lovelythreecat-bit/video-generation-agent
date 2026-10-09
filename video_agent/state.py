import json
import sqlite3
import uuid

from video_agent.config import ASPECT_RATIOS
from video_agent.errors import VideoAgentError
from video_agent.frames import redistribute
from video_agent.models import AudioOptions, FailureView, PlanDocument, PlanPatch, ProductInfo, ShotPlan, TaskView, VideoOptions


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


def load_plan_document(conn: sqlite3.Connection, task_id: str, plan_version: int) -> PlanDocument:
    row = conn.execute(
        "SELECT document_json FROM plan_versions WHERE task_id=? AND plan_version=?",
        (task_id, plan_version),
    ).fetchone()
    if row is None:
        raise VideoAgentError("NOT_FOUND", "方案不存在", task_id=task_id, plan_version=plan_version)
    return PlanDocument.model_validate_json(row["document_json"])


def publish_document(conn: sqlite3.Connection, *, task_id: str, document: PlanDocument, now: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(plan_version), 0) + 1 AS n FROM plan_versions WHERE task_id=?",
        (task_id,),
    ).fetchone()
    version = int(row["n"])
    conn.execute(
        """INSERT INTO plan_versions (task_id, plan_version, document_json, abandoned, created_at)
           VALUES (?, ?, ?, 0, ?)""",
        (task_id, version, document.model_dump_json(), now),
    )
    conn.execute(
        """UPDATE tasks SET
            status='awaiting_confirmation', stage=NULL, stage_detail=NULL,
            failure_scope=NULL, failure_code=NULL, failure_message=NULL,
            failure_stage=NULL, failure_retryable=NULL, failure_details_json=NULL,
            questions_json='[]', head_plan_version=?, pending_instruction=NULL,
            completed_shots=NULL, total_shots=NULL, updated_at=?
           WHERE task_id=?""",
        (version, now, task_id),
    )
    return version


def validate_frame_span(shots: list[ShotPlan], total_frames: int) -> None:
    if len(shots) == 0:
        raise VideoAgentError("VALIDATION_ERROR", "方案至少要有一个镜头", fields=["shots"])
    cursor = 0
    for shot in shots:
        if shot.start_frame != cursor or shot.end_frame <= shot.start_frame:
            raise VideoAgentError("VALIDATION_ERROR", "帧区间必须首尾相接", fields=["shots"])
        cursor = shot.end_frame
    if cursor != total_frames:
        raise VideoAgentError("VALIDATION_ERROR", "帧区间必须覆盖整段时长", fields=["shots"])


def apply_patch(
    document: PlanDocument,
    patch: PlanPatch,
    *,
    duration_tiers: list[int],
    native_aspects: list[str],
    image_asset_ids: list[str],
    audio_asset_ids: set[str],
) -> PlanDocument:
    if patch.shots is not None and patch.shot_texts is not None:
        raise VideoAgentError("VALIDATION_ERROR", "shots 和 shot_texts 不能同时出现", fields=["shots", "shot_texts"])
    if patch.clear_first_frame and patch.first_frame_asset_id:
        raise VideoAgentError("VALIDATION_ERROR", "不能同时指定和清除首帧", fields=["first_frame_asset_id"])
    if patch.clear_music_asset and patch.music_asset_id:
        raise VideoAgentError("VALIDATION_ERROR", "不能同时指定和清除音乐素材", fields=["music_asset_id"])
    duration = document.duration_seconds
    if patch.duration_seconds is not None:
        if patch.duration_seconds not in duration_tiers:
            raise VideoAgentError(
                "VALIDATION_ERROR",
                "时长不在当前档位",
                fields=["duration_seconds"],
                details={"duration_tiers": list(duration_tiers)},
            )
        duration = patch.duration_seconds
    aspect = document.aspect_ratio
    fit = document.fit
    if patch.aspect_ratio is not None:
        if patch.aspect_ratio not in ASPECT_RATIOS:
            raise VideoAgentError(
                "VALIDATION_ERROR",
                "画面比例不在预设中",
                fields=["aspect_ratio"],
                details={"aspect_ratios": list(ASPECT_RATIOS)},
            )
        aspect = patch.aspect_ratio
        if patch.fit is not None:
            fit = patch.fit
        elif aspect in native_aspects:
            fit = "native"
        else:
            fit = "pad"
    elif patch.fit is not None:
        fit = patch.fit
    subtitles = document.subtitles if patch.subtitles is None else patch.subtitles
    voiceover = document.voiceover if patch.voiceover is None else patch.voiceover
    music = document.music if patch.music is None else patch.music
    language = document.language if patch.language is None else patch.language
    voice_preference = document.voice_preference if patch.voice_preference is None else patch.voice_preference
    music_direction = document.music_direction if patch.music_direction is None else patch.music_direction
    music_asset_id = None if patch.clear_music_asset else document.music_asset_id
    if patch.music_asset_id is not None:
        if patch.music_asset_id not in audio_asset_ids:
            raise VideoAgentError("VALIDATION_ERROR", "音乐素材不存在或不是音频", fields=["music_asset_id"])
        music_asset_id = patch.music_asset_id
    shared = document.shared_visual if patch.shared_visual is None else patch.shared_visual
    first_source = document.first_frame_source
    first_id = document.first_frame_asset_id
    if patch.clear_first_frame:
        first_source = "generate"
        first_id = None
    elif patch.first_frame_asset_id is not None:
        if patch.first_frame_asset_id not in image_asset_ids:
            raise VideoAgentError("VALIDATION_ERROR", "首帧必须是本任务图片中的一张", fields=["first_frame_asset_id"])
        first_source = "selected"
        first_id = patch.first_frame_asset_id
    if patch.shots is not None:
        shots = [shot.model_copy(deep=True) for shot in patch.shots]
    else:
        shots = [shot.model_copy(deep=True) for shot in document.shots]
        if patch.shot_texts:
            index_by_id = {shot.shot_id: index for index, shot in enumerate(shots)}
            for text in patch.shot_texts:
                index = index_by_id.get(text.shot_id)
                if index is None:
                    raise VideoAgentError("VALIDATION_ERROR", "镜头标识不存在", fields=["shot_texts"], shot_ids=[text.shot_id])
                updates = {}
                for field in ("content", "action", "shot_size", "camera", "scene_id", "subtitle_text", "voiceover_text"):
                    value = getattr(text, field)
                    if value is not None:
                        updates[field] = value
                shots[index] = shots[index].model_copy(update=updates)
        if duration != document.duration_seconds:
            lengths = [shot.end_frame - shot.start_frame for shot in shots]
            resized = []
            cursor = 0
            for shot, length in zip(shots, redistribute(lengths, duration * 30), strict=True):
                resized.append(shot.model_copy(update={"start_frame": cursor, "end_frame": cursor + length}))
                cursor += length
            shots = resized
    validate_frame_span(shots, duration * 30)
    if subtitles and not document.subtitles and any(shot.subtitle_text.strip() == "" for shot in shots):
        raise VideoAgentError("VALIDATION_ERROR", "打开字幕时每个镜头都要有字幕文案", fields=["subtitles"])
    if voiceover and not document.voiceover and any(shot.voiceover_text.strip() == "" for shot in shots):
        raise VideoAgentError("VALIDATION_ERROR", "打开配音时每个镜头都要有配音文案", fields=["voiceover"])
    if music and not document.music and music_direction.strip() == "" and not music_asset_id:
        raise VideoAgentError("VALIDATION_ERROR", "打开音乐时要有音乐方向或已登记音频", fields=["music"])
    return document.model_copy(
        update={
            "shared_visual": shared,
            "shots": shots,
            "music_direction": music_direction,
            "music_asset_id": music_asset_id,
            "duration_seconds": duration,
            "aspect_ratio": aspect,
            "fit": fit,
            "subtitles": subtitles,
            "voiceover": voiceover,
            "music": music,
            "first_frame_source": first_source,
            "first_frame_asset_id": first_id,
            "language": language,
            "voice_preference": voice_preference,
        }
    )
