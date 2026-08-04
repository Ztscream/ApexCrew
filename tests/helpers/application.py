from __future__ import annotations

import json
import sqlite3
from base64 import b32encode
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from typing import Literal

from apexcrew.adapters.model.scripted import ScriptedMockLLM, ScriptedModelStep
from apexcrew.adapters.state.sqlite import SqliteStateStore
from apexcrew.adapters.system import SystemMonotonicClock
from apexcrew.application.control import (
    BootstrapRepositoryAuthority,
    ControlCommandService,
    CrewControlService,
)
from apexcrew.application.queries import RunQueryService
from apexcrew.application.runtime import (
    FinalIntegrationDriver,
    InjectedProcessCrash,
    InMemoryRunOwnership,
    PrivateRefDriver,
    RecoveredActionRouter,
    ResolutionDriver,
    RuntimeCoordinator,
    RuntimePhaseDriverService,
    RuntimeService,
    RuntimeStateStore,
    RuntimeWorkerLoop,
    TargetReservationDriverService,
    TerminalCleanupDriver,
)
from apexcrew.domain.admission import (
    TargetReservationAdmissionService,
    TargetReservationBootstrapAdmissionService,
    TargetReservationGitPort,
    TargetReservationObserver,
    TargetReservationOperation,
    TargetReservationOperationResult,
)
from apexcrew.domain.authority import (
    AuthorityService,
    MonotonicClock,
    MonotonicInstant,
    TaskCounterSnapshot,
    TaskPauseBinding,
)
from apexcrew.domain.commands import (
    ApplicableRevisionDigests,
    ApproveBudgetPayload,
    ApproveModelConfigurationPayload,
    ApprovePlanPayload,
    ApprovePolicyPayload,
    BeginPlanningPayload,
    CommandEnvelope,
    CommandOutcome,
    CommandPayload,
    ContinuePayload,
    CreateRunPayload,
    ProposeBudgetPayload,
    ProposeModelConfigurationPayload,
    ProposePolicyPayload,
    ResumePayload,
    RuntimeDecision,
    RuntimePermit,
)
from apexcrew.domain.effects import (
    AuditEvent,
    RecoveryOutcome,
    ReservationObservation,
    TargetReservation,
)
from apexcrew.domain.model import (
    CommittedModelTurn,
    DurableModelClient,
    ModelCompletion,
    ModelRequest,
    ModelUsage,
    ProviderAttemptResult,
    RecoveredModelAction,
)
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
    CommandStatus,
    GitOid,
    RepositoryId,
    RevisionDigest,
    RunId,
    RunState,
    RunStopReason,
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


def make_application(
    tmp_path: Path, *, monotonic_clock: MonotonicClock | None = None
) -> ApplicationFixture:
    root = tmp_path / "application"
    root.mkdir()
    database = root / "state.db"
    store = SqliteStateStore(database, monotonic_clock=monotonic_clock)
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
    app: ApplicationFixture | RuntimeApplicationFixture,
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


@dataclass
class ManualMonotonicClock:
    nanoseconds: int

    @classmethod
    def at_seconds(cls, seconds: int) -> ManualMonotonicClock:
        return cls(seconds * 1_000_000_000)

    def now(self) -> MonotonicInstant:
        return MonotonicInstant(self.nanoseconds)

    def advance_seconds(self, seconds: int) -> None:
        if seconds < 0:
            raise ValueError("MANUAL_MONOTONIC_ADVANCE_NEGATIVE")
        self.nanoseconds += seconds * 1_000_000_000


@dataclass
class RegressOnceMonotonicClock:
    first: MonotonicInstant
    regressed: MonotonicInstant
    calls: int = 0

    def now(self) -> MonotonicInstant:
        self.calls += 1
        return self.regressed if self.calls == 2 else self.first


class RuntimeAwareControl:
    def __init__(
        self,
        base: CrewControlService,
        store: SqliteStateStore,
        ownership: InMemoryRunOwnership,
    ) -> None:
        self._base = base
        self._store = store
        self._ownership = ownership

    def handle(self, command: CommandEnvelope) -> CommandOutcome:
        if not isinstance(command.payload, ContinuePayload):
            return self._base.handle(command)
        with self._ownership.acquire(command.payload.run_id) as owner:
            if owner is None:
                return CommandOutcome.for_payload(
                    command.payload,
                    status=CommandStatus.CONFLICT,
                    run_id=command.payload.run_id,
                    resulting_sequence=self._store.audit_sequence(command.payload.run_id),
                    failed_invariant="RUNTIME_DELIVERY_PENDING",
                )
            return self._store.apply_runtime_continue(command)


@dataclass(slots=True)
class RuntimeApplicationFixture:
    root: Path
    store: SqliteStateStore
    control: RuntimeAwareControl
    queries: RunQueryService
    runtime: RuntimeService
    model: ScriptedMockLLM
    git_spawner: RecordingGitSpawner
    ownership: InMemoryRunOwnership
    monotonic_clock: MonotonicClock
    run_id: RunId
    begin_command: CommandEnvelope | None = None

    def reopen(self) -> RuntimeApplicationFixture:
        self.store.close()
        return open_runtime_application(
            self.root,
            self.run_id,
            model=self.model,
            monotonic_clock=self.monotonic_clock,
        )


@dataclass
class RecordingGitSpawner:
    call_count: int = 0


class FailClosedPrivateRefDriver(PrivateRefDriver):
    def initialize(self, run_id: RunId, permit: RuntimePermit) -> RuntimeDecision:
        del run_id, permit
        return RuntimeDecision.pause("PRIVATE_REF_DRIVER_NOT_INSTALLED")


class FailClosedResolutionDriver(ResolutionDriver):
    def resume(self, run_id: RunId, permit: RuntimePermit) -> RuntimeDecision:
        del run_id, permit
        return RuntimeDecision.pause("RESOLUTION_DRIVER_NOT_INSTALLED")


class FailClosedFinalIntegrationDriver(FinalIntegrationDriver):
    def integrate(self, run_id: RunId, permit: RuntimePermit) -> RuntimeDecision:
        del run_id, permit
        return RuntimeDecision.pause("FINAL_INTEGRATION_DRIVER_NOT_INSTALLED")


class FailClosedTerminalCleanupDriver(TerminalCleanupDriver):
    def reconcile(self, run_id: RunId, permit: RuntimePermit) -> RuntimeDecision:
        del run_id, permit
        return RuntimeDecision.pause("TERMINAL_CLEANUP_DRIVER_NOT_INSTALLED")


class NoEffectRecoveryService:
    def __init__(self, store: SqliteStateStore) -> None:
        self._store = store

    def reconcile(self, run_id: RunId) -> RecoveryOutcome:
        if self._store.unsettled_intents(run_id):
            raise AssertionError("test fixture must install a recovery strategy")
        return RecoveryOutcome.empty()


class StaticToolSchemaProvider:
    @property
    def schema_digest(self) -> Sha256DigestText:
        return Sha256DigestText(FIXTURE_TOOL_SCHEMA_DIGEST)


class FixtureStoppingCoordinator:
    def __init__(self, store: RuntimeStateStore) -> None:
        self._store = store

    def run_planning_turn(self, run_id: RunId) -> RuntimeDecision:
        return RuntimeDecision.pause("FIXTURE_PLANNING_STOP", self._store.audit_sequence(run_id))

    def resume_recovered_planning_action(
        self, run_id: RunId, permit: RuntimePermit, action: RecoveredModelAction
    ) -> RuntimeDecision:
        del permit, action
        return RuntimeDecision.pause("FIXTURE_RECOVERED_STOP", self._store.audit_sequence(run_id))

    def schedule(self, run_id: RunId) -> RuntimeDecision:
        return RuntimeDecision.pause("FIXTURE_WORKER_STOP", self._store.audit_sequence(run_id))


class FixtureWorkerLoop:
    def resume_recovered_worker_action(
        self, run_id: RunId, permit: RuntimePermit, action: RecoveredModelAction
    ) -> RuntimeDecision:
        del run_id, permit, action
        raise AssertionError("Task 11 fixture has no Worker recovery")


@dataclass
class FixtureReservationGit:
    def apply(self, operation: TargetReservationOperation) -> TargetReservationOperationResult:
        return operation.applied()


def compose_runtime_fixture(
    base: ApplicationFixture,
    *,
    model: ScriptedMockLLM,
    coordinator: RuntimeCoordinator,
    workers: RuntimeWorkerLoop,
    reservation_git: TargetReservationGitPort,
    monotonic_clock: MonotonicClock,
    reservation_observer: TargetReservationObserver,
) -> RuntimeApplicationFixture:
    reservation_effects = TargetReservationAdmissionService(reservation_observer, reservation_git)
    target_driver = TargetReservationDriverService(
        TargetReservationBootstrapAdmissionService(
            state=base.store,
            observer=reservation_observer,
            effects=reservation_effects,
        )
    )
    phase_drivers = RuntimePhaseDriverService(
        recovered_actions=RecoveredActionRouter(coordinator, workers),
        target_reservations=target_driver,
        private_refs=FailClosedPrivateRefDriver(),
        resolution=FailClosedResolutionDriver(),
        integration=FailClosedFinalIntegrationDriver(),
        cleanup=FailClosedTerminalCleanupDriver(),
    )
    ownership = InMemoryRunOwnership()
    runtime = RuntimeService(
        store=base.store,
        ownership=ownership,
        journal=base.store,
        authority=AuthorityService(journal=base.store),
        recovery=NoEffectRecoveryService(base.store),
        coordinator=coordinator,
        model_client=DurableModelClient(model=model, journal=base.store),
        tools=StaticToolSchemaProvider(),
        phase_drivers=phase_drivers,
    )
    if base.run_id is None:
        raise AssertionError("runtime fixture requires a control-created Run")
    spawner = RecordingGitSpawner()
    return RuntimeApplicationFixture(
        root=base.root,
        store=base.store,
        control=RuntimeAwareControl(base.control, base.store, ownership),
        queries=base.queries,
        runtime=runtime,
        model=model,
        git_spawner=spawner,
        ownership=ownership,
        monotonic_clock=monotonic_clock,
        run_id=base.run_id,
    )


@dataclass
class ExactReservationObserver:
    calls: int = 0

    def observe(self, reservation: TargetReservation) -> ReservationObservation:
        self.calls += 1
        if self.calls <= 2:
            return ReservationObservation(False, False, False, False, False)
        return ReservationObservation(
            True,
            True,
            self.calls >= 4,
            True,
            True,
            admin_entry_name=reservation.reservation_id,
            admin_binding_digest="sha256:" + "b" * 64,
        )


def create_approved_draft(app: RuntimeApplicationFixture) -> RunId:
    return app.run_id


def make_runtime_application(
    tmp_path: Path,
    *,
    model: ScriptedMockLLM | None = None,
    coordinator_factory: Callable[
        [RuntimeStateStore], RuntimeCoordinator
    ] = FixtureStoppingCoordinator,
    reservation_git: TargetReservationGitPort | None = None,
    budget: BudgetRevisionDocument | None = None,
    monotonic_clock: MonotonicClock | None = None,
    reservation_observer: TargetReservationObserver | None = None,
) -> RuntimeApplicationFixture:
    runtime_clock = SystemMonotonicClock() if monotonic_clock is None else monotonic_clock
    base = make_application(tmp_path, monotonic_clock=runtime_clock)
    run_id = create_draft_with_three_proposals(base, budget=budget)
    approve_current_policy_budget_and_model(base, run_id)
    base.bind_run(run_id)
    return compose_runtime_fixture(
        base,
        model=ScriptedMockLLM([]) if model is None else model,
        coordinator=coordinator_factory(base.store),
        workers=FixtureWorkerLoop(),
        reservation_git=FixtureReservationGit() if reservation_git is None else reservation_git,
        monotonic_clock=runtime_clock,
        reservation_observer=(
            ExactReservationObserver() if reservation_observer is None else reservation_observer
        ),
    )


def make_continue_command(
    app: RuntimeApplicationFixture,
    run_id: RunId,
    *,
    request_id: str,
) -> CommandEnvelope:
    return _envelope(
        request_id,
        app.store.audit_sequence(run_id),
        app.store.current_revision_digests(run_id),
        ContinuePayload(run_id=run_id),
    )


def make_permitted_draft_runtime(
    tmp_path: Path,
    *,
    model: ScriptedMockLLM | None = None,
    coordinator_factory: Callable[
        [RuntimeStateStore], RuntimeCoordinator
    ] = FixtureStoppingCoordinator,
    reservation_git: TargetReservationGitPort | None = None,
    monotonic_clock: MonotonicClock | None = None,
    reservation_observer: TargetReservationObserver | None = None,
) -> RuntimeApplicationFixture:
    app = make_runtime_application(
        tmp_path,
        model=model,
        coordinator_factory=coordinator_factory,
        reservation_git=reservation_git,
        monotonic_clock=monotonic_clock,
        reservation_observer=reservation_observer,
    )
    command = make_begin_planning_command(app, app.run_id, request_id="begin-draft")
    assert app.control.handle(command).status == "ACCEPTED"
    return replace(app, begin_command=command)


def make_permitted_planning_application(
    tmp_path: Path,
    *,
    model: ScriptedMockLLM,
    coordinator_factory: Callable[
        [RuntimeStateStore], RuntimeCoordinator
    ] = FixtureStoppingCoordinator,
    monotonic_clock: MonotonicClock | None = None,
) -> RuntimeApplicationFixture:
    app = make_permitted_draft_runtime(
        tmp_path,
        model=model,
        coordinator_factory=coordinator_factory,
        monotonic_clock=monotonic_clock,
    )
    assert app.runtime.run_until_blocked(app.run_id).reason == RunStopReason.PAUSED
    assert app.store.run_record(app.run_id).state == RunState.PLANNING
    command = make_continue_command(app, app.run_id, request_id="continue-planning")
    assert app.control.handle(command).status == "ACCEPTED"
    return replace(app, begin_command=command)


def seed_runtime_active_state_for_test(app: RuntimeApplicationFixture, *, model_calls: int) -> None:
    expected = app.store.audit_sequence(app.run_id)

    def mutate(connection: sqlite3.Connection) -> None:
        if (
            connection.execute(
                "UPDATE runs SET state = 'ACTIVE' WHERE run_id = ?", (app.run_id,)
            ).rowcount
            != 1
        ):
            raise AssertionError("fixture Run missing")
        connection.execute(
            "INSERT INTO model_counters(run_id, calls, input_tokens, output_tokens, cost_usd) "
            "VALUES (?, ?, 0, 0, '0') ON CONFLICT(run_id) DO UPDATE SET calls = excluded.calls, "
            "input_tokens = excluded.input_tokens, output_tokens = excluded.output_tokens, "
            "cost_usd = excluded.cost_usd",
            (app.run_id, model_calls),
        )

    app.store._commit_state_and_event(
        run_id=app.run_id,
        expected_sequence=expected,
        event=AuditEvent.kind("TEST_RUNTIME_ACTIVE_STATE_SEEDED"),
        mutate=mutate,
    )


def make_permitted_active_runtime(
    tmp_path: Path,
    *,
    model_calls: int,
    model_call_ceiling: int,
    coordinator_factory: Callable[[RuntimeStateStore], RuntimeCoordinator],
    active_run_seconds_ceiling: int = 28_800,
    monotonic_clock: MonotonicClock | None = None,
) -> RuntimeApplicationFixture:
    budget = fixture_budget(
        active_run_seconds_ceiling=active_run_seconds_ceiling,
        model_call_ceiling=model_call_ceiling,
    )
    app = make_runtime_application(
        tmp_path,
        coordinator_factory=coordinator_factory,
        budget=budget,
        monotonic_clock=monotonic_clock,
    )
    seed_runtime_active_state_for_test(app, model_calls=model_calls)
    command = make_continue_command(app, app.run_id, request_id="continue-active")
    assert app.control.handle(command).status == "ACCEPTED"
    return replace(app, begin_command=command)


@contextmanager
def hold_runtime_owner(app: RuntimeApplicationFixture, run_id: RunId) -> Iterator[None]:
    with app.ownership.acquire(run_id) as owner:
        assert owner is not None
        yield


class CrashAfterPermitCoordinator(FixtureStoppingCoordinator):
    def __init__(self, store: RuntimeStateStore) -> None:
        super().__init__(store)
        self._planning_calls = 0

    def run_planning_turn(self, run_id: RunId) -> RuntimeDecision:
        self._planning_calls += 1
        if self._planning_calls == 1:
            return super().run_planning_turn(run_id)
        raise InjectedProcessCrash()


def crash_after_permit_consumption_application(
    tmp_path: Path,
) -> RuntimeApplicationFixture:
    return make_permitted_planning_application(
        tmp_path,
        model=ScriptedMockLLM([]),
        coordinator_factory=CrashAfterPermitCoordinator,
    )


def open_runtime_application(
    root: Path,
    run_id: RunId,
    *,
    model: ScriptedMockLLM,
    monotonic_clock: MonotonicClock,
) -> RuntimeApplicationFixture:
    store = SqliteStateStore(root / "state.db", monotonic_clock=monotonic_clock)
    target_authority = FixtureTargetAuthorityDigestService(store)
    base = ApplicationFixture(
        root=root,
        database=root / "state.db",
        store=store,
        control=CrewControlService(
            ControlCommandService(
                state=store,
                target_authority=target_authority,
                repository_authority=FixtureRepositoryBootstrapAuthorityService(),
            )
        ),
        queries=RunQueryService(ProjectionService(store)),
        target_authority_digest=target_authority.current_for(run_id),
    )
    base.bind_run(run_id)
    return compose_runtime_fixture(
        base,
        model=model,
        coordinator=FixtureStoppingCoordinator(store),
        workers=FixtureWorkerLoop(),
        reservation_git=FixtureReservationGit(),
        monotonic_clock=monotonic_clock,
        reservation_observer=ExactReservationObserver(),
    )


def seed_unreleased_committed_completion(
    store: SqliteStateStore,
    *,
    run_id: RunId,
    owner_kind: Literal["PLANNING", "WORKER"],
    normalized_action: dict[str, object],
) -> CommittedModelTurn:
    bindings = store.current_revision_digests(run_id)
    assert bindings.policy_digest is not None
    assert bindings.budget_digest is not None
    assert bindings.model_configuration_digest is not None
    request = ModelRequest(
        run_id=run_id,
        plan_digest=bindings.plan_digest,
        policy_digest=bindings.policy_digest,
        budget_digest=bindings.budget_digest,
        model_configuration_digest=bindings.model_configuration_digest,
        requested_model_id="mock-model",
        allowed_model_ids=frozenset({"mock-model"}),
        prompt=({"role": "user", "content": "finish"},),
        tool_schema_digest=FIXTURE_TOOL_SCHEMA_DIGEST,
        request_digest="sha256:" + "6" * 64,
        idempotency_key=f"fixture-recovery:{run_id}:{store.audit_sequence(run_id)}",
        max_input_tokens=100,
        max_output_tokens=20,
        reserved_cost_usd=Decimal("0.001"),
        owner_kind=owner_kind,
        task_id=None,
        attempt_id=None,
        tranche_id=None,
    )
    completion = ModelCompletion(
        response_id="fixture-response",
        requested_model_id="mock-model",
        returned_model_id="mock-model",
        usage=ModelUsage(10, 2, Decimal("0.000012")),
        normalized_action=normalized_action,
    )
    result = DurableModelClient(
        model=ScriptedMockLLM(
            [ScriptedModelStep.for_request(request, ProviderAttemptResult.completed(completion))]
        ),
        journal=store,
    ).complete(request)
    committed = store.committed_model_turn(run_id, result.logical_turn_id)
    assert committed is not None
    return committed
