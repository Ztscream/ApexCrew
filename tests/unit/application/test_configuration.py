from __future__ import annotations

import importlib
import importlib.util
from pathlib import Path

import pytest

from apexcrew.domain.types import GitOid


def test_run_options_reject_unknown_keys() -> None:
    module = (
        importlib.import_module("apexcrew.application.configuration")
        if importlib.util.find_spec("apexcrew.application.configuration") is not None
        else None
    )
    assert module is not None, "RunOptions production module is missing"
    run_options = getattr(module, "RunOptions", None)
    assert run_options is not None, "RunOptions production symbol is missing"

    with pytest.raises(ValueError, match="unknown configuration key"):
        run_options.from_mapping(
            {
                "goal": "bootstrap",
                "constraints": (),
                "acceptance_criteria": ("payload exists",),
                "repository_root": ".",
                "target_ref": "refs/heads/main",
                "unexpected": "must be rejected",
            }
        )


def test_run_options_and_payload_are_immutable_and_non_sensitive() -> None:
    from apexcrew.application.configuration import (
        RunOptions,
        build_create_run_payload,
    )

    options = RunOptions.from_mapping(
        {
            "goal": "bootstrap",
            "constraints": ("offline",),
            "acceptance_criteria": ("payload exists",),
            "repository_root": Path("repository"),
            "target_ref": "refs/heads/main",
        }
    )
    payload = build_create_run_payload(options, target_oid=GitOid("a" * 40))

    assert payload.repository_root == str(Path("repository").resolve())
    assert payload.target_ref == "refs/heads/main"
    assert payload.expected_target_oid == "a" * 40
    assert payload.policy_revision.schema_version == "policy-revision-v1"
    assert payload.budget_revision.schema_version == "budget-revision-v1"
    assert payload.model_configuration_revision.schema_version == (
        "model-configuration-revision-v1"
    )
    assert payload.model_configuration_revision.provider == "deepseek_responses"
    assert payload.model_configuration_revision.provider_base_origin == ("https://api.deepseek.com")
    assert payload.model_configuration_revision.inference_settings.max_input_tokens == 32_000
    assert payload.model_configuration_revision.inference_settings.max_output_tokens == 4_096
    assert "credential" not in payload.model_dump_json().lower()

    with pytest.raises((AttributeError, TypeError)):
        options.goal = "changed"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("field_name", "value"),
    (
        ("goal", 42),
        ("target_ref", 42),
        ("constraints", ["offline"]),
        ("constraints", "offline"),
        ("constraints", (42,)),
        ("acceptance_criteria", ["payload exists"]),
        ("acceptance_criteria", "payload exists"),
        ("acceptance_criteria", (42,)),
    ),
)
def test_run_options_rejects_malformed_direct_values(field_name: str, value: object) -> None:
    from apexcrew.application.configuration import ConfigurationError, RunOptions

    values: dict[str, object] = {
        "goal": "bootstrap",
        "constraints": ("offline",),
        "acceptance_criteria": ("payload exists",),
        "repository_root": Path("repository"),
        "target_ref": "refs/heads/main",
    }
    values[field_name] = value

    with pytest.raises(ConfigurationError):
        RunOptions(**values)  # type: ignore[arg-type]


def test_run_options_rejects_missing_mapping_values() -> None:
    from apexcrew.application.configuration import ConfigurationError, RunOptions

    with pytest.raises(ConfigurationError, match="missing configuration key"):
        RunOptions.from_mapping({"goal": "bootstrap"})
