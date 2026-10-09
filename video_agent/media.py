from __future__ import annotations

import json
import math
import shutil
import struct
import subprocess
import wave
from pathlib import Path
from typing import Literal

from PIL import Image, ImageFont
from pydantic import BaseModel

from video_agent.errors import VideoAgentError
from video_agent.ports import VoiceCue

FONT_DIR = Path(r"C:\Windows\Fonts")
FONT_FILES = (
    ("Microsoft YaHei", ("msyh.ttc", "msyh.ttf")),
    ("SimHei", ("simhei.ttf",)),
    ("Segoe UI", ("segoeui.ttf",)),
    ("Arial", ("arial.ttf",)),
)


class ProbeInfo(BaseModel):
    frame_count: int
    width: int
    height: int
    has_audio: bool
    video_codec: str
    audio_codec: str | None = None


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def still_clip(image: Path, frames: int, width: int, height: int, dest: Path, *, video_bitrate: int) -> None:
    if frames < 1:
        raise VideoAgentError("MEDIA_EXPORT_FAILED", "镜头帧数必须大于 0", retryable=True)
    dest.parent.mkdir(parents=True, exist_ok=True)
    _run_ffmpeg(
        [
            "-framerate", "30",
            "-loop", "1",
            "-i", str(image),
            "-frames:v", str(frames),
            "-r", "30",
            "-vf", f"scale={width}:{height}",
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            "-preset", "ultrafast",
            "-b:v", str(video_bitrate),
            "-an",
            str(dest),
        ]
    )


def fit_image(image: Path, fit: Literal["native", "pad", "crop"], width: int, height: int, dest: Path) -> None:
    source = Image.open(image).convert("RGB")
    src_w, src_h = source.size
    if src_w <= 0 or src_h <= 0 or width <= 0 or height <= 0:
        raise VideoAgentError("MEDIA_EXPORT_FAILED", "图片尺寸无效", retryable=True)
    mode = fit
    if fit == "native":
        mode = "native" if src_w * height == src_h * width else "pad"
    dest.parent.mkdir(parents=True, exist_ok=True)
    if mode == "native":
        source.resize((width, height), Image.Resampling.LANCZOS).save(dest, format="PNG")
        return
    if mode == "crop":
        scale = max(width / src_w, height / src_h)
        new_w = max(width, math.ceil(src_w * scale - 1e-9))
        new_h = max(height, math.ceil(src_h * scale - 1e-9))
        resized = source.resize((new_w, new_h), Image.Resampling.LANCZOS)
        left = max(0, (new_w - width) // 2)
        top = max(0, (new_h - height) // 2)
        resized.crop((left, top, left + width, top + height)).save(dest, format="PNG")
        return
    scale = min(width / src_w, height / src_h)
    new_w = min(width, max(1, math.floor(src_w * scale)))
    new_h = min(height, max(1, math.floor(src_h * scale)))
    canvas = Image.new("RGB", (width, height), (0, 0, 0))
    canvas.paste(source.resize((new_w, new_h), Image.Resampling.LANCZOS), ((width - new_w) // 2, (height - new_h) // 2))
    canvas.save(dest, format="PNG")


def concat_clips(clips: list[Path], dest: Path) -> None:
    if not clips:
        raise VideoAgentError("MEDIA_EXPORT_FAILED", "没有可拼接的镜头", retryable=True)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if len(clips) == 1:
        shutil.copyfile(clips[0], dest)
        return
    listing = dest.with_suffix(".concat.txt")
    listing.write_text("\n".join(f"file '{clip.resolve().as_posix()}'" for clip in clips), encoding="utf-8")
    _run_ffmpeg(
        [
            "-f", "concat",
            "-safe", "0",
            "-i", str(listing),
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            "-preset", "ultrafast",
            "-fps_mode", "passthrough",
            "-an",
            str(dest),
        ]
    )


def trim_or_loop_audio(src: Path, duration_seconds: float, dest: Path, *, audio_bitrate: int) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    _run_ffmpeg(
        [
            "-stream_loop", "-1",
            "-i", str(src),
            "-t", f"{duration_seconds:.3f}",
            "-c:a", "aac",
            "-ar", "48000",
            "-ac", "2",
            "-b:a", str(audio_bitrate),
            str(dest),
        ]
    )


def burn_subtitles(
    video: Path,
    cues: list[VoiceCue],
    width: int,
    height: int,
    dest: Path,
    *,
    video_bitrate: int,
) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    visible = [cue for cue in cues if cue.text.strip()]
    if not visible:
        _encode_video(video, dest, video_bitrate)
        return
    family, _path = _font_file("".join(cue.text for cue in visible))
    events = []
    font_size = max(1, round(height * 0.04))
    margin_v = round(height * 0.08)
    font = ImageFont.truetype(str(_path), font_size)
    max_width = width * 0.84
    ascent, descent = font.getmetrics()
    line_height = max(1, ascent + descent)
    for cue in visible:
        lines = _wrap_line(cue.text, font, max_width)
        if line_height * len(lines) > height - margin_v:
            raise VideoAgentError("MEDIA_EXPORT_FAILED", "换行后的字幕放不进画面", stage="exporting", retryable=True)
        events.append(
            f"Dialogue: 0,{_ass_time(cue.start_ms)},{_ass_time(cue.end_ms)},Default,,0,0,0,,{_ass_text(lines)}"
        )
    script = dest.with_suffix(".ass")
    script.write_text(_ass_document(width, height, family, font_size, margin_v, events), encoding="utf-8")
    subtitle_path = _filter_path(script)
    font_dir = _filter_path(FONT_DIR)
    _run_ffmpeg(
        [
            "-i", str(video),
            "-vf", f"subtitles='{subtitle_path}':fontsdir='{font_dir}'",
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            "-preset", "ultrafast",
            "-r", "30",
            "-b:v", str(video_bitrate),
            "-an",
            str(dest),
        ]
    )


def mix_audio(
    video: Path,
    voice: Path | None,
    music: Path | None,
    cues: list[VoiceCue],
    dest: Path,
    *,
    video_bitrate: int,
    audio_bitrate: int,
) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if voice is None and music is None:
        _encode_video(video, dest, video_bitrate)
        return
    if music is not None and voice is not None:
        wav = dest.with_suffix(".music.wav")
        ducked = dest.with_suffix(".ducked.wav")
        ducked_audio = dest.with_suffix(".ducked.m4a")
        _run_ffmpeg(["-i", str(music), "-ac", "2", "-ar", "48000", str(wav)])
        _duck_wav(wav, cues, ducked)
        _encode_audio(ducked, ducked_audio, audio_bitrate)
        _run_ffmpeg(
            [
                "-i", str(video),
                "-i", str(voice),
                "-i", str(ducked_audio),
                "-filter_complex", "[1:a][2:a]amix=inputs=2:duration=longest:normalize=0:dropout_transition=0[a]",
                "-map", "0:v:0",
                "-map", "[a]",
                *_video_args(video_bitrate),
                *_audio_args(audio_bitrate),
                str(dest),
            ]
        )
        return
    audio = voice if voice is not None else music
    _run_ffmpeg(
        [
            "-i", str(video),
            "-i", str(audio),
            "-map", "0:v:0",
            "-map", "1:a:0",
            *_video_args(video_bitrate),
            *_audio_args(audio_bitrate),
            str(dest),
        ]
    )


def extract_png(video: Path, frame_index: int, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    _run_ffmpeg(["-i", str(video), "-vf", f"select=eq(n\\,{frame_index})", "-vframes", "1", str(dest)])


def probe(path: Path) -> ProbeInfo:
    video, audio = _streams(path)
    if video is None or video.get("nb_read_frames") in (None, "N/A"):
        raise VideoAgentError("MEDIA_EXPORT_FAILED", "ffprobe 没有读到视频帧", retryable=True)
    return ProbeInfo(
        frame_count=int(video["nb_read_frames"]),
        width=int(video["width"]),
        height=int(video["height"]),
        has_audio=audio is not None,
        video_codec=str(video.get("codec_name") or ""),
        audio_codec=None if audio is None else str(audio.get("codec_name") or ""),
    )


def _pixel_format(path: Path) -> str:
    video, _audio = _streams(path)
    return "" if video is None else str(video.get("pix_fmt") or "")


def _frame_rate(path: Path) -> float:
    video, _audio = _streams(path)
    if video is None:
        return 0.0
    text = str(video.get("avg_frame_rate") or "0/1")
    num, den = text.split("/")
    if float(den) == 0:
        return 0.0
    return float(num) / float(den)


def _music_gain(ms: float, cues: list[VoiceCue]) -> float:
    gain = 1.0
    for cue in cues:
        pre = cue.start_ms - 200
        post = cue.end_ms + 200
        if ms < pre or ms > post:
            continue
        if ms < cue.start_ms:
            local = 1.0 - 0.75 * (ms - pre) / 200.0
        elif ms <= cue.end_ms:
            local = 0.25
        else:
            local = 0.25 + 0.75 * (ms - cue.end_ms) / 200.0
        if local < gain:
            gain = local
    return gain


def _duck_wav(src: Path, cues: list[VoiceCue], dest: Path) -> None:
    with wave.open(str(src), "rb") as handle:
        channels = handle.getnchannels()
        rate = handle.getframerate()
        width = handle.getsampwidth()
        frames = handle.getnframes()
        raw = handle.readframes(frames)
    if width != 2 or channels < 1:
        raise VideoAgentError("MEDIA_EXPORT_FAILED", "音乐必须是 16 位 PCM", retryable=True)
    samples = struct.unpack("<" + "h" * (len(raw) // 2), raw)
    out = bytearray()
    for index in range(frames):
        gain = _music_gain(index * 1000.0 / rate, cues)
        base = index * channels
        for channel in range(channels):
            value = int(samples[base + channel] * gain)
            value = max(-32768, min(32767, value))
            out += struct.pack("<h", value)
    with wave.open(str(dest), "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(bytes(out))


def _silence(duration_seconds: float, dest: Path, audio_bitrate: int) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    _run_ffmpeg(
        [
            "-f", "lavfi",
            "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
            "-t", f"{duration_seconds:.3f}",
            "-c:a", "aac",
            "-ar", "48000",
            "-ac", "2",
            "-b:a", str(audio_bitrate),
            str(dest),
        ]
    )


def _atempo_pad(src: Path, speed: float, window_seconds: float, dest: Path, audio_bitrate: int) -> None:
    if speed < 0.5 or speed > 2.0:
        raise VideoAgentError("VOICEOVER_DOES_NOT_FIT", "配音文案放不进已确认时长", stage="preparing_audio", retryable=False)
    filters = []
    if abs(speed - 1.0) > 0.001:
        filters.append(f"atempo={speed:.6f}")
    filters.append(f"apad=whole_dur={window_seconds:.3f}")
    filters.append(f"atrim=0:{window_seconds:.3f}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    _run_ffmpeg(
        [
            "-i", str(src),
            "-af", ",".join(filters),
            "-c:a", "aac",
            "-ar", "48000",
            "-ac", "2",
            "-b:a", str(audio_bitrate),
            str(dest),
        ]
    )


def _concat_audio(clips: list[Path], dest: Path, audio_bitrate: int) -> None:
    if not clips:
        raise VideoAgentError("MEDIA_EXPORT_FAILED", "没有可拼接的配音", stage="preparing_audio", retryable=True)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if len(clips) == 1:
        shutil.copyfile(clips[0], dest)
        return
    listing = dest.with_suffix(".concat.txt")
    listing.write_text("\n".join(f"file '{clip.resolve().as_posix()}'" for clip in clips), encoding="utf-8")
    _run_ffmpeg(
        [
            "-f", "concat",
            "-safe", "0",
            "-i", str(listing),
            "-c:a", "aac",
            "-ar", "48000",
            "-ac", "2",
            "-b:a", str(audio_bitrate),
            str(dest),
        ]
    )


def _font_file(text: str) -> tuple[str, Path]:
    needed = {ord(ch) for ch in text if not ch.isspace()}
    for family, names in FONT_FILES:
        for name in names:
            path = FONT_DIR / name
            if path.is_file() and _font_covers(path, needed):
                return family, path
    raise VideoAgentError("MEDIA_EXPORT_FAILED", "系统里没有能显示该文案的字体", stage="exporting", retryable=True)


def _font_covers(path: Path, needed: set[int]) -> bool:
    if not needed:
        return True
    data = path.read_bytes()
    return any(_font_covers_at(data, offset, needed) for offset in _font_offsets(data))


def _font_offsets(data: bytes) -> list[int]:
    if data[:4] == b"ttcf":
        count = struct.unpack_from(">I", data, 8)[0]
        return list(struct.unpack_from(f">{count}I", data, 12))
    return [0]


def _font_covers_at(data: bytes, font_offset: int, needed: set[int]) -> bool:
    num_tables = struct.unpack_from(">H", data, font_offset + 4)[0]
    cmap_offset = None
    cursor = font_offset + 12
    for _index in range(num_tables):
        tag, _checksum, offset, _length = struct.unpack_from(">4sIII", data, cursor)
        cursor += 16
        if tag == b"cmap":
            cmap_offset = offset
            break
    if cmap_offset is None:
        return False
    count = struct.unpack_from(">H", data, cmap_offset + 2)[0]
    for index in range(count):
        _platform, _encoding, sub_offset = struct.unpack_from(">HHI", data, cmap_offset + 4 + index * 8)
        absolute = cmap_offset + sub_offset
        fmt = struct.unpack_from(">H", data, absolute)[0]
        if fmt == 4 and _format4_covers(data, absolute, needed):
            return True
        if fmt == 12 and _format12_covers(data, absolute, needed):
            return True
    return False


def _format4_covers(data: bytes, offset: int, needed: set[int]) -> bool:
    seg_count = struct.unpack_from(">H", data, offset + 6)[0] // 2
    end_at = offset + 14
    ends = struct.unpack_from(f">{seg_count}H", data, end_at)
    start_at = end_at + seg_count * 2 + 2
    starts = struct.unpack_from(f">{seg_count}H", data, start_at)
    delta_at = start_at + seg_count * 2
    deltas = struct.unpack_from(f">{seg_count}h", data, delta_at)
    range_at = delta_at + seg_count * 2
    range_offsets = struct.unpack_from(f">{seg_count}H", data, range_at)
    for code in needed:
        if code > 0xFFFF or not _format4_has(data, code, ends, starts, deltas, range_offsets, range_at):
            return False
    return True


def _format4_has(data: bytes, code: int, ends, starts, deltas, range_offsets, range_at: int) -> bool:
    for index, (start, end) in enumerate(zip(starts, ends)):
        if code < start or code > end:
            continue
        if range_offsets[index] == 0:
            return ((code + deltas[index]) & 0xFFFF) != 0
        position = range_at + index * 2 + range_offsets[index] + (code - start) * 2
        glyph = struct.unpack_from(">H", data, position)[0]
        if glyph == 0:
            return False
        return ((glyph + deltas[index]) & 0xFFFF) != 0
    return False


def _format12_covers(data: bytes, offset: int, needed: set[int]) -> bool:
    groups = struct.unpack_from(">I", data, offset + 12)[0]
    cursor = offset + 16
    spans = []
    for _index in range(groups):
        start, end, glyph = struct.unpack_from(">III", data, cursor)
        cursor += 12
        spans.append((start, end, glyph))
    for code in needed:
        if not any(start <= code <= end and glyph + (code - start) != 0 for start, end, glyph in spans):
            return False
    return True


def _wrap_line(text: str, font: ImageFont.FreeTypeFont, max_width: float) -> list[str]:
    lines: list[str] = []
    current = ""
    for char in text.replace("\n", ""):
        if font.getlength(char) > max_width:
            raise VideoAgentError("MEDIA_EXPORT_FAILED", "换行后的字幕放不进画面", stage="exporting", retryable=True)
        trial = current + char
        if current and font.getlength(trial) > max_width:
            lines.append(current)
            current = char
        else:
            current = trial
    if current:
        lines.append(current)
    return lines or [""]


def _ass_document(width: int, height: int, family: str, font_size: int, margin_v: int, events: list[str]) -> str:
    header = "\n".join(
        [
            "[Script Info]",
            "ScriptType: v4.00+",
            f"PlayResX: {width}",
            f"PlayResY: {height}",
            "WrapStyle: 2",
            "",
            "[V4+ Styles]",
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
            f"Style: Default,{family},{font_size},&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,0,2,10,10,{margin_v},1",
            "",
            "[Events]",
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
            *events,
            "",
        ]
    )
    return header


def _ass_time(ms: int) -> str:
    centi = max(0, ms) // 10
    seconds, centi = divmod(centi, 100)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}.{centi:02d}"


def _ass_text(lines: list[str]) -> str:
    escaped = [line.replace("\\", r"\\").replace("{", r"\{").replace("}", r"\}") for line in lines]
    return r"\N".join(escaped)


def _filter_path(path: Path) -> str:
    text = path.resolve().as_posix()
    if len(text) >= 2 and text[1] == ":":
        text = text[0] + r"\:" + text[2:]
    return text.replace("'", r"\'")


def _encode_video(src: Path, dest: Path, video_bitrate: int) -> None:
    _run_ffmpeg(["-i", str(src), *_video_args(video_bitrate), "-an", str(dest)])


def _encode_audio(src: Path, dest: Path, audio_bitrate: int) -> None:
    _run_ffmpeg(["-i", str(src), *_audio_args(audio_bitrate), str(dest)])


def _video_args(video_bitrate: int) -> list[str]:
    return ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "ultrafast", "-r", "30", "-b:v", str(video_bitrate)]


def _audio_args(audio_bitrate: int) -> list[str]:
    return ["-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", str(audio_bitrate)]


def _streams(path: Path) -> tuple[dict | None, dict | None]:
    if shutil.which("ffprobe") is None:
        raise VideoAgentError("MEDIA_EXPORT_FAILED", "找不到 ffprobe", retryable=True)
    completed = subprocess.run(
        [
            "ffprobe", "-v", "error", "-count_frames", "-print_format", "json",
            "-show_streams",
            "-show_entries", "stream=codec_type,codec_name,width,height,nb_read_frames,pix_fmt,avg_frame_rate",
            str(path),
        ],
        capture_output=True,
        check=False,
        stdin=subprocess.DEVNULL,
    )
    if completed.returncode != 0:
        raise VideoAgentError("MEDIA_EXPORT_FAILED", "ffprobe 无法解码", retryable=True)
    payload = json.loads(completed.stdout.decode("utf-8"))
    video = None
    audio = None
    for stream in payload.get("streams", []):
        if stream.get("codec_type") == "video" and video is None:
            video = stream
        elif stream.get("codec_type") == "audio" and audio is None:
            audio = stream
    return video, audio


def _run_ffmpeg(args: list[str]) -> None:
    if shutil.which("ffmpeg") is None:
        raise VideoAgentError("MEDIA_EXPORT_FAILED", "找不到 ffmpeg", retryable=True)
    completed = subprocess.run(
        ["ffmpeg", "-y", *args],
        capture_output=True,
        check=False,
        stdin=subprocess.DEVNULL,
    )
    if completed.returncode != 0:
        tail = completed.stderr.decode("utf-8", errors="replace")[-500:]
        raise VideoAgentError("MEDIA_EXPORT_FAILED", f"ffmpeg 失败: {tail}", retryable=True)
