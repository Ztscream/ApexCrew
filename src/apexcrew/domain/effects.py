from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol, Self, cast

from pydantic import model_validator

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
    from apexcrew.domain.tools import ToolDenialAudit

from apexcrew.domain.commands import (
    ApplicableRevisionDigests,
    CommandEnvelope,
    CommandOutcome,
)
from apexcrew.domain.indeterminate import (
    ResolutionApplication,
    ResolutionSelection,
    UnresolvedIntentBinding,
    UnresolvedIntentSet,
    unresolved_set_digest_for_members,
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
    RuntimeOwnerId,
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
    path_identity: str | None = None
    gitfile_digest: Sha256DigestText | None = None

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
        path_identity: str | None = None,
        gitfile_digest: Sha256DigestText | None = None,
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
            path_identity=path_identity,
            gitfile_digest=gitfile_digest,
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


@dataclass(frozen=True, slots=True)
class RunBootstrapInputs:
    goal: str
    constraints: tuple[str, ...]
    acceptance_criteria: tuple[str, ...]


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

    def record_tool_denial(
        self, denial: ToolDenialAudit, expected_sequence: AuditSequence
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

    def indeterminate_intents(self, run_id: RunId) -> tuple[EffectIntent, ...]:
        raise NotImplementedError

    def unresolved_intent_set(self, run_id: RunId) -> UnresolvedIntentSet | None:
        raise NotImplementedError

    def apply_indeterminate_resolution(
        self, request: ApplyResolutionRequest
    ) -> ResolutionApplication:
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


class RecoveryActionClass(StrEnum):
    MODEL = "MODEL"
    READ_SEARCH = "READ_SEARCH"
    PATCH = "PATCH"
    CHECK = "CHECK"
    PRIVATE_REF = "PRIVATE_REF"
    TARGET_CAS = "TARGET_CAS"
    TARGET_RESERVATION = "TARGET_RESERVATION"
    GRANTED_ACTION = "GRANTED_ACTION"


def recovery_action_class_for_intent(intent: object) -> RecoveryActionClass:
    """Derive the recovery class from a persisted typed intent, never a command."""
    if isinstance(intent, ModelRequestIntent):
        return RecoveryActionClass.MODEL
    from apexcrew.domain.admission import RefCasIntent, TargetReservationCreationIntent
    from apexcrew.domain.authority import GrantedActionIntent
    from apexcrew.domain.tools import ToolIntent

    if isinstance(intent, ToolIntent):
        action_kind = intent.action.kind
        if action_kind in {"read", "search"}:
            return RecoveryActionClass.READ_SEARCH
        if action_kind == "patch":
            return RecoveryActionClass.PATCH
        if action_kind == "check":
            return RecoveryActionClass.CHECK
    if isinstance(intent, RefCasIntent):
        return RecoveryActionClass.PRIVATE_REF
    if isinstance(intent, TargetReservationCreationIntent):
        return RecoveryActionClass.TARGET_RESERVATION
    if isinstance(intent, GrantedActionIntent):
        return RecoveryActionClass.GRANTED_ACTION
    if isinstance(intent, EffectIntent):
        effect_classes = {
            "model": RecoveryActionClass.MODEL,
            "model_request": RecoveryActionClass.MODEL,
            "private_ref_init": RecoveryActionClass.PRIVATE_REF,
            "private_ref_cas": RecoveryActionClass.PRIVATE_REF,
            "target_ref_cas": RecoveryActionClass.TARGET_CAS,
            "target_reservation_creation": RecoveryActionClass.TARGET_RESERVATION,
            "target_reservation_cleanup": RecoveryActionClass.TARGET_RESERVATION,
            "granted_risky_action": RecoveryActionClass.GRANTED_ACTION,
            "read": RecoveryActionClass.READ_SEARCH,
            "search": RecoveryActionClass.READ_SEARCH,
            "patch": RecoveryActionClass.PATCH,
            "check": RecoveryActionClass.CHECK,
        }
        try:
            return effect_classes[intent.kind]
        except KeyError as exc:
            raise ValueError("RECOVERY_ACTION_CLASS_UNSUPPORTED") from exc
    raise ValueError("RECOVERY_ACTION_CLASS_UNSUPPORTED")


RecoveryObservationState = Literal[
    "EXACT_COMPLETION",
    "RETURNED_MODEL_MISMATCH",
    "EXACT_SNAPSHOT",
    "STALE",
    "EXACT_POST",
    "EXACT_PRE",
    "TARGET_UNSAFE",
    "THIRD_STATE",
    "EXACT_RECEIPT",
    "BOTH_ABSENT",
    "BOTH_PRESENT_LOCKED",
    "BOTH_PRESENT_UNLOCKED",
    "PATH_ONLY",
    "ADMIN_ONLY",
    "MIXED",
    "UNAVAILABLE",
    "UNOBSERVABLE",
]


def _validate_bounded_result(
    bounded_result_json: str, bounded_result_digest: Sha256DigestText | None
) -> None:
    try:
        parsed = json.loads(bounded_result_json)
    except json.JSONDecodeError as exc:
        raise ValueError("READ_RESULT_NOT_JSON") from exc
    if not isinstance(parsed, dict):
        raise TypeError("READ_RESULT_OBJECT_REQUIRED")
    canonical_result = json.dumps(parsed, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    if canonical_result != bounded_result_json:
        raise ValueError("READ_RESULT_NOT_CANONICAL")
    if len(bounded_result_json.encode("utf-8")) > 131_072:
        raise ValueError("READ_RESULT_TOO_LARGE")
    forbidden_keys = {
        "credential",
        "credentials",
        "nonce",
        "raw_transcript",
        "provider_transcript",
        "secret",
        "token",
    }

    def contains_forbidden_key(value: object) -> bool:
        if isinstance(value, dict):
            return any(
                isinstance(key, str)
                and key.lower() in forbidden_keys
                or contains_forbidden_key(item)
                for key, item in value.items()
            )
        if isinstance(value, list):
            return any(contains_forbidden_key(item) for item in value)
        return False

    if contains_forbidden_key(parsed):
        raise ValueError("READ_RESULT_NOT_SANITIZED")
    if bounded_result_digest != sha256_digest(bounded_result_json):
        raise ValueError("READ_RESULT_DIGEST_MISMATCH")


class RecoveryDecisionKind(StrEnum):
    COMPLETED = "COMPLETED"
    RETRY_SAME_INTENT = "RETRY_SAME_INTENT"
    ABANDONED = "ABANDONED"
    STALE = "STALE"
    CONFLICT = "CONFLICT"
    INDETERMINATE = "INDETERMINATE"


class RecoveryObservation(FrozenDocument):
    kind: RecoveryActionClass
    intent_id: IntentId
    recovery_generation: int
    source_payload_digest: Sha256DigestText
    state: RecoveryObservationState
    observation_digest: Sha256DigestText
    run_id: RunId | None = None
    settled_sequence: AuditSequence | None = None
    applicable_revision_digests: ApplicableRevisionDigests | None = None
    request_digest: Sha256DigestText | None = None
    idempotency_key: str | None = None
    provider_response_id: str | None = None
    returned_model_id: str | None = None
    schema_digest: Sha256DigestText | None = None
    usage_json: str | None = None
    normalized_completion_digest: Sha256DigestText | None = None
    normalized_completion_json: str | None = None
    reservation_charge: Literal["FULL"] | None = None
    snapshot_digest: Sha256DigestText | None = None
    scope_digest: Sha256DigestText | None = None
    ordering_digest: Sha256DigestText | None = None
    bounded_result_json: str | None = None
    bounded_result_digest: Sha256DigestText | None = None
    completion_proof_json: str | None = None
    completion_proof_digest: Sha256DigestText | None = None
    expected_pre_tree_digest: Sha256DigestText | None = None
    observed_post_tree_digest: Sha256DigestText | None = None
    check_id: str | None = None
    argv_digest: Sha256DigestText | None = None
    receipt_digest: Sha256DigestText | None = None
    repository_id: str | None = None
    repository_instance_digest: Sha256DigestText | None = None
    ref_name: str | None = None
    registration_digest: Sha256DigestText | None = None
    target_safety_digest: Sha256DigestText | None = None
    old_oid: GitOid | None = None
    prepared_oid: GitOid | None = None
    current_oid: GitOid | None = None
    registration_identity: str | None = None
    reservation_operation: Literal["CREATE", "CLEANUP"] | None = None
    admin_binding_digest: Sha256DigestText | None = None
    path_identity: str | None = None
    gitfile_digest: Sha256DigestText | None = None
    pending_action_id: str | None = None
    grant_id: str | None = None
    expected_prestate_digest: Sha256DigestText | None = None
    action_binding_digest: Sha256DigestText | None = None

    @property
    def action_class(self) -> RecoveryActionClass:
        return self.kind

    @classmethod
    def from_intent(cls, intent: object, **values: Any) -> Self:
        idempotency_key: str | None
        if isinstance(intent, EffectIntent):
            source_digest = intent.payload_digest
            intent_id = intent.intent_id
            idempotency_key = intent.idempotency_key
        else:
            intent_any = cast(Any, intent)
            if hasattr(intent, "model_dump"):
                payload = intent_any.model_dump(mode="json")
            else:
                from dataclasses import asdict

                payload = asdict(intent_any)
            source_digest = sha256_digest(canonical_json(payload))
            intent_id = intent_any.intent_id
            idempotency_key = (
                intent_any.idempotency_key if hasattr(intent, "idempotency_key") else None
            )
        values.update(
            kind=recovery_action_class_for_intent(intent),
            intent_id=intent_id,
            source_payload_digest=source_digest,
            idempotency_key=idempotency_key,
        )
        return cls.create(**values)

    @classmethod
    def create(cls, **values: Any) -> Self:
        candidate = cls.model_construct(**values)
        payload = candidate.model_dump(
            mode="json", exclude={"observation_digest"}, exclude_none=True
        )
        values["observation_digest"] = sha256_digest(canonical_json(payload))
        return cls(**values)

    @model_validator(mode="after")
    def validate_observation(self) -> Self:
        required: dict[RecoveryActionClass, tuple[str, ...]] = {
            RecoveryActionClass.MODEL: ("request_digest",),
            RecoveryActionClass.READ_SEARCH: ("snapshot_digest", "scope_digest", "ordering_digest"),
            RecoveryActionClass.PATCH: (
                "expected_pre_tree_digest",
                "observed_post_tree_digest",
                "snapshot_digest",
            ),
            RecoveryActionClass.CHECK: ("check_id", "argv_digest", "snapshot_digest"),
            RecoveryActionClass.PRIVATE_REF: (
                "repository_id",
                "repository_instance_digest",
                "ref_name",
                "registration_digest",
                "target_safety_digest",
                "old_oid",
                "prepared_oid",
                "current_oid",
            ),
            RecoveryActionClass.TARGET_CAS: (
                "repository_id",
                "repository_instance_digest",
                "ref_name",
                "registration_digest",
                "target_safety_digest",
                "old_oid",
                "prepared_oid",
                "current_oid",
            ),
            RecoveryActionClass.TARGET_RESERVATION: (
                "registration_identity",
                "reservation_operation",
                "admin_binding_digest",
                "path_identity",
                "gitfile_digest",
            ),
            RecoveryActionClass.GRANTED_ACTION: (
                "pending_action_id",
                "grant_id",
                "expected_prestate_digest",
                "action_binding_digest",
            ),
        }
        allowed: dict[RecoveryActionClass, frozenset[str]] = {
            RecoveryActionClass.MODEL: frozenset(
                {
                    "request_digest",
                    "idempotency_key",
                    "provider_response_id",
                    "returned_model_id",
                    "schema_digest",
                    "usage_json",
                    "normalized_completion_digest",
                    "normalized_completion_json",
                    "reservation_charge",
                    "completion_proof_json",
                    "completion_proof_digest",
                }
            ),
            RecoveryActionClass.READ_SEARCH: frozenset(
                {
                    "idempotency_key",
                    "snapshot_digest",
                    "scope_digest",
                    "ordering_digest",
                    "bounded_result_json",
                    "bounded_result_digest",
                }
            ),
            RecoveryActionClass.PATCH: frozenset(
                {
                    "idempotency_key",
                    "expected_pre_tree_digest",
                    "observed_post_tree_digest",
                    "snapshot_digest",
                    "completion_proof_json",
                    "completion_proof_digest",
                }
            ),
            RecoveryActionClass.CHECK: frozenset(
                {
                    "idempotency_key",
                    "check_id",
                    "argv_digest",
                    "snapshot_digest",
                    "receipt_digest",
                    "completion_proof_json",
                    "completion_proof_digest",
                }
            ),
            RecoveryActionClass.PRIVATE_REF: frozenset(
                {
                    "idempotency_key",
                    "repository_id",
                    "repository_instance_digest",
                    "ref_name",
                    "registration_digest",
                    "target_safety_digest",
                    "old_oid",
                    "prepared_oid",
                    "current_oid",
                    "completion_proof_json",
                    "completion_proof_digest",
                }
            ),
            RecoveryActionClass.TARGET_CAS: frozenset(
                {
                    "idempotency_key",
                    "repository_id",
                    "repository_instance_digest",
                    "ref_name",
                    "registration_digest",
                    "target_safety_digest",
                    "old_oid",
                    "prepared_oid",
                    "current_oid",
                    "completion_proof_json",
                    "completion_proof_digest",
                }
            ),
            RecoveryActionClass.TARGET_RESERVATION: frozenset(
                {
                    "idempotency_key",
                    "registration_identity",
                    "reservation_operation",
                    "admin_binding_digest",
                    "path_identity",
                    "gitfile_digest",
                    "completion_proof_json",
                    "completion_proof_digest",
                }
            ),
            RecoveryActionClass.GRANTED_ACTION: frozenset(
                {
                    "idempotency_key",
                    "pending_action_id",
                    "grant_id",
                    "expected_prestate_digest",
                    "action_binding_digest",
                    "completion_proof_json",
                    "completion_proof_digest",
                }
            ),
        }
        if self.recovery_generation < 1:
            raise ValueError("RECOVERY_GENERATION_INVALID")
        completed_state = (
            (self.kind is RecoveryActionClass.MODEL and self.state == "EXACT_COMPLETION")
            or (self.kind is RecoveryActionClass.READ_SEARCH and self.state == "EXACT_SNAPSHOT")
            or (self.kind is RecoveryActionClass.CHECK and self.state == "EXACT_RECEIPT")
            or (
                self.kind is RecoveryActionClass.TARGET_RESERVATION
                and (
                    (self.reservation_operation == "CLEANUP" and self.state == "BOTH_ABSENT")
                    or (
                        self.reservation_operation == "CREATE"
                        and self.state == "BOTH_PRESENT_LOCKED"
                    )
                )
            )
            or (
                self.kind
                in {
                    RecoveryActionClass.PATCH,
                    RecoveryActionClass.PRIVATE_REF,
                    RecoveryActionClass.TARGET_CAS,
                    RecoveryActionClass.GRANTED_ACTION,
                }
                and self.state == "EXACT_POST"
            )
        )
        if completed_state and (
            self.run_id is None
            or self.settled_sequence is None
            or self.settled_sequence < 1
            or self.applicable_revision_digests is None
            or (
                self.kind is RecoveryActionClass.READ_SEARCH
                and (self.bounded_result_json is None or self.bounded_result_digest is None)
            )
            or (
                self.kind is not RecoveryActionClass.READ_SEARCH
                and (self.completion_proof_json is None or self.completion_proof_digest is None)
            )
        ):
            raise ValueError("COMPLETION_DURABLE_BINDING_REQUIRED")
        common = {
            "kind",
            "intent_id",
            "recovery_generation",
            "source_payload_digest",
            "state",
            "observation_digest",
            "run_id",
            "settled_sequence",
            "applicable_revision_digests",
        }
        for field_name in type(self).model_fields:
            if (
                field_name not in common
                and field_name not in allowed[self.kind]
                and getattr(self, field_name) is not None
            ):
                raise ValueError(f"{self.kind}_OBSERVATION_FIELD_FORBIDDEN:{field_name}")
        for field_name in required[self.kind]:
            if getattr(self, field_name) is None:
                raise ValueError(f"{self.kind}_OBSERVATION_FIELD_REQUIRED:{field_name}")
        retry_state = self.state in {"EXACT_PRE", "BOTH_PRESENT_UNLOCKED"}
        retry_state = retry_state or (
            self.kind is RecoveryActionClass.TARGET_RESERVATION
            and (
                (self.reservation_operation == "CREATE" and self.state == "BOTH_ABSENT")
                or (
                    self.reservation_operation == "CLEANUP"
                    and self.state
                    in {"BOTH_PRESENT_LOCKED", "BOTH_PRESENT_UNLOCKED", "ADMIN_ONLY", "PATH_ONLY"}
                )
            )
        )
        if retry_state and self.kind is not RecoveryActionClass.MODEL and not self.idempotency_key:
            raise ValueError("RETRY_IDEMPOTENCY_KEY_REQUIRED")
        if (
            self.kind is RecoveryActionClass.MODEL
            and self.state == "EXACT_COMPLETION"
            and (
                self.normalized_completion_digest is None
                or self.provider_response_id is None
                or self.returned_model_id is None
                or self.schema_digest is None
                or self.usage_json is None
                or self.normalized_completion_json is None
            )
        ):
            raise ValueError("MODEL_COMPLETION_PROVIDER_EVIDENCE_REQUIRED")
        if self.kind is RecoveryActionClass.MODEL and self.state == "EXACT_COMPLETION":
            assert self.normalized_completion_json is not None
            assert self.normalized_completion_digest is not None
            if self.normalized_completion_digest != sha256_digest(self.normalized_completion_json):
                raise ValueError("MODEL_COMPLETION_DIGEST_MISMATCH")
        if (
            self.kind is RecoveryActionClass.MODEL
            and self.state == "RETURNED_MODEL_MISMATCH"
            and self.reservation_charge != "FULL"
        ):
            raise ValueError("MODEL_MISMATCH_FULL_RESERVATION_REQUIRED")
        if self.kind is RecoveryActionClass.READ_SEARCH:
            if self.state == "EXACT_SNAPSHOT":
                if self.bounded_result_json is None or self.bounded_result_digest is None:
                    raise ValueError("READ_RESULT_REQUIRED")
                try:
                    parsed = json.loads(self.bounded_result_json)
                except json.JSONDecodeError as exc:
                    raise ValueError("READ_RESULT_NOT_JSON") from exc
                canonical_result = json.dumps(
                    parsed, sort_keys=True, separators=(",", ":"), ensure_ascii=True
                )
                if canonical_result != self.bounded_result_json:
                    raise ValueError("READ_RESULT_NOT_CANONICAL")
                if len(self.bounded_result_json.encode("utf-8")) > 131_072:
                    raise ValueError("READ_RESULT_TOO_LARGE")
                forbidden_keys = {
                    "credential",
                    "credentials",
                    "nonce",
                    "raw_transcript",
                    "provider_transcript",
                    "secret",
                    "token",
                }

                def contains_forbidden_key(value: object) -> bool:
                    if isinstance(value, dict):
                        return any(
                            isinstance(key, str)
                            and key.lower() in forbidden_keys
                            or contains_forbidden_key(item)
                            for key, item in value.items()
                        )
                    if isinstance(value, list):
                        return any(contains_forbidden_key(item) for item in value)
                    return False

                if contains_forbidden_key(parsed):
                    raise ValueError("READ_RESULT_NOT_SANITIZED")
                if self.bounded_result_digest != sha256_digest(self.bounded_result_json):
                    raise ValueError("READ_RESULT_DIGEST_MISMATCH")
            elif self.bounded_result_json is not None or self.bounded_result_digest is not None:
                raise ValueError("READ_RESULT_FORBIDDEN")
        if (
            self.kind is RecoveryActionClass.CHECK
            and self.state == "EXACT_RECEIPT"
            and self.receipt_digest is None
        ):
            raise ValueError("CHECK_RECEIPT_REQUIRED")
        if self.kind in {RecoveryActionClass.PRIVATE_REF, RecoveryActionClass.TARGET_CAS}:
            if self.state == "EXACT_PRE" and self.current_oid != self.old_oid:
                raise ValueError("REF_EXACT_PRE_OID_MISMATCH")
            if self.state == "EXACT_POST" and self.current_oid != self.prepared_oid:
                raise ValueError("REF_EXACT_POST_OID_MISMATCH")
        if self.kind is RecoveryActionClass.TARGET_RESERVATION:
            if self.reservation_operation == "CREATE" and self.state in {"PATH_ONLY", "ADMIN_ONLY"}:
                raise ValueError("RESERVATION_CREATE_PARTIAL_STATE")
            if (
                self.state in {"PATH_ONLY", "ADMIN_ONLY", "MIXED"}
                and not self.registration_identity
            ):
                raise ValueError("RESERVATION_IDENTITY_REQUIRED")
        if (
            self.kind is not RecoveryActionClass.READ_SEARCH
            and not completed_state
            and (
                self.completion_proof_json is not None
                or self.completion_proof_digest is not None
                or self.normalized_completion_json is not None
            )
        ):
            raise ValueError("COMPLETION_PROOF_FORBIDDEN")
        if completed_state and self.kind is not RecoveryActionClass.READ_SEARCH:
            assert self.completion_proof_json is not None
            _validate_bounded_result(self.completion_proof_json, self.completion_proof_digest)
            proof_fields = {
                RecoveryActionClass.MODEL: (),
                RecoveryActionClass.PATCH: (
                    "expected_pre_tree_digest",
                    "observed_post_tree_digest",
                ),
                RecoveryActionClass.CHECK: (
                    "check_id",
                    "argv_digest",
                    "snapshot_digest",
                    "receipt_digest",
                ),
                RecoveryActionClass.PRIVATE_REF: (
                    "repository_id",
                    "repository_instance_digest",
                    "ref_name",
                    "registration_digest",
                    "target_safety_digest",
                    "old_oid",
                    "prepared_oid",
                    "current_oid",
                ),
                RecoveryActionClass.TARGET_CAS: (
                    "repository_id",
                    "repository_instance_digest",
                    "ref_name",
                    "registration_digest",
                    "target_safety_digest",
                    "old_oid",
                    "prepared_oid",
                    "current_oid",
                ),
                RecoveryActionClass.TARGET_RESERVATION: (
                    "registration_identity",
                    "reservation_operation",
                    "admin_binding_digest",
                    "path_identity",
                    "gitfile_digest",
                ),
                RecoveryActionClass.GRANTED_ACTION: (
                    "pending_action_id",
                    "grant_id",
                    "expected_prestate_digest",
                    "action_binding_digest",
                ),
                RecoveryActionClass.READ_SEARCH: (),
            }
            if self.kind is RecoveryActionClass.MODEL:
                assert self.normalized_completion_json is not None
                if self.completion_proof_json != self.normalized_completion_json:
                    raise ValueError("MODEL_COMPLETION_OUTPUT_MISMATCH")
            elif self.kind in {RecoveryActionClass.PATCH, RecoveryActionClass.CHECK}:
                from apexcrew.domain.tools import ToolResult

                try:
                    tool_result = ToolResult.model_validate_json(self.completion_proof_json)
                except (TypeError, ValueError) as exc:
                    raise ValueError("TOOL_COMPLETION_PROOF_INVALID") from exc
                if tool_result.run_id != self.run_id or tool_result.intent_id != self.intent_id:
                    raise ValueError("TOOL_COMPLETION_PROOF_BINDING_MISMATCH")
                expected_codes = {
                    RecoveryActionClass.PATCH: {"PATCH_APPLIED"},
                    RecoveryActionClass.CHECK: {"CHECK_PASSED", "CHECK_FAILED"},
                }
                if tool_result.code not in expected_codes[self.kind]:
                    raise ValueError("TOOL_COMPLETION_PROOF_CODE_INVALID")
                if self.kind is RecoveryActionClass.PATCH:
                    if (
                        tool_result.bounded_payload.get("snapshot_digest") != self.snapshot_digest
                        or tool_result.bounded_payload.get("post_tree_digest")
                        != self.observed_post_tree_digest
                        or tool_result.bounded_payload.get("pre_tree_digest")
                        != self.expected_pre_tree_digest
                    ):
                        raise ValueError("PATCH_COMPLETION_PROOF_BINDING_MISMATCH")
                elif (
                    tool_result.bounded_payload.get("check_id") != self.check_id
                    or tool_result.bounded_payload.get("argv_digest") != self.argv_digest
                    or tool_result.bounded_payload.get("snapshot_digest") != self.snapshot_digest
                    or tool_result.bounded_payload.get("receipt_digest") != self.receipt_digest
                    or tool_result.content_digest != self.receipt_digest
                ):
                    raise ValueError("CHECK_COMPLETION_PROOF_BINDING_MISMATCH")
            else:
                proof = {"state": self.state}
                proof.update({name: getattr(self, name) for name in proof_fields[self.kind]})
                if self.completion_proof_json != canonical_json(proof):
                    raise ValueError("COMPLETION_PROOF_NOT_BOUND_TO_ACTION")
        payload = self.model_dump(mode="json", exclude={"observation_digest"}, exclude_none=True)
        expected = sha256_digest(canonical_json(payload))
        if self.observation_digest != expected:
            raise ValueError("OBSERVATION_DIGEST_MISMATCH")
        return self


@dataclass(frozen=True, slots=True)
class RecoveryDecision:
    kind: RecoveryDecisionKind
    action_class: RecoveryActionClass
    reason: str
    bounded_result_json: str | None = None
    full_reservation_required: bool = False
    result_digest: Sha256DigestText | None = None
    successor: str | None = None
    prestate_digest: Sha256DigestText | None = None
    idempotency_key: str | None = None
    effect_result: EffectResult | None = None
    settled_sequence: AuditSequence | None = None
    applicable_revision_digests: ApplicableRevisionDigests | None = None

    def __post_init__(self) -> None:
        if self.kind is RecoveryDecisionKind.COMPLETED and (
            self.result_digest is None
            or self.effect_result is None
            or self.bounded_result_json is None
            or self.settled_sequence is None
            or self.applicable_revision_digests is None
        ):
            raise ValueError("COMPLETED_DURABLE_RESULT_REQUIRED")
        if self.kind is RecoveryDecisionKind.COMPLETED:
            assert self.effect_result is not None
            if (
                self.effect_result.outcome != "COMPLETED"
                or self.effect_result.result_digest != self.result_digest
                or self.effect_result.bounded_result_json != self.bounded_result_json
                or self.effect_result.settled_sequence != self.settled_sequence
            ):
                raise ValueError("COMPLETED_RESULT_BINDING_MISMATCH")
        if self.kind is RecoveryDecisionKind.RETRY_SAME_INTENT and (
            self.prestate_digest is None or not self.idempotency_key
        ):
            raise ValueError("RETRY_PROOF_REQUIRED")
        if self.kind is RecoveryDecisionKind.ABANDONED and not self.successor:
            raise ValueError("ABANDON_SUCCESSOR_REQUIRED")
        if self.kind is RecoveryDecisionKind.ABANDONED and (
            self.effect_result is not None
            or self.result_digest is not None
            or self.bounded_result_json is not None
            or self.settled_sequence is not None
            or self.applicable_revision_digests is not None
        ):
            raise ValueError("ABANDONED_CANNOT_CARRY_RESULT")
        if self.kind in {
            RecoveryDecisionKind.STALE,
            RecoveryDecisionKind.CONFLICT,
            RecoveryDecisionKind.INDETERMINATE,
        } and (
            self.result_digest is not None
            or self.bounded_result_json is not None
            or self.effect_result is not None
            or self.settled_sequence is not None
            or self.applicable_revision_digests is not None
        ):
            raise ValueError("NON_RESULT_DECISION_CARRIES_RESULT")


def observation_set_digest(observations: tuple[RecoveryObservation, ...]) -> Sha256DigestText:
    return sha256_digest(
        canonical_json(
            {"observations": [observation.model_dump(mode="json") for observation in observations]}
        )
    )


@dataclass(frozen=True, slots=True)
class ApplyResolutionRequest:
    run_id: RunId
    selection: ResolutionSelection
    permit_generation: int
    owner_id: RuntimeOwnerId
    expected_sequence: AuditSequence
    intent_ids: tuple[IntentId, ...]
    payload_digests: tuple[Sha256DigestText, ...]
    recovery_generations: tuple[int, ...]
    observations: tuple[RecoveryObservation, ...]
    observation_set_digest: Sha256DigestText


def _exact_prestate_digest(observation: RecoveryObservation) -> Sha256DigestText:
    if observation.kind is RecoveryActionClass.PATCH:
        assert observation.expected_pre_tree_digest is not None
        return observation.expected_pre_tree_digest
    if observation.kind is RecoveryActionClass.CHECK:
        assert observation.snapshot_digest is not None
        return observation.snapshot_digest
    if observation.kind is RecoveryActionClass.GRANTED_ACTION:
        assert observation.expected_prestate_digest is not None
        return observation.expected_prestate_digest
    if observation.kind in {RecoveryActionClass.PRIVATE_REF, RecoveryActionClass.TARGET_CAS}:
        return sha256_digest(
            canonical_json(
                {
                    "current_oid": observation.current_oid,
                    "old_oid": observation.old_oid,
                    "ref_name": observation.ref_name,
                    "registration_digest": observation.registration_digest,
                    "repository_id": observation.repository_id,
                    "repository_instance_digest": observation.repository_instance_digest,
                    "target_safety_digest": observation.target_safety_digest,
                }
            )
        )
    return observation.observation_digest


def _completed_decision(
    observation: RecoveryObservation,
    reason: str,
    result_digest: Sha256DigestText | None = None,
    bounded_result_json: str | None = None,
) -> RecoveryDecision:
    if observation.kind is RecoveryActionClass.READ_SEARCH:
        result_json = observation.bounded_result_json
        result_digest_value = observation.bounded_result_digest
    else:
        result_json = observation.completion_proof_json
        result_digest_value = observation.completion_proof_digest
    assert result_json is not None
    assert result_digest_value is not None
    if result_digest is not None and result_digest != result_digest_value:
        raise ValueError("COMPLETION_RESULT_DIGEST_MISMATCH")
    result_digest = result_digest_value
    bounded_result_json = result_json
    assert observation.run_id is not None
    assert observation.settled_sequence is not None
    assert observation.applicable_revision_digests is not None
    effect_result = EffectResult(
        intent_id=observation.intent_id,
        run_id=observation.run_id,
        outcome="COMPLETED",
        result_class=reason,
        result_digest=result_digest,
        bounded_result_json=bounded_result_json,
        settled_sequence=observation.settled_sequence,
        snapshot_digest=observation.snapshot_digest,
    )
    return RecoveryDecision(
        RecoveryDecisionKind.COMPLETED,
        observation.kind,
        reason,
        bounded_result_json=bounded_result_json,
        result_digest=result_digest,
        effect_result=effect_result,
        settled_sequence=observation.settled_sequence,
        applicable_revision_digests=observation.applicable_revision_digests,
    )


def abandon_observation(observation: RecoveryObservation, successor: str) -> RecoveryDecision:
    if observation.kind is RecoveryActionClass.MODEL:
        raise ValueError("MODEL_ABANDON_REQUIRES_OWNER_FAILURE")
    allowed_states = {
        RecoveryActionClass.READ_SEARCH: {"EXACT_SNAPSHOT", "STALE"},
        RecoveryActionClass.PATCH: {"EXACT_PRE"},
        RecoveryActionClass.CHECK: {"EXACT_PRE", "STALE"},
        RecoveryActionClass.PRIVATE_REF: {"EXACT_PRE"},
        RecoveryActionClass.TARGET_CAS: {"EXACT_PRE"},
        RecoveryActionClass.TARGET_RESERVATION: {"BOTH_ABSENT"},
        RecoveryActionClass.GRANTED_ACTION: {"EXACT_PRE"},
    }
    if observation.state not in allowed_states[observation.kind]:
        raise ValueError("ABANDON_EFFECT_NOT_PROVEN")
    allowed_successors = {
        RecoveryActionClass.READ_SEARCH: {"PAUSED", "PAUSED/READ_ABANDONED"},
        RecoveryActionClass.PATCH: {"PAUSED", "PAUSED/PATCH_ABANDONED"},
        RecoveryActionClass.CHECK: {"PAUSED", "PAUSED/CHECK_ABANDONED"},
        RecoveryActionClass.PRIVATE_REF: {"READY", "PAUSED/PRIVATE_REF_INIT_ABANDONED"},
        RecoveryActionClass.TARGET_CAS: {"READY_FOR_APPROVAL"},
        RecoveryActionClass.TARGET_RESERVATION: {"DRAFT", "PAUSED"},
        RecoveryActionClass.GRANTED_ACTION: {"PAUSED"},
    }
    if successor not in allowed_successors[observation.kind]:
        raise ValueError("ABANDON_SUCCESSOR_NOT_CLASS_SPECIFIC")
    return RecoveryDecision(
        RecoveryDecisionKind.ABANDONED,
        observation.kind,
        "NO_AUTHORITATIVE_EFFECT",
        successor=successor,
    )


def abandon_successor_for(observation: RecoveryObservation) -> str:
    return {
        RecoveryActionClass.READ_SEARCH: "PAUSED/READ_ABANDONED",
        RecoveryActionClass.PATCH: "PAUSED/PATCH_ABANDONED",
        RecoveryActionClass.CHECK: "PAUSED/CHECK_ABANDONED",
        RecoveryActionClass.PRIVATE_REF: "PAUSED/PRIVATE_REF_INIT_ABANDONED",
        RecoveryActionClass.TARGET_CAS: "READY_FOR_APPROVAL",
        RecoveryActionClass.TARGET_RESERVATION: "PAUSED",
        RecoveryActionClass.GRANTED_ACTION: "PAUSED",
    }.get(observation.kind, "PAUSED")


def recover_observation(observation: RecoveryObservation) -> RecoveryDecision:
    state = observation.state
    if observation.kind is RecoveryActionClass.MODEL:
        if state == "EXACT_COMPLETION" and observation.normalized_completion_digest is not None:
            return _completed_decision(observation, "EXACT_NORMALIZED_COMPLETION")
        return RecoveryDecision(
            RecoveryDecisionKind.INDETERMINATE,
            observation.kind,
            state,
            full_reservation_required=observation.reservation_charge == "FULL"
            or state == "RETURNED_MODEL_MISMATCH",
        )
    if observation.kind is RecoveryActionClass.READ_SEARCH:
        if state == "EXACT_SNAPSHOT" and observation.bounded_result_json is not None:
            assert observation.bounded_result_digest is not None
            return _completed_decision(observation, "EXACT_SNAPSHOT")
        if state == "STALE":
            return RecoveryDecision(RecoveryDecisionKind.STALE, observation.kind, state)
        return RecoveryDecision(RecoveryDecisionKind.INDETERMINATE, observation.kind, state)
    if observation.kind is RecoveryActionClass.CHECK:
        if state == "EXACT_RECEIPT" and observation.receipt_digest is not None:
            return _completed_decision(observation, state)
        if state == "EXACT_PRE":
            return RecoveryDecision(
                RecoveryDecisionKind.RETRY_SAME_INTENT,
                observation.kind,
                state,
                prestate_digest=_exact_prestate_digest(observation),
                idempotency_key=observation.idempotency_key,
            )
        return RecoveryDecision(RecoveryDecisionKind.INDETERMINATE, observation.kind, state)
    if observation.kind is RecoveryActionClass.TARGET_RESERVATION:
        if state == "BOTH_ABSENT":
            if observation.reservation_operation == "CREATE":
                return RecoveryDecision(
                    RecoveryDecisionKind.RETRY_SAME_INTENT,
                    observation.kind,
                    "EXACT_PRE",
                    prestate_digest=_exact_prestate_digest(observation),
                    idempotency_key=observation.idempotency_key,
                )
            return _completed_decision(observation, state)
        if observation.reservation_operation == "CREATE" and state == "BOTH_PRESENT_LOCKED":
            return _completed_decision(observation, state)
        if observation.reservation_operation == "CLEANUP" and state in {
            "BOTH_PRESENT_LOCKED",
            "BOTH_PRESENT_UNLOCKED",
            "ADMIN_ONLY",
            "PATH_ONLY",
        }:
            return RecoveryDecision(
                RecoveryDecisionKind.RETRY_SAME_INTENT,
                observation.kind,
                "EXACT_CLEANUP_PRE",
                prestate_digest=_exact_prestate_digest(observation),
                idempotency_key=observation.idempotency_key,
            )
        if state == "BOTH_PRESENT_UNLOCKED":
            return RecoveryDecision(
                RecoveryDecisionKind.RETRY_SAME_INTENT,
                observation.kind,
                "EXACT_UNLOCKED",
                prestate_digest=_exact_prestate_digest(observation),
                idempotency_key=observation.idempotency_key,
            )
        if state == "MIXED":
            return RecoveryDecision(RecoveryDecisionKind.CONFLICT, observation.kind, state)
        return RecoveryDecision(RecoveryDecisionKind.INDETERMINATE, observation.kind, state)
    if observation.kind in {RecoveryActionClass.PRIVATE_REF, RecoveryActionClass.TARGET_CAS}:
        if state == "EXACT_POST":
            return _completed_decision(observation, state)
        if state == "EXACT_PRE":
            return RecoveryDecision(
                RecoveryDecisionKind.RETRY_SAME_INTENT,
                observation.kind,
                state,
                prestate_digest=_exact_prestate_digest(observation),
                idempotency_key=observation.idempotency_key,
            )
        if state == "TARGET_UNSAFE":
            return RecoveryDecision(RecoveryDecisionKind.STALE, observation.kind, state)
        if state == "THIRD_STATE":
            return RecoveryDecision(RecoveryDecisionKind.CONFLICT, observation.kind, state)
        return RecoveryDecision(RecoveryDecisionKind.INDETERMINATE, observation.kind, state)
    if observation.kind in {
        RecoveryActionClass.PATCH,
        RecoveryActionClass.GRANTED_ACTION,
    }:
        if state == "EXACT_POST":
            return _completed_decision(observation, state)
        if state == "EXACT_PRE":
            return RecoveryDecision(
                RecoveryDecisionKind.RETRY_SAME_INTENT,
                observation.kind,
                state,
                prestate_digest=_exact_prestate_digest(observation),
                idempotency_key=observation.idempotency_key,
            )
        if state == "THIRD_STATE":
            return RecoveryDecision(RecoveryDecisionKind.CONFLICT, observation.kind, state)
    return RecoveryDecision(RecoveryDecisionKind.INDETERMINATE, observation.kind, state)


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
        unresolved = self._journal.unresolved_intent_set(run_id)
        if unresolved is None:
            return RecoveryOutcome.empty()
        members = tuple(
            UnresolvedIntentMember(
                intent_id=IntentId(member.intent_id),
                recovery_generation=member.recovery_generation,
                intent_digest=member.intent_digest,
            )
            for member in unresolved.member_bindings
        )
        set_digest = UnresolvedSetDigest(
            str(
                unresolved_set_digest_for_members(
                    tuple(
                        UnresolvedIntentBinding(
                            intent_id=str(member.intent_id),
                            recovery_generation=member.recovery_generation,
                            intent_digest=member.intent_digest,
                        )
                        for member in members
                    )
                )
            )
        )
        return RecoveryOutcome(
            recoveries=tuple(
                IntentRecovery(
                    intent_id=member.intent_id,
                    recovery_generation=member.recovery_generation,
                    disposition=RecoveryDisposition.INDETERMINATE,
                    reason="AUTHORITATIVE_RECOVERY_REQUIRED",
                )
                for member in members
            ),
            unresolved_members=members,
            unresolved_set_digest=set_digest,
        )
