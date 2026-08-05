from __future__ import annotations

from pathlib import Path

from helpers.application import make_active_application

from apexcrew.adapters.state.sqlite import SqliteStateStore
from apexcrew.domain.tools import ToolResult
from apexcrew.domain.types import AttemptId, RevisionDigest, TaskId
from apexcrew.domain.worker import WorkerTurnBinding, bounded_worker_feedback

SHA = "sha256:" + "1" * 64


def _binding(run_id: str, budget_digest: RevisionDigest, number: int) -> WorkerTurnBinding:
    return WorkerTurnBinding(
        run_id=run_id,
        task_id=TaskId("task-A"),
        attempt_id=AttemptId(f"attempt-{number}"),
        tranche_id=f"tranche-{number}",
        lease_id=f"lease-{number}",
        lease_generation=number,
        admissible_head="1" * 40,
        task_contract_digest=SHA,
        plan_digest=RevisionDigest(SHA),
        policy_digest=RevisionDigest(SHA),
        budget_digest=budget_digest,
        model_configuration_digest=RevisionDigest(SHA),
        tool_schema_digest=SHA,
        target_safety_digest=SHA,
        credential_profile=None,
        repository_id="repository-1",
        snapshot_digest=SHA,
        scope_digest=SHA,
        dependency_fingerprint_basis=SHA,
    )


def test_failed_check_feedback_reaches_next_turn() -> None:
    result = ToolResult(
        code="CHECK_FAILED",
        run_id="run-1",
        intent_id="intent-check",
        passed=False,
        bounded_payload={"output": "expected 3.00, received 2.99", "timing_ms": 20},
    )

    feedback = bounded_worker_feedback(result)

    assert "expected 3.00" in feedback
    assert "CHECK_FAILED" in feedback


def test_attempt_lease_action_and_pause_survive_restart(tmp_path: Path) -> None:
    app, run_id = make_active_application(tmp_path)
    budget_digest, _ = app.store.current_approved_budget(run_id)
    action_digest = "sha256:" + "9" * 64
    for number in range(1, 4):
        binding = _binding(str(run_id), budget_digest, number)
        app.store.install_worker_attempt_for_test(binding)
        app.store.record_malformed_worker_action(
            binding=binding,
            logical_turn_id=f"turn-{number}",
            action_digest=action_digest,
            recovered_marker=None,
            permit=None,
            expected_sequence=app.store.audit_sequence(run_id),
        )
    app.close()

    reopened = SqliteStateStore(app.database)
    try:
        assert reopened.attempts_for_task(TaskId("task-A"))[-1].state == "FAILED"
        assert reopened.task_record(TaskId("task-A")).state == "PAUSED"
        assert reopened.active_lease_for_task(TaskId("task-A")) is None
        assert reopened.invalid_action_count(TaskId("task-A")) == 3
    finally:
        reopened.close()
