from __future__ import annotations

import hashlib
import io
import json
import shutil
import sqlite3
import wave
from datetime import datetime, timezone
from pathlib import Path

import pytest
from PIL import Image

from video_agent.clock import FrozenClock
from video_agent.config import AdapterSetting, SettingsPatch, save_settings
from video_agent.models import (
    AudioOptions,
    CreateTaskRequest,
    PlanChange,
    PlanPatch,
    ProductInfo,
    RegenerateRequest,
    VideoOptions,
)
from video_agent.ports import VideoCapabilities
from video_agent.service import VideoAgent


def require_ffmpeg() -> None:
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("需要本机 PATH 上的 ffmpeg 和 ffprobe")


def png_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (64, 64), (180, 30, 30)).save(buf, format="PNG")
    return buf.getvalue()


def wav_bytes() -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(48000)
        handle.writeframes(b"\x00\x00" * 4800)
    return buf.getvalue()


def capabilities() -> VideoCapabilities:
    return VideoCapabilities(
        native_aspects=["9:16"],
        max_segment_seconds=30,
        supports_first_frame=True,
        supports_reference_images=True,
        supports_tail_frame=True,
    )


def write_fast_config(home: Path) -> None:
    home.mkdir(parents=True, exist_ok=True)
    payload = {
        "mode": "fake",
        "duration_tiers": [5, 10, 15, 30],
        "default_duration": 10,
        "max_images": 12,
        "max_file_bytes": 20971520,
        "video_bitrate": 8000000,
        "audio_bitrate": 192000,
        "lease_seconds": 60,
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


def make_agent(tmp_path: Path, **providers) -> VideoAgent:
    home = tmp_path / "home"
    write_fast_config(home)
    clock = FrozenClock(datetime(2026, 10, 9, tzinfo=timezone.utc))
    return VideoAgent(home, clock=clock, **providers)


def drain(agent: VideoAgent, task_id: str, limit: int = 80):
    task = agent.get_task(task_id)
    for _ in range(limit):
        if task.status not in {"pending", "planning", "generating"}:
            return task
        result = agent.tick()
        if not result.did_work:
            agent.clock.advance(1)
        task = agent.get_task(task_id)
    raise AssertionError((task.status, task.stage, task.failure))


def read_rows(home: Path, sql: str, params: tuple = ()):
    conn = sqlite3.connect(home / "agent.sqlite")
    conn.row_factory = sqlite3.Row
    try:
        return list(conn.execute(sql, params))
    finally:
        conn.close()


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class CountingVoice:
    def __init__(self, inner) -> None:
        self.inner = inner
        self.calls = 0

    def synthesize(self, *, text: str, language: str | None, preference: str):
        self.calls += 1
        return self.inner.synthesize(text=text, language=language, preference=preference)


class CountingMusic:
    def __init__(self, inner) -> None:
        self.inner = inner
        self.calls = 0

    def generate(self, *, direction: str, duration_seconds: float) -> bytes:
        self.calls += 1
        return self.inner.generate(direction=direction, duration_seconds=duration_seconds)


def open_plan(agent: VideoAgent, *, first_frame: bool = True, duration: int = 5, music_asset_id: str | None = None):
    image_id = agent.register_asset(filename="product.png", kind="image", data=png_bytes())
    task = agent.create_task(
        CreateTaskRequest(
            product_info=ProductInfo(name="杯子", category="家居", selling_points=["保温"]),
            images=[image_id],
            first_frame_asset_id=image_id if first_frame else None,
            user_prompt="展示杯身",
            video_options=VideoOptions(
                duration_seconds=duration,
                aspect_ratio="9:16",
                subtitles=True,
                voiceover=True,
                music=True,
            ),
            audio_options=AudioOptions(music_direction="轻快", music_asset_id=music_asset_id),
        )
    )
    task = drain(agent, task.task_id)
    return task, image_id


def revise_two_shots(agent: VideoAgent, task_id: str) -> None:
    task = agent.get_task(task_id)
    plan = agent.get_plan(task_id, int(task.head_plan_version))
    shot = plan.document.shots[0]
    total = plan.document.duration_seconds * 30
    half = total // 2
    first = shot.model_copy(update={"shot_id": "s1", "start_frame": 0, "end_frame": half})
    second = shot.model_copy(update={"shot_id": "s2", "start_frame": half, "end_frame": total, "content": shot.content + "特写"})
    agent.revise_plan(task_id, PlanChange(patch=PlanPatch(shots=[first, second])))


def confirm_and_finish(agent: VideoAgent, task_id: str):
    task = agent.get_task(task_id)
    agent.confirm(task_id, int(task.head_plan_version))
    return drain(agent, task_id)


def generation_dir(agent: VideoAgent, task_id: str, generation_id: str) -> Path:
    return agent.home / "tasks" / task_id / "generations" / generation_id


def set_voice_marker(agent: VideoAgent, marker: str) -> None:
    save_settings(agent.home, SettingsPatch(voice=AdapterSetting(adapter="fake", fields={"marker": marker})))
