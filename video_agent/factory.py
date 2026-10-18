from __future__ import annotations

import inspect
from pathlib import Path
from typing import NamedTuple

from video_agent.config import ADAPTER_ROLES, AppConfig
from video_agent.errors import VideoAgentError
from video_agent.ports import MusicProvider, Planner, VideoProvider, VisualChecker, VoiceProvider

_REQUIRED = ("base_url", "api_key", "model")


class AdapterSpec:
    def __init__(self, required_fields: tuple[str, ...], builder, *, roles: tuple[str, ...] = ADAPTER_ROLES) -> None:
        self.required_fields = required_fields
        self._builder = builder
        self.roles = roles

    def build(self, fields: dict[str, str], home=None, job_store=None):
        if "job_store" in inspect.signature(self._builder).parameters:
            return self._builder(fields, home, job_store=job_store)
        return self._builder(fields, home)


def _build_planner(fields: dict[str, str], home: Path | None = None):
    del home
    from video_agent.llm_planner import OpenAIChatPlanner

    return OpenAIChatPlanner(base_url=fields["base_url"], api_key=fields["api_key"], model=fields["model"])


def _build_video(fields: dict[str, str], home: Path | None = None, *, job_store=None):
    from video_agent.seedance_video import SeedanceVideo

    state = None if home is None or job_store is not None else home / "provider-state" / "seedance-tasks.json"
    extra = {} if job_store is None else {"job_store": job_store}
    return SeedanceVideo(
        base_url=fields["base_url"],
        api_key=fields["api_key"],
        model=fields["model"],
        state_path=state,
        **extra,
    )


def _build_wan(fields: dict[str, str], home: Path | None = None, *, job_store=None):
    from video_agent.wan_video import WanVideo

    state = None if home is None or job_store is not None else home / "provider-state" / "wan-tasks.json"
    extra = {} if job_store is None else {"job_store": job_store}
    return WanVideo(
        base_url=fields["base_url"],
        api_key=fields["api_key"],
        model=fields["model"],
        max_segment_seconds=fields["max_segment_seconds"],
        state_path=state,
        **extra,
    )


def _build_visual(fields: dict[str, str], home: Path | None = None):
    del home
    from video_agent.llm_visual import OpenAIVisionChecker

    return OpenAIVisionChecker(base_url=fields["base_url"], api_key=fields["api_key"], model=fields["model"])


REAL_ADAPTERS: dict[str, object] = {
    "openai_chat": AdapterSpec(_REQUIRED, _build_planner, roles=("planner",)),
    "seedance": AdapterSpec(_REQUIRED, _build_video, roles=("video",)),
    "wan": AdapterSpec(
        ("base_url", "api_key", "model", "max_segment_seconds"),
        _build_wan,
        roles=("video",),
    ),
    "openai_vision": AdapterSpec(_REQUIRED, _build_visual, roles=("visual",)),
}


def build_adapters(
    config: AppConfig,
    home: Path | None = None,
    *, job_store=None,
) -> tuple[Planner, VideoProvider, VoiceProvider | None, MusicProvider | None, VisualChecker | None]:
    if config.mode == "fake":
        from video_agent.fake_music import FakeMusic
        from video_agent.fake_planner import FakePlanner
        from video_agent.fake_video import FakeVideo
        from video_agent.fake_voice import FakeVoice
        from video_agent.visual import NoneVisual

        video = FakeVideo()
        if job_store is not None:
            from video_agent.provider_jobs import DurableFakeVideo
            video = DurableFakeVideo(job_store)
        return FakePlanner(), video, FakeVoice(), FakeMusic(), NoneVisual()

    for role in ADAPTER_ROLES:
        name = config.adapters[role].adapter
        if name in REAL_ADAPTERS and name not in {"fake", "none"} and role not in adapter_roles(name):
            raise VideoAgentError("VALIDATION_ERROR", "适配器与模型角色不兼容")

    planner = _build_required(config.adapters["planner"].adapter, config.adapters["planner"].field_map(), home, job_store)
    video = _build_required(config.adapters["video"].adapter, config.adapters["video"].field_map(), home, job_store)
    voice = _build_optional(
        config.adapters["voice"].adapter, config.adapters["voice"].field_map(), _fake_voice, None, home, job_store
    )
    music = _build_optional(
        config.adapters["music"].adapter, config.adapters["music"].field_map(), _fake_music, None, home, job_store
    )
    visual = _build_optional(
        config.adapters["visual"].adapter,
        config.adapters["visual"].field_map(),
        _none_visual,
        _none_visual(),
        home, job_store,
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


def _build_required(name: str, fields: dict[str, str], home: Path | None, job_store=None) -> object:
    if name in {"fake", "none"} or name not in REAL_ADAPTERS:
        raise VideoAgentError("VALIDATION_ERROR", "没有已注册的真实规划或视频适配器")
    return _invoke(name, fields, home, job_store)


def _build_optional(name: str, fields: dict[str, str], fake_factory, none_value, home: Path | None, job_store=None):
    if name == "none":
        return none_value
    if name == "fake":
        return fake_factory()
    if name not in REAL_ADAPTERS:
        raise VideoAgentError("VALIDATION_ERROR", "没有已注册的真实规划或视频适配器")
    return _invoke(name, fields, home, job_store)


def _invoke(name: str, fields: dict[str, str], home: Path | None, job_store=None) -> object:
    spec = REAL_ADAPTERS[name]
    required = tuple(getattr(spec, "required_fields", ()))
    missing = [key for key in required if not str(fields.get(key, "")).strip()]
    if missing:
        raise VideoAgentError("VALIDATION_ERROR", "缺少必填项: " + ",".join(missing), fields=missing)
    build = getattr(spec, "build", None)
    if not callable(build):
        raise VideoAgentError("VALIDATION_ERROR", "没有已注册的真实规划或视频适配器")
    parameters = inspect.signature(build).parameters
    kwargs = {}
    if "home" in parameters:
        kwargs["home"] = home
    if "job_store" in parameters:
        kwargs["job_store"] = job_store
    return build(fields, **kwargs)


def adapter_roles(name: str) -> tuple[str, ...]:
    spec = REAL_ADAPTERS.get(name)
    if spec is None or name in {"fake", "none"}:
        return ()
    return tuple(getattr(spec, "roles", ADAPTER_ROLES))


def adapter_type_metadata() -> list[dict]:
    video_capabilities = {
        'seedance': {'native_aspects': ['9:16', '16:9', '1:1', '4:3', '3:4', '21:9'],
            'max_segment_seconds': 15, 'supports_first_frame': True, 'supports_reference_images': False,
            'supports_tail_frame': True, 'audio': 'generate_audio request flag'},
        'wan': {'native_aspects': ['9:16', '16:9', '1:1', '4:5', '3:4', '4:3', '3:2', '2:3'],
            'max_segment_seconds': 'configured per model (1-15)', 'supports_first_frame': True,
            'supports_reference_images': False, 'supports_tail_frame': False,
            'audio': 'wan3.0/wan2.5/wan2.6 request flag; wan2.2/wanx2.1 unavailable'},
    }
    return [
        {"adapter": name, "roles": list(adapter_roles(name)),
         "required_fields": list(getattr(spec, "required_fields", ())),
         "secret_fields": [field for field in getattr(spec, "required_fields", ()) if field == "api_key"],
         "parameters": list(getattr(spec, "required_fields", ())),
         'capabilities': video_capabilities.get(name, {}) }
        for name, spec in sorted(REAL_ADAPTERS.items())
    ]


class AdapterBundle(NamedTuple):
    planner: Planner
    video: VideoProvider
    voice: VoiceProvider | None
    music: MusicProvider | None
    visual: VisualChecker | None


class AdapterResolver:
    def __init__(self, codec, *, job_store=None) -> None:
        self.codec = codec
        self.job_store = job_store

    def for_snapshot(self, snapshot: dict, home: Path) -> AdapterBundle:
        config = self.codec.decode(snapshot)
        return AdapterBundle(*build_adapters(config, home, job_store=self.job_store))
