import shutil
from pathlib import Path

import pytest
from PIL import Image

from video_agent.models import SharedVisual
from video_agent.ports import ShotRequest


def _need_ffmpeg() -> None:
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("ffmpeg and ffprobe are required")


def test_fake_video_capabilities_and_idempotent_submit():
    from video_agent.fake_video import FakeVideo

    video = FakeVideo()
    capabilities = video.capabilities()
    assert capabilities.native_aspects == ["9:16"]
    assert capabilities.max_segment_seconds == 30
    assert capabilities.supports_first_frame is True
    assert capabilities.supports_reference_images is True
    assert capabilities.supports_tail_frame is True
    request = _request(Path("frame.png"), 12)
    first = video.submit_shot(request)
    second = video.submit_shot(request)
    assert first.uncertain is False
    assert first.external_task_id == second.external_task_id
    assert video.query(external_task_id=None, provider_idempotency_key=request.provider_idempotency_key).state == "succeeded"


def test_fake_video_download_matches_requested_frames(tmp_path: Path):
    _need_ffmpeg()
    from video_agent.fake_video import FakeVideo
    from video_agent.media import probe

    image = tmp_path / "frame.png"
    Image.new("RGB", (18, 32), (220, 10, 10)).save(image)
    video = FakeVideo()
    request = _request(image, 12)
    submitted = video.submit_shot(request)
    assert submitted.external_task_id is not None
    dest = tmp_path / "clip.mp4"
    dest.write_bytes(video.download(submitted.external_task_id))
    info = probe(dest)
    assert info.frame_count == 12
    assert info.width == 18
    assert info.height == 32
    assert info.has_audio is False


def _request(image: Path, frames: int) -> ShotRequest:
    return ShotRequest(
        generation_id="gen-1",
        shot_id="s1",
        prompt="展示",
        shared_visual=SharedVisual(
            product_reference="杯子",
            scene="桌面",
            lighting="均匀",
            palette="红",
            style="写实",
        ),
        first_frame_path=image,
        reference_paths=[image],
        tail_frame_path=None,
        frame_count=frames,
        aspect_ratio="9:16",
        fit="native",
        width=18,
        height=32,
        provider_idempotency_key="gen-1:s1",
    )
