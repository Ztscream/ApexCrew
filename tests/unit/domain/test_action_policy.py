from apexcrew.domain.actions import delete_action, write_action
from apexcrew.domain.policy import ActionPolicy


def test_default_action_policy_requires_approval_or_hard_denies() -> None:
    policy = ActionPolicy.default()
    assert policy.classify(delete_action("src/old.py")) == "REQUIRE_APPROVAL"
    assert policy.classify(write_action(".git/config")) == "DENY"
