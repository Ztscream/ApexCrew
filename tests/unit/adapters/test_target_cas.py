from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from apexcrew.adapters.repository.git import GitShowRefVerify, GitUpdateRefCas
from apexcrew.adapters.repository.target_cas import GitTargetCasAdapter
from apexcrew.domain.admission import RepositoryEffectError, ReservationAdminObservation
from apexcrew.domain.effects import TargetReservation
from apexcrew.domain.revisions import Sha256DigestText
from apexcrew.domain.types import GitOid, RunId


class _Repository:
    def assert_stable_for(self, operation: object) -> None:
        del operation


class _Guard:
    def require_safe_before_list(self, reservation: TargetReservation) -> None:
        assert reservation.phase == "REGISTERED_LOCKED"

    def require_compatible_observation(
        self, reservation: TargetReservation
    ) -> ReservationAdminObservation:
        return ReservationAdminObservation(
            admin_entry_name=reservation.admin_entry_name,
            admin_binding_digest=reservation.admin_binding_digest,
        )


class _Runner:
    def __init__(self, expected: GitOid, prepared: GitOid) -> None:
        self.expected = expected
        self.prepared = prepared
        self.operations: list[object] = []

    def run(self, repository: object, operation: object) -> subprocess.CompletedProcess[str]:
        del repository
        self.operations.append(operation)
        if isinstance(operation, GitShowRefVerify):
            oid = (
                self.expected
                if len([x for x in self.operations if isinstance(x, GitShowRefVerify)]) == 1
                else self.prepared
            )
            return subprocess.CompletedProcess((), 0, f"{oid}\n", "")
        assert isinstance(operation, GitUpdateRefCas)
        return subprocess.CompletedProcess((), 0, "", "")


def _reservation(phase: str = "REGISTERED_LOCKED") -> TargetReservation:
    return TargetReservation(
        reservation_id="reservation-1",
        run_id=RunId("run-1"),
        target_ref="refs/heads/main",
        pinned_target_oid=GitOid("a" * 40),
        path=Path("/tmp/reservation-1"),
        phase=phase,  # type: ignore[arg-type]
        admin_entry_name="reservation-1",
        admin_binding_digest=Sha256DigestText("sha256:" + "b" * 64),
    )


def test_target_cas_emits_typed_compare_and_swap_and_observes_poststate() -> None:
    reservation = _reservation()
    runner = _Runner(reservation.pinned_target_oid, GitOid("c" * 40))
    adapter = GitTargetCasAdapter(_Repository(), runner, _Guard(), reservation)  # type: ignore[arg-type]

    result = adapter.apply(
        target_ref=reservation.target_ref,
        expected_old_oid=reservation.pinned_target_oid,
        prepared_oid=GitOid("c" * 40),
        reflog_message="ApexCrew integrate run",
    )

    assert result.result_class == "APPLIED"
    assert result.observed_oid == GitOid("c" * 40)
    cas = next(
        operation for operation in runner.operations if isinstance(operation, GitUpdateRefCas)
    )
    assert cas.expected_old_oid == reservation.pinned_target_oid
    assert cas.prepared_oid == GitOid("c" * 40)


def test_target_cas_requires_locked_reservation() -> None:
    reservation = _reservation("CREATION_INTENT_RECORDED")
    adapter = GitTargetCasAdapter(
        _Repository(),
        _Runner(reservation.pinned_target_oid, reservation.pinned_target_oid),
        _Guard(),
        reservation,
    )  # type: ignore[arg-type]

    with pytest.raises(RepositoryEffectError, match="TARGET_CAS_BINDING_INVALID"):
        adapter.apply(
            target_ref=reservation.target_ref,
            expected_old_oid=reservation.pinned_target_oid,
            prepared_oid=GitOid("c" * 40),
            reflog_message="ApexCrew integrate run",
        )
