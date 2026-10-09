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
    create_task_payload,
    lookup_replay,
    store_replay,
    submit_inputs_payload,
    validate_idempotency_key,
)
from video_agent.models import (
    AudioOptions,
    CreateTaskRequest,
    InputPatch,
    ProductInfo,
    TaskView,
    VideoOptions,
)
from video_agent.state import (
    assert_mutation_allowed,
    dumps,
    has_plan,
    insert_work,
    read_task_row,
    read_task_view,
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
