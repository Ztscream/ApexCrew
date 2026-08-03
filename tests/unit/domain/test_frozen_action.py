from __future__ import annotations

import json
from datetime import UTC, datetime

from apexcrew.domain.actions import RiskyAction
from apexcrew.domain.authority import (
    AuthorizationDecision,
    AuthorizationRequest,
    freeze_pending_action,
)
from apexcrew.domain.effects import sha256_digest
from apexcrew.domain.tools import ActionPreState
from apexcrew.domain.worker import normalized_action_digest

SHA = "sha256:" + "1" * 64


def instant(value: str) -> datetime:
    return datetime.fromisoformat(value)


def authorization_request(action: RiskyAction) -> AuthorizationRequest:
    started = datetime(2026, 7, 27, 9, 53, tzinfo=UTC)
    return AuthorizationRequest(
        run_id="run-1",
        task_id="task-1",
        attempt_id="attempt-1",
        logical_turn_id="turn-1",
        action_id="action-1",
        action=action,
        authority_origin="WORKER",
        action_digest=normalized_action_digest(action),
        expected_prestate_digest=sha256_digest(ActionPreState().canonical_json()),
        lease_id="lease-1",
        lease_generation=1,
        admissible_head="1" * 40,
        task_contract_digest=SHA,
        plan_digest=SHA,
        policy_digest=SHA,
        budget_digest=SHA,
        model_configuration_digest=SHA,
        tool_schema_digest=SHA,
        target_safety_digest=SHA,
        started_at_utc=started,
        deadline_at_utc=datetime(2026, 7, 27, 9, 55, tzinfo=UTC),
        expected_sequence=0,
    )


def approval_required_decision(request: AuthorizationRequest) -> AuthorizationDecision:
    return AuthorizationDecision(
        decision="REQUIRE_APPROVAL",
        reason="APPROVAL_REQUIRED",
        run_id=request.run_id,
        task_id=request.task_id,
        attempt_id=request.attempt_id,
        action_id=request.action_id,
        action_digest=request.action_digest,
        binding_digest=SHA,
        action_class="RISKY",
        approved_timeout_seconds=120,
        deadline_at_utc=request.deadline_at_utc,
        persistence="WITH_PENDING_ACTION",
        effect_intent_id=None,
        pending_action_id=None,
        resulting_sequence=None,
    )


def test_pending_action_persists_the_full_normalized_typed_action() -> None:
    action = RiskyAction(operation="rename", path="src/old.py", destination="src/new.py")
    request = authorization_request(action)
    decision = approval_required_decision(request)
    expected_pre_state = ActionPreState(
        source_digest=SHA,
        destination_absent=True,
    )

    pending = freeze_pending_action(
        pending_id="pending-1",
        request=request,
        decision=decision,
        expected_pre_state=expected_pre_state,
        now=instant("2026-07-27T09:55:00Z"),
        grant_ttl_seconds=600,
    )

    assert pending.action == action
    assert json.loads(pending.normalized_action_json) == action.model_dump(
        mode="json", exclude_none=True
    )
    assert pending.action_digest == sha256_digest(pending.normalized_action_json)
    assert pending.authorization_binding_digest == decision.binding_digest
    assert pending.bindings.action_id == request.action_id
    assert pending.expires_at == instant("2026-07-27T10:05:00Z")
