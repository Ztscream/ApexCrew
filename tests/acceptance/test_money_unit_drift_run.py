from __future__ import annotations

from pathlib import Path

from helpers.acceptance_lifecycle import FixtureRepairSpec, run_fixture_repair


def test_money_unit_drift_is_detected_and_repaired_end_to_end(tmp_path: Path) -> None:
    evidence = run_fixture_repair(
        tmp_path,
        FixtureRepairSpec(
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
        ),
    )
    assert evidence.initial_target_oid != evidence.prepared_oid
    assert evidence.target_source.endswith("return left_cents + right_cents")
