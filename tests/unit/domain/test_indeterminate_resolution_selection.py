from __future__ import annotations

import pytest
from pydantic import ValidationError

from apexcrew.domain.effects import sha256_digest
from apexcrew.domain.indeterminate import (
    ResolutionSelection,
    UnresolvedIntentBinding,
    UnresolvedIntentSet,
)
from apexcrew.domain.types import IntentId


def test_resolution_selection_requires_exact_member_or_set_shape() -> None:
    unresolved = UnresolvedIntentSet.create(("intent-1", "intent-2"))
    with pytest.raises(ValidationError):
        ResolutionSelection(
            resolution="ABANDON_INTENT",
            unresolved_set_digest=unresolved.set_digest,
        )
    member = ResolutionSelection(
        resolution="ABANDON_INTENT",
        intent_id=IntentId("intent-1"),
        recovery_generation=1,
        unresolved_set_digest=unresolved.set_digest,
    )
    assert member.intent_id == "intent-1"
    with pytest.raises(ValidationError):
        ResolutionSelection(
            resolution="CANCEL_RUN",
            intent_id=IntentId("intent-1"),
            recovery_generation=1,
            unresolved_set_digest=unresolved.set_digest,
        )


def test_unresolved_set_rejects_duplicates_and_malformed_digest() -> None:
    with pytest.raises(ValueError, match="UNRESOLVED_SET_DUPLICATE_MEMBER"):
        UnresolvedIntentSet.create(("intent-1", "intent-1"))
    with pytest.raises(ValidationError):
        ResolutionSelection(
            resolution="FAIL_RUN",
            unresolved_set_digest="not-a-sha256-digest",
        )


def test_unresolved_set_digest_includes_member_generation_and_payload() -> None:
    first = UnresolvedIntentSet.from_members(
        (
            UnresolvedIntentBinding(
                intent_id="intent-1", recovery_generation=1, intent_digest=sha256_digest("one")
            ),
            UnresolvedIntentBinding(
                intent_id="intent-2", recovery_generation=1, intent_digest=sha256_digest("two")
            ),
        )
    )
    changed = UnresolvedIntentSet.from_members(
        (
            UnresolvedIntentBinding(
                intent_id="intent-1", recovery_generation=2, intent_digest=sha256_digest("one")
            ),
            UnresolvedIntentBinding(
                intent_id="intent-2", recovery_generation=1, intent_digest=sha256_digest("two")
            ),
        )
    )
    assert first.set_digest != changed.set_digest
