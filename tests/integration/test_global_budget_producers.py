from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from apexcrew.adapters.model.scripted import ScriptedMockLLM
from apexcrew.adapters.state.sqlite import SqliteStateStore
from apexcrew.domain.authority import (
    AttemptAuthority,
    AuthorityService,
    GlobalBudgetMetric,
    ModelReservationRequest,
)
from apexcrew.domain.effects import TargetReservation
from apexcrew.domain.model import (
    ModelCompletion,
    ModelRequest,
    ModelUsage,
    ProviderAttemptResult,
)
from apexcrew.domain.plan import TaskContract
from apexcrew.domain.revisions import (
    BudgetRevisionDocument,
    ModelPricingEntryDocument,
    revision_digest,
)
from apexcrew.domain.types import GitOid, RepositoryId, RunId


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
        "pricing_observed_on": date(2026, 8, 5),
        "pricing_entries": (
            ModelPricingEntryDocument(
                returned_model_id="deepseek-v4-flash",
                input_usd_per_million=Decimal("0.28"),
                output_usd_per_million=Decimal("0.56"),
            ),
        ),
    }
    values.update(overrides)
    return BudgetRevisionDocument.model_validate(values)


def make_authority(
    store: SqliteStateStore,
    tmp_path: Path,
    **budget_overrides: object,
) -> AuthorityService:
    run_id = RunId("run-1")
    target_safety_digest = "sha256:" + "c" * 64
    store.create_draft_with_reservation(
        run_id,
        RepositoryId("repository-1"),
        "sha256:" + "a" * 64,
        TargetReservation(
            reservation_id="reservation-1",
            run_id=run_id,
            target_ref="refs/heads/main",
            pinned_target_oid=GitOid("1" * 40),
            path=tmp_path / "data" / "reservations" / "reservation-1",
            phase="ALLOCATED",
        ),
    )
    budget = make_budget(**budget_overrides)
    budget_digest = revision_digest(budget)
    store.install_approved_budget_for_test(run_id, budget_digest, budget)
    with store._transaction("IMMEDIATE") as connection:
        connection.execute(
            "UPDATE runs SET state = 'PLANNING', current_policy_digest = ?, "
            "current_budget_digest = ?, current_model_configuration_digest = ? "
            "WHERE run_id = ?",
            (
                "sha256:" + "3" * 64,
                budget_digest,
                "sha256:" + "5" * 64,
                run_id,
            ),
        )
        connection.execute(
            "UPDATE target_reservations SET phase = 'REGISTERED_LOCKED', "
            "admin_entry_name = ?, admin_binding_digest = ? WHERE run_id = ?",
            ("apexcrew-entry", target_safety_digest, run_id),
        )
    return AuthorityService(journal=store)


def planning_reservation_request(
    store: SqliteStateStore,
    expected_sequence: int,
) -> ModelReservationRequest:
    budget_digest, _ = store.current_approved_budget(RunId("run-1"))
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
        request_digest="sha256:" + "2" * 64,
        idempotency_key="request-1",
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
        expected_sequence=expected_sequence,
    )


def make_attempt(attempt_id: str) -> AttemptAuthority:
    return AttemptAuthority(
        run_id="run-1",
        task_id=f"task-{attempt_id}",
        attempt_id=attempt_id,
        generation=1,
        base_head="1" * 40,
        task_contract_digest="sha256:" + "9" * 64,
    )


def make_contract(task_id: str, write_globs: tuple[str, ...]) -> TaskContract:
    return TaskContract.from_strings(
        task_id=task_id,
        read_globs=write_globs,
        write_globs=write_globs,
    )


def test_authorized_model_reservation_produces_five_global_metrics_atomically(
    tmp_path: Path,
) -> None:
    store = SqliteStateStore(tmp_path / "producer.db")
    authority = make_authority(
        store,
        tmp_path,
        planning_request_ceiling=1,
        model_call_ceiling=1,
        input_token_ceiling=1_000,
        output_token_ceiling=200,
        cost_reserve_usd=Decimal("0.000392"),
    )
    reserved = authority.reserve_model_attempt(
        planning_reservation_request(
            store,
            expected_sequence=store.audit_sequence(RunId("run-1")),
        )
    )
    assert reserved.decision == "RESERVED"
    assert reserved.intent is not None
    assert set(store.budget_warning_metrics("run-1")) == {
        GlobalBudgetMetric.PLANNING_REQUESTS,
        GlobalBudgetMetric.MODEL_CALLS,
        GlobalBudgetMetric.INPUT_TOKENS,
        GlobalBudgetMetric.OUTPUT_TOKENS,
        GlobalBudgetMetric.COST_RESERVE_USD,
    }
    assert store.new_dispatch_open("run-1") is False
    assert store.runtime_barrier_state("run-1") == "IDLE"


def test_zero_cost_reserve_pauses_before_provider_dispatch(tmp_path: Path) -> None:
    store = SqliteStateStore(tmp_path / "zero-cost.db")
    authority = make_authority(store, tmp_path, cost_reserve_usd=Decimal(0))
    provider = ScriptedMockLLM(())
    denied = authority.reserve_model_attempt(
        planning_reservation_request(
            store,
            expected_sequence=store.audit_sequence(RunId("run-1")),
        )
    )
    assert denied.decision == "PAUSE"
    assert denied.reason == "COST_RESERVE_CEILING"
    assert denied.intent is None
    assert provider.call_count == 0


def test_workspace_lease_count_is_the_concurrent_worker_producer(tmp_path: Path) -> None:
    store = SqliteStateStore(tmp_path / "workers.db")
    authority = make_authority(store, tmp_path, concurrent_worker_ceiling=1)
    lease = authority.issue_lease(
        make_attempt("A"),
        make_contract("task-A", ("src/a.py",)),
        expected_sequence=store.audit_sequence(RunId("run-1")),
    )
    assert lease.state == "ACTIVE"
    assert store.global_usage_snapshot("run-1").concurrent_workers == 1
    assert store.budget_warning_metrics("run-1") == (GlobalBudgetMetric.CONCURRENT_WORKERS,)
    assert store.new_dispatch_open("run-1") is False


def test_model_settlement_replaces_reserved_token_and_cost_usage(tmp_path: Path) -> None:
    store = SqliteStateStore(tmp_path / "settlement.db")
    authority = make_authority(store, tmp_path)
    reserved = authority.reserve_model_attempt(
        planning_reservation_request(
            store,
            expected_sequence=store.audit_sequence(RunId("run-1")),
        )
    )
    assert reserved.intent is not None

    store.settle_model_attempt(
        reserved.intent,
        ProviderAttemptResult.completed(
            ModelCompletion(
                response_id="response-1",
                requested_model_id="deepseek-v4-flash",
                returned_model_id="deepseek-v4-flash",
                usage=ModelUsage(500, 100, Decimal("0.00275")),
                normalized_action={"kind": "finish"},
            )
        ),
        expected_sequence=store.audit_sequence(RunId("run-1")),
    )

    usage = store.global_usage_snapshot("run-1")
    assert usage.model_calls == 1
    assert usage.input_tokens == 500
    assert usage.output_tokens == 100
    assert usage.cost_reserve_usd == Decimal("0.00275")


def test_expired_lease_decreases_only_the_worker_gauge(tmp_path: Path) -> None:
    store = SqliteStateStore(tmp_path / "lease-release.db")
    authority = make_authority(store, tmp_path)
    lease = authority.issue_lease(
        make_attempt("A"),
        make_contract("task-A", ("src/a.py",)),
        expected_sequence=store.audit_sequence(RunId("run-1")),
    )
    before = store.global_usage_snapshot("run-1")

    store.expire_workspace_lease(
        RunId("run-1"),
        lease.lease_id,
        expected_sequence=store.audit_sequence(RunId("run-1")),
    )

    after = store.global_usage_snapshot("run-1")
    assert after.concurrent_workers == 0
    assert after.model_calls == before.model_calls
    assert after.input_tokens == before.input_tokens
    assert after.output_tokens == before.output_tokens
    assert after.cost_reserve_usd == before.cost_reserve_usd
