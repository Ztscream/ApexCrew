from __future__ import annotations

import pytest

from apexcrew.domain.evidence import ContextCapsule, EvidenceReceipt
from apexcrew.domain.freshness import FreshnessAssessment, promote_candidate


def test_changed_dependency_makes_candidate_stale_and_blocks_promotion() -> None:
    capsule = ContextCapsule.create(
        run_id="run-1",
        task_id="task-1",
        revision_digest="sha256:" + "1" * 64,
        dependencies=("sha256:" + "2" * 64,),
        content="context",
    )
    receipt = EvidenceReceipt.create(capsule, result_class="CHECK_PASSED", result="ok")
    assessment = FreshnessAssessment.assess(
        receipt,
        current_revision="sha256:" + "1" * 64,
        current_dependencies=("sha256:" + "3" * 64,),
    )

    assert assessment.status == "STALE"
    with pytest.raises(ValueError, match="STALE_EVIDENCE"):
        promote_candidate(receipt, assessment)
