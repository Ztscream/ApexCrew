from __future__ import annotations

import importlib
import importlib.util
import json
import sqlite3
import subprocess
from pathlib import Path

import pytest
from helpers.git_repository import (
    commit_repository,
    make_git_repository,
    make_git_repository_with_linked_worktree,
)
from typer.testing import CliRunner


def test_bootstrap_rejects_non_direct_target_ref(tmp_path: Path) -> None:
    module = (
        importlib.import_module("apexcrew.adapters.repository.bootstrap")
        if importlib.util.find_spec("apexcrew.adapters.repository.bootstrap") is not None
        else None
    )
    assert module is not None, "repository bootstrap production module is missing"
    service_type = getattr(module, "RepositoryBootstrapAuthorityService", None)
    assert service_type is not None, "repository bootstrap production symbol is missing"

    class ExplodingPreflight:
        def inspect(self, root: Path) -> object:
            raise AssertionError(f"preflight must not run: {root}")

    service = service_type(preflight=ExplodingPreflight())
    with pytest.raises(ValueError, match="direct target ref"):
        service.inspect(str(tmp_path), "refs/tags/v1")


def test_bootstrap_observes_repository_identity_and_target_oid(tmp_path: Path) -> None:
    from apexcrew.adapters.repository.bootstrap import RepositoryBootstrapAuthorityService

    root = make_git_repository(tmp_path)
    (root / "README.md").write_text("bootstrap\n", encoding="utf-8")
    (root / ".env").write_text("DEEPSEEK_API_KEY=must-not-be-read\n", encoding="utf-8")
    expected_oid = commit_repository(root, "bootstrap")
    subprocess.run(["git", "-C", str(root), "branch", "feature"], check=True)

    observed = RepositoryBootstrapAuthorityService().inspect(str(root), "refs/heads/feature")

    assert observed.repository_root == str(root)
    assert str(observed.repository_id)
    assert str(observed.repository_instance_digest).startswith("sha256:")
    assert observed.target_ref == "refs/heads/feature"
    assert observed.target_oid == expected_oid


def test_bootstrap_rejects_checked_out_target_ref(tmp_path: Path) -> None:
    from apexcrew.adapters.repository.bootstrap import (
        RepositoryBootstrapAuthorityService,
        RepositoryBootstrapError,
    )

    root = make_git_repository(tmp_path)
    (root / "README.md").write_text("bootstrap\n", encoding="utf-8")
    commit_repository(root, "bootstrap")
    subprocess.run(["git", "-C", str(root), "branch", "-M", "main"], check=True)

    with pytest.raises(RepositoryBootstrapError, match="target ref is checked out"):
        RepositoryBootstrapAuthorityService().inspect(str(root), "refs/heads/main")


@pytest.mark.parametrize(
    ("returncode", "stdout"),
    ((1, b""), (0, b"malformed")),
)
def test_bootstrap_fails_closed_on_worktree_observation(
    tmp_path: Path, returncode: int, stdout: bytes
) -> None:
    from apexcrew.adapters.repository.bootstrap import (
        RepositoryBootstrapAuthorityService,
        RepositoryBootstrapError,
    )
    from apexcrew.adapters.repository.git import GitShowRefVerify, GitWorktreeListPorcelain

    root = make_git_repository(tmp_path)
    (root / "README.md").write_text("bootstrap\n", encoding="utf-8")
    expected_oid = commit_repository(root, "bootstrap")
    subprocess.run(["git", "-C", str(root), "branch", "feature"], check=True)

    class FakeRunner:
        def run_bytes(self, repository: object, operation: object) -> subprocess.CompletedProcess[bytes]:
            del repository
            assert isinstance(operation, GitWorktreeListPorcelain)
            return subprocess.CompletedProcess(("git",), returncode, stdout, b"")

        def run(self, repository: object, operation: object) -> subprocess.CompletedProcess[str]:
            del repository
            assert isinstance(operation, GitShowRefVerify)
            return subprocess.CompletedProcess(("git",), 0, expected_oid + "\n", "")

    with pytest.raises(RepositoryBootstrapError, match="worktree observation failed"):
        RepositoryBootstrapAuthorityService(runner=FakeRunner()).inspect(
            str(root), "refs/heads/feature"
        )


@pytest.mark.parametrize("root_kind", ["missing", "non_git"])
def test_bootstrap_rejects_missing_or_non_git_root(tmp_path: Path, root_kind: str) -> None:
    from apexcrew.adapters.repository.bootstrap import (
        RepositoryBootstrapAuthorityService,
        RepositoryBootstrapError,
    )

    root = tmp_path / "missing" if root_kind == "missing" else tmp_path
    with pytest.raises(RepositoryBootstrapError, match="preflight rejected root"):
        RepositoryBootstrapAuthorityService().inspect(str(root), "refs/heads/main")


def test_bootstrap_rejects_linked_worktree_layout(tmp_path: Path) -> None:
    from apexcrew.adapters.repository.bootstrap import (
        RepositoryBootstrapAuthorityService,
        RepositoryBootstrapError,
    )

    root = make_git_repository_with_linked_worktree(tmp_path)
    with pytest.raises(RepositoryBootstrapError, match="preflight rejected root"):
        RepositoryBootstrapAuthorityService().inspect(str(root), "refs/heads/main")


def test_cli_run_create_parses_options_without_provider_dispatch(tmp_path: Path) -> None:
    from apexcrew.adapters.model import deepseek_responses
    from apexcrew.adapters.state.sqlite import SqliteStateStore
    from apexcrew.delivery.cli import app
    from apexcrew.domain.types import RunId

    root = make_git_repository(tmp_path)
    (root / "README.md").write_text("bootstrap\n", encoding="utf-8")
    expected_oid = commit_repository(root, "bootstrap")
    subprocess.run(["git", "-C", str(root), "branch", "feature"], check=True)

    class ExplodingClient:
        def __init__(self, **kwargs: object) -> None:
            raise AssertionError(f"provider dispatch is forbidden: {kwargs}")

    original_client = deepseek_responses.OpenAI
    deepseek_responses.OpenAI = ExplodingClient  # type: ignore[assignment]
    arguments = [
        "run-create",
        "--root",
        str(root / ".." / root.name),
        "--target-ref",
        "refs/heads/feature",
        "--goal",
        "bootstrap",
        "--constraint",
        "offline",
        "--acceptance",
        "payload exists",
    ]
    try:
        result = CliRunner().invoke(app, arguments)
    finally:
        deepseek_responses.OpenAI = original_client  # type: ignore[assignment]

    assert result.exit_code == 0, result.stdout
    output = json.loads(result.stdout)
    assert output["status"] == "RUN_CREATED"
    assert output["repository_root"] == str(root.resolve())
    assert expected_oid in result.stdout
    assert "must-not-be-read" not in result.stdout

    run_id = RunId(output["run_id"])
    database = root / ".apexcrew" / "state.db"
    store = SqliteStateStore(database)
    try:
        record = store.run_record(run_id)
        assert record.state == "DRAFT"
        assert record.target_ref == "refs/heads/feature"
        assert record.pinned_target_oid == expected_oid
        assert store.audit_sequence(run_id) == 1
    finally:
        store.close()

    reopened = SqliteStateStore(database)
    try:
        record = reopened.run_record(run_id)
        assert record.state == "DRAFT"
        assert reopened.current_revision_digests(run_id).policy_digest is not None
    finally:
        reopened.close()

    with sqlite3.connect(database) as connection:
        model_document = connection.execute(
            "SELECT document_json FROM revision_documents "
            "WHERE run_id = ? AND revision_class = 'MODEL_CONFIGURATION'",
            (run_id,),
        ).fetchone()
    assert model_document is not None
    assert json.loads(model_document[0])["provider"] == "deepseek_responses"

    repeated = CliRunner().invoke(app, arguments)
    assert repeated.exit_code == 0, repeated.stdout
    assert json.loads(repeated.stdout)["run_id"] == output["run_id"]
