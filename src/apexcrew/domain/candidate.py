from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from typing import Literal

from pydantic import Field, model_validator

from apexcrew.domain.freshness import PromotedCandidate
from apexcrew.domain.revisions import FrozenDocument, Sha256DigestText
from apexcrew.domain.types import CandidateId, GitOid, RunId


def _digest(value: object) -> Sha256DigestText:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return Sha256DigestText("sha256:" + sha256(payload.encode("utf-8")).hexdigest())


class FrozenRunCandidate(FrozenDocument):
    schema_version: Literal["run-candidate-v1"] = "run-candidate-v1"
    candidate_id: CandidateId
    run_id: RunId
    source_candidate_digest: Sha256DigestText
    head_oid: GitOid = Field(pattern=r"^[0-9a-f]{40}$")
    target_ref: str = Field(pattern=r"^refs/heads/[A-Za-z0-9._/-]+$")
    checks_digest: Sha256DigestText
    candidate_digest: Sha256DigestText
    target_base_oid: GitOid = Field(default=GitOid("0" * 40), pattern=r"^[0-9a-f]{40}$")
    prepared_oid: GitOid = Field(default=GitOid("0" * 40), pattern=r"^[0-9a-f]{40}$")
    target_safety_digest: Sha256DigestText = "sha256:" + "0" * 64

    @model_validator(mode="after")
    def validate_digest(self) -> FrozenRunCandidate:
        expected = _digest(
            {
                "candidate_id": self.candidate_id,
                "checks_digest": self.checks_digest,
                "head_oid": self.head_oid,
                "run_id": self.run_id,
                "source_candidate_digest": self.source_candidate_digest,
                "target_ref": self.target_ref,
                "target_base_oid": self.target_base_oid,
                "prepared_oid": self.prepared_oid,
                "target_safety_digest": self.target_safety_digest,
            }
        )
        if self.candidate_digest != expected:
            raise ValueError("CANDIDATE_BINDING_MISMATCH")
        return self


def freeze_run_candidate(
    candidate: PromotedCandidate,
    *,
    run_id: str,
    candidate_id: str,
    head_oid: str,
    target_ref: str,
    checks_digest: str,
    target_base_oid: str | None = None,
    prepared_oid: str | None = None,
    target_safety_digest: str | None = None,
) -> FrozenRunCandidate:
    target_base = GitOid(head_oid if target_base_oid is None else target_base_oid)
    prepared = GitOid(head_oid if prepared_oid is None else prepared_oid)
    safety = Sha256DigestText(
        "sha256:" + "0" * 64 if target_safety_digest is None else target_safety_digest
    )
    digest = _digest(
        {
            "candidate_id": candidate_id,
            "checks_digest": checks_digest,
            "head_oid": head_oid,
            "run_id": run_id,
            "source_candidate_digest": candidate.candidate_digest,
            "target_ref": target_ref,
            "target_base_oid": target_base,
            "prepared_oid": prepared,
            "target_safety_digest": safety,
        }
    )
    return FrozenRunCandidate(
        candidate_id=CandidateId(candidate_id),
        run_id=RunId(run_id),
        source_candidate_digest=candidate.candidate_digest,
        head_oid=GitOid(head_oid),
        target_ref=target_ref,
        checks_digest=checks_digest,
        candidate_digest=digest,
        target_base_oid=target_base,
        prepared_oid=prepared,
        target_safety_digest=safety,
    )


@dataclass(slots=True)
class FinalGrant:
    grant_id: str
    candidate_digest: Sha256DigestText
    confirmation_code: str
    state: Literal["ISSUED", "CONSUMED"] = "ISSUED"


def issue_final_grant(
    candidate: FrozenRunCandidate, *, grant_id: str, confirmation_code: str
) -> FinalGrant:
    if len(confirmation_code) != 6 or not confirmation_code.isalnum():
        raise ValueError("GRANT_CONFIRMATION_CODE_INVALID")
    return FinalGrant(grant_id, candidate.candidate_digest, confirmation_code)


class TargetCasRequest(FrozenDocument):
    candidate_id: CandidateId
    target_ref: str
    expected_old_oid: GitOid = Field(pattern=r"^[0-9a-f]{40}$")
    new_oid: GitOid = Field(pattern=r"^[0-9a-f]{40}$")


def consume_final_grant(
    grant: FinalGrant,
    candidate: FrozenRunCandidate,
    *,
    confirmation_code: str,
    new_oid: str,
) -> TargetCasRequest:
    try:
        FrozenRunCandidate.model_validate(candidate.model_dump())
    except ValueError as error:
        raise ValueError("GRANT_CANDIDATE_MISMATCH") from error
    if grant.state != "ISSUED":
        raise ValueError("GRANT_ALREADY_CONSUMED")
    if grant.candidate_digest != candidate.candidate_digest:
        raise ValueError("GRANT_CANDIDATE_MISMATCH")
    if confirmation_code != grant.confirmation_code:
        raise ValueError("GRANT_CONFIRMATION_CODE_INVALID")
    request = TargetCasRequest(
        candidate_id=candidate.candidate_id,
        target_ref=candidate.target_ref,
        expected_old_oid=candidate.head_oid,
        new_oid=GitOid(new_oid),
    )
    grant.state = "CONSUMED"
    return request


def apply_typed_cas(
    request: TargetCasRequest, *, observed_old_oid: str
) -> Literal["APPLIED", "CONFLICT"]:
    if observed_old_oid != request.expected_old_oid:
        return "CONFLICT"
    return "APPLIED"
