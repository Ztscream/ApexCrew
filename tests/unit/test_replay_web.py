from __future__ import annotations

from fastapi.testclient import TestClient

from apexcrew.delivery.replay import replay_frame
from apexcrew.delivery.web import create_read_only_app
from apexcrew.domain.projection import AvailableRunReadModel


class Queries:
    def get(self, run_id: str, at_sequence: int | None = None):
        assert run_id == "run-1"
        assert at_sequence is None
        return AvailableRunReadModel(run_id=run_id, sequence=3, state="DRAFT")


def test_replay_frame_contains_only_sanitized_tier_one_fields() -> None:
    frame = replay_frame(AvailableRunReadModel(run_id="run-1", sequence=3, state="DRAFT"))
    assert frame == {
        "availability": "AVAILABLE",
        "run_id": "run-1",
        "sequence": 3,
        "state": "DRAFT",
    }
    assert "token" not in str(frame)


def test_web_app_is_read_only_and_bound_to_queries() -> None:
    response = TestClient(create_read_only_app(Queries(), "run-1")).get("/api/run")
    assert response.status_code == 200
    assert response.json()["sequence"] == 3
