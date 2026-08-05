from __future__ import annotations

import importlib
import importlib.util
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from apexcrew.adapters.credentials.model_key import MemoryCredentialStore
from apexcrew.adapters.model.scripted import ScriptedMockLLM
from apexcrew.application.configuration import default_revision_documents
from apexcrew.domain.model import ModelRequest
from apexcrew.domain.types import RunId


class ExplodingClientFactory:
    def __call__(self, **_: object) -> object:
        raise AssertionError("scripted provider must not construct a network client")


def test_scripted_selection_never_calls_network(tmp_path: Path) -> None:
    module = (
        importlib.import_module("apexcrew.adapters.model.factory")
        if importlib.util.find_spec("apexcrew.adapters.model.factory") is not None
        else None
    )
    assert module is not None, "model factory module is missing"
    factory = getattr(module, "build_model_port", None)
    assert factory is not None, "model factory entry point is missing"

    model_configuration = default_revision_documents().model_configuration.model_copy(
        update={
            "provider": "scripted_mock",
            "provider_base_origin": "mock://scripted",
        }
    )
    scripted = ScriptedMockLLM(())
    selected = factory(
        model_configuration=model_configuration,
        budget=default_revision_documents().budget,
        scripted_model=scripted,
        client_factory=ExplodingClientFactory(),
    )

    assert selected is scripted
    assert scripted.call_count == 0


def test_default_deepseek_selection_does_not_resolve_credential(tmp_path: Path) -> None:
    del tmp_path
    module = importlib.import_module("apexcrew.adapters.model.factory")
    factory = module.build_model_port
    revisions = default_revision_documents()

    class ExplodingCredentialStore:
        def resolve(self, profile: str) -> str:
            raise AssertionError(f"credential resolved during composition: {profile}")

    selected = factory(
        model_configuration=revisions.model_configuration,
        budget=revisions.budget,
        credential_source=ExplodingCredentialStore(),
        client_factory=ExplodingClientFactory(),
    )

    assert selected is not None


def test_default_deepseek_selection_parses_one_fake_response() -> None:
    module = importlib.import_module("apexcrew.adapters.model.factory")
    revisions = default_revision_documents()
    captured: list[dict[str, object]] = []

    class Client:
        responses = None

        def __init__(self) -> None:
            self.responses = self

        def create(self, **request: object) -> object:
            captured.append(request)
            return SimpleNamespace(
                id="response-default-schema",
                model="deepseek-v4-flash",
                status="completed",
                output_parsed={"kind": "finish", "summary": "done"},
                usage=SimpleNamespace(
                    input_tokens=10,
                    output_tokens=2,
                    output_tokens_details=SimpleNamespace(reasoning_tokens=0),
                ),
            )

    client = Client()
    selected = module.build_model_port(
        model_configuration=revisions.model_configuration,
        budget=revisions.budget,
        credential_source=MemoryCredentialStore({"deepseek": "test-key"}),
        client_factory=lambda **_: client,
    )
    request = ModelRequest(
        run_id=RunId("run-default-schema"),
        plan_digest=None,
        policy_digest="sha256:" + "1" * 64,
        budget_digest="sha256:" + "2" * 64,
        model_configuration_digest="sha256:" + "3" * 64,
        requested_model_id="deepseek-v4-flash",
        allowed_model_ids=frozenset({"deepseek-v4-flash"}),
        prompt=({"role": "user", "content": "finish"},),
        tool_schema_digest=revisions.model_configuration.tool_schema_digest,
        request_digest="sha256:" + "4" * 64,
        idempotency_key="default-schema-request",
        max_input_tokens=100,
        max_output_tokens=100,
        reserved_cost_usd=Decimal("0.01"),
    )

    result = selected.complete(request)

    assert result.kind == "COMPLETED"
    assert captured[0]["store"] is False
