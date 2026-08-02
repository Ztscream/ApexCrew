from __future__ import annotations

import sqlite3
import subprocess
from base64 import b32encode
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

import pytest
from helpers.application import (
    FixtureRepositoryBootstrapAuthorityService,
    FixtureTargetAuthorityDigestService,
    approve_current_policy_budget_and_model,
    create_draft_with_three_proposals,
    make_application,
)

from apexcrew.adapters.repository.git import (
    GitCommandRunner,
    GitCreatePrivateRef,
    RepositoryUnsafeError,
)
from apexcrew.adapters.system import SystemMonotonicClock
from apexcrew.application.control import ControlCommandService, CrewControlService
from apexcrew.application.runtime import InjectedProcessCrash, PrivateRefInitializer
from apexcrew.domain.admission import (
    PrivateRefAdmissionPort,
    PrivateRefCasOutcome,
    RefEffectBinding,
    RefPathBinding,
    RuntimeStartBinding,
    StartGuardBinding,
    StartGuardDecision,
    private_ref,
)
from apexcrew.domain.commands import (
    ApplicableRevisionDigests,
    ApprovePlanPayload,
    CommandEnvelope,
    RuntimePermit,
    StartPayload,
)
from apexcrew.domain.coordination import PlanProposal
from apexcrew.domain.effects import AuditEvent, StateConflict, canonical_json
from apexcrew.domain.plan import CheckDefinition, GlobPattern, PlanRevision, TaskContract
from apexcrew.domain.revisions import Sha256DigestText
from apexcrew.domain.types import (
    AuditSequence,
    GitOid,
    RevisionDigest,
    RunId,
    RunState,
    RuntimeOwnerId,
    TaskId,
)


def _approval_code(run_id: RunId, digest: RevisionDigest) -> str:
    payload = canonical_json(
        {
            "command_kind": "approve_plan",
            "revision_class": "PLAN",
            "revision_digest": digest,
            "run_id": run_id,
        }
    ).encode("utf-8")
    return b32encode(sha256(payload).digest()).decode("ascii")[:6]


def _proposal(run_id: RunId, bindings: ApplicableRevisionDigests) -> PlanProposal:
    check = CheckDefinition(
        argv=("pytest", "-q"),
        input_globs=(GlobPattern.parse("src/**"),),
    )
    task = TaskContract.from_strings(
        TaskId("task-01"),
        ("src/**",),
        ("src/task.py",),
        checks=(check,),
        constraints=("offline",),
    )
    plan = PlanRevision(tasks=(task,), proposed_promotion_order=(task.task_id,))
    document = canonical_json(
        {
            "proposed_promotion_order": [task.task_id],
            "run_checks": [{"argv": list(check.argv), "input_globs": ["src/**"]}],
            "tasks": [
                {
                    "checks": [{"argv": list(check.argv), "input_globs": ["src/**"]}],
                    "constraints": ["offline"],
                    "dependency_globs": [],
                    "dependency_task_ids": [],
                    "read_globs": ["src/**"],
                    "task_id": task.task_id,
                    "write_globs": ["src/task.py"],
                }
            ],
        }
    )
    return PlanProposal.from_validated_plan(
        run_id=run_id,
        canonical_plan_json=document,
        plan=plan,
        base_run_head_oid=GitOid("a" * 40),
        applicable_revision_digests=bindings,
        run_check_set=(check,),
        planning_request_count=1,
    )


def _seed_valid_plan(tmp_path: Path):
    app = make_application(tmp_path, monotonic_clock=SystemMonotonicClock())
    run_id = create_draft_with_three_proposals(app)
    approve_current_policy_budget_and_model(app, run_id)
    bindings = app.store.current_revision_digests(run_id)
    proposal = _proposal(run_id, bindings)
    expected = app.store.audit_sequence(run_id)

    def mutate(connection: sqlite3.Connection) -> None:
        connection.execute("UPDATE runs SET state = 'PLANNING' WHERE run_id = ?", (run_id,))
        connection.execute(
            "UPDATE target_reservations SET phase = 'REGISTERED_LOCKED', "
            "admin_entry_name = reservation_id, admin_binding_digest = ? WHERE run_id = ?",
            ("sha256:" + "b" * 64, run_id),
        )
        connection.execute(
            "INSERT INTO run_authority_counters(run_id, planning_requests) VALUES (?, 1)",
            (run_id,),
        )

    app.store._commit_state_and_event(
        run_id=run_id,
        expected_sequence=expected,
        event=AuditEvent.kind("TEST_VALID_PLAN_PRESTATE"),
        mutate=mutate,
    )
    app.store.persist_plan_proposal(proposal, expected_sequence=app.store.audit_sequence(run_id))
    return app, run_id, proposal


def _ref_binding() -> RefEffectBinding:
    absent = RefPathBinding(state="ABSENT")
    return RefEffectBinding(
        repository_instance_digest=Sha256DigestText("sha256:" + "5" * 64),
        checkout_registration_digest=Sha256DigestText("sha256:" + "6" * 64),
        ref_file=absent,
        ref_lock=absent,
        reflog=absent,
        reflog_lock=absent,
        reflog_exists=False,
        reflog_message="ApexCrew initialize run head",
    )


@dataclass
class ScriptedStartGuard:
    store: object
    allow: bool = True

    def inspect(
        self,
        *,
        run_id: RunId,
        applicable_revision_digests: ApplicableRevisionDigests,
        expected_sequence: AuditSequence,
    ) -> StartGuardDecision:
        if not self.allow:
            return StartGuardDecision(ok=False, reason="PRIVATE_REF_CONFLICT")
        reservation = self.store.target_reservation_for_run(run_id)
        run = self.store.run_record(run_id)
        return StartGuardDecision(
            ok=True,
            binding=StartGuardBinding(
                run_id=run_id,
                repository_id=run.repository_id,
                target_reservation_id=reservation.reservation_id,
                pinned_target_oid=run.pinned_target_oid,
                target_safety_digest=self.store.target_authority_digest(run_id),
                ref_effect_binding=_ref_binding(),
                applicable_revision_digests=applicable_revision_digests,
            ),
        )

    def validate_consumed(
        self,
        *,
        binding: RuntimeStartBinding,
        permit: RuntimePermit,
        expected_sequence: AuditSequence,
    ) -> StartGuardDecision:
        if (
            not self.allow
            or permit.consumed_owner_id != binding.consumed_owner_id
            or permit.consumed_sequence != binding.consumed_sequence
            or expected_sequence != binding.sequence
        ):
            return StartGuardDecision(ok=False, reason="START_GUARD_DENIED")
        return StartGuardDecision(ok=True, binding=binding.guard)


@dataclass
class RecordingPrivateRefAdmission(PrivateRefAdmissionPort):
    crash: bool = False
    unobservable: bool = False
    calls: int = 0

    def initialize_private_ref(self, intent):
        self.calls += 1
        if self.crash:
            raise InjectedProcessCrash()
        if self.unobservable:
            return PrivateRefCasOutcome(
                intent_id=intent.intent_id,
                run_id=intent.run_id,
                result_class="PRIVATE_REF_UNOBSERVABLE",
                observed_oid=None,
            )
        return PrivateRefCasOutcome(
            intent_id=intent.intent_id,
            run_id=intent.run_id,
            result_class="PRIVATE_REF_INITIALIZED",
            observed_oid=intent.prepared_oid,
        )


def _control(app, guard: ScriptedStartGuard) -> CrewControlService:
    return CrewControlService(
        ControlCommandService(
            state=app.store,
            target_authority=FixtureTargetAuthorityDigestService(app.store),
            repository_authority=FixtureRepositoryBootstrapAuthorityService(),
            start_guard=guard,
        )
    )


def _approve(app, run_id: RunId, proposal: PlanProposal, guard: ScriptedStartGuard):
    return _control(app, guard).handle(
        CommandEnvelope(
            request_id="approve-plan",
            expected_sequence=app.store.audit_sequence(run_id),
            applicable_revision_digests=app.store.current_revision_digests(run_id),
            payload=ApprovePlanPayload(
                run_id=run_id,
                plan_digest=proposal.plan_digest,
                confirmation_code=_approval_code(run_id, proposal.plan_digest),
            ),
        )
    )


def _start(app, run_id: RunId, proposal: PlanProposal, guard: ScriptedStartGuard):
    return _control(app, guard).handle(
        CommandEnvelope(
            request_id="start-run",
            expected_sequence=app.store.audit_sequence(run_id),
            applicable_revision_digests=app.store.current_revision_digests(run_id),
            payload=StartPayload(run_id=run_id, plan_digest=proposal.plan_digest),
        )
    )


def test_plan_approval_binds_exact_graph_and_creates_no_effect(tmp_path: Path) -> None:
    app, run_id, proposal = _seed_valid_plan(tmp_path)
    guard = ScriptedStartGuard(app.store)

    outcome = _approve(app, run_id, proposal, guard)

    assert outcome.status == "ACCEPTED"
    assert app.store.run_record(run_id).state == RunState.READY_TO_START
    assert app.store.plan_approval(run_id).plan_digest == proposal.plan_digest
    assert app.store.run_ref(run_id, "PRIVATE").state == "ABSENT_EXPECTED"
    assert app.store.unsettled_intents(run_id) == ()


def test_plan_approval_and_private_ref_state_survive_restart(tmp_path: Path) -> None:
    app, run_id, proposal = _seed_valid_plan(tmp_path)
    guard = ScriptedStartGuard(app.store)
    _approve(app, run_id, proposal, guard)
    app.store.close()

    from apexcrew.adapters.state.sqlite import SqliteStateStore

    reopened = SqliteStateStore(app.database)
    assert reopened.plan_approval(run_id).plan_digest == proposal.plan_digest
    assert reopened.run_record(run_id).state == RunState.READY_TO_START
    assert reopened.run_ref(run_id, "PRIVATE").state == "ABSENT_EXPECTED"


def test_plan_approval_cannot_bind_a_different_plan_digest(tmp_path: Path) -> None:
    app, run_id, _proposal_value = _seed_valid_plan(tmp_path)
    guard = ScriptedStartGuard(app.store)
    foreign = RevisionDigest("sha256:" + "9" * 64)
    before = app.store.audit_sequence(run_id)

    outcome = _control(app, guard).handle(
        CommandEnvelope(
            request_id="approve-foreign-plan",
            expected_sequence=before,
            applicable_revision_digests=app.store.current_revision_digests(run_id),
            payload=ApprovePlanPayload(
                run_id=run_id,
                plan_digest=foreign,
                confirmation_code=_approval_code(run_id, foreign),
            ),
        )
    )

    assert outcome.status == "STALE"
    assert app.store.run_record(run_id).state == RunState.AWAITING_PLAN_APPROVAL
    with pytest.raises(StateConflict, match="PLAN_APPROVAL_NOT_FOUND"):
        app.store.plan_approval(run_id)


def test_failed_start_inspection_has_zero_durable_effect(tmp_path: Path) -> None:
    app, run_id, proposal = _seed_valid_plan(tmp_path)
    allow = ScriptedStartGuard(app.store)
    _approve(app, run_id, proposal, allow)
    denied = ScriptedStartGuard(app.store, allow=False)
    before = app.store.audit_sequence(run_id)

    outcome = _start(app, run_id, proposal, denied)

    assert outcome.status == "CONFLICT"
    assert app.store.audit_sequence(run_id) == before
    assert app.store.unconsumed_permit_count(run_id) == 0
    assert app.store.unsettled_intents(run_id) == ()


def test_consumed_owner_mismatch_records_no_ref_intent(tmp_path: Path) -> None:
    app, run_id, proposal = _seed_valid_plan(tmp_path)
    guard = ScriptedStartGuard(app.store)
    _approve(app, run_id, proposal, guard)
    assert _start(app, run_id, proposal, guard).status == "ACCEPTED"
    permit = app.store.consume_current_runtime_permit(
        run_id,
        RuntimeOwnerId("owner-1"),
        app.store.audit_sequence(run_id),
    )
    assert permit is not None
    admission = RecordingPrivateRefAdmission()
    driver = PrivateRefInitializer(app.store, guard, admission)

    decision = driver.initialize(
        run_id,
        permit.model_copy(update={"consumed_owner_id": RuntimeOwnerId("foreign-owner")}),
    )

    assert decision.stop_reason == "START_GUARD_DENIED"
    assert admission.calls == 0
    assert app.store.unsettled_intents(run_id) == ()


def test_private_ref_initialization_updates_only_private_run_head(tmp_path: Path) -> None:
    app, run_id, proposal = _seed_valid_plan(tmp_path)
    guard = ScriptedStartGuard(app.store)
    _approve(app, run_id, proposal, guard)
    _start(app, run_id, proposal, guard)
    permit = app.store.consume_current_runtime_permit(
        run_id,
        RuntimeOwnerId("owner-1"),
        app.store.audit_sequence(run_id),
    )
    assert permit is not None

    decision = PrivateRefInitializer(app.store, guard, RecordingPrivateRefAdmission()).initialize(
        run_id, permit
    )

    assert decision.phase_transition == "PRIVATE_REF_INITIALIZED"
    assert app.store.run_record(run_id).state == RunState.ACTIVE
    ref = app.store.run_ref(run_id, "PRIVATE")
    assert ref.ref_name == private_ref(run_id)
    assert ref.current_oid == GitOid("a" * 40)
    assert ref.state == "PRESENT"
    assert app.store.run_record(run_id).target_ref == "refs/heads/main"


def test_process_crash_leaves_one_durable_private_ref_intent(tmp_path: Path) -> None:
    app, run_id, proposal = _seed_valid_plan(tmp_path)
    guard = ScriptedStartGuard(app.store)
    _approve(app, run_id, proposal, guard)
    _start(app, run_id, proposal, guard)
    permit = app.store.consume_current_runtime_permit(
        run_id,
        RuntimeOwnerId("owner-1"),
        app.store.audit_sequence(run_id),
    )
    assert permit is not None

    with pytest.raises(InjectedProcessCrash):
        PrivateRefInitializer(
            app.store, guard, RecordingPrivateRefAdmission(crash=True)
        ).initialize(run_id, permit)
    intent = app.store.unsettled_intents(run_id)
    assert len(intent) == 1
    assert intent[0].kind == "private_ref_init"
    app.store.close()

    from apexcrew.adapters.state.sqlite import SqliteStateStore

    reopened = SqliteStateStore(app.database)
    assert reopened.unsettled_intents(run_id) == intent
    assert reopened.run_ref(run_id, "PRIVATE").state == "INIT_INTENT_RECORDED"


def test_unobservable_private_ref_result_preserves_recoverable_intent(tmp_path: Path) -> None:
    app, run_id, proposal = _seed_valid_plan(tmp_path)
    guard = ScriptedStartGuard(app.store)
    _approve(app, run_id, proposal, guard)
    _start(app, run_id, proposal, guard)
    permit = app.store.consume_current_runtime_permit(
        run_id,
        RuntimeOwnerId("owner-1"),
        app.store.audit_sequence(run_id),
    )
    assert permit is not None

    decision = PrivateRefInitializer(
        app.store,
        guard,
        RecordingPrivateRefAdmission(unobservable=True),
    ).initialize(run_id, permit)

    assert decision.stop_reason == "INDETERMINATE"
    assert app.store.run_record(run_id).state == RunState.INDETERMINATE
    assert app.store.run_ref(run_id, "PRIVATE").state == "INIT_INTENT_RECORDED"


@dataclass
class RecordingGitSpawner:
    argv: tuple[str, ...] | None = None

    def run(self, argv, cwd, environment, *, text):
        del cwd, environment, text
        self.argv = argv
        return subprocess.CompletedProcess(argv, 0, "", "")


@dataclass
class StableRepositoryStub:
    root: Path

    def assert_stable_for(self, operation: object) -> None:
        del operation


def test_typed_private_ref_create_cannot_target_the_target_branch(tmp_path: Path) -> None:
    spawner = RecordingGitSpawner()
    runner = GitCommandRunner(tmp_path / "git", tmp_path / "trusted", spawner)
    repository = StableRepositoryStub(tmp_path)
    operation = GitCreatePrivateRef(
        "refs/apexcrew/runs/run-1",
        GitOid("a" * 40),
        "ApexCrew initialize run head",
    )

    runner.run(repository, operation)  # type: ignore[arg-type]

    assert spawner.argv is not None
    assert spawner.argv[-7:] == (
        "update-ref",
        "--create-reflog",
        "-m",
        "ApexCrew initialize run head",
        "refs/apexcrew/runs/run-1",
        "a" * 40,
        "",
    )
    with pytest.raises(RepositoryUnsafeError, match="GIT_PRIVATE_REF_OPERAND_INVALID"):
        runner.run(  # type: ignore[arg-type]
            repository,
            GitCreatePrivateRef(
                "refs/heads/main",
                GitOid("a" * 40),
                "ApexCrew initialize run head",
            ),
        )
