from __future__ import annotations

from datetime import UTC, datetime, timedelta

from apexcrew.domain.retention import RetentionManager


def test_known_credentials_are_replaced_before_storage() -> None:
    manager = RetentionManager(known_credentials=("credential-value",))

    artifact = manager.persist(
        record_id="record-1",
        run_id="run-1",
        run_state="ACTIVE",
        tier=2,
        kind="response",
        content=b"token=credential-value",
    )

    assert artifact.state == "STORED"
    assert artifact.preview == b"token=[REDACTED]"
    assert b"credential-value" not in artifact.preview


def test_suspicious_content_is_quarantined_and_not_exported() -> None:
    manager = RetentionManager()

    artifact = manager.persist(
        record_id="record-1",
        run_id="run-1",
        run_state="ACTIVE",
        tier=2,
        kind="response",
        content=b"-----BEGIN PRIVATE KEY-----",
    )

    assert artifact.state == "QUARANTINED"
    assert artifact.preview is None
    assert manager.export_diagnostic(tier=2, record=artifact) is None


def test_preview_caps_keep_length_and_digest() -> None:
    manager = RetentionManager()
    content = b"x" * (128 * 1024 + 10)

    artifact = manager.persist(
        record_id="record-1",
        run_id="run-1",
        run_state="ACTIVE",
        tier=2,
        kind="prompt",
        content=content,
    )

    assert len(artifact.preview or b"") == 128 * 1024
    assert artifact.original_length == len(content)
    assert artifact.content_digest.startswith("sha256:")


def test_export_contains_tier_one_metadata_only() -> None:
    manager = RetentionManager()
    artifact = manager.persist(
        record_id="record-1",
        run_id="run-1",
        run_state="COMPLETED",
        tier=1,
        kind="audit",
        content=b"safe",
    )

    exported = manager.export_diagnostic(tier=1, record=artifact)

    assert exported == {
        "record_id": "record-1",
        "run_id": "run-1",
        "tier": 1,
        "kind": "audit",
        "state": "STORED",
        "original_length": 4,
        "content_digest": artifact.content_digest,
    }


def test_eviction_removes_expired_then_oldest_terminal() -> None:
    now = datetime(2026, 8, 6, tzinfo=UTC)
    manager = RetentionManager(max_bytes=3, now=lambda: now)
    manager.persist(
        record_id="expired",
        run_id="run-1",
        run_state="ACTIVE",
        tier=2,
        kind="response",
        content=b"12",
        persisted_at=now - timedelta(days=31),
    )
    manager.persist(
        record_id="terminal",
        run_id="run-2",
        run_state="COMPLETED",
        tier=2,
        kind="response",
        content=b"34",
        persisted_at=now - timedelta(days=2),
    )
    manager.persist(
        record_id="active",
        run_id="run-3",
        run_state="ACTIVE",
        tier=2,
        kind="response",
        content=b"56",
    )

    assert manager.get("expired") is None
    assert manager.get("terminal") is None
    assert manager.get("active") is not None


def test_overflow_keeps_metadata_for_active_artifact() -> None:
    manager = RetentionManager(max_bytes=1)

    artifact = manager.persist(
        record_id="record-1",
        run_id="run-1",
        run_state="ACTIVE",
        tier=2,
        kind="response",
        content=b"too large",
    )

    assert artifact.state == "DROPPED_BY_RETENTION"
    assert artifact.preview is None
    assert artifact.original_length == 9
