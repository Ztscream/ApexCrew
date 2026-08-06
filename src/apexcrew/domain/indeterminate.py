from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from typing import Literal, Self

from pydantic import Field, model_validator

from apexcrew.domain.revisions import FrozenDocument, Sha256DigestText
from apexcrew.domain.types import AuditSequence, IntentId, RunId, RuntimeOwnerId


class IndeterminateResolution(RuntimeError):
    pass


class UnresolvedIntentBinding(FrozenDocument):
    intent_id: str = Field(min_length=1)
    recovery_generation: int = Field(ge=1)
    intent_digest: Sha256DigestText

    @model_validator(mode="after")
    def validate_member(self) -> Self:
        if self.intent_id != self.intent_id.strip():
            raise ValueError("UNRESOLVED_SET_MEMBER_INVALID")
        return self


class ResolutionSelection(FrozenDocument):
    resolution: Literal[
        "RECONCILE_OBSERVED",
        "RETRY_SAME_INTENT",
        "ABANDON_INTENT",
        "FAIL_RUN",
        "CANCEL_RUN",
    ]
    unresolved_set_digest: Sha256DigestText
    intent_id: IntentId | None = None
    recovery_generation: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_shape(self) -> Self:
        member_bound = {
            "RECONCILE_OBSERVED",
            "RETRY_SAME_INTENT",
            "ABANDON_INTENT",
        }
        if self.resolution in member_bound:
            if self.intent_id is None or self.recovery_generation is None:
                raise ValueError("MEMBER_RESOLUTION_BINDING_REQUIRED")
            if (
                not str(self.intent_id).strip()
                or str(self.intent_id) != str(self.intent_id).strip()
            ):
                raise ValueError("MEMBER_RESOLUTION_INTENT_INVALID")
        elif self.intent_id is not None or self.recovery_generation is not None:
            raise ValueError("SET_RESOLUTION_FORBIDS_MEMBER_BINDING")
        return self


def unresolved_set_digest_for_members(
    members: tuple[UnresolvedIntentBinding, ...],
) -> Sha256DigestText:
    payload = json.dumps(
        [
            member.model_dump(mode="json")
            for member in sorted(members, key=lambda item: item.intent_id)
        ],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return Sha256DigestText("sha256:" + sha256(payload.encode()).hexdigest())


@dataclass(frozen=True, slots=True)
class ApplyResolutionRequest:
    run_id: RunId
    selection: ResolutionSelection
    permit_generation: int
    owner_id: RuntimeOwnerId
    expected_sequence: AuditSequence
    decision: object | None = None


@dataclass(frozen=True, slots=True)
class ResolutionApplication:
    status: Literal["SETTLED", "RETRY", "ABANDONED", "DENIED"]
    resulting_sequence: AuditSequence
    remaining_set_digest: Sha256DigestText | None
    successor: str


class UnresolvedIntentSet(FrozenDocument):
    intents: tuple[str, ...] = Field(min_length=1)
    set_digest: Sha256DigestText
    member_bindings: tuple[UnresolvedIntentBinding, ...] = ()
    status: Literal["INDETERMINATE"] = "INDETERMINATE"

    @classmethod
    def create(cls, intents: tuple[str, ...]) -> Self:
        if len(intents) != len(set(intents)):
            raise ValueError("UNRESOLVED_SET_DUPLICATE_MEMBER")
        if any(not item or item != item.strip() for item in intents):
            raise ValueError("UNRESOLVED_SET_MEMBER_INVALID")
        ordered = tuple(sorted(intents))
        if len(ordered) < 2:
            raise ValueError("MULTIPLE_INTENTS_REQUIRED")
        payload = json.dumps(ordered, separators=(",", ":"), ensure_ascii=True)
        return cls(
            intents=ordered,
            set_digest=Sha256DigestText("sha256:" + sha256(payload.encode()).hexdigest()),
        )

    @classmethod
    def from_members(cls, members: tuple[UnresolvedIntentBinding, ...]) -> Self:
        intents = tuple(member.intent_id for member in members)
        if not intents:
            raise ValueError("UNRESOLVED_SET_EMPTY")
        ordered = tuple(sorted(members, key=lambda item: item.intent_id))
        return cls(
            intents=tuple(sorted(intents)),
            member_bindings=tuple(sorted(members, key=lambda item: item.intent_id)),
            set_digest=unresolved_set_digest_for_members(ordered),
        )

    @model_validator(mode="after")
    def validate_set(self) -> Self:
        if self.intents != tuple(sorted(set(self.intents))):
            raise ValueError("UNRESOLVED_SET_NOT_CANONICAL")
        if self.member_bindings:
            binding_ids = tuple(member.intent_id for member in self.member_bindings)
            if binding_ids != self.intents:
                raise ValueError("UNRESOLVED_SET_BINDINGS_MISMATCH")
            payload = json.dumps(
                [member.model_dump(mode="json") for member in self.member_bindings],
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            )
        else:
            payload = json.dumps(self.intents, separators=(",", ":"), ensure_ascii=True)
        expected = "sha256:" + sha256(payload.encode()).hexdigest()
        if self.set_digest != expected:
            raise ValueError("UNRESOLVED_SET_DIGEST_MISMATCH")
        return self


def resolve_multiple_intents(unresolved: UnresolvedIntentSet) -> None:
    del unresolved
    # DEBT-M2-001: no deterministic multi-intent precedence table exists yet.
    raise IndeterminateResolution("MULTIPLE_INTENTS_UNRESOLVED")
