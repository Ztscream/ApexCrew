from __future__ import annotations

import json
import subprocess
from base64 import b32encode
from collections.abc import Sequence
from hashlib import sha256
from pathlib import Path

from apexcrew.adapters.model.scripted import ScriptedMockLLM
from apexcrew.application.composition import build_test_application_bundle
from apexcrew.application.configuration import default_revision_documents
from apexcrew.domain.commands import (
    ApplicableRevisionDigests,
    ApproveBudgetPayload,
    ApproveModelConfigurationPayload,
    ApprovePlanPayload,
    ApprovePolicyPayload,
    BeginPlanningPayload,
    CommandEnvelope,
    CreateRunPayload,
    StartPayload,
)
from apexcrew.domain.model import ModelCompletion, ModelUsage, ProviderAttemptResult
from apexcrew.domain.policy import SecretPathPolicy
from apexcrew.domain.revisions import revision_digest
from apexcrew.domain.tools import ExecutionResult, SanitizedSnapshot
from apexcrew.domain.types import GitOid, RunId


def _git(root: Path, *argv: str) -> str:
    result = subprocess.run(
        ("git", *argv),
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _approval_code(command_kind: str, run_id: RunId, revision_class: str, digest: str) -> str:
    payload = json.dumps(
        {
            "command_kind": command_kind,
            "revision_class": revision_class,
            "revision_digest": digest,
            "run_id": str(run_id),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return b32encode(sha256(payload).digest()).decode("ascii")[:6]


class _WorkerModel(ScriptedMockLLM):
    def __init__(self, worker_actions: Sequence[dict[str, object]]) -> None:
        super().__init__(())
        self.worker_actions = list(worker_actions)

    def complete(self, request):  # type: ignore[no-untyped-def]
        if request.owner_kind == "PLANNING":
            action = {
                "kind": "submit_plan",
                "plan_document": {
                    "proposed_promotion_order": ["task-01"],
                    "run_checks": [{"argv": ["python", "-m", "pytest"], "input_globs": ["src/**"]}],
                    "tasks": [
                        {
                            "checks": [
                                {
                                    "argv": ["python", "-m", "pytest"],
                                    "input_globs": ["src/task.py"],
                                }
                            ],
                            "constraints": ["keep the change scoped"],
                            "dependency_globs": [],
                            "dependency_task_ids": [],
                            "read_globs": ["src/task.py"],
                            "task_id": "task-01",
                            "write_globs": ["src/task.py"],
                        }
                    ],
                },
            }
        else:
            action = (
                self.worker_actions.pop(0)
                if self.worker_actions
                else {"kind": "finish", "summary": "task complete"}
            )
        completion = ModelCompletion(
            response_id="composition-test-response",
            requested_model_id=request.requested_model_id,
            returned_model_id=request.requested_model_id,
            usage=ModelUsage(10, 5, request.reserved_cost_usd / 10),
            normalized_action=action,
        )
        return ProviderAttemptResult.completed(completion)


class _RecordingExecutor:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], SanitizedSnapshot, int]] = []

    def run(
        self,
        argv: Sequence[str],
        snapshot: SanitizedSnapshot,
        timeout_seconds: int,
    ) -> ExecutionResult:
        self.calls.append((tuple(argv), snapshot, timeout_seconds))
        return ExecutionResult.from_output(
            exit_code=0,
            timed_out=False,
            timing_ms=1,
            secret_paths=SecretPathPolicy.from_host_rules((), b"k" * 32),
        )


def _envelope(request_id: str, sequence: int | None, payload: object) -> CommandEnvelope:
    return CommandEnvelope(
        request_id=request_id,
        expected_sequence=sequence,
        applicable_revision_digests=ApplicableRevisionDigests(),
        payload=payload,
    )


def _prepare_worker_run(
    tmp_path: Path,
    worker_actions: Sequence[dict[str, object]],
) -> tuple[object, RunId, _RecordingExecutor, Path, GitOid]:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.name", "ApexCrew Test")
    _git(root, "config", "user.email", "apexcrew@example.test")
    (root / "src").mkdir()
    (root / "src" / "task.py").write_text("value = 1\n", encoding="utf-8")
    _git(root, "add", "src/task.py")
    _git(root, "commit", "-qm", "initial")
    target_oid = GitOid(_git(root, "rev-parse", "refs/heads/main"))
    _git(root, "checkout", "--detach", str(target_oid))

    revisions = default_revision_documents()
    model_configuration = revisions.model_configuration.model_copy(
        update={"provider": "scripted_mock", "provider_base_origin": "mock://scripted"}
    )
    model = _WorkerModel(worker_actions)
    executor = _RecordingExecutor()
    bundle = build_test_application_bundle(
        root,
        model_configuration=model_configuration,
        scripted_model=model,
        secret_policy=SecretPathPolicy.from_host_rules((), b"k" * 32),
        executor=executor,
    )
    outcome = bundle.control.handle(
        _envelope(
            "create",
            None,
            CreateRunPayload(
                goal="complete the task",
                constraints=("stay within src",),
                acceptance_criteria=("the task is complete",),
                repository_root=str(root),
                target_ref="refs/heads/main",
                expected_target_oid=target_oid,
                policy_revision=revisions.policy,
                budget_revision=revisions.budget,
                model_configuration_revision=model_configuration,
            ),
        )
    )
    assert outcome.status == "ACCEPTED" and outcome.run_id is not None
    run_id = outcome.run_id
    policy_digest = revision_digest(revisions.policy)
    budget_digest = revision_digest(revisions.budget)
    model_digest = revision_digest(model_configuration)
    for request_id, command_kind, revision_class, digest, payload_type in (
        ("approve-policy", "approve_policy", "POLICY", policy_digest, "policy"),
        ("approve-budget", "approve_budget", "BUDGET", budget_digest, "budget"),
        (
            "approve-model",
            "approve_model_configuration",
            "MODEL_CONFIGURATION",
            model_digest,
            "model",
        ),
    ):
        code = _approval_code(command_kind, run_id, revision_class, digest)
        if payload_type == "policy":
            payload = ApprovePolicyPayload(
                run_id=run_id, policy_digest=digest, confirmation_code=code
            )
        elif payload_type == "budget":
            payload = ApproveBudgetPayload(
                run_id=run_id, budget_digest=digest, confirmation_code=code
            )
        else:
            payload = ApproveModelConfigurationPayload(
                run_id=run_id, model_configuration_digest=digest, confirmation_code=code
            )
        result = bundle.control.handle(
            CommandEnvelope(
                request_id=request_id,
                expected_sequence=bundle.queries.get(run_id).sequence,
                applicable_revision_digests=ApplicableRevisionDigests(
                    policy_digest=policy_digest if payload_type != "policy" else None,
                    budget_digest=budget_digest if payload_type == "model" else None,
                ),
                payload=payload,
            )
        )
        assert result.status == "ACCEPTED"
    all_revisions = ApplicableRevisionDigests(
        policy_digest=policy_digest,
        budget_digest=budget_digest,
        model_configuration_digest=model_digest,
    )
    begin = bundle.control.handle(
        CommandEnvelope(
            request_id="begin-planning",
            expected_sequence=bundle.queries.get(run_id).sequence,
            applicable_revision_digests=all_revisions,
            payload=BeginPlanningPayload(run_id=run_id),
        )
    )
    assert begin.status == "ACCEPTED"
    plan_stop = bundle.runtime.run_until_blocked(run_id)
    assert plan_stop.reason == "AWAITING_PLAN_APPROVAL"
    assert plan_stop.pending is not None
    plan_digest = plan_stop.pending.plan_digest
    approve_plan = bundle.control.handle(
        CommandEnvelope(
            request_id="approve-plan",
            expected_sequence=bundle.queries.get(run_id).sequence,
            applicable_revision_digests=all_revisions,
            payload=ApprovePlanPayload(
                run_id=run_id,
                plan_digest=plan_digest,
                confirmation_code=_approval_code("approve_plan", run_id, "PLAN", plan_digest),
            ),
        )
    )
    assert approve_plan.status == "ACCEPTED"
    start = bundle.control.handle(
        CommandEnvelope(
            request_id="start",
            expected_sequence=bundle.queries.get(run_id).sequence,
            applicable_revision_digests=ApplicableRevisionDigests(
                plan_digest=plan_digest,
                policy_digest=policy_digest,
                budget_digest=budget_digest,
                model_configuration_digest=model_digest,
            ),
            payload=StartPayload(run_id=run_id, plan_digest=plan_digest),
        )
    )
    assert start.status == "ACCEPTED"
    return bundle, run_id, executor, root, target_oid


def test_public_composition_binds_check_to_patched_workspace(tmp_path: Path) -> None:
    bundle, run_id, executor, root, _target_oid = _prepare_worker_run(
        tmp_path,
        (
            {
                "kind": "patch",
                "path": "src/task.py",
                "unified_diff": "@@ -1 +1 @@\n-value = 1\n+value = 2\n",
            },
            {"kind": "check", "check_id": "task-01:check-1"},
            {"kind": "finish", "summary": "task complete"},
        ),
    )
    try:
        stop = bundle.runtime.run_until_blocked(run_id)
        assert stop.reason == "AWAITING_FINAL_APPROVAL"
        assert len(executor.calls) == 1
        argv, snapshot, timeout_seconds = executor.calls[0]
        assert argv == ("python", "-m", "pytest")
        assert timeout_seconds == 600
        assert snapshot.root != root
        assert snapshot.materialized_paths == ("src/task.py",)
        assert (snapshot.root / "src" / "task.py").read_text(encoding="utf-8") == "value = 2\n"
    finally:
        bundle.close()


def test_unknown_check_is_settled_as_a_durable_tool_denial(tmp_path: Path) -> None:
    bundle, run_id, executor, _root, _target_oid = _prepare_worker_run(
        tmp_path,
        (
            {"kind": "check", "check_id": "task-01:unknown"},
            {"kind": "finish", "summary": "task complete"},
        ),
    )
    try:
        stop = bundle.runtime.run_until_blocked(run_id)
        assert stop.reason.value == "PAUSED"
        assert executor.calls == []
        assert bundle.queries.get(run_id).sequence > 0
    finally:
        bundle.close()


def test_invalid_risky_prestate_reaches_authority_without_raising(tmp_path: Path) -> None:
    bundle, run_id, _executor, _root, _target_oid = _prepare_worker_run(
        tmp_path,
        (
            {
                "kind": "risky_action",
                "path": "src/task.py",
                "operation": "protected_patch",
                "unified_diff": "@@ -1 +1 @@\n-value = 1\n+value = 2\n",
            },
        ),
    )
    try:
        stop = bundle.runtime.run_until_blocked(run_id)
        assert stop.reason.value == "AWAITING_ACTION_APPROVAL"
    finally:
        bundle.close()


def test_invalid_canonical_risky_path_is_durably_denied(tmp_path: Path) -> None:
    bundle, run_id, executor, _root, _target_oid = _prepare_worker_run(
        tmp_path,
        (
            {
                "kind": "risky_action",
                "path": "../src/task.py",
                "operation": "delete",
            },
        ),
    )
    try:
        stop = bundle.runtime.run_until_blocked(run_id)
        assert stop.reason.value == "PAUSED"
        assert executor.calls == []
        assert bundle.queries.get(run_id).sequence > 0
    finally:
        bundle.close()
