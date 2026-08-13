"""DeepSeek Responses transport adapter."""

from __future__ import annotations

import json
from collections.abc import Mapping
from decimal import Decimal
from typing import Any, Protocol, cast

from openai import APIConnectionError, APIError, APIStatusError, APITimeoutError, OpenAI

from apexcrew.adapters.credentials.model_key import ModelCredentialPort
from apexcrew.domain.model import (
    ModelCompletion,
    ModelRequest,
    ModelUsage,
    ProviderAttemptResult,
)

DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_MODEL_ID = "deepseek-v4-flash"
DEEPSEEK_PROFILE = "deepseek"
DEFAULT_INSTRUCTIONS = (
    "Return exactly one JSON object matching the supplied ApexCrew action schema."
)
DEFAULT_REASONING_EFFORT = "medium"
DEFAULT_TEMPERATURE = 0.0
_MISSING = object()


class ResponsesAPI(Protocol):
    def create(self, **request: object) -> Any:
        raise NotImplementedError


class ResponsesClient(Protocol):
    responses: ResponsesAPI


class ClientFactory(Protocol):
    def __call__(self, *, api_key: str, base_url: str, max_retries: int) -> ResponsesClient:
        raise NotImplementedError


def _nonnegative_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _schema_matches(value: object, schema: Mapping[str, object]) -> bool:
    enum = schema.get("enum")
    if isinstance(enum, list) and value not in enum:
        return False
    alternatives = schema.get("anyOf")
    if isinstance(alternatives, list):
        return any(
            isinstance(option, Mapping) and _schema_matches(value, option)
            for option in alternatives
        )
    kind = schema.get("type")
    if kind == "object":
        if not isinstance(value, Mapping):
            return False
        properties = schema.get("properties", {})
        if not isinstance(properties, Mapping):
            return False
        required = schema.get("required", [])
        if not isinstance(required, list) or any(
            not isinstance(name, str) or name not in value for name in required
        ):
            return False
        if schema.get("additionalProperties") is False and any(
            name not in properties for name in value
        ):
            return False
        return all(
            isinstance(name, str) and isinstance(child, Mapping) and _schema_matches(item, child)
            for name, item in value.items()
            if name in properties
            for child in (properties[name],)
        )
    if kind == "array":
        items = schema.get("items")
        return isinstance(value, list) and (
            not isinstance(items, Mapping) or all(_schema_matches(item, items) for item in value)
        )
    if kind == "string":
        return isinstance(value, str)
    if kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "number":
        return isinstance(value, (int, float, Decimal)) and not isinstance(value, bool)
    if kind == "boolean":
        return isinstance(value, bool)
    if kind == "null":
        return value is None
    return False


def _action_from_response(
    response: object, schema: Mapping[str, object]
) -> Mapping[str, object] | None:
    parsed = getattr(response, "output_parsed", _MISSING)
    if parsed is _MISSING:
        output_text = getattr(response, "output_text", _MISSING)
        if isinstance(output_text, str):
            try:
                parsed = json.loads(output_text)
            except json.JSONDecodeError:
                return None
        else:
            return None
    if not isinstance(parsed, Mapping):
        return None
    payload_schema = schema.get("schema", schema)
    if not isinstance(payload_schema, Mapping) or not _schema_matches(parsed, payload_schema):
        return None
    return dict(parsed)


def _usage_from_response(
    response: object,
    model_id: str,
    request: ModelRequest,
    pricing: Mapping[str, tuple[Decimal, Decimal]],
) -> ModelUsage | None:
    observed_usage = getattr(response, "usage", None)
    details = getattr(observed_usage, "output_tokens_details", None)
    if observed_usage is None or details is None:
        return None
    input_tokens = _nonnegative_int(getattr(observed_usage, "input_tokens", None))
    visible_output_tokens = _nonnegative_int(getattr(observed_usage, "output_tokens", None))
    reasoning_tokens = _nonnegative_int(getattr(details, "reasoning_tokens", None))
    if input_tokens is None or visible_output_tokens is None or reasoning_tokens is None:
        return None
    output_tokens = visible_output_tokens + reasoning_tokens
    rates = pricing.get(model_id)
    cost = request.reserved_cost_usd
    if rates is not None:
        input_rate, output_rate = rates
        cost = (
            Decimal(input_tokens) * input_rate + Decimal(output_tokens) * output_rate
        ) / Decimal(1_000_000)
    return ModelUsage(input_tokens, output_tokens, cost)


class DeepSeekResponsesAdapter:
    """Translate one observed DeepSeek Responses call into a ModelPort result."""

    def __init__(
        self,
        *,
        credential_source: ModelCredentialPort,
        response_schemas: Mapping[str, Mapping[str, object]],
        pricing_usd_per_million: Mapping[str, tuple[Decimal, Decimal]],
        profile: str = DEEPSEEK_PROFILE,
        client_factory: ClientFactory | None = None,
        instructions: str = DEFAULT_INSTRUCTIONS,
        temperature: float = DEFAULT_TEMPERATURE,
        reasoning_effort: str = DEFAULT_REASONING_EFFORT,
    ) -> None:
        self._credentials = credential_source
        self._response_schemas = dict(response_schemas)
        self._pricing = dict(pricing_usd_per_million)
        self._profile = profile
        self._client_factory = (
            cast(ClientFactory, OpenAI) if client_factory is None else client_factory
        )
        self._instructions = instructions
        self._temperature = temperature
        self._reasoning_effort = reasoning_effort

    def complete(self, request: ModelRequest) -> ProviderAttemptResult:
        schema = self._response_schemas.get(request.tool_schema_digest)
        if schema is None:
            return ProviderAttemptResult.unknown("TOOL_SCHEMA_MISSING")
        if request.requested_model_id != DEEPSEEK_MODEL_ID:
            return ProviderAttemptResult.unknown("MODEL_CONFIGURATION_UNSUPPORTED")
        if not request.allowed_model_ids <= self._pricing.keys():
            return ProviderAttemptResult.unknown("APPROVED_MODEL_PRICING_MISSING")

        api_key = self._credentials.resolve(self._profile)
        client = self._client_factory(
            api_key=api_key,
            base_url=DEEPSEEK_BASE_URL,
            max_retries=0,
        )
        try:
            response = client.responses.create(
                model=request.requested_model_id,
                input=list(request.prompt),
                instructions=self._instructions,
                max_output_tokens=request.max_output_tokens,
                temperature=self._temperature,
                text={"format": schema},
                reasoning={"effort": self._reasoning_effort},
                store=False,
            )
        except APIStatusError as error:
            request_id = getattr(error, "request_id", None)
            status_code = getattr(error, "status_code", None)
            if (
                isinstance(request_id, str)
                and isinstance(status_code, int)
                and 400 <= status_code < 500
            ):
                return ProviderAttemptResult.known_closed(request_id, f"HTTP_{status_code}")
            return ProviderAttemptResult.unknown("PROVIDER_STATUS_OUTCOME_UNKNOWN")
        except (APITimeoutError, APIConnectionError):
            return ProviderAttemptResult.unknown("PROVIDER_TRANSPORT_OUTCOME_UNKNOWN")
        except APIError:
            return ProviderAttemptResult.unknown("PROVIDER_ERROR_OUTCOME_UNKNOWN")

        response_id = getattr(response, "id", None)
        returned_model_id = getattr(response, "model", None)
        status = getattr(response, "status", None)
        if not isinstance(response_id, str) or not response_id:
            return ProviderAttemptResult.unknown("MISSING_RESPONSE_ID")
        if not isinstance(returned_model_id, str) or not returned_model_id:
            return ProviderAttemptResult.unknown("MISSING_RETURNED_MODEL_ID")
        if status == "incomplete":
            return ProviderAttemptResult.known_closed(
                response_id,
                "INCOMPLETE_RESPONSE",
                _usage_from_response(response, returned_model_id, request, self._pricing),
            )
        if status != "completed":
            return ProviderAttemptResult.unknown("UNEXPECTED_RESPONSE_STATUS")

        usage = _usage_from_response(response, returned_model_id, request, self._pricing)
        action = _action_from_response(response, schema)
        if action is None:
            return ProviderAttemptResult.known_closed(
                response_id, "MALFORMED_STRUCTURED_OUTPUT", usage
            )
        return ProviderAttemptResult.completed(
            ModelCompletion(
                response_id=response_id,
                requested_model_id=request.requested_model_id,
                returned_model_id=returned_model_id,
                usage=usage,
                normalized_action=action,
            )
        )
