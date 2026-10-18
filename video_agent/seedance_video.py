from __future__ import annotations

import base64
import io
import json
from pathlib import Path

from PIL import Image

from video_agent.errors import VideoAgentError
from video_agent.provider_jobs import DurableSubmission
from video_agent.http_json import request_bytes, request_json
from video_agent.ports import QueryResult, ShotRequest, SubmitResult, VideoCapabilities

_NATIVE = ("9:16", "16:9", "1:1", "4:3", "3:4", "21:9")


class SeedanceVideo(DurableSubmission):
    adapter_name = 'seedance'
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        state_path: Path | None = None,
        job_store=None,
        transport=None,
        byte_transport=None,
    ) -> None:
        self.base_url = base_url.strip().rstrip("/")
        self.api_key = api_key.strip()
        self.model = model.strip()
        self.job_store = job_store
        self.state_path = None if job_store is not None else state_path
        self.transport = transport
        self.byte_transport = byte_transport
        self._ids = _load_ids(self.state_path)

    def capabilities(self) -> VideoCapabilities:
        return VideoCapabilities(
            native_aspects=list(_NATIVE),
            max_segment_seconds=15,
            supports_first_frame=True,
            supports_reference_images=False,
            supports_tail_frame=True,
        )

    def submit_shot(self, request: ShotRequest) -> SubmitResult:
        known = self._ids.get(request.provider_idempotency_key)
        if known:
            return SubmitResult(external_task_id=known, uncertain=False)
        image = request.first_frame_path
        if image is None or not image.is_file():
            raise VideoAgentError("PROVIDER_REJECTED", "视频生成需要首帧图片", retryable=True)
        durable_id = self._durable_before(request)
        if durable_id:
            return SubmitResult(external_task_id=durable_id, uncertain=False)
        content = [
            {"type": "text", "text": request.prompt},
            {"type": "image_url", "image_url": {"url": _jpeg_data_url(image)}, "role": "first_frame"},
        ]
        if request.tail_frame_path is not None and request.tail_frame_path.is_file():
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": _jpeg_data_url(request.tail_frame_path)},
                    "role": "last_frame",
                }
            )
        seconds = max(1, int(round(request.frame_count / 30)))
        try:
            payload = request_json(
                "POST",
                _tasks_url(self.base_url),
                headers=_headers(self.api_key),
                body={
                    "model": self.model,
                    "content": content,
                    "ratio": request.aspect_ratio if request.aspect_ratio in _NATIVE else "adaptive",
                    "duration": seconds,
                    "resolution": "720p",
                    "watermark": False,
                    "generate_audio": request.with_audio,
                },
                timeout=60,
                transport=self.transport,
                uncertain=True,
            )
        except Exception as exc:
            if isinstance(exc, VideoAgentError) and exc.code == "PROVIDER_REJECTED":
                self._durable_absent(request.provider_idempotency_key)
            else:
                self._durable_uncertain(request.provider_idempotency_key)
            raise
        external_id = _task_id(payload)
        if external_id is None:
            self._durable_uncertain(request.provider_idempotency_key)
            raise VideoAgentError(
                "PROVIDER_SUBMISSION_UNCERTAIN",
                "视频接口没有返回任务号",
                retryable=True,
            )
        self._durable_accepted(request.provider_idempotency_key, external_id)
        self._remember(request.provider_idempotency_key, external_id)
        return SubmitResult(external_task_id=external_id, uncertain=False)

    def query(self, *, external_task_id: str | None, provider_idempotency_key: str) -> QueryResult:
        job = self._durable_lookup(provider_idempotency_key)
        if job is not None:
            external_task_id = external_task_id or job.external_task_id
            if not external_task_id and job.submission_state in {"intended", "uncertain", "accepted"}:
                return QueryResult(state="pending", message="外部提交结果等待确认")
        task_id = external_task_id or self._ids.get(provider_idempotency_key)
        if not task_id:
            return QueryResult(state="absent", message="还没有这个视频任务")
        try:
            payload = request_json(
                "GET",
                _tasks_url(self.base_url) + "/" + task_id,
                headers=_headers(self.api_key),
                timeout=30,
                transport=self.transport,
            )
        except VideoAgentError as exc:
            if "HTTP 404" in exc.message:
                self._durable_absent(provider_idempotency_key)
                return QueryResult(state="absent", message="视频任务不存在")
            raise
        task = _task_body(payload)
        status = str(task.get("status") or "").lower()
        if status in {"queued", "running", "pending", "submitted", "processing"}:
            return QueryResult(state="pending", message=status)
        if status in {"succeeded", "success", "completed"}:
            self._remember(provider_idempotency_key, task_id)
            return QueryResult(state="succeeded")
        if status in {"failed", "cancelled", "canceled", "expired"}:
            return QueryResult(state="failed", error_code="PROVIDER_REJECTED", message=_failure_text(task))
        if status in {"", "absent", "not_found"}:
            self._durable_absent(provider_idempotency_key)
            return QueryResult(state="absent", message="视频任务不存在")
        return QueryResult(state="pending", message=status)

    def download(self, external_task_id: str) -> bytes:
        task_id = self._ids.get(external_task_id, external_task_id)
        if self.job_store is not None:
            job = self.job_store.get(self.adapter_name, external_task_id)
            if job is not None and job.external_task_id:
                task_id = job.external_task_id
        payload = request_json(
            "GET",
            _tasks_url(self.base_url) + "/" + task_id,
            headers=_headers(self.api_key),
            timeout=30,
            transport=self.transport,
        )
        url = _video_url(_task_body(payload))
        if url is None:
            raise VideoAgentError("PROVIDER_TEMPORARY", "视频还没有下载地址", retryable=True)
        return request_bytes(url, timeout=120, transport=self.byte_transport)

    def _remember(self, key: str, external_id: str) -> None:
        if not key or not external_id:
            return
        self._ids[key] = external_id
        if self.job_store is not None or self.state_path is None:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self._ids, ensure_ascii=False), encoding="utf-8")
        temporary.replace(self.state_path)


def _tasks_url(base_url: str) -> str:
    suffix = "/contents/generations/tasks"
    if base_url.endswith(suffix):
        return base_url
    return base_url + suffix


def _headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}


def _load_ids(path: Path | None) -> dict[str, str]:
    if path is None or not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(key): str(value) for key, value in raw.items() if str(key) and str(value)}


def _task_id(payload: dict) -> str | None:
    task = _task_body(payload)
    for key in ("id", "task_id"):
        value = task.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _task_body(payload: dict) -> dict:
    data = payload.get("data", payload)
    if isinstance(data, list):
        data = data[0] if data else {}
    if isinstance(data, dict) and isinstance(data.get("task"), dict):
        return data["task"]
    return data if isinstance(data, dict) else {}


def _video_url(task: dict) -> str | None:
    content = task.get("content")
    if isinstance(content, dict):
        url = content.get("video_url")
        if isinstance(url, str) and url.strip():
            return url.strip()
    if isinstance(content, list):
        for item in content:
            if not isinstance(item, dict):
                continue
            url = item.get("video_url")
            if isinstance(url, str) and url.strip():
                return url.strip()
            if isinstance(url, dict) and isinstance(url.get("url"), str):
                return url["url"].strip()
    return None


def _failure_text(task: dict) -> str:
    error = task.get("error")
    if isinstance(error, dict):
        message = error.get("message") or error.get("code")
        if isinstance(message, str) and message.strip():
            return message.strip()[:300]
    message = task.get("message")
    if isinstance(message, str) and message.strip():
        return message.strip()[:300]
    return "视频生成失败"


def _jpeg_data_url(path: Path) -> str:
    image = Image.open(path).convert("RGB")
    image.thumbnail((1280, 1280))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=85)
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"
