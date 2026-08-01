from apexcrew.domain.plan import CanonicalPath
from apexcrew.domain.policy import SecretPathPolicy


def test_secret_policy_denies_mixed_case_default_without_returning_path() -> None:
    policy = SecretPathPolicy.from_host_rules(("private/**",), installation_key=b"k" * 32)
    result = policy.inspect(CanonicalPath.parse("Config/.ENV"))
    assert result.code == "SECRET_PATH_DENIED"
    assert result.safe_detail == "effective secret path"
