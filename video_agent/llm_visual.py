from __future__ import annotations

import base64
import io
import json

from PIL import Image

from video_agent.errors import VideoAgentError
from video_agent.openai_chat import complete
from video_agent.ports import CheckFrame, VisualResult

_MAX_FRAMES = 12
_SYSTEM = """你检查电商成片里的产品外观是否稳定。只输出一个 JSON 对象，不要 Markdown。
字段：status 只能是 passed 或 flagged；reason 是一句话；findings 是数组，每项含 shot_id、position、issue。
产品外形、颜色、包装文字相对说明发生了明显变化时用 flagged。看不清或无法判断时 status 仍用 flagged，并在 reason 里说明。"""


class OpenAIVisionChecker:
    def __init__(self, *, base_url: str, api_key: str, model: str, transport=None) -> None:
        self.base_url = base_url
        self.api_key = api_key
        self.model = model
        self.transport = transport

    def check(self, frames: list[CheckFrame], appearance: str) -> VisualResult:
        chosen = list(frames[:_MAX_FRAMES])
        if not chosen:
            return VisualResult(status="not_assessed", findings=[], reason="没有可检查的画面")
        content: list[dict] = [
            {
                "type": "text",
                "text": json.dumps(
                    {
                        "appearance": appearance,
                        "frames": [
                            {"shot_id": frame.shot_id, "position": frame.position}
                            for frame in chosen
                        ],
                        "omitted_frames": max(len(frames) - len(chosen), 0),
                    },
                    ensure_ascii=False,
                ),
            }
        ]
        for frame in chosen:
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": _data_url(frame.path)},
                }
            )
        try:
            raw = complete(
                base_url=self.base_url,
                api_key=self.api_key,
                model=self.model,
                messages=[{"role": "system", "content": _SYSTEM}, {"role": "user", "content": content}],
                timeout=120,
                transport=self.transport,
            )
            return _parse_result(raw)
        except VideoAgentError as exc:
            return VisualResult(status="not_assessed", findings=[], reason=exc.message)


def _parse_result(raw: str) -> VisualResult:
    text = raw.strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            return VisualResult(status="not_assessed", findings=[], reason="视觉模型没有返回 JSON")
        try:
            payload = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return VisualResult(status="not_assessed", findings=[], reason="视觉模型返回的 JSON 无法解析")
    if not isinstance(payload, dict):
        return VisualResult(status="not_assessed", findings=[], reason="视觉模型没有返回 JSON 对象")
    status = payload.get("status")
    if status not in {"passed", "flagged"}:
        return VisualResult(status="not_assessed", findings=[], reason="视觉模型没有给出通过或存疑")
    findings = payload.get("findings")
    if not isinstance(findings, list):
        findings = []
    clean = [item for item in findings if isinstance(item, dict)]
    reason = payload.get("reason")
    return VisualResult(
        status=status,
        findings=clean,
        reason=reason.strip() if isinstance(reason, str) else "",
    )


def _data_url(path) -> str:
    image = Image.open(path).convert("RGB")
    image.thumbnail((512, 512))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=70)
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"
