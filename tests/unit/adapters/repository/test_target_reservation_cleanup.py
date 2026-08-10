from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from apexcrew.adapters.repository.git import (
    NoFollowTargetReservationWorktreeGuard,
    RepositoryEffectError,
    reservation_for_operation,
)
from apexcrew.adapters.repository.no_follow_posix import PosixNoFollowBackend
from apexcrew.adapters.repository.no_follow_windows import WindowsNoFollowBackend
from apexcrew.adapters.system import ReservationPathInspector
from apexcrew.domain.admission import TargetReservationObservationService
from apexcrew.domain.effects import TargetReservation


def real_git_reservation_adapter(
    tmp_path: Path,
) -> tuple[object, object, object, object, object]:
    git_executable = shutil.which("git")
    assert git_executable is not None
    repository_root = tmp_path / "repository"
    subprocess.run(
        (git_executable, "init", "--quiet", "--initial-branch=main", str(repository_root)),
        check=True,
    )
    subprocess.run(
        (
            git_executable,
            "-C",
            str(repository_root),
            "-c",
            "user.name=ApexCrew Test",
            "-c",
            "user.email=apexcrew@example.invalid",
            "commit",
            "--quiet",
            "--allow-empty",
            "-m",
            "base",
        ),
        check=True,
    )
    base_oid = subprocess.run(
        (git_executable, "-C", str(repository_root), "rev-parse", "HEAD"),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(
        (git_executable, "-C", str(repository_root), "checkout", "--quiet", "--detach"),
        check=True,
    )
    from apexcrew.adapters.repository.git import (
        GitCommandRunner,
        GitRepositoryPreflight,
        GitTargetReservationRepository,
    )
    from apexcrew.adapters.repository.no_follow import StableHandleTree
    from apexcrew.domain.admission import TargetReservationOperation
    from apexcrew.domain.revisions import Sha256DigestText
    from apexcrew.domain.types import GitOid, IntentId, RepositoryId, RunId

    repository = GitRepositoryPreflight().inspect(repository_root)
    data_root = tmp_path / "data"
    (data_root / "reservations").mkdir(parents=True)
    trusted_empty = tmp_path / "trusted-empty"
    trusted_empty.mkdir()
    backend = WindowsNoFollowBackend() if os.name == "nt" else PosixNoFollowBackend()
    data_handles = StableHandleTree(data_root, backend)
    guard = NoFollowTargetReservationWorktreeGuard(
        repository, data_root, data_handles, repository_root
    )
    adapter = GitTargetReservationRepository(
        repository,
        RepositoryId("repository-1"),
        Sha256DigestText("sha256:" + "a" * 64),
        GitCommandRunner(Path(git_executable), trusted_empty),
        guard,
        data_root,
        Sha256DigestText("sha256:" + "c" * 64),
    )
    operation = TargetReservationOperation(
        intent_id=IntentId("intent-reservation-1"),
        run_id=RunId("run-1"),
        reservation_id="reservation-1",
        kind="ADD_NO_CHECKOUT",
        repository_id=RepositoryId("repository-1"),
        repository_instance_digest=Sha256DigestText("sha256:" + "a" * 64),
        target_ref="refs/heads/main",
        pinned_target_oid=GitOid(base_oid),
        reservation_path=str(data_root / "reservations" / "reservation-1"),
        target_authority_digest=Sha256DigestText("sha256:" + "c" * 64),
        lock_reason="run-1",
    )
    return adapter, operation, data_handles, GitOid(base_oid), repository


def _registered_reservation(adapter: object, operation: object) -> TargetReservation:
    guard = adapter._worktree_guard  # type: ignore[attr-defined]
    reservation = reservation_for_operation(operation)  # type: ignore[arg-type]
    observation = guard.require_compatible_observation(reservation)
    return replace(
        reservation,
        phase="REGISTERED_LOCKED",
        admin_entry_name=observation.admin_entry_name,
        admin_binding_digest=observation.admin_binding_digest,
    )


def test_exact_path_only_cleanup_removes_only_gitfile_and_empty_directory(
    tmp_path: Path,
) -> None:
    adapter, add, data_handles, _, repository = real_git_reservation_adapter(tmp_path)
    try:
        adapter.apply(add)
        reservation = reservation_for_operation(add)
        backend = WindowsNoFollowBackend() if os.name == "nt" else PosixNoFollowBackend()
        inspector = ReservationPathInspector(
            tmp_path / "data",
            lambda item: (
                b"gitdir: "
                + os.fsencode(
                    (repository.root / ".git" / "worktrees" / item.reservation_id).as_posix()
                )
                + b"\n"
            ),
            backend,
        )
        observation = inspector.observe_path(reservation)
        adapter._worktree_guard.release_cached_reservation(reservation)  # type: ignore[attr-defined]
        inspector.remove_exact_gitfile(reservation, observation)
        assert not reservation.path.exists()
        assert (repository.root / ".git" / "worktrees" / reservation.reservation_id).is_dir()
    finally:
        repository.close()
        data_handles.close()


def test_exact_admin_only_cleanup_removes_only_bound_admin_entry(tmp_path: Path) -> None:
    adapter, add, data_handles, _, repository = real_git_reservation_adapter(tmp_path)
    try:
        adapter.apply(add)
        lock = add.model_copy(update={"kind": "LOCK"})
        adapter.apply(lock)
        reservation = _registered_reservation(adapter, lock)
        guard: NoFollowTargetReservationWorktreeGuard = adapter._worktree_guard  # type: ignore[attr-defined]
        lock_digest = guard.require_compatible_observation(reservation).lock_digest
        adapter._worktree_guard.release_cached_reservation(reservation)  # type: ignore[attr-defined]
        reservation.path.joinpath(".git").unlink()
        reservation.path.rmdir()
        guard.remove_exact_admin_entry(reservation, reservation.admin_binding_digest, lock_digest)
        assert not (repository.root / ".git" / "worktrees" / reservation.reservation_id).exists()
    finally:
        repository.close()
        data_handles.close()


def test_altered_admin_digest_deletes_nothing(tmp_path: Path) -> None:
    adapter, add, data_handles, _, repository = real_git_reservation_adapter(tmp_path)
    try:
        adapter.apply(add)
        lock = add.model_copy(update={"kind": "LOCK"})
        adapter.apply(lock)
        reservation = _registered_reservation(adapter, lock)
        guard: NoFollowTargetReservationWorktreeGuard = adapter._worktree_guard  # type: ignore[attr-defined]
        lock_digest = guard.require_compatible_observation(reservation).lock_digest
        adapter._worktree_guard.release_cached_reservation(reservation)  # type: ignore[attr-defined]
        head = repository.root / ".git" / "worktrees" / reservation.reservation_id / "HEAD"
        head.write_bytes(b"ref: refs/heads/other\n")
        reservation.path.joinpath(".git").unlink()
        reservation.path.rmdir()
        with pytest.raises(RepositoryEffectError):
            guard.remove_exact_admin_entry(
                reservation, reservation.admin_binding_digest, lock_digest
            )
        assert (repository.root / ".git" / "worktrees" / reservation.reservation_id).exists()
    finally:
        repository.close()
        data_handles.close()


def test_mixed_cleanup_conflict_deletes_nothing(tmp_path: Path) -> None:
    adapter, add, data_handles, _, repository = real_git_reservation_adapter(tmp_path)
    try:
        adapter.apply(add)
        lock = add.model_copy(update={"kind": "LOCK"})
        adapter.apply(lock)
        reservation = _registered_reservation(adapter, lock)
        extra = reservation.path / "unexpected"
        extra.write_bytes(b"do not delete")
        guard: NoFollowTargetReservationWorktreeGuard = adapter._worktree_guard  # type: ignore[attr-defined]
        lock_digest = guard.require_compatible_observation(reservation).lock_digest
        adapter._worktree_guard.release_cached_reservation(reservation)  # type: ignore[attr-defined]
        inspector = ReservationPathInspector(
            tmp_path / "data",
            lambda item: (
                b"gitdir: "
                + os.fsencode(
                    (repository.root / ".git" / "worktrees" / item.reservation_id).as_posix()
                )
                + b"\n"
            ),
            WindowsNoFollowBackend() if os.name == "nt" else PosixNoFollowBackend(),
        )
        observed = TargetReservationObservationService(adapter, inspector).observe(reservation)
        assert observed.observable
        assert not observed.exact_identity
        with pytest.raises(RepositoryEffectError):
            guard.remove_exact_admin_entry(
                reservation, reservation.admin_binding_digest, lock_digest
            )
        assert extra.exists()
        assert (repository.root / ".git" / "worktrees" / reservation.reservation_id).exists()
    finally:
        repository.close()
        data_handles.close()


@pytest.mark.skipif(os.name == "nt", reason="requires unprivileged POSIX symlink support")
def test_unobservable_cleanup_preserves_terminal_run(tmp_path: Path) -> None:
    adapter, add, data_handles, _, repository = real_git_reservation_adapter(tmp_path)
    try:
        adapter.apply(add)
        lock = add.model_copy(update={"kind": "LOCK"})
        adapter.apply(lock)
        reservation = _registered_reservation(adapter, lock)
        guard: NoFollowTargetReservationWorktreeGuard = adapter._worktree_guard  # type: ignore[attr-defined]
        lock_digest = guard.require_compatible_observation(reservation).lock_digest
        adapter._worktree_guard.release_cached_reservation(reservation)  # type: ignore[attr-defined]
        outside = tmp_path / "outside"
        outside.write_bytes(b"outside")
        reservation.path.joinpath(".git").unlink()
        reservation.path.joinpath(".git").symlink_to(outside)
        inspector = ReservationPathInspector(
            tmp_path / "data",
            lambda item: (
                b"gitdir: "
                + os.fsencode(
                    (repository.root / ".git" / "worktrees" / item.reservation_id).as_posix()
                )
                + b"\n"
            ),
            PosixNoFollowBackend(),
        )
        observed = TargetReservationObservationService(adapter, inspector).observe(reservation)
        assert not observed.observable
        with pytest.raises(RepositoryEffectError):
            guard.remove_exact_admin_entry(
                reservation, reservation.admin_binding_digest, lock_digest
            )
        assert reservation.path.joinpath(".git").is_symlink()
        assert (repository.root / ".git" / "worktrees" / reservation.reservation_id).exists()
    finally:
        reservation_path = tmp_path / "data" / "reservations" / "reservation-1"
        if reservation_path.joinpath(".git").is_symlink():
            reservation_path.joinpath(".git").unlink()
        if reservation_path.is_dir():
            reservation_path.rmdir()
        repository.close()
        data_handles.close()
