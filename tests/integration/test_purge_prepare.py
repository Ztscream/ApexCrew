from __future__ import annotations

from hashlib import sha256
from pathlib import Path

from helpers.acceptance_lifecycle import FixtureRepairSpec, run_fixture_repair

from apexcrew.adapters.state.sqlite import SqliteStateStore
from apexcrew.domain.commands import (
    ApplicableRevisionDigests,
    CommandEnvelope,
    PreparePurgePayload,
    PurgeLocalArtifactEntry,
)
from apexcrew.domain.types import RunId


class _TargetAuthority:
    def __init__(self, store: SqliteStateStore) -> None:
        self._store = store

    def current_for(self, run_id: RunId) -> str:
        return self._store.target_authority_digest(run_id)


class _RepositoryAuthority:
    def inspect(self, repository_root: str, target_ref: str):  # type: ignore[no-untyped-def]
        raise AssertionError(f"purge must not inspect Git: {repository_root}:{target_ref}")


def _money_spec() -> FixtureRepairSpec:
    return FixtureRepairSpec(
        fixture_name="python-money",
        source_path="src/money.py",
        seeded_source=(
            '"""Amounts are integer cents."""\n\n'
            "def add_cents(left_cents: int, right_cents: int) -> float:\n"
            "    return (left_cents + right_cents) / 100.0\n"
        ),
        patch=(
            "@@ -3,2 +3,2 @@\n"
            "-def add_cents(left_cents: int, right_cents: int) -> float:\n"
            "-    return (left_cents + right_cents) / 100.0\n"
            "+def add_cents(left_cents: int, right_cents: int) -> int:\n"
            "+    return left_cents + right_cents\n"
        ),
        repaired_source=(
            '"""Amounts are integer cents."""\n\n'
            "def add_cents(left_cents: int, right_cents: int) -> int:\n"
            "    return left_cents + right_cents\n"
        ),
        check_argv=("python", "-m", "pytest"),
    )


def _artifact(artifact_id: str, relative_path: str, content: bytes) -> PurgeLocalArtifactEntry:
    return PurgeLocalArtifactEntry(
        artifact_id=artifact_id,
        relative_path=relative_path,
        artifact_digest="sha256:" + sha256(content).hexdigest(),
        byte_count=len(content),
    )


def _prepare(store: SqliteStateStore, run_id: RunId):
    command = CommandEnvelope(
        request_id="prepare-purge",
        expected_sequence=store.audit_sequence(run_id),
        applicable_revision_digests=ApplicableRevisionDigests(),
        payload=PreparePurgePayload(run_id=run_id),
    )
    return store.apply_control_command(
        command,
        _TargetAuthority(store),
        _RepositoryAuthority(),
    )


def test_retained_quarantined_and_dropped_artifacts_enter_frozen_manifest(tmp_path: Path) -> None:
    evidence = run_fixture_repair(tmp_path, _money_spec())
    store = SqliteStateStore(evidence.repository_root / ".apexcrew" / "state.db")
    try:
        data_root = store.data_root
        entries = (
            _artifact("retained", "retention/retained.bin", b"safe"),
            _artifact("quarantined", "retention/quarantined.bin", b"private-key"),
            _artifact("dropped", "retention/dropped.bin", b""),
            _artifact("missing", "retention/missing.bin", b"gone"),
        )
        (data_root / "retention").mkdir(parents=True, exist_ok=True)
        (data_root / "retention" / "retained.bin").write_bytes(b"safe")
        (data_root / "retention" / "quarantined.bin").write_bytes(b"redacted")
        for entry, state in zip(
            entries, ("STORED", "QUARANTINED", "DROPPED_BY_RETENTION", "STORED"), strict=True
        ):
            store.register_retention_artifact(evidence.run_id, entry, state=state)
        outcome = _prepare(store, evidence.run_id)
        assert outcome.status == "ACCEPTED", outcome.model_dump(mode="json")
        assert outcome.result is not None
        manifest = outcome.result.manifest  # type: ignore[union-attr]
        assert {
            entry.artifact_id for entry in manifest.entries if entry.kind == "local_artifact"
        } == {
            "retained",
            "quarantined",
            "dropped",
            "missing",
        }
        assert manifest.local_artifact_count == 4
        assert manifest.total_byte_count >= 4
    finally:
        store.close()


def test_missing_retention_content_does_not_block_purge(tmp_path: Path) -> None:
    evidence = run_fixture_repair(tmp_path, _money_spec())
    store = SqliteStateStore(evidence.repository_root / ".apexcrew" / "state.db")
    try:
        store.register_retention_artifact(
            evidence.run_id,
            _artifact("missing", "retention/missing.bin", b"not-on-disk"),
            state="DROPPED_BY_RETENTION",
        )
        outcome = _prepare(store, evidence.run_id)
        assert outcome.status == "ACCEPTED", outcome.model_dump(mode="json")
        assert outcome.result is not None
        assert any(
            entry.kind == "local_artifact" and entry.artifact_id == "missing"
            for entry in outcome.result.manifest.entries  # type: ignore[union-attr]
        )
    finally:
        store.close()
