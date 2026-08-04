import hashlib
import hmac
import json
from dataclasses import asdict

from apexcrew.domain.policy import SecretPathPolicy


def test_digest_changes_when_host_rule_changes() -> None:
    assert (
        SecretPathPolicy.from_host_rules(("a/**",), b"k" * 32).digest
        != SecretPathPolicy.from_host_rules(("b/**",), b"k" * 32).digest
    )


def test_exported_secret_policy_digest_requires_the_host_installation_key() -> None:
    installation_key = b"host-installation-key"
    rules = ("low-entropy-secret/**",)
    exported = asdict(SecretPathPolicy.from_host_rules(rules, installation_key).digest)
    serialized_evidence = json.dumps(exported, sort_keys=True, separators=(",", ":"))
    canonical_payload = json.dumps(
        {
            "defaults_version": "secret-path-defaults-v1",
            "matcher_version": "apexcrew-path-v1",
            "rules": list(rules),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")

    assert exported["user_rule_count"] == 1
    assert exported["rules_hmac"] != "sha256:" + hashlib.sha256(canonical_payload).hexdigest()
    assert (
        exported["rules_hmac"]
        != "sha256:"
        + hmac.new(b"guessed-installation-key", canonical_payload, hashlib.sha256).hexdigest()
    )
    assert installation_key.decode("ascii") not in serialized_evidence
    assert rules[0] not in serialized_evidence


def test_secret_policy_digest_changes_when_host_key_rotates() -> None:
    rules = ("private/**",)

    assert (
        SecretPathPolicy.from_host_rules(rules, b"old-installation-key").digest.rules_hmac
        != SecretPathPolicy.from_host_rules(rules, b"new-installation-key").digest.rules_hmac
    )
