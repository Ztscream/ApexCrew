from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from apexcrew.adapters.repository.attempt_workspace import MaterializedWorkspace
from apexcrew.application.composition import _CompositionWorkerContext
from apexcrew.domain.effects import RunBootstrapInputs, canonical_json, sha256_digest
from apexcrew.domain.plan import CheckDefinition, GlobPattern, TaskContract
from apexcrew.domain.policy import SecretPathPolicy
from apexcrew.domain.revisions import Sha256DigestText
from apexcrew.domain.tools import SanitizedSnapshotEntry
from apexcrew.domain.types import AttemptId, RepositoryId, RevisionDigest, RunId, TaskId
from apexcrew.domain.worker import WorkerTurnBinding

_DIGEST = RevisionDigest("sha256:" + "1" * 64)
_SHA = Sha256DigestText("sha256:" + "2" * 64)


def _binding() -> WorkerTurnBinding:
    return WorkerTurnBinding(
        run_id=RunId("run-1"),
        task_id=TaskId("task-1"),
        attempt_id=AttemptId("attempt-1"),
        tranche_id="tranche-1",
        lease_id="lease-1",
        lease_generation=1,
        admissible_head="a" * 40,
        task_contract_digest=_SHA,
        plan_digest=_DIGEST,
        policy_digest=_DIGEST,
        budget_digest=_DIGEST,
        model_configuration_digest=_DIGEST,
        tool_schema_digest=_SHA,
        target_safety_digest=_SHA,
        credential_profile=None,
        repository_id="repo-1",
        snapshot_digest=_SHA,
        scope_digest=_SHA,
        dependency_fingerprint_basis=_SHA,
    )


def _contract() -> TaskContract:
    return TaskContract.from_strings(
        "task-1",
        ("src/read.py",),
        ("src/write.py",),
        dependency_globs=("src/dependency.py",),
        checks=(
            CheckDefinition(
                argv=("pytest", "-q"),
                input_globs=(GlobPattern.parse("tests/**"),),
            ),
        ),
        constraints=("stay in src",),
    )


def _workspace(root: Path, files: dict[str, bytes]) -> MaterializedWorkspace:
    for raw_path, content in files.items():
        path = root / Path(raw_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    entries = tuple(
        SanitizedSnapshotEntry(
            path=path,
            kind="regular",
            content_digest=Sha256DigestText("sha256:" + hashlib.sha256(content).hexdigest()),
        )
        for path, content in sorted(files.items())
    )
    return MaterializedWorkspace(
        root=root,
        entries=entries,
        tree_digest=sha256_digest(
            canonical_json(
                {
                    path: "sha256:" + hashlib.sha256(content).hexdigest()
                    for path, content in sorted(files.items())
                }
            )
        ),
    )


class _WorkspaceAdapter:
    def __init__(self, workspace: MaterializedWorkspace) -> None:
        self.workspace = workspace
        self.calls: list[tuple[object, ...]] = []

    def materialize_context(self, **kwargs: object) -> MaterializedWorkspace:
        self.calls.append(tuple(kwargs[name] for name in ("attempt_id", "base_oid")))
        return self.workspace


class _Resources:
    def __init__(self, adapter: _WorkspaceAdapter) -> None:
        self.adapter = adapter

    def attempt_workspace_adapter(
        self,
        repository_id: RepositoryId,
        repository_instance_digest: Sha256DigestText,
        secret_policy: SecretPathPolicy,
    ) -> _WorkspaceAdapter:
        del repository_id, repository_instance_digest, secret_policy
        return self.adapter


class _Store:
    def __init__(self, workspace: MaterializedWorkspace) -> None:
        self.binding = _binding()
        self.adapter = _WorkspaceAdapter(workspace)

    def current_worker_turn_binding(self, attempt_id: AttemptId) -> WorkerTurnBinding:
        assert attempt_id == self.binding.attempt_id
        return self.binding

    def task_contracts(self, plan_digest: RevisionDigest) -> tuple[TaskContract, ...]:
        assert plan_digest == self.binding.plan_digest
        return (_contract(),)

    def bootstrap_inputs(self, run_id: RunId) -> RunBootstrapInputs:
        assert run_id == self.binding.run_id
        return RunBootstrapInputs(
            goal="repair the task",
            constraints=("offline",),
            acceptance_criteria=("check passes",),
        )

    def run_record(self, run_id: RunId) -> SimpleNamespace:
        assert run_id == self.binding.run_id
        return SimpleNamespace(repository_instance_digest=_SHA)


def _context(tmp_path: Path, files: dict[str, bytes], **kwargs: object):
    workspace = _workspace(tmp_path / "context", files)
    store = _Store(workspace)
    resources = _Resources(store.adapter)
    policy = SecretPathPolicy.from_host_rules(("private/**",), b"k" * 32)
    context = _CompositionWorkerContext(
        store,
        resources,
        policy,
        **kwargs,  # type: ignore[arg-type]
    )
    return context, store


def test_context_contains_task_contract_and_scoped_files(tmp_path: Path) -> None:
    context, store = _context(
        tmp_path,
        {
            "src/read.py": b"read = 1\n",
            "src/dependency.py": b"dependency = 1\n",
            "src/write.py": b"write = 1\n",
        },
    )

    capsule = context.build_current(store.binding.attempt_id)
    payload = json.loads(capsule.content)

    assert payload["goal"] == "repair the task"
    assert payload["constraints"] == ["offline"]
    assert payload["acceptance_criteria"] == ["check passes"]
    assert payload["task_id"] == "task-1"
    assert payload["task_contract"]["read_globs"] == ["src/read.py"]
    assert payload["task_contract"]["dependency_globs"] == ["src/dependency.py"]
    assert payload["task_contract"]["write_globs"] == ["src/write.py"]
    assert payload["task_contract"]["constraints"] == ["stay in src"]
    assert payload["checks"] == [{"argv": ["pytest", "-q"], "check_id": "task-1:check-1"}]
    assert [item["path"] for item in payload["files"]] == [
        "src/dependency.py",
        "src/read.py",
    ]
    assert all("dependency_digest" in item for item in payload["files"])
    assert store.adapter.calls
    assert capsule.dependencies == tuple(item["dependency_digest"] for item in payload["files"])


def test_context_excludes_secret_paths(tmp_path: Path) -> None:
    context, store = _context(
        tmp_path,
        {
            "src/read.py": b"safe\n",
            "private/token.key": b"TOP SECRET\n",
        },
    )

    capsule = context.build_current(store.binding.attempt_id)

    assert "private/token.key" not in capsule.content
    assert "TOP SECRET" not in capsule.content
    assert [item["path"] for item in json.loads(capsule.content)["files"]] == ["src/read.py"]


def test_context_truncation_is_marked(tmp_path: Path) -> None:
    context, store = _context(
        tmp_path,
        {"src/read.py": b"x" * 131_073},
    )

    capsule = context.build_current(store.binding.attempt_id)
    payload = json.loads(capsule.content)

    assert payload["truncated"] is True
    assert payload["truncation"]["marker"] == "CONTEXT_TRUNCATED"
    assert payload["files"][0]["truncated"] is True
    assert len(payload["files"][0]["content"].encode("utf-8")) == 131_072
    assert capsule.dependencies == (payload["files"][0]["dependency_digest"],)


def test_context_dependencies_bind_paths_even_when_bytes_match(tmp_path: Path) -> None:
    context, store = _context(
        tmp_path,
        {
            "src/read.py": b"same\n",
            "src/dependency.py": b"same\n",
        },
    )

    payload = json.loads(context.build_current(store.binding.attempt_id).content)
    dependencies = [item["dependency_digest"] for item in payload["files"]]

    assert len(dependencies) == 2
    assert dependencies[0] != dependencies[1]


def test_context_dependencies_bind_bytes_observed_from_workspace(tmp_path: Path) -> None:
    context, store = _context(tmp_path, {"src/read.py": b"before\n"})
    first = context.build_current(store.binding.attempt_id)
    (tmp_path / "context" / "src" / "read.py").write_bytes(b"after\n")

    second = context.build_current(store.binding.attempt_id)

    assert first.dependencies != second.dependencies
