import hmac
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from video_agent.idempotency import validate_idempotency_key


bearer = HTTPBearer(auto_error=False, scheme_name="BearerAuth", description="业务或管理 Bearer Token；仅管理 Token 可访问 admin。")


def require_business(request: Request, credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]) -> str:
    if credentials is None:
        raise HTTPException(401, headers={"WWW-Authenticate": "Bearer"})
    token = credentials.credentials.encode("utf-8")
    runtime = request.app.state.runtime
    is_admin = hmac.compare_digest(token, runtime.admin_token.encode("utf-8"))
    is_business = hmac.compare_digest(token, runtime.business_token.encode("utf-8"))
    if not (is_admin or is_business):
        raise HTTPException(401, headers={"WWW-Authenticate": "Bearer"})
    return "admin" if is_admin else "business"


def require_admin(role: Annotated[str, Depends(require_business)]) -> None:
    if role != "admin":
        raise HTTPException(403)


def get_agent(request: Request):
    return request.app.state.agent


def get_settings_service(request: Request):
    return request.app.state.settings_service


def idempotency_key(key: Annotated[str | None, Header(alias="Idempotency-Key", description="1～128 个可见 ASCII 字符；同方法同键同参数重放，参数不同返回 409。", min_length=1, max_length=128)] = None) -> str | None:
    validate_idempotency_key(key)
    return key


Agent = Annotated[object, Depends(get_agent)]
Settings = Annotated[object, Depends(get_settings_service)]
IdempotencyKey = Annotated[str | None, Depends(idempotency_key)]
