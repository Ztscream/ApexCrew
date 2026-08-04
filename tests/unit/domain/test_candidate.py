from __future__ import annotations

import pytest

from apexcrew.domain.candidate import (
    TargetCasRequest,
    consume_final_grant,
    freeze_run_candidate,
    issue_final_grant,
)
from apexcrew.domain.evidence import ContextCapsule, EvidenceReceipt
from apexcrew.domain.freshness import FreshnessAssessment, promote_candidate


def _candidate():
    capsule = ContextCapsule.create(
        run_id="run-1",
        task_id="task-1",
        revision_digest="sha256:" + "1" * 64,
        dependencies=(),
        content="verified",
    )
    receipt = EvidenceReceipt.create(capsule, result_class="CHECK_PASSED", result="ok")
    assessment = FreshnessAssessment.assess(
        receipt,
        current_revision=receipt.revision_digest,
        current_dependencies=(),
    )
    return freeze_run_candidate(
        promote_candidate(receipt, assessment),
        run_id="run-1",
        candidate_id="candidate-1",
        head_oid="a" * 40,
        target_ref="refs/heads/main",
        checks_digest="sha256:" + "2" * 64,
    )


def test_only_frozen_candidate_can_issue_and_consume_one_final_grant() -> None:
    candidate = _candidate()
    grant = issue_final_grant(candidate, grant_id="grant-1", confirmation_code="ABC123")
    request = consume_final_grant(grant, candidate, confirmation_code="ABC123", new_oid="b" * 40)

    assert request == TargetCasRequest(
        candidate_id="candidate-1",
        target_ref="refs/heads/main",
        expected_old_oid="a" * 40,
        new_oid="b" * 40,
    )
    with pytest.raises(ValueError, match="GRANT_ALREADY_CONSUMED"):
        consume_final_grant(grant, candidate, confirmation_code="ABC123", new_oid="b" * 40)


def test_grant_subject_or_expected_target_mismatch_fails_closed() -> None:
    candidate = _candidate()
    grant = issue_final_grant(candidate, grant_id="grant-1", confirmation_code="ABC123")

    with pytest.raises(ValueError, match="GRANT_CANDIDATE_MISMATCH"):
        consume_final_grant(
            grant,
            candidate.model_copy(update={"head_oid": "c" * 40}),
            confirmation_code="ABC123",
            new_oid="b" * 40,
        )
