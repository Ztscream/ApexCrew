from __future__ import annotations

import base64
import hmac
import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from hashlib import sha256
from typing import TYPE_CHECKING, Literal, Protocol

from apexcrew.domain.actions import ActionEnvelope, RiskyAction
from apexcrew.domain.commands import ApplicableRevisionDigests, ConfirmationCode, GrantPayload
from apexcrew.domain.effects import EffectIntent, StateConflict, canonical_json, sha256_digest
from apexcrew.domain.limits import V01_MECHANISM_LIMITS
from apexcrew.domain.model import (
    LogicalModelTurn,
    ModelBudgetAmounts,
    ModelCounters,
    ModelRequest,
    ModelRequestIntent,
)
from apexcrew.domain.plan import CanonicalPath, GlobPattern, TaskContract
from apexcrew.domain.policy import ActionPolicy, SecretPathPolicy
from apexcrew.domain.revisions import BudgetRevisionDocument, Sha256DigestText
from apexcrew.domain.types import (
    AttemptId,
    AuditSequence,
    GrantId,
    IntentId,
    PendingActionId,
    RequestId,
    RevisionDigest,
    RunId,
    TaskId,
)

if TYPE_CHECKING:
    from apexcrew.domain.tools import ActionPreState


class AuthorityDenied(ValueError):
    """Closed pre-transaction rejection of untrusted authority input."""


class ActionClass(StrEnum):
    ORDINARY = "ORDINARY"
    DECLARED_CHECK = "DECLARED_CHECK"


@dataclass(frozen=True, slots=True)
class ActionDeadline:
    run_id: RunId
    intent_id: IntentId
    budget_digest: RevisionDigest
    applicable_revision_digests: ApplicableRevisionDigests
    action_class: ActionClass
    started_at: datetime
    expires_at: datetime
    recorded_sequence: AuditSequence
    check_id: str | None = None
    snapshot_digest: str | None = None

    def __post_init__(self) -> None:
        timeout_seconds = (
            V01_MECHANISM_LIMITS.check_timeout_seconds
            if self.action_class == ActionClass.DECLARED_CHECK
            else V01_MECHANISM_LIMITS.ordinary_action_timeout_seconds
        )
        if (
            self.started_at.tzinfo is None
            or self.expires_at.tzinfo is None
            or self.expires_at != self.started_at + timedelta(seconds=timeout_seconds)
        ):
            raise ValueError("ACTION_DEADLINE_INVALID")
        check_binding_present = self.check_id is not None and self.snapshot_digest is not None
        if (self.action_class == ActionClass.DECLARED_CHECK) != check_binding_present:
            raise ValueError("ACTION_DEADLINE_CHECK_BINDING_INVALID")


TimeoutOutcome = Literal["INDETERMINATE", "INFRASTRUCTURE_UNCERTAINTY"]


@dataclass(frozen=True, slots=True)
class TimeoutDecision:
    outcome: TimeoutOutcome
    semantic_result: None
    receipt: None
    retry_scope: tuple[str, str] | None
    retry_allowed: bool
    full_reservation_charged: bool

    def __post_init__(self) -> None:
        if (
            self.outcome not in {"INDETERMINATE", "INFRASTRUCTURE_UNCERTAINTY"}
            or self.semantic_result is not None
            or self.receipt is not None
            or not isinstance(self.retry_allowed, bool)
            or not isinstance(self.full_reservation_charged, bool)
        ):
            raise ValueError("TIMEOUT_DECISION_BINDING_INVALID")
        ordinary = self.outcome == "INDETERMINATE"
        if ordinary != (
            self.retry_scope is None and not self.retry_allowed and self.full_reservation_charged
        ):
            raise ValueError("TIMEOUT_DECISION_BINDING_INVALID")
        if not ordinary and (self.retry_scope is None or self.full_reservation_charged):
            raise ValueError("TIMEOUT_DECISION_BINDING_INVALID")


def timeout_decision_to_json(decision: TimeoutDecision) -> str:
    return json.dumps(
        {
            "full_reservation_charged": decision.full_reservation_charged,
            "outcome": decision.outcome,
            "retry_allowed": decision.retry_allowed,
            "retry_scope": decision.retry_scope,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def timeout_decision_from_json(value: str) -> TimeoutDecision:
    try:
        data = json.loads(value)
        outcome = data["outcome"]
        scope = data["retry_scope"]
        if outcome not in {"INDETERMINATE", "INFRASTRUCTURE_UNCERTAINTY"}:
            raise ValueError("TIMEOUT_DECISION_STORAGE_INVALID")
        if scope is not None and (
            not isinstance(scope, list)
            or len(scope) != 2
            or not all(isinstance(item, str) for item in scope)
        ):
            raise ValueError("TIMEOUT_DECISION_STORAGE_INVALID")
        decision = TimeoutDecision(
            outcome=outcome,
            semantic_result=None,
            receipt=None,
            retry_scope=None if scope is None else (scope[0], scope[1]),
            retry_allowed=data["retry_allowed"],
            full_reservation_charged=data["full_reservation_charged"],
        )
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise ValueError("TIMEOUT_DECISION_STORAGE_INVALID") from error
    if timeout_decision_to_json(decision) != value:
        raise ValueError("TIMEOUT_DECISION_STORAGE_INVALID")
    return decision


def action_deadline_binding(
    intent: EffectIntent,
) -> tuple[ActionClass, str | None, str | None]:
    if intent.kind != "check":
        return ActionClass.ORDINARY, None, None
    try:
        payload = json.loads(intent.normalized_payload_json)
        action = payload["action"]
        check_id = action["check_id"]
        snapshot_digest = payload["snapshot_digest"]
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise AuthorityDenied("CHECK_TIMEOUT_BINDING_REQUIRED") from error
    if (
        not isinstance(action, dict)
        or action.get("kind") != "check"
        or not isinstance(check_id, str)
        or not check_id
        or not isinstance(snapshot_digest, str)
        or not snapshot_digest
    ):
        raise AuthorityDenied("CHECK_TIMEOUT_BINDING_REQUIRED")
    return ActionClass.DECLARED_CHECK, check_id, snapshot_digest


class GlobalBudgetMetric(StrEnum):
    ACTIVE_RUN_SECONDS = "ACTIVE_RUN_SECONDS"
    TASKS = "TASKS"
    PLANNING_REQUESTS = "PLANNING_REQUESTS"
    MODEL_CALLS = "MODEL_CALLS"
    INPUT_TOKENS = "INPUT_TOKENS"
    OUTPUT_TOKENS = "OUTPUT_TOKENS"
    COST_RESERVE_USD = "COST_RESERVE_USD"
    CONCURRENT_WORKERS = "CONCURRENT_WORKERS"


class DispatchCloseCause(StrEnum):
    MANUAL_PAUSE = "MANUAL_PAUSE"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    UNKNOWN_CHANGE = "UNKNOWN_CHANGE"
    IMMUTABLE_PLAN_INSUFFICIENCY = "IMMUTABLE_PLAN_INSUFFICIENCY"
    REVISION_REPLACEMENT = "REVISION_REPLACEMENT"
    RUNTIME_FAULT = "RUNTIME_FAULT"
    TASK_PAUSED = "TASK_PAUSED"


DECIMAL_GLOBAL_METRICS = frozenset(
    {GlobalBudgetMetric.ACTIVE_RUN_SECONDS, GlobalBudgetMetric.COST_RESERVE_USD}
)


@dataclass(frozen=True, slots=True)
class BudgetWarning:
    run_id: RunId
    budget_digest: RevisionDigest
    metric: GlobalBudgetMetric
    used: int | Decimal
    ceiling: int | Decimal
    threshold_percent: int


@dataclass(frozen=True, slots=True)
class BudgetCeilingExhaustion:
    metric: GlobalBudgetMetric
    used: int | Decimal
    ceiling: int | Decimal
    budget_digest: RevisionDigest


@dataclass(frozen=True, slots=True)
class GlobalUsageSnapshot:
    active_run_seconds: Decimal = Decimal(0)
    tasks: int = 0
    planning_requests: int = 0
    model_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_reserve_usd: Decimal = Decimal(0)
    concurrent_workers: int = 0

    @classmethod
    def zero(cls) -> GlobalUsageSnapshot:
        return cls()

    def amount_for(self, metric: GlobalBudgetMetric) -> int | Decimal:
        match metric:
            case GlobalBudgetMetric.ACTIVE_RUN_SECONDS:
                return self.active_run_seconds
            case GlobalBudgetMetric.TASKS:
                return self.tasks
            case GlobalBudgetMetric.PLANNING_REQUESTS:
                return self.planning_requests
            case GlobalBudgetMetric.MODEL_CALLS:
                return self.model_calls
            case GlobalBudgetMetric.INPUT_TOKENS:
                return self.input_tokens
            case GlobalBudgetMetric.OUTPUT_TOKENS:
                return self.output_tokens
            case GlobalBudgetMetric.COST_RESERVE_USD:
                return self.cost_reserve_usd
            case GlobalBudgetMetric.CONCURRENT_WORKERS:
                return self.concurrent_workers
        raise AuthorityDenied("UNKNOWN_GLOBAL_BUDGET_METRIC")


@dataclass(frozen=True, slots=True)
class AtomicAction:
    run_id: RunId
    action_id: str
    budget_digest: RevisionDigest
    state: Literal["IN_FLIGHT", "SETTLED"]
    opened_sequence: AuditSequence


@dataclass(frozen=True, slots=True)
class BudgetSettlement:
    run_id: RunId
    metric: GlobalBudgetMetric
    absolute_used: int | Decimal
    ceiling: int | Decimal
    action_state: Literal["SETTLED"] | None
    pause_after_barrier: bool
    pause_reason: str | None
    resulting_sequence: AuditSequence


def normalize_global_budget_metric(
    value: GlobalBudgetMetric | str,
) -> GlobalBudgetMetric:
    try:
        return GlobalBudgetMetric(value)
    except (TypeError, ValueError) as error:
        raise AuthorityDenied("UNKNOWN_GLOBAL_BUDGET_METRIC") from error


def crossed_threshold(
    previous: int | Decimal,
    current: int | Decimal,
    ceiling: int | Decimal,
    percent: int,
) -> bool:
    return previous * 100 < ceiling * percent <= current * 100


def global_ceiling_for(
    budget: BudgetRevisionDocument,
    value: GlobalBudgetMetric | str,
) -> int | Decimal:
    metric = normalize_global_budget_metric(value)
    match metric:
        case GlobalBudgetMetric.ACTIVE_RUN_SECONDS:
            return budget.active_run_seconds_ceiling
        case GlobalBudgetMetric.TASKS:
            return budget.task_ceiling
        case GlobalBudgetMetric.PLANNING_REQUESTS:
            return budget.planning_request_ceiling
        case GlobalBudgetMetric.MODEL_CALLS:
            return budget.model_call_ceiling
        case GlobalBudgetMetric.INPUT_TOKENS:
            return budget.input_token_ceiling
        case GlobalBudgetMetric.OUTPUT_TOKENS:
            return budget.output_token_ceiling
        case GlobalBudgetMetric.COST_RESERVE_USD:
            return budget.cost_reserve_usd
        case GlobalBudgetMetric.CONCURRENT_WORKERS:
            return budget.concurrent_worker_ceiling
    raise AuthorityDenied("UNKNOWN_GLOBAL_BUDGET_METRIC")


def global_budget_maximum_for(value: GlobalBudgetMetric | str) -> int | Decimal:
    metric = normalize_global_budget_metric(value)
    match metric:
        case GlobalBudgetMetric.ACTIVE_RUN_SECONDS:
            return 28_800
        case GlobalBudgetMetric.TASKS:
            return 12
        case GlobalBudgetMetric.PLANNING_REQUESTS:
            return 8
        case GlobalBudgetMetric.MODEL_CALLS:
            return 240
        case GlobalBudgetMetric.INPUT_TOKENS:
            return 2_000_000
        case GlobalBudgetMetric.OUTPUT_TOKENS:
            return 200_000
        case GlobalBudgetMetric.COST_RESERVE_USD:
            return Decimal(10)
        case GlobalBudgetMetric.CONCURRENT_WORKERS:
            return 3
    raise AuthorityDenied("UNKNOWN_GLOBAL_BUDGET_METRIC")


def global_numeric_from_text(
    metric: GlobalBudgetMetric,
    value: str,
) -> int | Decimal:
    return Decimal(value) if metric in DECIMAL_GLOBAL_METRICS else int(value)


def dispatch_close_causes_to_json(causes: frozenset[DispatchCloseCause]) -> str:
    return json.dumps(sorted(cause.value for cause in causes), separators=(",", ":"))


def dispatch_close_causes_from_json(value: str) -> frozenset[DispatchCloseCause]:
    try:
        raw = json.loads(value)
        if (
            not isinstance(raw, list)
            or any(not isinstance(item, str) for item in raw)
            or raw != sorted(set(raw))
        ):
            raise ValueError
        return frozenset(DispatchCloseCause(item) for item in raw)
    except (json.JSONDecodeError, TypeError, ValueError) as error:
        raise StateConflict("DISPATCH_CLOSE_CAUSES_INVALID") from error


def budget_warning_to_json(warning: BudgetWarning) -> str:
    return json.dumps(
        {
            "budget_digest": warning.budget_digest,
            "ceiling": str(warning.ceiling),
            "metric": warning.metric.value,
            "run_id": warning.run_id,
            "threshold_percent": warning.threshold_percent,
            "used": str(warning.used),
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def budget_warning_from_json(value: str) -> BudgetWarning:
    data = json.loads(value)
    metric = normalize_global_budget_metric(data["metric"])
    return BudgetWarning(
        run_id=RunId(data["run_id"]),
        budget_digest=RevisionDigest(data["budget_digest"]),
        metric=metric,
        used=global_numeric_from_text(metric, data["used"]),
        ceiling=global_numeric_from_text(metric, data["ceiling"]),
        threshold_percent=int(data["threshold_percent"]),
    )


def budget_ceiling_exhaustions_to_json(
    exhaustions: tuple[BudgetCeilingExhaustion, ...],
) -> str:
    return json.dumps(
        [
            {
                "budget_digest": item.budget_digest,
                "ceiling": str(item.ceiling),
                "metric": item.metric.value,
                "used": str(item.used),
            }
            for item in sorted(exhaustions, key=lambda item: item.metric.value)
        ],
        sort_keys=True,
        separators=(",", ":"),
    )


def budget_ceiling_exhaustions_from_json(
    value: str,
) -> tuple[BudgetCeilingExhaustion, ...]:
    data = json.loads(value)
    if not isinstance(data, list):
        raise TypeError("BUDGET_CEILING_EXHAUSTIONS_INVALID")
    parsed: list[BudgetCeilingExhaustion] = []
    for item in data:
        if not isinstance(item, dict) or set(item) != {
            "budget_digest",
            "ceiling",
            "metric",
            "used",
        }:
            raise ValueError("BUDGET_CEILING_EXHAUSTIONS_INVALID")
        metric = normalize_global_budget_metric(item["metric"])
        parsed.append(
            BudgetCeilingExhaustion(
                metric=metric,
                used=global_numeric_from_text(metric, str(item["used"])),
                ceiling=global_numeric_from_text(metric, str(item["ceiling"])),
                budget_digest=RevisionDigest(item["budget_digest"]),
            )
        )
    metrics = tuple(item.metric for item in parsed)
    if len(metrics) != len(set(metrics)) or metrics != tuple(
        sorted(metrics, key=lambda metric: metric.value)
    ):
        raise ValueError("BUDGET_CEILING_EXHAUSTIONS_INVALID")
    return tuple(parsed)


@dataclass(frozen=True, slots=True)
class AttemptAuthority:
    run_id: RunId
    task_id: TaskId
    attempt_id: AttemptId
    generation: int
    base_head: str
    task_contract_digest: str


@dataclass(frozen=True, slots=True)
class TaskBudgetState:
    run_id: RunId
    task_id: TaskId
    allocated_calls: int = 0
    consumed_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: Decimal = Decimal(0)
    tranche_count: int = 0
    bootstrap_tranches: int = 0
    consecutive_no_progress_tranches: int = 0
    attempts: int = 0
    stale_refreshes: int = 0
    manual_resumes: int = 0
    active_tranche_id: str | None = None
    active_tranche_remaining_calls: int = 0


@dataclass(frozen=True, slots=True, order=True)
class MonotonicInstant:
    nanoseconds: int

    def __post_init__(self) -> None:
        if self.nanoseconds < 0:
            raise ValueError("MONOTONIC_INSTANT_NEGATIVE")


class MonotonicClock(Protocol):
    def now(self) -> MonotonicInstant:
        raise NotImplementedError


class UtcClock(Protocol):
    def now(self) -> datetime:
        raise NotImplementedError


class SystemUtcClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class ActiveRunTimeState:
    run_id: RunId
    cumulative_nanoseconds: int
    open_owner_generation: int | None
    opened_at: MonotonicInstant | None
    latest_committed_at: MonotonicInstant | None

    def __post_init__(self) -> None:
        if self.cumulative_nanoseconds < 0:
            raise ValueError("ACTIVE_RUN_TIME_NEGATIVE")
        closed = self.open_owner_generation is None
        if closed != (self.opened_at is None) or closed != (self.latest_committed_at is None):
            raise ValueError("ACTIVE_RUN_TIME_OPEN_BINDING_INCOMPLETE")
        if (
            self.opened_at is not None
            and self.latest_committed_at is not None
            and self.latest_committed_at < self.opened_at
        ):
            raise ValueError("ACTIVE_RUN_TIME_AUDIT_BEFORE_OPEN")

    def observed_nanoseconds(self, now: MonotonicInstant) -> int:
        if self.opened_at is None:
            return self.cumulative_nanoseconds
        if (
            now < self.opened_at
            or self.latest_committed_at is None
            or now < self.latest_committed_at
        ):
            raise ValueError("MONOTONIC_CLOCK_REGRESSED")
        return self.cumulative_nanoseconds + now.nanoseconds - self.opened_at.nanoseconds


@dataclass(frozen=True, slots=True)
class RuntimeAuditStamp:
    sequence: AuditSequence
    owner_generation: int
    monotonic_instant: MonotonicInstant


ReservationOwnerKind = Literal["PLANNING", "WORKER"]
ModelReservationDecision = Literal["RESERVED", "DENY", "PAUSE"]
ModelReservationReason = Literal[
    "AUTHORIZED",
    "STALE_SEQUENCE",
    "OWNER_BINDING_MISMATCH",
    "REQUEST_BINDING_MISMATCH",
    "REVISION_BINDING_MISMATCH",
    "TARGET_BINDING_MISMATCH",
    "CREDENTIAL_UNAVAILABLE",
    "RUN_NOT_DISPATCHABLE",
    "COUNTER_SNAPSHOT_MISMATCH",
    "PRICING_MISSING",
    "DEADLINE_EXPIRED",
    "PLANNING_REQUEST_CEILING",
    "TASK_TRANCHE_EXHAUSTED",
    "MODEL_CALL_CEILING",
    "INPUT_TOKEN_CEILING",
    "OUTPUT_TOKEN_CEILING",
    "COST_RESERVE_CEILING",
]


@dataclass(frozen=True, slots=True)
class ModelReservationRequest:
    run_id: RunId
    owner_kind: ReservationOwnerKind
    task_id: TaskId | None
    attempt_id: AttemptId | None
    tranche_id: str | None
    turn: LogicalModelTurn | None
    model_request: ModelRequest
    provider_attempt_number: int
    target_safety_digest: str
    credential_profile: str | None
    expected_run_counters: ModelCounters
    expected_task_counters: TaskBudgetState | None
    started_at_utc: datetime
    deadline_at_utc: datetime
    expected_sequence: AuditSequence

    def __post_init__(self) -> None:
        if self.model_request.run_id != self.run_id:
            raise ValueError("MODEL_RESERVATION_RUN_MISMATCH")
        if (
            self.model_request.owner_kind != self.owner_kind
            or self.model_request.task_id != self.task_id
            or self.model_request.attempt_id != self.attempt_id
            or self.model_request.tranche_id != self.tranche_id
        ):
            raise ValueError("MODEL_RESERVATION_REQUEST_OWNER_MISMATCH")
        if self.provider_attempt_number < 1:
            raise ValueError("MODEL_ATTEMPT_NUMBER_INVALID")
        if self.turn is None and self.provider_attempt_number != 1:
            raise ValueError("RETRY_REQUIRES_LOGICAL_TURN")
        if self.turn is not None and (
            self.turn.run_id != self.run_id
            or self.turn.request_digest != self.model_request.request_digest
        ):
            raise ValueError("MODEL_TURN_REQUEST_BINDING_MISMATCH")
        planning = self.owner_kind == "PLANNING"
        if planning != (
            self.task_id is None
            and self.attempt_id is None
            and self.tranche_id is None
            and self.expected_task_counters is None
        ):
            raise ValueError("MODEL_RESERVATION_OWNER_MISMATCH")
        if not planning and (
            self.task_id is None
            or self.attempt_id is None
            or self.tranche_id is None
            or self.expected_task_counters is None
            or self.expected_task_counters.run_id != self.run_id
            or self.expected_task_counters.task_id != self.task_id
        ):
            raise ValueError("MODEL_RESERVATION_TASK_BINDING_MISMATCH")
        if (
            self.started_at_utc.tzinfo is None
            or self.deadline_at_utc.tzinfo is None
            or self.deadline_at_utc
            != self.started_at_utc
            + timedelta(seconds=V01_MECHANISM_LIMITS.ordinary_action_timeout_seconds)
        ):
            raise ValueError("MODEL_RESERVATION_DEADLINE_INVALID")


def model_reservation_amounts(
    request: ModelRequest, budget: BudgetRevisionDocument
) -> ModelBudgetAmounts:
    pricing = {entry.returned_model_id: entry for entry in budget.pricing_entries}
    if not request.allowed_model_ids or not request.allowed_model_ids <= pricing.keys():
        raise ValueError("MODEL_PRICING_MISSING")
    worst_cost = max(
        (
            Decimal(request.max_input_tokens) * pricing[model_id].input_usd_per_million
            + Decimal(request.max_output_tokens) * pricing[model_id].output_usd_per_million
        )
        / Decimal(1_000_000)
        for model_id in request.allowed_model_ids
    )
    return ModelBudgetAmounts(
        calls=1,
        input_tokens=request.max_input_tokens,
        output_tokens=request.max_output_tokens,
        cost_usd=worst_cost,
    )


@dataclass(frozen=True, slots=True)
class ModelReservation:
    decision: ModelReservationDecision
    reason: ModelReservationReason
    run_id: RunId
    task_id: TaskId | None
    attempt_id: AttemptId | None
    tranche_id: str | None
    turn: LogicalModelTurn | None
    intent: ModelRequestIntent | None
    reserved_amounts: ModelBudgetAmounts
    run_counters_before: ModelCounters
    run_counters_after: ModelCounters
    task_counters_before: TaskBudgetState | None
    task_counters_after: TaskBudgetState | None
    deadline_at_utc: datetime
    pause_after_barrier: bool
    resulting_sequence: AuditSequence


AuthorizationKind = Literal["ALLOW", "REQUIRE_APPROVAL", "DENY"]
AuthorizedActionClass = Literal[
    "READ",
    "SEARCH",
    "PATCH",
    "DECLARED_CHECK",
    "RISKY",
    "FINISH",
    "FAIL",
    "TARGET_CAS",
]
AuthorizationReason = Literal[
    "AUTHORIZED",
    "APPROVAL_REQUIRED",
    "HARD_DENIAL",
    "REVISION_BINDING_MISMATCH",
    "TARGET_BINDING_MISMATCH",
    "RUN_NOT_DISPATCHABLE",
    "COUNTER_BINDING_MISMATCH",
    "RUN_TASK_ATTEMPT_MISMATCH",
    "LEASE_MISSING",
    "LEASE_EXPIRED",
    "LEASE_GENERATION_MISMATCH",
    "LEASE_HEAD_NOT_ADMISSIBLE",
    "ACTION_OUTSIDE_LEASE",
    "ACTION_DEADLINE_INVALID",
    "BUDGET_EXHAUSTED",
]


@dataclass(frozen=True, slots=True)
class AuthorizationRequest:
    run_id: RunId
    task_id: TaskId
    attempt_id: AttemptId
    logical_turn_id: str
    action_id: str
    action: ActionEnvelope
    authority_origin: Literal["WORKER", "ADMISSION"]
    action_digest: str
    expected_prestate_digest: str
    lease_id: str
    lease_generation: int
    admissible_head: str
    task_contract_digest: str
    plan_digest: RevisionDigest
    policy_digest: RevisionDigest
    budget_digest: RevisionDigest
    model_configuration_digest: RevisionDigest
    tool_schema_digest: str
    target_safety_digest: str
    started_at_utc: datetime
    deadline_at_utc: datetime
    expected_sequence: AuditSequence


@dataclass(frozen=True, slots=True)
class AuthorizationDecision:
    decision: AuthorizationKind
    reason: AuthorizationReason
    run_id: RunId
    task_id: TaskId
    attempt_id: AttemptId
    action_id: str
    action_digest: str
    binding_digest: str
    action_class: AuthorizedActionClass
    approved_timeout_seconds: int
    deadline_at_utc: datetime
    persistence: Literal["WITH_EFFECT_INTENT", "WITH_PENDING_ACTION", "DENIAL_AUDIT"]
    effect_intent_id: IntentId | None
    pending_action_id: str | None
    resulting_sequence: AuditSequence | None


@dataclass(frozen=True, slots=True)
class FrozenActionBindings:
    run_id: RunId
    task_id: TaskId
    attempt_id: AttemptId
    logical_turn_id: str
    action_id: str
    lease_id: str
    lease_generation: int
    run_head_oid: str
    target_safety_digest: RevisionDigest
    plan_digest: RevisionDigest
    policy_digest: RevisionDigest
    budget_digest: RevisionDigest
    model_configuration_digest: RevisionDigest
    tool_schema_digest: str
    authorization_binding_digest: str
    deadline_at_utc: datetime

    @property
    def applicable_revision_digests(self) -> ApplicableRevisionDigests:
        return ApplicableRevisionDigests(
            plan_digest=self.plan_digest,
            policy_digest=self.policy_digest,
            budget_digest=self.budget_digest,
            model_configuration_digest=self.model_configuration_digest,
        )

    @classmethod
    def from_authorization(
        cls, request: AuthorizationRequest, decision: AuthorizationDecision
    ) -> FrozenActionBindings:
        if (
            decision.decision != "REQUIRE_APPROVAL"
            or decision.persistence != "WITH_PENDING_ACTION"
            or decision.run_id != request.run_id
            or decision.task_id != request.task_id
            or decision.attempt_id != request.attempt_id
            or decision.action_id != request.action_id
            or decision.action_digest != request.action_digest
            or decision.deadline_at_utc != request.deadline_at_utc
            or decision.resulting_sequence is not None
        ):
            raise ValueError("PENDING_ACTION_AUTHORIZATION_MISMATCH")
        return cls(
            run_id=request.run_id,
            task_id=request.task_id,
            attempt_id=request.attempt_id,
            logical_turn_id=request.logical_turn_id,
            action_id=request.action_id,
            lease_id=request.lease_id,
            lease_generation=request.lease_generation,
            run_head_oid=request.admissible_head,
            target_safety_digest=RevisionDigest(request.target_safety_digest),
            plan_digest=request.plan_digest,
            policy_digest=request.policy_digest,
            budget_digest=request.budget_digest,
            model_configuration_digest=request.model_configuration_digest,
            tool_schema_digest=request.tool_schema_digest,
            authorization_binding_digest=decision.binding_digest,
            deadline_at_utc=decision.deadline_at_utc,
        )


PendingActionState = Literal["WAITING_APPROVAL", "GRANT_CONSUMED", "SETTLED", "INVALIDATED"]
GrantState = Literal["ISSUED", "CONSUMED", "REJECTED", "EXPIRED", "INVALIDATED"]


@dataclass(frozen=True, slots=True)
class PendingAction:
    pending_id: PendingActionId
    action: RiskyAction
    normalized_action_json: str
    action_digest: Sha256DigestText
    pending_action_digest: Sha256DigestText
    confirmation_code_digest: Sha256DigestText
    authorization_binding_digest: str
    expected_pre_state: ActionPreState
    bindings: FrozenActionBindings
    expires_at: datetime
    state: PendingActionState = "WAITING_APPROVAL"


def canonical_action_json(action: RiskyAction) -> str:
    return json.dumps(
        action.model_dump(mode="json", exclude_none=True),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


def pending_action_subject_json(
    *,
    pending_id: PendingActionId,
    normalized_action_json: str,
    action_digest: Sha256DigestText,
    authorization_binding_digest: Sha256DigestText,
    expected_pre_state: ActionPreState,
    bindings: FrozenActionBindings,
    expires_at: datetime,
) -> str:
    return canonical_json(
        {
            "action_digest": action_digest,
            "authorization_binding_digest": authorization_binding_digest,
            "bindings": asdict(bindings),
            "expires_at_utc": expires_at.isoformat(),
            "expected_pre_state": expected_pre_state.model_dump(mode="json", exclude_none=True),
            "normalized_action_json": normalized_action_json,
            "pending_id": pending_id,
        }
    )


def pending_action_confirmation_material_digest(
    pending_action_digest: Sha256DigestText,
) -> Sha256DigestText:
    return sha256_digest(
        canonical_json({"kind": "grant", "pending_action_digest": pending_action_digest})
    )


def confirmation_code_for_pending_digest(
    pending_action_digest: Sha256DigestText,
) -> ConfirmationCode:
    raw_digest = pending_action_confirmation_material_digest(pending_action_digest).removeprefix(
        "sha256:"
    )
    return ConfirmationCode(base64.b32encode(bytes.fromhex(raw_digest)).decode("ascii")[:6])


def freeze_pending_action(
    pending_id: PendingActionId,
    request: AuthorizationRequest,
    decision: AuthorizationDecision,
    expected_pre_state: ActionPreState,
    now: datetime,
    grant_ttl_seconds: int,
) -> PendingAction:
    if not 1 <= grant_ttl_seconds <= 1_800:
        raise ValueError("PENDING_ACTION_GRANT_TTL_OUT_OF_RANGE")
    if not isinstance(request.action, RiskyAction):
        raise TypeError("PENDING_ACTION_REQUIRES_RISKY_ACTION")
    action = request.action
    bindings = FrozenActionBindings.from_authorization(request, decision)
    normalized = canonical_action_json(action)
    action_digest = sha256_digest(normalized)
    if action_digest != request.action_digest:
        raise ValueError("PENDING_ACTION_DIGEST_MISMATCH")
    expires_at = now + timedelta(seconds=grant_ttl_seconds)
    subject_json = pending_action_subject_json(
        pending_id=pending_id,
        normalized_action_json=normalized,
        action_digest=action_digest,
        authorization_binding_digest=Sha256DigestText(decision.binding_digest),
        expected_pre_state=expected_pre_state,
        bindings=bindings,
        expires_at=expires_at,
    )
    pending_action_digest = sha256_digest(subject_json)
    confirmation_code = confirmation_code_for_pending_digest(pending_action_digest)
    return PendingAction(
        pending_id=pending_id,
        action=action,
        normalized_action_json=normalized,
        action_digest=action_digest,
        pending_action_digest=pending_action_digest,
        confirmation_code_digest=sha256_digest(confirmation_code),
        authorization_binding_digest=decision.binding_digest,
        expected_pre_state=expected_pre_state,
        bindings=bindings,
        expires_at=expires_at,
    )


@dataclass(frozen=True, slots=True)
class ApprovalGrant:
    grant_id: GrantId
    pending_id: PendingActionId
    pending_action_digest: Sha256DigestText
    confirmation_code_digest: Sha256DigestText
    bindings: FrozenActionBindings
    expires_at: datetime
    state: GrantState = "ISSUED"
    consumed_intent_id: IntentId | None = None
    consumed_sequence: AuditSequence | None = None


def approval_grant_id(
    source_request_id: RequestId, pending_action_digest: Sha256DigestText
) -> GrantId:
    identity = canonical_json(
        {
            "pending_action_digest": pending_action_digest,
            "source_request_id": source_request_id,
        }
    )
    return GrantId("grant-" + sha256(identity.encode("utf-8")).hexdigest())


def granted_action_intent_id(grant: ApprovalGrant) -> IntentId:
    identity = canonical_json(
        {
            "grant_id": grant.grant_id,
            "pending_action_digest": grant.pending_action_digest,
            "pending_id": grant.pending_id,
        }
    )
    return IntentId("granted-action-" + sha256(identity.encode("utf-8")).hexdigest())


def frozen_action_bindings_from_mapping(
    data: Mapping[str, object],
) -> FrozenActionBindings:
    return FrozenActionBindings(
        run_id=RunId(str(data["run_id"])),
        task_id=TaskId(str(data["task_id"])),
        attempt_id=AttemptId(str(data["attempt_id"])),
        logical_turn_id=str(data["logical_turn_id"]),
        action_id=str(data["action_id"]),
        lease_id=str(data["lease_id"]),
        lease_generation=int(str(data["lease_generation"])),
        run_head_oid=str(data["run_head_oid"]),
        target_safety_digest=RevisionDigest(str(data["target_safety_digest"])),
        plan_digest=RevisionDigest(str(data["plan_digest"])),
        policy_digest=RevisionDigest(str(data["policy_digest"])),
        budget_digest=RevisionDigest(str(data["budget_digest"])),
        model_configuration_digest=RevisionDigest(str(data["model_configuration_digest"])),
        tool_schema_digest=str(data["tool_schema_digest"]),
        authorization_binding_digest=str(data["authorization_binding_digest"]),
        deadline_at_utc=datetime.fromisoformat(str(data["deadline_at_utc"])),
    )


def approval_grant_to_json(grant: ApprovalGrant) -> str:
    return canonical_json(asdict(grant))


def approval_grant_from_json(value: str) -> ApprovalGrant:
    data = json.loads(value)
    grant = ApprovalGrant(
        grant_id=GrantId(data["grant_id"]),
        pending_id=PendingActionId(data["pending_id"]),
        pending_action_digest=Sha256DigestText(data["pending_action_digest"]),
        confirmation_code_digest=Sha256DigestText(data["confirmation_code_digest"]),
        bindings=frozen_action_bindings_from_mapping(data["bindings"]),
        expires_at=datetime.fromisoformat(data["expires_at"]),
        state=data["state"],
        consumed_intent_id=(
            None if data["consumed_intent_id"] is None else IntentId(data["consumed_intent_id"])
        ),
        consumed_sequence=(
            None if data["consumed_sequence"] is None else AuditSequence(data["consumed_sequence"])
        ),
    )
    if approval_grant_to_json(grant) != value:
        raise ValueError("APPROVAL_GRANT_CANONICAL_BINDING_MISMATCH")
    return grant


class GrantValidationError(ValueError):
    pass


class GrantCommandValidator:
    def validate_payload_before_grant_write(
        self,
        payload: GrantPayload,
        pending: PendingAction,
        now: datetime,
    ) -> None:
        if (
            payload.run_id != pending.bindings.run_id
            or payload.pending_action_id != pending.pending_id
            or not hmac.compare_digest(payload.pending_action_digest, pending.pending_action_digest)
        ):
            raise GrantValidationError("PENDING_ACTION_BINDING_INVALID")
        expected_code = confirmation_code_for_pending_digest(pending.pending_action_digest)
        if not hmac.compare_digest(
            payload.confirmation_code, expected_code
        ) or not hmac.compare_digest(
            sha256_digest(payload.confirmation_code),
            pending.confirmation_code_digest,
        ):
            raise GrantValidationError("GRANT_CONFIRMATION_CODE_INVALID")
        if now >= pending.expires_at:
            raise GrantValidationError("PENDING_ACTION_EXPIRED")

    def validate_current_binding(
        self,
        grant: ApprovalGrant,
        pending: PendingAction,
        current: FrozenActionBindings,
        now: datetime,
    ) -> None:
        if now >= grant.expires_at:
            raise GrantValidationError("PENDING_ACTION_EXPIRED")
        if grant.state != "ISSUED" or pending.state != "WAITING_APPROVAL":
            raise GrantValidationError("GRANT_NOT_ISSUED_FOR_PENDING_ACTION")
        if (
            grant.pending_action_digest != pending.pending_action_digest
            or grant.confirmation_code_digest != pending.confirmation_code_digest
            or grant.bindings != pending.bindings
            or grant.expires_at != pending.expires_at
            or current != pending.bindings
        ):
            raise GrantValidationError("GRANT_BINDING_MISMATCH")


@dataclass(frozen=True, slots=True)
class GrantedActionIntent:
    intent_id: IntentId
    pending_id: PendingActionId
    grant_id: GrantId
    action: RiskyAction
    normalized_action_json: str
    action_digest: Sha256DigestText
    expected_pre_state: ActionPreState
    bindings: FrozenActionBindings
    state: Literal["INTENT_RECORDED", "DISPATCHED", "SETTLED", "INDETERMINATE"] = "INTENT_RECORDED"

    def to_effect_intent(self, recorded_sequence: AuditSequence) -> EffectIntent:
        payload = canonical_json(
            {
                "action_digest": self.action_digest,
                "grant_id": self.grant_id,
                "normalized_action_json": self.normalized_action_json,
                "pending_id": self.pending_id,
                "bindings": asdict(self.bindings),
            }
        )
        return EffectIntent(
            intent_id=self.intent_id,
            run_id=self.bindings.run_id,
            kind="granted_risky_action",
            idempotency_key=(
                f"granted-action:{self.bindings.run_id}:{self.pending_id}:{self.grant_id}"
            ),
            applicable_revision_digests=self.bindings.applicable_revision_digests,
            payload_digest=sha256_digest(payload),
            normalized_payload_json=payload,
            recorded_sequence=recorded_sequence,
            expected_prestate_json=self.expected_pre_state.canonical_json(),
            task_id=self.bindings.task_id,
            attempt_id=self.bindings.attempt_id,
            action_id=self.bindings.action_id,
        )


class Authority(Protocol):
    def authorize_action(self, request: AuthorizationRequest) -> AuthorizationDecision:
        raise NotImplementedError

    def reserve_model_attempt(self, request: ModelReservationRequest) -> ModelReservation:
        raise NotImplementedError

    def open_action_deadline(
        self, run_id: RunId, intent_id: IntentId, expected_sequence: AuditSequence
    ) -> ActionDeadline:
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class WorkspaceLease:
    lease_id: str
    run_id: RunId
    task_id: TaskId
    attempt_id: AttemptId
    generation: int
    base_head: str
    admissible_head: str
    task_contract_digest: str
    write_globs: tuple[GlobPattern, ...]
    sensitivity_globs: tuple[GlobPattern, ...]
    issued_at: datetime
    expires_at: datetime
    state: Literal["ACTIVE", "RELEASED", "EXPIRED", "REVOKED"]

    @property
    def decision(self) -> Literal["ALLOW"]:
        return "ALLOW"


@dataclass(frozen=True, slots=True)
class LeaseDenial:
    decision: Literal["DENY"] = "DENY"
    state: Literal["DENIED"] = "DENIED"
    reason: Literal["OVERLAPPING_WRITE_SCOPE", "WORKER_CEILING"] = "OVERLAPPING_WRITE_SCOPE"


@dataclass(frozen=True, slots=True)
class LeaseAuthorization:
    decision: Literal["ALLOW", "DENY"]
    reason: Literal[
        "AUTHORIZED",
        "LEASE_EXPIRED",
        "LEASE_GENERATION_MISMATCH",
        "LEASE_HEAD_NOT_ADMISSIBLE",
        "WRITE_OUTSIDE_LEASE",
    ]


@dataclass(frozen=True, slots=True)
class CheckSet:
    fresh_passes: frozenset[str]
    failures: frozenset[str]


def progress_from_checks(
    previous: CheckSet,
    current: CheckSet,
    previous_lifecycle: str,
    current_lifecycle: str,
) -> bool:
    pass_progress = (
        previous.fresh_passes < current.fresh_passes and current.failures <= previous.failures
    )
    failure_progress = (
        current.failures < previous.failures and previous.fresh_passes <= current.fresh_passes
    )
    lifecycle_progress = (previous_lifecycle, current_lifecycle) in {
        ("ACTIVE", "VERIFYING"),
        ("VERIFYING", "CANDIDATE_READY"),
    }
    return pass_progress or failure_progress or lifecycle_progress


@dataclass(frozen=True, slots=True)
class ProgressEvidence:
    previous: CheckSet
    current: CheckSet
    previous_lifecycle: str
    current_lifecycle: str


NO_PROGRESS = ProgressEvidence(
    CheckSet(frozenset(), frozenset()),
    CheckSet(frozenset(), frozenset()),
    "ACTIVE",
    "ACTIVE",
)


@dataclass(frozen=True, slots=True)
class TaskAuthority:
    run_id: RunId
    task_id: TaskId
    attempt_id: AttemptId


TaskLifecycleState = Literal["ACTIVE", "READY", "PAUSED"]
AttemptLifecycleState = Literal["RUNNING", "FAILED"]


@dataclass(frozen=True, slots=True)
class CheckpointKey:
    tree_oid: str
    check_set_digest: str


@dataclass(frozen=True, slots=True)
class TaskStopDecision:
    decision: Literal["CONTINUE", "PAUSE"]
    run_id: RunId
    task_id: TaskId
    task_state: TaskLifecycleState
    pause_reason: Literal["REPEATED_CHECKPOINT", "REPEATED_INVALID_ACTION"] | None
    resulting_sequence: AuditSequence
    checkpoint_count: int = 0
    identical_invalid_action_count: int = 0
    attempt_state: Literal["FAILED"] | None = None

    @property
    def code(self) -> Literal["MALFORMED_ACTION", "TASK_OBSERVATION_RECORDED"]:
        return "MALFORMED_ACTION" if self.attempt_state == "FAILED" else "TASK_OBSERVATION_RECORDED"

    @property
    def stop_reason(self) -> str | None:
        return self.pause_reason


@dataclass(frozen=True, slots=True)
class TaskPauseBinding:
    run_id: RunId
    task_id: TaskId
    pause_sequence: AuditSequence
    pause_reason: str
    counter_snapshot_digest: str
    previous_attempt_id: AttemptId
    budget_digest_at_pause: RevisionDigest
    applicable_revision_digests_at_pause: ApplicableRevisionDigests
    budget_ceiling_exhaustions: tuple[BudgetCeilingExhaustion, ...] = ()


@dataclass(frozen=True, slots=True)
class ResumeTaskRequest:
    run_id: RunId
    task_id: TaskId
    pause_sequence: AuditSequence
    pause_reason: str
    applicable_revision_digests: ApplicableRevisionDigests
    expected_sequence: AuditSequence


@dataclass(frozen=True, slots=True)
class TaskResumeDecision:
    decision: Literal["RESUME", "STALE", "DENY"]
    run_id: RunId
    task_id: TaskId
    task_state: Literal["READY", "PAUSED"]
    allocation_id: str | None
    new_attempt_id: AttemptId | None
    allocated_calls: int
    failed_invariant: str | None
    safe_next_action: str | None

    @classmethod
    def stale(cls, run_id: RunId, task_id: TaskId, invariant: str) -> TaskResumeDecision:
        return cls("STALE", run_id, task_id, "PAUSED", None, None, 0, invariant, None)

    @classmethod
    def denied(
        cls,
        run_id: RunId,
        task_id: TaskId,
        invariant: str,
        safe_next_action: str | None,
    ) -> TaskResumeDecision:
        return cls(
            "DENY",
            run_id,
            task_id,
            "PAUSED",
            None,
            None,
            0,
            invariant,
            safe_next_action,
        )


@dataclass(frozen=True, slots=True)
class TaskResumeAllocation:
    allocation_id: str
    run_id: RunId
    task_id: TaskId
    reserved_attempt_id: AttemptId
    budget_digest: RevisionDigest
    applicable_revision_digests: ApplicableRevisionDigests
    allocated_calls: int
    state: Literal["RESERVED", "CONSUMED", "INVALIDATED"]
    created_sequence: AuditSequence


@dataclass(frozen=True, slots=True)
class TaskCounterSnapshot:
    run_id: RunId
    task_id: TaskId
    allocated_calls: int
    model_calls: int
    input_tokens: int
    output_tokens: int
    cost_reserve_usd: Decimal
    attempts: int
    stale_refreshes: int
    manual_resumes: int
    next_lease_generation: int
    failure_digests: tuple[str, ...]
    checkpoint_history: tuple[CheckpointKey, ...]
    invalid_action_history: tuple[str, ...]
    warning_keys: tuple[str, ...]

    @property
    def digest(self) -> str:
        return "sha256:" + sha256(task_counter_snapshot_to_json(self).encode("utf-8")).hexdigest()


def task_counter_snapshot_to_json(snapshot: TaskCounterSnapshot) -> str:
    return json.dumps(
        {
            "allocated_calls": snapshot.allocated_calls,
            "attempts": snapshot.attempts,
            "checkpoint_history": [
                {
                    "check_set_digest": item.check_set_digest,
                    "tree_oid": item.tree_oid,
                }
                for item in snapshot.checkpoint_history
            ],
            "cost_reserve_usd": str(snapshot.cost_reserve_usd),
            "failure_digests": list(snapshot.failure_digests),
            "input_tokens": snapshot.input_tokens,
            "invalid_action_history": list(snapshot.invalid_action_history),
            "manual_resumes": snapshot.manual_resumes,
            "model_calls": snapshot.model_calls,
            "next_lease_generation": snapshot.next_lease_generation,
            "output_tokens": snapshot.output_tokens,
            "run_id": snapshot.run_id,
            "stale_refreshes": snapshot.stale_refreshes,
            "task_id": snapshot.task_id,
            "warning_keys": list(snapshot.warning_keys),
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def task_counter_snapshot_from_json(value: str) -> TaskCounterSnapshot:
    data = json.loads(value)
    return TaskCounterSnapshot(
        run_id=RunId(data["run_id"]),
        task_id=TaskId(data["task_id"]),
        allocated_calls=int(data["allocated_calls"]),
        model_calls=int(data["model_calls"]),
        input_tokens=int(data["input_tokens"]),
        output_tokens=int(data["output_tokens"]),
        cost_reserve_usd=Decimal(data["cost_reserve_usd"]),
        attempts=int(data["attempts"]),
        stale_refreshes=int(data["stale_refreshes"]),
        manual_resumes=int(data["manual_resumes"]),
        next_lease_generation=int(data["next_lease_generation"]),
        failure_digests=tuple(data["failure_digests"]),
        checkpoint_history=tuple(CheckpointKey(**item) for item in data["checkpoint_history"]),
        invalid_action_history=tuple(data["invalid_action_history"]),
        warning_keys=tuple(data["warning_keys"]),
    )


def task_resume_ids(
    request: ResumeTaskRequest,
    pause: TaskPauseBinding,
    counters: TaskCounterSnapshot,
    budget_digest: RevisionDigest,
    calls: int,
) -> tuple[str, AttemptId]:
    binding = (
        "sha256:"
        + sha256(
            json.dumps(
                {
                    "applicable_revision_digests": (
                        request.applicable_revision_digests.model_dump(mode="json")
                    ),
                    "budget_digest": budget_digest,
                    "calls": calls,
                    "counter_snapshot_digest": counters.digest,
                    "expected_sequence": request.expected_sequence,
                    "pause_reason": pause.pause_reason,
                    "pause_sequence": pause.pause_sequence,
                    "run_id": pause.run_id,
                    "task_id": pause.task_id,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
    )
    allocation_digest = sha256(f"task-resume-allocation:{binding}".encode()).hexdigest()
    attempt_digest = sha256(f"task-resume-attempt:{binding}".encode()).hexdigest()
    return (
        f"resume-allocation-{allocation_digest}",
        AttemptId(f"attempt-{attempt_digest}"),
    )


@dataclass(frozen=True, slots=True)
class DispatchAuthorization:
    decision: Literal["ALLOW", "DENY"]
    reason: Literal[
        "AUTHORIZED",
        "TASK_NOT_READY",
        "TASK_PAUSED",
        "RUN_DISPATCH_CLOSED",
        "ACTIVE_RUN_TIME_CEILING",
    ]


@dataclass(frozen=True, slots=True)
class ActiveRunTimeBoundaryDecision:
    decision: Literal["CONTINUE", "PAUSE"]
    observed_nanoseconds: int
    ceiling_nanoseconds: int
    resulting_sequence: AuditSequence


TrancheReason = Literal["BOOTSTRAP", "OBJECTIVE_PROGRESS", "NO_PROGRESS", "TASK_CALL_CEILING"]


@dataclass(frozen=True, slots=True)
class TrancheDecision:
    decision: Literal["ALLOCATE", "PAUSE"]
    reason: TrancheReason
    run_id: RunId
    task_id: TaskId
    attempt_id: AttemptId
    tranche_id: str | None
    tranche_number: int
    calls: int
    counters_before: TaskBudgetState
    counters_after: TaskBudgetState
    resulting_sequence: AuditSequence


class AuthorityState(Protocol):
    def install_approved_budget_for_test(
        self,
        run_id: RunId,
        budget_digest: RevisionDigest,
        budget: BudgetRevisionDocument,
    ) -> None:
        raise NotImplementedError

    def current_approved_budget(
        self, run_id: RunId
    ) -> tuple[RevisionDigest, BudgetRevisionDocument]:
        raise NotImplementedError

    def effect_intent(self, intent_id: IntentId) -> EffectIntent:
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

    def global_usage_snapshot(self, run_id: RunId) -> GlobalUsageSnapshot:
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

    def authorize_new_action(self, run_id: RunId) -> DispatchAuthorization:
        raise NotImplementedError

    def reserve_authorized_model_attempt(
        self, request: ModelReservationRequest
    ) -> ModelReservation:
        raise NotImplementedError

    def issue_workspace_lease(
        self,
        lease: WorkspaceLease,
        budget_digest: RevisionDigest,
        expected_sequence: AuditSequence,
    ) -> WorkspaceLease | LeaseDenial:
        raise NotImplementedError

    def workspace_lease(self, run_id: RunId, lease_id: str) -> WorkspaceLease | None:
        raise NotImplementedError

    def authorization_binding_failure(
        self, request: AuthorizationRequest
    ) -> AuthorizationReason | None:
        raise NotImplementedError

    def expire_workspace_lease(
        self,
        run_id: RunId,
        lease_id: str,
        expected_sequence: AuditSequence,
    ) -> AuditSequence:
        raise NotImplementedError

    def renew_workspace_lease(
        self,
        run_id: RunId,
        lease_id: str,
        generation: int,
        latest_admissible_head: str,
        renewed_at: datetime,
        expires_at: datetime,
        expected_sequence: AuditSequence,
    ) -> WorkspaceLease | LeaseDenial:
        raise NotImplementedError

    def record_authorization_denial(
        self,
        request: AuthorizationRequest,
        binding_digest: str,
        reason: AuthorizationReason,
        expected_sequence: AuditSequence,
    ) -> AuditSequence:
        raise NotImplementedError

    def task_budget_state(self, run_id: RunId, task_id: TaskId) -> TaskBudgetState:
        raise NotImplementedError

    def revision_binding_failure(
        self, run_id: RunId, expected: ApplicableRevisionDigests
    ) -> str | None:
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

    def allocate_task_tranche(
        self,
        task: TaskAuthority,
        expected: TaskBudgetState,
        calls: int,
        reason: TrancheReason,
        progress: ProgressEvidence,
        expected_sequence: AuditSequence,
    ) -> TrancheDecision:
        raise NotImplementedError

    def record_task_checkpoint(
        self,
        task: TaskAuthority,
        checkpoint: CheckpointKey,
        budget_digest: RevisionDigest,
        expected_sequence: AuditSequence,
    ) -> TaskStopDecision:
        raise NotImplementedError

    def record_invalid_action(
        self,
        task: TaskAuthority,
        attempt_id: AttemptId,
        action_digest: str,
        budget_digest: RevisionDigest,
        expected_sequence: AuditSequence,
    ) -> TaskStopDecision:
        raise NotImplementedError

    def authorize_new_attempt(self, run_id: RunId, task_id: TaskId) -> DispatchAuthorization:
        raise NotImplementedError

    def active_run_time_state(self, run_id: RunId) -> ActiveRunTimeState:
        raise NotImplementedError

    def evaluate_active_run_time_boundary(
        self,
        *,
        run_id: RunId,
        budget_digest: RevisionDigest,
        expected: ActiveRunTimeState,
        ceiling_nanoseconds: int,
        expected_sequence: AuditSequence,
    ) -> ActiveRunTimeBoundaryDecision:
        raise NotImplementedError


DIRECTLY_RESUMABLE_TASK_REASONS = frozenset(
    {"NO_PROGRESS", "REPEATED_CHECKPOINT", "REPEATED_INVALID_ACTION"}
)
NON_RESUMABLE_TASK_REASONS = frozenset(
    {
        "CONTEXT_OVERFLOW",
        "SCOPE_EXPANSION_REQUIRED",
        "RUN_CHECK_FAILED",
        "FROZEN_BINDING_MISMATCH",
    }
)


class CapacityKind(StrEnum):
    AVAILABLE = "AVAILABLE"
    CURRENT_REVISION_CEILING = "CURRENT_REVISION_CEILING"
    FIXED_MAXIMUM = "FIXED_MAXIMUM"


@dataclass(frozen=True, slots=True)
class CapacityAssessment:
    kind: CapacityKind
    metrics: tuple[GlobalBudgetMetric, ...]


def _remaining_task_calls(counters: TaskCounterSnapshot) -> int:
    return max(0, V01_MECHANISM_LIMITS.task_call_ceiling - counters.allocated_calls)


def _table_or_task_hard_cap_reached(
    counters: TaskCounterSnapshot,
    usage: GlobalUsageSnapshot,
    budget: BudgetRevisionDocument,
) -> CapacityAssessment:
    if (
        counters.allocated_calls >= V01_MECHANISM_LIMITS.task_call_ceiling
        or counters.attempts >= V01_MECHANISM_LIMITS.task_attempt_ceiling
        or counters.stale_refreshes >= V01_MECHANISM_LIMITS.stale_refresh_ceiling
        or counters.manual_resumes >= V01_MECHANISM_LIMITS.manual_resume_ceiling
    ):
        return CapacityAssessment(CapacityKind.FIXED_MAXIMUM, ())

    fixed_metrics: list[GlobalBudgetMetric] = []
    revision_metrics: list[GlobalBudgetMetric] = []
    for metric in GlobalBudgetMetric:
        used = usage.amount_for(metric)
        maximum = global_budget_maximum_for(metric)
        current = global_ceiling_for(budget, metric)
        if used >= maximum:
            fixed_metrics.append(metric)
        elif used >= current:
            revision_metrics.append(metric)
    if fixed_metrics:
        return CapacityAssessment(CapacityKind.FIXED_MAXIMUM, tuple(fixed_metrics))
    if revision_metrics:
        return CapacityAssessment(CapacityKind.CURRENT_REVISION_CEILING, tuple(revision_metrics))
    return CapacityAssessment(CapacityKind.AVAILABLE, ())


def _approved_higher_budget_restores_capacity(
    pause: TaskPauseBinding,
    current_budget_digest: RevisionDigest,
    current_budget: BudgetRevisionDocument,
    usage: GlobalUsageSnapshot,
) -> bool:
    if (
        current_budget_digest == pause.budget_digest_at_pause
        or not pause.budget_ceiling_exhaustions
    ):
        return False
    return all(
        global_ceiling_for(current_budget, exhausted.metric) > exhausted.ceiling
        and usage.amount_for(exhausted.metric)
        < global_ceiling_for(current_budget, exhausted.metric)
        for exhausted in pause.budget_ceiling_exhaustions
    )


def _resume_revision_binding_matches(
    pause: TaskPauseBinding,
    request: ResumeTaskRequest,
) -> bool:
    paused = pause.applicable_revision_digests_at_pause
    current = request.applicable_revision_digests
    fixed_revisions_match = (
        paused.plan_digest == current.plan_digest
        and paused.policy_digest == current.policy_digest
        and paused.model_configuration_digest == current.model_configuration_digest
    )
    budget_may_be_higher = pause.pause_reason == "LOWERED_BUDGET_CEILING" or bool(
        pause.budget_ceiling_exhaustions
    )
    return fixed_revisions_match and (
        budget_may_be_higher or paused.budget_digest == current.budget_digest
    )


class AuthorityService:
    def __init__(
        self,
        journal: AuthorityState,
        utc_clock: UtcClock | None = None,
        secret_paths: SecretPathPolicy | None = None,
    ) -> None:
        self._journal = journal
        self._utc_clock = utc_clock or SystemUtcClock()
        self._secret_paths = secret_paths

    def _utc_now(self) -> datetime:
        now = self._utc_clock.now()
        if now.tzinfo is None or now.utcoffset() is None:
            raise AuthorityDenied("UTC_CLOCK_REQUIRED")
        return now.astimezone(UTC)

    def _budget(self, run_id: RunId) -> tuple[RevisionDigest, BudgetRevisionDocument]:
        return self._journal.current_approved_budget(run_id)

    def task_counters(self, run_id: RunId, task_id: TaskId) -> TaskCounterSnapshot:
        return self._journal.task_counters(run_id, task_id)

    def resume_task(self, request: ResumeTaskRequest) -> TaskResumeDecision:
        pause = self._journal.current_task_pause(request.run_id, request.task_id)
        if pause is None or (
            pause.pause_sequence != request.pause_sequence
            or pause.pause_reason != request.pause_reason
        ):
            return TaskResumeDecision.stale(
                request.run_id,
                request.task_id,
                "TASK_PAUSE_BINDING_MISMATCH",
            )
        if self._journal.revision_binding_failure(
            request.run_id, request.applicable_revision_digests
        ) is not None or not _resume_revision_binding_matches(pause, request):
            return TaskResumeDecision.stale(
                request.run_id,
                request.task_id,
                "REVISION_BINDING_MISMATCH",
            )
        counters = self._journal.task_counters(request.run_id, request.task_id)
        if counters.digest != pause.counter_snapshot_digest:
            return TaskResumeDecision.stale(
                request.run_id,
                request.task_id,
                "TASK_COUNTER_BINDING_MISMATCH",
            )
        budget_digest, budget = self._budget(request.run_id)
        usage = self._journal.global_usage_snapshot(request.run_id)
        denial = self._resume_denial(pause, counters, budget_digest, budget, usage)
        if denial is not None:
            return denial
        calls = min(
            V01_MECHANISM_LIMITS.renewal_tranche_calls,
            _remaining_task_calls(counters),
        )
        return self._journal.accept_task_resume(
            request,
            pause,
            counters,
            budget_digest,
            usage,
            calls,
        )

    def _resume_denial(
        self,
        pause: TaskPauseBinding,
        counters: TaskCounterSnapshot,
        budget_digest: RevisionDigest,
        budget: BudgetRevisionDocument,
        usage: GlobalUsageSnapshot,
    ) -> TaskResumeDecision | None:
        capacity = _table_or_task_hard_cap_reached(counters, usage, budget)
        if capacity.kind == CapacityKind.FIXED_MAXIMUM:
            invariant = (
                "MANUAL_RESUME_CAP_REACHED"
                if counters.manual_resumes >= V01_MECHANISM_LIMITS.manual_resume_ceiling
                else "NON_RAISEABLE_CAP_REACHED"
            )
            return TaskResumeDecision.denied(
                pause.run_id,
                pause.task_id,
                invariant,
                "CANCEL_AND_CREATE_NEW_RUN",
            )
        if (
            capacity.kind == CapacityKind.CURRENT_REVISION_CEILING
            or pause.pause_reason == "LOWERED_BUDGET_CEILING"
        ):
            if _approved_higher_budget_restores_capacity(
                pause,
                budget_digest,
                budget,
                usage,
            ):
                return None
            return TaskResumeDecision.denied(
                pause.run_id,
                pause.task_id,
                "HIGHER_APPROVED_BUDGET_REQUIRED",
                None,
            )
        if pause.pause_reason in NON_RESUMABLE_TASK_REASONS:
            return TaskResumeDecision.denied(
                pause.run_id,
                pause.task_id,
                "TASK_PAUSE_NOT_RESUMABLE",
                "CANCEL_AND_CREATE_NEW_RUN",
            )
        if pause.pause_reason == "CHECK_INFRASTRUCTURE_UNCERTAINTY":
            if not self._journal.task_repair_observed(pause):
                return TaskResumeDecision.denied(
                    pause.run_id,
                    pause.task_id,
                    "INFRASTRUCTURE_CAUSE_NOT_REPAIRED",
                    None,
                )
            return None
        if pause.pause_reason not in DIRECTLY_RESUMABLE_TASK_REASONS:
            return TaskResumeDecision.denied(
                pause.run_id,
                pause.task_id,
                "UNKNOWN_TASK_PAUSE_REASON",
                None,
            )
        return None

    def open_action_deadline(
        self,
        run_id: RunId,
        intent_id: IntentId,
        expected_sequence: AuditSequence,
    ) -> ActionDeadline:
        intent = self._journal.effect_intent(intent_id)
        if intent.run_id != run_id:
            raise AuthorityDenied("ACTION_DEADLINE_INTENT_RUN_MISMATCH")
        action_class, check_id, snapshot_digest = action_deadline_binding(intent)
        started_at = self._utc_now()
        seconds = (
            V01_MECHANISM_LIMITS.check_timeout_seconds
            if action_class == ActionClass.DECLARED_CHECK
            else V01_MECHANISM_LIMITS.ordinary_action_timeout_seconds
        )
        budget_digest, _ = self._budget(run_id)
        deadline = ActionDeadline(
            run_id=run_id,
            intent_id=intent_id,
            budget_digest=budget_digest,
            applicable_revision_digests=intent.applicable_revision_digests,
            action_class=action_class,
            started_at=started_at,
            expires_at=started_at + timedelta(seconds=seconds),
            recorded_sequence=AuditSequence(expected_sequence + 1),
            check_id=check_id,
            snapshot_digest=snapshot_digest,
        )
        return self._journal.record_action_deadline(deadline, expected_sequence)

    def deadline_state(self, deadline: ActionDeadline) -> Literal["OPEN", "TIMED_OUT"]:
        return "TIMED_OUT" if self._utc_now() >= deadline.expires_at else "OPEN"

    def remaining_budget_allows_retry(self, run_id: RunId) -> bool:
        return self._journal.authorize_new_action(run_id).decision == "ALLOW"

    def settle_timeout(
        self,
        deadline: ActionDeadline,
        outcome_observable: bool,
        expected_sequence: AuditSequence,
    ) -> TimeoutDecision:
        if outcome_observable:
            raise AuthorityDenied("OBSERVABLE_RESULT_MUST_SETTLE_NORMALLY")
        if self.deadline_state(deadline) != "TIMED_OUT":
            raise AuthorityDenied("ACTION_DEADLINE_NOT_EXPIRED")
        if deadline.action_class == ActionClass.ORDINARY:
            decision = TimeoutDecision(
                outcome="INDETERMINATE",
                semantic_result=None,
                receipt=None,
                retry_scope=None,
                retry_allowed=False,
                full_reservation_charged=True,
            )
        else:
            if deadline.check_id is None or deadline.snapshot_digest is None:
                raise AuthorityDenied("CHECK_TIMEOUT_BINDING_REQUIRED")
            decision = TimeoutDecision(
                outcome="INFRASTRUCTURE_UNCERTAINTY",
                semantic_result=None,
                receipt=None,
                retry_scope=(deadline.check_id, deadline.snapshot_digest),
                retry_allowed=self.remaining_budget_allows_retry(deadline.run_id),
                full_reservation_charged=False,
            )
        return self._journal.settle_action_timeout(deadline, decision, expected_sequence)

    def settle_global_usage(
        self,
        run_id: RunId,
        metric: GlobalBudgetMetric | str,
        absolute_used: int | Decimal,
        expected_sequence: AuditSequence,
    ) -> BudgetSettlement:
        normalized = normalize_global_budget_metric(metric)
        budget_digest, _ = self._budget(run_id)
        return self._journal.settle_global_usage(
            run_id,
            budget_digest,
            normalized,
            absolute_used,
            expected_sequence,
        )

    def begin_atomic_action(
        self,
        run_id: RunId,
        action_id: str,
        expected_sequence: AuditSequence,
    ) -> AtomicAction:
        budget_digest, _ = self._budget(run_id)
        action = AtomicAction(
            run_id=run_id,
            action_id=action_id,
            budget_digest=budget_digest,
            state="IN_FLIGHT",
            opened_sequence=AuditSequence(expected_sequence + 1),
        )
        return self._journal.begin_atomic_action(action, expected_sequence)

    def settle_atomic_action(
        self,
        action: AtomicAction,
        model_calls: int,
        expected_sequence: AuditSequence,
    ) -> BudgetSettlement:
        return self._journal.settle_atomic_action(
            action,
            model_calls,
            expected_sequence,
        )

    def authorize_new_action(self, run_id: RunId) -> DispatchAuthorization:
        return self._journal.authorize_new_action(run_id)

    def reserve_model_attempt(self, request: ModelReservationRequest) -> ModelReservation:
        return self._journal.reserve_authorized_model_attempt(request)

    def record_checkpoint(
        self,
        task: TaskAuthority,
        checkpoint: CheckpointKey,
        expected_sequence: AuditSequence,
    ) -> TaskStopDecision:
        budget_digest, _ = self._budget(task.run_id)
        return self._journal.record_task_checkpoint(
            task,
            checkpoint,
            budget_digest,
            expected_sequence,
        )

    def record_invalid_action(
        self,
        task: TaskAuthority,
        attempt_id: AttemptId,
        action_digest: str,
        expected_sequence: AuditSequence,
    ) -> TaskStopDecision:
        budget_digest, _ = self._budget(task.run_id)
        return self._journal.record_invalid_action(
            task,
            attempt_id,
            action_digest,
            budget_digest,
            expected_sequence,
        )

    def authorize_new_attempt(self, run_id: RunId, task_id: TaskId) -> DispatchAuthorization:
        return self._journal.authorize_new_attempt(run_id, task_id)

    def evaluate_active_run_time_boundary(
        self,
        run_id: RunId,
        expected_sequence: AuditSequence,
    ) -> ActiveRunTimeBoundaryDecision:
        budget_digest, budget = self._budget(run_id)
        expected = self._journal.active_run_time_state(run_id)
        return self._journal.evaluate_active_run_time_boundary(
            run_id=run_id,
            budget_digest=budget_digest,
            expected=expected,
            ceiling_nanoseconds=budget.active_run_seconds_ceiling * 1_000_000_000,
            expected_sequence=expected_sequence,
        )

    def issue_lease(
        self,
        attempt: AttemptAuthority,
        contract: TaskContract,
        *,
        expected_sequence: AuditSequence,
        now: datetime | None = None,
    ) -> WorkspaceLease | LeaseDenial:
        if contract.task_id != attempt.task_id:
            raise ValueError("LEASE_TASK_CONTRACT_MISMATCH")
        issued_at = now or datetime.now(UTC)
        check_inputs = tuple(pattern for check in contract.checks for pattern in check.input_globs)
        sensitivity = tuple(
            dict.fromkeys(
                contract.read_globs
                + contract.dependency_globs
                + contract.write_globs
                + check_inputs
            )
        )
        budget_digest, _ = self._budget(attempt.run_id)
        identity = json.dumps(
            {
                "attempt_id": attempt.attempt_id,
                "expected_sequence": expected_sequence,
                "generation": attempt.generation,
                "run_id": attempt.run_id,
                "task_id": attempt.task_id,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        lease = WorkspaceLease(
            lease_id=f"lease-{sha256(identity).hexdigest()}",
            run_id=attempt.run_id,
            task_id=attempt.task_id,
            attempt_id=attempt.attempt_id,
            generation=attempt.generation,
            base_head=attempt.base_head,
            admissible_head=attempt.base_head,
            task_contract_digest=attempt.task_contract_digest,
            write_globs=contract.write_globs,
            sensitivity_globs=sensitivity,
            issued_at=issued_at,
            expires_at=issued_at + timedelta(minutes=15),
            state="ACTIVE",
        )
        return self._journal.issue_workspace_lease(
            lease,
            budget_digest,
            expected_sequence,
        )

    def renew_lease(
        self,
        run_id: RunId,
        lease_id: str,
        generation: int,
        latest_admissible_head: str,
        expected_sequence: AuditSequence,
        now: datetime | None = None,
    ) -> WorkspaceLease | LeaseDenial:
        renewed_at = now or datetime.now(UTC)
        return self._journal.renew_workspace_lease(
            run_id,
            lease_id,
            generation,
            latest_admissible_head,
            renewed_at,
            renewed_at + timedelta(minutes=15),
            expected_sequence,
        )

    def authorize_write(
        self,
        run_id: RunId,
        lease_id: str,
        generation: int,
        head: str,
        path: str,
        now: datetime,
        expected_sequence: AuditSequence,
    ) -> LeaseAuthorization:
        lease = self._journal.workspace_lease(run_id, lease_id)
        if lease is None:
            return LeaseAuthorization("DENY", "LEASE_EXPIRED")
        if lease.state != "ACTIVE" or lease.expires_at <= now:
            if lease.state == "ACTIVE":
                self._journal.expire_workspace_lease(run_id, lease_id, expected_sequence)
            return LeaseAuthorization("DENY", "LEASE_EXPIRED")
        if lease.generation != generation:
            return LeaseAuthorization("DENY", "LEASE_GENERATION_MISMATCH")
        if lease.admissible_head != head:
            return LeaseAuthorization("DENY", "LEASE_HEAD_NOT_ADMISSIBLE")
        canonical = CanonicalPath.parse(path)
        if not any(pattern.matches(canonical) for pattern in lease.write_globs):
            return LeaseAuthorization("DENY", "WRITE_OUTSIDE_LEASE")
        return LeaseAuthorization("ALLOW", "AUTHORIZED")

    def authorize_action(self, request: AuthorizationRequest) -> AuthorizationDecision:
        budget_digest, _ = self._budget(request.run_id)
        approved_timeout = (
            V01_MECHANISM_LIMITS.check_timeout_seconds
            if request.action.kind == "check"
            else V01_MECHANISM_LIMITS.ordinary_action_timeout_seconds
        )
        exact_deadline = request.started_at_utc + timedelta(seconds=approved_timeout)
        lease = self._journal.workspace_lease(request.run_id, request.lease_id)
        binding_failure = self._journal.authorization_binding_failure(request)
        reason: AuthorizationReason = binding_failure or "AUTHORIZED"
        if budget_digest != request.budget_digest:
            reason = "REVISION_BINDING_MISMATCH"
        elif (
            request.started_at_utc.tzinfo is None
            or request.deadline_at_utc.tzinfo is None
            or request.deadline_at_utc != exact_deadline
        ):
            reason = "ACTION_DEADLINE_INVALID"
        elif lease is None:
            reason = "LEASE_MISSING"
        elif (
            lease.run_id != request.run_id
            or lease.task_id != request.task_id
            or lease.attempt_id != request.attempt_id
            or lease.task_contract_digest != request.task_contract_digest
        ):
            reason = "RUN_TASK_ATTEMPT_MISMATCH"
        elif lease.state != "ACTIVE" or lease.expires_at <= request.started_at_utc:
            reason = "LEASE_EXPIRED"
        elif lease.generation != request.lease_generation:
            reason = "LEASE_GENERATION_MISMATCH"
        elif lease.admissible_head != request.admissible_head:
            reason = "LEASE_HEAD_NOT_ADMISSIBLE"
        elif request.action.path is not None and not any(
            pattern.matches(CanonicalPath.parse(request.action.path))
            for pattern in lease.write_globs
        ):
            reason = "ACTION_OUTSIDE_LEASE"
        if reason != "AUTHORIZED":
            policy_decision = "DENY"
        elif request.action.kind == "target_cas":
            policy_decision = (
                "REQUIRE_APPROVAL" if request.authority_origin == "ADMISSION" else "DENY"
            )
        elif request.action.kind in {
            "delete",
            "rename",
            "chmod_executable",
            "risky_action",
        }:
            policy_decision = "REQUIRE_APPROVAL"
        else:
            policy_decision = ActionPolicy.default(self._secret_paths).classify(request.action)
        action_classes: dict[str, AuthorizedActionClass] = {
            "read": "READ",
            "search": "SEARCH",
            "patch": "PATCH",
            "check": "DECLARED_CHECK",
            "finish": "FINISH",
            "fail": "FAIL",
            "target_cas": "TARGET_CAS",
        }
        action_class = action_classes.get(request.action.kind, "RISKY")
        binding_json = json.dumps(
            {
                "action_digest": request.action_digest,
                "action_id": request.action_id,
                "admissible_head": request.admissible_head,
                "attempt_id": request.attempt_id,
                "budget_digest": request.budget_digest,
                "deadline_at_utc": request.deadline_at_utc.isoformat(),
                "expected_prestate_digest": request.expected_prestate_digest,
                "lease_generation": request.lease_generation,
                "lease_id": request.lease_id,
                "logical_turn_id": request.logical_turn_id,
                "model_configuration_digest": request.model_configuration_digest,
                "plan_digest": request.plan_digest,
                "policy_digest": request.policy_digest,
                "run_id": request.run_id,
                "target_safety_digest": request.target_safety_digest,
                "task_contract_digest": request.task_contract_digest,
                "task_id": request.task_id,
                "tool_schema_digest": request.tool_schema_digest,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        binding_digest = "sha256:" + sha256(binding_json).hexdigest()
        if policy_decision == "DENY":
            denial_reason: AuthorizationReason = "HARD_DENIAL" if reason == "AUTHORIZED" else reason
            sequence = self._journal.record_authorization_denial(
                request, binding_digest, denial_reason, request.expected_sequence
            )
            return AuthorizationDecision(
                decision="DENY",
                reason=denial_reason,
                run_id=request.run_id,
                task_id=request.task_id,
                attempt_id=request.attempt_id,
                action_id=request.action_id,
                action_digest=request.action_digest,
                binding_digest=binding_digest,
                action_class=action_class,
                approved_timeout_seconds=approved_timeout,
                deadline_at_utc=request.deadline_at_utc,
                persistence="DENIAL_AUDIT",
                effect_intent_id=None,
                pending_action_id=None,
                resulting_sequence=sequence,
            )
        if policy_decision == "REQUIRE_APPROVAL":
            return AuthorizationDecision(
                decision="REQUIRE_APPROVAL",
                reason="APPROVAL_REQUIRED",
                run_id=request.run_id,
                task_id=request.task_id,
                attempt_id=request.attempt_id,
                action_id=request.action_id,
                action_digest=request.action_digest,
                binding_digest=binding_digest,
                action_class=action_class,
                approved_timeout_seconds=approved_timeout,
                deadline_at_utc=request.deadline_at_utc,
                persistence="WITH_PENDING_ACTION",
                effect_intent_id=None,
                pending_action_id=None,
                resulting_sequence=None,
            )
        return AuthorizationDecision(
            decision="ALLOW",
            reason="AUTHORIZED",
            run_id=request.run_id,
            task_id=request.task_id,
            attempt_id=request.attempt_id,
            action_id=request.action_id,
            action_digest=request.action_digest,
            binding_digest=binding_digest,
            action_class=action_class,
            approved_timeout_seconds=approved_timeout,
            deadline_at_utc=request.deadline_at_utc,
            persistence="WITH_EFFECT_INTENT",
            effect_intent_id=None,
            pending_action_id=None,
            resulting_sequence=None,
        )

    def allocate_tranche(
        self,
        task: TaskAuthority,
        progress: ProgressEvidence,
        expected_sequence: AuditSequence,
    ) -> TrancheDecision:
        state = self._journal.task_budget_state(task.run_id, task.task_id)
        progressed = progress_from_checks(
            progress.previous,
            progress.current,
            progress.previous_lifecycle,
            progress.current_lifecycle,
        )
        remaining = V01_MECHANISM_LIMITS.task_call_ceiling - state.allocated_calls
        reason: TrancheReason
        if remaining <= 0:
            calls, reason = 0, "TASK_CALL_CEILING"
        elif state.bootstrap_tranches < V01_MECHANISM_LIMITS.bootstrap_tranche_count:
            calls = min(V01_MECHANISM_LIMITS.bootstrap_tranche_calls, remaining)
            reason = "BOOTSTRAP"
        elif not progressed:
            calls, reason = 0, "NO_PROGRESS"
        else:
            calls = min(V01_MECHANISM_LIMITS.renewal_tranche_calls, remaining)
            reason = "OBJECTIVE_PROGRESS"
        return self._journal.allocate_task_tranche(
            task,
            state,
            calls,
            reason,
            progress,
            expected_sequence,
        )
