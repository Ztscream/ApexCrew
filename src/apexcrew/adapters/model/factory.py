"""Approved model-provider construction for the application composition root."""

from __future__ import annotations

from collections.abc import Mapping

from apexcrew.adapters.credentials.model_key import (
    KeyringModelCredentialStore,
    ModelCredentialPort,
)
from apexcrew.adapters.model.deepseek_responses import (
    ClientFactory,
    DeepSeekResponsesAdapter,
)
from apexcrew.adapters.model.scripted import ScriptedMockLLM
from apexcrew.domain.actions import ACTION_ADAPTER
from apexcrew.domain.model import ModelPort
from apexcrew.domain.revisions import BudgetRevisionDocument, ModelConfigurationRevisionDocument


class ModelFactoryError(ValueError):
    """Raised when a model provider cannot be constructed fail-closed."""


def build_model_port(
    *,
    model_configuration: ModelConfigurationRevisionDocument,
    budget: BudgetRevisionDocument,
    scripted_model: ScriptedMockLLM | None = None,
    credential_source: ModelCredentialPort | None = None,
    response_schemas: Mapping[str, Mapping[str, object]] | None = None,
    client_factory: ClientFactory | None = None,
) -> ModelPort:
    """Select one provider from the exact model configuration revision."""
    if model_configuration.provider == "scripted_mock":
        if scripted_model is None:
            raise ModelFactoryError("SCRIPTED_MODEL_REQUIRED_FOR_DETERMINISTIC_RUN")
        return scripted_model

    if model_configuration.provider != "deepseek_responses":
        raise ModelFactoryError("MODEL_PROVIDER_UNSUPPORTED")
    schemas = (
        {str(model_configuration.tool_schema_digest): ACTION_ADAPTER.json_schema()}
        if response_schemas is None
        else response_schemas
    )
    credentials = KeyringModelCredentialStore() if credential_source is None else credential_source
    try:
        return DeepSeekResponsesAdapter.from_approved_configuration(
            model_configuration=model_configuration,
            budget=budget,
            credential_source=credentials,
            response_schemas=schemas,
            client_factory=client_factory,
        )
    except ValueError as error:
        raise ModelFactoryError(str(error)) from error


__all__ = ["ModelFactoryError", "build_model_port"]
