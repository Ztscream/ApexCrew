from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
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
    budget_warning_from_json,
    budget_warning_to_json,
    crossed_threshold,
    dispatch_close_causes_from_json,
    dispatch_close_causes_to_json,
    global_ceiling_for,
    global_numeric_from_text,
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
    model_dispatch_result_from_json,
    model_dispatch_result_to_json,
    model_recovery_binding_from_json,
    model_recovery_binding_to_json,
    model_request_from_json,
    model_request_to_json,
)
from apexcrew.domain.plan import GlobPattern, may_overlap
from apexcrew.domain.revisions import (
    BudgetRevisionDocument,
    Sha256DigestText,
    revision_digest,
)
from apexcrew.domain.types import (
    AttemptId,
    AuditSequence,
    CommandStatus,
    GitOid,
    IntentId,
    RepositoryId,
    RevisionDigest,
    RunId,
    RunState,
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
    (
        3,
        (
            "ALTER TABLE model_attempts ADD COLUMN provider_attempt_number INTEGER NOT NULL DEFAULT 1",
            "ALTER TABLE model_attempts ADD COLUMN outcome TEXT",
            "ALTER TABLE model_attempts ADD COLUMN provider_response_id TEXT",
            "ALTER TABLE model_attempts ADD COLUMN reason_code TEXT",
            "ALTER TABLE model_attempts ADD COLUMN reported_usage_json TEXT",
            "ALTER TABLE model_attempts ADD COLUMN backoff_seconds INTEGER",
            "ALTER TABLE model_attempts ADD COLUMN result_digest TEXT",
            "ALTER TABLE model_attempts ADD COLUMN charged_json TEXT",
            "ALTER TABLE model_attempts ADD COLUMN reserved_sequence INTEGER",
            "ALTER TABLE model_attempts ADD COLUMN settled_sequence INTEGER",
            "ALTER TABLE model_attempts ADD COLUMN backoff_sequence INTEGER",
            """CREATE UNIQUE INDEX model_attempt_number_once
                ON model_attempts(run_id, logical_turn_id, provider_attempt_number)""",
            """CREATE TABLE model_counters (
                run_id TEXT PRIMARY KEY,
                calls INTEGER NOT NULL,
                input_tokens INTEGER NOT NULL,
                output_tokens INTEGER NOT NULL,
                cost_usd TEXT NOT NULL
            )""",
        ),
    ),
    (
        4,
        (
            "ALTER TABLE model_turns ADD COLUMN recovery_binding_json TEXT",
            "ALTER TABLE model_turns ADD COLUMN owner_kind TEXT",
            "ALTER TABLE model_turns ADD COLUMN task_id TEXT",
            "ALTER TABLE model_turns ADD COLUMN attempt_id TEXT",
            "ALTER TABLE model_turns ADD COLUMN tranche_id TEXT",
            "ALTER TABLE model_turns ADD COLUMN returned_model_id TEXT",
            "ALTER TABLE model_turns ADD COLUMN normalized_output_digest TEXT",
            "ALTER TABLE model_turns ADD COLUMN normalized_payload_json TEXT",
            "ALTER TABLE model_turns ADD COLUMN dispatch_result_json TEXT",
            "ALTER TABLE model_turns ADD COLUMN committed_sequence INTEGER",
            "ALTER TABLE model_turns ADD COLUMN downstream_intent_id TEXT",
            "ALTER TABLE model_turns ADD COLUMN downstream_sequence INTEGER",
        ),
    ),
    (
        5,
        (
            """CREATE TABLE runs (
                run_id TEXT PRIMARY KEY,
                repository_id TEXT NOT NULL,
                repository_instance_digest TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN (
                    'DRAFT','PLANNING','AWAITING_PLAN_APPROVAL','READY_TO_START','ACTIVE',
                    'VERIFYING_RUN','READY_FOR_APPROVAL','APPLYING','PAUSED','INDETERMINATE',
                    'COMPLETED','FAILED','CANCELLED'
                )),
                target_ref TEXT NOT NULL,
                pinned_target_oid TEXT NOT NULL,
                run_head_oid TEXT,
                runtime_progress_generation INTEGER NOT NULL DEFAULT 0,
                runtime_owner_id TEXT,
                runtime_owner_generation INTEGER NOT NULL DEFAULT 0,
                current_plan_digest TEXT,
                current_policy_digest TEXT,
                current_budget_digest TEXT,
                current_model_configuration_digest TEXT,
                UNIQUE(repository_id, run_id)
            )""",
            """CREATE TABLE target_reservations (
                reservation_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL UNIQUE REFERENCES runs(run_id),
                target_ref TEXT NOT NULL,
                pinned_target_oid TEXT NOT NULL,
                path TEXT NOT NULL UNIQUE,
                phase TEXT NOT NULL CHECK(phase IN (
                    'ALLOCATED','CREATION_INTENT_RECORDED','REGISTERED_LOCKED','CLEANUP_SETTLED'
                )),
                creation_intent_id TEXT REFERENCES effect_intents(intent_id),
                admin_entry_name TEXT UNIQUE,
                admin_binding_digest TEXT
            )""",
        ),
    ),
    (
        6,
        (
            "ALTER TABLE model_attempts ADD COLUMN owner_kind TEXT NOT NULL DEFAULT 'PLANNING'",
            "ALTER TABLE model_attempts ADD COLUMN task_id TEXT",
            "ALTER TABLE model_attempts ADD COLUMN attempt_id TEXT",
            "ALTER TABLE model_attempts ADD COLUMN tranche_id TEXT",
            "ALTER TABLE model_attempts ADD COLUMN dispatch_deadline_at_utc TEXT",
            "ALTER TABLE model_attempts ADD COLUMN target_safety_digest TEXT",
            "ALTER TABLE model_attempts ADD COLUMN budget_digest TEXT",
            "ALTER TABLE model_attempts ADD COLUMN model_configuration_digest TEXT",
            "ALTER TABLE runs ADD COLUMN active_runtime_nanoseconds INTEGER NOT NULL DEFAULT 0 CHECK(active_runtime_nanoseconds >= 0)",
            "ALTER TABLE runs ADD COLUMN runtime_interval_opened_nanoseconds INTEGER",
            "ALTER TABLE runs ADD COLUMN runtime_interval_owner_generation INTEGER",
            "ALTER TABLE runs ADD COLUMN new_dispatch_open INTEGER NOT NULL DEFAULT 1 CHECK(new_dispatch_open IN (0, 1))",
            "ALTER TABLE runs ADD COLUMN dispatch_close_causes_json TEXT NOT NULL DEFAULT '[]'",
            "ALTER TABLE audit_events ADD COLUMN runtime_owner_generation INTEGER",
            "ALTER TABLE audit_events ADD COLUMN runtime_monotonic_nanoseconds INTEGER",
            """CREATE TABLE run_authority_counters (
                run_id TEXT PRIMARY KEY,
                planning_requests INTEGER NOT NULL CHECK(planning_requests >= 0)
            )""",
            """CREATE TABLE task_budget_counters (
                run_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                counters_json TEXT NOT NULL,
                counters_digest TEXT NOT NULL,
                PRIMARY KEY(run_id, task_id)
            )""",
            """CREATE TABLE task_tranches (
                run_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                tranche_id TEXT NOT NULL,
                attempt_id TEXT NOT NULL,
                tranche_number INTEGER NOT NULL,
                tranche_kind TEXT NOT NULL CHECK(tranche_kind IN ('BOOTSTRAP', 'RENEWAL')),
                allocated_calls INTEGER NOT NULL CHECK(allocated_calls BETWEEN 1 AND 8),
                consumed_calls INTEGER NOT NULL CHECK(consumed_calls BETWEEN 0 AND allocated_calls),
                progress_evidence_json TEXT NOT NULL,
                progress_digest TEXT NOT NULL,
                allocated_sequence INTEGER NOT NULL,
                PRIMARY KEY(run_id, task_id, tranche_id),
                UNIQUE(run_id, task_id, tranche_number)
            )""",
            """CREATE TABLE workspace_leases (
                lease_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                attempt_id TEXT NOT NULL,
                generation INTEGER NOT NULL,
                base_head TEXT NOT NULL,
                admissible_head TEXT NOT NULL,
                task_contract_digest TEXT NOT NULL,
                write_globs_json TEXT NOT NULL,
                sensitivity_globs_json TEXT NOT NULL,
                issued_at_utc TEXT NOT NULL,
                expires_at_utc TEXT NOT NULL,
                state TEXT NOT NULL,
                issued_sequence INTEGER NOT NULL,
                renewed_sequence INTEGER,
                terminal_sequence INTEGER,
                UNIQUE(run_id, attempt_id, generation)
            )""",
            """CREATE TABLE authorization_denials (
                run_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                attempt_id TEXT NOT NULL,
                action_id TEXT NOT NULL,
                action_digest TEXT NOT NULL,
                binding_digest TEXT NOT NULL,
                plan_digest TEXT NOT NULL,
                policy_digest TEXT NOT NULL,
                budget_digest TEXT NOT NULL,
                model_configuration_digest TEXT NOT NULL,
                occurred_at_utc TEXT NOT NULL,
                reason TEXT NOT NULL,
                denied_sequence INTEGER NOT NULL,
                PRIMARY KEY(run_id, action_id)
            )""",
            """CREATE TABLE approved_budgets_for_test (
                run_id TEXT PRIMARY KEY,
                budget_digest TEXT NOT NULL,
                budget_json TEXT NOT NULL
            )""",
        ),
    ),
    (
        7,
        (
            """CREATE TABLE tasks (
                run_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('ACTIVE', 'READY', 'PAUSED')),
                pause_reason TEXT,
                pause_counter INTEGER CHECK(pause_counter IS NULL OR pause_counter >= 1),
                PRIMARY KEY(run_id, task_id)
            )""",
            """CREATE TABLE attempts (
                run_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                attempt_id TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('RUNNING', 'FAILED')),
                PRIMARY KEY(run_id, attempt_id),
                FOREIGN KEY(run_id, task_id) REFERENCES tasks(run_id, task_id)
            )""",
            """CREATE TABLE task_checkpoints (
                run_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                tree_oid TEXT NOT NULL,
                check_set_digest TEXT NOT NULL,
                budget_digest TEXT NOT NULL,
                observed_sequence INTEGER NOT NULL,
                PRIMARY KEY(run_id, task_id, observed_sequence),
                FOREIGN KEY(run_id, task_id) REFERENCES tasks(run_id, task_id)
            )""",
            """CREATE INDEX task_checkpoint_matches
                ON task_checkpoints(run_id, task_id, tree_oid, check_set_digest)""",
            """CREATE TABLE task_invalid_actions (
                run_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                attempt_id TEXT NOT NULL,
                action_digest TEXT NOT NULL,
                budget_digest TEXT NOT NULL,
                observed_sequence INTEGER NOT NULL,
                PRIMARY KEY(run_id, task_id, observed_sequence),
                UNIQUE(run_id, attempt_id),
                FOREIGN KEY(run_id, task_id) REFERENCES tasks(run_id, task_id),
                FOREIGN KEY(run_id, attempt_id) REFERENCES attempts(run_id, attempt_id)
            )""",
            """CREATE INDEX task_invalid_action_matches
                ON task_invalid_actions(run_id, task_id, action_digest)""",
        ),
    ),
    (
        8,
        (
            """CREATE TABLE global_budget_usage (
                run_id TEXT NOT NULL REFERENCES runs(run_id),
                metric TEXT NOT NULL,
                absolute_used TEXT NOT NULL,
                PRIMARY KEY(run_id, metric)
            )""",
            """CREATE TABLE budget_warnings (
                run_id TEXT NOT NULL REFERENCES runs(run_id),
                budget_digest TEXT NOT NULL,
                metric TEXT NOT NULL,
                warning_percent INTEGER NOT NULL,
                warning_json TEXT NOT NULL,
                PRIMARY KEY(run_id, budget_digest, metric, warning_percent)
            )""",
            """CREATE TABLE atomic_actions (
                run_id TEXT NOT NULL REFERENCES runs(run_id),
                action_id TEXT NOT NULL,
                budget_digest TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('IN_FLIGHT','SETTLED')),
                opened_sequence INTEGER NOT NULL,
                PRIMARY KEY(run_id, action_id)
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


def _task_budget_json(state: TaskBudgetState) -> str:
    return canonical_json(
        {
            "active_tranche_id": state.active_tranche_id,
            "active_tranche_remaining_calls": state.active_tranche_remaining_calls,
            "allocated_calls": state.allocated_calls,
            "attempts": state.attempts,
            "bootstrap_tranches": state.bootstrap_tranches,
            "consecutive_no_progress_tranches": state.consecutive_no_progress_tranches,
            "consumed_calls": state.consumed_calls,
            "cost_usd": str(state.cost_usd),
            "input_tokens": state.input_tokens,
            "manual_resumes": state.manual_resumes,
            "output_tokens": state.output_tokens,
            "run_id": state.run_id,
            "stale_refreshes": state.stale_refreshes,
            "task_id": state.task_id,
            "tranche_count": state.tranche_count,
        }
    )


def _task_budget_from_json(value: str) -> TaskBudgetState:
    data = _json_object(value, "TASK_BUDGET_STORAGE_INVALID")
    try:
        state = TaskBudgetState(
            run_id=RunId(str(data["run_id"])),
            task_id=TaskId(str(data["task_id"])),
            allocated_calls=int(str(data["allocated_calls"])),
            consumed_calls=int(str(data["consumed_calls"])),
            input_tokens=int(str(data["input_tokens"])),
            output_tokens=int(str(data["output_tokens"])),
            cost_usd=Decimal(str(data["cost_usd"])),
            tranche_count=int(str(data["tranche_count"])),
            bootstrap_tranches=int(str(data["bootstrap_tranches"])),
            consecutive_no_progress_tranches=int(str(data["consecutive_no_progress_tranches"])),
            attempts=int(str(data["attempts"])),
            stale_refreshes=int(str(data["stale_refreshes"])),
            manual_resumes=int(str(data["manual_resumes"])),
            active_tranche_id=(
                None if data["active_tranche_id"] is None else str(data["active_tranche_id"])
            ),
            active_tranche_remaining_calls=int(str(data["active_tranche_remaining_calls"])),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise StateConflict("TASK_BUDGET_STORAGE_INVALID") from error
    if _task_budget_json(state) != value:
        raise StateConflict("TASK_BUDGET_STORAGE_INVALID")
    return state


def _workspace_lease_from_row(row: sqlite3.Row) -> WorkspaceLease:
    try:
        return WorkspaceLease(
            lease_id=str(row["lease_id"]),
            run_id=RunId(str(row["run_id"])),
            task_id=TaskId(str(row["task_id"])),
            attempt_id=AttemptId(str(row["attempt_id"])),
            generation=int(row["generation"]),
            base_head=str(row["base_head"]),
            admissible_head=str(row["admissible_head"]),
            task_contract_digest=str(row["task_contract_digest"]),
            write_globs=tuple(
                GlobPattern.parse(value) for value in json.loads(row["write_globs_json"])
            ),
            sensitivity_globs=tuple(
                GlobPattern.parse(value) for value in json.loads(row["sensitivity_globs_json"])
            ),
            issued_at=datetime.fromisoformat(str(row["issued_at_utc"])),
            expires_at=datetime.fromisoformat(str(row["expires_at_utc"])),
            state=str(row["state"]),  # type: ignore[arg-type]
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise StateConflict("WORKSPACE_LEASE_STORAGE_INVALID") from error


class _LeaseDenied(RuntimeError):
    def __init__(self, denial: LeaseDenial) -> None:
        super().__init__(denial.reason)
        self.denial = denial


@dataclass(frozen=True, slots=True)
class _ReservationEvaluation:
    reason: ModelReservationReason | None
    budget: BudgetRevisionDocument
    amounts: ModelBudgetAmounts
    run_counters: ModelCounters
    task_counters: TaskBudgetState | None
    planning_requests: int


class SqliteStateStore:
    def __init__(self, database: Path, monotonic_clock: MonotonicClock | None = None) -> None:
        self._connection = sqlite3.connect(database, isolation_level=None, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._lock = RLock()
        self._fail_next_commit_after_state_write = False
        self._monotonic_clock = monotonic_clock
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
    def _transaction(
        self, mode: Literal["DEFERRED", "IMMEDIATE", "EXCLUSIVE"]
    ) -> Iterator[sqlite3.Connection]:
        with self._lock:
            connection = self._connection
            if connection.in_transaction:
                raise StateConflict("NESTED_STATE_TRANSACTION")
            connection.execute(f"BEGIN {mode}")
            try:
                yield connection
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
        runtime_now: MonotonicInstant | None = None,
    ) -> AuditSequence:
        next_sequence = AuditSequence(expected_sequence + 1)
        runtime_owner_generation: int | None = None
        runtime_monotonic_nanoseconds: int | None = None
        run = connection.execute(
            "SELECT runtime_interval_owner_generation, "
            "runtime_interval_opened_nanoseconds FROM runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if run is None or run["runtime_interval_owner_generation"] is None:
            if (
                event.runtime_owner_generation is not None
                or event.runtime_monotonic_nanoseconds is not None
            ):
                raise StateConflict("RUNTIME_AUDIT_WITHOUT_OWNER")
        else:
            if self._monotonic_clock is None:
                raise StateConflict("MONOTONIC_CLOCK_NOT_CONFIGURED")
            runtime_owner_generation = int(run["runtime_interval_owner_generation"])
            opened = run["runtime_interval_opened_nanoseconds"]
            if opened is None:
                raise StateConflict("ACTIVE_RUN_TIME_OPEN_BINDING_INCOMPLETE")
            now = self._monotonic_clock.now() if runtime_now is None else runtime_now
            latest = connection.execute(
                "SELECT runtime_monotonic_nanoseconds FROM audit_events WHERE run_id = ? "
                "AND runtime_owner_generation = ? ORDER BY sequence DESC LIMIT 1",
                (run_id, runtime_owner_generation),
            ).fetchone()
            latest_nanoseconds = int(opened) if latest is None else int(latest[0])
            if now.nanoseconds < int(opened) or now.nanoseconds < latest_nanoseconds:
                raise StateConflict("MONOTONIC_CLOCK_REGRESSED")
            runtime_monotonic_nanoseconds = now.nanoseconds
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
            "payload_json, created_at_utc, runtime_owner_generation, "
            "runtime_monotonic_nanoseconds) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                next_sequence,
                event.event_kind,
                correlation_json,
                payload_json,
                datetime.now(UTC).isoformat(),
                runtime_owner_generation,
                runtime_monotonic_nanoseconds,
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
        return self._commit_state_and_events(
            run_id=run_id,
            expected_sequence=expected_sequence,
            event_factory=lambda: (event,),
            mutate=mutate,
        )

    def _commit_state_and_events(
        self,
        *,
        run_id: RunId,
        expected_sequence: AuditSequence,
        event_factory: Callable[[], tuple[AuditEvent, ...]],
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
                events = event_factory()
                if not events:
                    raise StateConflict("AUDIT_EVENT_BATCH_EMPTY")
                sequence = expected_sequence
                for event in events:
                    sequence = self._append_audit_event(
                        connection,
                        run_id,
                        event,
                        sequence,
                    )
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
                run = connection.execute(
                    "SELECT repository_id FROM runs WHERE run_id = ?", (run_id,)
                ).fetchone()
                if run is None:
                    raise StateConflict("RUN_NOT_FOUND")
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
                    existing["repository_id"] == run["repository_id"]
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
                        run["repository_id"],
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

    def create_draft_with_reservation(
        self,
        run_id: RunId,
        repository_id: RepositoryId,
        repository_instance_digest: Sha256DigestText,
        reservation: TargetReservation,
    ) -> AuditSequence:
        _validate_draft_reservation(run_id, repository_id, repository_instance_digest, reservation)

        def mutate(connection: sqlite3.Connection) -> None:
            try:
                connection.execute(
                    "INSERT INTO runs(run_id, repository_id, repository_instance_digest, "
                    "state, target_ref, pinned_target_oid) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        run_id,
                        repository_id,
                        repository_instance_digest,
                        RunState.DRAFT,
                        reservation.target_ref,
                        reservation.pinned_target_oid,
                    ),
                )
                connection.execute(
                    "INSERT INTO target_reservations(reservation_id, run_id, target_ref, "
                    "pinned_target_oid, path, phase) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        reservation.reservation_id,
                        run_id,
                        reservation.target_ref,
                        reservation.pinned_target_oid,
                        str(reservation.path),
                        reservation.phase,
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise StateConflict("RUN_OR_TARGET_RESERVATION_DUPLICATE") from error

        return self._commit_state_and_event(
            run_id=run_id,
            expected_sequence=AuditSequence(0),
            event=AuditEvent.kind("RUN_DRAFT_AND_TARGET_RESERVATION_ALLOCATED"),
            mutate=mutate,
        )

    @staticmethod
    def _target_reservation_from_row(row: sqlite3.Row) -> TargetReservation:
        return TargetReservation(
            reservation_id=row["reservation_id"],
            run_id=RunId(row["run_id"]),
            target_ref=row["target_ref"],
            pinned_target_oid=GitOid(row["pinned_target_oid"]),
            path=Path(row["path"]),
            phase=row["phase"],
            admin_entry_name=row["admin_entry_name"],
            admin_binding_digest=row["admin_binding_digest"],
        )

    @staticmethod
    def _run_record_from_row(row: sqlite3.Row) -> RunRecord:
        return RunRecord(
            run_id=RunId(row["run_id"]),
            repository_id=RepositoryId(row["repository_id"]),
            repository_instance_digest=Sha256DigestText(row["repository_instance_digest"]),
            state=RunState(row["state"]),
            target_ref=row["target_ref"],
            pinned_target_oid=GitOid(row["pinned_target_oid"]),
            current_plan_digest=(
                None
                if row["current_plan_digest"] is None
                else RevisionDigest(row["current_plan_digest"])
            ),
            current_policy_digest=(
                None
                if row["current_policy_digest"] is None
                else RevisionDigest(row["current_policy_digest"])
            ),
            current_budget_digest=(
                None
                if row["current_budget_digest"] is None
                else RevisionDigest(row["current_budget_digest"])
            ),
            current_model_configuration_digest=(
                None
                if row["current_model_configuration_digest"] is None
                else RevisionDigest(row["current_model_configuration_digest"])
            ),
        )

    def run_record(self, run_id: RunId) -> RunRecord:
        with self._read_transaction() as connection:
            row = connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise StateConflict("RUN_NOT_FOUND")
        return self._run_record_from_row(row)

    def target_reservation(self, reservation_id: str) -> TargetReservation:
        with self._read_transaction() as connection:
            row = connection.execute(
                "SELECT * FROM target_reservations WHERE reservation_id = ?",
                (reservation_id,),
            ).fetchone()
        if row is None:
            raise StateConflict("TARGET_RESERVATION_NOT_FOUND")
        return self._target_reservation_from_row(row)

    def _target_reservation_for_run_for_update(
        self, connection: sqlite3.Connection, run_id: RunId
    ) -> TargetReservation:
        if not connection.in_transaction:
            raise StateConflict("TARGET_RESERVATION_WRITE_TRANSACTION_REQUIRED")
        row = connection.execute(
            "SELECT * FROM target_reservations WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            raise StateConflict("TARGET_RESERVATION_NOT_FOUND")
        return self._target_reservation_from_row(row)

    def _unsettled_effect_for_reservation(
        self,
        connection: sqlite3.Connection,
        reservation: TargetReservation,
    ) -> EffectIntent:
        row = connection.execute(
            "SELECT creation_intent_id FROM target_reservations "
            "WHERE reservation_id = ? AND run_id = ? "
            "AND phase = 'CREATION_INTENT_RECORDED'",
            (reservation.reservation_id, reservation.run_id),
        ).fetchone()
        if row is None or row["creation_intent_id"] is None:
            raise StateConflict("TARGET_RESERVATION_UNSETTLED_INTENT_REQUIRED")
        return self._require_unsettled_effect_intent(
            connection, reservation.run_id, IntentId(row["creation_intent_id"])
        )

    def _require_matching_unsettled_reservation_intent(
        self,
        connection: sqlite3.Connection,
        reservation: TargetReservation,
        intent: TargetReservationCreationIntent,
    ) -> None:
        try:
            stored = TargetReservationCreationIntent.from_effect_intent(
                self._unsettled_effect_for_reservation(connection, reservation)
            )
        except ValueError as error:
            raise StateConflict("TARGET_RESERVATION_INTENT_BINDING_MISMATCH") from error
        if stored != intent or stored.reservation_id != reservation.reservation_id:
            raise StateConflict("TARGET_RESERVATION_INTENT_BINDING_MISMATCH")

    def _new_target_reservation_creation_intent(
        self,
        connection: sqlite3.Connection,
        reservation: TargetReservation,
        expected_sequence: AuditSequence,
    ) -> TargetReservationCreationIntent:
        row = connection.execute(
            "SELECT * FROM runs WHERE run_id = ?", (reservation.run_id,)
        ).fetchone()
        if row is None or reservation.phase != "ALLOCATED":
            raise StateConflict("TARGET_RESERVATION_CREATION_NOT_ALLOCATED")
        run = self._run_record_from_row(row)
        target_authority_digest = sha256_digest(
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
        return TargetReservationCreationIntent(
            intent_id=IntentId(
                f"target-reservation-intent:{run.run_id}:{reservation.reservation_id}:"
                f"{expected_sequence + 1}"
            ),
            run_id=reservation.run_id,
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
            target_authority_digest=target_authority_digest,
            idempotency_key=(
                f"target-reservation-create:{run.run_id}:{reservation.reservation_id}:"
                f"{expected_sequence + 1}"
            ),
            recorded_sequence=AuditSequence(expected_sequence + 1),
        )

    def _record_or_load_target_reservation_creation_intent(
        self, run_id: RunId, *, expected_sequence: AuditSequence
    ) -> TargetReservationCreationIntent:
        with self._read_transaction() as connection:
            row = connection.execute(
                "SELECT * FROM target_reservations WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise StateConflict("TARGET_RESERVATION_NOT_FOUND")
            reservation = self._target_reservation_from_row(row)
            if reservation.phase == "CREATION_INTENT_RECORDED":
                return TargetReservationCreationIntent.from_effect_intent(
                    self._unsettled_effect_for_reservation(connection, reservation)
                )
            if reservation.phase != "ALLOCATED":
                raise StateConflict("TARGET_RESERVATION_CREATION_NOT_ALLOCATED")

        created: list[TargetReservationCreationIntent] = []

        def mutate(connection: sqlite3.Connection) -> None:
            current = self._target_reservation_for_run_for_update(connection, run_id)
            if current.phase != "ALLOCATED":
                raise StateConflict("TARGET_RESERVATION_CREATION_NOT_ALLOCATED")
            intent = self._new_target_reservation_creation_intent(
                connection, current, expected_sequence
            )
            effect = intent.to_effect_intent(AuditSequence(expected_sequence + 1))
            self._validate_effect_intent(effect, expected_sequence)
            self._insert_effect_intent(connection, effect)
            if (
                connection.execute(
                    "UPDATE target_reservations SET phase = "
                    "'CREATION_INTENT_RECORDED', creation_intent_id = ? "
                    "WHERE reservation_id = ? AND phase = 'ALLOCATED'",
                    (intent.intent_id, current.reservation_id),
                ).rowcount
                != 1
            ):
                raise StateConflict("TARGET_RESERVATION_ALLOCATION_COMPARE_AND_SET_FAILED")
            created.append(intent)

        self._commit_state_and_event(
            run_id=run_id,
            expected_sequence=expected_sequence,
            event=AuditEvent.kind("TARGET_RESERVATION_CREATION_INTENT_RECORDED"),
            mutate=mutate,
        )
        return created[0]

    def unsettled_target_reservation_creation(
        self, run_id: RunId
    ) -> TargetReservationCreationIntent:
        with self._read_transaction() as connection:
            reservation = self._target_reservation_for_run_for_update(connection, run_id)
            try:
                return TargetReservationCreationIntent.from_effect_intent(
                    self._unsettled_effect_for_reservation(connection, reservation)
                )
            except ValueError as error:
                raise StateConflict("TARGET_RESERVATION_INTENT_BINDING_MISMATCH") from error

    def _settle_target_reservation_creation(
        self,
        intent: TargetReservationCreationIntent,
        outcome: TargetReservationCreationOutcome,
        *,
        expected_sequence: AuditSequence,
    ) -> AuditSequence:
        _validate_reservation_outcome(intent, outcome)
        result = outcome.to_effect_result(AuditSequence(expected_sequence + 1))

        def mutate(connection: sqlite3.Connection) -> None:
            reservation = self._target_reservation_for_run_for_update(connection, intent.run_id)
            self._require_matching_unsettled_reservation_intent(connection, reservation, intent)
            self._insert_effect_result(
                connection,
                intent.run_id,
                intent.intent_id,
                result,
                intent.applicable_revision_digests,
            )
            if outcome.result_class == "REGISTERED_LOCKED":
                next_phase, next_state = "REGISTERED_LOCKED", RunState.DRAFT
                admin_entry_name = outcome.observed.admin_entry_name
                admin_binding_digest = outcome.observed.admin_binding_digest
            elif outcome.result_class == "CONFLICT":
                next_phase, next_state = "ALLOCATED", RunState.DRAFT
                admin_entry_name = admin_binding_digest = None
            else:
                next_phase, next_state = (
                    "CREATION_INTENT_RECORDED",
                    RunState.INDETERMINATE,
                )
                admin_entry_name = admin_binding_digest = None
            connection.execute(
                "UPDATE target_reservations SET phase = ?, admin_entry_name = ?, "
                "admin_binding_digest = ? WHERE reservation_id = ?",
                (
                    next_phase,
                    admin_entry_name,
                    admin_binding_digest,
                    reservation.reservation_id,
                ),
            )
            connection.execute(
                "UPDATE runs SET state = ? WHERE run_id = ?",
                (next_state, intent.run_id),
            )

        return self._commit_state_and_event(
            run_id=intent.run_id,
            expected_sequence=expected_sequence,
            event=AuditEvent.kind("TARGET_RESERVATION_CREATION_SETTLED"),
            mutate=mutate,
        )

    def audit_sequence(self, run_id: RunId) -> AuditSequence:
        with self._read_transaction() as connection:
            row = connection.execute(
                "SELECT current_sequence FROM run_sequences WHERE run_id = ?", (run_id,)
            ).fetchone()
        return AuditSequence(0 if row is None else row["current_sequence"])

    def install_running_attempt_for_test(self, task: TaskAuthority) -> None:
        with self._transaction("IMMEDIATE") as connection:
            task_row = connection.execute(
                "SELECT state FROM tasks WHERE run_id = ? AND task_id = ?",
                (task.run_id, task.task_id),
            ).fetchone()
            if task_row is None:
                connection.execute(
                    "INSERT INTO tasks(run_id, task_id, state) VALUES (?, ?, 'ACTIVE')",
                    (task.run_id, task.task_id),
                )
            elif task_row["state"] == "PAUSED":
                raise StateConflict("TASK_NOT_STARTABLE")
            else:
                connection.execute(
                    "UPDATE tasks SET state = 'ACTIVE', pause_reason = NULL, "
                    "pause_counter = NULL WHERE run_id = ? AND task_id = ?",
                    (task.run_id, task.task_id),
                )
            attempt_row = connection.execute(
                "SELECT task_id, state FROM attempts WHERE run_id = ? AND attempt_id = ?",
                (task.run_id, task.attempt_id),
            ).fetchone()
            if attempt_row is None:
                connection.execute(
                    "INSERT INTO attempts(run_id, task_id, attempt_id, state) "
                    "VALUES (?, ?, ?, 'RUNNING')",
                    (task.run_id, task.task_id, task.attempt_id),
                )
            elif attempt_row["task_id"] != task.task_id or attempt_row["state"] != "RUNNING":
                raise StateConflict("ATTEMPT_NOT_STARTABLE")

    def task_lifecycle_state(self, run_id: RunId, task_id: TaskId) -> TaskLifecycleState:
        with self._read_transaction() as connection:
            row = connection.execute(
                "SELECT state FROM tasks WHERE run_id = ? AND task_id = ?",
                (run_id, task_id),
            ).fetchone()
        if row is None:
            raise StateConflict("TASK_NOT_FOUND")
        state: TaskLifecycleState = row["state"]
        return state

    def attempt_lifecycle_state(
        self, run_id: RunId, attempt_id: AttemptId
    ) -> AttemptLifecycleState:
        with self._read_transaction() as connection:
            row = connection.execute(
                "SELECT state FROM attempts WHERE run_id = ? AND attempt_id = ?",
                (run_id, attempt_id),
            ).fetchone()
        if row is None:
            raise StateConflict("ATTEMPT_NOT_FOUND")
        state: AttemptLifecycleState = row["state"]
        return state

    @staticmethod
    def _require_current_task_budget(
        connection: sqlite3.Connection,
        task: TaskAuthority,
        budget_digest: RevisionDigest,
    ) -> None:
        row = connection.execute(
            "SELECT budget_digest FROM approved_budgets_for_test WHERE run_id = ?",
            (task.run_id,),
        ).fetchone()
        if row is None or row["budget_digest"] != budget_digest:
            raise StateConflict("CURRENT_BUDGET_BINDING_MISMATCH")

    @staticmethod
    def _set_task_state(
        connection: sqlite3.Connection,
        run_id: RunId,
        task_id: TaskId,
        state: TaskLifecycleState,
    ) -> None:
        legal_sources: dict[TaskLifecycleState, frozenset[TaskLifecycleState]] = {
            "ACTIVE": frozenset(),
            "READY": frozenset({"ACTIVE", "PAUSED"}),
            "PAUSED": frozenset({"ACTIVE"}),
        }
        sources = legal_sources[state]
        if not sources:
            raise StateConflict("TASK_STATE_TRANSITION_ILLEGAL")
        placeholders = ", ".join("?" for _ in sources)
        parameters = (state, run_id, task_id, *sorted(sources))
        changed = connection.execute(
            f"UPDATE tasks SET state = ? WHERE run_id = ? AND task_id = ? "
            f"AND state IN ({placeholders})",
            parameters,
        ).rowcount
        if changed != 1:
            raise StateConflict("TASK_STATE_TRANSITION_ILLEGAL")

    @classmethod
    def _pause_task(
        cls,
        connection: sqlite3.Connection,
        run_id: RunId,
        task_id: TaskId,
        reason: Literal["REPEATED_CHECKPOINT", "REPEATED_INVALID_ACTION"],
        counter: int,
    ) -> None:
        cls._set_task_state(connection, run_id, task_id, "PAUSED")
        if (
            connection.execute(
                "UPDATE tasks SET pause_reason = ?, pause_counter = ? "
                "WHERE run_id = ? AND task_id = ? AND state = 'PAUSED'",
                (reason, counter, run_id, task_id),
            ).rowcount
            != 1
        ):
            raise StateConflict("TASK_PAUSE_PERSIST_FAILED")

    @staticmethod
    def _finish_attempt(
        connection: sqlite3.Connection,
        run_id: RunId,
        task_id: TaskId,
        attempt_id: AttemptId,
        state: AttemptLifecycleState,
    ) -> None:
        if state != "FAILED":
            raise StateConflict("ATTEMPT_STATE_TRANSITION_ILLEGAL")
        changed = connection.execute(
            "UPDATE attempts SET state = 'FAILED' WHERE run_id = ? AND task_id = ? "
            "AND attempt_id = ? AND state = 'RUNNING'",
            (run_id, task_id, attempt_id),
        ).rowcount
        if changed != 1:
            raise StateConflict("ATTEMPT_STATE_TRANSITION_ILLEGAL")

    def _release_attempt_lease(
        self,
        connection: sqlite3.Connection,
        run_id: RunId,
        attempt_id: AttemptId,
        terminal_sequence: AuditSequence,
    ) -> None:
        connection.execute(
            "UPDATE workspace_leases SET state = 'REVOKED', terminal_sequence = ? "
            "WHERE run_id = ? AND attempt_id = ? AND state = 'ACTIVE'",
            (terminal_sequence, run_id, attempt_id),
        )
        if (
            connection.execute(
                "SELECT 1 FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            is not None
        ):
            budget_row = connection.execute(
                "SELECT budget_digest FROM approved_budgets_for_test WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if budget_row is None:
                raise StateConflict("APPROVED_BUDGET_NOT_FOUND")
            active_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM workspace_leases WHERE run_id = ? AND state = 'ACTIVE'",
                    (run_id,),
                ).fetchone()[0]
            )
            self._settle_global_usage_in_transaction(
                connection,
                run_id,
                RevisionDigest(str(budget_row["budget_digest"])),
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

        def mutate(connection: sqlite3.Connection) -> None:
            nonlocal count
            self._require_current_task_budget(connection, task, budget_digest)
            row = connection.execute(
                "SELECT state FROM tasks WHERE run_id = ? AND task_id = ?",
                (task.run_id, task.task_id),
            ).fetchone()
            if row is None or row["state"] != "ACTIVE":
                raise StateConflict("TASK_CHECKPOINT_SOURCE_STATE_ILLEGAL")
            connection.execute(
                "INSERT INTO task_checkpoints(run_id, task_id, tree_oid, check_set_digest, "
                "budget_digest, observed_sequence) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    task.run_id,
                    task.task_id,
                    checkpoint.tree_oid,
                    checkpoint.check_set_digest,
                    budget_digest,
                    expected_sequence + 1,
                ),
            )
            count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM task_checkpoints WHERE run_id = ? AND task_id = ? "
                    "AND tree_oid = ? AND check_set_digest = ?",
                    (task.run_id, task.task_id, checkpoint.tree_oid, checkpoint.check_set_digest),
                ).fetchone()[0]
            )
            if count >= V01_MECHANISM_LIMITS.repeated_checkpoint_ceiling:
                self._pause_task(
                    connection,
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

        def mutate(connection: sqlite3.Connection) -> None:
            nonlocal count
            self._require_current_task_budget(connection, task, budget_digest)
            self._finish_attempt(
                connection,
                task.run_id,
                task.task_id,
                attempt_id,
                "FAILED",
            )
            task_row = connection.execute(
                "SELECT state FROM tasks WHERE run_id = ? AND task_id = ?",
                (task.run_id, task.task_id),
            ).fetchone()
            if task_row is None or task_row["state"] != "ACTIVE":
                raise StateConflict("TASK_INVALID_ACTION_SOURCE_STATE_ILLEGAL")
            self._release_attempt_lease(
                connection,
                task.run_id,
                attempt_id,
                AuditSequence(expected_sequence + 1),
            )
            connection.execute(
                "INSERT INTO task_invalid_actions(run_id, task_id, attempt_id, action_digest, "
                "budget_digest, observed_sequence) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    task.run_id,
                    task.task_id,
                    attempt_id,
                    action_digest,
                    budget_digest,
                    expected_sequence + 1,
                ),
            )
            count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM task_invalid_actions WHERE run_id = ? AND task_id = ? "
                    "AND action_digest = ?",
                    (task.run_id, task.task_id, action_digest),
                ).fetchone()[0]
            )
            if count >= V01_MECHANISM_LIMITS.repeated_invalid_action_ceiling:
                self._pause_task(
                    connection,
                    task.run_id,
                    task.task_id,
                    "REPEATED_INVALID_ACTION",
                    count,
                )
            else:
                self._set_task_state(connection, task.run_id, task.task_id, "READY")

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
        with self._read_transaction() as connection:
            task = connection.execute(
                "SELECT state FROM tasks WHERE run_id = ? AND task_id = ?",
                (run_id, task_id),
            ).fetchone()
            run = connection.execute(
                "SELECT new_dispatch_open, dispatch_close_causes_json FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        if task is None:
            raise StateConflict("TASK_NOT_FOUND")
        if task["state"] == "PAUSED":
            return DispatchAuthorization("DENY", "TASK_PAUSED")
        if run is not None and not bool(run["new_dispatch_open"]):
            dispatch_close_causes_from_json(str(run["dispatch_close_causes_json"]))
            budget_digest, budget = self.current_approved_budget(run_id)
            del budget_digest
            if self.global_usage_snapshot(run_id).active_run_seconds >= global_ceiling_for(
                budget, GlobalBudgetMetric.ACTIVE_RUN_SECONDS
            ):
                return DispatchAuthorization("DENY", "ACTIVE_RUN_TIME_CEILING")
            return DispatchAuthorization("DENY", "RUN_DISPATCH_CLOSED")
        if task["state"] != "READY":
            return DispatchAuthorization("DENY", "TASK_NOT_READY")
        return DispatchAuthorization("ALLOW", "AUTHORIZED")

    def install_approved_budget_for_test(
        self,
        run_id: RunId,
        budget_digest: RevisionDigest,
        budget: BudgetRevisionDocument,
    ) -> None:
        if revision_digest(budget) != budget_digest:
            raise StateConflict("APPROVED_BUDGET_DIGEST_MISMATCH")
        with self._lock:
            self._connection.execute(
                "INSERT INTO approved_budgets_for_test(run_id, budget_digest, budget_json) "
                "VALUES (?, ?, ?) ON CONFLICT(run_id) DO UPDATE SET "
                "budget_digest = excluded.budget_digest, budget_json = excluded.budget_json",
                (run_id, budget_digest, budget.model_dump_json()),
            )

    def current_approved_budget(
        self, run_id: RunId
    ) -> tuple[RevisionDigest, BudgetRevisionDocument]:
        with self._read_transaction() as connection:
            row = connection.execute(
                "SELECT budget_digest, budget_json FROM approved_budgets_for_test WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            raise StateConflict("APPROVED_BUDGET_NOT_FOUND")
        budget = BudgetRevisionDocument.model_validate_json(str(row["budget_json"]))
        digest = RevisionDigest(str(row["budget_digest"]))
        if revision_digest(budget) != digest:
            raise StateConflict("APPROVED_BUDGET_STORAGE_INVALID")
        return digest, budget

    @staticmethod
    def _approved_budget_for_update(
        connection: sqlite3.Connection,
        run_id: RunId,
        budget_digest: RevisionDigest,
    ) -> BudgetRevisionDocument:
        if not connection.in_transaction:
            raise StateConflict("RUN_WRITE_TRANSACTION_REQUIRED")
        row = connection.execute(
            "SELECT budget_digest, budget_json FROM approved_budgets_for_test WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if row is None or row["budget_digest"] != budget_digest:
            raise StateConflict("CURRENT_BUDGET_BINDING_MISMATCH")
        budget = BudgetRevisionDocument.model_validate_json(str(row["budget_json"]))
        if revision_digest(budget) != budget_digest:
            raise StateConflict("APPROVED_BUDGET_STORAGE_INVALID")
        return budget

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

    @staticmethod
    def _dispatch_state_for_update(
        connection: sqlite3.Connection,
        run_id: RunId,
    ) -> tuple[bool, frozenset[DispatchCloseCause], str]:
        if not connection.in_transaction:
            raise StateConflict("RUN_WRITE_TRANSACTION_REQUIRED")
        row = connection.execute(
            "SELECT new_dispatch_open, dispatch_close_causes_json FROM runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            raise StateConflict("RUN_NOT_FOUND")
        raw = str(row["dispatch_close_causes_json"])
        causes = dispatch_close_causes_from_json(raw)
        is_open = bool(row["new_dispatch_open"])
        if is_open != (not causes):
            raise StateConflict("DISPATCH_CLOSURE_BINDING_INVALID")
        return is_open, causes, raw

    def _require_new_dispatch_open(
        self,
        connection: sqlite3.Connection,
        run_id: RunId,
    ) -> None:
        is_open, _, _ = self._dispatch_state_for_update(connection, run_id)
        if not is_open:
            raise StateConflict("NEW_DISPATCH_CLOSED")

    def _close_new_dispatch(
        self,
        connection: sqlite3.Connection,
        run_id: RunId,
        cause: DispatchCloseCause,
    ) -> bool:
        normalized = DispatchCloseCause(cause)
        is_open, causes, prior_json = self._dispatch_state_for_update(connection, run_id)
        if not is_open and normalized in causes:
            return False
        next_json = dispatch_close_causes_to_json(causes | {normalized})
        if (
            connection.execute(
                "UPDATE runs SET new_dispatch_open = 0, dispatch_close_causes_json = ? "
                "WHERE run_id = ? AND new_dispatch_open = ? "
                "AND dispatch_close_causes_json = ?",
                (next_json, run_id, int(is_open), prior_json),
            ).rowcount
            != 1
        ):
            raise StateConflict("DISPATCH_CLOSE_COMPARE_AND_SET_FAILED")
        return True

    @staticmethod
    def _read_global_usage(
        connection: sqlite3.Connection,
        run_id: RunId,
        metric: GlobalBudgetMetric,
    ) -> int | Decimal:
        normalized = normalize_global_budget_metric(metric)
        row = connection.execute(
            "SELECT absolute_used FROM global_budget_usage WHERE run_id = ? AND metric = ?",
            (run_id, normalized.value),
        ).fetchone()
        value = "0" if row is None else str(row["absolute_used"])
        return global_numeric_from_text(normalized, value)

    def _settle_global_usage_in_transaction(
        self,
        connection: sqlite3.Connection,
        run_id: RunId,
        budget_digest: RevisionDigest,
        metric: GlobalBudgetMetric,
        absolute_used: int | Decimal,
        *,
        allow_reservation_reconciliation: bool = False,
    ) -> tuple[BudgetSettlement, bool]:
        budget = self._approved_budget_for_update(connection, run_id, budget_digest)
        normalized_metric = normalize_global_budget_metric(metric)
        normalized_used = self._normalize_global_usage(normalized_metric, absolute_used)
        ceiling = global_ceiling_for(budget, normalized_metric)
        previous = self._read_global_usage(connection, run_id, normalized_metric)
        if (
            normalized_used < previous
            and normalized_metric != GlobalBudgetMetric.CONCURRENT_WORKERS
            and not allow_reservation_reconciliation
        ):
            raise StateConflict("GLOBAL_USAGE_NOT_MONOTONIC")
        connection.execute(
            "INSERT INTO global_budget_usage(run_id, metric, absolute_used) "
            "VALUES (?, ?, ?) ON CONFLICT(run_id, metric) DO UPDATE SET "
            "absolute_used = excluded.absolute_used",
            (run_id, normalized_metric.value, str(normalized_used)),
        )
        warning_percent = V01_MECHANISM_LIMITS.warning_percent
        if crossed_threshold(previous, normalized_used, ceiling, warning_percent):
            warning = BudgetWarning(
                run_id,
                budget_digest,
                normalized_metric,
                normalized_used,
                ceiling,
                warning_percent,
            )
            connection.execute(
                "INSERT INTO budget_warnings(run_id, budget_digest, metric, "
                "warning_percent, warning_json) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT DO NOTHING",
                (
                    run_id,
                    budget_digest,
                    normalized_metric.value,
                    warning_percent,
                    budget_warning_to_json(warning),
                ),
            )
        pause = normalized_used >= ceiling
        stopped = pause and self._close_new_dispatch(
            connection,
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

    def global_usage_snapshot(self, run_id: RunId) -> GlobalUsageSnapshot:
        with self._read_transaction() as connection:
            if (
                connection.execute(
                    "SELECT 1 FROM runs WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
                is None
            ):
                raise StateConflict("RUN_NOT_FOUND")
            rows = connection.execute(
                "SELECT metric, absolute_used FROM global_budget_usage WHERE run_id = ?",
                (run_id,),
            ).fetchall()
        values = {
            normalize_global_budget_metric(row["metric"]): str(row["absolute_used"]) for row in rows
        }
        return GlobalUsageSnapshot(
            active_run_seconds=Decimal(values.get(GlobalBudgetMetric.ACTIVE_RUN_SECONDS, "0")),
            tasks=int(values.get(GlobalBudgetMetric.TASKS, "0")),
            planning_requests=int(values.get(GlobalBudgetMetric.PLANNING_REQUESTS, "0")),
            model_calls=int(values.get(GlobalBudgetMetric.MODEL_CALLS, "0")),
            input_tokens=int(values.get(GlobalBudgetMetric.INPUT_TOKENS, "0")),
            output_tokens=int(values.get(GlobalBudgetMetric.OUTPUT_TOKENS, "0")),
            cost_reserve_usd=Decimal(values.get(GlobalBudgetMetric.COST_RESERVE_USD, "0")),
            concurrent_workers=int(values.get(GlobalBudgetMetric.CONCURRENT_WORKERS, "0")),
        )

    def settle_global_usage(
        self,
        run_id: RunId,
        budget_digest: RevisionDigest,
        metric: GlobalBudgetMetric,
        absolute_used: int | Decimal,
        expected_sequence: AuditSequence,
    ) -> BudgetSettlement:
        result: list[BudgetSettlement] = []
        events = [AuditEvent.kind("GLOBAL_BUDGET_USAGE_SETTLED")]

        def mutate(connection: sqlite3.Connection) -> None:
            settlement, stopped = self._settle_global_usage_in_transaction(
                connection,
                run_id,
                budget_digest,
                metric,
                absolute_used,
            )
            if stopped:
                events.append(AuditEvent.kind("BUDGET_STOP_REQUESTED"))
            result.append(
                replace(
                    settlement,
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

    def begin_atomic_action(
        self,
        action: AtomicAction,
        expected_sequence: AuditSequence,
    ) -> AtomicAction:
        def mutate(connection: sqlite3.Connection) -> None:
            self._approved_budget_for_update(
                connection,
                action.run_id,
                action.budget_digest,
            )
            self._require_new_dispatch_open(connection, action.run_id)
            try:
                connection.execute(
                    "INSERT INTO atomic_actions(run_id, action_id, budget_digest, "
                    "state, opened_sequence) VALUES (?, ?, ?, 'IN_FLIGHT', ?)",
                    (
                        action.run_id,
                        action.action_id,
                        action.budget_digest,
                        action.opened_sequence,
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise StateConflict("ATOMIC_ACTION_ID_REUSED") from error

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

        def mutate(connection: sqlite3.Connection) -> None:
            self._approved_budget_for_update(
                connection,
                action.run_id,
                action.budget_digest,
            )
            changed = connection.execute(
                "UPDATE atomic_actions SET state = 'SETTLED' "
                "WHERE run_id = ? AND action_id = ? AND budget_digest = ? "
                "AND opened_sequence = ? AND state = 'IN_FLIGHT'",
                (
                    action.run_id,
                    action.action_id,
                    action.budget_digest,
                    action.opened_sequence,
                ),
            ).rowcount
            if changed != 1:
                raise StateConflict("ATOMIC_ACTION_SETTLE_COMPARE_AND_SET_FAILED")
            previous = self._read_global_usage(
                connection,
                action.run_id,
                GlobalBudgetMetric.MODEL_CALLS,
            )
            settlement, stopped = self._settle_global_usage_in_transaction(
                connection,
                action.run_id,
                action.budget_digest,
                GlobalBudgetMetric.MODEL_CALLS,
                int(previous) + model_calls,
            )
            if stopped:
                events.append(AuditEvent.kind("BUDGET_STOP_REQUESTED"))
            result.append(
                replace(
                    settlement,
                    action_state="SETTLED",
                    pause_reason=(
                        "GLOBAL_MODEL_CALL_CEILING" if settlement.pause_after_barrier else None
                    ),
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

    def authorize_new_action(self, run_id: RunId) -> DispatchAuthorization:
        with self._read_transaction() as connection:
            row = connection.execute(
                "SELECT new_dispatch_open, dispatch_close_causes_json FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            raise StateConflict("RUN_NOT_FOUND")
        causes = dispatch_close_causes_from_json(str(row["dispatch_close_causes_json"]))
        is_open = bool(row["new_dispatch_open"])
        if is_open != (not causes):
            raise StateConflict("DISPATCH_CLOSURE_BINDING_INVALID")
        if not is_open:
            return DispatchAuthorization("DENY", "RUN_DISPATCH_CLOSED")
        return DispatchAuthorization("ALLOW", "AUTHORIZED")

    def budget_warnings(
        self,
        run_id: RunId,
        metric: GlobalBudgetMetric | str,
    ) -> tuple[BudgetWarning, ...]:
        normalized = normalize_global_budget_metric(metric)
        with self._read_transaction() as connection:
            rows = connection.execute(
                "SELECT warning_json FROM budget_warnings "
                "WHERE run_id = ? AND metric = ? ORDER BY budget_digest, warning_percent",
                (run_id, normalized.value),
            ).fetchall()
        return tuple(budget_warning_from_json(str(row["warning_json"])) for row in rows)

    def budget_warning_metrics(self, run_id: RunId) -> tuple[GlobalBudgetMetric, ...]:
        with self._read_transaction() as connection:
            rows = connection.execute(
                "SELECT DISTINCT metric FROM budget_warnings WHERE run_id = ? ORDER BY metric",
                (run_id,),
            ).fetchall()
        return tuple(normalize_global_budget_metric(row["metric"]) for row in rows)

    def dispatch_close_causes(self, run_id: RunId) -> frozenset[DispatchCloseCause]:
        with self._read_transaction() as connection:
            row = connection.execute(
                "SELECT new_dispatch_open, dispatch_close_causes_json FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            raise StateConflict("RUN_NOT_FOUND")
        causes = dispatch_close_causes_from_json(str(row["dispatch_close_causes_json"]))
        if bool(row["new_dispatch_open"]) != (not causes):
            raise StateConflict("DISPATCH_CLOSURE_BINDING_INVALID")
        return causes

    def audit_event_kinds(self, run_id: RunId) -> tuple[str, ...]:
        with self._read_transaction() as connection:
            rows = connection.execute(
                "SELECT event_kind FROM audit_events WHERE run_id = ? ORDER BY sequence",
                (run_id,),
            ).fetchall()
        return tuple(str(row["event_kind"]) for row in rows)

    def runtime_barrier_state(self, run_id: RunId) -> Literal["IN_FLIGHT", "SETTLED"]:
        with self._read_transaction() as connection:
            row = connection.execute(
                "SELECT 1 FROM model_attempts WHERE run_id = ? AND state = 'RESERVED' LIMIT 1",
                (run_id,),
            ).fetchone()
        return "IN_FLIGHT" if row is not None else "SETTLED"

    def _evaluate_model_reservation(
        self, connection: sqlite3.Connection, request: ModelReservationRequest
    ) -> _ReservationEvaluation:
        sequence_row = connection.execute(
            "SELECT current_sequence FROM run_sequences WHERE run_id = ?",
            (request.run_id,),
        ).fetchone()
        current_sequence = AuditSequence(
            0 if sequence_row is None else sequence_row["current_sequence"]
        )
        budget_row = connection.execute(
            "SELECT budget_digest, budget_json FROM approved_budgets_for_test WHERE run_id = ?",
            (request.run_id,),
        ).fetchone()
        if budget_row is None:
            raise StateConflict("APPROVED_BUDGET_NOT_FOUND")
        budget = BudgetRevisionDocument.model_validate_json(str(budget_row["budget_json"]))
        budget_digest = RevisionDigest(str(budget_row["budget_digest"]))
        if revision_digest(budget) != budget_digest:
            raise StateConflict("APPROVED_BUDGET_STORAGE_INVALID")
        run_counters = self._model_counters(connection, request.run_id)
        task_counters = (
            None
            if request.task_id is None
            else self._task_budget_state(connection, request.run_id, request.task_id)
        )
        planning_row = connection.execute(
            "SELECT planning_requests FROM run_authority_counters WHERE run_id = ?",
            (request.run_id,),
        ).fetchone()
        planning_requests = 0 if planning_row is None else int(planning_row[0])
        try:
            amounts = model_reservation_amounts(request.model_request, budget)
        except ValueError:
            amounts = ModelBudgetAmounts.zero()
            pricing_missing = True
        else:
            pricing_missing = False
        reason: ModelReservationReason | None = None
        if current_sequence != request.expected_sequence:
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
            run = connection.execute(
                "SELECT state, current_plan_digest, current_policy_digest, "
                "current_budget_digest, current_model_configuration_digest, "
                "new_dispatch_open FROM runs WHERE run_id = ?",
                (request.run_id,),
            ).fetchone()
            if run is not None:
                expected_revisions = (
                    request.model_request.plan_digest,
                    request.model_request.policy_digest,
                    request.model_request.budget_digest,
                    request.model_request.model_configuration_digest,
                )
                current_revisions = (
                    run["current_plan_digest"],
                    run["current_policy_digest"],
                    run["current_budget_digest"],
                    run["current_model_configuration_digest"],
                )
                target = connection.execute(
                    "SELECT admin_binding_digest FROM target_reservations WHERE run_id = ?",
                    (request.run_id,),
                ).fetchone()
                if current_revisions != expected_revisions:
                    reason = "REVISION_BINDING_MISMATCH"
                elif target is None or target[0] != request.target_safety_digest:
                    reason = "TARGET_BINDING_MISMATCH"
                elif run["new_dispatch_open"] != 1 or run["state"] not in {
                    "PLANNING",
                    "ACTIVE",
                }:
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
        with self._read_transaction() as connection:
            evaluation = self._evaluate_model_reservation(connection, request)
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
        if evaluation.reason is not None:

            def pause(connection: sqlite3.Connection) -> None:
                current = self._evaluate_model_reservation(connection, request)
                if current.reason != evaluation.reason:
                    raise StateConflict("MODEL_RESERVATION_REVALIDATION_MISMATCH")
                run_bound = connection.execute(
                    "SELECT 1 FROM runs WHERE run_id = ?",
                    (request.run_id,),
                ).fetchone()
                if run_bound is not None:
                    self._close_new_dispatch(
                        connection,
                        request.run_id,
                        DispatchCloseCause.BUDGET_EXHAUSTED,
                    )
                    connection.execute(
                        "UPDATE runs SET state = 'PAUSED' WHERE run_id = ?",
                        (request.run_id,),
                    )

            sequence = self._commit_state_and_event(
                run_id=request.run_id,
                expected_sequence=request.expected_sequence,
                event=AuditEvent.kind("MODEL_RESERVATION_PAUSED", result_class=evaluation.reason),
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

        def mutate(connection: sqlite3.Connection) -> None:
            nonlocal turn, intent, producer_stopped, run_after, task_after
            current = self._evaluate_model_reservation(connection, request)
            if current.reason is not None or current != evaluation:
                raise StateConflict("MODEL_RESERVATION_REVALIDATION_MISMATCH")
            run_bound = (
                connection.execute(
                    "SELECT 1 FROM runs WHERE run_id = ?",
                    (request.run_id,),
                ).fetchone()
                is not None
            )
            if run_bound:
                self._require_new_dispatch_open(connection, request.run_id)
            if request.turn is None:
                turn = LogicalModelTurn.new(request.model_request)
                connection.execute(
                    "INSERT INTO model_turns(logical_turn_id, run_id, request_digest, "
                    "created_sequence, state, owner_kind, task_id, attempt_id, tranche_id) "
                    "VALUES (?, ?, ?, ?, 'OPEN', ?, ?, ?, ?)",
                    (
                        turn.logical_turn_id,
                        request.run_id,
                        request.model_request.request_digest,
                        request.expected_sequence + 1,
                        request.owner_kind,
                        request.task_id,
                        request.attempt_id,
                        request.tranche_id,
                    ),
                )
            else:
                turn = request.turn
                bound = connection.execute(
                    "SELECT 1 FROM model_turns WHERE run_id = ? AND logical_turn_id = ? "
                    "AND request_digest = ? AND state = 'OPEN'",
                    (request.run_id, turn.logical_turn_id, turn.request_digest),
                ).fetchone()
                if bound is None:
                    raise StateConflict("MODEL_TURN_BINDING_MISMATCH")
            intent = replace(
                ModelRequestIntent.reserve(
                    turn, request.model_request, request.provider_attempt_number
                ),
                reserved_amounts=evaluation.amounts,
            )
            connection.execute(
                "INSERT INTO model_attempts(intent_id, run_id, logical_turn_id, "
                "provider_attempt_number, request_json, request_digest, idempotency_key, "
                "reserved_json, allowed_model_ids_json, reserved_sequence, state, "
                "owner_kind, task_id, attempt_id, tranche_id, dispatch_deadline_at_utc, "
                "target_safety_digest, budget_digest, model_configuration_digest) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'RESERVED', ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    intent.intent_id,
                    request.run_id,
                    turn.logical_turn_id,
                    request.provider_attempt_number,
                    model_request_to_json(request.model_request),
                    request.model_request.request_digest,
                    request.model_request.idempotency_key,
                    evaluation.amounts.to_json(),
                    json.dumps(
                        sorted(request.model_request.allowed_model_ids), separators=(",", ":")
                    ),
                    request.expected_sequence + 1,
                    request.owner_kind,
                    request.task_id,
                    request.attempt_id,
                    request.tranche_id,
                    request.deadline_at_utc.isoformat(),
                    request.target_safety_digest,
                    request.model_request.budget_digest,
                    request.model_request.model_configuration_digest,
                ),
            )
            run_after = evaluation.run_counters.reserve(evaluation.amounts)
            self._write_model_counters(connection, request.run_id, run_after)
            if request.owner_kind == "PLANNING":
                connection.execute(
                    "INSERT INTO run_authority_counters(run_id, planning_requests) "
                    "VALUES (?, ?) ON CONFLICT(run_id) DO UPDATE SET "
                    "planning_requests = excluded.planning_requests",
                    (request.run_id, evaluation.planning_requests + 1),
                )
            else:
                if evaluation.task_counters is None:
                    raise StateConflict("TASK_COUNTERS_REQUIRED")
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
                self._write_task_budget_state(connection, task_after)
                connection.execute(
                    "UPDATE task_tranches SET consumed_calls = consumed_calls + 1 "
                    "WHERE run_id = ? AND task_id = ? AND tranche_id = ?",
                    (request.run_id, request.task_id, request.tranche_id),
                )

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
                    _, stopped = self._settle_global_usage_in_transaction(
                        connection,
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

        def mutate(connection: sqlite3.Connection) -> None:
            nonlocal producer_stopped
            budget = self._approved_budget_for_update(
                connection,
                lease.run_id,
                budget_digest,
            )
            run_bound = (
                connection.execute(
                    "SELECT 1 FROM runs WHERE run_id = ?",
                    (lease.run_id,),
                ).fetchone()
                is not None
            )
            if run_bound:
                self._require_new_dispatch_open(connection, lease.run_id)
            active = tuple(
                _workspace_lease_from_row(row)
                for row in connection.execute(
                    "SELECT * FROM workspace_leases WHERE run_id = ? AND state = 'ACTIVE' "
                    "AND expires_at_utc > ?",
                    (lease.run_id, lease.issued_at.isoformat()),
                )
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
            connection.execute(
                "INSERT INTO workspace_leases(lease_id, run_id, task_id, attempt_id, "
                "generation, base_head, admissible_head, task_contract_digest, "
                "write_globs_json, sensitivity_globs_json, issued_at_utc, expires_at_utc, "
                "state, issued_sequence) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    lease.lease_id,
                    lease.run_id,
                    lease.task_id,
                    lease.attempt_id,
                    lease.generation,
                    lease.base_head,
                    lease.admissible_head,
                    lease.task_contract_digest,
                    json.dumps([item.value for item in lease.write_globs], separators=(",", ":")),
                    json.dumps(
                        [item.value for item in lease.sensitivity_globs], separators=(",", ":")
                    ),
                    lease.issued_at.isoformat(),
                    lease.expires_at.isoformat(),
                    lease.state,
                    expected_sequence + 1,
                ),
            )
            if run_bound:
                _, producer_stopped = self._settle_global_usage_in_transaction(
                    connection,
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
        with self._read_transaction() as connection:
            row = connection.execute(
                "SELECT * FROM workspace_leases WHERE run_id = ? AND lease_id = ?",
                (run_id, lease_id),
            ).fetchone()
        return None if row is None else _workspace_lease_from_row(row)

    def expire_workspace_lease(
        self,
        run_id: RunId,
        lease_id: str,
        expected_sequence: AuditSequence,
    ) -> AuditSequence:
        def mutate(connection: sqlite3.Connection) -> None:
            if (
                connection.execute(
                    "UPDATE workspace_leases SET state = 'EXPIRED', terminal_sequence = ? "
                    "WHERE run_id = ? AND lease_id = ? AND state = 'ACTIVE'",
                    (expected_sequence + 1, run_id, lease_id),
                ).rowcount
                != 1
            ):
                raise StateConflict("WORKSPACE_LEASE_NOT_ACTIVE")
            if (
                connection.execute(
                    "SELECT 1 FROM runs WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
                is not None
            ):
                budget_row = connection.execute(
                    "SELECT budget_digest FROM approved_budgets_for_test WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
                if budget_row is None:
                    raise StateConflict("APPROVED_BUDGET_NOT_FOUND")
                active_count = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM workspace_leases "
                        "WHERE run_id = ? AND state = 'ACTIVE'",
                        (run_id,),
                    ).fetchone()[0]
                )
                self._settle_global_usage_in_transaction(
                    connection,
                    run_id,
                    RevisionDigest(str(budget_row["budget_digest"])),
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

        def mutate(connection: sqlite3.Connection) -> None:
            nonlocal renewed
            row = connection.execute(
                "SELECT * FROM workspace_leases WHERE run_id = ? AND lease_id = ?",
                (run_id, lease_id),
            ).fetchone()
            if row is None:
                raise _LeaseDenied(LeaseDenial())
            current = _workspace_lease_from_row(row)
            if (
                current.state != "ACTIVE"
                or current.expires_at <= renewed_at
                or current.generation != generation
            ):
                raise _LeaseDenied(LeaseDenial())
            others = tuple(
                _workspace_lease_from_row(item)
                for item in connection.execute(
                    "SELECT * FROM workspace_leases WHERE run_id = ? AND lease_id <> ? "
                    "AND state = 'ACTIVE' AND expires_at_utc > ?",
                    (run_id, lease_id, renewed_at.isoformat()),
                )
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
            connection.execute(
                "UPDATE workspace_leases SET admissible_head = ?, expires_at_utc = ?, "
                "renewed_sequence = ? WHERE run_id = ? AND lease_id = ?",
                (
                    latest_admissible_head,
                    expires_at.isoformat(),
                    expected_sequence + 1,
                    run_id,
                    lease_id,
                ),
            )

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

    def _require_current_revisions(
        self,
        connection: sqlite3.Connection,
        run_id: RunId,
        expected: ApplicableRevisionDigests,
    ) -> None:
        row = connection.execute(
            "SELECT current_plan_digest, current_policy_digest, current_budget_digest, "
            "current_model_configuration_digest FROM runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            raise StateConflict("RUN_NOT_FOUND")
        current = ApplicableRevisionDigests(
            plan_digest=row["current_plan_digest"],
            policy_digest=row["current_policy_digest"],
            budget_digest=row["current_budget_digest"],
            model_configuration_digest=row["current_model_configuration_digest"],
        )
        if current != expected:
            raise StateConflict("CURRENT_REVISION_BINDING_MISMATCH")

    def _require_current_budget(
        self,
        connection: sqlite3.Connection,
        run_id: RunId,
        budget_digest: RevisionDigest,
    ) -> None:
        row = connection.execute(
            "SELECT current_budget_digest FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None or row["current_budget_digest"] != budget_digest:
            raise StateConflict("CURRENT_BUDGET_BINDING_MISMATCH")

    def authorization_binding_failure(
        self, request: AuthorizationRequest
    ) -> AuthorizationReason | None:
        with self._read_transaction() as connection:
            row = connection.execute(
                "SELECT state, current_plan_digest, current_policy_digest, "
                "current_budget_digest, current_model_configuration_digest, "
                "new_dispatch_open FROM runs WHERE run_id = ?",
                (request.run_id,),
            ).fetchone()
            target = connection.execute(
                "SELECT admin_binding_digest FROM target_reservations WHERE run_id = ?",
                (request.run_id,),
            ).fetchone()
        if row is None or row["state"] != "ACTIVE" or row["new_dispatch_open"] != 1:
            return "RUN_NOT_DISPATCHABLE"
        if (
            row["current_plan_digest"] != request.plan_digest
            or row["current_policy_digest"] != request.policy_digest
            or row["current_budget_digest"] != request.budget_digest
            or row["current_model_configuration_digest"] != request.model_configuration_digest
        ):
            return "REVISION_BINDING_MISMATCH"
        if target is None or target["admin_binding_digest"] != request.target_safety_digest:
            return "TARGET_BINDING_MISMATCH"
        return None

    def record_authorization_denial(
        self,
        request: AuthorizationRequest,
        binding_digest: str,
        reason: AuthorizationReason,
        expected_sequence: AuditSequence,
    ) -> AuditSequence:
        def mutate(connection: sqlite3.Connection) -> None:
            connection.execute(
                "INSERT INTO authorization_denials(run_id, task_id, attempt_id, action_id, "
                "action_digest, binding_digest, plan_digest, policy_digest, budget_digest, "
                "model_configuration_digest, occurred_at_utc, reason, denied_sequence) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    request.run_id,
                    request.task_id,
                    request.attempt_id,
                    request.action_id,
                    request.action_digest,
                    binding_digest,
                    request.plan_digest,
                    request.policy_digest,
                    request.budget_digest,
                    request.model_configuration_digest,
                    request.started_at_utc.isoformat(),
                    reason,
                    expected_sequence + 1,
                ),
            )

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

    def _task_budget_state(
        self, connection: sqlite3.Connection, run_id: RunId, task_id: TaskId
    ) -> TaskBudgetState:
        row = connection.execute(
            "SELECT counters_json, counters_digest FROM task_budget_counters "
            "WHERE run_id = ? AND task_id = ?",
            (run_id, task_id),
        ).fetchone()
        if row is None:
            return TaskBudgetState(run_id=run_id, task_id=task_id)
        counters_json = str(row["counters_json"])
        if sha256_digest(counters_json) != row["counters_digest"]:
            raise StateConflict("TASK_BUDGET_STORAGE_INVALID")
        state = _task_budget_from_json(counters_json)
        if state.run_id != run_id or state.task_id != task_id:
            raise StateConflict("TASK_BUDGET_STORAGE_INVALID")
        return state

    def _write_task_budget_state(
        self, connection: sqlite3.Connection, state: TaskBudgetState
    ) -> None:
        counters_json = _task_budget_json(state)
        connection.execute(
            "INSERT INTO task_budget_counters(run_id, task_id, counters_json, counters_digest) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(run_id, task_id) DO UPDATE SET "
            "counters_json = excluded.counters_json, counters_digest = excluded.counters_digest",
            (state.run_id, state.task_id, counters_json, sha256_digest(counters_json)),
        )

    def task_budget_state(self, run_id: RunId, task_id: TaskId) -> TaskBudgetState:
        with self._read_transaction() as connection:
            return self._task_budget_state(connection, run_id, task_id)

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
        tranche_number = expected.tranche_count + 1
        tranche_id = (
            None if calls == 0 else f"tranche-{task.run_id}-{task.task_id}-{tranche_number}"
        )
        decision: Literal["ALLOCATE", "PAUSE"]
        if calls == 0:
            after = replace(
                expected,
                consecutive_no_progress_tranches=(
                    expected.consecutive_no_progress_tranches + 1
                    if reason == "NO_PROGRESS"
                    else expected.consecutive_no_progress_tranches
                ),
            )
            event_kind = "TASK_PAUSED_NO_PROGRESS"
            decision = "PAUSE"
        else:
            after = replace(
                expected,
                allocated_calls=expected.allocated_calls + calls,
                tranche_count=tranche_number,
                bootstrap_tranches=expected.bootstrap_tranches
                + (1 if reason == "BOOTSTRAP" else 0),
                consecutive_no_progress_tranches=(
                    0
                    if reason == "OBJECTIVE_PROGRESS"
                    else expected.consecutive_no_progress_tranches
                ),
                active_tranche_id=tranche_id,
                active_tranche_remaining_calls=calls,
            )
            event_kind = "TASK_TRANCHE_ALLOCATED"
            decision = "ALLOCATE"
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

        def mutate(connection: sqlite3.Connection) -> None:
            current = self._task_budget_state(connection, task.run_id, task.task_id)
            if current != expected:
                raise StateConflict("TASK_COUNTER_SNAPSHOT_MISMATCH")
            if current.active_tranche_remaining_calls:
                raise StateConflict("TASK_TRANCHE_STILL_ACTIVE")
            if calls == 0:
                if reason not in {"NO_PROGRESS", "TASK_CALL_CEILING"}:
                    raise StateConflict("TASK_TRANCHE_PAUSE_REASON_INVALID")
            elif not 1 <= calls <= 8 or reason not in {"BOOTSTRAP", "OBJECTIVE_PROGRESS"}:
                raise StateConflict("TASK_TRANCHE_ALLOCATION_INVALID")
            self._write_task_budget_state(connection, after)
            if tranche_id is not None:
                connection.execute(
                    "INSERT INTO task_tranches(run_id, task_id, tranche_id, attempt_id, "
                    "tranche_number, tranche_kind, allocated_calls, consumed_calls, "
                    "progress_evidence_json, progress_digest, allocated_sequence) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)",
                    (
                        task.run_id,
                        task.task_id,
                        tranche_id,
                        task.attempt_id,
                        tranche_number,
                        "BOOTSTRAP" if reason == "BOOTSTRAP" else "RENEWAL",
                        calls,
                        progress_json,
                        sha256_digest(progress_json),
                        expected_sequence + 1,
                    ),
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
            counters_before=expected,
            counters_after=after,
            resulting_sequence=resulting_sequence,
        )

    def last_runtime_audit_event(
        self, run_id: RunId, owner_generation: int
    ) -> RuntimeAuditStamp | None:
        with self._read_transaction() as connection:
            row = connection.execute(
                "SELECT sequence, runtime_owner_generation, runtime_monotonic_nanoseconds "
                "FROM audit_events WHERE run_id = ? AND runtime_owner_generation = ? "
                "ORDER BY sequence DESC LIMIT 1",
                (run_id, owner_generation),
            ).fetchone()
        if row is None or row["runtime_monotonic_nanoseconds"] is None:
            return None
        return RuntimeAuditStamp(
            sequence=AuditSequence(row["sequence"]),
            owner_generation=int(row["runtime_owner_generation"]),
            monotonic_instant=MonotonicInstant(row["runtime_monotonic_nanoseconds"]),
        )

    def active_run_time_state(self, run_id: RunId) -> ActiveRunTimeState:
        with self._read_transaction() as connection:
            row = connection.execute(
                "SELECT active_runtime_nanoseconds, runtime_interval_owner_generation, "
                "runtime_interval_opened_nanoseconds FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            return ActiveRunTimeState(run_id, 0, None, None, None)
        generation = row["runtime_interval_owner_generation"]
        stamp = (
            None if generation is None else self.last_runtime_audit_event(run_id, int(generation))
        )
        return ActiveRunTimeState(
            run_id=run_id,
            cumulative_nanoseconds=int(row["active_runtime_nanoseconds"]),
            open_owner_generation=None if generation is None else int(generation),
            opened_at=(
                None
                if row["runtime_interval_opened_nanoseconds"] is None
                else MonotonicInstant(int(row["runtime_interval_opened_nanoseconds"]))
            ),
            latest_committed_at=None if stamp is None else stamp.monotonic_instant,
        )

    @staticmethod
    def _active_run_time_state_for_update(
        connection: sqlite3.Connection,
        run_id: RunId,
    ) -> ActiveRunTimeState:
        row = connection.execute(
            "SELECT active_runtime_nanoseconds, runtime_interval_owner_generation, "
            "runtime_interval_opened_nanoseconds FROM runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            raise StateConflict("RUN_NOT_FOUND")
        generation = row["runtime_interval_owner_generation"]
        stamp = None
        if generation is not None:
            stamp = connection.execute(
                "SELECT runtime_monotonic_nanoseconds FROM audit_events WHERE run_id = ? "
                "AND runtime_owner_generation = ? ORDER BY sequence DESC LIMIT 1",
                (run_id, generation),
            ).fetchone()
        return ActiveRunTimeState(
            run_id=run_id,
            cumulative_nanoseconds=int(row["active_runtime_nanoseconds"]),
            open_owner_generation=None if generation is None else int(generation),
            opened_at=(
                None
                if row["runtime_interval_opened_nanoseconds"] is None
                else MonotonicInstant(int(row["runtime_interval_opened_nanoseconds"]))
            ),
            latest_committed_at=(
                None
                if stamp is None or stamp["runtime_monotonic_nanoseconds"] is None
                else MonotonicInstant(int(stamp["runtime_monotonic_nanoseconds"]))
            ),
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
        with self._transaction("IMMEDIATE") as connection:
            self._require_expected_sequence(connection, run_id, expected_sequence)
            budget_row = connection.execute(
                "SELECT budget_digest, budget_json FROM approved_budgets_for_test WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if budget_row is None or budget_row["budget_digest"] != budget_digest:
                raise StateConflict("CURRENT_BUDGET_BINDING_MISMATCH")
            budget = BudgetRevisionDocument.model_validate_json(str(budget_row["budget_json"]))
            if ceiling_nanoseconds != budget.active_run_seconds_ceiling * 1_000_000_000:
                raise StateConflict("ACTIVE_RUN_TIME_CEILING_BINDING_MISMATCH")
            current = self._active_run_time_state_for_update(connection, run_id)
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
                Decimal(budget.active_run_seconds_ceiling)
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
            _, stopped = self._settle_global_usage_in_transaction(
                connection,
                run_id,
                budget_digest,
                GlobalBudgetMetric.ACTIVE_RUN_SECONDS,
                observed_seconds,
            )
            sequence = self._append_audit_event(
                connection,
                run_id,
                AuditEvent.kind(
                    "ACTIVE_RUN_TIME_CEILING_REACHED"
                    if observed >= ceiling_nanoseconds
                    else "GLOBAL_BUDGET_USAGE_SETTLED"
                ),
                expected_sequence,
                runtime_now=now,
            )
            if stopped:
                sequence = self._append_audit_event(
                    connection,
                    run_id,
                    AuditEvent.kind("BUDGET_STOP_REQUESTED"),
                    sequence,
                    runtime_now=now,
                )
            return ActiveRunTimeBoundaryDecision(
                "PAUSE" if observed >= ceiling_nanoseconds else "CONTINUE",
                observed,
                ceiling_nanoseconds,
                sequence,
            )

    def new_dispatch_open(self, run_id: RunId) -> bool:
        with self._read_transaction() as connection:
            row = connection.execute(
                "SELECT new_dispatch_open, dispatch_close_causes_json FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            raise StateConflict("RUN_NOT_FOUND")
        causes = dispatch_close_causes_from_json(str(row["dispatch_close_causes_json"]))
        is_open = bool(row["new_dispatch_open"])
        if is_open != (not causes):
            raise StateConflict("DISPATCH_CLOSURE_BINDING_INVALID")
        return is_open

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

    def _write_model_counters(
        self, connection: sqlite3.Connection, run_id: RunId, counters: ModelCounters
    ) -> None:
        connection.execute(
            "INSERT INTO model_counters(run_id, calls, input_tokens, output_tokens, "
            "cost_usd) VALUES (?, ?, ?, ?, ?) ON CONFLICT(run_id) DO UPDATE SET "
            "calls = excluded.calls, input_tokens = excluded.input_tokens, "
            "output_tokens = excluded.output_tokens, cost_usd = excluded.cost_usd",
            (
                run_id,
                counters.calls,
                counters.input_tokens,
                counters.output_tokens,
                str(counters.cost_usd),
            ),
        )

    def _model_counters(self, connection: sqlite3.Connection, run_id: RunId) -> ModelCounters:
        row = connection.execute(
            "SELECT calls, input_tokens, output_tokens, cost_usd "
            "FROM model_counters WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            return ModelCounters()
        return ModelCounters(row[0], row[1], row[2], Decimal(row[3]))

    def _reserve_model_counters(
        self,
        connection: sqlite3.Connection,
        run_id: RunId,
        reserved: ModelBudgetAmounts,
    ) -> None:
        self._write_model_counters(
            connection, run_id, self._model_counters(connection, run_id).reserve(reserved)
        )

    def model_counters(self, run_id: RunId) -> ModelCounters:
        return self._model_counters(self._connection, run_id)

    def begin_model_turn_and_reserve(
        self, request: ModelRequest, expected_sequence: AuditSequence
    ) -> tuple[LogicalModelTurn, ModelRequestIntent]:
        turn = LogicalModelTurn.new(request)
        intent = ModelRequestIntent.reserve(turn, request, provider_attempt_number=1)

        def mutate(connection: sqlite3.Connection) -> None:
            self._reserve_model_counters(connection, request.run_id, intent.reserved_amounts)
            connection.execute(
                "INSERT INTO model_turns(logical_turn_id, run_id, request_digest, "
                "created_sequence, state) VALUES (?, ?, ?, ?, 'OPEN')",
                (
                    turn.logical_turn_id,
                    request.run_id,
                    request.request_digest,
                    expected_sequence + 1,
                ),
            )
            connection.execute(
                "INSERT INTO model_attempts(intent_id, run_id, logical_turn_id, "
                "provider_attempt_number, request_json, request_digest, idempotency_key, "
                "reserved_json, allowed_model_ids_json, reserved_sequence, state) "
                "VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?, 'RESERVED')",
                (
                    intent.intent_id,
                    request.run_id,
                    turn.logical_turn_id,
                    model_request_to_json(request),
                    request.request_digest,
                    request.idempotency_key,
                    intent.reserved_amounts.to_json(),
                    json.dumps(sorted(request.allowed_model_ids), separators=(",", ":")),
                    expected_sequence + 1,
                ),
            )

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

        def mutate(connection: sqlite3.Connection) -> None:
            bound = connection.execute(
                "SELECT 1 FROM model_turns WHERE run_id = ? AND logical_turn_id = ? "
                "AND request_digest = ? AND state = 'OPEN'",
                (request.run_id, turn.logical_turn_id, request.request_digest),
            ).fetchone()
            if bound is None:
                raise StateConflict("MODEL_TURN_BINDING_MISMATCH")
            self._reserve_model_counters(connection, request.run_id, intent.reserved_amounts)
            connection.execute(
                "INSERT INTO model_attempts(intent_id, run_id, logical_turn_id, "
                "provider_attempt_number, request_json, request_digest, idempotency_key, "
                "reserved_json, allowed_model_ids_json, reserved_sequence, state) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'RESERVED')",
                (
                    intent.intent_id,
                    request.run_id,
                    turn.logical_turn_id,
                    provider_attempt_number,
                    model_request_to_json(request),
                    request.request_digest,
                    request.idempotency_key,
                    intent.reserved_amounts.to_json(),
                    json.dumps(sorted(request.allowed_model_ids), separators=(",", ":")),
                    expected_sequence + 1,
                ),
            )

        self._commit_state_and_event(
            run_id=request.run_id,
            expected_sequence=expected_sequence,
            event=AuditEvent.kind("MODEL_RETRY_ATTEMPT_RESERVED"),
            mutate=mutate,
        )
        return intent

    def reserve_model_request(
        self, request: ModelRequest, expected_sequence: AuditSequence
    ) -> ModelRequestIntent:
        _, intent = self.begin_model_turn_and_reserve(request, expected_sequence)
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
                provider_attempt_number=int(row["provider_attempt_number"]),
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
        if allowed_model_ids != intent.request.allowed_model_ids:
            raise StateConflict("MODEL_INTENT_BINDING_MISMATCH")
        return self.settle_model_attempt(
            intent,
            ProviderAttemptResult.completed(completion),
            expected_sequence,
        ).dispatch_result

    def _settle_model_counters(
        self,
        connection: sqlite3.Connection,
        run_id: RunId,
        reserved: ModelBudgetAmounts,
        charged: ModelBudgetAmounts,
    ) -> None:
        self._write_model_counters(
            connection,
            run_id,
            self._model_counters(connection, run_id).settle(reserved, charged),
        )

    def settle_model_attempt(
        self,
        intent: ModelRequestIntent,
        result: ProviderAttemptResult,
        expected_sequence: AuditSequence,
    ) -> SettledModelAttempt:
        settled = SettledModelAttempt.from_result(intent, result)
        dispatch_json = json.dumps(
            {
                "run_id": settled.run_id,
                "charged_amounts": json.loads(settled.charged_amounts.to_json()),
                "normalized_action": settled.dispatch_result.normalized_action,
                "normalized_payload_digest": (settled.dispatch_result.normalized_payload_digest),
                "outcome": settled.dispatch_result.outcome,
                "returned_model_id": settled.dispatch_result.returned_model_id,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        reported_usage_json = (
            None
            if result.usage is None
            else json.dumps(
                {
                    "cost_usd": str(result.usage.cost_usd),
                    "input_tokens": result.usage.input_tokens,
                    "output_tokens": result.usage.output_tokens,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
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
                raise StateConflict("MODEL_ATTEMPT_BINDING_MISMATCH")
            if row["state"] != "RESERVED":
                raise StateConflict("MODEL_ATTEMPT_ALREADY_SETTLED")
            stored = self._model_request_from_row(intent.run_id, intent.intent_id, row)
            if stored != intent:
                raise StateConflict("MODEL_ATTEMPT_BINDING_MISMATCH")
            changed = connection.execute(
                "UPDATE model_attempts SET state = 'CLOSED', outcome = ?, "
                "provider_response_id = ?, reason_code = ?, reported_usage_json = ?, "
                "returned_model_id = ?, result_digest = ?, charged_json = ?, "
                "result_json = ?, settled_sequence = ? WHERE run_id = ? AND intent_id = ? "
                "AND state = 'RESERVED'",
                (
                    settled.kind,
                    settled.provider_response_id,
                    settled.reason_code,
                    reported_usage_json,
                    settled.dispatch_result.returned_model_id,
                    settled.result_digest,
                    settled.charged_amounts.to_json(),
                    dispatch_json,
                    expected_sequence + 1,
                    intent.run_id,
                    intent.intent_id,
                ),
            ).rowcount
            if changed != 1:
                raise StateConflict("MODEL_ATTEMPT_ALREADY_SETTLED")
            self._settle_model_counters(
                connection,
                intent.run_id,
                intent.reserved_amounts,
                settled.charged_amounts,
            )
            if (
                connection.execute(
                    "SELECT 1 FROM runs WHERE run_id = ?",
                    (intent.run_id,),
                ).fetchone()
                is not None
            ):
                counters = self._model_counters(connection, intent.run_id)
                for metric, amount in (
                    (GlobalBudgetMetric.MODEL_CALLS, counters.calls),
                    (GlobalBudgetMetric.INPUT_TOKENS, counters.input_tokens),
                    (GlobalBudgetMetric.OUTPUT_TOKENS, counters.output_tokens),
                    (GlobalBudgetMetric.COST_RESERVE_USD, counters.cost_usd),
                ):
                    self._settle_global_usage_in_transaction(
                        connection,
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
                    committed = connection.execute(
                        "UPDATE model_turns SET owner_kind = ?, task_id = ?, attempt_id = ?, "
                        "tranche_id = ?, recovery_binding_json = ?, returned_model_id = ?, "
                        "normalized_output_digest = ?, normalized_payload_json = ?, "
                        "dispatch_result_json = ?, committed_sequence = ?, "
                        "state = 'COMPLETION_COMMITTED' WHERE run_id = ? "
                        "AND logical_turn_id = ? AND state = 'OPEN'",
                        (
                            intent.request.owner_kind,
                            intent.request.task_id,
                            intent.request.attempt_id,
                            intent.request.tranche_id,
                            model_recovery_binding_to_json(
                                ModelRecoveryBinding.from_request(intent.request)
                            ),
                            dispatch.returned_model_id,
                            dispatch.normalized_payload_digest,
                            json.dumps(
                                dispatch.normalized_action,
                                sort_keys=True,
                                separators=(",", ":"),
                            ),
                            model_dispatch_result_to_json(dispatch),
                            expected_sequence + 1,
                            intent.run_id,
                            intent.logical_turn_id,
                        ),
                    ).rowcount
                    if committed != 1:
                        raise StateConflict("MODEL_TURN_BINDING_MISMATCH")

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

        def mutate(connection: sqlite3.Connection) -> None:
            self._insert_effect_intent(connection, intent)
            changed = connection.execute(
                "UPDATE model_turns SET downstream_intent_id = ?, "
                "downstream_sequence = ?, state = 'DOWNSTREAM_INTENT_RECORDED' "
                "WHERE run_id = ? AND logical_turn_id = ? "
                "AND state = 'COMPLETION_COMMITTED' AND downstream_intent_id IS NULL",
                (
                    intent.intent_id,
                    expected_sequence + 1,
                    run_id,
                    logical_turn_id,
                ),
            ).rowcount
            if changed != 1:
                raise StateConflict("DOWNSTREAM_INTENT_ALREADY_RECORDED")

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
        def mutate(connection: sqlite3.Connection) -> None:
            changed = connection.execute(
                "UPDATE model_attempts SET backoff_seconds = ?, backoff_sequence = ? "
                "WHERE run_id = ? AND intent_id = ? AND state = 'CLOSED' "
                "AND outcome = 'KNOWN_CLOSED_REJECTION' AND backoff_seconds IS NULL",
                (seconds, expected_sequence + 1, run_id, intent_id),
            ).rowcount
            if changed != 1:
                raise StateConflict("BACKOFF_REQUIRES_CLOSED_REJECTION")

        return self._commit_state_and_event(
            run_id=run_id,
            expected_sequence=expected_sequence,
            event=AuditEvent.kind("MODEL_RETRY_BACKOFF_RECORDED"),
            mutate=mutate,
        )

    def model_attempts(
        self, run_id: RunId, logical_turn_id: LogicalTurnId
    ) -> tuple[SettledModelAttempt, ...]:
        rows = self._connection.execute(
            "SELECT run_id, intent_id, provider_attempt_number, request_json, "
            "reserved_json, outcome, provider_response_id, reason_code, result_digest, "
            "charged_json, result_json, backoff_seconds FROM model_attempts "
            "WHERE run_id = ? AND logical_turn_id = ? AND state = 'CLOSED' "
            "ORDER BY provider_attempt_number",
            (run_id, logical_turn_id),
        ).fetchall()
        attempts: list[SettledModelAttempt] = []
        for row in rows:
            dispatch_data = json.loads(row[10])
            charged = ModelBudgetAmounts.from_json(row[9])
            dispatch = ModelDispatchResult(
                run_id=RunId(dispatch_data["run_id"]),
                logical_turn_id=logical_turn_id,
                outcome=dispatch_data["outcome"],
                returned_model_id=dispatch_data["returned_model_id"],
                normalized_action=dispatch_data["normalized_action"],
                normalized_payload_digest=dispatch_data["normalized_payload_digest"],
                charged_amounts=charged,
            )
            attempts.append(
                SettledModelAttempt(
                    run_id=RunId(row[0]),
                    intent_id=IntentId(row[1]),
                    logical_turn_id=logical_turn_id,
                    provider_attempt_number=row[2],
                    request=model_request_from_json(row[3]),
                    reserved_amounts=ModelBudgetAmounts.from_json(row[4]),
                    kind=ProviderAttemptKind(row[5]),
                    provider_response_id=row[6],
                    reason_code=row[7],
                    charged_amounts=charged,
                    result_digest=row[8],
                    dispatch_result=dispatch,
                    backoff_seconds=row[11],
                )
            )
        return tuple(attempts)

    def committed_model_turn(
        self, run_id: RunId, logical_turn_id: LogicalTurnId
    ) -> CommittedModelTurn | None:
        row = self._connection.execute(
            "SELECT logical_turn_id, owner_kind, task_id, attempt_id, tranche_id, "
            "recovery_binding_json, returned_model_id, normalized_output_digest, "
            "normalized_payload_json, dispatch_result_json, committed_sequence, state, "
            "downstream_intent_id, downstream_sequence FROM model_turns "
            "WHERE run_id = ? AND logical_turn_id = ?",
            (run_id, logical_turn_id),
        ).fetchone()
        if row is None or row[11] not in {
            "COMPLETION_COMMITTED",
            "DOWNSTREAM_INTENT_RECORDED",
        }:
            return None
        if any(row[index] is None for index in (1, 5, 6, 7, 8, 9, 10)):
            raise StateConflict("COMMITTED_MODEL_TURN_INCOMPLETE")
        if row[1] == "PLANNING" and any(row[index] is not None for index in (2, 3, 4)):
            raise StateConflict("COMMITTED_MODEL_OWNER_BINDING_MISMATCH")
        if row[1] == "WORKER" and any(row[index] is None for index in (2, 3, 4)):
            raise StateConflict("COMMITTED_MODEL_OWNER_BINDING_MISMATCH")
        dispatch = model_dispatch_result_from_json(row[9])
        payload = json.loads(row[8])
        if (
            dispatch.run_id != run_id
            or dispatch.logical_turn_id != row[0]
            or dispatch.returned_model_id != row[6]
            or dispatch.normalized_payload_digest != row[7]
            or dispatch.normalized_action != payload
        ):
            raise StateConflict("COMMITTED_MODEL_TURN_BINDING_MISMATCH")
        return CommittedModelTurn(
            run_id=run_id,
            logical_turn_id=row[0],
            owner_kind=row[1],
            task_id=None if row[2] is None else TaskId(row[2]),
            attempt_id=None if row[3] is None else AttemptId(row[3]),
            tranche_id=row[4],
            recovery_binding=model_recovery_binding_from_json(row[5]),
            returned_model_id=row[6],
            normalized_output_digest=row[7],
            normalized_payload=payload,
            dispatch_result=dispatch,
            committed_sequence=AuditSequence(row[10]),
            state=row[11],
            downstream_intent_id=(None if row[12] is None else IntentId(row[12])),
            downstream_sequence=(None if row[13] is None else AuditSequence(row[13])),
        )

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
