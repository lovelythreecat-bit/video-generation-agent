import json
import os
import uuid
import copy
from pathlib import Path
from typing import Literal

from video_agent.clock import SystemClock
from video_agent.config import ASPECT_RATIOS, AppConfig, load_config, snapshot_config
from video_agent.db import Database, retry_database_mutation
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
    ArtifactView,
    AssetView,
    AudioOptions,
    CreateTaskRequest,
    FailureView,
    GenerationView,
    InputPatch,
    PlanChange,
    PlanDocument,
    PlanView,
    ProductInfo,
    RegenerateRequest,
    TaskSummary,
    TaskPage,
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
        database: Database | None = None,
        settings_service=None,
    ) -> None:
        if home is None:
            env = os.environ.get("VIDEO_AGENT_HOME")
            home = Path(env) if env else Path.cwd() / ".video-agent"
        self.home = Path(home)
        self.home.mkdir(parents=True, exist_ok=True)
        self.settings_service = settings_service
        if database is not None and database.dialect_name == "mysql" and settings_service is None:
            raise ValueError("MySQL VideoAgent requires a database settings service")
        self.config = settings_service.read_effective_config() if settings_service is not None else load_config(self.home)
        self.clock = clock if clock is not None else SystemClock()
        self.db = database if database is not None else Database(self.home)
        self.job_store = None
        if settings_service is not None:
            from video_agent.provider_jobs import ProviderJobStore, ensure_worker_schema
            if self.db.dialect_name == 'sqlite':
                ensure_worker_schema(self.db)
            self.job_store = ProviderJobStore(self.db)
        self.files = LocalFileStore(self.home)
        self.planner = planner
        self.video = video
        self.voice = voice
        self.music = music
        self.visual = visual
        self.worker_owner = str(uuid.uuid4())
        self.provider_config_calls = []
        if planner is None and video is None and voice is None and music is None and visual is None:
            self._load_default_adapters()

    def _load_default_adapters(self) -> None:
        try:
            from video_agent.factory import build_adapters
        except ImportError:
            return
        self.planner, self.video, self.voice, self.music, self.visual = build_adapters(self.config, self.home, job_store=self.job_store)

    def reload_adapters(self) -> None:
        from video_agent.factory import build_adapters

        self.config = self._refresh_config()
        self.planner, self.video, self.voice, self.music, self.visual = build_adapters(self.config, self.home, job_store=self.job_store)

    def _for_work(self, work):
        """A private execution view keeps concurrent API settings off in-flight work."""
        view = copy.copy(self)
        view._active_work_id = work['work_id']
        if self.settings_service is not None:
            from video_agent.factory import AdapterResolver
            snapshot = json.loads(work['copied_config_json'])
            if work['kind'] == 'generation':
                with self.db.transaction(write=False) as conn:
                    external = conn.execute("SELECT 1 AS ok FROM generation_shots WHERE generation_id=:id AND (external_task_id IS NOT NULL OR submission_state IN ('intended', 'uncertain', 'submitting')) LIMIT 1", {'id': work['generation_id']}).fetchone()
                    if external is not None:
                        generation = conn.execute('SELECT copied_config_json FROM generations WHERE generation_id=:id', {'id': work['generation_id']}).fetchone()
                        snapshot = json.loads(generation['copied_config_json'])
            view.config = self.settings_service.codec.decode(snapshot)
            bundle = AdapterResolver(self.settings_service.codec, job_store=self.job_store).for_snapshot(snapshot, self.home)
            view.planner, view.video, view.voice, view.music, view.visual = bundle
        else:
            # Explicit SQLite fixtures can inject scripted adapters. Preserve them.
            view.config = self.config.model_copy(deep=True)
        return view

    def _refresh_config(self, conn=None) -> AppConfig:
        self.config = self.settings_service.read_effective_config(conn=conn) if self.settings_service is not None else load_config(self.home)
        return self.config

    def _snapshot_config(self, conn, config: AppConfig) -> dict:
        if self.settings_service is not None:
            return self.settings_service.snapshot(conn=conn)
        return snapshot_config(config)

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
        try:
            with self.db.transaction() as conn:
                conn.execute(
                    """INSERT INTO assets (
                        asset_id, kind, filename, media_type, byte_size, relative_path, created_at
                    ) VALUES (:stored_asset_id, :stored_kind, :stored_filename, :stored_media_type, :stored_byte_size, :stored_relative_path, :now)""",
                    {'stored_asset_id': stored.asset_id, 'stored_kind': stored.kind, 'stored_filename': stored.filename, 'stored_media_type': stored.media_type, 'stored_byte_size': stored.byte_size, 'stored_relative_path': stored.relative_path, 'now': now},
                )
        except BaseException:
            target = self.files.path_for(stored.relative_path)
            try:
                target.unlink(missing_ok=True)
                target.parent.rmdir()
            except OSError:
                pass
            raise
        return stored.asset_id

    @retry_database_mutation
    def create_task(self, request: CreateTaskRequest, *, idempotency_key: str | None = None) -> TaskView:
        validate_idempotency_key(idempotency_key)
        payload = create_task_payload(request)
        now = self.clock.now().isoformat()
        with self.db.transaction() as conn:
            replay = lookup_replay(conn, "create_task", idempotency_key, payload)
            if replay is not None:
                return replay
            config = self._refresh_config(conn)
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
                    :task_id, :status, NULL, NULL, NULL, NULL, NULL,
                    NULL, NULL, NULL, :dumps_questions,
                    :request_external_user_id, :request_external_request_id, NULL, NULL,
                    NULL, :request_product_info_json, :request_user_prompt, :video_json, :request_audio_options_json,
                    :dumps_images, :first, NULL, :dumps_basis,
                    NULL, NULL, :now, :now_2
                )""",
                {'task_id': task_id, 'status': status, 'dumps_questions': dumps(questions), 'request_external_user_id': request.external_user_id, 'request_external_request_id': request.external_request_id, 'request_product_info_json': request.product_info.model_dump_json(), 'request_user_prompt': request.user_prompt, 'video_json': video.model_dump_json(), 'request_audio_options_json': request.audio_options.model_dump_json(), 'dumps_images': dumps(images), 'first': first, 'dumps_basis': dumps(basis), 'now': now, 'now_2': now},
            )
            if status == "pending":
                insert_work(
                    conn,
                    task_id=task_id,
                    kind="planning",
                    generation_id=None,
                    ready_at=now,
                    copied_config=self._snapshot_config(conn, config),
                    step="start",
                )
            view = read_task_view(conn, task_id)
            store_replay(conn, method="create_task", key=idempotency_key, payload=payload, task_id=task_id, view=view, now=now)
            return view

    def get_task(self, task_id: str) -> TaskView:
        with self.db.transaction(write=False) as conn:
            return read_task_view(conn, task_id)

    def list_tasks(self) -> list[TaskSummary]:
        with self.db.transaction(write=False) as conn:
            rows = conn.execute("SELECT * FROM tasks ORDER BY updated_at DESC, task_id DESC").fetchall()
        summaries = []
        for row in rows:
            product = json.loads(row["product_info_json"])
            summaries.append(
                TaskSummary(
                    task_id=row["task_id"],
                    status=row["status"],
                    product_name=product.get("name"),
                    updated_at=row["updated_at"],
                )
            )
        return summaries

    def list_tasks_page(self, *, limit: int = 20, offset: int = 0, status: str | None = None,
                        external_user_id: str | None = None, external_request_id: str | None = None) -> TaskPage:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise VideoAgentError("VALIDATION_ERROR", "limit 必须在 1 到 100 之间", fields=["limit"])
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise VideoAgentError("VALIDATION_ERROR", "offset 不能为负数", fields=["offset"])
        filters, parameters = [], {"limit": limit, "offset": offset}
        for name, value in (("status", status), ("external_user_id", external_user_id), ("external_request_id", external_request_id)):
            if value is not None:
                filters.append(f"{name}=:{name}")
                parameters[name] = value
        where = " WHERE " + " AND ".join(filters) if filters else ""
        with self.db.transaction(write=False) as conn:
            total = conn.execute("SELECT COUNT(*) AS total FROM tasks" + where, parameters).fetchone()["total"]
            rows = conn.execute("SELECT * FROM tasks" + where + " ORDER BY updated_at DESC, task_id DESC LIMIT :limit OFFSET :offset", parameters).fetchall()
        items = [TaskSummary(task_id=row["task_id"], status=row["status"], product_name=json.loads(row["product_info_json"]).get("name"), updated_at=row["updated_at"]) for row in rows]
        return TaskPage(items=items, limit=limit, offset=offset, total=total)

    def get_asset(self, asset_id: str) -> AssetView:
        with self.db.transaction(write=False) as conn:
            row = conn.execute("SELECT asset_id, kind, filename, media_type, byte_size FROM assets WHERE asset_id=:asset_id", {"asset_id": asset_id}).fetchone()
        if row is None:
            raise VideoAgentError("NOT_FOUND", "素材不存在")
        return AssetView.model_validate(dict(row))

    def asset_path(self, asset_id: str) -> Path:
        with self.db.transaction(write=False) as conn:
            row = conn.execute("SELECT relative_path FROM assets WHERE asset_id=:asset_id", {"asset_id": asset_id}).fetchone()
        if row is None:
            raise VideoAgentError("NOT_FOUND", "素材不存在")
        return self.files.path_for(row["relative_path"])

    def get_plan(self, task_id: str, plan_version: int) -> PlanView:
        with self.db.transaction(write=False) as conn:
            task = read_task_row(conn, task_id)
            row = conn.execute(
                'SELECT * FROM plan_versions WHERE task_id=:task_id AND plan_version=:plan_version',
                {'task_id': task_id, 'plan_version': plan_version},
            ).fetchone()
            if row is None:
                raise VideoAgentError("NOT_FOUND", "方案不存在", task_id=task_id, plan_version=plan_version)
            return PlanView(
                task_id=task_id,
                plan_version=plan_version,
                is_head=task["head_plan_version"] == plan_version,
                confirmed=task["confirmed_plan_version"] == plan_version,
                abandoned=bool(row["abandoned"]),
                document=PlanDocument.model_validate_json(row["document_json"]),
            )

    def get_generation(self, generation_id: str) -> GenerationView:
        with self.db.transaction(write=False) as conn:
            row = conn.execute('SELECT * FROM generations WHERE generation_id=:generation_id', {'generation_id': generation_id}).fetchone()
            if row is None:
                raise VideoAgentError("NOT_FOUND", "执行不存在", generation_id=generation_id)
            failure = None
            if row["failure_code"]:
                failure = FailureView(
                    code=row["failure_code"],
                    message=row["failure_message"] or "",
                    stage=row["failure_stage"],
                    retryable=bool(row["failure_retryable"]),
                )
            return GenerationView(
                generation_id=row["generation_id"],
                task_id=row["task_id"],
                plan_version=row["plan_version"],
                kind=row["kind"],
                shot_ids=json.loads(row["shot_ids_json"]),
                base_artifact_id=row["base_artifact_id"],
                status=row["status"],
                stage=row["stage"],
                stage_detail=row["stage_detail"],
                completed_shots=row["completed_shots"],
                total_shots=row["total_shots"],
                failure=failure,
            )

    def list_artifacts(self, task_id: str) -> list[ArtifactView]:
        with self.db.transaction(write=False) as conn:
            read_task_row(conn, task_id)
            rows = conn.execute(
                'SELECT a.*, g.copied_config_json AS generation_config FROM artifacts a '
                'LEFT JOIN generations g ON g.generation_id=a.generation_id AND g.task_id=a.task_id '
                'AND g.plan_version=a.plan_version WHERE a.task_id=:task_id ORDER BY a.version DESC',
                {'task_id': task_id},
            ).fetchall()
        return [
            ArtifactView(
                artifact_id=row["artifact_id"],
                task_id=row["task_id"],
                generation_id=row["generation_id"],
                plan_version=row["plan_version"],
                version=row["version"],
                duration_seconds=row["duration_seconds"],
                aspect_ratio=row["aspect_ratio"],
                width=row["width"],
                height=row["height"],
                quality_status=row["quality_status"],
                visual_check_enabled=_artifact_visual_check_enabled(row["generation_config"], row["quality_json"]),
                quality=json.loads(row["quality_json"]),
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def artifact_path(self, artifact_id: str) -> Path:
        with self.db.transaction(write=False) as conn:
            row = conn.execute('SELECT relative_path FROM artifacts WHERE artifact_id=:artifact_id', {'artifact_id': artifact_id}).fetchone()
            if row is None:
                raise VideoAgentError("NOT_FOUND", "成品不存在")
            return self.files.path_for(row["relative_path"])

    @retry_database_mutation
    def submit_inputs(self, task_id: str, patch: InputPatch, *, idempotency_key: str | None = None) -> TaskView:
        validate_idempotency_key(idempotency_key)
        payload = submit_inputs_payload(task_id, patch)
        now = self.clock.now().isoformat()
        with self.db.transaction() as conn:
            replay = lookup_replay(conn, "submit_inputs", idempotency_key, payload)
            if replay is not None:
                return replay
            config = self._refresh_config(conn)
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
                    status=:status, stage=NULL, stage_detail=NULL,
                    failure_scope=NULL, failure_code=NULL, failure_message=NULL,
                    failure_stage=NULL, failure_retryable=NULL, failure_details_json=NULL,
                    questions_json=:dumps_questions, product_info_json=:product_json, user_prompt=:user_prompt, video_options_json=:video_json,
                    audio_options_json=:audio_json, image_asset_ids_json=:dumps_images, first_frame_asset_id=:first,
                    pending_instruction=NULL, planning_basis_json=:dumps_basis, completed_shots=NULL,
                    total_shots=NULL, updated_at=:now
                   WHERE task_id=:task_id""",
                {'status': status, 'dumps_questions': dumps(questions), 'product_json': product.model_dump_json(), 'user_prompt': user_prompt, 'video_json': video.model_dump_json(), 'audio_json': audio.model_dump_json(), 'dumps_images': dumps(images), 'first': first, 'dumps_basis': dumps(basis), 'now': now, 'task_id': task_id},
            )
            if status == "pending":
                insert_work(
                    conn,
                    task_id=task_id,
                    kind="planning",
                    generation_id=None,
                    ready_at=now,
                    copied_config=self._snapshot_config(conn, config),
                    step="start",
                )
            view = read_task_view(conn, task_id)
            store_replay(conn, method="submit_inputs", key=idempotency_key, payload=payload, task_id=task_id, view=view, now=now)
            return view

    def _native_aspects(self, config: AppConfig) -> list[str]:
        if self.settings_service is not None:
            from video_agent.factory import build_adapters

            # The API's startup adapters may predate a management update. Build
            # a private bundle from this transaction's effective configuration.
            video = build_adapters(config, self.home, job_store=self.job_store)[1]
            return list(video.capabilities().native_aspects)
        if self.video is not None:
            return list(self.video.capabilities().native_aspects)
        from video_agent.ports import DEFAULT_VIDEO_CAPABILITIES

        return list(DEFAULT_VIDEO_CAPABILITIES.native_aspects)

    @retry_database_mutation
    def revise_plan(self, task_id: str, change: PlanChange, *, idempotency_key: str | None = None) -> TaskView:
        validate_idempotency_key(idempotency_key)
        payload = revise_plan_payload(task_id, change)
        now = self.clock.now().isoformat()
        with self.db.transaction() as conn:
            replay = lookup_replay(conn, "revise_plan", idempotency_key, payload)
            if replay is not None:
                return replay
            config = self._refresh_config(conn)
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
                        questions_json='[]', pending_instruction=:change_instruction, completed_shots=NULL,
                        total_shots=NULL, updated_at=:now
                       WHERE task_id=:task_id""",
                    {'change_instruction': change.instruction, 'now': now, 'task_id': task_id},
                )
                insert_work(
                    conn,
                    task_id=task_id,
                    kind="planning",
                    generation_id=None,
                    ready_at=now,
                    copied_config=self._snapshot_config(conn, config),
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
                    native_aspects=self._native_aspects(config),
                    image_asset_ids=json.loads(task["image_asset_ids_json"]),
                    audio_asset_ids=audios,
                )
                publish_document(conn, task_id=task_id, document=updated, now=now)
            view = read_task_view(conn, task_id)
            store_replay(conn, method="revise_plan", key=idempotency_key, payload=payload, task_id=task_id, view=view, now=now)
            return view

    @retry_database_mutation
    def discard_revision(self, task_id: str, *, idempotency_key: str | None = None) -> TaskView:
        validate_idempotency_key(idempotency_key)
        payload = discard_revision_payload(task_id)
        now = self.clock.now().isoformat()
        with self.db.transaction() as conn:
            replay = lookup_replay(conn, "discard_revision", idempotency_key, payload)
            if replay is not None:
                return replay
            config = self._refresh_config(conn)
            task = read_task_row(conn, task_id)
            assert_mutation_allowed(task, "discard_revision", has_plan=has_plan(conn, task_id))
            confirmed = int(task["confirmed_plan_version"])
            head = task["head_plan_version"]
            if head is not None and head != confirmed:
                conn.execute(
                    'UPDATE plan_versions SET abandoned=1 WHERE task_id=:task_id AND plan_version=:head',
                    {'task_id': task_id, 'head': head},
                )
            generation = conn.execute(
                """SELECT * FROM generations
                   WHERE task_id=:task_id AND plan_version=:confirmed
                   ORDER BY created_at DESC, generation_id DESC LIMIT 1""",
                {'task_id': task_id, 'confirmed': confirmed},
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
                    status=:status, stage=:stage, stage_detail=NULL, failure_scope=:failure_0, failure_code=:failure_1,
                    failure_message=:failure_2, failure_stage=:failure_3, failure_retryable=:failure_4, failure_details_json=NULL,
                    questions_json='[]', head_plan_version=:confirmed, pending_instruction=NULL,
                    generation_id=:generation_id, completed_shots=NULL, total_shots=NULL, updated_at=:now
                   WHERE task_id=:task_id""",
                {'status': status, 'stage': stage, 'failure_0': failure[0], 'failure_1': failure[1], 'failure_2': failure[2], 'failure_3': failure[3], 'failure_4': failure[4], 'confirmed': confirmed, 'generation_id': generation_id, 'now': now, 'task_id': task_id},
            )
            view = read_task_view(conn, task_id)
            store_replay(conn, method="discard_revision", key=idempotency_key, payload=payload, task_id=task_id, view=view, now=now)
            return view

    @retry_database_mutation
    def retry_planning(self, task_id: str, *, idempotency_key: str | None = None) -> TaskView:
        validate_idempotency_key(idempotency_key)
        payload = retry_planning_payload(task_id)
        now = self.clock.now().isoformat()
        with self.db.transaction() as conn:
            replay = lookup_replay(conn, "retry_planning", idempotency_key, payload)
            if replay is not None:
                return replay
            config = self._refresh_config(conn)
            task = read_task_row(conn, task_id)
            assert_mutation_allowed(task, "retry_planning", has_plan=has_plan(conn, task_id))
            conn.execute(
                """UPDATE tasks SET
                    status='pending', stage=NULL, stage_detail=NULL,
                    failure_scope=NULL, failure_code=NULL, failure_message=NULL,
                    failure_stage=NULL, failure_retryable=NULL, failure_details_json=NULL,
                    questions_json='[]', completed_shots=NULL, total_shots=NULL, updated_at=:now
                   WHERE task_id=:task_id""",
                {'now': now, 'task_id': task_id},
            )
            insert_work(
                conn,
                task_id=task_id,
                kind="planning",
                generation_id=None,
                ready_at=now,
                copied_config=self._snapshot_config(conn, config),
                step="start",
            )
            view = read_task_view(conn, task_id)
            store_replay(conn, method="retry_planning", key=idempotency_key, payload=payload, task_id=task_id, view=view, now=now)
            return view

    @retry_database_mutation
    def retry_generation(self, generation_id: str, *, idempotency_key: str | None = None) -> TaskView:
        validate_idempotency_key(idempotency_key)
        payload = retry_generation_payload(generation_id)
        now = self.clock.now().isoformat()
        with self.db.transaction() as conn:
            replay = lookup_replay(conn, "retry_generation", idempotency_key, payload)
            if replay is not None:
                return replay
            config = self._refresh_config(conn)
            generation = conn.execute('SELECT * FROM generations WHERE generation_id=:generation_id', {'generation_id': generation_id}).fetchone()
            if generation is None:
                raise VideoAgentError("NOT_FOUND", "执行不存在", generation_id=generation_id)
            task = read_task_row(conn, generation["task_id"])
            generation = conn.execute("SELECT * FROM generations WHERE generation_id=:generation_id", {"generation_id": generation_id}).fetchone()
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
                    status=:new_status, failure_code=NULL, failure_message=NULL, failure_stage=NULL,
                    failure_retryable=NULL, failure_details_json=NULL, auto_attempts_used=0,
                    next_attempt_at=NULL, updated_at=:now
                   WHERE generation_id=:generation_id""",
                {'new_status': new_status, 'now': now, 'generation_id': generation_id},
            )
            conn.execute(
                """UPDATE tasks SET
                    status='generating', stage=:generation_stage, stage_detail=NULL,
                    failure_scope=NULL, failure_code=NULL, failure_message=NULL,
                    failure_stage=NULL, failure_retryable=NULL, failure_details_json=NULL,
                    completed_shots=:generation_completed_shots, total_shots=:generation_total_shots, updated_at=:now
                   WHERE task_id=:generation_task_id""",
                {'generation_stage': generation["stage"], 'generation_completed_shots': generation["completed_shots"], 'generation_total_shots': generation["total_shots"], 'now': now, 'generation_task_id': generation["task_id"]},
            )
            external = conn.execute(
                "SELECT 1 AS ok FROM generation_shots WHERE generation_id=:generation_id AND (external_task_id IS NOT NULL OR submission_state IN ('intended', 'submitting', 'uncertain')) LIMIT 1",
                {'generation_id': generation_id},
            ).fetchone()
            copied = None if external is not None else self._snapshot_config(conn, config)
            work = conn.execute(
                'SELECT work_id FROM work_items WHERE generation_id=:generation_id ORDER BY ready_at DESC LIMIT 1',
                {'generation_id': generation_id},
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
                       step=:step, ready_at=:now, next_attempt_at=NULL WHERE work_id=:work_work_id""",
                    {'step': step, 'now': now, 'work_work_id': work["work_id"]},
                )
            else:
                conn.execute(
                    """UPDATE work_items SET done=0, lease_owner=NULL, lease_until=NULL,
                       step=:step, ready_at=:now, next_attempt_at=NULL, copied_config_json=:dumps_copied WHERE work_id=:work_work_id""",
                    {'step': step, 'now': now, 'dumps_copied': dumps(copied), 'work_work_id': work["work_id"]},
                )
            view = read_task_view(conn, generation["task_id"])
            store_replay(conn, method="retry_generation", key=idempotency_key, payload=payload, task_id=generation["task_id"], view=view, now=now)
            return view

    @retry_database_mutation
    def confirm(self, task_id: str, plan_version: int, *, idempotency_key: str | None = None) -> TaskView:
        validate_idempotency_key(idempotency_key)
        payload = confirm_payload(task_id, plan_version)
        now = self.clock.now().isoformat()
        with self.db.transaction() as conn:
            replay = lookup_replay(conn, "confirm", idempotency_key, payload)
            if replay is not None:
                return replay
            config = self._refresh_config(conn)
            task = read_task_row(conn, task_id)
            if task["confirmed_plan_version"] == plan_version and task["head_plan_version"] == plan_version:
                existing = conn.execute(
                    """SELECT generation_id FROM generations
                       WHERE task_id=:task_id AND plan_version=:plan_version
                       ORDER BY created_at ASC, generation_id ASC LIMIT 1""",
                    {'task_id': task_id, 'plan_version': plan_version},
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
                'SELECT abandoned FROM plan_versions WHERE task_id=:task_id AND plan_version=:plan_version',
                {'task_id': task_id, 'plan_version': plan_version},
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
                copied_config=self._snapshot_config(conn, config),
                now=now,
                confirm_version=plan_version,
            )
            view = read_task_view(conn, task_id)
            store_replay(conn, method="confirm", key=idempotency_key, payload=payload, task_id=task_id, view=view, now=now)
            return view

    @retry_database_mutation
    def regenerate(self, task_id: str, request: RegenerateRequest, *, idempotency_key: str | None = None) -> TaskView:
        validate_idempotency_key(idempotency_key)
        payload = regenerate_payload(task_id, request)
        now = self.clock.now().isoformat()
        with self.db.transaction() as conn:
            replay = lookup_replay(conn, "regenerate", idempotency_key, payload)
            if replay is not None:
                return replay
            config = self._refresh_config(conn)
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
                    copied_config=self._snapshot_config(conn, config),
                    now=now,
                    confirm_version=None,
                )
            else:
                if request.artifact_id is None or len(request.shot_ids) == 0:
                    raise VideoAgentError("VALIDATION_ERROR", "局部重生成需要基础成品和至少一个镜头", fields=["artifact_id", "shot_ids"])
                artifact = conn.execute('SELECT * FROM artifacts WHERE artifact_id=:request_artifact_id', {'request_artifact_id': request.artifact_id}).fetchone()
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
                    copied_config=self._snapshot_config(conn, config),
                    now=now,
                    confirm_version=None,
                )
            view = read_task_view(conn, task_id)
            store_replay(conn, method="regenerate", key=idempotency_key, payload=payload, task_id=task_id, view=view, now=now)
            return view

    def tick(self, *, runtime_owner: str | None = None) -> TickResult:
        from video_agent.worker import tick_once

        return tick_once(self, runtime_owner=runtime_owner)

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
        row = conn.execute('SELECT kind FROM assets WHERE asset_id=:asset_id', {'asset_id': asset_id}).fetchone()
        if row is None or row["kind"] != "image":
            raise VideoAgentError("VALIDATION_ERROR", "图片素材不存在或不是图片", fields=["images"])


def _require_audio(conn, asset_id: str | None) -> None:
    asset_id = _blank_to_none(asset_id)
    if asset_id is None:
        return
    row = conn.execute('SELECT kind FROM assets WHERE asset_id=:asset_id', {'asset_id': asset_id}).fetchone()
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


def _artifact_visual_check_enabled(raw_snapshot: str | None, raw_quality: str | None = None) -> bool | None:
    """Read explicit export policy, falling back to historical generation settings.

    The public half of an encrypted snapshot contains adapter names. Never decrypt
    or return credentials just to describe review policy. Unknown legacy data must
    not authorize delivery of an unchecked artifact.
    """
    if raw_quality is not None:
        try:
            if json.loads(raw_quality).get("visual_check_enabled") is False:
                return False
        except (ValueError, TypeError, AttributeError):
            pass
    try:
        snapshot = json.loads(raw_snapshot)
        if snapshot.get("format_version") == 1:
            config = snapshot["config"]
        elif "format_version" not in snapshot:
            config = snapshot
        else:
            return None
        mode = config.get("mode")
        visual = config.get("adapters", {}).get("visual", {}).get("adapter")
        if mode == "fake":
            return False
        if mode == "real" and isinstance(visual, str) and visual:
            return visual != "none"
    except (ValueError, TypeError, KeyError, AttributeError):
        pass
    return None
