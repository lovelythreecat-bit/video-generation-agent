from __future__ import annotations

import tempfile
from pathlib import Path

from video_agent.media import _run_ffmpeg
from video_agent.ports import VoiceCue, VoiceResult


class FakeVoice:
    def synthesize(self, *, text: str, language: str | None, preference: str) -> VoiceResult:
        del language, preference
        count = len("".join(text.split()))
        duration_ms = max(400, count * 100)
        seconds = duration_ms / 1000
        with tempfile.TemporaryDirectory() as directory:
            dest = Path(directory) / "voice.m4a"
            _run_ffmpeg(
                [
                    "-f", "lavfi",
                    "-i", f"sine=frequency=1000:sample_rate=48000:duration={seconds:.3f}",
                    "-ac", "2",
                    "-c:a", "aac",
                    "-b:a", "128000",
                    str(dest),
                ]
            )
            data = dest.read_bytes()
        return VoiceResult(
            audio=data,
            cues=[VoiceCue(text=text, start_ms=0, end_ms=duration_ms)],
            duration_ms=duration_ms,
        )
