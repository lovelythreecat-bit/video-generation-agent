from __future__ import annotations

import json
import os
import shutil
import uuid
from contextlib import contextmanager
from datetime import timedelta
from functools import wraps
from pathlib import Path

from video_agent.errors import VideoAgentError
from video_agent.lease import MAX_AUTO_RETRIES, LeaseLostError, assert_lease, hold_lease, redact_error, owned_transaction
import video_agent.media as media
from video_agent.media import (
    _frame_rate,
    _pixel_format,
    _run_ffmpeg,
    extract_png,
    fit_image,
    probe,
)
from video_agent.models import PlanDocument, ShotPlan
from video_agent.ports import ShotRequest, VisualResult

_STAGE = {
    "first_frame": "preparing_first_frame",
    "submit_shot": "generating_shots",
    "import_shot": "generating_shots",
    "export": "exporting",
}
_DETAIL = {
    "first_frame": "正在确定首帧",
    "submit_shot": "正在生成镜头",
    "import_shot": "正在导入镜头",
    "export": "正在导出成片",
}


def _private_stage(function):
    """Never reuse an interrupted attempt's output or share a writer's paths."""
    @wraps(function)
    def run(agent, work, *args, **kwargs):
        root = _scratch_dir(agent, work) / uuid.uuid4().hex[:8]
        root.mkdir()
        try:
            return function(agent, work, *args, private=root, **kwargs)
        finally:
            shutil.rmtree(root)
    return run


@contextmanager
def _publication_transaction(agent, publication):
    """Publish a completed file set with its SQL transition under the work lock.

    Only short same-volume renames run here. Restore replaced files on a body
    error or deadline expiry while the work row is still locked. A process crash
    can leave a partial set; the unchanged step rebuilds it from a fresh attempt
    directory instead of treating file existence as proof of stage completion.
    """
    with agent.db.transaction() as conn:
        work_id = getattr(agent, '_active_work_id', None)
        locked = assert_lease(conn, work_id, agent.worker_owner, agent.clock) if work_id else None
        moved = []
        try:
            for index, (source, dest) in enumerate(publication.items()):
                dest.parent.mkdir(parents=True, exist_ok=True)
                backup = source.parent / f'.previous-{index}'
                present = dest.is_file()
                if present:
                    os.replace(dest, backup)
                moved.append((source, dest, backup, present, False))
                os.replace(source, dest)
                moved[-1] = (source, dest, backup, present, True)
            yield conn
            if locked and locked['lease_until'] <= agent.clock.now().isoformat():
                raise LeaseLostError()
        except BaseException:
            # Do this before the SQL context rolls back and releases its lock.
            for source, dest, backup, present, published in reversed(moved):
                if published:
                    os.replace(dest, source)
                if present:
                    os.replace(backup, dest)
            raise


def advance_generation(agent, work_id: str) -> str:
    work = _load_work(agent, work_id)
    _sync_config_without_external_id(agent, work)
    work = _load_work(agent, work_id)
    step = str(work["step"])
    # Older workers may have paused in stages removed from the workflow.
    if step in {"audio", "compose", "visual"}:
        step = "export"
        work["step"] = step
    try:
        if step in {'first_frame', 'export'}:
            with hold_lease(agent.db, work_id, agent.worker_owner, agent.clock, int(agent.config.lease_seconds)):
                _dispatch(agent, work, step)
        else:
            _dispatch(agent, work, step)
    except LeaseLostError:
        raise
    except VideoAgentError as exc:
        _handle_generation_error(agent, work, exc)
    except Exception as exc:
        _handle_generation_error(
            agent,
            work,
            VideoAgentError("MEDIA_EXPORT_FAILED", str(exc), stage=_STAGE.get(step), retryable=True),
        )
    return step


def _sync_config_without_external_id(agent, work: dict) -> None:
    with _transaction(agent) as conn:
        external = conn.execute(
            "SELECT 1 FROM generation_shots\n               WHERE generation_id=:p0 AND (external_task_id IS NOT NULL OR submission_state IN ('intended', 'uncertain')) LIMIT 1",
            {'p0': work["generation_id"]},
        ).fetchone()
        if external is not None:
            return
        conn.execute(
            'UPDATE generations SET copied_config_json=:p0 WHERE generation_id=:p1',
            {'p0': work["copied_config_json"], 'p1': work["generation_id"]},
        )


def _provider_config(agent, work: dict, shot: dict) -> dict:
    if shot.get("external_task_id") or shot.get('submission_state') in {'intended', 'uncertain'}:
        generation = _require_generation(agent, work["generation_id"])
        return json.loads(generation["copied_config_json"])
    return json.loads(work["copied_config_json"])


def _remember_provider_config(agent, purpose: str, config: dict) -> None:
    calls = getattr(agent, "provider_config_calls", None)
    if calls is None:
        agent.provider_config_calls = []
        calls = agent.provider_config_calls
    calls.append({"purpose": purpose, "config": config})


def _set_stage_detail(agent, generation_id: str, detail: str) -> None:
    with _transaction(agent) as conn:
        conn.execute(
            'UPDATE generations SET stage_detail=:p0 WHERE generation_id=:p1',
            {'p0': detail, 'p1': generation_id},
        )


def _clear_lease(agent, work: dict) -> None:
    with _transaction(agent) as conn:
        conn.execute(
            'UPDATE work_items SET lease_owner=NULL, lease_until=NULL, done=0 WHERE work_id=:p0',
            {'p0': work["work_id"]},
        )


def _handle_generation_error(agent, work: dict, exc: VideoAgentError) -> None:
    exc = redact_error(agent, exc)
    if exc.code in {"PROVIDER_TEMPORARY", "SHOT_DURATION_SHORT"} and _schedule_generation_retry(agent, work, exc):
        return
    _fail(agent, work, exc)


def _schedule_generation_retry(agent, work: dict, exc: VideoAgentError) -> bool:
    generation = _require_generation(agent, work["generation_id"])
    used = int(generation["auto_attempts_used"])
    if used >= MAX_AUTO_RETRIES:
        return False
    delay = 1.0
    details = exc.details if isinstance(exc.details, dict) else {}
    if "retry_after_seconds" in details:
        try:
            delay = float(details["retry_after_seconds"])
        except (TypeError, ValueError):
            delay = 1.0
    if delay < 0:
        delay = 1.0
    later = (agent.clock.now() + timedelta(seconds=delay)).isoformat()
    now = agent.clock.now().isoformat()
    stage = exc.stage or _STAGE.get(str(work["step"]))
    with _transaction(agent) as conn:
        conn.execute(
            "UPDATE generations\n               SET status='running', stage=:p0, auto_attempts_used=:p1, next_attempt_at=:p2, updated_at=:p3\n               WHERE generation_id=:p4",
            {'p0': stage, 'p1': used + 1, 'p2': later, 'p3': now, 'p4': work["generation_id"]},
        )
        conn.execute(
            "UPDATE tasks\n               SET status='generating', stage=:p0, updated_at=:p1\n               WHERE task_id=:p2",
            {'p0': stage, 'p1': now, 'p2': work["task_id"]},
        )
        conn.execute(
            'UPDATE work_items\n               SET done=0, lease_owner=NULL, lease_until=NULL, next_attempt_at=:p0\n               WHERE work_id=:p1',
            {'p0': later, 'p1': work["work_id"]},
        )
    return True


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
    if step == "export":
        _step_export(agent, work)
        return
    raise VideoAgentError(
        "MEDIA_EXPORT_FAILED",
        f"未知生成步骤 {step}",
        stage=_STAGE.get(step),
        retryable=False,
    )


@_private_stage
def _step_first_frame(agent, work: dict, *, private: Path) -> None:
    generation = _require_generation(agent, work["generation_id"])
    document = PlanDocument.model_validate_json(generation["snapshot_json"])
    _ensure_shots(agent, work["generation_id"], document)
    if generation["kind"] == "shots":
        _reuse_base_shots(agent, work, generation, document)
    _enter(agent, work, "first_frame")
    if document.first_frame_source != "selected" or not document.first_frame_asset_id:
        raise VideoAgentError(
            "FIRST_FRAME_NOT_IMPLEMENTED",
            "未指定首帧，首帧生成尚未实现",
            stage="preparing_first_frame",
            retryable=False,
        )
    source = _asset_path(agent, document.first_frame_asset_id, "preparing_first_frame")
    root = private
    copied = root / f"first_frame{source.suffix.lower() or '.png'}"
    shutil.copyfile(source, copied)
    width, height = _canvas(agent, document.aspect_ratio)
    fitted = root / "first_frame_fitted.png"
    fit_image(copied, document.fit, width, height, fitted)
    shared = _gen_dir(agent, work)
    _advance(agent, work, "submit_shot", publication={copied: shared / copied.name, fitted: shared / fitted.name})


def _ensure_shots(agent, generation_id: str, document: PlanDocument) -> None:
    with _transaction(agent) as conn:
        count = conn.execute(
            'SELECT COUNT(*) AS count FROM generation_shots WHERE generation_id=:p0',
            {'p0': generation_id},
        ).fetchone()['count']
        if count:
            return
        for ordinal, shot in enumerate(document.shots):
            conn.execute(
                "INSERT INTO generation_shots (\n                    generation_id, shot_id, ordinal, start_frame, end_frame, status,\n                    provider_idempotency_key, external_task_id, submission_state, clip_path\n                ) VALUES (:p0, :p1, :p2, :p3, :p4, 'pending', NULL, NULL, NULL, NULL)",
                {'p0': generation_id, 'p1': shot.shot_id, 'p2': ordinal, 'p3': shot.start_frame, 'p4': shot.end_frame},
            )


def _enter(agent, work: dict, step: str) -> None:
    now = agent.clock.now().isoformat()
    with _transaction(agent) as conn:
        total = conn.execute(
            "SELECT COUNT(*) AS count FROM generation_shots WHERE generation_id=:p0 AND status!='reused'",
            {'p0': work["generation_id"]},
        ).fetchone()['count']
        done = conn.execute(
            "SELECT COUNT(*) AS count FROM generation_shots WHERE generation_id=:p0 AND status='imported'",
            {'p0': work["generation_id"]},
        ).fetchone()['count']
        conn.execute(
            "UPDATE generations\n               SET status='running', stage=:p0, stage_detail=:p1, completed_shots=:p2, total_shots=:p3, updated_at=:p4\n               WHERE generation_id=:p5",
            {'p0': _STAGE[step], 'p1': _DETAIL[step], 'p2': done, 'p3': total, 'p4': now, 'p5': work["generation_id"]},
        )
        conn.execute(
            "UPDATE tasks\n               SET status='generating', stage=:p0, stage_detail=:p1, completed_shots=:p2, total_shots=:p3, updated_at=:p4\n               WHERE task_id=:p5",
            {'p0': _STAGE[step], 'p1': _DETAIL[step], 'p2': done, 'p3': total, 'p4': now, 'p5': work["task_id"]},
        )


def _advance(agent, work: dict, step: str, *, publication=None) -> None:
    transaction = _publication_transaction(agent, publication) if publication else _transaction(agent)
    with transaction as conn:
        _set_work_step(conn, work, step)


def _set_work_step(conn, work: dict, step: str) -> None:
    conn.execute(
        'UPDATE work_items\n               SET step=:p0, lease_owner=NULL, lease_until=NULL, next_attempt_at=NULL\n               WHERE work_id=:p1',
        {'p0': step, 'p1': work["work_id"]},
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
            "UPDATE generations\n               SET status='failed', stage=:p0, stage_detail=:p1, failure_code=:p2, failure_message=:p3,\n                   failure_stage=:p4, failure_retryable=:p5, failure_details_json=:p6, updated_at=:p7\n               WHERE generation_id=:p8",
            {'p0': stage, 'p1': exc.message, 'p2': exc.code, 'p3': exc.message, 'p4': stage, 'p5': retryable, 'p6': details, 'p7': now, 'p8': work["generation_id"]},
        )
        conn.execute(
            "UPDATE tasks\n               SET status='failed', stage=:p0, stage_detail=:p1, failure_scope='generation',\n                   failure_code=:p2, failure_message=:p3, failure_stage=:p4, failure_retryable=:p5,\n                   failure_details_json=:p6, completed_shots=NULL, total_shots=NULL, updated_at=:p7\n               WHERE task_id=:p8",
            {'p0': stage, 'p1': exc.message, 'p2': exc.code, 'p3': exc.message, 'p4': stage, 'p5': retryable, 'p6': details, 'p7': now, 'p8': work["task_id"]},
        )
        for shot_id in exc.shot_ids or []:
            conn.execute(
                "UPDATE generation_shots SET status='failed' WHERE generation_id=:p0 AND shot_id=:p1",
                {'p0': work["generation_id"], 'p1': shot_id},
            )
        conn.execute(
            'UPDATE work_items\n               SET done=1, lease_owner=NULL, lease_until=NULL, next_attempt_at=NULL\n               WHERE work_id=:p0',
            {'p0': work["work_id"]},
        )


def _load_work(agent, work_id: str) -> dict:
    with _transaction(agent) as conn:
        row = _one(conn, 'SELECT * FROM work_items WHERE work_id=:p0', {'p0': work_id})
    if row is None:
        raise VideoAgentError("NOT_FOUND", "找不到生成工作")
    return row


def _require_generation(agent, generation_id: str) -> dict:
    with _transaction(agent) as conn:
        row = _one(conn, 'SELECT * FROM generations WHERE generation_id=:p0', {'p0': generation_id})
    if row is None:
        raise VideoAgentError("NOT_FOUND", "找不到视频执行")
    return row


def _asset_path(agent, asset_id: str, stage: str) -> Path:
    with _transaction(agent) as conn:
        row = _one(conn, 'SELECT relative_path FROM assets WHERE asset_id=:p0', {'p0': asset_id})
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


def _scratch_dir(agent, work):
    # Keep the publication source private to this work/owner, and short enough
    # for Windows audio writers that still use MAX_PATH-limited file APIs.
    path = agent.files.home / 'scratch' / agent.worker_owner[:16] / work['work_id'][:16]
    path.mkdir(parents=True, exist_ok=True)
    return path


def _one(conn, sql: str, params=None) -> dict | None:
    cursor = conn.execute(sql, params)
    row = cursor.fetchone()
    if row is None:
        return None
    return dict(row)


@contextmanager
def _transaction(agent):
    work_id = getattr(agent, '_active_work_id', None)
    if work_id is None:
        with agent.db.transaction() as connection:
            yield connection
    else:
        with owned_transaction(agent.db, work_id, agent.worker_owner, agent.clock) as connection:
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
        _advance(agent, work, "export")
        return
    if shot["submission_state"] in {"intended", "uncertain"}:
        if _query_before_submit(agent, work, shot) != "submit":
            return
        shot = _next_shot(agent, work["generation_id"])
        if shot is None:
            _advance(agent, work, "export")
            return
    elif shot["submission_state"] == "accepted":
        _advance(agent, work, "import_shot")
        return
    _submit_new_shot(agent, work, shot)


def _query_before_submit(agent, work: dict, shot: dict) -> str:
    config = _provider_config(agent, work, shot)
    _remember_provider_config(agent, "query", config)
    with hold_lease(agent.db, work["work_id"], agent.worker_owner, agent.clock, int(agent.config.lease_seconds)):
        queried = agent.video.query(
            external_task_id=shot["external_task_id"],
            provider_idempotency_key=shot["provider_idempotency_key"],
        )
    if queried.state == "pending":
        _defer(agent, work)
        return "stop"
    if queried.state == "absent":
        _mark_submission(agent, work["generation_id"], shot["shot_id"], "absent", None, "pending")
        return "submit"
    if queried.state == "failed":
        code = queried.error_code if queried.error_code in _ALLOWED else "PROVIDER_REJECTED"
        raise VideoAgentError(
            code,
            queried.message or "镜头生成失败",
            stage="generating_shots",
            retryable=_RETRYABLE[code],
            shot_ids=[shot["shot_id"]],
        )
    _set_stage_detail(agent, work["generation_id"], "download")
    _advance(agent, work, "import_shot")
    return "stop"


def _submit_new_shot(agent, work: dict, shot: dict) -> None:
    generation = _require_generation(agent, work["generation_id"])
    document = PlanDocument.model_validate_json(generation["snapshot_json"])
    # An asynchronous/local adapter may consume request images again during
    # download or after restart. Neighbor images stay in their immutable private
    # directory for that lifetime rather than being deleted after submission.
    request = _shot_request(agent, work, shot, document)
    _mark_intended(agent, work["generation_id"], shot["shot_id"], request.provider_idempotency_key)
    config = _provider_config(agent, work, shot)
    _remember_provider_config(agent, "submit", config)
    with hold_lease(agent.db, work["work_id"], agent.worker_owner, agent.clock, int(agent.config.lease_seconds)):
        try:
            result = agent.video.submit_shot(request)
        except VideoAgentError as exc:
            if exc.code == "PROVIDER_SUBMISSION_UNCERTAIN":
                _mark_submission(agent, work["generation_id"], shot["shot_id"], "uncertain", None, "pending")
                _clear_lease(agent, work)
                return
            if exc.code == 'PROVIDER_REJECTED':
                _mark_submission(agent, work['generation_id'], shot['shot_id'], 'absent', None, 'pending')
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
        _mark_submission(agent, work["generation_id"], shot["shot_id"], "uncertain", None, "pending")
        _clear_lease(agent, work)
        return
    _mark_submission(agent, work["generation_id"], shot["shot_id"], "accepted", result.external_task_id, "submitted")
    _advance(agent, work, "import_shot")


def _step_import_shot(agent, work: dict) -> None:
    generation = _require_generation(agent, work["generation_id"])
    _enter(agent, work, "import_shot")
    shot = _next_shot(agent, work["generation_id"])
    if shot is None:
        _advance(agent, work, "export")
        return
    if not shot["provider_idempotency_key"] and not shot["external_task_id"]:
        # Older workers committed the prior shot before advancing the work
        # step. Only that durable predecessor permits returning a pristine next
        # shot to submission. Missing evidence on the first shot stays uncertain.
        with _transaction(agent) as conn:
            predecessor = conn.execute(
                "SELECT 1 AS completed FROM generation_shots WHERE generation_id=:id AND ordinal<:ordinal "
                "AND status IN ('imported', 'reused') AND clip_path IS NOT NULL AND clip_path<>'' LIMIT 1",
                {'id': work['generation_id'], 'ordinal': shot['ordinal']},
            ).fetchone()
        if shot['status'] == 'pending' and shot['submission_state'] is None and predecessor is not None:
            _advance(agent, work, 'submit_shot')
            return
        raise VideoAgentError(
            "PROVIDER_SUBMISSION_UNCERTAIN",
            "无法确认外部任务是否已经创建",
            stage="generating_shots",
            retryable=True,
            shot_ids=[shot["shot_id"]],
        )
    if generation["stage_detail"] != "download":
        config = _provider_config(agent, work, shot)
        _remember_provider_config(agent, "query", config)
        with hold_lease(agent.db, work["work_id"], agent.worker_owner, agent.clock, int(agent.config.lease_seconds)):
            queried = agent.video.query(
                external_task_id=shot["external_task_id"],
                provider_idempotency_key=shot["provider_idempotency_key"],
            )
        if queried.state == "pending":
            _defer(agent, work)
            return
        if queried.state == "absent":
            _mark_submission(agent, work["generation_id"], shot["shot_id"], "absent", None, "pending")
            _advance(agent, work, "submit_shot")
            return
        if queried.state == "failed":
            code = queried.error_code if queried.error_code in _ALLOWED else "PROVIDER_REJECTED"
            raise VideoAgentError(
                code,
                queried.message or "镜头生成失败",
                stage="generating_shots",
                retryable=_RETRYABLE[code],
                shot_ids=[shot["shot_id"]],
            )
        _set_stage_detail(agent, work["generation_id"], "download")
    download_id = shot["external_task_id"] or shot["provider_idempotency_key"]
    config = _provider_config(agent, work, shot)
    _remember_provider_config(agent, "download", config)
    try:
        with hold_lease(agent.db, work["work_id"], agent.worker_owner, agent.clock, int(agent.config.lease_seconds)):
            data = agent.video.download(download_id)
    except VideoAgentError:
        _set_stage_detail(agent, work["generation_id"], "download")
        raise
    except Exception as exc:
        _set_stage_detail(agent, work["generation_id"], "download")
        raise VideoAgentError(
            "PROVIDER_TEMPORARY",
            str(exc),
            stage="generating_shots",
            retryable=True,
            shot_ids=[shot["shot_id"]],
            details={"retry_after_seconds": 1},
        ) from exc
    root = _gen_dir(agent, work)
    dest = _scratch_dir(agent, work) / f"{shot['shot_id']}.mp4"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    needed = int(shot["end_frame"]) - int(shot["start_frame"])
    info = probe(dest)
    if info.frame_count < needed:
        dest.unlink(missing_ok=True)
        _set_stage_detail(agent, work["generation_id"], "download")
        raise VideoAgentError(
            "SHOT_DURATION_SHORT",
            "模型片段短于镜头帧区间",
            stage="generating_shots",
            retryable=True,
            shot_ids=[shot["shot_id"]],
        )
    if info.frame_count > needed:
        document = PlanDocument.model_validate_json(generation["snapshot_json"])
        trimmed = dest.with_suffix(".trim.mp4")
        _trim_clip(
            dest,
            needed,
            trimmed,
            int(agent.config.video_bitrate),
            keep_audio=info.has_audio and bool(document.voiceover or document.music),
            audio_bitrate=int(agent.config.audio_bitrate),
        )
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
    published = root / 'shots' / f"{shot['shot_id']}.mp4"
    published.parent.mkdir(parents=True, exist_ok=True)
    relative = published.relative_to(agent.files.home).as_posix()
    _mark_imported(agent, work["generation_id"], shot["shot_id"], relative, source=dest, work=work)


def _defer(agent, work: dict) -> None:
    later = (agent.clock.now() + timedelta(seconds=1)).isoformat()
    with _transaction(agent) as conn:
        conn.execute(
            'UPDATE work_items\n               SET next_attempt_at=:p0, lease_owner=NULL, lease_until=NULL, done=0\n               WHERE work_id=:p1',
            {'p0': later, 'p1': work["work_id"]},
        )


def _shot_request(agent, work: dict, shot: dict, document: PlanDocument, *, private=None) -> ShotRequest:
    plan_shot = _plan_shot(document, shot["shot_id"])
    width, height = _canvas(agent, document.aspect_ratio)
    references = [_asset_path(agent, asset_id, "generating_shots") for asset_id in _image_ids(agent, work["task_id"])]
    generation = _require_generation(agent, work["generation_id"])
    first_path, tail_path, extras = _neighbor_media(agent, work, generation, document, shot, private=private)
    references.extend(extras)
    key = shot["provider_idempotency_key"] or f"{work['generation_id']}:{shot['shot_id']}"
    return ShotRequest(
        generation_id=work["generation_id"],
        shot_id=shot["shot_id"],
        prompt=_prompt(document, plan_shot),
        shared_visual=document.shared_visual,
        first_frame_path=first_path,
        reference_paths=references,
        tail_frame_path=tail_path,
        frame_count=int(shot["end_frame"]) - int(shot["start_frame"]),
        aspect_ratio=document.aspect_ratio,
        fit=document.fit,
        width=width,
        height=height,
        provider_idempotency_key=key,
        with_audio=bool(document.voiceover or document.music),
    )


def _next_shot(agent, generation_id: str) -> dict | None:
    with _transaction(agent) as conn:
        return _one(
            conn,
            "SELECT * FROM generation_shots\n               WHERE generation_id=:p0 AND status NOT IN ('imported', 'reused')\n               ORDER BY ordinal LIMIT 1",
            {'p0': generation_id},
        )


def _mark_intended(agent, generation_id: str, shot_id: str, key: str) -> None:
    with _transaction(agent) as conn:
        conn.execute(
            "UPDATE generation_shots\n               SET provider_idempotency_key=:p0, submission_state='intended'\n               WHERE generation_id=:p1 AND shot_id=:p2",
            {'p0': key, 'p1': generation_id, 'p2': shot_id},
        )


def _mark_submission(agent, generation_id: str, shot_id: str, state: str, external_task_id: str | None, status: str) -> None:
    with _transaction(agent) as conn:
        conn.execute(
            'UPDATE generation_shots\n               SET submission_state=:p0, external_task_id=:p1, status=:p2\n               WHERE generation_id=:p3 AND shot_id=:p4',
            {'p0': state, 'p1': external_task_id, 'p2': status, 'p3': generation_id, 'p4': shot_id},
        )


def _mark_imported(agent, generation_id: str, shot_id: str, relative: str, *, source=None, work=None) -> None:
    publication = {source: agent.files.path_for(relative)} if source is not None else None
    transaction = _publication_transaction(agent, publication) if publication else _transaction(agent)
    with transaction as conn:
        conn.execute(
            "UPDATE generation_shots\n               SET status='imported', clip_path=:p0\n               WHERE generation_id=:p1 AND shot_id=:p2",
            {'p0': relative, 'p1': generation_id, 'p2': shot_id},
        )
        if work is not None:
            remaining = conn.execute(
                "SELECT 1 AS remaining FROM generation_shots WHERE generation_id=:id "
                "AND status NOT IN ('imported', 'reused') LIMIT 1", {'id': generation_id},
            ).fetchone()
            _set_work_step(conn, work, 'export' if remaining is None else 'submit_shot')


def _trim_clip(
    src: Path,
    frames: int,
    dest: Path,
    video_bitrate: int,
    *,
    keep_audio: bool = False,
    audio_bitrate: int = 192000,
) -> None:
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
            *(["-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", str(audio_bitrate)] if keep_audio else ["-an"]),
            str(dest),
        ]
    )


def _image_ids(agent, task_id: str) -> list[str]:
    with _transaction(agent) as conn:
        row = _one(conn, 'SELECT image_asset_ids_json FROM tasks WHERE task_id=:p0', {'p0': task_id})
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
    text = shot.subtitle_text.strip() if document.subtitles else ""
    if text:
        text_direction = (
            "画面文字由视频模型直接生成，与场景和视觉风格融合。仅呈现以下文案："
            + json.dumps(text, ensure_ascii=False)
            + "。遵守镜头内容中的位置和排版要求；未指定时放在画面留白处，"
            "不要遮挡产品。文字简洁清晰、稳定可读，在本镜头结束前退出，"
            "不延续到其他镜头，不增加额外文字，"
            "不使用底部通栏字幕或大块字幕底板。保留产品参考原有的包装和标识。"
        )
    else:
        text_direction = "保留产品参考原有的包装和标识，不要新增标题、卖点文字或字幕。"
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
            text_direction,
        ]
    )


def _generation_clips(agent, work: dict) -> list[Path]:
    clips: list[Path] = []
    with _transaction(agent) as conn:
        rows = conn.execute(
            'SELECT shot_id, status, clip_path FROM generation_shots\n               WHERE generation_id=:p0 ORDER BY ordinal',
            {'p0': work["generation_id"]},
        ).fetchall()
    for row in rows:
        shot_id, status, clip_path = row['shot_id'], row['status'], row['clip_path']
        if status not in {"imported", "reused"} or not clip_path:
            raise VideoAgentError("MEDIA_EXPORT_FAILED", "镜头片段尚未导入", stage="exporting", retryable=True, shot_ids=[shot_id])
        clips.append(agent.files.path_for(clip_path))
    return clips


@_private_stage
def _step_export(agent, work: dict, *, private: Path) -> None:
    _enter(agent, work, "export")
    generation = _require_generation(agent, work["generation_id"])
    document = PlanDocument.model_validate_json(generation["snapshot_json"])
    root = _gen_dir(agent, work)
    video_bitrate = int(agent.config.video_bitrate)
    audio_bitrate = int(agent.config.audio_bitrate)
    clips = _generation_clips(agent, work)
    keep_audio = bool(document.voiceover or document.music) and any(probe(clip).has_audio for clip in clips)
    tmp = private / 'final.tmp.mp4'
    try:
        with hold_lease(agent.db, work["work_id"], agent.worker_owner, agent.clock, int(agent.config.lease_seconds)):
            media.export_clips(
                clips, tmp, keep_audio=keep_audio,
                video_bitrate=video_bitrate, audio_bitrate=audio_bitrate,
            )
        _assert_final(tmp, document, agent, keep_audio)
    except Exception:
        if tmp.is_file():
            tmp.unlink()
        raise
    final = root / "final.mp4"
    _register_artifact(agent, work, generation, document, final, source=tmp)


def _assert_final(path: Path, document: PlanDocument, agent, expect_audio: bool) -> None:
    info = probe(path)
    width, height = _canvas(agent, document.aspect_ratio)
    expected = document.duration_seconds * 30
    if info.frame_count != expected or info.width != width or info.height != height:
        raise VideoAgentError("MEDIA_EXPORT_FAILED", "成片帧数或尺寸不符合已确认方案", stage="exporting", retryable=True)
    if info.video_codec != "h264" or _pixel_format(path) != "yuv420p" or abs(_frame_rate(path) - 30) > 0.05:
        raise VideoAgentError("MEDIA_EXPORT_FAILED", "成片编码或帧率不符合要求", stage="exporting", retryable=True)
    if info.has_audio != expect_audio:
        raise VideoAgentError("MEDIA_EXPORT_FAILED", "成片音轨与模型片段不一致", stage="exporting", retryable=True)
    if expect_audio and info.audio_codec != "aac":
        raise VideoAgentError("MEDIA_EXPORT_FAILED", "成片音轨不是 AAC", stage="exporting", retryable=True)


def _register_artifact(agent, work: dict, generation: dict, document: PlanDocument, final: Path, *, source=None, publication=None) -> None:
    quality = VisualResult(status="not_assessed", reason="视觉检查已移除").model_dump()
    quality["visual_check_enabled"] = False
    now = agent.clock.now().isoformat()
    relative = f"tasks/{work['task_id']}/generations/{work['generation_id']}/final.mp4"
    width, height = _canvas(agent, document.aspect_ratio)
    publication = dict(publication or {})
    if source is not None:
        publication[source] = final
    transaction = _publication_transaction(agent, publication) if publication else _transaction(agent)
    with transaction as conn:
        current = _one(conn, 'SELECT COALESCE(MAX(version), 0) AS version FROM artifacts WHERE task_id=:p0', {'p0': work["task_id"]})
        version = int(current["version"]) + 1
        conn.execute(
            'INSERT INTO artifacts (\n                artifact_id, task_id, generation_id, plan_version, version, duration_seconds,\n                aspect_ratio, width, height, relative_path, quality_status, quality_json, created_at\n            ) VALUES (:p0, :p1, :p2, :p3, :p4, :p5, :p6, :p7, :p8, :p9, :p10, :p11, :p12)',
            {'p0': str(uuid.uuid4()), 'p1': work["task_id"], 'p2': work["generation_id"], 'p3': int(generation["plan_version"]), 'p4': version, 'p5': document.duration_seconds, 'p6': document.aspect_ratio, 'p7': width, 'p8': height, 'p9': relative, 'p10': quality["status"], 'p11': json.dumps(quality, ensure_ascii=False), 'p12': now},
        )
        conn.execute(
            "UPDATE generations\n               SET status='succeeded', stage='exporting', stage_detail='成片已导出',\n                   failure_code=NULL, failure_message=NULL, failure_stage=NULL,\n                   failure_retryable=NULL, failure_details_json=NULL, updated_at=:p0\n               WHERE generation_id=:p1",
            {'p0': now, 'p1': work["generation_id"]},
        )
        task = _one(
            conn,
            'SELECT head_plan_version, confirmed_plan_version FROM tasks WHERE task_id=:p0',
            {'p0': work["task_id"]},
        )
        plan_version = int(generation["plan_version"])
        if task["head_plan_version"] == plan_version and task["confirmed_plan_version"] == plan_version:
            status = "succeeded"
        else:
            status = "awaiting_confirmation"
        conn.execute(
            'UPDATE tasks\n               SET status=:p0, stage=NULL, stage_detail=NULL, failure_scope=NULL,\n                   failure_code=NULL, failure_message=NULL, failure_stage=NULL,\n                   failure_retryable=NULL, failure_details_json=NULL,\n                   completed_shots=NULL, total_shots=NULL, updated_at=:p1\n               WHERE task_id=:p2',
            {'p0': status, 'p1': now, 'p2': work["task_id"]},
        )
        conn.execute(
            'UPDATE work_items\n               SET done=1, lease_owner=NULL, lease_until=NULL, next_attempt_at=NULL\n               WHERE work_id=:p0',
            {'p0': work["work_id"]},
        )


def _base_generation_dir(agent, generation: dict) -> Path | None:
    if not generation.get("base_artifact_id"):
        return None
    with _transaction(agent) as conn:
        row = _one(
            conn,
            'SELECT task_id, generation_id FROM artifacts WHERE artifact_id=:p0',
            {'p0': generation["base_artifact_id"]},
        )
    if row is None:
        return None
    return agent.files.home / "tasks" / row["task_id"] / "generations" / row["generation_id"]


@_private_stage
def _reuse_base_shots(agent, work: dict, generation: dict, document: PlanDocument, *, private: Path) -> None:
    base = _base_generation_dir(agent, generation)
    if base is None:
        raise VideoAgentError("MEDIA_EXPORT_FAILED", "找不到基础成品的执行目录", stage="preparing_first_frame", retryable=True)
    root = _gen_dir(agent, work)
    with _transaction(agent) as conn:
        existing = {
            row['shot_id']
            for row in conn.execute(
                'SELECT shot_id FROM generation_shots WHERE generation_id=:p0',
                {'p0': generation["generation_id"]},
            )
        }
    publication = {}
    for shot in document.shots:
        if shot.shot_id in existing:
            continue
        source = base / "shots" / f"{shot.shot_id}.mp4"
        if not source.is_file():
            raise VideoAgentError(
                "MEDIA_EXPORT_FAILED", "基础镜头片段不存在", stage="preparing_first_frame",
                retryable=True, shot_ids=[shot.shot_id],
            )
        candidate = private / f"{shot.shot_id}.mp4"
        shutil.copyfile(source, candidate)
        publication[candidate] = root / "shots" / candidate.name
    with _publication_transaction(agent, publication) as conn:
        for index, shot in enumerate(document.shots):
            if shot.shot_id in existing:
                conn.execute(
                    'UPDATE generation_shots SET ordinal=:p0 WHERE generation_id=:p1 AND shot_id=:p2',
                    {'p0': index, 'p1': generation["generation_id"], 'p2': shot.shot_id},
                )
                continue
            dest = root / "shots" / f"{shot.shot_id}.mp4"
            relative = dest.relative_to(agent.files.home).as_posix()
            conn.execute(
                "INSERT INTO generation_shots (\n                    generation_id, shot_id, ordinal, start_frame, end_frame, status, clip_path\n                ) VALUES (:p0, :p1, :p2, :p3, :p4, 'reused', :p5)",
                {'p0': generation["generation_id"], 'p1': shot.shot_id, 'p2': index, 'p3': shot.start_frame, 'p4': shot.end_frame, 'p5': relative},
            )


def _replacing_ids(generation: dict, document: PlanDocument) -> set[str]:
    if generation["kind"] == "full":
        return {shot.shot_id for shot in document.shots}
    return set(json.loads(generation["shot_ids_json"]))


def _neighbor_media(agent, work: dict, generation: dict, document: PlanDocument, shot_row: dict, *, private=None):
    plan_shots = list(document.shots)
    index = next(position for position, item in enumerate(plan_shots) if item.shot_id == shot_row["shot_id"])
    current_text = plan_shots[index].subtitle_text.strip() if document.subtitles else ""
    replacing = _replacing_ids(generation, document)
    fitted = _gen_dir(agent, work) / "first_frame_fitted.png"
    first_path = fitted if fitted.is_file() and (len(plan_shots) == 1 or index == 0) else None
    tail_path = None
    if len(plan_shots) > 1 and index > 0:
        left = plan_shots[index - 1]
        left_text = left.subtitle_text.strip() if document.subtitles else ""
        if left_text and left_text != current_text:
            # A model-generated title is baked into the neighbor's pixels.
            # Use the clean reference instead of carrying a different title.
            first_path = fitted if fitted.is_file() else None
        else:
            if left.shot_id in replacing:
                clip = _gen_dir(agent, work) / "shots" / f"{left.shot_id}.mp4"
            else:
                base = _base_generation_dir(agent, generation)
                clip = None if base is None else base / "shots" / f"{left.shot_id}.mp4"
            if clip is None or not clip.is_file():
                raise VideoAgentError("MEDIA_EXPORT_FAILED", "左侧相邻镜头片段不存在", stage="generating_shots", retryable=True, shot_ids=[left.shot_id])
            private = private if private is not None else _scratch_dir(agent, work) / uuid.uuid4().hex[:8]
            dest = private / f"{left.shot_id}-taken-end.png"
            dest.parent.mkdir(parents=True, exist_ok=True)
            extract_png(clip, probe(clip).frame_count - 1, dest)
            first_path = dest
    if len(plan_shots) > 1 and index + 1 < len(plan_shots):
        right = plan_shots[index + 1]
        right_text = right.subtitle_text.strip() if document.subtitles else ""
        if right.shot_id not in replacing and (not right_text or right_text == current_text):
            base = _base_generation_dir(agent, generation)
            clip = None if base is None else base / "shots" / f"{right.shot_id}.mp4"
            if clip is None or not clip.is_file():
                raise VideoAgentError("MEDIA_EXPORT_FAILED", "右侧相邻镜头片段不存在", stage="generating_shots", retryable=True, shot_ids=[right.shot_id])
            private = private if private is not None else _scratch_dir(agent, work) / uuid.uuid4().hex[:8]
            dest = private / f"{right.shot_id}-taken-start.png"
            dest.parent.mkdir(parents=True, exist_ok=True)
            extract_png(clip, 0, dest)
            tail_path = dest
    extras: list[Path] = []
    caps = agent.video.capabilities()
    if first_path is not None and not caps.supports_first_frame:
        extras.append(first_path)
        first_path = fitted if index == 0 and fitted.is_file() else None
    if tail_path is not None and not caps.supports_tail_frame:
        extras.append(tail_path)
        tail_path = None
    return first_path, tail_path, extras

