import uuid

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException
from sqlalchemy.exc import SQLAlchemyError

from video_agent.errors import VideoAgentError


_MESSAGES = {
    401: "缺少或错误的访问凭据",
    403: "此操作需要管理权限",
    404: "请求的资源不存在",
    409: "操作与当前状态、版本或幂等请求冲突",
    413: "上传文件超过大小限制",
    422: "请求参数校验失败",
    500: "服务内部错误",
    503: "服务依赖暂不可用",
}
_HTTP_CODES = {401: "UNAUTHORIZED", 403: "FORBIDDEN", 404: "NOT_FOUND", 409: "STATE_CONFLICT", 413: "FILE_TOO_LARGE", 422: "VALIDATION_ERROR", 500: "INTERNAL_ERROR", 503: "DEPENDENCY_UNAVAILABLE"}


def error_response(request: Request, status: int, *, code: str | None = None, retryable: bool | None = None, fields=None, context=None, details=None, headers=None):
    request_id = getattr(request.state, "request_id", str(uuid.uuid4()))
    body = {"code": code or _HTTP_CODES.get(status, "HTTP_ERROR"), "message": _MESSAGES.get(status, "请求无法完成"), "retryable": status == 503 if retryable is None else retryable, "request_id": request_id}
    if fields:
        body["fields"] = fields
    if context:
        body.update({key: value for key, value in context.items() if value is not None})
    if details:
        body["details"] = details
    return JSONResponse({"error": body}, status_code=status, headers={"X-Request-ID": request_id, **(headers or {})})


def _business_status(code: str) -> int:
    if code.endswith("NOT_FOUND") or code in {"TASK_NOT_FOUND", "ASSET_NOT_FOUND", "GENERATION_NOT_FOUND", "ARTIFACT_NOT_FOUND", "PLAN_NOT_FOUND", "PROFILE_NOT_FOUND"}:
        return 404
    if code in {"VALIDATION_ERROR", "INVALID_IDEMPOTENCY_KEY"}:
        return 422
    if code in {"FILE_TOO_LARGE", "UPLOAD_TOO_LARGE"}:
        return 413
    if "CONFLICT" in code or code in {"PROFILE_IN_USE", "PROFILE_BOUND"}:
        return 409
    return 503


def safe_view(view):
    """Provider failure messages may contain URLs, credentials and local paths."""
    failure = getattr(view, "failure", None)
    if failure is not None:
        return view.model_copy(update={"stage_detail": "处理失败，请按错误码和阶段查询或重试", "failure": failure.model_copy(update={"message": "处理失败，请按错误码和阶段查询或重试"})})
    return view


def safe_artifact(view):
    if view.quality_status == "not_assessed":
        return view.model_copy(update={"quality": {"status": "not_assessed", "reason": "未完成视觉检查", "findings": []}})
    return view


def register_errors(app: FastAPI) -> None:
    @app.middleware("http")
    async def request_context(request: Request, call_next):
        request.state.request_id = str(uuid.uuid4())
        try:
            response = await call_next(request)
        except Exception:
            return error_response(request, 500)
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    @app.exception_handler(VideoAgentError)
    async def business_error(request: Request, error: VideoAgentError):
        return error_response(request, _business_status(error.code), code=error.code, retryable=error.retryable, fields=error.fields, context={"task_id": error.task_id, "generation_id": error.generation_id, "plan_version": error.plan_version})

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, error: RequestValidationError):
        fields = [".".join(str(piece) for piece in item["loc"][1:]) for item in error.errors()]
        return error_response(request, 422, fields=fields)

    @app.exception_handler(SQLAlchemyError)
    async def database_error(request: Request, error: SQLAlchemyError):
        return error_response(request, 503)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, error: HTTPException):
        return error_response(request, error.status_code, headers=error.headers)

    @app.exception_handler(Exception)
    async def unknown_error(request: Request, error: Exception):
        return error_response(request, 500)
