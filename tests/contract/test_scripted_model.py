import socket
from dataclasses import replace
from decimal import Decimal

import pytest

from apexcrew.adapters.model.scripted import ScriptedMockLLM, ScriptedModelStep
from apexcrew.domain.model import ModelCompletion, ModelRequest, ModelUsage, ProviderAttemptResult


def test_scripted_model_consumes_one_completion_without_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def deny_socket(*args: object, **kwargs: object) -> socket.socket:
        del args, kwargs
        raise AssertionError("network access denied")

    monkeypatch.setattr(socket, "socket", deny_socket)
    request = ModelRequest(
        run_id="run-1",
        plan_digest=None,
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
    completion = ModelCompletion(
        response_id="response-1",
        requested_model_id="deepseek-v4-flash",
        returned_model_id="deepseek-v4-flash",
        usage=ModelUsage(120, 12, Decimal("0.00048")),
        normalized_action={"kind": "finish"},
    )
    response = ProviderAttemptResult.completed(completion)
    model = ScriptedMockLLM([ScriptedModelStep.for_request(request, response)])
    assert model.complete(request) == response
    with pytest.raises(AssertionError, match="unexpected model request"):
        model.complete(request)


def test_scripted_model_rejects_unbound_nonempty_input() -> None:
    raw_result = ProviderAttemptResult.completed(
        ModelCompletion(
            response_id="response-1",
            requested_model_id="deepseek-v4-flash",
            returned_model_id="deepseek-v4-flash",
            usage=ModelUsage(120, 12, Decimal("0.00048")),
            normalized_action={"kind": "finish"},
        )
    )

    with pytest.raises(TypeError, match="SCRIPTED_MODEL_STEP_REQUIRED"):
        ScriptedMockLLM([raw_result])


def test_scripted_model_rejects_a_different_request_without_consuming_the_step() -> None:
    request = ModelRequest(
        run_id="run-1",
        plan_digest=None,
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
    response = ProviderAttemptResult.completed(
        ModelCompletion(
            response_id="response-1",
            requested_model_id="deepseek-v4-flash",
            returned_model_id="deepseek-v4-flash",
            usage=ModelUsage(120, 12, Decimal("0.00048")),
            normalized_action={"kind": "finish"},
        )
    )
    model = ScriptedMockLLM([ScriptedModelStep.for_request(request, response)])
    for different_request in (
        replace(request, request_digest="sha256:" + "f" * 64),
        replace(request, idempotency_key="request-2"),
        replace(request, requested_model_id="gpt-5.6-mini"),
    ):
        with pytest.raises(AssertionError, match="SCRIPTED_MODEL_REQUEST_BINDING_MISMATCH"):
            model.complete(different_request)
        assert model.call_count == 0

    assert model.complete(request) == response
    assert model.call_count == 1
