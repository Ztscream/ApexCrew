from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

from apexcrew.adapters.executor.restricted import RestrictedDockerExecutor
from apexcrew.adapters.repository.attempt_workspace import MaterializedWorkspace
from apexcrew.adapters.repository.granted_workspace import GrantedWorkspaceAdapter
from apexcrew.adapters.repository.snapshot import FilesystemRepositorySnapshot
from apexcrew.application.composition import (
    _CompositionWorkerContext,
    _CompositionWorkerTools,
    check_id_for,
)
from apexcrew.application.configuration import default_revision_documents
from apexcrew.domain.actions import CheckAction, PatchAction, ReadAction, RiskyAction
from apexcrew.domain.authority import (
    ActionClass,
    ActionDeadline,
    FrozenActionBindings,
    GrantedActionIntent,
    TimeoutDecision,
    WorkspaceLease,
    canonical_action_json,
)
from apexcrew.domain.effects import canonical_json, sha256_digest
from apexcrew.domain.plan import CheckDefinition, GlobPattern, TaskContract
from apexcrew.domain.policy import SecretPathPolicy
from apexcrew.domain.revisions import RevisionDigest, Sha256DigestText
from apexcrew.domain.tools import (
    ActionPreState,
    ExecutionResult,
    PatchExecutionResult,
    SanitizedSnapshotEntry,
    ToolIntent,
)
from apexcrew.domain.types import (
    AttemptId,
    AuditSequence,
    GitOid,
    GrantId,
    IntentId,
    PendingActionId,
    RunId,
    TaskId,
)
from apexcrew.domain.worker import WorkerTurnBinding

SHA = Sha256DigestText("sha256:" + "1" * 64)
AUTH = Sha256DigestText("sha256:" + "2" * 64)
OID = GitOid("a" * 40)


def _policy() -> SecretPathPolicy:
    return SecretPathPolicy.from_host_rules((), b"k" * 32)


def _contract(check_count: int = 1) -> TaskContract:
    check = CheckDefinition(
        argv=("python", "-c", "print('ok')"),
        input_globs=(GlobPattern.parse("tests/**"),),
    )
    return TaskContract.from_strings(
        "task-01",
        read_globs=("src/read.py",),
        dependency_globs=("src/dependency.py",),
        write_globs=("src/**",),
        checks=tuple(check for _ in range(check_count)),
        constraints=("keep the change scoped",),
    )


def _tree_digest(files: Mapping[str, bytes]) -> Sha256DigestText:
    return sha256_digest(
        canonical_json(
            {
                path: "sha256:" + sha256(content).hexdigest()
                for path, content in sorted(files.items())
            }
        )
    )


def _materialize(root: Path, files: Mapping[str, bytes]):
    root.mkdir(parents=True, exist_ok=True)
    for raw_path, content in files.items():
        target = root / Path(raw_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    entries = tuple(
        SanitizedSnapshotEntry(
            path=raw_path,
            kind="regular",
            content_digest=Sha256DigestText("sha256:" + sha256(content).hexdigest()),
        )
        for raw_path, content in sorted(files.items())
    )
    return MaterializedWorkspace(
        root=root,
        entries=entries,
        tree_digest=_tree_digest(files),
    )


class _AttemptAdapter:
    def __init__(self, root: Path) -> None:
        self.context = _materialize(
            root / "context",
            {
                "src/read.py": b"read = 1\n",
                "src/dependency.py": b"dependency = 1\n",
            },
        )
        self._check_files = {
            "src/write.py": b"value = 1\n",
            "tests/check.py": b"assert True\n",
        }
        self.check = _materialize(
            root / "check-1",
            self._check_files,
        )
        self.check_roots: dict[str, MaterializedWorkspace] = {}
        self.context_calls = 0
        self.check_calls = 0

    def materialize_context(self, **kwargs: object):
        self.context_calls += 1
        assert kwargs["attempt_id"] == AttemptId("attempt-1")
        return self.context

    def materialize_check(self, **kwargs: object):
        self.check_calls += 1
        assert kwargs["attempt_id"] == AttemptId("attempt-1")
        key = str(kwargs.get("workspace_key", "default"))
        existing = self.check_roots.get(key)
        if existing is not None:
            return existing
        if not self.check_roots:
            workspace = self.check
        else:
            workspace = _materialize(
                self.check.root.parent / f"check-{len(self.check_roots) + 1}",
                self._check_files,
            )
        self.check_roots[key] = workspace
        return workspace


class _LegacyWorkspace:
    def __init__(self, root: Path, secret_paths: SecretPathPolicy) -> None:
        self.root = root
        self._secret_paths = secret_paths

    def snapshot(self) -> FilesystemRepositorySnapshot:
        return FilesystemRepositorySnapshot(self.root)

    def expected_prestate(self, action: object) -> ActionPreState:
        del action
        return ActionPreState()

    def granted_workspace(self) -> GrantedWorkspaceAdapter:
        return GrantedWorkspaceAdapter(self.root, self._secret_paths)

    def apply_patch(
        self, lease: WorkspaceLease, patches: Mapping[str, bytes]
    ) -> PatchExecutionResult:
        del lease, patches
        return PatchExecutionResult(code="LEASE_SCOPE_DENIED")


class _Resources:
    def __init__(
        self, root: Path, adapter: _AttemptAdapter, secret_paths: SecretPathPolicy
    ) -> None:
        self._root = root
        self._adapter = adapter
        self._legacy = _LegacyWorkspace(root / "legacy", secret_paths)
        self._legacy.root.mkdir(parents=True, exist_ok=True)
        for raw_path, content in {
            "src/read.py": b"read = 1\n",
            "src/dependency.py": b"dependency = 1\n",
            "src/write.py": b"value = 1\n",
            "tests/check.py": b"assert True\n",
        }.items():
            target = self._legacy.root / Path(raw_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)

    def validate_repository_binding(
        self, repository_id: object, repository_instance_digest: object
    ) -> None:
        del repository_id, repository_instance_digest

    def attempt_workspace_adapter(
        self,
        repository_id: object,
        repository_instance_digest: object,
        secret_policy: SecretPathPolicy,
    ) -> _AttemptAdapter:
        del repository_id, repository_instance_digest, secret_policy
        return self._adapter

    def worker_workspace(
        self,
        reservation: object,
        repository_id: object,
        repository_instance_digest: object,
        tree_oid: GitOid,
        secret_policy: SecretPathPolicy,
    ) -> _LegacyWorkspace:
        del reservation, repository_id, repository_instance_digest, tree_oid, secret_policy
        return self._legacy


@dataclass
class _Store:
    binding: WorkerTurnBinding
    contract: TaskContract
    lease: WorkspaceLease
    policy: object
    deadline: ActionDeadline
    goal: str = "repair the task"

    def current_worker_turn_binding(self, attempt_id: AttemptId) -> WorkerTurnBinding:
        assert attempt_id == self.binding.attempt_id
        return self.binding

    def workspace_lease(self, run_id: RunId, lease_id: str) -> WorkspaceLease:
        assert run_id == self.binding.run_id
        assert lease_id == self.binding.lease_id
        return self.lease

    def target_reservation_for_run(self, run_id: RunId) -> object:
        assert run_id == self.binding.run_id
        return SimpleNamespace(reservation_id="reservation-1")

    def run_record(self, run_id: RunId) -> object:
        assert run_id == self.binding.run_id
        return SimpleNamespace(repository_instance_digest=SHA)

    def task_contracts(self, plan_digest: RevisionDigest) -> tuple[TaskContract, ...]:
        assert plan_digest == self.binding.plan_digest
        return (self.contract,)

    def bootstrap_inputs(self, run_id: RunId) -> object:
        assert run_id == self.binding.run_id
        return SimpleNamespace(
            goal=self.goal,
            constraints=("offline",),
            acceptance_criteria=("check passes",),
        )

    def current_revision_document(self, run_id: RunId, revision_class: str) -> object:
        assert run_id == self.binding.run_id
        assert revision_class == "POLICY"
        return self.policy

    def action_deadline(self, intent_id: IntentId) -> ActionDeadline | None:
        return self.deadline if intent_id == self.deadline.intent_id else None

    def audit_sequence(self, run_id: RunId) -> AuditSequence:
        assert run_id == self.binding.run_id
        return AuditSequence(0)

    def record_tool_denial(self, denial: object, expected_sequence: AuditSequence) -> AuditSequence:
        del denial
        return AuditSequence(expected_sequence + 1)


class _Authority:
    def deadline_state(self, deadline: ActionDeadline) -> str:
        del deadline
        return "OPEN"

    def settle_timeout(
        self,
        deadline: ActionDeadline,
        outcome_observable: bool,
        expected_sequence: AuditSequence,
    ) -> TimeoutDecision:
        del deadline, outcome_observable, expected_sequence
        return TimeoutDecision(
            outcome="INFRASTRUCTURE_UNCERTAINTY",
            semantic_result=None,
            receipt=None,
            retry_scope=None,
            retry_allowed=True,
            full_reservation_charged=False,
        )


class _RecordingExecutor:
    def __init__(self, secret_paths: SecretPathPolicy) -> None:
        self.calls: list[tuple[tuple[str, ...], object, int]] = []
        self._secret_paths = secret_paths

    def run(
        self,
        argv: Sequence[str],
        snapshot: object,
        timeout_seconds: int,
    ) -> ExecutionResult:
        self.calls.append((tuple(argv), snapshot, timeout_seconds))
        return ExecutionResult.from_output(
            exit_code=0,
            timed_out=False,
            timing_ms=1,
            secret_paths=self._secret_paths,
        )


def _fixture(
    tmp_path: Path,
) -> tuple[_CompositionWorkerTools, _Store, _AttemptAdapter, _RecordingExecutor]:
    secret_paths = _policy()
    contract = _contract()
    binding = WorkerTurnBinding(
        run_id=RunId("run-1"),
        task_id=TaskId("task-01"),
        attempt_id=AttemptId("attempt-1"),
        tranche_id="tranche-1",
        lease_id="lease-1",
        lease_generation=1,
        admissible_head=str(OID),
        task_contract_digest=SHA,
        plan_digest=RevisionDigest(SHA),
        policy_digest=RevisionDigest(SHA),
        budget_digest=RevisionDigest(SHA),
        model_configuration_digest=RevisionDigest(SHA),
        tool_schema_digest=SHA,
        target_safety_digest=SHA,
        credential_profile=None,
        repository_id="repository-1",
        snapshot_digest=SHA,
        scope_digest=SHA,
        dependency_fingerprint_basis=SHA,
    )
    now = datetime(2026, 8, 8, 8, 0, tzinfo=UTC)
    lease = WorkspaceLease(
        lease_id="lease-1",
        run_id=binding.run_id,
        task_id=binding.task_id,
        attempt_id=binding.attempt_id,
        generation=1,
        base_head=str(OID),
        admissible_head=str(OID),
        task_contract_digest=SHA,
        write_globs=(GlobPattern.parse("src/**"),),
        sensitivity_globs=(),
        issued_at=now,
        expires_at=now + timedelta(minutes=15),
        state="ACTIVE",
    )
    check_intent_id = IntentId("check-intent")
    deadline = ActionDeadline(
        run_id=binding.run_id,
        intent_id=check_intent_id,
        budget_digest=SHA,
        applicable_revision_digests=binding.applicable_revision_digests,
        action_class=ActionClass.DECLARED_CHECK,
        started_at=now,
        expires_at=now + timedelta(minutes=10),
        recorded_sequence=AuditSequence(0),
        check_id=check_id_for(binding.task_id, 0),
        snapshot_digest=SHA,
    )
    adapter = _AttemptAdapter(tmp_path / "attempts")
    resources = _Resources(tmp_path / "source", adapter, secret_paths)
    store = _Store(
        binding=binding,
        contract=contract,
        lease=lease,
        policy=default_revision_documents().policy,
        deadline=deadline,
    )
    executor = _RecordingExecutor(secret_paths)
    tools = _CompositionWorkerTools(store, resources, secret_paths, _Authority(), executor)
    return tools, store, adapter, executor


def _intent(store: _Store, action: object, intent_id: str) -> ToolIntent:
    return ToolIntent.for_authorized_worker_action(
        intent_id=IntentId(intent_id),
        run_id=store.binding.run_id,
        task_id=store.binding.task_id,
        attempt_id=store.binding.attempt_id,
        action_id=f"action-{intent_id}",
        action=action,
        authorization_binding_digest=AUTH,
        applicable_revision_digests=store.binding.applicable_revision_digests,
        repository_id=store.binding.repository_id,
        snapshot_digest=store.binding.snapshot_digest,
        scope_digest=store.binding.scope_digest,
        dependency_fingerprint_basis=store.binding.dependency_fingerprint_basis,
        idempotency_key=f"tool:{intent_id}",
        expected_prestate_json="{}",
    )


def test_check_id_derivation_is_shared(tmp_path: Path) -> None:
    tools, store, adapter, _executor = _fixture(tmp_path)
    context = _CompositionWorkerContext(
        store, _Resources(tmp_path / "source-2", adapter, _policy()), _policy()
    )

    payload = json.loads(context.build_current(store.binding.attempt_id).content)
    check_id = check_id_for(store.binding.task_id, 0)

    assert check_id == "task-01:check-1"
    assert payload["checks"][0]["check_id"] == check_id
    result = tools.execute(_intent(store, CheckAction(check_id=check_id), "check-intent"))
    assert result.code == "CHECK_PASSED"


def test_composed_patch_is_not_lease_scope_denied(tmp_path: Path) -> None:
    tools, store, _adapter, _executor = _fixture(tmp_path)

    result = tools.execute(
        _intent(
            store,
            PatchAction(
                path="src/write.py",
                unified_diff="@@ -1 +1 @@\n-value = 1\n+value = 2\n",
            ),
            "patch-intent",
        )
    )

    assert result.code == "PATCH_APPLIED"


def test_composed_check_resolves_declared_definition(tmp_path: Path) -> None:
    tools, store, adapter, executor = _fixture(tmp_path)

    result = tools.execute(
        _intent(
            store,
            CheckAction(check_id=check_id_for(store.binding.task_id, 0)),
            "check-intent",
        )
    )

    assert result.code == "CHECK_PASSED"
    assert executor.calls[0][0] == ("python", "-c", "print('ok')")
    assert executor.calls[0][1].root == adapter.check.root
    assert executor.calls[0][1].tree_digest != store.binding.snapshot_digest
    assert (
        executor.calls[0][1].dependency_fingerprint_digest
        != store.binding.dependency_fingerprint_basis
    )


def test_context_and_check_workspace_bindings_are_distinct(tmp_path: Path) -> None:
    tools, store, adapter, executor = _fixture(tmp_path)

    read = tools.execute(_intent(store, ReadAction(path="src/read.py"), "read-intent"))
    check = tools.execute(
        _intent(
            store,
            CheckAction(check_id=check_id_for(store.binding.task_id, 0)),
            "check-intent",
        )
    )

    assert read.code == "READ_COMPLETED"
    assert check.code == "CHECK_PASSED"
    assert adapter.context.root != adapter.check.root
    assert executor.calls[0][1].root == adapter.check.root


def test_composed_patch_state_is_reused_by_later_checks(tmp_path: Path) -> None:
    tools, store, adapter, executor = _fixture(tmp_path)

    patch = tools.execute(
        _intent(
            store,
            PatchAction(
                path="src/write.py",
                unified_diff="@@ -1 +1 @@\n-value = 1\n+value = 2\n",
            ),
            "patch-intent",
        )
    )
    first = tools.execute(
        _intent(
            store,
            CheckAction(check_id=check_id_for(store.binding.task_id, 0)),
            "check-intent",
        )
    )
    second = tools.execute(
        _intent(
            store,
            CheckAction(check_id=check_id_for(store.binding.task_id, 0)),
            "check-intent",
        )
    )

    assert patch.code == "PATCH_APPLIED"
    assert first.code == "CHECK_PASSED"
    assert second.code == "CHECK_PASSED"
    assert adapter.check_calls == 1
    assert len(executor.calls) == 2
    assert executor.calls[0][1].tree_digest == executor.calls[1][1].tree_digest


def test_granted_mutation_is_replayed_into_a_later_check_workspace(tmp_path: Path) -> None:
    tools, store, adapter, _executor = _fixture(tmp_path)
    store.contract = _contract(check_count=2)

    first = tools.execute(
        _intent(store, CheckAction(check_id=check_id_for(store.binding.task_id, 0)), "check-intent")
    )
    action = RiskyAction(path="src/write.py", operation="delete")
    expected = GrantedWorkspaceAdapter(adapter.check.root, _policy()).expected_prestate(action)
    bindings = FrozenActionBindings(
        run_id=store.binding.run_id,
        task_id=store.binding.task_id,
        attempt_id=store.binding.attempt_id,
        logical_turn_id="turn-1",
        action_id="grant-action",
        lease_id=store.binding.lease_id,
        lease_generation=store.binding.lease_generation,
        run_head_oid=store.binding.admissible_head,
        target_safety_digest=RevisionDigest(SHA),
        plan_digest=store.binding.plan_digest,
        policy_digest=store.binding.policy_digest,
        budget_digest=store.binding.budget_digest,
        model_configuration_digest=store.binding.model_configuration_digest,
        tool_schema_digest=store.binding.tool_schema_digest,
        authorization_binding_digest=AUTH,
        deadline_at_utc=datetime(2026, 8, 8, 8, 10, tzinfo=UTC),
    )
    granted = GrantedActionIntent(
        intent_id=IntentId("grant-intent"),
        pending_id=PendingActionId("pending-1"),
        grant_id=GrantId("grant-1"),
        action=action,
        normalized_action_json=canonical_action_json(action),
        action_digest=SHA,
        expected_pre_state=expected,
        bindings=bindings,
    )
    deleted = tools.execute_granted(granted)
    store.deadline = replace(
        store.deadline,
        intent_id=IntentId("second-check"),
        check_id=check_id_for(store.binding.task_id, 1),
    )
    second = tools.execute(
        _intent(
            store,
            CheckAction(check_id=check_id_for(store.binding.task_id, 1)),
            "second-check",
        )
    )

    assert first.code == "CHECK_PASSED"
    assert deleted.code == "DELETED"
    assert second.code == "CHECK_PASSED"
    second_root = next(
        workspace.root
        for workspace in adapter.check_roots.values()
        if workspace.root != adapter.check.root
    )
    assert not (second_root / "src" / "write.py").exists()


def test_docker_executor_is_the_only_composed_check_path(tmp_path: Path) -> None:
    secret_paths = _policy()
    adapter = _AttemptAdapter(tmp_path / "attempts")
    resources = _Resources(tmp_path / "source", adapter, secret_paths)
    contract = _contract()
    tools, store, _adapter, _executor = _fixture(tmp_path)
    del tools, resources, contract

    default_tools = _CompositionWorkerTools(
        store, _Resources(tmp_path / "source-3", adapter, secret_paths), secret_paths, _Authority()
    )
    runtime = default_tools._runtime(
        _intent(store, CheckAction(check_id=check_id_for(store.binding.task_id, 0)), "check-intent")
    )

    assert isinstance(runtime._executor, RestrictedDockerExecutor)
    assert "LocalSubprocessExecutor" not in type(runtime._executor).__name__
    assert "APEXCREW_HOST_EXECUTOR" not in repr(runtime)
