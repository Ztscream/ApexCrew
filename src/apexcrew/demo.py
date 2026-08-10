from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from typing import TypedDict

from apexcrew.adapters.executor.fake import FakeExecutor, FakeProcessResult
from apexcrew.adapters.executor.memory_patch import MemoryPatchExecutor
from apexcrew.adapters.model.scripted import ScriptedMockLLM
from apexcrew.adapters.repository.snapshot import MemoryRepositorySnapshot
from apexcrew.domain.actions import ActionEnvelope, FailAction, FinishAction
from apexcrew.domain.authority import (
    ActionClass,
    ActionDeadline,
    AuthorizationDecision,
    AuthorizationRequest,
    AuthorizedActionClass,
    ModelReservation,
    ModelReservationRequest,
    TaskBudgetState,
    TaskStopDecision,
    TimeoutDecision,
    WorkspaceLease,
)
from apexcrew.domain.commands import RuntimeDecision, RuntimePermit
from apexcrew.domain.effects import EffectIntent, canonical_json, sha256_digest
from apexcrew.domain.evidence import ContextCapsule, EvidenceReceipt
from apexcrew.domain.freshness import FreshnessAssessment
from apexcrew.domain.limits import V01_MECHANISM_LIMITS
from apexcrew.domain.model import (
    LogicalTurnId,
    ModelBudgetAmounts,
    ModelCompletion,
    ModelCounters,
    ModelDispatchResult,
    ModelRequest,
    ProviderAttemptResult,
)
from apexcrew.domain.plan import CheckDefinition, GlobPattern
from apexcrew.domain.policy import ActionPolicy, SecretPathPolicy
from apexcrew.domain.revisions import Sha256DigestText
from apexcrew.domain.tools import (
    ActionPreState,
    DeclaredCheckRegistry,
    SanitizedSnapshot,
    SanitizedSnapshotEntry,
    ScopedToolRuntime,
    ToolIntent,
    ToolResult,
)
from apexcrew.domain.types import AttemptId, AuditSequence, IntentId, RevisionDigest, RunId, TaskId
from apexcrew.domain.worker import (
    PendingActionFreeze,
    WorkerActionCodec,
    WorkerLoopService,
    WorkerTurnBinding,
    bounded_worker_feedback,
    validate_authorized_worker_action,
)

_SHA = "sha256:" + "a" * 64
_SNAPSHOT_DIGEST = _SHA
_DEPENDENCY_DIGEST = "sha256:" + "b" * 64
_AUTHORIZATION_DIGEST = "sha256:" + "c" * 64
_TASK_DIGEST = "sha256:" + "d" * 64
_REVISION = RevisionDigest(_SHA)
_CLOCK = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)


class DemoEvent(TypedDict):
    behavior: str
    action: str
    decision: str
    first_action: str
    next_action: str
    status: str
    model_calls: str
    feedback_bound: str
    loop_turns: str
    tool_executions: str
    first_turn_result: str
    next_turn_result: str
    feedback_role: str
    first_turn_feedback_absent: str
    repaired_path: str
    repaired: str


def _binding() -> WorkerTurnBinding:
    return WorkerTurnBinding(
        run_id=RunId("demo-run"),
        task_id=TaskId("demo-task"),
        attempt_id=AttemptId("demo-attempt"),
        tranche_id="demo-tranche",
        lease_id="demo-lease",
        lease_generation=1,
        admissible_head="1" * 40,
        task_contract_digest=_TASK_DIGEST,
        plan_digest=_REVISION,
        policy_digest=_REVISION,
        budget_digest=_REVISION,
        model_configuration_digest=_REVISION,
        tool_schema_digest=_REVISION,
        target_safety_digest=_REVISION,
        credential_profile=None,
        repository_id="demo-repository",
        snapshot_digest=_SNAPSHOT_DIGEST,
        scope_digest=_REVISION,
        dependency_fingerprint_basis=_DEPENDENCY_DIGEST,
    )


@dataclass
class _DemoJournal:
    sequence: int = 0
    deadlines: dict[IntentId, ActionDeadline] = field(default_factory=dict)

    def audit_sequence(self, run_id: RunId) -> AuditSequence:
        del run_id
        return AuditSequence(self.sequence)

    def model_counters(self, run_id: RunId) -> ModelCounters:
        del run_id
        return ModelCounters()

    def task_budget_state(self, run_id: RunId, task_id: TaskId) -> TaskBudgetState:
        return TaskBudgetState(run_id=run_id, task_id=task_id)

    def action_deadline(self, intent_id: IntentId) -> ActionDeadline | None:
        return self.deadlines.get(intent_id)


class _DemoAuthority:
    def __init__(self, binding: WorkerTurnBinding, journal: _DemoJournal) -> None:
        self._binding = binding
        self._journal = journal
        self._policy = ActionPolicy.default(SecretPathPolicy.from_host_rules((), b"k" * 32))

    def authorize_action(self, request: AuthorizationRequest) -> AuthorizationDecision:
        if self._policy.classify(request.action) != "ALLOW":
            raise AssertionError("demo action must be allowed by the real policy")
        action_class: AuthorizedActionClass = (
            "DECLARED_CHECK" if request.action.kind == "check" else "PATCH"
        )
        timeout_seconds = (
            V01_MECHANISM_LIMITS.check_timeout_seconds
            if request.action.kind == "check"
            else V01_MECHANISM_LIMITS.ordinary_action_timeout_seconds
        )
        return AuthorizationDecision(
            decision="ALLOW",
            reason="AUTHORIZED",
            run_id=request.run_id,
            task_id=request.task_id,
            attempt_id=request.attempt_id,
            action_id=request.action_id,
            action_digest=request.action_digest,
            binding_digest=_AUTHORIZATION_DIGEST,
            action_class=action_class,
            approved_timeout_seconds=timeout_seconds,
            deadline_at_utc=request.deadline_at_utc,
            persistence="WITH_EFFECT_INTENT",
            effect_intent_id=None,
            pending_action_id=None,
            resulting_sequence=None,
        )

    def open_action_deadline(
        self, run_id: RunId, intent_id: IntentId, expected_sequence: AuditSequence
    ) -> ActionDeadline:
        deadline = ActionDeadline(
            run_id=run_id,
            intent_id=intent_id,
            budget_digest=self._binding.budget_digest,
            applicable_revision_digests=self._binding.applicable_revision_digests,
            action_class=ActionClass.DECLARED_CHECK,
            started_at=_CLOCK,
            expires_at=_CLOCK + timedelta(seconds=V01_MECHANISM_LIMITS.check_timeout_seconds),
            recorded_sequence=AuditSequence(int(expected_sequence) + 1),
            check_id="fixture",
            snapshot_digest=self._binding.snapshot_digest,
        )
        self._journal.deadlines[intent_id] = deadline
        return deadline

    def reserve_model_attempt(self, request: ModelReservationRequest) -> ModelReservation:
        del request
        raise AssertionError("demo model client owns model reservation")

    def deadline_state(self, deadline: ActionDeadline) -> str:
        del deadline
        raise AssertionError("demo check executor does not time out")

    def settle_timeout(
        self,
        deadline: ActionDeadline,
        outcome_observable: bool,
        expected_sequence: AuditSequence,
    ) -> TimeoutDecision:
        del deadline, outcome_observable, expected_sequence
        raise AssertionError("demo check executor does not time out")


@dataclass
class _DemoAttempts:
    binding: WorkerTurnBinding
    journal: _DemoJournal
    feedback: str | None = None
    results: list[ToolResult] = field(default_factory=list)

    def current_worker_turn_binding(self, attempt_id: AttemptId) -> WorkerTurnBinding:
        if attempt_id != self.binding.attempt_id:
            raise AssertionError("demo attempt mismatch")
        return self.binding

    def latest_worker_feedback(self, attempt_id: AttemptId) -> str | None:
        if attempt_id != self.binding.attempt_id:
            raise AssertionError("demo attempt mismatch")
        return self.feedback

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
        del logical_turn_id, action_digest, recovered_marker, permit
        if binding != self.binding:
            raise AssertionError("demo binding mismatch")
        sequence = AuditSequence(int(expected_sequence) + 1)
        self.journal.sequence = int(sequence)
        return TaskStopDecision(
            decision="PAUSE",
            run_id=binding.run_id,
            task_id=binding.task_id,
            task_state="PAUSED",
            pause_reason="REPEATED_INVALID_ACTION",
            resulting_sequence=sequence,
            attempt_state="FAILED",
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
        del recovered_marker, permit, expected_sequence
        validate_authorized_worker_action(
            self.binding,
            intent,
            request,
            decision,
            expected_prestate,
        )
        return intent

    def settle_recovered_action_denial(
        self,
        *,
        binding: WorkerTurnBinding,
        marker: EffectIntent,
        permit: RuntimePermit,
        decision: AuthorizationDecision,
        expected_sequence: AuditSequence,
    ) -> AuditSequence:
        del binding, marker, permit, decision, expected_sequence
        raise AssertionError("demo does not exercise recovered action denial")

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
        del request, decision, expected_prestate, recovered_marker, permit, expected_sequence
        raise AssertionError("demo does not exercise pending action approval")

    def settle_worker_action(
        self,
        *,
        intent: ToolIntent,
        authorization: AuthorizationDecision,
        result: ToolResult,
        expected_sequence: AuditSequence,
    ) -> AuditSequence:
        del intent, authorization
        self.results.append(result)
        sequence = AuditSequence(int(expected_sequence) + 1)
        self.journal.sequence = int(sequence)
        if result.code == "CHECK_FAILED":
            self.feedback = bounded_worker_feedback(result)
        return sequence

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
        del logical_turn_id, action, action_digest, authorization, recovered_marker, permit
        if binding != self.binding:
            raise AssertionError("demo binding mismatch")
        sequence = AuditSequence(int(expected_sequence) + 1)
        self.journal.sequence = int(sequence)
        return RuntimeDecision(code="ACTION_RECORDED", resulting_sequence=sequence)


class _DemoContext:
    def build_current(self, attempt_id: AttemptId) -> object:
        if attempt_id != AttemptId("demo-attempt"):
            raise AssertionError("demo attempt mismatch")
        return {"goal": "repair money fixture", "files": ("src/money.py",)}


class _DemoRequests:
    def __init__(self, request: ModelRequest) -> None:
        self._request = request

    def for_attempt(self, attempt_id: AttemptId, capsule: object) -> ModelRequest:
        del capsule
        if attempt_id != AttemptId("demo-attempt"):
            raise AssertionError("demo attempt mismatch")
        return self._request


class _DemoScriptedMockLLM(ScriptedMockLLM):
    def __init__(self) -> None:
        super().__init__(())

    def complete(self, request: ModelRequest) -> ProviderAttemptResult:
        self.call_count += 1
        if self.call_count == 1:
            action: Mapping[str, object] = {"kind": "check", "check_id": "fixture"}
        elif self.call_count == 2:
            action = {
                "kind": "patch",
                "path": "src/money.py",
                "unified_diff": (
                    "--- a/src/money.py\n"
                    "+++ b/src/money.py\n"
                    "@@ -1 +1 @@\n"
                    "-TOTAL_CENTS = 250\n"
                    "+TOTAL_CENTS = 300\n"
                ),
            }
        else:
            raise AssertionError("demo model received an unexpected third turn")
        return ProviderAttemptResult.completed(
            ModelCompletion(
                response_id=f"demo-response-{self.call_count}",
                requested_model_id=request.requested_model_id,
                returned_model_id="demo-model",
                usage=None,
                normalized_action=action,
            )
        )


class _DemoModelClient:
    def __init__(self, scripted: _DemoScriptedMockLLM) -> None:
        self._scripted = scripted
        self.requests: list[ModelReservationRequest] = []

    @property
    def call_count(self) -> int:
        return self._scripted.call_count

    def complete(self, request: ModelReservationRequest) -> ModelDispatchResult | ModelReservation:
        self.requests.append(request)
        provider_result = self._scripted.complete(request.model_request)
        completion = provider_result.completion
        if completion is None:
            raise AssertionError("demo scripted model must complete")
        action = dict(completion.normalized_action)
        return ModelDispatchResult(
            run_id=request.run_id,
            logical_turn_id=f"demo-turn-{self.call_count}",
            outcome="COMPLETED",
            response_requested_model_id=completion.requested_model_id,
            returned_model_id=completion.returned_model_id,
            normalized_action=action,
            normalized_payload_digest=sha256_digest(canonical_json(action)),
            charged_amounts=ModelBudgetAmounts.zero(),
        )


class _DemoIds:
    def __init__(self) -> None:
        self._actions = 0
        self._intents = 0

    def next_action_id(self, run_id: RunId) -> str:
        self._actions += 1
        return f"demo-action-{self._actions}"

    def next_intent_id(self, run_id: RunId) -> IntentId:
        self._intents += 1
        return IntentId(f"demo-intent-{self._intents}")


def _demo_request(binding: WorkerTurnBinding) -> ModelRequest:
    return ModelRequest(
        run_id=binding.run_id,
        plan_digest=binding.plan_digest,
        policy_digest=binding.policy_digest,
        budget_digest=binding.budget_digest,
        model_configuration_digest=binding.model_configuration_digest,
        requested_model_id="demo-model",
        allowed_model_ids=frozenset({"demo-model"}),
        prompt=({"role": "user", "content": "repair fixture"},),
        tool_schema_digest=binding.tool_schema_digest,
        request_digest="sha256:" + "e" * 64,
        idempotency_key="demo-worker-request",
        max_input_tokens=128,
        max_output_tokens=64,
        reserved_cost_usd=Decimal(0),
        owner_kind="WORKER",
        task_id=binding.task_id,
        attempt_id=binding.attempt_id,
        tranche_id=binding.tranche_id,
    )


def _demo_loop() -> tuple[
    WorkerLoopService,
    _DemoAttempts,
    _DemoModelClient,
    MemoryPatchExecutor,
]:
    binding = _binding()
    journal = _DemoJournal()
    attempts = _DemoAttempts(binding, journal)
    secret_paths = SecretPathPolicy.from_host_rules((), b"k" * 32)
    files = {"src/money.py": b"TOTAL_CENTS = 250\n"}
    snapshot = MemoryRepositorySnapshot(files)
    content_digest = Sha256DigestText("sha256:" + sha256(files["src/money.py"]).hexdigest())
    sanitized_snapshot = SanitizedSnapshot.from_regular_files(
        root=Path("demo-workspace"),
        repository_id=binding.repository_id,
        tree_digest=binding.snapshot_digest,
        dependency_fingerprint_digest=binding.dependency_fingerprint_basis,
        entries=(
            SanitizedSnapshotEntry(
                path="src/money.py",
                kind="regular",
                content_digest=content_digest,
            ),
        ),
        secret_paths=secret_paths,
    )
    check = CheckDefinition(
        argv=("fixture-check",),
        input_globs=(GlobPattern.parse("src/**"),),
    )
    executor = FakeExecutor(Path("demo-workspace"), secret_paths=secret_paths)
    executor.add_response(
        check.argv,
        binding.snapshot_digest,
        FakeProcessResult(
            exit_code=1,
            timed_out=False,
            timing_ms=1,
            stderr_chunks=(b"expected 300 cents, received 250 cents\n",),
        ),
    )
    patch_executor = MemoryPatchExecutor(files, secret_paths=secret_paths)
    lease = WorkspaceLease(
        lease_id=binding.lease_id,
        run_id=binding.run_id,
        task_id=binding.task_id,
        attempt_id=binding.attempt_id,
        generation=binding.lease_generation,
        base_head=binding.admissible_head,
        admissible_head=binding.admissible_head,
        task_contract_digest=binding.task_contract_digest,
        write_globs=(GlobPattern.parse("src/**"),),
        sensitivity_globs=(GlobPattern.parse("src/**"),),
        issued_at=_CLOCK,
        expires_at=_CLOCK + timedelta(hours=1),
        state="ACTIVE",
    )
    authority = _DemoAuthority(binding, journal)
    runtime = ScopedToolRuntime(
        snapshot=snapshot,
        read_globs=("src/**",),
        secret_paths=secret_paths,
        authorization_binding_digest=_AUTHORIZATION_DIGEST,
        applicable_revision_digests=binding.applicable_revision_digests,
        repository_id=binding.repository_id,
        snapshot_digest=binding.snapshot_digest,
        scope_digest=binding.scope_digest,
        dependency_fingerprint_basis=binding.dependency_fingerprint_basis,
        executor=executor,
        patch_executor=patch_executor,
        declared_checks=DeclaredCheckRegistry({"fixture": check}),
        sanitized_snapshot=sanitized_snapshot,
        deadline_journal=journal,
        deadline_authority=authority,
        workspace_lease=lease,
    )
    model = _DemoModelClient(_DemoScriptedMockLLM())
    loop = WorkerLoopService(
        attempts=attempts,
        capsules=_DemoContext(),
        requests=_DemoRequests(_demo_request(binding)),
        models=model,
        actions=WorkerActionCodec(
            lambda current_binding, action: ActionPreState(
                source_digest=current_binding.snapshot_digest
            )
        ),
        authority=authority,
        tools=runtime,
        journal=journal,
        ids=_DemoIds(),
        clock=lambda: _CLOCK,
    )
    return loop, attempts, model, patch_executor


def build_demo_trace() -> tuple[DemoEvent, ...]:
    denied = ActionPolicy.default().classify(ActionEnvelope(kind="raw_shell"))
    loop, attempts, model, patch_executor = _demo_loop()
    first_decision = loop.run_turn(AttemptId("demo-attempt"))
    second_decision = loop.run_turn(AttemptId("demo-attempt"))
    del first_decision, second_decision
    if len(attempts.results) != 2 or model.call_count != 2:
        raise RuntimeError("DEMO_WORKER_LOOP_INVALID")
    first_result, next_result = attempts.results
    if first_result.code != "CHECK_FAILED" or next_result.code != "PATCH_APPLIED":
        raise RuntimeError("DEMO_TOOL_SEQUENCE_INVALID")
    first_request = model.requests[0].model_request
    next_request = model.requests[1].model_request
    feedback_message = next_request.prompt[-1]
    capsule = ContextCapsule.create(
        run_id="demo-run",
        task_id="demo-task",
        revision_digest=_SHA,
        dependencies=(_DEPENDENCY_DIGEST,),
        content="fixture context",
    )
    receipt = EvidenceReceipt.create(capsule, result_class="CHECK_FAILED", result="red")
    freshness = FreshnessAssessment.assess(
        receipt,
        current_revision=receipt.revision_digest,
        current_dependencies=("sha256:" + "3" * 64,),
    )
    repaired = patch_executor.workspace_files().get("src/money.py") == b"TOTAL_CENTS = 300\n"
    return (
        {
            "behavior": "guard",
            "action": "raw_shell",
            "decision": denied,
            "first_action": "",
            "next_action": "",
            "status": "",
            "model_calls": "",
            "feedback_bound": "",
            "loop_turns": "",
            "tool_executions": "",
            "first_turn_result": "",
            "next_turn_result": "",
            "feedback_role": "",
            "first_turn_feedback_absent": "",
            "repaired_path": "",
            "repaired": "",
        },
        {
            "behavior": "feedback",
            "action": "",
            "decision": "",
            "first_action": "check",
            "next_action": "patch",
            "status": first_result.code,
            "model_calls": str(model.call_count),
            "feedback_bound": str(feedback_message["content"]),
            "loop_turns": str(len(attempts.results)),
            "tool_executions": str(len(attempts.results)),
            "first_turn_result": first_result.code,
            "next_turn_result": next_result.code,
            "feedback_role": str(feedback_message["role"]),
            "first_turn_feedback_absent": str(
                all(message.get("role") != "tool" for message in first_request.prompt)
            ).lower(),
            "repaired_path": "src/money.py",
            "repaired": str(repaired).lower(),
        },
        {
            "behavior": "freshness",
            "action": "",
            "decision": "",
            "first_action": "",
            "next_action": "",
            "status": freshness.status,
            "model_calls": "",
            "feedback_bound": "",
            "loop_turns": "",
            "tool_executions": "",
            "first_turn_result": "",
            "next_turn_result": "",
            "feedback_role": "",
            "first_turn_feedback_absent": "",
            "repaired_path": "",
            "repaired": "",
        },
    )


def main() -> None:
    for event in build_demo_trace():
        print(json.dumps(event, sort_keys=True))


if __name__ == "__main__":
    main()
