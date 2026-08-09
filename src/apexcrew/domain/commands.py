from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import PurePosixPath
from typing import Annotated, Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    model_validator,
)

from apexcrew.domain.indeterminate import ResolutionSelection
from apexcrew.domain.revisions import (
    BudgetRevisionDocument,
    FrozenDocument,
    ModelConfigurationRevisionDocument,
    PolicyRevisionDocument,
    Sha256DigestText,
    revision_digest,
)
from apexcrew.domain.types import (
    AuditSequence,
    CandidateId,
    CommandStatus,
    EvidenceBundleDigest,
    GitOid,
    IntentId,
    PendingActionId,
    RequestId,
    RevisionDigest,
    RunId,
    RunState,
    RunStopReason,
    RuntimeOwnerId,
    TaskId,
    UnresolvedSetDigest,
)

NonEmptyText = Annotated[str, Field(min_length=1)]
ConfirmationCode = Annotated[str, Field(pattern=r"^[A-Z0-9]{6}$")]


class StrictPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ApplicableRevisionDigests(FrozenDocument):
    plan_digest: RevisionDigest | None = None
    policy_digest: RevisionDigest | None = None
    budget_digest: RevisionDigest | None = None
    model_configuration_digest: RevisionDigest | None = None


@dataclass(frozen=True, slots=True)
class PublicRunSnapshot:
    sequence: AuditSequence
    state: RunState


RuntimeAllowedPhase = Literal[
    "DRAFT",
    "PLANNING",
    "READY_TO_START",
    "ACTIVE",
    "PAUSED",
    "INDETERMINATE",
    "READY_FOR_APPROVAL",
    "TERMINAL_ADMINISTRATION",
]


class RuntimePermit(FrozenDocument):
    run_id: RunId
    generation: int = Field(ge=1)
    source_request_id: RequestId
    source_envelope_digest: Sha256DigestText
    issued_sequence: AuditSequence = Field(ge=1)
    allowed_phase: RuntimeAllowedPhase
    applicable_revision_digests: ApplicableRevisionDigests
    target_authority_digest: Sha256DigestText
    expected_runtime_progress_generation: int = Field(ge=0)
    state: Literal["UNCONSUMED", "CONSUMED", "INVALIDATED"]
    consumed_owner_id: RuntimeOwnerId | None = None
    consumed_sequence: AuditSequence | None = Field(default=None, ge=1)
    resolution_selection: ResolutionSelection | None = None

    @model_validator(mode="after")
    def validate_consumption_binding(self) -> Self:
        consumed = self.state == "CONSUMED"
        if consumed != (self.consumed_owner_id is not None):
            raise ValueError("consumed Permit must bind an owner")
        if consumed != (self.consumed_sequence is not None):
            raise ValueError("consumed Permit must bind its sequence")
        if self.allowed_phase == "INDETERMINATE":
            if self.resolution_selection is None:
                raise ValueError("indeterminate Permit must bind a resolution selection")
        elif self.resolution_selection is not None:
            raise ValueError("non-indeterminate Permit cannot bind a resolution selection")
        return self


@dataclass(frozen=True, slots=True)
class RuntimeState:
    run_id: RunId
    state: RunState
    sequence: AuditSequence
    runtime_progress_generation: int
    plan_digest: RevisionDigest | None
    policy_digest: RevisionDigest
    budget_digest: RevisionDigest
    model_configuration_digest: RevisionDigest


def applicable_revision_digests_to_json(
    digests: ApplicableRevisionDigests,
) -> str:
    return json.dumps(digests.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))


def applicable_revision_digests_from_json(value: str) -> ApplicableRevisionDigests:
    return ApplicableRevisionDigests.model_validate_json(value)


class CreateRunPayload(StrictPayload):
    kind: Literal["create_run"] = "create_run"
    goal: NonEmptyText
    constraints: tuple[NonEmptyText, ...]
    acceptance_criteria: tuple[NonEmptyText, ...]
    repository_root: NonEmptyText
    target_ref: NonEmptyText
    expected_target_oid: GitOid
    policy_revision: PolicyRevisionDocument
    budget_revision: BudgetRevisionDocument
    model_configuration_revision: ModelConfigurationRevisionDocument


class ProposePolicyPayload(StrictPayload):
    kind: Literal["propose_policy"] = "propose_policy"
    run_id: RunId
    policy_revision: PolicyRevisionDocument


class ApprovePolicyPayload(StrictPayload):
    kind: Literal["approve_policy"] = "approve_policy"
    run_id: RunId
    policy_digest: RevisionDigest
    confirmation_code: ConfirmationCode


class ProposeBudgetPayload(StrictPayload):
    kind: Literal["propose_budget"] = "propose_budget"
    run_id: RunId
    budget_revision: BudgetRevisionDocument


class ApproveBudgetPayload(StrictPayload):
    kind: Literal["approve_budget"] = "approve_budget"
    run_id: RunId
    budget_digest: RevisionDigest
    confirmation_code: ConfirmationCode


class ProposeModelConfigurationPayload(StrictPayload):
    kind: Literal["propose_model_configuration"] = "propose_model_configuration"
    run_id: RunId
    model_configuration_revision: ModelConfigurationRevisionDocument


class ApproveModelConfigurationPayload(StrictPayload):
    kind: Literal["approve_model_configuration"] = "approve_model_configuration"
    run_id: RunId
    model_configuration_digest: RevisionDigest
    confirmation_code: ConfirmationCode


class BeginPlanningPayload(StrictPayload):
    kind: Literal["begin_planning"] = "begin_planning"
    run_id: RunId


class ApprovePlanPayload(StrictPayload):
    kind: Literal["approve_plan"] = "approve_plan"
    run_id: RunId
    plan_digest: RevisionDigest
    confirmation_code: ConfirmationCode


class RejectPlanPayload(StrictPayload):
    kind: Literal["reject_plan"] = "reject_plan"
    run_id: RunId
    plan_digest: RevisionDigest
    reason: NonEmptyText


class StartPayload(StrictPayload):
    kind: Literal["start"] = "start"
    run_id: RunId
    plan_digest: RevisionDigest


class ContinuePayload(StrictPayload):
    kind: Literal["continue"] = "continue"
    run_id: RunId


class PausePayload(StrictPayload):
    kind: Literal["pause"] = "pause"
    run_id: RunId
    reason: NonEmptyText


class ResumePayload(StrictPayload):
    kind: Literal["resume"] = "resume"
    run_id: RunId
    pause_sequence: AuditSequence = Field(ge=0)
    pause_reason: NonEmptyText
    task_id: TaskId | None = None


class GrantPayload(StrictPayload):
    kind: Literal["grant"] = "grant"
    run_id: RunId
    pending_action_id: PendingActionId
    pending_action_digest: Sha256DigestText
    confirmation_code: ConfirmationCode


class ResolveIndeterminatePayload(StrictPayload):
    kind: Literal["resolve_indeterminate"] = "resolve_indeterminate"
    run_id: RunId
    unresolved_set_digest: UnresolvedSetDigest
    resolution: Literal[
        "RECONCILE_OBSERVED",
        "RETRY_SAME_INTENT",
        "ABANDON_INTENT",
        "FAIL_RUN",
        "CANCEL_RUN",
    ]
    intent_id: IntentId | None = None
    recovery_generation: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_resolution_shape(self) -> Self:
        member_bound = {
            "RECONCILE_OBSERVED",
            "RETRY_SAME_INTENT",
            "ABANDON_INTENT",
        }
        if self.resolution in member_bound:
            if self.intent_id is None or self.recovery_generation is None:
                raise ValueError(
                    "member-bound resolution requires intent_id and recovery_generation"
                )
            if str(self.intent_id) != str(self.intent_id).strip():
                raise ValueError("member-bound intent_id must be canonical")
        elif self.intent_id is not None or self.recovery_generation is not None:
            raise ValueError("set-bound terminal resolution forbids member fields")
        return self


class IntegratePayload(StrictPayload):
    kind: Literal["integrate"] = "integrate"
    run_id: RunId
    candidate_id: CandidateId
    prepared_oid: GitOid
    expected_target_oid: GitOid
    evidence_bundle_digest: EvidenceBundleDigest
    confirmation_code: ConfirmationCode


class ReconcileCleanupPayload(StrictPayload):
    kind: Literal["reconcile_cleanup"] = "reconcile_cleanup"
    run_id: RunId


class CancelPayload(StrictPayload):
    kind: Literal["cancel"] = "cancel"
    run_id: RunId
    reason: NonEmptyText


class PreparePurgePayload(StrictPayload):
    kind: Literal["prepare_purge"] = "prepare_purge"
    run_id: RunId


class ConfirmPurgePayload(StrictPayload):
    kind: Literal["confirm_purge"] = "confirm_purge"
    run_id: RunId
    purge_digest: Sha256DigestText
    confirmation_code: ConfirmationCode


CommandPayload = Annotated[
    CreateRunPayload
    | ProposePolicyPayload
    | ApprovePolicyPayload
    | ProposeBudgetPayload
    | ApproveBudgetPayload
    | ProposeModelConfigurationPayload
    | ApproveModelConfigurationPayload
    | BeginPlanningPayload
    | ApprovePlanPayload
    | RejectPlanPayload
    | StartPayload
    | ContinuePayload
    | PausePayload
    | ResumePayload
    | GrantPayload
    | ResolveIndeterminatePayload
    | IntegratePayload
    | ReconcileCleanupPayload
    | CancelPayload
    | PreparePurgePayload
    | ConfirmPurgePayload,
    Field(discriminator="kind"),
]


class CommandEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    request_id: NonEmptyText
    expected_sequence: int | None = Field(ge=0)
    applicable_revision_digests: ApplicableRevisionDigests = ApplicableRevisionDigests()
    payload: CommandPayload


class PlanApprovalPending(FrozenDocument):
    kind: Literal["plan"] = "plan"
    plan_digest: RevisionDigest


class ActionApprovalPending(FrozenDocument):
    kind: Literal["actions"] = "actions"
    pending_action_ids: tuple[PendingActionId, ...]

    @model_validator(mode="after")
    def validate_pending_action_ids(self) -> Self:
        values = tuple(str(value) for value in self.pending_action_ids)
        if not values:
            raise ValueError("pending_action_ids must be non-empty")
        if values != tuple(sorted(values)) or len(set(values)) != len(values):
            raise ValueError("pending_action_ids must be sorted and duplicate-free")
        return self


class FinalApprovalPending(FrozenDocument):
    kind: Literal["final"] = "final"
    candidate_id: CandidateId


ApprovalPending = Annotated[
    PlanApprovalPending | ActionApprovalPending | FinalApprovalPending,
    Field(discriminator="kind"),
]


class RunStop(FrozenDocument):
    run_id: RunId
    state: RunState
    reason: RunStopReason
    last_sequence: AuditSequence = Field(ge=0)
    pending: ApprovalPending | None = None

    @model_validator(mode="after")
    def validate_pending_subject(self) -> Self:
        if self.reason == RunStopReason.AWAITING_PLAN_APPROVAL:
            valid = self.state == RunState.AWAITING_PLAN_APPROVAL and isinstance(
                self.pending, PlanApprovalPending
            )
        elif self.reason == RunStopReason.AWAITING_ACTION_APPROVAL:
            valid = self.state == RunState.ACTIVE and isinstance(
                self.pending, ActionApprovalPending
            )
        elif self.reason == RunStopReason.AWAITING_FINAL_APPROVAL:
            valid = self.state == RunState.READY_FOR_APPROVAL and isinstance(
                self.pending, FinalApprovalPending
            )
        else:
            valid = self.pending is None
        if not valid:
            raise ValueError("RunStop reason, state, and pending subject do not match")
        return self


class RuntimeDecision(FrozenDocument):
    """Internal one-turn result; RuntimeService alone maps it to public RunStop."""

    code: Literal[
        "CONTINUE",
        "STOP",
        "MALFORMED_ACTION",
        "ACTION_RECORDED",
        "DENIED",
    ]
    stop_reason: str | None = None
    resulting_sequence: AuditSequence | None = None
    phase_transition: (
        Literal[
            "TARGET_RESERVATION_INITIALIZED",
            "PRIVATE_REF_INITIALIZED",
            "PRIVATE_REF_PROMOTED",
        ]
        | None
    ) = None

    @classmethod
    def continued(cls, sequence: AuditSequence | None = None) -> RuntimeDecision:
        return cls(code="CONTINUE", resulting_sequence=sequence)

    @classmethod
    def pause(cls, reason: str, sequence: AuditSequence | None = None) -> RuntimeDecision:
        return cls(code="STOP", stop_reason=reason, resulting_sequence=sequence)

    @classmethod
    def invalid_planning_action(cls, sequence: AuditSequence | None = None) -> RuntimeDecision:
        return cls(
            code="MALFORMED_ACTION",
            stop_reason=None,
            resulting_sequence=sequence,
        )

    @classmethod
    def denied(cls, reason: str, sequence: AuditSequence | None) -> RuntimeDecision:
        return cls(code="DENIED", stop_reason=reason, resulting_sequence=sequence)


class RevisionApprovalPreview(FrozenDocument):
    revision_kind: Literal["policy", "budget", "model_configuration"]
    revision_digest: RevisionDigest
    confirmation_code: ConfirmationCode


class RevisionApprovalResult(FrozenDocument):
    kind: Literal["revision_approval"] = "revision_approval"
    approvals: tuple[RevisionApprovalPreview, ...]

    @model_validator(mode="after")
    def validate_approval_set(self) -> Self:
        kinds = tuple(item.revision_kind for item in self.approvals)
        if len(kinds) == 1:
            return self
        if kinds != ("policy", "budget", "model_configuration"):
            raise ValueError("approvals must contain one item or the exact three-item order")
        return self


class PurgeDatabaseRowEntry(FrozenDocument):
    kind: Literal["database_row"] = "database_row"
    table_name: str = Field(min_length=1)
    row_id: str = Field(min_length=1)
    row_digest: Sha256DigestText
    byte_count: int = Field(ge=0)


class PurgeLocalArtifactEntry(FrozenDocument):
    kind: Literal["local_artifact"] = "local_artifact"
    artifact_id: str = Field(min_length=1)
    relative_path: str = Field(min_length=1)
    artifact_digest: Sha256DigestText
    byte_count: int = Field(ge=0)


PurgeInventoryEntry = Annotated[
    PurgeDatabaseRowEntry | PurgeLocalArtifactEntry,
    Field(discriminator="kind"),
]


def _validate_relative_posix_path(value: str) -> None:
    parts = value.split("/")
    has_drive = len(value) >= 2 and value[0].isalpha() and value[1] == ":"
    if (
        value.startswith("/")
        or has_drive
        or "\\" in value
        or "\x00" in value
        or any(part in {"", ".", ".."} for part in parts)
        or PurePosixPath(value).as_posix() != value
    ):
        raise ValueError("relative_path must be normalized relative POSIX text")


def _purge_entry_key(
    entry: PurgeDatabaseRowEntry | PurgeLocalArtifactEntry,
) -> tuple[str, str, str]:
    if isinstance(entry, PurgeDatabaseRowEntry):
        return (entry.kind, entry.table_name, entry.row_id)
    _validate_relative_posix_path(entry.relative_path)
    return (entry.kind, entry.relative_path, entry.artifact_id)


class PurgeManifestDocument(FrozenDocument):
    schema_version: Literal["purge-manifest-v1"] = "purge-manifest-v1"
    repository_id: Sha256DigestText
    run_id: RunId
    terminal_state: Literal["COMPLETED", "FAILED", "CANCELLED"]
    terminal_sequence: AuditSequence = Field(ge=0)
    ledger_head_digest: Sha256DigestText
    entries: tuple[PurgeInventoryEntry, ...]
    database_row_count: int = Field(ge=0)
    local_artifact_count: int = Field(ge=0)
    total_byte_count: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_inventory(self) -> Self:
        if not self.entries:
            raise ValueError("entries must be non-empty")
        keys = tuple(_purge_entry_key(entry) for entry in self.entries)
        if keys != tuple(sorted(keys)) or len(set(keys)) != len(keys):
            raise ValueError("entries must be sorted and have unique identities")
        database_count = sum(isinstance(entry, PurgeDatabaseRowEntry) for entry in self.entries)
        artifact_count = sum(isinstance(entry, PurgeLocalArtifactEntry) for entry in self.entries)
        byte_count = sum(entry.byte_count for entry in self.entries)
        if (
            self.database_row_count != database_count
            or self.local_artifact_count != artifact_count
            or self.total_byte_count != byte_count
        ):
            raise ValueError("declared purge totals must equal the entries")
        return self


class PurgePreparedResult(FrozenDocument):
    kind: Literal["purge_prepared"] = "purge_prepared"
    manifest: PurgeManifestDocument
    purge_digest: Sha256DigestText
    confirmation_code: ConfirmationCode
    expires_at_utc: datetime

    @model_validator(mode="after")
    def validate_manifest_digest_and_expiry(self) -> Self:
        if self.purge_digest != revision_digest(self.manifest):
            raise ValueError("purge_digest must bind the exact manifest")
        if self.expires_at_utc.tzinfo is None or self.expires_at_utc.utcoffset() != timedelta(0):
            raise ValueError("expires_at_utc must be timezone-aware UTC")
        return self


CommandResult = Annotated[
    RevisionApprovalResult | PurgePreparedResult,
    Field(discriminator="kind"),
]


class CommandOutcome(FrozenDocument):
    status: CommandStatus
    run_id: RunId | None
    resulting_sequence: AuditSequence | None = Field(default=None, ge=0)
    failed_invariant: str | None = None
    safe_next_action: str | None = None
    result: CommandResult | None = None

    @classmethod
    def for_payload(
        cls,
        payload: CommandPayload,
        *,
        status: CommandStatus,
        run_id: RunId | None,
        resulting_sequence: AuditSequence | None,
        failed_invariant: str | None = None,
        safe_next_action: str | None = None,
        result: CommandResult | None = None,
    ) -> Self:
        return cls.validate_for_payload(
            payload,
            {
                "status": status,
                "run_id": run_id,
                "resulting_sequence": resulting_sequence,
                "failed_invariant": failed_invariant,
                "safe_next_action": safe_next_action,
                "result": result,
            },
        )

    @classmethod
    def validate_for_payload(cls, payload: CommandPayload, value: Any) -> Self:
        return cls.model_validate(value, context={"command_payload": payload})

    @model_validator(mode="after")
    def validate_result_binding(self, info: ValidationInfo) -> Self:
        if self.result is None:
            return self
        if self.status != CommandStatus.ACCEPTED:
            raise ValueError("only ACCEPTED outcomes may carry a result")
        payload = None if info.context is None else info.context.get("command_payload")
        if isinstance(self.result, RevisionApprovalResult):
            allowed = isinstance(
                payload,
                (
                    CreateRunPayload,
                    ProposePolicyPayload,
                    ProposeBudgetPayload,
                    ProposeModelConfigurationPayload,
                ),
            )
        else:
            allowed = isinstance(payload, PreparePurgePayload)
        if not allowed:
            raise ValueError("result is not valid for this command payload")
        return self
