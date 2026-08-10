from __future__ import annotations

from pathlib import Path

from helpers.acceptance_lifecycle import FixtureRepairSpec, run_fixture_repair


def test_timestamp_unit_drift_is_detected_and_repaired_end_to_end(tmp_path: Path) -> None:
    evidence = run_fixture_repair(
        tmp_path,
        FixtureRepairSpec(
            fixture_name="typescript-time",
            source_path="src/time.ts",
            seeded_source=(
                "/** Public time unit is integer milliseconds. */\n"
                "export type Milliseconds = number;\n\n"
                "export function addMilliseconds(left: Milliseconds, right: Milliseconds): Milliseconds {\n"
                "  return (left + right) / 1000;\n"
                "}\n"
            ),
            patch=("@@ -5 +5 @@\n-  return (left + right) / 1000;\n+  return left + right;\n"),
            repaired_source=(
                "/** Public time unit is integer milliseconds. */\n"
                "export type Milliseconds = number;\n\n"
                "export function addMilliseconds(left: Milliseconds, right: Milliseconds): Milliseconds {\n"
                "  return left + right;\n"
                "}\n"
            ),
            check_argv=("npm", "test"),
        ),
    )
    assert evidence.initial_target_oid != evidence.prepared_oid
    assert "return left + right;" in evidence.target_source
