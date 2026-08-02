from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from typing import Literal, Protocol

from apexcrew.domain.actions import ActionEnvelope
from apexcrew.domain.limits import V01_MECHANISM_LIMITS
from apexcrew.domain.model import (
    LogicalModelTurn,
    ModelBudgetAmounts,
    ModelCounters,
    ModelRequest,
    ModelRequestIntent,
)
from apexcrew.domain.plan import CanonicalPath, GlobPattern, TaskContract
from apexcrew.domain.policy import ActionPolicy
from apexcrew.domain.revisions import BudgetRevisionDocument
from apexcrew.domain.types import (
    AttemptId,
    AuditSequence,
    IntentId,
    RevisionDigest,
    RunId,
    TaskId,
)


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


class Authority(Protocol):
    def authorize_action(self, request: AuthorizationRequest) -> AuthorizationDecision:
        raise NotImplementedError

    def reserve_model_attempt(self, request: ModelReservationRequest) -> ModelReservation:
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

    def reserve_authorized_model_attempt(
        self, request: ModelReservationRequest
    ) -> ModelReservation:
        raise NotImplementedError

    def issue_workspace_lease(
        self,
        lease: WorkspaceLease,
        worker_ceiling: int,
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


class AuthorityService:
    def __init__(self, journal: AuthorityState) -> None:
        self._journal = journal

    def _budget(self, run_id: RunId) -> tuple[RevisionDigest, BudgetRevisionDocument]:
        return self._journal.current_approved_budget(run_id)

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
        _, budget = self._budget(attempt.run_id)
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
            lease, budget.concurrent_worker_ceiling, expected_sequence
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
        elif request.action.kind in {"delete", "rename", "chmod_executable"}:
            policy_decision = "REQUIRE_APPROVAL"
        else:
            policy_decision = ActionPolicy.default().classify(request.action)
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
