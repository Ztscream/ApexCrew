from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from typing import Any, Literal, Protocol

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
from apexcrew.domain.revisions import Sha256DigestText
from apexcrew.domain.types import (
    AttemptId,
    AuditSequence,
    IntentId,
    RunId,
    TaskId,
    UnresolvedSetDigest,
)


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

    def record_intent(self, intent: EffectIntent, expected_sequence: AuditSequence) -> EffectIntent:
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
