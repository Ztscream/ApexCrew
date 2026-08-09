from __future__ import annotations

import json
import sqlite3
import subprocess
from base64 import b32encode
from collections.abc import Sequence
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

from apexcrew.adapters.model.scripted import ScriptedMockLLM
from apexcrew.application.composition import (
    _CompositionWorkerTools,
    build_application_bundle,
    build_test_application_bundle,
)
from apexcrew.application.configuration import default_revision_documents
from apexcrew.domain.actions import CheckAction, PatchAction
from apexcrew.domain.commands import (
    ApplicableRevisionDigests,
    ApproveBudgetPayload,
    ApproveModelConfigurationPayload,
    ApprovePlanPayload,
    ApprovePolicyPayload,
    BeginPlanningPayload,
    CommandEnvelope,
    CreateRunPayload,
    StartPayload,
)
from apexcrew.domain.coordination import task_contract_digest
from apexcrew.domain.model import ModelCompletion, ModelUsage, ProviderAttemptResult
from apexcrew.domain.policy import SecretPathPolicy
from apexcrew.domain.revisions import revision_digest
from apexcrew.domain.tools import ExecutionResult, SanitizedSnapshot, ToolIntent
from apexcrew.domain.types import AttemptId, AuditSequence, GitOid, IntentId, RunId, TaskId
from apexcrew.domain.worker import WorkerTurnBinding


def _git(root: Path, *argv: str) -> str:
    result = subprocess.run(
        ("git", *argv),
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _approval_code(command_kind: str, run_id: RunId, revision_class: str, digest: str) -> str:
    payload = json.dumps(
        {
            "command_kind": command_kind,
            "revision_class": revision_class,
            "revision_digest": digest,
            "run_id": str(run_id),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return b32encode(sha256(payload).digest()).decode("ascii")[:6]


class _WorkerModel(ScriptedMockLLM):
    def __init__(self, worker_actions: Sequence[dict[str, object]]) -> None:
        super().__init__(())
        self.worker_actions = list(worker_actions)

    def complete(self, request):  # type: ignore[no-untyped-def]
        if request.owner_kind == "PLANNING":
            action = {
                "kind": "submit_plan",
                "plan_document": {
                    "proposed_promotion_order": ["task-01"],
                    "run_checks": [{"argv": ["python", "-m", "pytest"], "input_globs": ["src/**"]}],
                    "tasks": [
                        {
                            "checks": [
                                {
                                    "argv": ["python", "-m", "pytest"],
                                    "input_globs": ["src/task.py"],
                                }
                            ],
                            "constraints": ["keep the change scoped"],
                            "dependency_globs": [],
                            "dependency_task_ids": [],
                            "read_globs": ["src/**"],
                            "task_id": "task-01",
                            "write_globs": ["src/task.py"],
                        }
                    ],
                },
            }
        else:
            action = (
                self.worker_actions.pop(0)
                if self.worker_actions
                else {"kind": "finish", "summary": "task complete"}
            )
        completion = ModelCompletion(
            response_id="composition-test-response",
            requested_model_id=request.requested_model_id,
            returned_model_id=request.requested_model_id,
            usage=ModelUsage(10, 5, request.reserved_cost_usd / 10),
            normalized_action=action,
        )
        return ProviderAttemptResult.completed(completion)


class _RecordingExecutor:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], SanitizedSnapshot, int]] = []

    def run(
        self,
        argv: Sequence[str],
        snapshot: SanitizedSnapshot,
        timeout_seconds: int,
    ) -> ExecutionResult:
        self.calls.append((tuple(argv), snapshot, timeout_seconds))
        return ExecutionResult.from_output(
            exit_code=0,
            timed_out=False,
            timing_ms=1,
            secret_paths=SecretPathPolicy.from_host_rules((), b"k" * 32),
        )


class _RaisingExecutor(_RecordingExecutor):
    def run(
        self,
        argv: Sequence[str],
        snapshot: SanitizedSnapshot,
        timeout_seconds: int,
    ) -> ExecutionResult:
        self.calls.append((tuple(argv), snapshot, timeout_seconds))
        raise RuntimeError("EXECUTOR_RUNTIME_FAILURE")


_COMPOSITION_TEST_SHA = "sha256:" + "9" * 64


def _worker_graph(bundle: object) -> tuple[object, object]:
    phase_drivers = bundle.runtime._phase_drivers  # type: ignore[attr-defined]
    workers = phase_drivers._recovered_actions._workers
    return workers._tools, workers._attempts


def _install_direct_worker_attempt(
    bundle: object, run_id: RunId
) -> tuple[object, object, WorkerTurnBinding, object]:
    tools, store = _worker_graph(bundle)
    revisions = store.current_revision_digests(run_id)
    assert revisions.plan_digest is not None
    contract = next(
        item
        for item in store.task_contracts(revisions.plan_digest)
        if item.task_id == TaskId("task-01")
    )
    model_configuration = store.current_revision_document(run_id, "MODEL_CONFIGURATION")
    run = store.run_record(run_id)
    binding = WorkerTurnBinding(
        run_id=run_id,
        task_id=TaskId("task-01"),
        attempt_id=AttemptId("composition-direct-attempt"),
        tranche_id="composition-direct-tranche",
        lease_id="composition-direct-lease",
        lease_generation=1,
        admissible_head=str(run.pinned_target_oid),
        task_contract_digest=task_contract_digest(contract),
        plan_digest=revisions.plan_digest,
        policy_digest=revisions.policy_digest,
        budget_digest=revisions.budget_digest,
        model_configuration_digest=revisions.model_configuration_digest,
        tool_schema_digest=model_configuration.tool_schema_digest,
        target_safety_digest=store.target_authority_digest(run_id),
        credential_profile=None,
        repository_id=str(run.repository_id),
        snapshot_digest=_COMPOSITION_TEST_SHA,
        scope_digest=_COMPOSITION_TEST_SHA,
        dependency_fingerprint_basis=_COMPOSITION_TEST_SHA,
    )
    store.install_worker_attempt_for_test(binding)
    return tools, store, binding, contract


def _direct_intent(
    binding: WorkerTurnBinding,
    action: CheckAction | PatchAction,
    snapshot_digest: str,
) -> ToolIntent:
    return ToolIntent.for_authorized_worker_action(
        intent_id=IntentId(f"composition-direct-{action.kind}"),
        run_id=binding.run_id,
        task_id=binding.task_id,
        attempt_id=binding.attempt_id,
        action_id=f"composition-direct-{action.kind}",
        action=action,
        authorization_binding_digest=_COMPOSITION_TEST_SHA,
        applicable_revision_digests=binding.applicable_revision_digests,
        repository_id=binding.repository_id,
        snapshot_digest=snapshot_digest,
        scope_digest=binding.scope_digest,
        dependency_fingerprint_basis=binding.dependency_fingerprint_basis,
        idempotency_key=f"composition-direct:{action.kind}",
        expected_prestate_json="{}",
    )


def _record_check_deadline(store: object, tools: object, intent: ToolIntent) -> None:
    expected_sequence = store.audit_sequence(intent.run_id)
    store.record_intent(
        intent.to_effect_intent(AuditSequence(expected_sequence + 1)), expected_sequence
    )
    tools._authority.open_action_deadline(  # type: ignore[attr-defined]
        intent.run_id,
        intent.intent_id,
        store.audit_sequence(intent.run_id),
    )


def test_composition_worker_tools_delegates_recovery_observation() -> None:
    observed: list[object] = []

    class _RecoveryRuntime:
        def observe_recovery(self, intent: object) -> tuple[str, None]:
            observed.append(intent)
            return "EXACT_PRE", None

    runtime = _RecoveryRuntime()
    worker_tools = object.__new__(_CompositionWorkerTools)
    worker_tools._runtime = lambda _intent: runtime
    intent = object()

    assert worker_tools.observe_recovery(intent) == ("EXACT_PRE", None)
    assert observed == [intent]


def test_composition_patch_snapshot_digest_uses_primary_workspace() -> None:
    worker_tools = object.__new__(_CompositionWorkerTools)
    worker_tools._store = SimpleNamespace(workspace_lease=lambda _run_id, _lease_id: object())
    worker_tools._contract = lambda _binding: object()
    worker_tools._attempt_state = lambda _binding: object()
    primary = SimpleNamespace(tree_digest="sha256:" + "4" * 64)
    worker_tools._primary_workspace = lambda _state, _binding, _lease, _contract: primary
    binding = SimpleNamespace(
        run_id="run-1",
        lease_id="lease-1",
        snapshot_digest="sha256:" + "5" * 64,
    )

    digest = worker_tools.capture_snapshot_digest(
        binding,
        PatchAction(path="src/a.py", unified_diff="@@ -1 +1 @@\n-old\n+new\n"),
    )

    assert digest == primary.tree_digest


def _envelope(request_id: str, sequence: int | None, payload: object) -> CommandEnvelope:
    return CommandEnvelope(
        request_id=request_id,
        expected_sequence=sequence,
        applicable_revision_digests=ApplicableRevisionDigests(),
        payload=payload,
    )


def _prepare_worker_run(
    tmp_path: Path,
    worker_actions: Sequence[dict[str, object]],
    *,
    executor: _RecordingExecutor | None = None,
    production: bool = False,
) -> tuple[object, RunId, _RecordingExecutor, Path, GitOid]:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.name", "ApexCrew Test")
    _git(root, "config", "user.email", "apexcrew@example.test")
    (root / "src").mkdir()
    (root / "src" / "task.py").write_text("value = 1\n", encoding="utf-8")
    (root / "src" / "context_only.py").write_text("context = True\n", encoding="utf-8")
    _git(root, "add", "src/task.py", "src/context_only.py")
    _git(root, "commit", "-qm", "initial")
    target_oid = GitOid(_git(root, "rev-parse", "refs/heads/main"))
    _git(root, "checkout", "--detach", str(target_oid))

    revisions = default_revision_documents()
    model_configuration = revisions.model_configuration.model_copy(
        update={"provider": "scripted_mock", "provider_base_origin": "mock://scripted"}
    )
    model = _WorkerModel(worker_actions)
    executor = _RecordingExecutor() if executor is None else executor
    if production:
        bundle = build_application_bundle(
            root,
            model_configuration=model_configuration,
            scripted_model=model,
            secret_policy=SecretPathPolicy.from_host_rules((), b"k" * 32),
        )
    else:
        bundle = build_test_application_bundle(
            root,
            model_configuration=model_configuration,
            scripted_model=model,
            secret_policy=SecretPathPolicy.from_host_rules((), b"k" * 32),
            executor=executor,
        )
    outcome = bundle.control.handle(
        _envelope(
            "create",
            None,
            CreateRunPayload(
                goal="complete the task",
                constraints=("stay within src",),
                acceptance_criteria=("the task is complete",),
                repository_root=str(root),
                target_ref="refs/heads/main",
                expected_target_oid=target_oid,
                policy_revision=revisions.policy,
                budget_revision=revisions.budget,
                model_configuration_revision=model_configuration,
            ),
        )
    )
    assert outcome.status == "ACCEPTED" and outcome.run_id is not None
    run_id = outcome.run_id
    policy_digest = revision_digest(revisions.policy)
    budget_digest = revision_digest(revisions.budget)
    model_digest = revision_digest(model_configuration)
    for request_id, command_kind, revision_class, digest, payload_type in (
        ("approve-policy", "approve_policy", "POLICY", policy_digest, "policy"),
        ("approve-budget", "approve_budget", "BUDGET", budget_digest, "budget"),
        (
            "approve-model",
            "approve_model_configuration",
            "MODEL_CONFIGURATION",
            model_digest,
            "model",
        ),
    ):
        code = _approval_code(command_kind, run_id, revision_class, digest)
        if payload_type == "policy":
            payload = ApprovePolicyPayload(
                run_id=run_id, policy_digest=digest, confirmation_code=code
            )
        elif payload_type == "budget":
            payload = ApproveBudgetPayload(
                run_id=run_id, budget_digest=digest, confirmation_code=code
            )
        else:
            payload = ApproveModelConfigurationPayload(
                run_id=run_id, model_configuration_digest=digest, confirmation_code=code
            )
        result = bundle.control.handle(
            CommandEnvelope(
                request_id=request_id,
                expected_sequence=bundle.queries.get(run_id).sequence,
                applicable_revision_digests=ApplicableRevisionDigests(
                    policy_digest=policy_digest if payload_type != "policy" else None,
                    budget_digest=budget_digest if payload_type == "model" else None,
                ),
                payload=payload,
            )
        )
        assert result.status == "ACCEPTED"
    all_revisions = ApplicableRevisionDigests(
        policy_digest=policy_digest,
        budget_digest=budget_digest,
        model_configuration_digest=model_digest,
    )
    begin = bundle.control.handle(
        CommandEnvelope(
            request_id="begin-planning",
            expected_sequence=bundle.queries.get(run_id).sequence,
            applicable_revision_digests=all_revisions,
            payload=BeginPlanningPayload(run_id=run_id),
        )
    )
    assert begin.status == "ACCEPTED"
    plan_stop = bundle.runtime.run_until_blocked(run_id)
    assert plan_stop.reason == "AWAITING_PLAN_APPROVAL"
    assert plan_stop.pending is not None
    plan_digest = plan_stop.pending.plan_digest
    approve_plan = bundle.control.handle(
        CommandEnvelope(
            request_id="approve-plan",
            expected_sequence=bundle.queries.get(run_id).sequence,
            applicable_revision_digests=all_revisions,
            payload=ApprovePlanPayload(
                run_id=run_id,
                plan_digest=plan_digest,
                confirmation_code=_approval_code("approve_plan", run_id, "PLAN", plan_digest),
            ),
        )
    )
    assert approve_plan.status == "ACCEPTED"
    start = bundle.control.handle(
        CommandEnvelope(
            request_id="start",
            expected_sequence=bundle.queries.get(run_id).sequence,
            applicable_revision_digests=ApplicableRevisionDigests(
                plan_digest=plan_digest,
                policy_digest=policy_digest,
                budget_digest=budget_digest,
                model_configuration_digest=model_digest,
            ),
            payload=StartPayload(run_id=run_id, plan_digest=plan_digest),
        )
    )
    assert start.status == "ACCEPTED"
    return bundle, run_id, executor, root, target_oid


def test_check_id_derivation_is_shared(tmp_path: Path) -> None:
    bundle, run_id, _executor, _root, _target_oid = _prepare_worker_run(tmp_path, ())
    try:
        tools, _store, binding, _contract = _install_direct_worker_attempt(bundle, run_id)
        phase_drivers = bundle.runtime._phase_drivers  # type: ignore[attr-defined]
        worker = phase_drivers._recovered_actions._workers
        capsule = worker._capsules.build_current(binding.attempt_id)
        context = json.loads(capsule.content)
        assert context["checks"][0]["check_id"] == "task-01:check-1"

        action = CheckAction(check_id="task-01:check-1")
        snapshot_digest = tools.capture_snapshot_digest(binding, action)
        runtime = tools._runtime(  # type: ignore[attr-defined]
            _direct_intent(binding, action, snapshot_digest)
        )
        assert runtime._declared_checks.require("task-01:check-1").argv == (  # type: ignore[attr-defined]
            "python",
            "-m",
            "pytest",
        )
    finally:
        bundle.close()


def test_composed_patch_is_not_lease_scope_denied(tmp_path: Path) -> None:
    bundle, run_id, _executor, _root, _target_oid = _prepare_worker_run(tmp_path, ())
    try:
        tools, _store, binding, _contract = _install_direct_worker_attempt(bundle, run_id)
        action = PatchAction(
            path="src/task.py",
            unified_diff="@@ -1 +1 @@\n-value = 1\n+value = 2\n",
        )
        snapshot_digest = tools.capture_snapshot_digest(binding, action)
        result = tools.execute(_direct_intent(binding, action, snapshot_digest))

        assert result.code == "PATCH_APPLIED"
    finally:
        bundle.close()


def test_composed_check_resolves_declared_definition(tmp_path: Path) -> None:
    bundle, run_id, _executor, _root, _target_oid = _prepare_worker_run(tmp_path, ())
    try:
        tools, store, binding, _contract = _install_direct_worker_attempt(bundle, run_id)
        action = CheckAction(check_id="task-01:check-1")
        snapshot_digest = tools.capture_snapshot_digest(binding, action)
        intent = _direct_intent(binding, action, snapshot_digest)
        _record_check_deadline(store, tools, intent)
        result = tools.execute(intent)

        assert result.code == "CHECK_PASSED"
        assert result.bounded_payload["snapshot_digest"] == snapshot_digest
    finally:
        bundle.close()


def test_context_and_check_workspace_bindings_are_distinct(tmp_path: Path) -> None:
    bundle, run_id, _executor, _root, _target_oid = _prepare_worker_run(tmp_path, ())
    try:
        tools, _store, binding, contract = _install_direct_worker_attempt(bundle, run_id)
        state = tools._attempt_state(binding)  # type: ignore[attr-defined]
        context = tools._context_workspace(state, binding, contract)  # type: ignore[attr-defined]
        action = CheckAction(check_id="task-01:check-1")
        snapshot_digest = tools.capture_snapshot_digest(binding, action)
        runtime = tools._runtime(  # type: ignore[attr-defined]
            _direct_intent(binding, action, snapshot_digest)
        )
        check_snapshot = runtime._sanitized_snapshot  # type: ignore[attr-defined]

        assert check_snapshot is not None
        assert context.root != check_snapshot.root
        assert context.tree_digest != check_snapshot.tree_digest
        assert snapshot_digest == check_snapshot.tree_digest
    finally:
        bundle.close()


def test_docker_executor_is_the_only_composed_check_path(tmp_path: Path) -> None:
    bundle, run_id, _executor, _root, _target_oid = _prepare_worker_run(
        tmp_path, (), production=True
    )
    try:
        tools, _store, binding, _contract = _install_direct_worker_attempt(bundle, run_id)
        action = CheckAction(check_id="task-01:check-1")
        snapshot_digest = tools.capture_snapshot_digest(binding, action)
        runtime = tools._runtime(  # type: ignore[attr-defined]
            _direct_intent(binding, action, snapshot_digest)
        )
        production_tools_executor = runtime._executor  # type: ignore[attr-defined]

        assert type(production_tools_executor).__name__ == "RestrictedDockerExecutor"
        assert "LocalSubprocessExecutor" not in repr(production_tools_executor)
    finally:
        bundle.close()


def test_public_composition_binds_check_to_patched_workspace(tmp_path: Path) -> None:
    bundle, run_id, executor, root, _target_oid = _prepare_worker_run(
        tmp_path,
        (
            {
                "kind": "patch",
                "path": "src/task.py",
                "unified_diff": "@@ -1 +1 @@\n-value = 1\n+value = 2\n",
            },
            {"kind": "check", "check_id": "task-01:check-1"},
            {"kind": "finish", "summary": "task complete"},
        ),
    )
    try:
        stop = bundle.runtime.run_until_blocked(run_id)
        assert stop.reason == "AWAITING_FINAL_APPROVAL"
        assert len(executor.calls) == 1
        argv, snapshot, timeout_seconds = executor.calls[0]
        assert argv == ("python", "-m", "pytest")
        assert timeout_seconds == 600
        assert snapshot.root != root
        assert snapshot.materialized_paths == ("src/task.py",)
        assert (snapshot.root / "src" / "task.py").read_text(encoding="utf-8") == "value = 2\n"
    finally:
        bundle.close()


def test_unknown_check_is_settled_as_a_durable_tool_denial(tmp_path: Path) -> None:
    bundle, run_id, executor, root, _target_oid = _prepare_worker_run(
        tmp_path,
        (
            {"kind": "check", "check_id": "task-01:unknown"},
            {"kind": "finish", "summary": "task complete"},
        ),
    )
    try:
        stop = bundle.runtime.run_until_blocked(run_id)
        assert stop.reason.value == "AWAITING_FINAL_APPROVAL"
        assert executor.calls == []
        assert bundle.queries.get(run_id).sequence > 0
        with sqlite3.connect(root / ".apexcrew" / "state.db") as connection:
            row = connection.execute(
                "SELECT effect_intents.state, effect_results.result_class, "
                "effect_results.result_json "
                "FROM effect_intents JOIN effect_results USING (intent_id) "
                "WHERE effect_intents.run_id = ? AND effect_intents.kind = 'check'",
                (run_id,),
            ).fetchone()
        assert row is not None
        assert row[0] == "SETTLED"
        assert row[1] in {
            "SCOPE_DENIED",
            "LEASE_SCOPE_DENIED",
        }
        result = json.loads(str(row[2]))
        assert result["outcome"] == "FAILED"
        assert result["result_class"] == row[1]
    finally:
        bundle.close()


def test_composed_tool_execution_failure_settles_the_recorded_intent(tmp_path: Path) -> None:
    executor = _RaisingExecutor()
    bundle, run_id, _executor, root, _target_oid = _prepare_worker_run(
        tmp_path,
        ({"kind": "check", "check_id": "task-01:check-1"},),
        executor=executor,
    )
    try:
        stop = bundle.runtime.run_until_blocked(run_id)
        assert stop.reason.value == "PAUSED"
        with sqlite3.connect(root / ".apexcrew" / "state.db") as connection:
            row = connection.execute(
                "SELECT effect_intents.state, effect_results.result_class, "
                "effect_results.result_json "
                "FROM effect_intents JOIN effect_results USING (intent_id) "
                "WHERE effect_intents.run_id = ? AND effect_intents.kind = 'check'",
                (run_id,),
            ).fetchone()
        assert row is not None
        assert row[0] == "INDETERMINATE"
        assert row[1] == "INFRASTRUCTURE_UNCERTAINTY"
        assert '"intent_id"' in str(row[2])
    finally:
        bundle.close()


def test_risky_workspace_capture_failure_is_recorded_without_approval(tmp_path: Path) -> None:
    bundle, run_id, _executor, _root, _target_oid = _prepare_worker_run(
        tmp_path,
        (
            {
                "kind": "risky_action",
                "path": "src/task.py",
                "operation": "protected_patch",
                "unified_diff": "@@ -1 +1 @@\n-value = 1\n+value = 2\n",
            },
        ),
    )
    try:
        stop = bundle.runtime.run_until_blocked(run_id)
        assert stop.reason.value == "PAUSED"
        assert bundle.queries.get(run_id).sequence > 0
    finally:
        bundle.close()


def test_invalid_canonical_risky_path_is_durably_denied(tmp_path: Path) -> None:
    bundle, run_id, executor, _root, _target_oid = _prepare_worker_run(
        tmp_path,
        (
            {
                "kind": "risky_action",
                "path": "../src/task.py",
                "operation": "delete",
            },
        ),
    )
    try:
        stop = bundle.runtime.run_until_blocked(run_id)
        assert stop.reason.value == "PAUSED"
        assert executor.calls == []
        assert bundle.queries.get(run_id).sequence > 0
    finally:
        bundle.close()


def test_risky_rename_destination_must_be_inside_lease(tmp_path: Path) -> None:
    bundle, run_id, executor, _root, _target_oid = _prepare_worker_run(
        tmp_path,
        (
            {
                "kind": "risky_action",
                "path": "src/task.py",
                "operation": "rename",
                "destination": "other/task.py",
            },
        ),
    )
    try:
        stop = bundle.runtime.run_until_blocked(run_id)
        assert stop.reason.value == "PAUSED"
        assert executor.calls == []
        assert bundle.queries.get(run_id).sequence > 0
    finally:
        bundle.close()
