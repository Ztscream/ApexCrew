from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Literal

from apexcrew.domain.commands import PurgeLocalArtifactEntry

RetentionState = Literal["STORED", "QUARANTINED", "DROPPED_BY_RETENTION"]

_PREVIEW_CAPS = {
    "prompt": 128 * 1024,
    "response": 128 * 1024,
    "diff": 256 * 1024,
    "stdout": 64 * 1024,
    "stderr": 64 * 1024,
}
_SUSPICIOUS_PATTERNS = (
    re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(rb"(?i)\b(?:bearer|api[_-]?key|access[_-]?token)\s*[:=]\s*\S+"),
    re.compile(rb"(?i)\bsk-[a-z0-9_-]{12,}"),
)


@dataclass(frozen=True, slots=True)
class RetentionArtifact:
    record_id: str
    run_id: str
    run_state: str
    tier: Literal[1, 2]
    kind: str
    state: RetentionState
    persisted_at: datetime
    original_length: int
    content_digest: str
    preview: bytes | None
    relative_path: str


class RetentionManager:
    """Manage local diagnostic artifacts without widening the query surface."""

    def __init__(
        self,
        *,
        known_credentials: tuple[str, ...] = (),
        max_bytes: int = 1 << 30,
        now: Callable[[], datetime] | None = None,
        data_root: Path | None = None,
    ) -> None:
        if max_bytes < 0:
            raise ValueError("RETENTION_CAP_INVALID")
        self._known_credentials = tuple(
            value.encode("utf-8") for value in known_credentials if value
        )
        self._max_bytes = max_bytes
        self._now = now or (lambda: datetime.now(UTC))
        self._data_root = None if data_root is None else data_root.resolve()
        self._artifacts: dict[str, RetentionArtifact] = {}
        # Metadata survives payload eviction so purge can be frozen without reading bytes.
        self._inventory: dict[str, RetentionArtifact] = {}

    def persist(
        self,
        *,
        record_id: str,
        run_id: str,
        run_state: str,
        tier: Literal[1, 2],
        kind: str,
        content: bytes,
        persisted_at: datetime | None = None,
    ) -> RetentionArtifact:
        if not record_id or not run_id or not kind or tier not in {1, 2}:
            raise ValueError("RETENTION_RECORD_INVALID")
        if persisted_at is None:
            persisted_at = self._now()
        if persisted_at.tzinfo is None:
            raise ValueError("RETENTION_TIMESTAMP_MUST_BE_AWARE")

        original_length = len(content)
        original_digest = "sha256:" + sha256(content).hexdigest()
        redacted = self._redact(content)
        suspicious = any(pattern.search(redacted) for pattern in _SUSPICIOUS_PATTERNS)
        state: RetentionState = "QUARANTINED" if suspicious else "STORED"
        preview = None if suspicious else redacted[: _PREVIEW_CAPS.get(kind, 64 * 1024)]
        artifact = RetentionArtifact(
            record_id=record_id,
            run_id=run_id,
            run_state=run_state,
            tier=tier,
            kind=kind,
            state=state,
            persisted_at=persisted_at,
            original_length=original_length,
            content_digest=original_digest,
            preview=preview,
            relative_path=("retention/" + sha256(record_id.encode("utf-8")).hexdigest() + ".bin"),
        )

        self._artifacts.pop(record_id, None)
        self._inventory.pop(record_id, None)
        self._evict_for(len(preview or b""))
        if self._stored_bytes() + len(preview or b"") > self._max_bytes:
            artifact = replace(artifact, state="DROPPED_BY_RETENTION", preview=None)
        self._artifacts[record_id] = artifact
        self._inventory[record_id] = artifact
        if (
            self._data_root is not None
            and artifact.state == "STORED"
            and artifact.preview is not None
        ):
            path = self._data_root / artifact.relative_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(artifact.preview)
        return artifact

    def export_diagnostic(
        self, *, tier: Literal[1, 2], record: RetentionArtifact
    ) -> dict[str, object] | None:
        if tier != 1 or record.tier != 1 or record.state != "STORED":
            return None
        return {
            "record_id": record.record_id,
            "run_id": record.run_id,
            "tier": record.tier,
            "kind": record.kind,
            "state": record.state,
            "original_length": record.original_length,
            "content_digest": record.content_digest,
        }

    def evict(self, *, record_id: str) -> bool:
        return self._artifacts.pop(record_id, None) is not None

    def get(self, record_id: str) -> RetentionArtifact | None:
        return self._artifacts.get(record_id)

    def purge_inventory(self, run_id: str) -> tuple[PurgeLocalArtifactEntry, ...]:
        """Return terminal artifact metadata without requiring a payload to exist."""
        entries = [
            PurgeLocalArtifactEntry(
                artifact_id=artifact.record_id,
                relative_path=artifact.relative_path,
                artifact_digest=artifact.content_digest,
                byte_count=len(artifact.preview or b""),
            )
            for artifact in self._inventory.values()
            if artifact.run_id == run_id
            and artifact.run_state in {"COMPLETED", "FAILED", "CANCELLED"}
        ]
        return tuple(
            sorted(entries, key=lambda entry: (entry.relative_path, entry.artifact_digest))
        )

    def purge(self, run_id: str) -> tuple[str, ...]:
        """Forget metadata after an external frozen manifest has been deleted."""
        record_ids = tuple(
            artifact.record_id for artifact in self._inventory.values() if artifact.run_id == run_id
        )
        for record_id in record_ids:
            self._artifacts.pop(record_id, None)
            self._inventory.pop(record_id, None)
        return record_ids

    def _redact(self, content: bytes) -> bytes:
        redacted = content
        for credential in sorted(self._known_credentials, key=len, reverse=True):
            redacted = redacted.replace(credential, b"[REDACTED]")
        return redacted

    def _stored_bytes(self) -> int:
        return sum(len(artifact.preview or b"") for artifact in self._artifacts.values())

    def _evict_for(self, incoming_bytes: int) -> None:
        while self._stored_bytes() + incoming_bytes > self._max_bytes:
            expired = [
                artifact
                for artifact in self._artifacts.values()
                if artifact.tier == 2 and self._now() - artifact.persisted_at >= timedelta(days=30)
            ]
            candidates = expired or [
                artifact
                for artifact in self._artifacts.values()
                if artifact.run_state in {"COMPLETED", "FAILED", "CANCELLED"}
                and artifact.state == "STORED"
            ]
            if not candidates:
                return
            oldest = min(candidates, key=lambda artifact: artifact.persisted_at)
            self._artifacts.pop(oldest.record_id, None)
