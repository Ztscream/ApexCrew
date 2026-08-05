from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

import pytest

from apexcrew.adapters.repository.snapshot import MemoryRepositorySnapshot
from apexcrew.adapters.state.sqlite import SqliteStateStore
from apexcrew.domain.actions import ReadAction, SearchAction
from apexcrew.domain.commands import ApplicableRevisionDigests
from apexcrew.domain.plan import CanonicalPath
from apexcrew.domain.policy import SecretPathPolicy
from apexcrew.domain.tools import ScopedToolRuntime, SnapshotEntry, ToolIntent
from apexcrew.domain.types import AttemptId, AuditSequence, IntentId, RunId, TaskId

SHA = "sha256:" + "1" * 64


def tool_intent(
    action: ReadAction | SearchAction,
    *,
    intent_id: str = "intent-1",
) -> ToolIntent:
    return ToolIntent.for_authorized_worker_action(
        intent_id=IntentId(intent_id),
        run_id=RunId("run-1"),
        task_id=TaskId("task-1"),
        attempt_id=AttemptId("attempt-1"),
        action_id="action-1",
        action=action,
        authorization_binding_digest=SHA,
        applicable_revision_digests=ApplicableRevisionDigests(),
        repository_id="repository-1",
        snapshot_digest=SHA,
        scope_digest=SHA,
        dependency_fingerprint_basis=SHA,
        idempotency_key=f"tool:run-1:{intent_id}",
        expected_prestate_json="{}",
    )


def make_tool_runtime(
    *,
    read_globs: tuple[str, ...],
    files: dict[str, bytes] | None = None,
    secret_rules: tuple[str, ...] = (),
    denial_journal: SqliteStateStore | None = None,
) -> ScopedToolRuntime:
    return ScopedToolRuntime(
        snapshot=MemoryRepositorySnapshot(files or {}),
        read_globs=read_globs,
        secret_paths=SecretPathPolicy.from_host_rules(secret_rules, installation_key=b"k" * 32),
        authorization_binding_digest=SHA,
        applicable_revision_digests=ApplicableRevisionDigests(),
        repository_id="repository-1",
        snapshot_digest=SHA,
        scope_digest=SHA,
        dependency_fingerprint_basis=SHA,
        denial_journal=denial_journal,
        denial_expected_sequence=AuditSequence(0) if denial_journal is not None else None,
    )


def test_out_of_scope_search_is_rejected_before_content_exposure() -> None:
    tools = make_tool_runtime(read_globs=("src/**",), files={"private/key.txt": b"secret"})
    result = tools.execute(tool_intent(SearchAction(query="secret", paths=("private/**",))))
    assert result.code == "SCOPE_DENIED"
    assert result.matches == ()
    assert result.bounded_payload == {}


def test_search_orders_matches_by_path_then_byte_offset() -> None:
    tools = make_tool_runtime(
        read_globs=("src/**",),
        files={"src/b.py": b"x x", "src/a.py": b"x"},
    )
    result = tools.execute(tool_intent(SearchAction(query="x", paths=("src/**",))))
    assert [(match.path, match.byte_offset) for match in result.matches] == [
        ("src/a.py", 0),
        ("src/b.py", 0),
        ("src/b.py", 2),
    ]


def test_real_composed_policy_denies_an_effective_secret_before_read() -> None:
    tools = make_tool_runtime(
        read_globs=("**",),
        files={"private/key.txt": b"planted-value"},
        secret_rules=("private/**",),
    )
    result = tools.execute(tool_intent(ReadAction(path="private/key.txt")))
    assert result.code == "SECRET_PATH_DENIED"
    assert result.bounded_payload == {}
    assert result.matches == ()
    assert result.content_digest is None


class CountingSnapshot(MemoryRepositorySnapshot):
    def __init__(self, files: dict[str, bytes]) -> None:
        super().__init__(files)
        self.entries_calls = 0
        self.read_calls = 0

    def entries(self) -> tuple[SnapshotEntry, ...]:
        self.entries_calls += 1
        return super().entries()

    def read(self, path: CanonicalPath, maximum: int) -> bytes:
        self.read_calls += 1
        return super().read(path, maximum)


def test_secret_and_nonexistent_reads_have_the_same_metadata_only_io_shape() -> None:
    snapshot = CountingSnapshot({"private/probe.key": b"planted-private-value"})
    runtime = ScopedToolRuntime(
        snapshot=snapshot,
        read_globs=("**",),
        secret_paths=SecretPathPolicy.from_host_rules(("private/**",), installation_key=b"k" * 32),
        authorization_binding_digest=SHA,
        applicable_revision_digests=ApplicableRevisionDigests(),
        repository_id="repository-1",
        snapshot_digest=SHA,
        scope_digest=SHA,
        dependency_fingerprint_basis=SHA,
    )
    secret = runtime.execute(tool_intent(ReadAction(path="private/probe.key")))
    missing = runtime.execute(tool_intent(ReadAction(path="src/missing.py")))
    assert secret == missing
    assert snapshot.entries_calls == 2
    assert snapshot.read_calls == 0


def _audit_rows(database: Path) -> tuple[tuple[str, str, str], ...]:
    with sqlite3.connect(database) as connection:
        return tuple(
            connection.execute(
                "SELECT event_kind, correlation_json, payload_json "
                "FROM audit_events ORDER BY sequence"
            ).fetchall()
        )


def _all_sqlite_values(database: Path) -> str:
    with sqlite3.connect(database) as connection:
        tables = tuple(
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        )
        values = {
            table: connection.execute(f'SELECT * FROM "{table}"').fetchall() for table in tables
        }
    return json.dumps(values, ensure_ascii=True, default=str)


def test_denied_secret_read_does_not_disclose_or_reveal_existence(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    secret_path = "private/probe.key"
    secret_content = "planted-private-value"
    matching_rule = "private/**"
    secret_database = tmp_path / "secret-audit.db"
    missing_database = tmp_path / "missing-audit.db"
    secret_store = SqliteStateStore(secret_database)
    missing_store = SqliteStateStore(missing_database)
    secret_tools = make_tool_runtime(
        read_globs=("**",),
        files={secret_path: secret_content.encode()},
        secret_rules=(matching_rule,),
        denial_journal=secret_store,
    )
    missing_tools = make_tool_runtime(
        read_globs=("**",),
        files={},
        secret_rules=(matching_rule,),
        denial_journal=missing_store,
    )

    caplog.set_level(logging.DEBUG, logger="apexcrew")
    secret_intent = tool_intent(ReadAction(path=secret_path))
    missing_intent = tool_intent(ReadAction(path="src/missing.py"))
    secret_result = secret_tools.execute(secret_intent)
    missing_result = missing_tools.execute(missing_intent)
    assert secret_result == missing_result
    secret_store.close()
    missing_store.close()

    returned = secret_result.model_dump_json()
    secret_audit = _audit_rows(secret_database)
    missing_audit = _audit_rows(missing_database)
    assert secret_audit == missing_audit
    assert secret_audit[0][0] == "TOOL_ACTION_DENIED"
    with sqlite3.connect(secret_database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM effect_intents").fetchone()[0] == 0
    persisted = _all_sqlite_values(secret_database)
    logs = caplog.text
    for protected in (secret_path, secret_content, matching_rule, "effective secret path"):
        assert protected not in returned
        assert protected not in persisted
        assert protected not in logs
