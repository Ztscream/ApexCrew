from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from apexcrew.adapters.repository.candidate_preparation import CandidatePreparationAdapter
from apexcrew.adapters.repository.git import GitCommandRunner, GitRepositoryPreflight
from apexcrew.domain.types import AttemptId, GitOid, RunId, TaskId


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


def test_run_candidate_uses_private_head_tree_with_pinned_target_parent(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repository"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.name", "ApexCrew Test")
    _git(root, "config", "user.email", "apexcrew@example.test")
    (root / "src").mkdir()
    (root / "src" / "task.py").write_text("value = 2\n", encoding="utf-8")
    _git(root, "add", "src/task.py")
    _git(root, "commit", "-qm", "initial")
    target_base = GitOid(_git(root, "rev-parse", "HEAD"))
    _git(root, "checkout", "--detach", target_base)

    task_workspace = tmp_path / "task-workspace"
    (task_workspace / "src").mkdir(parents=True)
    (task_workspace / "src" / "task.py").write_text("value = 2\n", encoding="utf-8")
    workspace = tmp_path / "data"
    executable = shutil.which("git")
    assert executable is not None
    repository = GitRepositoryPreflight().inspect(root)
    runner = GitCommandRunner(Path(executable).resolve())
    try:
        adapter = CandidatePreparationAdapter(repository, runner, workspace)
        task_head = adapter.prepare_task_candidate(
            run_id=RunId("run-01"),
            task_id=TaskId("task-01"),
            attempt_id=AttemptId("attempt-01"),
            run_head_oid=target_base,
            workspace=task_workspace,
            changed_paths=("src/task.py",),
            message="prepare task-01",
        )
        run_head = task_head.prepared_oid
        prepared = adapter.prepare_run_candidate(
            run_id=RunId("run-01"),
            head_oid=run_head,
            target_base_oid=target_base,
            message="prepare run candidate",
        )
    finally:
        repository.close()
        runner.close()

    assert prepared != run_head
    assert _git(root, "show", "-s", "--format=%P", str(prepared)) == str(target_base)
    assert _git(root, "show", f"{prepared}:src/task.py") == "value = 2"
    assert tuple((workspace / "index" / "run-01").glob("*.idx")) == ()
