from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from helpers.acceptance_lifecycle import run_fixture_repair
from test_purge_prepare import _artifact, _money_spec, _prepare, _TargetAuthority
from typer.testing import CliRunner

from apexcrew.adapters.state.sqlite import SqliteStateStore
from apexcrew.delivery.cli import app
from apexcrew.domain.commands import (
    ApplicableRevisionDigests,
    CommandEnvelope,
    ConfirmPurgePayload,
)
from apexcrew.domain.effects import StateConflict
from apexcrew.domain.types import RunId


class _RepositoryAuthority:
    def inspect(self, repository_root: str, target_ref: str):  # type: ignore[no-untyped-def]
        raise AssertionError(f"purge must not inspect Git: {repository_root}:{target_ref}")


def _confirm(store: SqliteStateStore, run_id: RunId, *, request_id: str, digest: str, code: str):
    command = CommandEnvelope(
        request_id=request_id,
        expected_sequence=store.audit_sequence(run_id),
        applicable_revision_digests=ApplicableRevisionDigests(),
        payload=ConfirmPurgePayload(
            run_id=run_id,
            purge_digest=digest,
            confirmation_code=code,
        ),
    )
    return store.apply_control_command(command, _TargetAuthority(store), _RepositoryAuthority())


def test_confirm_purge_removes_frozen_payload_and_never_touches_repository(
    tmp_path: Path,
) -> None:
    evidence = run_fixture_repair(tmp_path, _money_spec())
    store = SqliteStateStore(evidence.repository_root / ".apexcrew" / "state.db")
    try:
        entry = _artifact("retained", "retention/retained.bin", b"safe")
        payload_path = store.data_root / entry.relative_path
        payload_path.parent.mkdir(parents=True, exist_ok=True)
        payload_path.write_bytes(b"safe")
        store.register_retention_artifact(evidence.run_id, entry)

        prepared = _prepare(store, evidence.run_id)
        assert prepared.status == "ACCEPTED" and prepared.result is not None
        result = prepared.result
        outcome = _confirm(
            store,
            evidence.run_id,
            request_id="confirm-purge",
            digest=result.purge_digest,
            code=result.confirmation_code,
        )

        assert outcome.status == "ACCEPTED", outcome.model_dump(mode="json")
        assert outcome.failed_invariant is None
        assert not payload_path.exists()
        assert store.retention_artifacts(evidence.run_id) == ()
        assert evidence.repository_root.exists()
    finally:
        store.close()


def test_expired_purge_confirmation_fails_closed_without_deleting_payload(tmp_path: Path) -> None:
    evidence = run_fixture_repair(tmp_path, _money_spec())
    store = SqliteStateStore(evidence.repository_root / ".apexcrew" / "state.db")
    try:
        entry = _artifact("retained", "retention/retained.bin", b"safe")
        payload_path = store.data_root / entry.relative_path
        payload_path.parent.mkdir(parents=True, exist_ok=True)
        payload_path.write_bytes(b"safe")
        store.register_retention_artifact(evidence.run_id, entry)

        prepared = _prepare(store, evidence.run_id)
        assert prepared.status == "ACCEPTED" and prepared.result is not None
        result = prepared.result
        store._connection.execute(  # type: ignore[attr-defined]
            "UPDATE purge_manifests SET expires_at_utc = ? WHERE run_id = ?",
            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), evidence.run_id),
        )
        outcome = _confirm(
            store,
            evidence.run_id,
            request_id="confirm-expired-purge",
            digest=result.purge_digest,
            code=result.confirmation_code,
        )

        assert outcome.status == "DENIED"
        assert outcome.failed_invariant == "PURGE_CONFIRMATION_EXPIRED"
        assert payload_path.exists()
        assert store.retention_artifacts(evidence.run_id) == (entry,)
    finally:
        store.close()


def test_cli_prepare_and_confirm_purge_use_the_frozen_manifest(tmp_path: Path) -> None:
    evidence = run_fixture_repair(tmp_path, _money_spec())
    store = SqliteStateStore(evidence.repository_root / ".apexcrew" / "state.db")
    entry = _artifact("retained", "retention/retained.bin", b"safe")
    payload_path = store.data_root / entry.relative_path
    payload_path.parent.mkdir(parents=True, exist_ok=True)
    payload_path.write_bytes(b"safe")
    store.register_retention_artifact(evidence.run_id, entry)
    store.close()

    runner = CliRunner()
    prepared = runner.invoke(
        app,
        ["prepare-purge", str(evidence.run_id), "--root", str(evidence.repository_root)],
    )
    assert prepared.exit_code == 0, prepared.stdout
    prepared_payload = json.loads(prepared.stdout)
    assert prepared_payload["status"] == "PURGE_PREPARED"
    assert prepared_payload["manifest"]["local_artifact_count"] == 1

    confirmed = runner.invoke(
        app,
        [
            "confirm-purge",
            str(evidence.run_id),
            "--root",
            str(evidence.repository_root),
            "--purge-digest",
            prepared_payload["purge_digest"],
            "--confirmation-code",
            prepared_payload["confirmation_code"],
        ],
    )
    assert confirmed.exit_code == 0, confirmed.stdout
    assert json.loads(confirmed.stdout)["status"] == "PURGE_CONFIRMED"
    assert not payload_path.exists()


def test_purge_recovery_is_idempotent_for_missing_artifact(tmp_path: Path) -> None:
    evidence = run_fixture_repair(tmp_path, _money_spec())
    store = SqliteStateStore(evidence.repository_root / ".apexcrew" / "state.db")
    try:
        store.register_retention_artifact(
            evidence.run_id,
            _artifact("missing", "retention/missing.bin", b"gone"),
            state="DROPPED_BY_RETENTION",
        )
        prepared = _prepare(store, evidence.run_id)
        assert prepared.status == "ACCEPTED" and prepared.result is not None
        result = prepared.result
        assert result.kind == "purge_prepared"
        store._connection.execute(  # type: ignore[attr-defined]
            "UPDATE purge_manifests SET state = 'PURGING' WHERE run_id = ?",
            (evidence.run_id,),
        )
        first = store.recover_purge(evidence.run_id)
        second = store.recover_purge(evidence.run_id)
        assert first == 0
        assert second == 0
        assert store.retention_artifacts(evidence.run_id) == ()
        confirm = CommandEnvelope(
            request_id="confirm-after-recovery",
            expected_sequence=store.audit_sequence(evidence.run_id),
            payload=ConfirmPurgePayload(
                run_id=RunId(evidence.run_id),
                purge_digest=result.purge_digest,
                confirmation_code=result.confirmation_code,
            ),
        )
        outcome = store.apply_control_command(
            confirm, _TargetAuthority(store), _RepositoryAuthority()
        )
        assert outcome.status == "DENIED"
        assert outcome.failed_invariant == "RUN_ALREADY_PURGED"
        assert evidence.repository_root.exists()
    finally:
        store.close()


def test_purge_recovery_cannot_bypass_confirmation(tmp_path: Path) -> None:
    evidence = run_fixture_repair(tmp_path, _money_spec())
    store = SqliteStateStore(evidence.repository_root / ".apexcrew" / "state.db")
    try:
        prepared = _prepare(store, evidence.run_id)
        assert prepared.status == "ACCEPTED"
        with pytest.raises(StateConflict, match="PURGE_RECOVERY_REQUIRES_PURGING"):
            store.recover_purge(evidence.run_id)
    finally:
        store.close()


def test_prepare_purge_names_terminal_cleanup_next_action(tmp_path: Path) -> None:
    evidence = run_fixture_repair(tmp_path, _money_spec())
    store = SqliteStateStore(evidence.repository_root / ".apexcrew" / "state.db")
    try:
        store._connection.execute(  # type: ignore[attr-defined]
            "UPDATE target_reservations SET phase = 'REGISTERED_LOCKED' WHERE run_id = ?",
            (evidence.run_id,),
        )
        outcome = _prepare(store, evidence.run_id)
        assert outcome.status == "DENIED"
        assert outcome.failed_invariant == "TARGET_RESERVATION_CLEANUP_REQUIRED"
        assert outcome.safe_next_action == "reconcile terminal administrative cleanup"
    finally:
        store.close()
