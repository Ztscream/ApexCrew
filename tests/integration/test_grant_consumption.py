from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from helpers.application import ApplicationFixture, make_active_application

from apexcrew.adapters.repository.snapshot import MemoryRepositorySnapshot
from apexcrew.application.runtime import GrantedActionRuntime
from apexcrew.domain.actions import RiskyAction
from apexcrew.domain.authority import (
    AuthorityService,
    AuthorizationRequest,
    FrozenActionBindings,
    GrantedActionIntent,
    canonical_action_json,
    confirmation_code_for_pending_digest,
)
from apexcrew.domain.commands import (
    ApplicableRevisionDigests,
    CommandEnvelope,
    GrantPayload,
)
from apexcrew.domain.effects import AuditEvent, StateConflict, canonical_json, sha256_digest
from apexcrew.domain.policy import SecretPathPolicy
from apexcrew.domain.tools import (
    ActionPreState,
    GrantedActionObservation,
    ScopedToolRuntime,
    ToolIntent,
    ToolResult,
)
from apexcrew.domain.types import AttemptId, RevisionDigest, RunId, TaskId
from apexcrew.domain.worker import WorkerTurnBinding, normalized_action_digest

SHA = "sha256:" + "1" * 64


@dataclass
class PendingGrantApplication:
    base: ApplicationFixture
    run_id: RunId
    workspace: RecordingWorkspace
    tools: ScopedToolRuntime
    granted_runtime: GrantedActionRuntime

    @property
    def store(self):  # type: ignore[no-untyped-def]
        return self.base.store

    @property
    def control(self):  # type: ignore[no-untyped-def]
        return self.base.control


def _seed_current_plan_and_head(app: ApplicationFixture, run_id: RunId) -> RevisionDigest:
    plan_digest = RevisionDigest("sha256:" + "2" * 64)
    head = str(app.store.run_record(run_id).pinned_target_oid)
    expected = app.store.audit_sequence(run_id)

    def mutate(connection) -> None:  # type: ignore[no-untyped-def]
        connection.execute(
            "UPDATE runs SET current_plan_digest = ?, run_head_oid = ? WHERE run_id = ?",
            (plan_digest, head, run_id),
        )
        connection.execute(
            "UPDATE target_reservations SET phase = 'REGISTERED_LOCKED', "
            "admin_entry_name = reservation_id, admin_binding_digest = ? "
            "WHERE run_id = ?",
            (SHA, run_id),
        )

    app.store._commit_state_and_event(
        run_id=run_id,
        expected_sequence=expected,
        event=AuditEvent.kind("TEST_PLAN_AND_HEAD_SEEDED"),
        mutate=mutate,
    )
    app.bind_run(run_id)
    return plan_digest


def make_pending_action_application(
    tmp_path: Path, action: RiskyAction
) -> tuple[PendingGrantApplication, GrantedActionIntent | None]:
    base, run_id = make_active_application(tmp_path)
    plan_digest = _seed_current_plan_and_head(base, run_id)
    revisions = base.store.current_revision_digests(run_id)
    assert revisions.policy_digest is not None
    assert revisions.budget_digest is not None
    assert revisions.model_configuration_digest is not None
    head = str(base.store.run_record(run_id).pinned_target_oid)
    target = base.store.target_reservation_for_run(run_id).admin_binding_digest
    assert target is not None
    binding = WorkerTurnBinding(
        run_id=run_id,
        task_id=TaskId("task-granted"),
        attempt_id=AttemptId("attempt-granted"),
        tranche_id="tranche-granted",
        lease_id="lease-granted",
        lease_generation=1,
        admissible_head=head,
        task_contract_digest=SHA,
        plan_digest=plan_digest,
        policy_digest=revisions.policy_digest,
        budget_digest=revisions.budget_digest,
        model_configuration_digest=revisions.model_configuration_digest,
        tool_schema_digest=SHA,
        target_safety_digest=target,
        credential_profile="default",
        repository_id="repository-granted",
        snapshot_digest=SHA,
        scope_digest=SHA,
        dependency_fingerprint_basis=SHA,
    )
    base.store.install_worker_attempt_for_test(binding)
    expected_pre_state = ActionPreState(
        source_digest=SHA,
        source_mode=None,
        destination_absent=action.operation == "rename",
    )
    started = datetime.now(UTC)
    request = AuthorizationRequest(
        run_id=run_id,
        task_id=binding.task_id,
        attempt_id=binding.attempt_id,
        logical_turn_id="turn-granted",
        action_id="action-granted",
        action=action,
        authority_origin="WORKER",
        action_digest=normalized_action_digest(action),
        expected_prestate_digest=sha256_digest(expected_pre_state.canonical_json()),
        lease_id=binding.lease_id,
        lease_generation=binding.lease_generation,
        admissible_head=binding.admissible_head,
        task_contract_digest=binding.task_contract_digest,
        plan_digest=binding.plan_digest,
        policy_digest=binding.policy_digest,
        budget_digest=binding.budget_digest,
        model_configuration_digest=binding.model_configuration_digest,
        tool_schema_digest=binding.tool_schema_digest,
        target_safety_digest=binding.target_safety_digest,
        started_at_utc=started,
        deadline_at_utc=started + timedelta(seconds=120),
        expected_sequence=base.store.audit_sequence(run_id),
    )
    decision = AuthorityService(journal=base.store).authorize_action(request)
    assert decision.decision == "REQUIRE_APPROVAL", decision
    base.store.freeze_authorized_pending_action(
        request=request,
        decision=decision,
        expected_prestate=expected_pre_state,
        recovered_marker=None,
        permit=None,
        expected_sequence=request.expected_sequence,
    )
    workspace = RecordingWorkspace({action.path: b"old"})
    tools = ScopedToolRuntime(
        snapshot=workspace,
        read_globs=("**",),
        secret_paths=SecretPathPolicy.from_host_rules((), b"installation-key"),
        authorization_binding_digest=SHA,
        applicable_revision_digests=ApplicableRevisionDigests(),
        repository_id="repository-1",
        snapshot_digest=SHA,
        scope_digest=SHA,
        dependency_fingerprint_basis=SHA,
        granted_workspace=workspace,
    )
    application = PendingGrantApplication(
        base=base,
        run_id=run_id,
        workspace=workspace,
        tools=tools,
        granted_runtime=GrantedActionRuntime(base.store, tools),
    )
    return application, None


def exact_grant_command(
    app: PendingGrantApplication, request_id: str = "grant-exact"
) -> CommandEnvelope:
    pending = app.store.pending_action(app.run_id)
    return CommandEnvelope(
        request_id=request_id,
        expected_sequence=app.store.audit_sequence(app.run_id),
        applicable_revision_digests=pending.bindings.applicable_revision_digests,
        payload=GrantPayload(
            run_id=app.run_id,
            pending_action_id=pending.pending_id,
            pending_action_digest=pending.pending_action_digest,
            confirmation_code=confirmation_code_for_pending_digest(pending.pending_action_digest),
        ),
    )


class RecordingWorkspace(MemoryRepositorySnapshot):
    def __init__(self, files: dict[str, bytes]) -> None:
        super().__init__(files)
        self.calls: list[str] = []

    def observe(self, action: RiskyAction, expected: ActionPreState) -> GrantedActionObservation:
        del action, expected
        return GrantedActionObservation(state="EXACT_PRE", digest=SHA)

    def delete_regular_file(self, action: RiskyAction, expected: ActionPreState) -> ToolResult:
        del action, expected
        self.calls.append("delete_regular_file")
        return ToolResult(code="DELETED")

    def rename_regular_file(self, action: RiskyAction, expected: ActionPreState) -> ToolResult:
        del action, expected
        self.calls.append("rename_regular_file")
        return ToolResult(code="RENAMED")

    def set_executable(self, action: RiskyAction, expected: ActionPreState) -> ToolResult:
        del action, expected
        self.calls.append("set_executable")
        return ToolResult(code="EXECUTABLE_CHANGED")

    def apply_protected_patch(self, action: RiskyAction, expected: ActionPreState) -> ToolResult:
        del action, expected
        self.calls.append("apply_protected_patch")
        return ToolResult(code="PROTECTED_PATCH_APPLIED")


def make_tool_runtime(tmp_path: Path) -> tuple[ScopedToolRuntime, RecordingWorkspace]:
    del tmp_path
    workspace = RecordingWorkspace({"src/old.py": b"old"})
    runtime = ScopedToolRuntime(
        snapshot=workspace,
        read_globs=("src/**",),
        secret_paths=SecretPathPolicy.from_host_rules((), b"installation-key"),
        authorization_binding_digest=SHA,
        applicable_revision_digests=ApplicableRevisionDigests(),
        repository_id="repository-1",
        snapshot_digest=SHA,
        scope_digest=SHA,
        dependency_fingerprint_basis=SHA,
        granted_workspace=workspace,
    )
    return runtime, workspace


def tool_intent(action: RiskyAction) -> ToolIntent:
    return ToolIntent(
        intent_id="intent-1",
        run_id="run-1",
        owner_kind="WORKER",
        task_id="task-1",
        attempt_id="attempt-1",
        action_id="action-1",
        action=action,
        authorization_binding_digest=SHA,
        applicable_revision_digests=ApplicableRevisionDigests(),
        repository_id="repository-1",
        snapshot_digest=SHA,
        scope_digest=SHA,
        dependency_fingerprint_basis=SHA,
        idempotency_key="tool-1",
    )


def granted_intent(action: RiskyAction) -> GrantedActionIntent:
    return GrantedActionIntent(
        intent_id="granted-intent-1",
        pending_id="pending-1",
        grant_id="grant-1",
        action=action,
        normalized_action_json=canonical_action_json(action),
        action_digest=normalized_action_digest(action),
        expected_pre_state=ActionPreState(source_digest=SHA, destination_absent=True),
        bindings=FrozenActionBindings(
            run_id="run-1",
            task_id="task-1",
            attempt_id="attempt-1",
            logical_turn_id="turn-1",
            action_id="action-1",
            lease_id="lease-1",
            lease_generation=1,
            run_head_oid="1" * 40,
            target_safety_digest=SHA,
            plan_digest=SHA,
            policy_digest=SHA,
            budget_digest=SHA,
            model_configuration_digest=SHA,
            tool_schema_digest=SHA,
            authorization_binding_digest=SHA,
            deadline_at_utc=__import__("datetime").datetime(
                2026, 8, 3, tzinfo=__import__("datetime").UTC
            ),
        ),
    )


def test_ungranted_risky_action_keeps_task_15_zero_effect_rejection(
    tmp_path: Path,
) -> None:
    path = tmp_path / "src" / "old.py"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"old")
    runtime, workspace = make_tool_runtime(tmp_path)

    result = runtime.execute(tool_intent(RiskyAction(operation="delete", path="src/old.py")))

    assert result.code == "APPROVAL_REQUIRED"
    assert workspace.calls == []
    assert path.read_bytes() == b"old"


def test_exact_grant_executes_pending_action_without_second_model_call(
    tmp_path: Path,
) -> None:
    app, _ = make_pending_action_application(
        tmp_path,
        RiskyAction(operation="rename", path="src/old.py", destination="src/new.py"),
    )
    pending = app.store.pending_action(app.run_id)
    granted = app.control.handle(exact_grant_command(app))
    assert granted.status == "ACCEPTED"
    intent = app.store.next_unsettled_granted_action(app.run_id)
    assert intent is not None
    permit = app.store.unconsumed_permit(app.run_id).model_copy(
        update={
            "state": "CONSUMED",
            "consumed_owner_id": "test-owner",
            "consumed_sequence": app.store.audit_sequence(app.run_id),
        }
    )
    decision = app.granted_runtime.execute(app.run_id, permit, intent.intent_id)
    assert decision.code == "ACTION_RECORDED"
    assert app.store.effect_for_pending(pending.pending_id).grant_id is not None


@pytest.mark.parametrize(
    ("action", "handler"),
    (
        (RiskyAction(operation="delete", path="src/old.py"), "delete_regular_file"),
        (
            RiskyAction(
                operation="rename",
                path="src/old.py",
                destination="src/new.py",
            ),
            "rename_regular_file",
        ),
        (
            RiskyAction(
                operation="set_executable",
                path="scripts/check.py",
                executable=True,
            ),
            "set_executable",
        ),
        (
            RiskyAction(
                operation="protected_patch",
                path=".github/workflows/ci.yml",
                unified_diff="@@ -1 +1 @@\n-old\n+new\n",
            ),
            "apply_protected_patch",
        ),
    ),
)
def test_consumed_grant_dispatches_only_its_frozen_handler(
    tmp_path: Path, action: RiskyAction, handler: str
) -> None:
    runtime, workspace = make_tool_runtime(tmp_path)

    result = runtime.execute_granted(granted_intent(action))

    assert result.code in {
        "DELETED",
        "RENAMED",
        "EXECUTABLE_CHANGED",
        "PROTECTED_PATCH_APPLIED",
    }
    assert workspace.calls == [handler]


@pytest.mark.parametrize(
    ("mutation", "failed_invariant"),
    (
        ("wrong_pending_id", "PENDING_ACTION_BINDING_INVALID"),
        ("wrong_pending_digest", "PENDING_ACTION_BINDING_INVALID"),
        ("wrong_confirmation_code", "GRANT_CONFIRMATION_CODE_INVALID"),
    ),
)
def test_wrong_grant_subject_records_only_a_rejection_receipt(
    tmp_path: Path, mutation: str, failed_invariant: str
) -> None:
    app, _ = make_pending_action_application(
        tmp_path, RiskyAction(operation="delete", path="src/old.py")
    )
    pending = app.store.pending_action(app.run_id)
    command = exact_grant_command(app)
    payload_updates = {
        "wrong_pending_id": {"pending_action_id": "pending-wrong"},
        "wrong_pending_digest": {"pending_action_digest": SHA},
        "wrong_confirmation_code": {"confirmation_code": "AAAAAA"},
    }
    command = command.model_copy(
        update={"payload": command.payload.model_copy(update=payload_updates[mutation])}
    )

    outcome = app.control.handle(command)

    assert outcome.status == "DENIED"
    assert outcome.failed_invariant == failed_invariant
    assert app.store.pending_action(pending.pending_id) == pending
    assert app.store.approval_grant_count(pending.pending_id) == 0
    assert app.store.granted_intent_count(pending.pending_id) == 0
    assert app.store.unconsumed_permit_count(app.run_id) == 0
    assert app.workspace.calls == []


def test_expired_pending_action_is_invalidated_before_grant_creation(
    tmp_path: Path,
) -> None:
    app, _ = make_pending_action_application(
        tmp_path, RiskyAction(operation="delete", path="src/old.py")
    )
    pending = app.store.pending_action(app.run_id)
    command = exact_grant_command(app, request_id="grant-expired")

    accepted = app.store.accept_pending_action_grant(
        command=command,
        now=pending.expires_at,
        expected_sequence=app.store.audit_sequence(app.run_id),
    )
    outcome = app.control.handle(command)

    assert accepted is None
    assert outcome.status == "STALE"
    assert outcome.failed_invariant == "PENDING_ACTION_EXPIRED"
    assert app.store.pending_action(pending.pending_id).state == "INVALIDATED"
    assert app.store.attempt(pending.bindings.attempt_id).state == "STALE"
    assert app.store.approval_grant_count(pending.pending_id) == 0
    assert app.store.granted_intent_count(pending.pending_id) == 0
    assert app.store.unconsumed_permit_count(app.run_id) == 0


@pytest.mark.parametrize("mutation", ("stale_lease", "stale_head"))
def test_stale_frozen_binding_invalidates_grant_without_an_intent_or_permit(
    tmp_path: Path, mutation: str
) -> None:
    app, _ = make_pending_action_application(
        tmp_path, RiskyAction(operation="delete", path="src/old.py")
    )
    pending = app.store.pending_action(app.run_id)
    expected = app.store.audit_sequence(app.run_id)

    def mutate(connection) -> None:  # type: ignore[no-untyped-def]
        if mutation == "stale_lease":
            connection.execute(
                "UPDATE workspace_leases SET generation = generation + 1 WHERE lease_id = ?",
                (pending.bindings.lease_id,),
            )
        else:
            connection.execute(
                "UPDATE runs SET run_head_oid = ? WHERE run_id = ?",
                ("b" * 40, app.run_id),
            )

    app.store._commit_state_and_event(
        run_id=app.run_id,
        expected_sequence=expected,
        event=AuditEvent.kind("TEST_FROZEN_BINDING_STALE"),
        mutate=mutate,
    )
    command = exact_grant_command(app, request_id=f"grant-{mutation}")

    outcome = app.control.handle(command)

    assert outcome.status == "STALE"
    assert outcome.failed_invariant == "GRANT_BINDING_MISMATCH"
    assert app.store.pending_action(pending.pending_id).state == "INVALIDATED"
    assert app.store.attempt(pending.bindings.attempt_id).state == "STALE"
    assert app.store.approval_grant_count(pending.pending_id) == 1
    assert app.store.granted_intent_count(pending.pending_id) == 0
    assert app.store.unconsumed_permit_count(app.run_id) == 0


def test_identical_grant_command_replay_returns_one_receipt_and_one_intent(
    tmp_path: Path,
) -> None:
    app, _ = make_pending_action_application(
        tmp_path, RiskyAction(operation="delete", path="src/old.py")
    )
    pending = app.store.pending_action(app.run_id)
    command = exact_grant_command(app, request_id="grant-replay")

    first = app.control.handle(command)
    second = app.control.handle(command)

    assert first == second
    assert first.status == "ACCEPTED"
    assert app.store.approval_grant_count(pending.pending_id) == 1
    assert app.store.granted_intent_count(pending.pending_id) == 1
    assert app.store.unconsumed_permit_count(app.run_id) == 1
    assert app.workspace.calls == []


def test_pending_interrupt_wins_before_grant_consumption(
    tmp_path: Path,
) -> None:
    app, _ = make_pending_action_application(
        tmp_path, RiskyAction(operation="delete", path="src/old.py")
    )
    pending = app.store.pending_action(app.run_id)
    expected = app.store.audit_sequence(app.run_id)

    def mutate(connection) -> None:  # type: ignore[no-untyped-def]
        connection.execute(
            "INSERT INTO runtime_interrupts(run_id, request_id, kind, "
            "requested_sequence, state) VALUES (?, 'pause-before-grant', "
            "'PAUSE', ?, 'PENDING')",
            (app.run_id, expected + 1),
        )

    app.store._commit_state_and_event(
        run_id=app.run_id,
        expected_sequence=expected,
        event=AuditEvent.kind("TEST_INTERRUPT_COMMITTED"),
        mutate=mutate,
    )
    command = exact_grant_command(app, request_id="grant-after-pause")

    with pytest.raises(StateConflict, match="RUNTIME_INTERRUPT_PENDING"):
        app.control.handle(command)

    assert app.store.pending_action(pending.pending_id).state == "WAITING_APPROVAL"
    assert app.store.approval_grant_count(pending.pending_id) == 0
    assert app.store.granted_intent_count(pending.pending_id) == 0
    assert app.store.unconsumed_permit_count(app.run_id) == 0


@pytest.mark.parametrize("mutation", ("mutated_expiry", "mutated_pre_state", "mutated_binding"))
def test_mutated_persisted_pending_subject_cannot_create_authority(
    tmp_path: Path, mutation: str
) -> None:
    app, _ = make_pending_action_application(
        tmp_path, RiskyAction(operation="delete", path="src/old.py")
    )
    pending = app.store.pending_action(app.run_id)
    command = exact_grant_command(app, request_id=f"grant-{mutation}")
    expected = app.store.audit_sequence(app.run_id)

    def mutate(connection) -> None:  # type: ignore[no-untyped-def]
        if mutation == "mutated_expiry":
            connection.execute(
                "UPDATE pending_actions SET expires_at_utc = ? WHERE pending_id = ?",
                ((pending.expires_at + timedelta(seconds=1)).isoformat(), pending.pending_id),
            )
        elif mutation == "mutated_pre_state":
            connection.execute(
                "UPDATE pending_actions SET expected_pre_state_json = ? WHERE pending_id = ?",
                (
                    ActionPreState(source_digest="sha256:" + "3" * 64).canonical_json(),
                    pending.pending_id,
                ),
            )
        else:
            bindings = json.loads(canonical_json(asdict(pending.bindings)))
            bindings["lease_generation"] = 2
            connection.execute(
                "UPDATE pending_actions SET bindings_json = ? WHERE pending_id = ?",
                (canonical_json(bindings), pending.pending_id),
            )

    app.store._commit_state_and_event(
        run_id=app.run_id,
        expected_sequence=expected,
        event=AuditEvent.kind("TEST_PENDING_SUBJECT_MUTATED"),
        mutate=mutate,
    )
    command = command.model_copy(update={"expected_sequence": app.store.audit_sequence(app.run_id)})

    outcome = app.control.handle(command)

    assert outcome.status == "DENIED"
    assert outcome.failed_invariant == "PENDING_ACTION_BINDING_INVALID"
    assert app.store.approval_grant_count(pending.pending_id) == 0
    assert app.store.granted_intent_count(pending.pending_id) == 0
    assert app.store.unconsumed_permit_count(app.run_id) == 0
