import json
import os
import uuid
from pathlib import Path
from typing import Literal

from video_agent.clock import SystemClock
from video_agent.config import ASPECT_RATIOS, AppConfig, load_config, snapshot_config
from video_agent.db import Database
from video_agent.errors import VideoAgentError
from video_agent.files import LocalFileStore
from video_agent.idempotency import (
    confirm_payload,
    create_task_payload,
    discard_revision_payload,
    lookup_replay,
    regenerate_payload,
    retry_generation_payload,
    retry_planning_payload,
    revise_plan_payload,
    store_replay,
    submit_inputs_payload,
    validate_idempotency_key,
)
from video_agent.models import (
    AudioOptions,
    CreateTaskRequest,
    InputPatch,
    PlanChange,
    ProductInfo,
    RegenerateRequest,
    TaskView,
    TickResult,
    VideoOptions,
)
from video_agent.state import (
    apply_patch,
    assert_mutation_allowed,
    dumps,
    has_plan,
    insert_work,
    load_plan_document,
    publish_document,
    read_task_row,
    read_task_view,
    start_generation,
)


class VideoAgent:
    def __init__(
        self,
        home: Path | None = None,
        *,
        clock=None,
        planner=None,
        video=None,
        voice=None,
        music=None,
        visual=None,
    ) -> None:
        if home is None:
            env = os.environ.get("VIDEO_AGENT_HOME")
            home = Path(env) if env else Path.cwd() / ".video-agent"
        self.home = Path(home)
        self.home.mkdir(parents=True, exist_ok=True)
        self.config = load_config(self.home)
        self.clock = clock if clock is not None else SystemClock()
        self.db = Database(self.home)
        self.files = LocalFileStore(self.home)
        self.planner = planner
        self.video = video
        self.voice = voice
        self.music = music
        self.visual = visual
        self.worker_owner = str(uuid.uuid4())
        if planner is None and video is None and voice is None and music is None and visual is None:
            self._load_default_adapters()

    def _load_default_adapters(self) -> None:
        try:
            from video_agent.factory import build_adapters
        except ImportError:
            return
        self.planner, self.video, self.voice, self.music, self.visual = build_adapters(self.config)

    def _refresh_config(self) -> AppConfig:
        self.config = load_config(self.home)
        return self.config

    def register_asset(
        self,
        *,
        filename: str,
        kind: Literal["image", "audio"],
        path: Path | None = None,
        data: bytes | None = None,
    ) -> str:
        if (path is None) == (data is None):
            raise VideoAgentError("VALIDATION_ERROR", "path 和 data 必须恰好提供一个", fields=["path", "data"])
        if kind not in ("image", "audio"):
            raise VideoAgentError("VALIDATION_ERROR", "种类只能是 image 或 audio", fields=["kind"])
        blob = data if data is not None else Path(path).read_bytes()
        config = self._refresh_config()
        stored = self.files.register(filename=filename, kind=kind, data=blob, max_bytes=config.max_file_bytes)
        now = self.clock.now().isoformat()
        with self.db.transaction() as conn:
            conn.execute(
                """INSERT INTO assets (
                    asset_id, kind, filename, media_type, byte_size, relative_path, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (stored.asset_id, stored.kind, stored.filename, stored.media_type, stored.byte_size, stored.relative_path, now),
            )
        return stored.asset_id

    def create_task(self, request: CreateTaskRequest, *, idempotency_key: str | None = None) -> TaskView:
        validate_idempotency_key(idempotency_key)
        config = self._refresh_config()
        payload = create_task_payload(request)
        now = self.clock.now().isoformat()
        with self.db.transaction() as conn:
            replay = lookup_replay(conn, "create_task", idempotency_key, payload)
            if replay is not None:
                return replay
            images = list(request.images)
            _require_images(conn, images, config.max_images)
            first = _check_first_frame(_blank_to_none(request.first_frame_asset_id), images)
            _require_audio(conn, request.audio_options.music_asset_id)
            duration = _resolve_duration(request.video_options.duration_seconds, config)
            _require_aspect(request.video_options.aspect_ratio)
            video = request.video_options.model_copy(update={"duration_seconds": duration})
            task_id = str(uuid.uuid4())
            status = "awaiting_input" if len(images) == 0 else "pending"
            questions = ["缺少产品图片"] if status == "awaiting_input" else []
            basis = _basis(request.product_info, request.user_prompt, images, first, video, request.audio_options)
            conn.execute(
                """INSERT INTO tasks (
                    task_id, status, stage, stage_detail, failure_scope, failure_code, failure_message,
                    failure_stage, failure_retryable, failure_details_json, questions_json,
                    external_user_id, external_request_id, head_plan_version, confirmed_plan_version,
                    generation_id, product_info_json, user_prompt, video_options_json, audio_options_json,
                    image_asset_ids_json, first_frame_asset_id, pending_instruction, planning_basis_json,
                    completed_shots, total_shots, created_at, updated_at
                ) VALUES (
                    ?, ?, NULL, NULL, NULL, NULL, NULL,
                    NULL, NULL, NULL, ?,
                    ?, ?, NULL, NULL,
                    NULL, ?, ?, ?, ?,
                    ?, ?, NULL, ?,
                    NULL, NULL, ?, ?
                )""",
                (
                    task_id,
                    status,
                    dumps(questions),
                    request.external_user_id,
                    request.external_request_id,
                    request.product_info.model_dump_json(),
                    request.user_prompt,
                    video.model_dump_json(),
                    request.audio_options.model_dump_json(),
                    dumps(images),
                    first,
                    dumps(basis),
                    now,
                    now,
                ),
            )
            if status == "pending":
                insert_work(
                    conn,
                    task_id=task_id,
                    kind="planning",
                    generation_id=None,
                    ready_at=now,
                    copied_config=snapshot_config(config),
                    step="start",
                )
            view = read_task_view(conn, task_id)
            store_replay(conn, method="create_task", key=idempotency_key, payload=payload, task_id=task_id, view=view, now=now)
            return view

    def get_task(self, task_id: str) -> TaskView:
        with self.db.transaction() as conn:
            return read_task_view(conn, task_id)

    def submit_inputs(self, task_id: str, patch: InputPatch, *, idempotency_key: str | None = None) -> TaskView:
        validate_idempotency_key(idempotency_key)
        config = self._refresh_config()
        payload = submit_inputs_payload(task_id, patch)
        now = self.clock.now().isoformat()
        with self.db.transaction() as conn:
            replay = lookup_replay(conn, "submit_inputs", idempotency_key, payload)
            if replay is not None:
                return replay
            task = read_task_row(conn, task_id)
            assert_mutation_allowed(task, "submit_inputs", has_plan=has_plan(conn, task_id))
            if patch.clear_first_frame and _blank_to_none(patch.first_frame_asset_id) is not None:
                raise VideoAgentError("VALIDATION_ERROR", "不能同时指定和清除首帧", fields=["first_frame_asset_id"])
            product = patch.product_info if patch.product_info is not None else ProductInfo.model_validate_json(task["product_info_json"])
            user_prompt = task["user_prompt"] if patch.user_prompt is None else patch.user_prompt
            images = json.loads(task["image_asset_ids_json"]) if patch.images is None else list(patch.images)
            _require_images(conn, images, config.max_images)
            video = VideoOptions.model_validate_json(task["video_options_json"])
            if patch.video_options is not None:
                duration = _resolve_duration(patch.video_options.duration_seconds, config)
                _require_aspect(patch.video_options.aspect_ratio)
                video = patch.video_options.model_copy(update={"duration_seconds": duration})
            audio = AudioOptions.model_validate_json(task["audio_options_json"])
            if patch.audio_options is not None:
                _require_audio(conn, patch.audio_options.music_asset_id)
                audio = patch.audio_options
            if patch.clear_first_frame:
                first = None
            elif _blank_to_none(patch.first_frame_asset_id) is not None:
                first = _blank_to_none(patch.first_frame_asset_id)
            else:
                first = task["first_frame_asset_id"]
            first = _check_first_frame(first, images)
            status = "pending" if len(images) > 0 else "awaiting_input"
            questions = [] if status == "pending" else ["缺少产品图片"]
            basis = _basis(product, user_prompt, images, first, video, audio)
            conn.execute(
                """UPDATE tasks SET
                    status=?, stage=NULL, stage_detail=NULL,
                    failure_scope=NULL, failure_code=NULL, failure_message=NULL,
                    failure_stage=NULL, failure_retryable=NULL, failure_details_json=NULL,
                    questions_json=?, product_info_json=?, user_prompt=?, video_options_json=?,
                    audio_options_json=?, image_asset_ids_json=?, first_frame_asset_id=?,
                    pending_instruction=NULL, planning_basis_json=?, completed_shots=NULL,
                    total_shots=NULL, updated_at=?
                   WHERE task_id=?""",
                (
                    status,
                    dumps(questions),
                    product.model_dump_json(),
                    user_prompt,
                    video.model_dump_json(),
                    audio.model_dump_json(),
                    dumps(images),
                    first,
                    dumps(basis),
                    now,
                    task_id,
                ),
            )
            if status == "pending":
                insert_work(
                    conn,
                    task_id=task_id,
                    kind="planning",
                    generation_id=None,
                    ready_at=now,
                    copied_config=snapshot_config(config),
                    step="start",
                )
            view = read_task_view(conn, task_id)
            store_replay(conn, method="submit_inputs", key=idempotency_key, payload=payload, task_id=task_id, view=view, now=now)
            return view

    def _native_aspects(self) -> list[str]:
        if self.video is not None:
            return list(self.video.capabilities().native_aspects)
        from video_agent.ports import DEFAULT_VIDEO_CAPABILITIES

        return list(DEFAULT_VIDEO_CAPABILITIES.native_aspects)

    def revise_plan(self, task_id: str, change: PlanChange, *, idempotency_key: str | None = None) -> TaskView:
        validate_idempotency_key(idempotency_key)
        config = self._refresh_config()
        payload = revise_plan_payload(task_id, change)
        now = self.clock.now().isoformat()
        with self.db.transaction() as conn:
            replay = lookup_replay(conn, "revise_plan", idempotency_key, payload)
            if replay is not None:
                return replay
            provided = (change.instruction is not None, change.patch is not None)
            if provided == (False, False) or provided == (True, True):
                raise VideoAgentError("VALIDATION_ERROR", "修改必须恰好提供自然语言或结构化补丁中的一个", fields=["instruction", "patch"])
            task = read_task_row(conn, task_id)
            assert_mutation_allowed(task, "revise_plan", has_plan=has_plan(conn, task_id))
            if change.instruction is not None:
                if change.instruction.strip() == "":
                    raise VideoAgentError("VALIDATION_ERROR", "自然语言修改不能为空", fields=["instruction"])
                conn.execute(
                    """UPDATE tasks SET
                        status='pending', stage=NULL, stage_detail=NULL,
                        failure_scope=NULL, failure_code=NULL, failure_message=NULL,
                        failure_stage=NULL, failure_retryable=NULL, failure_details_json=NULL,
                        questions_json='[]', pending_instruction=?, completed_shots=NULL,
                        total_shots=NULL, updated_at=?
                       WHERE task_id=?""",
                    (change.instruction, now, task_id),
                )
                insert_work(
                    conn,
                    task_id=task_id,
                    kind="planning",
                    generation_id=None,
                    ready_at=now,
                    copied_config=snapshot_config(config),
                    step="start",
                )
            else:
                if task["head_plan_version"] is None:
                    raise VideoAgentError("STATE_CONFLICT", "还没有可修改的方案", task_id=task_id)
                current = load_plan_document(conn, task_id, int(task["head_plan_version"]))
                audios = {row["asset_id"] for row in conn.execute("SELECT asset_id FROM assets WHERE kind='audio'")}
                assert change.patch is not None
                updated = apply_patch(
                    current,
                    change.patch,
                    duration_tiers=list(config.duration_tiers),
                    native_aspects=self._native_aspects(),
                    image_asset_ids=json.loads(task["image_asset_ids_json"]),
                    audio_asset_ids=audios,
                )
                publish_document(conn, task_id=task_id, document=updated, now=now)
            view = read_task_view(conn, task_id)
            store_replay(conn, method="revise_plan", key=idempotency_key, payload=payload, task_id=task_id, view=view, now=now)
            return view

    def discard_revision(self, task_id: str, *, idempotency_key: str | None = None) -> TaskView:
        validate_idempotency_key(idempotency_key)
        payload = discard_revision_payload(task_id)
        now = self.clock.now().isoformat()
        with self.db.transaction() as conn:
            replay = lookup_replay(conn, "discard_revision", idempotency_key, payload)
            if replay is not None:
                return replay
            task = read_task_row(conn, task_id)
            assert_mutation_allowed(task, "discard_revision", has_plan=has_plan(conn, task_id))
            confirmed = int(task["confirmed_plan_version"])
            head = task["head_plan_version"]
            if head is not None and head != confirmed:
                conn.execute(
                    "UPDATE plan_versions SET abandoned=1 WHERE task_id=? AND plan_version=?",
                    (task_id, head),
                )
            generation = conn.execute(
                """SELECT * FROM generations
                   WHERE task_id=? AND plan_version=?
                   ORDER BY created_at DESC, generation_id DESC LIMIT 1""",
                (task_id, confirmed),
            ).fetchone()
            if generation is None:
                status, stage, generation_id = "succeeded", None, None
                failure = (None, None, None, None, None)
            elif generation["status"] == "succeeded":
                status, stage, generation_id = "succeeded", None, generation["generation_id"]
                failure = (None, None, None, None, None)
            elif generation["status"] == "failed":
                status = "failed"
                stage = generation["failure_stage"] or generation["stage"]
                generation_id = generation["generation_id"]
                failure = (
                    "generation",
                    generation["failure_code"],
                    generation["failure_message"],
                    generation["failure_stage"],
                    generation["failure_retryable"],
                )
            else:
                raise VideoAgentError("STATE_CONFLICT", "已确认版本仍有未结束的执行", task_id=task_id)
            conn.execute(
                """UPDATE tasks SET
                    status=?, stage=?, stage_detail=NULL, failure_scope=?, failure_code=?,
                    failure_message=?, failure_stage=?, failure_retryable=?, failure_details_json=NULL,
                    questions_json='[]', head_plan_version=?, pending_instruction=NULL,
                    generation_id=?, completed_shots=NULL, total_shots=NULL, updated_at=?
                   WHERE task_id=?""",
                (status, stage, failure[0], failure[1], failure[2], failure[3], failure[4], confirmed, generation_id, now, task_id),
            )
            view = read_task_view(conn, task_id)
            store_replay(conn, method="discard_revision", key=idempotency_key, payload=payload, task_id=task_id, view=view, now=now)
            return view

    def retry_planning(self, task_id: str, *, idempotency_key: str | None = None) -> TaskView:
        validate_idempotency_key(idempotency_key)
        config = self._refresh_config()
        payload = retry_planning_payload(task_id)
        now = self.clock.now().isoformat()
        with self.db.transaction() as conn:
            replay = lookup_replay(conn, "retry_planning", idempotency_key, payload)
            if replay is not None:
                return replay
            task = read_task_row(conn, task_id)
            assert_mutation_allowed(task, "retry_planning", has_plan=has_plan(conn, task_id))
            conn.execute(
                """UPDATE tasks SET
                    status='pending', stage=NULL, stage_detail=NULL,
                    failure_scope=NULL, failure_code=NULL, failure_message=NULL,
                    failure_stage=NULL, failure_retryable=NULL, failure_details_json=NULL,
                    questions_json='[]', completed_shots=NULL, total_shots=NULL, updated_at=?
                   WHERE task_id=?""",
                (now, task_id),
            )
            insert_work(
                conn,
                task_id=task_id,
                kind="planning",
                generation_id=None,
                ready_at=now,
                copied_config=snapshot_config(config),
                step="start",
            )
            view = read_task_view(conn, task_id)
            store_replay(conn, method="retry_planning", key=idempotency_key, payload=payload, task_id=task_id, view=view, now=now)
            return view

    def retry_generation(self, generation_id: str, *, idempotency_key: str | None = None) -> TaskView:
        validate_idempotency_key(idempotency_key)
        config = self._refresh_config()
        payload = retry_generation_payload(generation_id)
        now = self.clock.now().isoformat()
        with self.db.transaction() as conn:
            replay = lookup_replay(conn, "retry_generation", idempotency_key, payload)
            if replay is not None:
                return replay
            generation = conn.execute("SELECT * FROM generations WHERE generation_id=?", (generation_id,)).fetchone()
            if generation is None:
                raise VideoAgentError("NOT_FOUND", "执行不存在", generation_id=generation_id)
            task = read_task_row(conn, generation["task_id"])
            assert_mutation_allowed(task, "retry_generation", has_plan=has_plan(conn, generation["task_id"]))
            if generation["status"] != "failed" or not generation["failure_retryable"]:
                raise VideoAgentError(
                    "STATE_CONFLICT",
                    "该失败不能重试",
                    task_id=generation["task_id"],
                    generation_id=generation_id,
                )
            step = {
                "queued": "first_frame",
                "preparing_first_frame": "first_frame",
                "generating_shots": "submit_shot",
                "preparing_audio": "audio",
                "composing": "compose",
                "checking_visuals": "visual",
                "exporting": "export",
            }.get(generation["stage"])
            if step is None:
                raise VideoAgentError("STATE_CONFLICT", "该失败不能重试", task_id=generation["task_id"], generation_id=generation_id)
            new_status = "queued" if generation["stage"] == "queued" else "running"
            conn.execute(
                """UPDATE generations SET
                    status=?, failure_code=NULL, failure_message=NULL, failure_stage=NULL,
                    failure_retryable=NULL, failure_details_json=NULL, auto_attempts_used=0,
                    next_attempt_at=NULL, updated_at=?
                   WHERE generation_id=?""",
                (new_status, now, generation_id),
            )
            conn.execute(
                """UPDATE tasks SET
                    status='generating', stage=?, stage_detail=NULL,
                    failure_scope=NULL, failure_code=NULL, failure_message=NULL,
                    failure_stage=NULL, failure_retryable=NULL, failure_details_json=NULL,
                    completed_shots=?, total_shots=?, updated_at=?
                   WHERE task_id=?""",
                (generation["stage"], generation["completed_shots"], generation["total_shots"], now, generation["task_id"]),
            )
            external = conn.execute(
                "SELECT 1 AS ok FROM generation_shots WHERE generation_id=? AND external_task_id IS NOT NULL LIMIT 1",
                (generation_id,),
            ).fetchone()
            copied = None if external is not None else snapshot_config(config)
            work = conn.execute(
                "SELECT work_id FROM work_items WHERE generation_id=? ORDER BY ready_at DESC LIMIT 1",
                (generation_id,),
            ).fetchone()
            if work is None:
                raw = copied if copied is not None else json.loads(generation["copied_config_json"])
                insert_work(
                    conn,
                    task_id=generation["task_id"],
                    kind="generation",
                    generation_id=generation_id,
                    ready_at=now,
                    copied_config=raw,
                    step=step,
                )
            elif copied is None:
                conn.execute(
                    """UPDATE work_items SET done=0, lease_owner=NULL, lease_until=NULL,
                       step=?, ready_at=?, next_attempt_at=NULL WHERE work_id=?""",
                    (step, now, work["work_id"]),
                )
            else:
                conn.execute(
                    """UPDATE work_items SET done=0, lease_owner=NULL, lease_until=NULL,
                       step=?, ready_at=?, next_attempt_at=NULL, copied_config_json=? WHERE work_id=?""",
                    (step, now, dumps(copied), work["work_id"]),
                )
            view = read_task_view(conn, generation["task_id"])
            store_replay(conn, method="retry_generation", key=idempotency_key, payload=payload, task_id=generation["task_id"], view=view, now=now)
            return view

    def confirm(self, task_id: str, plan_version: int, *, idempotency_key: str | None = None) -> TaskView:
        validate_idempotency_key(idempotency_key)
        config = self._refresh_config()
        payload = confirm_payload(task_id, plan_version)
        now = self.clock.now().isoformat()
        with self.db.transaction() as conn:
            replay = lookup_replay(conn, "confirm", idempotency_key, payload)
            if replay is not None:
                return replay
            task = read_task_row(conn, task_id)
            if task["confirmed_plan_version"] == plan_version and task["head_plan_version"] == plan_version:
                existing = conn.execute(
                    """SELECT generation_id FROM generations
                       WHERE task_id=? AND plan_version=?
                       ORDER BY created_at ASC, generation_id ASC LIMIT 1""",
                    (task_id, plan_version),
                ).fetchone()
                raise VideoAgentError(
                    "STATE_CONFLICT",
                    "该方案版本已经确认",
                    task_id=task_id,
                    generation_id=None if existing is None else existing["generation_id"],
                    plan_version=plan_version,
                )
            plan_exists = has_plan(conn, task_id)
            if task["status"] in {"awaiting_input", "pending", "planning", "generating", "failed"}:
                assert_mutation_allowed(task, "confirm", has_plan=plan_exists)
            version_row = conn.execute(
                "SELECT abandoned FROM plan_versions WHERE task_id=? AND plan_version=?",
                (task_id, plan_version),
            ).fetchone()
            head = task["head_plan_version"]
            if version_row is None or version_row["abandoned"] == 1 or plan_version != head:
                raise VideoAgentError(
                    "PLAN_VERSION_CONFLICT",
                    "只能确认当前尚未确认的 head",
                    task_id=task_id,
                    plan_version=plan_version,
                    details={"head_plan_version": head},
                )
            assert_mutation_allowed(task, "confirm", has_plan=plan_exists)
            document = load_plan_document(conn, task_id, plan_version)
            start_generation(
                conn,
                task_id=task_id,
                plan_version=plan_version,
                kind="full",
                document=document,
                selected_shot_ids=[shot.shot_id for shot in document.shots],
                base_artifact_id=None,
                copied_config=snapshot_config(config),
                now=now,
                confirm_version=plan_version,
            )
            view = read_task_view(conn, task_id)
            store_replay(conn, method="confirm", key=idempotency_key, payload=payload, task_id=task_id, view=view, now=now)
            return view

    def regenerate(self, task_id: str, request: RegenerateRequest, *, idempotency_key: str | None = None) -> TaskView:
        validate_idempotency_key(idempotency_key)
        config = self._refresh_config()
        payload = regenerate_payload(task_id, request)
        now = self.clock.now().isoformat()
        with self.db.transaction() as conn:
            replay = lookup_replay(conn, "regenerate", idempotency_key, payload)
            if replay is not None:
                return replay
            task = read_task_row(conn, task_id)
            assert_mutation_allowed(task, "regenerate", has_plan=has_plan(conn, task_id))
            confirmed_version = int(task["confirmed_plan_version"])
            if request.kind == "full":
                document = load_plan_document(conn, task_id, confirmed_version)
                start_generation(
                    conn,
                    task_id=task_id,
                    plan_version=confirmed_version,
                    kind="full",
                    document=document,
                    selected_shot_ids=[shot.shot_id for shot in document.shots],
                    base_artifact_id=None,
                    copied_config=snapshot_config(config),
                    now=now,
                    confirm_version=None,
                )
            else:
                if request.artifact_id is None or len(request.shot_ids) == 0:
                    raise VideoAgentError("VALIDATION_ERROR", "局部重生成需要基础成品和至少一个镜头", fields=["artifact_id", "shot_ids"])
                artifact = conn.execute("SELECT * FROM artifacts WHERE artifact_id=?", (request.artifact_id,)).fetchone()
                if artifact is None or artifact["task_id"] != task_id:
                    raise VideoAgentError("VALIDATION_ERROR", "找不到基础成品", fields=["artifact_id"])
                document = load_plan_document(conn, task_id, int(artifact["plan_version"]))
                known = {shot.shot_id for shot in document.shots}
                if len(set(request.shot_ids)) != len(request.shot_ids) or any(shot_id not in known for shot_id in request.shot_ids):
                    raise VideoAgentError("VALIDATION_ERROR", "镜头标识不属于该成品", fields=["shot_ids"], shot_ids=list(request.shot_ids))
                start_generation(
                    conn,
                    task_id=task_id,
                    plan_version=int(artifact["plan_version"]),
                    kind="shots",
                    document=document,
                    selected_shot_ids=list(request.shot_ids),
                    base_artifact_id=request.artifact_id,
                    copied_config=snapshot_config(config),
                    now=now,
                    confirm_version=None,
                )
            view = read_task_view(conn, task_id)
            store_replay(conn, method="regenerate", key=idempotency_key, payload=payload, task_id=task_id, view=view, now=now)
            return view

    def tick(self) -> TickResult:
        from video_agent.worker import tick_once

        return tick_once(self)

    def recover(self) -> int:
        from video_agent.worker import recover_expired

        return recover_expired(self)


def _blank_to_none(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    return value


def _basis(product, user_prompt, images, first, video, audio) -> dict:
    return {
        "product_info": product.model_dump(mode="json"),
        "user_prompt": user_prompt,
        "images": images,
        "first_frame_asset_id": first,
        "video_options": video.model_dump(mode="json"),
        "audio_options": audio.model_dump(mode="json"),
    }


def _require_images(conn, images: list[str], max_images: int) -> None:
    if len(images) > max_images:
        raise VideoAgentError("VALIDATION_ERROR", "图片数量超过上限", fields=["images"])
    if len(set(images)) != len(images):
        raise VideoAgentError("VALIDATION_ERROR", "图片标识重复", fields=["images"])
    for asset_id in images:
        row = conn.execute("SELECT kind FROM assets WHERE asset_id=?", (asset_id,)).fetchone()
        if row is None or row["kind"] != "image":
            raise VideoAgentError("VALIDATION_ERROR", "图片素材不存在或不是图片", fields=["images"])


def _require_audio(conn, asset_id: str | None) -> None:
    asset_id = _blank_to_none(asset_id)
    if asset_id is None:
        return
    row = conn.execute("SELECT kind FROM assets WHERE asset_id=?", (asset_id,)).fetchone()
    if row is None or row["kind"] != "audio":
        raise VideoAgentError("VALIDATION_ERROR", "音乐素材不存在或不是音频", fields=["music_asset_id"])


def _require_aspect(ratio: str) -> None:
    if ratio not in ASPECT_RATIOS:
        raise VideoAgentError(
            "VALIDATION_ERROR",
            "画面比例不在预设中",
            fields=["aspect_ratio"],
            details={"aspect_ratios": list(ASPECT_RATIOS)},
        )


def _resolve_duration(value: int | None, config: AppConfig) -> int:
    if value is None:
        return config.default_duration
    if value not in config.duration_tiers:
        raise VideoAgentError(
            "VALIDATION_ERROR",
            "时长不在当前档位",
            fields=["duration_seconds"],
            details={"duration_tiers": list(config.duration_tiers)},
        )
    return value


def _check_first_frame(first: str | None, images: list[str]) -> str | None:
    if first is None:
        return None
    if first not in images:
        raise VideoAgentError("VALIDATION_ERROR", "首帧必须是本次图片中的一张", fields=["first_frame_asset_id"])
    return first
