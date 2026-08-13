from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Annotated, Literal, Protocol, Self

from pydantic import Field, TypeAdapter, ValidationError, model_validator

from apexcrew.domain.authority import Authority, ModelReservation, ModelReservationRequest
from apexcrew.domain.commands import ApplicableRevisionDigests, RuntimeDecision, RuntimePermit
from apexcrew.domain.effects import EffectIntent, EffectResult, canonical_json, sha256_digest
from apexcrew.domain.limits import V01_MECHANISM_LIMITS
from apexcrew.domain.model import (
    PROVIDER_RETRY_BACKOFF_SECONDS,
    BackoffPort,
    ImmediateBackoff,
    LogicalTurnId,
    ModelDispatchResult,
    ModelJournal,
    ModelPort,
    ModelRequest,
    ProviderAttemptKind,
    RecoveredModelAction,
)
from apexcrew.domain.plan import (
    CanonicalPath,
    CheckDefinition,
    GlobPattern,
    PlanRevision,
    PlanValidationError,
    TaskContract,
    validate_plan,
)
from apexcrew.domain.revisions import (
    FrozenDocument,
    PlanningReadAuthorizationDocument,
    Sha256DigestText,
)
from apexcrew.domain.types import (
    AttemptId,
    AuditSequence,
    GitOid,
    IntentId,
    RepositoryId,
    RevisionDigest,
    RunId,
    TaskId,
)

if TYPE_CHECKING:
    from apexcrew.domain.worker import WorkerAttemptSnapshot


def _check_document(check: CheckDefinition) -> dict[str, object]:
    return {
        "argv": list(check.argv),
        "input_globs": [pattern.value for pattern in check.input_globs],
    }


def check_definition_json(check: CheckDefinition) -> str:
    return canonical_json(_check_document(check))


def check_definition_from_json(value: str) -> CheckDefinition:
    try:
        data = json.loads(value)
    except (TypeError, json.JSONDecodeError) as error:
        raise ValueError("CHECK_DOCUMENT_INVALID") from error
    if not isinstance(data, dict) or set(data) != {"argv", "input_globs"}:
        raise ValueError("CHECK_DOCUMENT_INVALID")
    argv = data["argv"]
    input_globs = data["input_globs"]
    if (
        not isinstance(argv, list)
        or not argv
        or not all(isinstance(item, str) and item for item in argv)
        or not isinstance(input_globs, list)
        or not input_globs
        or not all(isinstance(item, str) for item in input_globs)
    ):
        raise ValueError("CHECK_DOCUMENT_INVALID")
    check = CheckDefinition(
        argv=tuple(argv),
        input_globs=tuple(GlobPattern.parse(item) for item in input_globs),
    )
    if check_definition_json(check) != value:
        raise ValueError("CHECK_DOCUMENT_NOT_CANONICAL")
    return check


def _task_document(task: TaskContract) -> dict[str, object]:
    return {
        "checks": [_check_document(check) for check in task.checks],
        "constraints": list(task.constraints),
        "dependency_globs": [pattern.value for pattern in task.dependency_globs],
        "dependency_task_ids": list(task.dependency_task_ids),
        "read_globs": [pattern.value for pattern in task.read_globs],
        "task_id": task.task_id,
        "write_globs": [pattern.value for pattern in task.write_globs],
    }


def task_contract_json(task: TaskContract) -> str:
    return canonical_json(_task_document(task))


def _string_list(data: dict[str, object], field: str) -> tuple[str, ...]:
    value = data[field]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError("TASK_CONTRACT_DOCUMENT_INVALID")
    return tuple(value)


def task_contract_from_json(value: str) -> TaskContract:
    try:
        data = json.loads(value)
    except (TypeError, json.JSONDecodeError) as error:
        raise ValueError("TASK_CONTRACT_DOCUMENT_INVALID") from error
    expected_fields = {
        "checks",
        "constraints",
        "dependency_globs",
        "dependency_task_ids",
        "read_globs",
        "task_id",
        "write_globs",
    }
    if not isinstance(data, dict) or set(data) != expected_fields:
        raise ValueError("TASK_CONTRACT_DOCUMENT_INVALID")
    task_id = data["task_id"]
    checks = data["checks"]
    if not isinstance(task_id, str) or not isinstance(checks, list):
        raise TypeError("TASK_CONTRACT_DOCUMENT_INVALID")
    parsed_checks = tuple(
        check_definition_from_json(canonical_json(check))
        for check in checks
        if isinstance(check, dict)
    )
    if len(parsed_checks) != len(checks):
        raise ValueError("TASK_CONTRACT_DOCUMENT_INVALID")
    task = TaskContract.from_strings(
        task_id,
        _string_list(data, "read_globs"),
        _string_list(data, "write_globs"),
        dependency_task_ids=_string_list(data, "dependency_task_ids"),
        dependency_globs=_string_list(data, "dependency_globs"),
        checks=parsed_checks,
        constraints=_string_list(data, "constraints"),
    )
    if task_contract_json(task) != value:
        raise ValueError("TASK_CONTRACT_DOCUMENT_NOT_CANONICAL")
    return task


def task_contract_digest(task: TaskContract) -> Sha256DigestText:
    return sha256_digest(task_contract_json(task))


def run_check_set_digest(checks: tuple[CheckDefinition, ...]) -> Sha256DigestText:
    return sha256_digest(canonical_json({"checks": [_check_document(check) for check in checks]}))


@dataclass(frozen=True, slots=True)
class PlanProposal:
    run_id: RunId
    plan_digest: RevisionDigest
    canonical_plan_json: str
    plan: PlanRevision
    base_run_head_oid: GitOid
    applicable_revision_digests: ApplicableRevisionDigests
    run_check_set: tuple[CheckDefinition, ...]
    dependency_edges: tuple[tuple[TaskId, TaskId], ...]
    hazard_edges: tuple[tuple[TaskId, TaskId], ...]
    planning_request_count: int

    @classmethod
    def from_validated_plan(
        cls,
        *,
        run_id: RunId,
        canonical_plan_json: str,
        plan: PlanRevision,
        base_run_head_oid: GitOid,
        applicable_revision_digests: ApplicableRevisionDigests,
        run_check_set: tuple[CheckDefinition, ...],
        planning_request_count: int,
    ) -> PlanProposal:
        if not 1 <= planning_request_count <= 8:
            raise ValueError("PLANNING_REQUEST_COUNT_INVALID")
        try:
            plan_document = json.loads(canonical_plan_json)
        except (TypeError, json.JSONDecodeError) as error:
            raise ValueError("PLAN_DOCUMENT_INVALID") from error
        if (
            not isinstance(plan_document, dict)
            or canonical_json(plan_document) != canonical_plan_json
        ):
            raise ValueError("PLAN_DOCUMENT_NOT_CANONICAL")
        if applicable_revision_digests.plan_digest is not None or any(
            digest is None
            for digest in (
                applicable_revision_digests.policy_digest,
                applicable_revision_digests.budget_digest,
                applicable_revision_digests.model_configuration_digest,
            )
        ):
            raise ValueError("PLAN_REVISION_BINDING_INVALID")
        if (
            not plan.tasks
            or tuple(task.task_id for task in plan.tasks)
            != tuple(sorted(task.task_id for task in plan.tasks))
            or any(
                not task.task_id
                or not task.checks
                or any(not check.argv or not check.argv[0] for check in task.checks)
                for task in plan.tasks
            )
        ):
            raise PlanValidationError("INVALID_TASK_SET")
        if not run_check_set or any(
            not check.argv or not check.argv[0] or not check.input_globs for check in run_check_set
        ):
            raise PlanValidationError("MISSING_RUN_CHECK")
        expected_document = {
            "proposed_promotion_order": list(plan.proposed_promotion_order),
            "run_checks": [_check_document(check) for check in run_check_set],
            "tasks": [_task_document(task) for task in plan.tasks],
        }
        if plan_document != expected_document:
            raise ValueError("PLAN_DOCUMENT_BINDING_INVALID")
        check_digests = tuple(
            sha256_digest(check_definition_json(check)) for check in run_check_set
        )
        if len(set(check_digests)) != len(check_digests):
            raise PlanValidationError("DUPLICATE_RUN_CHECK")
        validation = validate_plan(plan)
        dependencies = tuple(
            sorted(
                (dependency, task.task_id)
                for task in plan.tasks
                for dependency in task.dependency_task_ids
            )
        )
        order = {task_id: index for index, task_id in enumerate(plan.proposed_promotion_order)}
        if any(order[left] >= order[right] for left, right in dependencies):
            raise PlanValidationError("PROMOTION_ORDER_REQUIRED")
        return cls(
            run_id=run_id,
            plan_digest=RevisionDigest(sha256_digest(canonical_plan_json)),
            canonical_plan_json=canonical_plan_json,
            plan=plan,
            base_run_head_oid=base_run_head_oid,
            applicable_revision_digests=applicable_revision_digests,
            run_check_set=run_check_set,
            dependency_edges=dependencies,
            hazard_edges=tuple(sorted(validation.promotion_hazards)),
            planning_request_count=planning_request_count,
        )


def plan_proposal_record_json(proposal: PlanProposal) -> str:
    return canonical_json(
        {
            "canonical_plan_json": proposal.canonical_plan_json,
            "proposed_promotion_order": list(proposal.plan.proposed_promotion_order),
        }
    )


def plan_proposal_record_from_json(value: str) -> tuple[str, tuple[TaskId, ...]]:
    try:
        data = json.loads(value)
    except (TypeError, json.JSONDecodeError) as error:
        raise ValueError("PLAN_PROPOSAL_DOCUMENT_INVALID") from error
    if not isinstance(data, dict) or set(data) != {
        "canonical_plan_json",
        "proposed_promotion_order",
    }:
        raise ValueError("PLAN_PROPOSAL_DOCUMENT_INVALID")
    plan_json = data["canonical_plan_json"]
    order = data["proposed_promotion_order"]
    if (
        not isinstance(plan_json, str)
        or not isinstance(order, list)
        or not all(isinstance(item, str) for item in order)
        or canonical_json(data) != value
    ):
        raise ValueError("PLAN_PROPOSAL_DOCUMENT_INVALID")
    return plan_json, tuple(TaskId(item) for item in order)


def validate_plan_proposal(proposal: PlanProposal) -> None:
    rebuilt = PlanProposal.from_validated_plan(
        run_id=proposal.run_id,
        canonical_plan_json=proposal.canonical_plan_json,
        plan=proposal.plan,
        base_run_head_oid=proposal.base_run_head_oid,
        applicable_revision_digests=proposal.applicable_revision_digests,
        run_check_set=proposal.run_check_set,
        planning_request_count=proposal.planning_request_count,
    )
    if rebuilt != proposal:
        raise ValueError("PLAN_PROPOSAL_BINDING_INVALID")


def plan_proposal_from_document(
    *,
    run_id: RunId,
    plan_document: Mapping[str, object],
    authorization: PlanningAuthorization,
) -> PlanProposal:
    if set(plan_document) != {"tasks", "proposed_promotion_order", "run_checks"}:
        raise ValueError("PLAN_DOCUMENT_INVALID")
    tasks_value = plan_document["tasks"]
    order_value = plan_document["proposed_promotion_order"]
    checks_value = plan_document["run_checks"]
    if (
        not isinstance(tasks_value, list)
        or not all(isinstance(item, dict) for item in tasks_value)
        or not isinstance(order_value, list)
        or not all(isinstance(item, str) for item in order_value)
        or not isinstance(checks_value, list)
        or not all(isinstance(item, dict) for item in checks_value)
        or authorization.turn_binding is None
    ):
        raise ValueError("PLAN_DOCUMENT_INVALID")
    tasks = tuple(task_contract_from_json(canonical_json(item)) for item in tasks_value)
    checks = tuple(check_definition_from_json(canonical_json(item)) for item in checks_value)
    plan = PlanRevision(
        tasks=tasks,
        proposed_promotion_order=tuple(TaskId(item) for item in order_value),
    )
    return PlanProposal.from_validated_plan(
        run_id=run_id,
        canonical_plan_json=canonical_json(plan_document),
        plan=plan,
        base_run_head_oid=authorization.turn_binding.pinned_base_oid,
        applicable_revision_digests=authorization.applicable_revision_digests,
        run_check_set=checks,
        planning_request_count=authorization.planning_request_count + 1,
    )


PLANNING_ACTION_KINDS = frozenset(
    {"read_tracked_file", "search_tracked_content", "submit_plan", "fail"}
)


def planning_snapshot_digest(
    repository_id: RepositoryId,
    pinned_base_oid: GitOid,
    scope_digest: Sha256DigestText,
) -> Sha256DigestText:
    return Sha256DigestText(
        sha256_digest(
            canonical_json(
                {
                    "pinned_base_oid": str(pinned_base_oid),
                    "repository_id": str(repository_id),
                    "scope_digest": str(scope_digest),
                }
            )
        )
    )


class PlanningTurnBinding(FrozenDocument):
    repository_id: RepositoryId
    pinned_base_oid: GitOid
    snapshot_digest: Sha256DigestText
    scope_digest: Sha256DigestText

    @model_validator(mode="after")
    def validate_snapshot_digest(self) -> Self:
        if self.snapshot_digest != planning_snapshot_digest(
            self.repository_id, self.pinned_base_oid, self.scope_digest
        ):
            raise ValueError("PLANNING_SNAPSHOT_BINDING_INVALID")
        return self


class PlanningAuthorization(FrozenDocument):
    run_id: RunId
    decision: Literal["ALLOW", "PAUSE"]
    reason: str | None = None
    applicable_revision_digests: ApplicableRevisionDigests
    target_safety_digest: Sha256DigestText
    credential_profile: str | None
    read_authorization: PlanningReadAuthorizationDocument | None
    turn_binding: PlanningTurnBinding | None
    planning_request_count: int = Field(ge=0, le=8)
    planning_request_ceiling: int = Field(ge=1, le=8)

    @model_validator(mode="after")
    def validate_decision(self) -> Self:
        if self.decision == "ALLOW":
            if (
                self.reason is not None
                or self.read_authorization is None
                or self.turn_binding is None
                or self.planning_request_count >= self.planning_request_ceiling
            ):
                raise ValueError("PLANNING_ALLOW_BINDING_INVALID")
        elif (
            not self.reason or self.read_authorization is not None or self.turn_binding is not None
        ):
            raise ValueError("PLANNING_PAUSE_BINDING_INVALID")
        return self


class PlanningReadTrackedFileAction(FrozenDocument):
    kind: Literal["read_tracked_file"] = "read_tracked_file"
    path: str


class PlanningSearchTrackedContentAction(FrozenDocument):
    kind: Literal["search_tracked_content"] = "search_tracked_content"
    query: str = Field(min_length=1)
    paths: tuple[str, ...]


class PlanningSubmitPlanAction(FrozenDocument):
    kind: Literal["submit_plan"] = "submit_plan"
    plan_document: Mapping[str, object]


class PlanningFailAction(FrozenDocument):
    kind: Literal["fail"] = "fail"
    reason: str = Field(min_length=1, max_length=4_096)


PlanningAction = Annotated[
    PlanningReadTrackedFileAction
    | PlanningSearchTrackedContentAction
    | PlanningSubmitPlanAction
    | PlanningFailAction,
    Field(discriminator="kind"),
]
PLANNING_ACTION_ADAPTER: TypeAdapter[PlanningAction] = TypeAdapter(PlanningAction)


class PlanningReadIntent(FrozenDocument):
    intent_id: IntentId
    run_id: RunId
    logical_turn_id: LogicalTurnId
    action: PlanningReadTrackedFileAction | PlanningSearchTrackedContentAction
    applicable_revision_digests: ApplicableRevisionDigests
    repository_id: RepositoryId
    base_oid: GitOid
    snapshot_digest: Sha256DigestText
    scope_digest: Sha256DigestText
    idempotency_key: str

    def to_effect_intent(self, recorded_sequence: AuditSequence) -> EffectIntent:
        payload = canonical_json(self.model_dump(mode="json"))
        return EffectIntent(
            intent_id=self.intent_id,
            run_id=self.run_id,
            kind=self.action.kind,
            idempotency_key=self.idempotency_key,
            applicable_revision_digests=self.applicable_revision_digests,
            payload_digest=sha256_digest(payload),
            normalized_payload_json=payload,
            recorded_sequence=recorded_sequence,
            action_id=self.logical_turn_id,
        )


class PlanningReadResult(FrozenDocument):
    intent_id: IntentId
    run_id: RunId
    result_class: Literal["READ_COMPLETED", "SEARCH_COMPLETED", "DENIED"]
    bounded_payload: Mapping[str, object]
    snapshot_digest: Sha256DigestText
    returned_bytes: int = Field(ge=0)

    def to_effect_result(self, settled_sequence: AuditSequence) -> EffectResult:
        payload = canonical_json(self.model_dump(mode="json"))
        return EffectResult(
            intent_id=self.intent_id,
            run_id=self.run_id,
            outcome="COMPLETED" if self.result_class != "DENIED" else "FAILED",
            result_class=self.result_class,
            result_digest=sha256_digest(payload),
            bounded_result_json=payload,
            settled_sequence=settled_sequence,
            snapshot_digest=self.snapshot_digest,
        )


@dataclass(frozen=True, slots=True)
class PlanningReadSettlement:
    sequence: AuditSequence
    stop_reason: Literal["PLANNING_READ_LIMIT"] | None = None


class PlanningReadDenied(RuntimeError):
    pass


class PlanningSnapshotReader(Protocol):
    def read_tracked_file(
        self,
        base_oid: GitOid,
        path: CanonicalPath,
        authorization: PlanningReadAuthorizationDocument,
    ) -> tuple[str, Sha256DigestText, bool]: ...

    def search_tracked_content(
        self,
        base_oid: GitOid,
        query: str,
        paths: tuple[CanonicalPath, ...],
        authorization: PlanningReadAuthorizationDocument,
    ) -> tuple[Mapping[str, object], ...]: ...


class BoundedPlanningReadGateway:
    def __init__(self, reader: PlanningSnapshotReader) -> None:
        self._reader = reader

    def execute(
        self, intent: PlanningReadIntent, authorization: PlanningAuthorization
    ) -> PlanningReadResult:
        if (
            authorization.decision != "ALLOW"
            or authorization.read_authorization is None
            or authorization.turn_binding is None
        ):
            raise ValueError("PLANNING_READ_AUTHORIZATION_NOT_ALLOWED")
        binding = authorization.turn_binding
        if (
            intent.run_id != authorization.run_id
            or intent.applicable_revision_digests != authorization.applicable_revision_digests
            or intent.repository_id != binding.repository_id
            or intent.base_oid != binding.pinned_base_oid
            or intent.snapshot_digest != binding.snapshot_digest
            or intent.scope_digest != binding.scope_digest
        ):
            raise ValueError("PLANNING_READ_BINDING_MISMATCH")
        try:
            if isinstance(intent.action, PlanningReadTrackedFileAction):
                content, digest, truncated = self._reader.read_tracked_file(
                    intent.base_oid,
                    CanonicalPath.parse(intent.action.path),
                    authorization.read_authorization,
                )
                payload: Mapping[str, object] = {
                    "content": content,
                    "content_digest": digest,
                    "path": intent.action.path,
                    "truncated": truncated,
                }
                result_class: Literal["READ_COMPLETED", "SEARCH_COMPLETED", "DENIED"] = (
                    "READ_COMPLETED"
                )
            else:
                payload = {
                    "matches": self._reader.search_tracked_content(
                        intent.base_oid,
                        intent.action.query,
                        tuple(CanonicalPath.parse(path) for path in intent.action.paths),
                        authorization.read_authorization,
                    )
                }
                result_class = "SEARCH_COMPLETED"
        except (PlanningReadDenied, UnicodeError):
            payload = {"reason": "PLANNING_READ_DENIED"}
            result_class = "DENIED"
        return PlanningReadResult(
            intent_id=intent.intent_id,
            run_id=intent.run_id,
            result_class=result_class,
            bounded_payload=payload,
            snapshot_digest=intent.snapshot_digest,
            returned_bytes=(
                0 if result_class == "DENIED" else len(canonical_json(payload).encode("utf-8"))
            ),
        )


class PlanningAuthorizationProvider(Protocol):
    def current(self, run_id: RunId) -> PlanningAuthorization: ...

    def current_for_recovery(
        self, run_id: RunId, action: RecoveredModelAction
    ) -> PlanningAuthorization: ...


class PlanningContextOverflow(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PlanningManifest:
    entries: tuple[tuple[CanonicalPath, Sha256DigestText, int], ...]
    total_bytes: int

    def __post_init__(self) -> None:
        if self.total_bytes < 0:
            raise ValueError("PLANNING_MANIFEST_BYTES_INVALID")


class PlanningContextBuilder(Protocol):
    def build_planning_request(
        self, run_id: RunId, authorization: PlanningAuthorization
    ) -> ModelRequest: ...


class PlanningManifestReader(Protocol):
    def manifest(self, binding: PlanningTurnBinding) -> PlanningManifest: ...


class PlanningRequestFactory(Protocol):
    def create(
        self,
        *,
        run_id: RunId,
        authorization: PlanningAuthorization,
        manifest: PlanningManifest,
    ) -> ModelRequest: ...


class BoundedPlanningContextBuilder:
    def __init__(self, manifests: PlanningManifestReader, requests: PlanningRequestFactory) -> None:
        self._manifests = manifests
        self._requests = requests

    def build_planning_request(
        self, run_id: RunId, authorization: PlanningAuthorization
    ) -> ModelRequest:
        if (
            authorization.decision != "ALLOW"
            or authorization.turn_binding is None
            or authorization.read_authorization is None
            or authorization.run_id != run_id
        ):
            raise ValueError("PLANNING_CONTEXT_AUTHORIZATION_MISMATCH")
        manifest = self._manifests.manifest(authorization.turn_binding)
        limits = authorization.read_authorization
        if (
            len(manifest.entries) > limits.max_manifest_entries
            or manifest.total_bytes > limits.max_manifest_bytes
        ):
            raise PlanningContextOverflow()
        return self._requests.create(run_id=run_id, authorization=authorization, manifest=manifest)


class PlanningActionPort(Protocol):
    def apply(
        self,
        *,
        run_id: RunId,
        logical_turn_id: LogicalTurnId,
        action: Mapping[str, object],
        authorization: PlanningAuthorization,
        recovered_marker: EffectIntent | None,
        permit: RuntimePermit | None,
        expected_sequence: AuditSequence,
    ) -> RuntimeDecision: ...


class PlanningState(Protocol):
    def audit_sequence(self, run_id: RunId) -> AuditSequence: ...

    def return_to_draft_for_planning_context_overflow(
        self,
        run_id: RunId,
        authorization: PlanningAuthorization,
        expected_sequence: AuditSequence,
    ) -> AuditSequence: ...

    def record_planning_read_intent(
        self,
        intent: PlanningReadIntent,
        recovered_marker: EffectIntent | None,
        permit: RuntimePermit | None,
        expected_sequence: AuditSequence,
    ) -> AuditSequence: ...

    def settle_planning_read(
        self,
        intent: PlanningReadIntent,
        result: PlanningReadResult,
        expected_sequence: AuditSequence,
    ) -> PlanningReadSettlement: ...

    def persist_submitted_plan(
        self,
        run_id: RunId,
        plan_document: Mapping[str, object],
        authorization: PlanningAuthorization,
        logical_turn_id: LogicalTurnId,
        recovered_marker: EffectIntent | None,
        permit: RuntimePermit | None,
        expected_sequence: AuditSequence,
    ) -> AuditSequence: ...

    def record_planning_failure_or_invalid_action(
        self,
        run_id: RunId,
        logical_turn_id: LogicalTurnId,
        reason: str,
        authorization: PlanningAuthorization,
        recovered_marker: EffectIntent | None,
        permit: RuntimePermit | None,
        expected_sequence: AuditSequence,
    ) -> AuditSequence: ...


class PlanningIdSource(Protocol):
    def next_intent_id(self, run_id: RunId) -> IntentId: ...


class PlanningReadGateway(Protocol):
    def execute(
        self, intent: PlanningReadIntent, authorization: PlanningAuthorization
    ) -> PlanningReadResult: ...


class PlanningActionApplier:
    def __init__(
        self, state: PlanningState, reads: PlanningReadGateway, ids: PlanningIdSource
    ) -> None:
        self._state = state
        self._reads = reads
        self._ids = ids

    def apply(
        self,
        *,
        run_id: RunId,
        logical_turn_id: LogicalTurnId,
        action: Mapping[str, object],
        authorization: PlanningAuthorization,
        recovered_marker: EffectIntent | None,
        permit: RuntimePermit | None,
        expected_sequence: AuditSequence,
    ) -> RuntimeDecision:
        if (recovered_marker is None) != (permit is None):
            raise ValueError("RECOVERED_MARKER_PERMIT_BINDING_MISMATCH")
        if permit is not None and permit.state != "CONSUMED":
            raise ValueError("RECOVERED_MARKER_PERMIT_NOT_CONSUMED")
        if authorization.decision != "ALLOW" or authorization.turn_binding is None:
            raise ValueError("PLANNING_ACTION_AUTHORIZATION_NOT_ALLOWED")
        try:
            parsed = PLANNING_ACTION_ADAPTER.validate_python(action)
        except ValidationError:
            sequence = self._state.record_planning_failure_or_invalid_action(
                run_id,
                logical_turn_id,
                "INVALID_PLANNING_ACTION",
                authorization,
                recovered_marker,
                permit,
                expected_sequence,
            )
            if authorization.planning_request_count + 1 >= authorization.planning_request_ceiling:
                return RuntimeDecision.pause("PLANNING_REQUEST_LIMIT", sequence)
            return RuntimeDecision.invalid_planning_action(sequence)
        if isinstance(parsed, (PlanningReadTrackedFileAction, PlanningSearchTrackedContentAction)):
            binding = authorization.turn_binding
            intent = PlanningReadIntent(
                intent_id=self._ids.next_intent_id(run_id),
                run_id=run_id,
                logical_turn_id=logical_turn_id,
                action=parsed,
                applicable_revision_digests=authorization.applicable_revision_digests,
                repository_id=binding.repository_id,
                base_oid=binding.pinned_base_oid,
                snapshot_digest=binding.snapshot_digest,
                scope_digest=binding.scope_digest,
                idempotency_key=f"planning-read:{run_id}:{logical_turn_id}",
            )
            self._state.record_planning_read_intent(
                intent, recovered_marker, permit, expected_sequence
            )
            result = self._reads.execute(intent, authorization)
            settlement = self._state.settle_planning_read(
                intent, result, self._state.audit_sequence(run_id)
            )
            if settlement.stop_reason is not None:
                return RuntimeDecision.pause(settlement.stop_reason, settlement.sequence)
            return RuntimeDecision.continued(settlement.sequence)
        if isinstance(parsed, PlanningSubmitPlanAction):
            sequence = self._state.persist_submitted_plan(
                run_id,
                parsed.plan_document,
                authorization,
                logical_turn_id,
                recovered_marker,
                permit,
                expected_sequence,
            )
            return RuntimeDecision.pause("AWAITING_PLAN_APPROVAL", sequence)
        sequence = self._state.record_planning_failure_or_invalid_action(
            run_id,
            logical_turn_id,
            parsed.reason,
            authorization,
            recovered_marker,
            permit,
            expected_sequence,
        )
        return RuntimeDecision.pause("PLANNING_FAILED", sequence)


class Clock(Protocol):
    def now(self) -> datetime: ...


class AuthorityModelJournal(ModelJournal, Protocol):
    def model_counters(self, run_id: RunId): ...  # type: ignore[no-untyped-def]

    def task_budget_state(self, run_id: RunId, task_id: TaskId): ...  # type: ignore[no-untyped-def]

    def new_dispatch_open(self, run_id: RunId) -> bool: ...

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


class AuthorityModelClient:
    """The sole Coordinator/WorkerLoop path to a provider in production."""

    def __init__(
        self,
        model: ModelPort,
        journal: AuthorityModelJournal,
        authority: Authority,
        clock: Clock | Callable[[], datetime],
        backoff: BackoffPort | None = None,
    ) -> None:
        self._model = model
        self._journal = journal
        self._authority = authority
        self._clock = clock
        self._backoff = ImmediateBackoff() if backoff is None else backoff

    def _now(self) -> datetime:
        return self._clock() if callable(self._clock) else self._clock.now()

    def complete(
        self, first_request: ModelReservationRequest
    ) -> ModelDispatchResult | ModelReservation:
        if first_request.turn is not None or first_request.provider_attempt_number != 1:
            raise ValueError("FIRST_MODEL_ATTEMPT_REQUIRED")
        request = first_request
        for retry_index in range(V01_MECHANISM_LIMITS.provider_retry_ceiling + 1):
            action_id = "model-barrier-" + sha256_digest(
                canonical_json(
                    {
                        "attempt": request.provider_attempt_number,
                        "request_digest": request.model_request.request_digest,
                        "run_id": request.run_id,
                    }
                )
            ).removeprefix("sha256:")
            self._journal.begin_runtime_barrier(
                request.run_id, action_id, self._journal.audit_sequence(request.run_id)
            )
            request = replace(
                request,
                expected_sequence=self._journal.audit_sequence(request.run_id),
                expected_run_counters=self._journal.model_counters(request.run_id),
                expected_task_counters=(
                    None
                    if request.owner_kind == "PLANNING" or request.task_id is None
                    else self._journal.task_budget_state(request.run_id, request.task_id)
                ),
            )
            reservation = self._authority.reserve_model_attempt(request)
            if reservation.decision != "RESERVED":
                self._journal.settle_runtime_barrier(
                    request.run_id,
                    action_id,
                    0,
                    "BUDGET_STOP" if not self._journal.new_dispatch_open(request.run_id) else None,
                    self._journal.audit_sequence(request.run_id),
                )
                return reservation
            if reservation.turn is None or reservation.intent is None:
                raise AssertionError("reserved model attempt has no turn or intent")
            provider_result = self._model.complete(request.model_request)
            settled = self._journal.settle_model_attempt(
                reservation.intent,
                provider_result,
                self._journal.audit_sequence(request.run_id),
            )
            self._journal.settle_runtime_barrier(
                request.run_id,
                action_id,
                0,
                "BUDGET_STOP" if not self._journal.new_dispatch_open(request.run_id) else None,
                self._journal.audit_sequence(request.run_id),
            )
            if (
                settled.kind != ProviderAttemptKind.KNOWN_CLOSED_REJECTION
                or retry_index == V01_MECHANISM_LIMITS.provider_retry_ceiling
            ):
                return settled.dispatch_result
            seconds = PROVIDER_RETRY_BACKOFF_SECONDS[retry_index]
            self._journal.record_model_backoff(
                request.run_id,
                reservation.intent.intent_id,
                seconds,
                self._journal.audit_sequence(request.run_id),
            )
            self._backoff.wait(seconds)
            started_at = self._now()
            request = replace(
                first_request,
                turn=reservation.turn,
                provider_attempt_number=retry_index + 2,
                started_at_utc=started_at,
                deadline_at_utc=started_at
                + timedelta(seconds=V01_MECHANISM_LIMITS.ordinary_action_timeout_seconds),
            )
        raise AssertionError("closed model retry loop exhausted without a result")


@dataclass(frozen=True, slots=True)
class TaskDispatchSelection:
    dispatch_id: str
    run_id: RunId
    task_id: TaskId
    task_contract_digest: str
    base_run_head_oid: str
    applicable_revision_digests: ApplicableRevisionDigests
    target_safety_digest: str
    credential_profile: str | None
    resume_allocation_id: str | None
    reserved_attempt_id: AttemptId | None
    expected_sequence: AuditSequence
    existing_attempt_id: AttemptId | None = None


class SchedulingState(Protocol):
    def next_dispatchable(self, run_id: RunId) -> TaskDispatchSelection | RuntimeDecision: ...


class WorkerAttemptCreator(Protocol):
    def create_attempt_with_lease(
        self,
        selection: TaskDispatchSelection,
        *,
        expected_sequence: AuditSequence,
    ) -> WorkerAttemptSnapshot: ...


class WorkerTurnRunner(Protocol):
    def run_turn(self, attempt_id: AttemptId) -> RuntimeDecision: ...


class CoordinatorService:
    _scheduling: SchedulingState
    _worker_attempts: WorkerAttemptCreator
    _workers: WorkerTurnRunner

    def __init__(
        self,
        *,
        planning_authorization: PlanningAuthorizationProvider,
        context: PlanningContextBuilder,
        models: AuthorityModelClient,
        planning_actions: PlanningActionPort,
        journal: AuthorityModelJournal,
        state: PlanningState,
        clock: Clock,
    ) -> None:
        self._planning_authorization = planning_authorization
        self._context = context
        self._models = models
        self._planning_actions = planning_actions
        self._journal = journal
        self._state = state
        self._clock = clock

    @classmethod
    def for_worker_scheduling(
        cls,
        *,
        scheduling: SchedulingState,
        attempts: WorkerAttemptCreator,
        workers: WorkerTurnRunner,
    ) -> Self:
        service = cls.__new__(cls)
        service._scheduling = scheduling
        service._worker_attempts = attempts
        service._workers = workers
        return service

    def schedule(self, run_id: RunId) -> RuntimeDecision:
        selection = self._scheduling.next_dispatchable(run_id)
        if isinstance(selection, RuntimeDecision):
            return selection
        attempt = self._worker_attempts.create_attempt_with_lease(
            selection,
            expected_sequence=selection.expected_sequence,
        )
        return self._workers.run_turn(attempt.attempt_id)

    def run_planning_turn(self, run_id: RunId) -> RuntimeDecision:
        authorization = self._planning_authorization.current(run_id)
        if authorization.decision != "ALLOW":
            assert authorization.reason is not None
            return RuntimeDecision.pause(authorization.reason)
        try:
            model_request = replace(
                self._context.build_planning_request(run_id, authorization),
                owner_kind="PLANNING",
                task_id=None,
                attempt_id=None,
                tranche_id=None,
            )
        except PlanningContextOverflow:
            sequence = self._state.return_to_draft_for_planning_context_overflow(
                run_id, authorization, self._journal.audit_sequence(run_id)
            )
            return RuntimeDecision.pause("PLANNING_CONTEXT_OVERFLOW", sequence)
        started_at = self._clock.now()
        outcome = self._models.complete(
            ModelReservationRequest(
                run_id=run_id,
                owner_kind="PLANNING",
                task_id=None,
                attempt_id=None,
                tranche_id=None,
                turn=None,
                model_request=model_request,
                provider_attempt_number=1,
                target_safety_digest=authorization.target_safety_digest,
                credential_profile=authorization.credential_profile,
                expected_run_counters=self._journal.model_counters(run_id),
                expected_task_counters=None,
                started_at_utc=started_at,
                deadline_at_utc=started_at
                + timedelta(seconds=V01_MECHANISM_LIMITS.ordinary_action_timeout_seconds),
                expected_sequence=self._journal.audit_sequence(run_id),
            )
        )
        if isinstance(outcome, ModelReservation):
            return RuntimeDecision.pause(outcome.reason)
        if outcome.normalized_action is None:
            return RuntimeDecision.pause("RETURNED_MODEL_MISMATCH")
        return self._planning_actions.apply(
            run_id=run_id,
            logical_turn_id=outcome.logical_turn_id,
            action=outcome.normalized_action,
            authorization=authorization,
            recovered_marker=None,
            permit=None,
            expected_sequence=self._journal.audit_sequence(run_id),
        )

    def resume_recovered_planning_action(
        self,
        run_id: RunId,
        permit: RuntimePermit,
        action: RecoveredModelAction,
    ) -> RuntimeDecision:
        if (
            action.turn.owner_kind != "PLANNING"
            or action.turn.run_id != run_id
            or action.turn.task_id is not None
            or action.turn.attempt_id is not None
            or action.turn.tranche_id is not None
        ):
            raise ValueError("RECOVERED_PLANNING_OWNER_BINDING_MISMATCH")
        authorization = self._planning_authorization.current_for_recovery(run_id, action)
        if authorization.decision != "ALLOW":
            assert authorization.reason is not None
            return RuntimeDecision.pause(authorization.reason)
        return self._planning_actions.apply(
            run_id=run_id,
            logical_turn_id=action.turn.logical_turn_id,
            action=action.normalized_action,
            authorization=authorization,
            recovered_marker=action.effect_intent,
            permit=permit,
            expected_sequence=self._journal.audit_sequence(run_id),
        )
