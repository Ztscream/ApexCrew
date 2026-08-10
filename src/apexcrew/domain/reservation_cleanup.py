from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from apexcrew.domain.revisions import Sha256DigestText


class CleanupStatus(StrEnum):
    CLEANED = "CLEANED"
    ALREADY_CLEANED = "ALREADY_CLEANED"


class CleanupObservationKind(StrEnum):
    BOTH_ABSENT = "BOTH_ABSENT"
    BOTH_EXACT_LOCKED = "BOTH_EXACT_LOCKED"
    BOTH_EXACT_UNLOCKED = "BOTH_EXACT_UNLOCKED"
    PATH_ONLY_EXACT_GITFILE = "PATH_ONLY_EXACT_GITFILE"
    ADMIN_ONLY_EXACT = "ADMIN_ONLY_EXACT"
    CONFLICT = "CONFLICT"


@dataclass(frozen=True, slots=True)
class CleanupObservation:
    kind: CleanupObservationKind
    reservation_id: str
    path_identity_digest: str | None = None
    gitfile_digest: Sha256DigestText | None = None
    admin_identity_digest: Sha256DigestText | None = None
    lock_digest: Sha256DigestText | None = None


@dataclass(frozen=True, slots=True)
class Tombstone:
    run_id: str
    reservation_id: str


@dataclass(frozen=True, slots=True)
class CleanupResult:
    status: CleanupStatus
    tombstone: Tombstone


@dataclass(slots=True)
class ReservationCleanup:
    run_id: str
    reservation_id: str
    _cleaned: bool = False

    def settle(self, *, observed_exists: bool | None, cleanup_authorized: bool) -> CleanupResult:
        if self._cleaned:
            return CleanupResult(
                CleanupStatus.ALREADY_CLEANED,
                Tombstone(self.run_id, self.reservation_id),
            )
        if observed_exists is None:
            raise ValueError("CLEANUP_STATE_UNOBSERVABLE")
        if observed_exists and not cleanup_authorized:
            raise ValueError("CLEANUP_AUTHORITY_REQUIRED")
        self._cleaned = True
        return CleanupResult(
            CleanupStatus.CLEANED,
            Tombstone(self.run_id, self.reservation_id),
        )
