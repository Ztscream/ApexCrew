"""Target-ref compare-and-swap adapter with reservation safety checks."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from apexcrew.adapters.repository.git import (
    GitCommandRunner,
    GitShowRefVerify,
    GitUpdateRefCas,
    RepositoryInstance,
)
from apexcrew.domain.admission import RepositoryEffectError, TargetReservationWorktreeGuard
from apexcrew.domain.effects import TargetReservation
from apexcrew.domain.revisions import Sha256DigestText
from apexcrew.domain.types import GitOid


@dataclass(frozen=True, slots=True)
class TargetCasResult:
    result_class: Literal["APPLIED", "CONFLICT", "UNOBSERVABLE"]
    observed_oid: GitOid | None


@dataclass(frozen=True, slots=True)
class TargetCasObservation:
    state: Literal["EXACT_POST", "EXACT_PRE", "THIRD_STATE", "UNAVAILABLE"]
    observed_oid: GitOid | None
    registration_digest: Sha256DigestText | None


class GitTargetCasAdapter:
    """Apply one final target-ref CAS only with the exact locked reservation."""

    def __init__(
        self,
        repository: RepositoryInstance,
        runner: GitCommandRunner,
        guard: TargetReservationWorktreeGuard,
        reservation: TargetReservation,
    ) -> None:
        self._repository = repository
        self._runner = runner
        self._guard = guard
        self._reservation = reservation

    def apply(
        self,
        *,
        target_ref: str,
        expected_old_oid: GitOid,
        prepared_oid: GitOid,
        reflog_message: str,
    ) -> TargetCasResult:
        if (
            target_ref != self._reservation.target_ref
            or expected_old_oid != self._reservation.pinned_target_oid
            or self._reservation.phase != "REGISTERED_LOCKED"
            or not reflog_message
        ):
            raise RepositoryEffectError("TARGET_CAS_BINDING_INVALID")
        try:
            self._guard.require_safe_before_list(self._reservation)
            observation = self._guard.require_compatible_observation(self._reservation)
            if (
                observation.admin_entry_name != self._reservation.admin_entry_name
                or observation.admin_binding_digest != self._reservation.admin_binding_digest
                or not observation.locked
            ):
                raise RepositoryEffectError("TARGET_UNSAFE")
            before = self._runner.run(self._repository, GitShowRefVerify(target_ref))
            if before.returncode != 0:
                return TargetCasResult("UNOBSERVABLE", None)
            observed = GitOid(before.stdout.strip())
            if observed != expected_old_oid:
                return TargetCasResult("CONFLICT", observed)
            result = self._runner.run(
                self._repository,
                GitUpdateRefCas(
                    direct_ref=target_ref,
                    prepared_oid=prepared_oid,
                    expected_old_oid=expected_old_oid,
                    reflog_message=reflog_message,
                ),
            )
            if result.returncode != 0:
                after = self._runner.run(self._repository, GitShowRefVerify(target_ref))
                if after.returncode != 0:
                    return TargetCasResult("UNOBSERVABLE", None)
                observed_after = GitOid(after.stdout.strip())
                return TargetCasResult(
                    "CONFLICT" if observed_after != expected_old_oid else "UNOBSERVABLE",
                    observed_after,
                )
            refresh_repository = getattr(
                self._repository, "refresh_after_verified_owned_transition", None
            )
            if callable(refresh_repository):
                self._repository = refresh_repository()
            refresh_guard = getattr(self._guard, "refresh_after_git_transition", None)
            if callable(refresh_guard):
                refresh_guard()
            self._guard.require_safe_before_list(self._reservation)
            after = self._runner.run(self._repository, GitShowRefVerify(target_ref))
            if after.returncode != 0:
                return TargetCasResult("UNOBSERVABLE", None)
            observed_after = GitOid(after.stdout.strip())
            if observed_after != prepared_oid:
                return TargetCasResult("UNOBSERVABLE", observed_after)
            return TargetCasResult("APPLIED", observed_after)
        except (OSError, RepositoryEffectError, ValueError):
            return TargetCasResult("UNOBSERVABLE", None)

    def observe_resolution(
        self,
        *,
        target_ref: str,
        expected_old_oid: GitOid,
        prepared_oid: GitOid,
    ) -> TargetCasObservation:
        if (
            target_ref != self._reservation.target_ref
            or expected_old_oid != self._reservation.pinned_target_oid
            or self._reservation.phase != "REGISTERED_LOCKED"
        ):
            return TargetCasObservation("UNAVAILABLE", None, None)
        try:
            self._guard.require_safe_before_list(self._reservation)
            admin = self._guard.require_compatible_observation(self._reservation)
            if admin.admin_binding_digest is None:
                return TargetCasObservation("UNAVAILABLE", None, None)
            before = self._runner.run(self._repository, GitShowRefVerify(target_ref))
            if before.returncode != 0:
                return TargetCasObservation("UNAVAILABLE", None, admin.admin_binding_digest)
            observed = GitOid(before.stdout.strip())
            state: Literal["EXACT_POST", "EXACT_PRE", "THIRD_STATE"]
            if observed == prepared_oid:
                state = "EXACT_POST"
            elif observed == expected_old_oid:
                state = "EXACT_PRE"
            else:
                state = "THIRD_STATE"
            return TargetCasObservation(state, observed, admin.admin_binding_digest)
        except (OSError, RepositoryEffectError, ValueError):
            return TargetCasObservation("UNAVAILABLE", None, None)
