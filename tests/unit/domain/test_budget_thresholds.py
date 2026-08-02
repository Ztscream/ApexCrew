from dataclasses import dataclass, fields
from decimal import Decimal
from inspect import signature
from pathlib import Path

import pytest
from test_leases import make_budget

from apexcrew.adapters.state.memory import InMemoryStateStore
from apexcrew.adapters.state.sqlite import SqliteStateStore
from apexcrew.domain.authority import (
    ActiveRunTimeState,
    AtomicAction,
    AuthorityDenied,
    AuthorityService,
    DispatchCloseCause,
    GlobalBudgetMetric,
    GlobalUsageSnapshot,
    MonotonicInstant,
)
from apexcrew.domain.effects import TargetReservation
from apexcrew.domain.revisions import revision_digest
from apexcrew.domain.types import GitOid, RepositoryId, RunId


@dataclass
class AdjustableMonotonicClock:
    instant: MonotonicInstant

    def now(self) -> MonotonicInstant:
        return self.instant


def seed_approved_budget(
    store: InMemoryStateStore | SqliteStateStore,
    run_id: RunId,
    *,
    budget_overrides: dict[str, object] | None = None,
) -> AuthorityService:
    store.create_draft_with_reservation(
        run_id,
        RepositoryId(f"repository-{run_id}"),
        "sha256:" + "a" * 64,
        TargetReservation(
            reservation_id=f"reservation-{run_id}",
            run_id=run_id,
            target_ref="refs/heads/main",
            pinned_target_oid=GitOid("1" * 40),
            path=Path.cwd() / "data" / "reservations" / f"reservation-{run_id}",
            phase="ALLOCATED",
        ),
    )
    budget = make_budget(**(budget_overrides or {}))
    store.install_approved_budget_for_test(run_id, revision_digest(budget), budget)
    return AuthorityService(journal=store)


@pytest.mark.parametrize(
    ("metric", "below", "crossing"),
    [
        ("ACTIVE_RUN_SECONDS", 23_039, 23_040),
        ("TASKS", 9, 10),
        ("PLANNING_REQUESTS", 6, 7),
        ("MODEL_CALLS", 191, 192),
        ("INPUT_TOKENS", 1_599_999, 1_600_000),
        ("OUTPUT_TOKENS", 159_999, 160_000),
        ("COST_RESERVE_USD", Decimal("7.99"), Decimal("8.00")),
        ("CONCURRENT_WORKERS", 2, 3),
    ],
)
def test_each_global_ceiling_emits_one_warning(
    metric: str, below: int | Decimal, crossing: int | Decimal
) -> None:
    store = InMemoryStateStore()
    run_id = RunId("run-1")
    authority = seed_approved_budget(store, run_id)
    authority.settle_global_usage(
        run_id,
        metric,
        absolute_used=below,
        expected_sequence=store.audit_sequence(run_id),
    )
    assert store.budget_warnings(run_id, metric) == ()
    authority.settle_global_usage(
        run_id,
        metric,
        absolute_used=crossing,
        expected_sequence=store.audit_sequence(run_id),
    )
    authority.settle_global_usage(
        run_id,
        metric,
        absolute_used=crossing,
        expected_sequence=store.audit_sequence(run_id),
    )
    warnings = store.budget_warnings(run_id, metric)
    assert len(warnings) == 1
    assert warnings[0].threshold_percent == 80
    assert warnings[0].budget_digest == store.current_approved_budget(run_id)[0]


def test_unknown_global_metric_fails_closed_before_journal_mutation() -> None:
    store = InMemoryStateStore()
    run_id = RunId("run-unknown")
    authority = seed_approved_budget(store, run_id)
    before = store.audit_sequence(run_id)
    with pytest.raises(AuthorityDenied, match="UNKNOWN_GLOBAL_BUDGET_METRIC"):
        authority.settle_global_usage(
            run_id,
            "DISK_BYTES",
            absolute_used=1,
            expected_sequence=before,
        )
    assert store.audit_sequence(run_id) == before
    assert store.global_usage_snapshot(run_id) == GlobalUsageSnapshot.zero()


@pytest.mark.parametrize("store_kind", ["memory", "sqlite"])
def test_global_ceiling_is_not_caller_selectable(store_kind: str, tmp_path: Path) -> None:
    store = (
        InMemoryStateStore()
        if store_kind == "memory"
        else SqliteStateStore(tmp_path / "ceiling-binding.db")
    )
    run_id = RunId("run-binding")
    authority = seed_approved_budget(
        store,
        run_id,
        budget_overrides={"model_call_ceiling": 1},
    )
    before = store.audit_sequence(run_id)

    with pytest.raises(TypeError):
        store.settle_global_usage(
            run_id,
            store.current_approved_budget(run_id)[0],
            GlobalBudgetMetric.MODEL_CALLS,
            1,
            80,
            10_000,
            before,
        )

    assert authority.authorize_new_action(run_id).decision == "ALLOW"
    assert store.audit_sequence(run_id) == before
    assert store.new_dispatch_open(run_id) is True


@pytest.mark.parametrize("store_kind", ["memory", "sqlite"])
def test_enforced_cap_inputs_are_not_exposed_to_callers(
    store_kind: str,
    tmp_path: Path,
) -> None:
    store = (
        InMemoryStateStore()
        if store_kind == "memory"
        else SqliteStateStore(tmp_path / "cap-interface.db")
    )

    assert tuple(signature(store.settle_global_usage).parameters) == (
        "run_id",
        "budget_digest",
        "metric",
        "absolute_used",
        "expected_sequence",
    )
    assert tuple(field.name for field in fields(AtomicAction)) == (
        "run_id",
        "action_id",
        "budget_digest",
        "state",
        "opened_sequence",
    )
    assert tuple(signature(store.issue_workspace_lease).parameters) == (
        "lease",
        "budget_digest",
        "expected_sequence",
    )


def test_ceiling_settles_current_action_before_closing_new_dispatch() -> None:
    store = InMemoryStateStore()
    run_id = RunId("run-1")
    authority = seed_approved_budget(
        store,
        run_id,
        budget_overrides={"model_call_ceiling": 240},
    )
    authority.settle_global_usage(
        run_id,
        "MODEL_CALLS",
        absolute_used=239,
        expected_sequence=store.audit_sequence(run_id),
    )
    action = authority.begin_atomic_action(
        run_id,
        action_id="model-attempt-240",
        expected_sequence=store.audit_sequence(run_id),
    )
    decision = authority.settle_atomic_action(
        action,
        model_calls=1,
        expected_sequence=store.audit_sequence(run_id),
    )
    assert decision.action_state == "SETTLED"
    assert decision.pause_after_barrier is True
    assert decision.pause_reason == "GLOBAL_MODEL_CALL_CEILING"
    assert store.audit_event_kinds(run_id)[-2:] == (
        "ATOMIC_ACTION_SETTLED",
        "BUDGET_STOP_REQUESTED",
    )
    assert authority.authorize_new_action(run_id).decision == "DENY"


def test_sqlite_warning_and_stop_survive_restart(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    store = SqliteStateStore(database)
    run_id = RunId("run-1")
    authority = seed_approved_budget(
        store,
        run_id,
        budget_overrides={"model_call_ceiling": 240},
    )
    authority.settle_global_usage(
        run_id,
        "MODEL_CALLS",
        absolute_used=192,
        expected_sequence=store.audit_sequence(run_id),
    )
    authority.settle_global_usage(
        run_id,
        "MODEL_CALLS",
        absolute_used=239,
        expected_sequence=store.audit_sequence(run_id),
    )
    action = authority.begin_atomic_action(
        run_id,
        action_id="model-attempt-240",
        expected_sequence=store.audit_sequence(run_id),
    )
    authority.settle_atomic_action(
        action,
        model_calls=1,
        expected_sequence=store.audit_sequence(run_id),
    )
    store.close()

    reopened = SqliteStateStore(database)
    assert len(reopened.budget_warnings(run_id, "MODEL_CALLS")) == 1
    assert reopened.new_dispatch_open(run_id) is False
    assert reopened.dispatch_close_causes(run_id) == frozenset(
        {DispatchCloseCause.BUDGET_EXHAUSTED}
    )
    assert reopened.audit_event_kinds(run_id)[-2:] == (
        "ATOMIC_ACTION_SETTLED",
        "BUDGET_STOP_REQUESTED",
    )


def test_active_runtime_boundary_warns_before_requesting_stop() -> None:
    clock = AdjustableMonotonicClock(MonotonicInstant(108_000_000_000))
    store = InMemoryStateStore(monotonic_clock=clock)
    run_id = RunId("run-active-time")
    authority = seed_approved_budget(
        store,
        run_id,
        budget_overrides={"active_run_seconds_ceiling": 10},
    )
    store._active_run_times[run_id] = ActiveRunTimeState(
        run_id,
        cumulative_nanoseconds=0,
        open_owner_generation=1,
        opened_at=MonotonicInstant(100_000_000_000),
        latest_committed_at=MonotonicInstant(100_000_000_000),
    )

    warning = authority.evaluate_active_run_time_boundary(
        run_id,
        expected_sequence=store.audit_sequence(run_id),
    )
    assert warning.decision == "CONTINUE"
    assert len(store.budget_warnings(run_id, GlobalBudgetMetric.ACTIVE_RUN_SECONDS)) == 1
    assert store.global_usage_snapshot(run_id).active_run_seconds == Decimal(8)
    assert store.new_dispatch_open(run_id) is True

    clock.instant = MonotonicInstant(110_000_000_000)
    stopped = authority.evaluate_active_run_time_boundary(
        run_id,
        expected_sequence=store.audit_sequence(run_id),
    )
    assert stopped.decision == "PAUSE"
    assert store.global_usage_snapshot(run_id).active_run_seconds == Decimal(10)
    assert store.audit_event_kinds(run_id)[-2:] == (
        "ACTIVE_RUN_TIME_CEILING_REACHED",
        "BUDGET_STOP_REQUESTED",
    )
