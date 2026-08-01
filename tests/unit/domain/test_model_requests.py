from decimal import Decimal

from apexcrew.adapters.model.scripted import ScriptedMockLLM
from apexcrew.adapters.state.memory import InMemoryStateStore
from apexcrew.domain.model import (
    DurableModelClient,
    ModelBudgetAmounts,
    ModelCompletion,
    ModelRequest,
    ModelUsage,
)


def completion(model_id: str, action: dict[str, str]) -> ModelCompletion:
    return ModelCompletion(
        response_id="response-1",
        requested_model_id="gpt-5.6-terra",
        returned_model_id=model_id,
        usage=ModelUsage(input_tokens=120, output_tokens=12, cost_usd=Decimal("0.00048")),
        normalized_action=action,
    )


def make_model_request(allowed_model_ids: set[str]) -> ModelRequest:
    return ModelRequest(
        run_id="run-1",
        plan_digest=None,
        policy_digest="sha256:" + "3" * 64,
        budget_digest="sha256:" + "4" * 64,
        model_configuration_digest="sha256:" + "5" * 64,
        requested_model_id="gpt-5.6-terra",
        allowed_model_ids=frozenset(allowed_model_ids),
        prompt=({"role": "user", "content": "finish the bounded task"},),
        tool_schema_digest="sha256:" + "1" * 64,
        request_digest="sha256:" + "2" * 64,
        idempotency_key="model-request-1",
        max_input_tokens=1_000,
        max_output_tokens=200,
        reserved_cost_usd=Decimal("0.01"),
    )


def test_unapproved_returned_model_is_charged_but_not_released() -> None:
    model = ScriptedMockLLM([completion(model_id="unexpected-model", action={"kind": "finish"})])
    client = DurableModelClient(model=model, journal=InMemoryStateStore())
    request = make_model_request(allowed_model_ids={"gpt-5.6-terra"})
    result = client.complete(request)
    assert result.outcome == "RETURNED_MODEL_MISMATCH"
    assert result.normalized_action is None
    assert result.charged_amounts == ModelBudgetAmounts(
        calls=1,
        input_tokens=120,
        output_tokens=12,
        cost_usd=request.reserved_cost_usd,
    )
    assert client.journal.reserved_call_count(request.run_id) == 1
