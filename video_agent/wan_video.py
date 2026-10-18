from __future__ import annotations

import base64
import io
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from PIL import Image

from video_agent.config import wan_segment_error
from video_agent.errors import VideoAgentError
from video_agent.provider_jobs import DurableSubmission
from video_agent.http_json import request_bytes, request_json
from video_agent.media import probe
from video_agent.ports import QueryResult, ShotRequest, SubmitResult, VideoCapabilities

_NATIVE = ("9:16", "16:9", "1:1", "4:5", "3:4", "4:3", "3:2", "2:3")
_SYNTHESIS = "/services/aigc/video-generation/video-synthesis"


class WanVideo(DurableSubmission):
    adapter_name = 'wan'
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        max_segment_seconds: str,
        state_path: Path | None = None,
        job_store=None,
        transport=None,
        byte_transport=None,
    ) -> None:
        message = wan_segment_error(str(max_segment_seconds))
        if message:
            raise VideoAgentError("VALIDATION_ERROR", message)
        self.base_url = base_url.strip().rstrip("/")
        self.api_key = api_key.strip()
        self.model = model.strip()
        self.max_segment_seconds = int(str(max_segment_seconds).strip())
        self.job_store = job_store
        self.state_path = None if job_store is not None else state_path
        self.transport = transport
        self.byte_transport = byte_transport
        self._records = _load_records(self.state_path)

    def capabilities(self) -> VideoCapabilities:
        return VideoCapabilities(
            native_aspects=list(_NATIVE),
            max_segment_seconds=self.max_segment_seconds,
            supports_first_frame=True,
            supports_reference_images=False,
            supports_tail_frame=False,
        )

    def submit_shot(self, request: ShotRequest) -> SubmitResult:
        known = self._records.get(request.provider_idempotency_key)
        if known and known.get("task_id"):
            return SubmitResult(external_task_id=str(known["task_id"]), uncertain=False)
        image = request.first_frame_path
        if image is None or not image.is_file():
            raise VideoAgentError("PROVIDER_REJECTED", "视频生成需要首帧图片", retryable=True)
        durable_id = self._durable_before(request)
        if durable_id:
            return SubmitResult(external_task_id=durable_id, uncertain=False)
        duration, send_audio = _duration_and_audio(self.model, request.frame_count)
        parameters: dict[str, object] = {
            "resolution": "720P",
            "duration": duration,
            "prompt_extend": False,
            "watermark": False,
        }
        if send_audio:
            parameters["audio"] = request.with_audio
        image_url = _jpeg_data_url(image)
        if _is_wan30(self.model):
            shot_input: dict[str, object] = {
                "prompt": request.prompt,
                "media": [{"type": "first_frame", "url": image_url}],
            }
        else:
            shot_input = {"prompt": request.prompt, "img_url": image_url}
        try:
            payload = request_json(
                "POST",
                _submit_url(self.base_url),
                headers=_headers(self.api_key, async_call=True),
                body={
                    "model": self.model,
                    "input": shot_input,
                    "parameters": parameters,
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
        self._remember(request.provider_idempotency_key, external_id, request.width, request.height)
        return SubmitResult(external_task_id=external_id, uncertain=False)

    def query(self, *, external_task_id: str | None, provider_idempotency_key: str) -> QueryResult:
        job = self._durable_lookup(provider_idempotency_key)
        if job is not None:
            external_task_id = external_task_id or job.external_task_id
            if not external_task_id and job.submission_state in {"intended", "uncertain", "accepted"}:
                return QueryResult(state="pending", message="外部提交结果等待确认")
        task_id = external_task_id or _task_from(self._records.get(provider_idempotency_key))
        if not task_id:
            return QueryResult(state="absent", message="还没有这个视频任务")
        try:
            payload = request_json(
                "GET",
                _task_url(self.base_url, task_id),
                headers=_headers(self.api_key),
                timeout=30,
                transport=self.transport,
            )
        except VideoAgentError as exc:
            if "HTTP 404" in exc.message:
                self._durable_absent(provider_idempotency_key)
                return QueryResult(state="absent", message="视频任务不存在")
            raise
        status = _task_status(payload)
        if status in {None, "PENDING", "RUNNING"}:
            return QueryResult(state="pending", message=status or "")
        if status == "SUCCEEDED":
            return QueryResult(state="succeeded")
        if status in {"FAILED", "CANCELED", "CANCELLED"}:
            return QueryResult(state="failed", error_code="PROVIDER_REJECTED", message=_failure_text(payload))
        if status == "UNKNOWN":
            self._durable_absent(provider_idempotency_key)
            return QueryResult(state="absent", message="视频任务不存在")
        return QueryResult(state="pending", message=status or "")

    def download(self, external_task_id: str) -> bytes:
        record = _record_for(self._records, external_task_id)
        if self.job_store is not None:
            job = self.job_store.get_by_external_id(self.adapter_name, external_task_id) or self.job_store.get(self.adapter_name, external_task_id)
            record = None if job is None else {"task_id": job.external_task_id, **job.download_metadata}
        task_id = _task_from(record) or external_task_id
        payload = request_json(
            "GET",
            _task_url(self.base_url, task_id),
            headers=_headers(self.api_key),
            timeout=30,
            transport=self.transport,
        )
        url = _video_url(payload)
        if url is None:
            raise VideoAgentError("PROVIDER_TEMPORARY", "视频还没有下载地址", retryable=True)
        data = request_bytes(url, timeout=120, transport=self.byte_transport)
        if record is None:
            raise VideoAgentError("PROVIDER_TEMPORARY", "视频还没有记下画面尺寸", retryable=True)
        return _scale_mp4(data, int(record["width"]), int(record["height"]))

    def _remember(self, key: str, task_id: str, width: int, height: int) -> None:
        if not key or not task_id:
            return
        self._records[key] = {"task_id": task_id, "width": int(width), "height": int(height)}
        if self.job_store is not None or self.state_path is None:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self._records, ensure_ascii=False), encoding="utf-8")
        temporary.replace(self.state_path)


def _submit_url(base_url: str) -> str:
    if base_url.endswith(_SYNTHESIS):
        return base_url
    return base_url + _SYNTHESIS


def _task_url(base_url: str, task_id: str) -> str:
    root = base_url
    index = root.find(_SYNTHESIS)
    if index != -1:
        root = root[:index]
    return root.rstrip("/") + "/tasks/" + task_id


def _headers(api_key: str, *, async_call: bool = False) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    if async_call:
        headers["X-DashScope-Async"] = "enable"
    return headers


def _is_wan30(model: str) -> bool:
    return "wan3.0" in model.lower()


def _duration_and_audio(model: str, frame_count: int) -> tuple[int, bool]:
    seconds = max(1, int(round(frame_count / 30)))
    name = model.lower()
    if _is_wan30(name):
        return (2 if seconds < 2 else seconds), True
    if "wan2.2" in name or "wanx2.1" in name:
        allowed: tuple[int, ...] = (5,)
        send_audio = False
    elif "wan2.5" in name or "wan2.6" in name:
        allowed = (5, 10)
        send_audio = True
    else:
        return seconds, False
    fits = [item for item in allowed if item >= seconds]
    return (min(fits) if fits else seconds), send_audio


def _load_records(path: Path | None) -> dict[str, dict]:
    if path is None or not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    records: dict[str, dict] = {}
    for key, value in raw.items():
        if not str(key) or not isinstance(value, dict):
            continue
        task_id = _clean(value.get("task_id"))
        width = _int_value(value.get("width"))
        height = _int_value(value.get("height"))
        if task_id is None or width is None or height is None:
            continue
        records[str(key)] = {"task_id": task_id, "width": width, "height": height}
    return records


def _record_for(records: dict[str, dict], token: str) -> dict | None:
    direct = records.get(token)
    if direct and direct.get("task_id"):
        return direct
    for record in records.values():
        if record.get("task_id") == token:
            return record
    return None


def _task_from(record: dict | None) -> str | None:
    if not record:
        return None
    return _clean(record.get("task_id"))


def _task_id(payload: dict) -> str | None:
    output = payload.get("output") if isinstance(payload.get("output"), dict) else {}
    found = _clean(output.get("task_id"))
    if found:
        return found
    for source in (payload, output):
        for key in ("id", "task_id"):
            found = _clean(source.get(key))
            if found:
                return found
    data = payload.get("data")
    if isinstance(data, list):
        data = data[0] if data else None
    if isinstance(data, dict):
        for source in (data, data.get("task") if isinstance(data.get("task"), dict) else {}):
            for key in ("id", "task_id"):
                found = _clean(source.get(key))
                if found:
                    return found
    return None


def _task_status(payload: dict) -> str | None:
    output = payload.get("output")
    if not isinstance(output, dict) or "task_status" not in output:
        return None
    return _clean(output.get("task_status")).upper() if _clean(output.get("task_status")) else None


def _video_url(payload: dict) -> str | None:
    output = payload.get("output")
    if isinstance(output, dict):
        return _clean(output.get("video_url"))
    return None


def _failure_text(payload: dict) -> str:
    output = payload.get("output")
    sources = [output, payload] if isinstance(output, dict) else [payload]
    for source in sources:
        if not isinstance(source, dict):
            continue
        message = _clean(source.get("message"))
        if message:
            return message[:300]
        code = _clean(source.get("code"))
        if code:
            return code[:300]
    return "视频生成失败"


def _clean(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _int_value(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _jpeg_data_url(path: Path) -> str:
    image = Image.open(path).convert("RGB")
    image.thumbnail((1280, 1280))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=85)
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"


def _scale_mp4(data: bytes, width: int, height: int) -> bytes:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        source = root / "source.mp4"
        dest = root / "scaled.mp4"
        source.write_bytes(data)
        heard = probe(source).has_audio
        args = [
            "-i", str(source),
            "-vf", f"scale={width}:{height}",
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            "-r", "30",
            "-preset", "ultrafast",
        ]
        if heard:
            args.extend(["-c:a", "aac"])
        else:
            args.append("-an")
        args.append(str(dest))
        _run_ffmpeg(args)
        return dest.read_bytes()


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
