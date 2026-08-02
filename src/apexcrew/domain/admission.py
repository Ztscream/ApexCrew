from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from pydantic import Field

from apexcrew.domain.commands import ApplicableRevisionDigests, RuntimeDecision, RuntimePermit
from apexcrew.domain.effects import (
    EffectIntent,
    EffectResult,
    ReservationObservation,
    TargetReservation,
    canonical_json,
    classify_reservation_creation,
    sha256_digest,
)
from apexcrew.domain.revisions import FrozenDocument, Sha256DigestText
from apexcrew.domain.types import (
    AuditSequence,
    GitOid,
    IntentId,
    RepositoryId,
    RunId,
    RuntimeOwnerId,
)


class RepositoryEffectError(RuntimeError):
    """A known rejected Git operation; the persisted intent remains recoverable."""


class RepositoryEffectUncertain(RepositoryEffectError):
    """Observation could not prove the external operation's post-state."""


class TargetReservationCreationIntent(FrozenDocument):
    intent_id: IntentId
    run_id: RunId
    reservation_id: str
    repository_id: RepositoryId
    target_ref: str
    pinned_target_oid: GitOid
    reservation_path: str
    repository_instance_digest: Sha256DigestText
    applicable_revision_digests: ApplicableRevisionDigests
    target_authority_digest: Sha256DigestText
    idempotency_key: str
    recorded_sequence: AuditSequence = Field(default=AuditSequence(0), exclude=True)

    def to_effect_intent(self, recorded_sequence: AuditSequence) -> EffectIntent:
        payload = canonical_json(self.model_dump(mode="json"))
        return EffectIntent(
            intent_id=self.intent_id,
            run_id=self.run_id,
            kind="target_reservation_creation",
            idempotency_key=self.idempotency_key,
            applicable_revision_digests=self.applicable_revision_digests,
            payload_digest=sha256_digest(payload),
            normalized_payload_json=payload,
            recorded_sequence=recorded_sequence,
            expected_prestate_json=canonical_json(
                {
                    "reservation_id": self.reservation_id,
                    "target_ref": self.target_ref,
                    "pinned_target_oid": self.pinned_target_oid,
                    "reservation_path": self.reservation_path,
                    "repository_instance_digest": self.repository_instance_digest,
                    "target_authority_digest": self.target_authority_digest,
                }
            ),
        )

    @classmethod
    def from_effect_intent(cls, effect: EffectIntent) -> TargetReservationCreationIntent:
        value = cls.model_validate_json(effect.normalized_payload_json).model_copy(
            update={"recorded_sequence": effect.recorded_sequence}
        )
        if value.to_effect_intent(effect.recorded_sequence) != effect:
            raise ValueError("TARGET_RESERVATION_EFFECT_BINDING_MISMATCH")
        return value


class TargetReservationOperation(FrozenDocument):
    intent_id: IntentId
    run_id: RunId
    reservation_id: str
    kind: Literal["ADD_NO_CHECKOUT", "LOCK"]
    repository_id: RepositoryId
    repository_instance_digest: Sha256DigestText
    target_ref: str
    pinned_target_oid: GitOid
    reservation_path: str
    target_authority_digest: Sha256DigestText
    lock_reason: str

    def applied(self) -> TargetReservationOperationResult:
        return TargetReservationOperationResult(intent_id=self.intent_id, kind=self.kind)


class TargetReservationOperationResult(FrozenDocument):
    intent_id: IntentId
    kind: Literal["ADD_NO_CHECKOUT", "LOCK"]


class TargetReservationGitPort(Protocol):
    def apply(self, operation: TargetReservationOperation) -> TargetReservationOperationResult:
        raise NotImplementedError


class TargetReservationObserver(Protocol):
    def observe(self, reservation: TargetReservation) -> ReservationObservation:
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class ReservationRegistrationObservation:
    registration_present: bool
    locked: bool
    exact_identity: bool
    unexpected_registration: bool
    observable: bool
    admin_entry_name: str | None = None
    admin_binding_digest: Sha256DigestText | None = None


@dataclass(frozen=True, slots=True)
class ReservationPathObservation:
    path_present: bool
    gitfile_only: bool
    exact_back_reference: bool
    observable: bool


class TargetReservationRegistrationReader(Protocol):
    def observe_registration(
        self, reservation: TargetReservation
    ) -> ReservationRegistrationObservation:
        raise NotImplementedError


class TargetReservationPathReader(Protocol):
    def observe_path(self, reservation: TargetReservation) -> ReservationPathObservation:
        raise NotImplementedError


class TargetReservationObservationService(TargetReservationObserver):
    def __init__(
        self,
        registrations: TargetReservationRegistrationReader,
        paths: TargetReservationPathReader,
    ) -> None:
        self._registrations = registrations
        self._paths = paths

    def observe(self, reservation: TargetReservation) -> ReservationObservation:
        registration = self._registrations.observe_registration(reservation)
        path = self._paths.observe_path(reservation)
        if not registration.observable or not path.observable:
            return ReservationObservation(False, False, False, False, False, observable=False)
        return ReservationObservation(
            registration_present=(
                registration.registration_present or registration.unexpected_registration
            ),
            path_present=path.path_present,
            locked=registration.locked,
            exact_identity=(
                registration.exact_identity
                and not registration.unexpected_registration
                and path.exact_back_reference
            ),
            gitfile_only=path.gitfile_only,
            admin_entry_name=registration.admin_entry_name,
            admin_binding_digest=registration.admin_binding_digest,
        )


class ReservationAdminObservation(FrozenDocument):
    admin_entry_name: str | None
    admin_binding_digest: Sha256DigestText | None


class TargetReservationWorktreeGuard(Protocol):
    def require_safe_before_list(self, reservation: TargetReservation) -> None:
        """No-follow-verify the Git worktree admin root before `worktree list`."""
        raise NotImplementedError

    def require_compatible_observation(
        self, reservation: TargetReservation
    ) -> ReservationAdminObservation:
        raise NotImplementedError

    def require_absent_before_add(self, operation: TargetReservationOperation) -> None:
        raise NotImplementedError

    def require_exact_registered_unlocked(self, operation: TargetReservationOperation) -> None:
        raise NotImplementedError

    def require_exact_post_operation(self, operation: TargetReservationOperation) -> None:
        raise NotImplementedError


class TargetReservationCreationOutcome(FrozenDocument):
    intent_id: IntentId
    run_id: RunId
    result_class: Literal["REGISTERED_LOCKED", "CONFLICT", "UNOBSERVABLE"]
    observed: ReservationObservation

    def to_effect_result(self, settled_sequence: AuditSequence) -> EffectResult:
        payload = canonical_json(self.model_dump(mode="json"))
        outcome: Literal["COMPLETED", "CONFLICT", "INDETERMINATE"]
        if self.result_class == "REGISTERED_LOCKED":
            outcome = "COMPLETED"
        elif self.result_class == "CONFLICT":
            outcome = "CONFLICT"
        else:
            outcome = "INDETERMINATE"
        return EffectResult(
            intent_id=self.intent_id,
            run_id=self.run_id,
            outcome=outcome,
            result_class="TARGET_RESERVATION_" + self.result_class,
            result_digest=sha256_digest(payload),
            bounded_result_json=payload,
            settled_sequence=settled_sequence,
        )


class TargetReservationAdmission(Protocol):
    def execute_creation(
        self, intent: TargetReservationCreationIntent
    ) -> TargetReservationCreationOutcome:
        raise NotImplementedError


class TargetReservationAdmissionService(TargetReservationAdmission):
    def __init__(self, observer: TargetReservationObserver, git: TargetReservationGitPort) -> None:
        self._observer = observer
        self._git = git

    def execute_creation(
        self, intent: TargetReservationCreationIntent
    ) -> TargetReservationCreationOutcome:
        reservation = TargetReservation(
            reservation_id=intent.reservation_id,
            run_id=intent.run_id,
            target_ref=intent.target_ref,
            pinned_target_oid=intent.pinned_target_oid,
            path=Path(intent.reservation_path),
            phase="CREATION_INTENT_RECORDED",
        )
        try:
            observed = self._observer.observe(reservation)
            next_step = classify_reservation_creation(observed)
            if next_step == "ADD":
                self._require_applied(
                    self._git.apply(self._operation(intent, "ADD_NO_CHECKOUT")),
                    intent,
                    "ADD_NO_CHECKOUT",
                )
                observed = self._observer.observe(reservation)
                next_step = classify_reservation_creation(observed)
            if next_step == "LOCK":
                self._require_applied(
                    self._git.apply(self._operation(intent, "LOCK")), intent, "LOCK"
                )
                observed = self._observer.observe(reservation)
                next_step = classify_reservation_creation(observed)
        except RepositoryEffectError:
            try:
                observed = self._observer.observe(reservation)
                next_step = classify_reservation_creation(observed)
            except RepositoryEffectError:
                observed = ReservationObservation(
                    False, False, False, False, False, observable=False
                )
                next_step = "UNOBSERVABLE"
        result_class: Literal["REGISTERED_LOCKED", "CONFLICT", "UNOBSERVABLE"]
        if next_step == "SETTLE":
            result_class = "REGISTERED_LOCKED"
        elif next_step == "CONFLICT":
            result_class = "CONFLICT"
        else:
            result_class = "UNOBSERVABLE"
        return TargetReservationCreationOutcome(
            intent_id=intent.intent_id,
            run_id=intent.run_id,
            result_class=result_class,
            observed=observed,
        )

    @staticmethod
    def _operation(
        intent: TargetReservationCreationIntent,
        kind: Literal["ADD_NO_CHECKOUT", "LOCK"],
    ) -> TargetReservationOperation:
        return TargetReservationOperation(
            intent_id=intent.intent_id,
            run_id=intent.run_id,
            reservation_id=intent.reservation_id,
            kind=kind,
            repository_id=intent.repository_id,
            repository_instance_digest=intent.repository_instance_digest,
            target_ref=intent.target_ref,
            pinned_target_oid=intent.pinned_target_oid,
            reservation_path=intent.reservation_path,
            target_authority_digest=intent.target_authority_digest,
            lock_reason=str(intent.run_id),
        )

    @staticmethod
    def _require_applied(
        result: TargetReservationOperationResult,
        intent: TargetReservationCreationIntent,
        kind: Literal["ADD_NO_CHECKOUT", "LOCK"],
    ) -> None:
        if result.intent_id != intent.intent_id or result.kind != kind:
            raise ValueError("TARGET_RESERVATION_OPERATION_RESULT_MISMATCH")


class TargetReservationBootstrapState(Protocol):
    def target_reservation_for_run(self, run_id: RunId) -> TargetReservation:
        raise NotImplementedError

    def audit_sequence(self, run_id: RunId) -> AuditSequence:
        raise NotImplementedError

    def record_or_load_target_reservation_creation_intent_under_draft_permit(
        self,
        run_id: RunId,
        owner_id: RuntimeOwnerId,
        permit_generation: int,
        *,
        expected_sequence: AuditSequence,
    ) -> TargetReservationCreationIntent:
        raise NotImplementedError

    def settle_target_reservation_creation_under_draft_permit(
        self,
        intent: TargetReservationCreationIntent,
        outcome: TargetReservationCreationOutcome,
        owner_id: RuntimeOwnerId,
        permit_generation: int,
        *,
        expected_sequence: AuditSequence,
    ) -> AuditSequence:
        raise NotImplementedError

    def reuse_locked_target_reservation_under_draft_permit(
        self,
        run_id: RunId,
        observed: ReservationObservation,
        owner_id: RuntimeOwnerId,
        permit_generation: int,
        *,
        expected_sequence: AuditSequence,
    ) -> AuditSequence:
        raise NotImplementedError

    def record_target_reservation_pre_intent_stop(
        self,
        run_id: RunId,
        observed: ReservationObservation,
        owner_id: RuntimeOwnerId,
        permit_generation: int,
        *,
        expected_sequence: AuditSequence,
    ) -> AuditSequence:
        raise NotImplementedError


class TargetReservationBootstrapAdmissionService:
    def __init__(
        self,
        state: TargetReservationBootstrapState,
        observer: TargetReservationObserver,
        effects: TargetReservationAdmission,
    ) -> None:
        self._state = state
        self._observer = observer
        self._effects = effects

    def initialize_target_reservation(
        self, run_id: RunId, permit: RuntimePermit
    ) -> RuntimeDecision:
        owner_id = permit.consumed_owner_id
        if (
            permit.run_id != run_id
            or permit.state != "CONSUMED"
            or permit.allowed_phase != "DRAFT"
            or owner_id is None
        ):
            raise ValueError("TARGET_RESERVATION_PERMIT_BINDING_MISMATCH")
        reservation = self._state.target_reservation_for_run(run_id)
        observed = self._observer.observe(reservation)
        if reservation.phase == "REGISTERED_LOCKED":
            if classify_reservation_creation(observed) == "SETTLE":
                sequence = self._state.reuse_locked_target_reservation_under_draft_permit(
                    run_id,
                    observed,
                    owner_id,
                    permit.generation,
                    expected_sequence=self._state.audit_sequence(run_id),
                )
                return RuntimeDecision(
                    code="CONTINUE",
                    resulting_sequence=sequence,
                    phase_transition="TARGET_RESERVATION_INITIALIZED",
                )
            sequence = self._state.record_target_reservation_pre_intent_stop(
                run_id,
                observed,
                owner_id,
                permit.generation,
                expected_sequence=self._state.audit_sequence(run_id),
            )
            return RuntimeDecision.pause("TARGET_RESERVATION_REUSE_NOT_EXACT", sequence)
        if reservation.phase == "ALLOCATED" and classify_reservation_creation(observed) != "ADD":
            sequence = self._state.record_target_reservation_pre_intent_stop(
                run_id,
                observed,
                owner_id,
                permit.generation,
                expected_sequence=self._state.audit_sequence(run_id),
            )
            return RuntimeDecision.pause("TARGET_RESERVATION_CONFLICT", sequence)
        intent = self._state.record_or_load_target_reservation_creation_intent_under_draft_permit(
            run_id,
            owner_id,
            permit.generation,
            expected_sequence=self._state.audit_sequence(run_id),
        )
        outcome = self._effects.execute_creation(intent)
        sequence = self._state.settle_target_reservation_creation_under_draft_permit(
            intent,
            outcome,
            owner_id,
            permit.generation,
            expected_sequence=self._state.audit_sequence(run_id),
        )
        if outcome.result_class == "REGISTERED_LOCKED":
            return RuntimeDecision(
                code="CONTINUE",
                resulting_sequence=sequence,
                phase_transition="TARGET_RESERVATION_INITIALIZED",
            )
        return RuntimeDecision.pause(
            "INDETERMINATE"
            if outcome.result_class == "UNOBSERVABLE"
            else "TARGET_RESERVATION_CONFLICT",
            sequence,
        )
