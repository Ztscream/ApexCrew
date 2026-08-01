from decimal import Decimal

from apexcrew.adapters.model.scripted import ScriptedMockLLM
from apexcrew.adapters.state.memory import InMemoryStateStore
from apexcrew.domain.model import (
    DurableModelClient,
    ModelCompletion,
    ModelRequest,
    ModelUsage,
    ProviderAttemptResult,
)


def completion(model_id: str, action: dict[str, str]) -> ModelCompletion:
    return ModelCompletion(
        response_id="response-1",
        requested_model_id="gpt-5.6-terra",
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
        requested_model_id="gpt-5.6-terra",
        allowed_model_ids=frozenset({"gpt-5.6-terra"}),
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


def test_known_closed_rejection_retries_with_new_reserved_attempts() -> None:
    journal = InMemoryStateStore()
    model = ScriptedMockLLM(
        [
            ProviderAttemptResult.known_closed("reject-1", "TRANSIENT_REJECTION"),
            ProviderAttemptResult.known_closed("reject-2", "TRANSIENT_REJECTION"),
            ProviderAttemptResult.completed(
                completion(model_id="gpt-5.6-terra", action={"kind": "finish"})
            ),
        ]
    )
    backoff = RecordingBackoff()
    result = DurableModelClient(model=model, journal=journal, backoff=backoff).complete(
        make_model_request()
    )
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
    model = ScriptedMockLLM([ProviderAttemptResult.unknown("TIMEOUT_AFTER_POSSIBLE_DISPATCH")])
    backoff = RecordingBackoff()
    result = DurableModelClient(model=model, journal=journal, backoff=backoff).complete(
        make_model_request()
    )
    attempts = journal.model_attempts(result.run_id, result.logical_turn_id)
    assert result.outcome == "INDETERMINATE"
    assert len(attempts) == 1
    assert attempts[0].charged_amounts == attempts[0].reserved_amounts
    assert journal.model_counters(result.run_id).calls == 1
    assert model.call_count == 1
    assert backoff.seconds == []
