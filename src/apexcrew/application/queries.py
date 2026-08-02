from __future__ import annotations

from apexcrew.domain.projection import ProjectionService, RunReadModel
from apexcrew.domain.types import RunId


class RunQueryService:
    def __init__(self, projection: ProjectionService) -> None:
        self._projection = projection

    def get(self, run_id: RunId, at_sequence: int | None = None) -> RunReadModel:
        return self._projection.project(run_id, at_sequence)
