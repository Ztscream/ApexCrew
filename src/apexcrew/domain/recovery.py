from __future__ import annotations

from enum import StrEnum


class RecoveryStatus(StrEnum):
    COMPLETED = "COMPLETED"
    RETRY = "RETRY"
    INDETERMINATE = "INDETERMINATE"


def reconcile_model(*, intent_recorded: bool, completion_committed: bool) -> RecoveryStatus:
    """Reconcile a provider call from durable journal facts only."""
    if completion_committed:
        return RecoveryStatus.COMPLETED
    if intent_recorded:
        return RecoveryStatus.INDETERMINATE
    return RecoveryStatus.RETRY


def reconcile_patch(*, observed_digest: str | None, expected_digest: str) -> RecoveryStatus:
    if observed_digest is None:
        return RecoveryStatus.INDETERMINATE
    if observed_digest == expected_digest:
        return RecoveryStatus.COMPLETED
    return RecoveryStatus.INDETERMINATE


def reconcile_ref(*, observed_oid: str | None, old_oid: str, new_oid: str) -> RecoveryStatus:
    if observed_oid == new_oid:
        return RecoveryStatus.COMPLETED
    if observed_oid == old_oid:
        return RecoveryStatus.RETRY
    return RecoveryStatus.INDETERMINATE
