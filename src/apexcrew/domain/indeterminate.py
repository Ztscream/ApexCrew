from __future__ import annotations

import json
from hashlib import sha256
from typing import Literal, Self

from pydantic import Field, model_validator

from apexcrew.domain.revisions import FrozenDocument, Sha256DigestText
from apexcrew.domain.types import IntentId


class IndeterminateResolution(RuntimeError):
    pass


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


class UnresolvedIntentSet(FrozenDocument):
    intents: tuple[str, ...] = Field(min_length=2)
    set_digest: Sha256DigestText
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

    @model_validator(mode="after")
    def validate_set(self) -> Self:
        if self.intents != tuple(sorted(set(self.intents))):
            raise ValueError("UNRESOLVED_SET_NOT_CANONICAL")
        payload = json.dumps(self.intents, separators=(",", ":"), ensure_ascii=True)
        expected = "sha256:" + sha256(payload.encode()).hexdigest()
        if self.set_digest != expected:
            raise ValueError("UNRESOLVED_SET_DIGEST_MISMATCH")
        return self


def resolve_multiple_intents(unresolved: UnresolvedIntentSet) -> None:
    del unresolved
    # DEBT-M2-001: no deterministic multi-intent precedence table exists yet.
    raise IndeterminateResolution("MULTIPLE_INTENTS_UNRESOLVED")
