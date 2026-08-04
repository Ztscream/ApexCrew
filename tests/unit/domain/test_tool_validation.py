from __future__ import annotations

import pytest
from pydantic import ValidationError

from apexcrew.domain.actions import (
    ACTION_ADAPTER,
    ActionEnvelope,
    PatchAction,
    ReadAction,
    RiskyAction,
    SearchAction,
)
from apexcrew.domain.commands import ApplicableRevisionDigests
from apexcrew.domain.policy import ActionPolicy, SecretPathPolicy
from apexcrew.domain.tools import ToolIntent
from apexcrew.domain.types import AttemptId, IntentId, RunId, TaskId

SHA = "sha256:" + "1" * 64


def make_tool_intent(action: ReadAction | RiskyAction) -> ToolIntent:
    return ToolIntent.for_authorized_worker_action(
        intent_id=IntentId("intent-1"),
        run_id=RunId("run-1"),
        task_id=TaskId("task-1"),
        attempt_id=AttemptId("attempt-1"),
        action_id="action-1",
        action=action,
        authorization_binding_digest=SHA,
        applicable_revision_digests=ApplicableRevisionDigests(),
        repository_id="repository-1",
        snapshot_digest=SHA,
        scope_digest=SHA,
        dependency_fingerprint_basis=SHA,
        idempotency_key="tool:run-1:action-1",
        expected_prestate_json="{}",
    )


def test_tool_action_adapter_rejects_unknown_kinds_and_fields() -> None:
    with pytest.raises(ValidationError):
        ACTION_ADAPTER.validate_python({"kind": "raw_shell", "command": "type .env"})
    with pytest.raises(ValidationError):
        ACTION_ADAPTER.validate_python({"kind": "read", "path": "src/a.py", "batch": []})


@pytest.mark.parametrize(
    ("document", "error"),
    [
        ({"kind": "risky_action", "operation": "rename", "path": "src/a.py"}, "destination"),
        (
            {
                "kind": "risky_action",
                "operation": "delete",
                "path": "src/a.py",
                "destination": "src/b.py",
            },
            "destination",
        ),
        (
            {"kind": "risky_action", "operation": "protected_patch", "path": "ci.yml"},
            "unified_diff",
        ),
        (
            {"kind": "risky_action", "operation": "set_executable", "path": "tool.py"},
            "executable",
        ),
    ],
)
def test_risky_action_requires_only_its_operation_fields(
    document: dict[str, object], error: str
) -> None:
    with pytest.raises(ValidationError, match=error):
        ACTION_ADAPTER.validate_python(document)


def test_tool_intent_requires_complete_worker_owner() -> None:
    document = make_tool_intent(ReadAction(path="src/a.py")).model_dump(mode="json")
    document["attempt_id"] = None
    with pytest.raises(ValidationError, match="WORKER_TOOL_OWNER_INCOMPLETE"):
        ToolIntent.model_validate(document)


def test_tool_intent_round_trips_through_the_generic_effect_boundary() -> None:
    intent = make_tool_intent(ReadAction(path="src/a.py"))
    effect = intent.to_effect_intent(recorded_sequence=1)
    assert ToolIntent.from_effect_intent(effect) == intent


def test_default_action_policy_composes_the_real_secret_path_policy() -> None:
    secret_paths = SecretPathPolicy.from_host_rules(("private/**",), installation_key=b"k" * 32)
    policy = ActionPolicy.default(secret_paths)
    assert policy.classify(ReadAction(path="private/key.txt")) == "DENY"
    assert policy.classify(ReadAction(path="src/a.py")) == "ALLOW"


@pytest.mark.parametrize(
    "action",
    [
        ReadAction(path="Private/key.txt"),
        SearchAction(query="x", paths=("PRIVATE/**",)),
        PatchAction(path="private/key.txt", unified_diff="@@ -1 +1 @@"),
        RiskyAction(operation="delete", path="private/key.txt"),
        RiskyAction(operation="rename", path="src/a.py", destination="Private/key.txt"),
        ActionEnvelope(kind="delete", path="private/key.txt"),
    ],
)
def test_every_path_bearing_action_consults_the_real_secret_policy(
    action: ActionEnvelope,
) -> None:
    secret_paths = SecretPathPolicy.from_host_rules(("private/**",), installation_key=b"k" * 32)
    assert ActionPolicy.default(secret_paths).classify(action) == "DENY"


def test_unresolvable_path_fails_closed() -> None:
    secret_paths = SecretPathPolicy.from_host_rules((), installation_key=b"k" * 32)
    assert ActionPolicy.default(secret_paths).classify(ReadAction(path="../outside")) == "DENY"


def test_default_action_policy_without_secret_policy_fails_closed_for_paths() -> None:
    assert ActionPolicy.default().classify(ReadAction(path="src/a.py")) == "DENY"


@pytest.mark.parametrize(
    "kind",
    ["raw_shell", "host_access", "network", "socket", "push", "reset", "clean", "force"],
)
def test_hard_denied_kinds_remain_denied_even_for_secret_paths(kind: str) -> None:
    secret_paths = SecretPathPolicy.from_host_rules(("private/**",), installation_key=b"k" * 32)
    action = ActionEnvelope.model_validate({"kind": kind, "path": "private/key.txt"})
    assert ActionPolicy.default(secret_paths).classify(action) == "DENY"
