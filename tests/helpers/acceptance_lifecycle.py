from __future__ import annotations

import json
import shutil
import subprocess
from base64 import b32encode
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from typer.testing import CliRunner

from apexcrew.adapters.model.scripted import ScriptedMockLLM
from apexcrew.application.composition import build_test_application_bundle
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
from apexcrew.domain.tools import ExecutionResult, SanitizedSnapshot
from apexcrew.domain.types import GitOid, RunId

from .git_repository import commit_repository, make_git_repository


@dataclass(frozen=True, slots=True)
class FixtureRepairSpec:
    fixture_name: str
    source_path: str
    seeded_source: str
    patch: str
    repaired_source: str
    check_argv: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FixtureRepairEvidence:
    repository_root: Path
    run_id: RunId
    initial_target_oid: GitOid
    prepared_oid: GitOid
    private_head_oid: GitOid
    target_source: str
    observed_check_source: str
    model_action_kinds: tuple[str, ...]


class _FixtureModel(ScriptedMockLLM):
    def __init__(self, spec: FixtureRepairSpec) -> None:
        super().__init__(())
        self._spec = spec
        self.action_kinds: list[str] = []

    def complete(self, request):  # type: ignore[no-untyped-def]
        if request.owner_kind == "PLANNING":
            action = {
                "kind": "submit_plan",
                "plan_document": {
                    "proposed_promotion_order": ["task-01"],
                    "run_checks": [
                        {
                            "argv": list(self._spec.check_argv),
                            "input_globs": [self._spec.source_path],
                        }
                    ],
                    "tasks": [
                        {
                            "checks": [
                                {
                                    "argv": list(self._spec.check_argv),
                                    "input_globs": [self._spec.source_path],
                                }
                            ],
                            "constraints": ["keep the change scoped to the fixture source"],
                            "dependency_globs": [],
                            "dependency_task_ids": [],
                            "read_globs": [self._spec.source_path],
                            "task_id": "task-01",
                            "write_globs": [self._spec.source_path],
                        }
                    ],
                },
            }
        else:
            action = self._next_worker_action()
            self.action_kinds.append(str(action["kind"]))
        completion = ModelCompletion(
            response_id="fixture-repair-response",
            requested_model_id=request.requested_model_id,
            returned_model_id=request.requested_model_id,
            usage=ModelUsage(10, 5, request.reserved_cost_usd / 10),
            normalized_action=action,
        )
        return ProviderAttemptResult.completed(completion)

    def _next_worker_action(self) -> dict[str, object]:
        actions = (
            {"kind": "read", "path": self._spec.source_path},
            {
                "kind": "patch",
                "path": self._spec.source_path,
                "unified_diff": self._spec.patch,
            },
            {"kind": "check", "check_id": "task-01:check-1"},
            {"kind": "finish", "summary": "fixture defect repaired"},
        )
        if not hasattr(self, "_worker_index"):
            self._worker_index = 0
        if self._worker_index >= len(actions):
            return actions[-1]
        action = actions[self._worker_index]
        self._worker_index += 1
        return action


class _FixtureExecutor:
    def __init__(self, spec: FixtureRepairSpec) -> None:
        self._spec = spec
        self.observed_source = ""

    def run(
        self,
        argv: tuple[str, ...],
        snapshot: SanitizedSnapshot,
        timeout_seconds: int,
    ) -> ExecutionResult:
        del timeout_seconds
        source = snapshot.root / self._spec.source_path
        self.observed_source = source.read_text(encoding="utf-8")
        exit_code = (
            0
            if argv == self._spec.check_argv and self.observed_source == self._spec.repaired_source
            else 1
        )
        return ExecutionResult.from_output(
            exit_code=exit_code,
            timed_out=False,
            timing_ms=1,
            secret_paths=SecretPathPolicy.from_host_rules((), b"k" * 32),
        )


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


def run_fixture_repair(tmp_path: Path, spec: FixtureRepairSpec) -> FixtureRepairEvidence:
    root = make_git_repository(tmp_path)
    _git(root, "branch", "-M", "main")
    _git(root, "config", "user.name", "ApexCrew acceptance")
    _git(root, "config", "user.email", "acceptance@localhost")
    fixture = Path(__file__).parents[2] / "fixtures" / spec.fixture_name
    shutil.copytree(fixture, root, dirs_exist_ok=True)
    source = root / spec.source_path
    source.write_text(spec.seeded_source, encoding="utf-8")
    initial_oid = GitOid(commit_repository(root, f"seed {spec.fixture_name} unit drift"))
    _git(root, "checkout", "--detach", str(initial_oid))

    revisions = default_revision_documents()
    model_configuration = revisions.model_configuration.model_copy(
        update={"provider": "scripted_mock", "provider_base_origin": "mock://scripted"}
    )
    model = _FixtureModel(spec)
    executor = _FixtureExecutor(spec)
    bundle = build_test_application_bundle(
        root,
        model_configuration=model_configuration,
        scripted_model=model,
        secret_policy=SecretPathPolicy.from_host_rules((), b"k" * 32),
        executor=executor,
    )
    runner = CliRunner()
    try:
        assert runner.invoke(cli_app, ["init", "--root", str(root)]).exit_code == 0
        payload = CreateRunPayload(
            goal=f"repair the {spec.fixture_name} unit drift",
            constraints=("stay within the fixture source",),
            acceptance_criteria=("the declared check passes on the repaired source",),
            repository_root=str(root),
            target_ref="refs/heads/main",
            expected_target_oid=initial_oid,
            policy_revision=revisions.policy,
            budget_revision=revisions.budget,
            model_configuration_revision=model_configuration,
        )
        created = bundle.control.handle(_envelope("create", None, payload))
        assert created.status == "ACCEPTED" and created.run_id is not None
        run_id = created.run_id
        policy_digest = revision_digest(revisions.policy)
        budget_digest = revision_digest(revisions.budget)
        model_digest = revision_digest(model_configuration)
        approvals = (
            (
                "approve-policy",
                ApplicableRevisionDigests(),
                ApprovePolicyPayload(
                    run_id=run_id,
                    policy_digest=policy_digest,
                    confirmation_code=_approval_code(
                        "approve_policy", run_id, "POLICY", str(policy_digest)
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
                        "approve_budget", run_id, "BUDGET", str(budget_digest)
                    ),
                ),
            ),
            (
                "approve-model",
                ApplicableRevisionDigests(policy_digest=policy_digest, budget_digest=budget_digest),
                ApproveModelConfigurationPayload(
                    run_id=run_id,
                    model_configuration_digest=model_digest,
                    confirmation_code=_approval_code(
                        "approve_model_configuration",
                        run_id,
                        "MODEL_CONFIGURATION",
                        str(model_digest),
                    ),
                ),
            ),
        )
        for request_id, bindings, approval in approvals:
            result = bundle.control.handle(
                CommandEnvelope(
                    request_id=request_id,
                    expected_sequence=bundle.queries.get(run_id).sequence,
                    applicable_revision_digests=bindings,
                    payload=approval,
                )
            )
            assert result.status == "ACCEPTED", result.model_dump(mode="json")

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
        assert plan_stop.pending is not None
        plan_digest = plan_stop.pending.plan_digest
        plan_result = bundle.control.handle(
            CommandEnvelope(
                request_id="approve-plan",
                expected_sequence=bundle.queries.get(run_id).sequence,
                applicable_revision_digests=all_revisions,
                payload=ApprovePlanPayload(
                    run_id=run_id,
                    plan_digest=plan_digest,
                    confirmation_code=_approval_code(
                        "approve_plan", run_id, "PLAN", str(plan_digest)
                    ),
                ),
            )
        )
        assert plan_result.status == "ACCEPTED"
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
        final_stop = bundle.runtime.run_until_blocked(run_id)
        assert final_stop.reason.value == "AWAITING_FINAL_APPROVAL", final_stop.model_dump(
            mode="json"
        )
        candidate = bundle.runtime._store.final_candidate(run_id)
        assert candidate.target_base_oid == initial_oid
        assert candidate.prepared_oid != candidate.head_oid
        private_head = GitOid(_git(root, "rev-parse", f"refs/apexcrew/runs/{run_id}"))
        assert _git(root, "show", "-s", "--format=%P", str(candidate.prepared_oid)) == str(
            initial_oid
        )

        preview = runner.invoke(
            cli_app, ["integrate", str(run_id), "--root", str(root), "--preview"]
        )
        assert preview.exit_code == 0, preview.stdout
        integration = json.loads(preview.stdout)
        accepted = runner.invoke(
            cli_app,
            [
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
            ],
        )
        assert accepted.exit_code == 0, accepted.stdout
        assert bundle.runtime.run_until_blocked(run_id).reason.value == "TERMINAL"
        assert bundle.queries.get(run_id).state.value == "COMPLETED"
        cleanup = runner.invoke(cli_app, ["reconcile-cleanup", str(run_id), "--root", str(root)])
        assert cleanup.exit_code == 0, cleanup.stdout
        assert bundle.runtime.run_until_blocked(run_id).reason.value == "TERMINAL"
        assert _git(root, "rev-parse", "refs/heads/main") == str(candidate.prepared_oid)
        assert _git(root, "rev-parse", f"refs/apexcrew/runs/{run_id}") == str(private_head)
        target_source = _git(root, "show", f"{candidate.prepared_oid}:{spec.source_path}")
        assert target_source == spec.repaired_source.rstrip("\n")
        assert executor.observed_source == spec.repaired_source
        assert tuple(model.action_kinds) == ("read", "patch", "check", "finish")
        return FixtureRepairEvidence(
            repository_root=root,
            run_id=run_id,
            initial_target_oid=initial_oid,
            prepared_oid=GitOid(candidate.prepared_oid),
            private_head_oid=private_head,
            target_source=target_source,
            observed_check_source=executor.observed_source,
            model_action_kinds=tuple(model.action_kinds),
        )
    finally:
        bundle.close()
