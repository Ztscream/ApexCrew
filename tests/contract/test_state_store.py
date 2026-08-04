import sqlite3
from base64 import b32encode
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from threading import Barrier, Event, Lock

import pytest
from helpers.application import (
    FixtureRepositoryBootstrapAuthorityService,
    fixture_policy,
    make_create_run_command,
)

from apexcrew.adapters.model.scripted import ScriptedMockLLM, ScriptedModelStep
from apexcrew.adapters.state.memory import InMemoryStateStore
from apexcrew.adapters.state.sqlite import SqliteStateStore
from apexcrew.domain.actions import (
    CheckAction,
    FinishAction,
    PatchAction,
    ReadAction,
    RiskyAction,
    SearchAction,
)
from apexcrew.domain.admission import (
    RefEffectBinding,
    RefPathBinding,
    StartGuardBinding,
    StartGuardDecision,
    TargetReservationCreationOutcome,
)
from apexcrew.domain.authority import (
    AuthorityService,
    AuthorizationDecision,
    AuthorizationRequest,
    CheckpointKey,
    ModelReservationRequest,
    MonotonicInstant,
    TaskAuthority,
    confirmation_code_for_pending_digest,
)
from apexcrew.domain.commands import (
    ApplicableRevisionDigests,
    ApproveBudgetPayload,
    ApproveModelConfigurationPayload,
    ApprovePolicyPayload,
    BeginPlanningPayload,
    CommandEnvelope,
    CommandOutcome,
    GrantPayload,
    PausePayload,
    ProposePolicyPayload,
    RevisionApprovalPreview,
    RevisionApprovalResult,
    RunStop,
    StartPayload,
)
from apexcrew.domain.effects import (
    AuditEvent,
    EffectIntent,
    EffectJournal,
    EffectResult,
    PlanApproval,
    RecoveryOutcome,
    RecoveryService,
    ReservationObservation,
    RunRefRecord,
    StateCommitFault,
    StateConflict,
    TargetReservation,
    canonical_json,
)
from apexcrew.domain.model import (
    DurableModelClient,
    LogicalModelTurn,
    ModelBudgetAmounts,
    ModelCompletion,
    ModelRecoveryBinding,
    ModelRequest,
    ModelRequestIntent,
    ModelUsage,
    ProviderAttemptResult,
)
from apexcrew.domain.revisions import (
    BudgetRevisionDocument,
    ModelPricingEntryDocument,
    revision_digest,
)
from apexcrew.domain.tools import ActionPreState, ToolIntent, ToolResult
from apexcrew.domain.types import (
    AttemptId,
    AuditSequence,
    CommandStatus,
    GitOid,
    IntentId,
    RepositoryId,
    RevisionDigest,
    RunId,
    RunState,
    RunStopReason,
    RuntimeOwnerId,
    TaskId,
)
from apexcrew.domain.worker import WorkerTurnBinding, normalized_action_digest


def make_model_request(allowed_model_ids: set[str]) -> ModelRequest:
    return ModelRequest(
        run_id="run-1",
        plan_digest=None,
        policy_digest="sha256:" + "3" * 64,
        budget_digest="sha256:" + "4" * 64,
        model_configuration_digest="sha256:" + "5" * 64,
        requested_model_id="gpt-5.6-terra",
        allowed_model_ids=frozenset(allowed_model_ids),
        prompt=({"role": "user", "content": "finish"},),
        tool_schema_digest="sha256:" + "1" * 64,
        request_digest="sha256:" + "2" * 64,
        idempotency_key="request-1",
        max_input_tokens=1_000,
        max_output_tokens=200,
        reserved_cost_usd=Decimal("0.01"),
    )


def make_authority(store: SqliteStateStore, run_id: str) -> AuthorityService:
    budget = BudgetRevisionDocument(
        schema_version="budget-revision-v1",
        active_run_seconds_ceiling=28_800,
        task_ceiling=12,
        planning_request_ceiling=8,
        model_call_ceiling=240,
        input_token_ceiling=2_000_000,
        output_token_ceiling=200_000,
        cost_reserve_usd=Decimal(10),
        concurrent_worker_ceiling=3,
        pricing_observed_on=datetime(2026, 7, 26, tzinfo=UTC).date(),
        pricing_entries=(
            ModelPricingEntryDocument(
                returned_model_id="gpt-5.6-terra",
                input_usd_per_million=Decimal("2.50"),
                output_usd_per_million=Decimal("15.00"),
            ),
        ),
    )
    digest = revision_digest(budget)
    store.install_approved_budget_for_test(run_id, digest, budget)
    return AuthorityService(store)


def planning_reservation_request(
    store: SqliteStateStore, run_id: str, expected_sequence: int
) -> ModelReservationRequest:
    budget_digest, _ = store.current_approved_budget(run_id)
    request = replace(
        make_model_request({"gpt-5.6-terra"}),
        run_id=run_id,
        budget_digest=budget_digest,
        request_digest="sha256:" + ("a" if run_id == "run-a" else "b") * 64,
        idempotency_key=f"request-{run_id}",
    )
    started = datetime(2026, 7, 27, tzinfo=UTC)
    return ModelReservationRequest(
        run_id=run_id,
        owner_kind="PLANNING",
        task_id=None,
        attempt_id=None,
        tranche_id=None,
        turn=None,
        model_request=request,
        provider_attempt_number=1,
        target_safety_digest="sha256:" + "c" * 64,
        credential_profile="default",
        expected_run_counters=store.model_counters(run_id),
        expected_task_counters=None,
        started_at_utc=started,
        deadline_at_utc=started + timedelta(minutes=2),
        expected_sequence=expected_sequence,
    )


def test_sqlite_authority_model_reservation_is_run_bound_and_stale_safe(
    tmp_path: Path,
) -> None:
    store = SqliteStateStore(tmp_path / "state.db")
    authority = make_authority(store, run_id="run-a")
    make_authority(store, run_id="run-b")
    first = authority.reserve_model_attempt(planning_reservation_request(store, "run-a", 0))
    stale = authority.reserve_model_attempt(planning_reservation_request(store, "run-b", 1))
    assert first.decision == "RESERVED"
    assert first.reserved_amounts == ModelBudgetAmounts(
        calls=1,
        input_tokens=1_000,
        output_tokens=200,
        cost_usd=Decimal("0.0055"),
    )
    assert stale.decision == "DENY"
    assert stale.reason == "STALE_SEQUENCE"
    assert store.model_counters("run-a").calls == 1
    assert store.model_counters("run-b").calls == 0
    assert store.audit_sequence("run-a") == 1
    assert store.audit_sequence("run-b") == 0


def test_sqlite_authorized_model_attempt_retries_preserve_requested_model_anchor(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.db"
    store = SqliteStateStore(database)
    authority = make_authority(store, run_id="run-anchor")
    first_request = planning_reservation_request(store, "run-anchor", 0)
    first = authority.reserve_model_attempt(first_request)
    assert first.decision == "RESERVED"
    assert first.turn is not None
    assert first.intent is not None
    store.settle_model_attempt(
        first.intent,
        ProviderAttemptResult.known_closed("response-1", "TRANSIENT_REJECTION"),
        expected_sequence=store.audit_sequence("run-anchor"),
    )
    retry_request = replace(
        first_request,
        turn=first.turn,
        provider_attempt_number=2,
        expected_run_counters=store.model_counters("run-anchor"),
        expected_sequence=store.audit_sequence("run-anchor"),
    )
    retry = authority.reserve_model_attempt(retry_request)
    assert retry.decision == "RESERVED"
    assert retry.intent is not None
    store.settle_model_attempt(
        retry.intent,
        ProviderAttemptResult.completed(
            ModelCompletion(
                response_id="response-2",
                requested_model_id="gpt-5.6-terra",
                returned_model_id="gpt-5.6-terra",
                usage=ModelUsage(120, 12, Decimal("0.00048")),
                normalized_action={"kind": "finish"},
            )
        ),
        expected_sequence=store.audit_sequence("run-anchor"),
    )
    attempts = store.model_attempts("run-anchor", first.turn.logical_turn_id)
    store.close()

    reopened = SqliteStateStore(database)
    recovery_model = ScriptedMockLLM([])
    recovered = DurableModelClient(model=recovery_model, journal=reopened).recover_committed(
        "run-anchor",
        first.turn.logical_turn_id,
        ModelRecoveryBinding.from_request(first_request.model_request),
    )

    assert [attempt.request.requested_model_id for attempt in attempts] == [
        "gpt-5.6-terra",
        "gpt-5.6-terra",
    ]
    assert recovered.outcome == "COMPLETED"
    assert recovered.normalized_action == {"kind": "finish"}
    assert recovery_model.call_count == 0


def seeded_authority_store(database: Path, run_id: str) -> SqliteStateStore:
    store = SqliteStateStore(database)
    seed_command_run(store, database.parent, run_id)
    return store


@dataclass
class FixedMonotonicClock:
    instant: MonotonicInstant
    readings: int = 0

    def now(self) -> MonotonicInstant:
        self.readings += 1
        return self.instant


def test_active_runtime_account_survives_restart_and_rejects_clock_regression(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.db"
    first = seeded_authority_store(database, run_id="run-time")
    with first._transaction("IMMEDIATE") as connection:
        connection.execute(
            "UPDATE runs SET active_runtime_nanoseconds = ?, "
            "runtime_interval_owner_generation = ?, "
            "runtime_interval_opened_nanoseconds = ? WHERE run_id = ?",
            (7_000_000_000, 4, 100_000_000_000, "run-time"),
        )
        connection.execute(
            "UPDATE audit_events SET runtime_owner_generation = ?, "
            "runtime_monotonic_nanoseconds = ? WHERE run_id = ? AND sequence = "
            "(SELECT MAX(sequence) FROM audit_events WHERE run_id = ?)",
            (4, 103_000_000_000, "run-time", "run-time"),
        )
    first.close()
    clock = FixedMonotonicClock(MonotonicInstant(105_000_000_000))
    reopened = SqliteStateStore(database, monotonic_clock=clock)
    state = reopened.active_run_time_state(RunId("run-time"))
    assert state.cumulative_nanoseconds == 7_000_000_000
    assert state.open_owner_generation == 4
    assert state.latest_committed_at == MonotonicInstant(103_000_000_000)
    assert state.observed_nanoseconds(clock.now()) == 12_000_000_000
    with pytest.raises(ValueError, match="MONOTONIC_CLOCK_REGRESSED"):
        state.observed_nanoseconds(MonotonicInstant(99_999_999_999))
    event = reopened.last_runtime_audit_event(RunId("run-time"), owner_generation=4)
    assert event is not None
    assert event.owner_generation == 4
    assert event.monotonic_instant == MonotonicInstant(103_000_000_000)


def memory_store_factory(tmp_path: Path) -> InMemoryStateStore:
    del tmp_path
    return InMemoryStateStore()


def sqlite_store_factory(tmp_path: Path) -> SqliteStateStore:
    return SqliteStateStore(tmp_path / "state.db")


def memory_runtime_store_factory(tmp_path: Path, clock: FixedMonotonicClock) -> InMemoryStateStore:
    del tmp_path
    return InMemoryStateStore(monotonic_clock=clock)


def sqlite_runtime_store_factory(tmp_path: Path, clock: FixedMonotonicClock) -> SqliteStateStore:
    return SqliteStateStore(tmp_path / "state.db", monotonic_clock=clock)


@pytest.mark.parametrize("store_factory", [memory_store_factory, sqlite_store_factory])
def test_task_and_attempt_lifecycle_storage_matches(
    tmp_path: Path,
    store_factory: Callable[[Path], InMemoryStateStore | SqliteStateStore],
) -> None:
    store = store_factory(tmp_path)
    task = TaskAuthority(
        run_id=RunId("run-lifecycle"),
        task_id=TaskId("task-1"),
        attempt_id=AttemptId("attempt-1"),
    )

    store.install_running_attempt_for_test(task)

    assert store.task_lifecycle_state(task.run_id, task.task_id) == "ACTIVE"
    assert store.attempt_lifecycle_state(task.run_id, task.attempt_id) == "RUNNING"


@pytest.mark.parametrize("store_factory", [memory_store_factory, sqlite_store_factory])
def test_task_stop_cap_is_not_caller_selectable(
    tmp_path: Path,
    store_factory: Callable[[Path], InMemoryStateStore | SqliteStateStore],
) -> None:
    store = store_factory(tmp_path)
    make_authority(store, run_id="run-cap")
    task = TaskAuthority(
        run_id=RunId("run-cap"),
        task_id=TaskId("task-cap"),
        attempt_id=AttemptId("attempt-cap"),
    )
    store.install_running_attempt_for_test(task)
    budget_digest, _ = store.current_approved_budget(task.run_id)

    with pytest.raises(TypeError, match="ceiling"):
        store.record_task_checkpoint(
            task,
            CheckpointKey("1" * 40, "sha256:" + "2" * 64),
            budget_digest,
            AuditSequence(0),
            ceiling=1,
        )
    with pytest.raises(TypeError, match="ceiling"):
        store.record_invalid_action(
            task,
            task.attempt_id,
            "sha256:" + "3" * 64,
            budget_digest,
            AuditSequence(0),
            ceiling=1,
        )

    assert store.task_lifecycle_state(task.run_id, task.task_id) == "ACTIVE"
    assert store.audit_sequence(task.run_id) == 0


def test_sqlite_model_reservation_survives_restart(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    request = make_model_request(allowed_model_ids={"gpt-5.6-terra"})
    first = SqliteStateStore(database)
    before = first.audit_sequence(request.run_id)
    intent = first.reserve_model_request(request, expected_sequence=before)
    assert first.audit_sequence(request.run_id) == before + 1
    first.close()
    reopened = SqliteStateStore(database)
    restored = reopened.model_request(request.run_id, intent.intent_id)
    assert restored.intent_id == intent.intent_id
    assert restored.logical_turn_id == intent.logical_turn_id
    assert restored.request.request_digest == request.request_digest
    assert restored.request.allowed_model_ids == frozenset({"gpt-5.6-terra"})
    assert reopened.reserved_call_count(request.run_id) == 1


def test_sqlite_worker_model_owner_survives_restart(tmp_path: Path) -> None:
    request = replace(
        make_model_request(allowed_model_ids={"gpt-5.6-terra"}),
        owner_kind="WORKER",
        task_id=TaskId("task-1"),
        attempt_id=AttemptId("attempt-1"),
        tranche_id="tranche-1",
    )
    database = tmp_path / "state.db"
    first = SqliteStateStore(database)
    intent = first.reserve_model_request(
        request, expected_sequence=first.audit_sequence(request.run_id)
    )
    first.close()
    reopened = SqliteStateStore(database)
    restored = reopened.model_request(request.run_id, intent.intent_id)
    assert restored.request == request
    assert restored.request.owner_kind == "WORKER"


def test_sqlite_model_backoff_survives_restart(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    store = SqliteStateStore(database)
    request = make_model_request(allowed_model_ids={"gpt-5.6-terra"})
    turn, intent = store.begin_model_turn_and_reserve(
        request, expected_sequence=store.audit_sequence(request.run_id)
    )
    store.settle_model_attempt(
        intent,
        ProviderAttemptResult.known_closed("reject-1", "TRANSIENT_REJECTION"),
        expected_sequence=store.audit_sequence(request.run_id),
    )
    store.record_model_backoff(
        request.run_id,
        intent.intent_id,
        seconds=1,
        expected_sequence=store.audit_sequence(request.run_id),
    )
    store.close()
    attempts = SqliteStateStore(database).model_attempts(request.run_id, turn.logical_turn_id)
    assert len(attempts) == 1
    assert attempts[0].outcome == "KNOWN_CLOSED_REJECTION"
    assert attempts[0].backoff_seconds == 1
    assert attempts[0].charged_amounts == attempts[0].reserved_amounts


@pytest.mark.parametrize("store_factory", [memory_store_factory, sqlite_store_factory])
def test_requested_model_mismatch_round_trips_for_each_state_store(
    tmp_path: Path,
    store_factory: Callable[[Path], InMemoryStateStore | SqliteStateStore],
) -> None:
    store = store_factory(tmp_path)
    request = make_model_request(allowed_model_ids={"gpt-5.6-terra"})
    completion = ModelCompletion(
        response_id="response-requested-model-mismatch",
        requested_model_id="gpt-5.6-mini",
        returned_model_id="gpt-5.6-terra",
        usage=ModelUsage(120, 12, Decimal("0.00048")),
        normalized_action={"kind": "finish"},
    )

    result = DurableModelClient(
        model=ScriptedMockLLM(
            [ScriptedModelStep.for_request(request, ProviderAttemptResult.completed(completion))]
        ),
        journal=store,
    ).complete(request)
    attempts = store.model_attempts(request.run_id, result.logical_turn_id)

    assert result.outcome == "REQUESTED_MODEL_MISMATCH"
    assert result.normalized_action is None
    assert len(attempts) == 1
    assert attempts[0].request.requested_model_id == request.requested_model_id
    assert attempts[0].dispatch_result.response_requested_model_id == "gpt-5.6-mini"
    assert attempts[0].dispatch_result.returned_model_id == "gpt-5.6-terra"
    assert attempts[0].dispatch_result.outcome == "REQUESTED_MODEL_MISMATCH"
    assert attempts[0].reported_usage == completion.usage


@pytest.mark.parametrize("store_factory", [memory_store_factory, sqlite_store_factory])
def test_model_attempt_cannot_be_settled_twice(
    tmp_path: Path,
    store_factory: Callable[[Path], InMemoryStateStore | SqliteStateStore],
) -> None:
    store = store_factory(tmp_path)
    request = make_model_request(allowed_model_ids={"gpt-5.6-terra"})
    _, intent = store.begin_model_turn_and_reserve(
        request, expected_sequence=store.audit_sequence(request.run_id)
    )
    result = ProviderAttemptResult.known_closed("reject-1", "TRANSIENT_REJECTION")
    store.settle_model_attempt(
        intent, result, expected_sequence=store.audit_sequence(request.run_id)
    )
    before = store.audit_sequence(request.run_id)
    counters = store.model_counters(request.run_id)
    with pytest.raises(StateConflict, match="MODEL_ATTEMPT_ALREADY_SETTLED"):
        store.settle_model_attempt(intent, result, expected_sequence=before)
    assert store.audit_sequence(request.run_id) == before
    assert store.model_counters(request.run_id) == counters


@pytest.mark.parametrize("store_factory", [memory_store_factory, sqlite_store_factory])
def test_unjournaled_model_attempt_cannot_be_settled(
    tmp_path: Path,
    store_factory: Callable[[Path], InMemoryStateStore | SqliteStateStore],
) -> None:
    store = store_factory(tmp_path)
    request = make_model_request(allowed_model_ids={"gpt-5.6-terra"})
    turn = LogicalModelTurn.new(request)
    intent = ModelRequestIntent.reserve(turn, request)
    with pytest.raises(StateConflict, match="MODEL_ATTEMPT_BINDING_MISMATCH"):
        store.settle_model_attempt(
            intent,
            ProviderAttemptResult.known_closed("reject-1", "TRANSIENT_REJECTION"),
            expected_sequence=store.audit_sequence(request.run_id),
        )
    assert store.audit_sequence(request.run_id) == 0


@pytest.mark.parametrize("store_factory", [memory_store_factory, sqlite_store_factory])
def test_model_reservation_and_counters_roll_back_together(
    tmp_path: Path,
    store_factory: Callable[[Path], InMemoryStateStore | SqliteStateStore],
) -> None:
    store = store_factory(tmp_path)
    request = make_model_request(allowed_model_ids={"gpt-5.6-terra"})
    before = store.audit_sequence(request.run_id)
    store.fail_next_commit_after_state_write_for_test()
    with pytest.raises(StateCommitFault, match="TEST_FAULT_AFTER_STATE_WRITE"):
        store.begin_model_turn_and_reserve(request, expected_sequence=before)
    assert store.audit_sequence(request.run_id) == before
    assert store.reserved_call_count(request.run_id) == 0
    assert store.model_counters(request.run_id).calls == 0


def make_test_effect_intent(
    *, run_id: RunId, intent_id: str, recorded_sequence: AuditSequence
) -> EffectIntent:
    payload = '{"kind":"test"}'
    return EffectIntent(
        intent_id=IntentId(intent_id),
        run_id=run_id,
        kind="TEST_EFFECT",
        idempotency_key=f"test-effect:{run_id}:{intent_id}",
        applicable_revision_digests=ApplicableRevisionDigests(),
        payload_digest="sha256:" + sha256(payload.encode("utf-8")).hexdigest(),
        normalized_payload_json=payload,
        recorded_sequence=recorded_sequence,
    )


def make_test_effect_result(
    intent: EffectIntent, *, settled_sequence: AuditSequence
) -> EffectResult:
    payload = '{"result":"POST_STATE"}'
    return EffectResult(
        intent_id=intent.intent_id,
        run_id=intent.run_id,
        outcome="COMPLETED",
        result_class="POST_STATE",
        result_digest="sha256:" + sha256(payload.encode("utf-8")).hexdigest(),
        bounded_result_json=payload,
        settled_sequence=settled_sequence,
    )


def make_pause_command(
    *,
    request_id: str,
    expected_sequence: int,
    run_id: str = "run-command",
    reason: str = "operator",
) -> CommandEnvelope:
    return CommandEnvelope(
        request_id=request_id,
        expected_sequence=expected_sequence,
        payload=PausePayload(run_id=RunId(run_id), reason=reason),
    )


def accepted_outcome(*, sequence: int, run_id: str = "run-command") -> CommandOutcome:
    return CommandOutcome(
        status=CommandStatus.ACCEPTED,
        run_id=RunId(run_id),
        resulting_sequence=AuditSequence(sequence),
    )


def seed_command_run(
    store: InMemoryStateStore | SqliteStateStore,
    tmp_path: Path,
    run_id: str,
) -> None:
    store.create_draft_with_reservation(
        RunId(run_id),
        RepositoryId(f"repository-{run_id}"),
        "sha256:" + "a" * 64,
        TargetReservation(
            reservation_id=f"reservation-{run_id}",
            run_id=RunId(run_id),
            target_ref="refs/heads/main",
            pinned_target_oid=GitOid("1" * 40),
            path=tmp_path / "data" / "reservations" / f"reservation-{run_id}",
            phase="ALLOCATED",
        ),
    )


def worker_binding(
    *, run_id: RunId, attempt_id: str, budget_digest: RevisionDigest
) -> WorkerTurnBinding:
    digest = "sha256:" + "1" * 64
    return WorkerTurnBinding(
        run_id=run_id,
        task_id=TaskId("task-worker"),
        attempt_id=AttemptId(attempt_id),
        tranche_id="tranche-worker",
        lease_id=f"lease-{attempt_id}",
        lease_generation=int(attempt_id.rsplit("-", maxsplit=1)[-1]),
        admissible_head="1" * 40,
        task_contract_digest=digest,
        plan_digest=RevisionDigest(digest),
        policy_digest=RevisionDigest(digest),
        budget_digest=budget_digest,
        model_configuration_digest=RevisionDigest(digest),
        tool_schema_digest=digest,
        target_safety_digest=digest,
        credential_profile="default",
        repository_id="repository-worker",
        snapshot_digest=digest,
        scope_digest=digest,
        dependency_fingerprint_basis=digest,
    )


@pytest.mark.parametrize("store_factory", [memory_store_factory, sqlite_store_factory])
def test_worker_malformed_actions_fail_attempt_release_lease_and_pause_identically(
    tmp_path: Path,
    store_factory: Callable[[Path], InMemoryStateStore | SqliteStateStore],
) -> None:
    store = store_factory(tmp_path)
    run_id = RunId("run-worker-invalid")
    seed_command_run(store, tmp_path, str(run_id))
    make_authority(store, str(run_id))
    budget_digest, _ = store.current_approved_budget(run_id)
    action_digest = "sha256:" + "9" * 64

    for number in range(1, 4):
        current = worker_binding(
            run_id=run_id,
            attempt_id=f"attempt-{number}",
            budget_digest=budget_digest,
        )
        store.install_worker_attempt_for_test(current)
        decision = store.record_malformed_worker_action(
            binding=current,
            logical_turn_id=f"turn-{number}",
            action_digest=action_digest,
            recovered_marker=None,
            permit=None,
            expected_sequence=store.audit_sequence(run_id),
        )
        assert decision.attempt_state == "FAILED"
        assert store.attempt(current.attempt_id).state == "FAILED"
        assert store.active_lease_for_task(current.task_id) is None

    assert decision.task_state == "PAUSED"
    assert decision.pause_reason == "REPEATED_INVALID_ACTION"
    assert store.invalid_action_count(TaskId("task-worker")) == 3
    assert store.task_record(TaskId("task-worker")).state == "PAUSED"


def _worker_authorization_request(
    binding: WorkerTurnBinding,
    *,
    action: PatchAction | CheckAction | FinishAction | RiskyAction,
    action_id: str,
    logical_turn_id: str,
    expected_sequence: AuditSequence,
) -> tuple[AuthorizationRequest, ActionPreState]:
    prestate = ActionPreState(source_digest="sha256:" + "4" * 64)
    started_at = datetime.now(UTC)
    request = AuthorizationRequest(
        run_id=binding.run_id,
        task_id=binding.task_id,
        attempt_id=binding.attempt_id,
        logical_turn_id=logical_turn_id,
        action_id=action_id,
        action=action,
        authority_origin="WORKER",
        action_digest=normalized_action_digest(action),
        expected_prestate_digest="sha256:"
        + sha256(prestate.canonical_json().encode("utf-8")).hexdigest(),
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
        started_at_utc=started_at,
        deadline_at_utc=started_at + timedelta(seconds=600 if action.kind == "check" else 120),
        expected_sequence=expected_sequence,
    )
    return request, prestate


def _worker_allow_decision(request: AuthorizationRequest) -> AuthorizationDecision:
    action_class = {
        "check": "DECLARED_CHECK",
        "finish": "FINISH",
        "patch": "PATCH",
    }[request.action.kind]
    return AuthorizationDecision(
        decision="ALLOW",
        reason="AUTHORIZED",
        run_id=request.run_id,
        task_id=request.task_id,
        attempt_id=request.attempt_id,
        action_id=request.action_id,
        action_digest=request.action_digest,
        binding_digest="sha256:" + "8" * 64,
        action_class=action_class,
        approved_timeout_seconds=600 if request.action.kind == "check" else 120,
        deadline_at_utc=request.deadline_at_utc,
        persistence="WITH_EFFECT_INTENT",
        effect_intent_id=None,
        pending_action_id=None,
        resulting_sequence=None,
    )


def _worker_approval_decision(request: AuthorizationRequest) -> AuthorizationDecision:
    return AuthorizationDecision(
        decision="REQUIRE_APPROVAL",
        reason="APPROVAL_REQUIRED",
        run_id=request.run_id,
        task_id=request.task_id,
        attempt_id=request.attempt_id,
        action_id=request.action_id,
        action_digest=request.action_digest,
        binding_digest="sha256:" + "8" * 64,
        action_class="RISKY",
        approved_timeout_seconds=120,
        deadline_at_utc=request.deadline_at_utc,
        persistence="WITH_PENDING_ACTION",
        effect_intent_id=None,
        pending_action_id=None,
        resulting_sequence=None,
    )


@pytest.mark.parametrize("store_factory", [memory_store_factory, sqlite_store_factory])
def test_granted_action_journal_contract_is_atomic_and_restart_safe(
    tmp_path: Path,
    store_factory: Callable[[Path], InMemoryStateStore | SqliteStateStore],
) -> None:
    store = store_factory(tmp_path)
    target_authority = StoreTargetAuthority(store)
    repository_authority = FixtureRepositoryBootstrapAuthorityService()
    created = store.apply_control_command(
        make_create_run_command(request_id="granted-contract-create"),
        target_authority,
        repository_authority,
    )
    assert created.run_id is not None
    run_id = created.run_id
    current = store.current_revision_digests(run_id)
    approvals = (
        (
            "granted-contract-policy",
            "approve_policy",
            "POLICY",
            current.policy_digest,
            ApprovePolicyPayload,
        ),
        (
            "granted-contract-budget",
            "approve_budget",
            "BUDGET",
            current.budget_digest,
            ApproveBudgetPayload,
        ),
        (
            "granted-contract-model",
            "approve_model_configuration",
            "MODEL_CONFIGURATION",
            current.model_configuration_digest,
            ApproveModelConfigurationPayload,
        ),
    )
    for request_id, command_kind, revision_class, digest, payload_type in approvals:
        assert digest is not None
        code = control_approval_code(command_kind, run_id, revision_class, digest)
        if payload_type is ApprovePolicyPayload:
            payload = payload_type(run_id=run_id, policy_digest=digest, confirmation_code=code)
        elif payload_type is ApproveBudgetPayload:
            payload = payload_type(run_id=run_id, budget_digest=digest, confirmation_code=code)
        else:
            payload = payload_type(
                run_id=run_id,
                model_configuration_digest=digest,
                confirmation_code=code,
            )
        outcome = store.apply_control_command(
            CommandEnvelope(
                request_id=request_id,
                expected_sequence=store.audit_sequence(run_id),
                applicable_revision_digests=approved_control_bindings(store, run_id),
                payload=payload,
            ),
            target_authority,
            repository_authority,
        )
        assert outcome.status == CommandStatus.ACCEPTED
    budget_digest, _ = store.current_approved_budget(run_id)
    current = store.current_revision_digests(run_id)
    assert current.policy_digest is not None
    assert current.model_configuration_digest is not None
    plan_digest = RevisionDigest("sha256:" + "1" * 64)
    head = str(store.run_record(run_id).pinned_target_oid)
    expected_sequence = store.audit_sequence(run_id)
    if isinstance(store, InMemoryStateStore):

        def seed_plan_and_head(copied: InMemoryStateStore) -> None:
            copied._runs[run_id] = replace(
                copied._runs[run_id],
                state=RunState.ACTIVE,
                current_plan_digest=plan_digest,
            )

    else:

        def seed_plan_and_head(connection: sqlite3.Connection) -> None:
            connection.execute(
                "UPDATE runs SET state = 'ACTIVE', current_plan_digest = ?, "
                "run_head_oid = ? "
                "WHERE run_id = ?",
                (plan_digest, head, run_id),
            )

    store._commit_state_and_event(  # type: ignore[arg-type]
        run_id=run_id,
        expected_sequence=expected_sequence,
        event=AuditEvent.kind("TEST_GRANTED_CONTRACT_PLAN_AND_HEAD_SEEDED"),
        mutate=seed_plan_and_head,
    )
    binding = worker_binding(
        run_id=run_id,
        attempt_id="attempt-1",
        budget_digest=budget_digest,
    )
    binding = replace(
        binding,
        admissible_head=head,
        plan_digest=plan_digest,
        policy_digest=current.policy_digest,
        model_configuration_digest=current.model_configuration_digest,
    )
    store.install_worker_attempt_for_test(binding)
    action = RiskyAction(operation="delete", path="src/old.py")
    request, prestate = _worker_authorization_request(
        binding,
        action=action,
        action_id="action-granted-contract",
        logical_turn_id="turn-granted-contract",
        expected_sequence=store.audit_sequence(run_id),
    )
    decision = _worker_approval_decision(request)
    frozen = store.freeze_authorized_pending_action(
        request=request,
        decision=decision,
        expected_prestate=prestate,
        recovered_marker=None,
        permit=None,
        expected_sequence=request.expected_sequence,
    )
    pending = store.pending_action(frozen.pending_id)
    command = CommandEnvelope(
        request_id="grant-contract",
        expected_sequence=store.audit_sequence(run_id),
        applicable_revision_digests=binding.applicable_revision_digests,
        payload=GrantPayload(
            run_id=run_id,
            pending_action_id=pending.pending_id,
            pending_action_digest=pending.pending_action_digest,
            confirmation_code=confirmation_code_for_pending_digest(pending.pending_action_digest),
        ),
    )
    store.fail_next_commit_after_state_write_for_test()
    with pytest.raises(StateCommitFault):
        store.accept_pending_action_grant(
            command=command,
            now=request.started_at_utc + timedelta(seconds=1),
            expected_sequence=store.audit_sequence(run_id),
        )
    assert store.pending_action(pending.pending_id) == pending
    assert store.approval_grant_count(pending.pending_id) == 0
    assert store.granted_intent_count(pending.pending_id) == 0
    assert store.unconsumed_permit_count(run_id) == 0

    intent = store.accept_pending_action_grant(
        command=command,
        now=request.started_at_utc + timedelta(seconds=1),
        expected_sequence=store.audit_sequence(run_id),
    )

    assert intent is not None
    assert store.approval_grant_count(pending.pending_id) == 1
    assert store.granted_intent_count(pending.pending_id) == 1
    assert store.pending_action(pending.pending_id).state == "GRANT_CONSUMED"
    assert store.next_unsettled_granted_action(run_id) == intent
    replayed = store.apply_control_command(command, target_authority, repository_authority)
    assert replayed.status == CommandStatus.ACCEPTED
    assert store.approval_grant_count(pending.pending_id) == 1
    assert store.granted_intent_count(pending.pending_id) == 1
    assert store.unconsumed_permit_count(run_id) == 1
    if isinstance(store, SqliteStateStore):
        store.close()
        store = SqliteStateStore(tmp_path / "state.db")
        assert store.next_unsettled_granted_action(run_id) == intent

    dispatched = store.mark_granted_action_dispatched(
        run_id=run_id,
        intent_id=intent.intent_id,
        applicable_revision_digests=binding.applicable_revision_digests,
        expected_sequence=store.audit_sequence(run_id),
    )
    settled_sequence = store.settle_granted_action(
        run_id=run_id,
        intent_id=intent.intent_id,
        result=ToolResult(code="DELETED", run_id=run_id, intent_id=intent.intent_id),
        applicable_revision_digests=binding.applicable_revision_digests,
        expected_sequence=store.audit_sequence(run_id),
    )

    assert dispatched.state == "DISPATCHED"
    assert settled_sequence == store.audit_sequence(run_id)
    assert store.next_unsettled_granted_action(run_id) is None
    assert store.pending_action(pending.pending_id).state == "SETTLED"
    assert store.effect_for_pending(pending.pending_id).state == "SETTLED"


@pytest.mark.parametrize("store_factory", [memory_store_factory, sqlite_store_factory])
def test_worker_action_intent_result_and_feedback_are_bound_identically(
    tmp_path: Path,
    store_factory: Callable[[Path], InMemoryStateStore | SqliteStateStore],
) -> None:
    store = store_factory(tmp_path)
    run_id = RunId("run-worker-action")
    seed_command_run(store, tmp_path, str(run_id))
    make_authority(store, str(run_id))
    budget_digest, _ = store.current_approved_budget(run_id)
    binding = worker_binding(
        run_id=run_id,
        attempt_id="attempt-1",
        budget_digest=budget_digest,
    )
    store.install_worker_attempt_for_test(binding)
    action = CheckAction(check_id="task-check-1")
    request, prestate = _worker_authorization_request(
        binding,
        action=action,
        action_id="action-1",
        logical_turn_id="turn-1",
        expected_sequence=store.audit_sequence(run_id),
    )
    decision = _worker_allow_decision(request)
    intent = ToolIntent.for_authorized_worker_action(
        intent_id=IntentId("intent-worker-1"),
        run_id=run_id,
        task_id=binding.task_id,
        attempt_id=binding.attempt_id,
        action_id=request.action_id,
        action=action,
        authorization_binding_digest=decision.binding_digest,
        applicable_revision_digests=binding.applicable_revision_digests,
        repository_id=binding.repository_id,
        snapshot_digest=binding.snapshot_digest,
        scope_digest=binding.scope_digest,
        dependency_fingerprint_basis=binding.dependency_fingerprint_basis,
        idempotency_key=f"worker-tool:{run_id}:{binding.attempt_id}:turn-1",
        expected_prestate_json=prestate.canonical_json(),
    )
    assert (
        store.record_authorized_worker_action(
            intent=intent,
            request=request,
            decision=decision,
            expected_prestate=prestate,
            recovered_marker=None,
            permit=None,
            expected_sequence=request.expected_sequence,
        )
        == intent
    )
    with pytest.raises(StateConflict, match="WORKER_ACTION_DUPLICATE"):
        store.record_authorized_worker_action(
            intent=intent,
            request=request,
            decision=decision,
            expected_prestate=prestate,
            recovered_marker=None,
            permit=None,
            expected_sequence=store.audit_sequence(run_id),
        )
    result = ToolResult(
        code="CHECK_FAILED",
        run_id=run_id,
        intent_id=intent.intent_id,
        passed=False,
        bounded_payload={
            "output": "expected 3.00, received 2.99",
            "output_bytes": len(b"expected 3.00, received 2.99"),
            "snapshot_digest": binding.snapshot_digest,
        },
    )
    store.settle_worker_action(
        intent=intent,
        authorization=decision,
        result=result,
        expected_sequence=store.audit_sequence(run_id),
    )
    assert "expected 3.00" in (store.latest_worker_feedback(binding.attempt_id) or "")


@pytest.mark.parametrize("store_factory", [memory_store_factory, sqlite_store_factory])
def test_worker_finish_succeeds_and_releases_lease_identically(
    tmp_path: Path,
    store_factory: Callable[[Path], InMemoryStateStore | SqliteStateStore],
) -> None:
    store = store_factory(tmp_path)
    run_id = RunId("run-worker-finish")
    seed_command_run(store, tmp_path, str(run_id))
    make_authority(store, str(run_id))
    budget_digest, _ = store.current_approved_budget(run_id)
    binding = worker_binding(
        run_id=run_id,
        attempt_id="attempt-1",
        budget_digest=budget_digest,
    )
    store.install_worker_attempt_for_test(binding)
    action = FinishAction(summary="done")
    request, _ = _worker_authorization_request(
        binding,
        action=action,
        action_id="action-finish",
        logical_turn_id="turn-finish",
        expected_sequence=store.audit_sequence(run_id),
    )
    decision = _worker_allow_decision(request)

    result = store.finish_attempt(
        binding=binding,
        logical_turn_id=request.logical_turn_id,
        action=action,
        action_digest=request.action_digest,
        authorization=decision,
        recovered_marker=None,
        permit=None,
        expected_sequence=request.expected_sequence,
    )

    assert result.code == "ACTION_RECORDED"
    assert store.attempt(binding.attempt_id).state == "SUCCEEDED"
    assert store.task_record(binding.task_id).state == "SUCCEEDED"
    assert store.active_lease_for_task(binding.task_id) is None


@pytest.mark.parametrize("store_factory", [memory_store_factory, sqlite_store_factory])
def test_plan_approval_and_private_ref_prestate_roll_back_together(
    tmp_path: Path,
    store_factory: Callable[[Path], InMemoryStateStore | SqliteStateStore],
) -> None:
    store = store_factory(tmp_path)
    run_id = RunId("run-start-rollback")
    seed_command_run(store, tmp_path, str(run_id))
    before = store.audit_sequence(run_id)
    approval = PlanApproval(
        run_id,
        RevisionDigest("sha256:" + "1" * 64),
        "approve-start-rollback",
        AuditSequence(before + 1),
        "sha256:" + "2" * 64,
    )
    ref = RunRefRecord(
        run_id,
        "PRIVATE",
        f"refs/apexcrew/runs/{run_id}",
        None,
        None,
        "ABSENT_EXPECTED",
        None,
    )
    store.fail_next_commit_after_state_write_for_test()

    if isinstance(store, InMemoryStateStore):

        def mutate_memory(copied: InMemoryStateStore) -> None:
            copied._plan_approvals[run_id] = approval
            copied._run_refs[(run_id, "PRIVATE")] = ref

        mutation = mutate_memory
    else:

        def mutate_sqlite(connection: sqlite3.Connection) -> None:
            connection.execute(
                "INSERT INTO plan_approvals VALUES (?, ?, ?, ?, ?)",
                (
                    approval.run_id,
                    approval.plan_digest,
                    approval.approval_request_id,
                    approval.approval_sequence,
                    approval.binding_digest,
                ),
            )
            connection.execute(
                "INSERT INTO run_refs(run_id, ref_kind, ref_name, state) "
                "VALUES (?, 'PRIVATE', ?, 'ABSENT_EXPECTED')",
                (run_id, ref.ref_name),
            )

        mutation = mutate_sqlite

    with pytest.raises(StateCommitFault, match="TEST_FAULT_AFTER_STATE_WRITE"):
        store._commit_state_and_event(
            run_id=run_id,
            expected_sequence=before,
            event=AuditEvent.kind("PLAN_APPROVAL_TEST"),
            mutate=mutation,
        )
    assert store.audit_sequence(run_id) == before
    with pytest.raises(StateConflict, match="PLAN_APPROVAL_NOT_FOUND"):
        store.plan_approval(run_id)
    with pytest.raises(StateConflict, match="RUN_REF_NOT_FOUND"):
        store.run_ref(run_id, "PRIVATE")


@pytest.mark.parametrize("store_factory", [memory_store_factory, sqlite_store_factory])
def test_record_command_requires_existing_run_and_uses_real_repository_binding(
    tmp_path: Path,
    store_factory: Callable[[Path], InMemoryStateStore | SqliteStateStore],
) -> None:
    store = store_factory(tmp_path)
    command = make_pause_command(request_id="cmd-bound", expected_sequence=0)
    with pytest.raises(StateConflict, match="^RUN_NOT_FOUND$"):
        store.record_command(command, accepted_outcome(sequence=1))
    assert store.audit_sequence(RunId("run-command")) == 0

    seed_command_run(store, tmp_path, "run-command")
    bound_command = make_pause_command(request_id="cmd-bound", expected_sequence=1)
    outcome = store.record_command(bound_command, accepted_outcome(sequence=2))
    assert outcome.resulting_sequence == 2
    assert store.audit_sequence(RunId("run-command")) == 2


@pytest.mark.parametrize("store_factory", [memory_store_factory, sqlite_store_factory])
def test_identical_command_replay_returns_committed_outcome(
    tmp_path: Path,
    store_factory: Callable[[Path], InMemoryStateStore | SqliteStateStore],
) -> None:
    store = store_factory(tmp_path)
    run_id = RunId("run-command")
    seed_command_run(store, tmp_path, str(run_id))
    for expected_sequence in range(1, 4):
        store.append_event(
            run_id,
            AuditEvent.kind(f"PRIOR_EVENT_{expected_sequence}"),
            AuditSequence(expected_sequence),
        )
    envelope = make_pause_command(request_id="cmd-7", expected_sequence=4)
    first = store.record_command(envelope, accepted_outcome(sequence=5))
    replay = store.record_command(envelope, accepted_outcome(sequence=99))
    assert first.resulting_sequence == 5
    assert replay == first


@pytest.mark.parametrize("store_factory", [memory_store_factory, sqlite_store_factory])
def test_request_id_reuse_conflicts_without_appending_audit(
    tmp_path: Path,
    store_factory: Callable[[Path], InMemoryStateStore | SqliteStateStore],
) -> None:
    store = store_factory(tmp_path)
    seed_command_run(store, tmp_path, "run-command")
    seed_command_run(store, tmp_path, "run-other")
    original = make_pause_command(request_id="cmd-reused", expected_sequence=1)
    store.record_command(original, accepted_outcome(sequence=2))
    changed = make_pause_command(
        request_id="cmd-reused",
        expected_sequence=1,
        run_id="run-other",
        reason="different payload",
    )
    conflict = store.record_command(changed, accepted_outcome(sequence=2, run_id="run-other"))
    assert conflict.status == CommandStatus.CONFLICT
    assert conflict.failed_invariant == "IDEMPOTENCY_KEY_REUSE"
    assert conflict.resulting_sequence == 1
    assert store.audit_sequence(RunId("run-command")) == 2
    assert store.audit_sequence(RunId("run-other")) == 1


class StoreTargetAuthority:
    def __init__(self, store: InMemoryStateStore | SqliteStateStore) -> None:
        self._store = store

    def current_for(self, run_id: RunId) -> str:
        return self._store.target_authority_digest(run_id)


def control_approval_code(
    command_kind: str,
    run_id: RunId,
    revision_class: str,
    digest: RevisionDigest,
) -> str:
    payload = canonical_json(
        {
            "command_kind": command_kind,
            "revision_class": revision_class,
            "revision_digest": digest,
            "run_id": run_id,
        }
    ).encode("utf-8")
    return b32encode(sha256(payload).digest()).decode("ascii")[:6]


def approved_control_bindings(
    store: InMemoryStateStore | SqliteStateStore, run_id: RunId
) -> ApplicableRevisionDigests:
    current = store.current_revision_digests(run_id)
    approved = frozenset(store.approved_revision_classes(run_id))
    return ApplicableRevisionDigests(
        policy_digest=current.policy_digest if "POLICY" in approved else None,
        budget_digest=current.budget_digest if "BUDGET" in approved else None,
        model_configuration_digest=(
            current.model_configuration_digest if "MODEL_CONFIGURATION" in approved else None
        ),
    )


def seed_control_permit(
    store: InMemoryStateStore | SqliteStateStore,
) -> tuple[RunId, StoreTargetAuthority]:
    target_authority = StoreTargetAuthority(store)
    repository_authority = FixtureRepositoryBootstrapAuthorityService()
    created = store.apply_control_command(
        make_create_run_command(request_id="contract-create"),
        target_authority,
        repository_authority,
    )
    assert created.run_id is not None
    run_id = created.run_id
    current = store.current_revision_digests(run_id)
    approvals = (
        (
            "contract-policy",
            "approve_policy",
            "POLICY",
            current.policy_digest,
            ApprovePolicyPayload,
        ),
        (
            "contract-budget",
            "approve_budget",
            "BUDGET",
            current.budget_digest,
            ApproveBudgetPayload,
        ),
        (
            "contract-model",
            "approve_model_configuration",
            "MODEL_CONFIGURATION",
            current.model_configuration_digest,
            ApproveModelConfigurationPayload,
        ),
    )
    for request_id, command_kind, revision_class, digest, payload_type in approvals:
        assert digest is not None
        code = control_approval_code(command_kind, run_id, revision_class, digest)
        if payload_type is ApprovePolicyPayload:
            payload = payload_type(run_id=run_id, policy_digest=digest, confirmation_code=code)
        elif payload_type is ApproveBudgetPayload:
            payload = payload_type(run_id=run_id, budget_digest=digest, confirmation_code=code)
        else:
            payload = payload_type(
                run_id=run_id,
                model_configuration_digest=digest,
                confirmation_code=code,
            )
        outcome = store.apply_control_command(
            CommandEnvelope(
                request_id=request_id,
                expected_sequence=store.audit_sequence(run_id),
                applicable_revision_digests=approved_control_bindings(store, run_id),
                payload=payload,
            ),
            target_authority,
            repository_authority,
        )
        assert outcome.status == CommandStatus.ACCEPTED
    begin = store.apply_control_command(
        CommandEnvelope(
            request_id="contract-begin",
            expected_sequence=store.audit_sequence(run_id),
            applicable_revision_digests=store.current_revision_digests(run_id),
            payload=BeginPlanningPayload(run_id=run_id),
        ),
        target_authority,
        repository_authority,
    )
    assert begin.status == CommandStatus.ACCEPTED
    return run_id, target_authority


@pytest.mark.parametrize("store_factory", [memory_store_factory, sqlite_store_factory])
def test_control_transaction_rolls_back_complete_bootstrap_state(
    tmp_path: Path,
    store_factory: Callable[[Path], InMemoryStateStore | SqliteStateStore],
) -> None:
    store = store_factory(tmp_path)
    store.fail_next_commit_after_state_write_for_test()
    command = make_create_run_command(request_id="faulted-create")
    repository_authority = FixtureRepositoryBootstrapAuthorityService()
    with pytest.raises(StateCommitFault, match="TEST_FAULT_AFTER_STATE_WRITE"):
        store.apply_control_command(command, StoreTargetAuthority(store), repository_authority)
    assert store.run_count() == 0
    accepted = store.apply_control_command(
        command, StoreTargetAuthority(store), repository_authority
    )
    assert accepted.status == CommandStatus.ACCEPTED
    assert accepted.run_id is not None
    assert store.target_reservation_count(accepted.run_id) == 1


@pytest.mark.parametrize("backend", ("memory", "sqlite"))
def test_target_reservation_id_source_retries_collisions_and_exhausts_closed(
    tmp_path: Path, backend: str
) -> None:
    first_id = "reservation-" + "1" * 32
    second_id = "reservation-" + "2" * 32
    values = iter((first_id, second_id, *(first_id for _ in range(16))))
    calls = 0

    def source() -> str:
        nonlocal calls
        calls += 1
        return next(values)

    def make_store(
        path: Path, id_source: Callable[[], object]
    ) -> InMemoryStateStore | SqliteStateStore:
        if backend == "memory":
            return InMemoryStateStore(target_reservation_id_source=id_source)
        return SqliteStateStore(path, target_reservation_id_source=id_source)

    repository_authority = FixtureRepositoryBootstrapAuthorityService()
    store = make_store(tmp_path / "state.db", source)
    target_authority = StoreTargetAuthority(store)
    first_command = make_create_run_command(request_id="reservation-first")
    first = store.apply_control_command(first_command, target_authority, repository_authority)
    second_command = make_create_run_command(request_id="reservation-second")
    second = store.apply_control_command(second_command, target_authority, repository_authority)
    assert first.status == second.status == CommandStatus.ACCEPTED
    assert first.run_id is not None
    assert second.run_id is not None
    assert store.target_reservation_for_run(first.run_id).reservation_id == first_id
    assert store.target_reservation_for_run(second.run_id).reservation_id == second_id

    before_sequences = (
        store.audit_sequence(first.run_id),
        store.audit_sequence(second.run_id),
    )
    before_revisions = (
        store.current_revision_digests(first.run_id),
        store.current_revision_digests(second.run_id),
    )
    exhausted_command = make_create_run_command(request_id="reservation-exhausted")
    exhausted = store.apply_control_command(
        exhausted_command, target_authority, repository_authority
    )
    assert exhausted.status == CommandStatus.CONFLICT
    assert exhausted.run_id is None
    assert exhausted.resulting_sequence is None
    assert exhausted.failed_invariant == "TARGET_RESERVATION_ID_EXHAUSTED"
    assert calls == 18
    assert store.run_count() == 2
    assert (
        store.target_reservation_count(first.run_id),
        store.target_reservation_count(second.run_id),
    ) == (1, 1)
    assert (
        store.audit_sequence(first.run_id),
        store.audit_sequence(second.run_id),
    ) == before_sequences
    assert (
        store.current_revision_digests(first.run_id),
        store.current_revision_digests(second.run_id),
    ) == before_revisions
    assert (
        store.apply_control_command(exhausted_command, target_authority, repository_authority)
        == exhausted
    )
    assert calls == 18

    reused_request = exhausted_command.model_copy(
        update={"payload": exhausted_command.payload.model_copy(update={"goal": "changed"})}
    )
    reuse = store.apply_control_command(reused_request, target_authority, repository_authority)
    assert reuse.status == CommandStatus.CONFLICT
    assert reuse.failed_invariant == "IDEMPOTENCY_KEY_REUSE"
    assert reuse.run_id is None
    assert reuse.resulting_sequence is None

    invalid_values = iter(("reservation-" + "A" * 32,))
    invalid_calls = 0

    def invalid_source() -> str:
        nonlocal invalid_calls
        invalid_calls += 1
        return next(invalid_values)

    invalid_store = make_store(tmp_path / "invalid.db", invalid_source)
    invalid_authority = StoreTargetAuthority(invalid_store)
    invalid_command = make_create_run_command(request_id="reservation-invalid")
    invalid = invalid_store.apply_control_command(
        invalid_command, invalid_authority, repository_authority
    )
    assert invalid.status == CommandStatus.CONFLICT
    assert invalid.run_id is None
    assert invalid.resulting_sequence is None
    assert invalid.failed_invariant == "TARGET_RESERVATION_ID_SOURCE_INVALID"
    assert invalid_store.run_count() == 0
    assert (
        invalid_store.apply_control_command(
            invalid_command, invalid_authority, repository_authority
        )
        == invalid
    )
    assert invalid_calls == 1

    non_string_calls = 0

    def non_string_source() -> object:
        nonlocal non_string_calls
        non_string_calls += 1
        return 42

    non_string_store = make_store(tmp_path / "non-string.db", non_string_source)
    non_string_authority = StoreTargetAuthority(non_string_store)
    non_string_command = make_create_run_command(request_id="reservation-non-string")
    non_string = non_string_store.apply_control_command(
        non_string_command, non_string_authority, repository_authority
    )
    assert non_string.status == CommandStatus.CONFLICT
    assert non_string.run_id is None
    assert non_string.resulting_sequence is None
    assert non_string.failed_invariant == "TARGET_RESERVATION_ID_SOURCE_INVALID"
    assert non_string_store.run_count() == 0
    assert (
        non_string_store.apply_control_command(
            non_string_command, non_string_authority, repository_authority
        )
        == non_string
    )
    assert non_string_calls == 1

    empty_calls = 0

    def exhausted_source() -> str:
        nonlocal empty_calls
        empty_calls += 1
        raise StopIteration

    empty_store = make_store(tmp_path / "empty.db", exhausted_source)
    empty_authority = StoreTargetAuthority(empty_store)
    empty_command = make_create_run_command(request_id="reservation-source-empty")
    source_exhausted = empty_store.apply_control_command(
        empty_command, empty_authority, repository_authority
    )
    assert source_exhausted.status == CommandStatus.CONFLICT
    assert source_exhausted.run_id is None
    assert source_exhausted.resulting_sequence is None
    assert source_exhausted.failed_invariant == "TARGET_RESERVATION_ID_EXHAUSTED"
    assert empty_store.run_count() == 0
    assert (
        empty_store.apply_control_command(empty_command, empty_authority, repository_authority)
        == source_exhausted
    )
    assert empty_calls == 1


@pytest.mark.parametrize(
    ("backend", "source_values", "expected_status"),
    (
        (
            "memory",
            ("reservation-" + "F" * 32, "reservation-" + "a" * 32),
            CommandStatus.CONFLICT,
        ),
        (
            "sqlite",
            ("reservation-" + "F" * 32, "reservation-" + "a" * 32),
            CommandStatus.CONFLICT,
        ),
        (
            "memory",
            ("reservation-" + "a" * 32, "reservation-" + "F" * 32),
            CommandStatus.ACCEPTED,
        ),
        (
            "sqlite",
            ("reservation-" + "a" * 32, "reservation-" + "F" * 32),
            CommandStatus.ACCEPTED,
        ),
    ),
)
def test_concurrent_create_run_arbitrates_bootstrap_receipts(
    tmp_path: Path,
    backend: str,
    source_values: tuple[str, str],
    expected_status: CommandStatus,
) -> None:
    values = iter(source_values)
    source_lock = Lock()
    source_calls = 0

    def source() -> str:
        nonlocal source_calls
        with source_lock:
            source_calls += 1
            return next(values)

    if backend == "memory":
        primary: InMemoryStateStore | SqliteStateStore = InMemoryStateStore(
            target_reservation_id_source=source
        )
        stores = (primary, primary)
        closable: tuple[SqliteStateStore, ...] = ()
    else:
        first = SqliteStateStore(tmp_path / "state.db", target_reservation_id_source=source)
        second = SqliteStateStore(tmp_path / "state.db", target_reservation_id_source=source)
        primary = first
        stores = (first, second)
        closable = (first, second)
    command = make_create_run_command(request_id=f"concurrent-create-{backend}-{expected_status}")
    repository_authority = FixtureRepositoryBootstrapAuthorityService()
    barrier = Barrier(2)

    def apply(store: InMemoryStateStore | SqliteStateStore) -> CommandOutcome:
        barrier.wait()
        return store.apply_control_command(
            command,
            StoreTargetAuthority(store),
            repository_authority,
        )

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = tuple(executor.map(apply, stores))
        assert outcomes[0] == outcomes[1]
        outcome = outcomes[0]
        assert outcome.status == expected_status
        assert source_calls == 1
        replay = primary.apply_control_command(
            command,
            StoreTargetAuthority(primary),
            repository_authority,
        )
        assert replay == outcome
        assert source_calls == 1
        if expected_status == CommandStatus.CONFLICT:
            assert outcome.run_id is None
            assert outcome.resulting_sequence is None
            assert outcome.failed_invariant == "TARGET_RESERVATION_ID_SOURCE_INVALID"
            assert primary.run_count() == 0
        else:
            assert outcome.run_id is not None
            assert outcome.resulting_sequence == 1
            assert primary.run_count() == 1
            assert primary.audit_sequence(outcome.run_id) == 1
            assert primary.target_reservation_count(outcome.run_id) == 1
    finally:
        for store in closable:
            store.close()


@pytest.mark.parametrize("backend", ("memory", "sqlite"))
@pytest.mark.parametrize("create_wins", (True, False))
def test_control_request_id_arbitrates_bootstrap_and_normal_commands(
    tmp_path: Path, backend: str, create_wins: bool
) -> None:
    source_calls = 0
    source_entered = Event()
    release_source = Event()
    create_ready = Event()
    release_create = Event()

    def invalid_source() -> str:
        nonlocal source_calls
        source_calls += 1
        source_entered.set()
        assert release_source.wait(timeout=5)
        return "reservation-" + "F" * 32

    if backend == "memory":
        primary: InMemoryStateStore | SqliteStateStore = InMemoryStateStore(
            target_reservation_id_source=invalid_source
        )
        create_store = primary
        normal_store = primary
        closable: tuple[SqliteStateStore, ...] = ()
    else:
        database = tmp_path / "state.db"
        create_store = SqliteStateStore(database, target_reservation_id_source=invalid_source)
        normal_store = SqliteStateStore(database)
        primary = create_store
        closable = (create_store, normal_store)

    run_id = RunId("run-shared-request")
    seed_command_run(primary, tmp_path, str(run_id))
    request_id = f"shared-bootstrap-normal-{backend}-{create_wins}"
    create_command = make_create_run_command(request_id=request_id)
    normal_command = make_pause_command(
        request_id=request_id,
        expected_sequence=1,
        run_id=str(run_id),
        reason="shared request id",
    )
    repository_authority = FixtureRepositoryBootstrapAuthorityService()

    def apply_create() -> CommandOutcome:
        if not create_wins:
            create_ready.set()
            assert release_create.wait(timeout=5)
        return create_store.apply_control_command(
            create_command,
            StoreTargetAuthority(create_store),
            repository_authority,
        )

    def apply_normal() -> CommandOutcome:
        return normal_store.apply_control_command(
            normal_command,
            StoreTargetAuthority(normal_store),
            repository_authority,
        )

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            create_future = executor.submit(apply_create)
            if create_wins:
                assert source_entered.wait(timeout=5)
                normal_future = executor.submit(apply_normal)
                release_source.set()
            else:
                assert create_ready.wait(timeout=5)
                normal_future = executor.submit(apply_normal)
                normal = normal_future.result(timeout=5)
                release_create.set()
            create = create_future.result(timeout=5)
            normal = normal_future.result(timeout=5)

        if create_wins:
            assert create.status == CommandStatus.CONFLICT
            assert create.failed_invariant == "TARGET_RESERVATION_ID_SOURCE_INVALID"
            assert normal.status == CommandStatus.CONFLICT
            assert normal.failed_invariant == "IDEMPOTENCY_KEY_REUSE"
            assert source_calls == 1
            assert primary.audit_sequence(run_id) == 1
            assert (
                primary.apply_control_command(
                    create_command,
                    StoreTargetAuthority(primary),
                    repository_authority,
                )
                == create
            )
        else:
            assert normal.status == CommandStatus.INVALID
            assert normal.failed_invariant == "COMMAND_NOT_AVAILABLE_IN_TASK_10"
            assert normal.run_id == run_id
            assert normal.resulting_sequence == 2
            assert create.status == CommandStatus.CONFLICT
            assert create.failed_invariant == "IDEMPOTENCY_KEY_REUSE"
            assert source_calls == 0
            assert primary.audit_sequence(run_id) == 2
            assert (
                primary.apply_control_command(
                    normal_command,
                    StoreTargetAuthority(primary),
                    repository_authority,
                )
                == normal
            )
        assert primary.run_count() == 1
        assert primary.target_reservation_count(run_id) == 1
        assert source_calls == (1 if create_wins else 0)
    finally:
        release_source.set()
        release_create.set()
        for store in closable:
            store.close()


@pytest.mark.parametrize("backend", ("memory", "sqlite"))
@pytest.mark.parametrize("identical", (True, False))
def test_concurrent_ordinary_control_commands_claim_before_sequence_check(
    tmp_path: Path, backend: str, identical: bool
) -> None:
    if backend == "memory":
        primary: InMemoryStateStore | SqliteStateStore = InMemoryStateStore()
        stores = (primary, primary)
        closable: tuple[SqliteStateStore, ...] = ()
    else:
        database = tmp_path / "state.db"
        first = SqliteStateStore(database)
        second = SqliteStateStore(database)
        primary = first
        stores = (first, second)
        closable = (first, second)

    run_id = RunId("run-ordinary-race")
    seed_command_run(primary, tmp_path, str(run_id))
    request_id = f"ordinary-race-{backend}-{identical}"
    first_command = make_pause_command(
        request_id=request_id,
        expected_sequence=1,
        run_id=str(run_id),
        reason="first",
    )
    second_command = (
        first_command
        if identical
        else make_pause_command(
            request_id=request_id,
            expected_sequence=1,
            run_id=str(run_id),
            reason="second",
        )
    )
    gate = Barrier(2)
    gate_lock = Lock()
    gated_calls = 0

    def install_gate(store: InMemoryStateStore | SqliteStateStore) -> None:
        nonlocal gated_calls
        original = store._existing_control_outcome

        def gated(command: CommandEnvelope) -> CommandOutcome | None:
            nonlocal gated_calls
            outcome = original(command)
            if outcome is not None:
                return outcome
            with gate_lock:
                should_wait = gated_calls < 2
                gated_calls += 1
            if should_wait:
                gate.wait(timeout=5)
            return None

        store._existing_control_outcome = gated  # type: ignore[method-assign]

    for store in dict.fromkeys(stores):
        install_gate(store)

    def apply(
        store: InMemoryStateStore | SqliteStateStore, command: CommandEnvelope
    ) -> CommandOutcome:
        return store.apply_control_command(
            command,
            StoreTargetAuthority(store),
            FixtureRepositoryBootstrapAuthorityService(),
        )

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            first_future = executor.submit(apply, stores[0], first_command)
            second_future = executor.submit(apply, stores[1], second_command)
            first = first_future.result(timeout=5)
            second = second_future.result(timeout=5)

        if identical:
            assert first == second
            assert first.status == CommandStatus.INVALID
            assert first.failed_invariant == "COMMAND_NOT_AVAILABLE_IN_TASK_10"
            assert first.resulting_sequence == 2
        else:
            outcomes = (first, second)
            winner = next(
                outcome for outcome in outcomes if outcome.status == CommandStatus.INVALID
            )
            loser = next(
                outcome for outcome in outcomes if outcome.status == CommandStatus.CONFLICT
            )
            assert winner.failed_invariant == "COMMAND_NOT_AVAILABLE_IN_TASK_10"
            assert winner.resulting_sequence == 2
            assert loser.failed_invariant == "IDEMPOTENCY_KEY_REUSE"
            assert loser.run_id == run_id
            assert loser.resulting_sequence == 2
        assert primary.audit_sequence(run_id) == 2
        assert primary.run_record(run_id).state == RunState.DRAFT
        assert (
            primary.apply_control_command(
                first_command,
                StoreTargetAuthority(primary),
                FixtureRepositoryBootstrapAuthorityService(),
            )
            == first
        )
        assert (
            primary.apply_control_command(
                second_command,
                StoreTargetAuthority(primary),
                FixtureRepositoryBootstrapAuthorityService(),
            )
            == second
        )
    finally:
        for store in closable:
            store.close()


@pytest.mark.parametrize("store_factory", [memory_store_factory, sqlite_store_factory])
def test_request_id_reuse_with_result_never_validates_against_new_payload(
    tmp_path: Path,
    store_factory: Callable[[Path], InMemoryStateStore | SqliteStateStore],
) -> None:
    store = store_factory(tmp_path)
    run_id = RunId("run-result-reuse")
    seed_command_run(store, tmp_path, str(run_id))
    original = CommandEnvelope(
        request_id="result-reuse",
        expected_sequence=1,
        payload=ProposePolicyPayload(run_id=run_id, policy_revision=fixture_policy()),
    )
    accepted = CommandOutcome.for_payload(
        original.payload,
        status=CommandStatus.ACCEPTED,
        run_id=run_id,
        resulting_sequence=AuditSequence(2),
        result=RevisionApprovalResult(
            approvals=(
                RevisionApprovalPreview(
                    revision_kind="policy",
                    revision_digest=RevisionDigest("sha256:" + "b" * 64),
                    confirmation_code="ABC123",
                ),
            )
        ),
    )
    assert store.record_command(original, accepted) == accepted

    changed = make_pause_command(
        request_id=original.request_id,
        expected_sequence=1,
        run_id=str(run_id),
        reason="different payload",
    )
    conflict = store.apply_control_command(
        changed,
        StoreTargetAuthority(store),
        FixtureRepositoryBootstrapAuthorityService(),
    )
    assert conflict.status == CommandStatus.CONFLICT
    assert conflict.failed_invariant == "IDEMPOTENCY_KEY_REUSE"
    assert conflict.run_id == run_id
    assert conflict.resulting_sequence == 2
    assert store.audit_sequence(run_id) == 2


class _CountingStartGuard:
    def __init__(self, decision: StartGuardDecision) -> None:
        self._decision = decision
        self.calls = 0

    def inspect(
        self,
        *,
        run_id: RunId,
        applicable_revision_digests: ApplicableRevisionDigests,
        expected_sequence: AuditSequence,
    ) -> StartGuardDecision:
        self.calls += 1
        return self._decision


def _start_guard_binding(
    store: InMemoryStateStore | SqliteStateStore,
    run_id: RunId,
    current: ApplicableRevisionDigests,
    *,
    pinned_target_oid: GitOid | None = None,
) -> StartGuardBinding:
    run = store.run_record(run_id)
    reservation = store.target_reservation_for_run(run_id)
    absent = RefPathBinding(state="ABSENT")
    return StartGuardBinding(
        run_id=run_id,
        repository_id=run.repository_id,
        target_reservation_id=reservation.reservation_id,
        pinned_target_oid=run.pinned_target_oid if pinned_target_oid is None else pinned_target_oid,
        target_safety_digest=store.target_authority_digest(run_id),
        ref_effect_binding=RefEffectBinding(
            repository_instance_digest=run.repository_instance_digest,
            checkout_registration_digest="sha256:" + "c" * 64,
            ref_file=absent,
            ref_lock=absent,
            reflog=absent,
            reflog_lock=absent,
            reflog_exists=False,
            reflog_message="ApexCrew initialize run head",
        ),
        applicable_revision_digests=current,
    )


def _prepare_startable_run(
    store: InMemoryStateStore | SqliteStateStore, tmp_path: Path
) -> tuple[RunId, ApplicableRevisionDigests]:
    created = store.apply_control_command(
        make_create_run_command(request_id="start-guard-create"),
        StoreTargetAuthority(store),
        FixtureRepositoryBootstrapAuthorityService(),
    )
    assert created.run_id is not None
    run_id = created.run_id
    expected = store.audit_sequence(run_id)
    plan_digest = RevisionDigest("sha256:" + "d" * 64)
    approval = PlanApproval(
        run_id=run_id,
        plan_digest=plan_digest,
        approval_request_id="start-guard-plan-approval",
        approval_sequence=AuditSequence(expected + 1),
        binding_digest="sha256:" + "e" * 64,
    )

    if isinstance(store, InMemoryStateStore):

        def mutate_memory(copied: InMemoryStateStore) -> None:
            copied._runs[run_id] = replace(
                copied._runs[run_id],
                state=RunState.READY_TO_START,
                current_plan_digest=plan_digest,
            )
            copied._plan_approvals[run_id] = approval

        mutate = mutate_memory
    else:

        def mutate_sqlite(connection: sqlite3.Connection) -> None:
            assert (
                connection.execute(
                    "UPDATE runs SET state = ?, current_plan_digest = ? WHERE run_id = ?",
                    (RunState.READY_TO_START, plan_digest, run_id),
                ).rowcount
                == 1
            )
            connection.execute(
                "INSERT INTO plan_approvals(run_id, plan_digest, approval_request_id, "
                "approval_sequence, binding_digest) VALUES (?, ?, ?, ?, ?)",
                (
                    approval.run_id,
                    approval.plan_digest,
                    approval.approval_request_id,
                    approval.approval_sequence,
                    approval.binding_digest,
                ),
            )

        mutate = mutate_sqlite

    store._commit_state_and_event(
        run_id=run_id,
        expected_sequence=expected,
        event=AuditEvent.kind("TEST_START_READY"),
        mutate=mutate,
    )
    return run_id, store.current_revision_digests(run_id)


@pytest.mark.parametrize("store_factory", [memory_store_factory, sqlite_store_factory])
def test_start_guard_terminal_outcomes_are_globally_replayed(
    tmp_path: Path,
    store_factory: Callable[[Path], InMemoryStateStore | SqliteStateStore],
) -> None:
    store = store_factory(tmp_path)
    run_id, current = _prepare_startable_run(store, tmp_path)
    expected = store.audit_sequence(run_id)
    valid_guard = _CountingStartGuard(
        StartGuardDecision(ok=True, binding=_start_guard_binding(store, run_id, current))
    )
    mismatched_guard = _CountingStartGuard(
        StartGuardDecision(
            ok=True,
            binding=_start_guard_binding(
                store,
                run_id,
                current,
                pinned_target_oid=GitOid("f" * 40),
            ),
        )
    )
    denied_guard = _CountingStartGuard(StartGuardDecision(ok=False, reason="PRIVATE_REF_CONFLICT"))
    cases = (
        (
            "plan-binding",
            RevisionDigest("sha256:" + "a" * 64),
            valid_guard,
            CommandStatus.STALE,
            "PLAN_APPROVAL_BINDING_MISMATCH",
        ),
        (
            "unavailable",
            current.plan_digest,
            None,
            CommandStatus.CONFLICT,
            "START_GUARD_UNAVAILABLE",
        ),
        (
            "denied",
            current.plan_digest,
            denied_guard,
            CommandStatus.CONFLICT,
            "PRIVATE_REF_CONFLICT",
        ),
        (
            "binding-mismatch",
            current.plan_digest,
            mismatched_guard,
            CommandStatus.STALE,
            "START_GUARD_BINDING_MISMATCH",
        ),
    )
    for suffix, plan_digest, initial_guard, status, failed_invariant in cases:
        command = CommandEnvelope(
            request_id=f"start-guard-{suffix}",
            expected_sequence=expected,
            applicable_revision_digests=current,
            payload=StartPayload(run_id=run_id, plan_digest=plan_digest),
        )
        first = store.apply_control_command(
            command,
            StoreTargetAuthority(store),
            FixtureRepositoryBootstrapAuthorityService(),
            initial_guard,
        )
        assert first.status == status
        assert first.failed_invariant == failed_invariant
        assert first.run_id == run_id
        assert first.resulting_sequence == expected
        assert store.audit_sequence(run_id) == expected

        replay_guard = _CountingStartGuard(
            StartGuardDecision(ok=True, binding=_start_guard_binding(store, run_id, current))
        )
        assert (
            store.apply_control_command(
                command,
                StoreTargetAuthority(store),
                FixtureRepositoryBootstrapAuthorityService(),
                replay_guard,
            )
            == first
        )
        assert replay_guard.calls == 0
        assert store.audit_sequence(run_id) == expected


def test_sqlite_create_run_claim_precedes_bootstrap_rows(tmp_path: Path) -> None:
    store = SqliteStateStore(tmp_path / "state.db")
    statements: list[str] = []
    store._connection.set_trace_callback(statements.append)
    try:
        accepted = store.apply_control_command(
            make_create_run_command(request_id="create-claim-first"),
            StoreTargetAuthority(store),
            FixtureRepositoryBootstrapAuthorityService(),
        )
    finally:
        store._connection.set_trace_callback(None)

    assert accepted.status == CommandStatus.ACCEPTED
    upper = tuple(statement.upper() for statement in statements)
    claim_index = next(
        index
        for index, statement in enumerate(upper)
        if "INSERT INTO CONTROL_REQUEST_CLAIMS" in statement
    )
    for table in (
        "RUN_SEQUENCES",
        "RUNS",
        "TARGET_RESERVATIONS",
        "RUN_BOOTSTRAP_INPUTS",
        "REVISION_DOCUMENTS",
    ):
        bootstrap_index = next(
            index for index, statement in enumerate(upper) if f"INSERT INTO {table}" in statement
        )
        assert claim_index < bootstrap_index


@pytest.mark.parametrize("store_factory", [memory_store_factory, sqlite_store_factory])
def test_revision_approval_cannot_be_repurposed_as_runtime_authority(
    tmp_path: Path,
    store_factory: Callable[[Path], InMemoryStateStore | SqliteStateStore],
) -> None:
    store = store_factory(tmp_path)
    target_authority = StoreTargetAuthority(store)
    repository_authority = FixtureRepositoryBootstrapAuthorityService()
    created = store.apply_control_command(
        make_create_run_command(request_id="invalid-permit-create"),
        target_authority,
        repository_authority,
    )
    assert created.run_id is not None
    run_id = created.run_id
    current = store.current_revision_digests(run_id)
    assert current.policy_digest is not None
    approval = CommandEnvelope(
        request_id="invalid-permit-policy-approval",
        expected_sequence=store.audit_sequence(run_id),
        applicable_revision_digests=ApplicableRevisionDigests(),
        payload=ApprovePolicyPayload(
            run_id=run_id,
            policy_digest=current.policy_digest,
            confirmation_code=control_approval_code(
                "approve_policy", run_id, "POLICY", current.policy_digest
            ),
        ),
    )
    outcome = store.apply_control_command(approval, target_authority, repository_authority)
    assert outcome.status == CommandStatus.ACCEPTED
    before = store.audit_sequence(run_id)
    with pytest.raises(StateConflict, match="RUNTIME_PERMIT_SOURCE_COMMAND_INVALID"):
        store.issue_runtime_permit(
            approval,
            "DRAFT",
            store.current_revision_digests(run_id),
            store.target_authority_digest(run_id),
            before,
        )
    assert store.audit_sequence(run_id) == before
    with pytest.raises(StateConflict, match="RUNTIME_PERMIT_NOT_FOUND"):
        store.unconsumed_permit(run_id)


@pytest.mark.parametrize("store_factory", [memory_store_factory, sqlite_store_factory])
def test_stale_revision_document_cannot_be_reapproved(
    tmp_path: Path,
    store_factory: Callable[[Path], InMemoryStateStore | SqliteStateStore],
) -> None:
    store = store_factory(tmp_path)
    target_authority = StoreTargetAuthority(store)
    repository_authority = FixtureRepositoryBootstrapAuthorityService()
    created = store.apply_control_command(
        make_create_run_command(request_id="stale-revision-create"),
        target_authority,
        repository_authority,
    )
    assert created.run_id is not None
    run_id = created.run_id
    old_digest = store.current_revision_digests(run_id).policy_digest
    assert old_digest is not None
    replacement = fixture_policy().model_copy(update={"grant_ttl_seconds": 599})
    proposed = store.apply_control_command(
        CommandEnvelope(
            request_id="replace-policy",
            expected_sequence=store.audit_sequence(run_id),
            applicable_revision_digests=ApplicableRevisionDigests(),
            payload=ProposePolicyPayload(run_id=run_id, policy_revision=replacement),
        ),
        target_authority,
        repository_authority,
    )
    assert proposed.status == CommandStatus.ACCEPTED
    replacement_digest = store.current_revision_digests(run_id).policy_digest
    assert replacement_digest is not None and replacement_digest != old_digest
    stale_approval = store.apply_control_command(
        CommandEnvelope(
            request_id="approve-stale-policy",
            expected_sequence=store.audit_sequence(run_id),
            applicable_revision_digests=ApplicableRevisionDigests(),
            payload=ApprovePolicyPayload(
                run_id=run_id,
                policy_digest=old_digest,
                confirmation_code=control_approval_code(
                    "approve_policy", run_id, "POLICY", old_digest
                ),
            ),
        ),
        target_authority,
        repository_authority,
    )
    assert stale_approval.status == CommandStatus.STALE
    assert stale_approval.failed_invariant == "REVISION_PROPOSAL_NOT_CURRENT"
    assert store.current_revision_digests(run_id).policy_digest == replacement_digest


@pytest.mark.parametrize(
    "store_factory", [memory_runtime_store_factory, sqlite_runtime_store_factory]
)
def test_runtime_permit_consumption_is_atomic_and_one_use(
    tmp_path: Path,
    store_factory: Callable[[Path, FixedMonotonicClock], InMemoryStateStore | SqliteStateStore],
) -> None:
    clock = FixedMonotonicClock(MonotonicInstant(50_000_000_000))
    store = store_factory(tmp_path, clock)
    run_id, _ = seed_control_permit(store)
    permit = store.unconsumed_permit(run_id)
    store.fail_next_commit_after_state_write_for_test()
    with pytest.raises(StateCommitFault, match="TEST_FAULT_AFTER_STATE_WRITE"):
        store.consume_current_runtime_permit(
            run_id,
            RuntimeOwnerId("owner-faulted"),
            store.audit_sequence(run_id),
        )
    assert store.runtime_permit(run_id, permit.generation).state == "UNCONSUMED"
    assert store.active_run_time_state(run_id).open_owner_generation is None
    assert store.last_runtime_audit_event(run_id, owner_generation=1) is None
    assert clock.readings == 1
    consumed = store.consume_current_runtime_permit(
        run_id,
        RuntimeOwnerId("owner-1"),
        store.audit_sequence(run_id),
    )
    assert consumed is not None
    assert consumed.state == "CONSUMED"
    assert consumed.consumed_owner_id == "owner-1"
    active = store.active_run_time_state(run_id)
    assert active.open_owner_generation == 1
    assert active.opened_at == MonotonicInstant(50_000_000_000)
    assert active.latest_committed_at == MonotonicInstant(50_000_000_000)
    stamp = store.last_runtime_audit_event(run_id, owner_generation=1)
    assert stamp is not None
    assert stamp.monotonic_instant == MonotonicInstant(50_000_000_000)
    assert clock.readings == 2
    before_replay = store.audit_sequence(run_id)
    assert (
        store.consume_current_runtime_permit(run_id, RuntimeOwnerId("owner-2"), before_replay)
        is None
    )
    assert store.audit_sequence(run_id) == before_replay
    assert clock.readings == 2


@pytest.mark.parametrize(
    "store_factory", [memory_runtime_store_factory, sqlite_runtime_store_factory]
)
def test_runtime_reservation_barrier_and_interval_close_match_adapters(
    tmp_path: Path,
    store_factory: Callable[[Path, FixedMonotonicClock], InMemoryStateStore | SqliteStateStore],
) -> None:
    clock = FixedMonotonicClock(MonotonicInstant(50_000_000_000))
    store = store_factory(tmp_path, clock)
    run_id, _ = seed_control_permit(store)
    owner_id = RuntimeOwnerId("owner-contract")
    permit = store.consume_current_runtime_permit(run_id, owner_id, store.audit_sequence(run_id))
    assert permit is not None

    intent = store.record_or_load_target_reservation_creation_intent_under_draft_permit(
        run_id,
        owner_id,
        permit.generation,
        expected_sequence=store.audit_sequence(run_id),
    )
    observation = ReservationObservation(
        True,
        True,
        True,
        True,
        True,
        admin_entry_name=intent.reservation_id,
        admin_binding_digest="sha256:" + "b" * 64,
    )
    store.settle_target_reservation_creation_under_draft_permit(
        intent,
        TargetReservationCreationOutcome(
            intent_id=intent.intent_id,
            run_id=run_id,
            result_class="REGISTERED_LOCKED",
            observed=observation,
        ),
        owner_id,
        permit.generation,
        expected_sequence=store.audit_sequence(run_id),
    )
    assert store.load_runtime_state(run_id).state == RunState.PLANNING
    assert store.target_reservation_for_run(run_id).phase == "REGISTERED_LOCKED"

    store.begin_runtime_barrier(run_id, "contract-action", store.audit_sequence(run_id))
    store.settle_runtime_barrier(
        run_id,
        "contract-action",
        model_calls=0,
        pending_stop_reason=None,
        expected_sequence=store.audit_sequence(run_id),
    )
    clock.instant = MonotonicInstant(55_000_000_000)
    stop = store.record_runtime_delivery_stop(
        run_id,
        owner_id,
        permit.generation,
        RunStop(
            run_id=run_id,
            state=RunState.PLANNING,
            reason=RunStopReason.PAUSED,
            last_sequence=store.audit_sequence(run_id),
        ),
        store.audit_sequence(run_id),
    )
    assert stop.last_sequence == store.audit_sequence(run_id)
    assert store.runtime_owner(run_id) is None
    assert store.active_run_time_state(run_id).cumulative_nanoseconds == 5_000_000_000
    assert store.runtime_delivery_stop_count(run_id) == 1
    assert store.audit_event_kinds(run_id)[-2:] == (
        "RUNTIME_DELIVERY_STOP_RECORDED",
        "RUNTIME_OWNER_RELEASED",
    )


def seed_mismatched_permit_phase(
    store: InMemoryStateStore | SqliteStateStore, run_id: RunId
) -> None:
    expected = store.audit_sequence(run_id)
    if isinstance(store, InMemoryStateStore):

        def mutate(copied: InMemoryStateStore) -> None:
            copied._runs[run_id] = replace(copied._runs[run_id], state=RunState.ACTIVE)

    else:

        def mutate(connection: sqlite3.Connection) -> None:
            connection.execute("UPDATE runs SET state = 'ACTIVE' WHERE run_id = ?", (run_id,))

    store._commit_state_and_event(
        run_id=run_id,
        expected_sequence=expected,
        event=AuditEvent.kind("TEST_RUNTIME_PHASE_CHANGED"),
        mutate=mutate,
    )


@pytest.mark.parametrize("store_factory", [memory_store_factory, sqlite_store_factory])
def test_runtime_permit_is_bound_to_its_issued_phase(
    tmp_path: Path,
    store_factory: Callable[[Path], InMemoryStateStore | SqliteStateStore],
) -> None:
    store = store_factory(tmp_path)
    run_id, _ = seed_control_permit(store)
    permit = store.unconsumed_permit(run_id)
    assert permit.allowed_phase == "DRAFT"
    seed_mismatched_permit_phase(store, run_id)
    assert (
        store.consume_current_runtime_permit(
            run_id,
            RuntimeOwnerId("owner-wrong-phase"),
            store.audit_sequence(run_id),
        )
        is None
    )
    assert store.runtime_permit(run_id, permit.generation).state == "INVALIDATED"
    assert store.audit_event_kinds(run_id)[-1] == "RUNTIME_PERMIT_INVALIDATED"


def test_sqlite_command_replay_survives_restart(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    first_store = SqliteStateStore(database)
    seed_command_run(first_store, tmp_path, "run-command")
    command = make_pause_command(request_id="cmd-restart", expected_sequence=1)
    first = first_store.record_command(command, accepted_outcome(sequence=2))
    first_store.close()
    reopened = SqliteStateStore(database)
    assert reopened.record_command(command, accepted_outcome(sequence=99)) == first
    assert reopened.audit_sequence(RunId("run-command")) == 2


@pytest.mark.parametrize("store_factory", [memory_store_factory, sqlite_store_factory])
def test_audit_sequence_is_contiguous_and_rejects_stale_writes(
    tmp_path: Path, store_factory: Callable[[Path], EffectJournal]
) -> None:
    store = store_factory(tmp_path)
    run_id = RunId("run-sequence")
    assert store.append_event(run_id, AuditEvent.kind("FIRST"), AuditSequence(0)) == 1
    assert store.append_event(run_id, AuditEvent.kind("SECOND"), AuditSequence(1)) == 2
    with pytest.raises(StateConflict, match="STALE_SEQUENCE"):
        store.append_event(run_id, AuditEvent.kind("STALE"), AuditSequence(0))
    assert store.audit_sequence(run_id) == 2


def test_sqlite_expected_sequence_serializes_two_connections(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    first = SqliteStateStore(database)
    second = SqliteStateStore(database)
    run_id = RunId("run-race")

    def append(store: SqliteStateStore, kind: str) -> AuditSequence | str:
        try:
            return store.append_event(run_id, AuditEvent.kind(kind), AuditSequence(0))
        except StateConflict as error:
            return str(error)

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = tuple(
            executor.map(
                lambda item: append(*item),
                ((first, "FIRST_WRITER"), (second, "SECOND_WRITER")),
            )
        )
    assert sorted(str(outcome) for outcome in outcomes) == ["1", "STALE_SEQUENCE"]
    assert first.audit_sequence(run_id) == 1
    assert second.audit_sequence(run_id) == 1


@pytest.mark.parametrize("store_factory", [memory_store_factory, sqlite_store_factory])
def test_empty_recovery_is_stable_before_runtime_exists(
    tmp_path: Path, store_factory: Callable[[Path], EffectJournal]
) -> None:
    store = store_factory(tmp_path)
    run_id = RunId("run-empty")
    assert store.unsettled_intents(run_id) == ()
    assert RecoveryService(store).reconcile(run_id) == RecoveryOutcome.empty()


def test_sqlite_effect_intent_and_result_survive_restart(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    first = SqliteStateStore(database)
    run_id = RunId("run-journal")
    intent = make_test_effect_intent(
        run_id=run_id,
        intent_id="intent-1",
        recorded_sequence=AuditSequence(first.audit_sequence(run_id) + 1),
    )
    first.record_intent(intent, expected_sequence=first.audit_sequence(run_id))
    result = make_test_effect_result(
        intent,
        settled_sequence=AuditSequence(first.audit_sequence(run_id) + 1),
    )
    first.settle_intent(
        run_id=intent.run_id,
        intent_id=intent.intent_id,
        result=result,
        applicable_revision_digests=intent.applicable_revision_digests,
        expected_sequence=first.audit_sequence(run_id),
    )
    sequence = first.audit_sequence(intent.run_id)
    first.close()
    reopened = SqliteStateStore(database)
    assert reopened.effect_intent(intent.intent_id) == intent
    assert reopened.effect_result(intent.intent_id) == result
    assert reopened.audit_sequence(intent.run_id) == sequence


def test_sqlite_round_trips_every_effect_owner_and_binding_field(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    store = SqliteStateStore(database)
    intent = replace(
        make_test_effect_intent(
            run_id=RunId("run-fields"),
            intent_id="intent-fields",
            recorded_sequence=AuditSequence(1),
        ),
        applicable_revision_digests=ApplicableRevisionDigests(
            plan_digest=RevisionDigest("sha256:" + "1" * 64),
            policy_digest=RevisionDigest("sha256:" + "2" * 64),
            budget_digest=RevisionDigest("sha256:" + "3" * 64),
            model_configuration_digest=RevisionDigest("sha256:" + "4" * 64),
        ),
        expected_prestate_json='{"oid":"abc123"}',
        task_id=TaskId("task-1"),
        attempt_id=AttemptId("attempt-1"),
        action_id="action-1",
    )
    store.record_intent(intent, AuditSequence(0))
    result = replace(
        make_test_effect_result(intent, settled_sequence=AuditSequence(2)),
        snapshot_digest="sha256:" + "5" * 64,
    )
    store.settle_intent(
        intent.run_id,
        intent.intent_id,
        result,
        intent.applicable_revision_digests,
        AuditSequence(1),
    )
    store.close()
    reopened = SqliteStateStore(database)
    assert reopened.effect_intent(intent.intent_id) == intent
    assert reopened.effect_result(intent.intent_id) == result


def test_sqlite_rejects_a_result_whose_typed_columns_do_not_match_json(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.db"
    store = SqliteStateStore(database)
    intent = make_test_effect_intent(
        run_id=RunId("run-result-binding"),
        intent_id="intent-result-binding",
        recorded_sequence=AuditSequence(1),
    )
    store.record_intent(intent, AuditSequence(0))
    result = make_test_effect_result(intent, settled_sequence=AuditSequence(2))
    store.settle_intent(
        intent.run_id,
        intent.intent_id,
        result,
        intent.applicable_revision_digests,
        AuditSequence(1),
    )
    store.close()
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE effect_results SET result_class = 'ALTERED' WHERE intent_id = ?",
            (intent.intent_id,),
        )
    reopened = SqliteStateStore(database)
    with pytest.raises(StateConflict, match="EFFECT_RESULT_STORAGE_BINDING_MISMATCH"):
        reopened.effect_result(intent.intent_id)


@pytest.mark.parametrize("store_factory", [memory_store_factory, sqlite_store_factory])
def test_state_and_audit_roll_back_together(
    tmp_path: Path, store_factory: Callable[[Path], EffectJournal]
) -> None:
    store = store_factory(tmp_path)
    run_id = RunId("run-rollback")
    before = store.audit_sequence(run_id)
    intent = make_test_effect_intent(
        run_id=run_id,
        intent_id="intent-2",
        recorded_sequence=AuditSequence(before + 1),
    )
    store.fail_next_commit_after_state_write_for_test()
    with pytest.raises(StateCommitFault, match="TEST_FAULT_AFTER_STATE_WRITE"):
        store.record_intent(intent, expected_sequence=before)
    assert store.effect_intent_or_none(intent.intent_id) is None
    assert store.audit_sequence(intent.run_id) == before


@pytest.mark.parametrize("store_factory", [memory_store_factory, sqlite_store_factory])
def test_invalid_intent_digest_rolls_back_state_and_audit(
    tmp_path: Path, store_factory: Callable[[Path], EffectJournal]
) -> None:
    store = store_factory(tmp_path)
    intent = replace(
        make_test_effect_intent(
            run_id=RunId("run-bad-digest"),
            intent_id="intent-bad-digest",
            recorded_sequence=AuditSequence(1),
        ),
        payload_digest="sha256:" + "0" * 64,
    )
    with pytest.raises(StateConflict, match="EFFECT_INTENT_PAYLOAD_DIGEST_MISMATCH"):
        store.record_intent(intent, AuditSequence(0))
    assert store.effect_intent_or_none(intent.intent_id) is None
    assert store.audit_sequence(intent.run_id) == 0


@pytest.mark.parametrize("store_factory", [memory_store_factory, sqlite_store_factory])
def test_malformed_intent_payload_has_the_same_closed_error(
    tmp_path: Path, store_factory: Callable[[Path], EffectJournal]
) -> None:
    store = store_factory(tmp_path)
    malformed_payload = "{"
    intent = replace(
        make_test_effect_intent(
            run_id=RunId("run-malformed"),
            intent_id="intent-malformed",
            recorded_sequence=AuditSequence(1),
        ),
        normalized_payload_json=malformed_payload,
        payload_digest="sha256:" + sha256(malformed_payload.encode("utf-8")).hexdigest(),
    )
    with pytest.raises(StateConflict, match="^EFFECT_INTENT_PAYLOAD_NOT_CANONICAL$"):
        store.record_intent(intent, AuditSequence(0))
    assert store.effect_intent_or_none(intent.intent_id) is None
    assert store.audit_sequence(intent.run_id) == 0


@pytest.mark.parametrize("store_factory", [memory_store_factory, sqlite_store_factory])
def test_revision_mismatch_cannot_settle_an_intent(
    tmp_path: Path, store_factory: Callable[[Path], EffectJournal]
) -> None:
    store = store_factory(tmp_path)
    intent = make_test_effect_intent(
        run_id=RunId("run-revision"),
        intent_id="intent-revision",
        recorded_sequence=AuditSequence(1),
    )
    store.record_intent(intent, AuditSequence(0))
    result = make_test_effect_result(intent, settled_sequence=AuditSequence(2))
    wrong_revisions = ApplicableRevisionDigests(policy_digest=RevisionDigest("sha256:" + "7" * 64))
    with pytest.raises(StateConflict, match="EFFECT_RESULT_REVISION_BINDING_MISMATCH"):
        store.settle_intent(
            intent.run_id,
            intent.intent_id,
            result,
            wrong_revisions,
            AuditSequence(1),
        )
    assert store.unsettled_intents(intent.run_id) == (intent,)
    assert store.audit_sequence(intent.run_id) == 1


@pytest.mark.parametrize("store_factory", [memory_store_factory, sqlite_store_factory])
def test_duplicate_idempotency_key_cannot_create_a_second_intent(
    tmp_path: Path, store_factory: Callable[[Path], EffectJournal]
) -> None:
    store = store_factory(tmp_path)
    run_id = RunId("run-duplicate")
    first = make_test_effect_intent(
        run_id=run_id,
        intent_id="intent-first",
        recorded_sequence=AuditSequence(1),
    )
    store.record_intent(first, AuditSequence(0))
    duplicate = replace(
        make_test_effect_intent(
            run_id=run_id,
            intent_id="intent-second",
            recorded_sequence=AuditSequence(2),
        ),
        idempotency_key=first.idempotency_key,
    )
    with pytest.raises(StateConflict, match="EFFECT_INTENT_DUPLICATE"):
        store.record_intent(duplicate, AuditSequence(1))
    assert store.unsettled_intents(run_id) == (first,)
    assert store.audit_sequence(run_id) == 1


@pytest.mark.parametrize("store_factory", [memory_store_factory, sqlite_store_factory])
def test_result_digest_and_run_bindings_are_checked_before_settlement(
    tmp_path: Path, store_factory: Callable[[Path], EffectJournal]
) -> None:
    store = store_factory(tmp_path)
    intent = make_test_effect_intent(
        run_id=RunId("run-result-validation"),
        intent_id="intent-result-validation",
        recorded_sequence=AuditSequence(1),
    )
    store.record_intent(intent, AuditSequence(0))
    result = make_test_effect_result(intent, settled_sequence=AuditSequence(2))
    with pytest.raises(StateConflict, match="EFFECT_RESULT_DIGEST_MISMATCH"):
        store.settle_intent(
            intent.run_id,
            intent.intent_id,
            replace(result, result_digest="sha256:" + "0" * 64),
            intent.applicable_revision_digests,
            AuditSequence(1),
        )
    with pytest.raises(StateConflict, match="EFFECT_RESULT_RUN_OR_INTENT_MISMATCH"):
        store.settle_intent(
            intent.run_id,
            intent.intent_id,
            replace(result, run_id=RunId("run-other")),
            intent.applicable_revision_digests,
            AuditSequence(1),
        )
    assert store.unsettled_intents(intent.run_id) == (intent,)
    assert store.audit_sequence(intent.run_id) == 1


@pytest.mark.parametrize("store_factory", [memory_store_factory, sqlite_store_factory])
def test_unsettled_intents_have_identical_deterministic_order(
    tmp_path: Path, store_factory: Callable[[Path], EffectJournal]
) -> None:
    store = store_factory(tmp_path)
    run_id = RunId("run-order")
    first = make_test_effect_intent(
        run_id=run_id,
        intent_id="intent-z",
        recorded_sequence=AuditSequence(1),
    )
    second = make_test_effect_intent(
        run_id=run_id,
        intent_id="intent-a",
        recorded_sequence=AuditSequence(2),
    )
    store.record_intent(first, AuditSequence(0))
    store.record_intent(second, AuditSequence(1))
    assert store.unsettled_intents(run_id) == (first, second)


def make_contract_tool_intent(
    *, intent_id: str, action: ReadAction | SearchAction | PatchAction | CheckAction
) -> ToolIntent:
    digest = "sha256:" + "1" * 64
    return ToolIntent.for_authorized_worker_action(
        intent_id=IntentId(intent_id),
        run_id=RunId("run-tool-contract"),
        task_id=TaskId("task-1"),
        attempt_id=AttemptId("attempt-1"),
        action_id=intent_id,
        action=action,
        authorization_binding_digest=digest,
        applicable_revision_digests=ApplicableRevisionDigests(),
        repository_id="repository-1",
        snapshot_digest=digest,
        scope_digest=digest,
        dependency_fingerprint_basis=digest,
        idempotency_key=f"tool:{intent_id}",
        expected_prestate_json="{}",
    )


@pytest.mark.parametrize("store_factory", [memory_store_factory, sqlite_store_factory])
def test_read_and_search_tool_intents_use_the_same_effect_journal_contract(
    tmp_path: Path,
    store_factory: Callable[[Path], InMemoryStateStore | SqliteStateStore],
) -> None:
    store = store_factory(tmp_path)
    documents = (
        make_contract_tool_intent(intent_id="intent-read", action=ReadAction(path="src/a.py")),
        make_contract_tool_intent(
            intent_id="intent-search",
            action=SearchAction(query="name", paths=("src/**",)),
        ),
    )
    effects = tuple(
        document.to_effect_intent(AuditSequence(index + 1))
        for index, document in enumerate(documents)
    )
    for index, effect in enumerate(effects):
        store.record_intent(effect, AuditSequence(index))
        assert (
            ToolIntent.from_effect_intent(store.effect_intent(effect.intent_id)) == documents[index]
        )
    assert (
        tuple(
            ToolIntent.from_effect_intent(effect)
            for effect in store.unsettled_intents(documents[0].run_id)
        )
        == documents
    )


@pytest.mark.parametrize("store_factory", [memory_store_factory, sqlite_store_factory])
def test_patch_and_check_tool_documents_use_the_same_effect_journal_contract(
    tmp_path: Path,
    store_factory: Callable[[Path], InMemoryStateStore | SqliteStateStore],
) -> None:
    store = store_factory(tmp_path)
    patch = make_contract_tool_intent(
        intent_id="intent-patch",
        action=PatchAction(path="src/a.py", unified_diff="@@ -1 +1 @@\n-old\n+new\n"),
    )
    check = make_contract_tool_intent(
        intent_id="intent-check",
        action=CheckAction(check_id="task-check-1"),
    )
    patch_effect = patch.to_effect_intent(AuditSequence(1))
    check_effect = check.to_effect_intent(AuditSequence(2))
    store.record_intent(patch_effect, AuditSequence(0))
    store.record_intent(check_effect, AuditSequence(1))
    result = ToolResult(
        code="CHECK_PASSED",
        run_id=check.run_id,
        intent_id=check.intent_id,
        passed=True,
        bounded_payload={"snapshot_digest": check.snapshot_digest, "timing_ms": 10},
    )
    store.settle_intent(
        check.run_id,
        check.intent_id,
        result.to_effect_result(AuditSequence(3)),
        check.applicable_revision_digests,
        AuditSequence(2),
    )

    assert ToolIntent.from_effect_intent(store.effect_intent(patch.intent_id)) == patch
    assert store.effect_result(check.intent_id).result_class == "CHECK_PASSED"


@pytest.mark.parametrize("store_factory", [memory_store_factory, sqlite_store_factory])
def test_timed_out_check_cannot_be_settled_as_passing(
    tmp_path: Path,
    store_factory: Callable[[Path], InMemoryStateStore | SqliteStateStore],
) -> None:
    store = store_factory(tmp_path)
    check = make_contract_tool_intent(
        intent_id="intent-timeout-forged-pass",
        action=CheckAction(check_id="task-check-1"),
    )
    effect = check.to_effect_intent(AuditSequence(1))
    store.record_intent(effect, AuditSequence(0))
    forged = ToolResult.model_construct(
        code="CHECK_PASSED",
        run_id=check.run_id,
        intent_id=check.intent_id,
        passed=True,
        timed_out=True,
        matches=(),
        bounded_payload={"snapshot_digest": check.snapshot_digest},
        content_digest=None,
    ).to_effect_result(AuditSequence(2))

    with pytest.raises(StateConflict, match="CHECK_RESULT_BINDING_INVALID"):
        store.settle_intent(
            check.run_id,
            check.intent_id,
            forged,
            check.applicable_revision_digests,
            AuditSequence(1),
        )
