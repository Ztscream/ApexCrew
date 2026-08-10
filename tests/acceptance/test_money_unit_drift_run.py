from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from helpers.application import make_create_run_command
from helpers.git_repository import commit_repository, make_git_repository

from apexcrew.adapters.model.scripted import ScriptedMockLLM
from apexcrew.adapters.repository.bootstrap import RepositoryBootstrapAuthorityService
from apexcrew.application.composition import build_application_bundle
from apexcrew.domain.types import GitOid


def test_money_unit_drift_enters_the_production_run_boundary(tmp_path: Path) -> None:
    repository = make_git_repository(tmp_path)
    subprocess.run(["git", "-C", str(repository), "branch", "-M", "main"], check=True)
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
    subprocess.run(
        ["git", "-C", str(repository), "checkout", "--detach", str(initial_oid)], check=True
    )

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

    bundle = build_application_bundle(
        tmp_path / "apexcrew-data",
        repository_authority=RepositoryBootstrapAuthorityService(),
        model_configuration=create.payload.model_configuration_revision,
        budget=create.payload.budget_revision,
        scripted_model=ScriptedMockLLM(()),
    )
    try:
        outcome = bundle.control.handle(create)
        assert outcome.status == "ACCEPTED", f"production boundary: {outcome.failed_invariant}"
        assert outcome.run_id is not None
        assert bundle.queries.get(outcome.run_id).state == "DRAFT"
    finally:
        bundle.close()
