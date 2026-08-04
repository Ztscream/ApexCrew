from __future__ import annotations

from fastapi import FastAPI

from apexcrew.application import RunQueries
from apexcrew.domain.types import RunId

from .replay import replay_frame


def create_read_only_app(queries: RunQueries, run_id: RunId) -> FastAPI:
    app = FastAPI(title="ApexCrew Read-Only Run View", docs_url=None, redoc_url=None)

    @app.get("/api/run")
    def current_run() -> dict[str, object]:
        return replay_frame(queries.get(run_id))

    return app
