from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256
from typing import Literal, Protocol
from uuid import uuid4

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
class ModelCompletion:
    response_id: str
    requested_model_id: str
    returned_model_id: str
    usage: ModelUsage | None
    normalized_action: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class ModelBudgetAmounts:
    calls: int
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal


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
    state: Literal["RESERVED"] = "RESERVED"

    @classmethod
    def reserve(
        cls,
        turn: LogicalModelTurn | ModelRequest,
        request: ModelRequest | None = None,
        provider_attempt_number: int = 1,
    ) -> ModelRequestIntent:
        del provider_attempt_number
        actual_request = turn if isinstance(turn, ModelRequest) else request
        if actual_request is None:
            raise ValueError("MODEL_REQUEST_REQUIRED")
        logical_turn_id = (
            f"model-turn-{uuid4().hex}" if isinstance(turn, ModelRequest) else turn.logical_turn_id
        )
        return cls(
            run_id=actual_request.run_id,
            intent_id=IntentId(f"model-intent-{uuid4().hex}"),
            logical_turn_id=logical_turn_id,
            request=actual_request,
            reserved_amounts=ModelBudgetAmounts(
                calls=1,
                input_tokens=actual_request.max_input_tokens,
                output_tokens=actual_request.max_output_tokens,
                cost_usd=actual_request.reserved_cost_usd,
            ),
        )


@dataclass(frozen=True, slots=True)
class ModelDispatchResult:
    run_id: RunId
    logical_turn_id: LogicalTurnId
    outcome: Literal[
        "COMPLETED",
        "KNOWN_CLOSED_REJECTION",
        "RETURNED_MODEL_MISMATCH",
        "INDETERMINATE",
        "MODEL_COMPLETION_NOT_COMMITTED",
        "RECOVERY_BINDING_MISMATCH",
        "DOWNSTREAM_INTENT_REQUIRES_RECONCILIATION",
    ]
    returned_model_id: str | None
    normalized_action: Mapping[str, object] | None
    normalized_payload_digest: str | None
    charged_amounts: ModelBudgetAmounts


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


class ModelPort(Protocol):
    def complete(self, request: ModelRequest) -> ModelCompletion:
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


class ModelJournal(Protocol):
    def audit_sequence(self, run_id: RunId) -> AuditSequence:
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


class DurableModelClient:
    def __init__(self, model: ModelPort, journal: ModelJournal) -> None:
        self._model = model
        self._journal = journal
        self.journal = journal

    def complete(self, request: ModelRequest) -> ModelDispatchResult:
        intent = self._journal.reserve_model_request(
            request,
            self._journal.audit_sequence(request.run_id),
        )
        completion = self._model.complete(request)
        return self._journal.settle_model_request(
            intent,
            completion,
            request.allowed_model_ids,
            self._journal.audit_sequence(request.run_id),
        )
