from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .routes import router as github_router

# Projects API — Impact-Aware Coordinator (POST /api/projects/context, GET /commits, PR review/approve)
# Keep import resilient to different PYTHONPATH layouts: `backend` may be the cwd or a sub-package.
try:
    from api.projects import router as projects_router  # when PYTHONPATH = backend/
except ImportError:  # pragma: no cover
    try:
        from backend.api.projects import router as projects_router  # when PYTHONPATH = repo root
    except ImportError:
        projects_router = None  # type: ignore[assignment]
        logging.getLogger(__name__).warning("projects router not available — /api/projects/* will be missing")


def create_app() -> FastAPI:
    logging.basicConfig(level=logging.INFO)
    try:
        from telemetry.tracer import init_telemetry

        init_telemetry()
    except Exception:
        logging.getLogger(__name__).warning("Telemetry unavailable — continuing without tracing")
    app = FastAPI(title="Dev Coordination - GitHub Infrastructure", version="0.1.0")

    # CORS for Streamlit frontend (default 8501) and local dev
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Core infra
    app.include_router(github_router)
    # Project context / Work Journey / PR Review — must be mounted for frontend at /api/projects/*
    if projects_router is not None:
        app.include_router(projects_router)

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    return app


app = create_app()
