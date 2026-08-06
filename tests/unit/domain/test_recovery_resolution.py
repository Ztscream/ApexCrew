from __future__ import annotations

from apexcrew.domain.effects import (
    RecoveryActionClass,
    RecoveryDecisionKind,
    RecoveryObservation,
    recover_observation,
)
from apexcrew.domain.revisions import Sha256DigestText
from apexcrew.domain.types import IntentId

PAYLOAD = Sha256DigestText("sha256:" + "1" * 64)
RESULT = Sha256DigestText("sha256:" + "2" * 64)


def observation(
    action_class: RecoveryActionClass,
    state: str,
    **fields: object,
) -> RecoveryObservation:
    return RecoveryObservation(
        kind=action_class,
        intent_id=IntentId("intent-1"),
        recovery_generation=1,
        source_payload_digest=PAYLOAD,
        state=state,
        observation_digest=RESULT,
        **fields,
    )


def test_model_completed_normalized_response() -> None:
    decision = recover_observation(
        observation(
            RecoveryActionClass.MODEL,
            "EXACT_COMPLETION",
            request_digest=PAYLOAD,
            normalized_completion_digest=RESULT,
        )
    )
    assert decision.kind == RecoveryDecisionKind.COMPLETED


def test_model_authoritative_lookup_exact_response() -> None:
    decision = recover_observation(
        observation(
            RecoveryActionClass.MODEL,
            "EXACT_COMPLETION",
            request_digest=PAYLOAD,
            provider_response_id="response-1",
            normalized_completion_digest=RESULT,
        )
    )
    assert decision.kind == RecoveryDecisionKind.COMPLETED


def test_model_returned_model_mismatch_charges_full_reservation() -> None:
    decision = recover_observation(
        observation(
            RecoveryActionClass.MODEL,
            "RETURNED_MODEL_MISMATCH",
            request_digest=PAYLOAD,
            reservation_charge="FULL",
        )
    )
    assert decision.kind == RecoveryDecisionKind.INDETERMINATE
    assert decision.full_reservation_required is True


def test_model_unavailable_is_indeterminate() -> None:
    assert (
        recover_observation(observation(RecoveryActionClass.MODEL, "UNAVAILABLE")).kind
        == RecoveryDecisionKind.INDETERMINATE
    )


def test_read_search_same_snapshot_returns_bounded_payload() -> None:
    decision = recover_observation(
        observation(
            RecoveryActionClass.READ_SEARCH,
            "EXACT_SNAPSHOT",
            snapshot_digest=PAYLOAD,
            scope_digest=RESULT,
            bounded_result_json='{"items":["src/a.py"]}',
            bounded_result_digest=RESULT,
        )
    )
    assert decision.kind == RecoveryDecisionKind.COMPLETED
    assert decision.bounded_result_json == '{"items":["src/a.py"]}'


def test_read_search_changed_scope_is_stale_without_content() -> None:
    decision = recover_observation(observation(RecoveryActionClass.READ_SEARCH, "STALE"))
    assert decision.kind == RecoveryDecisionKind.STALE
    assert decision.bounded_result_json is None


def test_patch_exact_post_is_completed() -> None:
    assert (
        recover_observation(observation(RecoveryActionClass.PATCH, "EXACT_POST")).kind
        == RecoveryDecisionKind.COMPLETED
    )


def test_patch_exact_pre_is_retryable() -> None:
    assert (
        recover_observation(observation(RecoveryActionClass.PATCH, "EXACT_PRE")).kind
        == RecoveryDecisionKind.RETRY_SAME_INTENT
    )


def test_patch_third_state_is_conflict() -> None:
    assert (
        recover_observation(observation(RecoveryActionClass.PATCH, "THIRD_STATE")).kind
        == RecoveryDecisionKind.CONFLICT
    )


def test_check_exact_receipt_collapses_duplicate() -> None:
    decision = recover_observation(
        observation(
            RecoveryActionClass.CHECK,
            "EXACT_RECEIPT",
            check_id="check-1",
            argv_digest=PAYLOAD,
            receipt_digest=RESULT,
        )
    )
    assert decision.kind == RecoveryDecisionKind.COMPLETED


def test_check_exact_pre_reruns_same_argv() -> None:
    decision = recover_observation(
        observation(
            RecoveryActionClass.CHECK,
            "EXACT_PRE",
            check_id="check-1",
            argv_digest=PAYLOAD,
        )
    )
    assert decision.kind == RecoveryDecisionKind.RETRY_SAME_INTENT


def test_private_ref_requires_identity_and_registration_digest() -> None:
    decision = recover_observation(
        observation(
            RecoveryActionClass.PRIVATE_REF,
            "THIRD_STATE",
            repository_id="repo-1",
            registration_digest=PAYLOAD,
        )
    )
    assert decision.kind == RecoveryDecisionKind.CONFLICT


def test_target_cas_prepared_is_completed() -> None:
    assert (
        recover_observation(observation(RecoveryActionClass.TARGET_CAS, "EXACT_POST")).kind
        == RecoveryDecisionKind.COMPLETED
    )


def test_target_cas_exact_old_is_retryable() -> None:
    assert (
        recover_observation(observation(RecoveryActionClass.TARGET_CAS, "EXACT_PRE")).kind
        == RecoveryDecisionKind.RETRY_SAME_INTENT
    )


def test_target_cas_third_state_is_conflict() -> None:
    assert (
        recover_observation(observation(RecoveryActionClass.TARGET_CAS, "THIRD_STATE")).kind
        == RecoveryDecisionKind.CONFLICT
    )


def test_target_cas_target_unsafe_is_stale() -> None:
    assert (
        recover_observation(observation(RecoveryActionClass.TARGET_CAS, "TARGET_UNSAFE")).kind
        == RecoveryDecisionKind.STALE
    )


def test_reservation_creation_matrix_covers_absent_unlocked_locked_mixed_unobservable() -> None:
    expected = {
        "BOTH_ABSENT": RecoveryDecisionKind.COMPLETED,
        "BOTH_PRESENT_UNLOCKED": RecoveryDecisionKind.RETRY_SAME_INTENT,
        "BOTH_PRESENT_LOCKED": RecoveryDecisionKind.COMPLETED,
        "MIXED": RecoveryDecisionKind.CONFLICT,
        "UNAVAILABLE": RecoveryDecisionKind.INDETERMINATE,
    }
    for state, kind in expected.items():
        assert (
            recover_observation(observation(RecoveryActionClass.TARGET_RESERVATION, state)).kind
            == kind
        )
