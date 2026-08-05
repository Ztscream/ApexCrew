from __future__ import annotations

import os
from decimal import Decimal

import pytest

from apexcrew.adapters.credentials.model_key import MemoryCredentialStore, ModelCredentialError
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


def _request() -> ModelRequest:
    revisions = default_revision_documents()
    return ModelRequest(
        run_id=RunId("live-provider-smoke"),
        plan_digest=None,
        policy_digest="sha256:" + "1" * 64,
        budget_digest="sha256:" + "2" * 64,
        model_configuration_digest="sha256:" + "3" * 64,
        requested_model_id="deepseek-v4-flash",
        allowed_model_ids=frozenset({"deepseek-v4-flash"}),
        prompt=({"role": "user", "content": "live smoke"},),
        tool_schema_digest=str(revisions.model_configuration.tool_schema_digest),
        request_digest="sha256:" + "4" * 64,
        idempotency_key="live-provider-smoke-once",
        max_input_tokens=revisions.model_configuration.inference_settings.max_input_tokens,
        max_output_tokens=revisions.model_configuration.inference_settings.max_output_tokens,
        reserved_cost_usd=Decimal("0.672"),
    )


def test_missing_live_credential_fails_before_dispatch() -> None:
    dispatched = False

    def fail_if_dispatched(**_: object) -> object:
        nonlocal dispatched
        dispatched = True
        raise AssertionError("missing credentials must fail before client construction")

    model = build_model_port(
        model_configuration=default_revision_documents().model_configuration,
        budget=default_revision_documents().budget,
        credential_source=MemoryCredentialStore(),
        client_factory=fail_if_dispatched,
    )

    with pytest.raises(ModelCredentialError, match="MODEL_CREDENTIAL_MISSING"):
        model.complete(_request())
    assert not dispatched
