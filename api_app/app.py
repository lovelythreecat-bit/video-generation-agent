import hmac
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi

from api_app.errors import register_errors


_DESCRIPTION = """视频任务与模型配置 API。时间字段统一为 UTC。

业务流程：上传素材 → 创建任务（201）→ 轮询任务获取方案 → 修改或确认方案（202）→ 轮询执行 → 查询成片并下载。

点击 Authorize 输入业务或管理 Token，页面不会持久保存凭据。业务调用方共享任务空间；external_user_id 是筛选字段。

状态变更支持 Idempotency-Key（1～128 个可见 ASCII 字符）：相同方法与键且请求相同则重放结果；不同请求返回 409。

管理员流程：查询适配器类型 → 创建模型配置 → 按 configuration_revision 更新角色绑定 → 更新 settings 切换 real 模式。
配置 PATCH/DELETE 使用 expected_version 防止覆盖；模型秘密只写不读，工作快照和历史修订均加密。

异步任务由独立 Worker 执行；HTTP 不启动 Worker。所有错误使用 error 对象，request_id 可用于排查。
"""


def create_app(settings=None, *, agent=None, settings_service=None) -> FastAPI:
    """Construct production MySQL API, or explicitly inject isolated test services."""
    if settings is None:
        from video_agent.runtime import RuntimeSettings
        settings = RuntimeSettings.from_env()
    business = settings.business_token
    admin = settings.admin_token
    if not isinstance(business, str) or not isinstance(admin, str) or not business.strip() or not admin.strip() or hmac.compare_digest(business.encode(), admin.encode()):
        raise ValueError("业务与管理 Token 必须非空且不同")
    owned = agent is None
    database = None
    if owned:
        from video_agent.db import Database
        from video_agent.service import VideoAgent
        from video_agent.settings_service import SettingsService
        database = Database.from_settings(settings)
        try:
            settings_service = settings_service or SettingsService(database, settings.secret_key)
            agent = VideoAgent(settings.home, database=database, settings_service=settings_service)
        except Exception:
            database.close()
            raise
    elif settings_service is None:
        settings_service = agent.settings_service

    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            if owned:
                database.close()

    app = FastAPI(title="Video Agent API", version="1.0.0", description=_DESCRIPTION, docs_url=None, redoc_url=None, lifespan=lifespan, openapi_tags=[{"name": "任务与方案", "description": "规划、确认和任务轮询。"}, {"name": "生成与成品", "description": "生成进度、重试和成片版本。"}, {"name": "素材与文件", "description": "受限素材上传和 Range 下载。"}, {"name": "配置管理（管理员）", "description": "模型配置、绑定和公共设置；仅管理 Token。"}, {"name": "系统能力"}, {"name": "健康检查"}])
    app.state.runtime = settings
    app.state.agent = agent
    app.state.settings_service = settings_service
    app.state.production = owned
    app.state.schema_revision = "20261014_03"
    register_errors(app)
    from api_app.routes import capabilities, files, health, settings as settings_routes, tasks
    from api_app.routes.docs import register_docs
    for router in (health.router, tasks.router, files.router, capabilities.router, settings_routes.router):
        app.include_router(router)
    register_docs(app)
    from api_app.schemas import add_chinese_field_documentation

    def documented_openapi():
        if app.openapi_schema is None:
            app.openapi_schema = add_chinese_field_documentation(get_openapi(title=app.title, version=app.version, description=app.description, routes=app.routes, tags=app.openapi_tags))
        return app.openapi_schema

    app.openapi = documented_openapi
    return app
