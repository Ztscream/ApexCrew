from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from inspect import signature
from pathlib import Path

import pytest
from test_leases import make_budget

from apexcrew.adapters.state.memory import InMemoryStateStore
from apexcrew.adapters.state.sqlite import SqliteStateStore
from apexcrew.domain.authority import (
    ActionDeadline,
    AuthorityDenied,
    AuthorityService,
    TimeoutDecision,
)
from apexcrew.domain.commands import ApplicableRevisionDigests
from apexcrew.domain.effects import (
    EffectIntent,
    TargetReservation,
    canonical_json,
    sha256_digest,
)
from apexcrew.domain.revisions import revision_digest
from apexcrew.domain.types import AuditSequence, GitOid, IntentId, RepositoryId, RunId


@dataclass
class AdjustableUtcClock:
    instant: datetime

    def now(self) -> datetime:
        return self.instant


DeadlineStore = InMemoryStateStore | SqliteStateStore


def seed_store(
    store: DeadlineStore,
    intents: tuple[tuple[str, str], ...],
) -> DeadlineStore:
    run = RunId("run-1")
    store.create_draft_with_reservation(
        run,
        RepositoryId("repository-run-1"),
        "sha256:" + "a" * 64,
        TargetReservation(
            reservation_id="reservation-run-1",
            run_id=run,
            target_ref="refs/heads/main",
            pinned_target_oid=GitOid("1" * 40),
            path=Path.cwd() / "data" / "reservations" / "reservation-run-1",
            phase="ALLOCATED",
        ),
    )
    budget = make_budget()
    store.install_approved_budget_for_test(run, revision_digest(budget), budget)
    revisions = ApplicableRevisionDigests()
    for intent_id, kind in intents:
        action: dict[str, str] = {"kind": kind}
        if kind == "check":
            action["check_id"] = "task-check-1"
        payload = canonical_json(
            {
                "action": action,
                "snapshot_digest": "sha256:" + "4" * 64,
            }
        )
        expected_sequence = store.audit_sequence(run)
        store.record_intent(
            EffectIntent(
                intent_id=IntentId(intent_id),
                run_id=run,
                kind=kind,
                idempotency_key=f"action:{intent_id}",
                applicable_revision_digests=revisions,
                payload_digest=sha256_digest(payload),
                normalized_payload_json=payload,
                recorded_sequence=AuditSequence(expected_sequence + 1),
            ),
            expected_sequence,
        )
    return store


def seed_deadline_store(
    intents: tuple[tuple[str, str], ...],
) -> InMemoryStateStore:
    return seed_store(InMemoryStateStore(), intents)


def authority_with_open_deadline(
    kind: str,
) -> tuple[AuthorityService, InMemoryStateStore, AdjustableUtcClock, ActionDeadline]:
    intent_id = f"intent-{kind}"
    store = seed_deadline_store(((intent_id, kind),))
    clock = AdjustableUtcClock(datetime(2026, 7, 27, 8, 0, tzinfo=UTC))
    authority = AuthorityService(journal=store, utc_clock=clock)
    deadline = authority.open_action_deadline(
        RunId("run-1"),
        IntentId(intent_id),
        expected_sequence=store.audit_sequence(RunId("run-1")),
    )
    return authority, store, clock, deadline


def test_ordinary_and_check_deadlines_use_fixed_v01_limits() -> None:
    started_at = datetime(2026, 7, 27, 8, 0, tzinfo=UTC)
    clock = AdjustableUtcClock(started_at)
    store = seed_deadline_store((("intent-ordinary", "patch"), ("intent-check", "check")))
    authority = AuthorityService(journal=store, utc_clock=clock)

    ordinary = authority.open_action_deadline(
        RunId("run-1"),
        IntentId("intent-ordinary"),
        expected_sequence=store.audit_sequence(RunId("run-1")),
    )
    check = authority.open_action_deadline(
        RunId("run-1"),
        IntentId("intent-check"),
        expected_sequence=store.audit_sequence(RunId("run-1")),
    )

    assert ordinary.expires_at == started_at + timedelta(seconds=120)
    assert check.expires_at == started_at + timedelta(seconds=600)
    assert tuple(signature(authority.open_action_deadline).parameters) == (
        "run_id",
        "intent_id",
        "expected_sequence",
    )
    assert tuple(signature(authority.deadline_state).parameters) == ("deadline",)
    clock.instant = ordinary.expires_at - timedelta(microseconds=1)
    assert authority.deadline_state(ordinary) == "OPEN"
    clock.instant = ordinary.expires_at
    assert authority.deadline_state(ordinary) == "TIMED_OUT"
    clock.instant = check.expires_at - timedelta(microseconds=1)
    assert authority.deadline_state(check) == "OPEN"
    clock.instant = check.expires_at
    assert authority.deadline_state(check) == "TIMED_OUT"


def test_unobservable_ordinary_timeout_is_indeterminate() -> None:
    authority, store, clock, deadline = authority_with_open_deadline("patch")
    clock.instant = deadline.expires_at

    result = authority.settle_timeout(
        deadline,
        outcome_observable=False,
        expected_sequence=deadline.recorded_sequence,
    )

    assert result.outcome == "INDETERMINATE"
    assert result.semantic_result is None
    assert result.receipt is None
    assert result.full_reservation_charged is True
    assert store.unsettled_intents(deadline.run_id) == ()


def test_declared_check_timeout_has_no_passing_receipt() -> None:
    authority, store, clock, deadline = authority_with_open_deadline("check")
    clock.instant = deadline.expires_at

    result = authority.settle_timeout(
        deadline,
        outcome_observable=False,
        expected_sequence=deadline.recorded_sequence,
    )

    assert result.outcome == "INFRASTRUCTURE_UNCERTAINTY"
    assert result.semantic_result is None
    assert result.receipt is None
    assert result.retry_scope == ("task-check-1", "sha256:" + "4" * 64)
    assert result.retry_allowed is authority.remaining_budget_allows_retry(deadline.run_id)
    assert store.unsettled_intents(deadline.run_id) == (store.effect_intent(deadline.intent_id),)


def test_timeout_cannot_settle_before_trusted_deadline() -> None:
    authority, store, _, deadline = authority_with_open_deadline("patch")
    before = store.audit_sequence(deadline.run_id)

    with pytest.raises(AuthorityDenied, match="ACTION_DEADLINE_NOT_EXPIRED"):
        authority.settle_timeout(
            deadline,
            outcome_observable=False,
            expected_sequence=before,
        )

    assert store.audit_sequence(deadline.run_id) == before
    assert store.timeout_decision(deadline.intent_id) is None
    assert store.unsettled_intents(deadline.run_id) == (store.effect_intent(deadline.intent_id),)


def test_timeout_decision_cannot_carry_semantic_result_or_receipt() -> None:
    with pytest.raises(ValueError, match="TIMEOUT_DECISION_BINDING_INVALID"):
        TimeoutDecision(
            outcome="INFRASTRUCTURE_UNCERTAINTY",
            semantic_result="PASS",  # type: ignore[arg-type]
            receipt=object(),  # type: ignore[arg-type]
            retry_scope=("task-check-1", "sha256:" + "4" * 64),
            retry_allowed=True,
            full_reservation_charged=False,
        )
    with pytest.raises(ValueError, match="TIMEOUT_DECISION_BINDING_INVALID"):
        TimeoutDecision(
            outcome="FAILED",  # type: ignore[arg-type]
            semantic_result=None,
            receipt=None,
            retry_scope=("task-check-1", "sha256:" + "4" * 64),
            retry_allowed=True,
            full_reservation_charged=False,
        )


@pytest.mark.parametrize(
    ("kind", "expected_outcome"),
    [
        ("patch", "INDETERMINATE"),
        ("check", "INFRASTRUCTURE_UNCERTAINTY"),
    ],
)
def test_deadline_and_timeout_decision_survive_sqlite_restart(
    tmp_path: Path,
    kind: str,
    expected_outcome: str,
) -> None:
    database = tmp_path / "state.db"
    intent_id = IntentId(f"intent-{kind}")
    store = seed_store(SqliteStateStore(database), ((intent_id, kind),))
    clock = AdjustableUtcClock(datetime(2026, 7, 27, 8, 0, tzinfo=UTC))
    authority = AuthorityService(journal=store, utc_clock=clock)
    deadline = authority.open_action_deadline(
        RunId("run-1"),
        intent_id,
        expected_sequence=store.audit_sequence(RunId("run-1")),
    )
    clock.instant = deadline.expires_at
    authority.settle_timeout(
        deadline,
        outcome_observable=False,
        expected_sequence=store.audit_sequence(RunId("run-1")),
    )
    store.close()

    reopened = SqliteStateStore(database)
    assert reopened.action_deadline(intent_id) == deadline
    decision = reopened.timeout_decision(intent_id)
    assert decision is not None
    assert decision.outcome == expected_outcome
    if kind == "patch":
        assert reopened.unsettled_intents(RunId("run-1")) == ()
    else:
        assert reopened.unsettled_intents(RunId("run-1")) == (reopened.effect_intent(intent_id),)
