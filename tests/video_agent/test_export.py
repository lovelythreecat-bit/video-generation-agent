import io
import json
import struct
import wave
from pathlib import Path

import pytest
from PIL import Image

from video_agent.models import AudioOptions, CreateTaskRequest, PlanDocument, ProductInfo, SharedVisual, ShotPlan, VideoOptions
from video_agent.ports import CheckFrame, PlanRequest, PlanResult, ReviseResult, VisualResult, VoiceCue, VoiceResult
from video_agent.service import VideoAgent


def _need_ffmpeg() -> None:
    import shutil

    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("ffmpeg and ffprobe are required")


def _has_font() -> bool:
    font_dir = Path(r"C:\Windows\Fonts")
    return any((font_dir / name).is_file() for name in ("msyh.ttc", "msyh.ttf", "simhei.ttf", "segoeui.ttf", "arial.ttf"))


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


def _png(size: tuple[int, int] = (18, 32), color: tuple[int, int, int] = (220, 10, 10)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


def _wav() -> bytes:
    raw = struct.pack("<h", 1000) * 16000
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(8000)
        handle.writeframes(raw)
    return buffer.getvalue()


def _finish(agent: VideoAgent, task_id: str):
    for _ in range(30):
        view = agent.get_task(task_id)
        if view.status not in {"pending", "planning", "generating"}:
            return view
        agent.tick()
    return agent.get_task(task_id)


def _film(tmp_path: Path, **options):
    _need_ffmpeg()
    home = _home(tmp_path)
    planner = options.pop("planner", None)
    voice = options.pop("voice", None)
    music = options.pop("music", None)
    visual = options.pop("visual", None)
    video = None
    if any(item is not None for item in (planner, voice, music, visual)):
        from video_agent.fake_music import FakeMusic
        from video_agent.fake_planner import FakePlanner
        from video_agent.fake_video import FakeVideo
        from video_agent.fake_voice import FakeVoice
        from video_agent.visual import NoneVisual

        planner = FakePlanner() if planner is None else planner
        video = FakeVideo()
        voice = FakeVoice() if voice is None else voice
        music = FakeMusic() if music is None else music
        visual = NoneVisual() if visual is None else visual
    agent = VideoAgent(home=home, planner=planner, video=video, voice=voice, music=music, visual=visual)
    asset_id = agent.register_asset(filename="p.png", kind="image", data=options.pop("image", _png()))
    music_asset_id = options.pop("music_asset_id", None)
    created = agent.create_task(
        CreateTaskRequest(
            product_info=options.pop("product", ProductInfo(name="杯子", selling_points=["耐热"])),
            images=[asset_id],
            first_frame_asset_id=asset_id,
            user_prompt="放在桌上",
            video_options=options.pop(
                "video_options",
                VideoOptions(duration_seconds=5, subtitles=False, voiceover=False, music=False),
            ),
            audio_options=AudioOptions(music_asset_id=music_asset_id),
        )
    )
    ready = _finish(agent, created.task_id)
    assert ready.head_plan_version is not None
    if options.pop("clear_voice", False):
        agent.voice = None
    if options.pop("clear_music", False):
        agent.music = None
    agent.confirm(created.task_id, ready.head_plan_version)
    finished = _finish(agent, created.task_id)
    return agent, home, finished


class LongVoice:
    def synthesize(self, *, text: str, language: str | None, preference: str) -> VoiceResult:
        del language, preference
        return VoiceResult(audio=b"", cues=[VoiceCue(text=text, start_ms=0, end_ms=100000)], duration_ms=100000)


class CountingMusic:
    def __init__(self) -> None:
        self.calls = 0

    def generate(self, *, direction: str, duration_seconds: float) -> bytes:
        del direction, duration_seconds
        self.calls += 1
        raise AssertionError("music provider must not be called")


class FlagVisual:
    def __init__(self) -> None:
        self.frames: list[CheckFrame] = []

    def check(self, frames: list[CheckFrame], appearance: str) -> VisualResult:
        del appearance
        self.frames = list(frames)
        return VisualResult(
            status="flagged",
            findings=[{"shot_id": frames[0].shot_id, "item": "颜色", "reason": "漂移"}],
            reason="",
        )


class BoomVisual:
    def check(self, frames: list[CheckFrame], appearance: str) -> VisualResult:
        del frames, appearance
        raise RuntimeError("checker down")


class TwoShotPlanner:
    def capabilities_note(self) -> str:
        return "two shots"

    def plan(self, request: PlanRequest) -> PlanResult:
        total = request.duration_seconds * 30
        cut = total // 2
        return PlanResult(
            document=PlanDocument(
                creative="展示杯子",
                audience="商品观众",
                selling_points=list(request.product_info.selling_points),
                shared_visual=SharedVisual(
                    product_reference="红色杯子",
                    scene="桌面",
                    lighting="均匀",
                    palette="红",
                    style="写实",
                ),
                shots=[
                    ShotPlan(shot_id="s1", content="前段", action="静止", shot_size="中景", camera="固定", scene_id="scene-1", start_frame=0, end_frame=cut),
                    ShotPlan(shot_id="s2", content="后段", action="静止", shot_size="中景", camera="固定", scene_id="scene-1", start_frame=cut, end_frame=total),
                ],
                duration_seconds=request.duration_seconds,
                aspect_ratio=request.aspect_ratio,
                fit="native" if request.aspect_ratio == "9:16" else "pad",
                subtitles=request.subtitles,
                voiceover=request.voiceover,
                music=request.music,
                first_frame_source="selected",
                first_frame_asset_id=request.first_frame_asset_id,
                model_capabilities="two shots",
            )
        )

    def revise(self, current: PlanDocument, instruction: str, request: PlanRequest) -> ReviseResult:
        del current, instruction, request
        return ReviseResult()


def test_closed_audio_exports_silent_film_and_still_checks_visuals(tmp_path: Path):
    from video_agent.media import probe

    agent, home, finished = _film(tmp_path)
    assert finished.status == "succeeded"
    assert finished.failure is None
    artifacts = agent.list_artifacts(finished.task_id)
    assert len(artifacts) == 1
    assert artifacts[0].quality_status == "not_assessed"
    assert artifacts[0].quality["reason"] == "visual adapter is none"
    assert (artifacts[0].width, artifacts[0].height) == (180, 320)
    info = probe(agent.artifact_path(artifacts[0].artifact_id))
    assert info.frame_count == 150
    assert info.has_audio is False
    assert info.video_codec == "h264"
    root = home / "tasks" / finished.task_id / "generations" / finished.generation_id / "frames"
    assert (root / "s1-start.png").is_file()
    assert (root / "s1-middle.png").is_file()
    assert (root / "s1-end.png").is_file()
    assert not (root / "s1-boundary.png").is_file()


def test_missing_voice_and_music_are_rejected(tmp_path: Path):
    voice_off = _film(
        tmp_path,
        clear_voice=True,
        video_options=VideoOptions(duration_seconds=5, subtitles=False, voiceover=True, music=False),
    )[2]
    assert voice_off.failure is not None
    assert voice_off.failure.code == "PROVIDER_REJECTED"
    assert voice_off.failure.stage == "preparing_audio"
    assert voice_off.failure.retryable is True
    music_off = _film(
        tmp_path / "music",
        clear_music=True,
        video_options=VideoOptions(duration_seconds=5, subtitles=False, voiceover=False, music=True),
    )[2]
    assert music_off.failure is not None
    assert music_off.failure.code == "PROVIDER_REJECTED"
    assert music_off.failure.stage == "preparing_audio"


def test_registered_music_skips_the_music_provider(tmp_path: Path):
    from video_agent.media import probe

    home = _home(tmp_path)
    agent = VideoAgent(home=home)
    music_id = agent.register_asset(filename="bgm.wav", kind="audio", data=_wav())
    counter = CountingMusic()
    agent.music = counter
    image_id = agent.register_asset(filename="p.png", kind="image", data=_png())
    created = agent.create_task(
        CreateTaskRequest(
            product_info=ProductInfo(name="杯子", selling_points=["耐热"]),
            images=[image_id],
            first_frame_asset_id=image_id,
            user_prompt="放在桌上",
            video_options=VideoOptions(duration_seconds=5, subtitles=False, voiceover=False, music=True),
            audio_options=AudioOptions(music_asset_id=music_id),
        )
    )
    ready = _finish(agent, created.task_id)
    assert ready.head_plan_version is not None
    agent.confirm(created.task_id, ready.head_plan_version)
    finished = _finish(agent, created.task_id)
    assert finished.status == "succeeded"
    assert counter.calls == 0
    info = probe(agent.artifact_path(agent.list_artifacts(created.task_id)[0].artifact_id))
    assert info.has_audio is True
    assert info.audio_codec == "aac"


def test_voiceover_that_cannot_fit_is_not_retryable(tmp_path: Path):
    _need_ffmpeg()
    finished = _film(
        tmp_path,
        voice=LongVoice(),
        video_options=VideoOptions(duration_seconds=5, subtitles=False, voiceover=True, music=False),
    )[2]
    assert finished.failure is not None
    assert finished.failure.code == "VOICEOVER_DOES_NOT_FIT"
    assert finished.failure.retryable is False
    assert finished.failure.stage == "preparing_audio"


def test_flagged_visual_still_exports(tmp_path: Path):
    visual = FlagVisual()
    agent, home, finished = _film(tmp_path, planner=TwoShotPlanner(), visual=visual)
    assert finished.status == "succeeded"
    positions = {frame.position for frame in visual.frames}
    assert positions == {"start", "middle", "end", "boundary"}
    shot_ids = {frame.shot_id for frame in visual.frames}
    assert shot_ids == {"s1", "s2"}
    artifact = agent.list_artifacts(finished.task_id)[0]
    assert artifact.quality_status == "flagged"
    assert artifact.quality["findings"][0]["reason"] == "漂移"
    root = home / "tasks" / finished.task_id / "generations" / finished.generation_id / "frames"
    assert (root / "s2-boundary.png").is_file()


def test_visual_exception_is_not_assessed_and_still_exports(tmp_path: Path):
    agent, _home_path, finished = _film(tmp_path, visual=BoomVisual())
    assert finished.status == "succeeded"
    artifact = agent.list_artifacts(finished.task_id)[0]
    assert artifact.quality_status == "not_assessed"
    assert "checker down" in artifact.quality["reason"]


def test_failed_final_probe_does_not_register_artifact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _need_ffmpeg()
    from video_agent import pipeline as pipeline_module

    real_probe = pipeline_module.probe

    def bad_probe(path: Path):
        info = real_probe(path)
        if path.name.startswith("final"):
            return info.model_copy(update={"frame_count": 1})
        return info

    monkeypatch.setattr(pipeline_module, "probe", bad_probe)
    agent, home, finished = _film(tmp_path)
    assert finished.failure is not None
    assert finished.failure.code == "MEDIA_EXPORT_FAILED"
    assert finished.failure.stage == "exporting"
    assert agent.list_artifacts(finished.task_id) == []
    root = home / "tasks" / finished.task_id / "generations" / finished.generation_id
    assert not (root / "final.mp4").is_file()
    assert not (root / "final.tmp.mp4").is_file()
    assert (root / "composed.mp4").is_file()


def test_subtitles_are_burned_when_voice_and_music_are_off(tmp_path: Path):
    if not _has_font():
        pytest.skip("no Windows font for subtitles")
    agent, _home_path, finished = _film(
        tmp_path,
        video_options=VideoOptions(duration_seconds=5, subtitles=True, voiceover=False, music=False),
    )
    assert finished.status == "succeeded"
    from video_agent.media import extract_png

    final = agent.artifact_path(agent.list_artifacts(finished.task_id)[0].artifact_id)
    png = tmp_path / "bottom.png"
    extract_png(final, 10, png)
    frame = Image.open(png).convert("RGB")
    changed = False
    for y in range(int(320 * 0.80), int(320 * 0.92)):
        for x in range(36, 144):
            if frame.getpixel((x, y)) != (220, 10, 10):
                changed = True
    assert changed is True
