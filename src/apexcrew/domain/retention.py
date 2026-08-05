from __future__ import annotations


class RetentionNotImplemented(RuntimeError):
    pass


class RetentionManager:
    def export_diagnostic(self, *, tier: int, record: object) -> None:
        del record
        if tier == 2:
            # DEBT-M2-002: Tier 2 redaction/export policy is not implemented.
            raise RetentionNotImplemented("TIER_TWO_EXPORT_DISABLED")
        # DEBT-M2-003: retention-tier export is intentionally unavailable.
        raise RetentionNotImplemented("RETENTION_EXPORT_NOT_IMPLEMENTED")

    def evict(self, *, record_id: str) -> None:
        del record_id
        # DEBT-M2-004: durable retention eviction needs a reviewed tombstone policy.
        raise RetentionNotImplemented("EVICTION_NOT_IMPLEMENTED")
