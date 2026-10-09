import shutil
import struct
import wave
from pathlib import Path

import pytest
from PIL import Image

from video_agent.errors import VideoAgentError
from video_agent.ports import VoiceCue


def _need_ffmpeg() -> None:
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("ffmpeg and ffprobe are required")


def _png(path: Path, size: tuple[int, int], color: tuple[int, int, int], marks: list[tuple[int, int, tuple[int, int, int]]] | None = None) -> None:
    image = Image.new("RGB", size, color)
    for x, y, pixel in marks or []:
        image.putpixel((x, y), pixel)
    image.save(path)


def test_ffmpeg_available_matches_path():
    from video_agent.media import ffmpeg_available

    expected = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
    assert ffmpeg_available() is expected


def test_fit_image_pads_and_crops_without_stretch(tmp_path: Path):
    from video_agent.media import fit_image

    source = tmp_path / "wide.png"
    _png(source, (40, 10), (255, 0, 0), [(x, y, (0, 0, 255)) for x in range(5) for y in range(10)])
    padded = tmp_path / "pad.png"
    fit_image(source, "pad", 20, 20, padded)
    pad = Image.open(padded)
    assert pad.size == (20, 20)
    assert pad.getpixel((0, 0)) == (0, 0, 0)
    assert pad.getpixel((10, 10)) == (255, 0, 0)
    cropped = tmp_path / "crop.png"
    fit_image(source, "crop", 10, 10, cropped)
    crop = Image.open(cropped)
    assert crop.size == (10, 10)
    assert crop.getpixel((0, 0)) == (255, 0, 0)
    native = tmp_path / "native.png"
    square = tmp_path / "square.png"
    _png(square, (8, 8), (0, 255, 0))
    fit_image(square, "native", 16, 16, native)
    assert Image.open(native).size == (16, 16)
    unequal = tmp_path / "unequal.png"
    fit_image(source, "native", 20, 20, unequal)
    assert Image.open(unequal).getpixel((0, 0)) == (0, 0, 0)


def test_overlapping_fades_keep_the_lower_volume():
    from video_agent.media import _music_gain

    cues = [
        VoiceCue(text="a", start_ms=0, end_ms=1000),
        VoiceCue(text="b", start_ms=1100, end_ms=2000),
    ]
    assert _music_gain(500, cues) == pytest.approx(0.25)
    assert _music_gain(1000, cues) == pytest.approx(0.25)
    assert _music_gain(3000, cues) == pytest.approx(1.0)
    assert _music_gain(900, [VoiceCue(text="a", start_ms=1000, end_ms=2000)]) == pytest.approx(0.625)


def test_duck_wav_uses_quarter_amplitude(tmp_path: Path):
    from video_agent.media import _duck_wav

    source = tmp_path / "music.wav"
    frames = 24000
    with wave.open(str(source), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(8000)
        handle.writeframes(struct.pack("<" + "h" * (frames * 2), *([10000, 10000] * frames)))
    dest = tmp_path / "ducked.wav"
    _duck_wav(source, [VoiceCue(text="a", start_ms=1000, end_ms=2000)], dest)
    with wave.open(str(dest), "rb") as handle:
        raw = handle.readframes(handle.getnframes())
    samples = struct.unpack("<" + "h" * (len(raw) // 2), raw)
    assert samples[100] == 10000
    assert samples[12000 * 2] == 2500
    assert samples[int(0.9 * 8000) * 2] == 6250


def test_still_clip_has_exact_frame_count(tmp_path: Path):
    _need_ffmpeg()
    from video_agent.media import probe, still_clip

    image = tmp_path / "frame.png"
    _png(image, (18, 32), (220, 10, 10))
    dest = tmp_path / "clip.mp4"
    still_clip(image, 15, 18, 32, dest, video_bitrate=500_000)
    info = probe(dest)
    assert info.frame_count == 15
    assert info.width == 18
    assert info.height == 32
    assert info.has_audio is False
    assert info.video_codec == "h264"
    assert info.audio_codec is None


def test_concat_and_extract_keep_frames(tmp_path: Path):
    _need_ffmpeg()
    from video_agent.media import concat_clips, extract_png, probe, still_clip

    image = tmp_path / "frame.png"
    _png(image, (18, 32), (220, 10, 10))
    first = tmp_path / "a.mp4"
    second = tmp_path / "b.mp4"
    still_clip(image, 10, 18, 32, first, video_bitrate=500_000)
    still_clip(image, 5, 18, 32, second, video_bitrate=500_000)
    joined = tmp_path / "joined.mp4"
    concat_clips([first, second], joined)
    info = probe(joined)
    assert info.frame_count == 15
    assert info.has_audio is False
    png = tmp_path / "frame0.png"
    extract_png(joined, 0, png)
    assert Image.open(png).size == (18, 32)


def test_mix_audio_without_inputs_has_no_audio_track(tmp_path: Path):
    _need_ffmpeg()
    from video_agent.media import mix_audio, probe, still_clip

    image = tmp_path / "frame.png"
    _png(image, (18, 32), (220, 10, 10))
    video = tmp_path / "video.mp4"
    still_clip(image, 10, 18, 32, video, video_bitrate=500_000)
    dest = tmp_path / "final.mp4"
    mix_audio(video, None, None, [], dest, video_bitrate=500_000, audio_bitrate=192_000)
    info = probe(dest)
    assert info.has_audio is False
    assert info.frame_count == 10


def test_trim_or_loop_audio_reaches_duration(tmp_path: Path):
    _need_ffmpeg()
    from video_agent.media import trim_or_loop_audio

    source = tmp_path / "short.wav"
    with wave.open(str(source), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(48000)
        handle.writeframes(b"\x00\x00" * 48000)
    dest = tmp_path / "looped.m4a"
    trim_or_loop_audio(source, 1.5, dest, audio_bitrate=192_000)
    wav = tmp_path / "looped.wav"
    import subprocess

    completed = subprocess.run(
        ["ffmpeg", "-y", "-i", str(dest), str(wav)],
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0
    with wave.open(str(wav), "rb") as handle:
        duration = handle.getnframes() / handle.getframerate()
    assert duration == pytest.approx(1.5, abs=0.08)


def test_missing_font_directory_rejects_subtitles(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from video_agent import media

    monkeypatch.setattr(media, "FONT_DIR", tmp_path)
    with pytest.raises(VideoAgentError) as caught:
        media._font_file("耐")
    assert caught.value.code == "MEDIA_EXPORT_FAILED"


def test_burn_subtitles_draws_glyphs(tmp_path: Path):
    _need_ffmpeg()
    from video_agent.media import FONT_DIR, FONT_FILES, burn_subtitles, extract_png

    if not any((FONT_DIR / name).is_file() for _family, names in FONT_FILES for name in names):
        pytest.skip("no Windows font for subtitles")
    from video_agent.media import still_clip

    image = tmp_path / "frame.png"
    _png(image, (180, 320), (220, 10, 10))
    video = tmp_path / "video.mp4"
    still_clip(image, 30, 180, 320, video, video_bitrate=500_000)
    burned = tmp_path / "burned.mp4"
    burn_subtitles(
        video,
        [VoiceCue(text="耐热", start_ms=0, end_ms=1000)],
        180,
        320,
        burned,
        video_bitrate=500_000,
    )
    png = tmp_path / "bottom.png"
    extract_png(burned, 5, png)
    frame = Image.open(png).convert("RGB")
    changed = False
    for y in range(int(320 * 0.80), int(320 * 0.92)):
        for x in range(int(180 * 0.2), int(180 * 0.8)):
            if frame.getpixel((x, y)) != (220, 10, 10):
                changed = True
    assert changed is True


def test_subtitle_wider_than_frame_fails(tmp_path: Path):
    _need_ffmpeg()
    from video_agent.media import FONT_DIR, FONT_FILES, burn_subtitles, still_clip

    if not any((FONT_DIR / name).is_file() for _family, names in FONT_FILES for name in names):
        pytest.skip("no Windows font for subtitles")
    image = tmp_path / "frame.png"
    _png(image, (20, 1000), (220, 10, 10))
    video = tmp_path / "video.mp4"
    still_clip(image, 5, 20, 1000, video, video_bitrate=500_000)
    with pytest.raises(VideoAgentError) as caught:
        burn_subtitles(
            video,
            [VoiceCue(text="耐", start_ms=0, end_ms=100)],
            20,
            1000,
            tmp_path / "out.mp4",
            video_bitrate=500_000,
        )
    assert caught.value.code == "MEDIA_EXPORT_FAILED"
