from __future__ import annotations

import os
from decimal import Decimal

import pytest

from apexcrew.adapters.credentials.model_key import KeyringModelCredentialStore
from apexcrew.adapters.model.factory import build_model_port
from apexcrew.application.configuration import default_revision_documents
from apexcrew.domain.model import ModelRequest
from apexcrew.domain.types import RunId

LIVE_SMOKE_ENV = "APEXCREW_LIVE_SMOKE"


def _live_smoke_enabled() -> bool:
    return os.environ.get(LIVE_SMOKE_ENV) == "1"


def test_live_smoke_is_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(LIVE_SMOKE_ENV, raising=False)
    assert not _live_smoke_enabled()

    monkeypatch.setenv(LIVE_SMOKE_ENV, "1")
    assert _live_smoke_enabled()


@pytest.mark.skipif(
    os.environ.get(LIVE_SMOKE_ENV) != "1",
    reason=f"set {LIVE_SMOKE_ENV}=1 to authorize one live DeepSeek request",
)
def test_live_deepseek_provider_smoke() -> None:
    revisions = default_revision_documents()
    credentials = KeyringModelCredentialStore()
    if credentials.source("deepseek") == "absent":
        pytest.fail("APEXCREW_LIVE_SMOKE=1 requires a configured DeepSeek credential")

    model = build_model_port(
        model_configuration=revisions.model_configuration,
        budget=revisions.budget,
        credential_source=credentials,
    )
    request = ModelRequest(
        run_id=RunId("live-provider-smoke"),
        plan_digest=None,
        policy_digest="sha256:" + "1" * 64,
        budget_digest="sha256:" + "2" * 64,
        model_configuration_digest="sha256:" + "3" * 64,
        requested_model_id="deepseek-v4-flash",
        allowed_model_ids=frozenset({"deepseek-v4-flash"}),
        prompt=(
            {
                "role": "user",
                "content": (
                    "Return exactly one action object: "
                    '{"kind":"finish","summary":"live smoke"}. '
                    "Do not include any explanation."
                ),
            },
        ),
        tool_schema_digest=str(revisions.model_configuration.tool_schema_digest),
        request_digest="sha256:" + "4" * 64,
        idempotency_key="live-provider-smoke-once",
        max_input_tokens=revisions.model_configuration.inference_settings.max_input_tokens,
        max_output_tokens=revisions.model_configuration.inference_settings.max_output_tokens,
        reserved_cost_usd=Decimal("0.672"),
    )

    result = model.complete(request)

    assert result.kind == "COMPLETED"
    assert result.completion is not None
    assert result.completion.response_id
    assert result.completion.requested_model_id == "deepseek-v4-flash"
    assert result.completion.returned_model_id == "deepseek-v4-flash"
    assert result.completion.normalized_action["kind"] == "finish"
    assert result.completion.usage is not None
    assert result.completion.usage.input_tokens >= 0
    assert result.completion.usage.output_tokens >= 0
