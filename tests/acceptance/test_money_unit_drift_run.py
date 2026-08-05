from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from helpers.application import make_application, make_create_run_command
from helpers.git_repository import commit_repository, make_git_repository

from apexcrew.domain.types import GitOid


@pytest.mark.xfail(
    strict=True,
    reason=(
        "SKELETON_BOUNDARY: the acceptance application fixture still uses a fixed "
        "sqlite-only bootstrap authority, so a real Git repository is rejected "
        "before Run creation and before CrewRuntime can be composed."
    ),
)
def test_money_unit_drift_is_detected_and_repaired_end_to_end(tmp_path: Path) -> None:
    repository = make_git_repository(tmp_path)
    fixture = Path(__file__).parents[2] / "fixtures" / "python-money"
    shutil.copytree(fixture, repository, dirs_exist_ok=True)
    source = repository / "src" / "money.py"
    source.write_text(
        '"""Amounts are integer cents."""\n\n'
        "def add_cents(left_cents: int, right_cents: int) -> float:\n"
        "    return (left_cents + right_cents) / 100.0\n",
        encoding="utf-8",
    )
    initial_oid = GitOid(commit_repository(repository, "seed cents unit drift"))

    assert (
        subprocess.run(
            ["git", "-C", str(repository), "remote"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        == ""
    )
    assert " / 100.0" in source.read_text(encoding="utf-8")

    app = make_application(tmp_path)
    create = make_create_run_command()
    create = create.model_copy(
        update={
            "payload": create.payload.model_copy(
                update={
                    "repository_root": str(repository),
                    "expected_target_oid": initial_oid,
                }
            )
        }
    )

    # The intended continuation is the public control/runtime path:
    # planning read -> repair patch -> declared checks -> receipt -> Admission -> CAS.
    outcome = app.control.handle(create)
    assert outcome.status == "ACCEPTED", f"first boundary: {outcome.failed_invariant}"
