from __future__ import annotations

import base64
import json

import keyring


class SecretPolicyConfigurationError(RuntimeError):
    pass


class KeyringSecretPolicyStore:
    def __init__(self, service: str = "apexcrew", account: str = "secret-path-policy-v1") -> None:
        self._service = service
        self._account = account

    def load(self) -> tuple[bytes, tuple[str, ...]]:
        encoded = keyring.get_password(self._service, self._account)
        if encoded is None:
            raise SecretPolicyConfigurationError("SECRET_POLICY_CONFIGURATION_MISSING")
        try:
            document = json.loads(encoded)
            key = base64.b64decode(document["installation_key_b64"], validate=True)
            rules = tuple(document["positive_globs"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise SecretPolicyConfigurationError("SECRET_POLICY_CONFIGURATION_INVALID") from error
        if len(key) != 32 or any(not isinstance(rule, str) for rule in rules):
            raise SecretPolicyConfigurationError("SECRET_POLICY_CONFIGURATION_INVALID")
        return key, rules
