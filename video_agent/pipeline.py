from __future__ import annotations

import json
import os
import shutil
import uuid
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path

from video_agent.errors import VideoAgentError
from video_agent.lease import hold_lease
from video_agent.media import (
    _atempo_pad,
    _concat_audio,
    _frame_rate,
    _pixel_format,
    _run_ffmpeg,
    _silence,
    burn_subtitles,
    concat_clips,
    extract_png,
    fit_image,
    mix_audio,
    probe,
    trim_or_loop_audio,
)
from video_agent.models import PlanDocument, ShotPlan
from video_agent.ports import CheckFrame, ShotRequest, VisualResult, VoiceCue

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
    if step == "audio":
        _step_audio(agent, work)
        return
    if step == "compose":
        _step_compose(agent, work)
        return
    if step == "visual":
        _step_visual(agent, work)
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


def _step_first_frame(agent, work: dict) -> None:
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
    root = _gen_dir(agent, work)
    root.mkdir(parents=True, exist_ok=True)
    copied = root / f"first_frame{source.suffix.lower() or '.png'}"
    if not copied.is_file():
        shutil.copyfile(source, copied)
    width, height = _canvas(agent, document.aspect_ratio)
    fitted = root / "first_frame_fitted.png"
    if not fitted.is_file():
        fit_image(copied, document.fit, width, height, fitted)
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
            "SELECT COUNT(*) FROM generation_shots WHERE generation_id=? AND status!='reused'",
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
        _mark_submission(
            agent,
            work["generation_id"],
            shot["shot_id"],
            "uncertain",
            None,
            "pending",
        )
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
    generation = _require_generation(agent, work["generation_id"])
    first_path, tail_path, extras = _neighbor_media(agent, work, generation, document, shot)
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


def _step_audio(agent, work: dict) -> None:
    _enter(agent, work, "audio")
    generation = _require_generation(agent, work["generation_id"])
    document = PlanDocument.model_validate_json(generation["snapshot_json"])
    root = _gen_dir(agent, work)
    if not rebuilds_audio(str(generation["kind"])):
        _copy_base_audio(agent, generation, root, document)
        _advance(agent, work, "compose")
        return
    if not document.voiceover and not document.music:
        _advance(agent, work, "compose")
        return
    if document.voiceover and agent.voice is None and not (root / "voice.m4a").is_file():
        raise VideoAgentError(
            "PROVIDER_REJECTED",
            "配音已开启，但没有可用的配音实现",
            stage="preparing_audio",
            retryable=True,
        )
    bitrate = int(agent.config.audio_bitrate)
    if document.voiceover and not (root / "voice.m4a").is_file():
        _build_voice(agent, work, document, root, bitrate)
    if document.music and not (root / "music.m4a").is_file():
        _build_music(agent, work, document, root, bitrate)
    _advance(agent, work, "compose")


def _build_voice(agent, work: dict, document: PlanDocument, root: Path, audio_bitrate: int) -> None:
    pieces: list[Path] = []
    cues: list[VoiceCue] = []
    for shot in document.shots:
        window_ms = (shot.end_frame - shot.start_frame) * 1000 // 30
        window_seconds = window_ms / 1000
        segment = root / f"voice-{shot.shot_id}.m4a"
        if window_ms <= 0:
            raise VideoAgentError("MEDIA_EXPORT_FAILED", "镜头帧区间为空", stage="preparing_audio", retryable=True, shot_ids=[shot.shot_id])
        if not shot.voiceover_text.strip():
            _silence(window_seconds, segment, audio_bitrate)
            pieces.append(segment)
            continue
        with hold_lease(agent.db, work["work_id"], agent.worker_owner, agent.clock, int(agent.config.lease_seconds)):
            result = agent.voice.synthesize(
                text=shot.voiceover_text,
                language=document.language,
                preference=document.voice_preference,
            )
        speed = 1.0
        if result.duration_ms > window_ms:
            speed = result.duration_ms / window_ms
        if speed > 2.0:
            raise VideoAgentError(
                "VOICEOVER_DOES_NOT_FIT",
                "配音文案放不进已确认时长",
                stage="preparing_audio",
                retryable=False,
                shot_ids=[shot.shot_id],
            )
        raw = root / f"voice-{shot.shot_id}.raw.m4a"
        raw.write_bytes(result.audio)
        _atempo_pad(raw, speed, window_seconds, segment, audio_bitrate)
        offset = shot.start_frame * 1000 // 30
        window_end = shot.end_frame * 1000 // 30
        for cue in result.cues:
            start = min(max(int(cue.start_ms / speed) + offset, offset), window_end)
            end = min(max(int(cue.end_ms / speed) + offset, start + 1), window_end)
            cues.append(VoiceCue(text=cue.text, start_ms=start, end_ms=end))
        pieces.append(segment)
    _concat_audio(pieces, root / "voice.m4a", audio_bitrate)
    (root / "cues.json").write_text(
        json.dumps([cue.model_dump() for cue in cues], ensure_ascii=False),
        encoding="utf-8",
    )


def _build_music(agent, work: dict, document: PlanDocument, root: Path, audio_bitrate: int) -> None:
    dest = root / "music.m4a"
    if document.music_asset_id:
        source = _asset_path(agent, document.music_asset_id, "preparing_audio")
        trim_or_loop_audio(source, float(document.duration_seconds), dest, audio_bitrate=audio_bitrate)
        return
    if agent.music is None:
        raise VideoAgentError(
            "PROVIDER_REJECTED",
            "音乐已开启，但没有可用的音乐实现或已登记音频",
            stage="preparing_audio",
            retryable=True,
        )
    with hold_lease(agent.db, work["work_id"], agent.worker_owner, agent.clock, int(agent.config.lease_seconds)):
        data = agent.music.generate(direction=document.music_direction, duration_seconds=float(document.duration_seconds))
    raw = root / "music.raw.m4a"
    raw.write_bytes(data)
    trim_or_loop_audio(raw, float(document.duration_seconds), dest, audio_bitrate=audio_bitrate)


def _step_compose(agent, work: dict) -> None:
    _enter(agent, work, "compose")
    generation = _require_generation(agent, work["generation_id"])
    document = PlanDocument.model_validate_json(generation["snapshot_json"])
    clips: list[Path] = []
    with _transaction(agent) as conn:
        rows = conn.execute(
            """SELECT shot_id, status, clip_path FROM generation_shots
               WHERE generation_id=? ORDER BY ordinal""",
            (work["generation_id"],),
        ).fetchall()
    for shot_id, status, clip_path in rows:
        if status not in {"imported", "reused"} or not clip_path:
            raise VideoAgentError("MEDIA_EXPORT_FAILED", "镜头片段尚未导入", stage="composing", retryable=True, shot_ids=[shot_id])
        clips.append(agent.files.path_for(clip_path))
    dest = _gen_dir(agent, work) / "composed.mp4"
    concat_clips(clips, dest)
    info = probe(dest)
    if info.frame_count != document.duration_seconds * 30 or info.has_audio:
        raise VideoAgentError(
            "MEDIA_EXPORT_FAILED",
            "拼接后的帧数或音轨不符合无字幕画面",
            stage="composing",
            retryable=True,
        )
    _advance(agent, work, "visual")


def _step_visual(agent, work: dict) -> None:
    _enter(agent, work, "visual")
    generation = _require_generation(agent, work["generation_id"])
    document = PlanDocument.model_validate_json(generation["snapshot_json"])
    root = _gen_dir(agent, work)
    composed = root / "composed.mp4"
    frames: list[CheckFrame] = []
    for index, shot in enumerate(document.shots):
        positions = {
            "start": shot.start_frame,
            "middle": (shot.start_frame + shot.end_frame - 1) // 2,
            "end": shot.end_frame - 1,
        }
        for position, frame_index in positions.items():
            dest = root / "frames" / f"{shot.shot_id}-{position}.png"
            extract_png(composed, frame_index, dest)
            frames.append(CheckFrame(shot_id=shot.shot_id, position=position, path=dest))
        if index > 0:
            dest = root / "frames" / f"{shot.shot_id}-boundary.png"
            extract_png(composed, shot.start_frame, dest)
            frames.append(CheckFrame(shot_id=shot.shot_id, position="boundary", path=dest))
    if agent.visual is None:
        result = VisualResult(status="not_assessed", reason="visual adapter is none")
    else:
        try:
            result = agent.visual.check(frames, _appearance(document))
        except Exception as exc:
            result = VisualResult(status="not_assessed", reason=str(exc))
    if result.status not in {"passed", "flagged", "not_assessed"}:
        result = VisualResult(status="not_assessed", reason="视觉检查结果无法识别")
    (root / "quality.json").write_text(result.model_dump_json(), encoding="utf-8")
    _advance(agent, work, "export")


def _step_export(agent, work: dict) -> None:
    _enter(agent, work, "export")
    generation = _require_generation(agent, work["generation_id"])
    document = PlanDocument.model_validate_json(generation["snapshot_json"])
    root = _gen_dir(agent, work)
    width, height = _canvas(agent, document.aspect_ratio)
    video_bitrate = int(agent.config.video_bitrate)
    audio_bitrate = int(agent.config.audio_bitrate)
    picture = root / "composed.mp4"
    voice_cues = _read_cues(root / "cues.json")
    if document.subtitles:
        burned = root / "subtitled.mp4"
        burn_subtitles(
            picture,
            _subtitle_cues(document, voice_cues if document.voiceover else None),
            width,
            height,
            burned,
            video_bitrate=video_bitrate,
        )
        picture = burned
    voice = root / "voice.m4a" if document.voiceover else None
    music = root / "music.m4a" if document.music else None
    tmp = root / "final.tmp.mp4"
    try:
        with hold_lease(agent.db, work["work_id"], agent.worker_owner, agent.clock, int(agent.config.lease_seconds)):
            mix_audio(
                picture,
                voice if voice is not None and voice.is_file() else None,
                music if music is not None and music.is_file() else None,
                voice_cues if document.voiceover and document.music else [],
                tmp,
                video_bitrate=video_bitrate,
                audio_bitrate=audio_bitrate,
            )
        _assert_final(tmp, document, agent)
    except Exception:
        if tmp.is_file():
            tmp.unlink()
        raise
    final = root / "final.mp4"
    os.replace(tmp, final)
    _register_artifact(agent, work, generation, document, final)


def _assert_final(path: Path, document: PlanDocument, agent) -> None:
    info = probe(path)
    width, height = _canvas(agent, document.aspect_ratio)
    expected = document.duration_seconds * 30
    if info.frame_count != expected or info.width != width or info.height != height:
        raise VideoAgentError("MEDIA_EXPORT_FAILED", "成片帧数或尺寸不符合已确认方案", stage="exporting", retryable=True)
    if info.video_codec != "h264" or _pixel_format(path) != "yuv420p" or abs(_frame_rate(path) - 30) > 0.05:
        raise VideoAgentError("MEDIA_EXPORT_FAILED", "成片编码或帧率不符合要求", stage="exporting", retryable=True)
    want_audio = bool(document.voiceover or document.music)
    if info.has_audio != want_audio:
        raise VideoAgentError("MEDIA_EXPORT_FAILED", "成片音轨与开关不一致", stage="exporting", retryable=True)
    if want_audio and info.audio_codec != "aac":
        raise VideoAgentError("MEDIA_EXPORT_FAILED", "成片音轨不是 AAC", stage="exporting", retryable=True)


def _register_artifact(agent, work: dict, generation: dict, document: PlanDocument, final: Path) -> None:
    root = final.parent
    if (root / "quality.json").is_file():
        quality = VisualResult.model_validate_json((root / "quality.json").read_text(encoding="utf-8"))
    else:
        quality = VisualResult(status="not_assessed", reason="缺少视觉检查结果")
    now = agent.clock.now().isoformat()
    relative = f"tasks/{work['task_id']}/generations/{work['generation_id']}/final.mp4"
    width, height = _canvas(agent, document.aspect_ratio)
    with _transaction(agent) as conn:
        current = _one(conn, "SELECT COALESCE(MAX(version), 0) AS version FROM artifacts WHERE task_id=?", (work["task_id"],))
        version = int(current["version"]) + 1
        conn.execute(
            """INSERT INTO artifacts (
                artifact_id, task_id, generation_id, plan_version, version, duration_seconds,
                aspect_ratio, width, height, relative_path, quality_status, quality_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                str(uuid.uuid4()),
                work["task_id"],
                work["generation_id"],
                int(generation["plan_version"]),
                version,
                document.duration_seconds,
                document.aspect_ratio,
                width,
                height,
                relative,
                quality.status,
                quality.model_dump_json(),
                now,
            ),
        )
        conn.execute(
            """UPDATE generations
               SET status='succeeded', stage='exporting', stage_detail='成片已导出',
                   failure_code=NULL, failure_message=NULL, failure_stage=NULL,
                   failure_retryable=NULL, failure_details_json=NULL, updated_at=?
               WHERE generation_id=?""",
            (now, work["generation_id"]),
        )
        task = _one(
            conn,
            "SELECT head_plan_version, confirmed_plan_version FROM tasks WHERE task_id=?",
            (work["task_id"],),
        )
        plan_version = int(generation["plan_version"])
        if task["head_plan_version"] == plan_version and task["confirmed_plan_version"] == plan_version:
            status = "succeeded"
        else:
            status = "awaiting_confirmation"
        conn.execute(
            """UPDATE tasks
               SET status=?, stage=NULL, stage_detail=NULL, failure_scope=NULL,
                   failure_code=NULL, failure_message=NULL, failure_stage=NULL,
                   failure_retryable=NULL, failure_details_json=NULL,
                   completed_shots=NULL, total_shots=NULL, updated_at=?
               WHERE task_id=?""",
            (status, now, work["task_id"]),
        )
        conn.execute(
            """UPDATE work_items
               SET done=1, lease_owner=NULL, lease_until=NULL, next_attempt_at=NULL
               WHERE work_id=?""",
            (work["work_id"],),
        )


def _subtitle_cues(document: PlanDocument, voice_cues: list[VoiceCue] | None) -> list[VoiceCue]:
    cues: list[VoiceCue] = []
    for shot in document.shots:
        if not shot.subtitle_text.strip():
            continue
        window_start = shot.start_frame * 1000 // 30
        window_end = shot.end_frame * 1000 // 30
        if not voice_cues:
            cues.append(VoiceCue(text=shot.subtitle_text, start_ms=window_start, end_ms=window_end))
            continue
        inside = [cue for cue in voice_cues if cue.end_ms > window_start and cue.start_ms < window_end]
        if not inside:
            cues.append(VoiceCue(text=shot.subtitle_text, start_ms=window_start, end_ms=window_end))
            continue
        start = max(window_start, min(cue.start_ms for cue in inside))
        end = min(window_end, max(cue.end_ms for cue in inside))
        if end <= start:
            end = window_end
        cues.append(VoiceCue(text=shot.subtitle_text, start_ms=start, end_ms=end))
    return cues


def _read_cues(path: Path) -> list[VoiceCue]:
    if not path.is_file():
        return []
    return [VoiceCue.model_validate(item) for item in json.loads(path.read_text(encoding="utf-8"))]


def _appearance(document: PlanDocument) -> str:
    visual = document.shared_visual
    parts = [visual.product_reference, visual.character or "", visual.scene, visual.lighting, visual.palette, visual.style]
    return "\n".join(part for part in parts if part)


def rebuilds_audio(kind: str) -> bool:
    return kind == "full"


def _base_generation_dir(agent, generation: dict) -> Path | None:
    if not generation.get("base_artifact_id"):
        return None
    with _transaction(agent) as conn:
        row = _one(
            conn,
            "SELECT task_id, generation_id FROM artifacts WHERE artifact_id=?",
            (generation["base_artifact_id"],),
        )
    if row is None:
        return None
    return agent.files.home / "tasks" / row["task_id"] / "generations" / row["generation_id"]


def _copy_base_audio(agent, generation: dict, root: Path, document: PlanDocument) -> None:
    base = _base_generation_dir(agent, generation)
    if base is None:
        raise VideoAgentError("MEDIA_EXPORT_FAILED", "找不到基础成品的执行目录", stage="preparing_audio", retryable=True)
    root.mkdir(parents=True, exist_ok=True)
    for name in ("voice.m4a", "music.m4a", "cues.json"):
        source = base / name
        dest = root / name
        if source.is_file() and not dest.is_file():
            shutil.copyfile(source, dest)
    if document.voiceover and not (root / "voice.m4a").is_file():
        raise VideoAgentError("MEDIA_EXPORT_FAILED", "基础配音不存在", stage="preparing_audio", retryable=True)
    if document.music and not (root / "music.m4a").is_file():
        raise VideoAgentError("MEDIA_EXPORT_FAILED", "基础音乐不存在", stage="preparing_audio", retryable=True)
    if document.subtitles and document.voiceover and not (root / "cues.json").is_file():
        raise VideoAgentError("MEDIA_EXPORT_FAILED", "基础字幕时间不存在", stage="preparing_audio", retryable=True)


def _reuse_base_shots(agent, work: dict, generation: dict, document: PlanDocument) -> None:
    base = _base_generation_dir(agent, generation)
    if base is None:
        raise VideoAgentError("MEDIA_EXPORT_FAILED", "找不到基础成品的执行目录", stage="preparing_first_frame", retryable=True)
    root = _gen_dir(agent, work)
    (root / "shots").mkdir(parents=True, exist_ok=True)
    with _transaction(agent) as conn:
        existing = {
            row[0]
            for row in conn.execute(
                "SELECT shot_id FROM generation_shots WHERE generation_id=?",
                (generation["generation_id"],),
            )
        }
        for index, shot in enumerate(document.shots):
            if shot.shot_id in existing:
                conn.execute(
                    "UPDATE generation_shots SET ordinal=? WHERE generation_id=? AND shot_id=?",
                    (index, generation["generation_id"], shot.shot_id),
                )
                continue
            source = base / "shots" / f"{shot.shot_id}.mp4"
            if not source.is_file():
                raise VideoAgentError(
                    "MEDIA_EXPORT_FAILED",
                    "基础镜头片段不存在",
                    stage="preparing_first_frame",
                    retryable=True,
                    shot_ids=[shot.shot_id],
                )
            dest = root / "shots" / f"{shot.shot_id}.mp4"
            if not dest.is_file():
                shutil.copyfile(source, dest)
            relative = dest.relative_to(agent.files.home).as_posix()
            conn.execute(
                """INSERT INTO generation_shots (
                    generation_id, shot_id, ordinal, start_frame, end_frame, status, clip_path
                ) VALUES (?, ?, ?, ?, ?, 'reused', ?)""",
                (generation["generation_id"], shot.shot_id, index, shot.start_frame, shot.end_frame, relative),
            )


def _replacing_ids(generation: dict, document: PlanDocument) -> set[str]:
    if generation["kind"] == "full":
        return {shot.shot_id for shot in document.shots}
    return set(json.loads(generation["shot_ids_json"]))


def _neighbor_media(agent, work: dict, generation: dict, document: PlanDocument, shot_row: dict):
    plan_shots = list(document.shots)
    index = next(position for position, item in enumerate(plan_shots) if item.shot_id == shot_row["shot_id"])
    replacing = _replacing_ids(generation, document)
    fitted = _gen_dir(agent, work) / "first_frame_fitted.png"
    first_path = fitted if fitted.is_file() and (len(plan_shots) == 1 or index == 0) else None
    tail_path = None
    if len(plan_shots) > 1 and index > 0:
        left = plan_shots[index - 1]
        if left.shot_id in replacing:
            clip = _gen_dir(agent, work) / "shots" / f"{left.shot_id}.mp4"
        else:
            base = _base_generation_dir(agent, generation)
            clip = None if base is None else base / "shots" / f"{left.shot_id}.mp4"
        if clip is None or not clip.is_file():
            raise VideoAgentError("MEDIA_EXPORT_FAILED", "左侧相邻镜头片段不存在", stage="generating_shots", retryable=True, shot_ids=[left.shot_id])
        dest = _gen_dir(agent, work) / "frames" / f"{left.shot_id}-taken-end.png"
        dest.parent.mkdir(parents=True, exist_ok=True)
        extract_png(clip, probe(clip).frame_count - 1, dest)
        first_path = dest
    if len(plan_shots) > 1 and index + 1 < len(plan_shots):
        right = plan_shots[index + 1]
        if right.shot_id not in replacing:
            base = _base_generation_dir(agent, generation)
            clip = None if base is None else base / "shots" / f"{right.shot_id}.mp4"
            if clip is None or not clip.is_file():
                raise VideoAgentError("MEDIA_EXPORT_FAILED", "右侧相邻镜头片段不存在", stage="generating_shots", retryable=True, shot_ids=[right.shot_id])
            dest = _gen_dir(agent, work) / "frames" / f"{right.shot_id}-taken-start.png"
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

