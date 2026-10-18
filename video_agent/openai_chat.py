from __future__ import annotations

from video_agent.errors import VideoAgentError
from video_agent.http_json import request_json

_PROBE_TEXT = "只回复两个字母 OK"
_TINY_PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="


def chat_url(base_url: str) -> str:
    root = base_url.strip().rstrip("/")
    if root.endswith("/chat/completions"):
        return root
    return root + "/chat/completions"


def complete(
    *,
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict],
    timeout: float = 120,
    transport=None,
) -> str:
    payload = request_json(
        "POST",
        chat_url(base_url),
        headers={
            "Authorization": f"Bearer {api_key.strip()}",
            "Content-Type": "application/json",
        },
        body={"model": model.strip(), "messages": messages, "temperature": 0.2},
        timeout=timeout,
        transport=transport,
    )
    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise VideoAgentError("PROVIDER_REJECTED", "文本接口没有返回内容", retryable=True) from exc
    text = _content_text(content).strip()
    if text == "":
        raise VideoAgentError("PROVIDER_REJECTED", "文本接口返回了空内容", retryable=True)
    return text


def probe_text(*, base_url: str, api_key: str, model: str, transport=None) -> str:
    return complete(
        base_url=base_url,
        api_key=api_key,
        model=model,
        messages=[{"role": "user", "content": _PROBE_TEXT}],
        timeout=30,
        transport=transport,
    )


def probe_vision(*, base_url: str, api_key: str, model: str, transport=None) -> str:
    return complete(
        base_url=base_url,
        api_key=api_key,
        model=model,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": _PROBE_TEXT},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{_TINY_PNG}"}},
                ],
            }
        ],
        timeout=45,
        transport=transport,
    )


def _content_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)
    return ""
