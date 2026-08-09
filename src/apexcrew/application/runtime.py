from __future__ import annotations

import os
import secrets
from collections.abc import Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from dataclasses import asdict, dataclass
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
from apexcrew.domain.authority import (
    ActiveRunTimeBoundaryDecision,
    GrantedActionIntent,
    PendingAction,
)
from apexcrew.domain.commands import (
    ActionApprovalPending,
    ApprovalPending,
    FinalApprovalPending,
    PlanApprovalPending,
    RunStop,
    RuntimeDecision,
    RuntimePermit,
    RuntimeState,
)
from apexcrew.domain.effects import (
    ApplyResolutionRequest,
    EffectIntent,
    RecoveryActionClass,
    RecoveryObservation,
    RecoveryOutcome,
    StateConflict,
    TargetReservation,
    abandon_observation,
    abandon_successor_for,
    canonical_json,
    observation_set_digest,
    recover_observation,
    recovery_action_class_for_intent,
    sha256_digest,
)
from apexcrew.domain.indeterminate import (
    ResolutionApplication,
    ResolutionSelection,
    UnresolvedIntentBinding,
    UnresolvedIntentSet,
)
from apexcrew.domain.model import (
    CommittedModelTurn,
    DurableModelClient,
    LogicalTurnId,
    ModelRecoveryBinding,
    ModelRequest,
    ModelRequestIntent,
    ProviderAttemptKind,
    ProviderAttemptResult,
    RecoveredModelAction,
    SettledModelAttempt,
    model_request_from_json,
)
from apexcrew.domain.reservation_cleanup import CleanupObservation, CleanupObservationKind
from apexcrew.domain.revisions import Sha256DigestText
from apexcrew.domain.tools import (
    GrantedActionJournal,
    GrantedActionToolPort,
    ToolIntent,
    ToolResult,
)
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
    def acquire(
        self, run_id: RunId, permit: RuntimePermit | None | object = None
    ) -> AbstractContextManager[RuntimeOwner | None]:
        raise NotImplementedError


class FileLockBackend(Protocol):
    def try_lock(self, path: Path) -> AbstractContextManager[bool]:
        raise NotImplementedError


class RuntimeOwnerIdSource(Protocol):
    def next_runtime_owner_id(self) -> RuntimeOwnerId:
        raise NotImplementedError


class LocalFileLockBackend:
    """Use an OS advisory lock for one Run's cross-process runtime interval."""

    @contextmanager
    def try_lock(self, path: Path) -> Iterator[bool]:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a+b") as handle:
            handle.seek(0)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            try:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(  # type: ignore[attr-defined]
                        handle.fileno(),
                        fcntl.LOCK_EX | fcntl.LOCK_NB,  # type: ignore[attr-defined]
                    )
            except (ImportError, OSError):
                yield False
                return
            try:
                yield True
            finally:
                try:
                    if os.name == "nt":
                        import msvcrt

                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)  # type: ignore[attr-defined]
                except (ImportError, OSError):
                    pass


class ProcessRuntimeOwnerIds:
    def next_runtime_owner_id(self) -> RuntimeOwnerId:
        return RuntimeOwnerId(f"runtime-{os.getpid()}-{secrets.token_hex(8)}")


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
    def acquire(
        self, run_id: RunId, permit: RuntimePermit | None | object = None
    ) -> Iterator[RuntimeOwner | None]:
        del permit
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


class ResolutionObservationPort(Protocol):
    def observe(self, intent: EffectIntent, recovery_generation: int) -> RecoveryObservation:
        raise NotImplementedError


class ResolutionToolPort(Protocol):
    def execute(self, intent: ToolIntent) -> ToolResult:
        raise NotImplementedError

    def observe_recovery(self, intent: ToolIntent) -> tuple[str, ToolResult | None]:
        raise NotImplementedError


class ModelResolutionJournal(Protocol):
    def model_request(self, run_id: RunId, intent_id: IntentId) -> ModelRequestIntent:
        raise NotImplementedError

    def committed_model_turn(
        self, run_id: RunId, logical_turn_id: str
    ) -> CommittedModelTurn | None:
        raise NotImplementedError

    def model_attempts(
        self, run_id: RunId, logical_turn_id: str
    ) -> tuple[SettledModelAttempt, ...]:
        raise NotImplementedError

    def audit_sequence(self, run_id: RunId) -> AuditSequence:
        raise NotImplementedError


class ModelProviderLookup(Protocol):
    def lookup(
        self, request: ModelRequest, provider_response_id: str | None
    ) -> ProviderAttemptResult | None:
        raise NotImplementedError


class ResolutionObservationRegistry(ResolutionObservationPort):
    """Dispatch action-class observers while retaining a fail-closed fallback."""

    def __init__(
        self,
        observers: Mapping[str, ResolutionObservationPort] | None = None,
    ) -> None:
        self._observers = dict(observers or {})

    def observe(self, intent: EffectIntent, recovery_generation: int) -> RecoveryObservation:
        observer = self._observers.get(intent.kind, self)
        if observer is not self:
            return observer.observe(intent, recovery_generation)
        return _unavailable_resolution_observation(intent, recovery_generation)


class GrantedActionResolutionObserver(ResolutionObservationPort):
    """Adapt the existing granted-workspace observer to resolution evidence."""

    def __init__(self, journal: GrantedActionJournal, tools: GrantedActionToolPort) -> None:
        self._journal = journal
        self._tools = tools

    def observe(self, intent: EffectIntent, recovery_generation: int) -> RecoveryObservation:
        granted = self._journal.require_granted_action_for_recovery(intent.intent_id)
        if granted.bindings.run_id != intent.run_id:
            raise StateConflict("GRANTED_OBSERVATION_RUN_BINDING_MISMATCH")
        observed = self._tools.observe_granted_action(granted)
        values: dict[str, object] = {
            "kind": RecoveryActionClass.GRANTED_ACTION,
            "intent_id": intent.intent_id,
            "recovery_generation": recovery_generation,
            "source_payload_digest": intent.payload_digest,
            "state": {
                "EXACT_PRE": "EXACT_PRE",
                "EXACT_POST": "EXACT_POST",
                "THIRD": "THIRD_STATE",
                "UNAVAILABLE": "UNAVAILABLE",
            }[observed.state],
            "idempotency_key": intent.idempotency_key,
            "pending_action_id": str(granted.pending_id),
            "grant_id": str(granted.grant_id),
            "expected_prestate_digest": sha256_digest(granted.expected_pre_state.canonical_json()),
            "action_binding_digest": sha256_digest(canonical_json(asdict(granted.bindings))),
        }
        if observed.state == "EXACT_POST":
            proof = {
                "state": "EXACT_POST",
                "pending_action_id": str(granted.pending_id),
                "grant_id": str(granted.grant_id),
                "expected_prestate_digest": values["expected_prestate_digest"],
                "action_binding_digest": values["action_binding_digest"],
            }
            proof_json = canonical_json(proof)
            values.update(
                {
                    "run_id": intent.run_id,
                    "settled_sequence": AuditSequence(
                        self._journal.audit_sequence(intent.run_id) + 1
                    ),
                    "applicable_revision_digests": intent.applicable_revision_digests,
                    "completion_proof_json": proof_json,
                    "completion_proof_digest": sha256_digest(proof_json),
                }
            )
        return RecoveryObservation.create(**values)


class SnapshotResolutionObserver(ResolutionObservationPort):
    """Use the existing bounded tool runtime for read/search recovery only."""

    def __init__(self, journal: ResolutionStateJournal, tools: ResolutionToolPort) -> None:
        self._journal = journal
        self._tools = tools

    def observe(self, intent: EffectIntent, recovery_generation: int) -> RecoveryObservation:
        tool_intent = ToolIntent.from_effect_intent(intent)
        if tool_intent.action.kind not in {"read", "search"}:
            return _unavailable_resolution_observation(intent, recovery_generation)
        result = self._tools.execute(tool_intent)
        if (
            result.code not in {"READ_COMPLETED", "SEARCH_COMPLETED"}
            or result.run_id not in {None, intent.run_id}
            or result.intent_id not in {None, intent.intent_id}
        ):
            return _unavailable_resolution_observation(intent, recovery_generation)
        bounded_result = canonical_json(
            {
                "action": tool_intent.action.model_dump(mode="json"),
                "result": result.model_dump(mode="json"),
            }
        )
        ordering_digest = sha256_digest(
            canonical_json({"action": tool_intent.action.model_dump(mode="json")})
        )
        return RecoveryObservation.create(
            kind=RecoveryActionClass.READ_SEARCH,
            intent_id=intent.intent_id,
            recovery_generation=recovery_generation,
            source_payload_digest=intent.payload_digest,
            state="EXACT_SNAPSHOT",
            run_id=intent.run_id,
            settled_sequence=AuditSequence(self._journal.audit_sequence(intent.run_id) + 1),
            applicable_revision_digests=intent.applicable_revision_digests,
            idempotency_key=intent.idempotency_key,
            snapshot_digest=tool_intent.snapshot_digest,
            scope_digest=tool_intent.scope_digest,
            ordering_digest=ordering_digest,
            bounded_result_json=bounded_result,
            bounded_result_digest=sha256_digest(bounded_result),
        )


class ModelResolutionObserver(ResolutionObservationPort):
    """Project a durable provider completion into recovery evidence."""

    def __init__(
        self, journal: ModelResolutionJournal, provider_lookup: ModelProviderLookup
    ) -> None:
        self._journal = journal
        self._provider_lookup = provider_lookup

    def observe(self, intent: EffectIntent, recovery_generation: int) -> RecoveryObservation:
        try:
            request = self._journal.model_request(intent.run_id, intent.intent_id)
            turn = self._journal.committed_model_turn(intent.run_id, request.logical_turn_id)
            attempts = self._journal.model_attempts(intent.run_id, request.logical_turn_id)
            payload_request = model_request_from_json(intent.normalized_payload_json)
        except (AttributeError, KeyError, StateConflict, ValueError, TypeError):
            return _unavailable_resolution_observation(intent, recovery_generation)
        if (
            request.intent_id != intent.intent_id
            or request.run_id != intent.run_id
            or request.request.idempotency_key != intent.idempotency_key
            or intent.payload_digest != sha256_digest(intent.normalized_payload_json)
            or payload_request != request.request
            or (
                turn is not None
                and turn.recovery_binding.request_digest != request.request.request_digest
            )
        ):
            return _unavailable_resolution_observation(intent, recovery_generation)
        completed = next(
            (
                attempt
                for attempt in attempts
                if attempt.kind is ProviderAttemptKind.COMPLETED
                and attempt.intent_id == request.intent_id
                and attempt.run_id == request.run_id
                and attempt.logical_turn_id == request.logical_turn_id
                and attempt.request == request.request
                and (turn is None or attempt.dispatch_result == turn.dispatch_result)
            ),
            None,
        )
        provider_response_id = None if completed is None else completed.provider_response_id
        if provider_response_id is None:
            return _unavailable_resolution_observation(intent, recovery_generation)
        try:
            provider_result = self._provider_lookup.lookup(request.request, provider_response_id)
        except (OSError, AttributeError, KeyError, StateConflict, TypeError, ValueError):
            return _unavailable_resolution_observation(intent, recovery_generation)
        if (
            provider_result is None
            or provider_result.kind is not ProviderAttemptKind.COMPLETED
            or provider_result.completion is None
            or provider_result.completion.usage is None
            or provider_result.completion.requested_model_id != request.request.requested_model_id
            or provider_result.completion.returned_model_id not in request.request.allowed_model_ids
        ):
            return _unavailable_resolution_observation(intent, recovery_generation)
        provider_completion = provider_result.completion
        if completed is not None and (
            completed.provider_response_id != provider_completion.response_id
            or completed.reported_usage != provider_completion.usage
        ):
            return _unavailable_resolution_observation(intent, recovery_generation)
        if turn is not None and (
            turn.state != "COMPLETION_COMMITTED"
            or turn.returned_model_id != provider_completion.returned_model_id
            or turn.normalized_payload != provider_completion.normalized_action
            or sha256_digest(canonical_json(turn.normalized_payload))
            != Sha256DigestText(turn.normalized_output_digest)
        ):
            return _unavailable_resolution_observation(intent, recovery_generation)
        completion_json = canonical_json(provider_completion.normalized_action)
        normalized_completion_digest = sha256_digest(completion_json)
        usage = provider_completion.usage
        values: dict[str, object] = {
            "kind": RecoveryActionClass.MODEL,
            "intent_id": intent.intent_id,
            "recovery_generation": recovery_generation,
            "source_payload_digest": intent.payload_digest,
            "state": "EXACT_COMPLETION",
            "request_digest": request.request.request_digest,
            "idempotency_key": intent.idempotency_key,
            "provider_response_id": provider_completion.response_id,
            "returned_model_id": provider_completion.returned_model_id,
            "schema_digest": Sha256DigestText(request.request.tool_schema_digest),
            "usage_json": None
            if usage is None
            else canonical_json(
                {
                    "cost_usd": str(usage.cost_usd),
                    "input_tokens": usage.input_tokens,
                    "output_tokens": usage.output_tokens,
                }
            ),
            "normalized_completion_digest": normalized_completion_digest,
            "normalized_completion_json": completion_json,
            "completion_proof_json": completion_json,
            "completion_proof_digest": sha256_digest(completion_json),
            "run_id": intent.run_id,
            "settled_sequence": AuditSequence(self._journal.audit_sequence(intent.run_id) + 1),
            "applicable_revision_digests": intent.applicable_revision_digests,
        }
        return RecoveryObservation.create(**values)


class ToolActionResolutionObserver(ResolutionObservationPort):
    """Use the bounded tool runtime's action-specific recovery observation."""

    def __init__(self, journal: ResolutionStateJournal, tools: ResolutionToolPort) -> None:
        self._journal = journal
        self._tools = tools

    def observe(self, intent: EffectIntent, recovery_generation: int) -> RecoveryObservation:
        try:
            tool_intent = ToolIntent.from_effect_intent(intent)
            state, result = self._tools.observe_recovery(tool_intent)
        except (AttributeError, RuntimeError, StateConflict, TypeError, ValueError):
            return _unavailable_resolution_observation(intent, recovery_generation)
        if tool_intent.action.kind == "patch":
            if state not in {"EXACT_PRE", "EXACT_POST", "THIRD_STATE"}:
                return _unavailable_resolution_observation(intent, recovery_generation)
            assert result is None or result.code == "PATCH_APPLIED"
            expected_post = tool_intent.expected_poststate_digest
            if expected_post is None:
                return _unavailable_resolution_observation(intent, recovery_generation)
            values: dict[str, object] = {
                "kind": RecoveryActionClass.PATCH,
                "intent_id": intent.intent_id,
                "recovery_generation": recovery_generation,
                "source_payload_digest": intent.payload_digest,
                "state": state,
                "idempotency_key": intent.idempotency_key,
                "expected_pre_tree_digest": tool_intent.snapshot_digest,
                "observed_post_tree_digest": expected_post,
                "snapshot_digest": tool_intent.snapshot_digest,
            }
            if state == "EXACT_POST" and result is not None:
                proof = canonical_json(result.model_dump(mode="json"))
                values.update(
                    {
                        "run_id": intent.run_id,
                        "settled_sequence": AuditSequence(
                            self._journal.audit_sequence(intent.run_id) + 1
                        ),
                        "applicable_revision_digests": intent.applicable_revision_digests,
                        "completion_proof_json": proof,
                        "completion_proof_digest": sha256_digest(proof),
                    }
                )
            return RecoveryObservation.create(**values)
        if tool_intent.action.kind == "check" and state == "EXACT_RECEIPT" and result is not None:
            bounded = dict(result.bounded_payload)
            proof = canonical_json(result.model_dump(mode="json"))
            return RecoveryObservation.create(
                kind=RecoveryActionClass.CHECK,
                intent_id=intent.intent_id,
                recovery_generation=recovery_generation,
                source_payload_digest=intent.payload_digest,
                state="EXACT_RECEIPT",
                idempotency_key=intent.idempotency_key,
                check_id=tool_intent.action.check_id,
                argv_digest=Sha256DigestText(str(bounded["argv_digest"])),
                snapshot_digest=tool_intent.snapshot_digest,
                receipt_digest=Sha256DigestText(str(bounded["receipt_digest"])),
                run_id=intent.run_id,
                settled_sequence=AuditSequence(self._journal.audit_sequence(intent.run_id) + 1),
                applicable_revision_digests=intent.applicable_revision_digests,
                completion_proof_json=proof,
                completion_proof_digest=sha256_digest(proof),
            )
        return _unavailable_resolution_observation(intent, recovery_generation)


def _unavailable_resolution_observation(
    intent: EffectIntent, recovery_generation: int
) -> RecoveryObservation:
    zero = Sha256DigestText("sha256:" + "0" * 64)
    action_class = recovery_action_class_for_intent(intent)
    values: dict[str, object] = {
        "kind": action_class,
        "intent_id": intent.intent_id,
        "recovery_generation": recovery_generation,
        "source_payload_digest": intent.payload_digest,
        "state": "UNAVAILABLE",
        "run_id": intent.run_id,
        "idempotency_key": intent.idempotency_key,
    }
    required = {
        RecoveryActionClass.MODEL: {"request_digest": intent.payload_digest},
        RecoveryActionClass.READ_SEARCH: {
            "snapshot_digest": zero,
            "scope_digest": zero,
            "ordering_digest": zero,
        },
        RecoveryActionClass.PATCH: {
            "expected_pre_tree_digest": zero,
            "observed_post_tree_digest": zero,
            "snapshot_digest": zero,
        },
        RecoveryActionClass.CHECK: {
            "check_id": "unavailable",
            "argv_digest": zero,
            "snapshot_digest": zero,
        },
        RecoveryActionClass.PRIVATE_REF: {
            "repository_id": "unavailable",
            "repository_instance_digest": zero,
            "ref_name": "unavailable",
            "registration_digest": zero,
            "target_safety_digest": zero,
            "old_oid": "0" * 40,
            "prepared_oid": "1" * 40,
            "current_oid": "2" * 40,
        },
        RecoveryActionClass.TARGET_CAS: {
            "repository_id": "unavailable",
            "repository_instance_digest": zero,
            "ref_name": "unavailable",
            "registration_digest": zero,
            "target_safety_digest": zero,
            "old_oid": "0" * 40,
            "prepared_oid": "1" * 40,
            "current_oid": "2" * 40,
        },
        RecoveryActionClass.TARGET_RESERVATION: {
            "registration_identity": "unavailable",
            "reservation_operation": "CREATE",
            "admin_binding_digest": zero,
            "path_identity": "unavailable",
            "gitfile_digest": zero,
        },
        RecoveryActionClass.GRANTED_ACTION: {
            "pending_action_id": "unavailable",
            "grant_id": "unavailable",
            "expected_prestate_digest": zero,
            "action_binding_digest": zero,
        },
    }
    values.update(required[action_class])
    if action_class is RecoveryActionClass.MODEL:
        values["reservation_charge"] = "FULL"
    return RecoveryObservation.create(**values)


class ResolutionRuntime(ResolutionDriver):
    """Observe and atomically apply one Permit-bound indeterminate resolution."""

    def __init__(
        self,
        journal: ResolutionStateJournal,
        observer: ResolutionObservationPort,
    ) -> None:
        self._journal = journal
        self._observer = observer

    def resume(self, run_id: RunId, permit: RuntimePermit) -> RuntimeDecision:
        if (
            permit.run_id != run_id
            or permit.state != "CONSUMED"
            or permit.allowed_phase != "INDETERMINATE"
            or permit.consumed_owner_id is None
            or permit.resolution_selection is None
        ):
            raise ValueError("INDETERMINATE_RESOLUTION_PERMIT_PHASE_MISMATCH")
        selection = permit.resolution_selection
        unresolved = self._journal.unresolved_intent_set(run_id)
        if unresolved is None or unresolved.set_digest != selection.unresolved_set_digest:
            raise StateConflict("STALE_UNRESOLVED_SET")
        member_bindings = unresolved.member_bindings
        members: tuple[UnresolvedIntentBinding, ...]
        if selection.intent_id is not None:
            selected = next(
                (member for member in member_bindings if member.intent_id == selection.intent_id),
                None,
            )
            if selected is None or selected.recovery_generation != selection.recovery_generation:
                raise StateConflict("STALE_UNRESOLVED_MEMBER")
            members = (selected,)
        else:
            members = member_bindings
        observations = tuple(
            self._observe_member(
                run_id,
                self._journal.effect_intent(IntentId(member.intent_id)),
                member.recovery_generation,
            )
            for member in members
        )
        self._validate_member_strategy(
            selection,
            observations,
            expected_sequence=self._journal.audit_sequence(run_id),
        )
        application = self._journal.apply_indeterminate_resolution(
            ApplyResolutionRequest(
                run_id=run_id,
                selection=selection,
                permit_generation=permit.generation,
                owner_id=permit.consumed_owner_id,
                expected_sequence=self._journal.audit_sequence(run_id),
                intent_ids=tuple(IntentId(member.intent_id) for member in member_bindings),
                payload_digests=tuple(member.intent_digest for member in member_bindings),
                recovery_generations=tuple(
                    member.recovery_generation for member in member_bindings
                ),
                observations=observations,
                observation_set_digest=observation_set_digest(observations),
            )
        )
        return _resolution_application_decision(application)

    def _observe_member(
        self, run_id: RunId, intent: EffectIntent, recovery_generation: int
    ) -> RecoveryObservation:
        if intent.run_id != run_id:
            raise StateConflict("OBSERVATION_RUN_BINDING_MISMATCH")
        observation = self._observer.observe(intent, recovery_generation)
        if (
            observation.intent_id != intent.intent_id
            or observation.run_id not in {None, run_id}
            or observation.recovery_generation != recovery_generation
            or observation.source_payload_digest != intent.payload_digest
            or observation.kind != recovery_action_class_for_intent(intent)
        ):
            raise StateConflict("OBSERVATION_BINDING_MISMATCH")
        return observation

    @staticmethod
    def _validate_member_strategy(
        selection: ResolutionSelection,
        observations: tuple[RecoveryObservation, ...],
        *,
        expected_sequence: AuditSequence,
    ) -> None:
        if selection.resolution in {"FAIL_RUN", "CANCEL_RUN"}:
            if len(observations) == 0:
                raise StateConflict("SET_RESOLUTION_OBSERVATION_REQUIRED")
            try:
                for observation in observations:
                    abandon_observation(observation, abandon_successor_for(observation))
            except (TypeError, ValueError) as error:
                raise StateConflict("SET_RESOLUTION_OBSERVATION_NOT_ABANDONABLE") from error
            return
        if len(observations) != 1:
            raise StateConflict("OBSERVATION_SET_CARDINALITY_MISMATCH")
        observation = observations[0]
        if selection.resolution == "RECONCILE_OBSERVED":
            decision = recover_observation(observation)
            if (
                decision.kind.value != "COMPLETED"
                or decision.effect_result is None
                or decision.effect_result.settled_sequence != AuditSequence(expected_sequence + 1)
            ):
                raise StateConflict("OBSERVED_RECOVERY_COMPLETION_REQUIRED")
        elif selection.resolution == "RETRY_SAME_INTENT":
            decision = recover_observation(observation)
            if decision.kind.value != "RETRY_SAME_INTENT":
                raise StateConflict("RETRY_RECOVERY_PROOF_REQUIRED")
        else:
            abandon_observation(observation, abandon_successor_for(observation))


def _resolution_application_decision(application: ResolutionApplication) -> RuntimeDecision:
    if application.remaining_set_digest is not None or application.status == "DENIED":
        return RuntimeDecision.pause("INDETERMINATE", application.resulting_sequence)
    return RuntimeDecision.pause(application.successor, application.resulting_sequence)


class ResolutionStateJournal(Protocol):
    def audit_sequence(self, run_id: RunId) -> AuditSequence: ...

    def unresolved_intent_set(self, run_id: RunId) -> UnresolvedIntentSet | None: ...

    def effect_intent(self, intent_id: IntentId) -> EffectIntent: ...

    def apply_indeterminate_resolution(
        self, request: ApplyResolutionRequest
    ) -> ResolutionApplication: ...


class FinalIntegrationDriver(Protocol):
    def integrate(self, run_id: RunId, permit: RuntimePermit) -> RuntimeDecision:
        raise NotImplementedError


class TerminalCleanupDriver(Protocol):
    def reconcile(self, run_id: RunId, permit: RuntimePermit) -> RuntimeDecision:
        raise NotImplementedError


class TerminalCleanupState(Protocol):
    def target_reservation_for_run(self, run_id: RunId) -> TargetReservation:
        raise NotImplementedError

    def audit_sequence(self, run_id: RunId) -> AuditSequence:
        raise NotImplementedError

    def settle_terminal_cleanup(
        self,
        *,
        run_id: RunId,
        owner_id: RuntimeOwnerId,
        permit_generation: int,
        expected_sequence: AuditSequence,
    ) -> AuditSequence:
        raise NotImplementedError

    def record_terminal_cleanup_conflict(
        self,
        *,
        run_id: RunId,
        owner_id: RuntimeOwnerId,
        permit_generation: int,
        expected_sequence: AuditSequence,
    ) -> AuditSequence:
        raise NotImplementedError


class TargetReservationCleanup(Protocol):
    def observe(self, reservation: TargetReservation) -> CleanupObservation:
        raise NotImplementedError

    def apply_exact(self, reservation: TargetReservation, observation: CleanupObservation) -> None:
        raise NotImplementedError


class TerminalCleanupRuntime(TerminalCleanupDriver):
    """Run the exact terminal reservation cleanup under its administrative Permit."""

    def __init__(self, state: TerminalCleanupState, cleanup: TargetReservationCleanup) -> None:
        self._state = state
        self._cleanup = cleanup

    def reconcile(self, run_id: RunId, permit: RuntimePermit) -> RuntimeDecision:
        owner_id = permit.consumed_owner_id
        if (
            permit.run_id != run_id
            or permit.state != "CONSUMED"
            or permit.allowed_phase != "TERMINAL_ADMINISTRATION"
            or owner_id is None
        ):
            raise ValueError("TERMINAL_CLEANUP_PERMIT_PHASE_MISMATCH")
        reservation = self._state.target_reservation_for_run(run_id)
        observation = self._cleanup.observe(reservation)
        if observation.kind is CleanupObservationKind.CONFLICT:
            sequence = self._state.record_terminal_cleanup_conflict(
                run_id=run_id,
                owner_id=owner_id,
                permit_generation=permit.generation,
                expected_sequence=self._state.audit_sequence(run_id),
            )
            return RuntimeDecision.pause("TARGET_RESERVATION_CLEANUP_CONFLICT", sequence)
        try:
            self._cleanup.apply_exact(reservation, observation)
        except (RuntimeError, OSError):
            sequence = self._state.record_terminal_cleanup_conflict(
                run_id=run_id,
                owner_id=owner_id,
                permit_generation=permit.generation,
                expected_sequence=self._state.audit_sequence(run_id),
            )
            return RuntimeDecision.pause("TARGET_RESERVATION_CLEANUP_CONFLICT", sequence)
        sequence = self._state.settle_terminal_cleanup(
            run_id=run_id,
            owner_id=owner_id,
            permit_generation=permit.generation,
            expected_sequence=self._state.audit_sequence(run_id),
        )
        return RuntimeDecision.pause("TERMINAL", sequence)


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
    def unconsumed_permit(self, run_id: RunId) -> RuntimePermit: ...
    def proposed_plan(self, run_id: RunId): ...  # type: ignore[no-untyped-def]
    def final_candidate(self, run_id: RunId): ...  # type: ignore[no-untyped-def]
    def pending_actions(self, run_id: RunId) -> tuple[PendingAction, ...]: ...
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
    def audit_sequence(self, run_id: RunId) -> AuditSequence: ...

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
        "READY_FOR_APPROVAL": RunStopReason.AWAITING_FINAL_APPROVAL,
        "BUDGET_STOP": RunStopReason.BUDGET_STOP,
        "INDETERMINATE": RunStopReason.INDETERMINATE,
        "TERMINAL": RunStopReason.TERMINAL,
    }.get(reason, RunStopReason.PAUSED)


def _stop_for_state(
    run_id: RunId,
    state: RuntimeState,
    reason: RunStopReason,
    pending: ApprovalPending | None = None,
) -> RunStop:
    return RunStop(
        run_id=run_id,
        state=state.state,
        reason=reason,
        last_sequence=state.sequence,
        pending=pending,
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
        provider_dispatch_authorized: bool = True,
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
        self._provider_dispatch_authorized = provider_dispatch_authorized

    def _pending_for_reason(self, run_id: RunId, reason: RunStopReason) -> ApprovalPending | None:
        if reason == RunStopReason.AWAITING_PLAN_APPROVAL:
            return PlanApprovalPending(plan_digest=self._store.proposed_plan(run_id).plan_digest)
        if reason == RunStopReason.AWAITING_ACTION_APPROVAL:
            pending_ids = tuple(
                item.pending_id
                for item in self._store.pending_actions(run_id)
                if item.state == "WAITING_APPROVAL"
            )
            return ActionApprovalPending(pending_action_ids=pending_ids)
        if reason == RunStopReason.AWAITING_FINAL_APPROVAL:
            return FinalApprovalPending(
                candidate_id=self._store.final_candidate(run_id).candidate_id
            )
        return None

    def run_until_blocked(self, run_id: RunId) -> RunStop:
        state = self._store.load_runtime_state(run_id)
        try:
            pending_permit = self._store.unconsumed_permit(run_id)
        except StateConflict as error:
            if str(error) != "RUNTIME_PERMIT_NOT_FOUND":
                raise
            return _stop_for_state(run_id, state, RunStopReason.NO_RUNTIME_PERMIT)
        if not self._provider_dispatch_authorized:
            raise RuntimeError("LIVE_PROVIDER_NOT_AUTHORIZED")
        with self._ownership.acquire(run_id, pending_permit) as owner:
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
        if permit.allowed_phase == "INDETERMINATE":
            return self._drive_permitted_phase(run_id, permit, context)
        context.phase = "RECOVERED_ACTION"
        recovered = self._journal.next_recovered_model_action(run_id)
        if recovered is not None:
            return self._resume_recovered(run_id, permit, recovered, context)
        context.phase = "GRANTED_ACTION_RECOVERY"
        granted = self._journal.next_unsettled_granted_action(run_id)
        if granted is not None:
            return self._drive_granted_action(run_id, permit, granted.intent_id, context)
        if permit.allowed_phase == "TERMINAL_ADMINISTRATION":
            return self._drive_permitted_phase(run_id, permit, context)
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
        reason = _stop_reason_for_decision(decision.stop_reason)
        return _stop_for_state(run_id, state, reason, self._pending_for_reason(run_id, reason))

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
        reason = _stop_reason_for_decision(decision.stop_reason)
        return _stop_for_state(run_id, state, reason, self._pending_for_reason(run_id, reason))

    def _drive_permitted_phase(
        self, run_id: RunId, permit: RuntimePermit, context: RuntimeFaultContext
    ) -> RunStop:
        draft_initialized = False
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
                reason = _stop_reason_for_decision(pending.stop_reason)
                return _stop_for_state(
                    run_id, state, reason, self._pending_for_reason(run_id, reason)
                )
            state = self._store.load_runtime_state(run_id)
            terminal_state = state.state in {
                RunState.COMPLETED,
                RunState.FAILED,
                RunState.CANCELLED,
            }
            if terminal_state and permit.allowed_phase != "TERMINAL_ADMINISTRATION":
                return _stop_for_state(run_id, state, RunStopReason.TERMINAL)
            allowed = state.state.value == permit.allowed_phase
            allowed = allowed or (
                terminal_state and permit.allowed_phase == "TERMINAL_ADMINISTRATION"
            )
            allowed = allowed or (
                permit.allowed_phase == "DRAFT"
                and draft_initialized
                and state.state == RunState.PLANNING
            )
            allowed = allowed or (
                permit.allowed_phase == "READY_TO_START" and state.state == RunState.ACTIVE
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
            elif state.state in {RunState.ACTIVE, RunState.PAUSED}:
                context.phase = "WORKER_SCHEDULING"
                decision = self._coordinator.schedule(run_id)
            elif state.state == RunState.INDETERMINATE:
                context.phase = "PHASE_DRIVER"
                decision = self._phase_drivers.resume_resolution(run_id, permit)
            elif state.state == RunState.READY_FOR_APPROVAL:
                context.phase = "PHASE_DRIVER"
                decision = self._phase_drivers.integrate_candidate(run_id, permit)
            elif terminal_state and permit.allowed_phase == "TERMINAL_ADMINISTRATION":
                context.phase = "PHASE_DRIVER"
                decision = self._phase_drivers.reconcile_terminal_cleanup(run_id, permit)
            else:
                return _stop_for_state(run_id, state, RunStopReason.TERMINAL)
            budget = self._boundary(run_id)
            if budget is not None:
                return budget
            if decision.code not in {"CONTINUE", "MALFORMED_ACTION", "ACTION_RECORDED"}:
                current = self._store.load_runtime_state(run_id)
                reason = _stop_reason_for_decision(decision.stop_reason)
                return _stop_for_state(
                    run_id, current, reason, self._pending_for_reason(run_id, reason)
                )
