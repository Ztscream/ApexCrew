"""Host-local model credential ports."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from typing import Final, Literal, Protocol

import keyring

CredentialSource = Literal["keyring", "env", "absent"]

MODEL_KEYRING_SERVICE: Final = "apexcrew"
DEEPSEEK_PROFILE: Final = "deepseek"
_PROFILE_PATTERN: Final = re.compile(r"^[a-z][a-z0-9_-]*$")
_PROFILE_ENVIRONMENT: Final = {DEEPSEEK_PROFILE: "APEXCREW_DEEPSEEK_API_KEY"}


class ModelCredentialError(RuntimeError):
    """Fail-closed model credential resolution error."""


class ModelCredentialPort(Protocol):
    def resolve(self, profile: str) -> str:
        """Resolve a model credential at request time."""


def _validate_profile(profile: str) -> str:
    if not isinstance(profile, str) or _PROFILE_PATTERN.fullmatch(profile) is None:
        raise ModelCredentialError("MODEL_CREDENTIAL_PROFILE_INVALID")
    return profile


def _account(profile: str) -> str:
    return f"model-credential-{profile}"


def _usable(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ModelCredentialError("MODEL_CREDENTIAL_INVALID")
    return value if value.strip() else None


class KeyringModelCredentialStore:
    """Resolve model keys from keyring first and a narrow CI env fallback."""

    def __init__(self, service: str = MODEL_KEYRING_SERVICE) -> None:
        self._service = service

    def __repr__(self) -> str:
        return f"{type(self).__name__}(service={self._service!r})"

    def __str__(self) -> str:
        return repr(self)

    def _keyring_value(self, profile: str) -> str | None:
        try:
            return _usable(keyring.get_password(self._service, _account(profile)))
        except keyring.errors.KeyringError as error:
            raise ModelCredentialError("MODEL_CREDENTIAL_KEYRING_UNAVAILABLE") from error

    def _environment_value(self, profile: str) -> str | None:
        variable = _PROFILE_ENVIRONMENT.get(profile)
        return None if variable is None else _usable(os.environ.get(variable))

    def source(self, profile: str) -> CredentialSource:
        profile = _validate_profile(profile)
        if self._keyring_value(profile) is not None:
            return "keyring"
        if self._environment_value(profile) is not None:
            return "env"
        return "absent"

    def resolve(self, profile: str) -> str:
        profile = _validate_profile(profile)
        credential = self._keyring_value(profile)
        if credential is not None:
            return credential
        credential = self._environment_value(profile)
        if credential is not None:
            return credential
        raise ModelCredentialError("MODEL_CREDENTIAL_MISSING")

    def set(self, profile: str, credential: str) -> None:
        profile = _validate_profile(profile)
        if _usable(credential) is None:
            raise ModelCredentialError("MODEL_CREDENTIAL_INVALID")
        try:
            keyring.set_password(self._service, _account(profile), credential)
        except keyring.errors.KeyringError as error:
            raise ModelCredentialError("MODEL_CREDENTIAL_KEYRING_UNAVAILABLE") from error

    def clear(self, profile: str) -> None:
        profile = _validate_profile(profile)
        try:
            keyring.delete_password(self._service, _account(profile))
        except keyring.errors.PasswordDeleteError:
            return
        except keyring.errors.KeyringError as error:
            raise ModelCredentialError("MODEL_CREDENTIAL_KEYRING_UNAVAILABLE") from error


class MemoryCredentialStore:
    """Non-persistent test substitute for the model credential port."""

    def __init__(self, credentials: Mapping[str, str] | None = None) -> None:
        self._credentials = dict(credentials or {})

    def __repr__(self) -> str:
        return f"{type(self).__name__}(profiles={tuple(sorted(self._credentials))!r})"

    def __str__(self) -> str:
        return repr(self)

    def source(self, profile: str) -> CredentialSource:
        profile = _validate_profile(profile)
        return "keyring" if _usable(self._credentials.get(profile)) is not None else "absent"

    def resolve(self, profile: str) -> str:
        profile = _validate_profile(profile)
        credential = _usable(self._credentials.get(profile))
        if credential is None:
            raise ModelCredentialError("MODEL_CREDENTIAL_MISSING")
        return credential

    def set(self, profile: str, credential: str) -> None:
        profile = _validate_profile(profile)
        if _usable(credential) is None:
            raise ModelCredentialError("MODEL_CREDENTIAL_INVALID")
        self._credentials[profile] = credential

    def clear(self, profile: str) -> None:
        profile = _validate_profile(profile)
        self._credentials.pop(profile, None)
