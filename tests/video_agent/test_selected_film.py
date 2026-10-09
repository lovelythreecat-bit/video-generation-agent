import itertools
import math
import struct
import subprocess
import wave
from pathlib import Path

import pytest
from PIL import Image

from video_agent.fake_music import FakeMusic
from video_agent.fake_planner import FakePlanner
from video_agent.fake_video import FakeVideo
from video_agent.fake_voice import FakeVoice
from video_agent.models import CreateTaskRequest, ProductInfo, VideoOptions
from video_agent.service import VideoAgent
from video_agent.visual import NoneVisual


def _need_ffmpeg() -> None:
    import shutil

    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("ffmpeg and ffprobe are required")


def _has_font() -> bool:
    font_dir = Path(r"C:\Windows\Fonts")
    return any((font_dir / name).is_file() for name in ("msyh.ttc", "msyh.ttf", "simhei.ttf", "segoeui.ttf", "arial.ttf"))


def _home(tmp_path: Path) -> Path:
    import json

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


def _png(size: tuple[int, int], color: tuple[int, int, int]) -> bytes:
    import io

    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


class CountingVoice:
    def __init__(self) -> None:
        self.calls = 0
        self.inner = FakeVoice()

    def synthesize(self, *, text: str, language: str | None, preference: str):
        self.calls += 1
        return self.inner.synthesize(text=text, language=language, preference=preference)


class CountingMusic:
    def __init__(self) -> None:
        self.calls = 0
        self.inner = FakeMusic()

    def generate(self, *, direction: str, duration_seconds: float) -> bytes:
        self.calls += 1
        return self.inner.generate(direction=direction, duration_seconds=duration_seconds)


def _export(tmp_path: Path, *, aspect: str, size: tuple[int, int], color: tuple[int, int, int], subtitles: bool, voiceover: bool, music: bool):
    _need_ffmpeg()
    if subtitles and not _has_font():
        pytest.skip("no Windows font for subtitles")
    home = _home(tmp_path)
    voice = CountingVoice()
    music_provider = CountingMusic()
    agent = VideoAgent(
        home=home,
        planner=FakePlanner(),
        video=FakeVideo(),
        voice=voice,
        music=music_provider,
        visual=NoneVisual(),
    )
    asset_id = agent.register_asset(filename="p.png", kind="image", data=_png(size, color))
    created = agent.create_task(
        CreateTaskRequest(
            product_info=ProductInfo(name="甲" * 20, selling_points=["耐热"]),
            images=[asset_id],
            first_frame_asset_id=asset_id,
            user_prompt="放在桌上",
            video_options=VideoOptions(
                duration_seconds=5,
                aspect_ratio=aspect,
                subtitles=subtitles,
                voiceover=voiceover,
                music=music,
            ),
        )
    )
    for _ in range(40):
        view = agent.get_task(created.task_id)
        if view.status == "awaiting_confirmation" and view.head_plan_version is not None:
            agent.confirm(created.task_id, view.head_plan_version)
            continue
        if view.status not in {"pending", "planning", "generating"}:
            break
        agent.tick()
    finished = agent.get_task(created.task_id)
    return agent, finished, voice, music_provider


def test_nine_by_sixteen_frame_count(tmp_path: Path):
    from video_agent.media import probe

    agent, finished, voice, music_provider = _export(
        tmp_path,
        aspect="9:16",
        size=(18, 32),
        color=(220, 10, 10),
        subtitles=False,
        voiceover=False,
        music=False,
    )
    assert finished.status == "succeeded"
    artifact = agent.list_artifacts(finished.task_id)[0]
    assert (artifact.width, artifact.height, artifact.aspect_ratio, artifact.duration_seconds) == (180, 320, "9:16", 5)
    assert artifact.quality_status == "not_assessed"
    info = probe(agent.artifact_path(artifact.artifact_id))
    assert info.frame_count == 5 * 30
    assert info.has_audio is False
    assert info.video_codec == "h264"
    assert voice.calls == 0
    assert music_provider.calls == 0


def test_square_pad_keeps_black_bars(tmp_path: Path):
    from video_agent.media import extract_png

    agent, finished, _voice, _music = _export(
        tmp_path,
        aspect="1:1",
        size=(80, 10),
        color=(255, 0, 0),
        subtitles=False,
        voiceover=False,
        music=False,
    )
    assert finished.status == "succeeded"
    plan = agent.get_plan(finished.task_id, finished.confirmed_plan_version)
    assert plan.document.fit == "pad"
    artifact = agent.list_artifacts(finished.task_id)[0]
    assert (artifact.width, artifact.height) == (64, 64)
    png = tmp_path / "frame.png"
    extract_png(agent.artifact_path(artifact.artifact_id), 0, png)
    frame = Image.open(png).convert("RGB")
    assert frame.getpixel((0, 0)) == (0, 0, 0)
    assert frame.getpixel((32, 32)) == (255, 0, 0)


@pytest.mark.parametrize("subtitles,voiceover,music", list(itertools.product([False, True], repeat=3)))
def test_eight_switches(tmp_path: Path, subtitles: bool, voiceover: bool, music: bool):
    from video_agent.media import extract_png, probe

    agent, finished, voice, music_provider = _export(
        tmp_path,
        aspect="9:16",
        size=(18, 32),
        color=(220, 10, 10),
        subtitles=subtitles,
        voiceover=voiceover,
        music=music,
    )
    assert finished.status == "succeeded", finished.failure
    artifact = agent.list_artifacts(finished.task_id)[0]
    info = probe(agent.artifact_path(artifact.artifact_id))
    assert info.frame_count == 150
    assert info.has_audio is (voiceover or music)
    if voiceover or music:
        assert info.audio_codec == "aac"
    assert voice.calls == (1 if voiceover else 0)
    assert music_provider.calls == (1 if music else 0)
    final = agent.artifact_path(artifact.artifact_id)
    if subtitles:
        png = tmp_path / "bottom.png"
        extract_png(final, 10, png)
        frame = Image.open(png).convert("RGB")
        changed = any(
            frame.getpixel((x, y)) != (220, 10, 10)
            for y in range(int(320 * 0.80), int(320 * 0.92))
            for x in range(36, 144)
        )
        assert changed is True
    if voiceover and music:
        quiet = _rms_db(final, 0.6, 0.4, 200)
        loud = _rms_db(final, 3.2, 0.5, 200)
        assert -16 <= quiet - loud <= -8


def _rms_db(path: Path, start: float, duration: float, frequency: int) -> float:
    wav = path.with_name(f"{path.stem}-{start}.wav")
    subprocess.run(
        [
            "ffmpeg", "-y", "-ss", str(start), "-t", str(duration), "-i", str(path),
            "-af", f"bandpass=f={frequency}:width_type=h:width=30",
            "-ac", "1", "-ar", "8000", str(wav),
        ],
        check=True,
        capture_output=True,
        stdin=subprocess.DEVNULL,
    )
    with wave.open(str(wav), "rb") as handle:
        count = handle.getnframes()
        raw = handle.readframes(count)
    samples = struct.unpack("<" + "h" * count, raw)
    mean_square = sum(sample * sample for sample in samples) / count
    return 10 * math.log10(mean_square)
