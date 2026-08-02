from __future__ import annotations

import json
from dataclasses import dataclass

from apexcrew.domain.commands import ApplicableRevisionDigests
from apexcrew.domain.effects import canonical_json, sha256_digest
from apexcrew.domain.plan import (
    CheckDefinition,
    GlobPattern,
    PlanRevision,
    PlanValidationError,
    TaskContract,
    validate_plan,
)
from apexcrew.domain.revisions import Sha256DigestText
from apexcrew.domain.types import GitOid, RevisionDigest, RunId, TaskId


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
