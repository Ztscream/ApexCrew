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


def test_one_observed_member_is_selected_without_model_data() -> None:
    unresolved = UnresolvedIntentSet.create(("intent-a", "intent-b"))

    selected = resolve_multiple_intents(
        unresolved,
        observable_intent_ids=frozenset({"intent-b"}),
    )

    assert selected == "intent-b"


def test_two_observed_members_remain_indeterminate() -> None:
    unresolved = UnresolvedIntentSet.create(("intent-a", "intent-b"))

    with pytest.raises(IndeterminateResolution, match="MULTIPLE_INTENTS_UNRESOLVED"):
        resolve_multiple_intents(
            unresolved,
            observable_intent_ids=frozenset({"intent-a", "intent-b"}),
        )


def test_observation_set_cannot_select_an_intent_outside_the_set() -> None:
    unresolved = UnresolvedIntentSet.create(("intent-a", "intent-b"))

    with pytest.raises(IndeterminateResolution, match="OBSERVATION_MEMBER_NOT_IN_SET"):
        resolve_multiple_intents(
            unresolved,
            observable_intent_ids=frozenset({"intent-c"}),
        )
