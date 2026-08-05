from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from apexcrew.adapters.executor.fake import FakeExecutor, FakeProcessResult
from apexcrew.domain.authority import WorkspaceLease
from apexcrew.domain.plan import CheckDefinition, GlobPattern
from apexcrew.domain.policy import SecretPathPolicy
from apexcrew.domain.tools import (
    MAX_EXECUTOR_OUTPUT_BYTES,
    CheckDefinitionError,
    DeclaredCheckRegistry,
    ExecutionResult,
    SanitizedSnapshot,
    SanitizedSnapshotEntry,
)
from apexcrew.domain.types import AttemptId, RunId, TaskId


def active_lease(write_glob: str) -> WorkspaceLease:
    started = datetime(2026, 8, 2, 8, 0, tzinfo=UTC)
    return WorkspaceLease(
        lease_id="lease-1",
        run_id=RunId("run-1"),
        task_id=TaskId("task-1"),
        attempt_id=AttemptId("attempt-1"),
        generation=1,
        base_head="1" * 40,
        admissible_head="1" * 40,
        task_contract_digest="sha256:" + "1" * 64,
        write_globs=(GlobPattern.parse(write_glob),),
        sensitivity_globs=(GlobPattern.parse(write_glob),),
        issued_at=started,
        expires_at=started + timedelta(minutes=15),
        state="ACTIVE",
    )


def secret_policy(*rules: str) -> SecretPathPolicy:
    return SecretPathPolicy.from_host_rules(rules, installation_key=b"k" * 32)


def test_patch_outside_active_lease_has_zero_side_effects(tmp_path: Path) -> None:
    executor = FakeExecutor(tmp_path, secret_paths=secret_policy())

    result = executor.apply_patch(active_lease("src/**"), {"outside.txt": b"denied"})

    assert result.code == "LEASE_SCOPE_DENIED"
    assert executor.workspace_files() == {}
    assert not (tmp_path / "outside.txt").exists()


def test_check_rejects_shell_string() -> None:
    definition = CheckDefinition(
        argv=("pytest && curl example.invalid",),
        input_globs=(GlobPattern.parse("src/**"),),
    )

    with pytest.raises(CheckDefinitionError, match="^STRUCTURED_ARGV_REQUIRED$"):
        DeclaredCheckRegistry({"task-check-1": definition})


def test_executor_output_is_bounded_by_utf8_bytes_and_redacts_secret_error_lines() -> None:
    private_path = "private/probe.key"
    result = ExecutionResult.from_output(
        exit_code=1,
        timed_out=False,
        stdout_chunks=(b"assertion failed\n", ("x" * MAX_EXECUTOR_OUTPUT_BYTES).encode()),
        stderr_chunks=(f"failure while reading {private_path}\n".encode(),),
        timing_ms=12,
        secret_paths=secret_policy("private/**"),
    )

    assert result.code == "CHECK_FAILED"
    assert result.passed is False
    assert len(result.output.encode("utf-8")) <= MAX_EXECUTOR_OUTPUT_BYTES
    assert result.output_truncated is True
    assert private_path not in result.output
    assert "effective secret path" not in result.output


def test_unobservable_executor_outcome_is_not_a_semantic_failure() -> None:
    with pytest.raises(ValueError, match="^EXECUTOR_OUTCOME_UNOBSERVABLE$"):
        ExecutionResult.from_output(
            exit_code=None,
            timed_out=False,
            timing_ms=12,
            secret_paths=secret_policy(),
        )


def test_sanitized_snapshot_rejects_nonregular_entries() -> None:
    digest = "sha256:" + "1" * 64

    with pytest.raises(ValidationError, match="regular"):
        SanitizedSnapshot.model_validate(
            {
                "root": ".",
                "repository_id": "repository-1",
                "tree_digest": digest,
                "dependency_fingerprint_digest": digest,
                "entries": ({"path": "src/link.py", "kind": "symlink", "content_digest": digest},),
                "materialized_paths": ("src/link.py",),
            }
        )


def test_executor_rejects_forged_secret_snapshot_without_echoing_path(tmp_path: Path) -> None:
    private_path = "private/probe.txt"
    digest = "sha256:" + "1" * 64
    snapshot = SanitizedSnapshot(
        root=tmp_path,
        repository_id="repository-1",
        tree_digest=digest,
        dependency_fingerprint_digest=digest,
        entries=(
            SanitizedSnapshotEntry(
                path=private_path,
                kind="regular",
                content_digest=digest,
            ),
        ),
        materialized_paths=(private_path,),
    )
    executor = FakeExecutor(tmp_path, secret_paths=secret_policy("private/**"))
    executor.add_response(
        ("pytest",),
        digest,
        FakeProcessResult(exit_code=0, timed_out=False, timing_ms=1),
    )

    with pytest.raises(ValueError) as raised:
        executor.run(("pytest",), snapshot, timeout_seconds=600)

    assert str(raised.value) == "SANITIZED_SNAPSHOT_DENIED"
    assert private_path not in str(raised.value)
