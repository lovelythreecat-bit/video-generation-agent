from __future__ import annotations

import tempfile
import uuid
from pathlib import Path

from video_agent.errors import VideoAgentError
from video_agent.media import fit_image, still_clip
from video_agent.ports import QueryResult, ShotRequest, SubmitResult, VideoCapabilities

_CLIP_BITRATE = 500_000


class FakeVideo:
    def __init__(self) -> None:
        self._by_key: dict[str, tuple[str, ShotRequest]] = {}
        self._by_external: dict[str, ShotRequest] = {}

    def capabilities(self) -> VideoCapabilities:
        return VideoCapabilities(
            native_aspects=["9:16"],
            max_segment_seconds=30,
            supports_first_frame=True,
            supports_reference_images=True,
            supports_tail_frame=True,
        )

    def submit_shot(self, request: ShotRequest) -> SubmitResult:
        existing = self._by_key.get(request.provider_idempotency_key)
        if existing is not None:
            return SubmitResult(external_task_id=existing[0], uncertain=False)
        external_task_id = str(uuid.uuid4())
        self._by_key[request.provider_idempotency_key] = (external_task_id, request)
        self._by_external[external_task_id] = request
        return SubmitResult(external_task_id=external_task_id, uncertain=False)

    def query(self, *, external_task_id: str | None, provider_idempotency_key: str) -> QueryResult:
        if provider_idempotency_key in self._by_key or (
            external_task_id is not None and external_task_id in self._by_external
        ):
            return QueryResult(state="succeeded")
        return QueryResult(state="absent", message="外部任务不存在")

    def download(self, external_task_id: str) -> bytes:
        request = self._by_external.get(external_task_id)
        if request is None:
            raise VideoAgentError("PROVIDER_REJECTED", "找不到已提交的替身镜头", retryable=True)
        image = request.first_frame_path or (request.reference_paths[0] if request.reference_paths else None)
        if image is None:
            raise VideoAgentError("PROVIDER_REJECTED", "没有首帧或参考图", retryable=True)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fitted = root / "fitted.png"
            clip = root / "clip.mp4"
            fit_image(image, request.fit, request.width, request.height, fitted)
            still_clip(fitted, request.frame_count, request.width, request.height, clip, video_bitrate=_CLIP_BITRATE)
            return clip.read_bytes()
