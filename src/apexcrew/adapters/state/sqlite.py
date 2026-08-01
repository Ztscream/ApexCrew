from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from threading import RLock

from apexcrew.domain.commands import (
    ApplicableRevisionDigests,
    CommandEnvelope,
    CommandOutcome,
)
from apexcrew.domain.effects import (
    AuditEvent,
    EffectIntent,
    EffectResult,
    StateCommitFault,
    StateConflict,
    canonical_json,
    sha256_digest,
)
from apexcrew.domain.model import (
    ModelBudgetAmounts,
    ModelCompletion,
    ModelDispatchResult,
    ModelRequest,
    ModelRequestIntent,
    model_request_from_json,
    model_request_to_json,
    settle_model_completion,
)
from apexcrew.domain.revisions import Sha256DigestText
from apexcrew.domain.types import (
    AttemptId,
    AuditSequence,
    CommandStatus,
    IntentId,
    RunId,
    TaskId,
)

_MIGRATIONS: tuple[tuple[int, tuple[str, ...]], ...] = (
    (
        1,
        (
            """CREATE TABLE command_receipts (
                request_id TEXT PRIMARY KEY,
                repository_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                envelope_digest TEXT NOT NULL,
                outcome_json TEXT NOT NULL,
                resulting_sequence INTEGER NOT NULL
            )""",
            """CREATE TABLE audit_events (
                run_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                event_kind TEXT NOT NULL,
                correlation_json TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at_utc TEXT NOT NULL,
                PRIMARY KEY(run_id, sequence)
            )""",
            "CREATE INDEX audit_events_run_sequence ON audit_events(run_id, sequence)",
            """CREATE TABLE run_sequences (
                run_id TEXT PRIMARY KEY,
                current_sequence INTEGER NOT NULL CHECK(current_sequence >= 0)
            )""",
            """CREATE TABLE effect_intents (
                intent_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                intent_digest TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                expected_prestate_json TEXT NOT NULL,
                idempotency_key TEXT NOT NULL UNIQUE,
                created_sequence INTEGER NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('UNSETTLED', 'SETTLED', 'INDETERMINATE'))
            )""",
            """CREATE TABLE effect_results (
                intent_id TEXT PRIMARY KEY REFERENCES effect_intents(intent_id),
                result_class TEXT NOT NULL,
                result_json TEXT NOT NULL,
                poststate_json TEXT,
                snapshot_digest TEXT,
                settled_sequence INTEGER NOT NULL
            )""",
        ),
    ),
    (
        2,
        (
            """CREATE TABLE model_turns (
                logical_turn_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                request_digest TEXT NOT NULL,
                created_sequence INTEGER NOT NULL,
                state TEXT NOT NULL,
                UNIQUE(run_id, logical_turn_id)
            )""",
            """CREATE TABLE model_attempts (
                intent_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                logical_turn_id TEXT NOT NULL,
                request_json TEXT,
                request_digest TEXT,
                idempotency_key TEXT,
                reserved_json TEXT NOT NULL,
                allowed_model_ids_json TEXT,
                state TEXT NOT NULL,
                returned_model_id TEXT,
                result_json TEXT,
                FOREIGN KEY(run_id, logical_turn_id)
                    REFERENCES model_turns(run_id, logical_turn_id)
            )""",
        ),
    ),
)


def _command_run_id(command: CommandEnvelope, outcome: CommandOutcome) -> RunId:
    payload_run_id = getattr(command.payload, "run_id", None)
    run_id = outcome.run_id if payload_run_id is None else RunId(payload_run_id)
    if run_id is None or outcome.run_id != run_id:
        raise StateConflict("COMMAND_OUTCOME_RUN_MISMATCH")
    return run_id


def _command_digest(command: CommandEnvelope) -> str:
    return sha256_digest(canonical_json(command.model_dump(mode="json")))


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


def effect_intent_to_storage_json(intent: EffectIntent) -> str:
    return canonical_json(
        {
            "action_id": intent.action_id,
            "applicable_revision_digests": (
                intent.applicable_revision_digests.model_dump(mode="json")
            ),
            "attempt_id": intent.attempt_id,
            "expected_prestate_json": intent.expected_prestate_json,
            "idempotency_key": intent.idempotency_key,
            "intent_id": intent.intent_id,
            "kind": intent.kind,
            "normalized_payload_json": intent.normalized_payload_json,
            "payload_digest": intent.payload_digest,
            "recorded_sequence": intent.recorded_sequence,
            "run_id": intent.run_id,
            "task_id": intent.task_id,
        }
    )


def effect_intent_from_storage_json(value: str) -> EffectIntent:
    data = _json_object(value, "EFFECT_INTENT_STORAGE_BINDING_MISMATCH")
    try:
        intent = EffectIntent(
            intent_id=IntentId(str(data["intent_id"])),
            run_id=RunId(str(data["run_id"])),
            kind=str(data["kind"]),
            idempotency_key=str(data["idempotency_key"]),
            applicable_revision_digests=ApplicableRevisionDigests.model_validate(
                data["applicable_revision_digests"]
            ),
            payload_digest=Sha256DigestText(str(data["payload_digest"])),
            normalized_payload_json=str(data["normalized_payload_json"]),
            recorded_sequence=AuditSequence(int(str(data["recorded_sequence"]))),
            expected_prestate_json=str(data["expected_prestate_json"]),
            task_id=None if data["task_id"] is None else TaskId(str(data["task_id"])),
            attempt_id=(None if data["attempt_id"] is None else AttemptId(str(data["attempt_id"]))),
            action_id=None if data["action_id"] is None else str(data["action_id"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise StateConflict("EFFECT_INTENT_STORAGE_BINDING_MISMATCH") from error
    if effect_intent_to_storage_json(intent) != value:
        raise StateConflict("EFFECT_INTENT_STORAGE_BINDING_MISMATCH")
    return intent


def effect_result_to_storage_json(result: EffectResult) -> str:
    return canonical_json(
        {
            "bounded_result_json": result.bounded_result_json,
            "intent_id": result.intent_id,
            "outcome": result.outcome,
            "result_class": result.result_class,
            "result_digest": result.result_digest,
            "run_id": result.run_id,
            "settled_sequence": result.settled_sequence,
            "snapshot_digest": result.snapshot_digest,
        }
    )


def effect_result_from_storage_json(value: str) -> EffectResult:
    data = _json_object(value, "EFFECT_RESULT_STORAGE_BINDING_MISMATCH")
    try:
        outcome = str(data["outcome"])
        if outcome not in {"COMPLETED", "FAILED", "STALE", "CONFLICT", "INDETERMINATE"}:
            raise ValueError("invalid effect result outcome")
        result = EffectResult(
            intent_id=IntentId(str(data["intent_id"])),
            run_id=RunId(str(data["run_id"])),
            outcome=outcome,  # type: ignore[arg-type]
            result_class=str(data["result_class"]),
            result_digest=Sha256DigestText(str(data["result_digest"])),
            bounded_result_json=str(data["bounded_result_json"]),
            settled_sequence=AuditSequence(int(str(data["settled_sequence"]))),
            snapshot_digest=(
                None
                if data["snapshot_digest"] is None
                else Sha256DigestText(str(data["snapshot_digest"]))
            ),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise StateConflict("EFFECT_RESULT_STORAGE_BINDING_MISMATCH") from error
    if effect_result_to_storage_json(result) != value:
        raise StateConflict("EFFECT_RESULT_STORAGE_BINDING_MISMATCH")
    return result


class SqliteStateStore:
    def __init__(self, database: Path) -> None:
        self._connection = sqlite3.connect(database, isolation_level=None, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._lock = RLock()
        self._fail_next_commit_after_state_write = False
        self._apply_migrations()

    def _apply_migrations(self) -> None:
        connection = self._connection
        connection.execute("BEGIN EXCLUSIVE")
        try:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY)"
            )
            applied = {
                row["version"]
                for row in connection.execute("SELECT version FROM schema_migrations")
            }
            for version, statements in _MIGRATIONS:
                if version in applied:
                    continue
                for statement in statements:
                    connection.execute(statement)
                connection.execute("INSERT INTO schema_migrations(version) VALUES (?)", (version,))
        except BaseException:
            connection.rollback()
            raise
        else:
            connection.commit()

    @contextmanager
    def _read_transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            connection = self._connection
            if connection.in_transaction:
                raise StateConflict("NESTED_STATE_TRANSACTION")
            connection.execute("BEGIN DEFERRED")
            try:
                yield connection
            except BaseException:
                connection.rollback()
                raise
            else:
                connection.commit()

    def _require_expected_sequence(
        self,
        connection: sqlite3.Connection,
        run_id: RunId,
        expected_sequence: AuditSequence,
    ) -> None:
        if not connection.in_transaction:
            raise StateConflict("RUN_WRITE_TRANSACTION_REQUIRED")
        row = connection.execute(
            "SELECT current_sequence FROM run_sequences WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            if expected_sequence != 0:
                raise StateConflict("STALE_SEQUENCE")
            connection.execute(
                "INSERT INTO run_sequences(run_id, current_sequence) VALUES (?, ?)",
                (run_id, expected_sequence),
            )
            return
        current = AuditSequence(0 if row is None else row["current_sequence"])
        if current != expected_sequence:
            raise StateConflict("STALE_SEQUENCE")

    def _append_audit_event(
        self,
        connection: sqlite3.Connection,
        run_id: RunId,
        event: AuditEvent,
        expected_sequence: AuditSequence,
    ) -> AuditSequence:
        next_sequence = AuditSequence(expected_sequence + 1)
        correlation_json = canonical_json(
            {
                "action_id": event.action_id,
                "attempt_id": event.attempt_id,
                "task_id": event.task_id,
            }
        )
        payload_json = canonical_json(
            {
                "applicable_revision_digests": (
                    None
                    if event.applicable_revision_digests is None
                    else event.applicable_revision_digests.model_dump(mode="json")
                ),
                "budget_delta_json": event.budget_delta_json,
                "result_class": event.result_class,
                "subject_digests": event.subject_digests,
                "timing_ms": event.timing_ms,
            }
        )
        connection.execute(
            "INSERT INTO audit_events(run_id, sequence, event_kind, correlation_json, "
            "payload_json, created_at_utc) VALUES (?, ?, ?, ?, ?, ?)",
            (
                run_id,
                next_sequence,
                event.event_kind,
                correlation_json,
                payload_json,
                datetime.now(UTC).isoformat(),
            ),
        )
        if (
            connection.execute(
                "UPDATE run_sequences SET current_sequence = ? "
                "WHERE run_id = ? AND current_sequence = ?",
                (next_sequence, run_id, expected_sequence),
            ).rowcount
            != 1
        ):
            raise StateConflict("AUDIT_SEQUENCE_COMPARE_AND_SET_FAILED")
        return next_sequence

    def _commit_state_and_event(
        self,
        *,
        run_id: RunId,
        expected_sequence: AuditSequence,
        event: AuditEvent,
        mutate: Callable[[sqlite3.Connection], None],
    ) -> AuditSequence:
        with self._lock:
            connection = self._connection
            if connection.in_transaction:
                raise StateConflict("NESTED_STATE_TRANSACTION")
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._require_expected_sequence(
                    connection,
                    run_id,
                    expected_sequence,
                )
                mutate(connection)
                if self._fail_next_commit_after_state_write:
                    self._fail_next_commit_after_state_write = False
                    raise StateCommitFault("TEST_FAULT_AFTER_STATE_WRITE")
                sequence = self._append_audit_event(connection, run_id, event, expected_sequence)
            except BaseException:
                connection.rollback()
                raise
            else:
                connection.commit()
                return sequence

    def record_command(self, command: CommandEnvelope, outcome: CommandOutcome) -> CommandOutcome:
        with self._lock:
            run_id = _command_run_id(command, outcome)
            envelope_digest = _command_digest(command)
            with self._read_transaction() as connection:
                existing = connection.execute(
                    "SELECT repository_id, run_id, envelope_digest, outcome_json, "
                    "resulting_sequence FROM command_receipts WHERE request_id = ?",
                    (command.request_id,),
                ).fetchone()
                sequence_row = connection.execute(
                    "SELECT current_sequence FROM run_sequences WHERE run_id = ?", (run_id,)
                ).fetchone()
            if existing is not None:
                if (
                    existing["repository_id"] == ""
                    and existing["run_id"] == run_id
                    and existing["envelope_digest"] == envelope_digest
                ):
                    return CommandOutcome.validate_for_payload(
                        command.payload, _json_object(existing["outcome_json"])
                    )
                return CommandOutcome.for_payload(
                    command.payload,
                    status=CommandStatus.CONFLICT,
                    run_id=run_id,
                    resulting_sequence=AuditSequence(
                        0 if sequence_row is None else sequence_row["current_sequence"]
                    ),
                    failed_invariant="IDEMPOTENCY_KEY_REUSE",
                )
            expected = AuditSequence(
                0 if command.expected_sequence is None else command.expected_sequence
            )
            committed_sequence = outcome.resulting_sequence
            if committed_sequence is None or committed_sequence != AuditSequence(expected + 1):
                raise StateConflict("COMMAND_OUTCOME_SEQUENCE_MISMATCH")
            outcome_json = canonical_json(outcome.model_dump(mode="json"))

            def mutate(connection: sqlite3.Connection) -> None:
                connection.execute(
                    "INSERT INTO command_receipts(request_id, repository_id, run_id, "
                    "envelope_digest, outcome_json, resulting_sequence) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        command.request_id,
                        "",
                        run_id,
                        envelope_digest,
                        outcome_json,
                        committed_sequence,
                    ),
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

    def audit_sequence(self, run_id: RunId) -> AuditSequence:
        with self._read_transaction() as connection:
            row = connection.execute(
                "SELECT current_sequence FROM run_sequences WHERE run_id = ?", (run_id,)
            ).fetchone()
        return AuditSequence(0 if row is None else row["current_sequence"])

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
            mutate=lambda connection: None,
        )

    def reserve_model_request(
        self, request: ModelRequest, expected_sequence: AuditSequence
    ) -> ModelRequestIntent:
        intent = ModelRequestIntent.reserve(request)
        reserved_json = json.dumps(
            {
                "calls": intent.reserved_amounts.calls,
                "cost_usd": str(intent.reserved_amounts.cost_usd),
                "input_tokens": intent.reserved_amounts.input_tokens,
                "output_tokens": intent.reserved_amounts.output_tokens,
            },
            sort_keys=True,
            separators=(",", ":"),
        )

        def mutate(connection: sqlite3.Connection) -> None:
            connection.execute(
                "INSERT INTO model_turns(logical_turn_id, run_id, request_digest, "
                "created_sequence, state) VALUES (?, ?, ?, ?, 'OPEN')",
                (
                    intent.logical_turn_id,
                    request.run_id,
                    request.request_digest,
                    expected_sequence + 1,
                ),
            )
            connection.execute(
                "INSERT INTO model_attempts(intent_id, run_id, logical_turn_id, request_json, "
                "request_digest, idempotency_key, reserved_json, allowed_model_ids_json, state) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'RESERVED')",
                (
                    intent.intent_id,
                    request.run_id,
                    intent.logical_turn_id,
                    model_request_to_json(request),
                    request.request_digest,
                    request.idempotency_key,
                    reserved_json,
                    json.dumps(sorted(request.allowed_model_ids), separators=(",", ":")),
                ),
            )

        self._commit_state_and_event(
            run_id=request.run_id,
            expected_sequence=expected_sequence,
            event=AuditEvent.kind("MODEL_REQUEST_RESERVED"),
            mutate=mutate,
        )
        return intent

    def _model_request_from_row(
        self,
        run_id: RunId,
        intent_id: IntentId,
        row: sqlite3.Row,
    ) -> ModelRequestIntent:
        try:
            request_json = str(row["request_json"])
            request = model_request_from_json(request_json)
            reserved = json.loads(str(row["reserved_json"]))
            allowed_model_ids_json = json.dumps(
                sorted(request.allowed_model_ids), separators=(",", ":")
            )
            if (
                model_request_to_json(request) != request_json
                or request.run_id != run_id
                or request.request_digest != row["request_digest"]
                or request.idempotency_key != row["idempotency_key"]
                or allowed_model_ids_json != row["allowed_model_ids_json"]
                or row["turn_run_id"] != run_id
                or row["turn_request_digest"] != request.request_digest
            ):
                raise ValueError("stored model request binding mismatch")
            return ModelRequestIntent(
                run_id=run_id,
                intent_id=intent_id,
                logical_turn_id=str(row["logical_turn_id"]),
                request=request,
                reserved_amounts=ModelBudgetAmounts(
                    calls=int(reserved["calls"]),
                    input_tokens=int(reserved["input_tokens"]),
                    output_tokens=int(reserved["output_tokens"]),
                    cost_usd=Decimal(str(reserved["cost_usd"])),
                ),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise StateConflict("MODEL_REQUEST_STORAGE_BINDING_MISMATCH") from error

    def model_request(self, run_id: RunId, intent_id: IntentId) -> ModelRequestIntent:
        with self._read_transaction() as connection:
            row = connection.execute(
                "SELECT model_attempts.*, model_turns.run_id AS turn_run_id, "
                "model_turns.request_digest AS turn_request_digest "
                "FROM model_attempts JOIN model_turns USING(run_id, logical_turn_id) "
                "WHERE model_attempts.run_id = ? AND model_attempts.intent_id = ?",
                (run_id, intent_id),
            ).fetchone()
        if row is None or row["request_json"] is None:
            raise KeyError(intent_id)
        return self._model_request_from_row(run_id, intent_id, row)

    def settle_model_request(
        self,
        intent: ModelRequestIntent,
        completion: ModelCompletion,
        allowed_model_ids: frozenset[str],
        expected_sequence: AuditSequence,
    ) -> ModelDispatchResult:
        result = settle_model_completion(intent, completion, allowed_model_ids)
        encoded = json.dumps(
            {
                "run_id": result.run_id,
                "charged_cost_usd": str(result.charged_amounts.cost_usd),
                "charged_input_tokens": result.charged_amounts.input_tokens,
                "charged_output_tokens": result.charged_amounts.output_tokens,
                "normalized_action": result.normalized_action,
                "normalized_payload_digest": result.normalized_payload_digest,
                "outcome": result.outcome,
                "returned_model_id": result.returned_model_id,
            },
            sort_keys=True,
            separators=(",", ":"),
        )

        def mutate(connection: sqlite3.Connection) -> None:
            row = connection.execute(
                "SELECT model_attempts.*, model_turns.run_id AS turn_run_id, "
                "model_turns.request_digest AS turn_request_digest "
                "FROM model_attempts JOIN model_turns USING(run_id, logical_turn_id) "
                "WHERE model_attempts.run_id = ? AND model_attempts.intent_id = ?",
                (intent.run_id, intent.intent_id),
            ).fetchone()
            if row is None:
                raise StateConflict("MODEL_INTENT_BINDING_MISMATCH")
            if row["state"] != "RESERVED":
                raise StateConflict("MODEL_INTENT_ALREADY_SETTLED")
            stored = self._model_request_from_row(intent.run_id, intent.intent_id, row)
            if stored != intent or allowed_model_ids != stored.request.allowed_model_ids:
                raise StateConflict("MODEL_INTENT_BINDING_MISMATCH")
            changed = connection.execute(
                "UPDATE model_attempts SET state = 'CLOSED', returned_model_id = ?, "
                "result_json = ? WHERE run_id = ? AND intent_id = ? AND state = 'RESERVED'",
                (result.returned_model_id, encoded, intent.run_id, intent.intent_id),
            ).rowcount
            if changed != 1:
                raise StateConflict("MODEL_INTENT_ALREADY_SETTLED")

        self._commit_state_and_event(
            run_id=intent.run_id,
            expected_sequence=expected_sequence,
            event=AuditEvent.kind("MODEL_REQUEST_SETTLED"),
            mutate=mutate,
        )
        return result

    def reserved_call_count(self, run_id: RunId) -> int:
        with self._read_transaction() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS reserved_count FROM model_attempts WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            raise StateConflict("MODEL_RESERVATION_COUNT_UNAVAILABLE")
        return int(row["reserved_count"])

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

    def _insert_effect_intent(self, connection: sqlite3.Connection, intent: EffectIntent) -> None:
        duplicate = connection.execute(
            "SELECT 1 FROM effect_intents WHERE intent_id = ? OR idempotency_key = ?",
            (intent.intent_id, intent.idempotency_key),
        ).fetchone()
        if duplicate is not None:
            raise StateConflict("EFFECT_INTENT_DUPLICATE")
        stored = effect_intent_to_storage_json(intent)
        connection.execute(
            "INSERT INTO effect_intents(intent_id, run_id, kind, intent_digest, "
            "payload_json, expected_prestate_json, idempotency_key, created_sequence, state) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'UNSETTLED')",
            (
                intent.intent_id,
                intent.run_id,
                intent.kind,
                sha256_digest(stored),
                stored,
                intent.expected_prestate_json,
                intent.idempotency_key,
                intent.recorded_sequence,
            ),
        )

    def record_intent(self, intent: EffectIntent, expected_sequence: AuditSequence) -> EffectIntent:
        self._validate_effect_intent(intent, expected_sequence)
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
            mutate=lambda connection: self._insert_effect_intent(connection, intent),
        )
        return intent

    def _effect_intent_from_row(self, row: sqlite3.Row) -> EffectIntent:
        payload_json = str(row["payload_json"])
        if sha256_digest(payload_json) != row["intent_digest"]:
            raise StateConflict("EFFECT_INTENT_STORAGE_BINDING_MISMATCH")
        intent = effect_intent_from_storage_json(payload_json)
        _require_canonical_json_object(
            intent.normalized_payload_json,
            "EFFECT_INTENT_STORAGE_BINDING_MISMATCH",
        )
        _require_canonical_json_object(
            intent.expected_prestate_json,
            "EFFECT_INTENT_STORAGE_BINDING_MISMATCH",
        )
        if sha256_digest(intent.normalized_payload_json) != intent.payload_digest:
            raise StateConflict("EFFECT_INTENT_STORAGE_BINDING_MISMATCH")
        if (
            intent.intent_id != row["intent_id"]
            or intent.run_id != row["run_id"]
            or intent.kind != row["kind"]
            or intent.idempotency_key != row["idempotency_key"]
            or intent.recorded_sequence != row["created_sequence"]
            or intent.expected_prestate_json != row["expected_prestate_json"]
        ):
            raise StateConflict("EFFECT_INTENT_STORAGE_BINDING_MISMATCH")
        return intent

    def _require_unsettled_effect_intent(
        self,
        connection: sqlite3.Connection,
        run_id: RunId,
        intent_id: IntentId,
    ) -> EffectIntent:
        row = connection.execute(
            "SELECT * FROM effect_intents "
            "WHERE run_id = ? AND intent_id = ? AND state = 'UNSETTLED'",
            (run_id, intent_id),
        ).fetchone()
        if row is None:
            raise StateConflict("UNSETTLED_EFFECT_INTENT_REQUIRED")
        return self._effect_intent_from_row(row)

    def _insert_effect_result(
        self,
        connection: sqlite3.Connection,
        run_id: RunId,
        intent_id: IntentId,
        result: EffectResult,
        applicable_revision_digests: ApplicableRevisionDigests,
    ) -> None:
        intent = self._require_unsettled_effect_intent(connection, run_id, intent_id)
        if result.run_id != run_id or result.intent_id != intent_id:
            raise StateConflict("EFFECT_RESULT_RUN_OR_INTENT_MISMATCH")
        if intent.applicable_revision_digests != applicable_revision_digests:
            raise StateConflict("EFFECT_RESULT_REVISION_BINDING_MISMATCH")
        result_json = effect_result_to_storage_json(result)
        try:
            connection.execute(
                "INSERT INTO effect_results(intent_id, result_class, result_json, "
                "poststate_json, snapshot_digest, settled_sequence) "
                "VALUES (?, ?, ?, NULL, ?, ?)",
                (
                    result.intent_id,
                    result.result_class,
                    result_json,
                    result.snapshot_digest,
                    result.settled_sequence,
                ),
            )
        except sqlite3.IntegrityError as error:
            raise StateConflict("EFFECT_RESULT_DUPLICATE") from error
        state = "INDETERMINATE" if result.outcome == "INDETERMINATE" else "SETTLED"
        if (
            connection.execute(
                "UPDATE effect_intents SET state = ? "
                "WHERE intent_id = ? AND run_id = ? AND state = 'UNSETTLED'",
                (state, intent_id, run_id),
            ).rowcount
            != 1
        ):
            raise StateConflict("EFFECT_INTENT_SETTLE_COMPARE_AND_SET_FAILED")

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
        return self._commit_state_and_event(
            run_id=run_id,
            expected_sequence=expected_sequence,
            event=AuditEvent.kind(
                "EFFECT_INTENT_SETTLED",
                applicable_revision_digests=applicable_revision_digests,
                result_class=result.result_class,
                subject_digests=(result.result_digest,),
            ),
            mutate=lambda connection: self._insert_effect_result(
                connection,
                run_id,
                intent_id,
                result,
                applicable_revision_digests,
            ),
        )

    def effect_intent_or_none(self, intent_id: IntentId) -> EffectIntent | None:
        with self._read_transaction() as connection:
            row = connection.execute(
                "SELECT * FROM effect_intents WHERE intent_id = ?", (intent_id,)
            ).fetchone()
        return None if row is None else self._effect_intent_from_row(row)

    def effect_intent(self, intent_id: IntentId) -> EffectIntent:
        intent = self.effect_intent_or_none(intent_id)
        if intent is None:
            raise KeyError(intent_id)
        return intent

    def effect_result(self, intent_id: IntentId) -> EffectResult:
        with self._read_transaction() as connection:
            row = connection.execute(
                "SELECT effect_results.*, effect_intents.state AS intent_state "
                "FROM effect_results JOIN effect_intents USING(intent_id) "
                "WHERE intent_id = ?",
                (intent_id,),
            ).fetchone()
        if row is None:
            raise KeyError(intent_id)
        result_json = str(row["result_json"])
        result = effect_result_from_storage_json(result_json)
        intent = self.effect_intent(intent_id)
        if (
            result.intent_id != intent_id
            or result.run_id != intent.run_id
            or result.result_class != row["result_class"]
            or result.snapshot_digest != row["snapshot_digest"]
            or result.settled_sequence != row["settled_sequence"]
            or row["poststate_json"] is not None
            or row["intent_state"]
            != ("INDETERMINATE" if result.outcome == "INDETERMINATE" else "SETTLED")
        ):
            raise StateConflict("EFFECT_RESULT_STORAGE_BINDING_MISMATCH")
        if sha256_digest(result.bounded_result_json) != result.result_digest:
            raise StateConflict("EFFECT_RESULT_STORAGE_BINDING_MISMATCH")
        return result

    def unsettled_intents(self, run_id: RunId) -> tuple[EffectIntent, ...]:
        with self._read_transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM effect_intents WHERE run_id = ? AND state = 'UNSETTLED' "
                "ORDER BY created_sequence, intent_id",
                (run_id,),
            ).fetchall()
        return tuple(self._effect_intent_from_row(row) for row in rows)

    def fail_next_commit_after_state_write_for_test(self) -> None:
        with self._lock:
            self._fail_next_commit_after_state_write = True

    def close(self) -> None:
        self._connection.close()
