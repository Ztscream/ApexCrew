from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import cast

import pytest

from apexcrew.adapters.repository.attempt_workspace import (
    AttemptWorkspaceAdapter,
    AttemptWorkspaceError,
    MaterializedWorkspace,
)
from apexcrew.adapters.repository.git import (
    GitCatFileBlob,
    GitCatFileSize,
    GitCommandRunner,
    GitLsTreeRecursive,
    GitOperation,
    GitRepositoryPreflight,
    RepositoryInstance,
)
from apexcrew.domain.plan import GlobPattern
from apexcrew.domain.policy import SecretPathPolicy
from apexcrew.domain.types import AttemptId, GitOid


def _git(root: Path, *argv: str) -> str:
    result = subprocess.run(
        ("git", *argv),
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _repository(tmp_path: Path, *, symlink: bool = False) -> tuple[Path, GitOid]:
    root = tmp_path / "reservation-worktree"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "ApexCrew Test")
    _git(root, "config", "user.email", "apexcrew@example.test")
    (root / "src").mkdir()
    (root / "src" / "read.py").write_bytes(b"read = 1\n")
    (root / "src" / "dependency.py").write_bytes(b"dependency = 1\n")
    (root / "src" / "write.py").write_bytes(b"write = 1\n")
    (root / "tests").mkdir()
    (root / "tests" / "check.py").write_bytes(b"assert True\n")
    (root / "private").mkdir()
    (root / "private" / "token.key").write_bytes(b"private\n")
    if symlink:
        (root / "src" / "link").symlink_to("read.py")
    _git(root, "add", "--all")
    _git(root, "commit", "-qm", "initial")
    return root, GitOid(_git(root, "rev-parse", "HEAD^{tree}"))


def _adapter(
    root: Path, tmp_path: Path, *, secret_rules: tuple[str, ...] = ()
) -> AttemptWorkspaceAdapter:
    executable_name = shutil.which("git")
    assert executable_name is not None
    executable = Path(executable_name).resolve()
    repository = GitRepositoryPreflight().inspect(root)
    return AttemptWorkspaceAdapter(
        repository=repository,
        runner=GitCommandRunner(executable),
        data_root=tmp_path / "data",
        secret_paths=SecretPathPolicy.from_host_rules(secret_rules, b"k" * 32),
    )


def _close(adapter: AttemptWorkspaceAdapter) -> None:
    adapter.close()


def _paths(workspace: MaterializedWorkspace) -> tuple[str, ...]:
    return tuple(entry.path for entry in workspace.entries)


def test_context_materializes_read_and_dependency_union(tmp_path: Path) -> None:
    root, base_oid = _repository(tmp_path)
    adapter = _adapter(root, tmp_path)
    try:
        workspace = adapter.materialize_context(
            attempt_id=AttemptId("attempt-1"),
            base_oid=base_oid,
            read_globs=(GlobPattern.parse("src/read.py"),),
            dependency_globs=(GlobPattern.parse("src/dependency.py"),),
        )

        assert _paths(workspace) == ("src/dependency.py", "src/read.py")
        assert (workspace.root / "src" / "read.py").read_bytes() == b"read = 1\n"
        assert (workspace.root / "src" / "dependency.py").read_bytes() == b"dependency = 1\n"
        assert not (workspace.root / "src" / "write.py").exists()
    finally:
        _close(adapter)


def test_check_materializes_input_and_write_union(tmp_path: Path) -> None:
    root, base_oid = _repository(tmp_path)
    adapter = _adapter(root, tmp_path)
    try:
        workspace = adapter.materialize_check(
            attempt_id=AttemptId("attempt-1"),
            base_oid=base_oid,
            input_globs=(GlobPattern.parse("tests/**"),),
            write_globs=(GlobPattern.parse("src/write.py"),),
        )

        assert _paths(workspace) == ("src/write.py", "tests/check.py")
        assert (workspace.root / "src" / "write.py").read_bytes() == b"write = 1\n"
        assert (workspace.root / "tests" / "check.py").read_bytes() == b"assert True\n"
        assert not (workspace.root / "src" / "read.py").exists()
    finally:
        _close(adapter)


def test_context_and_check_digests_are_not_interchangeable(tmp_path: Path) -> None:
    root, base_oid = _repository(tmp_path)
    adapter = _adapter(root, tmp_path)
    try:
        context = adapter.materialize_context(
            attempt_id=AttemptId("attempt-1"),
            base_oid=base_oid,
            read_globs=(GlobPattern.parse("src/read.py"),),
            dependency_globs=(),
        )
        check = adapter.materialize_check(
            attempt_id=AttemptId("attempt-1"),
            base_oid=base_oid,
            input_globs=(GlobPattern.parse("tests/**"),),
            write_globs=(GlobPattern.parse("src/write.py"),),
        )

        assert context.root != check.root
        assert context.tree_digest != check.tree_digest
    finally:
        _close(adapter)


def test_glob_scope_enforced(tmp_path: Path) -> None:
    root, base_oid = _repository(tmp_path)
    adapter = _adapter(root, tmp_path)
    try:
        workspace = adapter.materialize_context(
            attempt_id=AttemptId("attempt-1"),
            base_oid=base_oid,
            read_globs=(GlobPattern.parse("src/read.py"),),
            dependency_globs=(),
        )

        assert _paths(workspace) == ("src/read.py",)
        assert not (workspace.root / "src" / "dependency.py").exists()
        assert not (workspace.root / "private" / "token.key").exists()
    finally:
        _close(adapter)


def test_scope_filter_precedes_mode_secret_and_blob_inspection(tmp_path: Path) -> None:
    class RecordingRunner:
        def __init__(self) -> None:
            self.calls: list[GitOperation] = []

        def run_bytes(
            self, _repository: RepositoryInstance, operation: GitOperation
        ) -> subprocess.CompletedProcess[bytes]:
            self.calls.append(operation)
            if isinstance(operation, GitLsTreeRecursive):
                return subprocess.CompletedProcess(
                    [],
                    0,
                    b"999999 weird " + b"not-an-oid" + b"\toutside.txt\0"
                    b"100644 blob " + b"1" * 40 + b"\tsrc/read.py\0",
                    b"",
                )
            if isinstance(operation, GitCatFileSize):
                return subprocess.CompletedProcess([], 0, b"5\n", b"")
            if isinstance(operation, GitCatFileBlob):
                return subprocess.CompletedProcess([], 0, b"read\n", b"")
            raise AssertionError(operation)

    runner = RecordingRunner()
    adapter = AttemptWorkspaceAdapter(
        cast(RepositoryInstance, object()),
        runner,
        tmp_path / "data",
        SecretPathPolicy.from_host_rules(("outside.txt",), b"k" * 32),
    )
    try:
        workspace = adapter.materialize_context(
            attempt_id=AttemptId("attempt-1"),
            base_oid=GitOid("a" * 40),
            read_globs=(GlobPattern.parse("src/read.py"),),
            dependency_globs=(),
        )

        assert _paths(workspace) == ("src/read.py",)
        assert runner.calls == [
            GitLsTreeRecursive(GitOid("a" * 40)),
            GitCatFileSize(GitOid("1" * 40)),
            GitCatFileBlob(GitOid("1" * 40)),
        ]
    finally:
        _close(adapter)


def test_casefold_path_collision_is_rejected_before_blob_reads(tmp_path: Path) -> None:
    class RecordingRunner:
        def __init__(self) -> None:
            self.calls: list[GitOperation] = []

        def run_bytes(
            self, _repository: RepositoryInstance, operation: GitOperation
        ) -> subprocess.CompletedProcess[bytes]:
            self.calls.append(operation)
            if isinstance(operation, GitLsTreeRecursive):
                return subprocess.CompletedProcess(
                    [],
                    0,
                    b"100644 blob " + b"1" * 40 + b"\tsrc/Foo.py\0"
                    b"100644 blob " + b"2" * 40 + b"\tsrc/foo.py\0",
                    b"",
                )
            raise AssertionError(operation)

    runner = RecordingRunner()
    adapter = AttemptWorkspaceAdapter(
        cast(RepositoryInstance, object()),
        runner,
        tmp_path / "data",
        SecretPathPolicy.from_host_rules((), b"k" * 32),
    )
    try:
        with pytest.raises(AttemptWorkspaceError, match="CASEFOLD_PATH_COLLISION"):
            adapter.materialize_context(
                attempt_id=AttemptId("attempt-1"),
                base_oid=GitOid("a" * 40),
                read_globs=(GlobPattern.parse("src/**"),),
                dependency_globs=(),
            )
        assert runner.calls == [GitLsTreeRecursive(GitOid("a" * 40))]
    finally:
        _close(adapter)


def test_submodule_mode_is_rejected_in_scope(tmp_path: Path) -> None:
    class RecordingRunner:
        def run_bytes(
            self, _repository: RepositoryInstance, operation: GitOperation
        ) -> subprocess.CompletedProcess[bytes]:
            if isinstance(operation, GitLsTreeRecursive):
                return subprocess.CompletedProcess(
                    [], 0, b"160000 commit " + b"1" * 40 + b"\tsrc/module\0", b""
                )
            raise AssertionError(operation)

    adapter = AttemptWorkspaceAdapter(
        cast(RepositoryInstance, object()),
        RecordingRunner(),
        tmp_path / "data",
        SecretPathPolicy.from_host_rules((), b"k" * 32),
    )
    try:
        with pytest.raises(AttemptWorkspaceError, match="SUBMODULE_MODE_DENIED"):
            adapter.materialize_context(
                attempt_id=AttemptId("attempt-1"),
                base_oid=GitOid("a" * 40),
                read_globs=(GlobPattern.parse("src/**"),),
                dependency_globs=(),
            )
    finally:
        _close(adapter)


def test_blob_size_is_checked_before_blob_read(tmp_path: Path) -> None:
    class RecordingRunner:
        def __init__(self) -> None:
            self.calls: list[GitOperation] = []

        def run_bytes(
            self, _repository: RepositoryInstance, operation: GitOperation
        ) -> subprocess.CompletedProcess[bytes]:
            self.calls.append(operation)
            if isinstance(operation, GitLsTreeRecursive):
                return subprocess.CompletedProcess(
                    [], 0, b"100644 blob " + b"1" * 40 + b"\tsrc/large.py\0", b""
                )
            if isinstance(operation, GitCatFileSize):
                return subprocess.CompletedProcess([], 0, b"16777217\n", b"")
            raise AssertionError(operation)

    runner = RecordingRunner()
    adapter = AttemptWorkspaceAdapter(
        cast(RepositoryInstance, object()),
        runner,
        tmp_path / "data",
        SecretPathPolicy.from_host_rules((), b"k" * 32),
    )
    try:
        with pytest.raises(AttemptWorkspaceError, match="WORKSPACE_FILE_TOO_LARGE"):
            adapter.materialize_context(
                attempt_id=AttemptId("attempt-1"),
                base_oid=GitOid("a" * 40),
                read_globs=(GlobPattern.parse("src/**"),),
                dependency_globs=(),
            )
        assert runner.calls == [
            GitLsTreeRecursive(GitOid("a" * 40)),
            GitCatFileSize(GitOid("1" * 40)),
        ]
    finally:
        _close(adapter)


def test_secret_path_rejected_without_echoing_path(tmp_path: Path) -> None:
    root, base_oid = _repository(tmp_path)
    adapter = _adapter(root, tmp_path, secret_rules=("private/**",))
    try:
        with pytest.raises(AttemptWorkspaceError) as raised:
            adapter.materialize_context(
                attempt_id=AttemptId("attempt-1"),
                base_oid=base_oid,
                read_globs=(GlobPattern.parse("private/**"),),
                dependency_globs=(),
            )
        assert "SECRET_PATH_DENIED" in str(raised.value)
        assert "private/token.key" not in str(raised.value)
    finally:
        _close(adapter)


@pytest.mark.skipif(os.name == "nt", reason="Git symlink entries are not portable on Windows")
def test_symlink_mode_rejected(tmp_path: Path) -> None:
    root, base_oid = _repository(tmp_path, symlink=True)
    adapter = _adapter(root, tmp_path)
    try:
        with pytest.raises(AttemptWorkspaceError, match="SYMLINK_MODE_DENIED"):
            adapter.materialize_context(
                attempt_id=AttemptId("attempt-1"),
                base_oid=base_oid,
                read_globs=(GlobPattern.parse("src/**"),),
                dependency_globs=(),
            )
    finally:
        _close(adapter)


def test_materialize_is_idempotent_after_partial_write(tmp_path: Path) -> None:
    root, base_oid = _repository(tmp_path)
    adapter = _adapter(root, tmp_path)
    partial = tmp_path / "data" / "attempts" / "attempt-1" / "context"
    partial.mkdir(parents=True)
    (partial / "partial.txt").write_bytes(b"crash residue")
    try:
        first = adapter.materialize_context(
            attempt_id=AttemptId("attempt-1"),
            base_oid=base_oid,
            read_globs=(GlobPattern.parse("src/read.py"),),
            dependency_globs=(),
        )
        second = adapter.materialize_context(
            attempt_id=AttemptId("attempt-1"),
            base_oid=base_oid,
            read_globs=(GlobPattern.parse("src/read.py"),),
            dependency_globs=(),
        )

        assert first.tree_digest == second.tree_digest
        assert not (first.root / "partial.txt").exists()
        assert _paths(second) == ("src/read.py",)
    finally:
        _close(adapter)


def test_reservation_worktree_is_not_written(tmp_path: Path) -> None:
    root, base_oid = _repository(tmp_path)
    before = {
        "read": (root / "src" / "read.py").read_bytes(),
        "status": _git(root, "status", "--porcelain=v1", "--untracked-files=all"),
    }
    adapter = _adapter(root, tmp_path)
    try:
        adapter.materialize_check(
            attempt_id=AttemptId("attempt-1"),
            base_oid=base_oid,
            input_globs=(GlobPattern.parse("tests/**"),),
            write_globs=(GlobPattern.parse("src/write.py"),),
        )
        assert (root / "src" / "read.py").read_bytes() == before["read"]
        assert _git(root, "status", "--porcelain=v1", "--untracked-files=all") == before["status"]
        assert not (root / "context").exists()
        assert not (root / "check").exists()
    finally:
        _close(adapter)
