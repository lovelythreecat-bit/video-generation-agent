import json
import shutil
import subprocess
from pathlib import Path

import pytest

from video_agent.config import AppConfig
from video_agent.factory import REAL_ADAPTERS, build_adapters


def _require_ffmpeg() -> None:
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("ffmpeg and ffprobe are required")


def test_none_visual_is_not_assessed():
    from video_agent.visual import NoneVisual

    result = NoneVisual().check([], "红色杯子")
    assert result.status == "not_assessed"
    assert result.reason == "visual adapter is none"
    assert result.findings == []


def test_fake_voice_duration_is_100ms_per_character():
    _require_ffmpeg()
    from video_agent.fake_voice import FakeVoice

    long = FakeVoice().synthesize(text="甲" * 20, language="zh", preference="")
    assert long.duration_ms == 2000
    assert long.cues[0].start_ms == 0
    assert long.cues[0].end_ms == 2000
    assert long.cues[0].text == "甲" * 20
    short = FakeVoice().synthesize(text="杯", language=None, preference="")
    assert short.duration_ms == 400


def test_fake_music_returns_aac(tmp_path: Path):
    _require_ffmpeg()
    from video_agent.fake_music import FakeMusic

    dest = tmp_path / "music.m4a"
    dest.write_bytes(FakeMusic().generate(direction="钢琴", duration_seconds=1))
    completed = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_name,sample_rate,channels", "-of", "json", str(dest)],
        capture_output=True,
        check=False,
    )
    payload = json.loads(completed.stdout.decode("utf-8"))
    stream = payload["streams"][0]
    assert stream["codec_name"] == "aac"
    assert int(stream["sample_rate"]) == 48000
    assert int(stream["channels"]) == 2


def test_fake_mode_builds_stand_ins():
    from video_agent.fake_music import FakeMusic
    from video_agent.fake_planner import FakePlanner
    from video_agent.fake_video import FakeVideo
    from video_agent.fake_voice import FakeVoice
    from video_agent.visual import NoneVisual

    config = AppConfig.model_validate(_default_config())
    planner, video, voice, music, visual = build_adapters(config)
    assert isinstance(planner, FakePlanner)
    assert isinstance(video, FakeVideo)
    assert isinstance(voice, FakeVoice)
    assert isinstance(music, FakeMusic)
    assert isinstance(visual, NoneVisual)


def test_real_mode_optional_fake_and_none():
    from video_agent.fake_voice import FakeVoice
    from video_agent.visual import NoneVisual

    class Spec:
        required_fields = ()

        def build(self, fields: dict[str, str]) -> object:
            return {"fields": fields}

    REAL_ADAPTERS["demo"] = Spec()
    try:
        payload = _default_config()
        payload["mode"] = "real"
        payload["adapters"] = {
            "planner": {"adapter": "demo"},
            "video": {"adapter": "demo"},
            "voice": {"adapter": "fake"},
            "music": {"adapter": "none"},
            "visual": {"adapter": "none"},
        }
        planner, video, voice, music, visual = build_adapters(AppConfig.model_validate(payload))
        assert planner == {"fields": {}}
        assert video == {"fields": {}}
        assert isinstance(voice, FakeVoice)
        assert music is None
        assert isinstance(visual, NoneVisual)
    finally:
        REAL_ADAPTERS.clear()


def _default_config() -> dict:
    return {
        "mode": "fake",
        "duration_tiers": [5, 10, 15, 30],
        "default_duration": 10,
        "max_images": 12,
        "max_file_bytes": 20971520,
        "video_bitrate": 8000000,
        "audio_bitrate": 192000,
        "lease_seconds": 60,
        "resolutions": {
            "9:16": [1080, 1920],
            "16:9": [1920, 1080],
            "1:1": [1080, 1080],
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
