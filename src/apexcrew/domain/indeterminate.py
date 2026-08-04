from __future__ import annotations

import json
from hashlib import sha256
from typing import Literal, Self

from pydantic import Field, model_validator

from apexcrew.domain.revisions import FrozenDocument, Sha256DigestText


class IndeterminateResolution(RuntimeError):
    pass


class UnresolvedIntentSet(FrozenDocument):
    intents: tuple[str, ...] = Field(min_length=2)
    set_digest: Sha256DigestText
    status: Literal["INDETERMINATE"] = "INDETERMINATE"

    @classmethod
    def create(cls, intents: tuple[str, ...]) -> Self:
        ordered = tuple(sorted(set(intents)))
        if len(ordered) < 2 or any(not item for item in ordered):
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
