from __future__ import annotations

from typing import Any

from apexcrew.domain.projection import RunReadModel

_PUBLIC_FIELDS = frozenset({"availability", "run_id", "sequence", "state"})


def replay_frame(model: RunReadModel) -> dict[str, Any]:
    data = model.model_dump(mode="json")
    if set(data) - _PUBLIC_FIELDS:
        raise ValueError("REPLAY_PROJECTION_NOT_SANITIZED")
    return {key: data[key] for key in sorted(data)}
