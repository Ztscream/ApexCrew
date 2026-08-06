from __future__ import annotations

import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from apexcrew.adapters.repository.detached_workspace import (
    DetachedWorkspace,
    DetachedWorkspaceError,
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
from apexcrew.domain.authority import WorkspaceLease
from apexcrew.domain.plan import GlobPattern
from apexcrew.domain.policy import SecretPathPolicy
from apexcrew.domain.types import AttemptId, GitOid, RunId, TaskId


def _git(root: Path, *argv: str) -> str:
    result = subprocess.run(
        ("git", *argv),
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _active_lease() -> WorkspaceLease:
    return WorkspaceLease(
        lease_id="lease-1",
        run_id=RunId("run-1"),
        task_id=TaskId("task-1"),
        attempt_id=AttemptId("attempt-1"),
        generation=1,
        base_head="1" * 40,
        admissible_head="1" * 40,
        task_contract_digest="sha256:" + "1" * 64,
        write_globs=(GlobPattern.parse("src/**"),),
        sensitivity_globs=(),
        issued_at=datetime(2026, 8, 2, 8, 0, tzinfo=UTC),
        expires_at=datetime(2026, 8, 2, 8, 15, tzinfo=UTC),
        state="ACTIVE",
    )


def _workspace(root: Path, secret_rules: tuple[str, ...] = ()) -> DetachedWorkspace:
    repository = GitRepositoryPreflight().inspect(root)
    executable_name = shutil.which("git")
    assert executable_name is not None
    executable = Path(executable_name).resolve()
    runner = GitCommandRunner(executable)
    workspace = DetachedWorkspace(
        repository,
        runner,
        root / ".apexcrew-workspace",
        SecretPathPolicy.from_host_rules(secret_rules, b"k" * 32),
    )
    return workspace


def test_materialization_is_pinned_and_excludes_secret_paths(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "ApexCrew Test")
    _git(root, "config", "user.email", "apexcrew@example.test")
    (root / "src").mkdir()
    (root / "src" / "task.py").write_text("value = 1\n", encoding="utf-8")
    (root / "private").mkdir()
    (root / "private" / "token.txt").write_text("do-not-materialize\n", encoding="utf-8")
    _git(root, "add", "--all")
    _git(root, "commit", "-qm", "initial")

    workspace = _workspace(root, ("private/**",))
    try:
        workspace.ensure_materialized(GitOid(_git(root, "rev-parse", "HEAD^{tree}")))

        assert (
            workspace.root.joinpath("src", "task.py").read_text(encoding="utf-8") == "value = 1\n"
        )
        assert not workspace.root.joinpath("private", "token.txt").exists()
        assert not workspace.root.joinpath(".git").exists()
        assert tuple(entry.path for entry in workspace.snapshot().entries()) == ("src/task.py",)
    finally:
        workspace._repository.close()


def test_patch_changes_detached_workspace_only(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "ApexCrew Test")
    _git(root, "config", "user.email", "apexcrew@example.test")
    (root / "src").mkdir()
    (root / "src" / "task.py").write_text("value = 1\n", encoding="utf-8")
    _git(root, "add", "--all")
    _git(root, "commit", "-qm", "initial")

    workspace = _workspace(root)
    try:
        workspace.ensure_materialized(GitOid(_git(root, "rev-parse", "HEAD^{tree}")))
        result = workspace.apply_patch(
            _active_lease(), {"src/task.py": b"@@ -1 +1 @@\n-value = 1\n+value = 2\n"}
        )

        assert result.code == "PATCH_APPLIED"
        assert (
            workspace.root.joinpath("src", "task.py").read_text(encoding="utf-8") == "value = 2\n"
        )
        assert _git(root, "show", "HEAD:src/task.py") == "value = 1"
    finally:
        workspace._repository.close()


def test_non_regular_tree_entry_is_rejected_before_materialization(tmp_path: Path) -> None:
    class FakeRunner:
        def run_bytes(
            self, _repository: RepositoryInstance, operation: GitOperation
        ) -> subprocess.CompletedProcess[bytes]:
            if isinstance(operation, GitLsTreeRecursive):
                return subprocess.CompletedProcess(
                    [], 0, b"120000 blob " + b"1" * 40 + b"\tlink\0", b""
                )
            if isinstance(operation, GitCatFileSize):
                return subprocess.CompletedProcess([], 0, b"4\n", b"")
            if isinstance(operation, GitCatFileBlob):
                return subprocess.CompletedProcess([], 0, b"link", b"")
            raise AssertionError(operation)

    workspace = DetachedWorkspace(
        cast(RepositoryInstance, object()),
        FakeRunner(),
        tmp_path / "workspace",
        SecretPathPolicy.from_host_rules((), b"k" * 32),
    )

    with pytest.raises(DetachedWorkspaceError, match="WORKSPACE_REGULAR_FILE_REQUIRED"):
        workspace.ensure_materialized(GitOid("1" * 40))
    assert not workspace.root.joinpath("link").exists()


def test_partial_materialization_is_not_reused(tmp_path: Path) -> None:
    class FakeRunner:
        def run_bytes(
            self, _repository: RepositoryInstance, operation: GitOperation
        ) -> subprocess.CompletedProcess[bytes]:
            if isinstance(operation, GitLsTreeRecursive):
                return subprocess.CompletedProcess(
                    [],
                    0,
                    b"100644 blob " + b"1" * 40 + b"\ta.txt\0"
                    b"100644 blob " + b"2" * 40 + b"\tb.txt\0",
                    b"",
                )
            if isinstance(operation, GitCatFileSize):
                return subprocess.CompletedProcess([], 0, b"1\n", b"")
            if isinstance(operation, GitCatFileBlob):
                return subprocess.CompletedProcess([], 0, operation.blob_oid.encode()[:1], b"")
            raise AssertionError(operation)

    root = tmp_path / "workspace"
    root.mkdir()
    (root / "a.txt").write_bytes(b"1")
    workspace = DetachedWorkspace(
        cast(RepositoryInstance, object()),
        FakeRunner(),
        root,
        SecretPathPolicy.from_host_rules((), b"k" * 32),
    )

    with pytest.raises(DetachedWorkspaceError, match="WORKSPACE_MATERIALIZATION_INCOMPLETE"):
        workspace.ensure_materialized(GitOid("1" * 40))
