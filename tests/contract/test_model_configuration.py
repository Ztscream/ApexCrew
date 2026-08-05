from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from helpers.application import fixture_budget, fixture_model_configuration

from apexcrew.domain.authority import model_reservation_amounts
from apexcrew.domain.model import ModelRequest

MODEL_ID = "deepseek-v4-flash"


def _request(*, max_input_tokens: int = 2_000, max_output_tokens: int = 200) -> ModelRequest:
    return ModelRequest(
        run_id="run-1",
        plan_digest=None,
        policy_digest="sha256:" + "3" * 64,
        budget_digest="sha256:" + "4" * 64,
        model_configuration_digest="sha256:" + "5" * 64,
        requested_model_id=MODEL_ID,
        allowed_model_ids=frozenset({MODEL_ID}),
        prompt=({"role": "user", "content": "finish"},),
        tool_schema_digest="sha256:" + "1" * 64,
        request_digest="sha256:" + "2" * 64,
        idempotency_key="request-1",
        max_input_tokens=max_input_tokens,
        max_output_tokens=max_output_tokens,
        reserved_cost_usd=Decimal(1),
    )


def test_default_fixtures_bind_the_revision_3_model_and_pricing() -> None:
    budget = fixture_budget()
    model = fixture_model_configuration()

    assert model.requested_model_id == MODEL_ID
    assert tuple(alias.returned_model_id for alias in model.returned_model_aliases) == (MODEL_ID,)
    assert tuple(entry.returned_model_id for entry in budget.pricing_entries) == (MODEL_ID,)
    assert budget.cost_reserve_usd == Decimal(1)
    assert budget.pricing_observed_on == date(2026, 8, 5)
    assert budget.pricing_entries[0].input_usd_per_million == Decimal("0.28")
    assert budget.pricing_entries[0].output_usd_per_million == Decimal("0.56")


def test_budget_missing_price_for_allowed_id_is_rejected() -> None:
    with pytest.raises(ValueError, match="MODEL_PRICING_MISSING"):
        model_reservation_amounts(_request(), fixture_budget(priced_model="legacy-model"))


def test_worst_case_reservation_matches_revision_3_rates() -> None:
    amounts = model_reservation_amounts(_request(), fixture_budget())

    assert amounts.cost_usd == Decimal("0.000672")
