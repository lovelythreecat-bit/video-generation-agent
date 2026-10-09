from __future__ import annotations

import json
import shutil
from contextlib import contextmanager
from pathlib import Path

from video_agent.errors import VideoAgentError
from video_agent.media import fit_image
from video_agent.models import PlanDocument

_STAGE = {
    "first_frame": "preparing_first_frame",
    "submit_shot": "generating_shots",
    "import_shot": "generating_shots",
    "audio": "preparing_audio",
    "compose": "composing",
    "visual": "checking_visuals",
    "export": "exporting",
}
_DETAIL = {
    "first_frame": "正在确定首帧",
    "submit_shot": "正在生成镜头",
    "import_shot": "正在导入镜头",
    "audio": "正在准备音频",
    "compose": "正在拼接画面",
    "visual": "正在检查画面",
    "export": "正在导出成片",
}


def advance_generation(agent, work_id: str) -> str:
    work = _load_work(agent, work_id)
    step = str(work["step"])
    try:
        _dispatch(agent, work, step)
    except VideoAgentError as exc:
        _fail(agent, work, exc)
    except Exception as exc:
        _fail(
            agent,
            work,
            VideoAgentError("MEDIA_EXPORT_FAILED", str(exc), stage=_STAGE.get(step), retryable=True),
        )
    return step


def _dispatch(agent, work: dict, step: str) -> None:
    if step == "first_frame":
        _step_first_frame(agent, work)
        return
    raise VideoAgentError(
        "MEDIA_EXPORT_FAILED",
        f"未知生成步骤 {step}",
        stage=_STAGE.get(step),
        retryable=False,
    )


def _step_first_frame(agent, work: dict) -> None:
    generation = _require_generation(agent, work["generation_id"])
    document = PlanDocument.model_validate_json(generation["snapshot_json"])
    _ensure_shots(agent, work["generation_id"], document)
    _enter(agent, work, "first_frame")
    if document.first_frame_source != "selected" or not document.first_frame_asset_id:
        raise VideoAgentError(
            "FIRST_FRAME_NOT_IMPLEMENTED",
            "未指定首帧，首帧生成尚未实现",
            stage="preparing_first_frame",
            retryable=False,
        )
    source = _asset_path(agent, document.first_frame_asset_id, "preparing_first_frame")
    root = _gen_dir(agent, work)
    root.mkdir(parents=True, exist_ok=True)
    copied = root / f"first_frame{source.suffix.lower() or '.png'}"
    shutil.copyfile(source, copied)
    width, height = _canvas(agent, document.aspect_ratio)
    fit_image(copied, document.fit, width, height, root / "first_frame_fitted.png")
    _advance(agent, work, "submit_shot")


def _ensure_shots(agent, generation_id: str, document: PlanDocument) -> None:
    with _transaction(agent) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM generation_shots WHERE generation_id=?",
            (generation_id,),
        ).fetchone()[0]
        if count:
            return
        for ordinal, shot in enumerate(document.shots):
            conn.execute(
                """INSERT INTO generation_shots (
                    generation_id, shot_id, ordinal, start_frame, end_frame, status,
                    provider_idempotency_key, external_task_id, submission_state, clip_path
                ) VALUES (?, ?, ?, ?, ?, 'pending', NULL, NULL, NULL, NULL)""",
                (generation_id, shot.shot_id, ordinal, shot.start_frame, shot.end_frame),
            )


def _enter(agent, work: dict, step: str) -> None:
    now = agent.clock.now().isoformat()
    with _transaction(agent) as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM generation_shots WHERE generation_id=?",
            (work["generation_id"],),
        ).fetchone()[0]
        done = conn.execute(
            "SELECT COUNT(*) FROM generation_shots WHERE generation_id=? AND status='imported'",
            (work["generation_id"],),
        ).fetchone()[0]
        conn.execute(
            """UPDATE generations
               SET status='running', stage=?, stage_detail=?, completed_shots=?, total_shots=?, updated_at=?
               WHERE generation_id=?""",
            (_STAGE[step], _DETAIL[step], done, total, now, work["generation_id"]),
        )
        conn.execute(
            """UPDATE tasks
               SET status='generating', stage=?, stage_detail=?, completed_shots=?, total_shots=?, updated_at=?
               WHERE task_id=?""",
            (_STAGE[step], _DETAIL[step], done, total, now, work["task_id"]),
        )


def _advance(agent, work: dict, step: str) -> None:
    with _transaction(agent) as conn:
        conn.execute(
            """UPDATE work_items
               SET step=?, lease_owner=NULL, lease_until=NULL, next_attempt_at=NULL
               WHERE work_id=?""",
            (step, work["work_id"]),
        )


def _fail(agent, work: dict, exc: VideoAgentError) -> None:
    now = agent.clock.now().isoformat()
    stage = exc.stage or _STAGE.get(str(work["step"]))
    details = json.dumps(
        {"shot_ids": exc.shot_ids or [], "fields": exc.fields or [], "details": exc.details or {}},
        ensure_ascii=False,
    )
    retryable = 1 if exc.retryable else 0
    with _transaction(agent) as conn:
        conn.execute(
            """UPDATE generations
               SET status='failed', stage=?, stage_detail=?, failure_code=?, failure_message=?,
                   failure_stage=?, failure_retryable=?, failure_details_json=?, updated_at=?
               WHERE generation_id=?""",
            (stage, exc.message, exc.code, exc.message, stage, retryable, details, now, work["generation_id"]),
        )
        conn.execute(
            """UPDATE tasks
               SET status='failed', stage=?, stage_detail=?, failure_scope='generation',
                   failure_code=?, failure_message=?, failure_stage=?, failure_retryable=?,
                   failure_details_json=?, completed_shots=NULL, total_shots=NULL, updated_at=?
               WHERE task_id=?""",
            (stage, exc.message, exc.code, exc.message, stage, retryable, details, now, work["task_id"]),
        )
        for shot_id in exc.shot_ids or []:
            conn.execute(
                "UPDATE generation_shots SET status='failed' WHERE generation_id=? AND shot_id=?",
                (work["generation_id"], shot_id),
            )
        conn.execute(
            """UPDATE work_items
               SET done=1, lease_owner=NULL, lease_until=NULL, next_attempt_at=NULL
               WHERE work_id=?""",
            (work["work_id"],),
        )


def _load_work(agent, work_id: str) -> dict:
    with _transaction(agent) as conn:
        row = _one(conn, "SELECT * FROM work_items WHERE work_id=?", (work_id,))
    if row is None:
        raise VideoAgentError("NOT_FOUND", "找不到生成工作")
    return row


def _require_generation(agent, generation_id: str) -> dict:
    with _transaction(agent) as conn:
        row = _one(conn, "SELECT * FROM generations WHERE generation_id=?", (generation_id,))
    if row is None:
        raise VideoAgentError("NOT_FOUND", "找不到视频执行")
    return row


def _asset_path(agent, asset_id: str, stage: str) -> Path:
    with _transaction(agent) as conn:
        row = _one(conn, "SELECT relative_path FROM assets WHERE asset_id=?", (asset_id,))
    if row is None:
        raise VideoAgentError("ASSET_MISSING", "素材不存在", stage=stage, retryable=True)
    path = agent.files.path_for(row["relative_path"])
    if not path.is_file():
        raise VideoAgentError("ASSET_MISSING", "素材文件不存在", stage=stage, retryable=True)
    return path


def _canvas(agent, aspect_ratio: str) -> tuple[int, int]:
    pair = agent.config.resolutions[aspect_ratio]
    return int(pair[0]), int(pair[1])


def _gen_dir(agent, work: dict) -> Path:
    return agent.files.home / "tasks" / work["task_id"] / "generations" / work["generation_id"]


def _one(conn, sql: str, params: tuple = ()) -> dict | None:
    cursor = conn.execute(sql, params)
    row = cursor.fetchone()
    if row is None:
        return None
    return {column[0]: row[index] for index, column in enumerate(cursor.description)}


@contextmanager
def _transaction(agent):
    with agent.db.transaction() as connection:
        yield connection
