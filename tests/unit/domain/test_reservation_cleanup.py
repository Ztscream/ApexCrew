from __future__ import annotations

import pytest

from apexcrew.domain.reservation_cleanup import (
    CleanupStatus,
    ReservationCleanup,
    Tombstone,
)


def test_cleanup_is_idempotent_and_returns_minimal_tombstone() -> None:
    cleanup = ReservationCleanup(run_id="run-1", reservation_id="reservation-1")
    first = cleanup.settle(observed_exists=False, cleanup_authorized=True)
    second = cleanup.settle(observed_exists=False, cleanup_authorized=True)

    assert first.status == CleanupStatus.CLEANED
    assert second.status == CleanupStatus.ALREADY_CLEANED
    assert first.tombstone == Tombstone(run_id="run-1", reservation_id="reservation-1")


def test_unknown_or_unauthorized_cleanup_never_removes_state() -> None:
    cleanup = ReservationCleanup(run_id="run-1", reservation_id="reservation-1")
    with pytest.raises(ValueError, match="CLEANUP_AUTHORITY_REQUIRED"):
        cleanup.settle(observed_exists=True, cleanup_authorized=False)
    with pytest.raises(ValueError, match="CLEANUP_STATE_UNOBSERVABLE"):
        cleanup.settle(observed_exists=None, cleanup_authorized=True)
