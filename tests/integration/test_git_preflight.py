import os
import shutil
import struct
import subprocess
from collections.abc import Mapping
from hashlib import sha1
from pathlib import Path

import pytest
from helpers.git_repository import (  # type: ignore[import-not-found]
    make_git_repository,
    make_git_repository_with_linked_worktree,
)

from apexcrew.adapters.repository.git import (
    MAX_GIT_CONFIG_BYTES,
    GitCommandRunner,
    GitConfigEntry,
    GitRepositoryPreflight,
    GitStatusPorcelain,
    GitTargetReservationRepository,
    NoFollowTargetReservationWorktreeGuard,
    RepositoryUnsafeError,
    parse_git_config,
)
from apexcrew.adapters.repository.no_follow import StableHandleTree
from apexcrew.adapters.repository.no_follow_posix import PosixNoFollowBackend
from apexcrew.adapters.repository.no_follow_windows import WindowsNoFollowBackend
from apexcrew.domain.admission import RepositoryEffectError, TargetReservationOperation
from apexcrew.domain.types import GitOid, IntentId, RepositoryId, RunId


def test_byte_config_parser_supports_subsections_quotes_and_continuations() -> None:
    raw = rb"""[apex "quoted\"subsection"]
key = "first\nsecond\t\"quoted\"\\tail" \
    continued
enabled
"""
    assert parse_git_config(raw) == (
        GitConfigEntry(
            section=b"apex",
            subsection=b'quoted"subsection',
            name=b"key",
            value=b'first\nsecond\t"quoted"\\tail continued',
        ),
        GitConfigEntry(
            section=b"apex",
            subsection=b'quoted"subsection',
            name=b"enabled",
            value=b"true",
        ),
    )


@pytest.mark.parametrize(
    "raw,reason",
    [
        (b'[core]\nkey = "bad\\q"\n', "GIT_CONFIG_SYNTAX_INVALID"),
        (b"[core]\nkey = value\\", "GIT_CONFIG_SYNTAX_INVALID"),
        (b"x" * (MAX_GIT_CONFIG_BYTES + 1), "GIT_CONFIG_LIMIT_EXCEEDED"),
    ],
    ids=["invalid-escape", "trailing-continuation", "over-limit"],
)
def test_byte_config_parser_fails_closed_on_malformed_or_over_limit_input(
    raw: bytes, reason: str
) -> None:
    with pytest.raises(RepositoryUnsafeError, match=reason):
        parse_git_config(raw)


@pytest.mark.parametrize(
    "config_bytes",
    [
        b"[include]\npath = //server/share/outside.ini\n",
        rb"""[includeIf "gitdir/i:C:/work/**"]
path = "\\\\server\\share\\outside.ini"
""",
        rb"""[includeIf "onbranch:release/**"]
path = "\\\\?\\C:\\outside.ini"
""",
    ],
)
def test_preflight_rejects_include_and_include_if_before_git_runs(
    tmp_path: Path, config_bytes: bytes
) -> None:
    repo = make_git_repository(tmp_path)
    (repo / ".git" / "config").write_bytes(config_bytes)
    with pytest.raises(RepositoryUnsafeError, match="CONFIG_INCLUDE_DENIED"):
        GitRepositoryPreflight().inspect(repo)


@pytest.mark.parametrize(
    "config_bytes,reason",
    [
        (b"[extensions]\nworktreeConfig = false\n", "UNSUPPORTED_GIT_CONFIG_FEATURE"),
        (b"[core]\nsparseCheckout = false\n", "UNSUPPORTED_GIT_CONFIG_FEATURE"),
        (b"[core]\nsparseCheckoutCone = false\n", "UNSUPPORTED_GIT_CONFIG_FEATURE"),
        (b"[index]\nsparse = false\n", "UNSUPPORTED_GIT_CONFIG_FEATURE"),
        (b"[core]\nsplitIndex = false\n", "UNSUPPORTED_GIT_CONFIG_FEATURE"),
        (b"[core]\ncommitGraph = false\n", "UNSUPPORTED_GIT_CONFIG_FEATURE"),
        (b"[commitGraph]\nreadChangedPaths = false\n", "UNSUPPORTED_GIT_CONFIG_FEATURE"),
        (b"[core]\nmultiPackIndex = false\n", "UNSUPPORTED_GIT_CONFIG_FEATURE"),
        (b"[pack]\nuseBitmap = false\n", "UNSUPPORTED_GIT_CONFIG_FEATURE"),
        (b"[safe]\nbareRepository = all\n", "EXTERNAL_GIT_ROUTING_DENIED"),
        (b"[core]\nworktree = ../../outside\n", "EXTERNAL_GIT_ROUTING_DENIED"),
        (b"[core]\nhooksPath = ../../hooks\n", "EXTERNAL_GIT_ROUTING_DENIED"),
    ],
)
def test_preflight_rejects_forbidden_config_and_routing_keys(
    tmp_path: Path, config_bytes: bytes, reason: str
) -> None:
    repo = make_git_repository(tmp_path)
    (repo / ".git" / "config").write_bytes(config_bytes)
    with pytest.raises(RepositoryUnsafeError, match=reason):
        GitRepositoryPreflight().inspect(repo)


def _index_bytes(mode: int, extension: bytes = b"") -> bytes:
    path = b"tracked.txt"
    entry = (
        struct.pack(">10I", 0, 0, 0, 0, 0, 0, mode, 0, 0, 0)
        + b"\x00" * 20
        + struct.pack(">H", len(path))
        + path
        + b"\x00"
    )
    entry += b"\x00" * ((8 - len(entry) % 8) % 8)
    payload = b"DIRC" + struct.pack(">II", 2, 1) + entry + extension
    return payload + sha1(payload).digest()


@pytest.mark.parametrize(
    "index_bytes,reason",
    [
        (_index_bytes(0o040000), "SPARSE_INDEX_DENIED"),
        (_index_bytes(0o100644, b"link" + struct.pack(">I", 0)), "SPLIT_INDEX_DENIED"),
    ],
)
def test_preflight_rejects_sparse_and_split_index_bytes_before_git_runs(
    tmp_path: Path, index_bytes: bytes, reason: str
) -> None:
    repo = make_git_repository(tmp_path)
    (repo / ".git" / "index").write_bytes(index_bytes)
    with pytest.raises(RepositoryUnsafeError, match=reason):
        GitRepositoryPreflight().inspect(repo)


def test_preflight_rejects_preexisting_linked_worktree(tmp_path: Path) -> None:
    repo = make_git_repository_with_linked_worktree(tmp_path)
    with pytest.raises(RepositoryUnsafeError, match="UNSUPPORTED_LINKED_WORKTREE"):
        GitRepositoryPreflight().inspect(repo)


@pytest.mark.parametrize(
    "malformation",
    (
        "nested-log-entry",
        "logs-head-directory",
        "logs-head-link",
        "locked-while-unlocked",
        "case-collision",
    ),
)
def test_reservation_inventory_rejects_malformed_admin_tree_before_git(
    tmp_path: Path, malformation: str
) -> None:
    repository_root = make_git_repository(tmp_path)
    repository = GitRepositoryPreflight().inspect(repository_root)
    data_root = tmp_path / "data"
    reservation_path = data_root / "reservations" / "reservation-1"
    reservation_path.mkdir(parents=True)
    (reservation_path / ".git").write_bytes(
        b"gitdir: "
        + os.fsencode((repository_root / ".git" / "worktrees" / "reservation-1").as_posix())
        + b"\n"
    )
    admin = repository_root / ".git" / "worktrees" / "reservation-1"
    (admin / "logs").mkdir(parents=True)
    (admin / "refs").mkdir()
    (admin / "gitdir").write_bytes(os.fsencode((reservation_path / ".git").as_posix()) + b"\n")
    (admin / "commondir").write_bytes(b"../..\n")
    (admin / "HEAD").write_bytes(b"ref: refs/heads/main\n")
    (admin / "logs" / "HEAD").write_bytes(b"0000000000000000000000000000000000000000\n")
    if malformation == "nested-log-entry":
        (admin / "logs" / "unexpected").write_bytes(b"must not be read\n")
    elif malformation == "logs-head-directory":
        (admin / "logs" / "HEAD").unlink()
        (admin / "logs" / "HEAD").mkdir()
    elif malformation == "logs-head-link":
        if os.name == "nt":
            pytest.skip("creating links requires unavailable Windows privileges")
        (admin / "logs" / "HEAD").unlink()
        os.symlink("unexpected-target", admin / "logs" / "HEAD")
    elif malformation == "locked-while-unlocked":
        (admin / "locked").write_bytes(b"run-1\n")
    else:
        if os.name == "nt":
            pytest.skip("the Windows filesystem cannot create case-colliding entries")
        (admin / "head").write_bytes(b"ref: refs/heads/main\n")
    try:
        repository = repository.refresh_after_verified_owned_transition()
    except RepositoryUnsafeError as error:
        if malformation != "logs-head-link":
            raise
        assert str(error) == "NO_FOLLOW_OPEN_DENIED"
        repository.close()
        return
    backend = WindowsNoFollowBackend() if os.name == "nt" else PosixNoFollowBackend()
    data_handles = StableHandleTree(data_root, backend)
    operation = TargetReservationOperation(
        intent_id=IntentId("intent-reservation-1"),
        run_id=RunId("run-1"),
        reservation_id="reservation-1",
        kind="LOCK",
        repository_id=RepositoryId("repository-1"),
        repository_instance_digest="sha256:" + "a" * 64,
        target_ref="refs/heads/main",
        pinned_target_oid=GitOid("1" * 40),
        reservation_path=str(reservation_path),
        target_authority_digest="sha256:" + "c" * 64,
        lock_reason="run-1",
    )
    guard = NoFollowTargetReservationWorktreeGuard(
        repository, data_root, data_handles, repository_root
    )
    trusted_empty_dir = tmp_path / "trusted-empty"
    trusted_empty_dir.mkdir()
    spawner = RecordingGitSpawner()
    adapter = GitTargetReservationRepository(
        repository,
        RepositoryId("repository-1"),
        "sha256:" + "a" * 64,
        GitCommandRunner(absolute_git(), trusted_empty_dir, spawner),
        guard,
        data_root,
        "sha256:" + "c" * 64,
    )
    try:
        with pytest.raises(RepositoryEffectError, match="^TARGET_UNSAFE$"):
            adapter.apply(operation)
        assert spawner.calls == []
    finally:
        data_handles.close()
        repository.close()


class RecordingGitSpawner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def run(
        self,
        argv: tuple[str, ...],
        cwd: Path,
        environment: Mapping[str, str],
        *,
        text: bool,
    ) -> subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes]:
        del cwd, environment
        self.calls.append(argv)
        if text:
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.CompletedProcess(argv, 0, b"", b"")


def absolute_git() -> Path:
    executable = shutil.which("git")
    assert executable is not None
    return Path(executable).absolute()


def replace_component_with_same_kind(target: Path) -> None:
    replacement = target.with_name(f"{target.name}.replaced")
    target.rename(replacement)
    if target.name == ".git":
        target.mkdir()
    else:
        target.write_bytes(b"[core]\nrepositoryformatversion = 0\n")


@pytest.mark.skipif(
    os.name == "nt",
    reason="Windows held handles deny rename; fake backend covers replacement",
)
@pytest.mark.parametrize("relative", [".git", ".git/config"])
def test_posix_real_component_replacement_is_denied_before_git(
    tmp_path: Path, relative: str
) -> None:
    root = make_git_repository(tmp_path)
    repository = GitRepositoryPreflight().inspect(root)
    replace_component_with_same_kind(root / relative)
    spawner = RecordingGitSpawner()
    with pytest.raises(RepositoryUnsafeError, match="IDENTITY_CHANGED"):
        GitCommandRunner(absolute_git(), tmp_path / "empty", spawner).run(
            repository, GitStatusPorcelain()
        )
    assert spawner.calls == []


@pytest.mark.skipif(os.name != "nt", reason="Windows-only share-mode assertion")
def test_windows_held_final_handle_denies_replacement_before_git(tmp_path: Path) -> None:
    root = make_git_repository(tmp_path)
    repository = GitRepositoryPreflight().inspect(root)
    with pytest.raises(PermissionError):
        replace_component_with_same_kind(root / ".git" / "config")
    assert repository.config_identity.file_id != 0


@pytest.mark.parametrize(
    "raw_operation",
    (
        ("-C", "C:/outside", "status"),
        ("--git-dir=C:/outside/.git", "status"),
        ("--work-tree=C:/outside", "status"),
        ("-c", "core.worktree=C:/outside", "status"),
    ),
)
def test_runner_rejects_raw_git_route_options_before_spawn(
    tmp_path: Path, raw_operation: tuple[str, ...]
) -> None:
    repository = GitRepositoryPreflight().inspect(make_git_repository(tmp_path))
    spawner = RecordingGitSpawner()
    runner = GitCommandRunner(absolute_git(), tmp_path / "empty", spawner)
    with pytest.raises(RepositoryUnsafeError, match="RAW_GIT_ARGUMENTS_DENIED"):
        runner.run(repository, raw_operation)  # type: ignore[arg-type]
    assert spawner.calls == []


def test_closed_operation_emits_no_caller_controlled_git_route_tokens(
    tmp_path: Path,
) -> None:
    repository = GitRepositoryPreflight().inspect(make_git_repository(tmp_path))
    spawner = RecordingGitSpawner()
    runner = GitCommandRunner(absolute_git(), tmp_path / "empty", spawner)
    runner.run(repository, GitStatusPorcelain())
    assert spawner.calls == [
        (
            str(absolute_git()),
            "-c",
            f"core.hooksPath={tmp_path / 'empty' / 'hooks'}",
            "-c",
            "commit.gpgSign=false",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "diff.external=",
            "-c",
            "credential.helper=",
            "status",
            "--porcelain=v1",
            "--untracked-files=no",
        )
    ]


@pytest.mark.parametrize(
    "relative_path,content,reason",
    [
        ("objects/info/alternates", "../outside\n", "UNSUPPORTED_GIT_STORAGE"),
        ("shallow", "1" * 40 + "\n", "UNSUPPORTED_GIT_STORAGE"),
        ("shallow.lock", "sentinel\n", "UNSUPPORTED_GIT_STORAGE"),
        ("info/grafts", "1" * 40 + " 2" * 40 + "\n", "UNSUPPORTED_GIT_STORAGE"),
        ("info/sparse-checkout", "src/**\n", "SPARSE_INDEX_DENIED"),
        ("sharedindex.deadbeef", "sentinel\n", "SPLIT_INDEX_DENIED"),
        (
            "config.worktree",
            "[core]\nworktree = ../../outside\n",
            "UNSUPPORTED_GIT_CONFIG_FEATURE",
        ),
    ],
)
def test_preflight_rejects_external_or_partial_storage(
    tmp_path: Path, relative_path: str, content: str, reason: str
) -> None:
    repo = make_git_repository(tmp_path)
    planted = repo / ".git" / relative_path
    planted.parent.mkdir(parents=True, exist_ok=True)
    planted.write_text(content, encoding="utf-8")
    with pytest.raises(RepositoryUnsafeError, match=reason):
        GitRepositoryPreflight().inspect(repo)
