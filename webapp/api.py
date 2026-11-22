"""FastAPI application factory wiring all modular Web App routers."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from webapp.routers import admin, apartments, families, readings, tenants

STATIC_DIR = Path(__file__).resolve().parent / "static"


def create_web_app(bot) -> FastAPI:
    app = FastAPI(title="Utility Meter Web App")
    app.state.bot = bot

    app.include_router(apartments.router)
    app.include_router(readings.router)
    app.include_router(tenants.router)
    app.include_router(families.router)
    app.include_router(admin.router)

    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
    return app
