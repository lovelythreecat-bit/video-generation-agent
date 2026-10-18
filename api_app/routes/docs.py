from pathlib import Path

from fastapi import FastAPI
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.staticfiles import StaticFiles


def register_docs(app: FastAPI) -> None:
    static = Path(__file__).resolve().parent.parent / "static" / "docs"
    app.mount("/static/docs", StaticFiles(directory=static), name="api-docs-assets")

    @app.get("/docs", include_in_schema=False)
    def swagger():
        return get_swagger_ui_html(openapi_url=app.openapi_url, title="Video Agent API · 交互文档", swagger_js_url="/static/docs/swagger-ui-bundle.js", swagger_css_url="/static/docs/swagger-ui.css", swagger_favicon_url="/static/docs/favicon.svg", swagger_ui_parameters={"persistAuthorization": False, "displayRequestDuration": True, "validatorUrl": None})

    @app.get("/redoc", include_in_schema=False)
    def redoc():
        return get_redoc_html(openapi_url=app.openapi_url, title="Video Agent API · 接口手册", redoc_js_url="/static/docs/redoc.standalone.js", redoc_favicon_url="/static/docs/favicon.svg", with_google_fonts=False)
