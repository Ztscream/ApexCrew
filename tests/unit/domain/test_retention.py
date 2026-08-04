from __future__ import annotations

import pytest

from apexcrew.domain.retention import RetentionManager, RetentionNotImplemented


def test_tier_two_diagnostic_export_and_eviction_fail_closed() -> None:
    manager = RetentionManager()
    with pytest.raises(RetentionNotImplemented, match="TIER_TWO_EXPORT_DISABLED"):
        manager.export_diagnostic(tier=2, record={"secret": "value"})
    with pytest.raises(RetentionNotImplemented, match="EVICTION_NOT_IMPLEMENTED"):
        manager.evict(record_id="record-1")
