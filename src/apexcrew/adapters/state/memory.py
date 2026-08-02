from __future__ import annotations

import json
from base64 import b32encode
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from hmac import compare_digest
from pathlib import Path
from threading import RLock
from typing import TYPE_CHECKING, Literal, cast

if TYPE_CHECKING:
    from apexcrew.application.runtime import RuntimeFault, RuntimeFaultDisposition
    from apexcrew.domain.tools import ToolDenialAudit

from apexcrew.application.control import (
    RepositoryBootstrapAuthorityService,
    TargetAuthorityDigestService,
)
from apexcrew.domain.actions import FailAction, FinishAction
from apexcrew.domain.admission import (
    PrivateRefCasOutcome,
    RefCasIntent,
    RuntimeStartBinding,
    StartGuard,
    StartGuardBinding,
    TargetReservationCreationIntent,
    TargetReservationCreationOutcome,
)
from apexcrew.domain.authority import (
    ActionClass,
    ActionDeadline,
    ActiveRunTimeBoundaryDecision,
    ActiveRunTimeState,
    AtomicAction,
    AttemptLifecycleState,
    AuthorityDenied,
    AuthorizationDecision,
    AuthorizationReason,
    AuthorizationRequest,
    BudgetSettlement,
    BudgetWarning,
    CheckpointKey,
    DispatchAuthorization,
    DispatchCloseCause,
    GlobalBudgetMetric,
    GlobalUsageSnapshot,
    LeaseDenial,
    ModelReservation,
    ModelReservationReason,
    ModelReservationRequest,
    MonotonicClock,
    MonotonicInstant,
    ProgressEvidence,
    ResumeTaskRequest,
    RuntimeAuditStamp,
    TaskAuthority,
    TaskBudgetState,
    TaskCounterSnapshot,
    TaskLifecycleState,
    TaskPauseBinding,
    TaskResumeAllocation,
    TaskResumeDecision,
    TaskStopDecision,
    TimeoutDecision,
    TrancheDecision,
    TrancheReason,
    WorkspaceLease,
    action_deadline_binding,
    crossed_threshold,
    global_ceiling_for,
    model_reservation_amounts,
    normalize_global_budget_metric,
    progress_from_checks,
    task_resume_ids,
)
from apexcrew.domain.commands import (
    ApplicableRevisionDigests,
    ApproveBudgetPayload,
    ApproveModelConfigurationPayload,
    ApprovePlanPayload,
    ApprovePolicyPayload,
    BeginPlanningPayload,
    CommandEnvelope,
    CommandOutcome,
    CreateRunPayload,
    ProposeBudgetPayload,
    ProposeModelConfigurationPayload,
    ProposePolicyPayload,
    PublicRunSnapshot,
    ResumePayload,
    RunStop,
    RuntimeAllowedPhase,
    RuntimeDecision,
    RuntimePermit,
    RuntimeState,
    StartPayload,
)
from apexcrew.domain.coordination import (
    PlanningAuthorization,
    PlanningReadIntent,
    PlanningReadResult,
    PlanningReadSettlement,
    PlanProposal,
    TaskDispatchSelection,
    plan_proposal_from_document,
    task_contract_digest,
    validate_plan_proposal,
)
from apexcrew.domain.effects import (
    AuditEvent,
    EffectIntent,
    EffectResult,
    PlanApproval,
    ReservationObservation,
    RunRecord,
    RunRefRecord,
    StateCommitFault,
    StateConflict,
    TargetReservation,
    canonical_json,
    classify_reservation_creation,
    sha256_digest,
)
from apexcrew.domain.limits import V01_MECHANISM_LIMITS
from apexcrew.domain.model import (
    CommittedModelTurn,
    LogicalModelTurn,
    LogicalTurnId,
    ModelBudgetAmounts,
    ModelCompletion,
    ModelCounters,
    ModelDispatchResult,
    ModelRecoveryBinding,
    ModelRequest,
    ModelRequestIntent,
    ProviderAttemptKind,
    ProviderAttemptResult,
    RecoveredModelAction,
    SettledModelAttempt,
)
from apexcrew.domain.plan import CheckDefinition, GlobPattern, TaskContract, may_overlap
from apexcrew.domain.revisions import (
    BudgetRevisionDocument,
    FrozenDocument,
    ModelConfigurationRevisionDocument,
    Sha256DigestText,
    revision_digest,
)
from apexcrew.domain.tools import ActionPreState, ToolIntent, ToolResult
from apexcrew.domain.types import (
    AttemptId,
    AuditSequence,
    CommandStatus,
    IntentId,
    RepositoryId,
    RequestId,
    RevisionDigest,
    RunId,
    RunState,
    RuntimeOwnerId,
    TaskId,
)
from apexcrew.domain.worker import (
    PendingActionFreeze,
    WorkerActionRecord,
    WorkerAttemptRecord,
    WorkerAttemptSnapshot,
    WorkerTaskRecord,
    WorkerTurnBinding,
    bounded_worker_feedback,
    pending_worker_action_id,
    terminal_worker_effects,
    validate_authorized_worker_action,
)

_EXECUTION_REVISION_STATES = frozenset(
    {
        RunState.ACTIVE,
        RunState.VERIFYING_RUN,
        RunState.READY_FOR_APPROVAL,
        RunState.APPLYING,
        RunState.PAUSED,
        RunState.INDETERMINATE,
    }
)


def _validate_draft_reservation(
    run_id: RunId,
    repository_id: RepositoryId,
    repository_instance_digest: Sha256DigestText,
    reservation: TargetReservation,
) -> None:
    oid = str(reservation.pinned_target_oid)
    if (
        reservation.run_id != run_id
        or not str(repository_id)
        or reservation.phase != "ALLOCATED"
        or reservation.admin_entry_name is not None
        or reservation.admin_binding_digest is not None
        or not reservation.target_ref.startswith("refs/heads/")
        or reservation.target_ref == "refs/heads/"
        or any(character.isspace() or character == "\x00" for character in reservation.target_ref)
        or len(oid) != 40
        or any(character not in "0123456789abcdef" for character in oid)
        or not reservation.path.is_absolute()
        or reservation.path.name != reservation.reservation_id
        or reservation.path.parent.name != "reservations"
    ):
        raise StateConflict("TARGET_RESERVATION_BINDING_INVALID")
    if (
        len(repository_instance_digest) != 71
        or not repository_instance_digest.startswith("sha256:")
        or any(character not in "0123456789abcdef" for character in repository_instance_digest[7:])
    ):
        raise StateConflict("REPOSITORY_INSTANCE_DIGEST_INVALID")


def _target_authority_digest(run: RunRecord, reservation: TargetReservation) -> Sha256DigestText:
    return sha256_digest(
        canonical_json(
            {
                "pinned_target_oid": reservation.pinned_target_oid,
                "repository_id": run.repository_id,
                "repository_instance_digest": run.repository_instance_digest,
                "reservation_id": reservation.reservation_id,
                "reservation_path": str(reservation.path),
                "reservation_pinned_target_oid": reservation.pinned_target_oid,
                "target_ref": reservation.target_ref,
                "target_safety_digest": reservation.admin_binding_digest,
            }
        )
    )


def _new_target_reservation_creation_intent(
    run: RunRecord,
    reservation: TargetReservation,
    expected_sequence: AuditSequence,
) -> TargetReservationCreationIntent:
    return TargetReservationCreationIntent(
        intent_id=IntentId(
            f"target-reservation-intent:{run.run_id}:{reservation.reservation_id}:"
            f"{expected_sequence + 1}"
        ),
        run_id=run.run_id,
        reservation_id=reservation.reservation_id,
        repository_id=run.repository_id,
        target_ref=reservation.target_ref,
        pinned_target_oid=reservation.pinned_target_oid,
        reservation_path=str(reservation.path),
        repository_instance_digest=run.repository_instance_digest,
        applicable_revision_digests=ApplicableRevisionDigests(
            plan_digest=run.current_plan_digest,
            policy_digest=run.current_policy_digest,
            budget_digest=run.current_budget_digest,
            model_configuration_digest=run.current_model_configuration_digest,
        ),
        target_authority_digest=_target_authority_digest(run, reservation),
        idempotency_key=(
            f"target-reservation-create:{run.run_id}:{reservation.reservation_id}:"
            f"{expected_sequence + 1}"
        ),
        recorded_sequence=AuditSequence(expected_sequence + 1),
    )


def _validate_reservation_outcome(
    intent: TargetReservationCreationIntent,
    outcome: TargetReservationCreationOutcome,
) -> None:
    if outcome.intent_id != intent.intent_id or outcome.run_id != intent.run_id:
        raise StateConflict("TARGET_RESERVATION_OUTCOME_BINDING_MISMATCH")
    if outcome.result_class == "REGISTERED_LOCKED":
        if classify_reservation_creation(outcome.observed) != "SETTLE":
            raise StateConflict("TARGET_RESERVATION_SUCCESS_NOT_EXACT")
        if (
            outcome.observed.admin_entry_name is None
            or outcome.observed.admin_binding_digest is None
        ):
            raise StateConflict("TARGET_RESERVATION_ADMIN_BINDING_MISSING")


def _command_run_id(command: CommandEnvelope, outcome: CommandOutcome) -> RunId:
    payload_run_id = getattr(command.payload, "run_id", None)
    run_id = outcome.run_id if payload_run_id is None else RunId(payload_run_id)
    if run_id is None or outcome.run_id != run_id:
        raise StateConflict("COMMAND_OUTCOME_RUN_MISMATCH")
    return run_id


def _command_digest(command: CommandEnvelope) -> str:
    return sha256_digest(canonical_json(command.model_dump(mode="json")))


def _approval_confirmation_code(
    command_kind: str,
    run_id: RunId,
    revision_class: str,
    revision_digest_value: RevisionDigest,
) -> str:
    payload = canonical_json(
        {
            "command_kind": command_kind,
            "revision_class": revision_class,
            "revision_digest": revision_digest_value,
            "run_id": run_id,
        }
    ).encode("utf-8")
    return b32encode(sha256(payload).digest()).decode("ascii")[:6]


def _run_revision_digest(run: RunRecord, revision_class: str) -> RevisionDigest | None:
    return {
        "PLAN": run.current_plan_digest,
        "POLICY": run.current_policy_digest,
        "BUDGET": run.current_budget_digest,
        "MODEL_CONFIGURATION": run.current_model_configuration_digest,
    }[revision_class]


def _replace_run_revision(
    run: RunRecord,
    revision_class: str,
    digest: RevisionDigest,
    *,
    return_to_draft: bool = False,
) -> RunRecord:
    if revision_class == "PLAN":
        result = replace(run, current_plan_digest=digest)
    elif revision_class == "POLICY":
        result = replace(run, current_policy_digest=digest)
    elif revision_class == "BUDGET":
        result = replace(run, current_budget_digest=digest)
    elif revision_class == "MODEL_CONFIGURATION":
        result = replace(run, current_model_configuration_digest=digest)
    else:
        raise StateConflict("REVISION_CLASS_INVALID")
    if return_to_draft:
        result = replace(result, state=RunState.DRAFT, current_plan_digest=None)
    return result


def _outcome_json(outcome: CommandOutcome) -> str:
    return canonical_json(outcome.model_dump(mode="json"))


def _json_object(value: str, error_code: str = "STORED_JSON_OBJECT_REQUIRED") -> dict[str, object]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as error:
        raise StateConflict(error_code) from error
    if not isinstance(parsed, dict):
        raise StateConflict(error_code)
    return parsed


def _require_canonical_json_object(value: str, error_code: str) -> None:
    if canonical_json(_json_object(value, error_code)) != value:
        raise StateConflict(error_code)


@dataclass(frozen=True, slots=True)
class _ReservationEvaluation:
    reason: ModelReservationReason | None
    budget: BudgetRevisionDocument
    amounts: ModelBudgetAmounts
    run_counters: ModelCounters
    task_counters: TaskBudgetState | None
    planning_requests: int


class _LeaseDenied(RuntimeError):
    def __init__(self, denial: LeaseDenial) -> None:
        super().__init__(denial.reason)
        self.denial = denial


class _ResumeStale(RuntimeError):
    def __init__(self, decision: TaskResumeDecision) -> None:
        super().__init__(decision.failed_invariant)
        self.decision = decision


class InMemoryStateStore:
    def __init__(self, monotonic_clock: MonotonicClock | None = None) -> None:
        self._command_receipts: dict[str, tuple[str, RunId, str, str, AuditSequence]] = {}
        self._audit_events: dict[RunId, list[tuple[AuditSequence, AuditEvent]]] = {}
        self._sequences: dict[RunId, AuditSequence] = {}
        self._effect_intents: dict[IntentId, EffectIntent] = {}
        self._effect_results: dict[IntentId, EffectResult] = {}
        self._action_deadlines: dict[IntentId, ActionDeadline] = {}
        self._timeout_decisions: dict[IntentId, TimeoutDecision] = {}
        self._indeterminate_effect_intents: set[IntentId] = set()
        self._model_turns: dict[LogicalTurnId, LogicalModelTurn | CommittedModelTurn] = {}
        self._model_attempt_numbers: dict[tuple[RunId, LogicalTurnId, int], IntentId] = {}
        self._model_attempts: dict[IntentId, ModelRequestIntent | SettledModelAttempt] = {}
        self._model_counters: dict[RunId, ModelCounters] = {}
        self._runs: dict[RunId, RunRecord] = {}
        self._target_reservations: dict[str, TargetReservation] = {}
        self._bootstrap_inputs: dict[RunId, tuple[str, tuple[str, ...], tuple[str, ...]]] = {}
        self._revision_documents: dict[
            tuple[RunId, str, RevisionDigest], tuple[FrozenDocument, AuditSequence, str]
        ] = {}
        self._revision_approvals: dict[
            tuple[RunId, str, RevisionDigest], tuple[str, AuditSequence, str]
        ] = {}
        self._pending_revision_replacements: dict[
            tuple[RunId, str], tuple[RevisionDigest, AuditSequence]
        ] = {}
        self._runtime_permits: dict[tuple[RunId, int], RuntimePermit] = {}
        self._runtime_progress_generations: dict[RunId, int] = {}
        self._runtime_owners: dict[RunId, tuple[RuntimeOwnerId, int]] = {}
        self._runtime_barriers: dict[RunId, tuple[str | None, str, str | None]] = {}
        self._runtime_interrupts: dict[RunId, tuple[str, str, int, str]] = {}
        self._runtime_delivery_stops: dict[tuple[RunId, int], RunStop] = {}
        self._runtime_faults: dict[RunId, object] = {}
        self._runtime_recorded_stop_reasons: dict[RunId, str] = {}
        self._approved_budgets: dict[RunId, tuple[RevisionDigest, BudgetRevisionDocument]] = {}
        self._plan_proposals: dict[tuple[RunId, RevisionDigest], PlanProposal] = {}
        self._plan_task_contracts: dict[RevisionDigest, tuple[TaskContract, ...]] = {}
        self._plan_dependency_edges: dict[RevisionDigest, tuple[tuple[TaskId, TaskId], ...]] = {}
        self._plan_hazard_edges: dict[RevisionDigest, tuple[tuple[TaskId, TaskId], ...]] = {}
        self._plan_run_checks: dict[RevisionDigest, tuple[CheckDefinition, ...]] = {}
        self._plan_approvals: dict[RunId, PlanApproval] = {}
        self._run_refs: dict[tuple[RunId, str], RunRefRecord] = {}
        self._global_usage: dict[tuple[RunId, GlobalBudgetMetric], int | Decimal] = {}
        self._budget_warnings: dict[
            tuple[RunId, RevisionDigest, GlobalBudgetMetric, int], BudgetWarning
        ] = {}
        self._atomic_actions: dict[tuple[RunId, str], AtomicAction] = {}
        self._workspace_leases: dict[tuple[RunId, str], WorkspaceLease] = {}
        self._authorization_denials: dict[tuple[RunId, str], tuple[str, AuthorizationReason]] = {}
        self._task_budget_counters: dict[tuple[RunId, TaskId], TaskBudgetState] = {}
        self._planning_request_counts: dict[RunId, int] = {}
        self._planning_returned_bytes: dict[RunId, int] = {}
        self._dispatch_close_causes: dict[RunId, tuple[str, ...]] = {}
        self._new_dispatch_open: dict[RunId, bool] = {}
        self._active_run_times: dict[RunId, ActiveRunTimeState] = {}
        self._task_tranches: dict[
            tuple[RunId, TaskId, str], tuple[AttemptId, int, int, str, str]
        ] = {}
        self._tasks: dict[
            tuple[RunId, TaskId], tuple[TaskLifecycleState, str | None, int | None]
        ] = {}
        self._attempts: dict[tuple[RunId, AttemptId], tuple[TaskId, AttemptLifecycleState]] = {}
        self._task_checkpoints: dict[
            tuple[RunId, TaskId], list[tuple[CheckpointKey, RevisionDigest]]
        ] = {}
        self._task_invalid_actions: dict[
            tuple[RunId, TaskId], list[tuple[AttemptId, str, RevisionDigest]]
        ] = {}
        self._task_pauses: dict[tuple[RunId, TaskId], TaskPauseBinding] = {}
        self._active_task_pauses: set[tuple[RunId, TaskId]] = set()
        self._task_resume_metadata: dict[
            tuple[RunId, TaskId], tuple[int, tuple[str, ...], tuple[str, ...]]
        ] = {}
        self._trusted_task_repairs: dict[tuple[RunId, TaskId, int, str], str] = {}
        self._task_resume_allocations: dict[str, TaskResumeAllocation] = {}
        self._worker_attempts: dict[AttemptId, WorkerAttemptRecord] = {}
        self._worker_bindings: dict[AttemptId, WorkerTurnBinding] = {}
        self._worker_actions: dict[str, WorkerActionRecord] = {}
        self._worker_turn_actions: dict[tuple[AttemptId, LogicalTurnId], str] = {}
        self._monotonic_clock = monotonic_clock
        self._lock = RLock()
        self._fail_next_commit_after_state_write = False

    def _copied(self) -> InMemoryStateStore:
        copied = object.__new__(InMemoryStateStore)
        copied._command_receipts = deepcopy(self._command_receipts)
        copied._audit_events = deepcopy(self._audit_events)
        copied._sequences = self._sequences.copy()
        copied._effect_intents = self._effect_intents.copy()
        copied._effect_results = self._effect_results.copy()
        copied._action_deadlines = self._action_deadlines.copy()
        copied._timeout_decisions = self._timeout_decisions.copy()
        copied._indeterminate_effect_intents = self._indeterminate_effect_intents.copy()
        copied._model_turns = self._model_turns.copy()
        copied._model_attempt_numbers = self._model_attempt_numbers.copy()
        copied._model_attempts = self._model_attempts.copy()
        copied._model_counters = self._model_counters.copy()
        copied._runs = self._runs.copy()
        copied._target_reservations = self._target_reservations.copy()
        copied._bootstrap_inputs = self._bootstrap_inputs.copy()
        copied._revision_documents = self._revision_documents.copy()
        copied._revision_approvals = self._revision_approvals.copy()
        copied._pending_revision_replacements = self._pending_revision_replacements.copy()
        copied._runtime_permits = self._runtime_permits.copy()
        copied._runtime_progress_generations = self._runtime_progress_generations.copy()
        copied._runtime_owners = self._runtime_owners.copy()
        copied._runtime_barriers = self._runtime_barriers.copy()
        copied._runtime_interrupts = self._runtime_interrupts.copy()
        copied._runtime_delivery_stops = self._runtime_delivery_stops.copy()
        copied._runtime_faults = self._runtime_faults.copy()
        copied._runtime_recorded_stop_reasons = self._runtime_recorded_stop_reasons.copy()
        copied._approved_budgets = self._approved_budgets.copy()
        copied._plan_proposals = self._plan_proposals.copy()
        copied._plan_task_contracts = self._plan_task_contracts.copy()
        copied._plan_dependency_edges = self._plan_dependency_edges.copy()
        copied._plan_hazard_edges = self._plan_hazard_edges.copy()
        copied._plan_run_checks = self._plan_run_checks.copy()
        copied._plan_approvals = self._plan_approvals.copy()
        copied._run_refs = self._run_refs.copy()
        copied._global_usage = self._global_usage.copy()
        copied._budget_warnings = self._budget_warnings.copy()
        copied._atomic_actions = self._atomic_actions.copy()
        copied._workspace_leases = self._workspace_leases.copy()
        copied._authorization_denials = self._authorization_denials.copy()
        copied._task_budget_counters = self._task_budget_counters.copy()
        copied._planning_request_counts = self._planning_request_counts.copy()
        copied._planning_returned_bytes = self._planning_returned_bytes.copy()
        copied._dispatch_close_causes = self._dispatch_close_causes.copy()
        copied._new_dispatch_open = self._new_dispatch_open.copy()
        copied._active_run_times = self._active_run_times.copy()
        copied._task_tranches = self._task_tranches.copy()
        copied._tasks = self._tasks.copy()
        copied._attempts = self._attempts.copy()
        copied._task_checkpoints = deepcopy(self._task_checkpoints)
        copied._task_invalid_actions = deepcopy(self._task_invalid_actions)
        copied._task_pauses = self._task_pauses.copy()
        copied._active_task_pauses = self._active_task_pauses.copy()
        copied._task_resume_metadata = self._task_resume_metadata.copy()
        copied._trusted_task_repairs = self._trusted_task_repairs.copy()
        copied._task_resume_allocations = self._task_resume_allocations.copy()
        copied._worker_attempts = self._worker_attempts.copy()
        copied._worker_bindings = self._worker_bindings.copy()
        copied._worker_actions = self._worker_actions.copy()
        copied._worker_turn_actions = self._worker_turn_actions.copy()
        copied._monotonic_clock = self._monotonic_clock
        copied._lock = self._lock
        copied._fail_next_commit_after_state_write = False
        return copied

    def _publish(self, copied: InMemoryStateStore) -> None:
        self._command_receipts = copied._command_receipts
        self._audit_events = copied._audit_events
        self._sequences = copied._sequences
        self._effect_intents = copied._effect_intents
        self._effect_results = copied._effect_results
        self._action_deadlines = copied._action_deadlines
        self._timeout_decisions = copied._timeout_decisions
        self._indeterminate_effect_intents = copied._indeterminate_effect_intents
        self._model_turns = copied._model_turns
        self._model_attempt_numbers = copied._model_attempt_numbers
        self._model_attempts = copied._model_attempts
        self._model_counters = copied._model_counters
        self._runs = copied._runs
        self._target_reservations = copied._target_reservations
        self._bootstrap_inputs = copied._bootstrap_inputs
        self._revision_documents = copied._revision_documents
        self._revision_approvals = copied._revision_approvals
        self._pending_revision_replacements = copied._pending_revision_replacements
        self._runtime_permits = copied._runtime_permits
        self._runtime_progress_generations = copied._runtime_progress_generations
        self._runtime_owners = copied._runtime_owners
        self._runtime_barriers = copied._runtime_barriers
        self._runtime_interrupts = copied._runtime_interrupts
        self._runtime_delivery_stops = copied._runtime_delivery_stops
        self._runtime_faults = copied._runtime_faults
        self._runtime_recorded_stop_reasons = copied._runtime_recorded_stop_reasons
        self._approved_budgets = copied._approved_budgets
        self._plan_proposals = copied._plan_proposals
        self._plan_task_contracts = copied._plan_task_contracts
        self._plan_dependency_edges = copied._plan_dependency_edges
        self._plan_hazard_edges = copied._plan_hazard_edges
        self._plan_run_checks = copied._plan_run_checks
        self._plan_approvals = copied._plan_approvals
        self._run_refs = copied._run_refs
        self._global_usage = copied._global_usage
        self._budget_warnings = copied._budget_warnings
        self._atomic_actions = copied._atomic_actions
        self._workspace_leases = copied._workspace_leases
        self._authorization_denials = copied._authorization_denials
        self._task_budget_counters = copied._task_budget_counters
        self._planning_request_counts = copied._planning_request_counts
        self._planning_returned_bytes = copied._planning_returned_bytes
        self._dispatch_close_causes = copied._dispatch_close_causes
        self._new_dispatch_open = copied._new_dispatch_open
        self._active_run_times = copied._active_run_times
        self._task_tranches = copied._task_tranches
        self._tasks = copied._tasks
        self._attempts = copied._attempts
        self._task_checkpoints = copied._task_checkpoints
        self._task_invalid_actions = copied._task_invalid_actions
        self._task_pauses = copied._task_pauses
        self._active_task_pauses = copied._active_task_pauses
        self._task_resume_metadata = copied._task_resume_metadata
        self._trusted_task_repairs = copied._trusted_task_repairs
        self._task_resume_allocations = copied._task_resume_allocations
        self._worker_attempts = copied._worker_attempts
        self._worker_bindings = copied._worker_bindings
        self._worker_actions = copied._worker_actions
        self._worker_turn_actions = copied._worker_turn_actions

    def _commit_state_and_event(
        self,
        *,
        run_id: RunId,
        expected_sequence: AuditSequence,
        event: AuditEvent,
        mutate: Callable[[InMemoryStateStore], None],
        runtime_now: MonotonicInstant | None = None,
    ) -> AuditSequence:
        return self._commit_state_and_events(
            run_id=run_id,
            expected_sequence=expected_sequence,
            event_factory=lambda: (event,),
            mutate=mutate,
            runtime_now=runtime_now,
        )

    def _commit_state_and_events(
        self,
        *,
        run_id: RunId,
        expected_sequence: AuditSequence,
        event_factory: Callable[[], tuple[AuditEvent, ...]],
        mutate: Callable[[InMemoryStateStore], None],
        runtime_now: MonotonicInstant | None = None,
        runtime_now_factory: Callable[[], MonotonicInstant] | None = None,
        finalize: Callable[[InMemoryStateStore], None] | None = None,
    ) -> AuditSequence:
        with self._lock:
            current = self._sequences.get(run_id, AuditSequence(0))
            if current != expected_sequence:
                raise StateConflict("STALE_SEQUENCE")
            copied = self._copied()
            mutate(copied)
            if self._fail_next_commit_after_state_write:
                self._fail_next_commit_after_state_write = False
                raise StateCommitFault("TEST_FAULT_AFTER_STATE_WRITE")
            events = event_factory()
            if not events:
                raise StateConflict("AUDIT_EVENT_BATCH_EMPTY")
            runtime_state = copied._active_run_times.get(
                run_id, ActiveRunTimeState(run_id, 0, None, None, None)
            )
            committed_events = events
            if runtime_state.open_owner_generation is None:
                if any(
                    event.runtime_owner_generation is not None
                    or event.runtime_monotonic_nanoseconds is not None
                    for event in events
                ):
                    raise StateConflict("RUNTIME_AUDIT_WITHOUT_OWNER")
                copied._active_run_times[run_id] = runtime_state
            else:
                if copied._monotonic_clock is None:
                    raise StateConflict("MONOTONIC_CLOCK_NOT_CONFIGURED")
                if runtime_now_factory is not None:
                    now = runtime_now_factory()
                else:
                    now = copied._monotonic_clock.now() if runtime_now is None else runtime_now
                runtime_state.observed_nanoseconds(now)
                committed_events = tuple(
                    replace(
                        event,
                        runtime_owner_generation=runtime_state.open_owner_generation,
                        runtime_monotonic_nanoseconds=now.nanoseconds,
                    )
                    for event in events
                )
                copied._active_run_times[run_id] = replace(runtime_state, latest_committed_at=now)
            for offset, committed_event in enumerate(committed_events, start=1):
                next_sequence = AuditSequence(expected_sequence + offset)
                copied._audit_events.setdefault(run_id, []).append((next_sequence, committed_event))
            if finalize is not None:
                finalize(copied)
            next_sequence = AuditSequence(expected_sequence + len(committed_events))
            copied._sequences[run_id] = next_sequence
            self._publish(copied)
            return next_sequence

    def record_command(self, command: CommandEnvelope, outcome: CommandOutcome) -> CommandOutcome:
        with self._lock:
            run_id = _command_run_id(command, outcome)
            run = self._runs.get(run_id)
            if run is None:
                raise StateConflict("RUN_NOT_FOUND")
            envelope_digest = _command_digest(command)
            existing = self._command_receipts.get(command.request_id)
            if existing is not None:
                repository_id, stored_run_id, stored_digest, stored_outcome, _ = existing
                if (
                    repository_id == run.repository_id
                    and stored_run_id == run_id
                    and stored_digest == envelope_digest
                ):
                    return CommandOutcome.validate_for_payload(
                        command.payload, _json_object(stored_outcome)
                    )
                return CommandOutcome.for_payload(
                    command.payload,
                    status=CommandStatus.CONFLICT,
                    run_id=run_id,
                    resulting_sequence=self._sequences.get(run_id, AuditSequence(0)),
                    failed_invariant="IDEMPOTENCY_KEY_REUSE",
                )
            expected = AuditSequence(
                0 if command.expected_sequence is None else command.expected_sequence
            )
            committed_sequence = outcome.resulting_sequence
            if committed_sequence is None or committed_sequence != AuditSequence(expected + 1):
                raise StateConflict("COMMAND_OUTCOME_SEQUENCE_MISMATCH")

            def mutate(copied: InMemoryStateStore) -> None:
                copied._command_receipts[command.request_id] = (
                    run.repository_id,
                    run_id,
                    envelope_digest,
                    _outcome_json(outcome),
                    committed_sequence,
                )

            self._commit_state_and_event(
                run_id=run_id,
                expected_sequence=expected,
                event=AuditEvent.kind(
                    "COMMAND_RECORDED",
                    applicable_revision_digests=command.applicable_revision_digests,
                    result_class=outcome.status,
                ),
                mutate=mutate,
            )
            return outcome

    def create_draft_with_reservation(
        self,
        run_id: RunId,
        repository_id: RepositoryId,
        repository_instance_digest: Sha256DigestText,
        reservation: TargetReservation,
    ) -> AuditSequence:
        _validate_draft_reservation(run_id, repository_id, repository_instance_digest, reservation)

        def mutate(copied: InMemoryStateStore) -> None:
            if run_id in copied._runs or reservation.reservation_id in copied._target_reservations:
                raise StateConflict("RUN_OR_TARGET_RESERVATION_DUPLICATE")
            if any(
                existing.path == reservation.path
                for existing in copied._target_reservations.values()
            ):
                raise StateConflict("TARGET_RESERVATION_PATH_DUPLICATE")
            copied._runs[run_id] = RunRecord(
                run_id=run_id,
                repository_id=repository_id,
                repository_instance_digest=repository_instance_digest,
                state=RunState.DRAFT,
                target_ref=reservation.target_ref,
                pinned_target_oid=reservation.pinned_target_oid,
            )
            copied._new_dispatch_open[run_id] = True
            copied._target_reservations[reservation.reservation_id] = reservation

        return self._commit_state_and_event(
            run_id=run_id,
            expected_sequence=AuditSequence(0),
            event=AuditEvent.kind("RUN_DRAFT_AND_TARGET_RESERVATION_ALLOCATED"),
            mutate=mutate,
        )

    def run_record(self, run_id: RunId) -> RunRecord:
        with self._lock:
            try:
                return self._runs[run_id]
            except KeyError as error:
                raise StateConflict("RUN_NOT_FOUND") from error

    def target_reservation(self, reservation_id: str) -> TargetReservation:
        with self._lock:
            try:
                return self._target_reservations[reservation_id]
            except KeyError as error:
                raise StateConflict("TARGET_RESERVATION_NOT_FOUND") from error

    def _record_or_load_target_reservation_creation_intent(
        self, run_id: RunId, *, expected_sequence: AuditSequence
    ) -> TargetReservationCreationIntent:
        reservation = next(
            (current for current in self._target_reservations.values() if current.run_id == run_id),
            None,
        )
        if reservation is None:
            raise StateConflict("TARGET_RESERVATION_NOT_FOUND")
        if reservation.phase == "CREATION_INTENT_RECORDED":
            effect_intent = next(
                (
                    current
                    for current in self._effect_intents.values()
                    if current.run_id == run_id
                    and current.kind == "target_reservation_creation"
                    and current.intent_id not in self._effect_results
                ),
                None,
            )
            if effect_intent is None:
                raise StateConflict("TARGET_RESERVATION_UNSETTLED_INTENT_REQUIRED")
            return TargetReservationCreationIntent.from_effect_intent(effect_intent)
        if reservation.phase != "ALLOCATED":
            raise StateConflict("TARGET_RESERVATION_CREATION_NOT_ALLOCATED")
        run = self.run_record(run_id)
        creation_intent = _new_target_reservation_creation_intent(
            run, reservation, expected_sequence
        )
        effect = creation_intent.to_effect_intent(AuditSequence(expected_sequence + 1))
        self._validate_effect_intent(effect, expected_sequence)

        def mutate(copied: InMemoryStateStore) -> None:
            if effect.intent_id in copied._effect_intents or any(
                existing.idempotency_key == effect.idempotency_key
                for existing in copied._effect_intents.values()
            ):
                raise StateConflict("EFFECT_INTENT_DUPLICATE")
            copied._effect_intents[effect.intent_id] = effect
            copied._target_reservations[reservation.reservation_id] = replace(
                reservation, phase="CREATION_INTENT_RECORDED"
            )

        self._commit_state_and_event(
            run_id=run_id,
            expected_sequence=expected_sequence,
            event=AuditEvent.kind("TARGET_RESERVATION_CREATION_INTENT_RECORDED"),
            mutate=mutate,
        )
        return creation_intent

    def unsettled_target_reservation_creation(
        self, run_id: RunId
    ) -> TargetReservationCreationIntent:
        reservation = next(
            (current for current in self._target_reservations.values() if current.run_id == run_id),
            None,
        )
        if reservation is None or reservation.phase != "CREATION_INTENT_RECORDED":
            raise StateConflict("TARGET_RESERVATION_UNSETTLED_INTENT_REQUIRED")
        effect = next(
            (
                current
                for current in self._effect_intents.values()
                if current.run_id == run_id
                and current.kind == "target_reservation_creation"
                and current.intent_id not in self._effect_results
            ),
            None,
        )
        if effect is None:
            raise StateConflict("TARGET_RESERVATION_UNSETTLED_INTENT_REQUIRED")
        return TargetReservationCreationIntent.from_effect_intent(effect)

    def _settle_target_reservation_creation(
        self,
        intent: TargetReservationCreationIntent,
        outcome: TargetReservationCreationOutcome,
        *,
        expected_sequence: AuditSequence,
    ) -> AuditSequence:
        effect_result = outcome.to_effect_result(AuditSequence(expected_sequence + 1))
        _validate_reservation_outcome(intent, outcome)

        def mutate(copied: InMemoryStateStore) -> None:
            reservation = copied.target_reservation(intent.reservation_id)
            stored_effect = copied._effect_intents.get(intent.intent_id)
            if (
                reservation.run_id != intent.run_id
                or reservation.phase != "CREATION_INTENT_RECORDED"
                or stored_effect is None
                or stored_effect != intent.to_effect_intent(stored_effect.recorded_sequence)
                or intent.intent_id in copied._effect_results
            ):
                raise StateConflict("TARGET_RESERVATION_INTENT_BINDING_MISMATCH")
            copied._effect_results[intent.intent_id] = effect_result
            if outcome.result_class == "REGISTERED_LOCKED":
                copied._target_reservations[intent.reservation_id] = replace(
                    reservation,
                    phase="REGISTERED_LOCKED",
                    admin_entry_name=outcome.observed.admin_entry_name,
                    admin_binding_digest=outcome.observed.admin_binding_digest,
                )
                next_state = RunState.DRAFT
            elif outcome.result_class == "CONFLICT":
                copied._target_reservations[intent.reservation_id] = replace(
                    reservation,
                    phase="ALLOCATED",
                    admin_entry_name=None,
                    admin_binding_digest=None,
                )
                next_state = RunState.DRAFT
            else:
                next_state = RunState.INDETERMINATE
            copied._runs[intent.run_id] = replace(copied._runs[intent.run_id], state=next_state)

        return self._commit_state_and_event(
            run_id=intent.run_id,
            expected_sequence=expected_sequence,
            event=AuditEvent.kind("TARGET_RESERVATION_CREATION_SETTLED"),
            mutate=mutate,
        )

    def audit_sequence(self, run_id: RunId) -> AuditSequence:
        with self._lock:
            return self._sequences.get(run_id, AuditSequence(0))

    def install_running_attempt_for_test(self, task: TaskAuthority) -> None:
        with self._lock:
            task_key = (task.run_id, task.task_id)
            current_task = self._tasks.get(task_key)
            if current_task is not None and current_task[0] == "PAUSED":
                raise StateConflict("TASK_NOT_STARTABLE")
            attempt_key = (task.run_id, task.attempt_id)
            current_attempt = self._attempts.get(attempt_key)
            if current_attempt is not None and current_attempt != (task.task_id, "RUNNING"):
                raise StateConflict("ATTEMPT_NOT_STARTABLE")
            self._tasks[task_key] = ("ACTIVE", None, None)
            if current_attempt is None:
                self._attempts[attempt_key] = (task.task_id, "RUNNING")

    def install_worker_attempt_for_test(self, binding: WorkerTurnBinding) -> None:
        with self._lock:
            current_budget = self._approved_budgets.get(binding.run_id)
            if current_budget is None or current_budget[0] != binding.budget_digest:
                raise StateConflict("CURRENT_BUDGET_BINDING_MISMATCH")
            if binding.attempt_id in self._worker_attempts:
                raise StateConflict("WORKER_ATTEMPT_DUPLICATE")
            now = datetime.now(UTC)
            self._tasks[(binding.run_id, binding.task_id)] = ("ACTIVE", None, None)
            self._attempts[(binding.run_id, binding.attempt_id)] = (
                binding.task_id,
                "RUNNING",
            )
            self._worker_attempts[binding.attempt_id] = WorkerAttemptRecord(
                run_id=binding.run_id,
                task_id=binding.task_id,
                attempt_id=binding.attempt_id,
                plan_digest=binding.plan_digest,
                policy_digest=binding.policy_digest,
                budget_digest=binding.budget_digest,
                model_configuration_digest=binding.model_configuration_digest,
                tool_schema_digest=binding.tool_schema_digest,
                target_safety_digest=binding.target_safety_digest,
                credential_profile=binding.credential_profile,
                task_contract_digest=binding.task_contract_digest,
                base_run_head_oid=binding.admissible_head,
                worker_slot=f"worker-{binding.lease_generation}",
                state="RUNNING",
                created_sequence=self._sequences.get(binding.run_id, AuditSequence(0)),
            )
            self._worker_bindings[binding.attempt_id] = binding
            self._workspace_leases[(binding.run_id, binding.lease_id)] = WorkspaceLease(
                lease_id=binding.lease_id,
                run_id=binding.run_id,
                task_id=binding.task_id,
                attempt_id=binding.attempt_id,
                generation=binding.lease_generation,
                base_head=binding.admissible_head,
                admissible_head=binding.admissible_head,
                task_contract_digest=binding.task_contract_digest,
                write_globs=(GlobPattern.parse("**"),),
                sensitivity_globs=(GlobPattern.parse("**"),),
                issued_at=now,
                expires_at=now + timedelta(minutes=30),
                state="ACTIVE",
            )

    def current_worker_turn_binding(self, attempt_id: AttemptId) -> WorkerTurnBinding:
        with self._lock:
            binding = self._worker_bindings.get(attempt_id)
            attempt = self._worker_attempts.get(attempt_id)
            lease = (
                None
                if binding is None
                else self._workspace_leases.get((binding.run_id, binding.lease_id))
            )
        if binding is None or attempt is None:
            raise StateConflict("WORKER_ATTEMPT_NOT_FOUND")
        if attempt.state != "RUNNING" or lease is None or lease.state != "ACTIVE":
            raise StateConflict("WORKER_ATTEMPT_NOT_RUNNABLE")
        return binding

    def attempt(self, attempt_id: AttemptId) -> WorkerAttemptRecord:
        with self._lock:
            attempt = self._worker_attempts.get(attempt_id)
        if attempt is None:
            raise StateConflict("WORKER_ATTEMPT_NOT_FOUND")
        return attempt

    def attempts_for_task(self, task_id: TaskId) -> tuple[WorkerAttemptRecord, ...]:
        with self._lock:
            return tuple(
                sorted(
                    (
                        attempt
                        for attempt in self._worker_attempts.values()
                        if attempt.task_id == task_id
                    ),
                    key=lambda attempt: (attempt.created_sequence, attempt.attempt_id),
                )
            )

    def active_lease_for_task(self, task_id: TaskId) -> WorkspaceLease | None:
        with self._lock:
            active = tuple(
                lease
                for lease in self._workspace_leases.values()
                if lease.task_id == task_id and lease.state == "ACTIVE"
            )
        if len(active) > 1:
            raise StateConflict("MULTIPLE_ACTIVE_TASK_LEASES")
        return None if not active else active[0]

    def invalid_action_count(self, task_id: TaskId) -> int:
        with self._lock:
            return sum(
                len(history)
                for (
                    candidate_run_id,
                    candidate_task_id,
                ), history in self._task_invalid_actions.items()
                if candidate_task_id == task_id and candidate_run_id in self._runs
            )

    def task_record(self, task_id: TaskId) -> WorkerTaskRecord:
        with self._lock:
            matches = tuple(
                (run_id, value)
                for (run_id, candidate_task_id), value in self._tasks.items()
                if candidate_task_id == task_id
            )
        if len(matches) != 1:
            raise StateConflict("WORKER_TASK_NOT_FOUND_OR_AMBIGUOUS")
        run_id, value = matches[0]
        terminal_results = tuple(
            result
            for action in self._worker_actions.values()
            if action.run_id == run_id
            and action.task_id == task_id
            and action.result_intent_id is not None
            and (result := self._effect_results.get(action.result_intent_id)) is not None
            and result.result_class in {"WORKER_FINISHED", "WORKER_FAILED"}
        )
        if terminal_results:
            latest = max(terminal_results, key=lambda result: result.settled_sequence)
            state = "SUCCEEDED" if latest.result_class == "WORKER_FINISHED" else "FAILED"
            return WorkerTaskRecord(run_id, task_id, state, None)
        return WorkerTaskRecord(run_id, task_id, value[0], value[1])

    def next_dispatchable(self, run_id: RunId) -> TaskDispatchSelection | RuntimeDecision:
        with self._lock:
            run = self._runs.get(run_id)
            sequence = self._sequences.get(run_id, AuditSequence(0))
            if run is None:
                raise StateConflict("RUN_NOT_FOUND")
            if run.state != RunState.ACTIVE or not self._new_dispatch_open.get(run_id, True):
                return RuntimeDecision.pause("RUN_DISPATCH_CLOSED", sequence)
            revisions = ApplicableRevisionDigests(
                plan_digest=run.current_plan_digest,
                policy_digest=run.current_policy_digest,
                budget_digest=run.current_budget_digest,
                model_configuration_digest=run.current_model_configuration_digest,
            )
            if (
                revisions.plan_digest is None
                or revisions.policy_digest is None
                or revisions.budget_digest is None
                or revisions.model_configuration_digest is None
            ):
                return RuntimeDecision.pause("REVISION_BINDING_MISMATCH", sequence)
            runnable = tuple(
                sorted(
                    (
                        attempt
                        for attempt in self._worker_attempts.values()
                        if attempt.run_id == run_id and attempt.state == "RUNNING"
                    ),
                    key=lambda item: (item.created_sequence, item.attempt_id),
                )
            )
            for attempt in runnable:
                binding = self._worker_bindings[attempt.attempt_id]
                lease = self._workspace_leases.get((run_id, binding.lease_id))
                if lease is not None and lease.state == "ACTIVE":
                    return TaskDispatchSelection(
                        dispatch_id=f"existing:{attempt.attempt_id}",
                        run_id=run_id,
                        task_id=attempt.task_id,
                        task_contract_digest=attempt.task_contract_digest,
                        base_run_head_oid=attempt.base_run_head_oid,
                        applicable_revision_digests=binding.applicable_revision_digests,
                        target_safety_digest=binding.target_safety_digest,
                        credential_profile=binding.credential_profile,
                        resume_allocation_id=None,
                        reserved_attempt_id=None,
                        expected_sequence=sequence,
                        existing_attempt_id=attempt.attempt_id,
                    )
            budget = self._require_current_budget(run_id, revisions.budget_digest)
            active_count = sum(
                lease.run_id == run_id and lease.state == "ACTIVE"
                for lease in self._workspace_leases.values()
            )
            if active_count >= budget.concurrent_worker_ceiling:
                return RuntimeDecision.pause("CONCURRENT_WORKER_CEILING", sequence)
            contracts = self._plan_task_contracts.get(revisions.plan_digest, ())
            active_task_ids = {
                attempt.task_id
                for attempt in self._worker_attempts.values()
                if attempt.run_id == run_id
                and attempt.state in {"RUNNING", "WAITING_APPROVAL", "VERIFYING"}
            }
            succeeded = {
                attempt.task_id
                for attempt in self._worker_attempts.values()
                if attempt.run_id == run_id and attempt.state == "SUCCEEDED"
            }
            hazards = self._plan_hazard_edges.get(revisions.plan_digest, ())
            for contract in sorted(contracts, key=lambda item: item.task_id):
                task_state = self._tasks.get((run_id, contract.task_id), ("READY", None, None))[0]
                if (
                    task_state != "READY"
                    or not set(contract.dependency_task_ids).issubset(succeeded)
                    or any(
                        contract.task_id in edge and any(item in active_task_ids for item in edge)
                        for edge in hazards
                    )
                ):
                    continue
                counters = self._task_budget_counters.get(
                    (run_id, contract.task_id),
                    TaskBudgetState(run_id=run_id, task_id=contract.task_id),
                )
                attempts = max(
                    counters.attempts,
                    sum(
                        item.run_id == run_id and item.task_id == contract.task_id
                        for item in self._worker_attempts.values()
                    ),
                )
                if attempts >= V01_MECHANISM_LIMITS.task_attempt_ceiling:
                    return RuntimeDecision.pause("TASK_ATTEMPT_CEILING", sequence)
                resume = next(
                    (
                        allocation
                        for allocation in self._task_resume_allocations.values()
                        if allocation.run_id == run_id
                        and allocation.task_id == contract.task_id
                        and allocation.state == "RESERVED"
                    ),
                    None,
                )
                tranche_id = counters.active_tranche_id
                tranche = (
                    None
                    if tranche_id is None
                    else self._task_tranches.get((run_id, contract.task_id, tranche_id))
                )
                if resume is None and (
                    tranche is None or counters.active_tranche_remaining_calls <= 0
                ):
                    continue
                identity = canonical_json(
                    {
                        "run_id": run_id,
                        "sequence": int(sequence),
                        "task_id": contract.task_id,
                    }
                )
                return TaskDispatchSelection(
                    dispatch_id="worker-dispatch-" + sha256(identity.encode()).hexdigest(),
                    run_id=run_id,
                    task_id=contract.task_id,
                    task_contract_digest=task_contract_digest(contract),
                    base_run_head_oid=str(run.pinned_target_oid),
                    applicable_revision_digests=revisions,
                    target_safety_digest=self.target_authority_digest(run_id),
                    credential_profile=None,
                    resume_allocation_id=None if resume is None else resume.allocation_id,
                    reserved_attempt_id=(None if resume is None else resume.reserved_attempt_id),
                    expected_sequence=sequence,
                )
            return RuntimeDecision.pause("NO_DISPATCHABLE_TASK", sequence)

    def create_attempt_with_lease(
        self,
        selection: TaskDispatchSelection,
        *,
        expected_sequence: AuditSequence,
    ) -> WorkerAttemptSnapshot:
        if selection.expected_sequence != expected_sequence:
            raise StateConflict("WORKER_DISPATCH_SEQUENCE_MISMATCH")
        if selection.existing_attempt_id is not None:
            binding = self.current_worker_turn_binding(selection.existing_attempt_id)
            if binding.run_id != selection.run_id or binding.task_id != selection.task_id:
                raise StateConflict("WORKER_EXISTING_ATTEMPT_BINDING_MISMATCH")
            return WorkerAttemptSnapshot(
                binding.run_id,
                binding.task_id,
                binding.attempt_id,
                binding.applicable_revision_digests,
            )
        created: list[WorkerAttemptSnapshot] = []

        def mutate(copied: InMemoryStateStore) -> None:
            run = copied._runs.get(selection.run_id)
            if run is None or run.state != RunState.ACTIVE:
                raise StateConflict("RUN_NOT_DISPATCHABLE")
            if copied.current_revision_digests(selection.run_id) != (
                selection.applicable_revision_digests
            ):
                raise StateConflict("REVISION_BINDING_MISMATCH")
            plan_digest = selection.applicable_revision_digests.plan_digest
            policy_digest = selection.applicable_revision_digests.policy_digest
            budget_digest = selection.applicable_revision_digests.budget_digest
            model_digest = selection.applicable_revision_digests.model_configuration_digest
            if (
                plan_digest is None
                or policy_digest is None
                or budget_digest is None
                or model_digest is None
            ):
                raise StateConflict("REVISION_BINDING_MISMATCH")
            budget = copied._require_current_budget(selection.run_id, budget_digest)
            active = sum(
                lease.run_id == selection.run_id and lease.state == "ACTIVE"
                for lease in copied._workspace_leases.values()
            )
            if active >= budget.concurrent_worker_ceiling:
                raise StateConflict("CONCURRENT_WORKER_CEILING")
            contracts = copied._plan_task_contracts[plan_digest]
            contract = next((item for item in contracts if item.task_id == selection.task_id), None)
            if contract is None or task_contract_digest(contract) != selection.task_contract_digest:
                raise StateConflict("TASK_CONTRACT_BINDING_MISMATCH")
            counters = copied._task_budget_counters.get(
                (selection.run_id, selection.task_id),
                TaskBudgetState(selection.run_id, selection.task_id),
            )
            if counters.attempts >= V01_MECHANISM_LIMITS.task_attempt_ceiling:
                raise StateConflict("TASK_ATTEMPT_CEILING")
            tranche_id = counters.active_tranche_id
            tranche = (
                None
                if tranche_id is None
                else copied._task_tranches.get((selection.run_id, selection.task_id, tranche_id))
            )
            resume = (
                None
                if selection.resume_allocation_id is None
                else copied._task_resume_allocations.get(selection.resume_allocation_id)
            )
            if resume is None and (tranche is None or counters.active_tranche_remaining_calls <= 0):
                raise StateConflict("TASK_ALLOCATION_REQUIRED")
            allocated_attempt_id = (
                resume.reserved_attempt_id if resume is not None else tranche[0]  # type: ignore[index]
            )
            if (
                selection.reserved_attempt_id is not None
                and selection.reserved_attempt_id != allocated_attempt_id
            ):
                raise StateConflict("RESERVED_ATTEMPT_BINDING_MISMATCH")
            attempt_id = AttemptId(allocated_attempt_id)
            if attempt_id in copied._worker_attempts:
                raise StateConflict("WORKER_ATTEMPT_DUPLICATE")
            generation = 1 + max(
                (
                    lease.generation
                    for lease in copied._workspace_leases.values()
                    if lease.run_id == selection.run_id and lease.task_id == selection.task_id
                ),
                default=0,
            )
            lease_id = (
                "worker-lease-"
                + sha256(f"{selection.run_id}:{attempt_id}:{generation}".encode()).hexdigest()
            )
            scope_digest = sha256_digest(
                canonical_json(
                    {
                        "read": [item.value for item in contract.read_globs],
                        "write": [item.value for item in contract.write_globs],
                    }
                )
            )
            snapshot_digest = sha256_digest(
                canonical_json(
                    {
                        "head": selection.base_run_head_oid,
                        "repository_id": run.repository_id,
                        "scope_digest": scope_digest,
                    }
                )
            )
            dependency_basis = sha256_digest(
                canonical_json({"dependencies": list(contract.dependency_task_ids)})
            )
            model_record = copied._revision_documents.get(
                (
                    selection.run_id,
                    "MODEL_CONFIGURATION",
                    model_digest,
                )
            )
            if model_record is None or not isinstance(
                model_record[0], ModelConfigurationRevisionDocument
            ):
                raise StateConflict("MODEL_CONFIGURATION_NOT_FOUND")
            binding = WorkerTurnBinding(
                run_id=selection.run_id,
                task_id=selection.task_id,
                attempt_id=attempt_id,
                tranche_id=str(tranche_id or resume.allocation_id),  # type: ignore[union-attr]
                lease_id=lease_id,
                lease_generation=generation,
                admissible_head=selection.base_run_head_oid,
                task_contract_digest=selection.task_contract_digest,
                plan_digest=plan_digest,
                policy_digest=policy_digest,
                budget_digest=budget_digest,
                model_configuration_digest=model_digest,
                tool_schema_digest=model_record[0].tool_schema_digest,
                target_safety_digest=selection.target_safety_digest,
                credential_profile=selection.credential_profile,
                repository_id=str(run.repository_id),
                snapshot_digest=snapshot_digest,
                scope_digest=scope_digest,
                dependency_fingerprint_basis=dependency_basis,
            )
            copied._tasks[(selection.run_id, selection.task_id)] = ("ACTIVE", None, None)
            copied._attempts[(selection.run_id, attempt_id)] = (
                selection.task_id,
                "RUNNING",
            )
            copied._worker_attempts[attempt_id] = WorkerAttemptRecord(
                selection.run_id,
                selection.task_id,
                attempt_id,
                binding.plan_digest,
                binding.policy_digest,
                binding.budget_digest,
                binding.model_configuration_digest,
                binding.tool_schema_digest,
                binding.target_safety_digest,
                binding.credential_profile,
                binding.task_contract_digest,
                binding.admissible_head,
                f"worker-{active + 1}",
                "RUNNING",
                AuditSequence(expected_sequence + 1),
            )
            copied._worker_bindings[attempt_id] = binding
            now = datetime.now(UTC)
            sensitivity = (
                contract.read_globs
                + contract.dependency_globs
                + tuple(item for check in contract.checks for item in check.input_globs)
            )
            copied._workspace_leases[(selection.run_id, lease_id)] = WorkspaceLease(
                lease_id,
                selection.run_id,
                selection.task_id,
                attempt_id,
                generation,
                selection.base_run_head_oid,
                selection.base_run_head_oid,
                selection.task_contract_digest,
                contract.write_globs,
                sensitivity,
                now,
                now + timedelta(minutes=15),
                "ACTIVE",
            )
            copied._task_budget_counters[(selection.run_id, selection.task_id)] = replace(
                counters, attempts=counters.attempts + 1
            )
            if resume is not None:
                copied._task_resume_allocations[resume.allocation_id] = replace(
                    resume, state="CONSUMED"
                )
            copied._settle_global_usage_for_producer(
                selection.run_id,
                binding.budget_digest,
                GlobalBudgetMetric.CONCURRENT_WORKERS,
                active + 1,
            )
            created.append(
                WorkerAttemptSnapshot(
                    selection.run_id,
                    selection.task_id,
                    attempt_id,
                    binding.applicable_revision_digests,
                )
            )

        self._commit_state_and_event(
            run_id=selection.run_id,
            expected_sequence=expected_sequence,
            event=AuditEvent.kind(
                "WORKER_ATTEMPT_CREATED",
                task_id=selection.task_id,
                attempt_id=selection.reserved_attempt_id,
                applicable_revision_digests=selection.applicable_revision_digests,
                subject_digests=(selection.task_contract_digest,),
            ),
            mutate=mutate,
        )
        return created[0]

    def task_lifecycle_state(self, run_id: RunId, task_id: TaskId) -> TaskLifecycleState:
        with self._lock:
            task = self._tasks.get((run_id, task_id))
        if task is None:
            raise StateConflict("TASK_NOT_FOUND")
        return task[0]

    def attempt_lifecycle_state(
        self, run_id: RunId, attempt_id: AttemptId
    ) -> AttemptLifecycleState:
        with self._lock:
            attempt = self._attempts.get((run_id, attempt_id))
        if attempt is None:
            raise StateConflict("ATTEMPT_NOT_FOUND")
        return attempt[1]

    def current_task_pause(self, run_id: RunId, task_id: TaskId) -> TaskPauseBinding | None:
        key = (run_id, task_id)
        with self._lock:
            pause = self._task_pauses.get(key)
            return pause if pause is not None and key in self._active_task_pauses else None

    def task_counters(self, run_id: RunId, task_id: TaskId) -> TaskCounterSnapshot:
        key = (run_id, task_id)
        with self._lock:
            if key not in self._tasks:
                raise StateConflict("TASK_NOT_FOUND")
            budget = self._task_budget_counters.get(
                key,
                TaskBudgetState(run_id=run_id, task_id=task_id),
            )
            metadata = self._task_resume_metadata.get(key)
            if metadata is None:
                generations = [
                    lease.generation
                    for lease in self._workspace_leases.values()
                    if lease.run_id == run_id and lease.task_id == task_id
                ]
                next_lease_generation = max(generations, default=0) + 1
                failure_digests: tuple[str, ...] = ()
                warning_keys: tuple[str, ...] = ()
            else:
                next_lease_generation, failure_digests, warning_keys = metadata
            return TaskCounterSnapshot(
                run_id=run_id,
                task_id=task_id,
                allocated_calls=budget.allocated_calls,
                model_calls=budget.consumed_calls,
                input_tokens=budget.input_tokens,
                output_tokens=budget.output_tokens,
                cost_reserve_usd=budget.cost_usd,
                attempts=budget.attempts,
                stale_refreshes=budget.stale_refreshes,
                manual_resumes=budget.manual_resumes,
                next_lease_generation=next_lease_generation,
                failure_digests=failure_digests,
                checkpoint_history=tuple(
                    checkpoint for checkpoint, _ in self._task_checkpoints.get(key, ())
                ),
                invalid_action_history=tuple(
                    action_digest for _, action_digest, _ in self._task_invalid_actions.get(key, ())
                ),
                warning_keys=warning_keys,
            )

    def _record_task_pause_binding(
        self,
        *,
        run_id: RunId,
        task_id: TaskId,
        attempt_id: AttemptId,
        pause_sequence: AuditSequence,
        pause_reason: str,
        budget_digest: RevisionDigest,
    ) -> None:
        if run_id not in self._runs:
            return
        key = (run_id, task_id)
        if key in self._active_task_pauses:
            raise StateConflict("TASK_PAUSE_ALREADY_ACTIVE")
        counters = self.task_counters(run_id, task_id)
        run = self._runs[run_id]
        self._task_pauses[key] = TaskPauseBinding(
            run_id=run_id,
            task_id=task_id,
            pause_sequence=pause_sequence,
            pause_reason=pause_reason,
            counter_snapshot_digest=counters.digest,
            previous_attempt_id=attempt_id,
            budget_digest_at_pause=budget_digest,
            applicable_revision_digests_at_pause=ApplicableRevisionDigests(
                plan_digest=run.current_plan_digest,
                policy_digest=run.current_policy_digest,
                budget_digest=run.current_budget_digest,
                model_configuration_digest=run.current_model_configuration_digest,
            ),
        )
        self._active_task_pauses.add(key)
        self._close_new_dispatch(run_id, DispatchCloseCause.TASK_PAUSED)

    def install_task_pause_for_test(
        self,
        pause: TaskPauseBinding,
        counters: TaskCounterSnapshot,
        applicable_revision_digests: ApplicableRevisionDigests,
    ) -> None:
        if (
            counters.run_id != pause.run_id
            or counters.task_id != pause.task_id
            or counters.digest != pause.counter_snapshot_digest
            or pause.applicable_revision_digests_at_pause != applicable_revision_digests
        ):
            raise StateConflict("TASK_PAUSE_COUNTER_BINDING_INVALID")
        with self._lock:
            self._require_current_revisions(pause.run_id, applicable_revision_digests)
            key = (pause.run_id, pause.task_id)
            self._tasks[key] = ("PAUSED", pause.pause_reason, 1)
            self._attempts[(pause.run_id, pause.previous_attempt_id)] = (
                pause.task_id,
                "FAILED",
            )
            tranche_count = (counters.allocated_calls + 7) // 8
            self._task_budget_counters[key] = TaskBudgetState(
                run_id=pause.run_id,
                task_id=pause.task_id,
                allocated_calls=counters.allocated_calls,
                consumed_calls=counters.model_calls,
                input_tokens=counters.input_tokens,
                output_tokens=counters.output_tokens,
                cost_usd=counters.cost_reserve_usd,
                tranche_count=tranche_count,
                bootstrap_tranches=min(2, tranche_count),
                attempts=counters.attempts,
                stale_refreshes=counters.stale_refreshes,
                manual_resumes=counters.manual_resumes,
            )
            self._task_checkpoints[key] = [
                (checkpoint, pause.budget_digest_at_pause)
                for checkpoint in counters.checkpoint_history
            ]
            self._task_invalid_actions[key] = []
            for index, action_digest in enumerate(counters.invalid_action_history, start=1):
                attempt_id = AttemptId(f"resume-history-{pause.task_id}-{index}")
                self._attempts[(pause.run_id, attempt_id)] = (pause.task_id, "FAILED")
                self._task_invalid_actions[key].append(
                    (attempt_id, action_digest, pause.budget_digest_at_pause)
                )
            self._task_resume_metadata[key] = (
                counters.next_lease_generation,
                counters.failure_digests,
                counters.warning_keys,
            )
            self._task_pauses[key] = pause
            self._active_task_pauses.add(key)
            close_cause = (
                DispatchCloseCause.BUDGET_EXHAUSTED
                if pause.pause_reason == "LOWERED_BUDGET_CEILING"
                or pause.budget_ceiling_exhaustions
                else DispatchCloseCause.TASK_PAUSED
            )
            self._close_new_dispatch(pause.run_id, close_cause)
            self._trusted_task_repairs = {
                repair_key: digest
                for repair_key, digest in self._trusted_task_repairs.items()
                if repair_key[:2] != key
            }

    def record_trusted_task_repair_for_test(
        self,
        pause: TaskPauseBinding,
        observation_digest: str,
    ) -> None:
        if not observation_digest:
            raise StateConflict("TASK_REPAIR_OBSERVATION_INVALID")
        with self._lock:
            if self.current_task_pause(pause.run_id, pause.task_id) != pause:
                raise StateConflict("TASK_REPAIR_PAUSE_BINDING_MISMATCH")
            self._trusted_task_repairs[
                (pause.run_id, pause.task_id, pause.pause_sequence, pause.pause_reason)
            ] = observation_digest

    def task_repair_observed(self, pause: TaskPauseBinding) -> bool:
        with self._lock:
            return self.current_task_pause(pause.run_id, pause.task_id) == pause and bool(
                self._trusted_task_repairs.get(
                    (pause.run_id, pause.task_id, pause.pause_sequence, pause.pause_reason)
                )
            )

    def accept_task_resume(
        self,
        request: ResumeTaskRequest,
        pause: TaskPauseBinding,
        counters: TaskCounterSnapshot,
        budget_digest: RevisionDigest,
        usage: GlobalUsageSnapshot,
        calls: int,
    ) -> TaskResumeDecision:
        allocation_id, new_attempt_id = task_resume_ids(
            request,
            pause,
            counters,
            budget_digest,
            calls,
        )
        result: list[TaskResumeDecision] = []

        def mutate(copied: InMemoryStateStore) -> None:
            copied._require_current_revisions(
                request.run_id,
                request.applicable_revision_digests,
            )
            copied._require_current_budget(request.run_id, budget_digest)
            current_pause = copied.current_task_pause(request.run_id, request.task_id)
            current_counters = copied.task_counters(request.run_id, request.task_id)
            current_usage = copied.global_usage_snapshot(request.run_id)
            if (
                current_pause != pause
                or current_counters.digest != counters.digest
                or current_usage != usage
            ):
                raise _ResumeStale(
                    TaskResumeDecision.stale(
                        request.run_id,
                        request.task_id,
                        "TASK_RESUME_COMPARE_AND_SET_FAILED",
                    )
                )
            remaining = V01_MECHANISM_LIMITS.task_call_ceiling - counters.allocated_calls
            if not 1 <= calls <= min(V01_MECHANISM_LIMITS.renewal_tranche_calls, remaining):
                raise StateConflict("TASK_RESUME_ALLOCATION_INVALID")
            key = (request.run_id, request.task_id)
            current_budget = copied._task_budget_counters[key]
            copied._task_budget_counters[key] = replace(
                current_budget,
                manual_resumes=current_budget.manual_resumes + 1,
            )
            copied._task_resume_allocations[allocation_id] = TaskResumeAllocation(
                allocation_id=allocation_id,
                run_id=request.run_id,
                task_id=request.task_id,
                reserved_attempt_id=new_attempt_id,
                budget_digest=budget_digest,
                applicable_revision_digests=request.applicable_revision_digests,
                allocated_calls=calls,
                state="RESERVED",
                created_sequence=AuditSequence(request.expected_sequence + 1),
            )
            if copied._tasks.get(key, (None, None, None))[0] != "PAUSED":
                raise StateConflict("TASK_RESUME_COMPARE_AND_SET_FAILED")
            copied._tasks[key] = ("READY", None, None)
            copied._active_task_pauses.remove(key)
            causes = set(copied._dispatch_close_causes.get(request.run_id, ()))
            close_cause = (
                DispatchCloseCause.BUDGET_EXHAUSTED.value
                if pause.pause_reason == "LOWERED_BUDGET_CEILING"
                or pause.budget_ceiling_exhaustions
                else DispatchCloseCause.TASK_PAUSED.value
            )
            if close_cause not in causes:
                raise StateConflict("EXACT_RESUME_DISPATCH_CAUSE_MISMATCH")
            causes.remove(close_cause)
            copied._dispatch_close_causes[request.run_id] = tuple(sorted(causes))
            copied._new_dispatch_open[request.run_id] = not causes
            result.append(
                TaskResumeDecision(
                    "RESUME",
                    request.run_id,
                    request.task_id,
                    "READY",
                    allocation_id,
                    new_attempt_id,
                    calls,
                    None,
                    None,
                )
            )

        try:
            self._commit_state_and_event(
                run_id=request.run_id,
                expected_sequence=request.expected_sequence,
                event=AuditEvent.kind(
                    "TASK_RESUME_ALLOCATED",
                    task_id=request.task_id,
                    attempt_id=new_attempt_id,
                    applicable_revision_digests=request.applicable_revision_digests,
                    subject_digests=(counters.digest,),
                ),
                mutate=mutate,
            )
        except _ResumeStale as stale:
            return stale.decision
        return result[0]

    def _require_current_task_budget(
        self,
        task: TaskAuthority,
        budget_digest: RevisionDigest,
    ) -> None:
        current = self._approved_budgets.get(task.run_id)
        if current is None or current[0] != budget_digest:
            raise StateConflict("CURRENT_BUDGET_BINDING_MISMATCH")

    def _set_task_state(
        self,
        run_id: RunId,
        task_id: TaskId,
        state: TaskLifecycleState,
    ) -> None:
        legal_sources: dict[TaskLifecycleState, frozenset[TaskLifecycleState]] = {
            "ACTIVE": frozenset(),
            "READY": frozenset({"ACTIVE", "PAUSED"}),
            "PAUSED": frozenset({"ACTIVE"}),
        }
        key = (run_id, task_id)
        current = self._tasks.get(key)
        if current is None or current[0] not in legal_sources[state]:
            raise StateConflict("TASK_STATE_TRANSITION_ILLEGAL")
        self._tasks[key] = (state, None, None)

    def _pause_task(
        self,
        run_id: RunId,
        task_id: TaskId,
        reason: str,
        counter: int,
    ) -> None:
        self._set_task_state(run_id, task_id, "PAUSED")
        self._tasks[(run_id, task_id)] = ("PAUSED", reason, counter)

    def _finish_attempt(
        self,
        run_id: RunId,
        task_id: TaskId,
        attempt_id: AttemptId,
        state: AttemptLifecycleState,
    ) -> None:
        key = (run_id, attempt_id)
        current = self._attempts.get(key)
        if state != "FAILED" or current != (task_id, "RUNNING"):
            raise StateConflict("ATTEMPT_STATE_TRANSITION_ILLEGAL")
        self._attempts[key] = (task_id, "FAILED")

    def _release_attempt_lease(self, run_id: RunId, attempt_id: AttemptId) -> None:
        for key, lease in tuple(self._workspace_leases.items()):
            if (
                lease.run_id == run_id
                and lease.attempt_id == attempt_id
                and lease.state == "ACTIVE"
            ):
                self._workspace_leases[key] = replace(lease, state="REVOKED")
        if run_id in self._runs:
            budget_digest, _ = self._approved_budgets[run_id]
            active_count = sum(
                1
                for (lease_run, _), lease in self._workspace_leases.items()
                if lease_run == run_id and lease.state == "ACTIVE"
            )
            self._settle_global_usage_for_producer(
                run_id,
                budget_digest,
                GlobalBudgetMetric.CONCURRENT_WORKERS,
                active_count,
            )

    def record_task_checkpoint(
        self,
        task: TaskAuthority,
        checkpoint: CheckpointKey,
        budget_digest: RevisionDigest,
        expected_sequence: AuditSequence,
    ) -> TaskStopDecision:
        count = 0

        def mutate(copied: InMemoryStateStore) -> None:
            nonlocal count
            copied._require_current_task_budget(task, budget_digest)
            current = copied._tasks.get((task.run_id, task.task_id))
            if current is None or current[0] != "ACTIVE":
                raise StateConflict("TASK_CHECKPOINT_SOURCE_STATE_ILLEGAL")
            history = copied._task_checkpoints.setdefault((task.run_id, task.task_id), [])
            history.append((checkpoint, budget_digest))
            count = sum(1 for observed, _ in history if observed == checkpoint)
            if count >= V01_MECHANISM_LIMITS.repeated_checkpoint_ceiling:
                copied._pause_task(
                    task.run_id,
                    task.task_id,
                    "REPEATED_CHECKPOINT",
                    count,
                )
                copied._record_task_pause_binding(
                    run_id=task.run_id,
                    task_id=task.task_id,
                    attempt_id=task.attempt_id,
                    pause_sequence=AuditSequence(expected_sequence + 1),
                    pause_reason="REPEATED_CHECKPOINT",
                    budget_digest=budget_digest,
                )

        sequence = self._commit_state_and_event(
            run_id=task.run_id,
            expected_sequence=expected_sequence,
            event=AuditEvent.kind(
                "TASK_CHECKPOINT_RECORDED",
                task_id=task.task_id,
                attempt_id=task.attempt_id,
            ),
            mutate=mutate,
        )
        paused = count >= V01_MECHANISM_LIMITS.repeated_checkpoint_ceiling
        return TaskStopDecision(
            decision="PAUSE" if paused else "CONTINUE",
            run_id=task.run_id,
            task_id=task.task_id,
            task_state="PAUSED" if paused else "ACTIVE",
            pause_reason="REPEATED_CHECKPOINT" if paused else None,
            checkpoint_count=count,
            resulting_sequence=sequence,
        )

    def record_invalid_action(
        self,
        task: TaskAuthority,
        attempt_id: AttemptId,
        action_digest: str,
        budget_digest: RevisionDigest,
        expected_sequence: AuditSequence,
        *,
        worker_recovery: tuple[
            WorkerTurnBinding,
            LogicalTurnId,
            EffectIntent | None,
            RuntimePermit | None,
        ]
        | None = None,
    ) -> TaskStopDecision:
        if attempt_id != task.attempt_id:
            raise StateConflict("TASK_ATTEMPT_BINDING_MISMATCH")
        count = 0

        def mutate(copied: InMemoryStateStore) -> None:
            nonlocal count
            copied._require_current_task_budget(task, budget_digest)
            copied._finish_attempt(task.run_id, task.task_id, attempt_id, "FAILED")
            worker_attempt = copied._worker_attempts.get(attempt_id)
            if worker_attempt is not None:
                if worker_attempt.run_id != task.run_id or worker_attempt.task_id != task.task_id:
                    raise StateConflict("WORKER_ATTEMPT_BINDING_MISMATCH")
                copied._worker_attempts[attempt_id] = replace(worker_attempt, state="FAILED")
            current = copied._tasks.get((task.run_id, task.task_id))
            if current is None or current[0] != "ACTIVE":
                raise StateConflict("TASK_INVALID_ACTION_SOURCE_STATE_ILLEGAL")
            copied._release_attempt_lease(task.run_id, attempt_id)
            history = copied._task_invalid_actions.setdefault((task.run_id, task.task_id), [])
            history.append((attempt_id, action_digest, budget_digest))
            count = sum(1 for _, observed, _ in history if observed == action_digest)
            if count >= V01_MECHANISM_LIMITS.repeated_invalid_action_ceiling:
                copied._pause_task(
                    task.run_id,
                    task.task_id,
                    "REPEATED_INVALID_ACTION",
                    count,
                )
                copied._record_task_pause_binding(
                    run_id=task.run_id,
                    task_id=task.task_id,
                    attempt_id=attempt_id,
                    pause_sequence=AuditSequence(expected_sequence + 1),
                    pause_reason="REPEATED_INVALID_ACTION",
                    budget_digest=budget_digest,
                )
            else:
                copied._set_task_state(task.run_id, task.task_id, "READY")
            if worker_recovery is not None:
                binding, logical_turn_id, marker, permit = worker_recovery
                copied._settle_recovered_worker_marker(
                    binding,
                    logical_turn_id,
                    marker,
                    permit,
                    "WORKER_MALFORMED_ACTION_RECORDED",
                    expected_sequence,
                )

        sequence = self._commit_state_and_event(
            run_id=task.run_id,
            expected_sequence=expected_sequence,
            event=AuditEvent.kind(
                "INVALID_ACTION_RECORDED",
                task_id=task.task_id,
                attempt_id=attempt_id,
            ),
            mutate=mutate,
        )
        paused = count >= V01_MECHANISM_LIMITS.repeated_invalid_action_ceiling
        return TaskStopDecision(
            decision="PAUSE" if paused else "CONTINUE",
            run_id=task.run_id,
            task_id=task.task_id,
            task_state="PAUSED" if paused else "READY",
            pause_reason="REPEATED_INVALID_ACTION" if paused else None,
            identical_invalid_action_count=count,
            attempt_state="FAILED",
            resulting_sequence=sequence,
        )

    def record_malformed_worker_action(
        self,
        *,
        binding: WorkerTurnBinding,
        logical_turn_id: LogicalTurnId,
        action_digest: str,
        recovered_marker: EffectIntent | None,
        permit: RuntimePermit | None,
        expected_sequence: AuditSequence,
    ) -> TaskStopDecision:
        if (recovered_marker is None) != (permit is None):
            raise StateConflict("RECOVERED_MARKER_PERMIT_BINDING_MISMATCH")
        if self.current_worker_turn_binding(binding.attempt_id) != binding:
            raise StateConflict("WORKER_TURN_BINDING_MISMATCH")
        return self.record_invalid_action(
            TaskAuthority(binding.run_id, binding.task_id, binding.attempt_id),
            binding.attempt_id,
            action_digest,
            binding.budget_digest,
            expected_sequence,
            worker_recovery=(binding, logical_turn_id, recovered_marker, permit),
        )

    def record_authorized_worker_action(
        self,
        *,
        intent: ToolIntent,
        request: AuthorizationRequest,
        decision: AuthorizationDecision,
        expected_prestate: ActionPreState,
        recovered_marker: EffectIntent | None,
        permit: RuntimePermit | None,
        expected_sequence: AuditSequence,
    ) -> ToolIntent:
        if (recovered_marker is None) != (permit is None):
            raise StateConflict("RECOVERED_MARKER_PERMIT_BINDING_MISMATCH")
        binding = self.current_worker_turn_binding(request.attempt_id)
        try:
            validate_authorized_worker_action(binding, intent, request, decision, expected_prestate)
        except ValueError as error:
            raise StateConflict(str(error)) from error
        effect = intent.to_effect_intent(AuditSequence(expected_sequence + 1))
        self._validate_effect_intent(effect, expected_sequence)

        def mutate(copied: InMemoryStateStore) -> None:
            if copied.current_worker_turn_binding(binding.attempt_id) != binding:
                raise StateConflict("WORKER_TURN_BINDING_MISMATCH")
            turn_key = (binding.attempt_id, request.logical_turn_id)
            if (
                request.action_id in copied._worker_actions
                or turn_key in copied._worker_turn_actions
            ):
                raise StateConflict("WORKER_ACTION_DUPLICATE")
            if effect.intent_id in copied._effect_intents or any(
                existing.idempotency_key == effect.idempotency_key
                for existing in copied._effect_intents.values()
            ):
                raise StateConflict("EFFECT_INTENT_DUPLICATE")
            copied._effect_intents[effect.intent_id] = effect
            copied._worker_actions[request.action_id] = WorkerActionRecord(
                action_id=request.action_id,
                run_id=binding.run_id,
                task_id=binding.task_id,
                attempt_id=binding.attempt_id,
                logical_turn_id=request.logical_turn_id,
                normalized_action_digest=request.action_digest,
                expected_prestate_digest=request.expected_prestate_digest,
                authorization_binding_digest=decision.binding_digest,
                deadline_at_utc=decision.deadline_at_utc,
                intent_id=intent.intent_id,
                result_intent_id=None,
                created_sequence=AuditSequence(expected_sequence + 1),
            )
            copied._worker_turn_actions[turn_key] = request.action_id
            copied._settle_recovered_worker_marker(
                binding,
                request.logical_turn_id,
                recovered_marker,
                permit,
                "WORKER_ACTION_RELEASED",
                expected_sequence,
            )

        self._commit_state_and_event(
            run_id=binding.run_id,
            expected_sequence=expected_sequence,
            event=AuditEvent.kind(
                "WORKER_ACTION_INTENT_RECORDED",
                task_id=binding.task_id,
                attempt_id=binding.attempt_id,
                action_id=request.action_id,
                applicable_revision_digests=binding.applicable_revision_digests,
                subject_digests=(request.action_digest, decision.binding_digest),
            ),
            mutate=mutate,
        )
        return intent

    def settle_worker_action(
        self,
        *,
        intent: ToolIntent,
        authorization: AuthorizationDecision,
        result: ToolResult,
        expected_sequence: AuditSequence,
    ) -> AuditSequence:
        if result.run_id != intent.run_id or result.intent_id != intent.intent_id:
            raise StateConflict("WORKER_TOOL_RESULT_BINDING_MISMATCH")
        effect_result = result.to_effect_result(AuditSequence(expected_sequence + 1))

        def mutate(copied: InMemoryStateStore) -> None:
            action = copied._worker_actions.get(intent.action_id)
            stored_intent = copied._effect_intents.get(intent.intent_id)
            if (
                action is None
                or action.intent_id != intent.intent_id
                or action.result_intent_id is not None
                or action.authorization_binding_digest != authorization.binding_digest
                or stored_intent is None
                or ToolIntent.from_effect_intent(stored_intent) != intent
                or intent.intent_id in copied._effect_results
            ):
                raise StateConflict("WORKER_ACTION_SETTLEMENT_BINDING_MISMATCH")
            copied._effect_results[intent.intent_id] = effect_result
            copied._worker_actions[intent.action_id] = replace(
                action, result_intent_id=intent.intent_id
            )

        return self._commit_state_and_event(
            run_id=intent.run_id,
            expected_sequence=expected_sequence,
            event=AuditEvent.kind(
                "WORKER_ACTION_SETTLED",
                task_id=intent.task_id,
                attempt_id=intent.attempt_id,
                action_id=intent.action_id,
                applicable_revision_digests=intent.applicable_revision_digests,
                result_class=result.code,
                subject_digests=(effect_result.result_digest,),
            ),
            mutate=mutate,
        )

    def settle_recovered_action_denial(
        self,
        *,
        binding: WorkerTurnBinding,
        marker: EffectIntent,
        permit: RuntimePermit,
        decision: AuthorizationDecision,
        expected_sequence: AuditSequence,
    ) -> AuditSequence:
        if (
            decision.decision != "DENY"
            or decision.persistence != "DENIAL_AUDIT"
            or decision.run_id != binding.run_id
            or decision.task_id != binding.task_id
            or decision.attempt_id != binding.attempt_id
            or decision.resulting_sequence != expected_sequence
        ):
            raise StateConflict("RECOVERED_WORKER_DENIAL_BINDING_MISMATCH")

        def mutate(copied: InMemoryStateStore) -> None:
            copied._settle_recovered_worker_marker(
                binding,
                LogicalTurnId(marker.action_id or ""),
                marker,
                permit,
                "WORKER_ACTION_DENIED",
                expected_sequence,
            )

        return self._commit_state_and_event(
            run_id=binding.run_id,
            expected_sequence=expected_sequence,
            event=AuditEvent.kind(
                "RECOVERED_WORKER_ACTION_DENIAL_SETTLED",
                task_id=binding.task_id,
                attempt_id=binding.attempt_id,
                action_id=decision.action_id,
                applicable_revision_digests=binding.applicable_revision_digests,
                result_class=decision.reason,
            ),
            mutate=mutate,
        )

    def freeze_authorized_pending_action(
        self,
        *,
        request: AuthorizationRequest,
        decision: AuthorizationDecision,
        expected_prestate: ActionPreState,
        recovered_marker: EffectIntent | None,
        permit: RuntimePermit | None,
        expected_sequence: AuditSequence,
    ) -> PendingActionFreeze:
        if (recovered_marker is None) != (permit is None):
            raise StateConflict("RECOVERED_MARKER_PERMIT_BINDING_MISMATCH")
        binding = self.current_worker_turn_binding(request.attempt_id)
        try:
            pending_id = pending_worker_action_id(binding, request, decision, expected_prestate)
        except ValueError as error:
            raise StateConflict(str(error)) from error

        def mutate(copied: InMemoryStateStore) -> None:
            if copied.current_worker_turn_binding(binding.attempt_id) != binding:
                raise StateConflict("WORKER_TURN_BINDING_MISMATCH")
            turn_key = (binding.attempt_id, request.logical_turn_id)
            if (
                request.action_id in copied._worker_actions
                or turn_key in copied._worker_turn_actions
            ):
                raise StateConflict("WORKER_ACTION_DUPLICATE")
            copied._worker_actions[request.action_id] = WorkerActionRecord(
                action_id=request.action_id,
                run_id=binding.run_id,
                task_id=binding.task_id,
                attempt_id=binding.attempt_id,
                logical_turn_id=request.logical_turn_id,
                normalized_action_digest=request.action_digest,
                expected_prestate_digest=request.expected_prestate_digest,
                authorization_binding_digest=decision.binding_digest,
                deadline_at_utc=decision.deadline_at_utc,
                intent_id=None,
                result_intent_id=None,
                created_sequence=AuditSequence(expected_sequence + 1),
            )
            copied._worker_turn_actions[turn_key] = request.action_id
            copied._worker_attempts[binding.attempt_id] = replace(
                copied._worker_attempts[binding.attempt_id], state="WAITING_APPROVAL"
            )
            copied._settle_recovered_worker_marker(
                binding,
                request.logical_turn_id,
                recovered_marker,
                permit,
                "WORKER_PENDING_ACTION_FROZEN",
                expected_sequence,
            )

        sequence = self._commit_state_and_event(
            run_id=binding.run_id,
            expected_sequence=expected_sequence,
            event=AuditEvent.kind(
                "WORKER_PENDING_ACTION_FROZEN",
                task_id=binding.task_id,
                attempt_id=binding.attempt_id,
                action_id=request.action_id,
                applicable_revision_digests=binding.applicable_revision_digests,
                subject_digests=(request.action_digest, decision.binding_digest),
            ),
            mutate=mutate,
        )
        return PendingActionFreeze(pending_id, sequence)

    def finish_attempt(
        self,
        *,
        binding: WorkerTurnBinding,
        logical_turn_id: LogicalTurnId,
        action: FinishAction | FailAction,
        action_digest: str,
        authorization: AuthorizationDecision,
        recovered_marker: EffectIntent | None,
        permit: RuntimePermit | None,
        expected_sequence: AuditSequence,
    ) -> RuntimeDecision:
        if (recovered_marker is None) != (permit is None):
            raise StateConflict("RECOVERED_MARKER_PERMIT_BINDING_MISMATCH")
        try:
            effect, result = terminal_worker_effects(
                binding,
                logical_turn_id,
                action,
                action_digest,
                authorization,
                AuditSequence(expected_sequence + 1),
            )
        except ValueError as error:
            raise StateConflict(str(error)) from error
        self._validate_effect_intent(effect, expected_sequence)

        def mutate(copied: InMemoryStateStore) -> None:
            if copied.current_worker_turn_binding(binding.attempt_id) != binding:
                raise StateConflict("WORKER_TURN_BINDING_MISMATCH")
            turn_key = (binding.attempt_id, logical_turn_id)
            if (
                authorization.action_id in copied._worker_actions
                or turn_key in copied._worker_turn_actions
                or effect.intent_id in copied._effect_intents
            ):
                raise StateConflict("WORKER_ACTION_DUPLICATE")
            copied._effect_intents[effect.intent_id] = effect
            copied._effect_results[effect.intent_id] = result
            copied._worker_actions[authorization.action_id] = WorkerActionRecord(
                action_id=authorization.action_id,
                run_id=binding.run_id,
                task_id=binding.task_id,
                attempt_id=binding.attempt_id,
                logical_turn_id=logical_turn_id,
                normalized_action_digest=action_digest,
                expected_prestate_digest=sha256_digest("{}"),
                authorization_binding_digest=authorization.binding_digest,
                deadline_at_utc=authorization.deadline_at_utc,
                intent_id=effect.intent_id,
                result_intent_id=effect.intent_id,
                created_sequence=AuditSequence(expected_sequence + 1),
            )
            copied._worker_turn_actions[turn_key] = authorization.action_id
            attempt = copied._worker_attempts[binding.attempt_id]
            terminal_state = "SUCCEEDED" if isinstance(action, FinishAction) else "FAILED"
            copied._worker_attempts[binding.attempt_id] = replace(attempt, state=terminal_state)
            if isinstance(action, FailAction):
                copied._finish_attempt(
                    binding.run_id, binding.task_id, binding.attempt_id, "FAILED"
                )
            copied._release_attempt_lease(binding.run_id, binding.attempt_id)
            copied._settle_recovered_worker_marker(
                binding,
                logical_turn_id,
                recovered_marker,
                permit,
                "WORKER_TERMINAL_ACTION_RELEASED",
                expected_sequence,
            )

        sequence = self._commit_state_and_event(
            run_id=binding.run_id,
            expected_sequence=expected_sequence,
            event=AuditEvent.kind(
                "WORKER_ATTEMPT_FINISHED",
                task_id=binding.task_id,
                attempt_id=binding.attempt_id,
                action_id=authorization.action_id,
                applicable_revision_digests=binding.applicable_revision_digests,
                result_class=result.result_class,
                subject_digests=(action_digest, result.result_digest),
            ),
            mutate=mutate,
        )
        return RuntimeDecision(
            code="ACTION_RECORDED",
            stop_reason=None,
            resulting_sequence=sequence,
        )

    def _settle_recovered_worker_marker(
        self,
        binding: WorkerTurnBinding,
        logical_turn_id: LogicalTurnId,
        marker: EffectIntent | None,
        permit: RuntimePermit | None,
        result_class: str,
        expected_sequence: AuditSequence,
    ) -> None:
        if marker is None and permit is None:
            return
        owner_id = None if permit is None else permit.consumed_owner_id
        try:
            payload = {} if marker is None else json.loads(marker.normalized_payload_json)
        except json.JSONDecodeError as error:
            raise StateConflict("RECOVERED_WORKER_MARKER_BINDING_MISMATCH") from error
        if (
            marker is None
            or permit is None
            or owner_id is None
            or permit.run_id != binding.run_id
            or permit.state != "CONSUMED"
            or permit.allowed_phase != "ACTIVE"
            or permit.applicable_revision_digests != binding.applicable_revision_digests
            or permit.target_authority_digest != binding.target_safety_digest
            or marker.run_id != binding.run_id
            or marker.kind != "RECOVERED_MODEL_ACTION"
            or marker.task_id != binding.task_id
            or marker.attempt_id != binding.attempt_id
            or marker.action_id != logical_turn_id
            or marker.applicable_revision_digests != binding.applicable_revision_digests
            or not isinstance(payload, dict)
            or payload.get("owner_kind") != "WORKER"
            or payload.get("task_id") != binding.task_id
            or payload.get("attempt_id") != binding.attempt_id
            or payload.get("logical_turn_id") != logical_turn_id
            or payload.get("tranche_id") != binding.tranche_id
            or self._require_unsettled_effect_intent(binding.run_id, marker.intent_id) != marker
        ):
            raise StateConflict("RECOVERED_WORKER_MARKER_BINDING_MISMATCH")
        stored_permit, _ = self._require_consumed_runtime_owner_on_copy(
            self, binding.run_id, owner_id, permit.generation
        )
        if stored_permit != permit:
            raise StateConflict("RECOVERED_WORKER_MARKER_BINDING_MISMATCH")
        result_payload = canonical_json({"result_class": result_class})
        self._effect_results[marker.intent_id] = EffectResult(
            intent_id=marker.intent_id,
            run_id=binding.run_id,
            outcome="COMPLETED",
            result_class=result_class,
            result_digest=sha256_digest(result_payload),
            bounded_result_json=result_payload,
            settled_sequence=AuditSequence(expected_sequence + 1),
        )

    def latest_worker_feedback(self, attempt_id: AttemptId) -> str | None:
        with self._lock:
            actions = tuple(
                action
                for action in self._worker_actions.values()
                if action.attempt_id == attempt_id and action.result_intent_id is not None
            )
            if not actions:
                return None
            latest = max(actions, key=lambda action: action.created_sequence)
            result_intent_id = latest.result_intent_id
            if result_intent_id is None:
                raise StateConflict("WORKER_FEEDBACK_RESULT_MISSING")
            result = self._effect_results.get(result_intent_id)
        if result is None:
            raise StateConflict("WORKER_FEEDBACK_RESULT_MISSING")
        return bounded_worker_feedback(ToolResult.model_validate_json(result.bounded_result_json))

    def authorize_new_attempt(self, run_id: RunId, task_id: TaskId) -> DispatchAuthorization:
        with self._lock:
            task = self._tasks.get((run_id, task_id))
            dispatch_open = self._new_dispatch_open.get(run_id, True)
        if task is None:
            raise StateConflict("TASK_NOT_FOUND")
        if task[0] == "PAUSED":
            return DispatchAuthorization("DENY", "TASK_PAUSED")
        if not dispatch_open:
            if self.global_usage_snapshot(run_id).active_run_seconds >= global_ceiling_for(
                self.current_approved_budget(run_id)[1],
                GlobalBudgetMetric.ACTIVE_RUN_SECONDS,
            ):
                return DispatchAuthorization("DENY", "ACTIVE_RUN_TIME_CEILING")
            return DispatchAuthorization("DENY", "RUN_DISPATCH_CLOSED")
        if task[0] != "READY":
            return DispatchAuthorization("DENY", "TASK_NOT_READY")
        return DispatchAuthorization("ALLOW", "AUTHORIZED")

    def _require_current_budget(
        self,
        run_id: RunId,
        budget_digest: RevisionDigest,
    ) -> BudgetRevisionDocument:
        current = self._approved_budgets.get(run_id)
        if current is None or current[0] != budget_digest:
            raise StateConflict("CURRENT_BUDGET_BINDING_MISMATCH")
        return current[1]

    def revision_binding_failure(
        self,
        run_id: RunId,
        expected: ApplicableRevisionDigests,
    ) -> str | None:
        with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                raise StateConflict("RUN_NOT_FOUND")
            current = ApplicableRevisionDigests(
                plan_digest=run.current_plan_digest,
                policy_digest=run.current_policy_digest,
                budget_digest=run.current_budget_digest,
                model_configuration_digest=run.current_model_configuration_digest,
            )
        return None if current == expected else "CURRENT_REVISION_BINDING_MISMATCH"

    def _require_current_revisions(
        self,
        run_id: RunId,
        expected: ApplicableRevisionDigests,
    ) -> None:
        run = self._runs.get(run_id)
        if run is None:
            raise StateConflict("RUN_NOT_FOUND")
        current = ApplicableRevisionDigests(
            plan_digest=run.current_plan_digest,
            policy_digest=run.current_policy_digest,
            budget_digest=run.current_budget_digest,
            model_configuration_digest=run.current_model_configuration_digest,
        )
        if current != expected:
            raise StateConflict("CURRENT_REVISION_BINDING_MISMATCH")

    def _require_dispatch_binding(self, run_id: RunId) -> None:
        if run_id not in self._runs:
            raise StateConflict("RUN_NOT_FOUND")
        is_open = self._new_dispatch_open.get(run_id, True)
        causes = self._dispatch_close_causes.get(run_id, ())
        if is_open != (not causes):
            raise StateConflict("DISPATCH_CLOSURE_BINDING_INVALID")

    def _close_new_dispatch(
        self,
        run_id: RunId,
        cause: DispatchCloseCause,
    ) -> bool:
        self._require_dispatch_binding(run_id)
        causes = set(self._dispatch_close_causes.get(run_id, ()))
        if cause.value in causes:
            return False
        causes.add(cause.value)
        self._dispatch_close_causes[run_id] = tuple(sorted(causes))
        self._new_dispatch_open[run_id] = False
        return True

    def authorize_new_action(self, run_id: RunId) -> DispatchAuthorization:
        with self._lock:
            self._require_dispatch_binding(run_id)
            is_open = self._new_dispatch_open[run_id]
        if not is_open:
            return DispatchAuthorization("DENY", "RUN_DISPATCH_CLOSED")
        return DispatchAuthorization("ALLOW", "AUTHORIZED")

    def global_usage_snapshot(self, run_id: RunId) -> GlobalUsageSnapshot:
        with self._lock:
            if run_id not in self._runs:
                raise StateConflict("RUN_NOT_FOUND")
            return GlobalUsageSnapshot(
                active_run_seconds=Decimal(
                    str(
                        self._global_usage.get(
                            (run_id, GlobalBudgetMetric.ACTIVE_RUN_SECONDS),
                            "0",
                        )
                    )
                ),
                tasks=int(self._global_usage.get((run_id, GlobalBudgetMetric.TASKS), 0)),
                planning_requests=int(
                    self._global_usage.get(
                        (run_id, GlobalBudgetMetric.PLANNING_REQUESTS),
                        0,
                    )
                ),
                model_calls=int(
                    self._global_usage.get(
                        (run_id, GlobalBudgetMetric.MODEL_CALLS),
                        0,
                    )
                ),
                input_tokens=int(
                    self._global_usage.get(
                        (run_id, GlobalBudgetMetric.INPUT_TOKENS),
                        0,
                    )
                ),
                output_tokens=int(
                    self._global_usage.get(
                        (run_id, GlobalBudgetMetric.OUTPUT_TOKENS),
                        0,
                    )
                ),
                cost_reserve_usd=Decimal(
                    str(
                        self._global_usage.get(
                            (run_id, GlobalBudgetMetric.COST_RESERVE_USD),
                            "0",
                        )
                    )
                ),
                concurrent_workers=int(
                    self._global_usage.get(
                        (run_id, GlobalBudgetMetric.CONCURRENT_WORKERS),
                        0,
                    )
                ),
            )

    @staticmethod
    def _normalize_global_usage(
        metric: GlobalBudgetMetric,
        value: int | Decimal,
    ) -> int | Decimal:
        if metric in {
            GlobalBudgetMetric.ACTIVE_RUN_SECONDS,
            GlobalBudgetMetric.COST_RESERVE_USD,
        }:
            normalized: int | Decimal = Decimal(str(value))
        elif isinstance(value, bool) or not isinstance(value, int):
            raise StateConflict("GLOBAL_USAGE_VALUE_INVALID")
        else:
            normalized = value
        if normalized < 0:
            raise StateConflict("GLOBAL_USAGE_VALUE_INVALID")
        return normalized

    def settle_global_usage(
        self,
        run_id: RunId,
        budget_digest: RevisionDigest,
        metric: GlobalBudgetMetric,
        absolute_used: int | Decimal,
        expected_sequence: AuditSequence,
    ) -> BudgetSettlement:
        normalized_metric = normalize_global_budget_metric(metric)
        normalized_used = self._normalize_global_usage(normalized_metric, absolute_used)
        result: list[BudgetSettlement] = []
        events = [AuditEvent.kind("GLOBAL_BUDGET_USAGE_SETTLED")]

        def mutate(copied: InMemoryStateStore) -> None:
            budget = copied._require_current_budget(run_id, budget_digest)
            ceiling = global_ceiling_for(budget, normalized_metric)
            key = (run_id, normalized_metric)
            previous = copied._global_usage.get(
                key, Decimal(0) if isinstance(ceiling, Decimal) else 0
            )
            if (
                normalized_used < previous
                and normalized_metric != GlobalBudgetMetric.CONCURRENT_WORKERS
            ):
                raise StateConflict("GLOBAL_USAGE_NOT_MONOTONIC")
            copied._global_usage[key] = normalized_used
            warning_percent = V01_MECHANISM_LIMITS.warning_percent
            warning_key = (run_id, budget_digest, normalized_metric, warning_percent)
            if crossed_threshold(previous, normalized_used, ceiling, warning_percent):
                copied._budget_warnings.setdefault(
                    warning_key,
                    BudgetWarning(
                        run_id,
                        budget_digest,
                        normalized_metric,
                        normalized_used,
                        ceiling,
                        warning_percent,
                    ),
                )
            stopped = normalized_used >= ceiling and copied._close_new_dispatch(
                run_id,
                DispatchCloseCause.BUDGET_EXHAUSTED,
            )
            if stopped:
                events.append(AuditEvent.kind("BUDGET_STOP_REQUESTED"))
            result.append(
                BudgetSettlement(
                    run_id=run_id,
                    metric=normalized_metric,
                    absolute_used=normalized_used,
                    ceiling=ceiling,
                    action_state=None,
                    pause_after_barrier=normalized_used >= ceiling,
                    pause_reason=(
                        f"GLOBAL_{normalized_metric.value.removesuffix('S')}_CEILING"
                        if normalized_used >= ceiling
                        else None
                    ),
                    resulting_sequence=AuditSequence(expected_sequence + (2 if stopped else 1)),
                )
            )

        self._commit_state_and_events(
            run_id=run_id,
            expected_sequence=expected_sequence,
            event_factory=lambda: tuple(events),
            mutate=mutate,
        )
        return result[0]

    def _settle_global_usage_for_producer(
        self,
        run_id: RunId,
        budget_digest: RevisionDigest,
        metric: GlobalBudgetMetric,
        absolute_used: int | Decimal,
        *,
        allow_reservation_reconciliation: bool = False,
    ) -> tuple[BudgetSettlement, bool]:
        budget = self._require_current_budget(run_id, budget_digest)
        normalized_metric = normalize_global_budget_metric(metric)
        normalized_used = self._normalize_global_usage(normalized_metric, absolute_used)
        ceiling = global_ceiling_for(budget, normalized_metric)
        key = (run_id, normalized_metric)
        previous = self._global_usage.get(
            key,
            Decimal(0) if isinstance(ceiling, Decimal) else 0,
        )
        if (
            normalized_used < previous
            and normalized_metric != GlobalBudgetMetric.CONCURRENT_WORKERS
            and not allow_reservation_reconciliation
        ):
            raise StateConflict("GLOBAL_USAGE_NOT_MONOTONIC")
        self._global_usage[key] = normalized_used
        warning_percent = V01_MECHANISM_LIMITS.warning_percent
        if crossed_threshold(previous, normalized_used, ceiling, warning_percent):
            self._budget_warnings.setdefault(
                (run_id, budget_digest, normalized_metric, warning_percent),
                BudgetWarning(
                    run_id,
                    budget_digest,
                    normalized_metric,
                    normalized_used,
                    ceiling,
                    warning_percent,
                ),
            )
        pause = normalized_used >= ceiling
        stopped = pause and self._close_new_dispatch(
            run_id,
            DispatchCloseCause.BUDGET_EXHAUSTED,
        )
        return (
            BudgetSettlement(
                run_id=run_id,
                metric=normalized_metric,
                absolute_used=normalized_used,
                ceiling=ceiling,
                action_state=None,
                pause_after_barrier=pause,
                pause_reason=(
                    f"GLOBAL_{normalized_metric.value.removesuffix('S')}_CEILING" if pause else None
                ),
                resulting_sequence=AuditSequence(0),
            ),
            stopped,
        )

    def begin_atomic_action(
        self,
        action: AtomicAction,
        expected_sequence: AuditSequence,
    ) -> AtomicAction:
        def mutate(copied: InMemoryStateStore) -> None:
            copied._require_current_budget(action.run_id, action.budget_digest)
            copied._require_dispatch_binding(action.run_id)
            if not copied._new_dispatch_open[action.run_id]:
                raise StateConflict("NEW_DISPATCH_CLOSED")
            key = (action.run_id, action.action_id)
            if key in copied._atomic_actions:
                raise StateConflict("ATOMIC_ACTION_ID_REUSED")
            copied._atomic_actions[key] = action

        self._commit_state_and_event(
            run_id=action.run_id,
            expected_sequence=expected_sequence,
            event=AuditEvent.kind("ATOMIC_ACTION_STARTED"),
            mutate=mutate,
        )
        return action

    def settle_atomic_action(
        self,
        action: AtomicAction,
        model_calls: int,
        expected_sequence: AuditSequence,
    ) -> BudgetSettlement:
        if isinstance(model_calls, bool) or not isinstance(model_calls, int) or model_calls < 0:
            raise StateConflict("GLOBAL_USAGE_VALUE_INVALID")
        result: list[BudgetSettlement] = []
        events = [AuditEvent.kind("ATOMIC_ACTION_SETTLED")]

        def mutate(copied: InMemoryStateStore) -> None:
            budget = copied._require_current_budget(action.run_id, action.budget_digest)
            key = (action.run_id, action.action_id)
            if copied._atomic_actions.get(key) != action:
                raise StateConflict("ATOMIC_ACTION_NOT_CURRENT")
            copied._atomic_actions[key] = replace(action, state="SETTLED")
            usage_key = (action.run_id, GlobalBudgetMetric.MODEL_CALLS)
            previous = copied._global_usage.get(usage_key, 0)
            calls = int(previous) + model_calls
            copied._global_usage[usage_key] = calls
            ceiling = global_ceiling_for(budget, GlobalBudgetMetric.MODEL_CALLS)
            warning_percent = V01_MECHANISM_LIMITS.warning_percent
            if crossed_threshold(previous, calls, ceiling, warning_percent):
                copied._budget_warnings.setdefault(
                    (
                        action.run_id,
                        action.budget_digest,
                        GlobalBudgetMetric.MODEL_CALLS,
                        warning_percent,
                    ),
                    BudgetWarning(
                        action.run_id,
                        action.budget_digest,
                        GlobalBudgetMetric.MODEL_CALLS,
                        calls,
                        ceiling,
                        warning_percent,
                    ),
                )
            pause = calls >= ceiling
            stopped = pause and copied._close_new_dispatch(
                action.run_id,
                DispatchCloseCause.BUDGET_EXHAUSTED,
            )
            if stopped:
                events.append(AuditEvent.kind("BUDGET_STOP_REQUESTED"))
            result.append(
                BudgetSettlement(
                    run_id=action.run_id,
                    metric=GlobalBudgetMetric.MODEL_CALLS,
                    absolute_used=calls,
                    ceiling=ceiling,
                    action_state="SETTLED",
                    pause_after_barrier=pause,
                    pause_reason="GLOBAL_MODEL_CALL_CEILING" if pause else None,
                    resulting_sequence=AuditSequence(expected_sequence + (2 if stopped else 1)),
                )
            )

        self._commit_state_and_events(
            run_id=action.run_id,
            expected_sequence=expected_sequence,
            event_factory=lambda: tuple(events),
            mutate=mutate,
        )
        return result[0]

    def budget_warnings(
        self,
        run_id: RunId,
        metric: GlobalBudgetMetric | str,
    ) -> tuple[BudgetWarning, ...]:
        normalized = normalize_global_budget_metric(metric)
        with self._lock:
            return tuple(
                warning
                for (warning_run, _, warning_metric, _), warning in self._budget_warnings.items()
                if warning_run == run_id and warning_metric == normalized
            )

    def budget_warning_metrics(self, run_id: RunId) -> tuple[GlobalBudgetMetric, ...]:
        with self._lock:
            return tuple(
                sorted(
                    {
                        metric
                        for (warning_run, _, metric, _) in self._budget_warnings
                        if warning_run == run_id
                    },
                    key=lambda metric: metric.value,
                )
            )

    def audit_event_kinds(self, run_id: RunId) -> tuple[str, ...]:
        with self._lock:
            return tuple(event.event_kind for _, event in self._audit_events.get(run_id, ()))

    def runtime_barrier_state(
        self, run_id: RunId
    ) -> Literal["IDLE", "IN_FLIGHT", "SETTLED", "INDETERMINATE"]:
        with self._lock:
            barrier = self._runtime_barriers.get(run_id)
            return "IDLE" if barrier is None else barrier[1]  # type: ignore[return-value]

    def dispatch_close_causes(self, run_id: RunId) -> frozenset[DispatchCloseCause]:
        with self._lock:
            self._require_dispatch_binding(run_id)
            return frozenset(
                DispatchCloseCause(cause) for cause in self._dispatch_close_causes.get(run_id, ())
            )

    def evaluate_active_run_time_boundary(
        self,
        *,
        run_id: RunId,
        budget_digest: RevisionDigest,
        expected: ActiveRunTimeState,
        ceiling_nanoseconds: int,
        expected_sequence: AuditSequence,
    ) -> ActiveRunTimeBoundaryDecision:
        with self._lock:
            if self._sequences.get(run_id, AuditSequence(0)) != expected_sequence:
                raise StateConflict("STALE_SEQUENCE")
            current_budget = self._approved_budgets.get(run_id)
            if current_budget is None or current_budget[0] != budget_digest:
                raise StateConflict("CURRENT_BUDGET_BINDING_MISMATCH")
            if ceiling_nanoseconds != current_budget[1].active_run_seconds_ceiling * 1_000_000_000:
                raise StateConflict("ACTIVE_RUN_TIME_CEILING_BINDING_MISMATCH")
            current = self._active_run_times.get(
                run_id,
                ActiveRunTimeState(run_id, 0, None, None, None),
            )
            if current != expected:
                raise StateConflict("ACTIVE_RUN_TIME_SNAPSHOT_MISMATCH")
            if current.open_owner_generation is None:
                observed = current.cumulative_nanoseconds
                now = None
            else:
                if self._monotonic_clock is None:
                    raise StateConflict("MONOTONIC_CLOCK_NOT_CONFIGURED")
                now = self._monotonic_clock.now()
                observed = current.observed_nanoseconds(now)
            observed_seconds = Decimal(observed) / Decimal(1_000_000_000)
            warning_floor = (
                Decimal(current_budget[1].active_run_seconds_ceiling)
                * V01_MECHANISM_LIMITS.warning_percent
                / 100
            )
            if observed_seconds < warning_floor and observed < ceiling_nanoseconds:
                return ActiveRunTimeBoundaryDecision(
                    "CONTINUE",
                    observed,
                    ceiling_nanoseconds,
                    expected_sequence,
                )

            events = [
                AuditEvent.kind(
                    "ACTIVE_RUN_TIME_CEILING_REACHED"
                    if observed >= ceiling_nanoseconds
                    else "GLOBAL_BUDGET_USAGE_SETTLED"
                )
            ]
            stopped = False

            def mutate(copied: InMemoryStateStore) -> None:
                nonlocal stopped
                copied_budget = copied._approved_budgets.get(run_id)
                if copied_budget is None or copied_budget[0] != budget_digest:
                    raise StateConflict("CURRENT_BUDGET_BINDING_MISMATCH")
                if copied._active_run_times.get(run_id) != expected:
                    raise StateConflict("ACTIVE_RUN_TIME_SNAPSHOT_MISMATCH")
                _, stopped = copied._settle_global_usage_for_producer(
                    run_id,
                    budget_digest,
                    GlobalBudgetMetric.ACTIVE_RUN_SECONDS,
                    observed_seconds,
                )
                if stopped:
                    events.append(AuditEvent.kind("BUDGET_STOP_REQUESTED"))

            sequence = self._commit_state_and_events(
                run_id=run_id,
                expected_sequence=expected_sequence,
                event_factory=lambda: tuple(events),
                mutate=mutate,
                runtime_now=now,
            )
            return ActiveRunTimeBoundaryDecision(
                "PAUSE" if observed >= ceiling_nanoseconds else "CONTINUE",
                observed,
                ceiling_nanoseconds,
                sequence,
            )

    def new_dispatch_open(self, run_id: RunId) -> bool:
        with self._lock:
            self._require_dispatch_binding(run_id)
            return self._new_dispatch_open[run_id]

    def install_approved_budget_for_test(
        self,
        run_id: RunId,
        budget_digest: RevisionDigest,
        budget: BudgetRevisionDocument,
    ) -> None:
        if revision_digest(budget) != budget_digest:
            raise StateConflict("APPROVED_BUDGET_DIGEST_MISMATCH")
        with self._lock:
            self._approved_budgets[run_id] = (budget_digest, budget)

    def current_approved_budget(
        self, run_id: RunId
    ) -> tuple[RevisionDigest, BudgetRevisionDocument]:
        with self._lock:
            try:
                return self._approved_budgets[run_id]
            except KeyError as error:
                raise StateConflict("APPROVED_BUDGET_NOT_FOUND") from error

    def persist_plan_proposal(
        self,
        proposal: PlanProposal,
        *,
        expected_sequence: AuditSequence,
        recovered_marker: EffectIntent | None = None,
        permit: RuntimePermit | None = None,
        recovered_logical_turn_id: LogicalTurnId | None = None,
    ) -> AuditSequence:
        if not (
            (recovered_marker is None and permit is None and recovered_logical_turn_id is None)
            or (
                recovered_marker is not None
                and permit is not None
                and recovered_logical_turn_id is not None
            )
        ):
            raise StateConflict("RECOVERED_MARKER_PERMIT_BINDING_MISMATCH")
        try:
            validate_plan_proposal(proposal)
        except ValueError as error:
            raise StateConflict(str(error)) from error
        events = [AuditEvent.kind("PLAN_PROPOSED")]

        def mutate(copied: InMemoryStateStore) -> None:
            run = copied._runs.get(proposal.run_id)
            if run is None:
                raise StateConflict("RUN_NOT_FOUND")
            if run.state != RunState.PLANNING:
                raise StateConflict("PLAN_PROPOSAL_REQUIRES_PLANNING")
            if run.pinned_target_oid != proposal.base_run_head_oid:
                raise StateConflict("PLAN_BASE_BINDING_MISMATCH")
            current = ApplicableRevisionDigests(
                plan_digest=run.current_plan_digest,
                policy_digest=run.current_policy_digest,
                budget_digest=run.current_budget_digest,
                model_configuration_digest=run.current_model_configuration_digest,
            )
            if current != proposal.applicable_revision_digests:
                raise StateConflict("PLAN_REVISION_BINDING_MISMATCH")
            if copied._planning_request_counts.get(proposal.run_id, 0) != (
                proposal.planning_request_count
            ):
                raise StateConflict("PLANNING_REQUEST_COUNT_MISMATCH")
            budget_digest = proposal.applicable_revision_digests.budget_digest
            if budget_digest is None:
                raise StateConflict("CURRENT_BUDGET_NOT_FOUND")
            budget = copied._require_current_budget(proposal.run_id, budget_digest)
            if len(proposal.plan.tasks) > budget.task_ceiling:
                raise StateConflict("PLAN_TASK_CEILING")
            copied._require_dispatch_binding(proposal.run_id)
            if not copied._new_dispatch_open[proposal.run_id]:
                raise StateConflict("NEW_DISPATCH_CLOSED")
            key = (proposal.run_id, proposal.plan_digest)
            if key in copied._plan_proposals or proposal.plan_digest in (
                digest for _, digest in copied._plan_proposals
            ):
                raise StateConflict("PLAN_PROPOSAL_DUPLICATE")
            copied._plan_proposals[key] = proposal
            copied._plan_task_contracts[proposal.plan_digest] = proposal.plan.tasks
            copied._plan_dependency_edges[proposal.plan_digest] = proposal.dependency_edges
            copied._plan_hazard_edges[proposal.plan_digest] = proposal.hazard_edges
            copied._plan_run_checks[proposal.plan_digest] = proposal.run_check_set
            task_count = len(copied._plan_task_contracts[proposal.plan_digest])
            settlement, stopped = copied._settle_global_usage_for_producer(
                proposal.run_id,
                budget_digest,
                GlobalBudgetMetric.TASKS,
                task_count,
            )
            if settlement.pause_after_barrier != stopped:
                raise StateConflict("PLAN_TASK_STOP_BINDING_INVALID")
            copied._runs[proposal.run_id] = replace(
                run,
                state=(RunState.PAUSED if stopped else RunState.AWAITING_PLAN_APPROVAL),
            )
            copied._settle_recovered_planning_marker(
                proposal.run_id,
                recovered_marker,
                permit,
                recovered_logical_turn_id,
                proposal.applicable_revision_digests,
                expected_sequence,
            )
            if stopped:
                events.append(AuditEvent.kind("BUDGET_STOP_REQUESTED"))

        return self._commit_state_and_events(
            run_id=proposal.run_id,
            expected_sequence=expected_sequence,
            event_factory=lambda: tuple(events),
            mutate=mutate,
        )

    def plan_proposal(self, run_id: RunId, plan_digest: RevisionDigest) -> PlanProposal:
        with self._lock:
            try:
                return self._plan_proposals[(run_id, plan_digest)]
            except KeyError as error:
                raise StateConflict("PLAN_PROPOSAL_NOT_FOUND") from error

    def task_contracts(self, plan_digest: RevisionDigest) -> tuple[TaskContract, ...]:
        with self._lock:
            try:
                return self._plan_task_contracts[plan_digest]
            except KeyError as error:
                raise StateConflict("PLAN_PROPOSAL_NOT_FOUND") from error

    def task_dependency_edges(
        self, plan_digest: RevisionDigest
    ) -> tuple[tuple[TaskId, TaskId], ...]:
        with self._lock:
            try:
                return self._plan_dependency_edges[plan_digest]
            except KeyError as error:
                raise StateConflict("PLAN_PROPOSAL_NOT_FOUND") from error

    def hazard_edges(self, plan_digest: RevisionDigest) -> tuple[tuple[TaskId, TaskId], ...]:
        with self._lock:
            try:
                return self._plan_hazard_edges[plan_digest]
            except KeyError as error:
                raise StateConflict("PLAN_PROPOSAL_NOT_FOUND") from error

    def run_check_set(self, plan_digest: RevisionDigest) -> tuple[CheckDefinition, ...]:
        with self._lock:
            try:
                return self._plan_run_checks[plan_digest]
            except KeyError as error:
                raise StateConflict("PLAN_PROPOSAL_NOT_FOUND") from error

    def planning_request_count(self, run_id: RunId) -> int:
        with self._lock:
            if run_id not in self._runs:
                raise StateConflict("RUN_NOT_FOUND")
            return self._planning_request_counts.get(run_id, 0)

    def planning_returned_bytes(self, run_id: RunId) -> int:
        with self._lock:
            if run_id not in self._runs:
                raise StateConflict("RUN_NOT_FOUND")
            return self._planning_returned_bytes.get(run_id, 0)

    def record_planning_read_intent(
        self,
        intent: PlanningReadIntent,
        recovered_marker: EffectIntent | None,
        permit: RuntimePermit | None,
        expected_sequence: AuditSequence,
    ) -> AuditSequence:
        if (recovered_marker is None) != (permit is None):
            raise StateConflict("RECOVERED_MARKER_PERMIT_BINDING_MISMATCH")
        effect_intent = intent.to_effect_intent(AuditSequence(expected_sequence + 1))
        self._validate_effect_intent(effect_intent, expected_sequence)

        def mutate(copied: InMemoryStateStore) -> None:
            if effect_intent.intent_id in copied._effect_intents or any(
                existing.idempotency_key == effect_intent.idempotency_key
                for existing in copied._effect_intents.values()
            ):
                raise StateConflict("EFFECT_INTENT_DUPLICATE")
            copied._effect_intents[effect_intent.intent_id] = effect_intent
            copied._settle_recovered_planning_marker(
                intent.run_id,
                recovered_marker,
                permit,
                intent.logical_turn_id,
                intent.applicable_revision_digests,
                expected_sequence,
            )

        return self._commit_state_and_event(
            run_id=intent.run_id,
            expected_sequence=expected_sequence,
            event=AuditEvent.kind(
                "PLANNING_READ_INTENT_RECORDED",
                action_id=intent.logical_turn_id,
                applicable_revision_digests=intent.applicable_revision_digests,
                subject_digests=(effect_intent.payload_digest,),
            ),
            mutate=mutate,
        )

    def _settle_recovered_planning_marker(
        self,
        run_id: RunId,
        recovered_marker: EffectIntent | None,
        permit: RuntimePermit | None,
        logical_turn_id: LogicalTurnId | None,
        applicable_revision_digests: ApplicableRevisionDigests,
        expected_sequence: AuditSequence,
    ) -> None:
        if recovered_marker is None and permit is None:
            return
        owner_id = None if permit is None else permit.consumed_owner_id
        if (
            recovered_marker is None
            or permit is None
            or permit.run_id != run_id
            or permit.state != "CONSUMED"
            or owner_id is None
            or permit.allowed_phase != "PLANNING"
            or permit.applicable_revision_digests != applicable_revision_digests
            or recovered_marker.kind != "RECOVERED_MODEL_ACTION"
            or recovered_marker.action_id != logical_turn_id
            or recovered_marker.applicable_revision_digests != applicable_revision_digests
            or self._require_unsettled_effect_intent(run_id, recovered_marker.intent_id)
            != recovered_marker
        ):
            raise StateConflict("RECOVERED_PLANNING_MARKER_BINDING_MISMATCH")
        stored_permit, _ = self._require_consumed_runtime_owner_on_copy(
            self, run_id, owner_id, permit.generation
        )
        if stored_permit != permit:
            raise StateConflict("RECOVERED_PLANNING_MARKER_BINDING_MISMATCH")
        payload = canonical_json({"result_class": "PLANNING_ACTION_RELEASED"})
        self._effect_results[recovered_marker.intent_id] = EffectResult(
            intent_id=recovered_marker.intent_id,
            run_id=run_id,
            outcome="COMPLETED",
            result_class="PLANNING_ACTION_RELEASED",
            result_digest=sha256_digest(payload),
            bounded_result_json=payload,
            settled_sequence=AuditSequence(expected_sequence + 1),
        )

    def settle_planning_read(
        self,
        intent: PlanningReadIntent,
        result: PlanningReadResult,
        expected_sequence: AuditSequence,
    ) -> PlanningReadSettlement:
        if result.intent_id != intent.intent_id or result.run_id != intent.run_id:
            raise StateConflict("PLANNING_READ_RESULT_BINDING_MISMATCH")
        expected_bytes = (
            0
            if result.result_class == "DENIED"
            else len(canonical_json(result.bounded_payload).encode("utf-8"))
        )
        if result.returned_bytes != expected_bytes:
            raise StateConflict("PLANNING_READ_RETURNED_BYTES_INVALID")
        overflow = self.planning_returned_bytes(intent.run_id) + result.returned_bytes > 2_097_152
        stored_result = result
        if overflow:
            stored_result = PlanningReadResult(
                intent_id=result.intent_id,
                run_id=result.run_id,
                result_class="DENIED",
                bounded_payload={"reason": "PLANNING_READ_LIMIT"},
                snapshot_digest=result.snapshot_digest,
                returned_bytes=0,
            )
        effect_result = stored_result.to_effect_result(AuditSequence(expected_sequence + 1))

        def mutate(copied: InMemoryStateStore) -> None:
            effect_intent = copied._require_unsettled_effect_intent(intent.run_id, intent.intent_id)
            if effect_intent.applicable_revision_digests != intent.applicable_revision_digests:
                raise StateConflict("EFFECT_RESULT_REVISION_BINDING_MISMATCH")
            copied._effect_results[intent.intent_id] = effect_result
            if not overflow:
                copied._planning_returned_bytes[intent.run_id] = (
                    copied._planning_returned_bytes.get(intent.run_id, 0) + result.returned_bytes
                )
            else:
                copied._runs[intent.run_id] = replace(
                    copied._runs[intent.run_id], state=RunState.PAUSED
                )
                copied._close_new_dispatch(intent.run_id, DispatchCloseCause.BUDGET_EXHAUSTED)

        sequence = self._commit_state_and_event(
            run_id=intent.run_id,
            expected_sequence=expected_sequence,
            event=AuditEvent.kind(
                "PLANNING_READ_SETTLED",
                applicable_revision_digests=intent.applicable_revision_digests,
                result_class=effect_result.result_class,
                subject_digests=(effect_result.result_digest,),
            ),
            mutate=mutate,
        )
        return PlanningReadSettlement(
            sequence,
            "PLANNING_READ_LIMIT" if overflow else None,
        )

    def persist_submitted_plan(
        self,
        run_id: RunId,
        plan_document: Mapping[str, object],
        authorization: PlanningAuthorization,
        logical_turn_id: LogicalTurnId,
        recovered_marker: EffectIntent | None,
        permit: RuntimePermit | None,
        expected_sequence: AuditSequence,
    ) -> AuditSequence:
        if (recovered_marker is None) != (permit is None):
            raise StateConflict("RECOVERED_MARKER_PERMIT_BINDING_MISMATCH")
        if authorization.run_id != run_id or authorization.decision != "ALLOW":
            raise StateConflict("PLANNING_AUTHORIZATION_MISMATCH")
        proposal = plan_proposal_from_document(
            run_id=run_id,
            plan_document=plan_document,
            authorization=authorization,
        )
        return self.persist_plan_proposal(
            proposal,
            expected_sequence=expected_sequence,
            recovered_marker=recovered_marker,
            permit=permit,
            recovered_logical_turn_id=logical_turn_id,
        )

    def record_planning_failure_or_invalid_action(
        self,
        run_id: RunId,
        logical_turn_id: LogicalTurnId,
        reason: str,
        authorization: PlanningAuthorization,
        recovered_marker: EffectIntent | None,
        permit: RuntimePermit | None,
        expected_sequence: AuditSequence,
    ) -> AuditSequence:
        if (recovered_marker is None) != (permit is None):
            raise StateConflict("RECOVERED_MARKER_PERMIT_BINDING_MISMATCH")

        def mutate(copied: InMemoryStateStore) -> None:
            if authorization.run_id != run_id or authorization.decision != "ALLOW":
                raise StateConflict("PLANNING_AUTHORIZATION_MISMATCH")
            if copied._planning_request_counts.get(run_id, 0) >= (
                authorization.planning_request_ceiling
            ):
                copied._runs[run_id] = replace(copied._runs[run_id], state=RunState.PAUSED)
                copied._close_new_dispatch(run_id, DispatchCloseCause.BUDGET_EXHAUSTED)
            copied._settle_recovered_planning_marker(
                run_id,
                recovered_marker,
                permit,
                logical_turn_id,
                authorization.applicable_revision_digests,
                expected_sequence,
            )

        return self._commit_state_and_event(
            run_id=run_id,
            expected_sequence=expected_sequence,
            event=AuditEvent.kind(
                "PLANNING_ACTION_REJECTED",
                action_id=logical_turn_id,
                applicable_revision_digests=authorization.applicable_revision_digests,
                result_class=reason,
            ),
            mutate=mutate,
        )

    def return_to_draft_for_planning_context_overflow(
        self,
        run_id: RunId,
        authorization: PlanningAuthorization,
        expected_sequence: AuditSequence,
    ) -> AuditSequence:
        def mutate(copied: InMemoryStateStore) -> None:
            if authorization.run_id != run_id or authorization.decision != "ALLOW":
                raise StateConflict("PLANNING_AUTHORIZATION_MISMATCH")
            copied._runs[run_id] = replace(copied._runs[run_id], state=RunState.DRAFT)

        return self._commit_state_and_event(
            run_id=run_id,
            expected_sequence=expected_sequence,
            event=AuditEvent.kind("PLANNING_CONTEXT_OVERFLOW"),
            mutate=mutate,
        )

    def _evaluate_model_reservation(
        self, request: ModelReservationRequest
    ) -> _ReservationEvaluation:
        try:
            budget_digest, budget = self._approved_budgets[request.run_id]
        except KeyError as error:
            raise StateConflict("APPROVED_BUDGET_NOT_FOUND") from error
        run_counters = self._model_counters.get(request.run_id, ModelCounters())
        task_counters = (
            None
            if request.task_id is None
            else self._task_budget_counters.get(
                (request.run_id, request.task_id),
                TaskBudgetState(request.run_id, request.task_id),
            )
        )
        planning_requests = self._planning_request_counts.get(request.run_id, 0)
        try:
            amounts = model_reservation_amounts(request.model_request, budget)
        except ValueError:
            amounts = ModelBudgetAmounts.zero()
            pricing_missing = True
        else:
            pricing_missing = False
        reason: ModelReservationReason | None = None
        if self._sequences.get(request.run_id, AuditSequence(0)) != request.expected_sequence:
            reason = "STALE_SEQUENCE"
        elif request.model_request.budget_digest != budget_digest:
            reason = "REVISION_BINDING_MISMATCH"
        elif request.credential_profile is None:
            reason = "CREDENTIAL_UNAVAILABLE"
        elif (
            request.expected_run_counters != run_counters
            or request.expected_task_counters != task_counters
        ):
            reason = "COUNTER_SNAPSHOT_MISMATCH"
        elif pricing_missing:
            reason = "PRICING_MISSING"
        else:
            run = self._runs.get(request.run_id)
            if run is not None:
                if (
                    run.current_plan_digest != request.model_request.plan_digest
                    or run.current_policy_digest != request.model_request.policy_digest
                    or run.current_budget_digest != request.model_request.budget_digest
                    or run.current_model_configuration_digest
                    != request.model_request.model_configuration_digest
                ):
                    reason = "REVISION_BINDING_MISMATCH"
                else:
                    reservation = next(
                        (
                            item
                            for item in self._target_reservations.values()
                            if item.run_id == request.run_id
                        ),
                        None,
                    )
                    if (
                        reservation is None
                        or reservation.admin_binding_digest != request.target_safety_digest
                    ):
                        reason = "TARGET_BINDING_MISMATCH"
                    elif run.state not in {RunState.PLANNING, RunState.ACTIVE} or (
                        request.run_id in self._dispatch_close_causes
                    ):
                        reason = "RUN_NOT_DISPATCHABLE"
            after = run_counters.reserve(amounts)
            if (
                reason is None
                and request.owner_kind == "PLANNING"
                and request.provider_attempt_number == 1
                and (planning_requests >= budget.planning_request_ceiling)
            ):
                reason = "PLANNING_REQUEST_CEILING"
            elif (
                reason is None
                and request.owner_kind == "WORKER"
                and (
                    task_counters is None
                    or task_counters.active_tranche_id != request.tranche_id
                    or task_counters.active_tranche_remaining_calls < 1
                )
            ):
                reason = "TASK_TRANCHE_EXHAUSTED"
            elif reason is None and after.calls > budget.model_call_ceiling:
                reason = "MODEL_CALL_CEILING"
            elif reason is None and after.input_tokens > budget.input_token_ceiling:
                reason = "INPUT_TOKEN_CEILING"
            elif reason is None and after.output_tokens > budget.output_token_ceiling:
                reason = "OUTPUT_TOKEN_CEILING"
            elif reason is None and after.cost_usd > budget.cost_reserve_usd:
                reason = "COST_RESERVE_CEILING"
        return _ReservationEvaluation(
            reason,
            budget,
            amounts,
            run_counters,
            task_counters,
            planning_requests,
        )

    def _model_reservation_result(
        self,
        request: ModelReservationRequest,
        evaluation: _ReservationEvaluation,
        *,
        decision: Literal["DENY", "PAUSE"],
        resulting_sequence: AuditSequence,
    ) -> ModelReservation:
        if evaluation.reason is None:
            raise AssertionError("denial result requires a reason")
        return ModelReservation(
            decision=decision,
            reason=evaluation.reason,
            run_id=request.run_id,
            task_id=request.task_id,
            attempt_id=request.attempt_id,
            tranche_id=request.tranche_id,
            turn=request.turn,
            intent=None,
            reserved_amounts=ModelBudgetAmounts.zero(),
            run_counters_before=evaluation.run_counters,
            run_counters_after=evaluation.run_counters,
            task_counters_before=evaluation.task_counters,
            task_counters_after=evaluation.task_counters,
            deadline_at_utc=request.deadline_at_utc,
            pause_after_barrier=decision == "PAUSE",
            resulting_sequence=resulting_sequence,
        )

    def reserve_authorized_model_attempt(
        self, request: ModelReservationRequest
    ) -> ModelReservation:
        with self._lock:
            evaluation = self._evaluate_model_reservation(request)
            ceiling_reasons: set[ModelReservationReason] = {
                "PLANNING_REQUEST_CEILING",
                "TASK_TRANCHE_EXHAUSTED",
                "MODEL_CALL_CEILING",
                "INPUT_TOKEN_CEILING",
                "OUTPUT_TOKEN_CEILING",
                "COST_RESERVE_CEILING",
            }
            if evaluation.reason is not None and evaluation.reason not in ceiling_reasons:
                return self._model_reservation_result(
                    request,
                    evaluation,
                    decision="DENY",
                    resulting_sequence=self.audit_sequence(request.run_id),
                )
            pause_reason = evaluation.reason
            if pause_reason is not None:

                def pause(copied: InMemoryStateStore) -> None:
                    current = copied._evaluate_model_reservation(request)
                    if current.reason != pause_reason:
                        raise StateConflict("MODEL_RESERVATION_REVALIDATION_MISMATCH")
                    if request.run_id in copied._runs:
                        copied._close_new_dispatch(
                            request.run_id,
                            DispatchCloseCause.BUDGET_EXHAUSTED,
                        )
                        copied._runs[request.run_id] = replace(
                            copied._runs[request.run_id], state=RunState.PAUSED
                        )
                    else:
                        copied._dispatch_close_causes[request.run_id] = (pause_reason,)

                sequence = self._commit_state_and_event(
                    run_id=request.run_id,
                    expected_sequence=request.expected_sequence,
                    event=AuditEvent.kind("MODEL_RESERVATION_PAUSED", result_class=pause_reason),
                    mutate=pause,
                )
                return self._model_reservation_result(
                    request,
                    evaluation,
                    decision="PAUSE",
                    resulting_sequence=sequence,
                )

            turn: LogicalModelTurn | None = None
            intent: ModelRequestIntent | None = None
            run_after: ModelCounters | None = None
            task_after: TaskBudgetState | None = None
            producer_stopped = False
            events = [
                AuditEvent.kind(
                    "MODEL_ATTEMPT_RESERVED",
                    task_id=request.task_id,
                    attempt_id=request.attempt_id,
                    budget_delta_json=evaluation.amounts.to_json(),
                )
            ]

            def mutate(copied: InMemoryStateStore) -> None:
                nonlocal turn, intent, producer_stopped, run_after, task_after
                current = copied._evaluate_model_reservation(request)
                if current.reason is not None or current != evaluation:
                    raise StateConflict("MODEL_RESERVATION_REVALIDATION_MISMATCH")
                run_bound = request.run_id in copied._runs
                if run_bound:
                    copied._require_dispatch_binding(request.run_id)
                    if not copied._new_dispatch_open[request.run_id]:
                        raise StateConflict("NEW_DISPATCH_CLOSED")
                if request.turn is None:
                    turn = LogicalModelTurn.new(request.model_request)
                    copied._model_turns[turn.logical_turn_id] = turn
                else:
                    turn = request.turn
                    stored = copied._model_turns.get(turn.logical_turn_id)
                    if stored != turn:
                        raise StateConflict("MODEL_TURN_BINDING_MISMATCH")
                intent = replace(
                    ModelRequestIntent.reserve(
                        turn, request.model_request, request.provider_attempt_number
                    ),
                    reserved_amounts=evaluation.amounts,
                )
                attempt_key = (
                    request.run_id,
                    turn.logical_turn_id,
                    request.provider_attempt_number,
                )
                if attempt_key in copied._model_attempt_numbers:
                    raise StateConflict("MODEL_ATTEMPT_DUPLICATE")
                copied._model_attempt_numbers[attempt_key] = intent.intent_id
                copied._model_attempts[intent.intent_id] = intent
                run_after = evaluation.run_counters.reserve(evaluation.amounts)
                copied._model_counters[request.run_id] = run_after
                new_planning_request = (
                    request.owner_kind == "PLANNING" and request.provider_attempt_number == 1
                )
                if new_planning_request:
                    copied._planning_request_counts[request.run_id] = (
                        evaluation.planning_requests + 1
                    )
                elif request.owner_kind == "WORKER":
                    if evaluation.task_counters is None:
                        raise StateConflict("TASK_COUNTERS_REQUIRED")
                    if request.task_id is None:
                        raise StateConflict("TASK_ID_REQUIRED")
                    remaining = evaluation.task_counters.active_tranche_remaining_calls - 1
                    task_after = replace(
                        evaluation.task_counters,
                        consumed_calls=evaluation.task_counters.consumed_calls + 1,
                        input_tokens=evaluation.task_counters.input_tokens
                        + evaluation.amounts.input_tokens,
                        output_tokens=evaluation.task_counters.output_tokens
                        + evaluation.amounts.output_tokens,
                        cost_usd=evaluation.task_counters.cost_usd + evaluation.amounts.cost_usd,
                        active_tranche_id=(
                            None if remaining == 0 else evaluation.task_counters.active_tranche_id
                        ),
                        active_tranche_remaining_calls=remaining,
                    )
                    copied._task_budget_counters[(request.run_id, request.task_id)] = task_after

                if run_bound:
                    usage = (
                        (
                            GlobalBudgetMetric.PLANNING_REQUESTS,
                            evaluation.planning_requests + 1,
                        ),
                        (GlobalBudgetMetric.MODEL_CALLS, run_after.calls),
                        (GlobalBudgetMetric.INPUT_TOKENS, run_after.input_tokens),
                        (GlobalBudgetMetric.OUTPUT_TOKENS, run_after.output_tokens),
                        (GlobalBudgetMetric.COST_RESERVE_USD, run_after.cost_usd),
                    )
                    for metric, amount in usage:
                        if (
                            metric == GlobalBudgetMetric.PLANNING_REQUESTS
                            and not new_planning_request
                        ):
                            continue
                        _, stopped = copied._settle_global_usage_for_producer(
                            request.run_id,
                            request.model_request.budget_digest,
                            metric,
                            amount,
                        )
                        producer_stopped = producer_stopped or stopped
                    if producer_stopped:
                        events.append(AuditEvent.kind("BUDGET_STOP_REQUESTED"))

            sequence = self._commit_state_and_events(
                run_id=request.run_id,
                expected_sequence=request.expected_sequence,
                event_factory=lambda: tuple(events),
                mutate=mutate,
            )
            if turn is None or intent is None or run_after is None:
                raise AssertionError("model reservation state missing after commit")
            return ModelReservation(
                decision="RESERVED",
                reason="AUTHORIZED",
                run_id=request.run_id,
                task_id=request.task_id,
                attempt_id=request.attempt_id,
                tranche_id=request.tranche_id,
                turn=turn,
                intent=intent,
                reserved_amounts=evaluation.amounts,
                run_counters_before=evaluation.run_counters,
                run_counters_after=run_after,
                task_counters_before=evaluation.task_counters,
                task_counters_after=task_after,
                deadline_at_utc=request.deadline_at_utc,
                pause_after_barrier=producer_stopped,
                resulting_sequence=sequence,
            )

    def issue_workspace_lease(
        self,
        lease: WorkspaceLease,
        budget_digest: RevisionDigest,
        expected_sequence: AuditSequence,
    ) -> WorkspaceLease | LeaseDenial:
        producer_stopped = False
        events = [
            AuditEvent.kind(
                "WORKSPACE_LEASE_ISSUED",
                task_id=lease.task_id,
                attempt_id=lease.attempt_id,
            )
        ]

        def mutate(copied: InMemoryStateStore) -> None:
            nonlocal producer_stopped
            budget = copied._require_current_budget(lease.run_id, budget_digest)
            run_bound = lease.run_id in copied._runs
            if run_bound:
                copied._require_dispatch_binding(lease.run_id)
                if not copied._new_dispatch_open[lease.run_id]:
                    raise StateConflict("NEW_DISPATCH_CLOSED")
            active = tuple(
                existing
                for (run_id, _), existing in copied._workspace_leases.items()
                if run_id == lease.run_id
                and existing.state == "ACTIVE"
                and existing.expires_at > lease.issued_at
            )
            if len(active) >= budget.concurrent_worker_ceiling:
                raise _LeaseDenied(LeaseDenial(reason="WORKER_CEILING"))
            if any(
                may_overlap(left, right)
                for existing in active
                for left in existing.write_globs
                for right in lease.write_globs
            ):
                raise _LeaseDenied(LeaseDenial())
            key = (lease.run_id, lease.lease_id)
            if key in copied._workspace_leases:
                raise StateConflict("WORKSPACE_LEASE_DUPLICATE")
            copied._workspace_leases[key] = lease
            if run_bound:
                _, producer_stopped = copied._settle_global_usage_for_producer(
                    lease.run_id,
                    budget_digest,
                    GlobalBudgetMetric.CONCURRENT_WORKERS,
                    len(active) + 1,
                )
                if producer_stopped:
                    events.append(AuditEvent.kind("BUDGET_STOP_REQUESTED"))

        try:
            self._commit_state_and_events(
                run_id=lease.run_id,
                expected_sequence=expected_sequence,
                event_factory=lambda: tuple(events),
                mutate=mutate,
            )
        except _LeaseDenied as denial:
            return denial.denial
        return lease

    def workspace_lease(self, run_id: RunId, lease_id: str) -> WorkspaceLease | None:
        with self._lock:
            return self._workspace_leases.get((run_id, lease_id))

    def expire_workspace_lease(
        self,
        run_id: RunId,
        lease_id: str,
        expected_sequence: AuditSequence,
    ) -> AuditSequence:
        def mutate(copied: InMemoryStateStore) -> None:
            key = (run_id, lease_id)
            lease = copied._workspace_leases.get(key)
            if lease is None or lease.state != "ACTIVE":
                raise StateConflict("WORKSPACE_LEASE_NOT_ACTIVE")
            copied._workspace_leases[key] = replace(lease, state="EXPIRED")
            if run_id in copied._runs:
                budget_digest, _ = copied._approved_budgets[run_id]
                active_count = sum(
                    1
                    for (lease_run, _), current in copied._workspace_leases.items()
                    if lease_run == run_id and current.state == "ACTIVE"
                )
                copied._settle_global_usage_for_producer(
                    run_id,
                    budget_digest,
                    GlobalBudgetMetric.CONCURRENT_WORKERS,
                    active_count,
                )

        return self._commit_state_and_event(
            run_id=run_id,
            expected_sequence=expected_sequence,
            event=AuditEvent.kind("WORKSPACE_LEASE_EXPIRED"),
            mutate=mutate,
        )

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
        renewed: WorkspaceLease | None = None

        def mutate(copied: InMemoryStateStore) -> None:
            nonlocal renewed
            key = (run_id, lease_id)
            current = copied._workspace_leases.get(key)
            if (
                current is None
                or current.state != "ACTIVE"
                or current.expires_at <= renewed_at
                or current.generation != generation
            ):
                raise _LeaseDenied(LeaseDenial())
            others = tuple(
                lease
                for other_key, lease in copied._workspace_leases.items()
                if other_key != key
                and lease.run_id == run_id
                and lease.state == "ACTIVE"
                and lease.expires_at > renewed_at
            )
            if any(
                may_overlap(left, right)
                for other in others
                for left in current.write_globs
                for right in other.write_globs
            ):
                raise _LeaseDenied(LeaseDenial())
            renewed = replace(
                current,
                admissible_head=latest_admissible_head,
                expires_at=expires_at,
            )
            copied._workspace_leases[key] = renewed

        try:
            self._commit_state_and_event(
                run_id=run_id,
                expected_sequence=expected_sequence,
                event=AuditEvent.kind("WORKSPACE_LEASE_RENEWED"),
                mutate=mutate,
            )
        except _LeaseDenied as denial:
            return denial.denial
        if renewed is None:
            raise AssertionError("renewed lease missing after committed mutation")
        return renewed

    def authorization_binding_failure(
        self, request: AuthorizationRequest
    ) -> AuthorizationReason | None:
        with self._lock:
            run = self._runs.get(request.run_id)
            if run is None:
                return "RUN_NOT_DISPATCHABLE"
            if (
                run.current_plan_digest != request.plan_digest
                or run.current_policy_digest != request.policy_digest
                or run.current_budget_digest != request.budget_digest
                or run.current_model_configuration_digest != request.model_configuration_digest
            ):
                return "REVISION_BINDING_MISMATCH"
            reservation = next(
                (
                    item
                    for item in self._target_reservations.values()
                    if item.run_id == request.run_id
                ),
                None,
            )
            if (
                reservation is None
                or reservation.admin_binding_digest != request.target_safety_digest
            ):
                return "TARGET_BINDING_MISMATCH"
            if run.state is not RunState.ACTIVE:
                return "RUN_NOT_DISPATCHABLE"
            return None

    def record_authorization_denial(
        self,
        request: AuthorizationRequest,
        binding_digest: str,
        reason: AuthorizationReason,
        expected_sequence: AuditSequence,
    ) -> AuditSequence:
        def mutate(copied: InMemoryStateStore) -> None:
            key = (request.run_id, request.action_id)
            if key in copied._authorization_denials:
                raise StateConflict("ACTION_AUTHORIZATION_DENIAL_DUPLICATE")
            copied._authorization_denials[key] = (binding_digest, reason)

        return self._commit_state_and_event(
            run_id=request.run_id,
            expected_sequence=expected_sequence,
            event=AuditEvent.kind(
                "ACTION_AUTHORIZATION_DENIED",
                task_id=request.task_id,
                attempt_id=request.attempt_id,
                action_id=request.action_id,
                result_class=reason,
                subject_digests=(request.action_digest, binding_digest),
            ),
            mutate=mutate,
        )

    def task_budget_state(self, run_id: RunId, task_id: TaskId) -> TaskBudgetState:
        with self._lock:
            return self._task_budget_counters.get(
                (run_id, task_id), TaskBudgetState(run_id=run_id, task_id=task_id)
            )

    def active_run_time_state(self, run_id: RunId) -> ActiveRunTimeState:
        with self._lock:
            return self._active_run_times.get(
                run_id, ActiveRunTimeState(run_id, 0, None, None, None)
            )

    def last_runtime_audit_event(
        self, run_id: RunId, owner_generation: int
    ) -> RuntimeAuditStamp | None:
        with self._lock:
            for sequence, event in reversed(self._audit_events.get(run_id, [])):
                if (
                    event.runtime_owner_generation == owner_generation
                    and event.runtime_monotonic_nanoseconds is not None
                ):
                    return RuntimeAuditStamp(
                        sequence,
                        owner_generation,
                        MonotonicInstant(event.runtime_monotonic_nanoseconds),
                    )
            return None

    def allocate_task_tranche(
        self,
        task: TaskAuthority,
        expected: TaskBudgetState,
        calls: int,
        reason: TrancheReason,
        progress: ProgressEvidence,
        expected_sequence: AuditSequence,
    ) -> TrancheDecision:
        progressed = progress_from_checks(
            progress.previous,
            progress.current,
            progress.previous_lifecycle,
            progress.current_lifecycle,
        )
        progress_json = canonical_json(
            {
                "current_failures": sorted(progress.current.failures),
                "current_fresh_passes": sorted(progress.current.fresh_passes),
                "current_lifecycle": progress.current_lifecycle,
                "previous_failures": sorted(progress.previous.failures),
                "previous_fresh_passes": sorted(progress.previous.fresh_passes),
                "previous_lifecycle": progress.previous_lifecycle,
                "progressed": progressed,
            }
        )
        key = (task.run_id, task.task_id)
        current = self.task_budget_state(*key)
        if current != expected:
            raise StateConflict("TASK_COUNTER_SNAPSHOT_MISMATCH")
        tranche_number = current.tranche_count + 1
        tranche_id = (
            None if calls == 0 else f"tranche-{task.run_id}-{task.task_id}-{tranche_number}"
        )
        decision: Literal["ALLOCATE", "PAUSE"]
        if calls == 0:
            after = replace(
                current,
                consecutive_no_progress_tranches=(
                    current.consecutive_no_progress_tranches + 1
                    if reason == "NO_PROGRESS"
                    else current.consecutive_no_progress_tranches
                ),
            )
            event_kind = "TASK_PAUSED_NO_PROGRESS"
            decision = "PAUSE"
        else:
            after = replace(
                current,
                allocated_calls=current.allocated_calls + calls,
                tranche_count=tranche_number,
                bootstrap_tranches=current.bootstrap_tranches + (1 if reason == "BOOTSTRAP" else 0),
                consecutive_no_progress_tranches=(
                    0
                    if reason == "OBJECTIVE_PROGRESS"
                    else current.consecutive_no_progress_tranches
                ),
                active_tranche_id=tranche_id,
                active_tranche_remaining_calls=calls,
            )
            event_kind = "TASK_TRANCHE_ALLOCATED"
            decision = "ALLOCATE"

        def mutate(copied: InMemoryStateStore) -> None:
            current_copy = copied.task_budget_state(*key)
            if current_copy != expected:
                raise StateConflict("TASK_COUNTER_SNAPSHOT_MISMATCH")
            if current_copy.active_tranche_remaining_calls:
                raise StateConflict("TASK_TRANCHE_STILL_ACTIVE")
            if calls == 0:
                if reason not in {"NO_PROGRESS", "TASK_CALL_CEILING"}:
                    raise StateConflict("TASK_TRANCHE_PAUSE_REASON_INVALID")
            elif not 1 <= calls <= 8 or reason not in {"BOOTSTRAP", "OBJECTIVE_PROGRESS"}:
                raise StateConflict("TASK_TRANCHE_ALLOCATION_INVALID")
            copied._task_budget_counters[key] = after
            if calls == 0 and key in copied._tasks and task.run_id in copied._runs:
                pause_reason = "NO_PROGRESS" if reason == "NO_PROGRESS" else "TASK_CALL_CEILING"
                copied._pause_task(task.run_id, task.task_id, pause_reason, 1)
                budget_digest, _ = copied._approved_budgets[task.run_id]
                copied._record_task_pause_binding(
                    run_id=task.run_id,
                    task_id=task.task_id,
                    attempt_id=task.attempt_id,
                    pause_sequence=AuditSequence(expected_sequence + 1),
                    pause_reason=pause_reason,
                    budget_digest=budget_digest,
                )
            if tranche_id is not None:
                tranche_key = (task.run_id, task.task_id, tranche_id)
                if tranche_key in copied._task_tranches:
                    raise StateConflict("TASK_TRANCHE_DUPLICATE")
                copied._task_tranches[tranche_key] = (
                    task.attempt_id,
                    tranche_number,
                    calls,
                    progress_json,
                    sha256_digest(progress_json),
                )

        resulting_sequence = self._commit_state_and_event(
            run_id=task.run_id,
            expected_sequence=expected_sequence,
            event=AuditEvent.kind(event_kind, task_id=task.task_id, attempt_id=task.attempt_id),
            mutate=mutate,
        )
        return TrancheDecision(
            decision=decision,
            reason=reason,
            run_id=task.run_id,
            task_id=task.task_id,
            attempt_id=task.attempt_id,
            tranche_id=tranche_id,
            tranche_number=tranche_number,
            calls=calls,
            counters_before=current,
            counters_after=after,
            resulting_sequence=resulting_sequence,
        )

    def append_event(
        self,
        run_id: RunId,
        event: AuditEvent,
        expected_sequence: AuditSequence,
    ) -> AuditSequence:
        return self._commit_state_and_event(
            run_id=run_id,
            expected_sequence=expected_sequence,
            event=event,
            mutate=lambda copied: None,
        )

    def record_tool_denial(
        self, denial: ToolDenialAudit, expected_sequence: AuditSequence
    ) -> AuditSequence:
        return self._commit_state_and_event(
            run_id=denial.run_id,
            expected_sequence=expected_sequence,
            event=AuditEvent.kind(
                "TOOL_ACTION_DENIED",
                task_id=denial.task_id,
                attempt_id=denial.attempt_id,
                action_id=denial.action_id,
                applicable_revision_digests=denial.applicable_revision_digests,
                result_class=denial.result_code,
            ),
            mutate=lambda copied: None,
        )

    def record_intent(self, intent: EffectIntent, expected_sequence: AuditSequence) -> EffectIntent:
        self._validate_effect_intent(intent, expected_sequence)

        def mutate(copied: InMemoryStateStore) -> None:
            if intent.intent_id in copied._effect_intents or any(
                existing.idempotency_key == intent.idempotency_key
                for existing in copied._effect_intents.values()
            ):
                raise StateConflict("EFFECT_INTENT_DUPLICATE")
            copied._effect_intents[intent.intent_id] = intent

        self._commit_state_and_event(
            run_id=intent.run_id,
            expected_sequence=expected_sequence,
            event=AuditEvent.kind(
                "EFFECT_INTENT_RECORDED",
                task_id=intent.task_id,
                attempt_id=intent.attempt_id,
                action_id=intent.action_id,
                applicable_revision_digests=intent.applicable_revision_digests,
                subject_digests=(intent.payload_digest,),
            ),
            mutate=mutate,
        )
        return intent

    @staticmethod
    def _validate_action_deadline_binding(
        deadline: ActionDeadline,
        intent: EffectIntent,
    ) -> None:
        try:
            action_class, check_id, snapshot_digest = action_deadline_binding(intent)
        except AuthorityDenied as error:
            raise StateConflict(str(error)) from error
        if (
            deadline.run_id != intent.run_id
            or deadline.intent_id != intent.intent_id
            or deadline.applicable_revision_digests != intent.applicable_revision_digests
            or deadline.action_class != action_class
            or deadline.check_id != check_id
            or deadline.snapshot_digest != snapshot_digest
        ):
            raise StateConflict("ACTION_DEADLINE_INTENT_BINDING_MISMATCH")

    def _require_unsettled_effect_intent(
        self,
        run_id: RunId,
        intent_id: IntentId,
    ) -> EffectIntent:
        intent = self._effect_intents.get(intent_id)
        if (
            intent is None
            or intent.run_id != run_id
            or intent_id in self._effect_results
            or intent_id in self._indeterminate_effect_intents
        ):
            raise StateConflict("UNSETTLED_EFFECT_INTENT_REQUIRED")
        return intent

    def record_action_deadline(
        self,
        deadline: ActionDeadline,
        expected_sequence: AuditSequence,
    ) -> ActionDeadline:
        if deadline.recorded_sequence != AuditSequence(expected_sequence + 1):
            raise StateConflict("ACTION_DEADLINE_SEQUENCE_MISMATCH")

        def mutate(copied: InMemoryStateStore) -> None:
            copied._require_current_revisions(deadline.run_id, deadline.applicable_revision_digests)
            copied._require_current_budget(deadline.run_id, deadline.budget_digest)
            intent = copied._require_unsettled_effect_intent(deadline.run_id, deadline.intent_id)
            copied._validate_action_deadline_binding(deadline, intent)
            if deadline.intent_id in copied._action_deadlines:
                raise StateConflict("ACTION_DEADLINE_ALREADY_RECORDED")
            copied._action_deadlines[deadline.intent_id] = deadline

        self._commit_state_and_event(
            run_id=deadline.run_id,
            expected_sequence=expected_sequence,
            event=AuditEvent.kind(
                "ACTION_DEADLINE_RECORDED",
                applicable_revision_digests=deadline.applicable_revision_digests,
            ),
            mutate=mutate,
        )
        return deadline

    def settle_action_timeout(
        self,
        deadline: ActionDeadline,
        decision: TimeoutDecision,
        expected_sequence: AuditSequence,
    ) -> TimeoutDecision:
        def mutate(copied: InMemoryStateStore) -> None:
            copied._require_current_revisions(deadline.run_id, deadline.applicable_revision_digests)
            copied._require_current_budget(deadline.run_id, deadline.budget_digest)
            intent = copied._require_unsettled_effect_intent(deadline.run_id, deadline.intent_id)
            copied._validate_action_deadline_binding(deadline, intent)
            current = copied._action_deadlines.get(deadline.intent_id)
            if current != deadline or deadline.intent_id in copied._timeout_decisions:
                raise StateConflict("ACTION_TIMEOUT_NOT_CURRENT")
            if deadline.action_class == ActionClass.ORDINARY:
                if decision.outcome != "INDETERMINATE":
                    raise StateConflict("ORDINARY_TIMEOUT_SUCCESSOR_REQUIRED")
                copied._indeterminate_effect_intents.add(deadline.intent_id)
            else:
                copied._require_dispatch_binding(deadline.run_id)
                if (
                    decision.outcome != "INFRASTRUCTURE_UNCERTAINTY"
                    or decision.retry_scope != (deadline.check_id, deadline.snapshot_digest)
                    or decision.retry_allowed != copied._new_dispatch_open[deadline.run_id]
                ):
                    raise StateConflict("CHECK_TIMEOUT_SUCCESSOR_BINDING_MISMATCH")
            copied._timeout_decisions[deadline.intent_id] = decision

        self._commit_state_and_event(
            run_id=deadline.run_id,
            expected_sequence=expected_sequence,
            event=AuditEvent.kind(
                "ACTION_TIMEOUT_SETTLED",
                applicable_revision_digests=deadline.applicable_revision_digests,
                result_class=decision.outcome,
            ),
            mutate=mutate,
        )
        return decision

    def action_deadline(self, intent_id: IntentId) -> ActionDeadline | None:
        with self._lock:
            return self._action_deadlines.get(intent_id)

    def timeout_decision(self, intent_id: IntentId) -> TimeoutDecision | None:
        with self._lock:
            return self._timeout_decisions.get(intent_id)

    def _validate_effect_intent(
        self, intent: EffectIntent, expected_sequence: AuditSequence
    ) -> None:
        if intent.recorded_sequence != AuditSequence(expected_sequence + 1):
            raise StateConflict("EFFECT_INTENT_SEQUENCE_MISMATCH")
        if not intent.kind or intent.kind.strip() != intent.kind:
            raise StateConflict("EFFECT_INTENT_KIND_INVALID")
        if not intent.idempotency_key or intent.idempotency_key.strip() != intent.idempotency_key:
            raise StateConflict("EFFECT_INTENT_IDEMPOTENCY_KEY_INVALID")
        _require_canonical_json_object(
            intent.normalized_payload_json, "EFFECT_INTENT_PAYLOAD_NOT_CANONICAL"
        )
        if sha256_digest(intent.normalized_payload_json) != intent.payload_digest:
            raise StateConflict("EFFECT_INTENT_PAYLOAD_DIGEST_MISMATCH")
        _require_canonical_json_object(
            intent.expected_prestate_json, "EFFECT_INTENT_PRESTATE_NOT_CANONICAL"
        )

    def settle_intent(
        self,
        run_id: RunId,
        intent_id: IntentId,
        result: EffectResult,
        applicable_revision_digests: ApplicableRevisionDigests,
        expected_sequence: AuditSequence,
    ) -> AuditSequence:
        if result.settled_sequence != AuditSequence(expected_sequence + 1):
            raise StateConflict("EFFECT_RESULT_SEQUENCE_MISMATCH")
        if result.run_id != run_id or result.intent_id != intent_id:
            raise StateConflict("EFFECT_RESULT_RUN_OR_INTENT_MISMATCH")
        if result.outcome not in {"COMPLETED", "FAILED", "STALE", "CONFLICT", "INDETERMINATE"}:
            raise StateConflict("EFFECT_RESULT_OUTCOME_INVALID")
        if not result.result_class or result.result_class.strip() != result.result_class:
            raise StateConflict("EFFECT_RESULT_CLASS_INVALID")
        _require_canonical_json_object(
            result.bounded_result_json, "EFFECT_RESULT_PAYLOAD_NOT_CANONICAL"
        )
        if sha256_digest(result.bounded_result_json) != result.result_digest:
            raise StateConflict("EFFECT_RESULT_DIGEST_MISMATCH")

        def mutate(copied: InMemoryStateStore) -> None:
            intent = copied._effect_intents.get(intent_id)
            if intent is None or intent.run_id != run_id or intent_id in copied._effect_results:
                raise StateConflict("UNSETTLED_EFFECT_INTENT_REQUIRED")
            if intent.applicable_revision_digests != applicable_revision_digests:
                raise StateConflict("EFFECT_RESULT_REVISION_BINDING_MISMATCH")
            from apexcrew.domain.tools import ToolEffectResultError, validate_tool_effect_result

            try:
                validate_tool_effect_result(intent, result)
            except ToolEffectResultError as error:
                raise StateConflict(error.code) from error
            copied._effect_results[intent_id] = result

        return self._commit_state_and_event(
            run_id=run_id,
            expected_sequence=expected_sequence,
            event=AuditEvent.kind(
                "EFFECT_INTENT_SETTLED",
                applicable_revision_digests=applicable_revision_digests,
                result_class=result.result_class,
                subject_digests=(result.result_digest,),
            ),
            mutate=mutate,
        )

    def effect_intent_or_none(self, intent_id: IntentId) -> EffectIntent | None:
        with self._lock:
            return self._effect_intents.get(intent_id)

    def effect_intent(self, intent_id: IntentId) -> EffectIntent:
        with self._lock:
            return self._effect_intents[intent_id]

    def effect_result(self, intent_id: IntentId) -> EffectResult:
        with self._lock:
            return self._effect_results[intent_id]

    def unsettled_intents(self, run_id: RunId) -> tuple[EffectIntent, ...]:
        with self._lock:
            return tuple(
                sorted(
                    (
                        intent
                        for intent_id, intent in self._effect_intents.items()
                        if intent.run_id == run_id
                        and intent_id not in self._effect_results
                        and intent_id not in self._indeterminate_effect_intents
                    ),
                    key=lambda intent: (intent.recorded_sequence, intent.intent_id),
                )
            )

    def reserve_model_request(
        self, request: ModelRequest, expected_sequence: AuditSequence
    ) -> ModelRequestIntent:
        _, intent = self.begin_model_turn_and_reserve(request, expected_sequence)
        return intent

    def begin_model_turn_and_reserve(
        self, request: ModelRequest, expected_sequence: AuditSequence
    ) -> tuple[LogicalModelTurn, ModelRequestIntent]:
        turn = LogicalModelTurn.new(request)
        intent = ModelRequestIntent.reserve(turn, request, provider_attempt_number=1)

        def mutate(copied: InMemoryStateStore) -> None:
            key = (request.run_id, turn.logical_turn_id, 1)
            if key in copied._model_attempt_numbers:
                raise StateConflict("MODEL_ATTEMPT_NUMBER_REUSED")
            copied._model_turns[turn.logical_turn_id] = turn
            copied._model_attempt_numbers[key] = intent.intent_id
            copied._model_attempts[intent.intent_id] = intent
            counters = copied._model_counters.get(request.run_id, ModelCounters())
            copied._model_counters[request.run_id] = counters.reserve(intent.reserved_amounts)

        self._commit_state_and_event(
            run_id=request.run_id,
            expected_sequence=expected_sequence,
            event=AuditEvent.kind("MODEL_TURN_AND_ATTEMPT_RESERVED"),
            mutate=mutate,
        )
        return turn, intent

    def reserve_model_attempt(
        self,
        turn: LogicalModelTurn,
        request: ModelRequest,
        provider_attempt_number: int,
        expected_sequence: AuditSequence,
    ) -> ModelRequestIntent:
        intent = ModelRequestIntent.reserve(turn, request, provider_attempt_number)

        def mutate(copied: InMemoryStateStore) -> None:
            if copied._model_turns.get(turn.logical_turn_id) != turn:
                raise StateConflict("MODEL_TURN_BINDING_MISMATCH")
            key = (request.run_id, turn.logical_turn_id, provider_attempt_number)
            if key in copied._model_attempt_numbers:
                raise StateConflict("MODEL_ATTEMPT_NUMBER_REUSED")
            copied._model_attempt_numbers[key] = intent.intent_id
            copied._model_attempts[intent.intent_id] = intent
            counters = copied._model_counters.get(request.run_id, ModelCounters())
            copied._model_counters[request.run_id] = counters.reserve(intent.reserved_amounts)

        self._commit_state_and_event(
            run_id=request.run_id,
            expected_sequence=expected_sequence,
            event=AuditEvent.kind("MODEL_RETRY_ATTEMPT_RESERVED"),
            mutate=mutate,
        )
        return intent

    def model_counters(self, run_id: RunId) -> ModelCounters:
        return self._model_counters.get(run_id, ModelCounters())

    def model_attempts(
        self, run_id: RunId, logical_turn_id: LogicalTurnId
    ) -> tuple[SettledModelAttempt, ...]:
        return tuple(
            sorted(
                (
                    attempt
                    for attempt in self._model_attempts.values()
                    if isinstance(attempt, SettledModelAttempt)
                    and attempt.run_id == run_id
                    and attempt.logical_turn_id == logical_turn_id
                ),
                key=lambda attempt: attempt.provider_attempt_number,
            )
        )

    def committed_model_turn(
        self, run_id: RunId, logical_turn_id: LogicalTurnId
    ) -> CommittedModelTurn | None:
        turn = self._model_turns.get(logical_turn_id)
        return turn if isinstance(turn, CommittedModelTurn) and turn.run_id == run_id else None

    def settle_model_attempt(
        self,
        intent: ModelRequestIntent,
        result: ProviderAttemptResult,
        expected_sequence: AuditSequence,
    ) -> SettledModelAttempt:
        settled = SettledModelAttempt.from_result(intent, result)

        def mutate(copied: InMemoryStateStore) -> None:
            current = copied._model_attempts.get(intent.intent_id)
            if current is None or current.run_id != intent.run_id:
                raise StateConflict("MODEL_ATTEMPT_BINDING_MISMATCH")
            if current.state != "RESERVED":
                raise StateConflict("MODEL_ATTEMPT_ALREADY_SETTLED")
            if current != intent:
                raise StateConflict("MODEL_ATTEMPT_BINDING_MISMATCH")
            copied._model_attempts[intent.intent_id] = settled
            counters = copied.model_counters(intent.run_id).settle(
                settled.reserved_amounts, settled.charged_amounts
            )
            copied._model_counters[intent.run_id] = counters
            if intent.run_id in copied._runs:
                for metric, amount in (
                    (GlobalBudgetMetric.MODEL_CALLS, counters.calls),
                    (GlobalBudgetMetric.INPUT_TOKENS, counters.input_tokens),
                    (GlobalBudgetMetric.OUTPUT_TOKENS, counters.output_tokens),
                    (GlobalBudgetMetric.COST_RESERVE_USD, counters.cost_usd),
                ):
                    copied._settle_global_usage_for_producer(
                        intent.run_id,
                        intent.request.budget_digest,
                        metric,
                        amount,
                        allow_reservation_reconciliation=True,
                    )
            if settled.kind is ProviderAttemptKind.COMPLETED:
                dispatch = settled.dispatch_result
                if dispatch.outcome == "COMPLETED":
                    if (
                        dispatch.returned_model_id is None
                        or dispatch.normalized_payload_digest is None
                        or dispatch.normalized_action is None
                    ):
                        raise StateConflict("MODEL_COMPLETION_NOT_RELEASABLE")
                    turn = copied._model_turns[intent.logical_turn_id]
                    if not isinstance(turn, LogicalModelTurn) or turn.run_id != intent.run_id:
                        raise StateConflict("MODEL_TURN_BINDING_MISMATCH")
                    copied._model_turns[intent.logical_turn_id] = CommittedModelTurn(
                        run_id=intent.run_id,
                        logical_turn_id=intent.logical_turn_id,
                        owner_kind=intent.request.owner_kind,
                        task_id=intent.request.task_id,
                        attempt_id=intent.request.attempt_id,
                        tranche_id=intent.request.tranche_id,
                        recovery_binding=ModelRecoveryBinding.from_request(intent.request),
                        returned_model_id=dispatch.returned_model_id,
                        normalized_output_digest=dispatch.normalized_payload_digest,
                        normalized_payload=dispatch.normalized_action,
                        dispatch_result=dispatch,
                        committed_sequence=AuditSequence(expected_sequence + 1),
                        state="COMPLETION_COMMITTED",
                        downstream_intent_id=None,
                        downstream_sequence=None,
                    )

        self._commit_state_and_event(
            run_id=intent.run_id,
            expected_sequence=expected_sequence,
            event=AuditEvent.kind("MODEL_ATTEMPT_SETTLED"),
            mutate=mutate,
        )
        return settled

    def record_downstream_action_intent(
        self,
        run_id: RunId,
        logical_turn_id: LogicalTurnId,
        intent: EffectIntent,
        expected_sequence: AuditSequence,
    ) -> AuditSequence:
        if intent.run_id != run_id:
            raise StateConflict("DOWNSTREAM_INTENT_RUN_MISMATCH")

        def mutate(copied: InMemoryStateStore) -> None:
            turn = copied._model_turns[logical_turn_id]
            if (
                not isinstance(turn, CommittedModelTurn)
                or turn.run_id != run_id
                or turn.state != "COMPLETION_COMMITTED"
                or turn.downstream_intent_id is not None
            ):
                raise StateConflict("DOWNSTREAM_INTENT_ALREADY_RECORDED")
            if intent.intent_id in copied._effect_intents:
                raise StateConflict("EFFECT_INTENT_REUSED")
            copied._effect_intents[intent.intent_id] = intent
            copied._model_turns[logical_turn_id] = replace(
                turn,
                state="DOWNSTREAM_INTENT_RECORDED",
                downstream_intent_id=intent.intent_id,
                downstream_sequence=AuditSequence(expected_sequence + 1),
            )

        return self._commit_state_and_event(
            run_id=run_id,
            expected_sequence=expected_sequence,
            event=AuditEvent.kind("MODEL_DOWNSTREAM_INTENT_RECORDED"),
            mutate=mutate,
        )

    def record_model_backoff(
        self,
        run_id: RunId,
        intent_id: IntentId,
        seconds: int,
        expected_sequence: AuditSequence,
    ) -> AuditSequence:
        def mutate(copied: InMemoryStateStore) -> None:
            attempt = copied._model_attempts.get(intent_id)
            if (
                not isinstance(attempt, SettledModelAttempt)
                or attempt.run_id != run_id
                or attempt.kind != "KNOWN_CLOSED_REJECTION"
                or attempt.backoff_seconds is not None
            ):
                raise StateConflict("BACKOFF_REQUIRES_CLOSED_REJECTION")
            copied._model_attempts[intent_id] = replace(attempt, backoff_seconds=seconds)

        return self._commit_state_and_event(
            run_id=run_id,
            expected_sequence=expected_sequence,
            event=AuditEvent.kind("MODEL_RETRY_BACKOFF_RECORDED"),
            mutate=mutate,
        )

    def settle_model_request(
        self,
        intent: ModelRequestIntent,
        completion: ModelCompletion,
        allowed_model_ids: frozenset[str],
        expected_sequence: AuditSequence,
    ) -> ModelDispatchResult:
        if allowed_model_ids != intent.request.allowed_model_ids:
            raise StateConflict("MODEL_INTENT_BINDING_MISMATCH")
        return self.settle_model_attempt(
            intent,
            ProviderAttemptResult.completed(completion),
            expected_sequence,
        ).dispatch_result

    def model_request(self, run_id: RunId, intent_id: IntentId) -> ModelRequestIntent:
        with self._lock:
            intent = self._model_attempts[intent_id]
            if not isinstance(intent, ModelRequestIntent):
                raise KeyError(intent_id)
            if intent.run_id != run_id:
                raise KeyError(intent_id)
            return intent

    def reserved_call_count(self, run_id: RunId) -> int:
        with self._lock:
            return sum(intent.run_id == run_id for intent in self._model_attempts.values())

    def target_authority_digest(self, run_id: RunId) -> Sha256DigestText:
        with self._lock:
            run = self._runs.get(run_id)
            reservation = next(
                (item for item in self._target_reservations.values() if item.run_id == run_id),
                None,
            )
            if run is None or reservation is None:
                raise StateConflict("RUN_OR_TARGET_RESERVATION_NOT_FOUND")
            return _target_authority_digest(run, reservation)

    def current_revision_digests(self, run_id: RunId) -> ApplicableRevisionDigests:
        with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                raise StateConflict("RUN_NOT_FOUND")
            return ApplicableRevisionDigests(
                plan_digest=run.current_plan_digest,
                policy_digest=run.current_policy_digest,
                budget_digest=run.current_budget_digest,
                model_configuration_digest=run.current_model_configuration_digest,
            )

    def _approved_revision_bindings(self, run_id: RunId) -> ApplicableRevisionDigests:
        current = self.current_revision_digests(run_id)
        approved = {
            revision_class
            for (stored_run_id, revision_class, digest) in self._revision_approvals
            if stored_run_id == run_id
            and digest
            == {
                "PLAN": current.plan_digest,
                "POLICY": current.policy_digest,
                "BUDGET": current.budget_digest,
                "MODEL_CONFIGURATION": current.model_configuration_digest,
            }[revision_class]
        }
        return ApplicableRevisionDigests(
            plan_digest=current.plan_digest if "PLAN" in approved else None,
            policy_digest=current.policy_digest if "POLICY" in approved else None,
            budget_digest=current.budget_digest if "BUDGET" in approved else None,
            model_configuration_digest=(
                current.model_configuration_digest if "MODEL_CONFIGURATION" in approved else None
            ),
        )

    def approved_revision_classes(self, run_id: RunId) -> tuple[str, ...]:
        with self._lock:
            approved = self._approved_revision_bindings(run_id)
            values = {
                "PLAN": approved.plan_digest,
                "POLICY": approved.policy_digest,
                "BUDGET": approved.budget_digest,
                "MODEL_CONFIGURATION": approved.model_configuration_digest,
            }
            return tuple(
                item
                for item in ("PLAN", "POLICY", "BUDGET", "MODEL_CONFIGURATION")
                if values[item] is not None
            )

    def current_budget_digest(self, run_id: RunId) -> RevisionDigest | None:
        return self.current_revision_digests(run_id).budget_digest

    def current_model_configuration_digest(self, run_id: RunId) -> RevisionDigest | None:
        return self.current_revision_digests(run_id).model_configuration_digest

    def pending_budget_replacement(self, run_id: RunId) -> RevisionDigest | None:
        with self._lock:
            pending = self._pending_revision_replacements.get((run_id, "BUDGET"))
            return None if pending is None else pending[0]

    def run_count(self) -> int:
        with self._lock:
            return len(self._runs)

    def target_reservation_count(self, run_id: RunId) -> int:
        with self._lock:
            return sum(
                reservation.run_id == run_id for reservation in self._target_reservations.values()
            )

    def public_run_snapshot(
        self, run_id: RunId, at_sequence: int | None
    ) -> PublicRunSnapshot | None:
        with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                return None
            current = self._sequences.get(run_id, AuditSequence(0))
            requested = int(current) if at_sequence is None else at_sequence
            if requested < 0 or requested > current:
                return None
            if requested != current and not any(
                sequence == requested for sequence, _ in self._audit_events.get(run_id, ())
            ):
                return None
            return PublicRunSnapshot(AuditSequence(requested), run.state)

    def runtime_permit(self, run_id: RunId, generation: int) -> RuntimePermit:
        with self._lock:
            permit = self._runtime_permits.get((run_id, generation))
            if permit is None:
                raise StateConflict("RUNTIME_PERMIT_NOT_FOUND")
            return permit

    def unconsumed_permit(self, run_id: RunId) -> RuntimePermit:
        with self._lock:
            permits = [
                permit
                for (stored_run_id, _), permit in self._runtime_permits.items()
                if stored_run_id == run_id and permit.state == "UNCONSUMED"
            ]
            if len(permits) != 1:
                raise StateConflict("RUNTIME_PERMIT_NOT_FOUND")
            return permits[0]

    @staticmethod
    def _issue_runtime_permit_on_copy(
        copied: InMemoryStateStore,
        command: CommandEnvelope,
        allowed_phase: RuntimeAllowedPhase,
        applicable_revision_digests: ApplicableRevisionDigests,
        target_authority_digest: Sha256DigestText,
        issued_sequence: AuditSequence,
    ) -> RuntimePermit:
        if isinstance(command.payload, CreateRunPayload):
            raise TypeError("runtime Permit source must identify a Run")
        run_id = RunId(command.payload.run_id)
        run = copied._runs.get(run_id)
        if run is None:
            raise StateConflict("RUN_NOT_FOUND")
        if (
            run.state.value != allowed_phase
            or copied.current_revision_digests(run_id) != applicable_revision_digests
            or copied.target_authority_digest(run_id) != target_authority_digest
        ):
            raise StateConflict("RUNTIME_PERMIT_BINDING_MISMATCH")
        source_digest = Sha256DigestText(_command_digest(command))
        if (
            any(
                (permit.run_id == run_id and permit.state == "UNCONSUMED")
                or (
                    permit.source_request_id == command.request_id
                    and permit.source_envelope_digest == source_digest
                )
                for permit in copied._runtime_permits.values()
            )
            or run_id in copied._runtime_owners
        ):
            raise StateConflict("RUNTIME_DELIVERY_PENDING")
        generation = (
            max(
                (
                    generation
                    for stored_run_id, generation in copied._runtime_permits
                    if stored_run_id == run_id
                ),
                default=0,
            )
            + 1
        )
        permit = RuntimePermit(
            run_id=run_id,
            generation=generation,
            source_request_id=RequestId(command.request_id),
            source_envelope_digest=source_digest,
            issued_sequence=issued_sequence,
            allowed_phase=allowed_phase,
            applicable_revision_digests=applicable_revision_digests,
            target_authority_digest=target_authority_digest,
            expected_runtime_progress_generation=copied._runtime_progress_generations.get(
                run_id, 0
            ),
            state="UNCONSUMED",
        )
        copied._runtime_permits[(run_id, generation)] = permit
        return permit

    def issue_runtime_permit(
        self,
        command: CommandEnvelope,
        allowed_phase: RuntimeAllowedPhase,
        applicable_revision_digests: ApplicableRevisionDigests,
        target_authority_digest: Sha256DigestText,
        expected_sequence: AuditSequence,
    ) -> RuntimePermit:
        if not isinstance(command.payload, (BeginPlanningPayload, ResumePayload)):
            raise StateConflict("RUNTIME_PERMIT_SOURCE_COMMAND_INVALID")
        required_phase: RuntimeAllowedPhase = (
            "DRAFT" if isinstance(command.payload, BeginPlanningPayload) else "PAUSED"
        )
        if allowed_phase != required_phase:
            raise StateConflict("RUNTIME_PERMIT_PHASE_MISMATCH")
        issued: list[RuntimePermit] = []

        def mutate(copied: InMemoryStateStore) -> None:
            receipt = copied._command_receipts.get(command.request_id)
            if receipt is None or receipt[2] != _command_digest(command):
                raise StateConflict("RUNTIME_PERMIT_SOURCE_COMMAND_NOT_ACCEPTED")
            outcome = CommandOutcome.validate_for_payload(command.payload, _json_object(receipt[3]))
            if outcome.status != CommandStatus.ACCEPTED:
                raise StateConflict("RUNTIME_PERMIT_SOURCE_COMMAND_NOT_ACCEPTED")
            issued.append(
                self._issue_runtime_permit_on_copy(
                    copied,
                    command,
                    allowed_phase,
                    applicable_revision_digests,
                    target_authority_digest,
                    AuditSequence(expected_sequence + 1),
                )
            )

        self._commit_state_and_event(
            run_id=RunId(command.payload.run_id),
            expected_sequence=expected_sequence,
            event=AuditEvent.kind("RUNTIME_PERMIT_ISSUED"),
            mutate=mutate,
        )
        return issued[0]

    def consume_current_runtime_permit(
        self,
        run_id: RunId,
        owner_id: RuntimeOwnerId,
        expected_sequence: AuditSequence,
    ) -> RuntimePermit | None:
        with self._lock:
            try:
                existing = self.unconsumed_permit(run_id)
            except StateConflict:
                return None
        consumed: list[RuntimePermit] = []
        event_kinds: list[str] = []
        opened_at: list[MonotonicInstant] = []

        def mutate(copied: InMemoryStateStore) -> None:
            permit = copied._runtime_permits[(run_id, existing.generation)]
            if permit.state != "UNCONSUMED":
                raise StateConflict("RUNTIME_PERMIT_CONSUME_COMPARE_AND_SET_FAILED")
            run = copied._runs[run_id]
            ordinary_phase_matches = permit.allowed_phase == run.state.value
            terminal_matches = permit.allowed_phase == "TERMINAL_ADMINISTRATION" and run.state in {
                RunState.COMPLETED,
                RunState.FAILED,
                RunState.CANCELLED,
            }
            valid = (
                (ordinary_phase_matches or terminal_matches)
                and permit.applicable_revision_digests == copied.current_revision_digests(run_id)
                and permit.target_authority_digest == copied.target_authority_digest(run_id)
                and permit.expected_runtime_progress_generation
                == copied._runtime_progress_generations.get(run_id, 0)
            )
            if not valid:
                copied._runtime_permits[(run_id, permit.generation)] = permit.model_copy(
                    update={"state": "INVALIDATED"}
                )
                event_kinds.append("RUNTIME_PERMIT_INVALIDATED")
                return
            if run_id in copied._runtime_owners:
                raise StateConflict("RUNTIME_DELIVERY_PENDING")
            active = copied._active_run_times.get(
                run_id, ActiveRunTimeState(run_id, 0, None, None, None)
            )
            if active.open_owner_generation is not None:
                raise StateConflict("RUNTIME_DELIVERY_PENDING")
            if copied._monotonic_clock is None:
                raise StateConflict("MONOTONIC_CLOCK_NOT_CONFIGURED")
            now = copied._monotonic_clock.now()
            opened_at.append(now)
            consumed_sequence = AuditSequence(expected_sequence + 1)
            result = permit.model_copy(
                update={
                    "state": "CONSUMED",
                    "consumed_owner_id": owner_id,
                    "consumed_sequence": consumed_sequence,
                }
            )
            copied._runtime_permits[(run_id, permit.generation)] = result
            owner_generation = copied._runtime_owners.get(run_id, (owner_id, 0))[1] + 1
            copied._runtime_owners[run_id] = (owner_id, owner_generation)
            copied._runtime_progress_generations[run_id] = (
                copied._runtime_progress_generations.get(run_id, 0) + 1
            )
            copied._active_run_times[run_id] = ActiveRunTimeState(
                run_id=run_id,
                cumulative_nanoseconds=active.cumulative_nanoseconds,
                open_owner_generation=owner_generation,
                opened_at=now,
                latest_committed_at=now,
            )
            consumed.append(result)
            event_kinds.append("RUNTIME_PERMIT_CONSUMED")

        self._commit_state_and_events(
            run_id=run_id,
            expected_sequence=expected_sequence,
            event_factory=lambda: (AuditEvent.kind(event_kinds[0]),),
            mutate=mutate,
            runtime_now_factory=lambda: opened_at[0],
        )
        return consumed[0] if consumed else None

    def _existing_control_outcome(self, command: CommandEnvelope) -> CommandOutcome | None:
        with self._lock:
            receipt = self._command_receipts.get(command.request_id)
            if receipt is None:
                return None
            _, _, stored_digest, outcome_json, _ = receipt
            stored = CommandOutcome.validate_for_payload(
                command.payload, _json_object(outcome_json)
            )
            if stored_digest == _command_digest(command):
                return stored
            return CommandOutcome.for_payload(
                command.payload,
                status=CommandStatus.CONFLICT,
                run_id=stored.run_id,
                resulting_sequence=stored.resulting_sequence,
                failed_invariant="IDEMPOTENCY_KEY_REUSE",
            )

    def _record_control_outcome(
        self,
        command: CommandEnvelope,
        run_id: RunId,
        status: CommandStatus,
        failed_invariant: str | None,
        event_kind: str,
        mutate_domain: Callable[[InMemoryStateStore], None] | None = None,
    ) -> CommandOutcome:
        if command.expected_sequence is None:
            raise StateConflict("EXPECTED_SEQUENCE_REQUIRED")
        expected = AuditSequence(command.expected_sequence)
        outcome = CommandOutcome.for_payload(
            command.payload,
            status=status,
            run_id=run_id,
            resulting_sequence=AuditSequence(expected + 1),
            failed_invariant=failed_invariant,
        )

        def mutate(copied: InMemoryStateStore) -> None:
            run = copied._runs.get(run_id)
            if run is None:
                raise StateConflict("RUN_NOT_FOUND")
            if mutate_domain is not None:
                mutate_domain(copied)
            copied._command_receipts[command.request_id] = (
                run.repository_id,
                run_id,
                _command_digest(command),
                _outcome_json(outcome),
                AuditSequence(expected + 1),
            )

        self._commit_state_and_event(
            run_id=run_id,
            expected_sequence=expected,
            event=AuditEvent.kind(
                event_kind,
                applicable_revision_digests=command.applicable_revision_digests,
                result_class=status,
            ),
            mutate=mutate,
        )
        return outcome

    def create_bootstrap_run(
        self,
        command: CommandEnvelope,
        repository_authority: RepositoryBootstrapAuthorityService,
    ) -> CommandOutcome:
        payload = command.payload
        if not isinstance(payload, CreateRunPayload):
            raise TypeError("create payload required")
        if command.expected_sequence is not None or command.applicable_revision_digests != (
            ApplicableRevisionDigests()
        ):
            return CommandOutcome.for_payload(
                payload,
                status=CommandStatus.INVALID,
                run_id=None,
                resulting_sequence=None,
                failed_invariant="CREATE_RUN_BINDING_INVALID",
            )
        authority = repository_authority.inspect(payload.repository_root, payload.target_ref)
        priced = {entry.returned_model_id for entry in payload.budget_revision.pricing_entries}
        returned = {
            alias.returned_model_id
            for alias in payload.model_configuration_revision.returned_model_aliases
        }
        if (
            not payload.target_ref.startswith("refs/heads/")
            or payload.target_ref == "refs/heads/"
            or any(character.isspace() or character == "\x00" for character in payload.target_ref)
        ):
            return CommandOutcome.for_payload(
                payload,
                status=CommandStatus.INVALID,
                run_id=None,
                resulting_sequence=None,
                failed_invariant="TARGET_REF_NOT_DIRECT_LOCAL_BRANCH",
            )
        if (
            authority.repository_root != payload.repository_root
            or authority.target_ref != payload.target_ref
            or authority.target_oid != payload.expected_target_oid
        ):
            return CommandOutcome.for_payload(
                payload,
                status=CommandStatus.INVALID,
                run_id=None,
                resulting_sequence=None,
                failed_invariant="CREATE_RUN_BINDING_INVALID",
            )
        if not returned.issubset(priced):
            return CommandOutcome.for_payload(
                payload,
                status=CommandStatus.INVALID,
                run_id=None,
                resulting_sequence=None,
                failed_invariant="MODEL_CONFIGURATION_UNPRICED",
            )
        binding = _command_digest(command)[7:]
        run_id = RunId(f"run-{binding[:32]}")
        repository_id = authority.repository_id
        repository_instance_digest = authority.repository_instance_digest
        reservation_id = f"reservation-{binding[32:64]}"
        revisions: dict[str, FrozenDocument] = {
            "POLICY": payload.policy_revision,
            "BUDGET": payload.budget_revision,
            "MODEL_CONFIGURATION": payload.model_configuration_revision,
        }
        digests = {
            revision_class: revision_digest(document)
            for revision_class, document in revisions.items()
        }
        outcome = CommandOutcome.for_payload(
            payload,
            status=CommandStatus.ACCEPTED,
            run_id=run_id,
            resulting_sequence=AuditSequence(1),
        )

        def mutate(copied: InMemoryStateStore) -> None:
            copied._runs[run_id] = RunRecord(
                run_id=run_id,
                repository_id=repository_id,
                repository_instance_digest=repository_instance_digest,
                state=RunState.DRAFT,
                target_ref=payload.target_ref,
                pinned_target_oid=payload.expected_target_oid,
                current_policy_digest=digests["POLICY"],
                current_budget_digest=digests["BUDGET"],
                current_model_configuration_digest=digests["MODEL_CONFIGURATION"],
            )
            copied._new_dispatch_open[run_id] = True
            copied._runtime_progress_generations[run_id] = 0
            copied._target_reservations[reservation_id] = TargetReservation(
                reservation_id=reservation_id,
                run_id=run_id,
                target_ref=payload.target_ref,
                pinned_target_oid=payload.expected_target_oid,
                path=Path.cwd() / "data" / "reservations" / reservation_id,
                phase="ALLOCATED",
            )
            copied._bootstrap_inputs[run_id] = (
                payload.goal,
                payload.constraints,
                payload.acceptance_criteria,
            )
            for revision_class, document in revisions.items():
                copied._revision_documents[
                    (
                        run_id,
                        revision_class,
                        digests[revision_class],
                    )
                ] = (document, AuditSequence(1), "CURRENT")
            copied._command_receipts[command.request_id] = (
                repository_id,
                run_id,
                _command_digest(command),
                _outcome_json(outcome),
                AuditSequence(1),
            )

        self._commit_state_and_event(
            run_id=run_id,
            expected_sequence=AuditSequence(0),
            event=AuditEvent.kind("RUN_CREATED"),
            mutate=mutate,
        )
        return outcome

    def propose_revision(
        self, command: CommandEnvelope, run_id: RunId, state: RunState
    ) -> CommandOutcome:
        expected_sequence = command.expected_sequence
        if expected_sequence is None:
            raise StateConflict("EXPECTED_SEQUENCE_REQUIRED")
        payload = command.payload
        document: FrozenDocument
        if isinstance(payload, ProposePolicyPayload):
            revision_class, document = (
                "POLICY",
                payload.policy_revision,
            )
        elif isinstance(payload, ProposeBudgetPayload):
            revision_class, document = (
                "BUDGET",
                payload.budget_revision,
            )
        elif isinstance(payload, ProposeModelConfigurationPayload):
            revision_class, document = (
                "MODEL_CONFIGURATION",
                payload.model_configuration_revision,
            )
        else:
            raise TypeError("revision proposal required")
        if state in {RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED} or (
            state in _EXECUTION_REVISION_STATES and revision_class == "POLICY"
        ):
            return self._record_control_outcome(
                command,
                run_id,
                CommandStatus.INVALID,
                "REVISION_FROZEN",
                "CONTROL_COMMAND_REJECTED",
            )
        required = (
            self.current_revision_digests(run_id)
            if state in _EXECUTION_REVISION_STATES
            else self._approved_revision_bindings(run_id)
        )
        if command.applicable_revision_digests != required:
            return self._record_control_outcome(
                command,
                run_id,
                CommandStatus.STALE,
                "REVISION_BINDING_MISMATCH",
                "CONTROL_COMMAND_REJECTED",
            )
        if isinstance(payload, ProposeModelConfigurationPayload):
            budget_digest = self.current_revision_digests(run_id).budget_digest
            assert budget_digest is not None
            budget_record = self._revision_documents[(run_id, "BUDGET", budget_digest)]
            budget = BudgetRevisionDocument.model_validate(
                budget_record[0].model_dump(mode="python")
            )
            priced = {entry.returned_model_id for entry in budget.pricing_entries}
            returned = {
                alias.returned_model_id
                for alias in payload.model_configuration_revision.returned_model_aliases
            }
            if not returned.issubset(priced):
                return self._record_control_outcome(
                    command,
                    run_id,
                    CommandStatus.INVALID,
                    "MODEL_CONFIGURATION_UNPRICED",
                    "CONTROL_COMMAND_REJECTED",
                )
        digest = revision_digest(document)

        def mutate(copied: InMemoryStateStore) -> None:
            run = copied._runs[run_id]
            current_digest = _run_revision_digest(run, revision_class)
            if current_digest == digest:
                return
            copied._revision_documents[(run_id, revision_class, digest)] = (
                document,
                AuditSequence(expected_sequence + 1),
                "PROPOSED" if state in _EXECUTION_REVISION_STATES else "CURRENT",
            )
            if state not in _EXECUTION_REVISION_STATES:
                for key, value in tuple(copied._revision_documents.items()):
                    if key[0] == run_id and key[1] == revision_class and key[2] != digest:
                        copied._revision_documents[key] = (value[0], value[1], "STALE")
                copied._runs[run_id] = _replace_run_revision(
                    run, revision_class, digest, return_to_draft=True
                )
                copied._revision_approvals = {
                    key: value
                    for key, value in copied._revision_approvals.items()
                    if key[0] != run_id or key[1] not in {revision_class, "PLAN"}
                }
                copied._runtime_permits = {
                    key: (
                        permit.model_copy(update={"state": "INVALIDATED"})
                        if permit.run_id == run_id and permit.state == "UNCONSUMED"
                        else permit
                    )
                    for key, permit in copied._runtime_permits.items()
                }

        return self._record_control_outcome(
            command,
            run_id,
            CommandStatus.ACCEPTED,
            None,
            "REVISION_PROPOSED",
            mutate,
        )

    def approve_revision(
        self, command: CommandEnvelope, run_id: RunId, state: RunState
    ) -> CommandOutcome:
        expected_sequence = command.expected_sequence
        if expected_sequence is None:
            raise StateConflict("EXPECTED_SEQUENCE_REQUIRED")
        payload = command.payload
        if isinstance(payload, ApprovePolicyPayload):
            revision_class, digest, code = (
                "POLICY",
                payload.policy_digest,
                payload.confirmation_code,
            )
        elif isinstance(payload, ApproveBudgetPayload):
            revision_class, digest, code = (
                "BUDGET",
                payload.budget_digest,
                payload.confirmation_code,
            )
        elif isinstance(payload, ApproveModelConfigurationPayload):
            revision_class, digest, code = (
                "MODEL_CONFIGURATION",
                payload.model_configuration_digest,
                payload.confirmation_code,
            )
        elif isinstance(payload, ApprovePlanPayload):
            revision_class, digest, code = (
                "PLAN",
                payload.plan_digest,
                payload.confirmation_code,
            )
        else:
            raise TypeError("revision approval required")
        if state in {RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED} or (
            state in _EXECUTION_REVISION_STATES and revision_class in {"PLAN", "POLICY"}
        ):
            return self._record_control_outcome(
                command,
                run_id,
                CommandStatus.INVALID,
                "REVISION_FROZEN",
                "CONTROL_COMMAND_REJECTED",
            )
        expected_code = _approval_confirmation_code(payload.kind, run_id, revision_class, digest)
        if not compare_digest(code, expected_code):
            return self._record_control_outcome(
                command,
                run_id,
                CommandStatus.DENIED,
                "REVISION_CONFIRMATION_CODE_MISMATCH",
                "CONTROL_COMMAND_REJECTED",
            )
        required = (
            self.current_revision_digests(run_id)
            if state in _EXECUTION_REVISION_STATES
            else self._approved_revision_bindings(run_id)
        )
        if command.applicable_revision_digests != required:
            return self._record_control_outcome(
                command,
                run_id,
                CommandStatus.STALE,
                "REVISION_BINDING_MISMATCH",
                "CONTROL_COMMAND_REJECTED",
            )
        record = self._revision_documents.get((run_id, revision_class, digest))
        if record is None or record[2] == "STALE":
            return self._record_control_outcome(
                command,
                run_id,
                CommandStatus.STALE,
                "REVISION_PROPOSAL_NOT_CURRENT",
                "CONTROL_COMMAND_REJECTED",
            )

        def mutate(copied: InMemoryStateStore) -> None:
            key = (run_id, revision_class, digest)
            copied._revision_approvals.setdefault(
                key,
                (
                    command.request_id,
                    AuditSequence(expected_sequence + 1),
                    expected_code,
                ),
            )
            run = copied._runs[run_id]
            in_flight = any(
                action.run_id == run_id and action.state == "IN_FLIGHT"
                for action in copied._atomic_actions.values()
            )
            if (
                state in _EXECUTION_REVISION_STATES
                and _run_revision_digest(run, revision_class) != digest
                and in_flight
            ):
                copied._pending_revision_replacements[(run_id, revision_class)] = (
                    digest,
                    AuditSequence(expected_sequence + 1),
                )
                causes = set(copied._dispatch_close_causes.get(run_id, ()))
                causes.add(DispatchCloseCause.REVISION_REPLACEMENT.value)
                copied._dispatch_close_causes[run_id] = tuple(sorted(causes))
                copied._new_dispatch_open[run_id] = False
                return
            if _run_revision_digest(run, revision_class) != digest:
                for candidate, value in tuple(copied._revision_documents.items()):
                    if candidate[0] == run_id and candidate[1] == revision_class:
                        copied._revision_documents[candidate] = (
                            value[0],
                            value[1],
                            "CURRENT" if candidate[2] == digest else "STALE",
                        )
                copied._runs[run_id] = _replace_run_revision(run, revision_class, digest)
            if revision_class == "BUDGET":
                copied._approved_budgets[run_id] = (
                    digest,
                    BudgetRevisionDocument.model_validate(record[0].model_dump(mode="python")),
                )
            copied._pending_revision_replacements.pop((run_id, revision_class), None)

        return self._record_control_outcome(
            command,
            run_id,
            CommandStatus.ACCEPTED,
            None,
            "REVISION_APPROVED",
            mutate,
        )

    def _apply_begin_planning(
        self,
        command: CommandEnvelope,
        run_id: RunId,
        target_authority: TargetAuthorityDigestService,
    ) -> CommandOutcome:
        expected_sequence = command.expected_sequence
        if expected_sequence is None:
            raise StateConflict("EXPECTED_SEQUENCE_REQUIRED")
        current = self.current_revision_digests(run_id)
        approved = self._approved_revision_bindings(run_id)
        if command.applicable_revision_digests != current:
            return self._record_control_outcome(
                command,
                run_id,
                CommandStatus.STALE,
                "REVISION_BINDING_MISMATCH",
                "CONTROL_COMMAND_REJECTED",
            )
        required = (
            current.policy_digest,
            current.budget_digest,
            current.model_configuration_digest,
        )
        if (
            any(item is None for item in required)
            or (
                approved.policy_digest,
                approved.budget_digest,
                approved.model_configuration_digest,
            )
            != required
        ):
            return self._record_control_outcome(
                command,
                run_id,
                CommandStatus.DENIED,
                "BOOTSTRAP_REVISIONS_NOT_APPROVED",
                "CONTROL_COMMAND_REJECTED",
            )
        target_digest = self.target_authority_digest(run_id)
        if target_authority.current_for(run_id) != target_digest:
            return self._record_control_outcome(
                command,
                run_id,
                CommandStatus.STALE,
                "TARGET_AUTHORITY_BINDING_MISMATCH",
                "CONTROL_COMMAND_REJECTED",
            )

        def mutate(copied: InMemoryStateStore) -> None:
            self._issue_runtime_permit_on_copy(
                copied,
                command,
                "DRAFT",
                current,
                target_digest,
                AuditSequence(expected_sequence + 1),
            )

        try:
            return self._record_control_outcome(
                command,
                run_id,
                CommandStatus.ACCEPTED,
                None,
                "RUNTIME_PERMIT_ISSUED",
                mutate,
            )
        except StateConflict as error:
            if str(error) != "RUNTIME_DELIVERY_PENDING":
                raise
            return self._record_control_outcome(
                command,
                run_id,
                CommandStatus.CONFLICT,
                "RUNTIME_DELIVERY_PENDING",
                "CONTROL_COMMAND_REJECTED",
            )

    def _apply_task_resume(self, command: CommandEnvelope, run_id: RunId) -> CommandOutcome:
        expected_sequence = command.expected_sequence
        if expected_sequence is None:
            raise StateConflict("EXPECTED_SEQUENCE_REQUIRED")
        payload = command.payload
        if not isinstance(payload, ResumePayload) or payload.task_id is None:
            return self._record_control_outcome(
                command,
                run_id,
                CommandStatus.INVALID,
                "RUN_RESUME_NOT_OWNED_BY_TASK_10",
                "CONTROL_COMMAND_REJECTED",
            )
        task_id = payload.task_id
        pause = self.current_task_pause(run_id, task_id)
        counters = self.task_counters(run_id, task_id)
        current = self.current_revision_digests(run_id)
        if (
            command.applicable_revision_digests != current
            or pause is None
            or pause.pause_sequence != payload.pause_sequence
            or pause.pause_reason != payload.pause_reason
            or pause.counter_snapshot_digest != counters.digest
            or pause.applicable_revision_digests_at_pause != current
        ):
            return self._record_control_outcome(
                command,
                run_id,
                CommandStatus.STALE,
                "TASK_PAUSE_BINDING_MISMATCH",
                "CONTROL_COMMAND_REJECTED",
            )
        remaining = V01_MECHANISM_LIMITS.task_call_ceiling - counters.allocated_calls
        calls = min(V01_MECHANISM_LIMITS.renewal_tranche_calls, remaining)
        if (
            payload.pause_reason
            not in {"NO_PROGRESS", "REPEATED_CHECKPOINT", "REPEATED_INVALID_ACTION"}
            or calls < 1
            or counters.manual_resumes >= V01_MECHANISM_LIMITS.manual_resume_ceiling
        ):
            return self._record_control_outcome(
                command,
                run_id,
                CommandStatus.DENIED,
                "TASK_PAUSE_NOT_RESUMABLE",
                "CONTROL_COMMAND_REJECTED",
            )
        budget_digest = current.budget_digest
        if budget_digest is None:
            raise StateConflict("CURRENT_BUDGET_NOT_FOUND")
        request = ResumeTaskRequest(
            run_id=run_id,
            task_id=task_id,
            pause_sequence=payload.pause_sequence,
            pause_reason=payload.pause_reason,
            applicable_revision_digests=current,
            expected_sequence=AuditSequence(expected_sequence),
        )
        allocation_id, new_attempt_id = task_resume_ids(
            request, pause, counters, budget_digest, calls
        )
        target_digest = self.target_authority_digest(run_id)

        def mutate(copied: InMemoryStateStore) -> None:
            key = (run_id, task_id)
            budget = copied._task_budget_counters[key]
            copied._task_budget_counters[key] = replace(
                budget, manual_resumes=budget.manual_resumes + 1
            )
            copied._task_resume_allocations[allocation_id] = TaskResumeAllocation(
                allocation_id=allocation_id,
                run_id=run_id,
                task_id=task_id,
                reserved_attempt_id=new_attempt_id,
                budget_digest=budget_digest,
                applicable_revision_digests=current,
                allocated_calls=calls,
                state="RESERVED",
                created_sequence=AuditSequence(expected_sequence + 1),
            )
            copied._tasks[key] = ("READY", None, None)
            copied._active_task_pauses.remove(key)
            causes = set(copied._dispatch_close_causes.get(run_id, ()))
            causes.remove(DispatchCloseCause.TASK_PAUSED.value)
            copied._dispatch_close_causes[run_id] = tuple(sorted(causes))
            copied._new_dispatch_open[run_id] = not causes
            self._issue_runtime_permit_on_copy(
                copied,
                command,
                "PAUSED",
                current,
                target_digest,
                AuditSequence(expected_sequence + 1),
            )

        return self._record_control_outcome(
            command,
            run_id,
            CommandStatus.ACCEPTED,
            None,
            "TASK_RESUME_AND_RUNTIME_PERMIT_ISSUED",
            mutate,
        )

    def _approve_plan(
        self, command: CommandEnvelope, run_id: RunId, state: RunState
    ) -> CommandOutcome:
        payload = command.payload
        if not isinstance(payload, ApprovePlanPayload):
            raise TypeError("Plan approval required")
        expected = command.expected_sequence
        if expected is None:
            raise StateConflict("EXPECTED_SEQUENCE_REQUIRED")
        if state != RunState.AWAITING_PLAN_APPROVAL:
            return self._record_control_outcome(
                command,
                run_id,
                CommandStatus.INVALID,
                "PLAN_APPROVAL_REQUIRES_PROPOSAL",
                "CONTROL_COMMAND_REJECTED",
            )
        expected_code = _approval_confirmation_code(
            payload.kind, run_id, "PLAN", payload.plan_digest
        )
        if not compare_digest(payload.confirmation_code, expected_code):
            return self._record_control_outcome(
                command,
                run_id,
                CommandStatus.DENIED,
                "REVISION_CONFIRMATION_CODE_MISMATCH",
                "CONTROL_COMMAND_REJECTED",
            )
        proposal = self._plan_proposals.get((run_id, payload.plan_digest))
        current = self.current_revision_digests(run_id)
        if proposal is None:
            return self._record_control_outcome(
                command,
                run_id,
                CommandStatus.STALE,
                "PLAN_PROPOSAL_NOT_CURRENT",
                "CONTROL_COMMAND_REJECTED",
            )
        if (
            command.applicable_revision_digests != current
            or current.plan_digest is not None
            or proposal.applicable_revision_digests != current
        ):
            return self._record_control_outcome(
                command,
                run_id,
                CommandStatus.STALE,
                "PLAN_REVISION_BINDING_MISMATCH",
                "CONTROL_COMMAND_REJECTED",
            )
        binding_digest = sha256_digest(
            canonical_json(
                {
                    "plan_digest": proposal.plan_digest,
                    "proposal_json": proposal.canonical_plan_json,
                    "revision_digests": current.model_dump(mode="json"),
                    "run_id": run_id,
                }
            )
        )

        def mutate(copied: InMemoryStateStore) -> None:
            run = copied._runs[run_id]
            stored = copied._plan_proposals.get((run_id, proposal.plan_digest))
            if (
                run.state != RunState.AWAITING_PLAN_APPROVAL
                or run.current_plan_digest is not None
                or stored != proposal
            ):
                raise StateConflict("PLAN_APPROVAL_BINDING_CHANGED")
            copied._plan_approvals[run_id] = PlanApproval(
                run_id,
                proposal.plan_digest,
                command.request_id,
                AuditSequence(expected + 1),
                binding_digest,
            )
            copied._runs[run_id] = replace(
                run,
                state=RunState.READY_TO_START,
                current_plan_digest=proposal.plan_digest,
            )
            copied._run_refs[(run_id, "PRIVATE")] = RunRefRecord(
                run_id,
                "PRIVATE",
                f"refs/apexcrew/runs/{run_id}",
                None,
                None,
                "ABSENT_EXPECTED",
                None,
            )

        return self._record_control_outcome(
            command,
            run_id,
            CommandStatus.ACCEPTED,
            None,
            "PLAN_APPROVED",
            mutate,
        )

    def _apply_start(
        self,
        command: CommandEnvelope,
        run_id: RunId,
        target_authority: TargetAuthorityDigestService,
        start_guard: StartGuard | None,
    ) -> CommandOutcome:
        payload = command.payload
        if not isinstance(payload, StartPayload):
            raise TypeError("start command required")
        expected = command.expected_sequence
        if expected is None:
            raise StateConflict("EXPECTED_SEQUENCE_REQUIRED")
        current = self.current_revision_digests(run_id)
        approval = self.plan_approval(run_id)
        if (
            payload.plan_digest != current.plan_digest
            or approval.plan_digest != payload.plan_digest
            or command.applicable_revision_digests != current
        ):
            return CommandOutcome.for_payload(
                payload,
                status=CommandStatus.STALE,
                run_id=run_id,
                resulting_sequence=AuditSequence(expected),
                failed_invariant="PLAN_APPROVAL_BINDING_MISMATCH",
            )
        target_digest = self.target_authority_digest(run_id)
        if target_authority.current_for(run_id) != target_digest or start_guard is None:
            return CommandOutcome.for_payload(
                payload,
                status=CommandStatus.CONFLICT,
                run_id=run_id,
                resulting_sequence=AuditSequence(expected),
                failed_invariant="START_GUARD_UNAVAILABLE",
            )
        decision = start_guard.inspect(
            run_id=run_id,
            applicable_revision_digests=current,
            expected_sequence=AuditSequence(expected),
        )
        if not decision.ok or decision.binding is None:
            return CommandOutcome.for_payload(
                payload,
                status=CommandStatus.CONFLICT,
                run_id=run_id,
                resulting_sequence=AuditSequence(expected),
                failed_invariant=decision.reason or "START_GUARD_DENIED",
            )
        guard = decision.binding
        run = self.run_record(run_id)
        reservation = self.target_reservation_for_run(run_id)
        if (
            guard.run_id != run_id
            or guard.repository_id != run.repository_id
            or guard.target_reservation_id != reservation.reservation_id
            or guard.pinned_target_oid != run.pinned_target_oid
            or guard.target_safety_digest != target_digest
            or guard.applicable_revision_digests != current
        ):
            return CommandOutcome.for_payload(
                payload,
                status=CommandStatus.STALE,
                run_id=run_id,
                resulting_sequence=AuditSequence(expected),
                failed_invariant="START_GUARD_BINDING_MISMATCH",
            )

        def mutate(copied: InMemoryStateStore) -> None:
            ref = copied._run_refs.get((run_id, "PRIVATE"))
            if ref is None or ref.state != "ABSENT_EXPECTED":
                raise StateConflict("PRIVATE_REF_PRESTATE_MISMATCH")
            copied._run_refs[(run_id, "PRIVATE")] = replace(
                ref, guard_binding_json=guard.model_dump_json()
            )
            self._issue_runtime_permit_on_copy(
                copied,
                command,
                "READY_TO_START",
                current,
                target_digest,
                AuditSequence(expected + 1),
            )

        return self._record_control_outcome(
            command,
            run_id,
            CommandStatus.ACCEPTED,
            None,
            "RUNTIME_PERMIT_ISSUED",
            mutate,
        )

    def apply_control_command(
        self,
        command: CommandEnvelope,
        target_authority: TargetAuthorityDigestService,
        repository_authority: RepositoryBootstrapAuthorityService,
        start_guard: StartGuard | None = None,
    ) -> CommandOutcome:
        existing = self._existing_control_outcome(command)
        if existing is not None:
            return existing
        if isinstance(command.payload, CreateRunPayload):
            return self.create_bootstrap_run(command, repository_authority)
        run_id = RunId(command.payload.run_id)
        with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                return CommandOutcome.for_payload(
                    command.payload,
                    status=CommandStatus.INVALID,
                    run_id=run_id,
                    resulting_sequence=None,
                    failed_invariant="RUN_NOT_FOUND",
                )
            sequence = self._sequences.get(run_id, AuditSequence(0))
        if command.expected_sequence != sequence:
            return CommandOutcome.for_payload(
                command.payload,
                status=CommandStatus.STALE,
                run_id=run_id,
                resulting_sequence=sequence,
                failed_invariant="STALE_SEQUENCE",
            )
        if isinstance(command.payload, ApprovePlanPayload):
            return self._approve_plan(command, run_id, run.state)
        if isinstance(
            command.payload,
            (ProposePolicyPayload, ProposeBudgetPayload, ProposeModelConfigurationPayload),
        ):
            return self.propose_revision(command, run_id, run.state)
        if isinstance(
            command.payload,
            (
                ApprovePolicyPayload,
                ApproveBudgetPayload,
                ApproveModelConfigurationPayload,
            ),
        ):
            return self.approve_revision(command, run_id, run.state)
        if isinstance(command.payload, BeginPlanningPayload):
            if run.state != RunState.DRAFT:
                return self._record_control_outcome(
                    command,
                    run_id,
                    CommandStatus.INVALID,
                    "BEGIN_PLANNING_REQUIRES_DRAFT",
                    "CONTROL_COMMAND_REJECTED",
                )
            return self._apply_begin_planning(command, run_id, target_authority)
        if isinstance(command.payload, StartPayload):
            if run.state != RunState.READY_TO_START:
                return self._record_control_outcome(
                    command,
                    run_id,
                    CommandStatus.INVALID,
                    "START_REQUIRES_READY_TO_START",
                    "CONTROL_COMMAND_REJECTED",
                )
            return self._apply_start(command, run_id, target_authority, start_guard)
        if isinstance(command.payload, ResumePayload):
            if run.state != RunState.PAUSED:
                return self._record_control_outcome(
                    command,
                    run_id,
                    CommandStatus.INVALID,
                    "RESUME_REQUIRES_PAUSED",
                    "CONTROL_COMMAND_REJECTED",
                )
            return self._apply_task_resume(command, run_id)
        return self._record_control_outcome(
            command,
            run_id,
            CommandStatus.INVALID,
            "COMMAND_NOT_AVAILABLE_IN_TASK_10",
            "CONTROL_COMMAND_REJECTED",
        )

    def plan_approval(self, run_id: RunId) -> PlanApproval:
        with self._lock:
            try:
                return self._plan_approvals[run_id]
            except KeyError as error:
                raise StateConflict("PLAN_APPROVAL_NOT_FOUND") from error

    def run_ref(self, run_id: RunId, ref_kind: Literal["PRIVATE", "TARGET"]) -> RunRefRecord:
        with self._lock:
            try:
                return self._run_refs[(run_id, ref_kind)]
            except KeyError as error:
                raise StateConflict("RUN_REF_NOT_FOUND") from error

    def runtime_start_binding(self, run_id: RunId) -> RuntimeStartBinding:
        with self._lock:
            run = self._runs.get(run_id)
            ref = self._run_refs.get((run_id, "PRIVATE"))
            permits = tuple(
                permit
                for (candidate, _), permit in self._runtime_permits.items()
                if candidate == run_id and permit.state == "CONSUMED"
            )
            permit = None if not permits else max(permits, key=lambda item: item.generation)
            if (
                run is None
                or run.state != RunState.READY_TO_START
                or ref is None
                or ref.guard_binding_json is None
                or permit is None
                or permit.consumed_owner_id is None
                or permit.consumed_sequence is None
            ):
                raise StateConflict("RUNTIME_START_BINDING_NOT_CURRENT")
            return RuntimeStartBinding(
                run_id=run_id,
                sequence=self._sequences[run_id],
                state=RunState.READY_TO_START,
                permit_generation=permit.generation,
                consumed_owner_id=permit.consumed_owner_id,
                consumed_sequence=permit.consumed_sequence,
                guard=StartGuardBinding.model_validate_json(ref.guard_binding_json),
            )

    def record_private_ref_init_intent(
        self,
        *,
        binding: RuntimeStartBinding,
        intent: RefCasIntent,
        expected_sequence: AuditSequence,
    ) -> AuditSequence:
        effect = intent.to_effect_intent(AuditSequence(expected_sequence + 1))

        def mutate(copied: InMemoryStateStore) -> None:
            run = copied._runs.get(binding.run_id)
            ref = copied._run_refs.get((binding.run_id, "PRIVATE"))
            permit = copied._runtime_permits.get((binding.run_id, binding.permit_generation))
            if (
                run is None
                or run.state != RunState.READY_TO_START
                or ref is None
                or ref.state != "ABSENT_EXPECTED"
                or ref.guard_binding_json is None
                or StartGuardBinding.model_validate_json(ref.guard_binding_json) != binding.guard
                or permit is None
                or permit.consumed_owner_id != binding.consumed_owner_id
                or permit.consumed_sequence != binding.consumed_sequence
                or copied._sequences.get(binding.run_id) != binding.sequence
                or intent.permit_generation != binding.permit_generation
            ):
                raise StateConflict("RUNTIME_START_BINDING_CHANGED")
            if effect.intent_id in copied._effect_intents:
                raise StateConflict("EFFECT_INTENT_DUPLICATE")
            copied._effect_intents[effect.intent_id] = effect
            copied._run_refs[(binding.run_id, "PRIVATE")] = replace(
                ref, state="INIT_INTENT_RECORDED", last_intent_id=intent.intent_id
            )
            copied._runs[binding.run_id] = replace(run, state=RunState.ACTIVE)

        return self._commit_state_and_event(
            run_id=intent.run_id,
            expected_sequence=expected_sequence,
            event=AuditEvent.kind(
                "PRIVATE_REF_INIT_INTENT_RECORDED",
                applicable_revision_digests=intent.applicable_revision_digests,
            ),
            mutate=mutate,
        )

    def settle_private_ref_init(
        self,
        *,
        binding: RuntimeStartBinding,
        intent: RefCasIntent,
        outcome: PrivateRefCasOutcome,
        expected_sequence: AuditSequence,
    ) -> AuditSequence:
        del binding
        result = outcome.to_effect_result(AuditSequence(expected_sequence + 1))

        def mutate(copied: InMemoryStateStore) -> None:
            ref = copied._run_refs.get((intent.run_id, "PRIVATE"))
            if (
                ref is None
                or ref.state != "INIT_INTENT_RECORDED"
                or ref.last_intent_id != intent.intent_id
                or intent.intent_id in copied._effect_results
            ):
                raise StateConflict("PRIVATE_REF_INTENT_NOT_CURRENT")
            copied._effect_results[intent.intent_id] = result
            run = copied._runs[intent.run_id]
            if outcome.result_class == "PRIVATE_REF_INITIALIZED":
                if outcome.observed_oid != intent.prepared_oid:
                    raise StateConflict("PRIVATE_REF_OUTCOME_OID_MISMATCH")
                copied._run_refs[(intent.run_id, "PRIVATE")] = replace(
                    ref, state="PRESENT", current_oid=intent.prepared_oid
                )
            else:
                copied._run_refs[(intent.run_id, "PRIVATE")] = replace(
                    ref,
                    state=(
                        "CONFLICT"
                        if outcome.result_class == "PRIVATE_REF_CONFLICT"
                        else (
                            "INIT_INTENT_RECORDED"
                            if outcome.result_class == "PRIVATE_REF_UNOBSERVABLE"
                            else "ABSENT_EXPECTED"
                        )
                    ),
                )
                copied._runs[intent.run_id] = replace(
                    run,
                    state=(
                        RunState.INDETERMINATE
                        if outcome.result_class == "PRIVATE_REF_UNOBSERVABLE"
                        else RunState.PAUSED
                    ),
                )

        return self._commit_state_and_event(
            run_id=intent.run_id,
            expected_sequence=expected_sequence,
            event=AuditEvent.kind(
                "PRIVATE_REF_INIT_SETTLED",
                applicable_revision_digests=intent.applicable_revision_digests,
                result_class=outcome.result_class,
            ),
            mutate=mutate,
        )

    def mark_private_ref_init_indeterminate(
        self,
        *,
        binding: RuntimeStartBinding,
        intent: RefCasIntent,
        failure_class: str,
        expected_sequence: AuditSequence,
    ) -> AuditSequence:
        del failure_class
        return self.settle_private_ref_init(
            binding=binding,
            intent=intent,
            outcome=PrivateRefCasOutcome(
                intent_id=intent.intent_id,
                run_id=intent.run_id,
                result_class="PRIVATE_REF_UNOBSERVABLE",
                observed_oid=None,
            ),
            expected_sequence=expected_sequence,
        )

    def fail_next_commit_after_state_write_for_test(self) -> None:
        with self._lock:
            self._fail_next_commit_after_state_write = True

    def load_runtime_state(self, run_id: RunId) -> RuntimeState:
        run = self.run_record(run_id)
        if (
            run.current_policy_digest is None
            or run.current_budget_digest is None
            or run.current_model_configuration_digest is None
        ):
            raise StateConflict("RUNTIME_REVISION_BINDING_INCOMPLETE")
        return RuntimeState(
            run_id,
            run.state,
            self.audit_sequence(run_id),
            self._runtime_progress_generations.get(run_id, 0),
            run.current_plan_digest,
            run.current_policy_digest,
            run.current_budget_digest,
            run.current_model_configuration_digest,
        )

    def target_reservation_for_run(self, run_id: RunId) -> TargetReservation:
        matches = tuple(
            reservation
            for reservation in self._target_reservations.values()
            if reservation.run_id == run_id
        )
        if len(matches) != 1:
            raise StateConflict("TARGET_RESERVATION_NOT_FOUND")
        return matches[0]

    def runtime_owner(self, run_id: RunId) -> RuntimeOwnerId | None:
        owner = self._runtime_owners.get(run_id)
        return None if owner is None else owner[0]

    def runtime_delivery_event(self, run_id: RunId) -> str | None:
        values = [
            event.event_kind
            for _, event in self._audit_events.get(run_id, ())
            if event.event_kind in {"RUNTIME_DELIVERY_STOP_RECORDED", "RUNTIME_OWNER_RELEASED"}
        ]
        return None if not values else values[-1]

    def runtime_delivery_stop_count(self, run_id: RunId) -> int:
        return sum(key[0] == run_id for key in self._runtime_delivery_stops)

    def model_attempt_count(self, logical_turn_id: LogicalTurnId) -> int:
        return sum(
            attempt.logical_turn_id == logical_turn_id for attempt in self._model_attempts.values()
        )

    def unconsumed_permit_count(self, run_id: RunId) -> int:
        return sum(
            permit.run_id == run_id and permit.state == "UNCONSUMED"
            for permit in self._runtime_permits.values()
        )

    @staticmethod
    def _require_consumed_runtime_owner_on_copy(
        copied: InMemoryStateStore,
        run_id: RunId,
        owner_id: RuntimeOwnerId,
        permit_generation: int,
    ) -> tuple[RuntimePermit, int]:
        owner = copied._runtime_owners.get(run_id)
        permit = copied._runtime_permits.get((run_id, permit_generation))
        if (
            owner is None
            or owner[0] != owner_id
            or permit is None
            or permit.state != "CONSUMED"
            or permit.consumed_owner_id != owner_id
        ):
            raise StateConflict("RUNTIME_OWNER_BINDING_MISMATCH")
        active = copied._active_run_times.get(run_id)
        if active is None or active.open_owner_generation != owner[1]:
            raise StateConflict("RUNTIME_OWNER_BINDING_MISMATCH")
        return permit, owner[1]

    @staticmethod
    def _require_consumed_draft_permit_on_copy(
        copied: InMemoryStateStore,
        run_id: RunId,
        owner_id: RuntimeOwnerId,
        permit_generation: int,
    ) -> RuntimePermit:
        permit, _ = InMemoryStateStore._require_consumed_runtime_owner_on_copy(
            copied, run_id, owner_id, permit_generation
        )
        if permit.allowed_phase != "DRAFT" or copied._runs[run_id].state != RunState.DRAFT:
            raise StateConflict("TARGET_RESERVATION_PERMIT_BINDING_MISMATCH")
        return permit

    def record_or_load_target_reservation_creation_intent_under_draft_permit(
        self,
        run_id: RunId,
        owner_id: RuntimeOwnerId,
        permit_generation: int,
        *,
        expected_sequence: AuditSequence,
    ) -> TargetReservationCreationIntent:
        reservation = self.target_reservation_for_run(run_id)
        if reservation.phase == "CREATION_INTENT_RECORDED":
            return self.unsettled_target_reservation_creation(run_id)
        created: list[TargetReservationCreationIntent] = []

        def mutate(copied: InMemoryStateStore) -> None:
            permit = self._require_consumed_draft_permit_on_copy(
                copied, run_id, owner_id, permit_generation
            )
            current = copied.target_reservation_for_run(run_id)
            intent = _new_target_reservation_creation_intent(
                copied._runs[run_id], current, expected_sequence
            ).model_copy(
                update={
                    "applicable_revision_digests": permit.applicable_revision_digests,
                    "target_authority_digest": permit.target_authority_digest,
                }
            )
            effect = intent.to_effect_intent(AuditSequence(expected_sequence + 1))
            if effect.intent_id in copied._effect_intents:
                raise StateConflict("EFFECT_INTENT_DUPLICATE")
            copied._effect_intents[effect.intent_id] = effect
            copied._target_reservations[current.reservation_id] = replace(
                current, phase="CREATION_INTENT_RECORDED"
            )
            created.append(intent)

        self._commit_state_and_event(
            run_id=run_id,
            expected_sequence=expected_sequence,
            event=AuditEvent.kind("TARGET_RESERVATION_CREATION_INTENT_RECORDED"),
            mutate=mutate,
        )
        return created[0]

    def settle_target_reservation_creation_under_draft_permit(
        self,
        intent: TargetReservationCreationIntent,
        outcome: TargetReservationCreationOutcome,
        owner_id: RuntimeOwnerId,
        permit_generation: int,
        *,
        expected_sequence: AuditSequence,
    ) -> AuditSequence:
        _validate_reservation_outcome(intent, outcome)
        result = outcome.to_effect_result(AuditSequence(expected_sequence + 1))

        def mutate(copied: InMemoryStateStore) -> None:
            permit = self._require_consumed_draft_permit_on_copy(
                copied, intent.run_id, owner_id, permit_generation
            )
            if (
                intent.applicable_revision_digests != permit.applicable_revision_digests
                or intent.target_authority_digest != permit.target_authority_digest
            ):
                raise StateConflict("TARGET_RESERVATION_PERMIT_AUTHORITY_MISMATCH")
            reservation = copied._target_reservations[intent.reservation_id]
            effect = copied._effect_intents.get(intent.intent_id)
            if (
                reservation.phase != "CREATION_INTENT_RECORDED"
                or effect
                != intent.to_effect_intent(effect.recorded_sequence if effect else AuditSequence(0))
                or intent.intent_id in copied._effect_results
            ):
                raise StateConflict("TARGET_RESERVATION_INTENT_BINDING_MISMATCH")
            copied._effect_results[intent.intent_id] = result
            if outcome.result_class == "REGISTERED_LOCKED":
                if classify_reservation_creation(outcome.observed) != "SETTLE":
                    raise StateConflict("TARGET_RESERVATION_SUCCESS_NOT_EXACT")
                copied._target_reservations[intent.reservation_id] = replace(
                    reservation,
                    phase="REGISTERED_LOCKED",
                    admin_entry_name=outcome.observed.admin_entry_name,
                    admin_binding_digest=outcome.observed.admin_binding_digest,
                )
                state = RunState.PLANNING
            elif outcome.result_class == "CONFLICT":
                copied._target_reservations[intent.reservation_id] = replace(
                    reservation,
                    phase="ALLOCATED",
                    admin_entry_name=None,
                    admin_binding_digest=None,
                )
                state = RunState.DRAFT
            else:
                copied._close_new_dispatch(intent.run_id, DispatchCloseCause.RUNTIME_FAULT)
                state = RunState.INDETERMINATE
            copied._runs[intent.run_id] = replace(copied._runs[intent.run_id], state=state)

        return self._commit_state_and_event(
            run_id=intent.run_id,
            expected_sequence=expected_sequence,
            event=AuditEvent.kind("TARGET_RESERVATION_CREATION_SETTLED"),
            mutate=mutate,
        )

    def reuse_locked_target_reservation_under_draft_permit(
        self,
        run_id: RunId,
        observed: ReservationObservation,
        owner_id: RuntimeOwnerId,
        permit_generation: int,
        *,
        expected_sequence: AuditSequence,
    ) -> AuditSequence:
        if classify_reservation_creation(observed) != "SETTLE":
            raise StateConflict("TARGET_RESERVATION_REUSE_NOT_EXACT")

        def mutate(copied: InMemoryStateStore) -> None:
            self._require_consumed_draft_permit_on_copy(copied, run_id, owner_id, permit_generation)
            reservation = copied.target_reservation_for_run(run_id)
            if reservation.phase != "REGISTERED_LOCKED":
                raise StateConflict("TARGET_RESERVATION_REUSE_PHASE_INVALID")
            copied._runs[run_id] = replace(copied._runs[run_id], state=RunState.PLANNING)

        return self._commit_state_and_event(
            run_id=run_id,
            expected_sequence=expected_sequence,
            event=AuditEvent.kind("TARGET_RESERVATION_REUSED_AND_PLANNING_STARTED"),
            mutate=mutate,
        )

    def record_target_reservation_pre_intent_stop(
        self,
        run_id: RunId,
        observed: ReservationObservation,
        owner_id: RuntimeOwnerId,
        permit_generation: int,
        *,
        expected_sequence: AuditSequence,
    ) -> AuditSequence:
        event = (
            "TARGET_RESERVATION_OBSERVATION_INDETERMINATE"
            if not observed.observable
            else "TARGET_RESERVATION_INITIALIZATION_CONFLICT"
        )

        def mutate(copied: InMemoryStateStore) -> None:
            self._require_consumed_draft_permit_on_copy(copied, run_id, owner_id, permit_generation)
            if not observed.observable:
                copied._runs[run_id] = replace(copied._runs[run_id], state=RunState.INDETERMINATE)
                copied._close_new_dispatch(run_id, DispatchCloseCause.RUNTIME_FAULT)

        return self._commit_state_and_event(
            run_id=run_id,
            expected_sequence=expected_sequence,
            event=AuditEvent.kind(event),
            mutate=mutate,
        )

    def begin_runtime_barrier(
        self, run_id: RunId, action_id: str, expected_sequence: AuditSequence
    ) -> str:
        def mutate(copied: InMemoryStateStore) -> None:
            copied._require_dispatch_binding(run_id)
            if not copied._new_dispatch_open.get(run_id, True):
                raise StateConflict("RUN_DISPATCH_CLOSED")
            current = copied._runtime_barriers.get(run_id)
            if current is not None and current[1] == "IN_FLIGHT":
                raise StateConflict("RUNTIME_BARRIER_IN_FLIGHT")
            copied._runtime_barriers[run_id] = (action_id, "IN_FLIGHT", None)

        self._commit_state_and_event(
            run_id=run_id,
            expected_sequence=expected_sequence,
            event=AuditEvent.kind("RUNTIME_BARRIER_STARTED", action_id=action_id),
            mutate=mutate,
        )
        return action_id

    def settle_runtime_barrier(
        self,
        run_id: RunId,
        action_id: str,
        model_calls: int,
        pending_stop_reason: str | None,
        expected_sequence: AuditSequence,
    ) -> AuditSequence:
        events = [AuditEvent.kind("RUNTIME_BARRIER_SETTLED", action_id=action_id)]

        def mutate(copied: InMemoryStateStore) -> None:
            current = copied._runtime_barriers.get(run_id)
            if current != (action_id, "IN_FLIGHT", None):
                raise StateConflict("RUNTIME_BARRIER_SETTLE_COMPARE_AND_SET_FAILED")
            counters = copied.model_counters(run_id)
            calls = counters.calls + model_calls
            copied._model_counters[run_id] = replace(counters, calls=calls)
            budget_digest = copied.current_revision_digests(run_id).budget_digest
            if budget_digest is None:
                raise StateConflict("CURRENT_BUDGET_NOT_FOUND")
            _, stopped = copied._settle_global_usage_for_producer(
                run_id, budget_digest, GlobalBudgetMetric.MODEL_CALLS, calls
            )
            derived = "BUDGET_STOP" if stopped else None
            if pending_stop_reason != derived:
                raise StateConflict("RUNTIME_BARRIER_STOP_CAUSE_MISMATCH")
            if stopped:
                events.append(AuditEvent.kind("BUDGET_STOP_REQUESTED"))
            copied._runtime_barriers[run_id] = (action_id, "SETTLED", derived)

        return self._commit_state_and_events(
            run_id=run_id,
            expected_sequence=expected_sequence,
            event_factory=lambda: tuple(events),
            mutate=mutate,
        )

    def apply_post_barrier_controls(
        self,
        run_id: RunId,
        owner_id: RuntimeOwnerId,
        permit_generation: int,
        expected_sequence: AuditSequence,
    ) -> RuntimeDecision | None:
        self._require_consumed_runtime_owner_on_copy(self, run_id, owner_id, permit_generation)
        interrupt = self._runtime_interrupts.get(run_id)
        if interrupt is not None and interrupt[3] == "PENDING":
            return RuntimeDecision.pause(interrupt[1], expected_sequence)
        barrier = self._runtime_barriers.get(run_id)
        if barrier is not None and barrier[2] == "BUDGET_STOP":
            return RuntimeDecision.pause("GLOBAL_MODEL_CALL_CEILING", expected_sequence)
        return None

    def record_runtime_delivery_stop(
        self,
        run_id: RunId,
        owner_id: RuntimeOwnerId,
        permit_generation: int,
        candidate: RunStop,
        expected_sequence: AuditSequence,
    ) -> RunStop:
        result: list[RunStop] = []
        closed_at: list[MonotonicInstant] = []

        def mutate(copied: InMemoryStateStore) -> None:
            self._require_consumed_runtime_owner_on_copy(
                copied, run_id, owner_id, permit_generation
            )
            barrier = copied._runtime_barriers.get(run_id)
            if barrier is not None and barrier[1] == "IN_FLIGHT":
                raise StateConflict("RUNTIME_BARRIER_IN_FLIGHT")
            if copied._monotonic_clock is None:
                raise StateConflict("MONOTONIC_CLOCK_NOT_CONFIGURED")
            closed = copied._monotonic_clock.now()
            active = copied._active_run_times[run_id]
            cumulative = active.observed_nanoseconds(closed)
            closed_at.append(closed)
            final = RunStop(
                run_id=run_id,
                state=copied._runs[run_id].state,
                reason=candidate.reason,
                last_sequence=AuditSequence(expected_sequence + 2),
                pending=candidate.pending,
            )
            copied._active_run_times[run_id] = replace(active, cumulative_nanoseconds=cumulative)
            copied._runtime_delivery_stops[(run_id, permit_generation)] = final
            result.append(final)

        def finalize(copied: InMemoryStateStore) -> None:
            copied._runtime_owners.pop(run_id, None)
            active = copied._active_run_times[run_id]
            copied._active_run_times[run_id] = ActiveRunTimeState(
                run_id, active.cumulative_nanoseconds, None, None, None
            )

        self._commit_state_and_events(
            run_id=run_id,
            expected_sequence=expected_sequence,
            event_factory=lambda: (
                AuditEvent.kind("RUNTIME_DELIVERY_STOP_RECORDED"),
                AuditEvent.kind("RUNTIME_OWNER_RELEASED"),
            ),
            mutate=mutate,
            runtime_now_factory=lambda: closed_at[0],
            finalize=finalize,
        )
        return result[0]

    def record_runtime_fault_and_classify_barrier(
        self,
        run_id: RunId,
        owner_id: RuntimeOwnerId,
        permit_generation: int,
        fault: RuntimeFault,
        expected_sequence: AuditSequence,
    ) -> RuntimeFaultDisposition:
        from apexcrew.application.runtime import RuntimeFaultDisposition

        result: list[RuntimeFaultDisposition] = []

        def mutate(copied: InMemoryStateStore) -> None:
            self._require_consumed_runtime_owner_on_copy(
                copied, run_id, owner_id, permit_generation
            )
            barrier = copied._runtime_barriers.get(run_id)
            stored_state = "IDLE" if barrier is None else barrier[1]
            if stored_state not in {"IDLE", "IN_FLIGHT", "SETTLED", "INDETERMINATE"}:
                raise StateConflict("RUNTIME_BARRIER_STATE_INVALID")
            state = cast(
                Literal["IDLE", "IN_FLIGHT", "SETTLED", "INDETERMINATE"],
                stored_state,
            )
            if state == "IN_FLIGHT":
                assert barrier is not None
                copied._runtime_barriers[run_id] = (
                    barrier[0],
                    "INDETERMINATE",
                    barrier[2],
                )
                copied._runs[run_id] = replace(copied._runs[run_id], state=RunState.INDETERMINATE)
                state = "INDETERMINATE"
                reason: Literal["RUNTIME_FAULT", "RUNTIME_CLOCK_REGRESSION", "INDETERMINATE"] = (
                    "INDETERMINATE"
                )
            else:
                state = "SETTLED" if state == "SETTLED" else "IDLE"
                reason = (
                    "RUNTIME_CLOCK_REGRESSION"
                    if fault.fault_code == "MONOTONIC_CLOCK_REGRESSED"
                    else "RUNTIME_FAULT"
                )
            copied._close_new_dispatch(run_id, DispatchCloseCause.RUNTIME_FAULT)
            copied._runtime_faults[run_id] = fault
            copied._runtime_recorded_stop_reasons[run_id] = reason
            result.append(
                RuntimeFaultDisposition(AuditSequence(expected_sequence + 1), reason, state)
            )

        self._commit_state_and_event(
            run_id=run_id,
            expected_sequence=expected_sequence,
            event=AuditEvent.kind("RUNTIME_FAULT_RECORDED"),
            mutate=mutate,
        )
        return result[0]

    def latest_runtime_fault(self, run_id: RunId) -> object:
        try:
            return self._runtime_faults[run_id]
        except KeyError as error:
            raise StateConflict("RUNTIME_FAULT_NOT_FOUND") from error

    def recorded_stop_reason(self, run_id: RunId) -> str | None:
        if run_id in self._runtime_recorded_stop_reasons:
            return self._runtime_recorded_stop_reasons[run_id]
        barrier = self._runtime_barriers.get(run_id)
        return None if barrier is None else barrier[2]

    def next_recoverable_model_turn(self, run_id: RunId) -> CommittedModelTurn | None:
        candidates = sorted(
            (
                turn
                for turn in self._model_turns.values()
                if isinstance(turn, CommittedModelTurn)
                and turn.run_id == run_id
                and turn.state == "COMPLETION_COMMITTED"
                and turn.downstream_intent_id is None
            ),
            key=lambda turn: (turn.committed_sequence, turn.logical_turn_id),
        )
        return None if not candidates else candidates[0]

    def next_recovered_model_action(self, run_id: RunId) -> RecoveredModelAction | None:
        candidates = sorted(
            (
                intent
                for intent in self._effect_intents.values()
                if intent.run_id == run_id
                and intent.kind == "RECOVERED_MODEL_ACTION"
                and intent.intent_id not in self._effect_results
            ),
            key=lambda intent: intent.recorded_sequence,
        )
        if not candidates:
            return None
        intent = candidates[0]
        turn = self._model_turns.get(intent.action_id or "")
        if not isinstance(turn, CommittedModelTurn):
            raise StateConflict("RECOVERED_MODEL_ACTION_BINDING_MISMATCH")
        try:
            return RecoveredModelAction.from_journal(turn, intent)
        except ValueError as error:
            raise StateConflict("RECOVERED_MODEL_ACTION_BINDING_MISMATCH") from error

    def apply_runtime_continue(self, command: CommandEnvelope) -> CommandOutcome:
        from apexcrew.domain.commands import ContinuePayload

        if not isinstance(command.payload, ContinuePayload):
            raise StateConflict("RUNTIME_CONTINUE_COMMAND_REQUIRED")
        run_id = command.payload.run_id
        expected_sequence = command.expected_sequence
        if expected_sequence is None:
            raise StateConflict("EXPECTED_SEQUENCE_REQUIRED")
        current = self.current_revision_digests(run_id)
        target_digest = self.target_authority_digest(run_id)

        def mutate(copied: InMemoryStateStore) -> None:
            if run_id in copied._runtime_owners:
                active = copied._active_run_times.get(run_id)
                if (
                    active is None
                    or active.opened_at is None
                    or active.latest_committed_at is None
                    or active.latest_committed_at < active.opened_at
                ):
                    raise StateConflict("ACTIVE_RUN_TIME_RECOVERY_INVALID")
                cumulative = active.observed_nanoseconds(active.latest_committed_at)
                copied._runtime_owners.pop(run_id)
                copied._active_run_times[run_id] = ActiveRunTimeState(
                    run_id, cumulative, None, None, None
                )
            state = copied._runs[run_id].state
            allowed: RuntimeAllowedPhase = (
                "TERMINAL_ADMINISTRATION"
                if state in {RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED}
                else state.value  # type: ignore[assignment]
            )
            self._issue_runtime_permit_on_copy(
                copied,
                command,
                allowed,
                current,
                target_digest,
                AuditSequence(expected_sequence + 1),
            )

        return self._record_control_outcome(
            command,
            run_id,
            CommandStatus.ACCEPTED,
            None,
            "RUNTIME_OWNER_ORPHANED_AND_PERMIT_ISSUED",
            mutate,
        )
