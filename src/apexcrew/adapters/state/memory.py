from __future__ import annotations

import json
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from threading import RLock
from typing import Literal

from apexcrew.domain.admission import (
    TargetReservationCreationIntent,
    TargetReservationCreationOutcome,
)
from apexcrew.domain.authority import (
    ActiveRunTimeBoundaryDecision,
    ActiveRunTimeState,
    AtomicAction,
    AttemptLifecycleState,
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
    RuntimeAuditStamp,
    TaskAuthority,
    TaskBudgetState,
    TaskLifecycleState,
    TaskStopDecision,
    TrancheDecision,
    TrancheReason,
    WorkspaceLease,
    crossed_threshold,
    global_ceiling_for,
    model_reservation_amounts,
    normalize_global_budget_metric,
    progress_from_checks,
)
from apexcrew.domain.commands import (
    ApplicableRevisionDigests,
    CommandEnvelope,
    CommandOutcome,
)
from apexcrew.domain.effects import (
    AuditEvent,
    EffectIntent,
    EffectResult,
    RunRecord,
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
    SettledModelAttempt,
)
from apexcrew.domain.plan import may_overlap
from apexcrew.domain.revisions import BudgetRevisionDocument, Sha256DigestText, revision_digest
from apexcrew.domain.types import (
    AttemptId,
    AuditSequence,
    CommandStatus,
    IntentId,
    RepositoryId,
    RevisionDigest,
    RunId,
    RunState,
    TaskId,
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
                "target_ref": reservation.target_ref,
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


class InMemoryStateStore:
    def __init__(self, monotonic_clock: MonotonicClock | None = None) -> None:
        self._command_receipts: dict[str, tuple[str, RunId, str, str, AuditSequence]] = {}
        self._audit_events: dict[RunId, list[tuple[AuditSequence, AuditEvent]]] = {}
        self._sequences: dict[RunId, AuditSequence] = {}
        self._effect_intents: dict[IntentId, EffectIntent] = {}
        self._effect_results: dict[IntentId, EffectResult] = {}
        self._model_turns: dict[LogicalTurnId, LogicalModelTurn | CommittedModelTurn] = {}
        self._model_attempt_numbers: dict[tuple[RunId, LogicalTurnId, int], IntentId] = {}
        self._model_attempts: dict[IntentId, ModelRequestIntent | SettledModelAttempt] = {}
        self._model_counters: dict[RunId, ModelCounters] = {}
        self._runs: dict[RunId, RunRecord] = {}
        self._target_reservations: dict[str, TargetReservation] = {}
        self._approved_budgets: dict[RunId, tuple[RevisionDigest, BudgetRevisionDocument]] = {}
        self._global_usage: dict[tuple[RunId, GlobalBudgetMetric], int | Decimal] = {}
        self._budget_warnings: dict[
            tuple[RunId, RevisionDigest, GlobalBudgetMetric, int], BudgetWarning
        ] = {}
        self._atomic_actions: dict[tuple[RunId, str], AtomicAction] = {}
        self._workspace_leases: dict[tuple[RunId, str], WorkspaceLease] = {}
        self._authorization_denials: dict[tuple[RunId, str], tuple[str, AuthorizationReason]] = {}
        self._task_budget_counters: dict[tuple[RunId, TaskId], TaskBudgetState] = {}
        self._planning_request_counts: dict[RunId, int] = {}
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
        copied._model_turns = self._model_turns.copy()
        copied._model_attempt_numbers = self._model_attempt_numbers.copy()
        copied._model_attempts = self._model_attempts.copy()
        copied._model_counters = self._model_counters.copy()
        copied._runs = self._runs.copy()
        copied._target_reservations = self._target_reservations.copy()
        copied._approved_budgets = self._approved_budgets.copy()
        copied._global_usage = self._global_usage.copy()
        copied._budget_warnings = self._budget_warnings.copy()
        copied._atomic_actions = self._atomic_actions.copy()
        copied._workspace_leases = self._workspace_leases.copy()
        copied._authorization_denials = self._authorization_denials.copy()
        copied._task_budget_counters = self._task_budget_counters.copy()
        copied._planning_request_counts = self._planning_request_counts.copy()
        copied._dispatch_close_causes = self._dispatch_close_causes.copy()
        copied._new_dispatch_open = self._new_dispatch_open.copy()
        copied._active_run_times = self._active_run_times.copy()
        copied._task_tranches = self._task_tranches.copy()
        copied._tasks = self._tasks.copy()
        copied._attempts = self._attempts.copy()
        copied._task_checkpoints = deepcopy(self._task_checkpoints)
        copied._task_invalid_actions = deepcopy(self._task_invalid_actions)
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
        self._model_turns = copied._model_turns
        self._model_attempt_numbers = copied._model_attempt_numbers
        self._model_attempts = copied._model_attempts
        self._model_counters = copied._model_counters
        self._runs = copied._runs
        self._target_reservations = copied._target_reservations
        self._approved_budgets = copied._approved_budgets
        self._global_usage = copied._global_usage
        self._budget_warnings = copied._budget_warnings
        self._atomic_actions = copied._atomic_actions
        self._workspace_leases = copied._workspace_leases
        self._authorization_denials = copied._authorization_denials
        self._task_budget_counters = copied._task_budget_counters
        self._planning_request_counts = copied._planning_request_counts
        self._dispatch_close_causes = copied._dispatch_close_causes
        self._new_dispatch_open = copied._new_dispatch_open
        self._active_run_times = copied._active_run_times
        self._task_tranches = copied._task_tranches
        self._tasks = copied._tasks
        self._attempts = copied._attempts
        self._task_checkpoints = copied._task_checkpoints
        self._task_invalid_actions = copied._task_invalid_actions

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
            else:
                if copied._monotonic_clock is None:
                    raise StateConflict("MONOTONIC_CLOCK_NOT_CONFIGURED")
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
        reason: Literal["REPEATED_CHECKPOINT", "REPEATED_INVALID_ACTION"],
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
    ) -> TaskStopDecision:
        if attempt_id != task.attempt_id:
            raise StateConflict("TASK_ATTEMPT_BINDING_MISMATCH")
        count = 0

        def mutate(copied: InMemoryStateStore) -> None:
            nonlocal count
            copied._require_current_task_budget(task, budget_digest)
            copied._finish_attempt(task.run_id, task.task_id, attempt_id, "FAILED")
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
            else:
                copied._set_task_state(task.run_id, task.task_id, "READY")

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

    def runtime_barrier_state(self, run_id: RunId) -> Literal["IN_FLIGHT", "SETTLED"]:
        with self._lock:
            in_flight = any(
                isinstance(attempt, ModelRequestIntent)
                and attempt.run_id == run_id
                and attempt.state == "RESERVED"
                for attempt in self._model_attempts.values()
            )
        return "IN_FLIGHT" if in_flight else "SETTLED"

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
                if request.owner_kind == "PLANNING":
                    copied._planning_request_counts[request.run_id] = (
                        evaluation.planning_requests + 1
                    )
                else:
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
                            and request.owner_kind != "PLANNING"
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
                        if intent.run_id == run_id and intent_id not in self._effect_results
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

    def fail_next_commit_after_state_write_for_test(self) -> None:
        with self._lock:
            self._fail_next_commit_after_state_write = True
