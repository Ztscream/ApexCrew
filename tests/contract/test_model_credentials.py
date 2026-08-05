from __future__ import annotations

from pathlib import Path

import pytest

from apexcrew.adapters.credentials.model_key import (
    KeyringModelCredentialStore,
    ModelCredentialError,
)


def test_missing_credential_fails_closed_with_zero_side_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("APEXCREW_DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr(
        "apexcrew.adapters.credentials.model_key.keyring.get_password", lambda *_: None
    )
    store = KeyringModelCredentialStore()

    with pytest.raises(ModelCredentialError, match="MODEL_CREDENTIAL_MISSING"):
        store.resolve("deepseek")

    assert list(tmp_path.iterdir()) == []


def test_credential_never_appears_in_repr_or_str(monkeypatch: pytest.MonkeyPatch) -> None:
    sentinel = "deepseek-sentinel-credential-value"
    monkeypatch.setattr(
        "apexcrew.adapters.credentials.model_key.keyring.get_password",
        lambda *_: sentinel,
    )
    store = KeyringModelCredentialStore()

    assert store.resolve("deepseek") == sentinel
    assert sentinel not in repr(store)
    assert sentinel not in str(store)


def test_repository_dotenv_is_not_loaded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("APEXCREW_DEEPSEEK_API_KEY", raising=False)
    (tmp_path / ".env").write_text("APEXCREW_DEEPSEEK_API_KEY=dotenv-secret\n", encoding="utf-8")
    monkeypatch.setattr(
        "apexcrew.adapters.credentials.model_key.keyring.get_password", lambda *_: None
    )

    with pytest.raises(ModelCredentialError, match="MODEL_CREDENTIAL_MISSING"):
        KeyringModelCredentialStore().resolve("deepseek")


def test_keyring_precedes_environment_and_resolution_is_not_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = iter(("keyring-first", "keyring-second"))
    monkeypatch.setenv("APEXCREW_DEEPSEEK_API_KEY", "environment-value")
    monkeypatch.setattr(
        "apexcrew.adapters.credentials.model_key.keyring.get_password",
        lambda *_: next(values),
    )
    store = KeyringModelCredentialStore()

    assert store.resolve("deepseek") == "keyring-first"
    assert store.resolve("deepseek") == "keyring-second"


def test_environment_fallback_is_narrow_and_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APEXCREW_DEEPSEEK_API_KEY", "environment-value")
    monkeypatch.setattr(
        "apexcrew.adapters.credentials.model_key.keyring.get_password", lambda *_: None
    )

    assert KeyringModelCredentialStore().resolve("deepseek") == "environment-value"
