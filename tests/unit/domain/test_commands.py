from typing import get_args

import pytest
from pydantic import ValidationError

from apexcrew.domain.commands import (
    ApproveBudgetPayload,
    CommandEnvelope,
    CommandOutcome,
    CommandPayload,
    FinalApprovalPending,
    PausePayload,
    PlanApprovalPending,
    PreparePurgePayload,
    ProposeBudgetPayload,
    PurgeDatabaseRowEntry,
    PurgeLocalArtifactEntry,
    PurgeManifestDocument,
    PurgePreparedResult,
    ResolveIndeterminatePayload,
    RunStop,
)
from apexcrew.domain.revisions import (
    BudgetRevisionDocument,
    InferenceSettingsDocument,
    ModelConfigurationRevisionDocument,
    ModelPricingEntryDocument,
    ReturnedModelAliasDocument,
    SecretPathBindingDocument,
    revision_digest,
)


def test_command_envelope_rejects_unknown_payload_fields() -> None:
    with pytest.raises(ValidationError):
        CommandEnvelope.model_validate(
            {
                "request_id": "cmd-1",
                "expected_sequence": 0,
                "payload": {
                    "kind": "pause",
                    "run_id": "run-1",
                    "reason": "operator",
                    "extra": "denied",
                },
            }
        )


def test_revision_approval_payload_is_closed_and_digest_bound() -> None:
    budget = BudgetRevisionDocument(
        schema_version="budget-revision-v1",
        active_run_seconds_ceiling=28_800,
        task_ceiling=12,
        planning_request_ceiling=8,
        model_call_ceiling=240,
        input_token_ceiling=2_000_000,
        output_token_ceiling=200_000,
        cost_reserve_usd="10.00",
        concurrent_worker_ceiling=3,
        pricing_observed_on="2026-07-26",
        pricing_entries=(
            ModelPricingEntryDocument(
                returned_model_id="gpt-5.6-terra",
                input_usd_per_million="2.50",
                output_usd_per_million="15.00",
            ),
        ),
    )
    proposal = ProposeBudgetPayload(run_id="run-1", budget_revision=budget)
    approval = ApproveBudgetPayload(
        run_id="run-1",
        budget_digest=revision_digest(budget),
        confirmation_code="B7K4Q2",
    )
    assert proposal.budget_revision == budget
    assert approval.model_fields_set == {"run_id", "budget_digest", "confirmation_code"}
    reordered = BudgetRevisionDocument.model_validate(
        dict(reversed(tuple(budget.model_dump(mode="json").items())))
    )
    assert revision_digest(reordered) == revision_digest(budget)


def test_secret_binding_cannot_carry_operator_globs() -> None:
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


def test_model_configuration_rejects_a_mismatched_provider_origin() -> None:
    with pytest.raises(ValidationError):
        ModelConfigurationRevisionDocument(
            schema_version="model-configuration-revision-v1",
            provider="scripted_mock",
            provider_base_origin="https://api.openai.com",
            requested_model_id="gpt-5.6-terra",
            returned_model_aliases=(
                ReturnedModelAliasDocument(
                    returned_model_id="gpt-5.6-terra",
                    canonical_model_id="gpt-5.6-terra",
                ),
            ),
            inference_settings=InferenceSettingsDocument(
                max_input_tokens=32_000,
                max_output_tokens=4_096,
                provider_storage_enabled=False,
            ),
            tool_schema_digest="sha256:" + "9" * 64,
        )


def test_indeterminate_resolution_distinguishes_member_and_set_strategies() -> None:
    member = ResolveIndeterminatePayload(
        run_id="run-1",
        unresolved_set_digest="sha256:" + "1" * 64,
        resolution="RECONCILE_OBSERVED",
        intent_id="intent-1",
        recovery_generation=2,
    )
    terminal = ResolveIndeterminatePayload(
        run_id="run-1",
        unresolved_set_digest="sha256:" + "1" * 64,
        resolution="FAIL_RUN",
    )
    assert member.intent_id == "intent-1"
    assert terminal.intent_id is None
    with pytest.raises(ValidationError):
        ResolveIndeterminatePayload(
            run_id="run-1",
            unresolved_set_digest="sha256:" + "1" * 64,
            resolution="RETRY_SAME_INTENT",
        )


def test_run_stop_has_one_exact_typed_approval_subject() -> None:
    stop = RunStop(
        run_id="run-1",
        state="AWAITING_PLAN_APPROVAL",
        reason="AWAITING_PLAN_APPROVAL",
        last_sequence=7,
        pending=PlanApprovalPending(plan_digest="sha256:" + "2" * 64),
    )
    assert stop.pending.kind == "plan"
    with pytest.raises(ValidationError):
        RunStop(
            run_id="run-1",
            state="AWAITING_PLAN_APPROVAL",
            reason="AWAITING_PLAN_APPROVAL",
            last_sequence=7,
            pending=FinalApprovalPending(candidate_id="candidate-1"),
        )


def test_purge_result_carries_one_closed_exact_manifest() -> None:
    manifest = PurgeManifestDocument(
        repository_id="sha256:" + "3" * 64,
        run_id="run-1",
        terminal_state="COMPLETED",
        terminal_sequence=17,
        ledger_head_digest="sha256:" + "4" * 64,
        entries=(
            PurgeDatabaseRowEntry(
                table_name="audit_events",
                row_id="17",
                row_digest="sha256:" + "5" * 64,
                byte_count=120,
            ),
            PurgeLocalArtifactEntry(
                artifact_id="artifact-1",
                relative_path="runs/run-1/artifact-1.txt",
                artifact_digest="sha256:" + "6" * 64,
                byte_count=80,
            ),
        ),
        database_row_count=1,
        local_artifact_count=1,
        total_byte_count=200,
    )
    result = PurgePreparedResult(
        manifest=manifest,
        purge_digest=revision_digest(manifest),
        confirmation_code="P7RG3Q",
        expires_at_utc="2026-07-27T00:10:00Z",
    )
    assert result.manifest.entries == manifest.entries
    accepted = CommandOutcome.for_payload(
        PreparePurgePayload(run_id="run-1"),
        status="ACCEPTED",
        run_id="run-1",
        resulting_sequence=17,
        result=result,
    )
    assert accepted.result == result
    with pytest.raises(ValidationError):
        CommandOutcome.for_payload(
            PausePayload(run_id="run-1", reason="operator"),
            status="ACCEPTED",
            run_id="run-1",
            resulting_sequence=17,
            result=result,
        )
    with pytest.raises(ValidationError):
        PurgeManifestDocument.model_validate(
            {**manifest.model_dump(mode="json"), "database_row_count": 2}
        )


def test_command_payload_union_contains_each_approved_variant_once() -> None:
    union = get_args(CommandPayload)[0]
    payload_types = get_args(union)
    assert tuple(payload_type.model_fields["kind"].default for payload_type in payload_types) == (
        "create_run",
        "propose_policy",
        "approve_policy",
        "propose_budget",
        "approve_budget",
        "propose_model_configuration",
        "approve_model_configuration",
        "begin_planning",
        "approve_plan",
        "reject_plan",
        "start",
        "continue",
        "pause",
        "resume",
        "grant",
        "resolve_indeterminate",
        "integrate",
        "reconcile_cleanup",
        "cancel",
        "prepare_purge",
        "confirm_purge",
    )
