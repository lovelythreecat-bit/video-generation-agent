from __future__ import annotations

import json
import urllib.error
import urllib.request

from video_agent.errors import VideoAgentError


def request_json(
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    body: dict | None = None,
    timeout: float = 60,
    transport=None,
    uncertain: bool = False,
) -> dict:
    try:
        if transport is not None:
            status, payload = transport(method, url, headers=headers, body=body, timeout=timeout)
        else:
            status, payload = _urllib_json(method, url, headers=headers, body=body, timeout=timeout)
    except VideoAgentError:
        raise
    except Exception as exc:
        _transport_failure(exc, uncertain=uncertain)
    if not isinstance(status, int):
        raise VideoAgentError("PROVIDER_TEMPORARY", "接口没有返回状态码", retryable=True)
    if status >= 400:
        _raise_http(status, payload)
    if not isinstance(payload, dict):
        raise VideoAgentError("PROVIDER_REJECTED", "接口没有返回 JSON 对象", retryable=True)
    return payload


def request_bytes(url: str, *, headers: dict[str, str] | None = None, timeout: float = 120, transport=None) -> bytes:
    try:
        if transport is not None:
            status, payload = transport("GET", url, headers=headers or {}, body=None, timeout=timeout)
        else:
            status, payload = _urllib_bytes(url, headers=headers or {}, timeout=timeout)
    except VideoAgentError:
        raise
    except Exception as exc:
        _transport_failure(exc, uncertain=False)
    if status >= 400:
        raise VideoAgentError("PROVIDER_TEMPORARY", f"下载视频失败（HTTP {status}）", retryable=True)
    if not isinstance(payload, (bytes, bytearray)) or len(payload) == 0:
        raise VideoAgentError("PROVIDER_REJECTED", "视频地址没有返回内容", retryable=True)
    return bytes(payload)


def _urllib_json(method: str, url: str, *, headers: dict[str, str], body: dict | None, timeout: float):
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, _decode_json(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, _decode_json(exc.read())


def _urllib_bytes(url: str, *, headers: dict[str, str], timeout: float):
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        exc.read()
        return exc.code, b""


def _decode_json(raw: bytes):
    if not raw:
        return {}
    text = raw.decode("utf-8", errors="replace")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"message": text[:300]}


def _transport_failure(exc: Exception, *, uncertain: bool) -> None:
    if uncertain:
        raise VideoAgentError(
            "PROVIDER_SUBMISSION_UNCERTAIN",
            "视频任务可能已经提交，但没有收到任务号",
            retryable=True,
        ) from exc
    raise VideoAgentError("PROVIDER_TEMPORARY", f"接口连不上：{exc}", retryable=True) from exc


def _raise_http(status: int, payload: object) -> None:
    message = _error_text(payload)
    if status in {401, 403}:
        raise VideoAgentError("PROVIDER_REJECTED", f"密钥或权限被拒绝：{message}", retryable=True)
    if status == 429 or status >= 500:
        raise VideoAgentError("PROVIDER_TEMPORARY", f"接口暂时失败（HTTP {status}）：{message}", retryable=True)
    raise VideoAgentError("PROVIDER_REJECTED", f"接口拒绝了请求（HTTP {status}）：{message}", retryable=True)


def _error_text(payload: object) -> str:
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            text = error.get("message") or error.get("code") or ""
            if str(text).strip():
                return str(text)[:300]
        message = payload.get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()[:300]
    return "没有更多说明"
