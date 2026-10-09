from __future__ import annotations

import tempfile
from pathlib import Path

from video_agent.media import _run_ffmpeg


class FakeMusic:
    def generate(self, *, direction: str, duration_seconds: float) -> bytes:
        del direction
        with tempfile.TemporaryDirectory() as directory:
            dest = Path(directory) / "music.m4a"
            _run_ffmpeg(
                [
                    "-f", "lavfi",
                    "-i", f"sine=frequency=200:sample_rate=48000:duration={duration_seconds:.3f}",
                    "-ac", "2",
                    "-ar", "48000",
                    "-c:a", "aac",
                    "-b:a", "128000",
                    str(dest),
                ]
            )
            return dest.read_bytes()
