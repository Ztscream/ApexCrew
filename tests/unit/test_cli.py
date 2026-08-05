from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from helpers.git_repository import commit_repository, make_git_repository
from typer.testing import CliRunner

from apexcrew.delivery.cli import app


def test_cli_exposes_required_commands_and_safe_terminal_results(tmp_path: Path) -> None:
    root = make_git_repository(tmp_path)
    runner = CliRunner()
    for command in ("init", "run", "status", "approve", "doctor"):
        result = runner.invoke(app, [command, "--help"])
        assert result.exit_code == 0, result.stdout

    initialized = runner.invoke(app, ["init", "--root", str(root)])
    assert initialized.exit_code == 0
    assert json.loads(initialized.stdout)["status"] == "INITIALIZED"
    assert (root / ".apexcrew" / "config.json").is_file()

    refused = runner.invoke(app, ["run", "run-1", "--root", str(root)])
    assert refused.exit_code == 0
    assert json.loads(refused.stdout)["status"] == "NO_RUNTIME_PERMIT"


@pytest.mark.parametrize(
    ("command", "arguments", "status"),
    (
        ("init", (), "INIT_REJECTED"),
        (
            "run-create",
            ("--target-ref", "refs/heads/main", "--goal", "bootstrap"),
            "RUN_CREATE_REJECTED",
        ),
    ),
)
def test_cli_rejects_invalid_root_with_bounded_json(
    tmp_path: Path,
    command: str,
    arguments: tuple[str, ...],
    status: str,
) -> None:
    result = CliRunner().invoke(app, [command, "--root", str(tmp_path), *arguments])

    assert result.exit_code == 1
    output = json.loads(result.stdout)
    assert output == {"failed_invariant": "REPOSITORY_BOOTSTRAP_REJECTED", "status": status}
    assert "Traceback" not in result.stdout
    assert not (tmp_path / ".apexcrew" / "config.json").exists()


def test_cli_init_rejects_apexcrew_directory_symlink_without_writing_outside(
    tmp_path: Path,
) -> None:
    root = make_git_repository(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (root / ".apexcrew").symlink_to(outside, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"symlink creation unavailable: {error}")

    result = CliRunner().invoke(app, ["init", "--root", str(root)])

    assert result.exit_code == 1
    assert json.loads(result.stdout) == {
        "failed_invariant": "CONTROL_PATH_UNSAFE",
        "status": "INIT_REJECTED",
    }
    assert not (outside / "config.json").exists()


def test_cli_run_create_rejects_state_database_symlink_without_writing_outside(
    tmp_path: Path,
) -> None:
    root = make_git_repository(tmp_path)
    (root / "README.md").write_text("bootstrap\n", encoding="utf-8")
    expected_oid = commit_repository(root, "bootstrap")
    subprocess.run(["git", "-C", str(root), "branch", "feature"], check=True)
    control_dir = root / ".apexcrew"
    control_dir.mkdir()
    outside_database = tmp_path / "outside-state.db"
    outside_database.write_bytes(b"outside")
    try:
        (control_dir / "state.db").symlink_to(outside_database)
    except OSError as error:
        pytest.skip(f"symlink creation unavailable: {error}")

    result = CliRunner().invoke(
        app,
        [
            "run-create",
            "--root",
            str(root),
            "--target-ref",
            "refs/heads/feature",
            "--goal",
            "bootstrap",
        ],
    )

    assert result.exit_code == 1
    assert json.loads(result.stdout) == {
        "failed_invariant": "CONTROL_PATH_UNSAFE",
        "status": "RUN_CREATE_REJECTED",
    }
    assert outside_database.read_bytes() == b"outside"
    assert expected_oid not in result.stdout
