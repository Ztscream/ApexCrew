from __future__ import annotations

import pytest
from pydantic import ValidationError

from apexcrew.domain.evidence import ContextCapsule, EvidenceReceipt


def test_context_capsule_is_immutable_and_revision_bound() -> None:
    capsule = ContextCapsule.create(
        run_id="run-1",
        task_id="task-1",
        revision_digest="sha256:" + "1" * 64,
        dependencies=("sha256:" + "2" * 64,),
        content="read-only context",
    )

    assert capsule.content_digest.startswith("sha256:")
    assert capsule.binding_digest.startswith("sha256:")
    with pytest.raises(ValidationError):
        capsule.content = "changed"  # type: ignore[misc]

    receipt = EvidenceReceipt.create(capsule, result_class="CHECK_PASSED", result="ok")
    assert EvidenceReceipt.from_json(receipt.to_json()) == receipt
    with pytest.raises(ValueError, match="CAPSULE_BINDING_MISMATCH"):
        EvidenceReceipt.create(
            capsule.model_copy(update={"revision_digest": "sha256:" + "3" * 64}),
            result_class="CHECK_PASSED",
            result="ok",
        )
