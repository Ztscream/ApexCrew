from dataclasses import dataclass
from pathlib import Path

import pytest
from test_leases import make_authority, make_contract

from apexcrew.adapters.state.memory import InMemoryStateStore
from apexcrew.adapters.state.sqlite import SqliteStateStore
from apexcrew.domain.authority import (
    ActiveRunTimeState,
    AttemptAuthority,
    CheckpointKey,
    MonotonicInstant,
    TaskAuthority,
    TaskStopDecision,
)
from apexcrew.domain.effects import StateCommitFault, StateConflict, TargetReservation
from apexcrew.domain.types import GitOid, RepositoryId, RunId


def make_task(task_id: str, attempt_id: str = "attempt-1") -> TaskAuthority:
    return TaskAuthority(run_id="run-1", task_id=task_id, attempt_id=attempt_id)


@dataclass
class FixedMonotonicClock:
    instant: MonotonicInstant

    def now(self) -> MonotonicInstant:
        return self.instant


def test_second_identical_checkpoint_pauses_task_and_closes_dispatch(tmp_path: Path) -> None:
    store = SqliteStateStore(tmp_path / "state.db")
    authority = make_authority(store)
    task = make_task("task-A")
    store.install_running_attempt_for_test(task)
    checkpoint = CheckpointKey(
        tree_oid="1" * 40,
        check_set_digest="sha256:" + "2" * 64,
    )

    first = authority.record_checkpoint(task, checkpoint, expected_sequence=0)
    assert authority.authorize_new_attempt(task.run_id, task.task_id).reason == "TASK_NOT_READY"
    second = authority.record_checkpoint(
        task,
        checkpoint,
        expected_sequence=first.resulting_sequence,
    )

    assert first.decision == "CONTINUE"
    assert second.decision == "PAUSE"
    assert second.pause_reason == "REPEATED_CHECKPOINT"
    assert second.checkpoint_count == 2
    assert authority.authorize_new_attempt(task.run_id, task.task_id).decision == "DENY"


def test_third_identical_invalid_action_fails_attempt_and_pauses_task(tmp_path: Path) -> None:
    store = SqliteStateStore(tmp_path / "state.db")
    authority = make_authority(store)
    action_digest = "sha256:" + "3" * 64
    decisions: list[TaskStopDecision] = []

    for number in range(1, 4):
        task = make_task("task-A", attempt_id=f"attempt-{number}")
        store.install_running_attempt_for_test(task)
        decisions.append(
            authority.record_invalid_action(
                task,
                task.attempt_id,
                action_digest,
                expected_sequence=store.audit_sequence(task.run_id),
            )
        )

    assert [decision.attempt_state for decision in decisions] == ["FAILED", "FAILED", "FAILED"]
    assert [decision.task_state for decision in decisions] == ["READY", "READY", "PAUSED"]
    assert decisions[-1].pause_reason == "REPEATED_INVALID_ACTION"
    assert decisions[-1].identical_invalid_action_count == 3
    assert authority.authorize_new_attempt(task.run_id, task.task_id).decision == "DENY"


def test_memory_and_sqlite_stopping_decisions_match(tmp_path: Path) -> None:
    stores = (InMemoryStateStore(), SqliteStateStore(tmp_path / "state.db"))
    outcomes: list[tuple[str, str, str]] = []

    for store in stores:
        authority = make_authority(store)
        task = make_task("task-parity")
        store.install_running_attempt_for_test(task)
        checkpoint = CheckpointKey("5" * 40, "sha256:" + "6" * 64)
        first = authority.record_checkpoint(task, checkpoint, expected_sequence=0)
        second = authority.record_checkpoint(
            task,
            checkpoint,
            expected_sequence=first.resulting_sequence,
        )
        outcomes.append((first.decision, second.decision, second.pause_reason or ""))

    assert outcomes == [
        ("CONTINUE", "PAUSE", "REPEATED_CHECKPOINT"),
        ("CONTINUE", "PAUSE", "REPEATED_CHECKPOINT"),
    ]


def test_checkpoint_history_survives_sqlite_restart(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    first_store = SqliteStateStore(database)
    first = make_authority(first_store)
    task = make_task("task-restart")
    first_store.install_running_attempt_for_test(task)
    checkpoint = CheckpointKey("7" * 40, "sha256:" + "8" * 64)
    observed = first.record_checkpoint(task, checkpoint, expected_sequence=0)
    assert observed.decision == "CONTINUE"
    first_store.close()

    reopened_store = SqliteStateStore(database)
    reopened = make_authority(reopened_store)
    second = reopened.record_checkpoint(
        task,
        checkpoint,
        expected_sequence=reopened_store.audit_sequence(task.run_id),
    )

    assert second.decision == "PAUSE"
    assert second.checkpoint_count == 2


def test_paused_task_cannot_record_another_checkpoint(tmp_path: Path) -> None:
    stores = (InMemoryStateStore(), SqliteStateStore(tmp_path / "state.db"))
    checkpoint = CheckpointKey("a" * 40, "sha256:" + "b" * 64)
    for store in stores:
        authority = make_authority(store)
        task = make_task("task-paused")
        store.install_running_attempt_for_test(task)
        first = authority.record_checkpoint(task, checkpoint, 0)
        authority.record_checkpoint(task, checkpoint, first.resulting_sequence)
        before = store.audit_sequence(task.run_id)

        with pytest.raises(StateConflict, match="TASK_CHECKPOINT_SOURCE_STATE_ILLEGAL"):
            authority.record_checkpoint(task, checkpoint, before)

        assert store.audit_sequence(task.run_id) == before
        assert store.task_lifecycle_state(task.run_id, task.task_id) == "PAUSED"


def test_terminal_attempt_cannot_record_a_second_invalid_action(tmp_path: Path) -> None:
    stores = (InMemoryStateStore(), SqliteStateStore(tmp_path / "state.db"))
    for store in stores:
        authority = make_authority(store)
        task = make_task("task-A")
        store.install_running_attempt_for_test(task)
        action_digest = "sha256:" + "9" * 64
        first = authority.record_invalid_action(task, task.attempt_id, action_digest, 0)
        before = store.audit_sequence(task.run_id)

        with pytest.raises(StateConflict, match="ATTEMPT_STATE_TRANSITION_ILLEGAL"):
            authority.record_invalid_action(task, task.attempt_id, action_digest, before)

        assert first.attempt_state == "FAILED"
        assert store.audit_sequence(task.run_id) == before
        assert store.attempt_lifecycle_state(task.run_id, task.attempt_id) == "FAILED"


def test_invalid_action_revokes_lease_and_rolls_back_atomically_on_fault(tmp_path: Path) -> None:
    stores = (InMemoryStateStore(), SqliteStateStore(tmp_path / "state.db"))
    for store in stores:
        authority = make_authority(store)
        task = make_task("task-lease")
        store.install_running_attempt_for_test(task)
        attempt = AttemptAuthority(
            run_id=task.run_id,
            task_id=task.task_id,
            attempt_id=task.attempt_id,
            generation=1,
            base_head="1" * 40,
            task_contract_digest="sha256:" + "9" * 64,
        )
        lease = authority.issue_lease(
            attempt,
            make_contract(task.task_id, ("src/**",)),
            expected_sequence=0,
        )
        before = store.audit_sequence(task.run_id)
        store.fail_next_commit_after_state_write_for_test()

        with pytest.raises(StateCommitFault, match="TEST_FAULT_AFTER_STATE_WRITE"):
            authority.record_invalid_action(
                task,
                task.attempt_id,
                "sha256:" + "c" * 64,
                before,
            )

        assert store.audit_sequence(task.run_id) == before
        assert store.task_lifecycle_state(task.run_id, task.task_id) == "ACTIVE"
        assert store.attempt_lifecycle_state(task.run_id, task.attempt_id) == "RUNNING"
        persisted_lease = store.workspace_lease(task.run_id, lease.lease_id)
        assert persisted_lease is not None
        assert persisted_lease.state == "ACTIVE"

        decision = authority.record_invalid_action(
            task,
            task.attempt_id,
            "sha256:" + "c" * 64,
            before,
        )
        assert decision.identical_invalid_action_count == 1
        persisted_lease = store.workspace_lease(task.run_id, lease.lease_id)
        assert persisted_lease is not None
        assert persisted_lease.state == "REVOKED"


def seeded_open_runtime_time_store(
    database: Path,
    *,
    ceiling_seconds: int,
    cumulative_nanoseconds: int,
    owner_generation: int,
    opened_at: MonotonicInstant,
) -> SqliteStateStore:
    store = SqliteStateStore(
        database,
        monotonic_clock=FixedMonotonicClock(MonotonicInstant(106_000_000_000)),
    )
    run_id = RunId("run-1")
    store.create_draft_with_reservation(
        run_id,
        RepositoryId("repository-1"),
        "sha256:" + "a" * 64,
        TargetReservation(
            reservation_id="reservation-1",
            run_id=run_id,
            target_ref="refs/heads/main",
            pinned_target_oid=GitOid("1" * 40),
            path=database.parent / "data" / "reservations" / "reservation-1",
            phase="ALLOCATED",
        ),
    )
    make_authority(store, active_run_seconds_ceiling=ceiling_seconds)
    store.install_running_attempt_for_test(make_task("task-A"))
    with store._transaction("IMMEDIATE") as connection:
        connection.execute(
            "UPDATE runs SET runtime_owner_id = ?, runtime_owner_generation = ?, "
            "active_runtime_nanoseconds = ?, runtime_interval_owner_generation = ?, "
            "runtime_interval_opened_nanoseconds = ? WHERE run_id = ?",
            (
                "owner-test",
                owner_generation,
                cumulative_nanoseconds,
                owner_generation,
                opened_at.nanoseconds,
                run_id,
            ),
        )
        connection.execute(
            "UPDATE audit_events SET runtime_owner_generation = ?, "
            "runtime_monotonic_nanoseconds = ? WHERE run_id = ? AND sequence = "
            "(SELECT MAX(sequence) FROM audit_events WHERE run_id = ?)",
            (owner_generation, opened_at.nanoseconds, run_id, run_id),
        )
    return store


def test_active_runtime_ceiling_closes_run_dispatch_without_reset(tmp_path: Path) -> None:
    store = seeded_open_runtime_time_store(
        tmp_path / "active-time.db",
        ceiling_seconds=10,
        cumulative_nanoseconds=4_000_000_000,
        owner_generation=3,
        opened_at=MonotonicInstant(100_000_000_000),
    )
    authority = make_authority(store, active_run_seconds_ceiling=10)
    decision = authority.evaluate_active_run_time_boundary(
        RunId("run-1"),
        expected_sequence=store.audit_sequence(RunId("run-1")),
    )

    assert decision.decision == "PAUSE"
    assert decision.observed_nanoseconds == 10_000_000_000
    assert store.new_dispatch_open(RunId("run-1")) is False
    assert store.active_run_time_state(RunId("run-1")).cumulative_nanoseconds == 4_000_000_000
    assert authority.authorize_new_attempt(RunId("run-1"), "task-A").reason == (
        "ACTIVE_RUN_TIME_CEILING"
    )


def test_active_runtime_below_ceiling_does_not_append_audit(tmp_path: Path) -> None:
    store = seeded_open_runtime_time_store(
        tmp_path / "below-ceiling.db",
        ceiling_seconds=10,
        cumulative_nanoseconds=3_000_000_000,
        owner_generation=3,
        opened_at=MonotonicInstant(100_000_000_000),
    )
    authority = make_authority(store, active_run_seconds_ceiling=10)
    before = store.audit_sequence(RunId("run-1"))

    decision = authority.evaluate_active_run_time_boundary(RunId("run-1"), before)

    assert decision.decision == "CONTINUE"
    assert decision.observed_nanoseconds == 9_000_000_000
    assert decision.resulting_sequence == before
    assert store.audit_sequence(RunId("run-1")) == before
    assert store.new_dispatch_open(RunId("run-1")) is True


def test_memory_active_runtime_boundary_matches_sqlite(tmp_path: Path) -> None:
    clock = FixedMonotonicClock(MonotonicInstant(106_000_000_000))
    memory = InMemoryStateStore(monotonic_clock=clock)
    run_id = RunId("run-1")
    memory.create_draft_with_reservation(
        run_id,
        RepositoryId("repository-memory"),
        "sha256:" + "b" * 64,
        TargetReservation(
            reservation_id="reservation-memory",
            run_id=run_id,
            target_ref="refs/heads/main",
            pinned_target_oid=GitOid("2" * 40),
            path=tmp_path / "memory" / "reservations" / "reservation-memory",
            phase="ALLOCATED",
        ),
    )
    memory._active_run_times[run_id] = ActiveRunTimeState(
        run_id,
        cumulative_nanoseconds=4_000_000_000,
        open_owner_generation=3,
        opened_at=MonotonicInstant(100_000_000_000),
        latest_committed_at=MonotonicInstant(100_000_000_000),
    )
    memory.install_running_attempt_for_test(make_task("task-A"))
    memory_authority = make_authority(memory, active_run_seconds_ceiling=10)
    sqlite = seeded_open_runtime_time_store(
        tmp_path / "sqlite.db",
        ceiling_seconds=10,
        cumulative_nanoseconds=4_000_000_000,
        owner_generation=3,
        opened_at=MonotonicInstant(100_000_000_000),
    )
    sqlite_authority = make_authority(sqlite, active_run_seconds_ceiling=10)

    memory_decision = memory_authority.evaluate_active_run_time_boundary(
        run_id,
        expected_sequence=memory.audit_sequence(run_id),
    )
    sqlite_decision = sqlite_authority.evaluate_active_run_time_boundary(
        run_id,
        expected_sequence=sqlite.audit_sequence(run_id),
    )

    assert memory_decision == sqlite_decision
    assert memory.new_dispatch_open(run_id) is False
    assert memory.active_run_time_state(run_id).cumulative_nanoseconds == 4_000_000_000
