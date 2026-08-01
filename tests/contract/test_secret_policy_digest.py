from apexcrew.domain.policy import SecretPathPolicy


def test_digest_changes_when_host_rule_changes() -> None:
    assert (
        SecretPathPolicy.from_host_rules(("a/**",), b"k" * 32).digest
        != SecretPathPolicy.from_host_rules(("b/**",), b"k" * 32).digest
    )
