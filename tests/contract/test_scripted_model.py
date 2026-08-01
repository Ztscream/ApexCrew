import socket
from decimal import Decimal

import pytest

from apexcrew.adapters.model.scripted import ScriptedMockLLM
from apexcrew.domain.model import ModelCompletion, ModelRequest, ModelUsage


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
    completion = ModelCompletion(
        response_id="response-1",
        requested_model_id="gpt-5.6-terra",
        returned_model_id="gpt-5.6-terra",
        usage=ModelUsage(120, 12, Decimal("0.00048")),
        normalized_action={"kind": "finish"},
    )
    model = ScriptedMockLLM([completion])
    assert model.complete(request) == completion
    with pytest.raises(AssertionError, match="unexpected model request"):
        model.complete(request)
