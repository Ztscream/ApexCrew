from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from apexcrew.adapters.state.memory import InMemoryStateStore
from apexcrew.domain.authority import AttemptAuthority, AuthorityService, AuthorityState
from apexcrew.domain.plan import TaskContract
from apexcrew.domain.revisions import (
    BudgetRevisionDocument,
    ModelPricingEntryDocument,
    revision_digest,
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


def make_authority(
    store: AuthorityState, run_id: str = "run-1", **budget_overrides: object
) -> AuthorityService:
    budget = make_budget(**budget_overrides)
    store.install_approved_budget_for_test(run_id, revision_digest(budget), budget)
    return AuthorityService(journal=store)


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


def test_overlapping_write_leases_are_denied() -> None:
    store = InMemoryStateStore()
    authority = make_authority(store)
    first = authority.issue_lease(
        make_attempt("A"),
        make_contract("task-A", ("src/**",)),
        expected_sequence=0,
    )
    second = authority.issue_lease(
        make_attempt("B"),
        make_contract("task-B", ("src/pricing.py",)),
        expected_sequence=store.audit_sequence("run-1"),
    )
    assert first.state == "ACTIVE"
    assert second.decision == "DENY"
    assert store.audit_sequence("run-1") == 1


def test_expired_or_wrong_generation_lease_cannot_authorize_write() -> None:
    issued_at = datetime(2026, 7, 27, tzinfo=UTC)
    store = InMemoryStateStore()
    authority = make_authority(store)
    lease = authority.issue_lease(
        make_attempt("A"),
        make_contract("task-A", ("src/**",)),
        expected_sequence=0,
        now=issued_at,
    )
    assert lease.state == "ACTIVE"
    assert (
        authority.authorize_write(
            "run-1",
            lease.lease_id,
            generation=2,
            head="1" * 40,
            path="src/main.py",
            now=issued_at + timedelta(minutes=16),
            expected_sequence=store.audit_sequence("run-1"),
        ).decision
        == "DENY"
    )
