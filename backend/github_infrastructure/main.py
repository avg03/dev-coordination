from __future__ import annotations

import logging

from fastapi import FastAPI

from .routes import router


def create_app() -> FastAPI:
    logging.basicConfig(level=logging.INFO)
    app = FastAPI(title="Dev Coordination - GitHub Infrastructure", version="0.1.0")
    app.include_router(router)

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    return app


app = create_app()
