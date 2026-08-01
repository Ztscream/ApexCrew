from enum import StrEnum
from typing import NewType

RunId = NewType("RunId", str)
RequestId = NewType("RequestId", str)
TaskId = NewType("TaskId", str)
AttemptId = NewType("AttemptId", str)
CandidateId = NewType("CandidateId", str)
PermitId = NewType("PermitId", str)
RuntimeOwnerId = NewType("RuntimeOwnerId", str)
GrantId = NewType("GrantId", str)
IntentId = NewType("IntentId", str)
PendingActionId = NewType("PendingActionId", str)
RevisionDigest = NewType("RevisionDigest", str)
AuditSequence = NewType("AuditSequence", int)
GitOid = NewType("GitOid", str)
EvidenceBundleDigest = NewType("EvidenceBundleDigest", str)
UnresolvedSetDigest = NewType("UnresolvedSetDigest", str)


class RunState(StrEnum):
    DRAFT = "DRAFT"
    PLANNING = "PLANNING"
    AWAITING_PLAN_APPROVAL = "AWAITING_PLAN_APPROVAL"
    READY_TO_START = "READY_TO_START"
    ACTIVE = "ACTIVE"
    VERIFYING_RUN = "VERIFYING_RUN"
    READY_FOR_APPROVAL = "READY_FOR_APPROVAL"
    APPLYING = "APPLYING"
    PAUSED = "PAUSED"
    INDETERMINATE = "INDETERMINATE"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class CommandStatus(StrEnum):
    ACCEPTED = "ACCEPTED"
    INVALID = "INVALID"
    STALE = "STALE"
    DENIED = "DENIED"
    CONFLICT = "CONFLICT"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    INDETERMINATE = "INDETERMINATE"


class RunStopReason(StrEnum):
    AWAITING_PLAN_APPROVAL = "AWAITING_PLAN_APPROVAL"
    AWAITING_ACTION_APPROVAL = "AWAITING_ACTION_APPROVAL"
    AWAITING_FINAL_APPROVAL = "AWAITING_FINAL_APPROVAL"
    PAUSED = "PAUSED"
    INTERRUPTED = "INTERRUPTED"
    BUDGET_STOP = "BUDGET_STOP"
    INDETERMINATE = "INDETERMINATE"
    TERMINAL = "TERMINAL"
    NO_RUNTIME_PERMIT = "NO_RUNTIME_PERMIT"
    ALREADY_RUNNING = "ALREADY_RUNNING"
