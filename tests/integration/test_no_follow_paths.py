from __future__ import annotations

import os
from pathlib import Path

import pytest

from apexcrew.adapters.repository.snapshot import FilesystemRepositorySnapshot
from apexcrew.adapters.state.sqlite import SqliteStateStore
from apexcrew.domain.actions import ReadAction, SearchAction
from apexcrew.domain.commands import ApplicableRevisionDigests
from apexcrew.domain.policy import SecretPathPolicy
from apexcrew.domain.tools import ScopedToolRuntime, ToolIntent
from apexcrew.domain.types import AttemptId, AuditSequence, IntentId, RunId, TaskId

SHA = "sha256:" + "1" * 64


def test_reparse_or_symlink_swap_is_denied_before_file_content_is_read(
    tmp_path: Path,
) -> None:
    (tmp_path / "src").mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside sentinel", encoding="utf-8")
    link = tmp_path / "src" / "safe.py"
    try:
        link.symlink_to(outside)
    except OSError as error:
        pytest.skip(f"symlink creation unavailable: {error}")

    runtime = ScopedToolRuntime(
        snapshot=FilesystemRepositorySnapshot(tmp_path),
        read_globs=("src/**",),
        secret_paths=SecretPathPolicy.from_host_rules((), installation_key=b"k" * 32),
        authorization_binding_digest=SHA,
        applicable_revision_digests=ApplicableRevisionDigests(),
        repository_id="repository-1",
        snapshot_digest=SHA,
        scope_digest=SHA,
        dependency_fingerprint_basis=SHA,
    )
    intent = ToolIntent.for_authorized_worker_action(
        intent_id=IntentId("intent-1"),
        run_id=RunId("run-1"),
        task_id=TaskId("task-1"),
        attempt_id=AttemptId("attempt-1"),
        action_id="action-1",
        action=ReadAction(path="src/safe.py"),
        authorization_binding_digest=SHA,
        applicable_revision_digests=ApplicableRevisionDigests(),
        repository_id="repository-1",
        snapshot_digest=SHA,
        scope_digest=SHA,
        dependency_fingerprint_basis=SHA,
        idempotency_key="tool:run-1:action-1",
        expected_prestate_json="{}",
    )
    assert runtime.execute(intent).code == "NO_FOLLOW_PATH_DENIED"
    assert outside.read_text(encoding="utf-8") == "outside sentinel"
    if os.name == "nt":
        assert link.is_symlink()


def _restart_intent(*, intent_id: str, action: ReadAction | SearchAction) -> ToolIntent:
    return ToolIntent.for_authorized_worker_action(
        intent_id=IntentId(intent_id),
        run_id=RunId("run-restart"),
        task_id=TaskId("task-1"),
        attempt_id=AttemptId("attempt-1"),
        action_id=intent_id,
        action=action,
        authorization_binding_digest=SHA,
        applicable_revision_digests=ApplicableRevisionDigests(),
        repository_id="repository-1",
        snapshot_digest=SHA,
        scope_digest=SHA,
        dependency_fingerprint_basis=SHA,
        idempotency_key=f"tool:{intent_id}",
        expected_prestate_json="{}",
    )


def test_read_and_search_intents_round_trip_after_restart(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    first = SqliteStateStore(database)
    documents = (
        _restart_intent(intent_id="intent-read", action=ReadAction(path="src/a.py")),
        _restart_intent(
            intent_id="intent-search",
            action=SearchAction(query="name", paths=("src/**",)),
        ),
    )
    for index, document in enumerate(documents):
        first.record_intent(
            document.to_effect_intent(AuditSequence(index + 1)), AuditSequence(index)
        )
    first.close()

    reopened = SqliteStateStore(database)
    assert (
        tuple(
            ToolIntent.from_effect_intent(reopened.effect_intent(document.intent_id))
            for document in documents
        )
        == documents
    )
    assert (
        tuple(
            ToolIntent.from_effect_intent(effect)
            for effect in reopened.unsettled_intents(documents[0].run_id)
        )
        == documents
    )
    reopened.close()
