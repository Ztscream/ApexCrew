from __future__ import annotations

import json
import sqlite3
from base64 import b32encode
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from hashlib import sha256
from pathlib import Path

from apexcrew.adapters.state.sqlite import SqliteStateStore
from apexcrew.application.control import (
    BootstrapRepositoryAuthority,
    ControlCommandService,
    CrewControlService,
)
from apexcrew.application.queries import RunQueryService
from apexcrew.domain.authority import TaskCounterSnapshot, TaskPauseBinding
from apexcrew.domain.commands import (
    ApplicableRevisionDigests,
    ApproveBudgetPayload,
    ApproveModelConfigurationPayload,
    ApprovePlanPayload,
    ApprovePolicyPayload,
    BeginPlanningPayload,
    CommandEnvelope,
    CommandPayload,
    CreateRunPayload,
    ProposeBudgetPayload,
    ProposeModelConfigurationPayload,
    ProposePolicyPayload,
    ResumePayload,
)
from apexcrew.domain.effects import AuditEvent
from apexcrew.domain.projection import ProjectionService
from apexcrew.domain.revisions import (
    BudgetRevisionDocument,
    ExecutorProfileDocument,
    HardDeniedPathClass,
    InferenceSettingsDocument,
    ModelConfigurationRevisionDocument,
    ModelPricingEntryDocument,
    PlanningReadAuthorizationDocument,
    PolicyRevisionDocument,
    ReturnedModelAliasDocument,
    SecretPathBindingDocument,
    Sha256DigestText,
    ToolVersionDocument,
    revision_digest,
)
from apexcrew.domain.types import (
    AttemptId,
    AuditSequence,
    GitOid,
    RepositoryId,
    RevisionDigest,
    RunId,
    RunState,
    TaskId,
)


class FixtureTargetAuthorityDigestService:
    def __init__(self, store: SqliteStateStore) -> None:
        self._store = store

    def current_for(self, run_id: RunId) -> Sha256DigestText:
        return self._store.target_authority_digest(run_id)


class FixtureRepositoryBootstrapAuthorityService:
    def inspect(self, repository_root: str, target_ref: str) -> BootstrapRepositoryAuthority:
        return BootstrapRepositoryAuthority(
            repository_root=repository_root,
            repository_id=RepositoryId("sha256:" + "4" * 64),
            repository_instance_digest=Sha256DigestText("sha256:" + "5" * 64),
            target_ref=target_ref,
            target_oid=GitOid(FIXTURE_TARGET_OID),
        )


@dataclass(slots=True)
class ApplicationFixture:
    root: Path
    database: Path
    store: SqliteStateStore
    control: CrewControlService
    queries: RunQueryService
    target_authority_digest: Sha256DigestText | None = None
    run_id: RunId | None = None
    current_revision_digests: ApplicableRevisionDigests = field(
        default_factory=ApplicableRevisionDigests
    )
    proposed_budget_digest: RevisionDigest | None = None
    proposed_model_digest: RevisionDigest | None = None

    def bind_run(self, run_id: RunId) -> None:
        self.run_id = run_id
        self.current_revision_digests = self.store.current_revision_digests(run_id)
        self.target_authority_digest = self.store.target_authority_digest(run_id)

    def close(self) -> None:
        self.store.close()


def make_application(tmp_path: Path) -> ApplicationFixture:
    root = tmp_path / "application"
    root.mkdir()
    database = root / "state.db"
    store = SqliteStateStore(database)
    target_authority = FixtureTargetAuthorityDigestService(store)
    control = CrewControlService(
        ControlCommandService(
            state=store,
            target_authority=target_authority,
            repository_authority=FixtureRepositoryBootstrapAuthorityService(),
        )
    )
    queries = RunQueryService(ProjectionService(store))
    return ApplicationFixture(
        root=root,
        database=database,
        store=store,
        control=control,
        queries=queries,
    )


FIXTURE_TARGET_REF = "refs/heads/main"
FIXTURE_TARGET_OID = "a" * 40
FIXTURE_POLICY_HMAC = "sha256:" + "1" * 64
FIXTURE_IMAGE_DIGEST = "sha256:" + "2" * 64
FIXTURE_TOOL_SCHEMA_DIGEST = "sha256:" + "3" * 64


def fixture_policy() -> PolicyRevisionDocument:
    return PolicyRevisionDocument(
        schema_version="policy-revision-v1",
        planning_read_authorization=PlanningReadAuthorizationDocument(
            matcher_version="apexcrew-path-v1",
            positive_globs=("src/**",),
            hard_denied_path_classes=tuple(HardDeniedPathClass),
            max_manifest_entries=2_000,
            max_manifest_bytes=131_072,
            max_file_bytes=131_072,
            max_total_returned_bytes=2_097_152,
            max_search_matches=200,
            max_search_bytes=65_536,
        ),
        secret_path_binding=SecretPathBindingDocument(
            defaults_version="secret-path-defaults-v1",
            matcher_version="apexcrew-path-v1",
            rules_hmac=FIXTURE_POLICY_HMAC,
            user_rule_count=0,
        ),
        executor_profile=ExecutorProfileDocument(
            image_digest=FIXTURE_IMAGE_DIGEST,
            platform="linux",
            architecture="x86_64",
            tool_versions=(ToolVersionDocument(name="python", version="3.12"),),
            allowed_executables=("python",),
            environment_allowlist=("LC_ALL",),
            run_as_uid=1000,
            run_as_gid=1000,
            root_filesystem_read_only=True,
            network_mode="none",
            cpu_limit=Decimal(2),
            memory_limit_bytes=2_147_483_648,
            pids_limit=256,
            scratch_limit_bytes=536_870_912,
            drop_all_capabilities=True,
            no_new_privileges=True,
        ),
        action_policy="default-action-policy-v1",
        grant_ttl_seconds=600,
    )


def fixture_budget(
    *,
    active_run_seconds_ceiling: int = 28_800,
    model_call_ceiling: int = 240,
    priced_model: str = "mock-model",
) -> BudgetRevisionDocument:
    return BudgetRevisionDocument(
        schema_version="budget-revision-v1",
        active_run_seconds_ceiling=active_run_seconds_ceiling,
        task_ceiling=12,
        planning_request_ceiling=8,
        model_call_ceiling=model_call_ceiling,
        input_token_ceiling=2_000_000,
        output_token_ceiling=200_000,
        cost_reserve_usd=Decimal(10),
        concurrent_worker_ceiling=3,
        pricing_observed_on=date(2026, 7, 26),
        pricing_entries=(
            ModelPricingEntryDocument(
                returned_model_id=priced_model,
                input_usd_per_million=Decimal(1),
                output_usd_per_million=Decimal(1),
            ),
        ),
    )


def fixture_model_configuration(
    model_id: str = "mock-model",
) -> ModelConfigurationRevisionDocument:
    return ModelConfigurationRevisionDocument(
        schema_version="model-configuration-revision-v1",
        provider="scripted_mock",
        provider_base_origin="mock://scripted",
        requested_model_id=model_id,
        returned_model_aliases=(
            ReturnedModelAliasDocument(
                returned_model_id=model_id,
                canonical_model_id=model_id,
            ),
        ),
        inference_settings=InferenceSettingsDocument(
            max_input_tokens=4_096,
            max_output_tokens=1_024,
            provider_storage_enabled=False,
        ),
        tool_schema_digest=FIXTURE_TOOL_SCHEMA_DIGEST,
    )


def _envelope(
    request_id: str,
    expected_sequence: AuditSequence | None,
    bindings: ApplicableRevisionDigests,
    payload: CommandPayload,
) -> CommandEnvelope:
    return CommandEnvelope(
        request_id=request_id,
        expected_sequence=expected_sequence,
        applicable_revision_digests=bindings,
        payload=payload,
    )


def make_create_run_command(
    request_id: str = "create-1",
    *,
    budget: BudgetRevisionDocument | None = None,
) -> CommandEnvelope:
    return _envelope(
        request_id,
        expected_sequence=None,
        bindings=ApplicableRevisionDigests(),
        payload=CreateRunPayload(
            goal="Exercise the offline application seam",
            constraints=("offline",),
            acceptance_criteria=("deterministic",),
            repository_root="fixture://sqlite-only-repository",
            target_ref=FIXTURE_TARGET_REF,
            expected_target_oid=GitOid(FIXTURE_TARGET_OID),
            policy_revision=fixture_policy(),
            budget_revision=fixture_budget() if budget is None else budget,
            model_configuration_revision=fixture_model_configuration(),
        ),
    )


def create_draft_with_three_proposals(
    app: ApplicationFixture,
    *,
    budget: BudgetRevisionDocument | None = None,
) -> RunId:
    outcome = app.control.handle(make_create_run_command(budget=budget))
    assert outcome.status == "ACCEPTED" and outcome.run_id is not None
    app.bind_run(outcome.run_id)
    return outcome.run_id


def _approved_bindings(app: ApplicationFixture, run_id: RunId) -> ApplicableRevisionDigests:
    current = app.store.current_revision_digests(run_id)
    approved = frozenset(app.store.approved_revision_classes(run_id))
    return ApplicableRevisionDigests(
        plan_digest=current.plan_digest if "PLAN" in approved else None,
        policy_digest=current.policy_digest if "POLICY" in approved else None,
        budget_digest=current.budget_digest if "BUDGET" in approved else None,
        model_configuration_digest=(
            current.model_configuration_digest if "MODEL_CONFIGURATION" in approved else None
        ),
    )


def _approval_code(
    command_kind: str,
    run_id: RunId,
    revision_class: str,
    digest: RevisionDigest,
) -> str:
    payload = json.dumps(
        {
            "command_kind": command_kind,
            "revision_class": revision_class,
            "revision_digest": digest,
            "run_id": run_id,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return b32encode(sha256(payload).digest()).decode("ascii")[:6]


def approve_current_policy_budget_and_model(app: ApplicationFixture, run_id: RunId) -> None:
    current = app.store.current_revision_digests(run_id)
    approvals = (
        ("approve-policy", "approve_policy", "POLICY", current.policy_digest),
        ("approve-budget", "approve_budget", "BUDGET", current.budget_digest),
        (
            "approve-model",
            "approve_model_configuration",
            "MODEL_CONFIGURATION",
            current.model_configuration_digest,
        ),
    )
    for request_id, command_kind, revision_class, digest in approvals:
        assert digest is not None
        code = _approval_code(command_kind, run_id, revision_class, digest)
        payload: ApprovePolicyPayload | ApproveBudgetPayload | ApproveModelConfigurationPayload
        if revision_class == "POLICY":
            payload = ApprovePolicyPayload(
                run_id=run_id,
                policy_digest=digest,
                confirmation_code=code,
            )
        elif revision_class == "BUDGET":
            payload = ApproveBudgetPayload(
                run_id=run_id,
                budget_digest=digest,
                confirmation_code=code,
            )
        else:
            payload = ApproveModelConfigurationPayload(
                run_id=run_id,
                model_configuration_digest=digest,
                confirmation_code=code,
            )
        outcome = app.control.handle(
            _envelope(
                request_id,
                app.store.audit_sequence(run_id),
                _approved_bindings(app, run_id),
                payload,
            )
        )
        assert outcome.status == "ACCEPTED"
    app.bind_run(run_id)


def make_begin_planning_command(
    app: ApplicationFixture,
    run_id: RunId,
    request_id: str = "begin-planning",
) -> CommandEnvelope:
    return _envelope(
        request_id,
        app.store.audit_sequence(run_id),
        app.store.current_revision_digests(run_id),
        BeginPlanningPayload(run_id=run_id),
    )


def _seed_control_state(app: ApplicationFixture, run_id: RunId, state: RunState) -> AuditSequence:
    expected = app.store.audit_sequence(run_id)

    def mutate(connection: sqlite3.Connection) -> None:
        cursor = connection.execute(
            "UPDATE runs SET state = ? WHERE run_id = ?", (state.value, run_id)
        )
        if cursor.rowcount != 1:
            raise AssertionError("fixture Run missing")

    return app.store._commit_state_and_event(
        run_id=run_id,
        expected_sequence=expected,
        event=AuditEvent.kind("TEST_CONTROL_STATE_SEEDED"),
        mutate=mutate,
    )


def make_active_application(tmp_path: Path) -> tuple[ApplicationFixture, RunId]:
    app = make_application(tmp_path)
    run_id = create_draft_with_three_proposals(app)
    approve_current_policy_budget_and_model(app, run_id)
    _seed_control_state(app, run_id, RunState.ACTIVE)
    app.bind_run(run_id)
    return app, run_id


def make_policy_replacement(app: ApplicationFixture, run_id: RunId) -> CommandEnvelope:
    return _envelope(
        "active-policy-replacement",
        app.store.audit_sequence(run_id),
        app.store.current_revision_digests(run_id),
        ProposePolicyPayload(run_id=run_id, policy_revision=fixture_policy()),
    )


def make_plan_reapproval(app: ApplicationFixture, run_id: RunId) -> CommandEnvelope:
    digest = RevisionDigest("sha256:" + "4" * 64)
    return _envelope(
        "active-plan-reapproval",
        app.store.audit_sequence(run_id),
        app.store.current_revision_digests(run_id),
        ApprovePlanPayload(
            run_id=run_id,
            plan_digest=digest,
            confirmation_code=_approval_code("approve_plan", run_id, "PLAN", digest),
        ),
    )


def make_unpriced_model_replacement(app: ApplicationFixture, run_id: RunId) -> CommandEnvelope:
    return _envelope(
        "unpriced-model",
        app.store.audit_sequence(run_id),
        app.store.current_revision_digests(run_id),
        ProposeModelConfigurationPayload(
            run_id=run_id,
            model_configuration_revision=fixture_model_configuration("unpriced-model"),
        ),
    )


def make_bounded_budget_replacement(app: ApplicationFixture, run_id: RunId) -> CommandEnvelope:
    budget = fixture_budget(model_call_ceiling=239)
    app.proposed_budget_digest = revision_digest(budget)
    return _envelope(
        "bounded-budget",
        app.store.audit_sequence(run_id),
        app.store.current_revision_digests(run_id),
        ProposeBudgetPayload(run_id=run_id, budget_revision=budget),
    )


def make_approve_budget_replacement(app: ApplicationFixture, run_id: RunId) -> CommandEnvelope:
    assert app.proposed_budget_digest is not None
    return _envelope(
        "approve-bounded-budget",
        app.store.audit_sequence(run_id),
        app.store.current_revision_digests(run_id),
        ApproveBudgetPayload(
            run_id=run_id,
            budget_digest=app.proposed_budget_digest,
            confirmation_code=_approval_code(
                "approve_budget", run_id, "BUDGET", app.proposed_budget_digest
            ),
        ),
    )


def make_priced_model_replacement(app: ApplicationFixture, run_id: RunId) -> CommandEnvelope:
    model = fixture_model_configuration("mock-model")
    app.proposed_model_digest = revision_digest(model)
    return _envelope(
        "priced-model",
        app.store.audit_sequence(run_id),
        app.store.current_revision_digests(run_id),
        ProposeModelConfigurationPayload(run_id=run_id, model_configuration_revision=model),
    )


def make_approve_model_replacement(app: ApplicationFixture, run_id: RunId) -> CommandEnvelope:
    assert app.proposed_model_digest is not None
    return _envelope(
        "approve-priced-model",
        app.store.audit_sequence(run_id),
        app.store.current_revision_digests(run_id),
        ApproveModelConfigurationPayload(
            run_id=run_id,
            model_configuration_digest=app.proposed_model_digest,
            confirmation_code=_approval_code(
                "approve_model_configuration",
                run_id,
                "MODEL_CONFIGURATION",
                app.proposed_model_digest,
            ),
        ),
    )


def seed_task_pause_for_test(
    app: ApplicationFixture,
    *,
    run_id: RunId,
    task_id: TaskId,
    pause_reason: str,
    applicable_revision_digests: ApplicableRevisionDigests,
) -> TaskPauseBinding:
    expected = app.store.audit_sequence(run_id)
    counters = TaskCounterSnapshot(
        run_id=run_id,
        task_id=task_id,
        allocated_calls=0,
        model_calls=0,
        input_tokens=0,
        output_tokens=0,
        cost_reserve_usd=Decimal(0),
        attempts=0,
        stale_refreshes=0,
        manual_resumes=0,
        next_lease_generation=1,
        failure_digests=(),
        checkpoint_history=(),
        invalid_action_history=(),
        warning_keys=(),
    )
    assert applicable_revision_digests.budget_digest is not None
    pause = TaskPauseBinding(
        run_id=run_id,
        task_id=task_id,
        pause_sequence=AuditSequence(expected + 1),
        pause_reason=pause_reason,
        counter_snapshot_digest=counters.digest,
        previous_attempt_id=AttemptId(f"fixture-{task_id}-attempt"),
        budget_digest_at_pause=applicable_revision_digests.budget_digest,
        applicable_revision_digests_at_pause=applicable_revision_digests,
    )
    app.store.install_task_pause_for_test(
        pause,
        counters,
        applicable_revision_digests,
    )
    return pause


def make_paused_application(
    tmp_path: Path, reason: str
) -> tuple[ApplicationFixture, TaskPauseBinding]:
    app, run_id = make_active_application(tmp_path)
    pause = seed_task_pause_for_test(
        app,
        run_id=run_id,
        task_id=TaskId("task-1"),
        pause_reason=reason,
        applicable_revision_digests=app.store.current_revision_digests(run_id),
    )
    _seed_control_state(app, run_id, RunState.PAUSED)
    app.bind_run(run_id)
    return app, pause


def make_resume_command(
    app: ApplicationFixture,
    run_id: RunId,
    *,
    task_id: TaskId,
    pause_sequence: AuditSequence,
    pause_reason: str,
    request_id: str = "resume-task",
) -> CommandEnvelope:
    return _envelope(
        request_id,
        app.store.audit_sequence(run_id),
        app.store.current_revision_digests(run_id),
        ResumePayload(
            run_id=run_id,
            task_id=task_id,
            pause_sequence=pause_sequence,
            pause_reason=pause_reason,
        ),
    )
