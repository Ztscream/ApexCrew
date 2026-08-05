from __future__ import annotations

import hashlib
import hmac
import json
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from hashlib import sha256
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from apexcrew.domain.coordination import PlanningTurnBinding

from apexcrew.domain.actions import ActionEnvelope
from apexcrew.domain.plan import (
    CanonicalPath,
    GlobPattern,
    GlobProof,
    GlobValidationError,
    PathValidationError,
    prove_included,
)
from apexcrew.domain.revisions import PlanningReadAuthorizationDocument

DEFAULT_SECRET_GLOBS = (
    "**/.env",
    "**/.env.*",
    "**/.netrc",
    "**/.npmrc",
    "**/.pypirc",
    "**/.ssh/**",
    "**/.gnupg/**",
    "**/.aws/**",
    "**/.azure/**",
    "**/.config/gcloud/**",
    "**/.kube/**",
    "**/.docker/config.json",
    "**/*.pem",
    "**/*.key",
    "**/*.p12",
    "**/*.pfx",
    "**/id_rsa",
    "**/id_dsa",
    "**/id_ecdsa",
    "**/id_ed25519",
    "**/*.tfstate",
    "**/*.tfstate.*",
    "**/credentials.json",
    "**/service-account*.json",
)


@dataclass(frozen=True, slots=True)
class SecretPathPolicyDigest:
    defaults_version: Literal["secret-path-defaults-v1"]
    matcher_version: Literal["apexcrew-path-v1"]
    rules_hmac: str
    user_rule_count: int


def secret_path_policy_digest(
    key: bytes, normalized_rules: Sequence[str]
) -> SecretPathPolicyDigest:
    payload = json.dumps(
        {
            "defaults_version": "secret-path-defaults-v1",
            "matcher_version": "apexcrew-path-v1",
            "rules": list(normalized_rules),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return SecretPathPolicyDigest(
        "secret-path-defaults-v1",
        "apexcrew-path-v1",
        "sha256:" + hmac.new(key, payload, hashlib.sha256).hexdigest(),
        len(normalized_rules),
    )


@dataclass(frozen=True, slots=True)
class SecretInspection:
    code: Literal["ALLOW", "SECRET_PATH_DENIED"]
    safe_detail: str


@dataclass(frozen=True, slots=True)
class SecretPathPolicy:
    _rules: tuple[GlobPattern, ...]
    _folded_rules: tuple[GlobPattern, ...]
    digest: SecretPathPolicyDigest

    @classmethod
    def from_host_rules(cls, rules: Sequence[str], installation_key: bytes) -> SecretPathPolicy:
        if not installation_key:
            raise ValueError("INSTALLATION_KEY_REQUIRED")
        normalized = tuple(sorted({unicodedata.normalize("NFC", rule) for rule in rules}))
        effective = DEFAULT_SECRET_GLOBS + normalized
        return cls(
            tuple(GlobPattern.parse(rule) for rule in effective),
            tuple(GlobPattern.parse(rule.casefold()) for rule in effective),
            secret_path_policy_digest(installation_key, normalized),
        )

    def inspect(self, path: CanonicalPath) -> SecretInspection:
        folded = CanonicalPath.parse(unicodedata.normalize("NFC", path).casefold())
        denied = False
        for rule in self._rules:
            denied |= rule.matches(path)
        for rule in self._folded_rules:
            denied |= rule.matches(folded)
        return SecretInspection(
            "SECRET_PATH_DENIED" if denied else "ALLOW",
            "effective secret path" if denied else "allowed path",
        )

    def inspect_selector(self, selector: GlobPattern) -> SecretInspection:
        folded = GlobPattern.parse(unicodedata.normalize("NFC", selector.value).casefold())
        denied = False
        for rule in self._rules:
            denied |= prove_included(selector, rule) is GlobProof.PROVEN
        for rule in self._folded_rules:
            denied |= prove_included(folded, rule) is GlobProof.PROVEN
        return SecretInspection(
            "SECRET_PATH_DENIED" if denied else "ALLOW",
            "effective secret path" if denied else "allowed path",
        )


@dataclass(frozen=True, slots=True)
class PlanningPathPolicy:
    authorization: PlanningReadAuthorizationDocument
    secret_paths: SecretPathPolicy

    @property
    def scope_digest(self) -> str:
        payload = json.dumps(
            self.authorization.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return "sha256:" + sha256(payload).hexdigest()

    def require_allowed(
        self, path: CanonicalPath, authorization: PlanningReadAuthorizationDocument
    ) -> None:
        if authorization != self.authorization:
            raise ValueError("PLANNING_READ_AUTHORIZATION_MISMATCH")
        if (
            str(path) == ".git"
            or str(path).startswith(".git/")
            or str(path) == ".apexcrew"
            or str(path).startswith(".apexcrew/")
            or not any(
                GlobPattern.parse(pattern).matches(path) for pattern in authorization.positive_globs
            )
            or self.secret_paths.inspect(path).code != "ALLOW"
        ):
            raise ValueError("PLANNING_READ_DENIED")

    def require_manifest_allowed(self, path: CanonicalPath, binding: PlanningTurnBinding) -> None:
        if binding.scope_digest != self.scope_digest:
            raise ValueError("PLANNING_SCOPE_BINDING_MISMATCH")
        self.require_allowed(path, self.authorization)


ActionDecision = Literal["ALLOW", "REQUIRE_APPROVAL", "DENY"]


@dataclass(frozen=True, slots=True)
class ActionPolicy:
    secret_paths: SecretPathPolicy | None = None

    @classmethod
    def default(cls, secret_paths: SecretPathPolicy | None = None) -> ActionPolicy:
        return cls(secret_paths)

    @staticmethod
    def _path_selectors(action: ActionEnvelope) -> tuple[str, ...]:
        selectors: list[str] = []
        if action.path is not None:
            selectors.append(action.path)
        paths = getattr(action, "paths", ())
        if isinstance(paths, tuple) and all(isinstance(path, str) for path in paths):
            selectors.extend(paths)
        destination = getattr(action, "destination", None)
        if isinstance(destination, str):
            selectors.append(destination)
        return tuple(selectors)

    def _classify_paths(self, action: ActionEnvelope) -> ActionDecision | None:
        selectors = self._path_selectors(action)
        if not selectors:
            return None
        if self.secret_paths is None:
            return "DENY"
        for selector in selectors:
            try:
                path = CanonicalPath.parse(selector)
            except PathValidationError:
                try:
                    pattern = GlobPattern.parse(selector)
                except GlobValidationError:
                    return "DENY"
                if self.secret_paths.inspect_selector(pattern).code != "ALLOW":
                    return "DENY"
                continue
            if self.secret_paths.inspect(path).code != "ALLOW":
                return "DENY"
            if str(path) == ".gitlab-ci.yml" or str(path).startswith(".github/workflows/"):
                return "REQUIRE_APPROVAL"
        return "ALLOW"

    def classify(self, action: ActionEnvelope) -> ActionDecision:
        if action.kind in {
            "raw_shell",
            "host_access",
            "network",
            "socket",
            "push",
            "reset",
            "clean",
            "force",
        }:
            return "DENY"
        if action.kind == "target_cas":
            return "REQUIRE_APPROVAL" if action.issued_by_admission else "DENY"
        path_decision = self._classify_paths(action)
        if path_decision in {"DENY", "REQUIRE_APPROVAL"}:
            return path_decision
        if action.kind in {"delete", "rename", "chmod_executable"}:
            return "REQUIRE_APPROVAL"
        if action.kind == "risky_action":
            return "REQUIRE_APPROVAL"
        if action.kind in {"read", "search", "patch", "check", "finish", "fail"}:
            return "ALLOW"
        return "DENY"
