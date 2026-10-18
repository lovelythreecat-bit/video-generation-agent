import shutil
from datetime import datetime, timedelta, timezone

from cryptography.fernet import Fernet
from fastapi import APIRouter, Request

from api_app.errors import error_response
from api_app.schemas import ERROR_RESPONSES, HealthView


router = APIRouter(tags=["健康检查"])


@router.get("/health/live", response_model=HealthView, operation_id="healthLive", summary="检查 HTTP 进程存活")
def live():
    return HealthView(status="live")


def _time(value):
    if isinstance(value, datetime):
        result = value
    else:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result.astimezone(timezone.utc)


@router.get("/health/ready", response_model=HealthView, operation_id="healthReady", summary="检查执行依赖就绪", description="检查数据库、schema、FFmpeg/ffprobe、加密密钥和 30 秒内有效的单 Worker 租约；不会调用模型供应商。失败返回脱敏检查项。", responses={503: ERROR_RESPONSES[503]})
def ready(request: Request):
    checks = {"database": False, "schema": False, "ffmpeg": shutil.which("ffmpeg") is not None, "ffprobe": shutil.which("ffprobe") is not None, "secret_key": False, "worker": False}
    try:
        Fernet(request.app.state.runtime.secret_key)
        request.app.state.settings_service.read_effective_config()
        checks["secret_key"] = True
    except Exception:
        pass
    try:
        with request.app.state.agent.db.transaction(write=False) as connection:
            connection.execute("SELECT 1 AS alive").fetchone()
            checks["database"] = True
            # All execution/configuration tables must be queryable. A partially applied
            # schema must never be advertised as ready.
            for table in ("assets", "tasks", "plan_versions", "generations", "generation_shots", "artifacts", "work_items", "idempotency", "model_profiles", "model_profile_revisions", "adapter_bindings", "app_settings", "provider_jobs", "worker_heartbeats"):
                connection.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
            checks["schema"] = True
            if request.app.state.production:
                version = connection.execute("SELECT version_num FROM alembic_version").fetchone()
                checks["schema"] = version is not None and version["version_num"] == request.app.state.schema_revision
            worker = connection.execute("SELECT last_seen_at, lease_until, stopping FROM worker_heartbeats WHERE singleton_id=1").fetchone()
            now = datetime.now(timezone.utc)
            checks["worker"] = worker is not None and not worker["stopping"] and now - timedelta(seconds=30) <= _time(worker["last_seen_at"]) <= now + timedelta(seconds=5) and _time(worker["lease_until"]) > now
    except Exception:
        pass
    if not all(checks.values()):
        return error_response(request, 503, details={"checks": checks})
    return HealthView(status="ready")
