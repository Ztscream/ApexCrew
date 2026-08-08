from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from apexcrew.adapters.executor.attempt_patch import (
    AttemptPatchExecutionError,
    AttemptPatchExecutor,
)
from apexcrew.adapters.executor.memory_patch import MemoryPatchExecutor
from apexcrew.adapters.repository.no_follow import StableHandleTree
from apexcrew.domain.authority import WorkspaceLease
from apexcrew.domain.effects import canonical_json, sha256_digest
from apexcrew.domain.plan import GlobPattern
from apexcrew.domain.policy import SecretPathPolicy
from apexcrew.domain.revisions import Sha256DigestText
from apexcrew.domain.types import AttemptId, RunId, TaskId


def _secret_policy(*rules: str) -> SecretPathPolicy:
    return SecretPathPolicy.from_host_rules(rules, b"k" * 32)


def _lease(*write_globs: str) -> WorkspaceLease:
    now = datetime.now(UTC)
    return WorkspaceLease(
        lease_id="lease-1",
        run_id=RunId("run-1"),
        task_id=TaskId("task-1"),
        attempt_id=AttemptId("attempt-1"),
        generation=1,
        base_head="a" * 40,
        admissible_head="a" * 40,
        task_contract_digest="sha256:" + "1" * 64,
        write_globs=tuple(GlobPattern.parse(value) for value in write_globs),
        sensitivity_globs=(),
        issued_at=now,
        expires_at=now + timedelta(minutes=15),
        state="ACTIVE",
    )


def _tree_digest(files: dict[str, bytes]) -> Sha256DigestText:
    payload = canonical_json(
        {
            path: "sha256:" + hashlib.sha256(content).hexdigest()
            for path, content in sorted(files.items())
        }
    )
    return sha256_digest(payload)


def test_patch_writes_real_bytes(tmp_path: Path) -> None:
    root = tmp_path / "check"
    (root / "src").mkdir(parents=True)
    original = b"value = 1\n"
    updated = b"value = 2\n"
    (root / "src" / "task.py").write_bytes(original)
    executor = AttemptPatchExecutor(root, _secret_policy())

    result = executor.apply_patch(
        _lease("src/**"),
        {
            "src/task.py": b"@@ -1 +1 @@\n-value = 1\n+value = 2\n",
        },
    )

    assert result.code == "PATCH_APPLIED"
    assert result.post_tree_digest == _tree_digest({"src/task.py": updated})
    assert (root / "src" / "task.py").read_bytes() == updated
    memory_result = MemoryPatchExecutor(
        {"src/task.py": original}, secret_paths=_secret_policy()
    ).apply_patch(
        _lease("src/**"),
        {"src/task.py": b"@@ -1 +1 @@\n-value = 1\n+value = 2\n"},
    )
    assert result.post_tree_digest == memory_result.post_tree_digest


def test_patch_outside_write_globs_denied_with_zero_side_effects(tmp_path: Path) -> None:
    root = tmp_path / "check"
    (root / "src").mkdir(parents=True)
    target = root / "src" / "task.py"
    target.write_bytes(b"value = 1\n")
    before = target.read_bytes()
    executor = AttemptPatchExecutor(root, _secret_policy())

    result = executor.apply_patch(
        _lease("src/**"),
        {
            "tests/task.py": b"@@ -0,0 +1 @@\n+created\n",
        },
    )

    assert result.code == "LEASE_SCOPE_DENIED"
    assert target.read_bytes() == before
    assert not (root / "tests").exists()


def test_malformed_diff_denied_with_zero_side_effects(tmp_path: Path) -> None:
    root = tmp_path / "check"
    (root / "src").mkdir(parents=True)
    target = root / "src" / "task.py"
    target.write_bytes(b"value = 1\n")
    before = target.read_bytes()
    executor = AttemptPatchExecutor(root, _secret_policy())

    result = executor.apply_patch(
        _lease("src/**"),
        {"src/task.py": b"this is not a unified diff\n"},
    )

    assert result.code == "LEASE_SCOPE_DENIED"
    assert target.read_bytes() == before
    assert tuple(root.glob("src/.task.py.*.tmp")) == ()


def test_post_replace_failure_is_uncertain_not_scope_denied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "check"
    (root / "src").mkdir(parents=True)
    target = root / "src" / "task.py"
    target.write_bytes(b"value = 1\n")
    original_assert = StableHandleTree.assert_name_bindings
    calls = 0

    def fail_after_replace(tree: StableHandleTree) -> None:
        nonlocal calls
        calls += 1
        if calls == 3:
            raise OSError("injected post-replace failure")
        original_assert(tree)

    monkeypatch.setattr(StableHandleTree, "assert_name_bindings", fail_after_replace)

    with pytest.raises(AttemptPatchExecutionError, match="PATCH_RESULT_UNCERTAIN"):
        AttemptPatchExecutor(root, _secret_policy()).apply_patch(
            _lease("src/**"),
            {"src/task.py": b"@@ -1 +1 @@\n-value = 1\n+value = 2\n"},
        )
    assert target.read_bytes() == b"value = 2\n"


@pytest.mark.skipif(os.name == "nt", reason="ancestor replacement requires POSIX symlinks")
def test_ancestor_replacement_between_preflight_and_write_denied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "check"
    source = root / "src"
    source.mkdir(parents=True)
    target = source / "task.py"
    target.write_bytes(b"value = 1\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "task.py").write_bytes(b"outside\n")
    original_assert = StableHandleTree.assert_name_bindings
    calls = 0

    def replace_ancestor(tree: StableHandleTree) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            moved = root / "src-real"
            source.rename(moved)
            source.symlink_to(outside, target_is_directory=True)
        original_assert(tree)

    monkeypatch.setattr(StableHandleTree, "assert_name_bindings", replace_ancestor)
    executor = AttemptPatchExecutor(root, _secret_policy())

    result = executor.apply_patch(
        _lease("src/**"),
        {"src/task.py": b"@@ -1 +1 @@\n-value = 1\n+value = 2\n"},
    )

    source.unlink()
    (root / "src-real").rename(source)
    assert result.code == "LEASE_SCOPE_DENIED"
    assert target.read_bytes() == b"value = 1\n"
