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
from apexcrew.domain.coordination import PLANNING_ACTION_ADAPTER
from apexcrew.domain.model import ModelPort
from apexcrew.domain.revisions import BudgetRevisionDocument, ModelConfigurationRevisionDocument


class ModelFactoryError(ValueError):
    """Raised when a model provider cannot be constructed fail-closed."""


def _default_response_schema() -> dict[str, object]:
    worker = ACTION_ADAPTER.json_schema()
    planning = PLANNING_ACTION_ADAPTER.json_schema()
    worker_definitions = worker.get("$defs", {})
    planning_definitions = planning.get("$defs", {})
    if not isinstance(worker_definitions, dict) or not isinstance(planning_definitions, dict):
        raise ModelFactoryError("MODEL_ACTION_SCHEMA_INVALID")
    worker_discriminator = worker.get("discriminator")
    planning_discriminator = planning.get("discriminator")
    if not isinstance(worker_discriminator, Mapping) or not isinstance(
        planning_discriminator, Mapping
    ):
        raise ModelFactoryError("MODEL_ACTION_SCHEMA_INVALID")
    worker_mapping = worker_discriminator.get("mapping")
    planning_mapping = planning_discriminator.get("mapping")
    if not isinstance(worker_mapping, Mapping) or not isinstance(planning_mapping, Mapping):
        raise ModelFactoryError("MODEL_ACTION_SCHEMA_INVALID")
    definitions = dict(worker_definitions)
    definitions.update(
        {
            name: definition
            for name, definition in planning_definitions.items()
            if name != "PlanningFailAction"
        }
    )
    worker_branches = worker.get("oneOf", [])
    planning_branches = planning.get("oneOf", [])
    if not isinstance(worker_branches, list) or not isinstance(planning_branches, list):
        raise ModelFactoryError("MODEL_ACTION_SCHEMA_INVALID")
    return {
        "$defs": definitions,
        "discriminator": {
            "propertyName": "kind",
            "mapping": {
                **dict(worker_mapping),
                **{
                    name: reference
                    for name, reference in planning_mapping.items()
                    if name != "fail"
                },
            },
        },
        "oneOf": worker_branches
        + [
            branch
            for branch in planning_branches
            if not (
                isinstance(branch, Mapping) and branch.get("$ref") == "#/$defs/PlanningFailAction"
            )
        ],
    }


def build_model_port(
    *,
    model_configuration: ModelConfigurationRevisionDocument,
    budget: BudgetRevisionDocument,
    scripted_model: ScriptedMockLLM | None = None,
    credential_source: ModelCredentialPort | None = None,
    response_schemas: Mapping[str, Mapping[str, object]] | None = None,
    client_factory: ClientFactory | None = None,
    allow_live_provider: bool = False,
) -> ModelPort:
    """Select one provider from the exact model configuration revision."""
    if model_configuration.provider == "scripted_mock":
        if scripted_model is None:
            raise ModelFactoryError("SCRIPTED_MODEL_REQUIRED_FOR_DETERMINISTIC_RUN")
        return scripted_model

    if model_configuration.provider != "deepseek_responses":
        raise ModelFactoryError("MODEL_PROVIDER_UNSUPPORTED")
    schemas = (
        {
            str(model_configuration.tool_schema_digest): {
                "type": "json_schema",
                "name": "apexcrew_action",
                "strict": True,
                "schema": _default_response_schema(),
            }
        }
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
            live_provider_authorized=allow_live_provider,
        )
    except ValueError as error:
        raise ModelFactoryError(str(error)) from error


__all__ = ["ModelFactoryError", "build_model_port"]
