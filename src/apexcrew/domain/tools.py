from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from hashlib import sha256
from typing import Literal, Protocol, Self

from pydantic import Field, model_validator

from apexcrew.domain.actions import ReadAction, SearchAction, ToolActionEnvelope
from apexcrew.domain.authority import ActionDeadline, TimeoutDecision
from apexcrew.domain.commands import ApplicableRevisionDigests
from apexcrew.domain.effects import EffectIntent, EffectResult, canonical_json, sha256_digest
from apexcrew.domain.plan import CanonicalPath, GlobPattern, GlobProof, prove_included
from apexcrew.domain.policy import ActionPolicy, SecretPathPolicy
from apexcrew.domain.revisions import FrozenDocument, Sha256DigestText
from apexcrew.domain.types import AttemptId, AuditSequence, IntentId, RunId, TaskId

ToolOwnerKind = Literal["PLANNING", "WORKER", "ADMISSION"]
ToolResultCode = Literal[
    "READ_COMPLETED",
    "SEARCH_COMPLETED",
    "PATCH_APPLIED",
    "CHECK_PASSED",
    "CHECK_FAILED",
    "FINISHED",
    "FAILED",
    "DELETED",
    "RENAMED",
    "EXECUTABLE_CHANGED",
    "PROTECTED_PATCH_APPLIED",
    "SECRET_PATH_DENIED",
    "SCOPE_DENIED",
    "LEASE_SCOPE_DENIED",
    "NO_FOLLOW_PATH_DENIED",
    "APPROVAL_REQUIRED",
    "INFRASTRUCTURE_UNCERTAINTY",
    "INDETERMINATE",
]
ToolDenialCode = Literal[
    "SECRET_PATH_DENIED",
    "SCOPE_DENIED",
    "LEASE_SCOPE_DENIED",
    "NO_FOLLOW_PATH_DENIED",
    "APPROVAL_REQUIRED",
]


class SnapshotUnavailable(RuntimeError):
    pass


class SnapshotNoFollowDenied(RuntimeError):
    pass


class SnapshotEntry(FrozenDocument):
    path: str
    size: int = Field(ge=0)


class RepositorySnapshot(Protocol):
    def entries(self) -> tuple[SnapshotEntry, ...]: ...

    def read(self, path: CanonicalPath, maximum: int) -> bytes: ...


class ToolDenialAudit(FrozenDocument):
    run_id: RunId
    task_id: TaskId | None
    attempt_id: AttemptId | None
    action_id: str
    applicable_revision_digests: ApplicableRevisionDigests
    result_code: ToolDenialCode


class ToolDenialJournal(Protocol):
    def record_tool_denial(
        self, denial: ToolDenialAudit, expected_sequence: AuditSequence
    ) -> AuditSequence: ...


class ToolMatch(FrozenDocument):
    path: str
    byte_offset: int = Field(ge=0)
    content_digest: Sha256DigestText


class ActionPreState(FrozenDocument):
    source_digest: Sha256DigestText | None = None
    source_mode: int | None = None
    destination_digest: Sha256DigestText | None = None
    destination_absent: bool = False
    protected_scope_digest: Sha256DigestText | None = None

    def canonical_json(self) -> str:
        return canonical_json(self.model_dump(mode="json", exclude_none=True))


class ToolIntent(FrozenDocument):
    intent_id: IntentId
    run_id: RunId
    owner_kind: ToolOwnerKind
    task_id: TaskId | None
    attempt_id: AttemptId | None
    action_id: str
    action: ToolActionEnvelope
    authorization_binding_digest: Sha256DigestText
    applicable_revision_digests: ApplicableRevisionDigests
    repository_id: str
    snapshot_digest: Sha256DigestText
    scope_digest: Sha256DigestText
    dependency_fingerprint_basis: Sha256DigestText
    idempotency_key: str
    expected_prestate_json: str = "{}"
    expected_poststate_digest: Sha256DigestText | None = None

    @model_validator(mode="after")
    def validate_owner(self) -> Self:
        if self.owner_kind == "WORKER":
            if self.task_id is None or self.attempt_id is None:
                raise ValueError("WORKER_TOOL_OWNER_INCOMPLETE")
        elif self.task_id is not None or self.attempt_id is not None:
            raise ValueError("NON_WORKER_TOOL_OWNER_HAS_TASK")
        return self

    @classmethod
    def for_authorized_worker_action(
        cls,
        *,
        intent_id: IntentId,
        run_id: RunId,
        task_id: TaskId,
        attempt_id: AttemptId,
        action_id: str,
        action: ToolActionEnvelope,
        authorization_binding_digest: Sha256DigestText,
        applicable_revision_digests: ApplicableRevisionDigests,
        repository_id: str,
        snapshot_digest: Sha256DigestText,
        scope_digest: Sha256DigestText,
        dependency_fingerprint_basis: Sha256DigestText,
        idempotency_key: str,
        expected_prestate_json: str,
    ) -> ToolIntent:
        return cls(
            intent_id=intent_id,
            run_id=run_id,
            owner_kind="WORKER",
            task_id=task_id,
            attempt_id=attempt_id,
            action_id=action_id,
            action=action,
            authorization_binding_digest=authorization_binding_digest,
            applicable_revision_digests=applicable_revision_digests,
            repository_id=repository_id,
            snapshot_digest=snapshot_digest,
            scope_digest=scope_digest,
            dependency_fingerprint_basis=dependency_fingerprint_basis,
            idempotency_key=idempotency_key,
            expected_prestate_json=expected_prestate_json,
        )

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
            expected_prestate_json=self.expected_prestate_json,
            task_id=self.task_id,
            attempt_id=self.attempt_id,
            action_id=self.action_id,
        )

    @classmethod
    def from_effect_intent(cls, effect: EffectIntent) -> ToolIntent:
        payload = json.loads(effect.normalized_payload_json)
        intent = cls.model_validate(payload)
        if intent.to_effect_intent(effect.recorded_sequence) != effect:
            raise ValueError("TOOL_EFFECT_INTENT_BINDING_MISMATCH")
        return intent


class ToolResult(FrozenDocument):
    code: ToolResultCode
    run_id: RunId | None = None
    intent_id: IntentId | None = None
    passed: bool | None = None
    timed_out: bool = False
    matches: tuple[ToolMatch, ...] = ()
    bounded_payload: Mapping[str, object] = Field(default_factory=dict)
    content_digest: str | None = None

    def to_effect_result(self, settled_sequence: AuditSequence) -> EffectResult:
        if self.run_id is None or self.intent_id is None:
            raise ValueError("TOOL_RESULT_OWNERSHIP_MISSING")
        payload = canonical_json(self.model_dump(mode="json", exclude_none=True))
        outcome: Literal["COMPLETED", "FAILED", "STALE", "CONFLICT", "INDETERMINATE"]
        if self.code in {"INDETERMINATE", "INFRASTRUCTURE_UNCERTAINTY"}:
            outcome = "INDETERMINATE"
        elif self.code in {
            "SECRET_PATH_DENIED",
            "SCOPE_DENIED",
            "LEASE_SCOPE_DENIED",
            "NO_FOLLOW_PATH_DENIED",
            "APPROVAL_REQUIRED",
        }:
            outcome = "FAILED"
        else:
            outcome = "COMPLETED"
        snapshot = self.bounded_payload.get("snapshot_digest")
        return EffectResult(
            intent_id=self.intent_id,
            run_id=self.run_id,
            outcome=outcome,
            result_class=self.code,
            result_digest=sha256_digest(payload),
            bounded_result_json=payload,
            settled_sequence=settled_sequence,
            snapshot_digest=None if snapshot is None else Sha256DigestText(str(snapshot)),
        )

    @classmethod
    def approval_required(cls, intent: ToolIntent) -> ToolResult:
        return cls(code="APPROVAL_REQUIRED", run_id=intent.run_id, intent_id=intent.intent_id)

    @classmethod
    def from_timeout(cls, deadline: ActionDeadline, decision: TimeoutDecision) -> ToolResult:
        return cls(
            code=decision.outcome,
            run_id=deadline.run_id,
            intent_id=deadline.intent_id,
            passed=None,
            timed_out=True,
            bounded_payload=(
                {}
                if decision.retry_scope is None
                else {
                    "retry_scope": decision.retry_scope,
                    "snapshot_digest": deadline.snapshot_digest,
                }
            ),
        )

    @classmethod
    def indeterminate(
        cls,
        reason: str,
        *,
        run_id: RunId | None = None,
        intent_id: IntentId | None = None,
    ) -> ToolResult:
        return cls(
            code="INDETERMINATE",
            run_id=run_id,
            intent_id=intent_id,
            bounded_payload={"reason": reason},
        )


class ScopedToolRuntime:
    def __init__(
        self,
        *,
        snapshot: RepositorySnapshot,
        read_globs: Sequence[str],
        secret_paths: SecretPathPolicy,
        authorization_binding_digest: Sha256DigestText,
        applicable_revision_digests: ApplicableRevisionDigests,
        repository_id: str,
        snapshot_digest: Sha256DigestText,
        scope_digest: Sha256DigestText,
        dependency_fingerprint_basis: Sha256DigestText,
        max_file_bytes: int = 131_072,
        max_search_matches: int = 200,
        max_search_bytes: int = 65_536,
        denial_journal: ToolDenialJournal | None = None,
        denial_expected_sequence: AuditSequence | None = None,
    ) -> None:
        self._snapshot = snapshot
        self._read_globs = tuple(GlobPattern.parse(pattern) for pattern in read_globs)
        self._policy = ActionPolicy.default(secret_paths)
        self._authorization_binding_digest = authorization_binding_digest
        self._applicable_revision_digests = applicable_revision_digests
        self._repository_id = repository_id
        self._snapshot_digest = snapshot_digest
        self._scope_digest = scope_digest
        self._dependency_fingerprint_basis = dependency_fingerprint_basis
        self._max_file_bytes = max_file_bytes
        self._max_search_matches = max_search_matches
        self._max_search_bytes = max_search_bytes
        if (denial_journal is None) != (denial_expected_sequence is None):
            raise ValueError("TOOL_DENIAL_AUDIT_BINDING_INCOMPLETE")
        self._denial_journal = denial_journal
        self._denial_expected_sequence = denial_expected_sequence

    def execute(self, intent: ToolIntent) -> ToolResult:
        if not self._current_authorization(intent):
            return self._denied(intent, "LEASE_SCOPE_DENIED")
        if isinstance(intent.action, ReadAction):
            return self._read(intent)
        if isinstance(intent.action, SearchAction):
            return self._search(intent)
        if self._policy.classify(intent.action) == "REQUIRE_APPROVAL":
            return ToolResult.approval_required(intent)
        return self._denied(intent, "LEASE_SCOPE_DENIED")

    def _current_authorization(self, intent: ToolIntent) -> bool:
        return (
            intent.authorization_binding_digest == self._authorization_binding_digest
            and intent.applicable_revision_digests == self._applicable_revision_digests
            and intent.repository_id == self._repository_id
            and intent.snapshot_digest == self._snapshot_digest
            and intent.scope_digest == self._scope_digest
            and intent.dependency_fingerprint_basis == self._dependency_fingerprint_basis
        )

    def _selector_in_scope(self, selector: str) -> bool:
        try:
            requested = GlobPattern.parse(selector)
        except ValueError:
            return False
        return any(
            prove_included(requested, allowed) is GlobProof.PROVEN for allowed in self._read_globs
        )

    def _read(self, intent: ToolIntent) -> ToolResult:
        action = intent.action
        assert isinstance(action, ReadAction)
        if not self._selector_in_scope(action.path):
            return self._denied(intent, "SCOPE_DENIED")
        try:
            path = CanonicalPath.parse(action.path)
            entries = self._snapshot.entries()
        except SnapshotNoFollowDenied:
            entries = None
        except (UnicodeError, ValueError):
            return self._denied(intent, "SECRET_PATH_DENIED")
        if self._policy.classify(action) != "ALLOW":
            return self._denied(intent, "SECRET_PATH_DENIED")
        if entries is None:
            return self._denied(intent, "NO_FOLLOW_PATH_DENIED")
        exists = sum(entry.path == str(path) for entry in entries)
        if exists != 1:
            return self._denied(intent, "SECRET_PATH_DENIED")
        try:
            raw = self._snapshot.read(path, self._max_file_bytes)
        except SnapshotNoFollowDenied:
            return self._denied(intent, "NO_FOLLOW_PATH_DENIED")
        except SnapshotUnavailable:
            return self._denied(intent, "SECRET_PATH_DENIED")
        digest = Sha256DigestText("sha256:" + sha256(raw).hexdigest())
        try:
            content = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            return self._denied(intent, "SECRET_PATH_DENIED")
        fingerprint = self._dependency_fingerprint(((path, digest),))
        return ToolResult(
            code="READ_COMPLETED",
            run_id=intent.run_id,
            intent_id=intent.intent_id,
            bounded_payload={
                "content": content,
                "dependency_fingerprint": fingerprint,
                "path": str(path),
                "snapshot_digest": self._snapshot_digest,
                "truncated": False,
            },
            content_digest=digest,
        )

    def _search(self, intent: ToolIntent) -> ToolResult:
        action = intent.action
        assert isinstance(action, SearchAction)
        if any(not self._selector_in_scope(selector) for selector in action.paths):
            return self._denied(intent, "SCOPE_DENIED")
        if self._policy.classify(action) != "ALLOW":
            return self._denied(intent, "SECRET_PATH_DENIED")
        selectors = tuple(GlobPattern.parse(selector) for selector in action.paths)
        try:
            entries = self._snapshot.entries()
        except SnapshotNoFollowDenied:
            return self._denied(intent, "NO_FOLLOW_PATH_DENIED")
        needle = action.query.encode("utf-8")
        matches: list[ToolMatch] = []
        observations: list[tuple[CanonicalPath, Sha256DigestText]] = []
        returned_bytes = 0
        truncated = False
        for entry in entries:
            try:
                path = CanonicalPath.parse(entry.path)
            except ValueError:
                return self._denied(intent, "NO_FOLLOW_PATH_DENIED")
            if not any(selector.matches(path) for selector in selectors):
                continue
            if self._policy.classify(ReadAction(path=str(path))) != "ALLOW":
                continue
            try:
                raw = self._snapshot.read(path, self._max_file_bytes)
            except SnapshotNoFollowDenied:
                return self._denied(intent, "NO_FOLLOW_PATH_DENIED")
            except SnapshotUnavailable:
                return self._denied(intent, "SECRET_PATH_DENIED")
            digest = Sha256DigestText("sha256:" + sha256(raw).hexdigest())
            observations.append((path, digest))
            start = 0
            while (offset := raw.find(needle, start)) != -1:
                match_size = len(str(path).encode("utf-8"))
                if (
                    len(matches) == self._max_search_matches
                    or returned_bytes + match_size > self._max_search_bytes
                ):
                    truncated = True
                    break
                matches.append(ToolMatch(path=str(path), byte_offset=offset, content_digest=digest))
                returned_bytes += match_size
                start = offset + len(needle)
            if truncated:
                break
        matches.sort(key=lambda match: (match.path, match.byte_offset))
        return ToolResult(
            code="SEARCH_COMPLETED",
            run_id=intent.run_id,
            intent_id=intent.intent_id,
            matches=tuple(matches),
            bounded_payload={
                "dependency_fingerprint": self._dependency_fingerprint(observations),
                "snapshot_digest": self._snapshot_digest,
                "truncated": truncated,
            },
        )

    def _dependency_fingerprint(
        self, observations: Sequence[tuple[CanonicalPath, Sha256DigestText]]
    ) -> Sha256DigestText:
        payload = canonical_json(
            {
                "basis": self._dependency_fingerprint_basis,
                "observations": [
                    {"content_digest": digest, "path": str(path)} for path, digest in observations
                ],
            }
        )
        return sha256_digest(payload)

    def _denied(self, intent: ToolIntent, code: ToolDenialCode) -> ToolResult:
        if self._denial_journal is not None and self._denial_expected_sequence is not None:
            self._denial_expected_sequence = self._denial_journal.record_tool_denial(
                ToolDenialAudit(
                    run_id=intent.run_id,
                    task_id=intent.task_id,
                    attempt_id=intent.attempt_id,
                    action_id=intent.action_id,
                    applicable_revision_digests=intent.applicable_revision_digests,
                    result_code=code,
                ),
                self._denial_expected_sequence,
            )
        return ToolResult(code=code, run_id=intent.run_id, intent_id=intent.intent_id)
