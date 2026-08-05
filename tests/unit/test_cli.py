from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

import pytest
from helpers.git_repository import commit_repository, make_git_repository
from typer.testing import CliRunner

from apexcrew.adapters.repository.no_follow import RepositoryUnsafeError
from apexcrew.adapters.state.sqlite import SqliteStateStore
from apexcrew.delivery import cli
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


@pytest.mark.parametrize(
    "failure",
    (
        RepositoryUnsafeError("unsafe"),
        sqlite3.Error("sqlite"),
        TimeoutError("timeout"),
        subprocess.TimeoutExpired(("git",), 1),
        UnicodeError("unicode"),
        OSError("os"),
        ValueError("value"),
    ),
)
@pytest.mark.parametrize("command", ["init", "run-create"])
def test_cli_bootstrap_failures_are_bounded_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
    command: str,
) -> None:
    root = make_git_repository(tmp_path)

    class FailingAuthority:
        def __init__(self) -> None:
            raise failure

    monkeypatch.setattr(cli, "RepositoryBootstrapAuthorityService", FailingAuthority)
    arguments = [command, "--root", str(root)]
    if command == "run-create":
        arguments.extend(["--target-ref", "refs/heads/main", "--goal", "bootstrap"])

    result = CliRunner().invoke(app, arguments)

    assert result.exit_code == 1
    output = json.loads(result.stdout)
    assert output["status"] == ("INIT_REJECTED" if command == "init" else "RUN_CREATE_REJECTED")
    assert output["failed_invariant"] in {
        "CONFIGURATION_INVALID",
        "CONTROL_PATH_UNSAFE",
        "REPOSITORY_BOOTSTRAP_REJECTED",
        "STATE_STORE_UNAVAILABLE",
    }
    assert "Traceback" not in result.stdout


def test_cli_status_and_doctor_reject_non_regular_config(tmp_path: Path) -> None:
    root = make_git_repository(tmp_path)
    control_dir = root / ".apexcrew"
    control_dir.mkdir()
    (control_dir / "config.json").mkdir()

    for command in ("status", "doctor"):
        result = CliRunner().invoke(app, [command, "--root", str(root)])

        assert result.exit_code == 1
        assert json.loads(result.stdout) == {
            "failed_invariant": "CONTROL_PATH_UNSAFE",
            "status": f"{command.upper()}_REJECTED",
        }


def test_cli_status_and_doctor_reject_config_symlink(tmp_path: Path) -> None:
    root = make_git_repository(tmp_path)
    control_dir = root / ".apexcrew"
    control_dir.mkdir()
    outside = tmp_path / "outside-config.json"
    outside.write_text("outside\n", encoding="utf-8")
    try:
        (control_dir / "config.json").symlink_to(outside)
    except OSError as error:
        pytest.skip(f"symlink creation unavailable: {error}")

    for command in ("status", "doctor"):
        result = CliRunner().invoke(app, [command, "--root", str(root)])

        assert result.exit_code == 1
        assert json.loads(result.stdout) == {
            "failed_invariant": "CONTROL_PATH_UNSAFE",
            "status": f"{command.upper()}_REJECTED",
        }


def test_cli_init_failure_injection_halts_before_config_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = make_git_repository(tmp_path)

    def fail_write(_guard: object) -> None:
        raise RepositoryUnsafeError("identity changed")

    monkeypatch.setattr(cli.ControlPathGuard, "write_config_if_missing", fail_write)

    result = CliRunner().invoke(app, ["init", "--root", str(root)])

    assert result.exit_code == 1
    assert json.loads(result.stdout) == {
        "failed_invariant": "CONTROL_PATH_UNSAFE",
        "status": "INIT_REJECTED",
    }
    assert not (root / ".apexcrew" / "config.json").exists()


def test_sqlite_store_closes_connection_when_migration_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeConnection:
        closed = False
        row_factory: object = None

        def execute(self, _sql: str) -> object:
            return self

        def close(self) -> None:
            self.closed = True

    connection = FakeConnection()
    monkeypatch.setattr(sqlite3, "connect", lambda *_args, **_kwargs: connection)

    def fail_migrations(_store: SqliteStateStore) -> None:
        raise sqlite3.Error("migration")

    monkeypatch.setattr(SqliteStateStore, "_apply_migrations", fail_migrations)

    with pytest.raises(sqlite3.Error, match="migration"):
        SqliteStateStore(tmp_path / "state.db")

    assert connection.closed
