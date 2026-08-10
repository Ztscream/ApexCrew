from __future__ import annotations

from datetime import UTC, datetime, timedelta

from apexcrew.adapters.executor.memory_patch import MemoryPatchExecutor
from apexcrew.domain.authority import WorkspaceLease
from apexcrew.domain.plan import GlobPattern
from apexcrew.domain.policy import SecretPathPolicy


def _lease() -> WorkspaceLease:
    return WorkspaceLease(
        lease_id="lease-demo",
        run_id="demo-run",
        task_id="demo-task",
        attempt_id="demo-attempt",
        generation=1,
        base_head="1" * 40,
        admissible_head="1" * 40,
        task_contract_digest="sha256:" + "a" * 64,
        write_globs=(GlobPattern.parse("src/**"),),
        sensitivity_globs=(GlobPattern.parse("src/**"),),
        issued_at=datetime(2026, 8, 8, tzinfo=UTC),
        expires_at=datetime(2026, 8, 8, tzinfo=UTC) + timedelta(hours=1),
        state="ACTIVE",
    )


def test_memory_patch_applies_unified_diff_and_updates_workspace() -> None:
    executor = MemoryPatchExecutor(
        {"src/money.py": b"TOTAL_CENTS = 250\n"},
        secret_paths=SecretPathPolicy.from_host_rules((), b"k" * 32),
    )

    result = executor.apply_patch(
        _lease(),
        {
            "src/money.py": (
                b"--- a/src/money.py\n"
                b"+++ b/src/money.py\n"
                b"@@ -1 +1 @@\n"
                b"-TOTAL_CENTS = 250\n"
                b"+TOTAL_CENTS = 300\n"
            )
        },
    )

    assert result.code == "PATCH_APPLIED"
    assert executor.workspace_files()["src/money.py"] == b"TOTAL_CENTS = 300\n"


def test_memory_patch_rejects_malformed_diff_without_side_effect() -> None:
    executor = MemoryPatchExecutor(
        {"src/money.py": b"TOTAL_CENTS = 250\n"},
        secret_paths=SecretPathPolicy.from_host_rules((), b"k" * 32),
    )

    result = executor.apply_patch(_lease(), {"src/money.py": b"not a diff"})

    assert result.code == "LEASE_SCOPE_DENIED"
    assert executor.workspace_files()["src/money.py"] == b"TOTAL_CENTS = 250\n"


def test_memory_patch_rejects_oversized_hunk_header_without_side_effect() -> None:
    executor = MemoryPatchExecutor(
        {"src/money.py": b"TOTAL_CENTS = 250\n"},
        secret_paths=SecretPathPolicy.from_host_rules((), b"k" * 32),
    )
    oversized_diff = b"--- a/src/money.py\n+++ b/src/money.py\n@@ -" + b"9" * 5000 + b" +1 @@\n"

    result = executor.apply_patch(_lease(), {"src/money.py": oversized_diff})

    assert result.code == "LEASE_SCOPE_DENIED"
    assert executor.workspace_files()["src/money.py"] == b"TOTAL_CENTS = 250\n"
