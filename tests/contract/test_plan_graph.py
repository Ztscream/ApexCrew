import sqlite3
from collections.abc import Callable
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from apexcrew.adapters.state.memory import InMemoryStateStore
from apexcrew.adapters.state.sqlite import SqliteStateStore
from apexcrew.domain.authority import GlobalBudgetMetric
from apexcrew.domain.commands import ApplicableRevisionDigests
from apexcrew.domain.coordination import PlanProposal
from apexcrew.domain.effects import StateConflict, TargetReservation, canonical_json
from apexcrew.domain.plan import (
    CheckDefinition,
    GlobPattern,
    PlanRevision,
    PlanValidationError,
    TaskContract,
)
from apexcrew.domain.revisions import (
    BudgetRevisionDocument,
    ModelPricingEntryDocument,
    revision_digest,
)
from apexcrew.domain.types import GitOid, RepositoryId, RevisionDigest, RunId, RunState, TaskId

StateStore = InMemoryStateStore | SqliteStateStore
StoreFactory = Callable[[Path], StateStore]
POLICY_DIGEST = RevisionDigest("sha256:" + "3" * 64)
MODEL_CONFIGURATION_DIGEST = RevisionDigest("sha256:" + "5" * 64)
PINNED_BASE = GitOid("1" * 40)


def _memory_store(_: Path) -> InMemoryStateStore:
    return InMemoryStateStore()


def _sqlite_store(path: Path) -> SqliteStateStore:
    return SqliteStateStore(path)


def _budget(task_ceiling: int = 12) -> BudgetRevisionDocument:
    return BudgetRevisionDocument(
        schema_version="budget-revision-v1",
        active_run_seconds_ceiling=28_800,
        task_ceiling=task_ceiling,
        planning_request_ceiling=8,
        model_call_ceiling=240,
        input_token_ceiling=2_000_000,
        output_token_ceiling=200_000,
        cost_reserve_usd=Decimal(10),
        concurrent_worker_ceiling=3,
        pricing_observed_on=date(2026, 7, 26),
        pricing_entries=(
            ModelPricingEntryDocument(
                returned_model_id="mock-model",
                input_usd_per_million=Decimal(1),
                output_usd_per_million=Decimal(1),
            ),
        ),
    )


def _seed_planning_store(
    store: StateStore,
    root: Path,
    run_id: RunId,
    *,
    task_ceiling: int = 12,
) -> ApplicableRevisionDigests:
    store.create_draft_with_reservation(
        run_id,
        RepositoryId(f"repository-{run_id}"),
        "sha256:" + "a" * 64,
        TargetReservation(
            reservation_id=f"reservation-{run_id}",
            run_id=run_id,
            target_ref="refs/heads/main",
            pinned_target_oid=PINNED_BASE,
            path=root / "reservations" / f"reservation-{run_id}",
            phase="ALLOCATED",
        ),
    )
    budget = _budget(task_ceiling)
    budget_digest = revision_digest(budget)
    store.install_approved_budget_for_test(run_id, budget_digest, budget)
    bindings = ApplicableRevisionDigests(
        policy_digest=POLICY_DIGEST,
        budget_digest=budget_digest,
        model_configuration_digest=MODEL_CONFIGURATION_DIGEST,
    )
    if isinstance(store, InMemoryStateStore):
        with store._lock:
            store._runs[run_id] = replace(
                store._runs[run_id],
                state=RunState.PLANNING,
                current_policy_digest=bindings.policy_digest,
                current_budget_digest=bindings.budget_digest,
                current_model_configuration_digest=bindings.model_configuration_digest,
            )
    else:
        with store._transaction("IMMEDIATE") as connection:
            connection.execute(
                "UPDATE runs SET state = 'PLANNING', current_policy_digest = ?, "
                "current_budget_digest = ?, current_model_configuration_digest = ? "
                "WHERE run_id = ?",
                (
                    bindings.policy_digest,
                    bindings.budget_digest,
                    bindings.model_configuration_digest,
                    run_id,
                ),
            )
    return bindings


def _seed_planning_request_count(store: StateStore, run_id: RunId, count: int) -> None:
    if isinstance(store, InMemoryStateStore):
        with store._lock:
            store._planning_request_counts[run_id] = count
    else:
        with store._transaction("IMMEDIATE") as connection:
            connection.execute(
                "INSERT INTO run_authority_counters(run_id, planning_requests) VALUES (?, ?)",
                (run_id, count),
            )


def _check(path: str) -> CheckDefinition:
    return CheckDefinition(
        argv=("pytest", "-q"),
        input_globs=(GlobPattern.parse(path),),
    )


def _task(
    task_number: int,
    *,
    dependency: TaskId | None = None,
    observe_dependency: bool = False,
) -> TaskContract:
    task_id = f"task-{task_number:02d}"
    path = f"src/task_{task_number:02d}.py"
    dependency_path = (
        ()
        if dependency is None or not observe_dependency
        else (f"src/task_{str(dependency).removeprefix('task-')}.py",)
    )
    return TaskContract.from_strings(
        task_id,
        (path,),
        (path,),
        dependency_task_ids=() if dependency is None else (dependency,),
        dependency_globs=dependency_path,
        checks=(_check(path),),
        constraints=(f"constraint-{task_number:02d}",),
    )


def _canonical_plan_json(
    plan: PlanRevision,
    run_checks: tuple[CheckDefinition, ...],
) -> str:
    return canonical_json(
        {
            "proposed_promotion_order": list(plan.proposed_promotion_order),
            "run_checks": [
                {
                    "argv": list(check.argv),
                    "input_globs": [item.value for item in check.input_globs],
                }
                for check in run_checks
            ],
            "tasks": [
                {
                    "checks": [
                        {
                            "argv": list(check.argv),
                            "input_globs": [item.value for item in check.input_globs],
                        }
                        for check in task.checks
                    ],
                    "constraints": list(task.constraints),
                    "dependency_globs": [item.value for item in task.dependency_globs],
                    "dependency_task_ids": list(task.dependency_task_ids),
                    "read_globs": [item.value for item in task.read_globs],
                    "task_id": task.task_id,
                    "write_globs": [item.value for item in task.write_globs],
                }
                for task in plan.tasks
            ],
        }
    )


def _proposal(
    run_id: RunId,
    bindings: ApplicableRevisionDigests,
    *,
    task_count: int,
    planning_request_count: int,
) -> PlanProposal:
    tasks: list[TaskContract] = []
    for index in range(1, task_count + 1):
        predecessor = None if index == 1 else TaskId(f"task-{index - 1:02d}")
        tasks.append(_task(index, dependency=predecessor, observe_dependency=True))
    plan = PlanRevision(
        tasks=tuple(tasks),
        proposed_promotion_order=tuple(task.task_id for task in tasks),
    )
    run_checks = (_check("src/**"),)
    return PlanProposal.from_validated_plan(
        run_id=run_id,
        canonical_plan_json=_canonical_plan_json(plan, run_checks),
        plan=plan,
        base_run_head_oid=PINNED_BASE,
        applicable_revision_digests=bindings,
        run_check_set=run_checks,
        planning_request_count=planning_request_count,
    )


def test_plan_graph_and_planning_count_survive_restart(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    store = SqliteStateStore(database)
    run_id = RunId("run-plan")
    bindings = _seed_planning_store(store, tmp_path, run_id)

    reversed_dependency = PlanRevision(
        tasks=(_task(1, dependency=TaskId("task-02")), _task(2)),
        proposed_promotion_order=(TaskId("task-01"), TaskId("task-02")),
    )
    run_checks = (_check("src/**"),)
    with pytest.raises(PlanValidationError, match="PROMOTION_ORDER_REQUIRED"):
        PlanProposal.from_validated_plan(
            run_id=run_id,
            canonical_plan_json=_canonical_plan_json(reversed_dependency, run_checks),
            plan=reversed_dependency,
            base_run_head_oid=PINNED_BASE,
            applicable_revision_digests=bindings,
            run_check_set=run_checks,
            planning_request_count=1,
        )

    proposal = _proposal(run_id, bindings, task_count=3, planning_request_count=7)
    _seed_planning_request_count(store, run_id, 7)
    store.persist_plan_proposal(
        proposal,
        expected_sequence=store.audit_sequence(proposal.run_id),
    )
    store.close()

    reopened = SqliteStateStore(database)
    assert reopened.plan_proposal(proposal.run_id, proposal.plan_digest) == proposal
    assert reopened.task_contracts(proposal.plan_digest) == proposal.plan.tasks
    assert reopened.task_dependency_edges(proposal.plan_digest) == proposal.dependency_edges
    assert reopened.hazard_edges(proposal.plan_digest) == proposal.hazard_edges
    assert reopened.run_check_set(proposal.plan_digest) == proposal.run_check_set
    assert reopened.planning_request_count(proposal.run_id) == 7

    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM plans").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM task_contracts").fetchone() == (3,)
        assert connection.execute("SELECT COUNT(*) FROM task_dependencies").fetchone() == (2,)
        assert connection.execute("SELECT COUNT(*) FROM hazard_edges").fetchone() == (2,)
        assert connection.execute("SELECT COUNT(*) FROM run_checks").fetchone() == (1,)


@pytest.mark.parametrize("store_factory", [_memory_store, _sqlite_store])
def test_plan_task_count_producer_warns_and_stops_at_budget_thresholds(
    tmp_path: Path,
    store_factory: StoreFactory,
) -> None:
    warning_store = store_factory(tmp_path / "task-warning.db")
    warning_run = RunId("run-task-warning")
    warning_bindings = _seed_planning_store(warning_store, tmp_path, warning_run)
    warning = _proposal(
        warning_run,
        warning_bindings,
        task_count=10,
        planning_request_count=1,
    )
    _seed_planning_request_count(warning_store, warning_run, 1)
    warning_store.persist_plan_proposal(
        warning,
        expected_sequence=warning_store.audit_sequence(warning.run_id),
    )
    assert warning_store.global_usage_snapshot(warning.run_id).tasks == 10
    assert warning_store.budget_warnings(warning.run_id, GlobalBudgetMetric.TASKS)[0].used == 10
    assert warning_store.run_record(warning.run_id).state == RunState.AWAITING_PLAN_APPROVAL

    stop_store = store_factory(tmp_path / "task-stop.db")
    stop_run = RunId("run-task-stop")
    stop_bindings = _seed_planning_store(stop_store, tmp_path, stop_run)
    at_ceiling = _proposal(
        stop_run,
        stop_bindings,
        task_count=12,
        planning_request_count=1,
    )
    _seed_planning_request_count(stop_store, stop_run, 1)
    stop_store.persist_plan_proposal(
        at_ceiling,
        expected_sequence=stop_store.audit_sequence(at_ceiling.run_id),
    )
    assert stop_store.plan_proposal(at_ceiling.run_id, at_ceiling.plan_digest) == at_ceiling
    assert stop_store.global_usage_snapshot(at_ceiling.run_id).tasks == 12
    assert stop_store.run_record(at_ceiling.run_id).state == RunState.PAUSED
    assert stop_store.new_dispatch_open(at_ceiling.run_id) is False
    assert stop_store.audit_event_kinds(at_ceiling.run_id)[-2:] == (
        "PLAN_PROPOSED",
        "BUDGET_STOP_REQUESTED",
    )

    overflow_store = store_factory(tmp_path / "task-overflow.db")
    overflow_run = RunId("run-task-overflow")
    overflow_bindings = _seed_planning_store(
        overflow_store,
        tmp_path,
        overflow_run,
        task_ceiling=2,
    )
    overflow = _proposal(
        overflow_run,
        overflow_bindings,
        task_count=3,
        planning_request_count=1,
    )
    _seed_planning_request_count(overflow_store, overflow_run, 1)
    before = overflow_store.audit_sequence(overflow_run)
    with pytest.raises(StateConflict, match="PLAN_TASK_CEILING"):
        overflow_store.persist_plan_proposal(overflow, expected_sequence=before)
    assert overflow_store.global_usage_snapshot(overflow_run).tasks == 0
    assert overflow_store.audit_sequence(overflow_run) == before
    with pytest.raises(StateConflict, match="PLAN_PROPOSAL_NOT_FOUND"):
        overflow_store.plan_proposal(overflow_run, overflow.plan_digest)
