import pytest

import apexcrew.domain.policy as policy_module
from apexcrew.domain.plan import CanonicalPath
from apexcrew.domain.policy import SecretPathPolicy


def test_empty_host_installation_key_is_rejected_before_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def digest_must_not_run(*_args: object) -> None:
        raise AssertionError("policy digest must not be created")

    monkeypatch.setattr(policy_module, "secret_path_policy_digest", digest_must_not_run)

    with pytest.raises(ValueError, match="INSTALLATION_KEY_REQUIRED"):
        SecretPathPolicy.from_host_rules(("private/**",), installation_key=b"")


def test_secret_policy_digest_canonicalizes_host_rules() -> None:
    canonical = SecretPathPolicy.from_host_rules(
        ("a/**", "z/**"), installation_key=b"host-installation-key"
    ).digest
    reordered_and_duplicated = SecretPathPolicy.from_host_rules(
        ("z/**", "a/**", "z/**"), installation_key=b"host-installation-key"
    ).digest

    assert reordered_and_duplicated == canonical
    assert reordered_and_duplicated.user_rule_count == 2


def test_secret_policy_denies_mixed_case_default_without_returning_path() -> None:
    policy = SecretPathPolicy.from_host_rules(("private/**",), installation_key=b"k" * 32)
    result = policy.inspect(CanonicalPath.parse("Config/.ENV"))
    assert result.code == "SECRET_PATH_DENIED"
    assert result.safe_detail == "effective secret path"
