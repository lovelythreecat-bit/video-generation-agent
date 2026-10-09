import json
import sqlite3
from pathlib import Path

import pytest
from PIL import Image

from video_agent.fake_planner import FakePlanner
from video_agent.fake_video import FakeVideo
from video_agent.models import CreateTaskRequest, ProductInfo, VideoOptions
from video_agent.ports import QueryResult, ShotRequest, SubmitResult, VideoCapabilities
from video_agent.service import VideoAgent


def _home(tmp_path: Path) -> Path:
    home = tmp_path / "va"
    home.mkdir(parents=True)
    payload = {
        "mode": "fake",
        "duration_tiers": [5, 10, 15, 30],
        "default_duration": 10,
        "max_images": 12,
        "max_file_bytes": 20971520,
        "video_bitrate": 8000000,
        "audio_bitrate": 192000,
        "lease_seconds": 15,
        "resolutions": {
            "9:16": [180, 320],
            "16:9": [320, 180],
            "1:1": [64, 64],
            "4:5": [1080, 1350],
            "3:4": [1080, 1440],
            "4:3": [1440, 1080],
            "3:2": [1620, 1080],
            "2:3": [1080, 1620],
        },
        "adapters": {
            "planner": {"adapter": "fake"},
            "video": {"adapter": "fake"},
            "voice": {"adapter": "fake"},
            "music": {"adapter": "fake"},
            "visual": {"adapter": "none"},
        },
    }
    (home / "config.json").write_text(json.dumps(payload), encoding="utf-8")
    return home


def _png() -> bytes:
    import io

    buffer = io.BytesIO()
    Image.new("RGB", (18, 32), (220, 10, 10)).save(buffer, format="PNG")
    return buffer.getvalue()


class ExplodingVideo:
    def capabilities(self) -> VideoCapabilities:
        return VideoCapabilities(
            native_aspects=["9:16"],
            max_segment_seconds=30,
            supports_first_frame=True,
            supports_reference_images=True,
            supports_tail_frame=True,
        )

    def submit_shot(self, request: ShotRequest) -> SubmitResult:
        raise AssertionError("must not submit")

    def query(self, *, external_task_id: str | None, provider_idempotency_key: str) -> QueryResult:
        raise AssertionError("must not query")

    def download(self, external_task_id: str) -> bytes:
        raise AssertionError("must not download")


def _until_settled(agent: VideoAgent, task_id: str):
    for _ in range(20):
        view = agent.get_task(task_id)
        if view.status not in {"pending", "planning", "generating"}:
            return view
        agent.tick()
    return agent.get_task(task_id)


def test_missing_first_frame_fails_before_video(tmp_path: Path):
    home = _home(tmp_path)
    agent = VideoAgent(home=home, planner=FakePlanner(), video=ExplodingVideo())
    asset_id = agent.register_asset(filename="p.png", kind="image", data=_png())
    created = agent.create_task(
        CreateTaskRequest(
            product_info=ProductInfo(name="杯子", selling_points=["耐热"]),
            images=[asset_id],
            user_prompt="放在桌上",
            video_options=VideoOptions(duration_seconds=5, subtitles=False, voiceover=False, music=False),
        )
    )
    ready = _until_settled(agent, created.task_id)
    assert ready.status == "awaiting_confirmation"
    assert ready.head_plan_version is not None
    agent.confirm(created.task_id, ready.head_plan_version)
    failed = _until_settled(agent, created.task_id)
    assert failed.status == "failed"
    assert failed.failure is not None
    assert failed.failure.code == "FIRST_FRAME_NOT_IMPLEMENTED"
    assert failed.failure.retryable is False
    assert failed.failure.stage == "preparing_first_frame"
    assert failed.generation_id is not None
    generation = agent.get_generation(failed.generation_id)
    assert generation.failure is not None
    assert generation.failure.code == "FIRST_FRAME_NOT_IMPLEMENTED"


def test_selected_first_frame_is_fitted_and_advances(tmp_path: Path):
    home = _home(tmp_path)
    agent = VideoAgent(home=home, planner=FakePlanner(), video=FakeVideo())
    asset_id = agent.register_asset(filename="p.png", kind="image", data=_png())
    created = agent.create_task(
        CreateTaskRequest(
            product_info=ProductInfo(name="杯子", selling_points=["耐热"]),
            images=[asset_id],
            first_frame_asset_id=asset_id,
            user_prompt="放在桌上",
            video_options=VideoOptions(duration_seconds=5, subtitles=False, voiceover=False, music=False),
        )
    )
    ready = _until_settled(agent, created.task_id)
    assert ready.head_plan_version is not None
    agent.confirm(created.task_id, ready.head_plan_version)
    tick = agent.tick()
    assert tick.did_work is True
    assert tick.step == "first_frame"
    view = agent.get_task(created.task_id)
    assert view.status == "generating"
    assert view.total_shots == 1
    assert view.completed_shots == 0
    assert view.generation_id is not None
    fitted = home / "tasks" / created.task_id / "generations" / view.generation_id / "first_frame_fitted.png"
    assert fitted.is_file()
    connection = sqlite3.connect(home / "agent.sqlite")
    step = connection.execute(
        "SELECT step FROM work_items WHERE generation_id=? AND done=0",
        (view.generation_id,),
    ).fetchone()
    assert step is not None
    assert step[0] == "submit_shot"
    count = connection.execute(
        "SELECT COUNT(*) FROM generation_shots WHERE generation_id=?",
        (view.generation_id,),
    ).fetchone()
    assert count[0] == 1


def test_missing_asset_file_does_not_call_video(tmp_path: Path):
    home = _home(tmp_path)
    agent = VideoAgent(home=home, planner=FakePlanner(), video=FakeVideo())
    asset_id = agent.register_asset(filename="p.png", kind="image", data=_png())
    created = agent.create_task(
        CreateTaskRequest(
            product_info=ProductInfo(name="杯子", selling_points=["耐热"]),
            images=[asset_id],
            first_frame_asset_id=asset_id,
            user_prompt="放在桌上",
            video_options=VideoOptions(duration_seconds=5, subtitles=False, voiceover=False, music=False),
        )
    )
    ready = _until_settled(agent, created.task_id)
    assert ready.head_plan_version is not None
    agent.confirm(created.task_id, ready.head_plan_version)
    for path in (home / "assets").rglob("*"):
        if path.is_file():
            path.unlink()
    agent.video = ExplodingVideo()
    failed = _until_settled(agent, created.task_id)
    assert failed.failure is not None
    assert failed.failure.code == "ASSET_MISSING"
    assert failed.failure.retryable is True
    assert failed.failure.stage == "preparing_first_frame"
