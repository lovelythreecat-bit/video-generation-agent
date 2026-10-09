from __future__ import annotations

from video_agent.config import AppConfig
from video_agent.errors import VideoAgentError
from video_agent.ports import MusicProvider, Planner, VideoProvider, VisualChecker, VoiceProvider

REAL_ADAPTERS: dict[str, object] = {}


def build_adapters(
    config: AppConfig,
) -> tuple[Planner, VideoProvider, VoiceProvider | None, MusicProvider | None, VisualChecker | None]:
    if config.mode == "fake":
        from video_agent.fake_music import FakeMusic
        from video_agent.fake_planner import FakePlanner
        from video_agent.fake_video import FakeVideo
        from video_agent.fake_voice import FakeVoice
        from video_agent.visual import NoneVisual

        return FakePlanner(), FakeVideo(), FakeVoice(), FakeMusic(), NoneVisual()

    planner = _build_required(config.adapters["planner"].adapter, config.adapters["planner"].field_map())
    video = _build_required(config.adapters["video"].adapter, config.adapters["video"].field_map())
    voice = _build_optional(config.adapters["voice"].adapter, config.adapters["voice"].field_map(), _fake_voice, None)
    music = _build_optional(config.adapters["music"].adapter, config.adapters["music"].field_map(), _fake_music, None)
    visual = _build_optional(
        config.adapters["visual"].adapter,
        config.adapters["visual"].field_map(),
        _none_visual,
        _none_visual(),
    )
    return planner, video, voice, music, visual


def _fake_voice() -> VoiceProvider:
    from video_agent.fake_voice import FakeVoice

    return FakeVoice()


def _fake_music() -> MusicProvider:
    from video_agent.fake_music import FakeMusic

    return FakeMusic()


def _none_visual() -> VisualChecker:
    from video_agent.visual import NoneVisual

    return NoneVisual()


def _build_required(name: str, fields: dict[str, str]) -> object:
    if name in {"fake", "none"} or name not in REAL_ADAPTERS:
        raise VideoAgentError("VALIDATION_ERROR", "没有已注册的真实规划或视频适配器")
    return _invoke(name, fields)


def _build_optional(name: str, fields: dict[str, str], fake_factory, none_value):
    if name == "none":
        return none_value
    if name == "fake":
        return fake_factory()
    if name not in REAL_ADAPTERS:
        raise VideoAgentError("VALIDATION_ERROR", "没有已注册的真实规划或视频适配器")
    return _invoke(name, fields)


def _invoke(name: str, fields: dict[str, str]) -> object:
    spec = REAL_ADAPTERS[name]
    required = tuple(getattr(spec, "required_fields", ()))
    missing = [key for key in required if not str(fields.get(key, "")).strip()]
    if missing:
        raise VideoAgentError("VALIDATION_ERROR", "缺少必填项: " + ",".join(missing), fields=missing)
    build = getattr(spec, "build", None)
    if not callable(build):
        raise VideoAgentError("VALIDATION_ERROR", "没有已注册的真实规划或视频适配器")
    return build(fields)
