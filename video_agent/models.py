from typing import Literal

from pydantic import BaseModel, Field


class ProductInfo(BaseModel):
    name: str | None = None
    category: str | None = None
    selling_points: list[str] = Field(default_factory=list)
    specs: str | None = None
    extra: dict[str, str] = Field(default_factory=dict)


class VideoOptions(BaseModel):
    duration_seconds: int | None = None
    aspect_ratio: str = "9:16"
    subtitles: bool = Field(default=True, description="允许视频模型按分镜生成画面文字；不做后期叠字。")
    voiceover: bool = Field(default=True, description="请求视频模型自带音频；不再独立合成配音，无音轨时可导出静音视频。")
    music: bool = Field(default=True, description="请求视频模型自带音频；不再后期配乐，与 voiceover 同时关闭时移除音轨。")


class AudioOptions(BaseModel):
    language: str | None = None
    voice_preference: str = ""
    music_direction: str = ""
    music_asset_id: str | None = None


class CreateTaskRequest(BaseModel):
    product_info: ProductInfo = Field(default_factory=ProductInfo)
    images: list[str] = Field(default_factory=list)
    first_frame_asset_id: str | None = None
    user_prompt: str = ""
    video_options: VideoOptions = Field(default_factory=VideoOptions)
    audio_options: AudioOptions = Field(default_factory=AudioOptions)
    external_user_id: str | None = None
    external_request_id: str | None = None


class InputPatch(BaseModel):
    product_info: ProductInfo | None = None
    user_prompt: str | None = None
    images: list[str] | None = None
    first_frame_asset_id: str | None = None
    clear_first_frame: bool = False
    video_options: VideoOptions | None = None
    audio_options: AudioOptions | None = None


class SharedVisual(BaseModel):
    product_reference: str
    scene: str
    lighting: str
    palette: str
    style: str
    character: str | None = None
    scene_change: str | None = None


class ShotPlan(BaseModel):
    shot_id: str
    content: str
    action: str
    shot_size: str
    camera: str
    scene_id: str
    start_frame: int
    end_frame: int
    subtitle_text: str = Field(default="", description="由视频模型直接生成的画面文字；不需要时留空。")
    voiceover_text: str = ""


class PlanDocument(BaseModel):
    creative: str
    audience: str
    selling_points: list[str]
    shared_visual: SharedVisual
    shots: list[ShotPlan]
    music_direction: str = ""
    music_asset_id: str | None = None
    duration_seconds: int
    aspect_ratio: str
    fit: Literal["native", "pad", "crop"]
    subtitles: bool = Field(description="允许视频模型按分镜生成画面文字；不做后期叠字。")
    voiceover: bool
    music: bool
    first_frame_source: Literal["selected", "generate"]
    first_frame_asset_id: str | None = None
    model_capabilities: str
    language: str | None = None
    voice_preference: str = ""


class ShotTextPatch(BaseModel):
    shot_id: str
    content: str | None = None
    action: str | None = None
    shot_size: str | None = None
    camera: str | None = None
    scene_id: str | None = None
    subtitle_text: str | None = Field(default=None, description="画面文字文案；空字符串表示该镜头无需文字。")
    voiceover_text: str | None = None


class PlanPatch(BaseModel):
    duration_seconds: int | None = None
    aspect_ratio: str | None = None
    fit: Literal["native", "pad", "crop"] | None = None
    subtitles: bool | None = Field(default=None, description="允许视频模型按分镜生成画面文字；不做后期叠字。")
    voiceover: bool | None = None
    music: bool | None = None
    language: str | None = None
    voice_preference: str | None = None
    music_direction: str | None = None
    music_asset_id: str | None = None
    clear_music_asset: bool = False
    first_frame_asset_id: str | None = None
    clear_first_frame: bool = False
    shared_visual: SharedVisual | None = None
    shots: list[ShotPlan] | None = None
    shot_texts: list[ShotTextPatch] | None = None


class PlanChange(BaseModel):
    instruction: str | None = None
    patch: PlanPatch | None = None


class RegenerateRequest(BaseModel):
    kind: Literal["full", "shots"]
    artifact_id: str | None = None
    shot_ids: list[str] = Field(default_factory=list)


class FailureView(BaseModel):
    code: str
    message: str
    stage: str | None = None
    retryable: bool


class TaskView(BaseModel):
    task_id: str
    status: str
    stage: str | None = None
    stage_detail: str | None = None
    failure_scope: str | None = None
    failure: FailureView | None = None
    questions: list[str] = Field(default_factory=list)
    external_user_id: str | None = None
    external_request_id: str | None = None
    head_plan_version: int | None = None
    confirmed_plan_version: int | None = None
    generation_id: str | None = None
    completed_shots: int | None = None
    total_shots: int | None = None
    product_name: str | None = None
    product_info: ProductInfo = Field(default_factory=ProductInfo)
    user_prompt: str = ""
    video_options: VideoOptions = Field(default_factory=VideoOptions)
    audio_options: AudioOptions = Field(default_factory=AudioOptions)
    image_asset_ids: list[str] = Field(default_factory=list)
    first_frame_asset_id: str | None = None
    updated_at: str


class TaskSummary(BaseModel):
    task_id: str
    status: str
    product_name: str | None
    updated_at: str


class TaskPage(BaseModel):
    items: list[TaskSummary]
    limit: int
    offset: int
    total: int


class AssetView(BaseModel):
    asset_id: str
    kind: Literal["image", "audio"]
    filename: str
    media_type: str
    byte_size: int


class PlanView(BaseModel):
    task_id: str
    plan_version: int
    is_head: bool
    confirmed: bool
    abandoned: bool
    document: PlanDocument


class GenerationView(BaseModel):
    generation_id: str
    task_id: str
    plan_version: int
    kind: Literal["full", "shots"]
    shot_ids: list[str]
    base_artifact_id: str | None
    status: str
    stage: str
    stage_detail: str | None = None
    completed_shots: int | None = None
    total_shots: int | None = None
    failure: FailureView | None = None


class ArtifactView(BaseModel):
    artifact_id: str
    task_id: str
    generation_id: str
    plan_version: int
    version: int
    duration_seconds: int
    aspect_ratio: str
    width: int
    height: int
    quality_status: Literal["passed", "flagged", "not_assessed"]
    visual_check_enabled: bool | None = None
    quality: dict
    created_at: str


class TickResult(BaseModel):
    did_work: bool
    task_id: str | None = None
    step: str | None = None
