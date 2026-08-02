from __future__ import annotations

from typing import Protocol

from apexcrew.domain.commands import CommandEnvelope, CommandOutcome
from apexcrew.domain.projection import RunReadModel
from apexcrew.domain.types import RunId


class CrewControl(Protocol):
    def handle(self, command: CommandEnvelope) -> CommandOutcome:
        raise NotImplementedError


class RunQueries(Protocol):
    def get(self, run_id: RunId, at_sequence: int | None = None) -> RunReadModel:
        raise NotImplementedError


__all__ = ["CrewControl", "RunQueries"]
