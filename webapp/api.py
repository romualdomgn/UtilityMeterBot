"""FastAPI application factory wiring all modular Web App routers with static cache-busting."""

from __future__ import annotations

import hashlib
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from services.notifications import BackgroundTaskManager
from webapp.routers import admin, apartments, families, readings, tenants

STATIC_DIR = Path(__file__).resolve().parent / "static"
NO_CACHE_PATHS = frozenset({"/", "/index.html", "/app.js", "/style.css"})


def _asset_hash(filename: str) -> str:
    path = STATIC_DIR / filename
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:10]
    except OSError:
        return "0"


def render_index_html() -> str:
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    css_v = _asset_hash("style.css")
    js_v = _asset_hash("app.js")
    html = html.replace('href="/style.css"', f'href="/style.css?v={css_v}"')
    html = html.replace('src="/app.js"', f'src="/app.js?v={js_v}"')
    return html


def create_web_app(bot, bg_tasks: BackgroundTaskManager | None = None) -> FastAPI:
    app = FastAPI(title="Utility Meter Web App")
    app.state.bot = bot
    app.state.bg_tasks = bg_tasks or BackgroundTaskManager()

    @app.middleware("http")
    async def disable_static_cache(request: Request, call_next):
        response = await call_next(request)
        if request.url.path in NO_CACHE_PATHS:
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        return response

    app.include_router(apartments.router)
    app.include_router(readings.router)
    app.include_router(tenants.router)
    app.include_router(families.router)
    app.include_router(admin.router)

    @app.get("/", include_in_schema=False)
    @app.get("/index.html", include_in_schema=False)
    async def serve_index() -> HTMLResponse:
        return HTMLResponse(
            content=render_index_html(),
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                "Pragma": "no-cache",
                "Expires": "0",
            },
        )

    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
    return app
