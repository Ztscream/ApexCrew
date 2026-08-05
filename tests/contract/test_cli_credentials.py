from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from apexcrew.delivery.cli import app


def test_status_output_contains_no_key_bytes(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    sentinel = "deepseek-sentinel-credential-value"
    monkeypatch.setattr(
        "apexcrew.adapters.credentials.model_key.keyring.get_password",
        lambda *_: sentinel,
    )

    result = CliRunner().invoke(app, ["credentials", "status"])

    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout)["source"] == "keyring"
    assert all(
        sentinel[index : index + 4] not in result.stdout for index in range(len(sentinel) - 3)
    )


def test_set_rejects_value_as_argv() -> None:
    result = CliRunner().invoke(app, ["credentials", "set", "--value", "secret-value"])

    assert result.exit_code != 0


def test_clear_is_idempotent(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        "apexcrew.adapters.credentials.model_key.keyring.delete_password",
        lambda *_: _raise_missing_keyring_entry(),
    )

    runner = CliRunner()
    first = runner.invoke(app, ["credentials", "clear"])
    second = runner.invoke(app, ["credentials", "clear"])

    assert first.exit_code == 0, first.stdout
    assert second.exit_code == 0, second.stdout


def test_set_uses_hidden_prompt_and_doctor_reports_only_presence(
    monkeypatch, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    calls: list[bool] = []
    sentinel = "deepseek-sentinel-credential-value"
    monkeypatch.setattr(
        "apexcrew.delivery.cli.typer.prompt",
        lambda _message, hide_input=False: calls.append(hide_input) or sentinel,
    )
    monkeypatch.setattr(
        "apexcrew.adapters.credentials.model_key.keyring.set_password", lambda *_: None
    )
    monkeypatch.setattr(
        "apexcrew.adapters.credentials.model_key.keyring.get_password", lambda *_: sentinel
    )

    set_result = CliRunner().invoke(app, ["credentials", "set", "--root", str(tmp_path)])
    doctor_result = CliRunner().invoke(app, ["doctor", "--root", str(tmp_path)])

    assert set_result.exit_code == 0, set_result.stdout
    assert calls == [True]
    assert sentinel not in doctor_result.stdout
    assert json.loads(doctor_result.stdout)["credential_source"] == "keyring"


def _raise_missing_keyring_entry() -> None:
    import keyring.errors

    raise keyring.errors.PasswordDeleteError()
