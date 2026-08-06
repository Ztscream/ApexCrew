from __future__ import annotations

import time
from collections.abc import Callable
from hashlib import sha256
from pathlib import Path

from apexcrew.adapters.repository.no_follow import (
    NoFollowBackend,
    RepositoryUnsafeError,
    StableHandleTree,
)
from apexcrew.domain.admission import ReservationPathObservation, TargetReservationPathReader
from apexcrew.domain.authority import MonotonicInstant
from apexcrew.domain.effects import TargetReservation
from apexcrew.domain.revisions import Sha256DigestText


class SystemMonotonicClock:
    def now(self) -> MonotonicInstant:
        return MonotonicInstant(time.monotonic_ns())


class ReservationPathInspector(TargetReservationPathReader):
    def __init__(
        self,
        data_root: Path,
        expected_gitfile: Callable[[TargetReservation], bytes],
        backend: NoFollowBackend,
    ) -> None:
        self._data_root = data_root
        self._expected_gitfile = expected_gitfile
        self._backend = backend

    def observe_path(self, reservation: TargetReservation) -> ReservationPathObservation:
        expected = self._data_root / "reservations" / reservation.reservation_id
        if reservation.path != expected:
            return ReservationPathObservation(True, False, False, True)
        tree = StableHandleTree(self._data_root, self._backend)
        try:
            relative = f"reservations/{reservation.reservation_id}"
            directory = tree.try_open(relative, "directory")
            if directory is None:
                return ReservationPathObservation(False, False, False, True)
            names = tree.list_names(directory, maximum=2)
            gitfile = tree.try_open(relative + "/.git", "file")
            if names != (".git",) or gitfile is None:
                return ReservationPathObservation(True, False, False, True)
            raw = tree.read_bytes(gitfile, maximum=4_096)
            return ReservationPathObservation(
                path_present=True,
                gitfile_only=True,
                exact_back_reference=raw == self._expected_gitfile(reservation),
                observable=True,
                gitfile_digest=Sha256DigestText("sha256:" + sha256(raw).hexdigest()),
            )
        except RepositoryUnsafeError:
            return ReservationPathObservation(True, False, False, False)
        finally:
            tree.close()
