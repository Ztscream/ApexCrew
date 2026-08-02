from __future__ import annotations

import json
from collections.abc import Callable
from copy import deepcopy
from dataclasses import replace
from threading import RLock

from apexcrew.domain.admission import (
    TargetReservationCreationIntent,
    TargetReservationCreationOutcome,
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
from apexcrew.domain.model import (
    CommittedModelTurn,
    LogicalModelTurn,
    LogicalTurnId,
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
from apexcrew.domain.revisions import Sha256DigestText
from apexcrew.domain.types import (
    AuditSequence,
    CommandStatus,
    IntentId,
    RepositoryId,
    RunId,
    RunState,
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


class InMemoryStateStore:
    def __init__(self) -> None:
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

    def _commit_state_and_event(
        self,
        *,
        run_id: RunId,
        expected_sequence: AuditSequence,
        event: AuditEvent,
        mutate: Callable[[InMemoryStateStore], None],
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
            next_sequence = AuditSequence(expected_sequence + 1)
            copied._audit_events.setdefault(run_id, []).append((next_sequence, event))
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
            copied._model_counters[intent.run_id] = copied.model_counters(intent.run_id).settle(
                settled.reserved_amounts, settled.charged_amounts
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
