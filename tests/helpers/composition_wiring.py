from __future__ import annotations

import json
import subprocess
from base64 import b32encode
from hashlib import sha256
from pathlib import Path

from apexcrew.adapters.model.scripted import ScriptedMockLLM
from apexcrew.application.composition import build_application_bundle
from apexcrew.application.configuration import default_revision_documents
from apexcrew.domain.actions import CheckAction
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
from apexcrew.domain.coordination import task_contract_digest
from apexcrew.domain.model import ModelCompletion, ModelUsage, ProviderAttemptResult
from apexcrew.domain.policy import SecretPathPolicy
from apexcrew.domain.revisions import revision_digest
from apexcrew.domain.tools import ToolIntent
from apexcrew.domain.types import AttemptId, GitOid, IntentId, RunId, TaskId
from apexcrew.domain.worker import WorkerTurnBinding

_TEST_SHA = "sha256:" + "9" * 64


class _ProductionModel(ScriptedMockLLM):
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
            action = {"kind": "finish", "summary": "task complete"}
        completion = ModelCompletion(
            response_id="production-composition-response",
            requested_model_id=request.requested_model_id,
            returned_model_id=request.requested_model_id,
            usage=ModelUsage(10, 5, request.reserved_cost_usd / 10),
            normalized_action=action,
        )
        return ProviderAttemptResult.completed(completion)


def _git(root: Path, *argv: str) -> str:
    result = subprocess.run(("git", *argv), cwd=root, check=True, capture_output=True, text=True)
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


def _envelope(request_id: str, sequence: int | None, payload: object) -> CommandEnvelope:
    return CommandEnvelope(
        request_id=request_id,
        expected_sequence=sequence,
        applicable_revision_digests=ApplicableRevisionDigests(),
        payload=payload,
    )


def production_check_executor(tmp_path: Path) -> object:
    root = tmp_path / "production-repo"
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
    bundle = build_application_bundle(
        root,
        model_configuration=model_configuration,
        scripted_model=_ProductionModel(()),
        secret_policy=SecretPathPolicy.from_host_rules((), b"k" * 32),
    )
    try:
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
        for request_id, command_kind, revision_class, digest, payload in (
            (
                "approve-policy",
                "approve_policy",
                "POLICY",
                policy_digest,
                ApprovePolicyPayload,
            ),
            (
                "approve-budget",
                "approve_budget",
                "BUDGET",
                budget_digest,
                ApproveBudgetPayload,
            ),
            (
                "approve-model",
                "approve_model_configuration",
                "MODEL_CONFIGURATION",
                model_digest,
                ApproveModelConfigurationPayload,
            ),
        ):
            kwargs = {
                "run_id": run_id,
                "confirmation_code": _approval_code(command_kind, run_id, revision_class, digest),
            }
            if revision_class == "POLICY":
                kwargs["policy_digest"] = digest
            elif revision_class == "BUDGET":
                kwargs["budget_digest"] = digest
            else:
                kwargs["model_configuration_digest"] = digest
            result = bundle.control.handle(
                CommandEnvelope(
                    request_id=request_id,
                    expected_sequence=bundle.queries.get(run_id).sequence,
                    applicable_revision_digests=ApplicableRevisionDigests(
                        policy_digest=policy_digest if revision_class != "POLICY" else None,
                        budget_digest=budget_digest
                        if revision_class == "MODEL_CONFIGURATION"
                        else None,
                    ),
                    payload=payload(**kwargs),
                )
            )
            assert result.status == "ACCEPTED"
        revisions_binding = ApplicableRevisionDigests(
            policy_digest=policy_digest,
            budget_digest=budget_digest,
            model_configuration_digest=model_digest,
        )
        assert (
            bundle.control.handle(
                CommandEnvelope(
                    request_id="begin-planning",
                    expected_sequence=bundle.queries.get(run_id).sequence,
                    applicable_revision_digests=revisions_binding,
                    payload=BeginPlanningPayload(run_id=run_id),
                )
            ).status
            == "ACCEPTED"
        )
        plan_stop = bundle.runtime.run_until_blocked(run_id)
        assert plan_stop.pending is not None
        plan_digest = plan_stop.pending.plan_digest
        assert (
            bundle.control.handle(
                CommandEnvelope(
                    request_id="approve-plan",
                    expected_sequence=bundle.queries.get(run_id).sequence,
                    applicable_revision_digests=revisions_binding,
                    payload=ApprovePlanPayload(
                        run_id=run_id,
                        plan_digest=plan_digest,
                        confirmation_code=_approval_code(
                            "approve_plan", run_id, "PLAN", plan_digest
                        ),
                    ),
                )
            ).status
            == "ACCEPTED"
        )
        assert (
            bundle.control.handle(
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
            ).status
            == "ACCEPTED"
        )

        phase_drivers = bundle.runtime._phase_drivers  # type: ignore[attr-defined]
        workers = phase_drivers._recovered_actions._workers
        tools = workers._tools
        store = workers._attempts
        current = store.current_revision_digests(run_id)
        assert current.plan_digest is not None
        contract = next(
            item
            for item in store.task_contracts(current.plan_digest)
            if item.task_id == TaskId("task-01")
        )
        model_document = store.current_revision_document(run_id, "MODEL_CONFIGURATION")
        run = store.run_record(run_id)
        binding = WorkerTurnBinding(
            run_id=run_id,
            task_id=TaskId("task-01"),
            attempt_id=AttemptId("production-composition-attempt"),
            tranche_id="production-composition-tranche",
            lease_id="production-composition-lease",
            lease_generation=1,
            admissible_head=str(run.pinned_target_oid),
            task_contract_digest=task_contract_digest(contract),
            plan_digest=current.plan_digest,
            policy_digest=current.policy_digest,
            budget_digest=current.budget_digest,
            model_configuration_digest=current.model_configuration_digest,
            tool_schema_digest=model_document.tool_schema_digest,
            target_safety_digest=store.target_authority_digest(run_id),
            credential_profile=None,
            repository_id=str(run.repository_id),
            snapshot_digest=_TEST_SHA,
            scope_digest=_TEST_SHA,
            dependency_fingerprint_basis=_TEST_SHA,
        )
        store.install_worker_attempt_for_test(binding)
        action = CheckAction(check_id="task-01:check-1")
        snapshot_digest = tools.capture_snapshot_digest(binding, action)
        intent = ToolIntent.for_authorized_worker_action(
            intent_id=IntentId("production-composition-check"),
            run_id=run_id,
            task_id=binding.task_id,
            attempt_id=binding.attempt_id,
            action_id="production-composition-check",
            action=action,
            authorization_binding_digest=_TEST_SHA,
            applicable_revision_digests=binding.applicable_revision_digests,
            repository_id=binding.repository_id,
            snapshot_digest=snapshot_digest,
            scope_digest=binding.scope_digest,
            dependency_fingerprint_basis=binding.dependency_fingerprint_basis,
            idempotency_key="production-composition-check",
            expected_prestate_json="{}",
        )
        runtime = tools._runtime(intent)  # type: ignore[attr-defined]
        return runtime._executor  # type: ignore[attr-defined]
    finally:
        bundle.close()
