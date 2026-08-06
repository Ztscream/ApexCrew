from __future__ import annotations

import json
import subprocess
from base64 import b32encode
from hashlib import sha256
from pathlib import Path

from typer.testing import CliRunner

from apexcrew.adapters.model.scripted import ScriptedMockLLM
from apexcrew.application.composition import build_application_bundle
from apexcrew.application.configuration import default_revision_documents
from apexcrew.delivery.cli import app as cli_app
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
from apexcrew.domain.types import GitOid


def _git(root: Path, *argv: str) -> str:
    result = subprocess.run(
        ("git", *argv),
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _approval_code(command_kind: str, run_id: str, revision_class: str, digest: str) -> str:
    payload = (
        f'{{"command_kind":"{command_kind}","revision_class":"{revision_class}",'
        f'"revision_digest":"{digest}","run_id":"{run_id}"}}'
    ).encode()
    return b32encode(sha256(payload).digest()).decode("ascii")[:6]


class _LifecycleModel(ScriptedMockLLM):
    def __init__(self) -> None:
        super().__init__(())
        self.requests = []

    def complete(self, request):  # type: ignore[no-untyped-def]
        self.requests.append(request)
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
            action = {"kind": "finish", "summary": "task complete"}
        completion = ModelCompletion(
            response_id="lifecycle-response",
            requested_model_id=request.requested_model_id,
            returned_model_id=request.requested_model_id,
            usage=ModelUsage(10, 5, request.reserved_cost_usd / 10),
            normalized_action=action,
        )
        return ProviderAttemptResult.completed(completion)


def _envelope(request_id: str, sequence: int | None, payload) -> CommandEnvelope:  # type: ignore[no-untyped-def]
    return CommandEnvelope(
        request_id=request_id,
        expected_sequence=sequence,
        applicable_revision_digests=ApplicableRevisionDigests(),
        payload=payload,
    )


def test_composed_runtime_reaches_plan_approval_with_real_git_reservation(tmp_path: Path) -> None:
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
    _git(root, "checkout", "--detach", target_oid)

    revisions = default_revision_documents()
    model_configuration = revisions.model_configuration.model_copy(
        update={"provider": "scripted_mock", "provider_base_origin": "mock://scripted"}
    )
    payload = CreateRunPayload(
        goal="complete the task",
        constraints=("stay within src",),
        acceptance_criteria=("the task is complete",),
        repository_root=str(root),
        target_ref="refs/heads/main",
        expected_target_oid=target_oid,
        policy_revision=revisions.policy,
        budget_revision=revisions.budget,
        model_configuration_revision=model_configuration,
    )
    secret_policy = SecretPathPolicy.from_host_rules((), b"k" * 32)
    model = _LifecycleModel()
    bundle = build_application_bundle(
        root,
        model_configuration=model_configuration,
        scripted_model=model,
        secret_policy=secret_policy,
    )
    try:
        created = bundle.control.handle(_envelope("create", None, payload))
        assert created.status == "ACCEPTED"
        assert created.run_id is not None
        run_id = created.run_id
        policy_digest = revision_digest(revisions.policy)
        budget_digest = revision_digest(revisions.budget)
        model_digest = revision_digest(model_configuration)

        policy_approval = bundle.control.handle(
            CommandEnvelope(
                request_id="approve-policy",
                expected_sequence=created.resulting_sequence,
                applicable_revision_digests=ApplicableRevisionDigests(),
                payload=ApprovePolicyPayload(
                    run_id=run_id,
                    policy_digest=policy_digest,
                    confirmation_code=_approval_code(
                        "approve_policy", str(run_id), "POLICY", str(policy_digest)
                    ),
                ),
            )
        )
        assert policy_approval.status == "ACCEPTED"

        budget_approval = bundle.control.handle(
            CommandEnvelope(
                request_id="approve-budget",
                expected_sequence=bundle.queries.get(run_id).sequence,
                applicable_revision_digests=ApplicableRevisionDigests(policy_digest=policy_digest),
                payload=ApproveBudgetPayload(
                    run_id=run_id,
                    budget_digest=budget_digest,
                    confirmation_code=_approval_code(
                        "approve_budget", str(run_id), "BUDGET", str(budget_digest)
                    ),
                ),
            )
        )
        assert budget_approval.status == "ACCEPTED"

        model_approval = bundle.control.handle(
            CommandEnvelope(
                request_id="approve-model",
                expected_sequence=bundle.queries.get(run_id).sequence,
                applicable_revision_digests=ApplicableRevisionDigests(
                    policy_digest=policy_digest,
                    budget_digest=budget_digest,
                ),
                payload=ApproveModelConfigurationPayload(
                    run_id=run_id,
                    model_configuration_digest=model_digest,
                    confirmation_code=_approval_code(
                        "approve_model_configuration",
                        str(run_id),
                        "MODEL_CONFIGURATION",
                        str(model_digest),
                    ),
                ),
            )
        )
        assert model_approval.status == "ACCEPTED"

        begin = bundle.control.handle(
            CommandEnvelope(
                request_id="begin-planning",
                expected_sequence=bundle.queries.get(run_id).sequence,
                applicable_revision_digests=ApplicableRevisionDigests(
                    policy_digest=policy_digest,
                    budget_digest=budget_digest,
                    model_configuration_digest=model_digest,
                ),
                payload=BeginPlanningPayload(run_id=run_id),
            )
        )
        assert begin.status == "ACCEPTED"
        planning_stop = bundle.runtime.run_until_blocked(run_id)
        assert planning_stop.reason == "AWAITING_PLAN_APPROVAL"
        assert planning_stop.pending is not None
        assert bundle.queries.get(run_id).state == "AWAITING_PLAN_APPROVAL"
        assert planning_stop.last_sequence == bundle.queries.get(run_id).sequence
        prompt = model.requests[0].prompt[0]["content"]
        assert "complete the task" in prompt
        assert "src/task.py" in prompt
    finally:
        bundle.close()


def test_composed_runtime_integrates_frozen_candidate_once(tmp_path: Path) -> None:
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
    _git(root, "checkout", "--detach", target_oid)

    revisions = default_revision_documents()
    model_configuration = revisions.model_configuration.model_copy(
        update={"provider": "scripted_mock", "provider_base_origin": "mock://scripted"}
    )
    payload = CreateRunPayload(
        goal="complete the task",
        constraints=("stay within src",),
        acceptance_criteria=("the task is complete",),
        repository_root=str(root),
        target_ref="refs/heads/main",
        expected_target_oid=target_oid,
        policy_revision=revisions.policy,
        budget_revision=revisions.budget,
        model_configuration_revision=model_configuration,
    )
    secret_policy = SecretPathPolicy.from_host_rules((), b"k" * 32)
    bundle = build_application_bundle(
        root,
        model_configuration=model_configuration,
        scripted_model=_LifecycleModel(),
        secret_policy=secret_policy,
    )
    runner = CliRunner()
    try:
        assert runner.invoke(cli_app, ["init", "--root", str(root)]).exit_code == 0
        created = bundle.control.handle(_envelope("create", None, payload))
        assert created.status == "ACCEPTED"
        assert created.run_id is not None
        run_id = created.run_id
        policy_digest = revision_digest(revisions.policy)
        budget_digest = revision_digest(revisions.budget)
        model_digest = revision_digest(model_configuration)
        all_revisions = ApplicableRevisionDigests(
            policy_digest=policy_digest,
            budget_digest=budget_digest,
            model_configuration_digest=model_digest,
        )
        for request_id, bindings, approval in (
            (
                "approve-policy",
                ApplicableRevisionDigests(),
                ApprovePolicyPayload(
                    run_id=run_id,
                    policy_digest=policy_digest,
                    confirmation_code=_approval_code(
                        "approve_policy", str(run_id), "POLICY", str(policy_digest)
                    ),
                ),
            ),
            (
                "approve-budget",
                ApplicableRevisionDigests(policy_digest=policy_digest),
                ApproveBudgetPayload(
                    run_id=run_id,
                    budget_digest=budget_digest,
                    confirmation_code=_approval_code(
                        "approve_budget", str(run_id), "BUDGET", str(budget_digest)
                    ),
                ),
            ),
            (
                "approve-model",
                ApplicableRevisionDigests(
                    policy_digest=policy_digest,
                    budget_digest=budget_digest,
                ),
                ApproveModelConfigurationPayload(
                    run_id=run_id,
                    model_configuration_digest=model_digest,
                    confirmation_code=_approval_code(
                        "approve_model_configuration",
                        str(run_id),
                        "MODEL_CONFIGURATION",
                        str(model_digest),
                    ),
                ),
            ),
        ):
            result = bundle.control.handle(
                CommandEnvelope(
                    request_id=request_id,
                    expected_sequence=bundle.queries.get(run_id).sequence,
                    applicable_revision_digests=bindings,
                    payload=approval,
                )
            )
            assert result.status == "ACCEPTED"
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
                request_id="approve-plan-final",
                expected_sequence=bundle.queries.get(run_id).sequence,
                applicable_revision_digests=all_revisions,
                payload=ApprovePlanPayload(
                    run_id=run_id,
                    plan_digest=plan_digest,
                    confirmation_code=_approval_code(
                        "approve_plan", str(run_id), "PLAN", str(plan_digest)
                    ),
                ),
            )
        )
        assert approve_plan.status == "ACCEPTED"
        start = bundle.control.handle(
            CommandEnvelope(
                request_id="start-final",
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
        final_stop = bundle.runtime.run_until_blocked(run_id)
        assert final_stop.reason == "AWAITING_FINAL_APPROVAL", final_stop.model_dump(mode="json")
        assert final_stop.pending is not None
        assert bundle.queries.get(run_id).state == "READY_FOR_APPROVAL"

        preview = runner.invoke(
            cli_app,
            ["integrate", str(run_id), "--root", str(root), "--preview"],
        )
        assert preview.exit_code == 0, preview.stdout
        integration = json.loads(preview.stdout)
        command_args = [
            "integrate",
            str(run_id),
            "--root",
            str(root),
            "--candidate-id",
            integration["candidate_id"],
            "--evidence-bundle-digest",
            integration["evidence_bundle_digest"],
            "--expected-target-oid",
            integration["expected_target_oid"],
            "--prepared-oid",
            integration["prepared_oid"],
            "--confirmation-code",
            integration["confirmation_code"],
        ]
        accepted = runner.invoke(cli_app, command_args)
        assert accepted.exit_code == 0, accepted.stdout
        assert json.loads(accepted.stdout)["status"] == "COMMAND_ACCEPTED"
        integrated = bundle.runtime.run_until_blocked(run_id)
        assert integrated.reason == "TERMINAL"
        assert bundle.queries.get(run_id).state == "COMPLETED"
        cleanup_outcome = runner.invoke(
            cli_app,
            ["reconcile-cleanup", str(run_id), "--root", str(root)],
        )
        assert cleanup_outcome.exit_code == 0, cleanup_outcome.stdout
        assert json.loads(cleanup_outcome.stdout)["status"] == "COMMAND_ACCEPTED"
        cleanup_stop = bundle.runtime.run_until_blocked(run_id)
        assert cleanup_stop.reason == "TERMINAL"
        assert bundle.queries.get(run_id).state == "COMPLETED"
        reservations = root / ".apexcrew" / "data" / "reservations"
        assert tuple(reservations.iterdir()) == ()
        assert _git(root, "worktree", "list", "--porcelain").count("worktree ") == 1
        replay = runner.invoke(cli_app, command_args)
        assert replay.exit_code == 0, replay.stdout
        assert json.loads(replay.stdout)["status"] == "COMMAND_ACCEPTED"
        assert _git(root, "rev-parse", "refs/heads/main") == str(target_oid)
    finally:
        bundle.close()
