from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from apexcrew.adapters.credentials.model_key import KeyringModelCredentialStore
from apexcrew.adapters.model import deepseek_responses
from apexcrew.delivery.cli import app

LIVE_SMOKE_ENV = "APEXCREW_LIVE_SMOKE"


def _git(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ("git", *arguments),
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def test_cli_run_rejects_unauthorized_deepseek_before_permit_consumption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(LIVE_SMOKE_ENV, raising=False)
    root = tmp_path / "repository"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.name", "ApexCrew gate test")
    _git(root, "config", "user.email", "apexcrew-gate@example.test")
    (root / "task.py").write_text("value = 1\n", encoding="utf-8")
    _git(root, "add", "task.py")
    _git(root, "commit", "-qm", "gate fixture")
    target_oid = _git(root, "rev-parse", "refs/heads/main")
    _git(root, "checkout", "--detach", target_oid)

    runner = CliRunner()
    assert runner.invoke(app, ["init", "--root", str(root)]).exit_code == 0
    created = runner.invoke(
        app,
        [
            "run-create",
            "--root",
            str(root),
            "--target-ref",
            "refs/heads/main",
            "--goal",
            "gate test",
        ],
    )
    assert created.exit_code == 0, created.stdout
    run_id = json.loads(created.stdout)["run_id"]

    for command in ("approve-policy", "approve-budget", "approve-model"):
        preview = runner.invoke(app, [command, run_id, "--root", str(root), "--preview"])
        assert preview.exit_code == 0, preview.stdout
        values = json.loads(preview.stdout)
        accepted = runner.invoke(
            app,
            [
                command,
                run_id,
                "--root",
                str(root),
                "--digest",
                values["revision_digest"],
                "--confirmation-code",
                values["confirmation_code"],
            ],
        )
        assert accepted.exit_code == 0, accepted.stdout

    begin = runner.invoke(app, ["begin-planning", run_id, "--root", str(root)])
    assert begin.exit_code == 0, begin.stdout
    before = json.loads(runner.invoke(app, ["show", run_id, "--root", str(root)]).stdout)

    delivered = runner.invoke(app, ["run", run_id, "--root", str(root)])
    after = json.loads(runner.invoke(app, ["show", run_id, "--root", str(root)]).stdout)

    assert delivered.exit_code == 1
    assert json.loads(delivered.stdout) == {
        "failed_invariant": "LIVE_PROVIDER_NOT_AUTHORIZED",
        "status": "RUN_REJECTED",
    }
    assert after["sequence"] == before["sequence"]


@pytest.mark.skipif(
    os.environ.get(LIVE_SMOKE_ENV) != "1",
    reason=f"set {LIVE_SMOKE_ENV}=1 to authorize one live DeepSeek request",
)
def test_live_cli_approval_permit_runtime_lifecycle(tmp_path: Path) -> None:
    credentials = KeyringModelCredentialStore()
    if credentials.source("deepseek") == "absent":
        pytest.fail("APEXCREW_LIVE_SMOKE=1 requires a configured DeepSeek credential")

    root = tmp_path / "repository"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.name", "ApexCrew live smoke")
    _git(root, "config", "user.email", "apexcrew-live-smoke@example.test")
    (root / "src").mkdir()
    (root / "src" / "task.py").write_text("value = 1\n", encoding="utf-8")
    _git(root, "add", "src/task.py")
    _git(root, "commit", "-qm", "live smoke fixture")
    target_oid = _git(root, "rev-parse", "refs/heads/main")
    _git(root, "checkout", "--detach", target_oid)

    runner = CliRunner()
    initialized = runner.invoke(app, ["init", "--root", str(root)])
    assert initialized.exit_code == 0, initialized.stdout
    created = runner.invoke(
        app,
        [
            "run-create",
            "--root",
            str(root),
            "--target-ref",
            "refs/heads/main",
            "--goal",
            "complete the task",
            "--constraint",
            "stay within src",
            "--acceptance",
            "the task is complete",
        ],
    )
    assert created.exit_code == 0, created.stdout
    run_id = json.loads(created.stdout)["run_id"]

    for command in ("approve-policy", "approve-budget", "approve-model"):
        preview = runner.invoke(app, [command, run_id, "--root", str(root), "--preview"])
        assert preview.exit_code == 0, preview.stdout
        values = json.loads(preview.stdout)
        accepted = runner.invoke(
            app,
            [
                command,
                run_id,
                "--root",
                str(root),
                "--digest",
                values["revision_digest"],
                "--confirmation-code",
                values["confirmation_code"],
            ],
        )
        assert accepted.exit_code == 0, accepted.stdout

    begin = runner.invoke(app, ["begin-planning", run_id, "--root", str(root)])
    assert begin.exit_code == 0, begin.stdout

    original_client = deepseek_responses.OpenAI
    calls = 0

    class CountingClient:
        def __init__(self, **options: object) -> None:
            self._client = original_client(**options)
            self.responses = self

        def create(self, **request: object) -> object:
            nonlocal calls
            calls += 1
            return self._client.responses.create(**request)

    deepseek_responses.OpenAI = CountingClient  # type: ignore[assignment]
    try:
        delivered = runner.invoke(app, ["run", run_id, "--root", str(root)])
    finally:
        deepseek_responses.OpenAI = original_client

    assert delivered.exit_code == 0, delivered.stdout
    outcome = json.loads(delivered.stdout)
    assert outcome["status"] == "AWAITING_PLAN_APPROVAL"
    assert calls == 1
