from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from pathlib import Path

from video_agent.models import (
    ArtifactView,
    AudioOptions,
    CreateTaskRequest,
    InputPatch,
    PlanChange,
    PlanPatch,
    PlanView,
    ProductInfo,
    RegenerateRequest,
    ShotTextPatch,
    TaskSummary,
    TaskView,
    VideoOptions,
)

POLL_INTERVAL_MS = 1000
IDLE_SLEEP_SECONDS = 1


@dataclass(frozen=True)
class ButtonStates:
    create: bool
    submit_inputs: bool
    revise_plan: bool
    confirm: bool
    discard_revision: bool
    regenerate_full: bool
    regenerate_shots: bool
    retry_planning: bool
    retry_generation: bool
    open_artifact: bool


def button_states(
    *,
    status: str | None,
    failure_scope: str | None,
    has_head: bool,
    head_confirmed: bool,
    has_confirmed_plan: bool,
    has_successful_artifact: bool,
    ffmpeg_available: bool,
) -> ButtonStates:
    can_regenerate = has_confirmed_plan and ffmpeg_available
    shots = can_regenerate and has_successful_artifact
    submit_inputs = False
    revise_plan = False
    confirm = False
    discard_revision = False
    regenerate_full = False
    regenerate_shots = False
    retry_planning = False
    retry_generation = False

    if status == "awaiting_input":
        submit_inputs = True
        discard_revision = has_confirmed_plan
    elif status == "awaiting_confirmation":
        revise_plan = True
        confirm = ffmpeg_available
        discard_revision = has_confirmed_plan
        regenerate_full = can_regenerate
        regenerate_shots = shots
    elif status == "succeeded":
        revise_plan = True
        regenerate_full = can_regenerate
        regenerate_shots = shots
    elif status == "failed" and failure_scope == "planning":
        retry_planning = True
        submit_inputs = not has_head
        revise_plan = has_head
        discard_revision = has_confirmed_plan
    elif status == "failed" and failure_scope == "generation":
        retry_generation = ffmpeg_available
        revise_plan = True
        regenerate_full = can_regenerate
        regenerate_shots = shots
        head_open = has_head and not head_confirmed
        confirm = head_open and ffmpeg_available
        discard_revision = head_open and has_confirmed_plan

    return ButtonStates(
        create=ffmpeg_available,
        submit_inputs=submit_inputs,
        revise_plan=revise_plan,
        confirm=confirm,
        discard_revision=discard_revision,
        regenerate_full=regenerate_full,
        regenerate_shots=regenerate_shots,
        retry_planning=retry_planning,
        retry_generation=retry_generation,
        open_artifact=has_successful_artifact,
    )


def button_states_for(
    task: TaskView | None,
    *,
    has_successful_artifact: bool,
    ffmpeg_available: bool,
) -> ButtonStates:
    if task is None:
        return button_states(
            status=None,
            failure_scope=None,
            has_head=False,
            head_confirmed=False,
            has_confirmed_plan=False,
            has_successful_artifact=has_successful_artifact,
            ffmpeg_available=ffmpeg_available,
        )
    has_head = task.head_plan_version is not None
    has_confirmed_plan = task.confirmed_plan_version is not None
    head_confirmed = has_head and task.head_plan_version == task.confirmed_plan_version
    return button_states(
        status=task.status,
        failure_scope=task.failure_scope,
        has_head=has_head,
        head_confirmed=head_confirmed,
        has_confirmed_plan=has_confirmed_plan,
        has_successful_artifact=has_successful_artifact,
        ffmpeg_available=ffmpeg_available,
    )


def failure_text(task: TaskView | None) -> tuple[str, str, str]:
    if task is None or task.status != "failed" or task.failure is None:
        return ("", "", "")
    return (task.failure.code, task.failure.message, task.failure.stage or "")


def question_text(task: TaskView | None) -> str:
    if task is None or task.status != "awaiting_input":
        return ""
    return "\n".join(task.questions)


def error_dialog_text(code: str, message: str, stage: str | None) -> str:
    return f"{code}\n{message}\n{stage or ''}"


def should_poll(status: str | None) -> bool:
    return status in {"pending", "planning", "generating"}


def new_idempotency_key() -> str:
    return uuid.uuid4().hex


def command_enabled(allowed: bool, busy: bool) -> bool:
    return allowed and not busy


def resolve_home() -> Path:
    raw = os.environ.get("VIDEO_AGENT_HOME")
    if raw:
        return Path(raw)
    return Path.cwd() / ".video-agent"


WINDOW_TITLE = "视频生成测试台"
MODE_CHOICES = (("fake", "替身"), ("real", "真实"))


def ffmpeg_label(available: bool) -> str:
    if available:
        return "FFmpeg：可用"
    return "FFmpeg：缺少 FFmpeg"


def mode_label(mode: str) -> str:
    for key, text in MODE_CHOICES:
        if key == mode:
            return f"模式：{text}"
    return f"模式：{mode}"


def home_label(home: Path) -> str:
    return f"数据目录：{home.as_posix()}"


def format_task_row(summary: TaskSummary) -> str:
    name = summary.product_name or "未命名"
    return f"{name}  {summary.status}  {summary.task_id}"


def format_execution(task: TaskView) -> str:
    if task.completed_shots is None or task.total_shots is None:
        shots = ""
    else:
        shots = f"{task.completed_shots}/{task.total_shots}"
    return f"阶段：{task.stage or ''}\n已完成镜头：{shots}\n阶段说明：{task.stage_detail or ''}"


FIRST_FRAME_NONE = "不指定首帧"
ASPECT_RATIOS = ("9:16", "16:9", "1:1", "4:5", "3:4", "4:3", "3:2", "2:3")


@dataclass
class DemandEdit:
    name: str
    category: str
    selling_points: list[str]
    specs: str
    user_prompt: str
    images: list[str]
    first_frame_asset_id: str | None
    duration_seconds: int
    aspect_ratio: str
    subtitles: bool
    voiceover: bool
    music: bool
    language: str
    voice_preference: str
    music_direction: str
    music_asset_id: str | None


@dataclass
class PlanEdit:
    duration_seconds: int
    aspect_ratio: str
    fit: str
    subtitles: bool
    voiceover: bool
    music: bool
    language: str
    voice_preference: str
    music_direction: str
    music_asset_id: str | None
    clear_music_asset: bool
    first_frame_asset_id: str | None
    clear_first_frame: bool
    shot_texts: list[ShotTextPatch]


def parse_selling_points(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def _blank_none(value: str) -> str | None:
    text = value.strip()
    if text:
        return text
    return None


def _product(edit: DemandEdit) -> ProductInfo:
    return ProductInfo(
        name=_blank_none(edit.name),
        category=_blank_none(edit.category),
        selling_points=list(edit.selling_points),
        specs=_blank_none(edit.specs),
    )


def _video(edit: DemandEdit) -> VideoOptions:
    return VideoOptions(
        duration_seconds=edit.duration_seconds,
        aspect_ratio=edit.aspect_ratio,
        subtitles=edit.subtitles,
        voiceover=edit.voiceover,
        music=edit.music,
    )


def _audio_demand(edit: DemandEdit) -> AudioOptions:
    return AudioOptions(
        language=_blank_none(edit.language),
        voice_preference=edit.voice_preference,
        music_direction=edit.music_direction,
        music_asset_id=edit.music_asset_id,
    )


def build_create_request(edit: DemandEdit) -> CreateTaskRequest:
    return CreateTaskRequest(
        product_info=_product(edit),
        images=list(edit.images),
        first_frame_asset_id=edit.first_frame_asset_id,
        user_prompt=edit.user_prompt,
        video_options=_video(edit),
        audio_options=_audio_demand(edit),
    )


def build_input_patch(edit: DemandEdit, *, images_changed: bool) -> InputPatch:
    clear_first = edit.first_frame_asset_id is None
    return InputPatch(
        product_info=_product(edit),
        user_prompt=edit.user_prompt,
        images=list(edit.images) if images_changed else None,
        first_frame_asset_id=edit.first_frame_asset_id,
        clear_first_frame=clear_first,
        video_options=_video(edit),
        audio_options=_audio_demand(edit),
    )


def build_plan_change(instruction: str, edit: PlanEdit) -> PlanChange:
    text = instruction.strip()
    if text:
        return PlanChange(instruction=text)
    first_id = None if edit.clear_first_frame else edit.first_frame_asset_id
    return PlanChange(
        patch=PlanPatch(
            duration_seconds=edit.duration_seconds,
            aspect_ratio=edit.aspect_ratio,
            fit=edit.fit,
            subtitles=edit.subtitles,
            voiceover=edit.voiceover,
            music=edit.music,
            language=_blank_none(edit.language),
            voice_preference=edit.voice_preference,
            music_direction=edit.music_direction,
            music_asset_id=None if edit.clear_music_asset else edit.music_asset_id,
            clear_music_asset=edit.clear_music_asset,
            first_frame_asset_id=first_id,
            clear_first_frame=edit.clear_first_frame,
            shot_texts=list(edit.shot_texts),
        )
    )


def build_regenerate_full() -> RegenerateRequest:
    return RegenerateRequest(kind="full")


def build_regenerate_shots(artifact_id: str, shot_ids: list[str]) -> RegenerateRequest:
    return RegenerateRequest(kind="shots", artifact_id=artifact_id, shot_ids=list(shot_ids))


def newest_artifact(artifacts: list[ArtifactView]) -> ArtifactView | None:
    if not artifacts:
        return None
    return artifacts[0]


def shot_plan_version(artifacts: list[ArtifactView]) -> int | None:
    artifact = newest_artifact(artifacts)
    if artifact is None:
        return None
    return artifact.plan_version


def shot_checkbox_items(plan: PlanView) -> list[tuple[str, str]]:
    items = []
    for shot in plan.document.shots:
        label = f"{shot.shot_id}  {shot.content}  [{shot.start_frame},{shot.end_frame})"
        items.append((shot.shot_id, label))
    return items


def format_plan(plan: PlanView) -> str:
    doc = plan.document
    visual = doc.shared_visual
    lines = [
        f"版本 {plan.plan_version}  head={plan.is_head}  已确认={plan.confirmed}  已放弃={plan.abandoned}",
        f"创意：{doc.creative}",
        f"受众：{doc.audience}",
        f"卖点：{'、'.join(doc.selling_points)}",
        f"产品参考：{visual.product_reference}",
        f"场景：{visual.scene}",
        f"光线：{visual.lighting}",
        f"色调：{visual.palette}",
        f"风格：{visual.style}",
        f"人物：{visual.character or ''}",
        f"换景：{visual.scene_change or ''}",
        f"首帧来源：{doc.first_frame_source}  素材：{doc.first_frame_asset_id or ''}",
        f"比例适配：{doc.fit}  字幕：{doc.subtitles}  配音：{doc.voiceover}  音乐：{doc.music}",
        f"时长：{doc.duration_seconds}  比例：{doc.aspect_ratio}",
        f"模型能力：{doc.model_capabilities}",
    ]
    for shot in doc.shots:
        lines.append(
            f"{shot.shot_id}  内容={shot.content}  动作={shot.action}  景别={shot.shot_size}  "
            f"运镜={shot.camera}  场景={shot.scene_id}  帧=[{shot.start_frame},{shot.end_frame})"
        )
    return "\n".join(lines)


def format_artifact(artifact: ArtifactView) -> str:
    return (
        f"v{artifact.version}  {artifact.duration_seconds}s  "
        f"{artifact.width}×{artifact.height}  {artifact.aspect_ratio}  "
        f"质量={artifact.quality_status}  {artifact.artifact_id}"
    )


def first_frame_label(name: str, asset_id: str) -> str:
    return f"{name} | {asset_id}"


def first_frame_asset_id(label: str) -> str | None:
    if label == FIRST_FRAME_NONE or " | " not in label:
        return None
    return label.split(" | ", 1)[1]


from video_agent.config import AdapterSetting, SettingsPatch

SETTINGS_ROLES = (
    ("planner", "规划"),
    ("video", "视频"),
    ("voice", "配音"),
    ("music", "音乐"),
    ("visual", "视觉检查"),
)
_SECRET_MARKERS = ("key", "token", "secret")


@dataclass
class AdapterEdit:
    adapter: str
    fields: dict[str, str]


@dataclass
class SettingsEdit:
    duration_tiers: list[int]
    default_duration: int
    mode: str
    planner: AdapterEdit
    video: AdapterEdit
    voice: AdapterEdit
    music: AdapterEdit
    visual: AdapterEdit


def is_secret_field(name: str) -> bool:
    lowered = name.lower()
    return any(marker in lowered for marker in _SECRET_MARKERS)


def redact_secret_fields(fields: dict[str, str]) -> dict[str, str]:
    shown = {}
    for key, value in fields.items():
        shown[key] = "" if is_secret_field(key) else value
    return shown


def _read(obj: object, name: str, default=None):
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def adapter_field_map(adapter_value: object) -> tuple[str, dict[str, str]]:
    if adapter_value is None:
        return "", {}
    if isinstance(adapter_value, dict):
        name = str(adapter_value.get("adapter", ""))
        nested = adapter_value.get("fields")
        raw = nested if isinstance(nested, dict) else adapter_value
    else:
        name = str(getattr(adapter_value, "adapter", ""))
        nested = getattr(adapter_value, "fields", None)
        if isinstance(nested, dict):
            raw = nested
        else:
            extra = getattr(adapter_value, "model_extra", None) or {}
            raw = {}
            for key in dir(adapter_value):
                if key.startswith("_"):
                    continue
                value = getattr(adapter_value, key, None)
                if isinstance(value, str):
                    raw[key] = value
            for key, value in dict(extra).items():
                if isinstance(value, str):
                    raw[str(key)] = value
    fields = {}
    for key, value in raw.items():
        if key in {"adapter", "fields"}:
            continue
        if isinstance(value, str):
            fields[str(key)] = value
    return name, fields


def settings_form_from_mapping(config: object) -> SettingsEdit:
    adapters = _read(config, "adapters")

    def role(role_name: str) -> AdapterEdit:
        adapter_name, fields = adapter_field_map(_read(adapters, role_name))
        return AdapterEdit(adapter=adapter_name, fields=redact_secret_fields(fields))

    return SettingsEdit(
        duration_tiers=list(_read(config, "duration_tiers")),
        default_duration=int(_read(config, "default_duration")),
        mode=str(_read(config, "mode")),
        planner=role("planner"),
        video=role("video"),
        voice=role("voice"),
        music=role("music"),
        visual=role("visual"),
    )


def fields_to_text(fields: dict[str, str]) -> str:
    return "\n".join(f"{key}={value}" for key, value in fields.items())


def parse_field_lines(text: str) -> dict[str, str]:
    fields = {}
    for line in text.splitlines():
        if not line.strip() or "=" not in line:
            continue
        key, value = line.split("=", 1)
        fields[key.strip()] = value
    return fields


def parse_duration_tiers(text: str) -> list[int]:
    return [int(part.strip()) for part in text.split(",") if part.strip()]


def format_duration_tiers(tiers: list[int]) -> str:
    return ",".join(str(item) for item in tiers)


def mode_from_label(label: str) -> str:
    for mode, text in MODE_CHOICES:
        if label == text:
            return mode
    raise ValueError(label)


def build_settings_patch(edit: SettingsEdit) -> SettingsPatch:
    def one(item: AdapterEdit) -> AdapterSetting:
        return AdapterSetting(adapter=item.adapter.strip(), fields=dict(item.fields))

    return SettingsPatch(
        duration_tiers=list(edit.duration_tiers),
        default_duration=edit.default_duration,
        mode=edit.mode,
        planner=one(edit.planner),
        video=one(edit.video),
        voice=one(edit.voice),
        music=one(edit.music),
        visual=one(edit.visual),
    )


