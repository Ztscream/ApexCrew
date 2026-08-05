"""Production application composition root."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path

from apexcrew.adapters.credentials.model_key import ModelCredentialPort
from apexcrew.adapters.model.deepseek_responses import ClientFactory
from apexcrew.adapters.model.factory import build_model_port
from apexcrew.adapters.model.scripted import ScriptedMockLLM
from apexcrew.adapters.repository.bootstrap import (
    RepositoryBootstrapAuthorityService as RepositoryBootstrapAdapter,
)
from apexcrew.adapters.state.sqlite import SqliteStateStore
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
    InMemoryRunOwnership,
    RecoveredActionRouter,
    RuntimePhaseDriverService,
    RuntimeService,
    TargetReservationDriver,
)
from apexcrew.domain.admission import (
    TargetReservationAdmissionService,
    TargetReservationBootstrapAdmissionService,
    TargetReservationGitPort,
    TargetReservationObserver,
    TargetReservationOperation,
    TargetReservationOperationResult,
)
from apexcrew.domain.authority import AuthorityService, SystemUtcClock
from apexcrew.domain.commands import ApplicableRevisionDigests, RuntimeDecision, RuntimePermit
from apexcrew.domain.coordination import (
    AuthorityModelClient,
    BoundedPlanningContextBuilder,
    CoordinatorService,
    PlanningActionApplier,
    PlanningAuthorization,
    PlanningManifest,
    PlanningReadGateway,
    PlanningReadIntent,
    PlanningReadResult,
    PlanningTurnBinding,
)
from apexcrew.domain.effects import (
    RecoveryService,
    ReservationObservation,
    TargetReservation,
    canonical_json,
    sha256_digest,
)
from apexcrew.domain.evidence import ContextCapsule
from apexcrew.domain.model import DurableModelClient, ModelRequest
from apexcrew.domain.projection import ProjectionService
from apexcrew.domain.revisions import (
    BudgetRevisionDocument,
    ModelConfigurationRevisionDocument,
    Sha256DigestText,
)
from apexcrew.domain.tools import ActionPreState, ToolIntent, ToolResult
from apexcrew.domain.types import AttemptId, IntentId, RevisionDigest, RunId
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
    def __init__(self, store: SqliteStateStore) -> None:
        self._store = store

    def for_attempt(self, attempt_id: AttemptId, capsule: object) -> ModelRequest:
        binding = self._store.current_worker_turn_binding(attempt_id)
        content = capsule.content if isinstance(capsule, ContextCapsule) else str(capsule)
        digest = sha256_digest(canonical_json({"attempt_id": str(attempt_id), "content": content}))
        return ModelRequest(
            run_id=binding.run_id,
            plan_digest=RevisionDigest(str(binding.plan_digest)),
            policy_digest=RevisionDigest(str(binding.policy_digest)),
            budget_digest=RevisionDigest(str(binding.budget_digest)),
            model_configuration_digest=RevisionDigest(str(binding.model_configuration_digest)),
            requested_model_id="deepseek-v4-flash",
            allowed_model_ids=frozenset({"deepseek-v4-flash"}),
            prompt=({"role": "user", "content": content},),
            tool_schema_digest=str(binding.tool_schema_digest),
            request_digest=digest,
            idempotency_key=f"worker-request:{attempt_id}:{digest}",
            max_input_tokens=32_000,
            max_output_tokens=4_096,
            reserved_cost_usd=Decimal("0.01"),
            owner_kind="WORKER",
            task_id=binding.task_id,
            attempt_id=binding.attempt_id,
            tranche_id=binding.tranche_id,
        )


class _CompositionWorkerTools:
    def execute(self, intent: ToolIntent) -> ToolResult:
        return ToolResult(
            code="INDETERMINATE",
            run_id=intent.run_id,
            intent_id=intent.intent_id,
            bounded_payload={"reason": "TOOL_RUNTIME_NOT_CONNECTED"},
        )


class _CompositionPlanningAuthorization:
    _DIGEST = Sha256DigestText("sha256:" + "0" * 64)

    def current(self, run_id: RunId) -> PlanningAuthorization:
        return self._paused(run_id)

    def current_for_recovery(self, run_id: RunId, action: object) -> PlanningAuthorization:
        del action
        return self._paused(run_id)

    @classmethod
    def _paused(cls, run_id: RunId) -> PlanningAuthorization:
        return PlanningAuthorization(
            run_id=run_id,
            decision="PAUSE",
            reason="PLANNING_AUTHORITY_NOT_CONNECTED",
            applicable_revision_digests=ApplicableRevisionDigests(),
            target_safety_digest=cls._DIGEST,
            credential_profile=None,
            read_authorization=None,
            turn_binding=None,
            planning_request_count=0,
            planning_request_ceiling=8,
        )


class _CompositionPlanningContext:
    def build_planning_request(
        self, run_id: RunId, authorization: PlanningAuthorization
    ) -> ModelRequest:
        del authorization
        return ModelRequest(
            run_id=run_id,
            plan_digest=None,
            policy_digest=RevisionDigest(str(_CompositionPlanningAuthorization._DIGEST)),
            budget_digest=RevisionDigest(str(_CompositionPlanningAuthorization._DIGEST)),
            model_configuration_digest=RevisionDigest(
                str(_CompositionPlanningAuthorization._DIGEST)
            ),
            requested_model_id="deepseek-v4-flash",
            allowed_model_ids=frozenset({"deepseek-v4-flash"}),
            prompt=({"role": "user", "content": "planning"},),
            tool_schema_digest=str(_CompositionPlanningAuthorization._DIGEST),
            request_digest=sha256_digest(str(run_id)),
            idempotency_key=f"planning-request:{run_id}",
            max_input_tokens=32_000,
            max_output_tokens=4_096,
            reserved_cost_usd=Decimal("0.01"),
        )


class _CompositionPlanningManifests:
    def manifest(self, binding: PlanningTurnBinding) -> PlanningManifest:
        del binding
        return PlanningManifest(entries=(), total_bytes=0)


class _CompositionPlanningRequests:
    def create(
        self,
        *,
        run_id: RunId,
        authorization: PlanningAuthorization,
        manifest: PlanningManifest,
    ) -> ModelRequest:
        del manifest
        return _CompositionPlanningContext().build_planning_request(run_id, authorization)


class _CompositionPlanningReads(PlanningReadGateway):
    def execute(
        self, intent: PlanningReadIntent, authorization: PlanningAuthorization
    ) -> PlanningReadResult:
        del authorization
        return PlanningReadResult(
            intent_id=intent.intent_id,
            run_id=intent.run_id,
            result_class="DENIED",
            bounded_payload={"reason": "PLANNING_SNAPSHOT_NOT_CONNECTED"},
            snapshot_digest=intent.snapshot_digest,
            returned_bytes=0,
        )


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


class _CompositionReservationObserver(TargetReservationObserver):
    def observe(self, reservation: TargetReservation) -> ReservationObservation:
        del reservation
        return ReservationObservation(False, False, False, False, False)


class _CompositionReservationGit(TargetReservationGitPort):
    def apply(self, operation: TargetReservationOperation) -> TargetReservationOperationResult:
        del operation
        raise RuntimeError("TARGET_RESERVATION_GIT_NOT_CONNECTED")


class _CompositionPhaseDriver:
    def initialize(self, run_id: RunId, permit: RuntimePermit) -> RuntimeDecision:
        del run_id, permit
        return RuntimeDecision.pause("RUNTIME_PHASE_DRIVER_NOT_CONNECTED")

    resume = initialize
    integrate = initialize
    reconcile = initialize


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
    response_schemas: Mapping[str, Mapping[str, object]] | None = None,
    client_factory: ClientFactory | None = None,
) -> ApplicationBundle:
    """Build one shared, closeable application object graph."""
    root = root.resolve()
    store = SqliteStateStore(root / "state.db")
    repository = (
        RepositoryBootstrapAdapter() if repository_authority is None else repository_authority
    )
    revisions = _default_revisions()
    selected_budget = revisions[0] if budget is None else budget
    selected_model_configuration = (
        revisions[1] if model_configuration is None else model_configuration
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
        worker = WorkerLoopService(
            attempts=store,
            capsules=_CompositionWorkerContext(store),
            requests=_CompositionWorkerRequests(store),
            models=worker_model,
            actions=WorkerActionCodec(lambda _binding, _action: ActionPreState()),
            authority=authority,
            tools=_CompositionWorkerTools(),
            journal=store,
            ids=ids,
            clock=SystemUtcClock().now,
        )
        planning = PlanningActionApplier(
            state=store,
            reads=_CompositionPlanningReads(),
            ids=ids,
        )
        coordinator = CoordinatorService(
            planning_authorization=_CompositionPlanningAuthorization(),
            context=BoundedPlanningContextBuilder(
                manifests=_CompositionPlanningManifests(),
                requests=_CompositionPlanningRequests(),
            ),
            models=worker_model,
            planning_actions=planning,
            journal=store,
            state=store,
            clock=SystemUtcClock(),
            scheduling=store,
            attempts=store,
            workers=worker,
        )
        control = CrewControlService(
            ControlCommandService(
                state=store,
                target_authority=_TargetAuthority(store),
                repository_authority=repository,
            )
        )
        reservation_observer = _CompositionReservationObserver()
        reservation_effects = TargetReservationAdmissionService(
            reservation_observer, _CompositionReservationGit()
        )
        target_reservations = TargetReservationDriver(
            TargetReservationBootstrapAdmissionService(
                state=store,
                observer=reservation_observer,
                effects=reservation_effects,
            )
        )
        phase_drivers = RuntimePhaseDriverService(
            recovered_actions=RecoveredActionRouter(coordinator, worker),
            target_reservations=target_reservations,
            private_refs=_CompositionPhaseDriver(),
            resolution=_CompositionPhaseDriver(),
            integration=_CompositionPhaseDriver(),
            cleanup=_CompositionPhaseDriver(),
        )
        runtime = RuntimeService(
            store=store,
            ownership=InMemoryRunOwnership(),
            journal=store,
            authority=authority,
            recovery=RecoveryService(store),
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
            close = getattr(repository, "close", None)
            if callable(close):
                close()
        raise
    return ApplicationBundle(
        control=control,
        runtime=runtime,
        queries=queries,
        closeables=(repository, store),
    )


class _ToolSchema:
    def __init__(self, digest: Sha256DigestText) -> None:
        self._digest = digest

    @property
    def schema_digest(self) -> Sha256DigestText:
        return self._digest


def _default_revisions() -> tuple[BudgetRevisionDocument, ModelConfigurationRevisionDocument]:
    from apexcrew.application.configuration import default_revision_documents

    revisions = default_revision_documents()
    return revisions.budget, revisions.model_configuration


__all__ = ["ApplicationBundle", "build_application_bundle"]
