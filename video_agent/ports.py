from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, Field

from video_agent.models import AudioOptions, PlanDocument, PlanPatch, ProductInfo, SharedVisual


class VideoCapabilities(BaseModel):
    native_aspects: list[str]
    max_segment_seconds: int
    supports_first_frame: bool
    supports_reference_images: bool
    supports_tail_frame: bool


class PlanRequest(BaseModel):
    product_info: ProductInfo
    user_prompt: str
    images: list[str]
    duration_seconds: int
    duration_tiers: list[int]
    aspect_ratio: str
    subtitles: bool
    voiceover: bool
    music: bool
    audio: AudioOptions
    first_frame_asset_id: str | None
    video_capabilities: VideoCapabilities


class PlanResult(BaseModel):
    document: PlanDocument | None = None
    questions: list[str] | None = None


class ReviseResult(BaseModel):
    patch: PlanPatch | None = None
    questions: list[str] | None = None


class Planner(Protocol):
    def capabilities_note(self) -> str: ...

    def plan(self, request: PlanRequest) -> PlanResult: ...

    def revise(self, current: PlanDocument, instruction: str, request: PlanRequest) -> ReviseResult: ...


class ShotRequest(BaseModel):
    generation_id: str
    shot_id: str
    prompt: str
    shared_visual: SharedVisual
    first_frame_path: Path | None
    reference_paths: list[Path]
    tail_frame_path: Path | None
    frame_count: int
    aspect_ratio: str
    fit: Literal["native", "pad", "crop"]
    width: int
    height: int
    provider_idempotency_key: str


class SubmitResult(BaseModel):
    external_task_id: str | None = None
    uncertain: bool = False


class QueryResult(BaseModel):
    state: Literal["pending", "succeeded", "failed", "absent"]
    error_code: str | None = None
    message: str = ""


class VideoProvider(Protocol):
    def capabilities(self) -> VideoCapabilities: ...

    def submit_shot(self, request: ShotRequest) -> SubmitResult: ...

    def query(self, *, external_task_id: str | None, provider_idempotency_key: str) -> QueryResult: ...

    def download(self, external_task_id: str) -> bytes: ...


class VoiceCue(BaseModel):
    text: str
    start_ms: int
    end_ms: int


class VoiceResult(BaseModel):
    audio: bytes
    cues: list[VoiceCue]
    duration_ms: int


class VoiceProvider(Protocol):
    def synthesize(self, *, text: str, language: str | None, preference: str) -> VoiceResult: ...


class MusicProvider(Protocol):
    def generate(self, *, direction: str, duration_seconds: float) -> bytes: ...


class CheckFrame(BaseModel):
    shot_id: str
    position: Literal["start", "middle", "end", "boundary"]
    path: Path


class VisualResult(BaseModel):
    status: Literal["passed", "flagged", "not_assessed"]
    findings: list[dict] = Field(default_factory=list)
    reason: str = ""


class VisualChecker(Protocol):
    def check(self, frames: list[CheckFrame], appearance: str) -> VisualResult: ...


DEFAULT_VIDEO_CAPABILITIES = VideoCapabilities(
    native_aspects=["9:16"],
    max_segment_seconds=30,
    supports_first_frame=True,
    supports_reference_images=True,
    supports_tail_frame=True,
)
