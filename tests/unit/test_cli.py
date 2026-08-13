from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from apexcrew.delivery.cli import app


def test_cli_exposes_required_commands_and_safe_terminal_results(tmp_path: Path) -> None:
    runner = CliRunner()
    for command in ("init", "run", "status", "approve", "doctor"):
        result = runner.invoke(app, [command, "--help"])
        assert result.exit_code == 0, result.stdout

    initialized = runner.invoke(app, ["init", "--root", str(tmp_path)])
    assert initialized.exit_code == 0
    assert json.loads(initialized.stdout)["status"] == "INITIALIZED"
    assert (tmp_path / ".apexcrew" / "config.json").is_file()

    refused = runner.invoke(app, ["run", "run-1", "--root", str(tmp_path)])
    assert refused.exit_code == 0
    assert json.loads(refused.stdout)["status"] == "NO_RUNTIME_PERMIT"
