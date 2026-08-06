"""Production application composition root."""

from __future__ import annotations

import os
import shutil
from collections.abc import Mapping
from contextvars import ContextVar
from decimal import Decimal
from pathlib import Path
from typing import cast

from apexcrew.adapters.credentials.keyring import (
    KeyringSecretPolicyStore,
    SecretPolicyConfigurationError,
)
from apexcrew.adapters.credentials.model_key import ModelCredentialPort
from apexcrew.adapters.model.deepseek_responses import ClientFactory
from apexcrew.adapters.model.factory import build_model_port
from apexcrew.adapters.model.scripted import ScriptedMockLLM
from apexcrew.adapters.repository.bootstrap import (
    RepositoryBootstrapAuthorityService as RepositoryBootstrapAdapter,
)
from apexcrew.adapters.repository.bootstrap import (
    repository_binding,
)
from apexcrew.adapters.repository.git import (
    GitCommandRunner,
    GitPrivateRefStartGuard,
    GitRepositoryPreflight,
    GitTargetReservationRepository,
    NoFollowTargetReservationWorktreeGuard,
    RepositoryInstance,
)
from apexcrew.adapters.repository.granted_workspace import GrantedWorkspaceAdapter
from apexcrew.adapters.repository.no_follow import StableHandleTree
from apexcrew.adapters.repository.no_follow_posix import PosixNoFollowBackend
from apexcrew.adapters.repository.no_follow_windows import WindowsNoFollowBackend
from apexcrew.adapters.repository.planning import GitPlanningSnapshotReader
from apexcrew.adapters.repository.snapshot import FilesystemRepositorySnapshot
from apexcrew.adapters.repository.target_cas import GitTargetCasAdapter
from apexcrew.adapters.state.sqlite import SqliteStateStore
from apexcrew.adapters.system import ReservationPathInspector, SystemMonotonicClock
from apexcrew.application.configuration import RevisionDocuments, default_revision_documents
from apexcrew.application.control import (
    ControlCommandService,
    CrewControlService,
    TargetAuthorityDigestService,
)
from apexcrew.application.control import (
    RepositoryBootstrapAuthorityService as RepositoryBootstrapPort,
)
from apexcrew.application.queries import RunQueryService
from apexcrew.application.runtime import (
    FileRunOwnership,
    GrantedActionRuntime,
    LocalFileLockBackend,
    PrivateRefInitializer,
    ProcessRuntimeOwnerIds,
    RecoveredActionRouter,
    ResolutionObservationRegistry,
    ResolutionRuntime,
    RuntimePhaseDriverService,
    RuntimeService,
    TargetReservationDriver,
    TerminalCleanupRuntime,
)
from apexcrew.domain.admission import (
    RuntimeStartBinding,
    StartGuardDecision,
    TargetReservationAdmissionService,
    TargetReservationBootstrapAdmissionService,
    TargetReservationObservationService,
)
from apexcrew.domain.authority import (
    NO_PROGRESS,
    AuthorityService,
    GrantedActionIntent,
    SystemUtcClock,
    TaskAuthority,
)
from apexcrew.domain.commands import ApplicableRevisionDigests, RuntimeDecision, RuntimePermit
from apexcrew.domain.coordination import (
    AuthorityModelClient,
    BoundedPlanningContextBuilder,
    BoundedPlanningReadGateway,
    CoordinatorService,
    PlanningActionApplier,
    PlanningAuthorization,
    PlanningManifest,
    PlanningReadGateway,
    PlanningReadIntent,
    PlanningReadResult,
    PlanningTurnBinding,
    TaskDispatchSelection,
    planning_snapshot_digest,
)
from apexcrew.domain.effects import (
    RecoveryService,
    RunRecord,
    StateConflict,
    TargetReservation,
    canonical_json,
    sha256_digest,
)
from apexcrew.domain.evidence import ContextCapsule
from apexcrew.domain.model import DurableModelClient, ModelRequest, RecoveredModelAction
from apexcrew.domain.plan import CanonicalPath
from apexcrew.domain.policy import PlanningPathPolicy, SecretPathPolicy
from apexcrew.domain.projection import ProjectionService
from apexcrew.domain.revisions import (
    BudgetRevisionDocument,
    ModelConfigurationRevisionDocument,
    PlanningReadAuthorizationDocument,
    PolicyRevisionDocument,
    Sha256DigestText,
)
from apexcrew.domain.tools import (
    ActionPreState,
    GrantedActionObservation,
    ScopedToolRuntime,
    ToolIntent,
    ToolResult,
)
from apexcrew.domain.types import (
    AttemptId,
    AuditSequence,
    IntentId,
    RepositoryId,
    RevisionDigest,
    RunId,
    TaskId,
)
from apexcrew.domain.worker import WorkerActionCodec, WorkerLoopService


class _TargetAuthority(TargetAuthorityDigestService):
    def __init__(self, store: SqliteStateStore) -> None:
        self._store = store

    def current_for(self, run_id: RunId) -> Sha256DigestText:
        return self._store.target_authority_digest(run_id)


class _CompositionWorkerContext:
    def __init__(self, store: SqliteStateStore) -> None:
        self._store = store

    def build_current(self, attempt_id: AttemptId) -> ContextCapsule:
        binding = self._store.current_worker_turn_binding(attempt_id)
        return ContextCapsule.create(
            run_id=str(binding.run_id),
            task_id=str(binding.task_id),
            revision_digest=str(binding.plan_digest),
            dependencies=(str(binding.dependency_fingerprint_basis),),
            content="ApexCrew worker context is bounded to the persisted attempt binding.",
        )


class _CompositionWorkerRequests:
    def __init__(
        self,
        store: SqliteStateStore,
        model_configuration: ModelConfigurationRevisionDocument,
        budget: BudgetRevisionDocument,
    ) -> None:
        self._store = store
        self.requested_model_id = model_configuration.requested_model_id
        self.allowed_model_ids = frozenset(
            alias.returned_model_id for alias in model_configuration.returned_model_aliases
        )
        self.max_input_tokens = model_configuration.inference_settings.max_input_tokens
        self.max_output_tokens = model_configuration.inference_settings.max_output_tokens
        self.reserved_cost_usd = _worst_case_reservation(model_configuration, budget)

    def for_attempt(self, attempt_id: AttemptId, capsule: object) -> ModelRequest:
        binding = self._store.current_worker_turn_binding(attempt_id)
        model_configuration = cast(
            ModelConfigurationRevisionDocument,
            self._store.current_revision_document(binding.run_id, "MODEL_CONFIGURATION"),
        )
        budget = cast(
            BudgetRevisionDocument,
            self._store.current_revision_document(binding.run_id, "BUDGET"),
        )
        allowed_model_ids = frozenset(
            alias.returned_model_id for alias in model_configuration.returned_model_aliases
        )
        content = capsule.content if isinstance(capsule, ContextCapsule) else str(capsule)
        digest = sha256_digest(canonical_json({"attempt_id": str(attempt_id), "content": content}))
        return ModelRequest(
            run_id=binding.run_id,
            plan_digest=RevisionDigest(str(binding.plan_digest)),
            policy_digest=RevisionDigest(str(binding.policy_digest)),
            budget_digest=RevisionDigest(str(binding.budget_digest)),
            model_configuration_digest=RevisionDigest(str(binding.model_configuration_digest)),
            requested_model_id=model_configuration.requested_model_id,
            allowed_model_ids=allowed_model_ids,
            prompt=({"role": "user", "content": content},),
            tool_schema_digest=str(binding.tool_schema_digest),
            request_digest=digest,
            idempotency_key=f"worker-request:{attempt_id}:{digest}",
            max_input_tokens=model_configuration.inference_settings.max_input_tokens,
            max_output_tokens=model_configuration.inference_settings.max_output_tokens,
            reserved_cost_usd=_worst_case_reservation(model_configuration, budget),
            owner_kind="WORKER",
            task_id=binding.task_id,
            attempt_id=binding.attempt_id,
            tranche_id=binding.tranche_id,
        )


class _CompositionPlanningPathGate:
    def __init__(self, policy: PlanningPathPolicy) -> None:
        self._policy = policy

    def require_allowed(self, path: CanonicalPath, authorization: object) -> None:
        self._policy.require_allowed(path, cast(PlanningReadAuthorizationDocument, authorization))

    def require_manifest_allowed(self, path: CanonicalPath, binding: PlanningTurnBinding) -> None:
        self._policy.require_manifest_allowed(path, binding)


class _CompositionPlanningRevisionContext:
    """Carries the planning run identity across the existing manifest port."""

    def __init__(self, store: SqliteStateStore) -> None:
        self._store = store
        self._run_id: ContextVar[RunId | None] = ContextVar("planning_run_id", default=None)

    def bind(self, run_id: RunId) -> None:
        self._run_id.set(run_id)

    def run(self) -> RunRecord:
        run_id = self._run_id.get()
        if run_id is None:
            raise RuntimeError("PLANNING_RUN_NOT_BOUND")
        return self._store.run_record(run_id)

    def policy(self) -> PlanningReadAuthorizationDocument:
        policy = self._store.current_revision_document(self.run().run_id, "POLICY")
        return cast(PolicyRevisionDocument, policy).planning_read_authorization


class _CompositionRepositoryResources:
    """Lazily owns one preflight-bound repository and its internal Git adapters."""

    def __init__(
        self,
        root: Path,
        authority: RepositoryBootstrapPort,
        data_root: Path,
    ) -> None:
        self._root = root
        self._authority = authority
        self._data_root = data_root
        self._repository: RepositoryInstance | None = None
        self._runner: GitCommandRunner | None = None
        self._owns_runner = False
        self._data_handles: StableHandleTree | None = None
        self._validated_binding: tuple[RepositoryId, Sha256DigestText] | None = None
        self._allowed_worktree_admin_entries: set[str] = set()

    def _allow_reservation_worktree(self, reservation_id: str) -> None:
        self._allowed_worktree_admin_entries.add(reservation_id)

    def _ensure(self) -> tuple[RepositoryInstance, GitCommandRunner, StableHandleTree]:
        if self._repository is None:
            if isinstance(self._authority, RepositoryBootstrapAdapter):
                self._repository = self._authority.open_repository(
                    self._root,
                    allowed_worktree_admin_entries=tuple(
                        sorted(self._allowed_worktree_admin_entries)
                    ),
                )
                self._runner = self._authority.git_runner
            else:
                self._repository = GitRepositoryPreflight().inspect(
                    self._root,
                    allowed_worktree_admin_entries=tuple(
                        sorted(self._allowed_worktree_admin_entries)
                    ),
                )
                executable = shutil.which("git")
                if executable is None:
                    self._repository.close()
                    self._repository = None
                    raise RuntimeError("GIT_EXECUTABLE_UNAVAILABLE")
                self._runner = GitCommandRunner(Path(executable).resolve())
                self._owns_runner = True
        if self._runner is None or self._repository is None:
            raise RuntimeError("REPOSITORY_RUNTIME_NOT_INITIALIZED")
        if self._data_handles is None:
            self._data_root.mkdir(parents=True, exist_ok=True)
            (self._data_root / "reservations").mkdir(exist_ok=True)
            backend = WindowsNoFollowBackend() if os.name == "nt" else PosixNoFollowBackend()
            self._data_handles = StableHandleTree(self._data_root, backend)
        return self._repository, self._runner, self._data_handles

    def validate_repository_binding(
        self,
        repository_id: RepositoryId,
        repository_instance_digest: Sha256DigestText,
    ) -> None:
        repository, _, _ = self._ensure()
        if self._validated_binding is not None:
            repository.assert_stable()
            if self._validated_binding != (repository_id, repository_instance_digest):
                raise RuntimeError("REPOSITORY_BINDING_MISMATCH")
            return
        actual_repository_id, actual_instance_digest = repository_binding(
            repository,
            allowed_worktree_admin_entries=tuple(sorted(self._allowed_worktree_admin_entries)),
        )
        if (
            actual_repository_id != repository_id
            or actual_instance_digest != repository_instance_digest
        ):
            repository.close()
            self._repository = None
            raise RuntimeError("REPOSITORY_BINDING_MISMATCH")
        self._validated_binding = (repository_id, repository_instance_digest)

    def planning_reader(
        self,
        repository_id: RepositoryId,
        repository_instance_digest: Sha256DigestText,
        policy: PlanningPathPolicy,
    ) -> GitPlanningSnapshotReader:
        self.validate_repository_binding(repository_id, repository_instance_digest)
        repository, runner, _ = self._ensure()
        return GitPlanningSnapshotReader(
            repository,
            repository_id,
            runner,
            _CompositionPlanningPathGate(policy),
        )

    def refresh(self) -> None:
        if self._repository is None:
            return
        self._repository = self._repository.refresh_after_verified_owned_transition()

    def reservation_adapters(
        self,
        reservation: TargetReservation,
        repository_id: RepositoryId,
        repository_instance_digest: Sha256DigestText,
        target_authority_digest: Sha256DigestText,
    ) -> tuple[TargetReservationObservationService, GitTargetReservationRepository]:
        self._allow_reservation_worktree(reservation.reservation_id)
        self.validate_repository_binding(repository_id, repository_instance_digest)
        repository, runner, data_handles = self._ensure()
        guard = NoFollowTargetReservationWorktreeGuard(
            repository,
            self._data_root,
            data_handles,
            repository.root,
        )
        git = GitTargetReservationRepository(
            repository=repository,
            repository_id=repository_id,
            repository_instance_digest=repository_instance_digest,
            runner=runner,
            worktree_guard=guard,
            data_root=self._data_root,
            target_authority_digest=target_authority_digest,
        )
        backend = WindowsNoFollowBackend() if os.name == "nt" else PosixNoFollowBackend()
        path_reader = ReservationPathInspector(
            self._data_root,
            lambda item: (
                b"gitdir: "
                + os.fsencode(
                    (repository.root / ".git" / "worktrees" / item.reservation_id).as_posix()
                )
                + b"\n"
            ),
            backend,
        )
        return TargetReservationObservationService(git, path_reader), git

    def private_ref_guard(
        self,
        *,
        reservation: TargetReservation,
        repository_id: RepositoryId,
        repository_instance_digest: Sha256DigestText,
        target_safety_digest: Sha256DigestText,
    ) -> GitPrivateRefStartGuard:
        self._allow_reservation_worktree(reservation.reservation_id)
        repository, runner, _ = self._ensure()
        observer, git = self.reservation_adapters(
            reservation,
            repository_id,
            repository_instance_digest,
            target_safety_digest,
        )
        del git
        return GitPrivateRefStartGuard(
            repository=repository,
            repository_id=repository_id,
            repository_instance_digest=repository_instance_digest,
            runner=runner,
            reservation_observer=observer,
            reservation=reservation,
            target_safety_digest=target_safety_digest,
            reflog_message=f"apexcrew private run {reservation.run_id}",
        )

    def target_cas(
        self,
        reservation: TargetReservation,
        repository_id: RepositoryId,
        repository_instance_digest: Sha256DigestText,
    ) -> GitTargetCasAdapter:
        self.validate_repository_binding(repository_id, repository_instance_digest)
        repository, runner, data_handles = self._ensure()
        guard = NoFollowTargetReservationWorktreeGuard(
            repository,
            self._data_root,
            data_handles,
            repository.root,
        )
        return GitTargetCasAdapter(repository, runner, guard, reservation)

    def close(self) -> None:
        first_error: BaseException | None = None
        if self._data_handles is not None:
            try:
                self._data_handles.close()
            except BaseException as error:  # noqa: BLE001
                first_error = error
            self._data_handles = None
        if self._repository is not None:
            try:
                self._repository.close()
            except BaseException as error:  # noqa: BLE001
                if first_error is None:
                    first_error = error
            self._repository = None
            self._validated_binding = None
        if self._owns_runner and self._runner is not None:
            try:
                self._runner.close()
            except BaseException as error:  # noqa: BLE001
                if first_error is None:
                    first_error = error
        self._runner = None
        if first_error is not None:
            raise first_error


class _CompositionWorkerTools(ScopedToolRuntime):
    def __init__(
        self,
        store: SqliteStateStore,
        resources: _CompositionRepositoryResources,
        secret_policy: SecretPathPolicy,
    ) -> None:
        self._store = store
        self._resources = resources
        self._secret_policy = secret_policy
        zero = Sha256DigestText("sha256:" + "0" * 64)
        super().__init__(
            snapshot=FilesystemRepositorySnapshot(resources._root),
            read_globs=("**",),
            secret_paths=secret_policy,
            authorization_binding_digest=zero,
            applicable_revision_digests=ApplicableRevisionDigests(),
            repository_id="composition-placeholder",
            snapshot_digest=zero,
            scope_digest=zero,
            dependency_fingerprint_basis=zero,
        )

    def _runtime(self, intent: ToolIntent | GrantedActionIntent) -> ScopedToolRuntime:
        if isinstance(intent, ToolIntent):
            attempt_id = intent.attempt_id
            authorization_binding_digest = intent.authorization_binding_digest
        else:
            attempt_id = intent.bindings.attempt_id
            authorization_binding_digest = intent.bindings.authorization_binding_digest
        if attempt_id is None:
            raise RuntimeError("WORKER_ATTEMPT_BINDING_MISSING")
        binding = self._store.current_worker_turn_binding(attempt_id)
        lease = self._store.workspace_lease(binding.run_id, binding.lease_id)
        if lease is None:
            raise RuntimeError("WORKER_LEASE_NOT_FOUND")
        reservation = self._store.target_reservation_for_run(binding.run_id)
        self._resources.validate_repository_binding(
            RepositoryId(binding.repository_id),
            self._store.run_record(binding.run_id).repository_instance_digest,
        )
        contract = next(
            (
                item
                for item in self._store.task_contracts(binding.plan_digest)
                if item.task_id == str(binding.task_id)
            ),
            None,
        )
        if contract is None:
            raise RuntimeError("WORKER_TASK_CONTRACT_NOT_FOUND")
        return ScopedToolRuntime(
            snapshot=FilesystemRepositorySnapshot(reservation.path),
            read_globs=tuple(pattern.value for pattern in contract.read_globs),
            secret_paths=self._secret_policy,
            authorization_binding_digest=Sha256DigestText(str(authorization_binding_digest)),
            applicable_revision_digests=binding.applicable_revision_digests,
            repository_id=binding.repository_id,
            snapshot_digest=binding.snapshot_digest,
            scope_digest=binding.scope_digest,
            dependency_fingerprint_basis=binding.dependency_fingerprint_basis,
            denial_journal=self._store,
            denial_expected_sequence=self._store.audit_sequence(binding.run_id),
            workspace_lease=lease,
            granted_workspace=GrantedWorkspaceAdapter(reservation.path, self._secret_policy),
        )

    def execute(self, intent: ToolIntent) -> ToolResult:
        return self._runtime(intent).execute(intent)

    def observe_granted_action(self, intent: GrantedActionIntent) -> GrantedActionObservation:
        return self._runtime(intent).observe_granted_action(intent)

    def execute_granted(self, intent: GrantedActionIntent) -> ToolResult:
        return self._runtime(intent).execute_granted(intent)


class _CompositionPlanningAuthorization:
    def __init__(
        self,
        store: SqliteStateStore,
        secret_policy: SecretPathPolicy | None = None,
        revisions: _CompositionPlanningRevisionContext | None = None,
    ) -> None:
        self._store = store
        self._provided_secret_policy = secret_policy
        self._revisions = (
            _CompositionPlanningRevisionContext(store) if revisions is None else revisions
        )

    @staticmethod
    def _secret_policy() -> SecretPathPolicy:
        try:
            key, rules = KeyringSecretPolicyStore().load()
        except SecretPolicyConfigurationError as error:
            raise RuntimeError(str(error)) from error
        return SecretPathPolicy.from_host_rules(rules, key)

    def _effective_secret_policy(self) -> SecretPathPolicy:
        return (
            self._provided_secret_policy
            if self._provided_secret_policy is not None
            else self._secret_policy()
        )

    def current(self, run_id: RunId) -> PlanningAuthorization:
        return self._build(
            run_id, planning_request_count=self._store.planning_request_count(run_id)
        )

    def current_for_recovery(
        self, run_id: RunId, action: RecoveredModelAction
    ) -> PlanningAuthorization:
        del action
        return self._build(
            run_id,
            planning_request_count=max(0, self._store.planning_request_count(run_id) - 1),
        )

    def _build(self, run_id: RunId, planning_request_count: int) -> PlanningAuthorization:
        self._revisions.bind(run_id)
        run = self._store.run_record(run_id)
        reservation = self._store.target_reservation_for_run(run_id)
        current = self._store.current_revision_digests(run_id)
        approved = set(self._store.approved_revision_classes(run_id))
        required = {"POLICY", "BUDGET", "MODEL_CONFIGURATION"}
        if (
            not required.issubset(approved)
            or current.policy_digest is None
            or current.budget_digest is None
            or current.model_configuration_digest is None
        ):
            return PlanningAuthorization(
                run_id=run_id,
                decision="PAUSE",
                reason="PLANNING_REVISIONS_NOT_APPROVED",
                applicable_revision_digests=current,
                target_safety_digest=self._store.target_authority_digest(run_id),
                credential_profile=None,
                read_authorization=None,
                turn_binding=None,
                planning_request_count=planning_request_count,
                planning_request_ceiling=8,
            )
        try:
            secret_policy = self._effective_secret_policy()
        except RuntimeError:
            return PlanningAuthorization(
                run_id=run_id,
                decision="PAUSE",
                reason="SECRET_POLICY_CONFIGURATION_MISSING",
                applicable_revision_digests=current,
                target_safety_digest=self._store.target_authority_digest(run_id),
                credential_profile=None,
                read_authorization=None,
                turn_binding=None,
                planning_request_count=planning_request_count,
                planning_request_ceiling=8,
            )
        policy = self._store.current_revision_document(run_id, "POLICY")
        budget = self._store.current_revision_document(run_id, "BUDGET")
        model = self._store.current_revision_document(run_id, "MODEL_CONFIGURATION")
        assert hasattr(policy, "planning_read_authorization")
        assert hasattr(budget, "planning_request_ceiling")
        assert hasattr(model, "provider")
        read_authorization = policy.planning_read_authorization
        scope_digest = Sha256DigestText(
            PlanningPathPolicy(read_authorization, secret_policy).scope_digest
        )
        binding = PlanningTurnBinding(
            repository_id=run.repository_id,
            pinned_base_oid=run.pinned_target_oid,
            scope_digest=scope_digest,
            snapshot_digest=planning_snapshot_digest(
                run.repository_id,
                run.pinned_target_oid,
                scope_digest,
            ),
        )
        return PlanningAuthorization(
            run_id=run_id,
            decision="ALLOW"
            if planning_request_count < budget.planning_request_ceiling
            else "PAUSE",
            reason=(
                None
                if planning_request_count < budget.planning_request_ceiling
                else "PLANNING_REQUEST_LIMIT"
            ),
            applicable_revision_digests=current,
            target_safety_digest=(
                reservation.admin_binding_digest or self._store.target_authority_digest(run_id)
            ),
            credential_profile=(
                "deepseek" if model.provider == "deepseek_responses" else "scripted_mock"
            ),
            read_authorization=read_authorization
            if planning_request_count < budget.planning_request_ceiling
            else None,
            turn_binding=binding
            if planning_request_count < budget.planning_request_ceiling
            else None,
            planning_request_count=planning_request_count,
            planning_request_ceiling=budget.planning_request_ceiling,
        )


class _CompositionPlanningManifests:
    def __init__(
        self,
        resources: _CompositionRepositoryResources,
        revisions: _CompositionPlanningRevisionContext,
        secret_policy: SecretPathPolicy | None = None,
    ) -> None:
        self._resources = resources
        self._revisions = revisions
        self._secret_policy = secret_policy

    def manifest(self, binding: PlanningTurnBinding) -> PlanningManifest:
        run = self._revisions.run()
        return self._resources.planning_reader(
            binding.repository_id,
            run.repository_instance_digest,
            PlanningPathPolicy(
                self._revisions.policy(),
                _secret_policy_for_runtime(self._secret_policy),
            ),
        ).manifest(binding)


class _CompositionPlanningReads(PlanningReadGateway):
    def __init__(
        self,
        store: SqliteStateStore,
        resources: _CompositionRepositoryResources,
        secret_policy: SecretPathPolicy | None = None,
    ) -> None:
        self._store = store
        self._resources = resources
        self._secret_policy = secret_policy

    def execute(
        self, intent: PlanningReadIntent, authorization: PlanningAuthorization
    ) -> PlanningReadResult:
        read_authorization = authorization.read_authorization
        if read_authorization is None:
            raise ValueError("PLANNING_READ_AUTHORIZATION_NOT_ALLOWED")
        run = self._store.run_record(intent.run_id)
        reader = self._resources.planning_reader(
            intent.repository_id,
            run.repository_instance_digest,
            PlanningPathPolicy(
                read_authorization,
                _secret_policy_for_runtime(self._secret_policy),
            ),
        )
        return BoundedPlanningReadGateway(reader).execute(intent, authorization)


class _ProductionTargetReservationDriver(TargetReservationDriver):
    def __init__(self, store: SqliteStateStore, resources: _CompositionRepositoryResources) -> None:
        self._store = store
        self._resources = resources

    def initialize(self, run_id: RunId, permit: RuntimePermit) -> RuntimeDecision:
        run = self._store.run_record(run_id)
        reservation = self._store.target_reservation_for_run(run_id)
        observer, git = self._resources.reservation_adapters(
            reservation,
            run.repository_id,
            run.repository_instance_digest,
            self._store.target_authority_digest(run_id),
        )
        admission = TargetReservationBootstrapAdmissionService(
            state=self._store,
            observer=observer,
            effects=TargetReservationAdmissionService(observer, git),
        )
        decision = TargetReservationDriver(admission).initialize(run_id, permit)
        if decision.phase_transition == "TARGET_RESERVATION_INITIALIZED":
            self._resources.refresh()
        return decision


class _ProductionPrivateRefDriver:
    def __init__(self, store: SqliteStateStore, resources: _CompositionRepositoryResources) -> None:
        self._store = store
        self._resources = resources

    def initialize(self, run_id: RunId, permit: RuntimePermit) -> RuntimeDecision:
        run = self._store.run_record(run_id)
        reservation = self._store.target_reservation_for_run(run_id)
        guard = self._resources.private_ref_guard(
            reservation=reservation,
            repository_id=run.repository_id,
            repository_instance_digest=run.repository_instance_digest,
            target_safety_digest=self._store.target_authority_digest(run_id),
        )
        decision = PrivateRefInitializer(self._store, guard, guard).initialize(run_id, permit)
        if decision.phase_transition == "PRIVATE_REF_INITIALIZED":
            self._resources.refresh()
        return decision


class _CompositionIntegrationDriver:
    def __init__(self, store: SqliteStateStore, resources: _CompositionRepositoryResources) -> None:
        self._store = store
        self._resources = resources

    def integrate(self, run_id: RunId, permit: RuntimePermit) -> RuntimeDecision:
        if permit.state != "CONSUMED" or permit.consumed_owner_id is None:
            return RuntimeDecision.pause("FINAL_INTEGRATION_PERMIT_INVALID")
        candidate = self._store.final_candidate(run_id)
        prepared_oid = self._store.final_candidate_prepared_oid(run_id)
        run = self._store.run_record(run_id)
        result = self._resources.target_cas(
            self._store.target_reservation_for_run(run_id),
            run.repository_id,
            run.repository_instance_digest,
        ).apply(
            target_ref=candidate.target_ref,
            expected_old_oid=candidate.head_oid,
            prepared_oid=prepared_oid,
            reflog_message=f"apexcrew integrate {run_id}",
        )
        sequence = self._store.settle_final_integration(
            run_id=run_id,
            owner_id=permit.consumed_owner_id,
            permit_generation=permit.generation,
            candidate_id=candidate.candidate_id,
            result_class=result.result_class,
            observed_oid=result.observed_oid,
            expected_sequence=self._store.audit_sequence(run_id),
        )
        if result.result_class == "APPLIED":
            return RuntimeDecision.continued(sequence)
        return RuntimeDecision.pause(
            "INDETERMINATE"
            if result.result_class == "UNOBSERVABLE"
            else "FINAL_INTEGRATION_CONFLICT",
            sequence,
        )


class _CompositionReservationCleanup:
    def __init__(self, store: SqliteStateStore, resources: _CompositionRepositoryResources) -> None:
        self._store = store
        self._resources = resources

    def reconcile(self, reservation: TargetReservation) -> None:
        run = self._store.run_record(reservation.run_id)
        observer, git = self._resources.reservation_adapters(
            reservation,
            run.repository_id,
            run.repository_instance_digest,
            self._store.target_authority_digest(reservation.run_id),
        )
        observed = observer.observe(reservation)
        if not observed.observable or observed.registration_present != observed.path_present:
            raise RuntimeError("TARGET_RESERVATION_CLEANUP_CONFLICT")
        if not observed.registration_present and not observed.path_present:
            return
        if (
            not observed.exact_identity
            or not observed.registration_present
            or not observed.path_present
        ):
            raise RuntimeError("TARGET_RESERVATION_CLEANUP_CONFLICT")
        if observed.locked:
            git.unlock(reservation)
            observed = observer.observe(reservation)
        if (
            not observed.observable
            or not observed.exact_identity
            or not observed.registration_present
            or not observed.path_present
            or observed.locked
        ):
            raise RuntimeError("TARGET_RESERVATION_CLEANUP_CONFLICT")
        git.remove_force(reservation)
        observed = observer.observe(reservation)
        if not observed.observable or observed.registration_present or observed.path_present:
            raise RuntimeError("TARGET_RESERVATION_CLEANUP_CONFLICT")
        self._resources.refresh()


class _ProductionStartGuard:
    def __init__(self, store: SqliteStateStore, resources: _CompositionRepositoryResources) -> None:
        self._store = store
        self._resources = resources

    def _guard(self, run_id: RunId) -> GitPrivateRefStartGuard:
        run = self._store.run_record(run_id)
        reservation = self._store.target_reservation_for_run(run_id)
        return self._resources.private_ref_guard(
            reservation=reservation,
            repository_id=run.repository_id,
            repository_instance_digest=run.repository_instance_digest,
            target_safety_digest=self._store.target_authority_digest(run_id),
        )

    def inspect(
        self,
        *,
        run_id: RunId,
        applicable_revision_digests: ApplicableRevisionDigests,
        expected_sequence: AuditSequence,
    ) -> StartGuardDecision:
        return self._guard(run_id).inspect(
            run_id=run_id,
            applicable_revision_digests=applicable_revision_digests,
            expected_sequence=expected_sequence,
        )

    def validate_consumed(
        self,
        *,
        binding: RuntimeStartBinding,
        permit: RuntimePermit,
        expected_sequence: AuditSequence,
    ) -> StartGuardDecision:
        return self._guard(permit.run_id).validate_consumed(
            binding=binding, permit=permit, expected_sequence=expected_sequence
        )


def _secret_policy_for_runtime(provided: SecretPathPolicy | None = None) -> SecretPathPolicy:
    if provided is not None:
        return provided
    try:
        key, rules = KeyringSecretPolicyStore().load()
    except SecretPolicyConfigurationError as error:
        raise RuntimeError(str(error)) from error
    return SecretPathPolicy.from_host_rules(rules, key)


class _CompositionPlanningContext:
    def __init__(self, store: SqliteStateStore) -> None:
        self._store = store

    def _documents(
        self, run_id: RunId
    ) -> tuple[ModelConfigurationRevisionDocument, BudgetRevisionDocument]:
        model_configuration = cast(
            ModelConfigurationRevisionDocument,
            self._store.current_revision_document(run_id, "MODEL_CONFIGURATION"),
        )
        budget = cast(
            BudgetRevisionDocument,
            self._store.current_revision_document(run_id, "BUDGET"),
        )
        return model_configuration, budget

    def build_planning_request(
        self,
        run_id: RunId,
        authorization: PlanningAuthorization,
        manifest: PlanningManifest,
    ) -> ModelRequest:
        model_configuration, budget = self._documents(run_id)
        self._requested_model_id = model_configuration.requested_model_id
        self._allowed_model_ids = frozenset(
            alias.returned_model_id for alias in model_configuration.returned_model_aliases
        )
        self._max_input_tokens = model_configuration.inference_settings.max_input_tokens
        self._max_output_tokens = model_configuration.inference_settings.max_output_tokens
        self._tool_schema_digest = model_configuration.tool_schema_digest
        self._reserved_cost_usd = _worst_case_reservation(model_configuration, budget)
        inputs = self._store.bootstrap_inputs(run_id)
        prompt_document = {
            "acceptance_criteria": list(inputs.acceptance_criteria),
            "constraints": list(inputs.constraints),
            "goal": inputs.goal,
            "manifest": [
                {"path": str(path), "digest": str(digest), "size": size}
                for path, digest, size in manifest.entries
            ],
            "instructions": (
                "Return exactly one planning action object. Use submit_plan with a complete "
                "plan_document, or use one bounded read/search action before submitting. "
                "Do not return a Worker action."
            ),
        }
        prompt = canonical_json(prompt_document)
        current = authorization.applicable_revision_digests
        if (
            current.policy_digest is None
            or current.budget_digest is None
            or current.model_configuration_digest is None
        ):
            raise ValueError("PLANNING_REVISION_BINDING_INCOMPLETE")
        request_digest = sha256_digest(
            canonical_json(
                {
                    "run_id": run_id,
                    "prompt": prompt,
                    "snapshot_digest": authorization.turn_binding.snapshot_digest
                    if authorization.turn_binding is not None
                    else None,
                }
            )
        )
        return ModelRequest(
            run_id=run_id,
            plan_digest=None,
            policy_digest=current.policy_digest,
            budget_digest=current.budget_digest,
            model_configuration_digest=current.model_configuration_digest,
            requested_model_id=self._requested_model_id,
            allowed_model_ids=self._allowed_model_ids,
            prompt=({"role": "user", "content": prompt},),
            tool_schema_digest=str(self._tool_schema_digest),
            request_digest=request_digest,
            idempotency_key=f"planning-request:{run_id}:{request_digest}",
            max_input_tokens=self._max_input_tokens,
            max_output_tokens=self._max_output_tokens,
            reserved_cost_usd=self._reserved_cost_usd,
        )


class _CompositionPlanningRequests:
    def __init__(self, store: SqliteStateStore) -> None:
        self._context = _CompositionPlanningContext(store)

    def create(
        self,
        *,
        run_id: RunId,
        authorization: PlanningAuthorization,
        manifest: PlanningManifest,
    ) -> ModelRequest:
        return self._context.build_planning_request(run_id, authorization, manifest)


class _RuntimeIds:
    def __init__(self) -> None:
        self._next = 0

    def _value(self, prefix: str, run_id: RunId) -> str:
        self._next += 1
        return f"{prefix}-{run_id}-{self._next}"

    def next_action_id(self, run_id: RunId) -> str:
        return self._value("action", run_id)

    def next_intent_id(self, run_id: RunId) -> IntentId:
        return IntentId(self._value("intent", run_id))


class _CompositionWorkerScheduling:
    """Allocate the first bounded worker tranche through the authority service."""

    def __init__(self, store: SqliteStateStore, authority: AuthorityService) -> None:
        self._store = store
        self._authority = authority

    def next_dispatchable(self, run_id: RunId) -> TaskDispatchSelection | RuntimeDecision:
        state = self._store.load_runtime_state(run_id)
        if state.state == "ACTIVE" and state.plan_digest is not None:
            for contract in self._store.task_contracts(state.plan_digest):
                counters = self._store.task_budget_state(run_id, TaskId(contract.task_id))
                if (
                    contract.dependency_task_ids
                    or counters.allocated_calls != 0
                    or counters.tranche_count != 0
                ):
                    continue
                task = TaskAuthority(
                    run_id=run_id,
                    task_id=TaskId(contract.task_id),
                    attempt_id=AttemptId(f"bootstrap-{run_id}-{contract.task_id}"),
                )
                self._authority.allocate_tranche(
                    task,
                    NO_PROGRESS,
                    expected_sequence=self._store.audit_sequence(run_id),
                )
                break
        selection = self._store.next_dispatchable(run_id)
        if (
            isinstance(selection, RuntimeDecision)
            and selection.stop_reason == "NO_DISPATCHABLE_TASK"
        ):
            try:
                sequence = self._store.prepare_final_candidate(
                    run_id, self._store.audit_sequence(run_id)
                )
            except StateConflict as error:
                if str(error) not in {
                    "FINAL_CANDIDATE_TASKS_INCOMPLETE",
                    "FINAL_CANDIDATE_TASKS_MISSING",
                    "FINAL_CANDIDATE_PLAN_NOT_FOUND",
                }:
                    raise
                return selection
            return RuntimeDecision.pause("AWAITING_FINAL_APPROVAL", sequence)
        return selection


class ApplicationBundle:
    """The only concrete object exposed by the application composition root."""

    __slots__ = ("_closeables", "_closed", "_closed_indices", "control", "queries", "runtime")

    def __init__(
        self,
        *,
        control: CrewControlService,
        runtime: RuntimeService,
        queries: RunQueryService,
        closeables: tuple[object, ...],
    ) -> None:
        self.control = control
        self.runtime = runtime
        self.queries = queries
        self._closeables = closeables
        self._closed = False
        self._closed_indices: set[int] = set()

    def close(self) -> None:
        if self._closed:
            return
        first_error: BaseException | None = None
        for index in range(len(self._closeables) - 1, -1, -1):
            if index in self._closed_indices:
                continue
            closeable = self._closeables[index]
            close = getattr(closeable, "close", None)
            if callable(close):
                try:
                    close()
                except BaseException as error:  # noqa: BLE001 - cleanup must continue
                    if first_error is None:
                        first_error = error
                else:
                    self._closed_indices.add(index)
            else:
                self._closed_indices.add(index)
        if first_error is not None:
            raise first_error
        self._closed = True


def build_application_bundle(
    root: Path,
    *,
    repository_authority: RepositoryBootstrapPort | None = None,
    model_configuration: ModelConfigurationRevisionDocument | None = None,
    budget: BudgetRevisionDocument | None = None,
    scripted_model: ScriptedMockLLM | None = None,
    credential_source: ModelCredentialPort | None = None,
    secret_policy: SecretPathPolicy | None = None,
    response_schemas: Mapping[str, Mapping[str, object]] | None = None,
    client_factory: ClientFactory | None = None,
) -> ApplicationBundle:
    """Build one shared, closeable application object graph."""
    root = root.resolve()
    data_root = root / ".apexcrew"
    data_root.mkdir(parents=True, exist_ok=True)
    store = SqliteStateStore(data_root / "state.db", monotonic_clock=SystemMonotonicClock())
    repository = (
        RepositoryBootstrapAdapter() if repository_authority is None else repository_authority
    )
    resources = _CompositionRepositoryResources(root, repository, store.data_root)
    revisions = _default_revisions()
    selected_budget = revisions.budget if budget is None else budget
    selected_model_configuration = (
        revisions.model_configuration if model_configuration is None else model_configuration
    )
    try:
        model = build_model_port(
            model_configuration=selected_model_configuration,
            budget=selected_budget,
            scripted_model=scripted_model,
            credential_source=credential_source,
            response_schemas=response_schemas,
            client_factory=client_factory,
        )
        authority = AuthorityService(store)
        worker_model = AuthorityModelClient(model, store, authority, SystemUtcClock())
        ids = _RuntimeIds()
        worker_tools = _CompositionWorkerTools(
            store,
            resources,
            (
                _secret_policy_for_runtime(secret_policy)
                if secret_policy is not None
                else SecretPathPolicy.from_host_rules((), b"k" * 32)
            ),
        )
        planning_revisions = _CompositionPlanningRevisionContext(store)
        worker = WorkerLoopService(
            attempts=store,
            capsules=_CompositionWorkerContext(store),
            requests=_CompositionWorkerRequests(
                store, selected_model_configuration, selected_budget
            ),
            models=worker_model,
            actions=WorkerActionCodec(lambda _binding, _action: ActionPreState()),
            authority=authority,
            tools=worker_tools,
            journal=store,
            ids=ids,
            clock=SystemUtcClock().now,
        )
        planning = PlanningActionApplier(
            state=store,
            reads=_CompositionPlanningReads(store, resources, secret_policy),
            ids=ids,
        )
        coordinator = CoordinatorService(
            planning_authorization=_CompositionPlanningAuthorization(
                store, secret_policy, planning_revisions
            ),
            context=BoundedPlanningContextBuilder(
                manifests=_CompositionPlanningManifests(
                    resources, planning_revisions, secret_policy
                ),
                requests=_CompositionPlanningRequests(store),
            ),
            models=worker_model,
            planning_actions=planning,
            journal=store,
            state=store,
            clock=SystemUtcClock(),
            scheduling=_CompositionWorkerScheduling(store, authority),
            attempts=store,
            workers=worker,
        )
        control = CrewControlService(
            ControlCommandService(
                state=store,
                target_authority=_TargetAuthority(store),
                repository_authority=repository,
                start_guard=_ProductionStartGuard(store, resources),
            )
        )
        target_reservations = _ProductionTargetReservationDriver(store, resources)
        recovery = RecoveryService(store)
        phase_drivers = RuntimePhaseDriverService(
            recovered_actions=RecoveredActionRouter(coordinator, worker),
            target_reservations=target_reservations,
            private_refs=_ProductionPrivateRefDriver(store, resources),
            resolution=ResolutionRuntime(store, ResolutionObservationRegistry()),
            integration=_CompositionIntegrationDriver(store, resources),
            cleanup=TerminalCleanupRuntime(store, _CompositionReservationCleanup(store, resources)),
            granted_actions=GrantedActionRuntime(store, worker_tools),
        )
        runtime = RuntimeService(
            store=store,
            ownership=FileRunOwnership(
                store.data_root,
                LocalFileLockBackend(),
                ProcessRuntimeOwnerIds(),
            ),
            journal=store,
            authority=authority,
            recovery=recovery,
            coordinator=coordinator,
            model_client=DurableModelClient(model=model, journal=store),
            tools=_ToolSchema(selected_model_configuration.tool_schema_digest),
            phase_drivers=phase_drivers,
        )
        queries = RunQueryService(ProjectionService(store))
    except BaseException:
        try:
            store.close()
        finally:
            resources.close()
            close = getattr(repository, "close", None)
            if callable(close):
                close()
        raise
    return ApplicationBundle(
        control=control,
        runtime=runtime,
        queries=queries,
        closeables=(repository, resources, store),
    )


class _ToolSchema:
    def __init__(self, digest: Sha256DigestText) -> None:
        self._digest = digest

    @property
    def schema_digest(self) -> Sha256DigestText:
        return self._digest


def _default_revisions() -> RevisionDocuments:
    return default_revision_documents()


def _worst_case_reservation(
    model_configuration: ModelConfigurationRevisionDocument,
    budget: BudgetRevisionDocument,
) -> Decimal:
    priced = {
        entry.returned_model_id: entry
        for entry in budget.pricing_entries
        if entry.returned_model_id
        in {alias.returned_model_id for alias in model_configuration.returned_model_aliases}
    }
    if len(priced) != len(model_configuration.returned_model_aliases):
        raise ValueError("MODEL_CONFIGURATION_PRICING_INCOMPLETE")
    input_tokens = Decimal(model_configuration.inference_settings.max_input_tokens)
    output_tokens = Decimal(model_configuration.inference_settings.max_output_tokens)
    return max(
        (
            input_tokens * entry.input_usd_per_million / Decimal(1_000_000)
            + output_tokens * entry.output_usd_per_million / Decimal(1_000_000)
            for entry in priced.values()
        ),
        default=Decimal(0),
    )


__all__ = ["ApplicationBundle", "build_application_bundle"]
