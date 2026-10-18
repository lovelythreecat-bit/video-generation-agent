from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from python_multipart import MultipartParser
from python_multipart.multipart import parse_options_header
from starlette.concurrency import run_in_threadpool

from api_app.dependencies import Agent, require_business
from api_app.schemas import ERROR_RESPONSES
from video_agent.errors import VideoAgentError
from video_agent.models import AssetView


router = APIRouter(prefix="/api/v1", tags=["素材与文件"], dependencies=[Depends(require_business)], responses=ERROR_RESPONSES)


async def read_asset_upload(request: Request, maximum: int) -> tuple[str, Literal["image", "audio"], bytes]:
    """Parse incrementally: trust actual file bytes, never Content-Length."""
    media_type, options = parse_options_header(request.headers.get("content-type", ""))
    boundary = options.get(b"boundary")
    if media_type != b"multipart/form-data" or not boundary or len(boundary) > 200:
        raise VideoAgentError("VALIDATION_ERROR", "需要 multipart/form-data", fields=["file", "kind"])
    parts = 0
    state = {}
    values = {}
    completed = False
    total = 0

    def invalid():
        raise VideoAgentError("VALIDATION_ERROR", "上传字段无效", fields=["file", "kind"])

    def part_begin():
        nonlocal parts
        parts += 1
        if parts > 2:
            invalid()
        state.clear()
        state.update(headers={}, header_name=bytearray(), header_value=bytearray(), data=bytearray(), header_bytes=0)

    def header_field(data, start, end):
        state["header_bytes"] += end - start
        if state["header_bytes"] > 8192:
            invalid()
        state["header_name"].extend(data[start:end])

    def header_value(data, start, end):
        state["header_bytes"] += end - start
        if state["header_bytes"] > 8192:
            invalid()
        state["header_value"].extend(data[start:end])

    def header_end():
        state["headers"][bytes(state["header_name"]).lower()] = bytes(state["header_value"])
        state["header_name"].clear()
        state["header_value"].clear()

    def headers_finished():
        disposition, fields = parse_options_header(state["headers"].get(b"content-disposition", b""))
        if disposition != b"form-data" or fields.get(b"name") not in {b"file", b"kind"}:
            invalid()
        name = fields[b"name"].decode("ascii")
        if name in values or (name == "file") != (b"filename" in fields):
            invalid()
        state["name"] = name
        if name == "file":
            try:
                state["filename"] = fields[b"filename"].decode("utf-8")
            except UnicodeDecodeError:
                invalid()

    def part_data(data, start, end):
        limit = maximum if state["name"] == "file" else 32
        remaining = limit + 1 - len(state["data"])
        state["data"].extend(data[start:min(end, start + remaining)])
        if len(state["data"]) > limit:
            if state["name"] == "file":
                raise VideoAgentError("FILE_TOO_LARGE", "上传文件超过大小限制", fields=["file"])
            invalid()

    def part_end():
        values[state["name"]] = (state.get("filename"), bytes(state["data"]))

    def end():
        nonlocal completed
        completed = True

    parser = MultipartParser(boundary, {"on_part_begin": part_begin, "on_header_field": header_field, "on_header_value": header_value, "on_header_end": header_end, "on_headers_finished": headers_finished, "on_part_data": part_data, "on_part_end": part_end, "on_end": end})
    try:
        async for chunk in request.stream():
            total += len(chunk)
            if total > maximum + 65536:
                raise VideoAgentError("FILE_TOO_LARGE", "上传内容超过大小限制", fields=["file"])
            parser.write(chunk)
        parser.finalize()
    except VideoAgentError:
        raise
    except Exception as error:
        raise VideoAgentError("VALIDATION_ERROR", "无法解析上传内容", fields=["file", "kind"]) from error
    if not completed or set(values) != {"file", "kind"}:
        invalid()
    try:
        kind = values["kind"][1].decode("ascii")
    except UnicodeDecodeError:
        invalid()
    if kind not in {"image", "audio"}:
        invalid()
    return values["file"][0], kind, values["file"][1]


@router.post("/assets", status_code=201, response_model=AssetView, operation_id="uploadAsset", summary="上传图片或音频素材", description="multipart 字段为 file 与 kind=image/audio。按实际文件字节流限制大小，超限读取到上限加一即停止；不接受服务器路径。图片须能真实解码，文件 Content-Type 不能替代内容校验。", openapi_extra={"requestBody": {"required": True, "content": {"multipart/form-data": {"schema": {"type": "object", "required": ["file", "kind"], "additionalProperties": False, "properties": {"file": {"type": "string", "format": "binary", "description": "图片（JPEG/PNG/WebP）或音频（MP3/WAV/M4A）。"}, "kind": {"type": "string", "enum": ["image", "audio"], "description": "素材种类。"}}}}}}})
async def upload_asset(request: Request, agent: Agent):
    settings = await run_in_threadpool(request.app.state.settings_service.read_settings)
    filename, kind, data = await read_asset_upload(request, settings.max_file_bytes)
    asset_id = await run_in_threadpool(agent.register_asset, filename=filename, kind=kind, data=data)
    return await run_in_threadpool(agent.get_asset, asset_id)


def _file_response(path, media_type: str, filename: str):
    if not path.is_file():
        raise HTTPException(404)
    return FileResponse(path, media_type=media_type, filename=filename, content_disposition_type="attachment")


_BINARY = {200: {"description": "文件内容。", "content": {"application/octet-stream": {"schema": {"type": "string", "format": "binary"}}}}, 206: {"description": "标准 Range 部分内容。"}, 416: {"description": "Range 超出文件范围。"}}


@router.get("/assets/{asset_id}/content", response_class=FileResponse, responses=_BINARY, operation_id="downloadAsset", summary="按素材 ID 下载", description="只解析数据库资产 ID；不接受文件路径。支持框架标准 Range。")
def download_asset(asset_id: str, agent: Agent):
    asset = agent.get_asset(asset_id)
    return _file_response(agent.asset_path(asset_id), asset.media_type, asset.filename)


@router.get("/artifacts/{artifact_id}/content", response_class=FileResponse, responses=_BINARY, operation_id="downloadArtifact", summary="下载成片视频", description="支持 Range: bytes=0-1023，成功返回 206；越界返回 416。资源响应不暴露服务端路径。")
def download_artifact(artifact_id: str, agent: Agent):
    return _file_response(agent.artifact_path(artifact_id), "video/mp4", f"{artifact_id}.mp4")
