from __future__ import annotations

from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Literal, Protocol

from apexcrew.domain.admission import (
    PrivateRefAdmissionPort,
    PrivateRefCasOutcome,
    RefCasIntent,
    RepositoryEffectUncertain,
    RuntimeStartBinding,
    StartGuard,
    TargetReservationBootstrapAdmissionService,
    private_ref,
)
from apexcrew.domain.authority import ActiveRunTimeBoundaryDecision, GrantedActionIntent
from apexcrew.domain.commands import RunStop, RuntimeDecision, RuntimePermit, RuntimeState
from apexcrew.domain.effects import EffectIntent, RecoveryOutcome, StateConflict, canonical_json
from apexcrew.domain.model import (
    CommittedModelTurn,
    DurableModelClient,
    LogicalTurnId,
    ModelRecoveryBinding,
    RecoveredModelAction,
)
from apexcrew.domain.revisions import Sha256DigestText
from apexcrew.domain.tools import GrantedActionJournal, GrantedActionToolPort
from apexcrew.domain.types import (
    AuditSequence,
    IntentId,
    RunId,
    RunState,
    RunStopReason,
    RuntimeOwnerId,
)


@dataclass(frozen=True, slots=True)
class RuntimeOwner:
    owner_id: RuntimeOwnerId


class RunOwnership(Protocol):
    def acquire(self, run_id: RunId) -> AbstractContextManager[RuntimeOwner | None]:
        raise NotImplementedError


class FileLockBackend(Protocol):
    def try_lock(self, path: Path) -> AbstractContextManager[bool]:
        raise NotImplementedError


class RuntimeOwnerIdSource(Protocol):
    def next_runtime_owner_id(self) -> RuntimeOwnerId:
        raise NotImplementedError


_UNSET = object()


class FileRunOwnership:
    def __init__(self, data_root: Path, locks: FileLockBackend, ids: RuntimeOwnerIdSource) -> None:
        self._data_root = data_root
        self._locks = locks
        self._ids = ids

    @contextmanager
    def acquire(
        self, run_id: RunId, permit: RuntimePermit | None | object = _UNSET
    ) -> Iterator[RuntimeOwner | None]:
        # DEBT-M1-006: cross-process mutex and concrete OS file-lock ownership remain deferred.
        if permit is not _UNSET and (
            permit is None
            or not isinstance(permit, RuntimePermit)
            or permit.run_id != run_id
            or permit.state not in {"UNCONSUMED", "CONSUMED"}
        ):
            yield None
            return
        path = self._data_root / "runtime-locks" / f"{run_id}.lock"
        with self._locks.try_lock(path) as locked:
            yield RuntimeOwner(self._ids.next_runtime_owner_id()) if locked else None


class InMemoryRunOwnership:
    def __init__(self) -> None:
        self._held: set[RunId] = set()
        self._next = 0

    @contextmanager
    def acquire(self, run_id: RunId) -> Iterator[RuntimeOwner | None]:
        if run_id in self._held:
            yield None
            return
        self._held.add(run_id)
        self._next += 1
        try:
            yield RuntimeOwner(RuntimeOwnerId(f"memory-owner-{self._next}"))
        finally:
            self._held.remove(run_id)


class RuntimeCoordinator(Protocol):
    def run_planning_turn(self, run_id: RunId) -> RuntimeDecision:
        raise NotImplementedError

    def resume_recovered_planning_action(
        self, run_id: RunId, permit: RuntimePermit, action: RecoveredModelAction
    ) -> RuntimeDecision:
        raise NotImplementedError

    def schedule(self, run_id: RunId) -> RuntimeDecision:
        raise NotImplementedError


class RuntimeWorkerLoop(Protocol):
    def resume_recovered_worker_action(
        self, run_id: RunId, permit: RuntimePermit, action: RecoveredModelAction
    ) -> RuntimeDecision:
        raise NotImplementedError


class RecoveredActionRouter:
    def __init__(self, coordinator: RuntimeCoordinator, workers: RuntimeWorkerLoop) -> None:
        self._coordinator = coordinator
        self._workers = workers

    def resume(
        self, run_id: RunId, permit: RuntimePermit, action: RecoveredModelAction
    ) -> RuntimeDecision:
        if action.turn.owner_kind == "PLANNING":
            return self._coordinator.resume_recovered_planning_action(run_id, permit, action)
        return self._workers.resume_recovered_worker_action(run_id, permit, action)


class TargetReservationDriver:
    def __init__(self, admission: TargetReservationBootstrapAdmissionService) -> None:
        self._admission = admission

    def initialize(self, run_id: RunId, permit: RuntimePermit) -> RuntimeDecision:
        return self._admission.initialize_target_reservation(run_id, permit)


TargetReservationDriverService = TargetReservationDriver


class PrivateRefDriver(Protocol):
    def initialize(self, run_id: RunId, permit: RuntimePermit) -> RuntimeDecision:
        raise NotImplementedError


class PrivateRefState(Protocol):
    def runtime_start_binding(self, run_id: RunId) -> RuntimeStartBinding: ...

    def record_private_ref_init_intent(
        self,
        *,
        binding: RuntimeStartBinding,
        intent: RefCasIntent,
        expected_sequence: AuditSequence,
    ) -> AuditSequence: ...

    def settle_private_ref_init(
        self,
        *,
        binding: RuntimeStartBinding,
        intent: RefCasIntent,
        outcome: PrivateRefCasOutcome,
        expected_sequence: AuditSequence,
    ) -> AuditSequence: ...

    def mark_private_ref_init_indeterminate(
        self,
        *,
        binding: RuntimeStartBinding,
        intent: RefCasIntent,
        failure_class: str,
        expected_sequence: AuditSequence,
    ) -> AuditSequence: ...

    def audit_sequence(self, run_id: RunId) -> AuditSequence: ...


class PrivateRefInitializer:
    def __init__(
        self,
        store: PrivateRefState,
        start_guard: StartGuard,
        admission: PrivateRefAdmissionPort,
    ) -> None:
        self._store = store
        self._start_guard = start_guard
        self._admission = admission

    def initialize(self, run_id: RunId, permit: RuntimePermit) -> RuntimeDecision:
        current = self._store.runtime_start_binding(run_id)
        guard = self._start_guard.validate_consumed(
            binding=current,
            permit=permit,
            expected_sequence=current.sequence,
        )
        if not guard.ok or guard.binding is None:
            return RuntimeDecision.pause(guard.reason or "START_GUARD_DENIED")
        identity = canonical_json({"permit_generation": permit.generation, "run_id": run_id})
        digest = sha256(identity.encode("utf-8")).hexdigest()
        intent = RefCasIntent(
            intent_id=IntentId("private-ref-init-" + digest),
            run_id=run_id,
            kind="private_ref_init",
            repository_id=guard.binding.repository_id,
            ref_name=private_ref(run_id),
            expected_old_oid=None,
            prepared_oid=guard.binding.pinned_target_oid,
            target_safety_digest=guard.binding.target_safety_digest,
            ref_effect_binding=guard.binding.ref_effect_binding,
            target_reservation_id=guard.binding.target_reservation_id,
            permit_generation=permit.generation,
            applicable_revision_digests=guard.binding.applicable_revision_digests,
            idempotency_key=f"private-ref-init:{run_id}:{permit.generation}",
        )
        self._store.record_private_ref_init_intent(
            binding=current,
            intent=intent,
            expected_sequence=current.sequence,
        )
        try:
            outcome = self._admission.initialize_private_ref(intent)
        except RepositoryEffectUncertain:
            sequence = self._store.mark_private_ref_init_indeterminate(
                binding=current,
                intent=intent,
                failure_class="PRIVATE_REF_INIT_UNOBSERVABLE",
                expected_sequence=self._store.audit_sequence(run_id),
            )
            return RuntimeDecision.pause("INDETERMINATE", sequence)
        sequence = self._store.settle_private_ref_init(
            binding=current,
            intent=intent,
            outcome=outcome,
            expected_sequence=self._store.audit_sequence(run_id),
        )
        if outcome.result_class == "PRIVATE_REF_INITIALIZED":
            return RuntimeDecision(
                code="CONTINUE",
                resulting_sequence=sequence,
                phase_transition="PRIVATE_REF_INITIALIZED",
            )
        if outcome.result_class == "PRIVATE_REF_UNOBSERVABLE":
            return RuntimeDecision.pause("INDETERMINATE", sequence)
        return RuntimeDecision.pause(
            "PRIVATE_REF_CONFLICT"
            if outcome.result_class == "PRIVATE_REF_CONFLICT"
            else "PRIVATE_REF_INIT_FAILED",
            sequence,
        )


class ResolutionDriver(Protocol):
    def resume(self, run_id: RunId, permit: RuntimePermit) -> RuntimeDecision:
        raise NotImplementedError


class FinalIntegrationDriver(Protocol):
    def integrate(self, run_id: RunId, permit: RuntimePermit) -> RuntimeDecision:
        raise NotImplementedError


class TerminalCleanupDriver(Protocol):
    def reconcile(self, run_id: RunId, permit: RuntimePermit) -> RuntimeDecision:
        raise NotImplementedError


class GrantedActionDriver(Protocol):
    def execute(self, run_id: RunId, permit: RuntimePermit, intent_id: IntentId) -> RuntimeDecision:
        raise NotImplementedError


class GrantedActionRuntime(GrantedActionDriver):
    def __init__(self, journal: GrantedActionJournal, tools: GrantedActionToolPort) -> None:
        self._journal = journal
        self._tools = tools

    def execute(self, run_id: RunId, permit: RuntimePermit, intent_id: IntentId) -> RuntimeDecision:
        if permit.state != "CONSUMED" or permit.allowed_phase != "ACTIVE":
            raise ValueError("GRANTED_ACTION_PERMIT_PHASE_MISMATCH")
        intent = self._journal.require_unsettled_granted_intent(intent_id)
        if (
            intent.bindings.run_id != run_id
            or permit.run_id != run_id
            or permit.applicable_revision_digests != intent.bindings.applicable_revision_digests
        ):
            raise ValueError("GRANTED_ACTION_PERMIT_BINDING_MISMATCH")
        observation = self._tools.observe_granted_action(intent)
        if observation.state == "EXACT_POST":
            sequence = self._journal.settle_granted_action(
                run_id=run_id,
                intent_id=intent_id,
                result=observation.result_for(intent),
                applicable_revision_digests=intent.bindings.applicable_revision_digests,
                expected_sequence=self._journal.audit_sequence(run_id),
            )
            return RuntimeDecision(
                code="ACTION_RECORDED",
                stop_reason=None,
                resulting_sequence=sequence,
            )
        if observation.state == "EXACT_PRE":
            if intent.state == "INTENT_RECORDED":
                intent = self._journal.mark_granted_action_dispatched(
                    run_id=run_id,
                    intent_id=intent_id,
                    applicable_revision_digests=intent.bindings.applicable_revision_digests,
                    expected_sequence=self._journal.audit_sequence(run_id),
                )
            if intent.state != "DISPATCHED":
                raise StateConflict("GRANTED_ACTION_DISPATCH_STATE_INVALID")
            try:
                result = self._tools.execute_granted(intent)
            except RepositoryEffectUncertain:
                return self._mark_indeterminate(run_id, intent, observation.digest)
            if result.code in {"INDETERMINATE", "INFRASTRUCTURE_UNCERTAINTY"}:
                return self._mark_indeterminate(run_id, intent, observation.digest)
            sequence = self._journal.settle_granted_action(
                run_id=run_id,
                intent_id=intent_id,
                result=result,
                applicable_revision_digests=intent.bindings.applicable_revision_digests,
                expected_sequence=self._journal.audit_sequence(run_id),
            )
            return RuntimeDecision(
                code="ACTION_RECORDED",
                stop_reason=None,
                resulting_sequence=sequence,
            )
        return self._mark_indeterminate(run_id, intent, observation.digest)

    def _mark_indeterminate(
        self,
        run_id: RunId,
        intent: GrantedActionIntent,
        observation_digest: Sha256DigestText,
    ) -> RuntimeDecision:
        sequence = self._journal.mark_granted_action_indeterminate(
            run_id=run_id,
            intent_id=intent.intent_id,
            observation_digest=observation_digest,
            applicable_revision_digests=intent.bindings.applicable_revision_digests,
            expected_sequence=self._journal.audit_sequence(run_id),
        )
        return RuntimeDecision.pause("INDETERMINATE", sequence)


class RuntimePhaseDriverService:
    def __init__(
        self,
        recovered_actions: RecoveredActionRouter,
        target_reservations: TargetReservationDriver,
        private_refs: PrivateRefDriver,
        resolution: ResolutionDriver,
        integration: FinalIntegrationDriver,
        cleanup: TerminalCleanupDriver,
        granted_actions: GrantedActionDriver | None = None,
    ) -> None:
        self._recovered_actions = recovered_actions
        self._target_reservations = target_reservations
        self._private_refs = private_refs
        self._resolution = resolution
        self._integration = integration
        self._cleanup = cleanup
        self._granted_actions = granted_actions

    def resume_recovered_model_action(
        self, run_id: RunId, permit: RuntimePermit, action: RecoveredModelAction
    ) -> RuntimeDecision:
        return self._recovered_actions.resume(run_id, permit, action)

    def initialize_target_reservation(
        self, run_id: RunId, permit: RuntimePermit
    ) -> RuntimeDecision:
        return self._target_reservations.initialize(run_id, permit)

    def initialize_private_ref(self, run_id: RunId, permit: RuntimePermit) -> RuntimeDecision:
        return self._private_refs.initialize(run_id, permit)

    def resume_resolution(self, run_id: RunId, permit: RuntimePermit) -> RuntimeDecision:
        return self._resolution.resume(run_id, permit)

    def integrate_candidate(self, run_id: RunId, permit: RuntimePermit) -> RuntimeDecision:
        return self._integration.integrate(run_id, permit)

    def reconcile_terminal_cleanup(self, run_id: RunId, permit: RuntimePermit) -> RuntimeDecision:
        return self._cleanup.reconcile(run_id, permit)

    def execute_granted_action(
        self, run_id: RunId, permit: RuntimePermit, intent_id: IntentId
    ) -> RuntimeDecision:
        if self._granted_actions is None:
            return RuntimeDecision.pause("GRANTED_ACTION_DRIVER_NOT_INSTALLED")
        return self._granted_actions.execute(run_id, permit, intent_id)


RuntimeFaultPhase = Literal[
    "PERMIT_CONSUMPTION",
    "TARGET_RESERVATION",
    "RECOVERED_ACTION",
    "GRANTED_ACTION_RECOVERY",
    "EFFECT_RECOVERY",
    "MODEL_RECOVERY",
    "PLANNING",
    "WORKER_SCHEDULING",
    "POST_BARRIER",
    "PHASE_DRIVER",
]


@dataclass(frozen=True, slots=True)
class RuntimeFault:
    phase: RuntimeFaultPhase
    fault_code: Literal["UNHANDLED_RUNTIME_EXCEPTION", "MONOTONIC_CLOCK_REGRESSED"]
    fingerprint: Sha256DigestText

    @classmethod
    def from_exception(cls, phase: RuntimeFaultPhase, error: Exception) -> RuntimeFault:
        type_name = f"{type(error).__module__}.{type(error).__qualname__}"
        code: Literal["UNHANDLED_RUNTIME_EXCEPTION", "MONOTONIC_CLOCK_REGRESSED"] = (
            "MONOTONIC_CLOCK_REGRESSED"
            if str(error) == "MONOTONIC_CLOCK_REGRESSED"
            else "UNHANDLED_RUNTIME_EXCEPTION"
        )
        return cls(
            phase,
            code,
            Sha256DigestText("sha256:" + sha256(type_name.encode("utf-8")).hexdigest()),
        )


@dataclass(frozen=True, slots=True)
class RuntimeFaultDisposition:
    resulting_sequence: AuditSequence
    stop_reason: Literal["RUNTIME_FAULT", "RUNTIME_CLOCK_REGRESSION", "INDETERMINATE"]
    barrier_state: Literal["IDLE", "SETTLED", "INDETERMINATE"]


class RuntimeStateStore(Protocol):
    def audit_sequence(self, run_id: RunId) -> AuditSequence: ...
    def load_runtime_state(self, run_id: RunId) -> RuntimeState: ...
    def consume_current_runtime_permit(
        self, run_id: RunId, owner_id: RuntimeOwnerId, expected_sequence: AuditSequence
    ) -> RuntimePermit | None: ...
    def apply_post_barrier_controls(
        self,
        run_id: RunId,
        owner_id: RuntimeOwnerId,
        permit_generation: int,
        expected_sequence: AuditSequence,
    ) -> RuntimeDecision | None: ...
    def record_runtime_delivery_stop(
        self,
        run_id: RunId,
        owner_id: RuntimeOwnerId,
        permit_generation: int,
        candidate: RunStop,
        expected_sequence: AuditSequence,
    ) -> RunStop: ...
    def record_runtime_fault_and_classify_barrier(
        self,
        run_id: RunId,
        owner_id: RuntimeOwnerId,
        permit_generation: int,
        fault: RuntimeFault,
        expected_sequence: AuditSequence,
    ) -> RuntimeFaultDisposition: ...
    def begin_runtime_barrier(
        self, run_id: RunId, action_id: str, expected_sequence: AuditSequence
    ) -> str: ...
    def settle_runtime_barrier(
        self,
        run_id: RunId,
        action_id: str,
        model_calls: int,
        pending_stop_reason: str | None,
        expected_sequence: AuditSequence,
    ) -> AuditSequence: ...


class RuntimeRecoveryJournal(Protocol):
    def next_recovered_model_action(self, run_id: RunId) -> RecoveredModelAction | None: ...
    def next_recoverable_model_turn(self, run_id: RunId) -> CommittedModelTurn | None: ...
    def next_unsettled_granted_action(self, run_id: RunId) -> GrantedActionIntent | None: ...
    def record_downstream_action_intent(
        self,
        run_id: RunId,
        logical_turn_id: LogicalTurnId,
        intent: EffectIntent,
        expected_sequence: AuditSequence,
    ) -> AuditSequence: ...


class RuntimeBoundaryAuthority(Protocol):
    def evaluate_active_run_time_boundary(
        self, run_id: RunId, expected_sequence: AuditSequence
    ) -> ActiveRunTimeBoundaryDecision: ...


class RuntimeRecoveryService(Protocol):
    def reconcile(self, run_id: RunId) -> RecoveryOutcome: ...


class ToolSchemaProvider(Protocol):
    @property
    def schema_digest(self) -> Sha256DigestText: ...


class InjectedProcessCrash(BaseException):
    """Test-only process termination that deliberately leaves durable ownership open."""


def _stop_reason_for_decision(reason: str | None) -> RunStopReason:
    if reason is None:
        return RunStopReason.PAUSED
    return {
        "AWAITING_PLAN_APPROVAL": RunStopReason.AWAITING_PLAN_APPROVAL,
        "AWAITING_ACTION_APPROVAL": RunStopReason.AWAITING_ACTION_APPROVAL,
        "AWAITING_FINAL_APPROVAL": RunStopReason.AWAITING_FINAL_APPROVAL,
        "BUDGET_STOP": RunStopReason.BUDGET_STOP,
        "INDETERMINATE": RunStopReason.INDETERMINATE,
        "TERMINAL": RunStopReason.TERMINAL,
    }.get(reason, RunStopReason.PAUSED)


def _stop_for_state(run_id: RunId, state: RuntimeState, reason: RunStopReason) -> RunStop:
    return RunStop(
        run_id=run_id,
        state=state.state,
        reason=reason,
        last_sequence=state.sequence,
    )


@dataclass(slots=True)
class RuntimeFaultContext:
    phase: RuntimeFaultPhase = "PERMIT_CONSUMPTION"


class RuntimeService:
    def __init__(
        self,
        *,
        store: RuntimeStateStore,
        ownership: RunOwnership,
        journal: RuntimeRecoveryJournal,
        authority: RuntimeBoundaryAuthority,
        recovery: RuntimeRecoveryService,
        coordinator: RuntimeCoordinator,
        model_client: DurableModelClient,
        tools: ToolSchemaProvider,
        phase_drivers: RuntimePhaseDriverService,
    ) -> None:
        self._store = store
        self._ownership = ownership
        self._journal = journal
        self._authority = authority
        self._recovery = recovery
        self._coordinator = coordinator
        self._model_client = model_client
        self._tools = tools
        self._phase_drivers = phase_drivers

    def run_until_blocked(self, run_id: RunId) -> RunStop:
        with self._ownership.acquire(run_id) as owner:
            state = self._store.load_runtime_state(run_id)
            if owner is None:
                return _stop_for_state(run_id, state, RunStopReason.ALREADY_RUNNING)
            permit = self._store.consume_current_runtime_permit(
                run_id, owner.owner_id, self._store.audit_sequence(run_id)
            )
            if permit is None:
                return _stop_for_state(run_id, state, RunStopReason.NO_RUNTIME_PERMIT)
            context = RuntimeFaultContext()
            try:
                stop = self._run_consumed_permit(run_id, permit, context)
            except InjectedProcessCrash:
                raise
            except Exception as error:  # noqa: BLE001 - the runtime boundary classifies all faults
                disposition = self._store.record_runtime_fault_and_classify_barrier(
                    run_id,
                    owner.owner_id,
                    permit.generation,
                    RuntimeFault.from_exception(context.phase, error),
                    self._store.audit_sequence(run_id),
                )
                state = self._store.load_runtime_state(run_id)
                reason = (
                    RunStopReason.INDETERMINATE
                    if disposition.barrier_state == "INDETERMINATE"
                    else RunStopReason.PAUSED
                )
                stop = _stop_for_state(run_id, state, reason)
            return self._store.record_runtime_delivery_stop(
                run_id,
                owner.owner_id,
                permit.generation,
                stop,
                self._store.audit_sequence(run_id),
            )

    def _boundary(self, run_id: RunId) -> RunStop | None:
        decision = self._authority.evaluate_active_run_time_boundary(
            run_id, self._store.audit_sequence(run_id)
        )
        if decision.decision == "CONTINUE":
            return None
        state = self._store.load_runtime_state(run_id)
        return _stop_for_state(run_id, state, RunStopReason.BUDGET_STOP)

    def _run_consumed_permit(
        self, run_id: RunId, permit: RuntimePermit, context: RuntimeFaultContext
    ) -> RunStop:
        state = self._store.load_runtime_state(run_id)
        if state.state == RunState.DRAFT:
            return self._drive_permitted_phase(run_id, permit, context)
        context.phase = "RECOVERED_ACTION"
        recovered = self._journal.next_recovered_model_action(run_id)
        if recovered is not None:
            return self._resume_recovered(run_id, permit, recovered, context)
        context.phase = "GRANTED_ACTION_RECOVERY"
        granted = self._journal.next_unsettled_granted_action(run_id)
        if granted is not None:
            return self._drive_granted_action(run_id, permit, granted.intent_id, context)
        context.phase = "EFFECT_RECOVERY"
        recovery = self._recovery.reconcile(run_id)
        if recovery.requires_human_resolution:
            state = self._store.load_runtime_state(run_id)
            return _stop_for_state(run_id, state, RunStopReason.INDETERMINATE)
        committed = self._journal.next_recoverable_model_turn(run_id)
        if committed is not None:
            context.phase = "MODEL_RECOVERY"
            current = self._store.load_runtime_state(run_id)
            expected = ModelRecoveryBinding(
                request_digest=committed.recovery_binding.request_digest,
                tool_schema_digest=self._tools.schema_digest,
                plan_digest=current.plan_digest,
                policy_digest=current.policy_digest,
                budget_digest=current.budget_digest,
                model_configuration_digest=current.model_configuration_digest,
            )
            result = self._model_client.recover_committed(
                run_id, committed.logical_turn_id, expected
            )
            if result.normalized_action is None:
                return _stop_for_state(run_id, current, RunStopReason.PAUSED)
            sequence = self._store.audit_sequence(run_id)
            identity = canonical_json(
                {
                    "logical_turn_id": committed.logical_turn_id,
                    "recorded_sequence": sequence + 1,
                    "run_id": run_id,
                }
            )
            action = RecoveredModelAction.for_committed_turn(
                committed,
                intent_id=IntentId(
                    "recovered-model-action-" + sha256(identity.encode("utf-8")).hexdigest()
                ),
                recorded_sequence=AuditSequence(sequence + 1),
            )
            self._journal.record_downstream_action_intent(
                run_id,
                committed.logical_turn_id,
                action.effect_intent,
                sequence,
            )
            return self._resume_recovered(run_id, permit, action, context)
        return self._drive_permitted_phase(run_id, permit, context)

    def _drive_granted_action(
        self,
        run_id: RunId,
        permit: RuntimePermit,
        intent_id: IntentId,
        context: RuntimeFaultContext,
    ) -> RunStop:
        context.phase = "GRANTED_ACTION_RECOVERY"
        decision = self._phase_drivers.execute_granted_action(run_id, permit, intent_id)
        budget_stop = self._boundary(run_id)
        if decision.resulting_sequence is None:
            state = self._store.load_runtime_state(run_id)
            return _stop_for_state(run_id, state, RunStopReason.INTERRUPTED)
        if budget_stop is not None:
            return budget_stop
        if decision.code in {"CONTINUE", "MALFORMED_ACTION", "ACTION_RECORDED"}:
            return self._drive_permitted_phase(run_id, permit, context)
        state = self._store.load_runtime_state(run_id)
        return _stop_for_state(run_id, state, _stop_reason_for_decision(decision.stop_reason))

    def _resume_recovered(
        self,
        run_id: RunId,
        permit: RuntimePermit,
        action: RecoveredModelAction,
        context: RuntimeFaultContext,
    ) -> RunStop:
        decision = self._phase_drivers.resume_recovered_model_action(run_id, permit, action)
        budget = self._boundary(run_id)
        if budget is not None:
            return budget
        if decision.code in {"CONTINUE", "MALFORMED_ACTION", "ACTION_RECORDED"}:
            return self._drive_permitted_phase(run_id, permit, context)
        state = self._store.load_runtime_state(run_id)
        return _stop_for_state(run_id, state, _stop_reason_for_decision(decision.stop_reason))

    def _drive_permitted_phase(
        self, run_id: RunId, permit: RuntimePermit, context: RuntimeFaultContext
    ) -> RunStop:
        draft_initialized = False
        private_ref_initialized = False
        while True:
            context.phase = "POST_BARRIER"
            owner_id = permit.consumed_owner_id
            if owner_id is None:
                raise StateConflict("RUNTIME_OWNER_BINDING_MISSING")
            pending = self._store.apply_post_barrier_controls(
                run_id,
                owner_id,
                permit.generation,
                self._store.audit_sequence(run_id),
            )
            if pending is not None:
                state = self._store.load_runtime_state(run_id)
                return _stop_for_state(
                    run_id, state, _stop_reason_for_decision(pending.stop_reason)
                )
            state = self._store.load_runtime_state(run_id)
            allowed = state.state.value == permit.allowed_phase
            allowed = allowed or (
                permit.allowed_phase == "DRAFT"
                and draft_initialized
                and state.state == RunState.PLANNING
            )
            allowed = allowed or (
                permit.allowed_phase == "READY_TO_START"
                and private_ref_initialized
                and state.state == RunState.ACTIVE
            )
            if not allowed:
                return _stop_for_state(run_id, state, RunStopReason.PAUSED)
            budget = self._boundary(run_id)
            if budget is not None:
                return budget
            if state.state == RunState.DRAFT:
                context.phase = "TARGET_RESERVATION"
                decision = self._phase_drivers.initialize_target_reservation(run_id, permit)
                draft_initialized = decision.phase_transition == "TARGET_RESERVATION_INITIALIZED"
            elif state.state == RunState.PLANNING:
                context.phase = "PLANNING"
                decision = self._coordinator.run_planning_turn(run_id)
            elif state.state == RunState.READY_TO_START:
                context.phase = "PHASE_DRIVER"
                decision = self._phase_drivers.initialize_private_ref(run_id, permit)
                private_ref_initialized = decision.phase_transition == "PRIVATE_REF_INITIALIZED"
            elif state.state in {RunState.ACTIVE, RunState.PAUSED}:
                context.phase = "WORKER_SCHEDULING"
                decision = self._coordinator.schedule(run_id)
            elif state.state == RunState.INDETERMINATE:
                context.phase = "PHASE_DRIVER"
                decision = self._phase_drivers.resume_resolution(run_id, permit)
            elif state.state == RunState.READY_FOR_APPROVAL:
                context.phase = "PHASE_DRIVER"
                decision = self._phase_drivers.integrate_candidate(run_id, permit)
            else:
                return _stop_for_state(run_id, state, RunStopReason.TERMINAL)
            budget = self._boundary(run_id)
            if budget is not None:
                return budget
            if decision.code not in {"CONTINUE", "MALFORMED_ACTION", "ACTION_RECORDED"}:
                current = self._store.load_runtime_state(run_id)
                return _stop_for_state(
                    run_id, current, _stop_reason_for_decision(decision.stop_reason)
                )
