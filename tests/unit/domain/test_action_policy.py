from apexcrew.domain.actions import delete_action, write_action
from apexcrew.domain.policy import ActionPolicy, SecretPathPolicy


def test_default_action_policy_requires_approval_or_hard_denies() -> None:
    secret_paths = SecretPathPolicy.from_host_rules((), installation_key=b"k" * 32)
    policy = ActionPolicy.default(secret_paths)
    assert policy.classify(delete_action("src/old.py")) == "REQUIRE_APPROVAL"
    assert policy.classify(write_action(".git/config")) == "DENY"
