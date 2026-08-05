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
        "SKELETON_BOUNDARY: E1 established that the acceptance application fixture "
        "uses a fixed sqlite-only bootstrap authority, so a real Git repository is "
        "rejected before Run creation and before CrewRuntime can be composed."
    ),
)
def test_timestamp_unit_drift_is_detected_and_repaired_end_to_end(tmp_path: Path) -> None:
    repository = make_git_repository(tmp_path)
    fixture = Path(__file__).parents[2] / "fixtures" / "typescript-time"
    shutil.copytree(fixture, repository, dirs_exist_ok=True)
    source = repository / "src" / "time.ts"
    source.write_text(
        "/** Public time unit is integer milliseconds. */\n"
        "export type Milliseconds = number;\n\n"
        "export function addMilliseconds(left: Milliseconds, right: Milliseconds): Milliseconds {\n"
        "  return (left + right) / 1000;\n"
        "}\n",
        encoding="utf-8",
    )
    initial_oid = GitOid(commit_repository(repository, "seed milliseconds unit drift"))

    assert (
        subprocess.run(
            ["git", "-C", str(repository), "remote"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        == ""
    )
    assert " / 1000" in source.read_text(encoding="utf-8")

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

    # E1's observed bootstrap boundary is intentionally not bypassed here.
    outcome = app.control.handle(create)
    assert outcome.status == "ACCEPTED", f"first boundary: {outcome.failed_invariant}"
