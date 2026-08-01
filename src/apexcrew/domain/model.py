from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from decimal import Decimal
from enum import StrEnum
from hashlib import sha256
from typing import Literal, Protocol
from uuid import uuid4

from apexcrew.domain.limits import V01_MECHANISM_LIMITS
from apexcrew.domain.types import (
    AttemptId,
    AuditSequence,
    IntentId,
    RevisionDigest,
    RunId,
    TaskId,
)

LogicalTurnId = str


@dataclass(frozen=True, slots=True)
class ModelUsage:
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal


@dataclass(frozen=True, slots=True)
class ModelRequest:
    run_id: RunId
    plan_digest: RevisionDigest | None
    policy_digest: RevisionDigest
    budget_digest: RevisionDigest
    model_configuration_digest: RevisionDigest
    requested_model_id: str
    allowed_model_ids: frozenset[str]
    prompt: tuple[Mapping[str, str], ...]
    tool_schema_digest: str
    request_digest: str
    idempotency_key: str
    max_input_tokens: int
    max_output_tokens: int
    reserved_cost_usd: Decimal
    owner_kind: Literal["PLANNING", "WORKER"] = "PLANNING"
    task_id: TaskId | None = None
    attempt_id: AttemptId | None = None
    tranche_id: str | None = None

    def __post_init__(self) -> None:
        planning = self.owner_kind == "PLANNING"
        if planning != (
            self.task_id is None and self.attempt_id is None and self.tranche_id is None
        ):
            raise ValueError("MODEL_REQUEST_OWNER_BINDING_MISMATCH")
        if not planning and (
            self.task_id is None or self.attempt_id is None or self.tranche_id is None
        ):
            raise ValueError("WORKER_MODEL_REQUEST_OWNER_INCOMPLETE")


@dataclass(frozen=True, slots=True)
class ModelRecoveryBinding:
    request_digest: str
    tool_schema_digest: str
    plan_digest: RevisionDigest | None
    policy_digest: RevisionDigest
    budget_digest: RevisionDigest
    model_configuration_digest: RevisionDigest

    @classmethod
    def from_request(cls, request: ModelRequest) -> ModelRecoveryBinding:
        return cls(
            request.request_digest,
            request.tool_schema_digest,
            request.plan_digest,
            request.policy_digest,
            request.budget_digest,
            request.model_configuration_digest,
        )


RecoveryBlock = Literal[
    "MODEL_COMPLETION_NOT_COMMITTED",
    "RECOVERY_BINDING_MISMATCH",
    "DOWNSTREAM_INTENT_REQUIRES_RECONCILIATION",
]
ModelDispatchOutcome = Literal[
    "COMPLETED",
    "RETURNED_MODEL_MISMATCH",
    "KNOWN_CLOSED_REJECTION",
    "INDETERMINATE",
    "MODEL_COMPLETION_NOT_COMMITTED",
    "RECOVERY_BINDING_MISMATCH",
    "DOWNSTREAM_INTENT_REQUIRES_RECONCILIATION",
]


@dataclass(frozen=True, slots=True)
class ModelCompletion:
    response_id: str
    requested_model_id: str
    returned_model_id: str
    usage: ModelUsage | None
    normalized_action: Mapping[str, object]


class ProviderAttemptKind(StrEnum):
    COMPLETED = "COMPLETED"
    KNOWN_CLOSED_REJECTION = "KNOWN_CLOSED_REJECTION"
    UNKNOWN_OUTCOME = "UNKNOWN_OUTCOME"


@dataclass(frozen=True, slots=True)
class ProviderAttemptResult:
    kind: ProviderAttemptKind
    provider_response_id: str | None
    reason_code: str | None
    completion: ModelCompletion | None
    usage: ModelUsage | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", ProviderAttemptKind(self.kind))

    @classmethod
    def completed(cls, completion: ModelCompletion) -> ProviderAttemptResult:
        return cls(
            ProviderAttemptKind.COMPLETED,
            completion.response_id,
            None,
            completion,
            completion.usage,
        )

    @classmethod
    def known_closed(
        cls,
        provider_response_id: str,
        reason_code: str,
        usage: ModelUsage | None = None,
    ) -> ProviderAttemptResult:
        return cls(
            ProviderAttemptKind.KNOWN_CLOSED_REJECTION,
            provider_response_id,
            reason_code,
            None,
            usage,
        )

    @classmethod
    def unknown(cls, reason_code: str) -> ProviderAttemptResult:
        return cls(
            ProviderAttemptKind.UNKNOWN_OUTCOME,
            None,
            reason_code,
            None,
            None,
        )


@dataclass(frozen=True, slots=True)
class ModelBudgetAmounts:
    calls: int
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal

    @classmethod
    def zero(cls) -> ModelBudgetAmounts:
        return cls(calls=0, input_tokens=0, output_tokens=0, cost_usd=Decimal(0))

    def to_json(self) -> str:
        return json.dumps(
            {
                "calls": self.calls,
                "cost_usd": str(self.cost_usd),
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, value: str) -> ModelBudgetAmounts:
        data = json.loads(value)
        return cls(
            calls=data["calls"],
            input_tokens=data["input_tokens"],
            output_tokens=data["output_tokens"],
            cost_usd=Decimal(data["cost_usd"]),
        )


@dataclass(frozen=True, slots=True)
class ModelCounters:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: Decimal = Decimal(0)

    def reserve(self, amounts: ModelBudgetAmounts) -> ModelCounters:
        return replace(
            self,
            calls=self.calls + amounts.calls,
            input_tokens=self.input_tokens + amounts.input_tokens,
            output_tokens=self.output_tokens + amounts.output_tokens,
            cost_usd=self.cost_usd + amounts.cost_usd,
        )

    def settle(self, reserved: ModelBudgetAmounts, charged: ModelBudgetAmounts) -> ModelCounters:
        return replace(
            self,
            calls=self.calls - reserved.calls + charged.calls,
            input_tokens=self.input_tokens - reserved.input_tokens + charged.input_tokens,
            output_tokens=self.output_tokens - reserved.output_tokens + charged.output_tokens,
            cost_usd=self.cost_usd - reserved.cost_usd + charged.cost_usd,
        )


@dataclass(frozen=True, slots=True)
class LogicalModelTurn:
    run_id: RunId
    logical_turn_id: LogicalTurnId
    request_digest: str
    state: Literal["OPEN"] = "OPEN"

    @classmethod
    def new(cls, request: ModelRequest) -> LogicalModelTurn:
        return cls(request.run_id, f"model-turn-{uuid4().hex}", request.request_digest)


@dataclass(frozen=True, slots=True)
class ModelRequestIntent:
    run_id: RunId
    intent_id: IntentId
    logical_turn_id: LogicalTurnId
    request: ModelRequest
    reserved_amounts: ModelBudgetAmounts
    provider_attempt_number: int = 1
    state: Literal["RESERVED"] = "RESERVED"

    @classmethod
    def reserve(
        cls,
        turn: LogicalModelTurn,
        request: ModelRequest,
        provider_attempt_number: int = 1,
    ) -> ModelRequestIntent:
        if turn.run_id != request.run_id or turn.request_digest != request.request_digest:
            raise ValueError("MODEL_TURN_REQUEST_BINDING_MISMATCH")
        return cls(
            run_id=request.run_id,
            intent_id=IntentId(f"model-intent-{uuid4().hex}"),
            logical_turn_id=turn.logical_turn_id,
            request=request,
            reserved_amounts=ModelBudgetAmounts(
                calls=1,
                input_tokens=request.max_input_tokens,
                output_tokens=request.max_output_tokens,
                cost_usd=request.reserved_cost_usd,
            ),
            provider_attempt_number=provider_attempt_number,
        )


@dataclass(frozen=True, slots=True)
class ModelDispatchResult:
    run_id: RunId
    logical_turn_id: LogicalTurnId
    outcome: ModelDispatchOutcome
    returned_model_id: str | None
    normalized_action: Mapping[str, object] | None
    normalized_payload_digest: str | None
    charged_amounts: ModelBudgetAmounts

    @classmethod
    def blocked(
        cls,
        *,
        run_id: RunId,
        logical_turn_id: LogicalTurnId,
        outcome: RecoveryBlock,
    ) -> ModelDispatchResult:
        return cls(
            run_id=run_id,
            logical_turn_id=logical_turn_id,
            outcome=outcome,
            returned_model_id=None,
            normalized_action=None,
            normalized_payload_digest=None,
            charged_amounts=ModelBudgetAmounts.zero(),
        )


@dataclass(frozen=True, slots=True)
class CommittedModelTurn:
    run_id: RunId
    logical_turn_id: LogicalTurnId
    owner_kind: Literal["PLANNING", "WORKER"]
    task_id: TaskId | None
    attempt_id: AttemptId | None
    tranche_id: str | None
    recovery_binding: ModelRecoveryBinding
    returned_model_id: str
    normalized_output_digest: str
    normalized_payload: Mapping[str, object]
    dispatch_result: ModelDispatchResult
    committed_sequence: AuditSequence
    state: Literal["COMPLETION_COMMITTED", "DOWNSTREAM_INTENT_RECORDED"]
    downstream_intent_id: IntentId | None
    downstream_sequence: AuditSequence | None


def _normalized_payload_digest(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + sha256(encoded).hexdigest()


def settle_model_completion(
    intent: ModelRequestIntent,
    completion: ModelCompletion,
    allowed_model_ids: frozenset[str],
) -> ModelDispatchResult:
    if completion.returned_model_id not in allowed_model_ids:
        usage = completion.usage
        charged = (
            intent.reserved_amounts
            if usage is None
            else ModelBudgetAmounts(
                calls=1,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cost_usd=intent.reserved_amounts.cost_usd,
            )
        )
        return ModelDispatchResult(
            run_id=intent.run_id,
            logical_turn_id=intent.logical_turn_id,
            outcome="RETURNED_MODEL_MISMATCH",
            returned_model_id=completion.returned_model_id,
            normalized_action=None,
            normalized_payload_digest=None,
            charged_amounts=charged,
        )
    usage = completion.usage
    charged = (
        intent.reserved_amounts
        if usage is None
        else ModelBudgetAmounts(
            calls=1,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cost_usd=usage.cost_usd,
        )
    )
    return ModelDispatchResult(
        run_id=intent.run_id,
        logical_turn_id=intent.logical_turn_id,
        outcome="COMPLETED",
        returned_model_id=completion.returned_model_id,
        normalized_action=completion.normalized_action,
        normalized_payload_digest=_normalized_payload_digest(completion.normalized_action),
        charged_amounts=charged,
    )


@dataclass(frozen=True, slots=True)
class SettledModelAttempt:
    run_id: RunId
    intent_id: IntentId
    logical_turn_id: LogicalTurnId
    provider_attempt_number: int
    request: ModelRequest
    reserved_amounts: ModelBudgetAmounts
    kind: ProviderAttemptKind
    provider_response_id: str | None
    reason_code: str | None
    charged_amounts: ModelBudgetAmounts
    result_digest: str
    dispatch_result: ModelDispatchResult
    backoff_seconds: int | None = None
    state: Literal["CLOSED"] = "CLOSED"

    @property
    def outcome(self) -> ProviderAttemptKind:
        return self.kind

    @classmethod
    def from_result(
        cls, intent: ModelRequestIntent, result: ProviderAttemptResult
    ) -> SettledModelAttempt:
        if result.kind is ProviderAttemptKind.COMPLETED:
            if result.completion is None:
                raise ValueError("COMPLETED_ATTEMPT_REQUIRES_COMPLETION")
            dispatch = settle_model_completion(
                intent, result.completion, intent.request.allowed_model_ids
            )
        else:
            if result.completion is not None:
                raise ValueError("CLOSED_ATTEMPT_FORBIDS_COMPLETION")
            charged = (
                intent.reserved_amounts
                if result.usage is None
                else ModelBudgetAmounts(
                    calls=1,
                    input_tokens=result.usage.input_tokens,
                    output_tokens=result.usage.output_tokens,
                    cost_usd=result.usage.cost_usd,
                )
            )
            dispatch = ModelDispatchResult(
                run_id=intent.run_id,
                logical_turn_id=intent.logical_turn_id,
                outcome=(
                    "KNOWN_CLOSED_REJECTION"
                    if result.kind is ProviderAttemptKind.KNOWN_CLOSED_REJECTION
                    else "INDETERMINATE"
                ),
                returned_model_id=None,
                normalized_action=None,
                normalized_payload_digest=None,
                charged_amounts=charged,
            )
        digest_payload = json.dumps(
            {
                "charged": {
                    "calls": dispatch.charged_amounts.calls,
                    "cost_usd": str(dispatch.charged_amounts.cost_usd),
                    "input_tokens": dispatch.charged_amounts.input_tokens,
                    "output_tokens": dispatch.charged_amounts.output_tokens,
                },
                "kind": result.kind,
                "normalized_payload_digest": dispatch.normalized_payload_digest,
                "provider_response_id": result.provider_response_id,
                "reason_code": result.reason_code,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return cls(
            intent.run_id,
            intent.intent_id,
            intent.logical_turn_id,
            intent.provider_attempt_number,
            intent.request,
            intent.reserved_amounts,
            result.kind,
            result.provider_response_id,
            result.reason_code,
            dispatch.charged_amounts,
            "sha256:" + sha256(digest_payload).hexdigest(),
            dispatch,
        )


class BackoffPort(Protocol):
    def wait(self, seconds: int) -> None:
        raise NotImplementedError


PROVIDER_RETRY_BACKOFF_SECONDS: tuple[int, int] = (1, 2)


class ModelPort(Protocol):
    def complete(self, request: ModelRequest) -> ProviderAttemptResult:
        raise NotImplementedError


def model_request_to_json(request: ModelRequest) -> str:
    return json.dumps(
        {
            "allowed_model_ids": sorted(request.allowed_model_ids),
            "attempt_id": request.attempt_id,
            "budget_digest": request.budget_digest,
            "idempotency_key": request.idempotency_key,
            "max_input_tokens": request.max_input_tokens,
            "max_output_tokens": request.max_output_tokens,
            "prompt": list(request.prompt),
            "plan_digest": request.plan_digest,
            "policy_digest": request.policy_digest,
            "owner_kind": request.owner_kind,
            "request_digest": request.request_digest,
            "requested_model_id": request.requested_model_id,
            "reserved_cost_usd": str(request.reserved_cost_usd),
            "run_id": request.run_id,
            "task_id": request.task_id,
            "tranche_id": request.tranche_id,
            "tool_schema_digest": request.tool_schema_digest,
            "model_configuration_digest": request.model_configuration_digest,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def model_request_from_json(value: str) -> ModelRequest:
    data = json.loads(value)
    owner_keys = {"owner_kind", "task_id", "attempt_id", "tranche_id"}
    if not owner_keys <= data.keys():
        raise ValueError("MODEL_REQUEST_OWNER_FIELDS_MISSING")
    return ModelRequest(
        run_id=RunId(data["run_id"]),
        plan_digest=(None if data["plan_digest"] is None else RevisionDigest(data["plan_digest"])),
        policy_digest=RevisionDigest(data["policy_digest"]),
        budget_digest=RevisionDigest(data["budget_digest"]),
        model_configuration_digest=RevisionDigest(data["model_configuration_digest"]),
        requested_model_id=data["requested_model_id"],
        allowed_model_ids=frozenset(data["allowed_model_ids"]),
        prompt=tuple(data["prompt"]),
        tool_schema_digest=data["tool_schema_digest"],
        request_digest=data["request_digest"],
        idempotency_key=data["idempotency_key"],
        max_input_tokens=data["max_input_tokens"],
        max_output_tokens=data["max_output_tokens"],
        reserved_cost_usd=Decimal(data["reserved_cost_usd"]),
        owner_kind=data["owner_kind"],
        task_id=None if data["task_id"] is None else TaskId(data["task_id"]),
        attempt_id=(None if data["attempt_id"] is None else AttemptId(data["attempt_id"])),
        tranche_id=data["tranche_id"],
    )


def model_recovery_binding_to_json(binding: ModelRecoveryBinding) -> str:
    return json.dumps(
        {
            "budget_digest": binding.budget_digest,
            "model_configuration_digest": binding.model_configuration_digest,
            "plan_digest": binding.plan_digest,
            "policy_digest": binding.policy_digest,
            "request_digest": binding.request_digest,
            "tool_schema_digest": binding.tool_schema_digest,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def model_recovery_binding_from_json(value: str) -> ModelRecoveryBinding:
    data = json.loads(value)
    return ModelRecoveryBinding(
        request_digest=data["request_digest"],
        tool_schema_digest=data["tool_schema_digest"],
        plan_digest=(None if data["plan_digest"] is None else RevisionDigest(data["plan_digest"])),
        policy_digest=RevisionDigest(data["policy_digest"]),
        budget_digest=RevisionDigest(data["budget_digest"]),
        model_configuration_digest=RevisionDigest(data["model_configuration_digest"]),
    )


def model_dispatch_result_to_json(result: ModelDispatchResult) -> str:
    return json.dumps(
        {
            "charged_amounts": json.loads(result.charged_amounts.to_json()),
            "logical_turn_id": result.logical_turn_id,
            "normalized_action": result.normalized_action,
            "normalized_payload_digest": result.normalized_payload_digest,
            "outcome": result.outcome,
            "returned_model_id": result.returned_model_id,
            "run_id": result.run_id,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def model_dispatch_result_from_json(value: str) -> ModelDispatchResult:
    data = json.loads(value)
    return ModelDispatchResult(
        run_id=RunId(data["run_id"]),
        logical_turn_id=data["logical_turn_id"],
        outcome=data["outcome"],
        returned_model_id=data["returned_model_id"],
        normalized_action=data["normalized_action"],
        normalized_payload_digest=data["normalized_payload_digest"],
        charged_amounts=ModelBudgetAmounts.from_json(
            json.dumps(data["charged_amounts"], sort_keys=True, separators=(",", ":"))
        ),
    )


class ModelJournal(Protocol):
    def audit_sequence(self, run_id: RunId) -> AuditSequence:
        raise NotImplementedError

    def committed_model_turn(
        self, run_id: RunId, logical_turn_id: LogicalTurnId
    ) -> CommittedModelTurn | None:
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


class ImmediateBackoff:
    def wait(self, seconds: int) -> None:
        del seconds


class DurableModelClient:
    def __init__(
        self,
        model: ModelPort,
        journal: ModelJournal,
        backoff: BackoffPort | None = None,
    ) -> None:
        self._model = model
        self._journal = journal
        self._backoff = ImmediateBackoff() if backoff is None else backoff
        self.journal = journal

    def recover_committed(
        self,
        run_id: RunId,
        logical_turn_id: LogicalTurnId,
        expected: ModelRecoveryBinding,
    ) -> ModelDispatchResult:
        committed = self._journal.committed_model_turn(run_id, logical_turn_id)
        if committed is None:
            return ModelDispatchResult.blocked(
                run_id=run_id,
                logical_turn_id=logical_turn_id,
                outcome="MODEL_COMPLETION_NOT_COMMITTED",
            )
        if committed.recovery_binding != expected:
            return ModelDispatchResult.blocked(
                run_id=run_id,
                logical_turn_id=logical_turn_id,
                outcome="RECOVERY_BINDING_MISMATCH",
            )
        if committed.downstream_intent_id is not None:
            return ModelDispatchResult.blocked(
                run_id=run_id,
                logical_turn_id=logical_turn_id,
                outcome="DOWNSTREAM_INTENT_REQUIRES_RECONCILIATION",
            )
        return committed.dispatch_result

    def complete(self, request: ModelRequest) -> ModelDispatchResult:
        for retry_index in range(V01_MECHANISM_LIMITS.provider_retry_ceiling + 1):
            sequence = self._journal.audit_sequence(request.run_id)
            if retry_index == 0:
                turn, intent = self._journal.begin_model_turn_and_reserve(request, sequence)
            else:
                intent = self._journal.reserve_model_attempt(
                    turn, request, retry_index + 1, sequence
                )
            attempt_result = self._model.complete(request)
            if isinstance(attempt_result, ModelCompletion):
                attempt_result = ProviderAttemptResult.completed(attempt_result)
            settled = self._journal.settle_model_attempt(
                intent,
                attempt_result,
                self._journal.audit_sequence(request.run_id),
            )
            if settled.kind == ProviderAttemptKind.COMPLETED:
                return settled.dispatch_result
            if (
                settled.kind != ProviderAttemptKind.KNOWN_CLOSED_REJECTION
                or retry_index == V01_MECHANISM_LIMITS.provider_retry_ceiling
            ):
                return settled.dispatch_result
            seconds = PROVIDER_RETRY_BACKOFF_SECONDS[retry_index]
            self._journal.record_model_backoff(
                request.run_id,
                intent.intent_id,
                seconds,
                self._journal.audit_sequence(request.run_id),
            )
            self._backoff.wait(seconds)
        raise AssertionError("closed retry loop exhausted without a dispatch result")
