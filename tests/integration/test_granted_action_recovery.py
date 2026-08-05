from __future__ import annotations

import os
import stat
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Literal

import pytest

from apexcrew.adapters.repository.granted_workspace import GrantedWorkspaceAdapter
from apexcrew.application.runtime import GrantedActionRuntime
from apexcrew.domain.actions import RiskyAction
from apexcrew.domain.admission import RepositoryEffectUncertain
from apexcrew.domain.authority import (
    FrozenActionBindings,
    GrantedActionIntent,
    canonical_action_json,
)
from apexcrew.domain.commands import ApplicableRevisionDigests, RuntimePermit
from apexcrew.domain.effects import StateConflict
from apexcrew.domain.policy import SecretPathPolicy
from apexcrew.domain.revisions import Sha256DigestText
from apexcrew.domain.tools import ActionPreState, GrantedActionObservation, ToolResult
from apexcrew.domain.types import AuditSequence, IntentId, RunId
from apexcrew.domain.worker import normalized_action_digest

SHA = "sha256:" + "1" * 64


def digest(content: bytes) -> Sha256DigestText:
    return Sha256DigestText("sha256:" + sha256(content).hexdigest())


def granted_intent(
    action: RiskyAction,
    expected: ActionPreState,
    *,
    state: Literal["INTENT_RECORDED", "DISPATCHED", "SETTLED", "INDETERMINATE"] = "INTENT_RECORDED",
) -> GrantedActionIntent:
    return GrantedActionIntent(
        intent_id="intent-granted-recovery",
        pending_id="pending-granted-recovery",
        grant_id="grant-granted-recovery",
        action=action,
        normalized_action_json=canonical_action_json(action),
        action_digest=normalized_action_digest(action),
        expected_pre_state=expected,
        bindings=FrozenActionBindings(
            run_id="run-granted-recovery",
            task_id="task-granted-recovery",
            attempt_id="attempt-granted-recovery",
            logical_turn_id="turn-granted-recovery",
            action_id="action-granted-recovery",
            lease_id="lease-granted-recovery",
            lease_generation=1,
            run_head_oid="1" * 40,
            target_safety_digest=SHA,
            plan_digest=SHA,
            policy_digest=SHA,
            budget_digest=SHA,
            model_configuration_digest=SHA,
            tool_schema_digest=SHA,
            authorization_binding_digest=SHA,
            deadline_at_utc=datetime(2026, 8, 3, tzinfo=UTC),
        ),
        state=state,
    )


def consumed_permit(run_id: RunId) -> RuntimePermit:
    return RuntimePermit(
        run_id=run_id,
        generation=1,
        source_request_id="continue-granted-recovery",
        source_envelope_digest=SHA,
        issued_sequence=1,
        allowed_phase="ACTIVE",
        applicable_revision_digests=ApplicableRevisionDigests(
            plan_digest=SHA,
            policy_digest=SHA,
            budget_digest=SHA,
            model_configuration_digest=SHA,
        ),
        target_authority_digest=SHA,
        expected_runtime_progress_generation=0,
        state="CONSUMED",
        consumed_owner_id="runtime-owner-granted-recovery",
        consumed_sequence=2,
    )


@dataclass
class RecordingJournal:
    intent: GrantedActionIntent
    sequence: AuditSequence = field(default_factory=lambda: AuditSequence(10))
    dispatch_count: int = 0
    settlement_count: int = 0
    indeterminate_count: int = 0

    def next_unsettled_granted_action(self, run_id: RunId) -> GrantedActionIntent | None:
        return self.intent if self.intent.bindings.run_id == run_id else None

    def require_unsettled_granted_intent(self, intent_id: IntentId) -> GrantedActionIntent:
        if self.intent.intent_id != intent_id:
            raise StateConflict("GRANTED_ACTION_INTENT_NOT_FOUND")
        return self.intent

    def mark_granted_action_dispatched(
        self,
        *,
        run_id: RunId,
        intent_id: IntentId,
        applicable_revision_digests: ApplicableRevisionDigests,
        expected_sequence: AuditSequence,
    ) -> GrantedActionIntent:
        assert run_id == self.intent.bindings.run_id
        assert intent_id == self.intent.intent_id
        assert applicable_revision_digests == self.intent.bindings.applicable_revision_digests
        assert expected_sequence == self.sequence
        self.dispatch_count += 1
        self.sequence = AuditSequence(self.sequence + 1)
        self.intent = replace(self.intent, state="DISPATCHED")
        return self.intent

    def settle_granted_action(
        self,
        *,
        run_id: RunId,
        intent_id: IntentId,
        result: ToolResult,
        applicable_revision_digests: ApplicableRevisionDigests,
        expected_sequence: AuditSequence,
    ) -> AuditSequence:
        assert run_id == self.intent.bindings.run_id
        assert intent_id == self.intent.intent_id
        assert result.run_id == run_id
        assert result.intent_id == intent_id
        assert applicable_revision_digests == self.intent.bindings.applicable_revision_digests
        assert expected_sequence == self.sequence
        self.settlement_count += 1
        self.sequence = AuditSequence(self.sequence + 1)
        self.intent = replace(self.intent, state="SETTLED")
        return self.sequence

    def mark_granted_action_indeterminate(
        self,
        *,
        run_id: RunId,
        intent_id: IntentId,
        observation_digest: Sha256DigestText,
        applicable_revision_digests: ApplicableRevisionDigests,
        expected_sequence: AuditSequence,
    ) -> AuditSequence:
        assert run_id == self.intent.bindings.run_id
        assert intent_id == self.intent.intent_id
        assert observation_digest == SHA
        assert applicable_revision_digests == self.intent.bindings.applicable_revision_digests
        assert expected_sequence == self.sequence
        self.indeterminate_count += 1
        self.sequence = AuditSequence(self.sequence + 1)
        self.intent = replace(self.intent, state="INDETERMINATE")
        return self.sequence

    def audit_sequence(self, run_id: RunId) -> AuditSequence:
        assert run_id == self.intent.bindings.run_id
        return self.sequence


@dataclass
class RecordingTools:
    observation: GrantedActionObservation
    execute_count: int = 0

    def observe_granted_action(self, intent: GrantedActionIntent) -> GrantedActionObservation:
        del intent
        return self.observation

    def execute_granted(self, intent: GrantedActionIntent) -> ToolResult:
        self.execute_count += 1
        return ToolResult(
            code="RENAMED",
            run_id=intent.bindings.run_id,
            intent_id=intent.intent_id,
        )


def test_exact_post_recovery_settles_without_replaying_the_handler() -> None:
    action = RiskyAction(operation="rename", path="src/old.py", destination="src/new.py")
    intent = granted_intent(action, ActionPreState(source_digest=SHA, destination_absent=True))
    journal = RecordingJournal(intent)
    tools = RecordingTools(
        GrantedActionObservation(
            state="EXACT_POST", digest=SHA, post_result=ToolResult(code="RENAMED")
        )
    )

    decision = GrantedActionRuntime(journal, tools).execute(
        intent.bindings.run_id,
        consumed_permit(intent.bindings.run_id),
        intent.intent_id,
    )

    assert decision.code == "ACTION_RECORDED"
    assert decision.stop_reason is None
    assert decision.resulting_sequence == journal.sequence
    assert tools.execute_count == 0
    assert journal.dispatch_count == 0
    assert journal.settlement_count == 1


@pytest.mark.parametrize("state", ("INTENT_RECORDED", "DISPATCHED"))
def test_exact_pre_recovery_reuses_the_one_recorded_intent(
    state: Literal["INTENT_RECORDED", "DISPATCHED"],
) -> None:
    action = RiskyAction(operation="rename", path="src/old.py", destination="src/new.py")
    intent = granted_intent(
        action,
        ActionPreState(source_digest=SHA, destination_absent=True),
        state=state,
    )
    journal = RecordingJournal(intent)
    tools = RecordingTools(GrantedActionObservation(state="EXACT_PRE", digest=SHA))

    decision = GrantedActionRuntime(journal, tools).execute(
        intent.bindings.run_id,
        consumed_permit(intent.bindings.run_id),
        intent.intent_id,
    )

    assert decision.code == "ACTION_RECORDED"
    assert decision.stop_reason is None
    assert tools.execute_count == 1
    assert journal.dispatch_count == (state == "INTENT_RECORDED")
    assert journal.settlement_count == 1


def test_third_state_recovery_records_indeterminate_without_dispatch() -> None:
    action = RiskyAction(operation="delete", path="src/old.py")
    intent = granted_intent(action, ActionPreState(source_digest=SHA))
    journal = RecordingJournal(intent)
    tools = RecordingTools(GrantedActionObservation(state="THIRD", digest=SHA))

    decision = GrantedActionRuntime(journal, tools).execute(
        intent.bindings.run_id,
        consumed_permit(intent.bindings.run_id),
        intent.intent_id,
    )

    assert decision.code == "STOP"
    assert decision.stop_reason == "INDETERMINATE"
    assert tools.execute_count == 0
    assert journal.indeterminate_count == 1


@pytest.mark.parametrize("mutation", ("wrong_run", "wrong_revisions"))
def test_granted_runtime_rejects_a_nonmatching_consumed_permit(mutation: str) -> None:
    action = RiskyAction(operation="delete", path="src/old.py")
    intent = granted_intent(action, ActionPreState(source_digest=SHA))
    journal = RecordingJournal(intent)
    tools = RecordingTools(GrantedActionObservation(state="EXACT_PRE", digest=SHA))
    permit = consumed_permit(intent.bindings.run_id)
    if mutation == "wrong_run":
        permit = permit.model_copy(update={"run_id": "run-other"})
    else:
        permit = permit.model_copy(
            update={"applicable_revision_digests": ApplicableRevisionDigests()}
        )

    with pytest.raises(ValueError, match="GRANTED_ACTION_PERMIT_BINDING_MISMATCH"):
        GrantedActionRuntime(journal, tools).execute(
            intent.bindings.run_id, permit, intent.intent_id
        )

    assert journal.dispatch_count == 0
    assert journal.settlement_count == 0
    assert tools.execute_count == 0


def test_granted_workspace_observes_exact_rename_post_state(tmp_path: Path) -> None:
    source = tmp_path / "src" / "old.py"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"old\n")
    adapter = GrantedWorkspaceAdapter(
        tmp_path, SecretPathPolicy.from_host_rules((), b"installation-key")
    )
    action = RiskyAction(operation="rename", path="src/old.py", destination="src/new.py")
    expected = ActionPreState(source_digest=digest(b"old\n"), destination_absent=True)

    assert adapter.observe(action, expected).state == "EXACT_PRE"
    result = adapter.rename_regular_file(action, expected)
    observed = adapter.observe(action, expected)

    assert result.code == "RENAMED"
    assert observed.state == "EXACT_POST"
    assert observed.post_result is not None
    assert observed.post_result.code == "RENAMED"


def test_granted_workspace_observes_exact_delete_post_state(tmp_path: Path) -> None:
    source = tmp_path / "src" / "old.py"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"old\n")
    adapter = GrantedWorkspaceAdapter(
        tmp_path, SecretPathPolicy.from_host_rules((), b"installation-key")
    )
    action = RiskyAction(operation="delete", path="src/old.py")
    expected = ActionPreState(source_digest=digest(b"old\n"))

    result = adapter.delete_regular_file(action, expected)
    observed = adapter.observe(action, expected)

    assert result.code == "DELETED"
    assert observed.state == "EXACT_POST"
    assert observed.post_result is not None
    assert observed.post_result.code == "DELETED"


def test_granted_workspace_observes_exact_executable_post_state(
    tmp_path: Path,
) -> None:
    source = tmp_path / "scripts" / "check.py"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"print('ok')\n")
    original_mode = stat.S_IMODE(source.stat().st_mode)
    desired = not bool(original_mode & 0o111)
    adapter = GrantedWorkspaceAdapter(
        tmp_path, SecretPathPolicy.from_host_rules((), b"installation-key")
    )
    action = RiskyAction(operation="set_executable", path="scripts/check.py", executable=desired)
    expected = ActionPreState(source_digest=digest(b"print('ok')\n"), source_mode=original_mode)

    result = adapter.set_executable(action, expected)

    if os.name == "nt":
        assert result.code == "INDETERMINATE"
        assert source.read_bytes() == b"print('ok')\n"
        return
    observed = adapter.observe(action, expected)
    assert result.code == "EXECUTABLE_CHANGED"
    assert observed.state == "EXACT_POST"
    assert source.read_bytes() == b"print('ok')\n"


def test_granted_workspace_proves_protected_patch_post_state(tmp_path: Path) -> None:
    workflow = tmp_path / ".github" / "workflows" / "ci.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_bytes(b"old\n")
    adapter = GrantedWorkspaceAdapter(
        tmp_path, SecretPathPolicy.from_host_rules((), b"installation-key")
    )
    action = RiskyAction(
        operation="protected_patch",
        path=".github/workflows/ci.yml",
        unified_diff="@@ -1 +1 @@\n-old\n+new\n",
    )
    expected = ActionPreState(source_digest=digest(b"old\n"))

    assert adapter.observe(action, expected).state == "EXACT_PRE"
    result = adapter.apply_protected_patch(action, expected)
    observed = adapter.observe(action, expected)

    assert result.code == "PROTECTED_PATCH_APPLIED"
    assert observed.state == "EXACT_POST"
    assert observed.post_result is not None
    assert observed.post_result.content_digest == digest(b"new\n")


def test_granted_workspace_safe_preflight_denial_is_bounded(tmp_path: Path) -> None:
    secret = tmp_path / ".env"
    secret.write_bytes(b"TOKEN=secret\n")
    adapter = GrantedWorkspaceAdapter(
        tmp_path, SecretPathPolicy.from_host_rules((), b"installation-key")
    )
    action = RiskyAction(operation="delete", path=".env")

    result = adapter.delete_regular_file(
        action, ActionPreState(source_digest=digest(secret.read_bytes()))
    )

    assert result.code == "INDETERMINATE"
    assert secret.exists()


def test_granted_workspace_no_follow_denial_is_bounded(tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"outside\n")
    source = tmp_path / "src" / "old.py"
    source.parent.mkdir(parents=True)
    try:
        source.symlink_to(outside)
    except OSError as error:
        pytest.skip(f"symlink creation unavailable: {error}")
    adapter = GrantedWorkspaceAdapter(
        tmp_path, SecretPathPolicy.from_host_rules((), b"installation-key")
    )

    result = adapter.delete_regular_file(
        RiskyAction(operation="delete", path="src/old.py"),
        ActionPreState(source_digest=digest(outside.read_bytes())),
    )

    assert result.code == "INDETERMINATE"
    assert outside.read_bytes() == b"outside\n"


def test_granted_delete_rejects_ancestor_replacement_without_side_effect(
    tmp_path: Path,
) -> None:
    if os.name != "posix":
        pytest.skip("ancestor replacement injection uses POSIX directory handles")
    source_root = tmp_path / "src"
    source_root.mkdir()
    original = source_root / "old.py"
    original.write_bytes(b"original\n")
    replacement = tmp_path / "replacement.py"

    def replace_ancestor() -> None:
        source_root.rename(tmp_path / "src-old")
        source_root.mkdir()
        (source_root / "old.py").write_bytes(b"replacement\n")
        replacement.write_bytes(b"sentinel\n")

    adapter = GrantedWorkspaceAdapter(
        tmp_path,
        SecretPathPolicy.from_host_rules((), b"installation-key"),
        before_mutation=replace_ancestor,
    )
    result = adapter.delete_regular_file(
        RiskyAction(operation="delete", path="src/old.py"),
        ActionPreState(source_digest=digest(b"original\n")),
    )

    assert result.code == "INDETERMINATE"
    assert (tmp_path / "src-old" / "old.py").read_bytes() == b"original\n"
    assert (source_root / "old.py").read_bytes() == b"replacement\n"
    assert replacement.read_bytes() == b"sentinel\n"


def test_granted_workspace_possibly_applied_failure_is_uncertain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "src" / "old.py"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"old\n")
    adapter = GrantedWorkspaceAdapter(
        tmp_path, SecretPathPolicy.from_host_rules((), b"installation-key")
    )
    action = RiskyAction(operation="delete", path="src/old.py")
    expected = ActionPreState(source_digest=digest(b"old\n"))

    def fail_unlink(*_args: object, **_kwargs: object) -> None:
        raise OSError("injected unlink uncertainty")

    if os.name == "posix":
        monkeypatch.setattr(os, "unlink", fail_unlink)
    else:
        monkeypatch.setattr(Path, "unlink", fail_unlink)

    with pytest.raises(RepositoryEffectUncertain, match="GRANTED_DELETE_UNCERTAIN"):
        adapter.delete_regular_file(action, expected)
