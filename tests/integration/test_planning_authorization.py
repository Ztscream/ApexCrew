from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest
from helpers.application import (
    fixture_policy,
    make_permitted_planning_application,
    seed_unreleased_committed_completion,
)

from apexcrew.adapters.model.scripted import ScriptedMockLLM
from apexcrew.adapters.state.memory import InMemoryStateStore
from apexcrew.adapters.state.sqlite import SqliteStateStore
from apexcrew.domain.authority import AuthorityService, ModelReservationRequest
from apexcrew.domain.commands import ApplicableRevisionDigests
from apexcrew.domain.coordination import (
    AuthorityModelClient,
    Clock,
    CoordinatorService,
    PlanningActionApplier,
    PlanningAuthorization,
    PlanningContextBuilder,
    PlanningIdSource,
    PlanningReadGateway,
    PlanningReadIntent,
    PlanningReadResult,
    PlanningReadTrackedFileAction,
    PlanningTurnBinding,
    planning_snapshot_digest,
)
from apexcrew.domain.effects import TargetReservation, canonical_json
from apexcrew.domain.model import (
    ModelCompletion,
    ModelDispatchResult,
    ModelRequest,
    ModelUsage,
    ProviderAttemptResult,
    RecoveredModelAction,
)
from apexcrew.domain.revisions import (
    BudgetRevisionDocument,
    ModelPricingEntryDocument,
    revision_digest,
)
from apexcrew.domain.types import (
    AuditSequence,
    GitOid,
    IntentId,
    RepositoryId,
    RevisionDigest,
    RunId,
    RunState,
    RuntimeOwnerId,
)


def budget(*, model_call_ceiling: int = 240) -> BudgetRevisionDocument:
    from datetime import date

    return BudgetRevisionDocument(
        schema_version="budget-revision-v1",
        active_run_seconds_ceiling=28_800,
        task_ceiling=12,
        planning_request_ceiling=8,
        model_call_ceiling=model_call_ceiling,
        input_token_ceiling=2_000_000,
        output_token_ceiling=200_000,
        cost_reserve_usd=Decimal(10),
        concurrent_worker_ceiling=3,
        pricing_observed_on=date(2026, 7, 26),
        pricing_entries=(
            ModelPricingEntryDocument(
                returned_model_id="gpt-5.6-terra",
                input_usd_per_million=Decimal("2.50"),
                output_usd_per_million=Decimal("15.00"),
            ),
        ),
    )


def seeded_store() -> InMemoryStateStore:
    store = InMemoryStateStore()
    run_id = RunId("run-plan")
    reservation = TargetReservation(
        reservation_id="reservation-1",
        run_id=run_id,
        target_ref="refs/heads/main",
        pinned_target_oid=GitOid("1" * 40),
        path=__import__("pathlib").Path.cwd() / "reservations" / "reservation-1",
        phase="ALLOCATED",
    )
    store.create_draft_with_reservation(
        run_id, RepositoryId("repository-1"), "sha256:" + "a" * 64, reservation
    )
    current_budget = budget()
    budget_digest = revision_digest(current_budget)
    store.install_approved_budget_for_test(run_id, budget_digest, current_budget)
    with store._lock:
        store._runs[run_id] = replace(
            store._runs[run_id],
            state=RunState.PLANNING,
            current_policy_digest=RevisionDigest("sha256:" + "3" * 64),
            current_budget_digest=budget_digest,
            current_model_configuration_digest=RevisionDigest("sha256:" + "5" * 64),
        )
        store._target_reservations["reservation-1"] = replace(
            store._target_reservations["reservation-1"],
            phase="REGISTERED_LOCKED",
            admin_entry_name="apexcrew-run-plan",
            admin_binding_digest="sha256:" + "c" * 64,
        )
    return store


def model_request(store: InMemoryStateStore) -> ModelRequest:
    budget_digest, _ = store.current_approved_budget(RunId("run-plan"))
    return ModelRequest(
        run_id=RunId("run-plan"),
        plan_digest=None,
        policy_digest=RevisionDigest("sha256:" + "3" * 64),
        budget_digest=budget_digest,
        model_configuration_digest=RevisionDigest("sha256:" + "5" * 64),
        requested_model_id="gpt-5.6-terra",
        allowed_model_ids=frozenset({"gpt-5.6-terra"}),
        prompt=({"role": "user", "content": "plan"},),
        tool_schema_digest="sha256:" + "1" * 64,
        request_digest="sha256:" + "2" * 64,
        idempotency_key="planning-request-1",
        max_input_tokens=1_000,
        max_output_tokens=200,
        reserved_cost_usd=Decimal("9.99"),
    )


def reservation_request(store: InMemoryStateStore) -> ModelReservationRequest:
    started = datetime(2026, 7, 27, tzinfo=UTC)
    request = model_request(store)
    return ModelReservationRequest(
        run_id=request.run_id,
        owner_kind="PLANNING",
        task_id=None,
        attempt_id=None,
        tranche_id=None,
        turn=None,
        model_request=request,
        provider_attempt_number=1,
        target_safety_digest="sha256:" + "c" * 64,
        credential_profile="default",
        expected_run_counters=store.model_counters(request.run_id),
        expected_task_counters=None,
        started_at_utc=started,
        deadline_at_utc=started + timedelta(seconds=120),
        expected_sequence=store.audit_sequence(request.run_id),
    )


class BarrierObservingModel(ScriptedMockLLM):
    def __init__(self, store: InMemoryStateStore) -> None:
        completion = ModelCompletion(
            response_id="response-1",
            requested_model_id="gpt-5.6-terra",
            returned_model_id="gpt-5.6-terra",
            usage=ModelUsage(10, 5, Decimal("0.0001")),
            normalized_action={"kind": "fail", "reason": "done"},
        )
        super().__init__(
            [
                ProviderAttemptResult.known_closed("reject-1", "TRANSIENT_REJECTION"),
                ProviderAttemptResult.completed(completion),
            ]
        )
        self._store = store
        self.observed_barriers: list[str] = []

    def complete(self, request: ModelRequest) -> ProviderAttemptResult:
        self.observed_barriers.append(self._store.runtime_barrier_state(request.run_id))
        return super().complete(request)


def test_provider_retry_is_authority_reserved_and_has_an_open_runtime_barrier() -> None:
    store = seeded_store()
    model = BarrierObservingModel(store)
    client = AuthorityModelClient(
        model=model,
        journal=store,
        authority=AuthorityService(journal=store),
        clock=lambda: datetime(2026, 7, 27, 0, 1, tzinfo=UTC),
    )
    result = client.complete(reservation_request(store))
    assert isinstance(result, ModelDispatchResult)
    assert result.outcome == "COMPLETED"
    assert model.observed_barriers == ["IN_FLIGHT", "IN_FLIGHT"]
    assert store.runtime_barrier_state(RunId("run-plan")) == "SETTLED"
    attempts = store.model_attempts(result.run_id, result.logical_turn_id)
    assert [attempt.provider_attempt_number for attempt in attempts] == [1, 2]
    assert store.planning_request_count(result.run_id) == 1


def test_authority_denial_makes_zero_provider_calls() -> None:
    store = seeded_store()
    model = ScriptedMockLLM(())
    request = reservation_request(store)
    client = AuthorityModelClient(
        model=model,
        journal=store,
        authority=AuthorityService(journal=store),
        clock=lambda: datetime(2026, 7, 27, 0, 1, tzinfo=UTC),
    )
    denied = client.complete(
        replace(
            request,
            credential_profile=None,
        )
    )
    assert not isinstance(denied, ModelDispatchResult)
    assert denied.decision == "DENY"
    assert model.call_count == 0


class StaticPlanningAuthorization:
    def __init__(self, value: PlanningAuthorization) -> None:
        self._value = value

    def current(self, run_id: RunId) -> PlanningAuthorization:
        assert run_id == self._value.run_id
        return self._value

    def current_for_recovery(
        self, run_id: RunId, action: RecoveredModelAction
    ) -> PlanningAuthorization:
        del action
        assert run_id == self._value.run_id
        assert self._value.planning_request_count > 0
        return self._value.model_copy(
            update={"planning_request_count": self._value.planning_request_count - 1}
        )


def recovered_authorization(store: SqliteStateStore, run_id: RunId) -> PlanningAuthorization:
    run = store.run_record(run_id)
    scope_digest = "sha256:" + "d" * 64
    binding = PlanningTurnBinding(
        repository_id=run.repository_id,
        pinned_base_oid=run.pinned_target_oid,
        scope_digest=scope_digest,
        snapshot_digest=planning_snapshot_digest(
            run.repository_id, run.pinned_target_oid, scope_digest
        ),
    )
    return PlanningAuthorization(
        run_id=run_id,
        decision="ALLOW",
        applicable_revision_digests=store.current_revision_digests(run_id),
        target_safety_digest=store.target_authority_digest(run_id),
        credential_profile="default",
        read_authorization=fixture_policy().planning_read_authorization,
        turn_binding=binding,
        planning_request_count=store.planning_request_count(run_id),
        planning_request_ceiling=8,
    )


@pytest.mark.parametrize(
    ("normalized_action", "expected_stop"),
    [
        ({"kind": "fail", "reason": "recovered failure"}, "PLANNING_FAILED"),
        (
            {
                "kind": "submit_plan",
                "plan_document": {
                    "proposed_promotion_order": ["task-01"],
                    "run_checks": [{"argv": ["pytest", "-q"], "input_globs": ["src/**"]}],
                    "tasks": [
                        {
                            "checks": [
                                {
                                    "argv": ["pytest", "-q"],
                                    "input_globs": ["src/task_01.py"],
                                }
                            ],
                            "constraints": ["remain read-only"],
                            "dependency_globs": [],
                            "dependency_task_ids": [],
                            "read_globs": ["src/task_01.py"],
                            "task_id": "task-01",
                            "write_globs": ["src/task_01.py"],
                        }
                    ],
                },
            },
            "AWAITING_PLAN_APPROVAL",
        ),
    ],
)
def test_recovered_planning_action_settles_marker_without_model_redispatch(
    tmp_path: Path,
    normalized_action: dict[str, object],
    expected_stop: str,
) -> None:
    app = make_permitted_planning_application(tmp_path, model=ScriptedMockLLM(()))
    store = app.store
    owner_id = RuntimeOwnerId("planning-recovery-owner")
    permit = store.consume_current_runtime_permit(
        app.run_id, owner_id, store.audit_sequence(app.run_id)
    )
    assert permit is not None
    committed = seed_unreleased_committed_completion(
        store,
        run_id=app.run_id,
        owner_kind="PLANNING",
        normalized_action=normalized_action,
    )
    with store._transaction("IMMEDIATE") as connection:
        connection.execute(
            "INSERT INTO run_authority_counters(run_id, planning_requests) VALUES (?, 1) "
            "ON CONFLICT(run_id) DO UPDATE SET planning_requests = 1",
            (app.run_id,),
        )
    recovered = RecoveredModelAction.for_committed_turn(
        committed,
        intent_id=IntentId("recovered-planning-action"),
        recorded_sequence=AuditSequence(store.audit_sequence(app.run_id) + 1),
    )
    store.record_downstream_action_intent(
        app.run_id,
        committed.logical_turn_id,
        recovered.effect_intent,
        store.audit_sequence(app.run_id),
    )
    model = ScriptedMockLLM(())
    coordinator = CoordinatorService(
        planning_authorization=StaticPlanningAuthorization(
            recovered_authorization(store, app.run_id)
        ),
        context=cast(PlanningContextBuilder, object()),
        models=cast(AuthorityModelClient, model),
        planning_actions=PlanningActionApplier(
            state=store,
            reads=cast(PlanningReadGateway, object()),
            ids=cast(PlanningIdSource, object()),
        ),
        journal=store,
        state=store,
        clock=cast(Clock, object()),
    )

    decision = coordinator.resume_recovered_planning_action(app.run_id, permit, recovered)

    assert decision.stop_reason == expected_stop
    assert model.call_count == 0
    assert store.effect_result(recovered.effect_intent.intent_id).result_class == (
        "PLANNING_ACTION_RELEASED"
    )


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
def test_planning_read_settlement_enforces_cumulative_returned_bytes(
    tmp_path: Path, kind: str
) -> None:
    store = (
        cast(InMemoryStateStore | SqliteStateStore, InMemoryStateStore())
        if kind == "memory"
        else SqliteStateStore(tmp_path / "planning-bytes.db")
    )
    run_id = RunId("run-bytes")
    reservation = TargetReservation(
        reservation_id="reservation-bytes",
        run_id=run_id,
        target_ref="refs/heads/main",
        pinned_target_oid=GitOid("1" * 40),
        path=tmp_path / "reservations" / "reservation-bytes",
        phase="ALLOCATED",
    )
    store.create_draft_with_reservation(
        run_id, RepositoryId("repository-bytes"), "sha256:" + "a" * 64, reservation
    )
    payload = {"content": "x" * 8, "truncated": False}
    returned_bytes = len(canonical_json(payload).encode("utf-8"))
    prior = 2_097_152 - returned_bytes + 1
    if isinstance(store, InMemoryStateStore):
        store._planning_returned_bytes[run_id] = prior
    else:
        with store._transaction("IMMEDIATE") as connection:
            connection.execute(
                "UPDATE runs SET planning_returned_bytes = ? WHERE run_id = ?", (prior, run_id)
            )
    intent = PlanningReadIntent(
        intent_id=IntentId("intent-bytes"),
        run_id=run_id,
        logical_turn_id="turn-bytes",
        action=PlanningReadTrackedFileAction(path="src/a.py"),
        applicable_revision_digests=ApplicableRevisionDigests(),
        repository_id=RepositoryId("repository-bytes"),
        base_oid=GitOid("1" * 40),
        snapshot_digest="sha256:" + "2" * 64,
        scope_digest="sha256:" + "3" * 64,
        idempotency_key="planning-read:run-bytes:turn-bytes",
    )
    store.record_planning_read_intent(intent, None, None, store.audit_sequence(run_id))
    result = PlanningReadResult(
        intent_id=intent.intent_id,
        run_id=run_id,
        result_class="READ_COMPLETED",
        bounded_payload=payload,
        snapshot_digest=intent.snapshot_digest,
        returned_bytes=returned_bytes,
    )
    settlement = store.settle_planning_read(intent, result, store.audit_sequence(run_id))
    assert settlement.stop_reason == "PLANNING_READ_LIMIT"
    assert store.planning_returned_bytes(run_id) == prior
    persisted = store.effect_result(intent.intent_id)
    assert persisted.result_class == "DENIED"
    assert "xxxxxxxx" not in persisted.bounded_result_json
