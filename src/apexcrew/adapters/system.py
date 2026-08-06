from __future__ import annotations

import os
import time
from collections.abc import Callable
from hashlib import sha256
from pathlib import Path

from apexcrew.adapters.repository.no_follow import (
    NoFollowBackend,
    RepositoryUnsafeError,
    StableHandleTree,
)
from apexcrew.adapters.repository.no_follow_posix import PosixNoFollowBackend
from apexcrew.adapters.repository.no_follow_windows import WindowsNoFollowBackend
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

    @staticmethod
    def _identity_digest(node: object) -> str:
        identity = node.identity  # type: ignore[attr-defined]
        value = f"{identity.platform}:{identity.volume}:{identity.file_id}:{identity.kind}"
        return "sha256:" + sha256(value.encode("utf-8")).hexdigest()

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
                path_identity=self._identity_digest(directory),
            )
        except RepositoryUnsafeError:
            return ReservationPathObservation(True, False, False, False)
        finally:
            tree.close()

    def remove_exact_gitfile(
        self, reservation: TargetReservation, expected: ReservationPathObservation
    ) -> None:
        expected_path = self._data_root / "reservations" / reservation.reservation_id
        if reservation.path != expected_path:
            raise RepositoryUnsafeError("TARGET_RESERVATION_PATH_BINDING_INVALID")
        if (
            not expected.observable
            or not expected.path_present
            or not expected.gitfile_only
            or not expected.exact_back_reference
            or expected.gitfile_digest is None
            or expected.path_identity is None
        ):
            raise RepositoryUnsafeError("TARGET_RESERVATION_PATH_PRESTATE_NOT_EXACT")
        backend = (
            WindowsNoFollowBackend(allow_delete_share=True)
            if os.name == "nt"
            else PosixNoFollowBackend()
        )
        tree = StableHandleTree(self._data_root, backend)
        try:
            relative = f"reservations/{reservation.reservation_id}"
            directory = tree.open(relative, "directory")
            if tree.list_names(directory, 2) != (".git",):
                raise RepositoryUnsafeError("TARGET_RESERVATION_PATH_PRESTATE_NOT_EXACT")
            if self._identity_digest(directory) != expected.path_identity:
                raise RepositoryUnsafeError("TARGET_RESERVATION_PATH_PRESTATE_CHANGED")
            gitfile = tree.open(relative + "/.git", "file")
            raw = tree.read_bytes(gitfile, 4_096)
            digest = Sha256DigestText("sha256:" + sha256(raw).hexdigest())
            if raw != self._expected_gitfile(reservation) or digest != expected.gitfile_digest:
                raise RepositoryUnsafeError("TARGET_RESERVATION_PATH_PRESTATE_CHANGED")
            tree.remove_file(relative + "/.git", gitfile.identity)
            if tree.list_names(directory, 1) != ():
                raise RepositoryUnsafeError("TARGET_RESERVATION_PATH_POSTSTATE_NOT_EMPTY")
            tree.remove_directory(relative, directory.identity)
        finally:
            tree.close()
