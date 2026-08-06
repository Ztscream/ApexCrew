from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from apexcrew.domain.limits import V01_MECHANISM_LIMITS
from apexcrew.domain.revisions import (
    BudgetRevisionDocument,
    InferenceSettingsDocument,
    ModelConfigurationRevisionDocument,
    ModelPricingEntryDocument,
    ReturnedModelAliasDocument,
    SecretPathBindingDocument,
)


def test_revision_documents_reject_secret_globs_and_mismatched_origin() -> None:
    with pytest.raises(ValidationError):
        SecretPathBindingDocument.model_validate(
            {
                "defaults_version": "secret-path-defaults-v1",
                "matcher_version": "apexcrew-path-v1",
                "rules_hmac": "sha256:" + "0" * 64,
                "user_rule_count": 1,
                "user_globs": ["private/**"],
            }
        )
    with pytest.raises(ValidationError):
        ModelConfigurationRevisionDocument(
            schema_version="model-configuration-revision-v1",
            provider="scripted_mock",
            provider_base_origin="https://api.openai.com",
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
            tool_schema_digest="sha256:" + "9" * 64,
        )


def valid_budget_revision() -> dict[str, object]:
    return {
        "schema_version": "budget-revision-v1",
        "active_run_seconds_ceiling": 28_800,
        "task_ceiling": 12,
        "planning_request_ceiling": 8,
        "model_call_ceiling": 240,
        "input_token_ceiling": 2_000_000,
        "output_token_ceiling": 200_000,
        "cost_reserve_usd": Decimal(10),
        "concurrent_worker_ceiling": 3,
        "pricing_observed_on": date(2026, 8, 5),
        "pricing_entries": (
            ModelPricingEntryDocument(
                returned_model_id="deepseek-v4-flash",
                input_usd_per_million=Decimal("0.28"),
                output_usd_per_million=Decimal("0.56"),
            ),
        ),
    }


@pytest.mark.parametrize(
    ("field_name", "value"),
    (
        ("bootstrap_tranche_calls", 8),
        ("task_call_ceiling", 48),
        ("task_attempt_ceiling", 5),
        ("stale_refresh_ceiling", 3),
        ("manual_resume_ceiling", 2),
        ("ordinary_action_timeout_seconds", 120),
        ("check_timeout_seconds", 600),
        ("provider_retry_ceiling", 2),
        ("warning_percent", 80),
    ),
)
def test_budget_revision_rejects_v01_mechanism_fields(field_name: str, value: int) -> None:
    assert set(BudgetRevisionDocument.model_fields) == {
        "schema_version",
        "active_run_seconds_ceiling",
        "task_ceiling",
        "planning_request_ceiling",
        "model_call_ceiling",
        "input_token_ceiling",
        "output_token_ceiling",
        "cost_reserve_usd",
        "concurrent_worker_ceiling",
        "pricing_observed_on",
        "pricing_entries",
    }
    proposal = valid_budget_revision()
    proposal[field_name] = value
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        BudgetRevisionDocument.model_validate(proposal)
    assert V01_MECHANISM_LIMITS.task_call_ceiling == 48
    assert V01_MECHANISM_LIMITS.provider_retry_ceiling == 2
