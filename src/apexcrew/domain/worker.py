from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from hashlib import sha256
from typing import Protocol

from pydantic import ValidationError

from apexcrew.domain.actions import (
    ACTION_ADAPTER,
    ActionEnvelope,
    CheckAction,
    FailAction,
    FinishAction,
    PatchAction,
    ToolActionEnvelope,
)
from apexcrew.domain.authority import (
    Authority,
    AuthorizationDecision,
    AuthorizationRequest,
    ModelReservation,
    ModelReservationRequest,
    TaskBudgetState,
    TaskStopDecision,
)
from apexcrew.domain.commands import ApplicableRevisionDigests, RuntimeDecision, RuntimePermit
from apexcrew.domain.effects import EffectIntent, EffectResult, canonical_json, sha256_digest
from apexcrew.domain.limits import V01_MECHANISM_LIMITS
from apexcrew.domain.model import (
    LogicalTurnId,
    ModelCounters,
    ModelDispatchResult,
    ModelRequest,
    RecoveredModelAction,
)
from apexcrew.domain.revisions import Sha256DigestText
from apexcrew.domain.tools import ActionPreState, ToolIntent, ToolResult
from apexcrew.domain.types import (
    AttemptId,
    AuditSequence,
    IntentId,
    RevisionDigest,
    RunId,
    TaskId,
)

MAX_WORKER_FEEDBACK_BYTES = 8_192


def normalized_action_digest(action: ActionEnvelope) -> Sha256DigestText:
    payload = json.dumps(
        action.model_dump(mode="json", exclude_none=True),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return Sha256DigestText("sha256:" + sha256(payload).hexdigest())


def bounded_worker_feedback(result: ToolResult) -> str:
    payload = canonical_json(
        {
            "code": result.code,
            "passed": result.passed,
            "timed_out": result.timed_out,
            "bounded_payload": result.bounded_payload,
        }
    )
    encoded = payload.encode("utf-8")
    if len(encoded) <= MAX_WORKER_FEEDBACK_BYTES:
        return payload
    suffix = b'..."truncated":true}'
    prefix = encoded[: MAX_WORKER_FEEDBACK_BYTES - len(suffix)]
    return prefix.decode("utf-8", errors="ignore") + suffix.decode("ascii")


def worker_request_with_feedback(request: ModelRequest, feedback: str | None) -> ModelRequest:
    if feedback is None:
        return request
    feedback_binding = sha256_digest(
        canonical_json(
            {
                "base_request_digest": request.request_digest,
                "feedback": feedback,
            }
        )
    )
    return replace(
        request,
        prompt=request.prompt + ({"role": "tool", "content": feedback},),
        request_digest=feedback_binding,
        idempotency_key=f"{request.idempotency_key}:feedback:{feedback_binding}",
    )


def validate_authorized_worker_action(
    binding: WorkerTurnBinding,
    intent: ToolIntent,
    request: AuthorizationRequest,
    decision: AuthorizationDecision,
    expected_prestate: ActionPreState,
) -> None:
    expected_timeout = (
        V01_MECHANISM_LIMITS.check_timeout_seconds
        if request.action.kind == "check"
        else V01_MECHANISM_LIMITS.ordinary_action_timeout_seconds
    )
    if (
        request.run_id != binding.run_id
        or request.task_id != binding.task_id
        or request.attempt_id != binding.attempt_id
        or request.lease_id != binding.lease_id
        or request.lease_generation != binding.lease_generation
        or request.admissible_head != binding.admissible_head
        or request.task_contract_digest != binding.task_contract_digest
        or request.plan_digest != binding.plan_digest
        or request.policy_digest != binding.policy_digest
        or request.budget_digest != binding.budget_digest
        or request.model_configuration_digest != binding.model_configuration_digest
        or request.tool_schema_digest != binding.tool_schema_digest
        or request.target_safety_digest != binding.target_safety_digest
        or request.action_digest != normalized_action_digest(request.action)
        or request.expected_prestate_digest != sha256_digest(expected_prestate.canonical_json())
        or request.deadline_at_utc != request.started_at_utc + timedelta(seconds=expected_timeout)
    ):
        raise ValueError("WORKER_AUTHORIZATION_REQUEST_BINDING_MISMATCH")
    if (
        decision.decision != "ALLOW"
        or decision.reason != "AUTHORIZED"
        or decision.persistence != "WITH_EFFECT_INTENT"
        or decision.run_id != request.run_id
        or decision.task_id != request.task_id
        or decision.attempt_id != request.attempt_id
        or decision.action_id != request.action_id
        or decision.action_digest != request.action_digest
        or decision.approved_timeout_seconds != expected_timeout
        or decision.deadline_at_utc != request.deadline_at_utc
        or decision.resulting_sequence is not None
    ):
        raise ValueError("WORKER_AUTHORIZATION_DECISION_BINDING_MISMATCH")
    if (
        intent.run_id != binding.run_id
        or intent.task_id != binding.task_id
        or intent.attempt_id != binding.attempt_id
        or intent.action_id != request.action_id
        or intent.action != request.action
        or intent.authorization_binding_digest != decision.binding_digest
        or intent.applicable_revision_digests != binding.applicable_revision_digests
        or intent.repository_id != binding.repository_id
        or (
            not isinstance(request.action, (CheckAction, PatchAction))
            and intent.snapshot_digest != binding.snapshot_digest
        )
        or intent.scope_digest != binding.scope_digest
        or intent.dependency_fingerprint_basis != binding.dependency_fingerprint_basis
        or intent.expected_prestate_json != expected_prestate.canonical_json()
    ):
        raise ValueError("WORKER_TOOL_INTENT_BINDING_MISMATCH")


def terminal_worker_effects(
    binding: WorkerTurnBinding,
    logical_turn_id: LogicalTurnId,
    action: FinishAction | FailAction,
    action_digest: str,
    authorization: AuthorizationDecision,
    sequence: AuditSequence,
) -> tuple[EffectIntent, EffectResult]:
    action_class = "FINISH" if isinstance(action, FinishAction) else "FAIL"
    if (
        authorization.decision != "ALLOW"
        or authorization.reason != "AUTHORIZED"
        or authorization.persistence != "WITH_EFFECT_INTENT"
        or authorization.run_id != binding.run_id
        or authorization.task_id != binding.task_id
        or authorization.attempt_id != binding.attempt_id
        or authorization.action_digest != action_digest
        or action_digest != normalized_action_digest(action)
        or authorization.action_class != action_class
        or authorization.approved_timeout_seconds
        != V01_MECHANISM_LIMITS.ordinary_action_timeout_seconds
    ):
        raise ValueError("WORKER_TERMINAL_AUTHORIZATION_BINDING_MISMATCH")
    identity = canonical_json(
        {
            "attempt_id": binding.attempt_id,
            "logical_turn_id": logical_turn_id,
            "run_id": binding.run_id,
        }
    )
    intent_id = IntentId("worker-terminal-" + sha256(identity.encode("utf-8")).hexdigest())
    payload = canonical_json(
        {
            "action": action.model_dump(mode="json", exclude_none=True),
            "authorization_binding_digest": authorization.binding_digest,
            "logical_turn_id": logical_turn_id,
        }
    )
    intent = EffectIntent(
        intent_id=intent_id,
        run_id=binding.run_id,
        kind="WORKER_TERMINAL_ACTION",
        idempotency_key=(
            f"worker-terminal:{binding.run_id}:{binding.attempt_id}:{logical_turn_id}"
        ),
        applicable_revision_digests=binding.applicable_revision_digests,
        payload_digest=sha256_digest(payload),
        normalized_payload_json=payload,
        recorded_sequence=sequence,
        task_id=binding.task_id,
        attempt_id=binding.attempt_id,
        action_id=authorization.action_id,
    )
    result_payload = canonical_json(
        {
            "attempt_state": "SUCCEEDED" if isinstance(action, FinishAction) else "FAILED",
            "result_class": "WORKER_FINISHED"
            if isinstance(action, FinishAction)
            else "WORKER_FAILED",
        }
    )
    result = EffectResult(
        intent_id=intent_id,
        run_id=binding.run_id,
        outcome="COMPLETED",
        result_class=("WORKER_FINISHED" if isinstance(action, FinishAction) else "WORKER_FAILED"),
        result_digest=sha256_digest(result_payload),
        bounded_result_json=result_payload,
        settled_sequence=sequence,
    )
    return intent, result


def pending_worker_action_id(
    binding: WorkerTurnBinding,
    request: AuthorizationRequest,
    decision: AuthorizationDecision,
    expected_prestate: ActionPreState,
) -> str:
    expected_timeout = (
        V01_MECHANISM_LIMITS.check_timeout_seconds
        if request.action.kind == "check"
        else V01_MECHANISM_LIMITS.ordinary_action_timeout_seconds
    )
    if (
        request.run_id != binding.run_id
        or request.task_id != binding.task_id
        or request.attempt_id != binding.attempt_id
        or request.lease_id != binding.lease_id
        or request.lease_generation != binding.lease_generation
        or request.admissible_head != binding.admissible_head
        or request.task_contract_digest != binding.task_contract_digest
        or request.plan_digest != binding.plan_digest
        or request.policy_digest != binding.policy_digest
        or request.budget_digest != binding.budget_digest
        or request.model_configuration_digest != binding.model_configuration_digest
        or request.tool_schema_digest != binding.tool_schema_digest
        or request.target_safety_digest != binding.target_safety_digest
        or request.action_digest != normalized_action_digest(request.action)
        or request.expected_prestate_digest != sha256_digest(expected_prestate.canonical_json())
        or request.deadline_at_utc != request.started_at_utc + timedelta(seconds=expected_timeout)
        or decision.decision != "REQUIRE_APPROVAL"
        or decision.reason != "APPROVAL_REQUIRED"
        or decision.persistence != "WITH_PENDING_ACTION"
        or decision.run_id != request.run_id
        or decision.task_id != request.task_id
        or decision.attempt_id != request.attempt_id
        or decision.action_id != request.action_id
        or decision.action_digest != request.action_digest
        or decision.approved_timeout_seconds != expected_timeout
        or decision.deadline_at_utc != request.deadline_at_utc
        or decision.resulting_sequence is not None
    ):
        raise ValueError("WORKER_PENDING_ACTION_BINDING_MISMATCH")
    identity = canonical_json(
        {
            "action_id": request.action_id,
            "attempt_id": binding.attempt_id,
            "authorization_binding_digest": decision.binding_digest,
            "logical_turn_id": request.logical_turn_id,
            "run_id": binding.run_id,
        }
    )
    return "pending-worker-" + sha256(identity.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class WorkerAttemptSnapshot:
    run_id: RunId
    task_id: TaskId
    attempt_id: AttemptId
    applicable_revision_digests: ApplicableRevisionDigests


@dataclass(frozen=True, slots=True)
class WorkerAttemptRecord:
    run_id: RunId
    task_id: TaskId
    attempt_id: AttemptId
    plan_digest: RevisionDigest
    policy_digest: RevisionDigest
    budget_digest: RevisionDigest
    model_configuration_digest: RevisionDigest
    tool_schema_digest: Sha256DigestText
    target_safety_digest: Sha256DigestText
    credential_profile: str | None
    task_contract_digest: Sha256DigestText
    base_run_head_oid: str
    worker_slot: str
    state: str
    created_sequence: AuditSequence


@dataclass(frozen=True, slots=True)
class WorkerTaskRecord:
    run_id: RunId
    task_id: TaskId
    state: str
    pause_reason: str | None = None


@dataclass(frozen=True, slots=True)
class WorkerTurnBinding:
    run_id: RunId
    task_id: TaskId
    attempt_id: AttemptId
    tranche_id: str
    lease_id: str
    lease_generation: int
    admissible_head: str
    task_contract_digest: Sha256DigestText
    plan_digest: RevisionDigest
    policy_digest: RevisionDigest
    budget_digest: RevisionDigest
    model_configuration_digest: RevisionDigest
    tool_schema_digest: Sha256DigestText
    target_safety_digest: Sha256DigestText
    credential_profile: str | None
    repository_id: str
    snapshot_digest: Sha256DigestText
    scope_digest: Sha256DigestText
    dependency_fingerprint_basis: Sha256DigestText

    @property
    def applicable_revision_digests(self) -> ApplicableRevisionDigests:
        return ApplicableRevisionDigests(
            plan_digest=self.plan_digest,
            policy_digest=self.policy_digest,
            budget_digest=self.budget_digest,
            model_configuration_digest=self.model_configuration_digest,
        )


@dataclass(frozen=True, slots=True)
class WorkerActionRecord:
    action_id: str
    run_id: RunId
    task_id: TaskId
    attempt_id: AttemptId
    logical_turn_id: LogicalTurnId
    normalized_action_digest: Sha256DigestText
    expected_prestate_digest: Sha256DigestText
    authorization_binding_digest: Sha256DigestText
    deadline_at_utc: datetime
    intent_id: IntentId | None
    result_intent_id: IntentId | None
    created_sequence: AuditSequence


@dataclass(frozen=True, slots=True)
class PendingActionFreeze:
    pending_id: str
    resulting_sequence: AuditSequence


class WorkerContextBuilder(Protocol):
    def build_current(self, attempt_id: AttemptId) -> object: ...


class WorkerRequestFactory(Protocol):
    def for_attempt(self, attempt_id: AttemptId, capsule: object) -> ModelRequest: ...


class WorkerModelClient(Protocol):
    def complete(
        self, request: ModelReservationRequest
    ) -> ModelDispatchResult | ModelReservation: ...


class WorkerActionPort(Protocol):
    def parse_one(self, action: Mapping[str, object]) -> ToolActionEnvelope | None: ...

    def capture_expected_prestate(
        self, binding: WorkerTurnBinding, action: ToolActionEnvelope
    ) -> ActionPreState: ...

    def capture_snapshot_digest(
        self, binding: WorkerTurnBinding, action: ToolActionEnvelope
    ) -> Sha256DigestText: ...


class WorkerActionCodec:
    def __init__(
        self,
        prestate: Callable[[WorkerTurnBinding, ToolActionEnvelope], ActionPreState],
        snapshot_digest: Callable[[WorkerTurnBinding, ToolActionEnvelope], Sha256DigestText]
        | None = None,
    ) -> None:
        self._prestate = prestate
        self._snapshot_digest = snapshot_digest

    def parse_one(self, action: Mapping[str, object]) -> ToolActionEnvelope | None:
        try:
            return ACTION_ADAPTER.validate_python(action)
        except ValidationError:
            return None

    def capture_expected_prestate(
        self, binding: WorkerTurnBinding, action: ToolActionEnvelope
    ) -> ActionPreState:
        return self._prestate(binding, action)

    def capture_snapshot_digest(
        self, binding: WorkerTurnBinding, action: ToolActionEnvelope
    ) -> Sha256DigestText:
        if self._snapshot_digest is None:
            return binding.snapshot_digest
        return self._snapshot_digest(binding, action)


class WorkerAttemptState(Protocol):
    def current_worker_turn_binding(self, attempt_id: AttemptId) -> WorkerTurnBinding: ...

    def latest_worker_feedback(self, attempt_id: AttemptId) -> str | None: ...

    def record_malformed_worker_action(
        self,
        *,
        binding: WorkerTurnBinding,
        logical_turn_id: LogicalTurnId,
        action_digest: str,
        recovered_marker: EffectIntent | None,
        permit: RuntimePermit | None,
        expected_sequence: AuditSequence,
    ) -> TaskStopDecision: ...

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
    ) -> ToolIntent: ...

    def settle_recovered_action_denial(
        self,
        *,
        binding: WorkerTurnBinding,
        marker: EffectIntent,
        permit: RuntimePermit,
        decision: AuthorizationDecision,
        expected_sequence: AuditSequence,
    ) -> AuditSequence: ...

    def freeze_authorized_pending_action(
        self,
        *,
        request: AuthorizationRequest,
        decision: AuthorizationDecision,
        expected_prestate: ActionPreState,
        recovered_marker: EffectIntent | None,
        permit: RuntimePermit | None,
        expected_sequence: AuditSequence,
    ) -> PendingActionFreeze: ...

    def settle_worker_action(
        self,
        *,
        intent: ToolIntent,
        authorization: AuthorizationDecision,
        result: ToolResult,
        expected_sequence: AuditSequence,
    ) -> AuditSequence: ...

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
    ) -> RuntimeDecision: ...


class WorkerJournal(Protocol):
    def audit_sequence(self, run_id: RunId) -> AuditSequence: ...

    def model_counters(self, run_id: RunId) -> ModelCounters: ...

    def task_budget_state(self, run_id: RunId, task_id: TaskId) -> TaskBudgetState: ...


class WorkerIdSource(Protocol):
    def next_action_id(self, run_id: RunId) -> str: ...

    def next_intent_id(self, run_id: RunId) -> IntentId: ...


class WorkerToolRuntime(Protocol):
    def execute(self, intent: ToolIntent) -> ToolResult: ...


class WorkerLoopService:
    def __init__(
        self,
        *,
        attempts: WorkerAttemptState,
        capsules: WorkerContextBuilder,
        requests: WorkerRequestFactory,
        models: WorkerModelClient,
        actions: WorkerActionPort,
        authority: Authority,
        tools: WorkerToolRuntime,
        journal: WorkerJournal,
        ids: WorkerIdSource,
        clock: Callable[[], datetime],
    ) -> None:
        self._attempts = attempts
        self._capsules = capsules
        self._requests = requests
        self._models = models
        self._actions = actions
        self._authority = authority
        self._tools = tools
        self._journal = journal
        self._ids = ids
        self._clock = clock

    def run_turn(self, attempt_id: AttemptId) -> RuntimeDecision:
        binding = self._attempts.current_worker_turn_binding(attempt_id)
        capsule = self._capsules.build_current(attempt_id)
        model_request = replace(
            worker_request_with_feedback(
                self._requests.for_attempt(attempt_id, capsule),
                self._attempts.latest_worker_feedback(attempt_id),
            ),
            owner_kind="WORKER",
            task_id=binding.task_id,
            attempt_id=binding.attempt_id,
            tranche_id=binding.tranche_id,
        )
        started_at = self._clock()
        reservation_request = ModelReservationRequest(
            run_id=binding.run_id,
            owner_kind="WORKER",
            task_id=binding.task_id,
            attempt_id=binding.attempt_id,
            tranche_id=binding.tranche_id,
            turn=None,
            model_request=model_request,
            provider_attempt_number=1,
            target_safety_digest=binding.target_safety_digest,
            credential_profile=binding.credential_profile,
            expected_run_counters=self._journal.model_counters(binding.run_id),
            expected_task_counters=self._journal.task_budget_state(binding.run_id, binding.task_id),
            started_at_utc=started_at,
            deadline_at_utc=started_at
            + timedelta(seconds=V01_MECHANISM_LIMITS.ordinary_action_timeout_seconds),
            expected_sequence=self._journal.audit_sequence(binding.run_id),
        )
        model_outcome = self._models.complete(reservation_request)
        if isinstance(model_outcome, ModelReservation):
            return RuntimeDecision.pause(model_outcome.reason, model_outcome.resulting_sequence)
        if (
            model_outcome.outcome != "COMPLETED"
            or model_outcome.normalized_action is None
            or model_outcome.normalized_payload_digest is None
        ):
            return RuntimeDecision.pause(model_outcome.outcome)
        return self._consume_released_worker_action(
            binding,
            logical_turn_id=model_outcome.logical_turn_id,
            normalized_action=model_outcome.normalized_action,
            normalized_payload_digest=model_outcome.normalized_payload_digest,
            recovered_marker=None,
            permit=None,
        )

    def resume_recovered_worker_action(
        self,
        run_id: RunId,
        permit: RuntimePermit,
        recovered: RecoveredModelAction,
    ) -> RuntimeDecision:
        if (
            recovered.turn.owner_kind != "WORKER"
            or recovered.turn.run_id != run_id
            or recovered.turn.task_id is None
            or recovered.turn.attempt_id is None
            or recovered.turn.tranche_id is None
        ):
            raise ValueError("RECOVERED_WORKER_OWNER_BINDING_MISMATCH")
        binding = self._attempts.current_worker_turn_binding(recovered.turn.attempt_id)
        if (
            binding.run_id != run_id
            or binding.task_id != recovered.turn.task_id
            or binding.attempt_id != recovered.turn.attempt_id
            or binding.tranche_id != recovered.turn.tranche_id
        ):
            raise ValueError("RECOVERED_WORKER_ATTEMPT_BINDING_MISMATCH")
        return self._consume_released_worker_action(
            binding,
            logical_turn_id=recovered.turn.logical_turn_id,
            normalized_action=recovered.normalized_action,
            normalized_payload_digest=recovered.turn.normalized_output_digest,
            recovered_marker=recovered.effect_intent,
            permit=permit,
        )

    def _consume_released_worker_action(
        self,
        binding: WorkerTurnBinding,
        *,
        logical_turn_id: LogicalTurnId,
        normalized_action: Mapping[str, object],
        normalized_payload_digest: str,
        recovered_marker: EffectIntent | None,
        permit: RuntimePermit | None,
    ) -> RuntimeDecision:
        if (recovered_marker is None) != (permit is None):
            raise ValueError("RECOVERED_MARKER_PERMIT_BINDING_MISMATCH")
        action = self._actions.parse_one(normalized_action)
        if action is None:
            return self._record_malformed_worker_action(
                binding=binding,
                logical_turn_id=logical_turn_id,
                action_digest=normalized_payload_digest,
                recovered_marker=recovered_marker,
                permit=permit,
            )
        try:
            prestate = self._actions.capture_expected_prestate(binding, action)
            snapshot_digest = self._actions.capture_snapshot_digest(binding, action)
        except Exception:  # noqa: BLE001 - capture failures must settle as malformed actions
            return self._record_malformed_worker_action(
                binding=binding,
                logical_turn_id=logical_turn_id,
                action_digest=normalized_payload_digest,
                recovered_marker=recovered_marker,
                permit=permit,
            )
        prestate_digest = sha256_digest(prestate.canonical_json())
        action_started_at = self._clock()
        timeout_seconds = (
            V01_MECHANISM_LIMITS.check_timeout_seconds
            if action.kind == "check"
            else V01_MECHANISM_LIMITS.ordinary_action_timeout_seconds
        )
        authorization_request = AuthorizationRequest(
            run_id=binding.run_id,
            task_id=binding.task_id,
            attempt_id=binding.attempt_id,
            logical_turn_id=logical_turn_id,
            action_id=self._ids.next_action_id(binding.run_id),
            action=action,
            authority_origin="WORKER",
            action_digest=normalized_action_digest(action),
            expected_prestate_digest=prestate_digest,
            lease_id=binding.lease_id,
            lease_generation=binding.lease_generation,
            admissible_head=binding.admissible_head,
            task_contract_digest=binding.task_contract_digest,
            plan_digest=binding.plan_digest,
            policy_digest=binding.policy_digest,
            budget_digest=binding.budget_digest,
            model_configuration_digest=binding.model_configuration_digest,
            tool_schema_digest=binding.tool_schema_digest,
            target_safety_digest=binding.target_safety_digest,
            started_at_utc=action_started_at,
            deadline_at_utc=action_started_at + timedelta(seconds=timeout_seconds),
            expected_sequence=self._journal.audit_sequence(binding.run_id),
        )
        decision = self._authority.authorize_action(authorization_request)
        if decision.decision == "DENY":
            if recovered_marker is not None:
                assert permit is not None
                sequence = self._attempts.settle_recovered_action_denial(
                    binding=binding,
                    marker=recovered_marker,
                    permit=permit,
                    decision=decision,
                    expected_sequence=self._journal.audit_sequence(binding.run_id),
                )
                return RuntimeDecision.denied(decision.reason, sequence)
            return RuntimeDecision.denied(decision.reason, decision.resulting_sequence)
        if isinstance(action, (FinishAction, FailAction)):
            if decision.decision != "ALLOW":
                raise AssertionError("TERMINAL_ACTION_REQUIRES_DIRECT_ALLOW")
            return self._attempts.finish_attempt(
                binding=binding,
                logical_turn_id=logical_turn_id,
                action=action,
                action_digest=normalized_action_digest(action),
                authorization=decision,
                recovered_marker=recovered_marker,
                permit=permit,
                expected_sequence=authorization_request.expected_sequence,
            )
        if decision.decision == "REQUIRE_APPROVAL":
            frozen = self._attempts.freeze_authorized_pending_action(
                request=authorization_request,
                decision=decision,
                expected_prestate=prestate,
                recovered_marker=recovered_marker,
                permit=permit,
                expected_sequence=authorization_request.expected_sequence,
            )
            return RuntimeDecision.pause("AWAITING_ACTION_APPROVAL", frozen.resulting_sequence)
        intent = ToolIntent.for_authorized_worker_action(
            intent_id=self._ids.next_intent_id(binding.run_id),
            run_id=binding.run_id,
            task_id=binding.task_id,
            attempt_id=binding.attempt_id,
            action_id=authorization_request.action_id,
            action=action,
            authorization_binding_digest=decision.binding_digest,
            applicable_revision_digests=binding.applicable_revision_digests,
            repository_id=binding.repository_id,
            snapshot_digest=snapshot_digest,
            scope_digest=binding.scope_digest,
            dependency_fingerprint_basis=binding.dependency_fingerprint_basis,
            idempotency_key=(
                f"worker-tool:{binding.run_id}:{binding.attempt_id}:{logical_turn_id}"
            ),
            expected_prestate_json=prestate.canonical_json(),
        )
        recorded = self._attempts.record_authorized_worker_action(
            intent=intent,
            request=authorization_request,
            decision=decision,
            expected_prestate=prestate,
            recovered_marker=recovered_marker,
            permit=permit,
            expected_sequence=authorization_request.expected_sequence,
        )
        if recorded != intent:
            raise AssertionError("RECORDED_WORKER_TOOL_INTENT_MISMATCH")
        if action.kind == "check":
            self._authority.open_action_deadline(
                intent.run_id,
                intent.intent_id,
                self._journal.audit_sequence(intent.run_id),
            )
        return self._execute_and_settle(intent, decision)

    def _record_malformed_worker_action(
        self,
        *,
        binding: WorkerTurnBinding,
        logical_turn_id: LogicalTurnId,
        action_digest: str,
        recovered_marker: EffectIntent | None,
        permit: RuntimePermit | None,
    ) -> RuntimeDecision:
        stopped = self._attempts.record_malformed_worker_action(
            binding=binding,
            logical_turn_id=logical_turn_id,
            action_digest=action_digest,
            recovered_marker=recovered_marker,
            permit=permit,
            expected_sequence=self._journal.audit_sequence(binding.run_id),
        )
        if stopped.decision == "CONTINUE":
            return RuntimeDecision.invalid_planning_action(stopped.resulting_sequence)
        return RuntimeDecision.pause(
            stopped.pause_reason or "TASK_ATTEMPT_PAUSED",
            stopped.resulting_sequence,
        )

    def _execute_and_settle(
        self, intent: ToolIntent, decision: AuthorizationDecision
    ) -> RuntimeDecision:
        try:
            result = self._tools.execute(intent)
        except Exception:  # noqa: BLE001 - an execution fault must settle this intent
            result = ToolResult(
                code="INFRASTRUCTURE_UNCERTAINTY",
                run_id=intent.run_id,
                intent_id=intent.intent_id,
                timed_out=True,
                bounded_payload={
                    "reason": "WORKER_TOOL_EXECUTION_UNCERTAIN",
                    "snapshot_digest": intent.snapshot_digest,
                },
            )
        if result.code == "APPROVAL_REQUIRED":
            raise AssertionError("allowed intent returned approval-required")
        sequence = self._attempts.settle_worker_action(
            intent=intent,
            authorization=decision,
            result=result,
            expected_sequence=self._journal.audit_sequence(intent.run_id),
        )
        if result.code in {
            "INDETERMINATE",
            "EXECUTOR_UNAVAILABLE",
            "INFRASTRUCTURE_UNCERTAINTY",
        }:
            return RuntimeDecision.pause(result.code, sequence)
        return RuntimeDecision(
            code="ACTION_RECORDED",
            stop_reason=None,
            resulting_sequence=sequence,
        )
