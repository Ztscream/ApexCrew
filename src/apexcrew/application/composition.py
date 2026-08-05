"""Production application composition root."""

from __future__ import annotations

from collections.abc import Mapping
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
from apexcrew.domain.commands import RuntimeDecision, RuntimePermit
from apexcrew.domain.coordination import AuthorityModelClient, CoordinatorService
from apexcrew.domain.effects import RecoveryService, ReservationObservation, TargetReservation
from apexcrew.domain.model import DurableModelClient, ModelRequest
from apexcrew.domain.projection import ProjectionService
from apexcrew.domain.revisions import (
    BudgetRevisionDocument,
    ModelConfigurationRevisionDocument,
    Sha256DigestText,
)
from apexcrew.domain.tools import ActionPreState, ToolIntent, ToolResult
from apexcrew.domain.types import AttemptId, IntentId, RunId
from apexcrew.domain.worker import WorkerActionCodec, WorkerLoopService


class _TargetAuthority(TargetAuthorityDigestService):
    def __init__(self, store: SqliteStateStore) -> None:
        self._store = store

    def current_for(self, run_id: RunId) -> Sha256DigestText:
        return self._store.target_authority_digest(run_id)


class _DeferredWorkerContext:
    def build_current(self, attempt_id: AttemptId) -> object:
        raise RuntimeError("WORKER_CONTEXT_NOT_COMPOSED")


class _DeferredWorkerRequests:
    def for_attempt(self, attempt_id: AttemptId, capsule: object) -> ModelRequest:
        del attempt_id, capsule
        raise RuntimeError("WORKER_REQUEST_FACTORY_NOT_COMPOSED")


class _DeferredWorkerTools:
    def execute(self, intent: ToolIntent) -> ToolResult:
        del intent
        raise RuntimeError("WORKER_TOOL_RUNTIME_NOT_COMPOSED")


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


class _DeferredReservationObserver(TargetReservationObserver):
    def observe(self, reservation: TargetReservation) -> ReservationObservation:
        del reservation
        return ReservationObservation(False, False, False, False, False)


class _DeferredReservationGit(TargetReservationGitPort):
    def apply(self, operation: TargetReservationOperation) -> TargetReservationOperationResult:
        del operation
        raise RuntimeError("TARGET_RESERVATION_GIT_NOT_COMPOSED")


class _DeferredRuntimeDriver:
    def initialize(self, run_id: RunId, permit: RuntimePermit) -> RuntimeDecision:
        del run_id, permit
        return RuntimeDecision.pause("RUNTIME_DRIVER_NOT_COMPOSED")

    resume = initialize
    integrate = initialize
    reconcile = initialize


class ApplicationBundle:
    """The only concrete object exposed by the application composition root."""

    __slots__ = ("_closeables", "_closed", "control", "queries", "runtime")

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

    def close(self) -> None:
        if self._closed:
            return
        first_error: BaseException | None = None
        for closeable in reversed(self._closeables):
            close = getattr(closeable, "close", None)
            if callable(close):
                try:
                    close()
                except BaseException as error:  # noqa: BLE001 - cleanup must continue
                    if first_error is None:
                        first_error = error
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
            capsules=_DeferredWorkerContext(),
            requests=_DeferredWorkerRequests(),
            models=worker_model,
            actions=WorkerActionCodec(lambda _binding, _action: ActionPreState()),
            authority=authority,
            tools=_DeferredWorkerTools(),
            journal=store,
            ids=ids,
            clock=SystemUtcClock().now,
        )
        coordinator = CoordinatorService.for_worker_scheduling(
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
        reservation_observer = _DeferredReservationObserver()
        reservation_effects = TargetReservationAdmissionService(
            reservation_observer, _DeferredReservationGit()
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
            private_refs=_DeferredRuntimeDriver(),
            resolution=_DeferredRuntimeDriver(),
            integration=_DeferredRuntimeDriver(),
            cleanup=_DeferredRuntimeDriver(),
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
