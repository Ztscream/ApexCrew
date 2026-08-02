from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import TypedDict

import pytest

from apexcrew.adapters.state.memory import InMemoryStateStore
from apexcrew.adapters.state.sqlite import SqliteStateStore
from apexcrew.domain.authority import (
    AuthorityDenied,
    AuthorityService,
    BudgetCeilingExhaustion,
    CheckpointKey,
    GlobalBudgetMetric,
    ResumeTaskRequest,
    TaskCounterSnapshot,
    TaskPauseBinding,
)
from apexcrew.domain.commands import ApplicableRevisionDigests
from apexcrew.domain.effects import TargetReservation
from apexcrew.domain.revisions import (
    BudgetRevisionDocument,
    ModelPricingEntryDocument,
    revision_digest,
)
from apexcrew.domain.types import (
    AttemptId,
    AuditSequence,
    GitOid,
    RepositoryId,
    RevisionDigest,
    RunId,
    TaskId,
)

type StateStore = InMemoryStateStore | SqliteStateStore


class CounterOverrides(TypedDict, total=False):
    attempts: int
    stale_refreshes: int


PRESERVED_COUNTER_FIELDS = (
    "model_calls",
    "input_tokens",
    "output_tokens",
    "cost_reserve_usd",
    "attempts",
    "stale_refreshes",
    "failure_digests",
    "checkpoint_history",
    "invalid_action_history",
    "warning_keys",
)


def make_budget(**overrides: object) -> BudgetRevisionDocument:
    values: dict[str, object] = {
        "schema_version": "budget-revision-v1",
        "active_run_seconds_ceiling": 28_800,
        "task_ceiling": 12,
        "planning_request_ceiling": 8,
        "model_call_ceiling": 240,
        "input_token_ceiling": 2_000_000,
        "output_token_ceiling": 200_000,
        "cost_reserve_usd": Decimal(10),
        "concurrent_worker_ceiling": 3,
        "pricing_observed_on": date(2026, 7, 26),
        "pricing_entries": (
            ModelPricingEntryDocument(
                returned_model_id="gpt-5.6-terra",
                input_usd_per_million=Decimal("2.50"),
                output_usd_per_million=Decimal("15.00"),
            ),
        ),
    }
    values.update(overrides)
    return BudgetRevisionDocument.model_validate(values)


def _reservation(database: Path, run: RunId) -> TargetReservation:
    return TargetReservation(
        reservation_id=f"reservation-{run}",
        run_id=run,
        target_ref="refs/heads/main",
        pinned_target_oid=GitOid("1" * 40),
        path=database.parent / "reservations" / f"reservation-{run}",
        phase="ALLOCATED",
    )


def _approve_budget(
    store: StateStore,
    run: RunId,
    budget: BudgetRevisionDocument,
) -> RevisionDigest:
    digest = revision_digest(budget)
    store.install_approved_budget_for_test(run, digest, budget)
    if isinstance(store, InMemoryStateStore):
        store._runs[run] = replace(store._runs[run], current_budget_digest=digest)
    else:
        with store._transaction("IMMEDIATE") as connection:
            connection.execute(
                "UPDATE runs SET current_budget_digest = ? WHERE run_id = ?",
                (digest, run),
            )
    return digest


def seed_approved_budget(
    store: StateStore,
    database: Path,
    run: RunId,
    budget: BudgetRevisionDocument | None = None,
) -> RevisionDigest:
    store.create_draft_with_reservation(
        run,
        RepositoryId(f"repository-{run}"),
        "sha256:" + "a" * 64,
        _reservation(database, run),
    )
    return _approve_budget(store, run, budget or make_budget())


def current_revisions(store: StateStore, run: RunId) -> ApplicableRevisionDigests:
    budget_digest, _ = store.current_approved_budget(run)
    return ApplicableRevisionDigests(budget_digest=budget_digest)


def populated_task_counter_snapshot(
    *,
    run: RunId | None = None,
    task_id: TaskId | None = None,
    manual_resumes: int = 0,
    counter_overrides: CounterOverrides | None = None,
) -> TaskCounterSnapshot:
    resolved_run = RunId("run-1") if run is None else run
    resolved_task_id = TaskId("task-A") if task_id is None else task_id
    snapshot = TaskCounterSnapshot(
        run_id=resolved_run,
        task_id=resolved_task_id,
        allocated_calls=16,
        model_calls=11,
        input_tokens=1_337,
        output_tokens=233,
        cost_reserve_usd=Decimal("1.75"),
        attempts=2,
        stale_refreshes=1,
        manual_resumes=manual_resumes,
        next_lease_generation=3,
        failure_digests=("sha256:" + "f" * 64,),
        checkpoint_history=(CheckpointKey("2" * 40, "sha256:" + "3" * 64),),
        invalid_action_history=("sha256:" + "4" * 64,),
        warning_keys=("MODEL_CALLS:80",),
    )
    overrides: CounterOverrides = {} if counter_overrides is None else counter_overrides
    return replace(snapshot, **overrides)


def seed_paused_task(
    store: StateStore,
    *,
    task_id: str,
    pause_sequence: int,
    pause_reason: str,
    counters: TaskCounterSnapshot,
    budget_ceiling_exhaustions: tuple[BudgetCeilingExhaustion, ...] = (),
) -> TaskPauseBinding:
    run = counters.run_id
    normalized_counters = replace(counters, task_id=TaskId(task_id))
    budget_digest, _ = store.current_approved_budget(run)
    revisions = current_revisions(store, run)
    pause = TaskPauseBinding(
        run_id=run,
        task_id=TaskId(task_id),
        pause_sequence=AuditSequence(pause_sequence),
        pause_reason=pause_reason,
        counter_snapshot_digest=normalized_counters.digest,
        previous_attempt_id=AttemptId(f"attempt-before-{task_id}"),
        budget_digest_at_pause=budget_digest,
        applicable_revision_digests_at_pause=revisions,
        budget_ceiling_exhaustions=budget_ceiling_exhaustions,
    )
    store.install_task_pause_for_test(
        pause,
        normalized_counters,
        revisions,
    )
    return pause


def exact_resume_request(
    authority: AuthorityService,
    store: StateStore,
    pause: TaskPauseBinding,
) -> ResumeTaskRequest:
    del authority
    return ResumeTaskRequest(
        run_id=pause.run_id,
        task_id=pause.task_id,
        pause_sequence=pause.pause_sequence,
        pause_reason=pause.pause_reason,
        applicable_revision_digests=current_revisions(store, pause.run_id),
        expected_sequence=store.audit_sequence(pause.run_id),
    )


def authority_with_pause(
    database: Path,
    *,
    pause_reason: str,
    manual_resumes: int = 0,
    counter_overrides: CounterOverrides | None = None,
) -> tuple[AuthorityService, SqliteStateStore, TaskPauseBinding]:
    store = SqliteStateStore(database)
    run = RunId("run-1")
    seed_approved_budget(store, database, run)
    counters = populated_task_counter_snapshot(
        run=run,
        manual_resumes=manual_resumes,
        counter_overrides=counter_overrides,
    )
    pause = seed_paused_task(
        store,
        task_id="task-A",
        pause_sequence=41,
        pause_reason=pause_reason,
        counters=counters,
    )
    return AuthorityService(journal=store), store, pause


def test_resume_requires_exact_pause_and_preserves_all_history(tmp_path: Path) -> None:
    store = SqliteStateStore(tmp_path / "state.db")
    run = RunId("run-1")
    seed_approved_budget(store, tmp_path / "state.db", run)
    authority = AuthorityService(journal=store)
    pause = seed_paused_task(
        store,
        task_id="task-A",
        pause_sequence=41,
        pause_reason="REPEATED_CHECKPOINT",
        counters=populated_task_counter_snapshot(manual_resumes=0),
    )
    before = authority.task_counters(pause.run_id, pause.task_id)
    assert store.new_dispatch_open(pause.run_id) is False
    wrong = authority.resume_task(
        replace(
            exact_resume_request(authority, store, pause),
            pause_sequence=AuditSequence(pause.pause_sequence + 1),
        )
    )
    assert wrong.decision == "STALE"
    assert authority.task_counters(pause.run_id, pause.task_id) == before
    assert store.new_dispatch_open(pause.run_id) is False

    wrong_revision = authority.resume_task(
        replace(
            exact_resume_request(authority, store, pause),
            applicable_revision_digests=ApplicableRevisionDigests(
                budget_digest=RevisionDigest("sha256:" + "9" * 64)
            ),
        )
    )
    assert wrong_revision.decision == "STALE"
    assert authority.task_counters(pause.run_id, pause.task_id) == before
    assert store.new_dispatch_open(pause.run_id) is False

    accepted = authority.resume_task(exact_resume_request(authority, store, pause))
    after = authority.task_counters(pause.run_id, pause.task_id)
    assert accepted.decision == "RESUME"
    assert accepted.task_state == "READY"
    assert accepted.new_attempt_id != pause.previous_attempt_id
    assert 0 < accepted.allocated_calls <= 8
    for field_name in PRESERVED_COUNTER_FIELDS:
        assert getattr(after, field_name) == getattr(before, field_name)
    assert after.manual_resumes == before.manual_resumes + 1
    assert store.new_dispatch_open(pause.run_id) is True


@pytest.mark.parametrize(
    "pause_reason",
    [
        "CONTEXT_OVERFLOW",
        "SCOPE_EXPANSION_REQUIRED",
        "RUN_CHECK_FAILED",
        "FROZEN_BINDING_MISMATCH",
    ],
)
def test_non_resumable_task_causes_require_cancel_new_run(
    pause_reason: str,
    tmp_path: Path,
) -> None:
    authority, store, pause = authority_with_pause(
        tmp_path / "non-resumable.db",
        pause_reason=pause_reason,
    )
    result = authority.resume_task(exact_resume_request(authority, store, pause))
    assert result.decision == "DENY"
    assert result.safe_next_action == "CANCEL_AND_CREATE_NEW_RUN"


def test_third_manual_resume_is_non_raiseable(tmp_path: Path) -> None:
    authority, store, pause = authority_with_pause(
        tmp_path / "resume-cap.db",
        manual_resumes=2,
        pause_reason="NO_PROGRESS",
    )
    result = authority.resume_task(exact_resume_request(authority, store, pause))
    assert result.decision == "DENY"
    assert result.failed_invariant == "MANUAL_RESUME_CAP_REACHED"


@pytest.mark.parametrize(
    "counter_overrides",
    ({"attempts": 5}, {"stale_refreshes": 3}),
)
def test_task_attempt_and_stale_refresh_caps_require_a_new_run(
    counter_overrides: CounterOverrides,
    tmp_path: Path,
) -> None:
    authority, store, pause = authority_with_pause(
        tmp_path / "fixed-task-cap.db",
        pause_reason="NO_PROGRESS",
        counter_overrides=counter_overrides,
    )
    result = authority.resume_task(exact_resume_request(authority, store, pause))
    assert result.decision == "DENY"
    assert result.failed_invariant == "NON_RAISEABLE_CAP_REACHED"
    assert result.safe_next_action == "CANCEL_AND_CREATE_NEW_RUN"


def test_infrastructure_resume_requires_current_objective_repair(tmp_path: Path) -> None:
    authority, store, pause = authority_with_pause(
        tmp_path / "repair.db",
        pause_reason="CHECK_INFRASTRUCTURE_UNCERTAINTY",
    )
    denied = authority.resume_task(exact_resume_request(authority, store, pause))
    assert denied.failed_invariant == "INFRASTRUCTURE_CAUSE_NOT_REPAIRED"

    store.record_trusted_task_repair_for_test(
        pause,
        observation_digest="sha256:" + "8" * 64,
    )
    accepted = authority.resume_task(exact_resume_request(authority, store, pause))
    assert accepted.decision == "RESUME"


def authority_with_lowered_model_call_ceiling(
    database: Path,
    *,
    used: int,
    ceiling: int,
) -> tuple[AuthorityService, SqliteStateStore, TaskPauseBinding]:
    store = SqliteStateStore(database)
    run = RunId("run-1")
    original_budget = make_budget(model_call_ceiling=ceiling, input_token_ceiling=1_000_000)
    original_digest = seed_approved_budget(store, database, run, original_budget)
    authority = AuthorityService(journal=store)
    authority.settle_global_usage(
        run,
        GlobalBudgetMetric.MODEL_CALLS,
        used,
        expected_sequence=store.audit_sequence(run),
    )
    pause = seed_paused_task(
        store,
        task_id="task-budget",
        pause_sequence=61,
        pause_reason="LOWERED_BUDGET_CEILING",
        counters=populated_task_counter_snapshot(run=run, task_id=TaskId("task-budget")),
        budget_ceiling_exhaustions=(
            BudgetCeilingExhaustion(
                GlobalBudgetMetric.MODEL_CALLS,
                used,
                ceiling,
                original_digest,
            ),
        ),
    )
    return authority, store, pause


def test_lowered_ceiling_requires_approved_higher_budget_and_exact_resume(
    tmp_path: Path,
) -> None:
    authority, store, pause = authority_with_lowered_model_call_ceiling(
        tmp_path / "lowered.db",
        used=24,
        ceiling=24,
    )
    assert authority.resume_task(exact_resume_request(authority, store, pause)).decision == "DENY"
    _approve_budget(store, pause.run_id, make_budget(model_call_ceiling=32))
    accepted = authority.resume_task(exact_resume_request(authority, store, pause))
    assert accepted.decision == "RESUME"
    assert accepted.allocated_calls == 8


def test_fixed_table_maximum_cannot_be_restored_but_lower_revision_can(
    tmp_path: Path,
) -> None:
    lower, lower_store, pause = authority_with_lowered_model_call_ceiling(
        tmp_path / "lower.db",
        used=24,
        ceiling=24,
    )
    assert lower.resume_task(exact_resume_request(lower, lower_store, pause)).failed_invariant == (
        "HIGHER_APPROVED_BUDGET_REQUIRED"
    )
    _approve_budget(lower_store, pause.run_id, make_budget(model_call_ceiling=32))
    assert lower.resume_task(exact_resume_request(lower, lower_store, pause)).decision == "RESUME"

    fixed_store = SqliteStateStore(tmp_path / "fixed.db")
    run = RunId("run-fixed")
    fixed_digest = seed_approved_budget(fixed_store, tmp_path / "fixed.db", run)
    fixed = AuthorityService(journal=fixed_store)
    fixed.settle_global_usage(
        run,
        GlobalBudgetMetric.MODEL_CALLS,
        240,
        expected_sequence=fixed_store.audit_sequence(run),
    )
    fixed_pause = seed_paused_task(
        fixed_store,
        task_id="task-fixed",
        pause_sequence=62,
        pause_reason="LOWERED_BUDGET_CEILING",
        counters=populated_task_counter_snapshot(run=run, task_id=TaskId("task-fixed")),
        budget_ceiling_exhaustions=(
            BudgetCeilingExhaustion(
                GlobalBudgetMetric.MODEL_CALLS,
                240,
                240,
                fixed_digest,
            ),
        ),
    )
    denied = fixed.resume_task(exact_resume_request(fixed, fixed_store, fixed_pause))
    assert denied.failed_invariant == "NON_RAISEABLE_CAP_REACHED"
    assert denied.safe_next_action == "CANCEL_AND_CREATE_NEW_RUN"


def test_unrelated_budget_raise_does_not_restore_exhausted_metric(tmp_path: Path) -> None:
    authority, store, pause = authority_with_lowered_model_call_ceiling(
        tmp_path / "unrelated.db",
        used=24,
        ceiling=24,
    )
    _approve_budget(
        store,
        pause.run_id,
        make_budget(model_call_ceiling=24, input_token_ceiling=2_000_000),
    )
    denied = authority.resume_task(exact_resume_request(authority, store, pause))
    assert denied.failed_invariant == "HIGHER_APPROVED_BUDGET_REQUIRED"


def test_pause_budget_exhaustions_round_trip_and_unknown_metric_fails_closed(
    tmp_path: Path,
) -> None:
    database = tmp_path / "pause.db"
    store = SqliteStateStore(database)
    run = RunId("run-1")
    budget_digest = seed_approved_budget(store, database, run)
    pause = seed_paused_task(
        store,
        task_id="task-budget",
        pause_sequence=61,
        pause_reason="LOWERED_BUDGET_CEILING",
        counters=populated_task_counter_snapshot(run=run, task_id=TaskId("task-budget")),
        budget_ceiling_exhaustions=(
            BudgetCeilingExhaustion(
                GlobalBudgetMetric.MODEL_CALLS,
                24,
                24,
                budget_digest,
            ),
            BudgetCeilingExhaustion(
                GlobalBudgetMetric.COST_RESERVE_USD,
                Decimal("2.50"),
                Decimal("2.50"),
                budget_digest,
            ),
        ),
    )
    store.close()
    reopened = SqliteStateStore(database)
    restored = reopened.current_task_pause(pause.run_id, pause.task_id)
    assert restored is not None
    assert restored.run_id == pause.run_id
    assert restored.task_id == pause.task_id
    assert restored.pause_sequence == pause.pause_sequence
    assert restored.pause_reason == pause.pause_reason
    assert restored.counter_snapshot_digest == pause.counter_snapshot_digest
    assert restored.previous_attempt_id == pause.previous_attempt_id
    assert restored.budget_digest_at_pause == pause.budget_digest_at_pause
    assert (
        restored.applicable_revision_digests_at_pause == pause.applicable_revision_digests_at_pause
    )
    assert restored.budget_ceiling_exhaustions == tuple(
        sorted(pause.budget_ceiling_exhaustions, key=lambda item: str(item.metric))
    )
    before = reopened.audit_sequence(pause.run_id)
    with reopened._transaction("IMMEDIATE") as connection:
        connection.execute(
            "UPDATE task_pauses SET budget_ceiling_exhaustions_json = ? "
            "WHERE run_id = ? AND task_id = ?",
            (
                '[{"budget_digest":"sha256:'
                + "a" * 64
                + '","ceiling":"1","metric":"DISK_BYTES","used":"1"}]',
                pause.run_id,
                pause.task_id,
            ),
        )
    with pytest.raises(AuthorityDenied, match="UNKNOWN_GLOBAL_BUDGET_METRIC"):
        reopened.current_task_pause(pause.run_id, pause.task_id)
    assert reopened.audit_sequence(pause.run_id) == before


def test_direct_unsorted_budget_exhaustion_json_fails_closed(tmp_path: Path) -> None:
    database = tmp_path / "pause-unsorted.db"
    store = SqliteStateStore(database)
    run = RunId("run-1")
    seed_approved_budget(store, database, run)
    pause = seed_paused_task(
        store,
        task_id="task-budget-unsorted",
        pause_sequence=62,
        pause_reason="LOWERED_BUDGET_CEILING",
        counters=populated_task_counter_snapshot(
            run=run,
            task_id=TaskId("task-budget-unsorted"),
        ),
    )
    unsorted_json = (
        '[{"budget_digest":"sha256:'
        + "a" * 64
        + '","ceiling":"24","metric":"MODEL_CALLS","used":"24"},'
        + '{"budget_digest":"sha256:'
        + "a" * 64
        + '","ceiling":"2.50","metric":"COST_RESERVE_USD","used":"2.50"}]'
    )
    with store._transaction("IMMEDIATE") as connection:
        connection.execute(
            "UPDATE task_pauses SET budget_ceiling_exhaustions_json = ? "
            "WHERE run_id = ? AND task_id = ?",
            (unsorted_json, pause.run_id, pause.task_id),
        )
    with pytest.raises(ValueError, match="BUDGET_CEILING_EXHAUSTIONS_INVALID"):
        store.current_task_pause(pause.run_id, pause.task_id)


def test_memory_and_sqlite_exact_resume_preserve_the_same_counters(tmp_path: Path) -> None:
    stores = (InMemoryStateStore(), SqliteStateStore(tmp_path / "state.db"))
    results: list[tuple[str, int, int]] = []
    for store in stores:
        run = RunId("run-1")
        seed_approved_budget(store, tmp_path / "state.db", run)
        authority = AuthorityService(journal=store)
        pause = seed_paused_task(
            store,
            task_id="task-parity",
            pause_sequence=52,
            pause_reason="NO_PROGRESS",
            counters=populated_task_counter_snapshot(
                run=run,
                task_id=TaskId("task-parity"),
                manual_resumes=0,
            ),
        )
        before_calls = authority.task_counters(pause.run_id, pause.task_id).model_calls
        decision = authority.resume_task(exact_resume_request(authority, store, pause))
        after = authority.task_counters(pause.run_id, pause.task_id)
        results.append(
            (
                decision.decision,
                after.model_calls - before_calls,
                after.manual_resumes,
            )
        )
    assert results == [("RESUME", 0, 1), ("RESUME", 0, 1)]
