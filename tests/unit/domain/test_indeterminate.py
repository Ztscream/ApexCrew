from __future__ import annotations

import pytest

from apexcrew.domain.indeterminate import (
    IndeterminateResolution,
    UnresolvedIntentSet,
    resolve_multiple_intents,
)


def test_multiple_unresolved_intents_are_never_auto_resolved() -> None:
    unresolved = UnresolvedIntentSet.create(("intent-a", "intent-b"))
    assert unresolved.status == "INDETERMINATE"
    with pytest.raises(IndeterminateResolution, match="MULTIPLE_INTENTS_UNRESOLVED"):
        resolve_multiple_intents(unresolved)
