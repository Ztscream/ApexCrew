from collections import deque
from decimal import Decimal
from pathlib import Path

import pytest
from helpers.application import (
    InjectedProcessCrash,
    ManualMonotonicClock,
    RegressOnceMonotonicClock,
    crash_after_permit_consumption_application,
    create_approved_draft,
    hold_runtime_owner,
    make_continue_command,
    make_permitted_active_runtime,
    make_permitted_draft_runtime,
    make_permitted_planning_application,
    make_runtime_application,
    seed_unreleased_committed_completion,
)

from apexcrew.adapters.model.scripted import ScriptedMockLLM
from apexcrew.application.runtime import RuntimeStateStore
from apexcrew.domain.authority import GlobalBudgetMetric, MonotonicInstant
from apexcrew.domain.commands import RuntimeDecision, RuntimePermit
from apexcrew.domain.effects import StateCommitFault
from apexcrew.domain.model import RecoveredModelAction
from apexcrew.domain.types import AuditSequence, RunId, RunStopReason


def test_direct_runtime_call_without_permit_has_zero_mutation(tmp_path: Path) -> None:
    app = make_runtime_application(tmp_path)
    run_id = create_approved_draft(app)
    before = app.store.audit_sequence(run_id)

    stop = app.runtime.run_until_blocked(run_id)

    assert stop.reason == RunStopReason.NO_RUNTIME_PERMIT
    assert app.store.audit_sequence(run_id) == before


def test_second_runtime_owner_returns_already_running(tmp_path: Path) -> None:
    app = make_permitted_planning_application(tmp_path, model=ScriptedMockLLM([]))
    with hold_runtime_owner(app, app.run_id):
        assert app.runtime.run_until_blocked(app.run_id).reason == RunStopReason.ALREADY_RUNNING


def test_normal_delivery_releases_durable_owner_before_return(tmp_path: Path) -> None:
    app = make_permitted_planning_application(tmp_path, model=ScriptedMockLLM([]))

    stop = app.runtime.run_until_blocked(app.run_id)

    assert stop.reason == RunStopReason.PAUSED
    assert app.store.runtime_owner(app.run_id) is None
    assert app.store.runtime_delivery_event(app.run_id) == "RUNTIME_OWNER_RELEASED"


class ScriptedBoundaryCoordinator:
    def __init__(
        self,
        store: RuntimeStateStore,
        clock: ManualMonotonicClock,
        advances: deque[int],
    ) -> None:
        self._store = store
        self._clock = clock
        self._advances = advances
        self.action_count = 0

    def run_planning_turn(self, run_id: RunId) -> RuntimeDecision:
        raise AssertionError("active fixture must not plan")

    def resume_recovered_planning_action(
        self, run_id: RunId, permit: RuntimePermit, action: RecoveredModelAction
    ) -> RuntimeDecision:
        raise AssertionError("active fixture has no planning recovery")

    def schedule(self, run_id: RunId) -> RuntimeDecision:
        self.action_count += 1
        action_id = f"active-time-action-{self.action_count}"
        self._store.begin_runtime_barrier(
            run_id,
            action_id=action_id,
            expected_sequence=self._store.audit_sequence(run_id),
        )
        self._clock.advance_seconds(self._advances.popleft())
        sequence = self._store.settle_runtime_barrier(
            run_id,
            action_id,
            model_calls=0,
            pending_stop_reason=None,
            expected_sequence=self._store.audit_sequence(run_id),
        )
        if self.action_count == 1:
            return RuntimeDecision.pause("FIXTURE_BOUNDARY_STOP", sequence)
        return RuntimeDecision.continued(sequence)


def test_owned_active_time_opens_closes_and_excludes_waits(tmp_path: Path) -> None:
    clock = ManualMonotonicClock.at_seconds(100)
    drivers: list[ScriptedBoundaryCoordinator] = []
    app = make_permitted_active_runtime(
        tmp_path,
        model_calls=0,
        model_call_ceiling=240,
        active_run_seconds_ceiling=10,
        monotonic_clock=clock,
        coordinator_factory=lambda store: (
            drivers.append(ScriptedBoundaryCoordinator(store, clock, deque((8, 2)))) or drivers[0]
        ),
    )

    first = app.runtime.run_until_blocked(app.run_id)
    assert first.reason == RunStopReason.PAUSED
    assert app.store.active_run_time_state(app.run_id).cumulative_nanoseconds == 8_000_000_000
    assert app.store.active_run_time_state(app.run_id).opened_at is None
    assert app.store.budget_warnings(app.run_id, GlobalBudgetMetric.ACTIVE_RUN_SECONDS)[
        0
    ].used == Decimal(8)

    clock.advance_seconds(3_600)
    assert app.store.active_run_time_state(app.run_id).cumulative_nanoseconds == 8_000_000_000
    accepted = app.control.handle(
        make_continue_command(app, app.run_id, request_id="continue-active-time")
    )
    assert accepted.status == "ACCEPTED"
    second = app.runtime.run_until_blocked(app.run_id)
    assert second.reason == RunStopReason.BUDGET_STOP
    assert drivers[0].action_count == 2
    assert app.store.active_run_time_state(app.run_id).cumulative_nanoseconds == 10_000_000_000
    kinds = app.store.audit_event_kinds(app.run_id)
    assert kinds.index("RUNTIME_BARRIER_SETTLED") < kinds.index("ACTIVE_RUN_TIME_CEILING_REACHED")


class CrashAfterCommittedRuntimeAction(ScriptedBoundaryCoordinator):
    def schedule(self, run_id: RunId) -> RuntimeDecision:
        action_id = "crash-after-settlement"
        self._store.begin_runtime_barrier(
            run_id,
            action_id=action_id,
            expected_sequence=self._store.audit_sequence(run_id),
        )
        self._clock.advance_seconds(3)
        self._store.settle_runtime_barrier(
            run_id,
            action_id,
            model_calls=0,
            pending_stop_reason=None,
            expected_sequence=self._store.audit_sequence(run_id),
        )
        self._clock.advance_seconds(900)
        raise InjectedProcessCrash()


def test_crash_seals_at_last_generation_audit_and_excludes_unknown_remainder(
    tmp_path: Path,
) -> None:
    clock = ManualMonotonicClock.at_seconds(200)
    app = make_permitted_active_runtime(
        tmp_path,
        model_calls=0,
        model_call_ceiling=240,
        active_run_seconds_ceiling=1_000,
        monotonic_clock=clock,
        coordinator_factory=lambda store: CrashAfterCommittedRuntimeAction(store, clock, deque()),
    )
    with pytest.raises(InjectedProcessCrash):
        app.runtime.run_until_blocked(app.run_id)
    crashed = app.store.active_run_time_state(app.run_id)
    assert crashed.opened_at == MonotonicInstant(200_000_000_000)
    assert crashed.latest_committed_at == MonotonicInstant(203_000_000_000)

    reopened = app.reopen()
    accepted = reopened.control.handle(
        make_continue_command(reopened, reopened.run_id, request_id="continue-crash")
    )
    assert accepted.status == "ACCEPTED"
    sealed = reopened.store.active_run_time_state(reopened.run_id)
    assert sealed.cumulative_nanoseconds == 3_000_000_000
    assert sealed.open_owner_generation is None
    assert reopened.store.runtime_owner(reopened.run_id) is None
    assert reopened.store.unconsumed_permit(reopened.run_id).generation > 1


class FailNextCloseCoordinator:
    def __init__(self, store: RuntimeStateStore) -> None:
        self._store = store

    def run_planning_turn(self, run_id: RunId) -> RuntimeDecision:
        raise AssertionError("active fixture must not plan")

    def resume_recovered_planning_action(
        self, run_id: RunId, permit: RuntimePermit, action: RecoveredModelAction
    ) -> RuntimeDecision:
        raise AssertionError("active fixture has no planning recovery")

    def schedule(self, run_id: RunId) -> RuntimeDecision:
        self._store.fail_next_commit_after_state_write_for_test()
        return RuntimeDecision.pause("FIXTURE_CLOSE_STOP", self._store.audit_sequence(run_id))


def test_active_interval_open_and_runstop_close_roll_back_with_audit(tmp_path: Path) -> None:
    clock = ManualMonotonicClock.at_seconds(300)
    app = make_permitted_active_runtime(
        tmp_path,
        model_calls=0,
        model_call_ceiling=240,
        active_run_seconds_ceiling=1_000,
        monotonic_clock=clock,
        coordinator_factory=FailNextCloseCoordinator,
    )
    before = app.store.audit_sequence(app.run_id)
    app.store.fail_next_commit_after_state_write_for_test()
    with pytest.raises(StateCommitFault, match="TEST_FAULT_AFTER_STATE_WRITE"):
        app.runtime.run_until_blocked(app.run_id)
    assert app.store.audit_sequence(app.run_id) == before
    assert app.store.runtime_owner(app.run_id) is None
    assert app.store.active_run_time_state(app.run_id).opened_at is None
    assert app.store.unconsumed_permit(app.run_id) is not None

    with pytest.raises(StateCommitFault, match="TEST_FAULT_AFTER_STATE_WRITE"):
        app.runtime.run_until_blocked(app.run_id)
    open_state = app.store.active_run_time_state(app.run_id)
    assert open_state.cumulative_nanoseconds == 0
    assert open_state.open_owner_generation is not None
    assert app.store.runtime_owner(app.run_id) is not None
    assert app.store.runtime_delivery_stop_count(app.run_id) == 0


class CountingStoppingCoordinator:
    action_count = 0

    def run_planning_turn(self, run_id: RunId) -> RuntimeDecision:
        raise AssertionError("active fixture must not plan")

    def resume_recovered_planning_action(
        self, run_id: RunId, permit: RuntimePermit, action: RecoveredModelAction
    ) -> RuntimeDecision:
        raise AssertionError("active fixture has no planning recovery")

    def schedule(self, run_id: RunId) -> RuntimeDecision:
        self.action_count += 1
        return RuntimeDecision.pause("UNREACHABLE", AuditSequence(0))


def test_clock_regression_starts_no_atomic_action_and_closes_fail_closed(
    tmp_path: Path,
) -> None:
    clock = RegressOnceMonotonicClock(
        first=MonotonicInstant(400_000_000_000),
        regressed=MonotonicInstant(399_000_000_000),
    )
    coordinator = CountingStoppingCoordinator()
    app = make_permitted_active_runtime(
        tmp_path,
        model_calls=0,
        model_call_ceiling=240,
        active_run_seconds_ceiling=1_000,
        monotonic_clock=clock,
        coordinator_factory=lambda store: coordinator,
    )

    stop = app.runtime.run_until_blocked(app.run_id)

    assert stop.reason == RunStopReason.PAUSED
    assert coordinator.action_count == 0
    assert app.store.recorded_stop_reason(app.run_id) == "RUNTIME_CLOCK_REGRESSION"
    assert app.store.active_run_time_state(app.run_id).cumulative_nanoseconds == 0
    assert app.store.runtime_owner(app.run_id) is None


def test_crashed_delivery_requires_fresh_continue_to_reclaim_orphan(tmp_path: Path) -> None:
    app = crash_after_permit_consumption_application(tmp_path)
    with pytest.raises(InjectedProcessCrash):
        app.runtime.run_until_blocked(app.run_id)
    reopened = app.reopen()
    assert (
        reopened.runtime.run_until_blocked(reopened.run_id).reason
        == RunStopReason.NO_RUNTIME_PERMIT
    )
    recovered = reopened.control.handle(
        make_continue_command(reopened, reopened.run_id, request_id="continue-orphan")
    )
    assert recovered.status == "ACCEPTED"
    assert reopened.store.runtime_owner(reopened.run_id) is None
    assert reopened.store.unconsumed_permit(reopened.run_id).generation > 1


def test_consumed_begin_command_replay_cannot_mint_another_permit(tmp_path: Path) -> None:
    app = make_permitted_draft_runtime(tmp_path)
    assert app.begin_command is not None
    app.runtime.run_until_blocked(app.run_id)

    replay = app.control.handle(app.begin_command)

    assert replay.status == "ACCEPTED"
    assert app.store.unconsumed_permit_count(app.run_id) == 0


class OneSettlingActionCoordinator:
    def __init__(self, store: RuntimeStateStore) -> None:
        self._store = store

    def run_planning_turn(self, run_id: RunId) -> RuntimeDecision:
        raise AssertionError("active delivery must not plan")

    def resume_recovered_planning_action(
        self, run_id: RunId, permit: RuntimePermit, action: RecoveredModelAction
    ) -> RuntimeDecision:
        raise AssertionError("active delivery has no planning marker")

    def schedule(self, run_id: RunId) -> RuntimeDecision:
        action = self._store.begin_runtime_barrier(
            run_id,
            action_id="model-turn-240",
            expected_sequence=self._store.audit_sequence(run_id),
        )
        sequence = self._store.settle_runtime_barrier(
            run_id,
            action,
            model_calls=1,
            pending_stop_reason="BUDGET_STOP",
            expected_sequence=self._store.audit_sequence(run_id),
        )
        return RuntimeDecision.continued(sequence)


def test_budget_ceiling_settles_in_flight_action_before_run_pause(tmp_path: Path) -> None:
    app = make_permitted_active_runtime(
        tmp_path,
        model_calls=239,
        model_call_ceiling=240,
        coordinator_factory=OneSettlingActionCoordinator,
    )

    stop = app.runtime.run_until_blocked(app.run_id)

    assert app.store.new_dispatch_open(app.run_id) is False
    assert stop.reason == RunStopReason.PAUSED
    assert app.store.recorded_stop_reason(app.run_id) == "BUDGET_STOP"
    assert app.store.model_counters(app.run_id).calls == 240


class FaultingBarrierCoordinator(OneSettlingActionCoordinator):
    def schedule(self, run_id: RunId) -> RuntimeDecision:
        self._store.begin_runtime_barrier(
            run_id,
            action_id="faulting-model-turn",
            expected_sequence=self._store.audit_sequence(run_id),
        )
        raise RuntimeError("fixture fault text must not be persisted")


def test_unhandled_runtime_fault_classifies_in_flight_barrier_before_owner_release(
    tmp_path: Path,
) -> None:
    app = make_permitted_active_runtime(
        tmp_path,
        model_calls=0,
        model_call_ceiling=240,
        coordinator_factory=FaultingBarrierCoordinator,
    )

    stop = app.runtime.run_until_blocked(app.run_id)

    assert stop.reason == RunStopReason.INDETERMINATE
    assert app.store.runtime_owner(app.run_id) is None
    assert app.store.runtime_barrier_state(app.run_id) == "INDETERMINATE"
    fault = app.store.latest_runtime_fault(app.run_id)
    assert fault.phase == "WORKER_SCHEDULING"
    assert fault.fault_code == "UNHANDLED_RUNTIME_EXCEPTION"
    assert "fixture fault text" not in fault.fingerprint


class RecordingRecoveredPlanningCoordinator:
    def __init__(self, store: RuntimeStateStore) -> None:
        self._store = store
        self.actions: list[RecoveredModelAction] = []

    def run_planning_turn(self, run_id: RunId) -> RuntimeDecision:
        raise AssertionError("recovered completion must route before a new planning turn")

    def resume_recovered_planning_action(
        self,
        run_id: RunId,
        permit: RuntimePermit,
        action: RecoveredModelAction,
    ) -> RuntimeDecision:
        assert run_id == action.turn.run_id
        assert permit.state == "CONSUMED"
        self.actions.append(action)
        return RuntimeDecision.pause("TEST_RECOVERED_ACTION", self._store.audit_sequence(run_id))

    def schedule(self, run_id: RunId) -> RuntimeDecision:
        raise AssertionError("recovered PLANNING completion must not schedule a Worker")


def test_runtime_journals_recovered_completion_before_phase_dispatch(tmp_path: Path) -> None:
    empty_model = ScriptedMockLLM([])
    drivers: list[RecordingRecoveredPlanningCoordinator] = []
    app = make_permitted_planning_application(
        tmp_path,
        model=empty_model,
        coordinator_factory=lambda store: (
            drivers.append(RecordingRecoveredPlanningCoordinator(store)) or drivers[0]
        ),
    )
    turn = seed_unreleased_committed_completion(
        app.store,
        run_id=app.run_id,
        owner_kind="PLANNING",
        normalized_action={"kind": "finish"},
    )

    stop = app.runtime.run_until_blocked(app.run_id)

    assert stop.reason == RunStopReason.PAUSED
    assert empty_model.call_count == 0
    assert app.store.model_attempt_count(turn.logical_turn_id) == 1
    committed = app.store.committed_model_turn(app.run_id, turn.logical_turn_id)
    assert committed is not None
    assert committed.downstream_intent_id is not None
    assert len(drivers[0].actions) == 1
    assert drivers[0].actions[0].turn.logical_turn_id == turn.logical_turn_id
    assert drivers[0].actions[0].normalized_action == {"kind": "finish"}


def test_runtime_resumes_journaled_recovered_action_before_generic_reconciliation(
    tmp_path: Path,
) -> None:
    empty_model = ScriptedMockLLM([])
    drivers: list[RecordingRecoveredPlanningCoordinator] = []
    app = make_permitted_planning_application(
        tmp_path,
        model=empty_model,
        coordinator_factory=lambda store: (
            drivers.append(RecordingRecoveredPlanningCoordinator(store)) or drivers[0]
        ),
    )
    seed_unreleased_committed_completion(
        app.store,
        run_id=app.run_id,
        owner_kind="PLANNING",
        normalized_action={"kind": "finish"},
    )
    first = app.runtime.run_until_blocked(app.run_id)
    assert first.reason == RunStopReason.PAUSED
    marker_id = drivers[0].actions[0].effect_intent.intent_id
    app.control.handle(make_continue_command(app, app.run_id, request_id="continue-1"))

    second = app.runtime.run_until_blocked(app.run_id)

    assert second.reason == RunStopReason.PAUSED
    assert [action.effect_intent.intent_id for action in drivers[0].actions] == [
        marker_id,
        marker_id,
    ]
    assert empty_model.call_count == 0
