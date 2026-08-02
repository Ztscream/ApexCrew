from __future__ import annotations

from typing import Annotated, Literal, Protocol

from pydantic import Field

from apexcrew.domain.commands import PublicRunSnapshot
from apexcrew.domain.revisions import FrozenDocument
from apexcrew.domain.types import AuditSequence, RunId, RunState


class PublicStateReader(Protocol):
    def public_run_snapshot(
        self, run_id: RunId, at_sequence: int | None
    ) -> PublicRunSnapshot | None:
        raise NotImplementedError


class AvailableRunReadModel(FrozenDocument):
    availability: Literal["AVAILABLE"] = "AVAILABLE"
    run_id: RunId
    sequence: AuditSequence
    state: RunState


class RunNotFoundReadModel(FrozenDocument):
    availability: Literal["RUN_NOT_FOUND"] = "RUN_NOT_FOUND"
    run_id: RunId


RunReadModel = Annotated[
    AvailableRunReadModel | RunNotFoundReadModel,
    Field(discriminator="availability"),
]


class ProjectionService:
    def __init__(self, state: PublicStateReader) -> None:
        self._state = state

    def project(self, run_id: RunId, at_sequence: int | None = None) -> RunReadModel:
        snapshot = self._state.public_run_snapshot(run_id, at_sequence)
        if snapshot is None:
            return RunNotFoundReadModel(run_id=run_id)
        return AvailableRunReadModel(
            run_id=run_id,
            sequence=snapshot.sequence,
            state=snapshot.state,
        )
