from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from secrets import token_hex
from typing import Literal, Protocol, Self, cast

from pydantic import Field, model_validator

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
    AttemptId,
    AuditSequence,
    CandidateId,
    EvidenceBundleDigest,
    GitOid,
    IntentId,
    RepositoryId,
    RunId,
    RunState,
    RuntimeOwnerId,
    TaskId,
)

MAX_TARGET_RESERVATION_ID_ATTEMPTS = 16
_TARGET_RESERVATION_ID_PREFIX = "reservation-"


def _task_candidate_digest(values: Mapping[str, object]) -> Sha256DigestText:
    return sha256_digest(canonical_json(values))


def _require_sha256_digest(value: str, field_name: str) -> None:
    if (
        len(value) != 71
        or not value.startswith("sha256:")
        or any(character not in "0123456789abcdef" for character in value[7:])
    ):
        raise ValueError(f"{field_name.upper()}_INVALID")


def task_candidate_lease_provenance_digest(
    *,
    attempt_id: AttemptId,
    lease_id: str,
    lease_generation: int,
    lease_base_head_oid: GitOid,
    lease_admissible_head_oid: GitOid,
    lease_issued_at_utc: datetime,
    lease_expires_at_utc: datetime,
    prepared_at_utc: datetime,
) -> Sha256DigestText:
    return sha256_digest(
        canonical_json(
            {
                "attempt_id": attempt_id,
                "lease_admissible_head_oid": lease_admissible_head_oid,
                "lease_base_head_oid": lease_base_head_oid,
                "lease_expires_at_utc": lease_expires_at_utc,
                "lease_generation": lease_generation,
                "lease_id": lease_id,
                "lease_issued_at_utc": lease_issued_at_utc,
                "prepared_at_utc": prepared_at_utc,
            }
        )
    )


class TaskCandidateGateBinding(FrozenDocument):
    """Immutable evidence and authority provenance for one prepared Attempt."""

    schema_version: Literal["task-candidate-gate-v1"] = "task-candidate-gate-v1"
    attempt_id: AttemptId
    task_contract_digest: Sha256DigestText
    base_run_head_oid: GitOid = Field(pattern=r"^[0-9a-f]{40}$")
    post_tree_oid: GitOid = Field(pattern=r"^[0-9a-f]{40}$")
    evidence_bundle_digest: EvidenceBundleDigest
    freshness_assessment_digest: Sha256DigestText
    freshness_status: Literal["FRESH", "STALE", "INDETERMINATE"]
    applicable_revision_digests: ApplicableRevisionDigests
    target_safety_digest: Sha256DigestText
    scope_digest: Sha256DigestText
    check_workspace_digest: Sha256DigestText
    policy_decision: Literal["ALLOW", "REQUIRE_APPROVAL", "DENY"]
    approval_binding_digest: Sha256DigestText | None = None
    approval_sequence: AuditSequence | None = Field(default=None, ge=1)
    lease_id: str = Field(min_length=1, max_length=256)
    lease_generation: int = Field(ge=1)
    lease_base_head_oid: GitOid = Field(pattern=r"^[0-9a-f]{40}$")
    lease_admissible_head_oid: GitOid = Field(pattern=r"^[0-9a-f]{40}$")
    lease_issued_at_utc: datetime
    lease_expires_at_utc: datetime
    prepared_at_utc: datetime
    lease_provenance_digest: Sha256DigestText

    @model_validator(mode="after")
    def validate_gate(self) -> Self:
        for field_name, value in (
            ("task_contract_digest", self.task_contract_digest),
            ("evidence_bundle_digest", self.evidence_bundle_digest),
            ("freshness_assessment_digest", self.freshness_assessment_digest),
            ("target_safety_digest", self.target_safety_digest),
            ("scope_digest", self.scope_digest),
            ("check_workspace_digest", self.check_workspace_digest),
            ("lease_provenance_digest", self.lease_provenance_digest),
        ):
            _require_sha256_digest(str(value), field_name)
        if any(digest is None for digest in self.applicable_revision_digests.model_dump().values()):
            raise ValueError("TASK_CANDIDATE_REVISIONS_INCOMPLETE")
        if self.freshness_status != "FRESH":
            raise ValueError("TASK_CANDIDATE_FRESHNESS_NOT_FRESH")
        if self.policy_decision == "DENY":
            raise ValueError("TASK_CANDIDATE_POLICY_DENIED")
        if (self.approval_binding_digest is None) != (self.approval_sequence is None):
            raise ValueError("TASK_CANDIDATE_APPROVAL_BINDING_INVALID")
        if self.approval_binding_digest is not None:
            _require_sha256_digest(str(self.approval_binding_digest), "approval_binding_digest")
        if self.policy_decision == "ALLOW" and self.approval_binding_digest is not None:
            raise ValueError("TASK_CANDIDATE_UNEXPECTED_APPROVAL")
        if self.policy_decision == "REQUIRE_APPROVAL" and self.approval_binding_digest is None:
            raise ValueError("TASK_CANDIDATE_APPROVAL_REQUIRED")
        if self.lease_base_head_oid != self.base_run_head_oid:
            raise ValueError("TASK_CANDIDATE_LEASE_BASE_MISMATCH")
        if self.lease_admissible_head_oid != self.base_run_head_oid:
            raise ValueError("TASK_CANDIDATE_LEASE_HEAD_MISMATCH")
        if self.prepared_at_utc < self.lease_issued_at_utc:
            raise ValueError("TASK_CANDIDATE_LEASE_PROVENANCE_INVALID")
        if self.prepared_at_utc >= self.lease_expires_at_utc:
            raise ValueError("TASK_CANDIDATE_LEASE_EXPIRED_AT_PREPARATION")
        expected = task_candidate_lease_provenance_digest(
            attempt_id=self.attempt_id,
            lease_id=self.lease_id,
            lease_generation=self.lease_generation,
            lease_base_head_oid=self.lease_base_head_oid,
            lease_admissible_head_oid=self.lease_admissible_head_oid,
            lease_issued_at_utc=self.lease_issued_at_utc,
            lease_expires_at_utc=self.lease_expires_at_utc,
            prepared_at_utc=self.prepared_at_utc,
        )
        if self.lease_provenance_digest != expected:
            raise ValueError("TASK_CANDIDATE_LEASE_PROVENANCE_MISMATCH")
        return self


class TaskCandidate(FrozenDocument):
    """An immutable prepared Attempt change awaiting private-ref promotion."""

    schema_version: Literal["task-candidate-v1"] = "task-candidate-v1"
    candidate_id: CandidateId
    run_id: RunId
    task_id: TaskId
    attempt_id: AttemptId
    expected_run_head_oid: GitOid = Field(pattern=r"^[0-9a-f]{40}$")
    prepared_oid: GitOid = Field(pattern=r"^[0-9a-f]{40}$")
    prepared_tree_oid: GitOid = Field(pattern=r"^[0-9a-f]{40}$")
    changed_paths: tuple[str, ...]
    gate_binding: TaskCandidateGateBinding | None = None
    state: Literal["READY", "PROMOTING", "PROMOTED", "CONFLICT", "INDETERMINATE"] = "READY"
    candidate_digest: Sha256DigestText

    @property
    def run_head_oid(self) -> GitOid:
        return self.expected_run_head_oid

    @classmethod
    def create(
        cls,
        *,
        run_id: RunId,
        task_id: TaskId,
        attempt_id: AttemptId,
        expected_run_head_oid: GitOid,
        prepared_oid: GitOid,
        changed_paths: tuple[str, ...],
        prepared_tree_oid: GitOid | None = None,
        gate_binding: TaskCandidateGateBinding | None = None,
    ) -> Self:
        post_tree_oid = (
            prepared_tree_oid
            if prepared_tree_oid is not None
            else (gate_binding.post_tree_oid if gate_binding is not None else prepared_oid)
        )
        identity = {
            "attempt_id": attempt_id,
            "changed_paths": changed_paths,
            "gate_binding": (
                None if gate_binding is None else gate_binding.model_dump(mode="json")
            ),
            "expected_run_head_oid": expected_run_head_oid,
            "prepared_oid": prepared_oid,
            "prepared_tree_oid": post_tree_oid,
            "run_id": run_id,
            "task_id": task_id,
        }
        digest = _task_candidate_digest(identity)
        candidate_id = CandidateId("task-candidate-" + str(digest).removeprefix("sha256:")[:24])
        return cls(
            candidate_id=candidate_id,
            run_id=run_id,
            task_id=task_id,
            attempt_id=attempt_id,
            expected_run_head_oid=expected_run_head_oid,
            prepared_oid=prepared_oid,
            prepared_tree_oid=post_tree_oid,
            changed_paths=changed_paths,
            gate_binding=gate_binding,
            candidate_digest=_task_candidate_digest({**identity, "candidate_id": candidate_id}),
        )

    @model_validator(mode="after")
    def validate_digest(self) -> Self:
        identity = {
            "attempt_id": self.attempt_id,
            "changed_paths": self.changed_paths,
            "gate_binding": (
                None if self.gate_binding is None else self.gate_binding.model_dump(mode="json")
            ),
            "expected_run_head_oid": self.expected_run_head_oid,
            "prepared_oid": self.prepared_oid,
            "prepared_tree_oid": self.prepared_tree_oid,
            "run_id": self.run_id,
            "task_id": self.task_id,
        }
        expected_id = CandidateId(
            "task-candidate-" + str(_task_candidate_digest(identity)).removeprefix("sha256:")[:24]
        )
        expected_digest = _task_candidate_digest({**identity, "candidate_id": expected_id})
        if self.candidate_id != expected_id or self.candidate_digest != expected_digest:
            raise ValueError("TASK_CANDIDATE_BINDING_MISMATCH")
        if self.gate_binding is not None and (
            self.gate_binding.attempt_id != self.attempt_id
            or self.gate_binding.base_run_head_oid != self.expected_run_head_oid
            or self.gate_binding.post_tree_oid != self.prepared_tree_oid
        ):
            raise ValueError("TASK_CANDIDATE_GATE_BINDING_MISMATCH")
        return self


class TargetReservationIdAllocationError(RuntimeError):
    def __init__(self, failed_invariant: str) -> None:
        super().__init__(failed_invariant)
        self.failed_invariant = failed_invariant


def random_target_reservation_id() -> str:
    return _TARGET_RESERVATION_ID_PREFIX + token_hex(16)


def allocate_target_reservation_id(
    source: Callable[[], object], is_reserved: Callable[[str], bool]
) -> str:
    for _ in range(MAX_TARGET_RESERVATION_ID_ATTEMPTS):
        try:
            candidate = source()
        except StopIteration as error:
            raise TargetReservationIdAllocationError("TARGET_RESERVATION_ID_EXHAUSTED") from error
        if (
            not isinstance(candidate, str)
            or len(candidate) != len(_TARGET_RESERVATION_ID_PREFIX) + 32
            or not candidate.startswith(_TARGET_RESERVATION_ID_PREFIX)
            or any(
                character not in "0123456789abcdef"
                for character in candidate[len(_TARGET_RESERVATION_ID_PREFIX) :]
            )
        ):
            raise TargetReservationIdAllocationError("TARGET_RESERVATION_ID_SOURCE_INVALID")
        if not is_reserved(candidate):
            return candidate
    raise TargetReservationIdAllocationError("TARGET_RESERVATION_ID_EXHAUSTED")


def private_ref(run_id: RunId) -> str:
    return f"refs/apexcrew/runs/{run_id}"


class PrivateRefCasOutcome(FrozenDocument):
    intent_id: IntentId
    run_id: RunId
    result_class: Literal[
        "PRIVATE_REF_INITIALIZED",
        "PRIVATE_REF_PROMOTED",
        "PRIVATE_REF_ABSENT_FAILED",
        "PRIVATE_REF_CONFLICT",
        "PRIVATE_REF_UNOBSERVABLE",
    ]
    observed_oid: GitOid | None

    def to_effect_result(self, settled_sequence: AuditSequence) -> EffectResult:
        payload = canonical_json(self.model_dump(mode="json"))
        return EffectResult(
            intent_id=self.intent_id,
            run_id=self.run_id,
            outcome=cast(
                Literal["COMPLETED", "FAILED", "CONFLICT", "INDETERMINATE"],
                {
                    "PRIVATE_REF_INITIALIZED": "COMPLETED",
                    "PRIVATE_REF_PROMOTED": "COMPLETED",
                    "PRIVATE_REF_ABSENT_FAILED": "FAILED",
                    "PRIVATE_REF_CONFLICT": "CONFLICT",
                    "PRIVATE_REF_UNOBSERVABLE": "INDETERMINATE",
                }[self.result_class],
            ),
            result_class=self.result_class,
            result_digest=sha256_digest(payload),
            bounded_result_json=payload,
            settled_sequence=settled_sequence,
        )


class RefPathBinding(FrozenDocument):
    state: Literal["ABSENT", "REGULAR_FILE"]
    identity_digest: Sha256DigestText | None = None

    @model_validator(mode="after")
    def validate_state(self) -> Self:
        if (self.state == "REGULAR_FILE") != (self.identity_digest is not None):
            raise ValueError("REF_PATH_BINDING_INVALID")
        return self


class RefEffectBinding(FrozenDocument):
    repository_instance_digest: Sha256DigestText
    checkout_registration_digest: Sha256DigestText
    ref_file: RefPathBinding
    ref_lock: RefPathBinding
    reflog: RefPathBinding
    reflog_lock: RefPathBinding
    reflog_exists: bool
    reflog_message: str = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def validate_reflog_state(self) -> Self:
        if self.reflog_exists != (self.reflog.state == "REGULAR_FILE"):
            raise ValueError("REF_EFFECT_REFLOG_BINDING_INVALID")
        return self


class RefCasIntent(FrozenDocument):
    intent_id: IntentId
    run_id: RunId
    kind: Literal["private_ref_init", "private_ref_cas"]
    candidate_id: CandidateId | None = None
    repository_id: RepositoryId
    ref_name: str
    expected_old_oid: GitOid | None
    prepared_oid: GitOid
    target_safety_digest: Sha256DigestText
    ref_effect_binding: RefEffectBinding
    target_reservation_id: str
    permit_generation: int = Field(ge=1)
    applicable_revision_digests: ApplicableRevisionDigests
    idempotency_key: str

    @model_validator(mode="after")
    def validate_binding(self) -> Self:
        if self.ref_name != private_ref(self.run_id):
            raise ValueError("REF_CAS_KIND_BINDING_INVALID")
        if self.kind == "private_ref_init":
            if self.expected_old_oid is not None or self.candidate_id is not None:
                raise ValueError("REF_CAS_INIT_BINDING_INVALID")
        elif self.expected_old_oid is None or self.candidate_id is None:
            raise ValueError("REF_CAS_PROMOTION_BINDING_INVALID")
        return self

    def to_effect_intent(self, recorded_sequence: AuditSequence) -> EffectIntent:
        payload = canonical_json(self.model_dump(mode="json"))
        return EffectIntent(
            intent_id=self.intent_id,
            run_id=self.run_id,
            kind=self.kind,
            idempotency_key=self.idempotency_key,
            applicable_revision_digests=self.applicable_revision_digests,
            payload_digest=sha256_digest(payload),
            normalized_payload_json=payload,
            recorded_sequence=recorded_sequence,
            expected_prestate_json=canonical_json(
                {
                    "candidate_id": self.candidate_id,
                    "expected_old_oid": self.expected_old_oid,
                    "ref_effect_binding": self.ref_effect_binding.model_dump(mode="json"),
                    "ref_name": self.ref_name,
                    "repository_id": self.repository_id,
                    "target_safety_digest": self.target_safety_digest,
                }
            ),
        )

    @classmethod
    def from_effect_intent(cls, effect: EffectIntent) -> RefCasIntent:
        candidate = cls.model_validate_json(effect.normalized_payload_json)
        if candidate.to_effect_intent(effect.recorded_sequence) != effect:
            raise ValueError("REF_CAS_EFFECT_INTENT_BINDING_MISMATCH")
        return candidate


class TargetCasIntent(FrozenDocument):
    intent_id: IntentId
    run_id: RunId
    kind: Literal["target_ref_cas"]
    repository_id: RepositoryId
    repository_instance_digest: Sha256DigestText
    ref_name: str
    expected_old_oid: GitOid
    prepared_oid: GitOid
    target_safety_digest: Sha256DigestText
    registration_digest: Sha256DigestText
    applicable_revision_digests: ApplicableRevisionDigests
    idempotency_key: str

    def to_effect_intent(self, recorded_sequence: AuditSequence) -> EffectIntent:
        payload = canonical_json(self.model_dump(mode="json"))
        return EffectIntent(
            intent_id=self.intent_id,
            run_id=self.run_id,
            kind=self.kind,
            idempotency_key=self.idempotency_key,
            applicable_revision_digests=self.applicable_revision_digests,
            payload_digest=sha256_digest(payload),
            normalized_payload_json=payload,
            recorded_sequence=recorded_sequence,
            expected_prestate_json=canonical_json(
                {
                    "expected_old_oid": self.expected_old_oid,
                    "prepared_oid": self.prepared_oid,
                    "ref_name": self.ref_name,
                    "registration_digest": self.registration_digest,
                    "repository_id": self.repository_id,
                    "repository_instance_digest": self.repository_instance_digest,
                    "target_safety_digest": self.target_safety_digest,
                }
            ),
        )

    @classmethod
    def from_effect_intent(cls, effect: EffectIntent) -> TargetCasIntent:
        candidate = cls.model_validate_json(effect.normalized_payload_json)
        if candidate.to_effect_intent(effect.recorded_sequence) != effect:
            raise ValueError("TARGET_CAS_EFFECT_INTENT_BINDING_MISMATCH")
        return candidate


class StartGuardBinding(FrozenDocument):
    run_id: RunId
    repository_id: RepositoryId
    target_reservation_id: str
    pinned_target_oid: GitOid
    target_safety_digest: Sha256DigestText
    ref_effect_binding: RefEffectBinding
    applicable_revision_digests: ApplicableRevisionDigests


class RuntimeStartBinding(FrozenDocument):
    run_id: RunId
    sequence: AuditSequence
    state: Literal[RunState.READY_TO_START]
    permit_generation: int
    consumed_owner_id: RuntimeOwnerId
    consumed_sequence: AuditSequence
    guard: StartGuardBinding


class StartGuardDecision(FrozenDocument):
    ok: bool
    reason: str | None = None
    binding: StartGuardBinding | None = None

    @model_validator(mode="after")
    def validate_shape(self) -> Self:
        if self.ok != (self.reason is None and self.binding is not None):
            raise ValueError("START_GUARD_DECISION_SHAPE_INVALID")
        return self


class StartGuard(Protocol):
    def inspect(
        self,
        *,
        run_id: RunId,
        applicable_revision_digests: ApplicableRevisionDigests,
        expected_sequence: AuditSequence,
    ) -> StartGuardDecision: ...

    def validate_consumed(
        self,
        *,
        binding: RuntimeStartBinding,
        permit: RuntimePermit,
        expected_sequence: AuditSequence,
    ) -> StartGuardDecision: ...


class PrivateRefAdmissionPort(Protocol):
    def initialize_private_ref(self, intent: RefCasIntent) -> PrivateRefCasOutcome: ...

    def promote_private_ref(self, intent: RefCasIntent) -> PrivateRefCasOutcome: ...


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
    kind: Literal["ADD_NO_CHECKOUT", "LOCK", "UNLOCK", "REMOVE_FORCE"]
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
    kind: Literal["ADD_NO_CHECKOUT", "LOCK", "UNLOCK", "REMOVE_FORCE"]


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
    lock_digest: Sha256DigestText | None = None


@dataclass(frozen=True, slots=True)
class ReservationPathObservation:
    path_present: bool
    gitfile_only: bool
    exact_back_reference: bool
    observable: bool
    gitfile_digest: Sha256DigestText | None = None
    path_identity: str | None = None


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
            lock_digest=registration.lock_digest,
            path_identity=path.path_identity,
            gitfile_digest=path.gitfile_digest,
            registration_exact_identity=registration.exact_identity
            and not registration.unexpected_registration,
            path_exact_back_reference=path.exact_back_reference,
        )


class ReservationAdminObservation(FrozenDocument):
    admin_entry_name: str | None
    admin_binding_digest: Sha256DigestText | None
    locked: bool = True
    lock_digest: Sha256DigestText | None = None


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

    def release_cached_admin_entry(self, reservation: TargetReservation) -> None:
        raise NotImplementedError

    def release_cached_reservation(self, reservation: TargetReservation) -> None:
        raise NotImplementedError

    def remove_exact_admin_entry(
        self,
        reservation: TargetReservation,
        expected_digest: Sha256DigestText | None,
        expected_lock_digest: Sha256DigestText | None,
    ) -> None:
        raise NotImplementedError

    def require_exact_cleanup_path(
        self,
        reservation: TargetReservation,
        expected_path_identity: str,
        expected_gitfile_digest: Sha256DigestText,
    ) -> None:
        raise NotImplementedError

    def refresh_after_git_transition(self) -> None:
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
