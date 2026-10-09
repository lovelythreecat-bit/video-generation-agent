import json
from datetime import timedelta

from video_agent.config import load_config
from video_agent.errors import VideoAgentError
from video_agent.lease import MAX_AUTO_RETRIES, hold_lease
from video_agent.models import AudioOptions, PlanDocument, ProductInfo, TickResult, VideoOptions
from video_agent.ports import DEFAULT_VIDEO_CAPABILITIES, PlanRequest
from video_agent.state import apply_patch, dumps, load_plan_document, publish_document, read_task_row, validate_frame_span


def recover_expired(agent) -> int:
    now = agent.clock.now().isoformat()
    with agent.db.transaction() as conn:
        cursor = conn.execute(
            "UPDATE work_items SET lease_owner=NULL, lease_until=NULL WHERE lease_until IS NOT NULL AND lease_until<?",
            (now,),
        )
        return int(cursor.rowcount)


def tick_once(agent) -> TickResult:
    config = load_config(agent.home)
    now = agent.clock.now()
    now_iso = now.isoformat()
    lease_until = (now + timedelta(seconds=config.lease_seconds)).isoformat()
    with agent.db.transaction() as conn:
        work = _claim(conn, owner=agent.worker_owner, now_iso=now_iso, lease_until=lease_until)
        if work is None:
            return TickResult(did_work=False)
        if work["kind"] == "planning" and work["step"] == "start":
            conn.execute(
                "UPDATE tasks SET status='planning', stage='planning', stage_detail=NULL, updated_at=? WHERE task_id=?",
                (now_iso, work["task_id"]),
            )
            conn.execute(
                "UPDATE work_items SET step='finish', lease_owner=NULL, lease_until=NULL WHERE work_id=?",
                (work["work_id"],),
            )
            return TickResult(did_work=True, task_id=work["task_id"], step="start")
        if work["kind"] == "planning" and work["step"] == "finish":
            work_id = work["work_id"]
            task_id = work["task_id"]
            mode = "finish"
        elif work["kind"] == "generation":
            work_id = work["work_id"]
            task_id = work["task_id"]
            mode = "generation"
        else:
            raise VideoAgentError("STATE_CONFLICT", "未知的工作步骤", task_id=work["task_id"])
    if mode == "generation":
        from video_agent.pipeline import advance_generation

        step = advance_generation(agent, work_id)
        return TickResult(did_work=True, task_id=task_id, step=step)
    _finish_planning(agent, work_id)
    return TickResult(did_work=True, task_id=task_id, step="finish")


def _claim(conn, *, owner: str, now_iso: str, lease_until: str):
    row = conn.execute(
        """SELECT * FROM work_items
           WHERE done=0 AND ready_at<=?
             AND (next_attempt_at IS NULL OR next_attempt_at<=?)
             AND (lease_until IS NULL OR lease_until<?)
           ORDER BY ready_at ASC, work_id ASC
           LIMIT 1""",
        (now_iso, now_iso, now_iso),
    ).fetchone()
    if row is None:
        return None
    conn.execute(
        "UPDATE work_items SET lease_owner=?, lease_until=? WHERE work_id=?",
        (owner, lease_until, row["work_id"]),
    )
    return row


def _finish_planning(agent, work_id: str) -> None:
    config = load_config(agent.home)
    with hold_lease(agent.db, work_id, agent.worker_owner, agent.clock, config.lease_seconds):
        with agent.db.transaction() as conn:
            work = conn.execute("SELECT * FROM work_items WHERE work_id=?", (work_id,)).fetchone()
            task = read_task_row(conn, work["task_id"])
            current = None
            if task["pending_instruction"]:
                current = load_plan_document(conn, task["task_id"], int(task["head_plan_version"]))
            request = _plan_request(agent, task, config)
        try:
            if agent.planner is None:
                outcome = VideoAgentError("PROVIDER_REJECTED", "没有注入规划器", retryable=True)
            elif current is not None:
                outcome = agent.planner.revise(current, task["pending_instruction"], request)
            else:
                outcome = agent.planner.plan(request)
        except VideoAgentError as exc:
            outcome = exc
        except Exception as exc:
            outcome = VideoAgentError("PROVIDER_TEMPORARY", str(exc), retryable=True)
    _apply_planning_outcome(agent, work_id, outcome)


def _plan_request(agent, task, config) -> PlanRequest:
    video = VideoOptions.model_validate_json(task["video_options_json"])
    capabilities = agent.video.capabilities() if agent.video is not None else DEFAULT_VIDEO_CAPABILITIES
    return PlanRequest(
        product_info=ProductInfo.model_validate_json(task["product_info_json"]),
        user_prompt=task["user_prompt"],
        images=json.loads(task["image_asset_ids_json"]),
        duration_seconds=int(video.duration_seconds),
        duration_tiers=list(config.duration_tiers),
        aspect_ratio=video.aspect_ratio,
        subtitles=video.subtitles,
        voiceover=video.voiceover,
        music=video.music,
        audio=AudioOptions.model_validate_json(task["audio_options_json"]),
        first_frame_asset_id=task["first_frame_asset_id"],
        video_capabilities=capabilities,
    )


def _apply_planning_outcome(agent, work_id: str, outcome) -> None:
    config = load_config(agent.home)
    now = agent.clock.now().isoformat()
    with agent.db.transaction() as conn:
        work = conn.execute("SELECT * FROM work_items WHERE work_id=?", (work_id,)).fetchone()
        task = read_task_row(conn, work["task_id"])
        if isinstance(outcome, VideoAgentError):
            if outcome.code == "PROVIDER_TEMPORARY" and _schedule_retry(conn, agent, work, outcome):
                return
            _fail_planning(conn, task["task_id"], work_id, outcome, now)
            return
        questions = _nonempty(getattr(outcome, "questions", None))
        if questions is not None:
            conn.execute(
                """UPDATE tasks SET
                    status='awaiting_input', stage=NULL, stage_detail=NULL,
                    failure_scope=NULL, failure_code=NULL, failure_message=NULL,
                    failure_stage=NULL, failure_retryable=NULL, failure_details_json=NULL,
                    questions_json=?, pending_instruction=NULL, completed_shots=NULL,
                    total_shots=NULL, updated_at=?
                   WHERE task_id=?""",
                (dumps(questions), now, task["task_id"]),
            )
            _finish_work(conn, work_id)
            return
        document = getattr(outcome, "document", None)
        patch = getattr(outcome, "patch", None)
        if patch is not None:
            try:
                current = load_plan_document(conn, task["task_id"], int(task["head_plan_version"]))
                audios = {row["asset_id"] for row in conn.execute("SELECT asset_id FROM assets WHERE kind='audio'")}
                native = list(agent.video.capabilities().native_aspects) if agent.video is not None else list(DEFAULT_VIDEO_CAPABILITIES.native_aspects)
                document = apply_patch(
                    current,
                    patch,
                    duration_tiers=list(config.duration_tiers),
                    native_aspects=native,
                    image_asset_ids=json.loads(task["image_asset_ids_json"]),
                    audio_asset_ids=audios,
                )
            except VideoAgentError as exc:
                _fail_planning(conn, task["task_id"], work_id, VideoAgentError("PROVIDER_REJECTED", exc.message, stage="planning", retryable=True), now)
                return
        if isinstance(document, PlanDocument):
            try:
                _validate_planner_document(conn, task, document, require_selected_duration=patch is None)
            except VideoAgentError as exc:
                _fail_planning(conn, task["task_id"], work_id, VideoAgentError("PROVIDER_REJECTED", exc.message, stage="planning", retryable=True), now)
                return
            publish_document(conn, task_id=task["task_id"], document=document, now=now)
            _finish_work(conn, work_id)
            return
        _fail_planning(conn, task["task_id"], work_id, VideoAgentError("PROVIDER_REJECTED", "规划结果无法解释", stage="planning", retryable=True), now)


def _schedule_retry(conn, agent, work, error: VideoAgentError) -> bool:
    copied = json.loads(work["copied_config_json"])
    used = int(copied.get("_auto_attempts_used", 0))
    if used >= MAX_AUTO_RETRIES:
        return False
    copied["_auto_attempts_used"] = used + 1
    delay = 1.0
    if isinstance(error.details, dict) and "retry_after_seconds" in error.details:
        delay = float(error.details["retry_after_seconds"])
    if delay < 0:
        delay = 0.0
    nxt = (agent.clock.now() + timedelta(seconds=delay)).isoformat()
    conn.execute(
        """UPDATE work_items SET
            copied_config_json=?, next_attempt_at=?, ready_at=?,
            lease_owner=NULL, lease_until=NULL, done=0, step='finish'
           WHERE work_id=?""",
        (dumps(copied), nxt, nxt, work["work_id"]),
    )
    return True


def _validate_planner_document(conn, task, document: PlanDocument, *, require_selected_duration: bool = True) -> None:
    video = VideoOptions.model_validate_json(task["video_options_json"])
    if require_selected_duration and document.duration_seconds != video.duration_seconds:
        raise VideoAgentError("PROVIDER_REJECTED", "方案秒数不是已选档位")
    validate_frame_span(document.shots, document.duration_seconds * 30)
    images = json.loads(task["image_asset_ids_json"])
    if document.first_frame_source == "selected" and document.first_frame_asset_id not in images:
        raise VideoAgentError("PROVIDER_REJECTED", "方案引用了不存在的首帧")
    if document.first_frame_source == "generate" and document.first_frame_asset_id is not None:
        raise VideoAgentError("PROVIDER_REJECTED", "未指定首帧时不能引用首帧素材")
    if document.music_asset_id:
        row = conn.execute("SELECT kind FROM assets WHERE asset_id=?", (document.music_asset_id,)).fetchone()
        if row is None or row["kind"] != "audio":
            raise VideoAgentError("PROVIDER_REJECTED", "方案引用了不存在的音频")


def _fail_planning(conn, task_id: str, work_id: str, error: VideoAgentError, now: str) -> None:
    retryable = 1 if error.code in {"PROVIDER_REJECTED", "PROVIDER_TEMPORARY", "PROVIDER_SUBMISSION_UNCERTAIN"} else 0
    conn.execute(
        """UPDATE tasks SET
            status='failed', stage='planning', stage_detail=NULL, failure_scope='planning',
            failure_code=?, failure_message=?, failure_stage='planning', failure_retryable=?,
            failure_details_json=?, questions_json='[]', completed_shots=NULL, total_shots=NULL,
            updated_at=?
           WHERE task_id=?""",
        (error.code, error.message, retryable, None if error.details is None else dumps(error.details), now, task_id),
    )
    _finish_work(conn, work_id)


def _finish_work(conn, work_id: str) -> None:
    conn.execute(
        "UPDATE work_items SET done=1, lease_owner=NULL, lease_until=NULL, next_attempt_at=NULL WHERE work_id=?",
        (work_id,),
    )


def _nonempty(value) -> list[str] | None:
    if not isinstance(value, list) or len(value) == 0:
        return None
    return [str(item) for item in value]
