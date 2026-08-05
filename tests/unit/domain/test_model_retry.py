from decimal import Decimal

from apexcrew.adapters.model.scripted import ScriptedMockLLM, ScriptedModelStep
from apexcrew.adapters.state.memory import InMemoryStateStore
from apexcrew.domain.model import (
    DurableModelClient,
    LogicalModelTurn,
    ModelCompletion,
    ModelIdentitySource,
    ModelRequest,
    ModelRequestIntent,
    ModelUsage,
    ProviderAttemptResult,
)


def completion(model_id: str, action: dict[str, str]) -> ModelCompletion:
    return ModelCompletion(
        response_id="response-1",
        requested_model_id="deepseek-v4-flash",
        returned_model_id=model_id,
        usage=ModelUsage(120, 12, Decimal("0.00048")),
        normalized_action=action,
    )


def make_model_request() -> ModelRequest:
    return ModelRequest(
        run_id="run-1",
        plan_digest="sha256:" + "2" * 64,
        policy_digest="sha256:" + "3" * 64,
        budget_digest="sha256:" + "4" * 64,
        model_configuration_digest="sha256:" + "5" * 64,
        requested_model_id="deepseek-v4-flash",
        allowed_model_ids=frozenset({"deepseek-v4-flash"}),
        prompt=({"role": "user", "content": "finish"},),
        tool_schema_digest="sha256:" + "1" * 64,
        request_digest="sha256:" + "2" * 64,
        idempotency_key="request-1",
        max_input_tokens=1_000,
        max_output_tokens=200,
        reserved_cost_usd=Decimal("0.01"),
    )


class RecordingBackoff:
    def __init__(self) -> None:
        self.seconds: list[int] = []

    def wait(self, seconds: int) -> None:
        self.seconds.append(seconds)


class FiniteModelIdentitySource(ModelIdentitySource):
    def __init__(self) -> None:
        self._logical_turn_ids = iter(("model-turn-fixed",))
        self._provider_attempt_ids = iter(
            ("model-intent-first", "model-intent-second", "model-intent-third")
        )

    def next_logical_turn_id(self) -> str:
        return next(self._logical_turn_ids)

    def next_provider_attempt_id(self) -> str:
        return next(self._provider_attempt_ids)


def test_model_identity_source_makes_logical_turn_and_attempt_ids_deterministic() -> None:
    request = make_model_request()
    ids = FiniteModelIdentitySource()

    turn = LogicalModelTurn.new(request, ids=ids)
    attempts = [
        ModelRequestIntent.reserve(turn, request, provider_attempt_number, ids=ids)
        for provider_attempt_number in (1, 2, 3)
    ]

    assert turn.logical_turn_id == "model-turn-fixed"
    assert [attempt.intent_id for attempt in attempts] == [
        "model-intent-first",
        "model-intent-second",
        "model-intent-third",
    ]


def test_known_closed_rejection_retries_with_new_reserved_attempts() -> None:
    journal = InMemoryStateStore()
    request = make_model_request()
    model = ScriptedMockLLM(
        [
            ScriptedModelStep.for_request(
                request, ProviderAttemptResult.known_closed("reject-1", "TRANSIENT_REJECTION")
            ),
            ScriptedModelStep.for_request(
                request, ProviderAttemptResult.known_closed("reject-2", "TRANSIENT_REJECTION")
            ),
            ScriptedModelStep.for_request(
                request,
                ProviderAttemptResult.completed(
                    completion(model_id="deepseek-v4-flash", action={"kind": "finish"})
                ),
            ),
        ]
    )
    backoff = RecordingBackoff()
    result = DurableModelClient(model=model, journal=journal, backoff=backoff).complete(request)
    attempts = journal.model_attempts(result.run_id, result.logical_turn_id)
    assert result.outcome == "COMPLETED"
    assert [attempt.provider_attempt_number for attempt in attempts] == [1, 2, 3]
    assert len({attempt.intent_id for attempt in attempts}) == 3
    assert {attempt.logical_turn_id for attempt in attempts} == {result.logical_turn_id}
    assert [attempt.backoff_seconds for attempt in attempts] == [1, 2, None]
    assert journal.model_counters(result.run_id).calls == 3
    assert backoff.seconds == [1, 2]


def test_timeout_after_possible_dispatch_is_fully_charged_without_retry() -> None:
    journal = InMemoryStateStore()
    request = make_model_request()
    model = ScriptedMockLLM(
        [
            ScriptedModelStep.for_request(
                request, ProviderAttemptResult.unknown("TIMEOUT_AFTER_POSSIBLE_DISPATCH")
            )
        ]
    )
    backoff = RecordingBackoff()
    result = DurableModelClient(model=model, journal=journal, backoff=backoff).complete(request)
    attempts = journal.model_attempts(result.run_id, result.logical_turn_id)
    assert result.outcome == "INDETERMINATE"
    assert len(attempts) == 1
    assert attempts[0].charged_amounts == attempts[0].reserved_amounts
    assert journal.model_counters(result.run_id).calls == 1
    assert model.call_count == 1
    assert backoff.seconds == []


def test_returned_model_mismatch_does_not_retry() -> None:
    journal = InMemoryStateStore()
    request = make_model_request()
    model = ScriptedMockLLM(
        [
            ScriptedModelStep.for_request(
                request,
                ProviderAttemptResult.known_closed(
                    "response-mismatch",
                    "RETURNED_MODEL_MISMATCH",
                    ModelUsage(120, 12, request.reserved_cost_usd),
                    response_requested_model_id=request.requested_model_id,
                    returned_model_id="deepseek-v4-flash-0731",
                ),
            )
        ]
    )
    backoff = RecordingBackoff()

    result = DurableModelClient(model=model, journal=journal, backoff=backoff).complete(request)

    attempts = journal.model_attempts(result.run_id, result.logical_turn_id)
    assert result.outcome == "RETURNED_MODEL_MISMATCH"
    assert len(attempts) == 1
    assert model.call_count == 1
    assert backoff.seconds == []
