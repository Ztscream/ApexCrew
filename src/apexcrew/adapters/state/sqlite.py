from __future__ import annotations

import json
import sqlite3
from base64 import b32encode
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
from hmac import compare_digest
from pathlib import Path
from threading import RLock
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from apexcrew.application.runtime import RuntimeFault, RuntimeFaultDisposition

from apexcrew.application.control import (
    RepositoryBootstrapAuthorityService,
    TargetAuthorityDigestService,
)
from apexcrew.domain.admission import (
    PrivateRefCasOutcome,
    RefCasIntent,
    RuntimeStartBinding,
    StartGuard,
    StartGuardBinding,
    TargetReservationCreationIntent,
    TargetReservationCreationOutcome,
    TargetReservationIdAllocationError,
    allocate_target_reservation_id,
    random_target_reservation_id,
)
from apexcrew.domain.authority import (
    ActionClass,
    ActionDeadline,
    ActiveRunTimeBoundaryDecision,
    ActiveRunTimeState,
    AtomicAction,
    AttemptLifecycleState,
    AuthorityDenied,
    AuthorizationReason,
    AuthorizationRequest,
    BudgetCeilingExhaustion,
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
    TaskResumeDecision,
    TaskStopDecision,
    TimeoutDecision,
    TrancheDecision,
    TrancheReason,
    WorkspaceLease,
    action_deadline_binding,
    budget_ceiling_exhaustions_from_json,
    budget_ceiling_exhaustions_to_json,
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
    task_resume_ids,
    timeout_decision_from_json,
    timeout_decision_to_json,
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
    applicable_revision_digests_from_json,
    applicable_revision_digests_to_json,
)
from apexcrew.domain.coordination import (
    PlanningAuthorization,
    PlanningReadIntent,
    PlanningReadResult,
    PlanningReadSettlement,
    PlanProposal,
    check_definition_from_json,
    check_definition_json,
    plan_proposal_from_document,
    plan_proposal_record_from_json,
    plan_proposal_record_json,
    run_check_set_digest,
    task_contract_digest,
    task_contract_from_json,
    task_contract_json,
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
    ModelUsage,
    ProviderAttemptKind,
    ProviderAttemptResult,
    RecoveredModelAction,
    SettledModelAttempt,
    model_dispatch_result_from_json,
    model_dispatch_result_to_json,
    model_recovery_binding_from_json,
    model_recovery_binding_to_json,
    model_request_from_json,
    model_request_to_json,
)
from apexcrew.domain.plan import (
    CheckDefinition,
    GlobPattern,
    PlanRevision,
    TaskContract,
    may_overlap,
)
from apexcrew.domain.revisions import (
    BudgetRevisionDocument,
    FrozenDocument,
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
    RequestId,
    RevisionDigest,
    RunId,
    RunState,
    RuntimeOwnerId,
    TaskId,
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
    (
        9,
        (
            """CREATE TABLE action_deadlines (
                intent_id TEXT PRIMARY KEY REFERENCES effect_intents(intent_id),
                run_id TEXT NOT NULL REFERENCES runs(run_id),
                budget_digest TEXT NOT NULL,
                applicable_revision_digests_json TEXT NOT NULL,
                action_class TEXT NOT NULL CHECK(action_class IN ('ORDINARY','DECLARED_CHECK')),
                started_at_utc TEXT NOT NULL,
                expires_at_utc TEXT NOT NULL,
                check_id TEXT,
                snapshot_digest TEXT,
                recorded_sequence INTEGER NOT NULL
            )""",
            """CREATE TABLE action_timeout_decisions (
                intent_id TEXT PRIMARY KEY REFERENCES action_deadlines(intent_id),
                decision_json TEXT NOT NULL,
                settled_sequence INTEGER NOT NULL
            )""",
        ),
    ),
    (
        10,
        (
            """CREATE TABLE task_pauses (
                run_id TEXT NOT NULL REFERENCES runs(run_id),
                task_id TEXT NOT NULL,
                pause_sequence INTEGER NOT NULL,
                pause_reason TEXT NOT NULL,
                counter_snapshot_digest TEXT NOT NULL,
                previous_attempt_id TEXT NOT NULL,
                budget_digest_at_pause TEXT NOT NULL,
                budget_ceiling_exhaustions_json TEXT NOT NULL,
                applicable_revision_digests_json TEXT NOT NULL,
                active INTEGER NOT NULL CHECK(active IN (0, 1)),
                PRIMARY KEY(run_id, task_id),
                FOREIGN KEY(run_id, task_id) REFERENCES tasks(run_id, task_id)
            )""",
            """CREATE TABLE task_resume_allocations (
                allocation_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES runs(run_id),
                task_id TEXT NOT NULL,
                reserved_attempt_id TEXT NOT NULL,
                budget_digest TEXT NOT NULL,
                applicable_revision_digests_json TEXT NOT NULL,
                allocated_calls INTEGER NOT NULL CHECK(allocated_calls BETWEEN 1 AND 8),
                state TEXT NOT NULL CHECK(state IN ('RESERVED','CONSUMED','INVALIDATED')),
                created_sequence INTEGER NOT NULL,
                UNIQUE(run_id, task_id, reserved_attempt_id),
                FOREIGN KEY(run_id, task_id) REFERENCES tasks(run_id, task_id)
            )""",
            """CREATE TABLE task_resume_metadata (
                run_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                next_lease_generation INTEGER NOT NULL CHECK(next_lease_generation >= 1),
                failure_digests_json TEXT NOT NULL,
                warning_keys_json TEXT NOT NULL,
                PRIMARY KEY(run_id, task_id),
                FOREIGN KEY(run_id, task_id) REFERENCES tasks(run_id, task_id)
            )""",
            """CREATE TABLE trusted_task_repairs (
                run_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                pause_sequence INTEGER NOT NULL,
                pause_reason TEXT NOT NULL,
                observation_digest TEXT NOT NULL,
                PRIMARY KEY(run_id, task_id, pause_sequence, pause_reason),
                FOREIGN KEY(run_id, task_id) REFERENCES tasks(run_id, task_id)
            )""",
        ),
    ),
    (
        11,
        (
            """CREATE TABLE run_bootstrap_inputs (
                run_id TEXT PRIMARY KEY REFERENCES runs(run_id),
                goal_json TEXT NOT NULL,
                constraints_json TEXT NOT NULL,
                acceptance_json TEXT NOT NULL
            )""",
            """CREATE TABLE revision_documents (
                run_id TEXT NOT NULL REFERENCES runs(run_id),
                revision_class TEXT NOT NULL CHECK(revision_class IN (
                    'PLAN','POLICY','BUDGET','MODEL_CONFIGURATION'
                )),
                revision_digest TEXT NOT NULL,
                document_json TEXT NOT NULL,
                proposed_sequence INTEGER NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('PROPOSED','CURRENT','STALE')),
                PRIMARY KEY(run_id, revision_class, revision_digest)
            )""",
            """CREATE TABLE revision_approvals (
                run_id TEXT NOT NULL,
                revision_class TEXT NOT NULL,
                revision_digest TEXT NOT NULL,
                approval_request_id TEXT NOT NULL UNIQUE,
                approval_sequence INTEGER NOT NULL,
                display_digest TEXT NOT NULL,
                PRIMARY KEY(run_id, revision_class, revision_digest),
                FOREIGN KEY(run_id, revision_class, revision_digest)
                    REFERENCES revision_documents(run_id, revision_class, revision_digest)
            )""",
            """CREATE TABLE pending_revision_replacements (
                run_id TEXT NOT NULL REFERENCES runs(run_id),
                revision_class TEXT NOT NULL CHECK(revision_class IN (
                    'BUDGET','MODEL_CONFIGURATION'
                )),
                revision_digest TEXT NOT NULL,
                requested_sequence INTEGER NOT NULL,
                PRIMARY KEY(run_id, revision_class)
            )""",
            """CREATE TABLE runtime_permits (
                run_id TEXT NOT NULL REFERENCES runs(run_id),
                generation INTEGER NOT NULL,
                source_request_id TEXT NOT NULL,
                source_envelope_digest TEXT NOT NULL,
                issued_sequence INTEGER NOT NULL,
                allowed_phase TEXT NOT NULL,
                applicable_revision_digests_json TEXT NOT NULL,
                target_authority_digest TEXT NOT NULL,
                expected_runtime_progress_generation INTEGER NOT NULL,
                state TEXT NOT NULL CHECK(state IN (
                    'UNCONSUMED','CONSUMED','INVALIDATED'
                )),
                consumed_owner_id TEXT,
                consumed_sequence INTEGER,
                PRIMARY KEY(run_id, generation),
                UNIQUE(source_request_id, source_envelope_digest)
            )""",
            """CREATE UNIQUE INDEX one_unconsumed_runtime_permit
                ON runtime_permits(run_id) WHERE state = 'UNCONSUMED'""",
        ),
    ),
    (
        12,
        (
            """CREATE TABLE runtime_interrupts (
                run_id TEXT PRIMARY KEY REFERENCES runs(run_id),
                request_id TEXT NOT NULL,
                kind TEXT NOT NULL CHECK(kind IN ('PAUSE','CANCEL')),
                requested_sequence INTEGER NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('PENDING','APPLIED','SUPERSEDED'))
            )""",
            """CREATE TABLE runtime_barriers (
                run_id TEXT PRIMARY KEY REFERENCES runs(run_id),
                action_id TEXT,
                effect_intent_id TEXT,
                state TEXT NOT NULL CHECK(state IN ('IDLE','IN_FLIGHT','SETTLED','INDETERMINATE')),
                pending_stop_reason TEXT,
                pending_budget_digest TEXT,
                pending_model_configuration_digest TEXT
            )""",
            """CREATE TABLE runtime_delivery_stops (
                run_id TEXT NOT NULL REFERENCES runs(run_id),
                permit_generation INTEGER NOT NULL,
                owner_generation INTEGER NOT NULL,
                reason TEXT NOT NULL,
                stop_json TEXT NOT NULL,
                interval_opened_nanoseconds INTEGER NOT NULL,
                interval_closed_nanoseconds INTEGER NOT NULL,
                interval_delta_nanoseconds INTEGER NOT NULL CHECK(interval_delta_nanoseconds >= 0),
                cumulative_nanoseconds INTEGER NOT NULL CHECK(cumulative_nanoseconds >= 0),
                recorded_sequence INTEGER NOT NULL,
                PRIMARY KEY(run_id, permit_generation, recorded_sequence)
            )""",
            """CREATE TABLE runtime_faults (
                run_id TEXT NOT NULL REFERENCES runs(run_id),
                permit_generation INTEGER NOT NULL,
                phase TEXT NOT NULL,
                fault_code TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                resulting_sequence INTEGER NOT NULL,
                barrier_state TEXT NOT NULL CHECK(barrier_state IN ('IDLE','SETTLED','INDETERMINATE')),
                PRIMARY KEY(run_id, permit_generation, resulting_sequence)
            )""",
        ),
    ),
    (
        13,
        (
            """CREATE TABLE plans (
                run_id TEXT NOT NULL REFERENCES runs(run_id),
                plan_digest TEXT NOT NULL,
                base_run_head_oid TEXT NOT NULL,
                policy_digest TEXT NOT NULL,
                budget_digest TEXT NOT NULL,
                model_configuration_digest TEXT NOT NULL,
                run_check_set_digest TEXT NOT NULL,
                planning_request_count INTEGER NOT NULL
                    CHECK(planning_request_count BETWEEN 1 AND 8),
                state TEXT NOT NULL CHECK(state IN ('PROPOSED','APPROVED','STALE')),
                proposal_json TEXT NOT NULL,
                PRIMARY KEY(run_id, plan_digest),
                UNIQUE(plan_digest)
            )""",
            """CREATE TABLE task_contracts (
                plan_digest TEXT NOT NULL,
                task_id TEXT NOT NULL,
                task_revision INTEGER NOT NULL CHECK(task_revision = 1),
                contract_digest TEXT NOT NULL,
                contract_json TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN (
                    'BLOCKED','READY','ACTIVE','CANDIDATE_READY','PROMOTED',
                    'PAUSED','FAILED','CANCELLED'
                )),
                PRIMARY KEY(plan_digest, task_id),
                UNIQUE(plan_digest, contract_digest),
                FOREIGN KEY(plan_digest) REFERENCES plans(plan_digest)
            )""",
            """CREATE TABLE task_dependencies (
                plan_digest TEXT NOT NULL,
                predecessor_task_id TEXT NOT NULL,
                successor_task_id TEXT NOT NULL,
                PRIMARY KEY(plan_digest, predecessor_task_id, successor_task_id),
                FOREIGN KEY(plan_digest, predecessor_task_id)
                    REFERENCES task_contracts(plan_digest, task_id),
                FOREIGN KEY(plan_digest, successor_task_id)
                    REFERENCES task_contracts(plan_digest, task_id)
            )""",
            """CREATE TABLE hazard_edges (
                plan_digest TEXT NOT NULL,
                predecessor_task_id TEXT NOT NULL,
                successor_task_id TEXT NOT NULL,
                hazard_class TEXT NOT NULL CHECK(hazard_class = 'PROMOTION'),
                PRIMARY KEY(
                    plan_digest, predecessor_task_id, successor_task_id, hazard_class
                ),
                FOREIGN KEY(plan_digest, predecessor_task_id)
                    REFERENCES task_contracts(plan_digest, task_id),
                FOREIGN KEY(plan_digest, successor_task_id)
                    REFERENCES task_contracts(plan_digest, task_id)
            )""",
            """CREATE TABLE run_checks (
                plan_digest TEXT NOT NULL,
                ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
                check_digest TEXT NOT NULL,
                check_json TEXT NOT NULL,
                PRIMARY KEY(plan_digest, ordinal),
                UNIQUE(plan_digest, check_digest),
                FOREIGN KEY(plan_digest) REFERENCES plans(plan_digest)
            )""",
        ),
    ),
    (
        14,
        ("ALTER TABLE runs ADD COLUMN planning_returned_bytes INTEGER NOT NULL DEFAULT 0",),
    ),
    (
        15,
        (
            """CREATE TABLE plan_approvals (
                run_id TEXT PRIMARY KEY REFERENCES runs(run_id),
                plan_digest TEXT NOT NULL,
                approval_request_id TEXT NOT NULL UNIQUE,
                approval_sequence INTEGER NOT NULL,
                binding_digest TEXT NOT NULL
            )""",
            """CREATE TABLE run_refs (
                run_id TEXT NOT NULL REFERENCES runs(run_id),
                ref_kind TEXT NOT NULL CHECK(ref_kind IN ('PRIVATE','TARGET')),
                ref_name TEXT NOT NULL,
                expected_old_oid TEXT,
                current_oid TEXT,
                state TEXT NOT NULL CHECK(state IN (
                    'ABSENT_EXPECTED','INIT_INTENT_RECORDED','PRESENT','CONFLICT'
                )),
                last_intent_id TEXT REFERENCES effect_intents(intent_id),
                guard_binding_json TEXT,
                PRIMARY KEY(run_id, ref_kind)
            )""",
        ),
    ),
    (
        16,
        (
            "ALTER TABLE model_attempts ADD COLUMN response_requested_model_id TEXT",
            "ALTER TABLE model_turns ADD COLUMN response_requested_model_id TEXT",
        ),
    ),
    (
        17,
        (
            (
                "ALTER TABLE model_attempts ADD COLUMN response_requested_model_binding TEXT "
                "NOT NULL DEFAULT 'LEGACY' "
                "CHECK(response_requested_model_binding IN ('LEGACY', 'BOUND'))"
            ),
            (
                "ALTER TABLE model_turns ADD COLUMN response_requested_model_binding TEXT "
                "NOT NULL DEFAULT 'LEGACY' "
                "CHECK(response_requested_model_binding IN ('LEGACY', 'BOUND'))"
            ),
            (
                "UPDATE model_attempts SET response_requested_model_binding = 'BOUND' "
                "WHERE response_requested_model_id IS NOT NULL "
                "OR json_type(result_json, '$.response_requested_model_id') IS NOT NULL"
            ),
            (
                "UPDATE model_turns SET response_requested_model_binding = 'BOUND' "
                "WHERE response_requested_model_id IS NOT NULL "
                "OR json_type(dispatch_result_json, '$.response_requested_model_id') IS NOT NULL"
            ),
        ),
    ),
    (
        18,
        (
            "ALTER TABLE model_attempts ADD COLUMN request_requested_model_id TEXT",
            (
                "UPDATE model_attempts SET request_requested_model_id = "
                "json_extract(request_json, '$.requested_model_id')"
            ),
        ),
    ),
    (
        19,
        (
            """CREATE TABLE bootstrap_command_receipts (
                request_id TEXT PRIMARY KEY,
                envelope_digest TEXT NOT NULL,
                outcome_json TEXT NOT NULL
            )""",
        ),
    ),
    (
        20,
        (
            """CREATE TABLE control_request_claims (
                request_id TEXT PRIMARY KEY,
                envelope_digest TEXT NOT NULL,
                outcome_json TEXT NOT NULL
            )""",
            """INSERT OR IGNORE INTO control_request_claims(
                request_id, envelope_digest, outcome_json
            ) SELECT request_id, envelope_digest, outcome_json FROM command_receipts""",
            """INSERT OR IGNORE INTO control_request_claims(
                request_id, envelope_digest, outcome_json
            ) SELECT request_id, envelope_digest, outcome_json FROM bootstrap_command_receipts""",
        ),
    ),
)


def _legacy_model_attempt_result_digest(
    *,
    kind: str,
    charged: ModelBudgetAmounts,
    provider_response_id: str | None,
    reason_code: str | None,
    normalized_payload_digest: str | None,
) -> str:
    payload = json.dumps(
        {
            "charged": {
                "calls": charged.calls,
                "cost_usd": str(charged.cost_usd),
                "input_tokens": charged.input_tokens,
                "output_tokens": charged.output_tokens,
            },
            "kind": kind,
            "normalized_payload_digest": normalized_payload_digest,
            "provider_response_id": provider_response_id,
            "reason_code": reason_code,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + sha256(payload).hexdigest()


def _bound_model_attempt_result_digest(
    *,
    kind: str,
    charged: ModelBudgetAmounts,
    provider_response_id: str | None,
    reason_code: str | None,
    normalized_payload_digest: str | None,
    response_requested_model_id: str | None,
    returned_model_id: str | None,
) -> str:
    payload = json.dumps(
        {
            "charged": {
                "calls": charged.calls,
                "cost_usd": str(charged.cost_usd),
                "input_tokens": charged.input_tokens,
                "output_tokens": charged.output_tokens,
            },
            "kind": kind,
            "normalized_payload_digest": normalized_payload_digest,
            "provider_response_id": provider_response_id,
            "reason_code": reason_code,
            "response_requested_model_id": response_requested_model_id,
            "returned_model_id": returned_model_id,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + sha256(payload).hexdigest()


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


def _revision_json(document: FrozenDocument) -> str:
    return canonical_json(document.model_dump(mode="json"))


def _json_object(value: str, error_code: str = "STORED_JSON_OBJECT_REQUIRED") -> dict[str, object]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as error:
        raise StateConflict(error_code) from error
    if not isinstance(parsed, dict):
        raise StateConflict(error_code)
    return parsed


def _stored_command_outcome_identity(value: str) -> tuple[RunId | None, AuditSequence | None]:
    try:
        outcome = CommandOutcome.model_validate(
            {
                **_json_object(value, "CONTROL_REQUEST_CLAIM_STORAGE_INVALID"),
                "result": None,
            }
        )
    except ValueError as error:
        raise StateConflict("CONTROL_REQUEST_CLAIM_STORAGE_INVALID") from error
    return outcome.run_id, outcome.resulting_sequence


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


def _string_tuple_to_json(values: tuple[str, ...]) -> str:
    return json.dumps(list(values), separators=(",", ":"))


def _string_tuple_from_json(value: str, error_code: str) -> tuple[str, ...]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as error:
        raise StateConflict(error_code) from error
    if (
        not isinstance(parsed, list)
        or any(not isinstance(item, str) or not item for item in parsed)
        or _string_tuple_to_json(tuple(parsed)) != value
    ):
        raise StateConflict(error_code)
    return tuple(parsed)


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


class _ResumeStale(RuntimeError):
    def __init__(self, decision: TaskResumeDecision) -> None:
        super().__init__(decision.failed_invariant)
        self.decision = decision


class _ControlRequestClaimed(RuntimeError):
    def __init__(self, outcome: CommandOutcome) -> None:
        super().__init__("CONTROL_REQUEST_ALREADY_CLAIMED")
        self.outcome = outcome


@dataclass(frozen=True, slots=True)
class _ReservationEvaluation:
    reason: ModelReservationReason | None
    budget: BudgetRevisionDocument
    amounts: ModelBudgetAmounts
    run_counters: ModelCounters
    task_counters: TaskBudgetState | None
    planning_requests: int


class SqliteStateStore:
    def __init__(
        self,
        database: Path,
        monotonic_clock: MonotonicClock | None = None,
        *,
        target_reservation_id_source: Callable[[], object] | None = None,
    ) -> None:
        self._database = database
        self._data_root = database.parent / "data"
        self._connection = sqlite3.connect(database, isolation_level=None, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._lock = RLock()
        self._fail_next_commit_after_state_write = False
        self._monotonic_clock = monotonic_clock
        self._target_reservation_id_source = (
            random_target_reservation_id
            if target_reservation_id_source is None
            else target_reservation_id_source
        )
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
        runtime_now_factory: Callable[[], MonotonicInstant | None] | None = None,
        finalize: Callable[[sqlite3.Connection], None] | None = None,
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
                runtime_now = None if runtime_now_factory is None else runtime_now_factory()
                sequence = expected_sequence
                for event in events:
                    sequence = self._append_audit_event(
                        connection,
                        run_id,
                        event,
                        sequence,
                        runtime_now=runtime_now,
                    )
                if finalize is not None:
                    finalize(connection)
            except BaseException:
                connection.rollback()
                raise
            else:
                connection.commit()
                return sequence

    def record_command(self, command: CommandEnvelope, outcome: CommandOutcome) -> CommandOutcome:
        with self._lock:
            run_id = _command_run_id(command, outcome)
            with self._read_transaction() as connection:
                run = connection.execute(
                    "SELECT repository_id FROM runs WHERE run_id = ?", (run_id,)
                ).fetchone()
                if run is None:
                    raise StateConflict("RUN_NOT_FOUND")
                claim = connection.execute(
                    "SELECT envelope_digest, outcome_json FROM control_request_claims "
                    "WHERE request_id = ?",
                    (command.request_id,),
                ).fetchone()
                sequence_row = connection.execute(
                    "SELECT current_sequence FROM run_sequences WHERE run_id = ?", (run_id,)
                ).fetchone()
            if claim is not None:
                if claim["envelope_digest"] == _command_digest(command):
                    return CommandOutcome.validate_for_payload(
                        command.payload, _json_object(claim["outcome_json"])
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

            def mutate(connection: sqlite3.Connection) -> None:
                self._insert_control_receipt(
                    connection,
                    command,
                    run_id,
                    RepositoryId(run["repository_id"]),
                    outcome,
                )

            try:
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
            except _ControlRequestClaimed as error:
                return error.outcome
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
    def _task_pause_from_row(row: sqlite3.Row) -> TaskPauseBinding:
        revisions_json = str(row["applicable_revision_digests_json"])
        revisions = applicable_revision_digests_from_json(revisions_json)
        if applicable_revision_digests_to_json(revisions) != revisions_json:
            raise StateConflict("TASK_PAUSE_REVISION_BINDING_INVALID")
        return TaskPauseBinding(
            run_id=RunId(row["run_id"]),
            task_id=TaskId(row["task_id"]),
            pause_sequence=AuditSequence(row["pause_sequence"]),
            pause_reason=str(row["pause_reason"]),
            counter_snapshot_digest=str(row["counter_snapshot_digest"]),
            previous_attempt_id=AttemptId(row["previous_attempt_id"]),
            budget_digest_at_pause=RevisionDigest(row["budget_digest_at_pause"]),
            applicable_revision_digests_at_pause=revisions,
            budget_ceiling_exhaustions=budget_ceiling_exhaustions_from_json(
                str(row["budget_ceiling_exhaustions_json"])
            ),
        )

    @staticmethod
    def _insert_task_pause(
        connection: sqlite3.Connection,
        pause: TaskPauseBinding,
        applicable_revision_digests: ApplicableRevisionDigests,
    ) -> None:
        if pause.applicable_revision_digests_at_pause != applicable_revision_digests:
            raise StateConflict("TASK_PAUSE_REVISION_BINDING_INVALID")
        changed = connection.execute(
            "INSERT INTO task_pauses(run_id, task_id, pause_sequence, pause_reason, "
            "counter_snapshot_digest, previous_attempt_id, budget_digest_at_pause, "
            "budget_ceiling_exhaustions_json, applicable_revision_digests_json, active) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1) "
            "ON CONFLICT(run_id, task_id) DO UPDATE SET "
            "pause_sequence = excluded.pause_sequence, pause_reason = excluded.pause_reason, "
            "counter_snapshot_digest = excluded.counter_snapshot_digest, "
            "previous_attempt_id = excluded.previous_attempt_id, "
            "budget_digest_at_pause = excluded.budget_digest_at_pause, "
            "budget_ceiling_exhaustions_json = excluded.budget_ceiling_exhaustions_json, "
            "applicable_revision_digests_json = excluded.applicable_revision_digests_json, "
            "active = 1 WHERE task_pauses.active = 0",
            (
                pause.run_id,
                pause.task_id,
                pause.pause_sequence,
                pause.pause_reason,
                pause.counter_snapshot_digest,
                pause.previous_attempt_id,
                pause.budget_digest_at_pause,
                budget_ceiling_exhaustions_to_json(pause.budget_ceiling_exhaustions),
                applicable_revision_digests_to_json(applicable_revision_digests),
            ),
        ).rowcount
        if changed != 1:
            raise StateConflict("TASK_PAUSE_ALREADY_ACTIVE")

    @classmethod
    def _read_current_task_pause(
        cls,
        connection: sqlite3.Connection,
        run_id: RunId,
        task_id: TaskId,
    ) -> TaskPauseBinding | None:
        row = connection.execute(
            "SELECT * FROM task_pauses WHERE run_id = ? AND task_id = ? AND active = 1",
            (run_id, task_id),
        ).fetchone()
        return None if row is None else cls._task_pause_from_row(row)

    def current_task_pause(self, run_id: RunId, task_id: TaskId) -> TaskPauseBinding | None:
        with self._read_transaction() as connection:
            return self._read_current_task_pause(connection, run_id, task_id)

    def _task_counters_in_transaction(
        self,
        connection: sqlite3.Connection,
        run_id: RunId,
        task_id: TaskId,
    ) -> TaskCounterSnapshot:
        task = connection.execute(
            "SELECT 1 FROM tasks WHERE run_id = ? AND task_id = ?",
            (run_id, task_id),
        ).fetchone()
        if task is None:
            raise StateConflict("TASK_NOT_FOUND")
        budget = self._task_budget_state(connection, run_id, task_id)
        checkpoints = tuple(
            CheckpointKey(str(row["tree_oid"]), str(row["check_set_digest"]))
            for row in connection.execute(
                "SELECT tree_oid, check_set_digest FROM task_checkpoints "
                "WHERE run_id = ? AND task_id = ? ORDER BY observed_sequence",
                (run_id, task_id),
            )
        )
        invalid_actions = tuple(
            str(row["action_digest"])
            for row in connection.execute(
                "SELECT action_digest FROM task_invalid_actions "
                "WHERE run_id = ? AND task_id = ? ORDER BY observed_sequence",
                (run_id, task_id),
            )
        )
        metadata = connection.execute(
            "SELECT next_lease_generation, failure_digests_json, warning_keys_json "
            "FROM task_resume_metadata WHERE run_id = ? AND task_id = ?",
            (run_id, task_id),
        ).fetchone()
        if metadata is None:
            maximum_generation = connection.execute(
                "SELECT MAX(generation) FROM workspace_leases WHERE run_id = ? AND task_id = ?",
                (run_id, task_id),
            ).fetchone()[0]
            next_lease_generation = 1 if maximum_generation is None else int(maximum_generation) + 1
            failure_digests: tuple[str, ...] = ()
            warning_keys: tuple[str, ...] = ()
        else:
            next_lease_generation = int(metadata["next_lease_generation"])
            failure_digests = _string_tuple_from_json(
                str(metadata["failure_digests_json"]),
                "TASK_FAILURE_HISTORY_INVALID",
            )
            warning_keys = _string_tuple_from_json(
                str(metadata["warning_keys_json"]),
                "TASK_WARNING_HISTORY_INVALID",
            )
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
            checkpoint_history=checkpoints,
            invalid_action_history=invalid_actions,
            warning_keys=warning_keys,
        )

    def task_counters(self, run_id: RunId, task_id: TaskId) -> TaskCounterSnapshot:
        with self._read_transaction() as connection:
            return self._task_counters_in_transaction(connection, run_id, task_id)

    def _record_task_pause_binding(
        self,
        connection: sqlite3.Connection,
        *,
        run_id: RunId,
        task_id: TaskId,
        attempt_id: AttemptId,
        pause_sequence: AuditSequence,
        pause_reason: str,
        budget_digest: RevisionDigest,
        budget_ceiling_exhaustions: tuple[BudgetCeilingExhaustion, ...] = (),
    ) -> None:
        row = connection.execute(
            "SELECT current_plan_digest, current_policy_digest, current_budget_digest, "
            "current_model_configuration_digest FROM runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            raise StateConflict("RUN_NOT_FOUND")
        revisions = ApplicableRevisionDigests(
            plan_digest=row["current_plan_digest"],
            policy_digest=row["current_policy_digest"],
            budget_digest=row["current_budget_digest"],
            model_configuration_digest=row["current_model_configuration_digest"],
        )
        counters = self._task_counters_in_transaction(connection, run_id, task_id)
        self._insert_task_pause(
            connection,
            TaskPauseBinding(
                run_id=run_id,
                task_id=task_id,
                pause_sequence=pause_sequence,
                pause_reason=pause_reason,
                counter_snapshot_digest=counters.digest,
                previous_attempt_id=attempt_id,
                budget_digest_at_pause=budget_digest,
                applicable_revision_digests_at_pause=revisions,
                budget_ceiling_exhaustions=budget_ceiling_exhaustions,
            ),
            revisions,
        )
        close_cause = (
            DispatchCloseCause.BUDGET_EXHAUSTED
            if pause_reason == "LOWERED_BUDGET_CEILING" or budget_ceiling_exhaustions
            else DispatchCloseCause.TASK_PAUSED
        )
        self._close_new_dispatch(connection, run_id, close_cause)

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
        ):
            raise StateConflict("TASK_PAUSE_COUNTER_BINDING_INVALID")
        with self._transaction("IMMEDIATE") as connection:
            connection.execute(
                "INSERT INTO tasks(run_id, task_id, state, pause_reason, pause_counter) "
                "VALUES (?, ?, 'PAUSED', ?, 1) ON CONFLICT(run_id, task_id) DO UPDATE SET "
                "state = 'PAUSED', pause_reason = excluded.pause_reason, pause_counter = 1",
                (pause.run_id, pause.task_id, pause.pause_reason),
            )
            connection.execute(
                "INSERT INTO attempts(run_id, task_id, attempt_id, state) "
                "VALUES (?, ?, ?, 'FAILED') ON CONFLICT(run_id, attempt_id) DO UPDATE SET "
                "task_id = excluded.task_id, state = 'FAILED'",
                (pause.run_id, pause.task_id, pause.previous_attempt_id),
            )
            state = TaskBudgetState(
                run_id=pause.run_id,
                task_id=pause.task_id,
                allocated_calls=counters.allocated_calls,
                consumed_calls=counters.model_calls,
                input_tokens=counters.input_tokens,
                output_tokens=counters.output_tokens,
                cost_usd=counters.cost_reserve_usd,
                tranche_count=(counters.allocated_calls + 7) // 8,
                bootstrap_tranches=min(2, (counters.allocated_calls + 7) // 8),
                attempts=counters.attempts,
                stale_refreshes=counters.stale_refreshes,
                manual_resumes=counters.manual_resumes,
            )
            self._write_task_budget_state(connection, state)
            connection.execute(
                "DELETE FROM task_invalid_actions WHERE run_id = ? AND task_id = ?",
                (pause.run_id, pause.task_id),
            )
            connection.execute(
                "DELETE FROM task_checkpoints WHERE run_id = ? AND task_id = ?",
                (pause.run_id, pause.task_id),
            )
            for index, checkpoint in enumerate(counters.checkpoint_history, start=1):
                connection.execute(
                    "INSERT INTO task_checkpoints(run_id, task_id, tree_oid, check_set_digest, "
                    "budget_digest, observed_sequence) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        pause.run_id,
                        pause.task_id,
                        checkpoint.tree_oid,
                        checkpoint.check_set_digest,
                        pause.budget_digest_at_pause,
                        index,
                    ),
                )
            for index, action_digest in enumerate(counters.invalid_action_history, start=1):
                attempt_id = AttemptId(f"resume-history-{pause.task_id}-{index}")
                connection.execute(
                    "INSERT INTO attempts(run_id, task_id, attempt_id, state) "
                    "VALUES (?, ?, ?, 'FAILED') ON CONFLICT(run_id, attempt_id) DO UPDATE SET "
                    "task_id = excluded.task_id, state = 'FAILED'",
                    (pause.run_id, pause.task_id, attempt_id),
                )
                connection.execute(
                    "INSERT INTO task_invalid_actions(run_id, task_id, attempt_id, action_digest, "
                    "budget_digest, observed_sequence) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        pause.run_id,
                        pause.task_id,
                        attempt_id,
                        action_digest,
                        pause.budget_digest_at_pause,
                        index,
                    ),
                )
            connection.execute(
                "INSERT INTO task_resume_metadata(run_id, task_id, next_lease_generation, "
                "failure_digests_json, warning_keys_json) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(run_id, task_id) DO UPDATE SET "
                "next_lease_generation = excluded.next_lease_generation, "
                "failure_digests_json = excluded.failure_digests_json, "
                "warning_keys_json = excluded.warning_keys_json",
                (
                    pause.run_id,
                    pause.task_id,
                    counters.next_lease_generation,
                    _string_tuple_to_json(counters.failure_digests),
                    _string_tuple_to_json(counters.warning_keys),
                ),
            )
            connection.execute(
                "DELETE FROM trusted_task_repairs WHERE run_id = ? AND task_id = ?",
                (pause.run_id, pause.task_id),
            )
            connection.execute(
                "DELETE FROM task_pauses WHERE run_id = ? AND task_id = ?",
                (pause.run_id, pause.task_id),
            )
            self._insert_task_pause(connection, pause, applicable_revision_digests)
            close_cause = (
                DispatchCloseCause.BUDGET_EXHAUSTED
                if pause.pause_reason == "LOWERED_BUDGET_CEILING"
                or pause.budget_ceiling_exhaustions
                else DispatchCloseCause.TASK_PAUSED
            )
            self._close_new_dispatch(connection, pause.run_id, close_cause)

    def record_trusted_task_repair_for_test(
        self,
        pause: TaskPauseBinding,
        observation_digest: str,
    ) -> None:
        if not observation_digest:
            raise StateConflict("TASK_REPAIR_OBSERVATION_INVALID")
        with self._transaction("IMMEDIATE") as connection:
            if self._read_current_task_pause(connection, pause.run_id, pause.task_id) != pause:
                raise StateConflict("TASK_REPAIR_PAUSE_BINDING_MISMATCH")
            connection.execute(
                "INSERT INTO trusted_task_repairs(run_id, task_id, pause_sequence, "
                "pause_reason, observation_digest) VALUES (?, ?, ?, ?, ?)",
                (
                    pause.run_id,
                    pause.task_id,
                    pause.pause_sequence,
                    pause.pause_reason,
                    observation_digest,
                ),
            )

    def task_repair_observed(self, pause: TaskPauseBinding) -> bool:
        with self._read_transaction() as connection:
            if self._read_current_task_pause(connection, pause.run_id, pause.task_id) != pause:
                return False
            row = connection.execute(
                "SELECT observation_digest FROM trusted_task_repairs WHERE run_id = ? "
                "AND task_id = ? AND pause_sequence = ? AND pause_reason = ?",
                (pause.run_id, pause.task_id, pause.pause_sequence, pause.pause_reason),
            ).fetchone()
        return row is not None and bool(row["observation_digest"])

    def _resolve_dispatch_close_cause_after_exact_resume(
        self,
        connection: sqlite3.Connection,
        run_id: RunId,
        cause: DispatchCloseCause,
    ) -> None:
        is_open, causes, prior_json = self._dispatch_state_for_update(connection, run_id)
        normalized = DispatchCloseCause(cause)
        if normalized not in causes:
            raise StateConflict("EXACT_RESUME_DISPATCH_CAUSE_MISMATCH")
        remaining = causes - {normalized}
        next_json = dispatch_close_causes_to_json(remaining)
        changed = connection.execute(
            "UPDATE runs SET new_dispatch_open = ?, dispatch_close_causes_json = ? "
            "WHERE run_id = ? AND new_dispatch_open = ? AND dispatch_close_causes_json = ?",
            (int(not remaining), next_json, run_id, int(is_open), prior_json),
        ).rowcount
        if changed != 1:
            raise StateConflict("DISPATCH_REOPEN_COMPARE_AND_SET_FAILED")

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

        def mutate(connection: sqlite3.Connection) -> None:
            self._require_current_revisions(
                connection,
                request.run_id,
                request.applicable_revision_digests,
            )
            self._require_current_budget(connection, request.run_id, budget_digest)
            current_pause = self._read_current_task_pause(
                connection,
                request.run_id,
                request.task_id,
            )
            current_counters = self._task_counters_in_transaction(
                connection,
                request.run_id,
                request.task_id,
            )
            current_usage = self._global_usage_snapshot_in_transaction(connection, request.run_id)
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
            current_budget = self._task_budget_state(
                connection,
                request.run_id,
                request.task_id,
            )
            self._write_task_budget_state(
                connection,
                replace(current_budget, manual_resumes=current_budget.manual_resumes + 1),
            )
            connection.execute(
                "INSERT INTO task_resume_allocations(allocation_id, run_id, task_id, "
                "reserved_attempt_id, budget_digest, applicable_revision_digests_json, "
                "allocated_calls, state, created_sequence) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'RESERVED', ?)",
                (
                    allocation_id,
                    request.run_id,
                    request.task_id,
                    new_attempt_id,
                    budget_digest,
                    applicable_revision_digests_to_json(request.applicable_revision_digests),
                    calls,
                    request.expected_sequence + 1,
                ),
            )
            task_changed = connection.execute(
                "UPDATE tasks SET state = 'READY', pause_reason = NULL, pause_counter = NULL "
                "WHERE run_id = ? AND task_id = ? AND state = 'PAUSED'",
                (request.run_id, request.task_id),
            ).rowcount
            pause_changed = connection.execute(
                "UPDATE task_pauses SET active = 0 WHERE run_id = ? AND task_id = ? "
                "AND pause_sequence = ? AND active = 1",
                (request.run_id, request.task_id, pause.pause_sequence),
            ).rowcount
            if task_changed != 1 or pause_changed != 1:
                raise StateConflict("TASK_RESUME_COMPARE_AND_SET_FAILED")
            close_cause = (
                DispatchCloseCause.BUDGET_EXHAUSTED
                if pause.pause_reason == "LOWERED_BUDGET_CEILING"
                or pause.budget_ceiling_exhaustions
                else DispatchCloseCause.TASK_PAUSED
            )
            self._resolve_dispatch_close_cause_after_exact_resume(
                connection,
                request.run_id,
                close_cause,
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
        return TaskResumeDecision(
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
        reason: str,
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
                if (
                    connection.execute(
                        "SELECT 1 FROM runs WHERE run_id = ?", (task.run_id,)
                    ).fetchone()
                    is not None
                ):
                    self._record_task_pause_binding(
                        connection,
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
                if (
                    connection.execute(
                        "SELECT 1 FROM runs WHERE run_id = ?", (task.run_id,)
                    ).fetchone()
                    is not None
                ):
                    self._record_task_pause_binding(
                        connection,
                        run_id=task.run_id,
                        task_id=task.task_id,
                        attempt_id=attempt_id,
                        pause_sequence=AuditSequence(expected_sequence + 1),
                        pause_reason="REPEATED_INVALID_ACTION",
                        budget_digest=budget_digest,
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
    def _task_contracts_in_transaction(
        connection: sqlite3.Connection,
        plan_digest: RevisionDigest,
    ) -> tuple[TaskContract, ...]:
        rows = connection.execute(
            "SELECT task_id, task_revision, contract_digest, contract_json "
            "FROM task_contracts WHERE plan_digest = ? ORDER BY task_id",
            (plan_digest,),
        ).fetchall()
        if not rows:
            raise StateConflict("PLAN_PROPOSAL_NOT_FOUND")
        contracts: list[TaskContract] = []
        try:
            for row in rows:
                contract = task_contract_from_json(str(row["contract_json"]))
                if (
                    row["task_revision"] != 1
                    or row["task_id"] != contract.task_id
                    or row["contract_digest"] != task_contract_digest(contract)
                ):
                    raise StateConflict("TASK_CONTRACT_STORAGE_BINDING_MISMATCH")
                contracts.append(contract)
        except (TypeError, ValueError) as error:
            raise StateConflict("TASK_CONTRACT_STORAGE_BINDING_MISMATCH") from error
        return tuple(contracts)

    @staticmethod
    def _task_edges_in_transaction(
        connection: sqlite3.Connection,
        table: Literal["task_dependencies", "hazard_edges"],
        plan_digest: RevisionDigest,
    ) -> tuple[tuple[TaskId, TaskId], ...]:
        rows = connection.execute(
            f"SELECT predecessor_task_id, successor_task_id FROM {table} "
            "WHERE plan_digest = ? ORDER BY predecessor_task_id, successor_task_id",
            (plan_digest,),
        ).fetchall()
        return tuple(
            (TaskId(row["predecessor_task_id"]), TaskId(row["successor_task_id"])) for row in rows
        )

    @staticmethod
    def _run_check_set_in_transaction(
        connection: sqlite3.Connection,
        plan_digest: RevisionDigest,
    ) -> tuple[CheckDefinition, ...]:
        rows = connection.execute(
            "SELECT ordinal, check_digest, check_json FROM run_checks "
            "WHERE plan_digest = ? ORDER BY ordinal",
            (plan_digest,),
        ).fetchall()
        if tuple(row["ordinal"] for row in rows) != tuple(range(len(rows))):
            raise StateConflict("RUN_CHECK_STORAGE_BINDING_MISMATCH")
        checks: list[CheckDefinition] = []
        try:
            for row in rows:
                check = check_definition_from_json(str(row["check_json"]))
                if row["check_digest"] != sha256_digest(check_definition_json(check)):
                    raise StateConflict("RUN_CHECK_STORAGE_BINDING_MISMATCH")
                checks.append(check)
        except ValueError as error:
            raise StateConflict("RUN_CHECK_STORAGE_BINDING_MISMATCH") from error
        return tuple(checks)

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

        def mutate(connection: sqlite3.Connection) -> None:
            run = connection.execute(
                "SELECT state, pinned_target_oid, current_plan_digest, current_policy_digest, "
                "current_budget_digest, current_model_configuration_digest, new_dispatch_open "
                "FROM runs WHERE run_id = ?",
                (proposal.run_id,),
            ).fetchone()
            if run is None:
                raise StateConflict("RUN_NOT_FOUND")
            if run["state"] != RunState.PLANNING:
                raise StateConflict("PLAN_PROPOSAL_REQUIRES_PLANNING")
            if run["pinned_target_oid"] != proposal.base_run_head_oid:
                raise StateConflict("PLAN_BASE_BINDING_MISMATCH")
            current = ApplicableRevisionDigests(
                plan_digest=run["current_plan_digest"],
                policy_digest=run["current_policy_digest"],
                budget_digest=run["current_budget_digest"],
                model_configuration_digest=run["current_model_configuration_digest"],
            )
            if current != proposal.applicable_revision_digests:
                raise StateConflict("PLAN_REVISION_BINDING_MISMATCH")
            planning_row = connection.execute(
                "SELECT planning_requests FROM run_authority_counters WHERE run_id = ?",
                (proposal.run_id,),
            ).fetchone()
            planning_count = 0 if planning_row is None else int(planning_row["planning_requests"])
            if planning_count != proposal.planning_request_count:
                raise StateConflict("PLANNING_REQUEST_COUNT_MISMATCH")
            budget_digest = proposal.applicable_revision_digests.budget_digest
            if budget_digest is None:
                raise StateConflict("CURRENT_BUDGET_NOT_FOUND")
            budget = self._approved_budget_for_update(connection, proposal.run_id, budget_digest)
            if len(proposal.plan.tasks) > budget.task_ceiling:
                raise StateConflict("PLAN_TASK_CEILING")
            if not bool(run["new_dispatch_open"]):
                raise StateConflict("NEW_DISPATCH_CLOSED")
            try:
                connection.execute(
                    "INSERT INTO plans(run_id, plan_digest, base_run_head_oid, policy_digest, "
                    "budget_digest, model_configuration_digest, run_check_set_digest, "
                    "planning_request_count, state, proposal_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'PROPOSED', ?)",
                    (
                        proposal.run_id,
                        proposal.plan_digest,
                        proposal.base_run_head_oid,
                        proposal.applicable_revision_digests.policy_digest,
                        budget_digest,
                        proposal.applicable_revision_digests.model_configuration_digest,
                        run_check_set_digest(proposal.run_check_set),
                        proposal.planning_request_count,
                        plan_proposal_record_json(proposal),
                    ),
                )
                for task in proposal.plan.tasks:
                    connection.execute(
                        "INSERT INTO task_contracts(plan_digest, task_id, task_revision, "
                        "contract_digest, contract_json, state) VALUES (?, ?, 1, ?, ?, ?)",
                        (
                            proposal.plan_digest,
                            task.task_id,
                            task_contract_digest(task),
                            task_contract_json(task),
                            "BLOCKED" if task.dependency_task_ids else "READY",
                        ),
                    )
                connection.executemany(
                    "INSERT INTO task_dependencies(plan_digest, predecessor_task_id, "
                    "successor_task_id) VALUES (?, ?, ?)",
                    (
                        (proposal.plan_digest, predecessor, successor)
                        for predecessor, successor in proposal.dependency_edges
                    ),
                )
                connection.executemany(
                    "INSERT INTO hazard_edges(plan_digest, predecessor_task_id, "
                    "successor_task_id, hazard_class) VALUES (?, ?, ?, 'PROMOTION')",
                    (
                        (proposal.plan_digest, predecessor, successor)
                        for predecessor, successor in proposal.hazard_edges
                    ),
                )
                for ordinal, check in enumerate(proposal.run_check_set):
                    check_json = check_definition_json(check)
                    connection.execute(
                        "INSERT INTO run_checks(plan_digest, ordinal, check_digest, check_json) "
                        "VALUES (?, ?, ?, ?)",
                        (
                            proposal.plan_digest,
                            ordinal,
                            sha256_digest(check_json),
                            check_json,
                        ),
                    )
            except sqlite3.IntegrityError as error:
                raise StateConflict("PLAN_GRAPH_STORAGE_CONFLICT") from error
            task_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM task_contracts WHERE plan_digest = ?",
                    (proposal.plan_digest,),
                ).fetchone()[0]
            )
            settlement, stopped = self._settle_global_usage_in_transaction(
                connection,
                proposal.run_id,
                budget_digest,
                GlobalBudgetMetric.TASKS,
                task_count,
            )
            if settlement.pause_after_barrier != stopped:
                raise StateConflict("PLAN_TASK_STOP_BINDING_INVALID")
            next_state = RunState.PAUSED if stopped else RunState.AWAITING_PLAN_APPROVAL
            if (
                connection.execute(
                    "UPDATE runs SET state = ? WHERE run_id = ? AND state = 'PLANNING'",
                    (next_state.value, proposal.run_id),
                ).rowcount
                != 1
            ):
                raise StateConflict("PLAN_PROPOSAL_STATE_COMPARE_AND_SET_FAILED")
            self._settle_recovered_planning_marker(
                connection,
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
        with self._read_transaction() as connection:
            row = connection.execute(
                "SELECT * FROM plans WHERE run_id = ? AND plan_digest = ?",
                (run_id, plan_digest),
            ).fetchone()
            if row is None:
                raise StateConflict("PLAN_PROPOSAL_NOT_FOUND")
            tasks = self._task_contracts_in_transaction(connection, plan_digest)
            dependencies = self._task_edges_in_transaction(
                connection, "task_dependencies", plan_digest
            )
            hazards = self._task_edges_in_transaction(connection, "hazard_edges", plan_digest)
            checks = self._run_check_set_in_transaction(connection, plan_digest)
            try:
                canonical_plan_json, promotion_order = plan_proposal_record_from_json(
                    str(row["proposal_json"])
                )
                proposal = PlanProposal.from_validated_plan(
                    run_id=RunId(row["run_id"]),
                    canonical_plan_json=canonical_plan_json,
                    plan=PlanRevision(tasks=tasks, proposed_promotion_order=promotion_order),
                    base_run_head_oid=GitOid(row["base_run_head_oid"]),
                    applicable_revision_digests=ApplicableRevisionDigests(
                        policy_digest=RevisionDigest(row["policy_digest"]),
                        budget_digest=RevisionDigest(row["budget_digest"]),
                        model_configuration_digest=RevisionDigest(
                            row["model_configuration_digest"]
                        ),
                    ),
                    run_check_set=checks,
                    planning_request_count=int(row["planning_request_count"]),
                )
            except ValueError as error:
                raise StateConflict("PLAN_PROPOSAL_STORAGE_BINDING_MISMATCH") from error
            if (
                proposal.plan_digest != row["plan_digest"]
                or proposal.dependency_edges != dependencies
                or proposal.hazard_edges != hazards
                or run_check_set_digest(checks) != row["run_check_set_digest"]
            ):
                raise StateConflict("PLAN_PROPOSAL_STORAGE_BINDING_MISMATCH")
            return proposal

    def task_contracts(self, plan_digest: RevisionDigest) -> tuple[TaskContract, ...]:
        with self._read_transaction() as connection:
            return self._task_contracts_in_transaction(connection, plan_digest)

    def task_dependency_edges(
        self, plan_digest: RevisionDigest
    ) -> tuple[tuple[TaskId, TaskId], ...]:
        with self._read_transaction() as connection:
            if (
                connection.execute(
                    "SELECT 1 FROM plans WHERE plan_digest = ?", (plan_digest,)
                ).fetchone()
                is None
            ):
                raise StateConflict("PLAN_PROPOSAL_NOT_FOUND")
            return self._task_edges_in_transaction(connection, "task_dependencies", plan_digest)

    def hazard_edges(self, plan_digest: RevisionDigest) -> tuple[tuple[TaskId, TaskId], ...]:
        with self._read_transaction() as connection:
            if (
                connection.execute(
                    "SELECT 1 FROM plans WHERE plan_digest = ?", (plan_digest,)
                ).fetchone()
                is None
            ):
                raise StateConflict("PLAN_PROPOSAL_NOT_FOUND")
            return self._task_edges_in_transaction(connection, "hazard_edges", plan_digest)

    def run_check_set(self, plan_digest: RevisionDigest) -> tuple[CheckDefinition, ...]:
        with self._read_transaction() as connection:
            if (
                connection.execute(
                    "SELECT 1 FROM plans WHERE plan_digest = ?", (plan_digest,)
                ).fetchone()
                is None
            ):
                raise StateConflict("PLAN_PROPOSAL_NOT_FOUND")
            return self._run_check_set_in_transaction(connection, plan_digest)

    def planning_request_count(self, run_id: RunId) -> int:
        with self._read_transaction() as connection:
            if (
                connection.execute("SELECT 1 FROM runs WHERE run_id = ?", (run_id,)).fetchone()
                is None
            ):
                raise StateConflict("RUN_NOT_FOUND")
            row = connection.execute(
                "SELECT planning_requests FROM run_authority_counters WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            return 0 if row is None else int(row["planning_requests"])

    def planning_returned_bytes(self, run_id: RunId) -> int:
        with self._read_transaction() as connection:
            row = connection.execute(
                "SELECT planning_returned_bytes FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise StateConflict("RUN_NOT_FOUND")
            return int(row["planning_returned_bytes"])

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

        def mutate(connection: sqlite3.Connection) -> None:
            self._insert_effect_intent(connection, effect_intent)
            self._settle_recovered_planning_marker(
                connection,
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
        connection: sqlite3.Connection,
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
        ):
            raise StateConflict("RECOVERED_PLANNING_MARKER_BINDING_MISMATCH")
        stored_permit, _ = self._require_consumed_runtime_owner(
            connection, run_id, owner_id, permit.generation
        )
        if stored_permit != permit:
            raise StateConflict("RECOVERED_PLANNING_MARKER_BINDING_MISMATCH")
        stored = self._require_unsettled_effect_intent(
            connection, run_id, recovered_marker.intent_id
        )
        if stored != recovered_marker:
            raise StateConflict("RECOVERED_PLANNING_MARKER_BINDING_MISMATCH")
        payload = canonical_json({"result_class": "PLANNING_ACTION_RELEASED"})
        result = EffectResult(
            intent_id=recovered_marker.intent_id,
            run_id=run_id,
            outcome="COMPLETED",
            result_class="PLANNING_ACTION_RELEASED",
            result_digest=sha256_digest(payload),
            bounded_result_json=payload,
            settled_sequence=AuditSequence(expected_sequence + 1),
        )
        self._insert_effect_result(
            connection,
            run_id,
            recovered_marker.intent_id,
            result,
            recovered_marker.applicable_revision_digests,
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
        with self._read_transaction() as connection:
            row = connection.execute(
                "SELECT planning_returned_bytes FROM runs WHERE run_id = ?", (intent.run_id,)
            ).fetchone()
            if row is None:
                raise StateConflict("RUN_NOT_FOUND")
            overflow = int(row["planning_returned_bytes"]) + result.returned_bytes > 2_097_152
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

        def mutate(connection: sqlite3.Connection) -> None:
            self._insert_effect_result(
                connection,
                intent.run_id,
                intent.intent_id,
                effect_result,
                intent.applicable_revision_digests,
            )
            if not overflow:
                updated = connection.execute(
                    "UPDATE runs SET planning_returned_bytes = planning_returned_bytes + ? "
                    "WHERE run_id = ? AND planning_returned_bytes + ? <= 2097152",
                    (result.returned_bytes, intent.run_id, result.returned_bytes),
                )
                if updated.rowcount != 1:
                    raise StateConflict("PLANNING_READ_LIMIT_REVALIDATION_FAILED")
            else:
                connection.execute(
                    "UPDATE runs SET state = 'PAUSED' WHERE run_id = ?", (intent.run_id,)
                )
                self._close_new_dispatch(
                    connection, intent.run_id, DispatchCloseCause.BUDGET_EXHAUSTED
                )

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
        return PlanningReadSettlement(sequence, "PLANNING_READ_LIMIT" if overflow else None)

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

        def mutate(connection: sqlite3.Connection) -> None:
            if authorization.run_id != run_id or authorization.decision != "ALLOW":
                raise StateConflict("PLANNING_AUTHORIZATION_MISMATCH")
            row = connection.execute(
                "SELECT planning_requests FROM run_authority_counters WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            count = 0 if row is None else int(row["planning_requests"])
            if count >= authorization.planning_request_ceiling:
                connection.execute("UPDATE runs SET state = 'PAUSED' WHERE run_id = ?", (run_id,))
                self._close_new_dispatch(connection, run_id, DispatchCloseCause.BUDGET_EXHAUSTED)
            self._settle_recovered_planning_marker(
                connection,
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
        def mutate(connection: sqlite3.Connection) -> None:
            if authorization.run_id != run_id or authorization.decision != "ALLOW":
                raise StateConflict("PLANNING_AUTHORIZATION_MISMATCH")
            connection.execute("UPDATE runs SET state = 'DRAFT' WHERE run_id = ?", (run_id,))

        return self._commit_state_and_event(
            run_id=run_id,
            expected_sequence=expected_sequence,
            event=AuditEvent.kind("PLANNING_CONTEXT_OVERFLOW"),
            mutate=mutate,
        )

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

    @staticmethod
    def _global_usage_snapshot_in_transaction(
        connection: sqlite3.Connection,
        run_id: RunId,
    ) -> GlobalUsageSnapshot:
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

    def global_usage_snapshot(self, run_id: RunId) -> GlobalUsageSnapshot:
        with self._read_transaction() as connection:
            return self._global_usage_snapshot_in_transaction(connection, run_id)

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

    def runtime_barrier_state(
        self, run_id: RunId
    ) -> Literal["IDLE", "IN_FLIGHT", "SETTLED", "INDETERMINATE"]:
        with self._read_transaction() as connection:
            row = connection.execute(
                "SELECT state FROM runtime_barriers WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return "IDLE" if row is None else row["state"]

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
                "request_requested_model_id, reserved_json, allowed_model_ids_json, "
                "reserved_sequence, state, "
                "owner_kind, task_id, attempt_id, tranche_id, dispatch_deadline_at_utc, "
                "target_safety_digest, budget_digest, model_configuration_digest) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'RESERVED', ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    intent.intent_id,
                    request.run_id,
                    turn.logical_turn_id,
                    request.provider_attempt_number,
                    model_request_to_json(request.model_request),
                    request.model_request.request_digest,
                    request.model_request.idempotency_key,
                    request.model_request.requested_model_id,
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
            new_planning_request = (
                request.owner_kind == "PLANNING" and request.provider_attempt_number == 1
            )
            if new_planning_request:
                connection.execute(
                    "INSERT INTO run_authority_counters(run_id, planning_requests) "
                    "VALUES (?, ?) ON CONFLICT(run_id) DO UPDATE SET "
                    "planning_requests = excluded.planning_requests",
                    (request.run_id, evaluation.planning_requests + 1),
                )
            elif request.owner_kind == "WORKER":
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
                    if metric == GlobalBudgetMetric.PLANNING_REQUESTS and not new_planning_request:
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

    def revision_binding_failure(
        self,
        run_id: RunId,
        expected: ApplicableRevisionDigests,
    ) -> str | None:
        record = self.run_record(run_id)
        current = ApplicableRevisionDigests(
            plan_digest=record.current_plan_digest,
            policy_digest=record.current_policy_digest,
            budget_digest=record.current_budget_digest,
            model_configuration_digest=record.current_model_configuration_digest,
        )
        return None if current == expected else "CURRENT_REVISION_BINDING_MISMATCH"

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
            task_row = connection.execute(
                "SELECT state FROM tasks WHERE run_id = ? AND task_id = ?",
                (task.run_id, task.task_id),
            ).fetchone()
            run_row = connection.execute(
                "SELECT 1 FROM runs WHERE run_id = ?", (task.run_id,)
            ).fetchone()
            if calls == 0 and task_row is not None and run_row is not None:
                pause_reason = "NO_PROGRESS" if reason == "NO_PROGRESS" else "TASK_CALL_CEILING"
                self._pause_task(
                    connection,
                    task.run_id,
                    task.task_id,
                    pause_reason,
                    1,
                )
                budget_row = connection.execute(
                    "SELECT budget_digest FROM approved_budgets_for_test WHERE run_id = ?",
                    (task.run_id,),
                ).fetchone()
                if budget_row is None:
                    raise StateConflict("APPROVED_BUDGET_NOT_FOUND")
                self._record_task_pause_binding(
                    connection,
                    run_id=task.run_id,
                    task_id=task.task_id,
                    attempt_id=task.attempt_id,
                    pause_sequence=AuditSequence(expected_sequence + 1),
                    pause_reason=pause_reason,
                    budget_digest=RevisionDigest(str(budget_row["budget_digest"])),
                )
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
                "request_requested_model_id, reserved_json, allowed_model_ids_json, "
                "reserved_sequence, state) VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, 'RESERVED')",
                (
                    intent.intent_id,
                    request.run_id,
                    turn.logical_turn_id,
                    model_request_to_json(request),
                    request.request_digest,
                    request.idempotency_key,
                    request.requested_model_id,
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
                "request_requested_model_id, reserved_json, allowed_model_ids_json, "
                "reserved_sequence, state) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'RESERVED')",
                (
                    intent.intent_id,
                    request.run_id,
                    turn.logical_turn_id,
                    provider_attempt_number,
                    model_request_to_json(request),
                    request.request_digest,
                    request.idempotency_key,
                    request.requested_model_id,
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
                "response_requested_model_id": (
                    settled.dispatch_result.response_requested_model_id
                ),
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
                "response_requested_model_id = ?, response_requested_model_binding = 'BOUND', "
                "returned_model_id = ?, result_digest = ?, "
                "charged_json = ?, "
                "result_json = ?, settled_sequence = ? WHERE run_id = ? AND intent_id = ? "
                "AND state = 'RESERVED'",
                (
                    settled.kind,
                    settled.provider_response_id,
                    settled.reason_code,
                    reported_usage_json,
                    settled.dispatch_result.response_requested_model_id,
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
                        dispatch.response_requested_model_id is None
                        or dispatch.response_requested_model_id != intent.request.requested_model_id
                        or dispatch.returned_model_id is None
                        or dispatch.normalized_payload_digest is None
                        or dispatch.normalized_action is None
                    ):
                        raise StateConflict("MODEL_COMPLETION_NOT_RELEASABLE")
                    committed = connection.execute(
                        "UPDATE model_turns SET owner_kind = ?, task_id = ?, attempt_id = ?, "
                        "tranche_id = ?, recovery_binding_json = ?, "
                        "response_requested_model_id = ?, response_requested_model_binding = 'BOUND', "
                        "returned_model_id = ?, "
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
                            dispatch.response_requested_model_id,
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
            "charged_json, result_json, model_attempts.response_requested_model_id, "
            "model_attempts.response_requested_model_binding, reported_usage_json, backoff_seconds, "
            "model_attempts.request_digest, model_turns.request_digest, "
            "model_attempts.request_requested_model_id "
            "FROM model_attempts JOIN model_turns USING(run_id, logical_turn_id) "
            "WHERE model_attempts.run_id = ? AND model_attempts.logical_turn_id = ? "
            "AND model_attempts.state = 'CLOSED' ORDER BY model_attempts.provider_attempt_number",
            (run_id, logical_turn_id),
        ).fetchall()
        attempts: list[SettledModelAttempt] = []
        for row in rows:
            dispatch_data = json.loads(row[10])
            charged = ModelBudgetAmounts.from_json(row[9])
            request = model_request_from_json(row[3])
            if (
                request.request_digest != row[15]
                or row[15] != row[16]
                or request.requested_model_id != row[17]
            ):
                raise StateConflict("MODEL_REQUEST_STORAGE_BINDING_MISMATCH")
            dispatch = ModelDispatchResult(
                run_id=RunId(dispatch_data["run_id"]),
                logical_turn_id=logical_turn_id,
                outcome=dispatch_data["outcome"],
                response_requested_model_id=dispatch_data.get("response_requested_model_id"),
                returned_model_id=dispatch_data["returned_model_id"],
                normalized_action=dispatch_data["normalized_action"],
                normalized_payload_digest=dispatch_data["normalized_payload_digest"],
                charged_amounts=charged,
            )
            response_requested_model_id_present = "response_requested_model_id" in dispatch_data
            if row[12] == "LEGACY":
                if (
                    row[11] is not None
                    or response_requested_model_id_present
                    or _legacy_model_attempt_result_digest(
                        kind=row[5],
                        charged=charged,
                        provider_response_id=row[6],
                        reason_code=row[7],
                        normalized_payload_digest=dispatch.normalized_payload_digest,
                    )
                    != row[8]
                ):
                    raise StateConflict("MODEL_RESPONSE_REQUESTED_ID_STORAGE_BINDING_MISMATCH")
            elif row[12] != "BOUND" or (
                not response_requested_model_id_present
                or (dispatch.outcome == "COMPLETED" and row[11] is None)
                or dispatch.response_requested_model_id != row[11]
                or _bound_model_attempt_result_digest(
                    kind=row[5],
                    charged=charged,
                    provider_response_id=row[6],
                    reason_code=row[7],
                    normalized_payload_digest=dispatch.normalized_payload_digest,
                    response_requested_model_id=dispatch.response_requested_model_id,
                    returned_model_id=dispatch.returned_model_id,
                )
                != row[8]
                or (
                    dispatch.outcome == "COMPLETED"
                    and dispatch.response_requested_model_id != request.requested_model_id
                )
            ):
                raise StateConflict("MODEL_RESPONSE_REQUESTED_ID_STORAGE_BINDING_MISMATCH")
            reported_usage = (
                None
                if row[13] is None
                else ModelUsage(
                    input_tokens=int(json.loads(row[13])["input_tokens"]),
                    output_tokens=int(json.loads(row[13])["output_tokens"]),
                    cost_usd=Decimal(str(json.loads(row[13])["cost_usd"])),
                )
            )
            attempts.append(
                SettledModelAttempt(
                    run_id=RunId(row[0]),
                    intent_id=IntentId(row[1]),
                    logical_turn_id=logical_turn_id,
                    provider_attempt_number=row[2],
                    request=request,
                    reserved_amounts=ModelBudgetAmounts.from_json(row[4]),
                    kind=ProviderAttemptKind(row[5]),
                    provider_response_id=row[6],
                    reason_code=row[7],
                    charged_amounts=charged,
                    result_digest=row[8],
                    dispatch_result=dispatch,
                    reported_usage=reported_usage,
                    backoff_seconds=row[14],
                )
            )
        return tuple(attempts)

    def committed_model_turn(
        self, run_id: RunId, logical_turn_id: LogicalTurnId
    ) -> CommittedModelTurn | None:
        row = self._connection.execute(
            "SELECT logical_turn_id, owner_kind, task_id, attempt_id, tranche_id, "
            "recovery_binding_json, response_requested_model_id, "
            "response_requested_model_binding, returned_model_id, "
            "normalized_output_digest, normalized_payload_json, dispatch_result_json, "
            "committed_sequence, state, "
            "downstream_intent_id, downstream_sequence FROM model_turns "
            "WHERE run_id = ? AND logical_turn_id = ?",
            (run_id, logical_turn_id),
        ).fetchone()
        if row is None or row[13] not in {
            "COMPLETION_COMMITTED",
            "DOWNSTREAM_INTENT_RECORDED",
        }:
            return None
        if any(row[index] is None for index in (1, 5, 8, 9, 10, 11, 12)):
            raise StateConflict("COMMITTED_MODEL_TURN_INCOMPLETE")
        if row[1] == "PLANNING" and any(row[index] is not None for index in (2, 3, 4)):
            raise StateConflict("COMMITTED_MODEL_OWNER_BINDING_MISMATCH")
        if row[1] == "WORKER" and any(row[index] is None for index in (2, 3, 4)):
            raise StateConflict("COMMITTED_MODEL_OWNER_BINDING_MISMATCH")
        dispatch_data = json.loads(row[11])
        response_requested_model_id_present = "response_requested_model_id" in dispatch_data
        dispatch = model_dispatch_result_from_json(row[11])
        payload = json.loads(row[10])
        if row[7] == "LEGACY":
            if row[6] is not None or response_requested_model_id_present:
                raise StateConflict(
                    "COMMITTED_MODEL_RESPONSE_REQUESTED_ID_STORAGE_BINDING_MISMATCH"
                )
        elif row[7] != "BOUND" or (
            not response_requested_model_id_present
            or row[6] is None
            or dispatch.response_requested_model_id != row[6]
        ):
            raise StateConflict("COMMITTED_MODEL_RESPONSE_REQUESTED_ID_STORAGE_BINDING_MISMATCH")
        try:
            attempts = self.model_attempts(run_id, logical_turn_id)
        except StateConflict as error:
            raise StateConflict(
                "COMMITTED_MODEL_RESPONSE_REQUESTED_ID_STORAGE_BINDING_MISMATCH"
            ) from error
        if not any(
            attempt.kind is ProviderAttemptKind.COMPLETED and attempt.dispatch_result == dispatch
            for attempt in attempts
        ):
            raise StateConflict("COMMITTED_MODEL_RESPONSE_REQUESTED_ID_STORAGE_BINDING_MISMATCH")
        if (
            dispatch.run_id != run_id
            or dispatch.logical_turn_id != row[0]
            or dispatch.returned_model_id != row[8]
            or dispatch.normalized_payload_digest != row[9]
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
            response_requested_model_id=row[6],
            returned_model_id=row[8],
            normalized_output_digest=row[9],
            normalized_payload=payload,
            dispatch_result=dispatch,
            committed_sequence=AuditSequence(row[12]),
            state=row[13],
            downstream_intent_id=(None if row[14] is None else IntentId(row[14])),
            downstream_sequence=(None if row[15] is None else AuditSequence(row[15])),
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

    def _read_action_deadline(
        self,
        connection: sqlite3.Connection,
        intent_id: IntentId,
    ) -> ActionDeadline | None:
        row = connection.execute(
            "SELECT * FROM action_deadlines WHERE intent_id = ?", (intent_id,)
        ).fetchone()
        if row is None:
            return None
        try:
            return ActionDeadline(
                run_id=RunId(str(row["run_id"])),
                intent_id=IntentId(str(row["intent_id"])),
                budget_digest=RevisionDigest(str(row["budget_digest"])),
                applicable_revision_digests=ApplicableRevisionDigests.model_validate_json(
                    str(row["applicable_revision_digests_json"])
                ),
                action_class=ActionClass(str(row["action_class"])),
                started_at=datetime.fromisoformat(str(row["started_at_utc"])),
                expires_at=datetime.fromisoformat(str(row["expires_at_utc"])),
                recorded_sequence=AuditSequence(int(row["recorded_sequence"])),
                check_id=None if row["check_id"] is None else str(row["check_id"]),
                snapshot_digest=(
                    None if row["snapshot_digest"] is None else str(row["snapshot_digest"])
                ),
            )
        except (TypeError, ValueError) as error:
            raise StateConflict("ACTION_DEADLINE_STORAGE_INVALID") from error

    def record_action_deadline(
        self,
        deadline: ActionDeadline,
        expected_sequence: AuditSequence,
    ) -> ActionDeadline:
        if deadline.recorded_sequence != AuditSequence(expected_sequence + 1):
            raise StateConflict("ACTION_DEADLINE_SEQUENCE_MISMATCH")

        def mutate(connection: sqlite3.Connection) -> None:
            self._require_current_revisions(
                connection, deadline.run_id, deadline.applicable_revision_digests
            )
            self._approved_budget_for_update(connection, deadline.run_id, deadline.budget_digest)
            intent = self._require_unsettled_effect_intent(
                connection, deadline.run_id, deadline.intent_id
            )
            self._validate_action_deadline_binding(deadline, intent)
            try:
                connection.execute(
                    "INSERT INTO action_deadlines(intent_id, run_id, budget_digest, "
                    "applicable_revision_digests_json, action_class, started_at_utc, "
                    "expires_at_utc, check_id, snapshot_digest, recorded_sequence) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        deadline.intent_id,
                        deadline.run_id,
                        deadline.budget_digest,
                        deadline.applicable_revision_digests.model_dump_json(),
                        deadline.action_class,
                        deadline.started_at.isoformat(),
                        deadline.expires_at.isoformat(),
                        deadline.check_id,
                        deadline.snapshot_digest,
                        deadline.recorded_sequence,
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise StateConflict("ACTION_DEADLINE_ALREADY_RECORDED") from error

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
        def mutate(connection: sqlite3.Connection) -> None:
            self._require_current_revisions(
                connection, deadline.run_id, deadline.applicable_revision_digests
            )
            self._approved_budget_for_update(connection, deadline.run_id, deadline.budget_digest)
            intent = self._require_unsettled_effect_intent(
                connection, deadline.run_id, deadline.intent_id
            )
            self._validate_action_deadline_binding(deadline, intent)
            if self._read_action_deadline(connection, deadline.intent_id) != deadline:
                raise StateConflict("ACTION_TIMEOUT_NOT_CURRENT")
            if deadline.action_class == ActionClass.ORDINARY:
                if decision.outcome != "INDETERMINATE":
                    raise StateConflict("ORDINARY_TIMEOUT_SUCCESSOR_REQUIRED")
                if (
                    connection.execute(
                        "UPDATE effect_intents SET state = 'INDETERMINATE' "
                        "WHERE intent_id = ? AND run_id = ? AND state = 'UNSETTLED'",
                        (deadline.intent_id, deadline.run_id),
                    ).rowcount
                    != 1
                ):
                    raise StateConflict("ACTION_TIMEOUT_SUCCESSOR_COMPARE_AND_SET_FAILED")
            else:
                dispatch_open, _, _ = self._dispatch_state_for_update(connection, deadline.run_id)
                if (
                    decision.outcome != "INFRASTRUCTURE_UNCERTAINTY"
                    or decision.retry_scope != (deadline.check_id, deadline.snapshot_digest)
                    or decision.retry_allowed != dispatch_open
                ):
                    raise StateConflict("CHECK_TIMEOUT_SUCCESSOR_BINDING_MISMATCH")
            try:
                connection.execute(
                    "INSERT INTO action_timeout_decisions"
                    "(intent_id, decision_json, settled_sequence) VALUES (?, ?, ?)",
                    (
                        deadline.intent_id,
                        timeout_decision_to_json(decision),
                        expected_sequence + 1,
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise StateConflict("ACTION_TIMEOUT_NOT_CURRENT") from error

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
        with self._read_transaction() as connection:
            return self._read_action_deadline(connection, intent_id)

    def timeout_decision(self, intent_id: IntentId) -> TimeoutDecision | None:
        with self._read_transaction() as connection:
            row = connection.execute(
                "SELECT decision_json FROM action_timeout_decisions WHERE intent_id = ?",
                (intent_id,),
            ).fetchone()
        if row is None:
            return None
        try:
            return timeout_decision_from_json(str(row["decision_json"]))
        except ValueError as error:
            raise StateConflict("TIMEOUT_DECISION_STORAGE_INVALID") from error

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

    @staticmethod
    def _current_revision_digests_in_transaction(
        connection: sqlite3.Connection, run_id: RunId
    ) -> ApplicableRevisionDigests:
        row = connection.execute(
            "SELECT current_plan_digest, current_policy_digest, current_budget_digest, "
            "current_model_configuration_digest FROM runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            raise StateConflict("RUN_NOT_FOUND")
        return ApplicableRevisionDigests(
            plan_digest=row["current_plan_digest"],
            policy_digest=row["current_policy_digest"],
            budget_digest=row["current_budget_digest"],
            model_configuration_digest=row["current_model_configuration_digest"],
        )

    @classmethod
    def _approved_revision_bindings_in_transaction(
        cls, connection: sqlite3.Connection, run_id: RunId
    ) -> ApplicableRevisionDigests:
        current = cls._current_revision_digests_in_transaction(connection, run_id)
        approved = {
            str(row["revision_class"])
            for row in connection.execute(
                "SELECT revision_class FROM revision_approvals WHERE run_id = ? "
                "AND ((revision_class = 'PLAN' AND revision_digest = ?) "
                "OR (revision_class = 'POLICY' AND revision_digest = ?) "
                "OR (revision_class = 'BUDGET' AND revision_digest = ?) "
                "OR (revision_class = 'MODEL_CONFIGURATION' AND revision_digest = ?))",
                (
                    run_id,
                    current.plan_digest,
                    current.policy_digest,
                    current.budget_digest,
                    current.model_configuration_digest,
                ),
            )
        }
        return ApplicableRevisionDigests(
            plan_digest=current.plan_digest if "PLAN" in approved else None,
            policy_digest=current.policy_digest if "POLICY" in approved else None,
            budget_digest=current.budget_digest if "BUDGET" in approved else None,
            model_configuration_digest=(
                current.model_configuration_digest if "MODEL_CONFIGURATION" in approved else None
            ),
        )

    @staticmethod
    def _target_authority_digest_in_transaction(
        connection: sqlite3.Connection, run_id: RunId
    ) -> Sha256DigestText:
        row = connection.execute(
            "SELECT runs.repository_id, runs.repository_instance_digest, runs.target_ref, "
            "runs.pinned_target_oid, target_reservations.reservation_id, "
            "target_reservations.path, target_reservations.pinned_target_oid AS reservation_oid, "
            "target_reservations.admin_binding_digest FROM runs JOIN target_reservations "
            "ON target_reservations.run_id = runs.run_id WHERE runs.run_id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            raise StateConflict("RUN_OR_TARGET_RESERVATION_NOT_FOUND")
        return sha256_digest(
            canonical_json(
                {
                    "pinned_target_oid": row["pinned_target_oid"],
                    "repository_id": row["repository_id"],
                    "repository_instance_digest": row["repository_instance_digest"],
                    "reservation_id": row["reservation_id"],
                    "reservation_path": row["path"],
                    "reservation_pinned_target_oid": row["reservation_oid"],
                    "target_ref": row["target_ref"],
                    "target_safety_digest": row["admin_binding_digest"],
                }
            )
        )

    def target_authority_digest(self, run_id: RunId) -> Sha256DigestText:
        with self._read_transaction() as connection:
            return self._target_authority_digest_in_transaction(connection, run_id)

    def current_revision_digests(self, run_id: RunId) -> ApplicableRevisionDigests:
        with self._read_transaction() as connection:
            return self._current_revision_digests_in_transaction(connection, run_id)

    def approved_revision_classes(self, run_id: RunId) -> tuple[str, ...]:
        with self._read_transaction() as connection:
            current = self._current_revision_digests_in_transaction(connection, run_id)
            rows = connection.execute(
                "SELECT revision_class, revision_digest FROM revision_approvals WHERE run_id = ?",
                (run_id,),
            ).fetchall()
        current_by_class = {
            "PLAN": current.plan_digest,
            "POLICY": current.policy_digest,
            "BUDGET": current.budget_digest,
            "MODEL_CONFIGURATION": current.model_configuration_digest,
        }
        present = {
            str(row["revision_class"])
            for row in rows
            if current_by_class[str(row["revision_class"])] == row["revision_digest"]
        }
        return tuple(
            item for item in ("PLAN", "POLICY", "BUDGET", "MODEL_CONFIGURATION") if item in present
        )

    def current_budget_digest(self, run_id: RunId) -> RevisionDigest | None:
        return self.current_revision_digests(run_id).budget_digest

    def current_model_configuration_digest(self, run_id: RunId) -> RevisionDigest | None:
        return self.current_revision_digests(run_id).model_configuration_digest

    def pending_budget_replacement(self, run_id: RunId) -> RevisionDigest | None:
        with self._read_transaction() as connection:
            row = connection.execute(
                "SELECT revision_digest FROM pending_revision_replacements "
                "WHERE run_id = ? AND revision_class = 'BUDGET'",
                (run_id,),
            ).fetchone()
        return None if row is None else RevisionDigest(row["revision_digest"])

    def run_count(self) -> int:
        with self._read_transaction() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0])

    def target_reservation_count(self, run_id: RunId) -> int:
        with self._read_transaction() as connection:
            return int(
                connection.execute(
                    "SELECT COUNT(*) FROM target_reservations WHERE run_id = ?", (run_id,)
                ).fetchone()[0]
            )

    def public_run_snapshot(
        self, run_id: RunId, at_sequence: int | None
    ) -> PublicRunSnapshot | None:
        with self._read_transaction() as connection:
            row = connection.execute(
                "SELECT runs.state, run_sequences.current_sequence FROM runs "
                "JOIN run_sequences USING(run_id) WHERE runs.run_id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                return None
            current = int(row["current_sequence"])
            requested = current if at_sequence is None else at_sequence
            if requested < 0 or requested > current:
                return None
            if requested != current:
                exists = connection.execute(
                    "SELECT 1 FROM audit_events WHERE run_id = ? AND sequence = ?",
                    (run_id, requested),
                ).fetchone()
                if exists is None:
                    return None
            return PublicRunSnapshot(AuditSequence(requested), RunState(row["state"]))

    @staticmethod
    def _runtime_permit_from_row(row: sqlite3.Row) -> RuntimePermit:
        return RuntimePermit(
            run_id=RunId(row["run_id"]),
            generation=int(row["generation"]),
            source_request_id=RequestId(row["source_request_id"]),
            source_envelope_digest=Sha256DigestText(row["source_envelope_digest"]),
            issued_sequence=AuditSequence(row["issued_sequence"]),
            allowed_phase=row["allowed_phase"],
            applicable_revision_digests=applicable_revision_digests_from_json(
                str(row["applicable_revision_digests_json"])
            ),
            target_authority_digest=Sha256DigestText(row["target_authority_digest"]),
            expected_runtime_progress_generation=int(row["expected_runtime_progress_generation"]),
            state=row["state"],
            consumed_owner_id=(
                None
                if row["consumed_owner_id"] is None
                else RuntimeOwnerId(row["consumed_owner_id"])
            ),
            consumed_sequence=(
                None
                if row["consumed_sequence"] is None
                else AuditSequence(row["consumed_sequence"])
            ),
        )

    def runtime_permit(self, run_id: RunId, generation: int) -> RuntimePermit:
        with self._read_transaction() as connection:
            row = connection.execute(
                "SELECT * FROM runtime_permits WHERE run_id = ? AND generation = ?",
                (run_id, generation),
            ).fetchone()
        if row is None:
            raise StateConflict("RUNTIME_PERMIT_NOT_FOUND")
        return self._runtime_permit_from_row(row)

    def unconsumed_permit(self, run_id: RunId) -> RuntimePermit:
        with self._read_transaction() as connection:
            row = connection.execute(
                "SELECT * FROM runtime_permits WHERE run_id = ? AND state = 'UNCONSUMED'",
                (run_id,),
            ).fetchone()
        if row is None:
            raise StateConflict("RUNTIME_PERMIT_NOT_FOUND")
        return self._runtime_permit_from_row(row)

    def _issue_runtime_permit_in_transaction(
        self,
        connection: sqlite3.Connection,
        command: CommandEnvelope,
        allowed_phase: RuntimeAllowedPhase,
        applicable_revision_digests: ApplicableRevisionDigests,
        target_authority_digest: Sha256DigestText,
        issued_sequence: AuditSequence,
    ) -> RuntimePermit:
        if isinstance(command.payload, CreateRunPayload):
            raise TypeError("runtime Permit source must identify a Run")
        run_id = RunId(command.payload.run_id)
        current = self._current_revision_digests_in_transaction(connection, run_id)
        current_target = self._target_authority_digest_in_transaction(connection, run_id)
        row = connection.execute(
            "SELECT state, runtime_progress_generation, runtime_owner_id FROM runs "
            "WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if (
            row is None
            or row["state"] != allowed_phase
            or current != applicable_revision_digests
            or current_target != target_authority_digest
        ):
            raise StateConflict("RUNTIME_PERMIT_BINDING_MISMATCH")
        if row["runtime_owner_id"] is not None:
            raise StateConflict("RUNTIME_DELIVERY_PENDING")
        generation = int(
            connection.execute(
                "SELECT COALESCE(MAX(generation), 0) + 1 FROM runtime_permits WHERE run_id = ?",
                (run_id,),
            ).fetchone()[0]
        )
        permit = RuntimePermit(
            run_id=run_id,
            generation=generation,
            source_request_id=RequestId(command.request_id),
            source_envelope_digest=Sha256DigestText(_command_digest(command)),
            issued_sequence=issued_sequence,
            allowed_phase=allowed_phase,
            applicable_revision_digests=applicable_revision_digests,
            target_authority_digest=target_authority_digest,
            expected_runtime_progress_generation=int(row["runtime_progress_generation"]),
            state="UNCONSUMED",
        )
        try:
            connection.execute(
                "INSERT INTO runtime_permits(run_id, generation, source_request_id, "
                "source_envelope_digest, issued_sequence, allowed_phase, "
                "applicable_revision_digests_json, target_authority_digest, "
                "expected_runtime_progress_generation, state) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'UNCONSUMED')",
                (
                    permit.run_id,
                    permit.generation,
                    permit.source_request_id,
                    permit.source_envelope_digest,
                    permit.issued_sequence,
                    permit.allowed_phase,
                    applicable_revision_digests_to_json(permit.applicable_revision_digests),
                    permit.target_authority_digest,
                    permit.expected_runtime_progress_generation,
                ),
            )
        except sqlite3.IntegrityError as error:
            raise StateConflict("RUNTIME_DELIVERY_PENDING") from error
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
        run_id = RunId(command.payload.run_id)
        issued: list[RuntimePermit] = []

        def mutate(connection: sqlite3.Connection) -> None:
            row = connection.execute(
                "SELECT envelope_digest, outcome_json FROM command_receipts WHERE request_id = ?",
                (command.request_id,),
            ).fetchone()
            if row is None or row["envelope_digest"] != _command_digest(command):
                raise StateConflict("RUNTIME_PERMIT_SOURCE_COMMAND_NOT_ACCEPTED")
            outcome = CommandOutcome.validate_for_payload(
                command.payload, _json_object(row["outcome_json"])
            )
            if outcome.status != CommandStatus.ACCEPTED:
                raise StateConflict("RUNTIME_PERMIT_SOURCE_COMMAND_NOT_ACCEPTED")
            issued.append(
                self._issue_runtime_permit_in_transaction(
                    connection,
                    command,
                    allowed_phase,
                    applicable_revision_digests,
                    target_authority_digest,
                    AuditSequence(expected_sequence + 1),
                )
            )

        self._commit_state_and_event(
            run_id=run_id,
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
        consumed: list[RuntimePermit] = []
        event_kinds: list[str] = []
        opened_at: list[MonotonicInstant] = []

        def mutate(connection: sqlite3.Connection) -> None:
            row = connection.execute(
                "SELECT runtime_permits.*, runs.state AS run_state, "
                "runs.runtime_progress_generation, runs.runtime_owner_id, "
                "runs.runtime_owner_generation, runs.runtime_interval_owner_generation, "
                "runs.runtime_interval_opened_nanoseconds "
                "FROM runtime_permits JOIN runs USING(run_id) WHERE run_id = ? "
                "AND runtime_permits.state = 'UNCONSUMED'",
                (run_id,),
            ).fetchone()
            if row is None:
                raise StateConflict("RUNTIME_PERMIT_NOT_FOUND")
            permit = self._runtime_permit_from_row(row)
            ordinary_phase_matches = permit.allowed_phase == row["run_state"]
            terminal_matches = permit.allowed_phase == "TERMINAL_ADMINISTRATION" and row[
                "run_state"
            ] in {"COMPLETED", "FAILED", "CANCELLED"}
            bindings_match = (
                permit.applicable_revision_digests
                == self._current_revision_digests_in_transaction(connection, run_id)
                and permit.target_authority_digest
                == self._target_authority_digest_in_transaction(connection, run_id)
                and permit.expected_runtime_progress_generation
                == int(row["runtime_progress_generation"])
            )
            if not (ordinary_phase_matches or terminal_matches) or not bindings_match:
                if (
                    connection.execute(
                        "UPDATE runtime_permits SET state = 'INVALIDATED' "
                        "WHERE run_id = ? AND generation = ? AND state = 'UNCONSUMED'",
                        (run_id, permit.generation),
                    ).rowcount
                    != 1
                ):
                    raise StateConflict("RUNTIME_PERMIT_CONSUME_COMPARE_AND_SET_FAILED")
                event_kinds.append("RUNTIME_PERMIT_INVALIDATED")
                return
            if row["runtime_owner_id"] is not None:
                raise StateConflict("RUNTIME_DELIVERY_PENDING")
            if (
                row["runtime_interval_owner_generation"] is not None
                or row["runtime_interval_opened_nanoseconds"] is not None
            ):
                raise StateConflict("RUNTIME_DELIVERY_PENDING")
            if self._monotonic_clock is None:
                raise StateConflict("MONOTONIC_CLOCK_NOT_CONFIGURED")
            now = self._monotonic_clock.now()
            opened_at.append(now)
            owner_generation = int(row["runtime_owner_generation"]) + 1
            consumed_sequence = AuditSequence(expected_sequence + 1)
            if (
                connection.execute(
                    "UPDATE runtime_permits SET state = 'CONSUMED', consumed_owner_id = ?, "
                    "consumed_sequence = ? WHERE run_id = ? AND generation = ? "
                    "AND state = 'UNCONSUMED'",
                    (owner_id, consumed_sequence, run_id, permit.generation),
                ).rowcount
                != 1
            ):
                raise StateConflict("RUNTIME_PERMIT_CONSUME_COMPARE_AND_SET_FAILED")
            if (
                connection.execute(
                    "UPDATE runs SET runtime_owner_id = ?, runtime_owner_generation = ?, "
                    "runtime_progress_generation = runtime_progress_generation + 1, "
                    "runtime_interval_owner_generation = ?, "
                    "runtime_interval_opened_nanoseconds = ? WHERE run_id = ? "
                    "AND runtime_owner_id IS NULL AND runtime_progress_generation = ? "
                    "AND runtime_interval_owner_generation IS NULL "
                    "AND runtime_interval_opened_nanoseconds IS NULL",
                    (
                        owner_id,
                        owner_generation,
                        owner_generation,
                        now.nanoseconds,
                        run_id,
                        permit.expected_runtime_progress_generation,
                    ),
                ).rowcount
                != 1
            ):
                raise StateConflict("RUNTIME_OWNER_COMPARE_AND_SET_FAILED")
            consumed.append(
                permit.model_copy(
                    update={
                        "state": "CONSUMED",
                        "consumed_owner_id": owner_id,
                        "consumed_sequence": consumed_sequence,
                    }
                )
            )
            event_kinds.append("RUNTIME_PERMIT_CONSUMED")

        try:
            self._commit_state_and_events(
                run_id=run_id,
                expected_sequence=expected_sequence,
                event_factory=lambda: (AuditEvent.kind(event_kinds[0]),),
                mutate=mutate,
                runtime_now_factory=lambda: opened_at[0] if opened_at else None,
            )
        except StateConflict as error:
            if str(error) == "RUNTIME_PERMIT_NOT_FOUND":
                return None
            raise
        return consumed[0] if consumed else None

    def _existing_control_outcome(self, command: CommandEnvelope) -> CommandOutcome | None:
        with self._read_transaction() as connection:
            return self._existing_control_outcome_in_transaction(connection, command)

    def _existing_control_outcome_in_transaction(
        self, connection: sqlite3.Connection, command: CommandEnvelope
    ) -> CommandOutcome | None:
        row = connection.execute(
            "SELECT envelope_digest, outcome_json FROM control_request_claims WHERE request_id = ?",
            (command.request_id,),
        ).fetchone()
        if row is None:
            return None
        if row["envelope_digest"] == _command_digest(command):
            return CommandOutcome.validate_for_payload(
                command.payload, _json_object(row["outcome_json"])
            )
        stored_run_id, stored_sequence = _stored_command_outcome_identity(row["outcome_json"])
        return CommandOutcome.for_payload(
            command.payload,
            status=CommandStatus.CONFLICT,
            run_id=stored_run_id,
            resulting_sequence=stored_sequence,
            failed_invariant="IDEMPOTENCY_KEY_REUSE",
        )

    def _claim_control_request_in_transaction(
        self,
        connection: sqlite3.Connection,
        command: CommandEnvelope,
        outcome: CommandOutcome,
    ) -> None:
        existing = self._existing_control_outcome_in_transaction(connection, command)
        if existing is not None:
            raise _ControlRequestClaimed(existing)
        connection.execute(
            "INSERT INTO control_request_claims(request_id, envelope_digest, outcome_json) "
            "VALUES (?, ?, ?)",
            (
                command.request_id,
                _command_digest(command),
                canonical_json(outcome.model_dump(mode="json")),
            ),
        )

    def _record_unsequenced_control_outcome(
        self,
        connection: sqlite3.Connection,
        command: CommandEnvelope,
        outcome: CommandOutcome,
    ) -> CommandOutcome:
        existing = self._existing_control_outcome_in_transaction(connection, command)
        if existing is not None:
            return existing
        self._claim_control_request_in_transaction(connection, command, outcome)
        return outcome

    def _record_bootstrap_conflict_receipt(
        self,
        connection: sqlite3.Connection,
        command: CommandEnvelope,
        failed_invariant: str,
    ) -> CommandOutcome:
        outcome = CommandOutcome.for_payload(
            command.payload,
            status=CommandStatus.CONFLICT,
            run_id=None,
            resulting_sequence=None,
            failed_invariant=failed_invariant,
        )
        return self._record_unsequenced_control_outcome(connection, command, outcome)

    def _insert_control_receipt(
        self,
        connection: sqlite3.Connection,
        command: CommandEnvelope,
        run_id: RunId,
        repository_id: RepositoryId,
        outcome: CommandOutcome,
        *,
        claim: bool = True,
    ) -> None:
        assert outcome.resulting_sequence is not None
        if claim:
            self._claim_control_request_in_transaction(connection, command, outcome)
        connection.execute(
            "INSERT INTO command_receipts(request_id, repository_id, run_id, "
            "envelope_digest, outcome_json, resulting_sequence) VALUES (?, ?, ?, ?, ?, ?)",
            (
                command.request_id,
                repository_id,
                run_id,
                _command_digest(command),
                canonical_json(outcome.model_dump(mode="json")),
                outcome.resulting_sequence,
            ),
        )

    def _record_control_outcome(
        self,
        command: CommandEnvelope,
        run_id: RunId,
        status: CommandStatus,
        failed_invariant: str | None,
        event_kind: str,
        mutate_domain: Callable[[sqlite3.Connection], None] | None = None,
    ) -> CommandOutcome:
        if command.expected_sequence is None:
            raise StateConflict("EXPECTED_SEQUENCE_REQUIRED")
        expected = AuditSequence(command.expected_sequence)
        with self._transaction("IMMEDIATE") as connection:
            existing = self._existing_control_outcome_in_transaction(connection, command)
            if existing is not None:
                return existing
            run = connection.execute(
                "SELECT repository_id FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if run is None:
                missing = CommandOutcome.for_payload(
                    command.payload,
                    status=CommandStatus.INVALID,
                    run_id=run_id,
                    resulting_sequence=None,
                    failed_invariant="RUN_NOT_FOUND",
                )
                self._claim_control_request_in_transaction(connection, command, missing)
                return missing
            sequence_row = connection.execute(
                "SELECT current_sequence FROM run_sequences WHERE run_id = ?", (run_id,)
            ).fetchone()
            current = AuditSequence(0 if sequence_row is None else sequence_row["current_sequence"])
            if current != expected:
                stale = CommandOutcome.for_payload(
                    command.payload,
                    status=CommandStatus.STALE,
                    run_id=run_id,
                    resulting_sequence=current,
                    failed_invariant="STALE_SEQUENCE",
                )
                self._claim_control_request_in_transaction(connection, command, stale)
                return stale
            outcome = CommandOutcome.for_payload(
                command.payload,
                status=status,
                run_id=run_id,
                resulting_sequence=AuditSequence(expected + 1),
                failed_invariant=failed_invariant,
            )
            self._claim_control_request_in_transaction(connection, command, outcome)
            if mutate_domain is not None:
                mutate_domain(connection)
            self._insert_control_receipt(
                connection,
                command,
                run_id,
                RepositoryId(run["repository_id"]),
                outcome,
                claim=False,
            )
            if self._fail_next_commit_after_state_write:
                self._fail_next_commit_after_state_write = False
                raise StateCommitFault("TEST_FAULT_AFTER_STATE_WRITE")
            self._append_audit_event(
                connection,
                run_id,
                AuditEvent.kind(
                    event_kind,
                    applicable_revision_digests=command.applicable_revision_digests,
                    result_class=status,
                ),
                expected,
            )
            return outcome

    def create_bootstrap_run(
        self,
        command: CommandEnvelope,
        repository_authority: RepositoryBootstrapAuthorityService,
    ) -> CommandOutcome:
        existing = self._existing_control_outcome(command)
        if existing is not None:
            return existing
        outcome = self._create_bootstrap_run_unchecked(command, repository_authority)
        if outcome.status == CommandStatus.ACCEPTED:
            return outcome
        with self._transaction("IMMEDIATE") as connection:
            return self._record_unsequenced_control_outcome(connection, command, outcome)

    def _create_bootstrap_run_unchecked(
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
        priced = {entry.returned_model_id for entry in payload.budget_revision.pricing_entries}
        returned = {
            alias.returned_model_id
            for alias in payload.model_configuration_revision.returned_model_aliases
        }
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
        revisions = {
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

        def mutate(connection: sqlite3.Connection, reservation_id: str) -> None:
            reservation_path = self._data_root / "reservations" / reservation_id
            connection.execute(
                "INSERT INTO runs(run_id, repository_id, repository_instance_digest, state, "
                "target_ref, pinned_target_oid, current_policy_digest, current_budget_digest, "
                "current_model_configuration_digest) VALUES (?, ?, ?, 'DRAFT', ?, ?, ?, ?, ?)",
                (
                    run_id,
                    repository_id,
                    repository_instance_digest,
                    payload.target_ref,
                    payload.expected_target_oid,
                    digests["POLICY"],
                    digests["BUDGET"],
                    digests["MODEL_CONFIGURATION"],
                ),
            )
            connection.execute(
                "INSERT INTO target_reservations(reservation_id, run_id, target_ref, "
                "pinned_target_oid, path, phase) VALUES (?, ?, ?, ?, ?, 'ALLOCATED')",
                (
                    reservation_id,
                    run_id,
                    payload.target_ref,
                    payload.expected_target_oid,
                    str(reservation_path),
                ),
            )
            connection.execute(
                "INSERT INTO run_bootstrap_inputs(run_id, goal_json, constraints_json, "
                "acceptance_json) VALUES (?, ?, ?, ?)",
                (
                    run_id,
                    canonical_json({"goal": payload.goal}),
                    json.dumps(payload.constraints, separators=(",", ":")),
                    json.dumps(payload.acceptance_criteria, separators=(",", ":")),
                ),
            )
            for revision_class, document in revisions.items():
                connection.execute(
                    "INSERT INTO revision_documents(run_id, revision_class, revision_digest, "
                    "document_json, proposed_sequence, state) VALUES (?, ?, ?, ?, 1, 'CURRENT')",
                    (
                        run_id,
                        revision_class,
                        digests[revision_class],
                        _revision_json(document),
                    ),
                )
            self._insert_control_receipt(
                connection,
                command,
                run_id,
                repository_id,
                outcome,
                claim=False,
            )

        with self._transaction("IMMEDIATE") as connection:
            existing = self._existing_control_outcome_in_transaction(connection, command)
            if existing is not None:
                return existing
            try:
                reservation_id = allocate_target_reservation_id(
                    self._target_reservation_id_source,
                    lambda candidate: (
                        connection.execute(
                            "SELECT 1 FROM target_reservations WHERE reservation_id = ?",
                            (candidate,),
                        ).fetchone()
                        is not None
                    ),
                )
            except TargetReservationIdAllocationError as error:
                return self._record_bootstrap_conflict_receipt(
                    connection, command, error.failed_invariant
                )
            self._claim_control_request_in_transaction(connection, command, outcome)
            self._require_expected_sequence(connection, run_id, AuditSequence(0))
            mutate(connection, reservation_id)
            if self._fail_next_commit_after_state_write:
                self._fail_next_commit_after_state_write = False
                raise StateCommitFault("TEST_FAULT_AFTER_STATE_WRITE")
            self._append_audit_event(
                connection,
                run_id,
                AuditEvent.kind("RUN_CREATED"),
                AuditSequence(0),
            )
        return outcome

    def _run_state_and_sequence(self, run_id: RunId) -> tuple[RunState, AuditSequence]:
        with self._read_transaction() as connection:
            row = connection.execute(
                "SELECT runs.state, run_sequences.current_sequence FROM runs "
                "JOIN run_sequences USING(run_id) WHERE runs.run_id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            raise StateConflict("RUN_NOT_FOUND")
        return RunState(row["state"]), AuditSequence(row["current_sequence"])

    def propose_revision(
        self, command: CommandEnvelope, run_id: RunId, state: RunState
    ) -> CommandOutcome:
        expected_sequence = command.expected_sequence
        if expected_sequence is None:
            raise StateConflict("EXPECTED_SEQUENCE_REQUIRED")
        payload = command.payload
        mapping: tuple[str, FrozenDocument, str]
        if isinstance(payload, ProposePolicyPayload):
            mapping = ("POLICY", payload.policy_revision, "current_policy_digest")
        elif isinstance(payload, ProposeBudgetPayload):
            mapping = ("BUDGET", payload.budget_revision, "current_budget_digest")
        elif isinstance(payload, ProposeModelConfigurationPayload):
            mapping = (
                "MODEL_CONFIGURATION",
                payload.model_configuration_revision,
                "current_model_configuration_digest",
            )
        else:
            raise TypeError("revision proposal required")
        revision_class, document, run_column = mapping
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
        model_is_priced = True
        with self._read_transaction() as connection:
            required_bindings = (
                self._current_revision_digests_in_transaction(connection, run_id)
                if state in _EXECUTION_REVISION_STATES
                else self._approved_revision_bindings_in_transaction(connection, run_id)
            )
            if isinstance(payload, ProposeModelConfigurationPayload):
                budget_digest = self._current_revision_digests_in_transaction(
                    connection, run_id
                ).budget_digest
                budget_row = connection.execute(
                    "SELECT document_json FROM revision_documents WHERE run_id = ? "
                    "AND revision_class = 'BUDGET' AND revision_digest = ?",
                    (run_id, budget_digest),
                ).fetchone()
                if budget_row is None:
                    raise StateConflict("CURRENT_BUDGET_NOT_FOUND")
                budget = BudgetRevisionDocument.model_validate_json(budget_row["document_json"])
                priced = {entry.returned_model_id for entry in budget.pricing_entries}
                returned = {
                    alias.returned_model_id
                    for alias in payload.model_configuration_revision.returned_model_aliases
                }
                model_is_priced = returned.issubset(priced)
        if not model_is_priced:
            return self._record_control_outcome(
                command,
                run_id,
                CommandStatus.INVALID,
                "MODEL_CONFIGURATION_UNPRICED",
                "CONTROL_COMMAND_REJECTED",
            )
        if command.applicable_revision_digests != required_bindings:
            return self._record_control_outcome(
                command,
                run_id,
                CommandStatus.STALE,
                "REVISION_BINDING_MISMATCH",
                "CONTROL_COMMAND_REJECTED",
            )
        digest = revision_digest(document)

        def mutate(connection: sqlite3.Connection) -> None:
            current = self._current_revision_digests_in_transaction(connection, run_id)
            current_digest = {
                "POLICY": current.policy_digest,
                "BUDGET": current.budget_digest,
                "MODEL_CONFIGURATION": current.model_configuration_digest,
            }[revision_class]
            if digest == current_digest:
                return
            connection.execute(
                "INSERT INTO revision_documents(run_id, revision_class, revision_digest, "
                "document_json, proposed_sequence, state) VALUES (?, ?, ?, ?, ?, 'PROPOSED') "
                "ON CONFLICT(run_id, revision_class, revision_digest) DO UPDATE SET "
                "document_json = excluded.document_json, proposed_sequence = "
                "excluded.proposed_sequence, state = 'PROPOSED'",
                (
                    run_id,
                    revision_class,
                    digest,
                    _revision_json(document),
                    expected_sequence + 1,
                ),
            )
            if state not in _EXECUTION_REVISION_STATES:
                connection.execute(
                    "UPDATE revision_documents SET state = 'STALE' WHERE run_id = ? "
                    "AND revision_class = ? AND revision_digest <> ?",
                    (run_id, revision_class, digest),
                )
                connection.execute(
                    "UPDATE revision_documents SET state = 'CURRENT' WHERE run_id = ? "
                    "AND revision_class = ? AND revision_digest = ?",
                    (run_id, revision_class, digest),
                )
                connection.execute(
                    f"UPDATE runs SET {run_column} = ?, state = 'DRAFT', "
                    "current_plan_digest = NULL WHERE run_id = ?",
                    (digest, run_id),
                )
                connection.execute(
                    "DELETE FROM revision_approvals WHERE run_id = ? "
                    "AND revision_class IN (?, 'PLAN')",
                    (run_id, revision_class),
                )
                connection.execute(
                    "UPDATE runtime_permits SET state = 'INVALIDATED' WHERE run_id = ? "
                    "AND state = 'UNCONSUMED'",
                    (run_id,),
                )

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
            revision_class = "POLICY"
            digest = payload.policy_digest
            code = payload.confirmation_code
            run_column = "current_policy_digest"
        elif isinstance(payload, ApproveBudgetPayload):
            revision_class = "BUDGET"
            digest = payload.budget_digest
            code = payload.confirmation_code
            run_column = "current_budget_digest"
        elif isinstance(payload, ApproveModelConfigurationPayload):
            revision_class = "MODEL_CONFIGURATION"
            digest = payload.model_configuration_digest
            code = payload.confirmation_code
            run_column = "current_model_configuration_digest"
        elif isinstance(payload, ApprovePlanPayload):
            revision_class = "PLAN"
            digest = payload.plan_digest
            code = payload.confirmation_code
            run_column = "current_plan_digest"
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
        with self._read_transaction() as connection:
            required_bindings = (
                self._current_revision_digests_in_transaction(connection, run_id)
                if state in _EXECUTION_REVISION_STATES
                else self._approved_revision_bindings_in_transaction(connection, run_id)
            )
            document = connection.execute(
                "SELECT document_json, state FROM revision_documents WHERE run_id = ? "
                "AND revision_class = ? AND revision_digest = ?",
                (run_id, revision_class, digest),
            ).fetchone()
        if command.applicable_revision_digests != required_bindings:
            return self._record_control_outcome(
                command,
                run_id,
                CommandStatus.STALE,
                "REVISION_BINDING_MISMATCH",
                "CONTROL_COMMAND_REJECTED",
            )
        if document is None or document["state"] == "STALE":
            return self._record_control_outcome(
                command,
                run_id,
                CommandStatus.STALE,
                "REVISION_PROPOSAL_NOT_CURRENT",
                "CONTROL_COMMAND_REJECTED",
            )

        def mutate(connection: sqlite3.Connection) -> None:
            current = self._current_revision_digests_in_transaction(connection, run_id)
            current_digest = {
                "PLAN": current.plan_digest,
                "POLICY": current.policy_digest,
                "BUDGET": current.budget_digest,
                "MODEL_CONFIGURATION": current.model_configuration_digest,
            }[revision_class]
            existing = connection.execute(
                "SELECT 1 FROM revision_approvals WHERE run_id = ? AND revision_class = ? "
                "AND revision_digest = ?",
                (run_id, revision_class, digest),
            ).fetchone()
            if existing is None:
                connection.execute(
                    "INSERT INTO revision_approvals(run_id, revision_class, revision_digest, "
                    "approval_request_id, approval_sequence, display_digest) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        run_id,
                        revision_class,
                        digest,
                        command.request_id,
                        expected_sequence + 1,
                        expected_code,
                    ),
                )
            in_flight = connection.execute(
                "SELECT 1 FROM atomic_actions WHERE run_id = ? AND state = 'IN_FLIGHT' LIMIT 1",
                (run_id,),
            ).fetchone()
            if (
                state in _EXECUTION_REVISION_STATES
                and current_digest != digest
                and in_flight is not None
            ):
                connection.execute(
                    "INSERT INTO pending_revision_replacements(run_id, revision_class, "
                    "revision_digest, requested_sequence) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(run_id, revision_class) DO UPDATE SET "
                    "revision_digest = excluded.revision_digest, "
                    "requested_sequence = excluded.requested_sequence",
                    (run_id, revision_class, digest, expected_sequence + 1),
                )
                dispatch = connection.execute(
                    "SELECT dispatch_close_causes_json FROM runs WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
                if dispatch is None:
                    raise StateConflict("RUN_NOT_FOUND")
                causes = dispatch_close_causes_from_json(
                    str(dispatch["dispatch_close_causes_json"])
                ) | {DispatchCloseCause.REVISION_REPLACEMENT}
                connection.execute(
                    "UPDATE runs SET new_dispatch_open = 0, dispatch_close_causes_json = ? "
                    "WHERE run_id = ?",
                    (dispatch_close_causes_to_json(causes), run_id),
                )
                return
            if current_digest != digest:
                connection.execute(
                    "UPDATE revision_documents SET state = 'STALE' WHERE run_id = ? "
                    "AND revision_class = ? AND state = 'CURRENT'",
                    (run_id, revision_class),
                )
                connection.execute(
                    "UPDATE revision_documents SET state = 'CURRENT' WHERE run_id = ? "
                    "AND revision_class = ? AND revision_digest = ?",
                    (run_id, revision_class, digest),
                )
                connection.execute(
                    f"UPDATE runs SET {run_column} = ? WHERE run_id = ?",
                    (digest, run_id),
                )
            if revision_class == "BUDGET":
                connection.execute(
                    "INSERT INTO approved_budgets_for_test(run_id, budget_digest, budget_json) "
                    "VALUES (?, ?, ?) ON CONFLICT(run_id) DO UPDATE SET "
                    "budget_digest = excluded.budget_digest, budget_json = excluded.budget_json",
                    (run_id, digest, document["document_json"]),
                )
            connection.execute(
                "DELETE FROM pending_revision_replacements WHERE run_id = ? AND revision_class = ?",
                (run_id, revision_class),
            )

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
        with self._read_transaction() as connection:
            current = self._current_revision_digests_in_transaction(connection, run_id)
            approved = self._approved_revision_bindings_in_transaction(connection, run_id)
            current_target = self._target_authority_digest_in_transaction(connection, run_id)
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
        approved_required = (
            approved.policy_digest,
            approved.budget_digest,
            approved.model_configuration_digest,
        )
        if any(item is None for item in required) or approved_required != required:
            return self._record_control_outcome(
                command,
                run_id,
                CommandStatus.DENIED,
                "BOOTSTRAP_REVISIONS_NOT_APPROVED",
                "CONTROL_COMMAND_REJECTED",
            )
        external_target = target_authority.current_for(run_id)
        if external_target != current_target:
            return self._record_control_outcome(
                command,
                run_id,
                CommandStatus.STALE,
                "TARGET_AUTHORITY_BINDING_MISMATCH",
                "CONTROL_COMMAND_REJECTED",
            )

        def mutate(connection: sqlite3.Connection) -> None:
            if self._target_authority_digest_in_transaction(connection, run_id) != current_target:
                raise StateConflict("TARGET_AUTHORITY_BINDING_MISMATCH")
            self._issue_runtime_permit_in_transaction(
                connection,
                command,
                "DRAFT",
                current,
                current_target,
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
        request = ResumeTaskRequest(
            run_id=run_id,
            task_id=task_id,
            pause_sequence=payload.pause_sequence,
            pause_reason=payload.pause_reason,
            applicable_revision_digests=command.applicable_revision_digests,
            expected_sequence=AuditSequence(expected_sequence),
        )
        with self._read_transaction() as connection:
            current = self._current_revision_digests_in_transaction(connection, run_id)
            pause = self._read_current_task_pause(connection, run_id, task_id)
            counters = self._task_counters_in_transaction(connection, run_id, task_id)
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
        if payload.pause_reason not in {
            "NO_PROGRESS",
            "REPEATED_CHECKPOINT",
            "REPEATED_INVALID_ACTION",
        }:
            return self._record_control_outcome(
                command,
                run_id,
                CommandStatus.DENIED,
                "TASK_PAUSE_NOT_RESUMABLE",
                "CONTROL_COMMAND_REJECTED",
            )
        remaining = V01_MECHANISM_LIMITS.task_call_ceiling - counters.allocated_calls
        calls = min(V01_MECHANISM_LIMITS.renewal_tranche_calls, remaining)
        if calls < 1 or counters.manual_resumes >= V01_MECHANISM_LIMITS.manual_resume_ceiling:
            return self._record_control_outcome(
                command,
                run_id,
                CommandStatus.DENIED,
                "NON_RAISEABLE_CAP_REACHED",
                "CONTROL_COMMAND_REJECTED",
            )
        budget_digest = current.budget_digest
        if budget_digest is None:
            raise StateConflict("CURRENT_BUDGET_NOT_FOUND")
        allocation_id, new_attempt_id = task_resume_ids(
            request, pause, counters, budget_digest, calls
        )
        target_digest = self.target_authority_digest(run_id)

        def mutate(connection: sqlite3.Connection) -> None:
            if self._read_current_task_pause(connection, run_id, task_id) != pause:
                raise StateConflict("TASK_RESUME_COMPARE_AND_SET_FAILED")
            current_budget = self._task_budget_state(connection, run_id, task_id)
            self._write_task_budget_state(
                connection,
                replace(current_budget, manual_resumes=current_budget.manual_resumes + 1),
            )
            connection.execute(
                "INSERT INTO task_resume_allocations(allocation_id, run_id, task_id, "
                "reserved_attempt_id, budget_digest, applicable_revision_digests_json, "
                "allocated_calls, state, created_sequence) VALUES (?, ?, ?, ?, ?, ?, ?, "
                "'RESERVED', ?)",
                (
                    allocation_id,
                    run_id,
                    task_id,
                    new_attempt_id,
                    budget_digest,
                    applicable_revision_digests_to_json(current),
                    calls,
                    expected_sequence + 1,
                ),
            )
            if (
                connection.execute(
                    "UPDATE tasks SET state = 'READY', pause_reason = NULL, pause_counter = NULL "
                    "WHERE run_id = ? AND task_id = ? AND state = 'PAUSED'",
                    (run_id, task_id),
                ).rowcount
                != 1
                or connection.execute(
                    "UPDATE task_pauses SET active = 0 WHERE run_id = ? AND task_id = ? "
                    "AND pause_sequence = ? AND active = 1",
                    (run_id, task_id, pause.pause_sequence),
                ).rowcount
                != 1
            ):
                raise StateConflict("TASK_RESUME_COMPARE_AND_SET_FAILED")
            self._resolve_dispatch_close_cause_after_exact_resume(
                connection, run_id, DispatchCloseCause.TASK_PAUSED
            )
            self._issue_runtime_permit_in_transaction(
                connection,
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
        try:
            proposal = self.plan_proposal(run_id, payload.plan_digest)
        except StateConflict:
            return self._record_control_outcome(
                command,
                run_id,
                CommandStatus.STALE,
                "PLAN_PROPOSAL_NOT_CURRENT",
                "CONTROL_COMMAND_REJECTED",
            )
        current = self.current_revision_digests(run_id)
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
        binding_payload = canonical_json(
            {
                "plan_digest": proposal.plan_digest,
                "proposal_json": proposal.canonical_plan_json,
                "revision_digests": current.model_dump(mode="json"),
                "run_id": run_id,
            }
        )
        binding_digest = sha256_digest(binding_payload)

        def mutate(connection: sqlite3.Connection) -> None:
            row = connection.execute(
                "SELECT state, current_plan_digest FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            plan = connection.execute(
                "SELECT state, policy_digest, budget_digest, model_configuration_digest "
                "FROM plans WHERE run_id = ? AND plan_digest = ?",
                (run_id, proposal.plan_digest),
            ).fetchone()
            if (
                row is None
                or row["state"] != RunState.AWAITING_PLAN_APPROVAL
                or row["current_plan_digest"] is not None
                or plan is None
                or plan["state"] != "PROPOSED"
                or (
                    plan["policy_digest"],
                    plan["budget_digest"],
                    plan["model_configuration_digest"],
                )
                != (
                    current.policy_digest,
                    current.budget_digest,
                    current.model_configuration_digest,
                )
            ):
                raise StateConflict("PLAN_APPROVAL_BINDING_CHANGED")
            connection.execute(
                "INSERT INTO plan_approvals(run_id, plan_digest, approval_request_id, "
                "approval_sequence, binding_digest) VALUES (?, ?, ?, ?, ?)",
                (run_id, proposal.plan_digest, command.request_id, expected + 1, binding_digest),
            )
            connection.execute(
                "UPDATE plans SET state = 'APPROVED' WHERE run_id = ? AND plan_digest = ?",
                (run_id, proposal.plan_digest),
            )
            connection.execute(
                "UPDATE runs SET state = 'READY_TO_START', current_plan_digest = ? "
                "WHERE run_id = ?",
                (proposal.plan_digest, run_id),
            )
            connection.execute(
                "INSERT INTO run_refs(run_id, ref_kind, ref_name, expected_old_oid, "
                "current_oid, state) VALUES (?, 'PRIVATE', ?, NULL, NULL, 'ABSENT_EXPECTED')",
                (run_id, f"refs/apexcrew/runs/{run_id}"),
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
            outcome = CommandOutcome.for_payload(
                payload,
                status=CommandStatus.STALE,
                run_id=run_id,
                resulting_sequence=AuditSequence(expected),
                failed_invariant="PLAN_APPROVAL_BINDING_MISMATCH",
            )
            with self._transaction("IMMEDIATE") as connection:
                return self._record_unsequenced_control_outcome(connection, command, outcome)
        target_digest = self.target_authority_digest(run_id)
        if target_authority.current_for(run_id) != target_digest or start_guard is None:
            outcome = CommandOutcome.for_payload(
                payload,
                status=CommandStatus.CONFLICT,
                run_id=run_id,
                resulting_sequence=AuditSequence(expected),
                failed_invariant="START_GUARD_UNAVAILABLE",
            )
            with self._transaction("IMMEDIATE") as connection:
                return self._record_unsequenced_control_outcome(connection, command, outcome)
        decision = start_guard.inspect(
            run_id=run_id,
            applicable_revision_digests=current,
            expected_sequence=AuditSequence(expected),
        )
        if not decision.ok or decision.binding is None:
            outcome = CommandOutcome.for_payload(
                payload,
                status=CommandStatus.CONFLICT,
                run_id=run_id,
                resulting_sequence=AuditSequence(expected),
                failed_invariant=decision.reason or "START_GUARD_DENIED",
            )
            with self._transaction("IMMEDIATE") as connection:
                return self._record_unsequenced_control_outcome(connection, command, outcome)
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
            outcome = CommandOutcome.for_payload(
                payload,
                status=CommandStatus.STALE,
                run_id=run_id,
                resulting_sequence=AuditSequence(expected),
                failed_invariant="START_GUARD_BINDING_MISMATCH",
            )
            with self._transaction("IMMEDIATE") as connection:
                return self._record_unsequenced_control_outcome(connection, command, outcome)

        def mutate(connection: sqlite3.Connection) -> None:
            if (
                connection.execute(
                    "UPDATE run_refs SET guard_binding_json = ? WHERE run_id = ? "
                    "AND ref_kind = 'PRIVATE' AND state = 'ABSENT_EXPECTED'",
                    (guard.model_dump_json(), run_id),
                ).rowcount
                != 1
            ):
                raise StateConflict("PRIVATE_REF_PRESTATE_MISMATCH")
            self._issue_runtime_permit_in_transaction(
                connection,
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
        if isinstance(command.payload, CreateRunPayload):
            return self.create_bootstrap_run(command, repository_authority)
        existing = self._existing_control_outcome(command)
        if existing is not None:
            return existing
        run_id = RunId(command.payload.run_id)
        try:
            state, sequence = self._run_state_and_sequence(run_id)
        except StateConflict as error:
            if str(error) != "RUN_NOT_FOUND":
                raise
            outcome = CommandOutcome.for_payload(
                command.payload,
                status=CommandStatus.INVALID,
                run_id=run_id,
                resulting_sequence=None,
                failed_invariant="RUN_NOT_FOUND",
            )
            with self._transaction("IMMEDIATE") as connection:
                return self._record_unsequenced_control_outcome(connection, command, outcome)
        if command.expected_sequence != sequence:
            outcome = CommandOutcome.for_payload(
                command.payload,
                status=CommandStatus.STALE,
                run_id=run_id,
                resulting_sequence=sequence,
                failed_invariant="STALE_SEQUENCE",
            )
            with self._transaction("IMMEDIATE") as connection:
                return self._record_unsequenced_control_outcome(connection, command, outcome)
        if isinstance(command.payload, ApprovePlanPayload):
            return self._approve_plan(command, run_id, state)
        if isinstance(
            command.payload,
            (ProposePolicyPayload, ProposeBudgetPayload, ProposeModelConfigurationPayload),
        ):
            return self.propose_revision(command, run_id, state)
        if isinstance(
            command.payload,
            (
                ApprovePolicyPayload,
                ApproveBudgetPayload,
                ApproveModelConfigurationPayload,
            ),
        ):
            return self.approve_revision(command, run_id, state)
        if isinstance(command.payload, BeginPlanningPayload):
            if state != RunState.DRAFT:
                return self._record_control_outcome(
                    command,
                    run_id,
                    CommandStatus.INVALID,
                    "BEGIN_PLANNING_REQUIRES_DRAFT",
                    "CONTROL_COMMAND_REJECTED",
                )
            return self._apply_begin_planning(command, run_id, target_authority)
        if isinstance(command.payload, StartPayload):
            if state != RunState.READY_TO_START:
                return self._record_control_outcome(
                    command,
                    run_id,
                    CommandStatus.INVALID,
                    "START_REQUIRES_READY_TO_START",
                    "CONTROL_COMMAND_REJECTED",
                )
            return self._apply_start(command, run_id, target_authority, start_guard)
        if isinstance(command.payload, ResumePayload):
            if state != RunState.PAUSED:
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

    def load_runtime_state(self, run_id: RunId) -> RuntimeState:
        with self._read_transaction() as connection:
            row = connection.execute(
                "SELECT runs.state, runs.runtime_progress_generation, "
                "runs.current_plan_digest, runs.current_policy_digest, "
                "runs.current_budget_digest, runs.current_model_configuration_digest, "
                "COALESCE(run_sequences.current_sequence, 0) AS sequence "
                "FROM runs LEFT JOIN run_sequences USING(run_id) WHERE runs.run_id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            raise StateConflict("RUN_NOT_FOUND")
        if any(
            row[name] is None
            for name in (
                "current_policy_digest",
                "current_budget_digest",
                "current_model_configuration_digest",
            )
        ):
            raise StateConflict("RUNTIME_REVISION_BINDING_INCOMPLETE")
        return RuntimeState(
            run_id=run_id,
            state=RunState(row["state"]),
            sequence=AuditSequence(row["sequence"]),
            runtime_progress_generation=int(row["runtime_progress_generation"]),
            plan_digest=(
                None
                if row["current_plan_digest"] is None
                else RevisionDigest(row["current_plan_digest"])
            ),
            policy_digest=RevisionDigest(row["current_policy_digest"]),
            budget_digest=RevisionDigest(row["current_budget_digest"]),
            model_configuration_digest=RevisionDigest(row["current_model_configuration_digest"]),
        )

    def target_reservation_for_run(self, run_id: RunId) -> TargetReservation:
        with self._read_transaction() as connection:
            row = connection.execute(
                "SELECT * FROM target_reservations WHERE run_id = ?", (run_id,)
            ).fetchone()
        if row is None:
            raise StateConflict("TARGET_RESERVATION_NOT_FOUND")
        return self._target_reservation_from_row(row)

    def runtime_owner(self, run_id: RunId) -> RuntimeOwnerId | None:
        row = self._connection.execute(
            "SELECT runtime_owner_id FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            raise StateConflict("RUN_NOT_FOUND")
        return None if row[0] is None else RuntimeOwnerId(row[0])

    def runtime_delivery_event(self, run_id: RunId) -> str | None:
        row = self._connection.execute(
            "SELECT event_kind FROM audit_events WHERE run_id = ? "
            "AND event_kind IN ('RUNTIME_OWNER_RELEASED','RUNTIME_DELIVERY_STOP_RECORDED') "
            "ORDER BY sequence DESC LIMIT 1",
            (run_id,),
        ).fetchone()
        return None if row is None else str(row[0])

    def runtime_delivery_stop_count(self, run_id: RunId) -> int:
        return int(
            self._connection.execute(
                "SELECT COUNT(*) FROM runtime_delivery_stops WHERE run_id = ?", (run_id,)
            ).fetchone()[0]
        )

    def model_attempt_count(self, logical_turn_id: LogicalTurnId) -> int:
        return int(
            self._connection.execute(
                "SELECT COUNT(*) FROM model_attempts WHERE logical_turn_id = ?",
                (logical_turn_id,),
            ).fetchone()[0]
        )

    def unconsumed_permit_count(self, run_id: RunId) -> int:
        return int(
            self._connection.execute(
                "SELECT COUNT(*) FROM runtime_permits WHERE run_id = ? AND state = 'UNCONSUMED'",
                (run_id,),
            ).fetchone()[0]
        )

    def _require_consumed_runtime_owner(
        self,
        connection: sqlite3.Connection,
        run_id: RunId,
        owner_id: RuntimeOwnerId,
        permit_generation: int,
    ) -> tuple[RuntimePermit, sqlite3.Row]:
        run = connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        permit_row = connection.execute(
            "SELECT * FROM runtime_permits WHERE run_id = ? AND generation = ? "
            "AND state = 'CONSUMED' AND consumed_owner_id = ?",
            (run_id, permit_generation, owner_id),
        ).fetchone()
        if (
            run is None
            or permit_row is None
            or run["runtime_owner_id"] != owner_id
            or run["runtime_interval_owner_generation"] != run["runtime_owner_generation"]
            or run["runtime_interval_opened_nanoseconds"] is None
        ):
            raise StateConflict("RUNTIME_OWNER_BINDING_MISMATCH")
        permit = self._runtime_permit_from_row(permit_row)
        return permit, run

    def _require_consumed_draft_reservation_permit(
        self,
        connection: sqlite3.Connection,
        run_id: RunId,
        owner_id: RuntimeOwnerId,
        permit_generation: int,
    ) -> RuntimePermit:
        permit, run = self._require_consumed_runtime_owner(
            connection, run_id, owner_id, permit_generation
        )
        if permit.allowed_phase != "DRAFT" or RunState(run["state"]) != RunState.DRAFT:
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
        existing = self.target_reservation_for_run(run_id)
        if existing.phase == "CREATION_INTENT_RECORDED":
            with self._read_transaction() as connection:
                return TargetReservationCreationIntent.from_effect_intent(
                    self._unsettled_effect_for_reservation(connection, existing)
                )
        created: list[TargetReservationCreationIntent] = []

        def mutate(connection: sqlite3.Connection) -> None:
            permit = self._require_consumed_draft_reservation_permit(
                connection, run_id, owner_id, permit_generation
            )
            reservation = self._target_reservation_for_run_for_update(connection, run_id)
            intent = self._new_target_reservation_creation_intent(
                connection, reservation, expected_sequence
            ).model_copy(
                update={
                    "applicable_revision_digests": permit.applicable_revision_digests,
                    "target_authority_digest": permit.target_authority_digest,
                }
            )
            self._insert_effect_intent(
                connection, intent.to_effect_intent(AuditSequence(expected_sequence + 1))
            )
            if (
                connection.execute(
                    "UPDATE target_reservations SET phase = 'CREATION_INTENT_RECORDED', "
                    "creation_intent_id = ? WHERE reservation_id = ? AND phase = 'ALLOCATED'",
                    (intent.intent_id, reservation.reservation_id),
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

        def mutate(connection: sqlite3.Connection) -> None:
            permit = self._require_consumed_draft_reservation_permit(
                connection, intent.run_id, owner_id, permit_generation
            )
            if (
                intent.applicable_revision_digests != permit.applicable_revision_digests
                or intent.target_authority_digest != permit.target_authority_digest
            ):
                raise StateConflict("TARGET_RESERVATION_PERMIT_AUTHORITY_MISMATCH")
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
                if classify_reservation_creation(outcome.observed) != "SETTLE":
                    raise StateConflict("TARGET_RESERVATION_SUCCESS_NOT_EXACT")
                phase, state = "REGISTERED_LOCKED", RunState.PLANNING
                admin_name = outcome.observed.admin_entry_name
                admin_digest = outcome.observed.admin_binding_digest
            elif outcome.result_class == "CONFLICT":
                phase, state = "ALLOCATED", RunState.DRAFT
                admin_name = admin_digest = None
            else:
                phase, state = "CREATION_INTENT_RECORDED", RunState.INDETERMINATE
                admin_name = admin_digest = None
                self._close_new_dispatch(
                    connection, intent.run_id, DispatchCloseCause.RUNTIME_FAULT
                )
            connection.execute(
                "UPDATE target_reservations SET phase = ?, admin_entry_name = ?, "
                "admin_binding_digest = ? WHERE reservation_id = ?",
                (phase, admin_name, admin_digest, reservation.reservation_id),
            )
            connection.execute(
                "UPDATE runs SET state = ? WHERE run_id = ?", (state.value, intent.run_id)
            )

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

        def mutate(connection: sqlite3.Connection) -> None:
            self._require_consumed_draft_reservation_permit(
                connection, run_id, owner_id, permit_generation
            )
            reservation = self._target_reservation_for_run_for_update(connection, run_id)
            if reservation.phase != "REGISTERED_LOCKED":
                raise StateConflict("TARGET_RESERVATION_REUSE_PHASE_INVALID")
            if (
                connection.execute(
                    "UPDATE runs SET state = 'PLANNING' WHERE run_id = ? AND state = 'DRAFT'",
                    (run_id,),
                ).rowcount
                != 1
            ):
                raise StateConflict("TARGET_RESERVATION_DRAFT_TRANSITION_REQUIRED")

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

        def mutate(connection: sqlite3.Connection) -> None:
            self._require_consumed_draft_reservation_permit(
                connection, run_id, owner_id, permit_generation
            )
            if not observed.observable:
                connection.execute(
                    "UPDATE runs SET state = 'INDETERMINATE' WHERE run_id = ?", (run_id,)
                )
                self._close_new_dispatch(connection, run_id, DispatchCloseCause.RUNTIME_FAULT)

        return self._commit_state_and_event(
            run_id=run_id,
            expected_sequence=expected_sequence,
            event=AuditEvent.kind(event),
            mutate=mutate,
        )

    def begin_runtime_barrier(
        self, run_id: RunId, action_id: str, expected_sequence: AuditSequence
    ) -> str:
        def mutate(connection: sqlite3.Connection) -> None:
            self._require_new_dispatch_open(connection, run_id)
            current = connection.execute(
                "SELECT state FROM runtime_barriers WHERE run_id = ?", (run_id,)
            ).fetchone()
            if current is not None and current[0] == "IN_FLIGHT":
                raise StateConflict("RUNTIME_BARRIER_IN_FLIGHT")
            connection.execute(
                "INSERT INTO runtime_barriers(run_id, action_id, state) "
                "VALUES (?, ?, 'IN_FLIGHT') ON CONFLICT(run_id) DO UPDATE SET "
                "action_id = excluded.action_id, effect_intent_id = NULL, "
                "state = 'IN_FLIGHT', pending_stop_reason = NULL",
                (run_id, action_id),
            )

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
        if isinstance(model_calls, bool) or model_calls < 0:
            raise StateConflict("GLOBAL_USAGE_VALUE_INVALID")
        events = [AuditEvent.kind("RUNTIME_BARRIER_SETTLED", action_id=action_id)]

        def mutate(connection: sqlite3.Connection) -> None:
            row = connection.execute(
                "SELECT state, action_id FROM runtime_barriers WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None or row["state"] != "IN_FLIGHT" or row["action_id"] != action_id:
                raise StateConflict("RUNTIME_BARRIER_SETTLE_COMPARE_AND_SET_FAILED")
            counters = self._model_counters(connection, run_id)
            calls = counters.calls + model_calls
            connection.execute(
                "INSERT INTO model_counters(run_id, calls, input_tokens, output_tokens, cost_usd) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(run_id) DO UPDATE SET calls = excluded.calls, "
                "input_tokens = excluded.input_tokens, output_tokens = excluded.output_tokens, "
                "cost_usd = excluded.cost_usd",
                (
                    run_id,
                    calls,
                    counters.input_tokens,
                    counters.output_tokens,
                    str(counters.cost_usd),
                ),
            )
            budget_digest = self._current_revision_digests_in_transaction(
                connection, run_id
            ).budget_digest
            if budget_digest is None:
                raise StateConflict("CURRENT_BUDGET_NOT_FOUND")
            self._approved_budget_for_update(connection, run_id, budget_digest)
            _settlement, stopped = self._settle_global_usage_in_transaction(
                connection,
                run_id,
                budget_digest,
                GlobalBudgetMetric.MODEL_CALLS,
                calls,
            )
            derived = "BUDGET_STOP" if stopped else None
            if pending_stop_reason != derived:
                raise StateConflict("RUNTIME_BARRIER_STOP_CAUSE_MISMATCH")
            if stopped:
                events.append(AuditEvent.kind("BUDGET_STOP_REQUESTED"))
            connection.execute(
                "UPDATE runtime_barriers SET state = 'SETTLED', pending_stop_reason = ? "
                "WHERE run_id = ?",
                (derived, run_id),
            )

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
        with self._read_transaction() as connection:
            self._require_consumed_runtime_owner(connection, run_id, owner_id, permit_generation)
            interrupt = connection.execute(
                "SELECT kind FROM runtime_interrupts WHERE run_id = ? AND state = 'PENDING'",
                (run_id,),
            ).fetchone()
            barrier = connection.execute(
                "SELECT state, pending_stop_reason FROM runtime_barriers WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        if interrupt is not None:
            return RuntimeDecision.pause(str(interrupt["kind"]), expected_sequence)
        if barrier is not None and barrier["pending_stop_reason"] == "BUDGET_STOP":
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

        def mutate(connection: sqlite3.Connection) -> None:
            permit, run = self._require_consumed_runtime_owner(
                connection, run_id, owner_id, permit_generation
            )
            barrier = connection.execute(
                "SELECT state FROM runtime_barriers WHERE run_id = ?", (run_id,)
            ).fetchone()
            if barrier is not None and barrier["state"] == "IN_FLIGHT":
                raise StateConflict("RUNTIME_BARRIER_IN_FLIGHT")
            generation = int(run["runtime_interval_owner_generation"])
            opened = int(run["runtime_interval_opened_nanoseconds"])
            latest = connection.execute(
                "SELECT runtime_monotonic_nanoseconds FROM audit_events WHERE run_id = ? "
                "AND runtime_owner_generation = ? ORDER BY sequence DESC LIMIT 1",
                (run_id, generation),
            ).fetchone()
            if self._monotonic_clock is None:
                raise StateConflict("MONOTONIC_CLOCK_NOT_CONFIGURED")
            closed = self._monotonic_clock.now()
            floor = opened if latest is None else int(latest[0])
            if closed.nanoseconds < floor:
                raise StateConflict("MONOTONIC_CLOCK_REGRESSED")
            closed_at.append(closed)
            cumulative = int(run["active_runtime_nanoseconds"]) + closed.nanoseconds - opened
            final = RunStop(
                run_id=run_id,
                state=RunState(run["state"]),
                reason=candidate.reason,
                last_sequence=AuditSequence(expected_sequence + 2),
                pending=candidate.pending,
            )
            connection.execute(
                "UPDATE runs SET active_runtime_nanoseconds = ? WHERE run_id = ?",
                (cumulative, run_id),
            )
            connection.execute(
                "INSERT INTO runtime_delivery_stops VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    permit.generation,
                    generation,
                    final.reason.value,
                    final.model_dump_json(),
                    opened,
                    closed.nanoseconds,
                    closed.nanoseconds - opened,
                    cumulative,
                    final.last_sequence,
                ),
            )
            result.append(final)

        def finalize(connection: sqlite3.Connection) -> None:
            if (
                connection.execute(
                    "UPDATE runs SET runtime_owner_id = NULL, "
                    "runtime_interval_owner_generation = NULL, "
                    "runtime_interval_opened_nanoseconds = NULL WHERE run_id = ? "
                    "AND runtime_owner_id = ?",
                    (run_id, owner_id),
                ).rowcount
                != 1
            ):
                raise StateConflict("RUNTIME_OWNER_RELEASE_COMPARE_AND_SET_FAILED")

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

        dispositions: list[RuntimeFaultDisposition] = []

        def mutate(connection: sqlite3.Connection) -> None:
            self._require_consumed_runtime_owner(connection, run_id, owner_id, permit_generation)
            barrier = connection.execute(
                "SELECT state FROM runtime_barriers WHERE run_id = ?", (run_id,)
            ).fetchone()
            stored_state = "IDLE" if barrier is None else str(barrier["state"])
            state: Literal["IDLE", "SETTLED", "INDETERMINATE"]
            stop_reason: Literal["RUNTIME_FAULT", "RUNTIME_CLOCK_REGRESSION", "INDETERMINATE"]
            if stored_state == "IN_FLIGHT":
                state = "INDETERMINATE"
                connection.execute(
                    "UPDATE runtime_barriers SET state = 'INDETERMINATE' WHERE run_id = ?",
                    (run_id,),
                )
                connection.execute(
                    "UPDATE runs SET state = 'INDETERMINATE' WHERE run_id = ?", (run_id,)
                )
                stop_reason = "INDETERMINATE"
            else:
                state = "SETTLED" if stored_state == "SETTLED" else "IDLE"
                stop_reason = (
                    "RUNTIME_CLOCK_REGRESSION"
                    if fault.fault_code == "MONOTONIC_CLOCK_REGRESSED"
                    else "RUNTIME_FAULT"
                )
            self._close_new_dispatch(connection, run_id, DispatchCloseCause.RUNTIME_FAULT)
            connection.execute(
                "INSERT INTO runtime_faults VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    permit_generation,
                    fault.phase,
                    fault.fault_code,
                    fault.fingerprint,
                    expected_sequence + 1,
                    state,
                ),
            )
            dispositions.append(
                RuntimeFaultDisposition(AuditSequence(expected_sequence + 1), stop_reason, state)
            )

        self._commit_state_and_event(
            run_id=run_id,
            expected_sequence=expected_sequence,
            event=AuditEvent.kind("RUNTIME_FAULT_RECORDED"),
            mutate=mutate,
        )
        return dispositions[0]

    def latest_runtime_fault(self, run_id: RunId) -> object:
        from apexcrew.application.runtime import RuntimeFault

        row = self._connection.execute(
            "SELECT phase, fault_code, fingerprint FROM runtime_faults WHERE run_id = ? "
            "ORDER BY resulting_sequence DESC LIMIT 1",
            (run_id,),
        ).fetchone()
        if row is None:
            raise StateConflict("RUNTIME_FAULT_NOT_FOUND")
        return RuntimeFault(row["phase"], row["fault_code"], row["fingerprint"])

    def recorded_stop_reason(self, run_id: RunId) -> str | None:
        fault = self._connection.execute(
            "SELECT fault_code FROM runtime_faults WHERE run_id = ? "
            "ORDER BY resulting_sequence DESC LIMIT 1",
            (run_id,),
        ).fetchone()
        if fault is not None:
            return (
                "RUNTIME_CLOCK_REGRESSION"
                if fault[0] == "MONOTONIC_CLOCK_REGRESSED"
                else "RUNTIME_FAULT"
            )
        barrier = self._connection.execute(
            "SELECT pending_stop_reason FROM runtime_barriers WHERE run_id = ?", (run_id,)
        ).fetchone()
        return None if barrier is None else barrier[0]

    def next_recoverable_model_turn(self, run_id: RunId) -> CommittedModelTurn | None:
        row = self._connection.execute(
            "SELECT logical_turn_id FROM model_turns WHERE run_id = ? "
            "AND state = 'COMPLETION_COMMITTED' AND downstream_intent_id IS NULL "
            "ORDER BY committed_sequence, logical_turn_id LIMIT 1",
            (run_id,),
        ).fetchone()
        return None if row is None else self.committed_model_turn(run_id, row[0])

    def next_recovered_model_action(self, run_id: RunId) -> RecoveredModelAction | None:
        row = self._connection.execute(
            "SELECT model_turns.logical_turn_id, effect_intents.intent_id "
            "FROM model_turns JOIN effect_intents "
            "ON effect_intents.intent_id = model_turns.downstream_intent_id "
            "WHERE model_turns.run_id = ? AND model_turns.state = 'DOWNSTREAM_INTENT_RECORDED' "
            "AND effect_intents.kind = 'RECOVERED_MODEL_ACTION' "
            "AND NOT EXISTS (SELECT 1 FROM effect_results "
            "WHERE effect_results.intent_id = effect_intents.intent_id) "
            "ORDER BY effect_intents.created_sequence LIMIT 1",
            (run_id,),
        ).fetchone()
        if row is None:
            return None
        turn = self.committed_model_turn(run_id, row["logical_turn_id"])
        if turn is None:
            raise StateConflict("RECOVERED_MODEL_ACTION_BINDING_MISMATCH")
        try:
            return RecoveredModelAction.from_journal(turn, self.effect_intent(row["intent_id"]))
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

        def mutate(connection: sqlite3.Connection) -> None:
            run = connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if run is None:
                raise StateConflict("RUN_NOT_FOUND")
            if run["runtime_owner_id"] is not None:
                generation = run["runtime_interval_owner_generation"]
                opened = run["runtime_interval_opened_nanoseconds"]
                if generation is None or opened is None:
                    raise StateConflict("ACTIVE_RUN_TIME_RECOVERY_INVALID")
                last = connection.execute(
                    "SELECT runtime_monotonic_nanoseconds FROM audit_events WHERE run_id = ? "
                    "AND runtime_owner_generation = ? ORDER BY sequence DESC LIMIT 1",
                    (run_id, generation),
                ).fetchone()
                if last is None or last[0] is None or int(last[0]) < int(opened):
                    raise StateConflict("ACTIVE_RUN_TIME_RECOVERY_INVALID")
                cumulative = int(run["active_runtime_nanoseconds"]) + int(last[0]) - int(opened)
                connection.execute(
                    "UPDATE runs SET active_runtime_nanoseconds = ?, runtime_owner_id = NULL, "
                    "runtime_interval_owner_generation = NULL, "
                    "runtime_interval_opened_nanoseconds = NULL WHERE run_id = ?",
                    (cumulative, run_id),
                )
            state = RunState(run["state"])
            allowed: RuntimeAllowedPhase = (
                "TERMINAL_ADMINISTRATION"
                if state in {RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED}
                else state.value  # type: ignore[assignment]
            )
            self._issue_runtime_permit_in_transaction(
                connection,
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

    def plan_approval(self, run_id: RunId) -> PlanApproval:
        with self._read_transaction() as connection:
            row = connection.execute(
                "SELECT * FROM plan_approvals WHERE run_id = ?", (run_id,)
            ).fetchone()
        if row is None:
            raise StateConflict("PLAN_APPROVAL_NOT_FOUND")
        return PlanApproval(
            run_id,
            RevisionDigest(row["plan_digest"]),
            str(row["approval_request_id"]),
            AuditSequence(row["approval_sequence"]),
            Sha256DigestText(row["binding_digest"]),
        )

    def run_ref(self, run_id: RunId, ref_kind: Literal["PRIVATE", "TARGET"]) -> RunRefRecord:
        with self._read_transaction() as connection:
            row = connection.execute(
                "SELECT * FROM run_refs WHERE run_id = ? AND ref_kind = ?",
                (run_id, ref_kind),
            ).fetchone()
        if row is None:
            raise StateConflict("RUN_REF_NOT_FOUND")
        return RunRefRecord(
            run_id=run_id,
            ref_kind=ref_kind,
            ref_name=str(row["ref_name"]),
            expected_old_oid=(
                None if row["expected_old_oid"] is None else GitOid(row["expected_old_oid"])
            ),
            current_oid=None if row["current_oid"] is None else GitOid(row["current_oid"]),
            state=row["state"],
            last_intent_id=(
                None if row["last_intent_id"] is None else IntentId(row["last_intent_id"])
            ),
            guard_binding_json=row["guard_binding_json"],
        )

    def runtime_start_binding(self, run_id: RunId) -> RuntimeStartBinding:
        with self._read_transaction() as connection:
            row = connection.execute(
                "SELECT runs.state, run_sequences.current_sequence, run_refs.guard_binding_json, "
                "runtime_permits.generation, runtime_permits.consumed_owner_id, "
                "runtime_permits.consumed_sequence FROM runs JOIN run_sequences USING(run_id) "
                "JOIN run_refs ON run_refs.run_id = runs.run_id AND run_refs.ref_kind = 'PRIVATE' "
                "JOIN runtime_permits ON runtime_permits.run_id = runs.run_id "
                "AND runtime_permits.state = 'CONSUMED' WHERE runs.run_id = ? "
                "ORDER BY runtime_permits.generation DESC LIMIT 1",
                (run_id,),
            ).fetchone()
        if (
            row is None
            or row["state"] != RunState.READY_TO_START
            or row["guard_binding_json"] is None
            or row["consumed_owner_id"] is None
            or row["consumed_sequence"] is None
        ):
            raise StateConflict("RUNTIME_START_BINDING_NOT_CURRENT")
        return RuntimeStartBinding(
            run_id=run_id,
            sequence=AuditSequence(row["current_sequence"]),
            state=RunState.READY_TO_START,
            permit_generation=int(row["generation"]),
            consumed_owner_id=RuntimeOwnerId(row["consumed_owner_id"]),
            consumed_sequence=AuditSequence(row["consumed_sequence"]),
            guard=StartGuardBinding.model_validate_json(row["guard_binding_json"]),
        )

    def record_private_ref_init_intent(
        self,
        *,
        binding: RuntimeStartBinding,
        intent: RefCasIntent,
        expected_sequence: AuditSequence,
    ) -> AuditSequence:
        effect = intent.to_effect_intent(AuditSequence(expected_sequence + 1))

        def mutate(connection: sqlite3.Connection) -> None:
            current = connection.execute(
                "SELECT runs.state, run_sequences.current_sequence, run_refs.guard_binding_json, "
                "runtime_permits.generation, runtime_permits.consumed_owner_id, "
                "runtime_permits.consumed_sequence FROM runs JOIN run_sequences USING(run_id) "
                "JOIN run_refs ON run_refs.run_id = runs.run_id AND run_refs.ref_kind = 'PRIVATE' "
                "JOIN runtime_permits ON runtime_permits.run_id = runs.run_id "
                "AND runtime_permits.state = 'CONSUMED' WHERE runs.run_id = ? "
                "ORDER BY runtime_permits.generation DESC LIMIT 1",
                (binding.run_id,),
            ).fetchone()
            if (
                current is None
                or current["state"] != RunState.READY_TO_START
                or AuditSequence(current["current_sequence"]) != binding.sequence
                or int(current["generation"]) != binding.permit_generation
                or current["consumed_owner_id"] != binding.consumed_owner_id
                or AuditSequence(current["consumed_sequence"]) != binding.consumed_sequence
                or StartGuardBinding.model_validate_json(current["guard_binding_json"])
                != binding.guard
                or intent.permit_generation != binding.permit_generation
            ):
                raise StateConflict("RUNTIME_START_BINDING_CHANGED")
            self._insert_effect_intent(connection, effect)
            if (
                connection.execute(
                    "UPDATE run_refs SET state = 'INIT_INTENT_RECORDED', last_intent_id = ? "
                    "WHERE run_id = ? AND ref_kind = 'PRIVATE' AND state = 'ABSENT_EXPECTED'",
                    (intent.intent_id, intent.run_id),
                ).rowcount
                != 1
            ):
                raise StateConflict("PRIVATE_REF_PRESTATE_MISMATCH")
            connection.execute(
                "UPDATE runs SET state = 'ACTIVE' WHERE run_id = ? AND state = 'READY_TO_START'",
                (intent.run_id,),
            )

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

        def mutate(connection: sqlite3.Connection) -> None:
            row = connection.execute(
                "SELECT state, last_intent_id FROM run_refs WHERE run_id = ? "
                "AND ref_kind = 'PRIVATE'",
                (intent.run_id,),
            ).fetchone()
            if (
                row is None
                or row["state"] != "INIT_INTENT_RECORDED"
                or row["last_intent_id"] != intent.intent_id
            ):
                raise StateConflict("PRIVATE_REF_INTENT_NOT_CURRENT")
            self._insert_effect_result(
                connection,
                intent.run_id,
                intent.intent_id,
                result,
                intent.applicable_revision_digests,
            )
            if outcome.result_class == "PRIVATE_REF_INITIALIZED":
                if outcome.observed_oid != intent.prepared_oid:
                    raise StateConflict("PRIVATE_REF_OUTCOME_OID_MISMATCH")
                connection.execute(
                    "UPDATE run_refs SET state = 'PRESENT', current_oid = ? "
                    "WHERE run_id = ? AND ref_kind = 'PRIVATE'",
                    (intent.prepared_oid, intent.run_id),
                )
                connection.execute(
                    "UPDATE runs SET run_head_oid = ? WHERE run_id = ? AND state = 'ACTIVE'",
                    (intent.prepared_oid, intent.run_id),
                )
            else:
                state = (
                    "INDETERMINATE"
                    if outcome.result_class == "PRIVATE_REF_UNOBSERVABLE"
                    else "PAUSED"
                )
                ref_state = (
                    "CONFLICT"
                    if outcome.result_class == "PRIVATE_REF_CONFLICT"
                    else (
                        "INIT_INTENT_RECORDED"
                        if outcome.result_class == "PRIVATE_REF_UNOBSERVABLE"
                        else "ABSENT_EXPECTED"
                    )
                )
                connection.execute(
                    "UPDATE run_refs SET state = ? WHERE run_id = ? AND ref_kind = 'PRIVATE'",
                    (ref_state, intent.run_id),
                )
                connection.execute(
                    "UPDATE runs SET state = ? WHERE run_id = ? AND state = 'ACTIVE'",
                    (state, intent.run_id),
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

    def close(self) -> None:
        self._connection.close()
