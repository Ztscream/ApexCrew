from __future__ import annotations

import pytest
from pydantic import ValidationError

from apexcrew.domain.indeterminate import ResolutionSelection, UnresolvedIntentSet
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
