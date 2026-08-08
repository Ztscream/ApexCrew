from __future__ import annotations

from dataclasses import dataclass, fields, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import cast

from apexcrew.domain.authority import (
    ActionClass,
    ActionDeadline,
    AuthorizationDecision,
    AuthorizationRequest,
    ModelReservation,
    ModelReservationRequest,
    TaskBudgetState,
    TaskStopDecision,
)
from apexcrew.domain.commands import RuntimeDecision
from apexcrew.domain.coordination import CoordinatorService, TaskDispatchSelection
from apexcrew.domain.model import (
    ModelBudgetAmounts,
    ModelCounters,
    ModelDispatchResult,
    ModelRequest,
)
from apexcrew.domain.tools import ActionPreState, ToolIntent, ToolResult
from apexcrew.domain.types import AttemptId, AuditSequence, IntentId, RunId, TaskId
from apexcrew.domain.worker import (
    WorkerActionCodec,
    WorkerAttemptSnapshot,
    WorkerLoopService,
    WorkerTurnBinding,
)

SHA = "sha256:" + "1" * 64


def binding() -> WorkerTurnBinding:
    return WorkerTurnBinding(
        run_id=RunId("run-1"),
        task_id=TaskId("task-1"),
        attempt_id=AttemptId("attempt-1"),
        tranche_id="tranche-1",
        lease_id="lease-1",
        lease_generation=1,
        admissible_head="1" * 40,
        task_contract_digest=SHA,
        plan_digest=SHA,
        policy_digest=SHA,
        budget_digest=SHA,
        model_configuration_digest=SHA,
        tool_schema_digest=SHA,
        target_safety_digest=SHA,
        credential_profile="default",
        repository_id="repository-1",
        snapshot_digest=SHA,
        scope_digest=SHA,
        dependency_fingerprint_basis=SHA,
    )


def worker_request() -> ModelRequest:
    current = binding()
    return ModelRequest(
        run_id=current.run_id,
        plan_digest=current.plan_digest,
        policy_digest=current.policy_digest,
        budget_digest=current.budget_digest,
        model_configuration_digest=current.model_configuration_digest,
        requested_model_id="deepseek-v4-flash",
        allowed_model_ids=frozenset({"deepseek-v4-flash"}),
        prompt=({"role": "user", "content": "act"},),
        tool_schema_digest=current.tool_schema_digest,
        request_digest=SHA,
        idempotency_key="worker-request-1",
        max_input_tokens=1_000,
        max_output_tokens=200,
        reserved_cost_usd=Decimal("0.01"),
        owner_kind="WORKER",
        task_id=current.task_id,
        attempt_id=current.attempt_id,
        tranche_id=current.tranche_id,
    )


@dataclass
class StaticAttempts:
    malformed_count: int = 0

    def current_worker_turn_binding(self, attempt_id: AttemptId) -> WorkerTurnBinding:
        assert attempt_id == binding().attempt_id
        return binding()

    def record_malformed_worker_action(self, **values: object) -> TaskStopDecision:
        assert values["binding"] == binding()
        self.malformed_count += 1
        return TaskStopDecision(
            decision="CONTINUE",
            run_id=binding().run_id,
            task_id=binding().task_id,
            task_state="READY",
            pause_reason=None,
            attempt_state="FAILED",
            identical_invalid_action_count=self.malformed_count,
            resulting_sequence=AuditSequence(1),
        )

    def latest_worker_feedback(self, attempt_id: AttemptId) -> str | None:
        assert attempt_id == binding().attempt_id
        return None


@dataclass
class RecordingAttempts(StaticAttempts):
    recorded_intents: int = 0
    settled_results: int = 0
    feedback: str | None = None

    def record_authorized_worker_action(self, **values: object) -> ToolIntent:
        intent = cast(ToolIntent, values["intent"])
        request = cast(AuthorizationRequest, values["request"])
        assert intent.action_id == request.action_id
        self.recorded_intents += 1
        return intent

    def settle_worker_action(self, **values: object) -> AuditSequence:
        intent = cast(ToolIntent, values["intent"])
        result = cast(ToolResult, values["result"])
        assert result.intent_id == intent.intent_id
        self.settled_results += 1
        if result.code == "CHECK_FAILED":
            self.feedback = "CHECK_FAILED: expected 3.00, received 2.99"
        return AuditSequence(2)

    def latest_worker_feedback(self, attempt_id: AttemptId) -> str | None:
        assert attempt_id == binding().attempt_id
        return self.feedback


@dataclass
class FinishingAttempts(StaticAttempts):
    finish_count: int = 0

    def finish_attempt(self, **values: object) -> RuntimeDecision:
        assert values["binding"] == binding()
        self.finish_count += 1
        return RuntimeDecision(
            code="ACTION_RECORDED",
            resulting_sequence=AuditSequence(1),
        )


@dataclass
class StaticContext:
    def build_current(self, attempt_id: AttemptId) -> object:
        assert attempt_id == binding().attempt_id
        return object()


@dataclass
class StaticRequests:
    def for_attempt(self, attempt_id: AttemptId, capsule: object) -> ModelRequest:
        del capsule
        assert attempt_id == binding().attempt_id
        return worker_request()


@dataclass
class OneCompletion:
    action: dict[str, object]
    call_count: int = 0

    def complete(self, request: object) -> ModelDispatchResult | ModelReservation:
        del request
        self.call_count += 1
        return ModelDispatchResult(
            run_id=binding().run_id,
            logical_turn_id="turn-1",
            outcome="COMPLETED",
            response_requested_model_id="deepseek-v4-flash",
            returned_model_id="deepseek-v4-flash",
            normalized_action=self.action,
            normalized_payload_digest=SHA,
            charged_amounts=ModelBudgetAmounts.zero(),
        )


@dataclass
class SequencedCompletions:
    actions: list[dict[str, object]]
    requests: list[ModelReservationRequest]

    def complete(self, request: ModelReservationRequest) -> ModelDispatchResult:
        self.requests.append(request)
        action = self.actions.pop(0)
        return ModelDispatchResult(
            run_id=binding().run_id,
            logical_turn_id=f"turn-{len(self.requests)}",
            outcome="COMPLETED",
            response_requested_model_id="deepseek-v4-flash",
            returned_model_id="deepseek-v4-flash",
            normalized_action=action,
            normalized_payload_digest=SHA,
            charged_amounts=ModelBudgetAmounts.zero(),
        )


@dataclass
class AllowAuthority:
    authorization_count: int = 0
    deadline_count: int = 0
    request: AuthorizationRequest | None = None

    def authorize_action(self, request: AuthorizationRequest) -> AuthorizationDecision:
        self.authorization_count += 1
        self.request = request
        return AuthorizationDecision(
            decision="ALLOW",
            reason="AUTHORIZED",
            run_id=request.run_id,
            task_id=request.task_id,
            attempt_id=request.attempt_id,
            action_id=request.action_id,
            action_digest=request.action_digest,
            binding_digest=SHA,
            action_class="PATCH",
            approved_timeout_seconds=120,
            deadline_at_utc=request.deadline_at_utc,
            persistence="WITH_EFFECT_INTENT",
            effect_intent_id=None,
            pending_action_id=None,
            resulting_sequence=None,
        )

    def open_action_deadline(
        self, run_id: RunId, intent_id: IntentId, expected_sequence: AuditSequence
    ) -> ActionDeadline:
        self.deadline_count += 1
        started = datetime(2026, 8, 2, tzinfo=UTC)
        return ActionDeadline(
            run_id=run_id,
            intent_id=intent_id,
            budget_digest=SHA,
            applicable_revision_digests=binding().applicable_revision_digests,
            action_class=ActionClass.DECLARED_CHECK,
            started_at=started,
            expires_at=started + timedelta(seconds=600),
            recorded_sequence=AuditSequence(expected_sequence + 1),
            check_id="unit",
            snapshot_digest=SHA,
        )


class NeverAuthority:
    def authorize_action(self, request: object) -> object:
        del request
        raise AssertionError("malformed output must not reach Authority")


class NeverTools:
    execution_count = 0

    def execute(self, intent: ToolIntent) -> ToolResult:
        del intent
        self.execution_count += 1
        raise AssertionError("malformed output must not execute a tool")


class RecordingTools:
    execution_count = 0

    def execute(self, intent: ToolIntent) -> ToolResult:
        self.execution_count += 1
        return ToolResult(
            code="PATCH_APPLIED",
            run_id=intent.run_id,
            intent_id=intent.intent_id,
            bounded_payload={"snapshot_digest": SHA},
        )


class UncertainTools:
    execution_count = 0

    def execute(self, intent: ToolIntent) -> ToolResult:
        self.execution_count += 1
        return ToolResult(
            code="INFRASTRUCTURE_UNCERTAINTY",
            run_id=intent.run_id,
            intent_id=intent.intent_id,
            timed_out=True,
            bounded_payload={"snapshot_digest": SHA, "reason": "PATCH_RESULT_UNCERTAIN"},
        )


class FailingThenPassingTools:
    def __init__(self) -> None:
        self.execution_count = 0

    def execute(self, intent: ToolIntent) -> ToolResult:
        self.execution_count += 1
        if self.execution_count == 1:
            return ToolResult(
                code="CHECK_FAILED",
                run_id=intent.run_id,
                intent_id=intent.intent_id,
                passed=False,
                bounded_payload={"output": "expected 3.00, received 2.99"},
            )
        return ToolResult(
            code="PATCH_APPLIED",
            run_id=intent.run_id,
            intent_id=intent.intent_id,
            bounded_payload={"snapshot_digest": SHA},
        )


class StaticJournal:
    def audit_sequence(self, run_id: RunId) -> AuditSequence:
        assert run_id == binding().run_id
        return AuditSequence(0)

    def model_counters(self, run_id: RunId) -> ModelCounters:
        del run_id
        return ModelCounters()

    def task_budget_state(self, run_id: RunId, task_id: TaskId) -> TaskBudgetState:
        return TaskBudgetState(run_id=run_id, task_id=task_id)


class StaticIds:
    def next_action_id(self, run_id: RunId) -> str:
        del run_id
        return "action-1"

    def next_intent_id(self, run_id: RunId) -> IntentId:
        del run_id
        return IntentId("intent-1")


def test_action_batch_is_rejected_without_tool_execution() -> None:
    attempts = StaticAttempts()
    tools = NeverTools()
    model = OneCompletion({"kind": "batch", "actions": []})
    worker = WorkerLoopService(
        attempts=cast(object, attempts),
        capsules=StaticContext(),
        requests=StaticRequests(),
        models=cast(object, model),
        actions=WorkerActionCodec(lambda _binding, _action: ActionPreState()),
        authority=cast(object, NeverAuthority()),
        tools=cast(object, tools),
        journal=cast(object, StaticJournal()),
        ids=StaticIds(),
        clock=lambda: datetime(2026, 8, 2, tzinfo=UTC),
    )

    decision = worker.run_turn(binding().attempt_id)

    assert decision.code == "MALFORMED_ACTION"
    assert decision.resulting_sequence == 1
    assert attempts.malformed_count == 1
    assert model.call_count == 1
    assert tools.execution_count == 0


def test_model_deadline_is_not_caller_selectable() -> None:
    request = worker_request()

    assert all("deadline" not in field.name for field in fields(request))
    assert replace(request, max_output_tokens=200).max_output_tokens == 200


def test_one_typed_completion_executes_and_records_exactly_one_action() -> None:
    attempts = RecordingAttempts()
    authority = AllowAuthority()
    tools = RecordingTools()
    model = OneCompletion(
        {
            "kind": "patch",
            "path": "src/a.py",
            "unified_diff": "@@ -1 +1 @@\n-old\n+new\n",
        }
    )
    worker = WorkerLoopService(
        attempts=cast(object, attempts),
        capsules=StaticContext(),
        requests=StaticRequests(),
        models=cast(object, model),
        actions=WorkerActionCodec(lambda _binding, _action: ActionPreState(source_digest=SHA)),
        authority=cast(object, authority),
        tools=cast(object, tools),
        journal=cast(object, StaticJournal()),
        ids=StaticIds(),
        clock=lambda: datetime(2026, 8, 2, tzinfo=UTC),
    )

    decision = worker.run_turn(binding().attempt_id)

    assert decision.code == "ACTION_RECORDED"
    assert decision.stop_reason is None
    assert decision.resulting_sequence == 2
    assert model.call_count == 1
    assert authority.authorization_count == 1
    assert attempts.recorded_intents == 1
    assert tools.execution_count == 1
    assert attempts.settled_results == 1


def test_uncertain_patch_result_settles_before_worker_pauses() -> None:
    attempts = RecordingAttempts()
    tools = UncertainTools()
    model = OneCompletion(
        {
            "kind": "patch",
            "path": "src/a.py",
            "unified_diff": "@@ -1 +1 @@\n-old\n+new\n",
        }
    )
    worker = WorkerLoopService(
        attempts=cast(object, attempts),
        capsules=StaticContext(),
        requests=StaticRequests(),
        models=cast(object, model),
        actions=WorkerActionCodec(lambda _binding, _action: ActionPreState(source_digest=SHA)),
        authority=cast(object, AllowAuthority()),
        tools=cast(object, tools),
        journal=cast(object, StaticJournal()),
        ids=StaticIds(),
        clock=lambda: datetime(2026, 8, 2, tzinfo=UTC),
    )

    decision = worker.run_turn(binding().attempt_id)

    assert decision.code == "STOP"
    assert decision.stop_reason == "INFRASTRUCTURE_UNCERTAINTY"
    assert decision.resulting_sequence == 2
    assert tools.execution_count == 1
    assert attempts.settled_results == 1


def test_failed_check_feedback_reaches_next_model_request() -> None:
    attempts = RecordingAttempts()
    model = SequencedCompletions(
        actions=[
            {"kind": "check", "check_id": "unit"},
            {
                "kind": "patch",
                "path": "src/a.py",
                "unified_diff": "@@ -1 +1 @@\n-old\n+new\n",
            },
        ],
        requests=[],
    )
    worker = WorkerLoopService(
        attempts=cast(object, attempts),
        capsules=StaticContext(),
        requests=StaticRequests(),
        models=cast(object, model),
        actions=WorkerActionCodec(lambda _binding, _action: ActionPreState(source_digest=SHA)),
        authority=cast(object, AllowAuthority()),
        tools=cast(object, FailingThenPassingTools()),
        journal=cast(object, StaticJournal()),
        ids=StaticIds(),
        clock=lambda: datetime(2026, 8, 2, tzinfo=UTC),
    )

    worker.run_turn(binding().attempt_id)
    worker.run_turn(binding().attempt_id)

    assert len(model.requests) == 2
    assert "expected 3.00" not in str(model.requests[0].model_request.prompt)
    assert "expected 3.00" in str(model.requests[1].model_request.prompt)


@dataclass
class OneSelection:
    selection_count: int = 0

    def next_dispatchable(self, run_id: RunId) -> TaskDispatchSelection:
        self.selection_count += 1
        current = binding()
        return TaskDispatchSelection(
            dispatch_id="dispatch-1",
            run_id=run_id,
            task_id=current.task_id,
            task_contract_digest=current.task_contract_digest,
            base_run_head_oid=current.admissible_head,
            applicable_revision_digests=current.applicable_revision_digests,
            target_safety_digest=current.target_safety_digest,
            credential_profile=current.credential_profile,
            resume_allocation_id=None,
            reserved_attempt_id=None,
            expected_sequence=AuditSequence(0),
        )


@dataclass
class OneAttemptCreation:
    creation_count: int = 0

    def create_attempt_with_lease(
        self, selection: TaskDispatchSelection, *, expected_sequence: AuditSequence
    ) -> WorkerAttemptSnapshot:
        assert expected_sequence == selection.expected_sequence
        self.creation_count += 1
        return WorkerAttemptSnapshot(
            run_id=selection.run_id,
            task_id=selection.task_id,
            attempt_id=binding().attempt_id,
            applicable_revision_digests=selection.applicable_revision_digests,
        )


@dataclass
class OneWorkerTurn:
    turn_count: int = 0

    def run_turn(self, attempt_id: AttemptId) -> RuntimeDecision:
        assert attempt_id == binding().attempt_id
        self.turn_count += 1
        return RuntimeDecision(code="ACTION_RECORDED", resulting_sequence=AuditSequence(2))


def test_coordinator_schedules_only_one_authorized_worker_turn() -> None:
    scheduling = OneSelection()
    attempts = OneAttemptCreation()
    workers = OneWorkerTurn()
    coordinator = CoordinatorService.for_worker_scheduling(
        scheduling=scheduling,
        attempts=attempts,
        workers=workers,
    )

    decision = coordinator.schedule(binding().run_id)

    assert decision.code == "ACTION_RECORDED"
    assert scheduling.selection_count == 1
    assert attempts.creation_count == 1
    assert workers.turn_count == 1


def test_finish_action_settles_without_tool_execution() -> None:
    attempts = FinishingAttempts()
    tools = NeverTools()
    worker = WorkerLoopService(
        attempts=cast(object, attempts),
        capsules=StaticContext(),
        requests=StaticRequests(),
        models=cast(object, OneCompletion({"kind": "finish", "summary": "done"})),
        actions=WorkerActionCodec(lambda _binding, _action: ActionPreState()),
        authority=cast(object, AllowAuthority()),
        tools=cast(object, tools),
        journal=cast(object, StaticJournal()),
        ids=StaticIds(),
        clock=lambda: datetime(2026, 8, 2, tzinfo=UTC),
    )

    decision = worker.run_turn(binding().attempt_id)

    assert decision.code == "ACTION_RECORDED"
    assert attempts.finish_count == 1
    assert tools.execution_count == 0
