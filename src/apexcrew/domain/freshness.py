from __future__ import annotations

import json
from hashlib import sha256
from typing import Literal, Self

from pydantic import model_validator

from apexcrew.domain.evidence import EvidenceReceipt
from apexcrew.domain.revisions import FrozenDocument, Sha256DigestText


def _digest(value: object) -> Sha256DigestText:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return Sha256DigestText("sha256:" + sha256(payload.encode()).hexdigest())


class FreshnessAssessment(FrozenDocument):
    status: Literal["FRESH", "STALE", "INDETERMINATE"]
    reason: str
    observed_revision: Sha256DigestText
    observed_dependencies: tuple[Sha256DigestText, ...]

    @classmethod
    def assess(
        cls,
        receipt: EvidenceReceipt,
        *,
        current_revision: str,
        current_dependencies: tuple[str, ...],
    ) -> Self:
        if receipt.revision_digest != current_revision:
            return cls(
                status="STALE",
                reason="REVISION_CHANGED",
                observed_revision=current_revision,
                observed_dependencies=current_dependencies,
            )
        status: Literal["FRESH", "STALE"] = (
            "FRESH" if receipt.dependencies == current_dependencies else "STALE"
        )
        return cls(
            status=status,
            reason="NO_DEPENDENCY_CHANGE" if status == "FRESH" else "DEPENDENCY_CHANGED",
            observed_revision=current_revision,
            observed_dependencies=current_dependencies,
        )


class PromotedCandidate(FrozenDocument):
    schema_version: Literal["candidate-v1"] = "candidate-v1"
    receipt: EvidenceReceipt
    freshness: FreshnessAssessment
    candidate_digest: Sha256DigestText

    @model_validator(mode="after")
    def require_fresh(self) -> Self:
        if self.freshness.status != "FRESH":
            raise ValueError("STALE_EVIDENCE")
        return self


def promote_candidate(
    receipt: EvidenceReceipt, assessment: FreshnessAssessment
) -> PromotedCandidate:
    if assessment.status != "FRESH":
        raise ValueError("STALE_EVIDENCE")
    digest = _digest(
        {
            "receipt": receipt.model_dump(mode="json"),
            "freshness": assessment.model_dump(mode="json"),
        }
    )
    return PromotedCandidate(receipt=receipt, freshness=assessment, candidate_digest=digest)
