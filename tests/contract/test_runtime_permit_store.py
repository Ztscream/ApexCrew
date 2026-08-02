from dataclasses import dataclass
from pathlib import Path

from helpers.application import (
    approve_current_policy_budget_and_model,
    create_draft_with_three_proposals,
    make_application,
    make_begin_planning_command,
)

from apexcrew.adapters.state.sqlite import SqliteStateStore
from apexcrew.domain.authority import MonotonicInstant
from apexcrew.domain.types import RunId, RuntimeOwnerId


@dataclass
class PermitStoreClock:
    instant: MonotonicInstant
    readings: int = 0

    def now(self) -> MonotonicInstant:
        self.readings += 1
        return self.instant


def test_unconsumed_and_consumed_permits_survive_restart(tmp_path: Path) -> None:
    app = make_application(tmp_path)
    run_id = create_draft_with_three_proposals(app)
    approve_current_policy_budget_and_model(app, run_id)
    accepted = app.control.handle(make_begin_planning_command(app, run_id))
    assert accepted.status == "ACCEPTED"
    database = app.database
    app.close()

    clock = PermitStoreClock(MonotonicInstant(50_000_000_000))
    reopened = SqliteStateStore(database, monotonic_clock=clock)
    permit = reopened.unconsumed_permit(run_id)
    consumed = reopened.consume_current_runtime_permit(
        run_id,
        RuntimeOwnerId("owner-1"),
        expected_sequence=reopened.audit_sequence(run_id),
    )
    assert consumed is not None
    reopened.close()

    after = SqliteStateStore(database, monotonic_clock=clock)
    assert after.runtime_permit(run_id, permit.generation).state == "CONSUMED"
    assert (
        after.consume_current_runtime_permit(
            run_id,
            RuntimeOwnerId("owner-2"),
            expected_sequence=after.audit_sequence(run_id),
        )
        is None
    )
    assert consumed.generation == permit.generation
    active = after.active_run_time_state(RunId(run_id))
    assert active.open_owner_generation == 1
    assert active.opened_at == MonotonicInstant(50_000_000_000)
    assert active.latest_committed_at == MonotonicInstant(50_000_000_000)
    stamp = after.last_runtime_audit_event(run_id, owner_generation=1)
    assert stamp is not None
    assert stamp.monotonic_instant == MonotonicInstant(50_000_000_000)
    assert clock.readings == 1
