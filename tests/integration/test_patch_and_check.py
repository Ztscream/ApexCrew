from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from apexcrew.adapters.executor.attempt_patch import AttemptPatchExecutionError
from apexcrew.adapters.executor.fake import FakeExecutor, FakeProcessResult
from apexcrew.adapters.repository.snapshot import MemoryRepositorySnapshot
from apexcrew.adapters.state.sqlite import SqliteStateStore
from apexcrew.application.runtime import ToolActionResolutionObserver
from apexcrew.domain.actions import CheckAction, PatchAction
from apexcrew.domain.authority import ActionClass, ActionDeadline, TimeoutDecision, WorkspaceLease
from apexcrew.domain.commands import ApplicableRevisionDigests
from apexcrew.domain.effects import recover_observation, sha256_digest
from apexcrew.domain.plan import CheckDefinition, GlobPattern
from apexcrew.domain.policy import SecretPathPolicy
from apexcrew.domain.tools import (
    DeclaredCheckRegistry,
    PatchExecutionResult,
    SanitizedSnapshot,
    SanitizedSnapshotEntry,
    ScopedToolRuntime,
    ToolIntent,
    ToolResult,
    validate_tool_effect_result,
)
from apexcrew.domain.types import AttemptId, AuditSequence, IntentId, RunId, TaskId

SHA = "sha256:" + "1" * 64
SNAPSHOT_SHA = "sha256:" + "2" * 64


def secret_policy(*rules: str) -> SecretPathPolicy:
    return SecretPathPolicy.from_host_rules(rules, installation_key=b"k" * 32)


def check_intent(intent_id: str = "intent-check", check_id: str = "task-check-1") -> ToolIntent:
    return ToolIntent.for_authorized_worker_action(
        intent_id=IntentId(intent_id),
        run_id=RunId("run-1"),
        task_id=TaskId("task-1"),
        attempt_id=AttemptId("attempt-1"),
        action_id="action-check",
        action=CheckAction(check_id=check_id),
        authorization_binding_digest=SHA,
        applicable_revision_digests=ApplicableRevisionDigests(),
        repository_id="repository-1",
        snapshot_digest=SNAPSHOT_SHA,
        scope_digest=SHA,
        dependency_fingerprint_basis=SHA,
        idempotency_key=f"tool:{intent_id}",
        expected_prestate_json="{}",
    )


def patch_intent() -> ToolIntent:
    return ToolIntent.for_authorized_worker_action(
        intent_id=IntentId("intent-patch"),
        run_id=RunId("run-1"),
        task_id=TaskId("task-1"),
        attempt_id=AttemptId("attempt-1"),
        action_id="action-patch",
        action=PatchAction(path="src/a.py", unified_diff="@@ -1 +1 @@\n-old\n+new\n"),
        authorization_binding_digest=SHA,
        applicable_revision_digests=ApplicableRevisionDigests(),
        repository_id="repository-1",
        snapshot_digest=SHA,
        scope_digest=SHA,
        dependency_fingerprint_basis=SHA,
        idempotency_key="tool:intent-patch",
        expected_prestate_json="{}",
    )


class UncertainPatchExecutor:
    def apply_patch(
        self, lease: WorkspaceLease, patches: Mapping[str, bytes]
    ) -> PatchExecutionResult:
        del lease, patches
        raise AttemptPatchExecutionError("PATCH_RESULT_UNCERTAIN")


def patch_runtime() -> ScopedToolRuntime:
    now = datetime(2026, 8, 2, 8, 0, tzinfo=UTC)
    return ScopedToolRuntime(
        snapshot=MemoryRepositorySnapshot({"src/a.py": b"old\n"}),
        read_globs=("src/**",),
        secret_paths=secret_policy("private/**"),
        authorization_binding_digest=SHA,
        applicable_revision_digests=ApplicableRevisionDigests(),
        repository_id="repository-1",
        snapshot_digest=SHA,
        scope_digest=SHA,
        dependency_fingerprint_basis=SHA,
        patch_executor=UncertainPatchExecutor(),
        workspace_lease=WorkspaceLease(
            lease_id="lease-1",
            run_id=RunId("run-1"),
            task_id=TaskId("task-1"),
            attempt_id=AttemptId("attempt-1"),
            generation=1,
            base_head="a" * 40,
            admissible_head="a" * 40,
            task_contract_digest=SHA,
            write_globs=(GlobPattern.parse("src/**"),),
            sensitivity_globs=(),
            issued_at=now,
            expires_at=now + timedelta(minutes=15),
            state="ACTIVE",
        ),
    )


def sanitized_snapshot(tmp_path: Path) -> SanitizedSnapshot:
    return SanitizedSnapshot.from_regular_files(
        root=tmp_path,
        repository_id="repository-1",
        tree_digest=SNAPSHOT_SHA,
        dependency_fingerprint_digest=SHA,
        entries=(
            SanitizedSnapshotEntry(
                path="src/a.py",
                kind="regular",
                content_digest=sha256_digest("old\n"),
            ),
        ),
        secret_paths=secret_policy("private/**"),
    )


def test_uncertain_patch_returns_settleable_result(tmp_path: Path) -> None:
    del tmp_path
    intent = patch_intent()

    result = patch_runtime().execute(intent)

    assert result.code == "INFRASTRUCTURE_UNCERTAINTY"
    assert result.run_id == intent.run_id
    assert result.intent_id == intent.intent_id
    assert result.passed is None
    assert result.timed_out is True
    assert result.bounded_payload == {
        "reason": "PATCH_RESULT_UNCERTAIN",
        "snapshot_digest": intent.snapshot_digest,
    }
    validate_tool_effect_result(
        intent.to_effect_intent(AuditSequence(1)),
        result.to_effect_result(AuditSequence(2)),
    )


@dataclass
class DeadlineJournal:
    deadline: ActionDeadline
    sequence: AuditSequence

    def action_deadline(self, intent_id: IntentId) -> ActionDeadline | None:
        return self.deadline if intent_id == self.deadline.intent_id else None

    def audit_sequence(self, run_id: RunId) -> AuditSequence:
        assert run_id == self.deadline.run_id
        return self.sequence


@dataclass
class ExpiredDeadlineAuthority:
    settled: TimeoutDecision | None = None

    def deadline_state(self, deadline: ActionDeadline) -> str:
        return "TIMED_OUT"

    def settle_timeout(
        self,
        deadline: ActionDeadline,
        outcome_observable: bool,
        expected_sequence: AuditSequence,
    ) -> TimeoutDecision:
        assert outcome_observable is False
        assert expected_sequence == AuditSequence(2)
        self.settled = TimeoutDecision(
            outcome="INFRASTRUCTURE_UNCERTAINTY",
            semantic_result=None,
            receipt=None,
            retry_scope=("task-check-1", SNAPSHOT_SHA),
            retry_allowed=True,
            full_reservation_charged=False,
        )
        return self.settled


def check_runtime(
    tmp_path: Path,
    response: FakeProcessResult,
    *,
    deadline_authority: ExpiredDeadlineAuthority | None = None,
) -> ScopedToolRuntime:
    intent = check_intent()
    started = datetime(2026, 8, 2, 8, 0, tzinfo=UTC)
    deadline = ActionDeadline(
        run_id=intent.run_id,
        intent_id=intent.intent_id,
        budget_digest=SHA,
        applicable_revision_digests=intent.applicable_revision_digests,
        action_class=ActionClass.DECLARED_CHECK,
        started_at=started,
        expires_at=started + timedelta(seconds=600),
        recorded_sequence=AuditSequence(2),
        check_id="task-check-1",
        snapshot_digest=SNAPSHOT_SHA,
    )
    snapshot = sanitized_snapshot(tmp_path)
    executor = FakeExecutor(tmp_path, secret_paths=secret_policy("private/**"))
    executor.add_response(("pytest", "-q"), SNAPSHOT_SHA, response)
    return ScopedToolRuntime(
        snapshot=snapshot,
        read_globs=("src/**",),
        secret_paths=secret_policy("private/**"),
        authorization_binding_digest=SHA,
        applicable_revision_digests=ApplicableRevisionDigests(),
        repository_id="repository-1",
        snapshot_digest=SNAPSHOT_SHA,
        scope_digest=SHA,
        dependency_fingerprint_basis=SHA,
        executor=executor,
        declared_checks=DeclaredCheckRegistry(
            {
                "task-check-1": CheckDefinition(
                    argv=("pytest", "-q"),
                    input_globs=(GlobPattern.parse("src/**"),),
                )
            }
        ),
        sanitized_snapshot=snapshot,
        deadline_journal=DeadlineJournal(deadline, AuditSequence(2)),
        deadline_authority=deadline_authority or ExpiredDeadlineAuthority(),
    )


def test_timed_out_check_returns_uncertainty_without_receipt(tmp_path: Path) -> None:
    runtime = check_runtime(
        tmp_path,
        FakeProcessResult(exit_code=None, timed_out=True, timing_ms=600_000),
    )

    result = runtime.execute(check_intent())

    assert result.code == "INFRASTRUCTURE_UNCERTAINTY"
    assert result.passed is None
    assert result.timed_out is True
    assert result.bounded_payload["retry_scope"] == ("task-check-1", SNAPSHOT_SHA)
    assert "receipt" not in result.model_dump(mode="json")


class RecoveryJournal:
    def audit_sequence(self, run_id: RunId) -> AuditSequence:
        assert run_id == RunId("run-1")
        return AuditSequence(2)


def test_unknown_check_denial_is_an_exact_recovery_receipt(tmp_path: Path) -> None:
    runtime = check_runtime(
        tmp_path,
        FakeProcessResult(exit_code=0, timed_out=False, timing_ms=1),
    )
    intent = check_intent("intent-unknown", "task-check-unknown")

    state, result = runtime.observe_recovery(intent)

    assert state == "EXACT_RECEIPT"
    assert result is not None
    assert result.code == "SCOPE_DENIED"
    assert result.bounded_payload["check_id"] == "task-check-unknown"
    assert result.bounded_payload["argv_digest"] == "sha256:" + "0" * 64
    assert result.bounded_payload["snapshot_digest"] == intent.snapshot_digest
    assert result.content_digest == result.bounded_payload["receipt_digest"]

    observation = ToolActionResolutionObserver(RecoveryJournal(), runtime).observe(
        intent.to_effect_intent(AuditSequence(1)),
        recovery_generation=1,
    )
    decision = recover_observation(observation)
    assert decision.kind.value == "COMPLETED"
    assert decision.effect_result is not None
    assert decision.effect_result.outcome == "FAILED"


def test_failed_check_error_output_does_not_disclose_secret_path(tmp_path: Path) -> None:
    private_path = "private/probe.key"
    runtime = check_runtime(
        tmp_path,
        FakeProcessResult(
            exit_code=1,
            timed_out=False,
            stdout_chunks=(b"expected 3.00, received 2.99\n",),
            stderr_chunks=(f"diff failed at {private_path}\n".encode(),),
            timing_ms=20,
        ),
    )

    result = runtime.execute(check_intent())
    encoded = result.model_dump_json()

    assert result.code == "CHECK_FAILED"
    assert result.passed is False
    assert "expected 3.00" in str(result.bounded_payload["output"])
    assert private_path not in encoded
    assert "effective secret path" not in encoded

    database = tmp_path / "failed-check.db"
    store = SqliteStateStore(database)
    intent = check_intent()
    store.record_intent(intent.to_effect_intent(AuditSequence(1)), AuditSequence(0))
    store.settle_intent(
        intent.run_id,
        intent.intent_id,
        result.to_effect_result(AuditSequence(2)),
        intent.applicable_revision_digests,
        AuditSequence(1),
    )
    store.close()
    assert private_path.encode() not in database.read_bytes()


def test_patch_and_check_results_survive_restart(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    first = SqliteStateStore(database)
    patch = patch_intent()
    check = check_intent("intent-check-restart")
    patch_effect = patch.to_effect_intent(AuditSequence(1))
    first.record_intent(patch_effect, expected_sequence=AuditSequence(0))
    check_effect = check.to_effect_intent(AuditSequence(2))
    first.record_intent(check_effect, expected_sequence=AuditSequence(1))
    result = ToolResult(
        code="CHECK_PASSED",
        run_id=check.run_id,
        intent_id=check.intent_id,
        passed=True,
        bounded_payload={"snapshot_digest": SNAPSHOT_SHA, "timing_ms": 12},
    )
    first.settle_intent(
        run_id=check.run_id,
        intent_id=check.intent_id,
        result=result.to_effect_result(AuditSequence(3)),
        applicable_revision_digests=check.applicable_revision_digests,
        expected_sequence=AuditSequence(2),
    )
    first.close()

    reopened = SqliteStateStore(database)
    assert ToolIntent.from_effect_intent(reopened.effect_intent(patch.intent_id)) == patch
    assert reopened.effect_result(check.intent_id).result_class == "CHECK_PASSED"
    assert reopened.effect_result(check.intent_id).snapshot_digest == SNAPSHOT_SHA
