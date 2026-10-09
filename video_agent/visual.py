from __future__ import annotations

from video_agent.ports import CheckFrame, VisualResult


class NoneVisual:
    def check(self, frames: list[CheckFrame], appearance: str) -> VisualResult:
        del frames, appearance
        return VisualResult(status="not_assessed", findings=[], reason="visual adapter is none")
