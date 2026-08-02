from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast

import pytest
from pydantic import ValidationError

from apexcrew.domain.commands import ApplicableRevisionDigests
from apexcrew.domain.coordination import (
    BoundedPlanningReadGateway,
    CoordinatorService,
    PlanningAuthorization,
    PlanningReadIntent,
    PlanningReadTrackedFileAction,
    PlanningTurnBinding,
    planning_snapshot_digest,
)
from apexcrew.domain.plan import CanonicalPath
from apexcrew.domain.policy import PlanningPathPolicy, SecretPathPolicy
from apexcrew.domain.revisions import HardDeniedPathClass, PlanningReadAuthorizationDocument
from apexcrew.domain.types import AuditSequence, GitOid, IntentId, RepositoryId, RunId


def read_authorization() -> PlanningReadAuthorizationDocument:
    return PlanningReadAuthorizationDocument(
        matcher_version="apexcrew-path-v1",
        positive_globs=("src/**",),
        hard_denied_path_classes=tuple(HardDeniedPathClass),
        max_manifest_entries=2_000,
        max_manifest_bytes=131_072,
        max_file_bytes=131_072,
        max_total_returned_bytes=2_097_152,
        max_search_matches=200,
        max_search_bytes=65_536,
    )


def turn_binding() -> PlanningTurnBinding:
    repository_id = RepositoryId("repository-1")
    base = GitOid("1" * 40)
    scope = "sha256:" + "2" * 64
    return PlanningTurnBinding(
        repository_id=repository_id,
        pinned_base_oid=base,
        scope_digest=scope,
        snapshot_digest=planning_snapshot_digest(repository_id, base, scope),
    )


def authorization(
    decision: str = "ALLOW", *, count: int = 0, ceiling: int = 8
) -> PlanningAuthorization:
    allowed = decision == "ALLOW"
    return PlanningAuthorization(
        run_id=RunId("run-plan"),
        decision=cast(object, decision),
        reason=None if allowed else "AWAITING_BOOTSTRAP_APPROVAL",
        applicable_revision_digests=ApplicableRevisionDigests(
            policy_digest="sha256:" + "3" * 64,
            budget_digest="sha256:" + "4" * 64,
            model_configuration_digest="sha256:" + "5" * 64,
        ),
        target_safety_digest="sha256:" + "6" * 64,
        credential_profile=None,
        read_authorization=read_authorization() if allowed else None,
        turn_binding=turn_binding() if allowed else None,
        planning_request_count=count,
        planning_request_ceiling=ceiling,
    )


@dataclass
class StaticAuthorizationProvider:
    value: PlanningAuthorization

    def current(self, run_id: RunId) -> PlanningAuthorization:
        del run_id
        return self.value

    def current_for_recovery(self, run_id: RunId, action: object) -> PlanningAuthorization:
        del run_id, action
        return self.value


class NeverContext:
    def build_planning_request(self, run_id: RunId, authorization: PlanningAuthorization) -> object:
        del run_id, authorization
        raise AssertionError("planning context must not be opened")


class NeverModels:
    call_count = 0

    def complete(self, request: object) -> object:
        del request
        self.call_count += 1
        raise AssertionError("model must not be called")


class MinimalJournal:
    def audit_sequence(self, run_id: RunId) -> AuditSequence:
        del run_id
        return AuditSequence(0)


class NeverActions:
    def apply(self, **kwargs: object) -> object:
        del kwargs
        raise AssertionError("planning action must not be applied")


def test_planning_never_dispatches_before_bootstrap_approval() -> None:
    models = NeverModels()
    coordinator = CoordinatorService(
        planning_authorization=StaticAuthorizationProvider(authorization("PAUSE")),
        context=NeverContext(),
        models=cast(object, models),
        planning_actions=cast(object, NeverActions()),
        journal=cast(object, MinimalJournal()),
        state=cast(object, object()),
        clock=cast(object, object()),
    )
    decision = coordinator.run_planning_turn(RunId("run-plan"))
    assert decision.stop_reason == "AWAITING_BOOTSTRAP_APPROVAL"
    assert models.call_count == 0


def test_planning_authorization_requires_a_complete_allow_or_pause_binding() -> None:
    with pytest.raises(ValidationError, match="PLANNING_ALLOW_BINDING_INVALID"):
        PlanningAuthorization.model_validate(
            {**authorization().model_dump(), "read_authorization": None}
        )
    with pytest.raises(ValidationError, match="PLANNING_PAUSE_BINDING_INVALID"):
        PlanningAuthorization.model_validate(
            {**authorization("PAUSE").model_dump(), "turn_binding": turn_binding()}
        )


@dataclass
class RecordingSnapshotReader:
    call_count: int = 0

    def read_tracked_file(
        self,
        base_oid: GitOid,
        path: CanonicalPath,
        authorization: PlanningReadAuthorizationDocument,
    ) -> tuple[str, str, bool]:
        del base_oid, path, authorization
        self.call_count += 1
        return "x", "sha256:" + "7" * 64, False

    def search_tracked_content(
        self,
        base_oid: GitOid,
        query: str,
        paths: tuple[CanonicalPath, ...],
        authorization: PlanningReadAuthorizationDocument,
    ) -> tuple[Mapping[str, object], ...]:
        del base_oid, query, paths, authorization
        self.call_count += 1
        return ()


def test_gateway_rejects_any_drift_from_the_authorized_snapshot_before_read() -> None:
    reader = RecordingSnapshotReader()
    gateway = BoundedPlanningReadGateway(reader)
    approved = authorization()
    binding = approved.turn_binding
    assert binding is not None
    intent = PlanningReadIntent(
        intent_id=IntentId("intent-1"),
        run_id=approved.run_id,
        logical_turn_id="turn-1",
        action=PlanningReadTrackedFileAction(path="src/a.py"),
        applicable_revision_digests=approved.applicable_revision_digests,
        repository_id=RepositoryId("other-repository"),
        base_oid=binding.pinned_base_oid,
        snapshot_digest=binding.snapshot_digest,
        scope_digest=binding.scope_digest,
        idempotency_key="planning-read:run-plan:turn-1",
    )
    with pytest.raises(ValueError, match="PLANNING_READ_BINDING_MISMATCH"):
        gateway.execute(intent, approved)
    assert reader.call_count == 0


def test_planning_read_result_counts_canonical_returned_bytes() -> None:
    reader = RecordingSnapshotReader()
    gateway = BoundedPlanningReadGateway(reader)
    approved = authorization()
    binding = approved.turn_binding
    assert binding is not None
    intent = PlanningReadIntent(
        intent_id=IntentId("intent-1"),
        run_id=approved.run_id,
        logical_turn_id="turn-1",
        action=PlanningReadTrackedFileAction(path="src/a.py"),
        applicable_revision_digests=approved.applicable_revision_digests,
        repository_id=binding.repository_id,
        base_oid=binding.pinned_base_oid,
        snapshot_digest=binding.snapshot_digest,
        scope_digest=binding.scope_digest,
        idempotency_key="planning-read:run-plan:turn-1",
    )
    result = gateway.execute(intent, approved)
    assert result.result_class == "READ_COMPLETED"
    assert result.returned_bytes > 0
    assert reader.call_count == 1


@pytest.mark.parametrize("path", [".git/config", ".apexcrew/state.db"])
def test_planning_path_policy_hard_denies_internal_metadata(path: str) -> None:
    broad_authorization = read_authorization().model_copy(update={"positive_globs": ("**",)})
    policy = PlanningPathPolicy(
        broad_authorization, SecretPathPolicy.from_host_rules((), b"test-key")
    )
    with pytest.raises(ValueError, match="PLANNING_READ_DENIED"):
        policy.require_allowed(CanonicalPath(path), broad_authorization)
