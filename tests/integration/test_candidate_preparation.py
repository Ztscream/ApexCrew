from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from apexcrew.adapters.repository.candidate_preparation import (
    CandidatePreparationAdapter,
    CandidatePreparationError,
)
from apexcrew.adapters.repository.git import (
    GitCommandRunner,
    GitHashObjectWrite,
    GitRepositoryPreflight,
)
from apexcrew.delivery.cli import _RunCommandContext
from apexcrew.delivery.cli import app as cli_app
from apexcrew.domain.admission import TaskCandidate
from apexcrew.domain.commands import ApplicableRevisionDigests
from apexcrew.domain.types import (
    AttemptId,
    AuditSequence,
    CandidateId,
    EvidenceBundleDigest,
    GitOid,
    RunId,
    TaskId,
)


def _git(root: Path, *argv: str) -> str:
    environment = os.environ.copy()
    environment.update(
        {
            "GIT_AUTHOR_DATE": "2000-01-01T00:00:00Z",
            "GIT_COMMITTER_DATE": "2000-01-01T00:00:00Z",
        }
    )
    result = subprocess.run(
        ("git", "-C", str(root), *argv),
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    return result.stdout.strip()


class _RecordingRunner:
    def __init__(self, delegate: GitCommandRunner) -> None:
        self.delegate = delegate
        self.operations: list[object] = []

    def run(self, repository, operation, *, index_file=None):  # type: ignore[no-untyped-def]
        self.operations.append(operation)
        return self.delegate.run(repository, operation, index_file=index_file)

    def run_bytes(self, repository, operation, *, index_file=None):  # type: ignore[no-untyped-def]
        self.operations.append(operation)
        return self.delegate.run_bytes(repository, operation, index_file=index_file)


def _fixture(tmp_path: Path) -> tuple[Path, Path, GitOid, _RecordingRunner, Path]:
    root = tmp_path / "repository"
    root.mkdir(parents=True)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.name", "ApexCrew Test")
    _git(root, "config", "user.email", "apexcrew@example.test")
    (root / "src").mkdir()
    (root / "src" / "task.py").write_text("value = 1\n", encoding="utf-8")
    (root / "README.md").write_text("unchanged\n", encoding="utf-8")
    (root / "obsolete.txt").write_text("remove me\n", encoding="utf-8")
    _git(root, "add", "--all")
    _git(root, "commit", "-qm", "initial")
    head = GitOid(_git(root, "rev-parse", "HEAD"))

    workspace = tmp_path / "workspace"
    (workspace / "src").mkdir(parents=True)
    (workspace / "src" / "task.py").write_text("value = 2\n", encoding="utf-8")
    data_root = tmp_path / "data"
    executable = shutil.which("git")
    assert executable is not None
    runner = _RecordingRunner(GitCommandRunner(Path(executable).resolve()))
    return root, workspace, head, runner, data_root


def _adapter(runner: _RecordingRunner, root: Path, data_root: Path) -> CandidatePreparationAdapter:
    repository = GitRepositoryPreflight().inspect(root)
    return CandidatePreparationAdapter(repository, runner, data_root)


def _prepare(
    tmp_path: Path,
    *,
    attempt_id: str = "attempt-01",
) -> tuple[Path, Path, GitOid, _RecordingRunner, TaskCandidate]:
    root, workspace, head, runner, data_root = _fixture(tmp_path)
    adapter = _adapter(runner, root, data_root)
    candidate = adapter.prepare_task_candidate(
        run_id=RunId("run-01"),
        task_id=TaskId("task-01"),
        attempt_id=AttemptId(attempt_id),
        run_head_oid=head,
        workspace=workspace,
        changed_paths=("src/task.py", "obsolete.txt"),
        message="prepare task-01",
    )
    return root, workspace, head, runner, candidate


def test_prepared_commit_parent_is_run_head(tmp_path: Path) -> None:
    root, _workspace, head, _runner, candidate = _prepare(tmp_path)

    parents = _git(root, "show", "-s", "--format=%P", str(candidate.prepared_oid))

    assert isinstance(candidate, TaskCandidate)
    assert parents == str(head)
    assert _git(root, "rev-parse", "refs/heads/main") == str(head)


def test_prepared_commit_contains_patched_bytes(tmp_path: Path) -> None:
    root, _workspace, _head, _runner, candidate = _prepare(tmp_path)

    assert _git(root, "show", f"{candidate.prepared_oid}:src/task.py") == "value = 2"
    with pytest.raises(subprocess.CalledProcessError):
        _git(root, "show", f"{candidate.prepared_oid}:obsolete.txt")


def test_unchanged_blobs_reuse_base_tree(tmp_path: Path) -> None:
    root, _workspace, head, runner, candidate = _prepare(tmp_path)

    base_blob = _git(root, "rev-parse", f"{head}:README.md")
    prepared_blob = _git(root, "rev-parse", f"{candidate.prepared_oid}:README.md")
    hashed_paths = [
        operation.blob_path.name
        for operation in runner.operations
        if isinstance(operation, GitHashObjectWrite)
    ]

    assert prepared_blob == base_blob
    assert hashed_paths == ["task.py"]


def test_prepared_oid_is_deterministic_across_runs(tmp_path: Path) -> None:
    first_root, _workspace, _head, _runner, first = _prepare(tmp_path / "first")
    second_root, _workspace, _head, _runner, second = _prepare(tmp_path / "second")

    assert first_root != second_root
    assert first.prepared_oid == second.prepared_oid


def test_preparation_failure_yields_no_candidate_and_cleans_index(tmp_path: Path) -> None:
    root, workspace, head, runner, data_root = _fixture(tmp_path)
    (workspace / "src" / "task.py").unlink()
    (workspace / "src" / "task.py").mkdir()
    adapter = _adapter(runner, root, data_root)

    with pytest.raises(CandidatePreparationError, match="WORKSPACE_ENTRY_NOT_REGULAR"):
        adapter.prepare_task_candidate(
            run_id=RunId("run-01"),
            task_id=TaskId("task-01"),
            attempt_id=AttemptId("attempt-failure"),
            run_head_oid=head,
            workspace=workspace,
            changed_paths=("src/task.py",),
            message="prepare task-01",
        )

    assert (
        not list((data_root / "index").rglob("*.idx")) if (data_root / "index").exists() else True
    )


def test_preview_refuses_without_prepared_oid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = _RunCommandContext(
        run_id=RunId("run-01"),
        sequence=AuditSequence(0),
        current=ApplicableRevisionDigests(),
        approved=ApplicableRevisionDigests(),
        proposed_plan_digest=None,
        candidate_id=CandidateId("candidate-01"),
        evidence_bundle_digest=EvidenceBundleDigest("sha256:" + "b" * 64),
        candidate_head_oid=GitOid("a" * 40),
        prepared_oid=None,
    )
    monkeypatch.setattr(
        "apexcrew.delivery.cli._read_run_context",
        lambda _root, _run_id: context,
    )

    result = CliRunner().invoke(
        cli_app,
        ["integrate", str(context.run_id), "--root", str(tmp_path), "--preview"],
    )

    assert result.exit_code == 1
    assert result.stdout.strip() == (
        '{"failed_invariant": "STATE_CONFLICT", "status": "INTEGRATE_REJECTED"}'
    )
