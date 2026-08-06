from __future__ import annotations

import pytest
from pydantic import ValidationError

from apexcrew.domain.commands import ApplicableRevisionDigests
from apexcrew.domain.effects import (
    RecoveryActionClass,
    RecoveryDecisionKind,
    RecoveryObservation,
    abandon_observation,
    canonical_json,
    recover_observation,
    sha256_digest,
)
from apexcrew.domain.revisions import Sha256DigestText
from apexcrew.domain.types import AuditSequence, IntentId, RunId

PAYLOAD = Sha256DigestText("sha256:" + "1" * 64)
RESULT = Sha256DigestText("sha256:" + "2" * 64)
BOUNDED = '{"items":["src/a.py"]}'
BOUNDED_DIGEST = sha256_digest(BOUNDED)


def observation(
    action_class: RecoveryActionClass,
    state: str,
    **fields: object,
) -> RecoveryObservation:
    defaults: dict[str, object] = {
        "request_digest": PAYLOAD,
        "idempotency_key": "intent-1-key",
        "returned_model_id": "deepseek-chat",
        "provider_response_id": "response-1",
        "schema_digest": PAYLOAD,
        "usage_json": "{}",
        "run_id": RunId("run-1"),
        "settled_sequence": AuditSequence(1),
        "applicable_revision_digests": ApplicableRevisionDigests(),
        "snapshot_digest": PAYLOAD,
        "scope_digest": PAYLOAD,
        "ordering_digest": PAYLOAD,
        "expected_pre_tree_digest": PAYLOAD,
        "observed_post_tree_digest": RESULT,
        "check_id": "check-1",
        "argv_digest": PAYLOAD,
        "repository_id": "repo-1",
        "repository_instance_digest": PAYLOAD,
        "ref_name": "refs/heads/private",
        "registration_digest": PAYLOAD,
        "target_safety_digest": PAYLOAD,
        "old_oid": "old",
        "prepared_oid": "prepared",
        "current_oid": "current",
        "registration_identity": "reservation-1",
        "reservation_operation": "CREATE",
        "admin_binding_digest": PAYLOAD,
        "path_identity": "workspace-1",
        "gitfile_digest": PAYLOAD,
    }
    defaults.update(fields)
    if action_class in {RecoveryActionClass.PRIVATE_REF, RecoveryActionClass.TARGET_CAS}:
        if state == "EXACT_PRE":
            defaults["current_oid"] = defaults["old_oid"]
        elif state == "EXACT_POST":
            defaults["current_oid"] = defaults["prepared_oid"]
    if state == "EXACT_COMPLETION":
        normalized_json = canonical_json({"completion": "done"})
        defaults.setdefault("normalized_completion_json", normalized_json)
        defaults["normalized_completion_digest"] = sha256_digest(normalized_json)
    if state == "EXACT_SNAPSHOT":
        defaults.setdefault("bounded_result_json", BOUNDED)
        defaults.setdefault("bounded_result_digest", BOUNDED_DIGEST)
    if state == "EXACT_RECEIPT":
        defaults.setdefault("receipt_digest", RESULT)
    completed = (
        (action_class is RecoveryActionClass.MODEL and state == "EXACT_COMPLETION")
        or (action_class is RecoveryActionClass.READ_SEARCH and state == "EXACT_SNAPSHOT")
        or (action_class is RecoveryActionClass.CHECK and state == "EXACT_RECEIPT")
        or (
            action_class is RecoveryActionClass.TARGET_RESERVATION
            and (
                (defaults["reservation_operation"] == "CREATE" and state == "BOTH_PRESENT_LOCKED")
                or (defaults["reservation_operation"] == "CLEANUP" and state == "BOTH_ABSENT")
            )
        )
        or (
            action_class
            in {
                RecoveryActionClass.PATCH,
                RecoveryActionClass.PRIVATE_REF,
                RecoveryActionClass.TARGET_CAS,
                RecoveryActionClass.GRANTED_ACTION,
            }
            and state == "EXACT_POST"
        )
    )
    if completed:
        proof_fields = {
            RecoveryActionClass.MODEL: (
                "request_digest",
                "provider_response_id",
                "returned_model_id",
                "schema_digest",
                "usage_json",
                "normalized_completion_digest",
            ),
            RecoveryActionClass.PATCH: ("expected_pre_tree_digest", "observed_post_tree_digest"),
            RecoveryActionClass.CHECK: (
                "check_id",
                "argv_digest",
                "snapshot_digest",
                "receipt_digest",
            ),
            RecoveryActionClass.PRIVATE_REF: (
                "repository_id",
                "repository_instance_digest",
                "ref_name",
                "registration_digest",
                "target_safety_digest",
                "old_oid",
                "prepared_oid",
                "current_oid",
            ),
            RecoveryActionClass.TARGET_CAS: (
                "repository_id",
                "repository_instance_digest",
                "ref_name",
                "registration_digest",
                "target_safety_digest",
                "old_oid",
                "prepared_oid",
                "current_oid",
            ),
            RecoveryActionClass.TARGET_RESERVATION: (
                "registration_identity",
                "reservation_operation",
                "admin_binding_digest",
                "path_identity",
                "gitfile_digest",
            ),
            RecoveryActionClass.GRANTED_ACTION: (
                "pending_action_id",
                "grant_id",
                "expected_prestate_digest",
                "action_binding_digest",
            ),
            RecoveryActionClass.READ_SEARCH: (),
        }
        if action_class is RecoveryActionClass.MODEL:
            completed_json = defaults["normalized_completion_json"]
        elif action_class is RecoveryActionClass.PATCH:
            completed_json = canonical_json(
                {
                    "code": "PATCH_APPLIED",
                    "run_id": defaults["run_id"],
                    "intent_id": "intent-1",
                    "bounded_payload": {"snapshot_digest": defaults["snapshot_digest"]},
                }
            )
        elif action_class is RecoveryActionClass.CHECK:
            completed_json = canonical_json(
                {
                    "code": "CHECK_PASSED",
                    "run_id": defaults["run_id"],
                    "intent_id": "intent-1",
                    "passed": True,
                    "bounded_payload": {
                        "output": "",
                        "output_bytes": 0,
                        "snapshot_digest": defaults["snapshot_digest"],
                    },
                }
            )
        else:
            proof = {"state": state}
            proof.update({name: defaults[name] for name in proof_fields[action_class]})
            completed_json = canonical_json(proof)
        if action_class is RecoveryActionClass.READ_SEARCH:
            defaults.setdefault("bounded_result_json", completed_json)
            defaults.setdefault("bounded_result_digest", sha256_digest(completed_json))
        else:
            defaults.setdefault("completion_proof_json", completed_json)
            defaults.setdefault("completion_proof_digest", sha256_digest(completed_json))
    allowed = {
        RecoveryActionClass.MODEL: {
            "request_digest",
            "idempotency_key",
            "provider_response_id",
            "returned_model_id",
            "schema_digest",
            "usage_json",
            "normalized_completion_digest",
            "normalized_completion_json",
            "reservation_charge",
            "completion_proof_json",
            "completion_proof_digest",
        },
        RecoveryActionClass.READ_SEARCH: {
            "idempotency_key",
            "snapshot_digest",
            "scope_digest",
            "ordering_digest",
            "bounded_result_json",
            "bounded_result_digest",
        },
        RecoveryActionClass.PATCH: {
            "idempotency_key",
            "expected_pre_tree_digest",
            "observed_post_tree_digest",
            "completion_proof_json",
            "completion_proof_digest",
        },
        RecoveryActionClass.CHECK: {
            "idempotency_key",
            "check_id",
            "argv_digest",
            "snapshot_digest",
            "receipt_digest",
            "completion_proof_json",
            "completion_proof_digest",
        },
        RecoveryActionClass.PRIVATE_REF: {
            "idempotency_key",
            "repository_id",
            "repository_instance_digest",
            "ref_name",
            "registration_digest",
            "target_safety_digest",
            "old_oid",
            "prepared_oid",
            "current_oid",
            "completion_proof_json",
            "completion_proof_digest",
        },
        RecoveryActionClass.TARGET_CAS: {
            "idempotency_key",
            "repository_id",
            "repository_instance_digest",
            "ref_name",
            "registration_digest",
            "target_safety_digest",
            "old_oid",
            "prepared_oid",
            "current_oid",
            "completion_proof_json",
            "completion_proof_digest",
        },
        RecoveryActionClass.TARGET_RESERVATION: {
            "idempotency_key",
            "registration_identity",
            "reservation_operation",
            "admin_binding_digest",
            "path_identity",
            "gitfile_digest",
            "completion_proof_json",
            "completion_proof_digest",
        },
        RecoveryActionClass.GRANTED_ACTION: set(),
    }
    defaults = {
        key: value
        for key, value in defaults.items()
        if key in allowed[action_class]
        or key in {"run_id", "settled_sequence", "applicable_revision_digests"}
    }
    return RecoveryObservation.create(
        kind=action_class,
        intent_id=IntentId("intent-1"),
        recovery_generation=1,
        source_payload_digest=PAYLOAD,
        state=state,
        **defaults,
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


def test_model_exact_pre_is_never_retryable() -> None:
    decision = recover_observation(observation(RecoveryActionClass.MODEL, "EXACT_PRE"))
    assert decision.kind == RecoveryDecisionKind.INDETERMINATE


def test_model_unavailable_cannot_carry_normalized_output() -> None:
    with pytest.raises(ValidationError, match="COMPLETION_PROOF_FORBIDDEN"):
        observation(
            RecoveryActionClass.MODEL,
            "UNAVAILABLE",
            normalized_completion_json='{"completion":"must-not-escape"}',
        )


def test_read_search_same_snapshot_returns_bounded_payload() -> None:
    decision = recover_observation(
        observation(
            RecoveryActionClass.READ_SEARCH,
            "EXACT_SNAPSHOT",
            snapshot_digest=PAYLOAD,
            scope_digest=RESULT,
            bounded_result_json=BOUNDED,
            bounded_result_digest=BOUNDED_DIGEST,
        )
    )
    assert decision.kind == RecoveryDecisionKind.COMPLETED
    assert decision.bounded_result_json == '{"items":["src/a.py"]}'


def test_read_search_changed_scope_is_stale_without_content() -> None:
    decision = recover_observation(observation(RecoveryActionClass.READ_SEARCH, "STALE"))
    assert decision.kind == RecoveryDecisionKind.STALE
    assert decision.bounded_result_json is None


def test_observation_digest_is_canonical_and_bound_to_all_fields() -> None:
    valid = observation(RecoveryActionClass.PATCH, "EXACT_POST")
    altered = valid.model_dump()
    altered["observation_digest"] = PAYLOAD
    with pytest.raises(ValidationError, match="OBSERVATION_DIGEST_MISMATCH"):
        RecoveryObservation(**altered)


def test_stale_read_observation_cannot_carry_result_content() -> None:
    with pytest.raises(ValidationError, match="READ_RESULT_FORBIDDEN"):
        observation(
            RecoveryActionClass.READ_SEARCH,
            "STALE",
            bounded_result_json=BOUNDED,
            bounded_result_digest=BOUNDED_DIGEST,
        )


def test_read_result_rejects_sensitive_fields() -> None:
    secret_result = '{"items":[{"token":"redacted"}]}'
    with pytest.raises(ValidationError, match="READ_RESULT_NOT_SANITIZED"):
        observation(
            RecoveryActionClass.READ_SEARCH,
            "EXACT_SNAPSHOT",
            bounded_result_json=secret_result,
            bounded_result_digest=sha256_digest(secret_result),
        )


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


def test_abandon_requires_proven_no_authoritative_effect() -> None:
    decision = abandon_observation(
        observation(RecoveryActionClass.PATCH, "EXACT_PRE"), "PAUSED/PATCH_ABANDONED"
    )
    assert decision.kind == RecoveryDecisionKind.ABANDONED
    assert decision.successor == "PAUSED/PATCH_ABANDONED"


def test_model_abandon_is_owner_failure_not_generic_effect_abandon() -> None:
    with pytest.raises(ValueError, match="MODEL_ABANDON_REQUIRES_OWNER_FAILURE"):
        abandon_observation(observation(RecoveryActionClass.MODEL, "UNAVAILABLE"), "PAUSED")


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
        "BOTH_ABSENT": RecoveryDecisionKind.RETRY_SAME_INTENT,
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


def test_reservation_cleanup_absence_is_already_complete() -> None:
    decision = recover_observation(
        observation(
            RecoveryActionClass.TARGET_RESERVATION,
            "BOTH_ABSENT",
            reservation_operation="CLEANUP",
        )
    )
    assert decision.kind == RecoveryDecisionKind.COMPLETED


def test_reservation_cleanup_locked_state_requires_exact_cleanup_retry() -> None:
    decision = recover_observation(
        observation(
            RecoveryActionClass.TARGET_RESERVATION,
            "BOTH_PRESENT_LOCKED",
            reservation_operation="CLEANUP",
        )
    )
    assert decision.kind == RecoveryDecisionKind.RETRY_SAME_INTENT
