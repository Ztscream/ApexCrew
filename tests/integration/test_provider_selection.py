from __future__ import annotations

import importlib
import importlib.util
from pathlib import Path

from apexcrew.adapters.model.scripted import ScriptedMockLLM
from apexcrew.application.configuration import default_revision_documents


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
