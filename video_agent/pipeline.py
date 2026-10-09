from __future__ import annotations

import json
import os
import shutil
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path

from video_agent.errors import VideoAgentError
from video_agent.lease import hold_lease
from video_agent.media import _run_ffmpeg, fit_image, probe
from video_agent.models import PlanDocument, ShotPlan
from video_agent.ports import ShotRequest

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
    if step == "submit_shot":
        _step_submit_shot(agent, work)
        return
    if step == "import_shot":
        _step_import_shot(agent, work)
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


_ALLOWED = {
    "VALIDATION_ERROR",
    "NOT_FOUND",
    "STATE_CONFLICT",
    "PLAN_VERSION_CONFLICT",
    "IDEMPOTENCY_CONFLICT",
    "FIRST_FRAME_NOT_IMPLEMENTED",
    "VOICEOVER_DOES_NOT_FIT",
    "SHOT_DURATION_SHORT",
    "ASSET_MISSING",
    "PROVIDER_TEMPORARY",
    "PROVIDER_REJECTED",
    "PROVIDER_SUBMISSION_UNCERTAIN",
    "MEDIA_EXPORT_FAILED",
}
_RETRYABLE = {
    "VALIDATION_ERROR": False,
    "NOT_FOUND": False,
    "STATE_CONFLICT": False,
    "PLAN_VERSION_CONFLICT": False,
    "IDEMPOTENCY_CONFLICT": False,
    "FIRST_FRAME_NOT_IMPLEMENTED": False,
    "VOICEOVER_DOES_NOT_FIT": False,
    "SHOT_DURATION_SHORT": True,
    "ASSET_MISSING": True,
    "PROVIDER_TEMPORARY": True,
    "PROVIDER_REJECTED": True,
    "PROVIDER_SUBMISSION_UNCERTAIN": True,
    "MEDIA_EXPORT_FAILED": True,
}


def _step_submit_shot(agent, work: dict) -> None:
    _enter(agent, work, "submit_shot")
    shot = _next_shot(agent, work["generation_id"])
    if shot is None:
        _advance(agent, work, "audio")
        return
    if shot["submission_state"] in {"intended", "accepted", "uncertain"}:
        _advance(agent, work, "import_shot")
        return
    generation = _require_generation(agent, work["generation_id"])
    document = PlanDocument.model_validate_json(generation["snapshot_json"])
    request = _shot_request(agent, work, shot, document)
    _mark_intended(agent, work["generation_id"], shot["shot_id"], request.provider_idempotency_key)
    with hold_lease(agent.db, work["work_id"], agent.worker_owner, agent.clock, int(agent.config.lease_seconds)):
        try:
            result = agent.video.submit_shot(request)
        except VideoAgentError:
            raise
        except Exception as exc:
            raise VideoAgentError(
                "PROVIDER_TEMPORARY",
                str(exc),
                stage="generating_shots",
                retryable=True,
                shot_ids=[shot["shot_id"]],
            ) from exc
    if result.uncertain or not result.external_task_id:
        _mark_submission(
            agent,
            work["generation_id"],
            shot["shot_id"],
            "uncertain",
            result.external_task_id,
            "pending",
        )
        raise VideoAgentError(
            "PROVIDER_SUBMISSION_UNCERTAIN",
            "无法确认外部任务是否已经创建",
            stage="generating_shots",
            retryable=True,
            shot_ids=[shot["shot_id"]],
        )
    _mark_submission(
        agent,
        work["generation_id"],
        shot["shot_id"],
        "accepted",
        result.external_task_id,
        "submitted",
    )
    _advance(agent, work, "import_shot")


def _step_import_shot(agent, work: dict) -> None:
    _enter(agent, work, "import_shot")
    shot = _next_shot(agent, work["generation_id"])
    if shot is None:
        _advance(agent, work, "audio")
        return
    if not shot["external_task_id"]:
        raise VideoAgentError(
            "PROVIDER_SUBMISSION_UNCERTAIN",
            "无法确认外部任务是否已经创建",
            stage="generating_shots",
            retryable=True,
            shot_ids=[shot["shot_id"]],
        )
    with hold_lease(agent.db, work["work_id"], agent.worker_owner, agent.clock, int(agent.config.lease_seconds)):
        queried = agent.video.query(
            external_task_id=shot["external_task_id"],
            provider_idempotency_key=shot["provider_idempotency_key"],
        )
    if queried.state == "pending":
        _defer(agent, work)
        return
    if queried.state == "absent":
        raise VideoAgentError(
            "PROVIDER_SUBMISSION_UNCERTAIN",
            "无法确认外部任务是否已经创建",
            stage="generating_shots",
            retryable=True,
            shot_ids=[shot["shot_id"]],
        )
    if queried.state == "failed":
        code = queried.error_code if queried.error_code in _ALLOWED else "PROVIDER_REJECTED"
        raise VideoAgentError(
            code,
            queried.message or "镜头生成失败",
            stage="generating_shots",
            retryable=_RETRYABLE[code],
            shot_ids=[shot["shot_id"]],
        )
    with hold_lease(agent.db, work["work_id"], agent.worker_owner, agent.clock, int(agent.config.lease_seconds)):
        data = agent.video.download(shot["external_task_id"])
    root = _gen_dir(agent, work)
    dest = root / "shots" / f"{shot['shot_id']}.mp4"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    needed = int(shot["end_frame"]) - int(shot["start_frame"])
    info = probe(dest)
    if info.frame_count < needed:
        raise VideoAgentError(
            "SHOT_DURATION_SHORT",
            "模型片段短于镜头帧区间",
            stage="generating_shots",
            retryable=True,
            shot_ids=[shot["shot_id"]],
        )
    if info.frame_count > needed:
        trimmed = dest.with_suffix(".trim.mp4")
        _trim_clip(dest, needed, trimmed, int(agent.config.video_bitrate))
        os.replace(trimmed, dest)
        info = probe(dest)
        if info.frame_count != needed:
            raise VideoAgentError(
                "MEDIA_EXPORT_FAILED",
                "裁切后帧数仍不等于镜头区间",
                stage="generating_shots",
                retryable=True,
                shot_ids=[shot["shot_id"]],
            )
    relative = dest.relative_to(agent.files.home).as_posix()
    _mark_imported(agent, work["generation_id"], shot["shot_id"], relative)
    if _next_shot(agent, work["generation_id"]) is None:
        _advance(agent, work, "audio")
    else:
        _advance(agent, work, "submit_shot")


def _defer(agent, work: dict) -> None:
    now = agent.clock.now()
    later = (now + timedelta(seconds=1)).isoformat()
    until = (now + timedelta(seconds=int(agent.config.lease_seconds))).isoformat()
    with _transaction(agent) as conn:
        conn.execute(
            """UPDATE work_items
               SET next_attempt_at=?, lease_owner=?, lease_until=?
               WHERE work_id=?""",
            (later, agent.worker_owner, until, work["work_id"]),
        )


def _shot_request(agent, work: dict, shot: dict, document: PlanDocument) -> ShotRequest:
    plan_shot = _plan_shot(document, shot["shot_id"])
    width, height = _canvas(agent, document.aspect_ratio)
    references = [_asset_path(agent, asset_id, "generating_shots") for asset_id in _image_ids(agent, work["task_id"])]
    fitted = _gen_dir(agent, work) / "first_frame_fitted.png"
    key = shot["provider_idempotency_key"] or f"{work['generation_id']}:{shot['shot_id']}"
    return ShotRequest(
        generation_id=work["generation_id"],
        shot_id=shot["shot_id"],
        prompt=_prompt(document, plan_shot),
        shared_visual=document.shared_visual,
        first_frame_path=fitted if fitted.is_file() else None,
        reference_paths=references,
        tail_frame_path=None,
        frame_count=int(shot["end_frame"]) - int(shot["start_frame"]),
        aspect_ratio=document.aspect_ratio,
        fit=document.fit,
        width=width,
        height=height,
        provider_idempotency_key=key,
    )


def _next_shot(agent, generation_id: str) -> dict | None:
    with _transaction(agent) as conn:
        return _one(
            conn,
            """SELECT * FROM generation_shots
               WHERE generation_id=? AND status NOT IN ('imported', 'reused')
               ORDER BY ordinal LIMIT 1""",
            (generation_id,),
        )


def _mark_intended(agent, generation_id: str, shot_id: str, key: str) -> None:
    with _transaction(agent) as conn:
        conn.execute(
            """UPDATE generation_shots
               SET provider_idempotency_key=?, submission_state='intended'
               WHERE generation_id=? AND shot_id=?""",
            (key, generation_id, shot_id),
        )


def _mark_submission(agent, generation_id: str, shot_id: str, state: str, external_task_id: str | None, status: str) -> None:
    with _transaction(agent) as conn:
        conn.execute(
            """UPDATE generation_shots
               SET submission_state=?, external_task_id=?, status=?
               WHERE generation_id=? AND shot_id=?""",
            (state, external_task_id, status, generation_id, shot_id),
        )


def _mark_imported(agent, generation_id: str, shot_id: str, relative: str) -> None:
    with _transaction(agent) as conn:
        conn.execute(
            """UPDATE generation_shots
               SET status='imported', clip_path=?
               WHERE generation_id=? AND shot_id=?""",
            (relative, generation_id, shot_id),
        )


def _trim_clip(src: Path, frames: int, dest: Path, video_bitrate: int) -> None:
    _run_ffmpeg(
        [
            "-i",
            str(src),
            "-frames:v",
            str(frames),
            "-r",
            "30",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-preset",
            "ultrafast",
            "-b:v",
            str(video_bitrate),
            "-an",
            str(dest),
        ]
    )


def _image_ids(agent, task_id: str) -> list[str]:
    with _transaction(agent) as conn:
        row = _one(conn, "SELECT image_asset_ids_json FROM tasks WHERE task_id=?", (task_id,))
    if row is None:
        raise VideoAgentError("NOT_FOUND", "找不到任务")
    return list(json.loads(row["image_asset_ids_json"]))


def _plan_shot(document: PlanDocument, shot_id: str) -> ShotPlan:
    for shot in document.shots:
        if shot.shot_id == shot_id:
            return shot
    raise VideoAgentError("NOT_FOUND", "找不到镜头", shot_ids=[shot_id])


def _prompt(document: PlanDocument, shot: ShotPlan) -> str:
    visual = document.shared_visual
    return "\n".join(
        [
            visual.product_reference,
            visual.scene,
            visual.lighting,
            visual.palette,
            visual.style,
            shot.content,
            shot.action,
            shot.shot_size,
            shot.camera,
        ]
    )
