from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from test_leases import make_authority

from apexcrew.adapters.state.memory import InMemoryStateStore
from apexcrew.domain.authority import (
    NO_PROGRESS,
    AuthorityService,
    ModelReservationRequest,
    TaskAuthority,
)
from apexcrew.domain.effects import StateConflict
from apexcrew.domain.model import ModelRequest


def make_task(task_id: str) -> TaskAuthority:
    return TaskAuthority(run_id="run-1", task_id=task_id, attempt_id="attempt-1")


def consume_tranche(
    authority: AuthorityService,
    store: InMemoryStateStore,
    task: TaskAuthority,
    tranche_id: str,
) -> None:
    budget_digest, _ = store.current_approved_budget(task.run_id)
    started = datetime(2026, 7, 27, tzinfo=UTC)
    for index in range(8):
        request = ModelRequest(
            run_id=task.run_id,
            plan_digest=None,
            policy_digest="sha256:" + "3" * 64,
            budget_digest=budget_digest,
            model_configuration_digest="sha256:" + "5" * 64,
            requested_model_id="deepseek-v4-flash",
            allowed_model_ids=frozenset({"deepseek-v4-flash"}),
            prompt=({"role": "user", "content": "finish"},),
            tool_schema_digest="sha256:" + "1" * 64,
            request_digest="sha256:" + f"{index + 1:064x}",
            idempotency_key=f"request-{tranche_id}-{index}",
            max_input_tokens=1_000,
            max_output_tokens=200,
            reserved_cost_usd=Decimal("9.99"),
            owner_kind="WORKER",
            task_id=task.task_id,
            attempt_id=task.attempt_id,
            tranche_id=tranche_id,
        )
        reservation = authority.reserve_model_attempt(
            ModelReservationRequest(
                run_id=task.run_id,
                owner_kind="WORKER",
                task_id=task.task_id,
                attempt_id=task.attempt_id,
                tranche_id=tranche_id,
                turn=None,
                model_request=request,
                provider_attempt_number=1,
                target_safety_digest="sha256:" + "c" * 64,
                credential_profile="default",
                expected_run_counters=store.model_counters(task.run_id),
                expected_task_counters=store.task_budget_state(task.run_id, task.task_id),
                started_at_utc=started,
                deadline_at_utc=started + timedelta(minutes=2),
                expected_sequence=store.audit_sequence(task.run_id),
            )
        )
        assert reservation.decision == "RESERVED"


def planning_reservation(store: InMemoryStateStore, *, request_id: str) -> ModelReservationRequest:
    budget_digest, _ = store.current_approved_budget("run-1")
    started = datetime(2026, 7, 27, tzinfo=UTC)
    request = ModelRequest(
        run_id="run-1",
        plan_digest=None,
        policy_digest="sha256:" + "3" * 64,
        budget_digest=budget_digest,
        model_configuration_digest="sha256:" + "5" * 64,
        requested_model_id="deepseek-v4-flash",
        allowed_model_ids=frozenset({"deepseek-v4-flash"}),
        prompt=({"role": "user", "content": "finish"},),
        tool_schema_digest="sha256:" + "1" * 64,
        request_digest="sha256:" + request_id * 64,
        idempotency_key=f"request-{request_id}",
        max_input_tokens=1_000,
        max_output_tokens=200,
        reserved_cost_usd=Decimal("9.99"),
    )
    return ModelReservationRequest(
        run_id="run-1",
        owner_kind="PLANNING",
        task_id=None,
        attempt_id=None,
        tranche_id=None,
        turn=None,
        model_request=request,
        provider_attempt_number=1,
        target_safety_digest="sha256:" + "c" * 64,
        credential_profile="default",
        expected_run_counters=store.model_counters("run-1"),
        expected_task_counters=None,
        started_at_utc=started,
        deadline_at_utc=started + timedelta(minutes=2),
        expected_sequence=store.audit_sequence("run-1"),
    )


def test_global_model_call_ceiling_pauses_before_second_intent() -> None:
    store = InMemoryStateStore()
    authority = make_authority(store, model_call_ceiling=1)
    first = authority.reserve_model_attempt(planning_reservation(store, request_id="a"))
    stopped = authority.reserve_model_attempt(planning_reservation(store, request_id="b"))
    assert first.decision == "RESERVED"
    assert first.reserved_amounts.cost_usd == Decimal("0.000392")
    assert stopped.decision == "PAUSE"
    assert stopped.reason == "MODEL_CALL_CEILING"
    assert store.model_counters("run-1").calls == 1
    assert store.audit_sequence("run-1") == 2


def test_active_tranche_must_be_consumed_before_another_allocation() -> None:
    store = InMemoryStateStore()
    authority = make_authority(store)
    task = make_task("A")
    first = authority.allocate_tranche(task, NO_PROGRESS, expected_sequence=0)
    with pytest.raises(StateConflict, match="TASK_TRANCHE_STILL_ACTIVE"):
        authority.allocate_tranche(
            task,
            NO_PROGRESS,
            expected_sequence=first.resulting_sequence,
        )
    assert store.audit_sequence(task.run_id) == first.resulting_sequence


def test_no_progress_tranche_cannot_renew_after_bootstrap() -> None:
    store = InMemoryStateStore()
    authority = make_authority(store)
    task = make_task("A")
    first = authority.allocate_tranche(task, NO_PROGRESS, expected_sequence=0)
    assert first.tranche_id is not None
    consume_tranche(authority, store, task, first.tranche_id)
    second = authority.allocate_tranche(
        task, NO_PROGRESS, expected_sequence=store.audit_sequence(task.run_id)
    )
    assert second.tranche_id is not None
    consume_tranche(authority, store, task, second.tranche_id)
    paused = authority.allocate_tranche(
        task, NO_PROGRESS, expected_sequence=store.audit_sequence(task.run_id)
    )
    assert (first.calls, second.calls) == (8, 8)
    assert first.tranche_id != second.tranche_id
    assert paused.decision == "PAUSE"
    assert paused.calls == 0
