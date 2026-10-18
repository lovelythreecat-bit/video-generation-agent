import pytest

from video_agent.config import AppConfig
from video_agent.errors import VideoAgentError


DEFAULT = {
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


def _config(mode: str, planner: str, video: str, planner_fields: dict[str, str] | None = None) -> AppConfig:
    payload = dict(DEFAULT)
    payload["mode"] = mode
    extra = planner_fields or {}
    payload["adapters"] = {
        "planner": {"adapter": planner, **extra},
        "video": {"adapter": video, **extra},
        "voice": {"adapter": "none"},
        "music": {"adapter": "none"},
        "visual": {"adapter": "none"},
    }
    return AppConfig.model_validate(payload)


def test_real_adapters_register_text_video_and_vision():
    from video_agent.factory import REAL_ADAPTERS

    assert set(REAL_ADAPTERS) >= {"openai_chat", "seedance", "openai_vision"}


def test_real_mode_without_registry_is_rejected():
    from video_agent.factory import build_adapters

    with pytest.raises(VideoAgentError) as caught:
        build_adapters(_config("real", "demo-planner", "demo-video"))
    assert caught.value.code == "VALIDATION_ERROR"
    assert caught.value.message == "没有已注册的真实规划或视频适配器"


def test_fake_and_none_are_not_real_adapters():
    from video_agent.factory import REAL_ADAPTERS, build_adapters

    saved = dict(REAL_ADAPTERS)
    REAL_ADAPTERS["fake"] = object()
    try:
        with pytest.raises(VideoAgentError) as caught:
            build_adapters(_config("real", "fake", "none"))
        assert caught.value.message == "没有已注册的真实规划或视频适配器"
    finally:
        REAL_ADAPTERS.clear()
        REAL_ADAPTERS.update(saved)


def test_missing_required_adapter_field_is_rejected():
    from video_agent.factory import REAL_ADAPTERS, build_adapters

    class Spec:
        required_fields = ("api_key",)

        def build(self, fields: dict[str, str]) -> object:
            return fields

    saved = dict(REAL_ADAPTERS)
    REAL_ADAPTERS["demo"] = Spec()
    try:
        with pytest.raises(VideoAgentError) as caught:
            build_adapters(_config("real", "demo", "demo", {"api_key": ""}))
        assert caught.value.code == "VALIDATION_ERROR"
        assert caught.value.fields == ["api_key"]
    finally:
        REAL_ADAPTERS.clear()
        REAL_ADAPTERS.update(saved)
    assert "demo" not in REAL_ADAPTERS
    assert "openai_chat" in REAL_ADAPTERS


def test_real_mode_builds_text_video_and_optional_vision(tmp_path):
    from video_agent.factory import build_adapters
    from video_agent.llm_planner import OpenAIChatPlanner
    from video_agent.llm_visual import OpenAIVisionChecker
    from video_agent.seedance_video import SeedanceVideo

    fields = {"base_url": "https://example.invalid/v1", "api_key": "secret", "model": "demo-model"}
    payload = dict(DEFAULT)
    payload["mode"] = "real"
    payload["adapters"] = {
        "planner": {"adapter": "openai_chat", **fields},
        "video": {"adapter": "seedance", **fields},
        "voice": {"adapter": "none"},
        "music": {"adapter": "fake"},
        "visual": {"adapter": "openai_vision", **fields},
    }
    planner, video, voice, music, visual = build_adapters(AppConfig.model_validate(payload), tmp_path)
    assert isinstance(planner, OpenAIChatPlanner)
    assert isinstance(video, SeedanceVideo)
    assert voice is None
    assert music is not None
    assert isinstance(visual, OpenAIVisionChecker)
    assert video.state_path == tmp_path / "provider-state" / "seedance-tasks.json"


def test_real_mode_builds_wan(tmp_path):
    from video_agent.factory import build_adapters
    from video_agent.wan_video import WanVideo

    fields = {"base_url": "https://example.invalid/api/v1", "api_key": "secret", "model": "wan2.6-i2v-flash"}
    payload = dict(DEFAULT)
    payload["mode"] = "real"
    payload["adapters"] = {
        "planner": {"adapter": "openai_chat", **fields},
        "video": {"adapter": "wan", **fields, "max_segment_seconds": "5"},
        "voice": {"adapter": "none"},
        "music": {"adapter": "none"},
        "visual": {"adapter": "none"},
    }
    _planner, video, _voice, _music, _visual = build_adapters(AppConfig.model_validate(payload), tmp_path)
    assert isinstance(video, WanVideo)
    assert str(video.state_path).endswith("wan-tasks.json")
    assert video.state_path == tmp_path / "provider-state" / "wan-tasks.json"
    assert video.capabilities().max_segment_seconds == 5
