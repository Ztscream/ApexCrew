from __future__ import annotations

import pytest

from apexcrew.domain.recovery import (
    RecoveryStatus,
    reconcile_model,
    reconcile_patch,
    reconcile_ref,
)


def test_model_completion_replays_as_completed_and_unknown_provider_is_indeterminate() -> None:
    assert (
        reconcile_model(intent_recorded=True, completion_committed=True) == RecoveryStatus.COMPLETED
    )
    assert (
        reconcile_model(intent_recorded=True, completion_committed=False)
        == RecoveryStatus.INDETERMINATE
    )


@pytest.mark.parametrize(
    ("observed", "expected", "result"),
    (
        ("sha256:" + "2" * 64, "sha256:" + "2" * 64, RecoveryStatus.COMPLETED),
        (None, "sha256:" + "2" * 64, RecoveryStatus.INDETERMINATE),
        ("sha256:" + "3" * 64, "sha256:" + "2" * 64, RecoveryStatus.INDETERMINATE),
    ),
)
def test_patch_recovery_never_guesses_unobserved_state(observed, expected, result) -> None:
    assert reconcile_patch(observed_digest=observed, expected_digest=expected) == result


def test_ref_recovery_retries_only_when_old_ref_is_still_observed() -> None:
    old = "a" * 40
    new = "b" * 40
    assert reconcile_ref(observed_oid=new, old_oid=old, new_oid=new) == RecoveryStatus.COMPLETED
    assert reconcile_ref(observed_oid=old, old_oid=old, new_oid=new) == RecoveryStatus.RETRY
    assert (
        reconcile_ref(observed_oid="c" * 40, old_oid=old, new_oid=new)
        == RecoveryStatus.INDETERMINATE
    )
