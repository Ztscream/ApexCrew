from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace

from apexcrew.adapters.credentials.model_key import MemoryCredentialStore
from apexcrew.adapters.model.deepseek_responses import DeepSeekResponsesAdapter
from apexcrew.domain.model import (
    LogicalModelTurn,
    ModelRequest,
    ModelRequestIntent,
    SettledModelAttempt,
    settle_model_completion,
)
from apexcrew.domain.revisions import (
    InferenceSettingsDocument,
    ModelConfigurationRevisionDocument,
    ReturnedModelAliasDocument,
)

SCHEMA_DIGEST = "sha256:" + "6" * 64
RESPONSE_SCHEMA: dict[str, object] = {
    "type": "json_schema",
    "name": "apexcrew_action",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["kind"],
        "properties": {"kind": {"type": "string"}},
    },
}


class RecordingResponsesClient:
    def __init__(self, response: object) -> None:
        self.response = response
        self.requests: list[dict[str, object]] = []
        self.retrieved: list[str] = []
        self.responses = self

    def create(self, **request: object) -> object:
        self.requests.append(dict(request))
        return self.response

    def retrieve(self, response_id: str) -> object:
        self.retrieved.append(response_id)
        return self.response


class RecordingClientFactory:
    def __init__(self, client: RecordingResponsesClient) -> None:
        self.client = client
        self.options: dict[str, object] | None = None

    def __call__(self, **options: object) -> RecordingResponsesClient:
        self.options = dict(options)
        return self.client


def _response(
    *,
    model: str = "deepseek-v4-flash",
    status: str = "completed",
    action: object = None,
    usage: object = None,
    response_id: str = "response-1",
) -> SimpleNamespace:
    return SimpleNamespace(
        id=response_id,
        model=model,
        status=status,
        incomplete_details=SimpleNamespace(reason="max_output_tokens")
        if status == "incomplete"
        else None,
        output_parsed={"kind": "finish"} if action is None else action,
        usage=usage,
    )


def _usage(
    *, input_tokens: int = 120, output_tokens: int = 12, reasoning_tokens: int = 0
) -> SimpleNamespace:
    return SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        output_tokens_details=SimpleNamespace(reasoning_tokens=reasoning_tokens),
    )


def _request() -> ModelRequest:
    return ModelRequest(
        run_id="run-1",
        plan_digest=None,
        policy_digest="sha256:" + "3" * 64,
        budget_digest="sha256:" + "4" * 64,
        model_configuration_digest="sha256:" + "5" * 64,
        requested_model_id="deepseek-v4-flash",
        allowed_model_ids=frozenset({"deepseek-v4-flash"}),
        prompt=({"role": "user", "content": "finish"},),
        tool_schema_digest=SCHEMA_DIGEST,
        request_digest="sha256:" + "7" * 64,
        idempotency_key="request-1",
        max_input_tokens=1_000,
        max_output_tokens=200,
        reserved_cost_usd=Decimal("0.01"),
        temperature=0.0,
        reasoning_effort="medium",
    )


def _adapter(
    response: object,
    *,
    allowed_returned_model_ids: frozenset[str] | None = None,
    provider_storage_enabled: bool = False,
) -> tuple[DeepSeekResponsesAdapter, RecordingClientFactory, RecordingResponsesClient]:
    client = RecordingResponsesClient(response)
    factory = RecordingClientFactory(client)
    return (
        DeepSeekResponsesAdapter(
            credential_source=MemoryCredentialStore({"deepseek": "key"}),
            response_schemas={SCHEMA_DIGEST: RESPONSE_SCHEMA},
            pricing_usd_per_million={"deepseek-v4-flash": (Decimal("0.28"), Decimal("0.56"))},
            client_factory=factory,
            live_provider_authorized=True,
            allowed_returned_model_ids=allowed_returned_model_ids,
            provider_storage_enabled=provider_storage_enabled,
        ),
        factory,
        client,
    )


def test_lookup_returns_only_the_exact_retrieved_provider_response() -> None:
    adapter, _, client = _adapter(_response(usage=_usage()), provider_storage_enabled=True)

    result = adapter.lookup(_request(), "response-1")

    assert result is not None
    assert result.kind == "COMPLETED"
    assert result.completion is not None
    assert result.completion.response_id == "response-1"
    assert client.retrieved == ["response-1"]


def test_disabled_provider_storage_fails_closed_without_lookup() -> None:
    adapter, _, client = _adapter(_response(usage=_usage()))

    assert adapter.lookup(_request(), "response-1") is None
    assert client.retrieved == []


def test_request_pins_no_sdk_retries_and_deepseek_parameters() -> None:
    adapter, factory, client = _adapter(
        _response(usage=_usage()),
    )

    result = adapter.complete(_request())

    assert result.kind == "COMPLETED"
    assert factory.options == {
        "api_key": "key",
        "base_url": "https://api.deepseek.com",
        "max_retries": 0,
    }
    assert client.requests == [
        {
            "model": "deepseek-v4-flash",
            "input": [{"role": "user", "content": "finish"}],
            "instructions": "Return exactly one JSON object matching the supplied ApexCrew action schema.",
            "max_output_tokens": 200,
            "temperature": 0.0,
            "text": {"format": RESPONSE_SCHEMA},
            "reasoning": {"effort": "medium"},
            "store": False,
        }
    ]


def test_inference_parameters_follow_the_bound_model_request() -> None:
    adapter, _, client = _adapter(_response(usage=_usage()))

    result = adapter.complete(replace(_request(), temperature=0.7, reasoning_effort="high"))

    assert result.kind == "COMPLETED"
    assert client.requests[0]["temperature"] == 0.7
    assert client.requests[0]["reasoning"] == {"effort": "high"}


def test_missing_inference_parameters_fail_closed() -> None:
    adapter, _, client = _adapter(_response(usage=_usage()))

    result = adapter.complete(replace(_request(), temperature=None, reasoning_effort=None))

    assert result.kind == "UNKNOWN_OUTCOME"
    assert result.reason_code == "INFERENCE_SETTINGS_MISSING"
    assert client.requests == []


def test_effective_inference_parameters_remain_in_settled_attempt_request() -> None:
    adapter, _, _ = _adapter(_response(usage=_usage()))
    request = replace(_request(), temperature=0.3, reasoning_effort="low")
    result = adapter.complete(request)
    assert result.completion is not None

    intent = ModelRequestIntent.reserve(LogicalModelTurn.new(request), request)
    settled = SettledModelAttempt.from_result(
        intent,
        result,
    )

    assert settled.request.temperature == 0.3
    assert settled.request.reasoning_effort == "low"


def test_returned_model_mismatch_releases_no_action() -> None:
    adapter, _, _ = _adapter(
        _response(model="deepseek-v4-flash-0731", usage=_usage()),
    )
    request = _request()
    result = adapter.complete(request)
    assert result.completion is not None

    intent = ModelRequestIntent.reserve(LogicalModelTurn.new(request), request)
    settled = settle_model_completion(
        intent,
        result.completion,
        request.allowed_model_ids,
    )
    assert settled.outcome == "RETURNED_MODEL_MISMATCH"
    assert settled.normalized_action is None
    assert settled.charged_amounts.cost_usd == request.reserved_cost_usd


def test_incomplete_status_is_closed_failure() -> None:
    adapter, _, _ = _adapter(_response(status="incomplete", usage=_usage()))

    result = adapter.complete(_request())

    assert result.kind == "KNOWN_CLOSED_REJECTION"
    assert result.reason_code == "INCOMPLETE_RESPONSE"
    assert result.completion is None


def test_missing_usage_keeps_the_full_reservation() -> None:
    adapter, _, _ = _adapter(_response(usage=None))

    result = adapter.complete(_request())

    assert result.kind == "COMPLETED"
    assert result.completion is not None
    assert result.completion.usage is None


def test_reasoning_tokens_count_as_output_and_cost() -> None:
    adapter, _, _ = _adapter(_response(usage=_usage(reasoning_tokens=1_000)))

    result = adapter.complete(_request())

    assert result.completion is not None
    assert result.completion.usage is not None
    assert result.completion.usage.output_tokens == 1_012
    assert result.completion.usage.cost_usd == Decimal("0.00060032")


def test_non_conformant_payload_fails_closed() -> None:
    adapter, _, _ = _adapter(
        _response(action={"kind": "finish", "unexpected": True}, usage=_usage()),
    )

    result = adapter.complete(_request())

    assert result.kind == "KNOWN_CLOSED_REJECTION"
    assert result.reason_code == "MALFORMED_STRUCTURED_OUTPUT"
    assert result.completion is None


def test_schema_length_constraint_fails_closed() -> None:
    schema = {
        "type": "json_schema",
        "name": "apexcrew_action",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["summary"],
            "properties": {"summary": {"type": "string", "minLength": 1}},
        },
    }
    client = RecordingResponsesClient(_response(action={"summary": ""}, usage=_usage()))
    adapter = DeepSeekResponsesAdapter(
        credential_source=MemoryCredentialStore({"deepseek": "key"}),
        response_schemas={SCHEMA_DIGEST: schema},
        pricing_usd_per_million={"deepseek-v4-flash": (Decimal("0.28"), Decimal("0.56"))},
        client_factory=RecordingClientFactory(client),
        live_provider_authorized=True,
    )

    result = adapter.complete(_request())

    assert result.kind == "KNOWN_CLOSED_REJECTION"
    assert result.reason_code == "MALFORMED_STRUCTURED_OUTPUT"
    assert result.completion is None


def test_missing_returned_model_id_is_closed_model_mismatch() -> None:
    adapter, _, client = _adapter(_response(usage=_usage()))
    client.response.model = None

    request = _request()
    result = adapter.complete(request)

    assert result.kind == "KNOWN_CLOSED_REJECTION"
    assert result.reason_code == "RETURNED_MODEL_MISMATCH"
    assert result.completion is None
    assert result.response_requested_model_id == request.requested_model_id
    assert result.returned_model_id is None
    assert result.usage is not None
    intent = ModelRequestIntent.reserve(LogicalModelTurn.new(request), request)
    settled = SettledModelAttempt.from_result(intent, result)
    assert settled.dispatch_result.outcome == "RETURNED_MODEL_MISMATCH"
    assert settled.dispatch_result.response_requested_model_id == request.requested_model_id
    assert settled.dispatch_result.returned_model_id is None
    assert settled.charged_amounts.input_tokens == 120
    assert settled.charged_amounts.output_tokens == 12
    assert settled.charged_amounts.cost_usd == request.reserved_cost_usd


def test_unexpected_returned_model_id_is_closed_model_mismatch() -> None:
    adapter, _, _ = _adapter(
        _response(model="deepseek-v4-flash-0731", usage=_usage()),
        allowed_returned_model_ids=frozenset({"deepseek-v4-flash"}),
    )

    result = adapter.complete(_request())

    assert result.kind == "KNOWN_CLOSED_REJECTION"
    assert result.reason_code == "RETURNED_MODEL_MISMATCH"
    assert result.completion is None
    assert result.response_requested_model_id == _request().requested_model_id
    assert result.returned_model_id == "deepseek-v4-flash-0731"
    assert result.usage is not None
    assert result.usage.input_tokens == 120
    assert result.usage.output_tokens == 12


def test_model_configuration_accepts_deepseek_origin() -> None:
    configuration = ModelConfigurationRevisionDocument(
        schema_version="model-configuration-revision-v1",
        provider="deepseek_responses",
        provider_base_origin="https://api.deepseek.com",
        requested_model_id="deepseek-v4-flash",
        returned_model_aliases=(
            ReturnedModelAliasDocument(
                returned_model_id="deepseek-v4-flash",
                canonical_model_id="deepseek-v4-flash",
            ),
        ),
        inference_settings=InferenceSettingsDocument(
            max_input_tokens=32_000,
            max_output_tokens=4_096,
            temperature=0.0,
            reasoning_effort="medium",
            provider_storage_enabled=False,
        ),
        tool_schema_digest=SCHEMA_DIGEST,
    )

    assert configuration.provider_base_origin == "https://api.deepseek.com"
