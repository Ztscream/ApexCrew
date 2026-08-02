from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol

if TYPE_CHECKING:
    from apexcrew.domain.authority import (
        ActionDeadline,
        AtomicAction,
        BudgetSettlement,
        GlobalBudgetMetric,
        GlobalUsageSnapshot,
        ResumeTaskRequest,
        TaskCounterSnapshot,
        TaskPauseBinding,
        TaskResumeDecision,
        TimeoutDecision,
    )

from apexcrew.domain.commands import (
    ApplicableRevisionDigests,
    CommandEnvelope,
    CommandOutcome,
)
from apexcrew.domain.model import (
    CommittedModelTurn,
    LogicalModelTurn,
    LogicalTurnId,
    ModelCompletion,
    ModelDispatchResult,
    ModelRequest,
    ModelRequestIntent,
    ProviderAttemptResult,
    SettledModelAttempt,
)
from apexcrew.domain.revisions import FrozenDocument, Sha256DigestText
from apexcrew.domain.types import (
    AttemptId,
    AuditSequence,
    GitOid,
    IntentId,
    RepositoryId,
    RevisionDigest,
    RunId,
    RunState,
    TaskId,
    UnresolvedSetDigest,
)


@dataclass(frozen=True, slots=True)
class TargetReservation:
    reservation_id: str
    run_id: RunId
    target_ref: str
    pinned_target_oid: GitOid
    path: Path
    phase: Literal[
        "ALLOCATED",
        "CREATION_INTENT_RECORDED",
        "REGISTERED_LOCKED",
        "CLEANUP_SETTLED",
    ]
    admin_entry_name: str | None = None
    admin_binding_digest: Sha256DigestText | None = None


@dataclass(frozen=True, slots=True)
class PlanApproval:
    run_id: RunId
    plan_digest: RevisionDigest
    approval_request_id: str
    approval_sequence: AuditSequence
    binding_digest: Sha256DigestText


@dataclass(frozen=True, slots=True)
class RunRefRecord:
    run_id: RunId
    ref_kind: Literal["PRIVATE", "TARGET"]
    ref_name: str
    expected_old_oid: GitOid | None
    current_oid: GitOid | None
    state: Literal["ABSENT_EXPECTED", "INIT_INTENT_RECORDED", "PRESENT", "CONFLICT"]
    last_intent_id: IntentId | None
    guard_binding_json: str | None = None


class ReservationObservation(FrozenDocument):
    registration_present: bool
    path_present: bool
    locked: bool
    exact_identity: bool
    gitfile_only: bool
    admin_entry_name: str | None = None
    admin_binding_digest: Sha256DigestText | None = None
    observable: bool = True

    def __init__(
        self,
        registration_present: bool,
        path_present: bool,
        locked: bool,
        exact_identity: bool,
        gitfile_only: bool,
        admin_entry_name: str | None = None,
        admin_binding_digest: Sha256DigestText | None = None,
        observable: bool = True,
    ) -> None:
        super().__init__(  # type: ignore[call-arg]
            registration_present=registration_present,
            path_present=path_present,
            locked=locked,
            exact_identity=exact_identity,
            gitfile_only=gitfile_only,
            admin_entry_name=admin_entry_name,
            admin_binding_digest=admin_binding_digest,
            observable=observable,
        )


ReservationCreationNext = Literal["ADD", "LOCK", "SETTLE", "CONFLICT", "UNOBSERVABLE"]


def classify_reservation_creation(
    observed: ReservationObservation,
) -> ReservationCreationNext:
    if not observed.observable:
        return "UNOBSERVABLE"
    if not observed.registration_present and not observed.path_present:
        return "ADD"
    if (
        observed.registration_present
        and observed.path_present
        and observed.exact_identity
        and observed.gitfile_only
        and not observed.locked
    ):
        return "LOCK"
    if (
        observed.registration_present
        and observed.path_present
        and observed.exact_identity
        and observed.gitfile_only
        and observed.locked
    ):
        return "SETTLE"
    return "CONFLICT"


@dataclass(frozen=True, slots=True)
class RunRecord:
    run_id: RunId
    repository_id: RepositoryId
    repository_instance_digest: Sha256DigestText
    state: RunState
    target_ref: str
    pinned_target_oid: GitOid
    current_plan_digest: RevisionDigest | None = None
    current_policy_digest: RevisionDigest | None = None
    current_budget_digest: RevisionDigest | None = None
    current_model_configuration_digest: RevisionDigest | None = None


class StateConflict(RuntimeError):
    """A durable compare-and-set or invariant violation."""


class StateCommitFault(RuntimeError):
    """Test-only fault injected before a state/Audit transaction commits."""


def canonical_json(value: Mapping[str, object]) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=lambda item: item.isoformat() if hasattr(item, "isoformat") else str(item),
    )


def sha256_digest(payload: str) -> Sha256DigestText:
    return Sha256DigestText("sha256:" + sha256(payload.encode("utf-8")).hexdigest())


@dataclass(frozen=True, slots=True)
class AuditEvent:
    event_kind: str
    task_id: TaskId | None = None
    attempt_id: AttemptId | None = None
    action_id: str | None = None
    applicable_revision_digests: ApplicableRevisionDigests | None = None
    result_class: str | None = None
    subject_digests: tuple[str, ...] = ()
    timing_ms: int | None = None
    budget_delta_json: str | None = None
    runtime_owner_generation: int | None = None
    runtime_monotonic_nanoseconds: int | None = None

    @classmethod
    def kind(cls, event_kind: str, **fields: Any) -> AuditEvent:
        if not event_kind or event_kind.strip() != event_kind:
            raise ValueError("AUDIT_EVENT_KIND_INVALID")
        return cls(event_kind=event_kind, **fields)


@dataclass(frozen=True, slots=True)
class EffectIntent:
    intent_id: IntentId
    run_id: RunId
    kind: str
    idempotency_key: str
    applicable_revision_digests: ApplicableRevisionDigests
    payload_digest: Sha256DigestText
    normalized_payload_json: str
    recorded_sequence: AuditSequence
    expected_prestate_json: str = "{}"
    task_id: TaskId | None = None
    attempt_id: AttemptId | None = None
    action_id: str | None = None


@dataclass(frozen=True, slots=True)
class EffectResult:
    intent_id: IntentId
    run_id: RunId
    outcome: Literal["COMPLETED", "FAILED", "STALE", "CONFLICT", "INDETERMINATE"]
    result_class: str
    result_digest: Sha256DigestText
    bounded_result_json: str
    settled_sequence: AuditSequence
    snapshot_digest: Sha256DigestText | None = None


class EffectJournal(Protocol):
    def record_command(self, command: CommandEnvelope, outcome: CommandOutcome) -> CommandOutcome:
        raise NotImplementedError

    def append_event(
        self,
        run_id: RunId,
        event: AuditEvent,
        expected_sequence: AuditSequence,
    ) -> AuditSequence:
        raise NotImplementedError

    def global_usage_snapshot(self, run_id: RunId) -> GlobalUsageSnapshot:
        raise NotImplementedError

    def current_task_pause(self, run_id: RunId, task_id: TaskId) -> TaskPauseBinding | None:
        raise NotImplementedError

    def task_counters(self, run_id: RunId, task_id: TaskId) -> TaskCounterSnapshot:
        raise NotImplementedError

    def task_repair_observed(self, pause: TaskPauseBinding) -> bool:
        raise NotImplementedError

    def accept_task_resume(
        self,
        request: ResumeTaskRequest,
        pause: TaskPauseBinding,
        counters: TaskCounterSnapshot,
        budget_digest: RevisionDigest,
        usage: GlobalUsageSnapshot,
        calls: int,
    ) -> TaskResumeDecision:
        raise NotImplementedError

    def settle_global_usage(
        self,
        run_id: RunId,
        budget_digest: RevisionDigest,
        metric: GlobalBudgetMetric,
        absolute_used: int | Decimal,
        expected_sequence: AuditSequence,
    ) -> BudgetSettlement:
        raise NotImplementedError

    def begin_atomic_action(
        self,
        action: AtomicAction,
        expected_sequence: AuditSequence,
    ) -> AtomicAction:
        raise NotImplementedError

    def settle_atomic_action(
        self,
        action: AtomicAction,
        model_calls: int,
        expected_sequence: AuditSequence,
    ) -> BudgetSettlement:
        raise NotImplementedError

    def record_intent(self, intent: EffectIntent, expected_sequence: AuditSequence) -> EffectIntent:
        raise NotImplementedError

    def record_action_deadline(
        self, deadline: ActionDeadline, expected_sequence: AuditSequence
    ) -> ActionDeadline:
        raise NotImplementedError

    def settle_action_timeout(
        self,
        deadline: ActionDeadline,
        decision: TimeoutDecision,
        expected_sequence: AuditSequence,
    ) -> TimeoutDecision:
        raise NotImplementedError

    def settle_intent(
        self,
        run_id: RunId,
        intent_id: IntentId,
        result: EffectResult,
        applicable_revision_digests: ApplicableRevisionDigests,
        expected_sequence: AuditSequence,
    ) -> AuditSequence:
        raise NotImplementedError

    def unsettled_intents(self, run_id: RunId) -> tuple[EffectIntent, ...]:
        raise NotImplementedError

    def committed_model_turn(
        self, run_id: RunId, logical_turn_id: LogicalTurnId
    ) -> CommittedModelTurn | None:
        raise NotImplementedError

    def record_downstream_action_intent(
        self,
        run_id: RunId,
        logical_turn_id: LogicalTurnId,
        intent: EffectIntent,
        expected_sequence: AuditSequence,
    ) -> AuditSequence:
        raise NotImplementedError

    def begin_model_turn_and_reserve(
        self, request: ModelRequest, expected_sequence: AuditSequence
    ) -> tuple[LogicalModelTurn, ModelRequestIntent]:
        raise NotImplementedError

    def reserve_model_attempt(
        self,
        turn: LogicalModelTurn,
        request: ModelRequest,
        provider_attempt_number: int,
        expected_sequence: AuditSequence,
    ) -> ModelRequestIntent:
        raise NotImplementedError

    def settle_model_attempt(
        self,
        intent: ModelRequestIntent,
        result: ProviderAttemptResult,
        expected_sequence: AuditSequence,
    ) -> SettledModelAttempt:
        raise NotImplementedError

    def record_model_backoff(
        self,
        run_id: RunId,
        intent_id: IntentId,
        seconds: int,
        expected_sequence: AuditSequence,
    ) -> AuditSequence:
        raise NotImplementedError

    def reserve_model_request(
        self, request: ModelRequest, expected_sequence: AuditSequence
    ) -> ModelRequestIntent:
        raise NotImplementedError

    def settle_model_request(
        self,
        intent: ModelRequestIntent,
        completion: ModelCompletion,
        allowed_model_ids: frozenset[str],
        expected_sequence: AuditSequence,
    ) -> ModelDispatchResult:
        raise NotImplementedError

    def model_request(self, run_id: RunId, intent_id: IntentId) -> ModelRequestIntent:
        raise NotImplementedError

    def reserved_call_count(self, run_id: RunId) -> int:
        raise NotImplementedError


class RecoveryDisposition(StrEnum):
    SETTLED = "SETTLED"
    STALE = "STALE"
    CONFLICT = "CONFLICT"
    INDETERMINATE = "INDETERMINATE"


@dataclass(frozen=True, slots=True)
class IntentRecovery:
    intent_id: IntentId
    recovery_generation: int
    disposition: RecoveryDisposition
    reason: str
    successor: str | None = None


@dataclass(frozen=True, slots=True)
class UnresolvedIntentMember:
    intent_id: IntentId
    recovery_generation: int
    intent_digest: Sha256DigestText


@dataclass(frozen=True, slots=True)
class RecoveryOutcome:
    recoveries: tuple[IntentRecovery, ...]
    unresolved_members: tuple[UnresolvedIntentMember, ...] = ()
    unresolved_set_digest: UnresolvedSetDigest | None = None

    @property
    def requires_human_resolution(self) -> bool:
        return bool(self.unresolved_members)

    @classmethod
    def empty(cls) -> RecoveryOutcome:
        return cls(recoveries=())


class RecoveryService:
    def __init__(self, journal: EffectJournal) -> None:
        self._journal = journal

    def reconcile(self, run_id: RunId) -> RecoveryOutcome:
        unsettled = self._journal.unsettled_intents(run_id)
        if not unsettled:
            return RecoveryOutcome.empty()
        kinds = ",".join(sorted(intent.kind for intent in unsettled))
        raise RuntimeError(f"RECOVERY_STRATEGY_NOT_REGISTERED:{kinds}")
