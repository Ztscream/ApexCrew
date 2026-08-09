from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from hashlib import sha256
from pathlib import Path
from typing import Literal, Protocol, Self

from pydantic import Field, model_validator

from apexcrew.domain.actions import (
    CheckAction,
    PatchAction,
    ReadAction,
    RiskyAction,
    SearchAction,
    ToolActionEnvelope,
)
from apexcrew.domain.authority import (
    ActionClass,
    ActionDeadline,
    GrantedActionIntent,
    TimeoutDecision,
    WorkspaceLease,
    canonical_action_json,
)
from apexcrew.domain.commands import ApplicableRevisionDigests
from apexcrew.domain.effects import EffectIntent, EffectResult, canonical_json, sha256_digest
from apexcrew.domain.plan import (
    CanonicalPath,
    CheckDefinition,
    GlobPattern,
    GlobProof,
    prove_included,
)
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
    "EXECUTOR_UNAVAILABLE",
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
CHECK_DENIAL_CODES = frozenset(
    {
        "SECRET_PATH_DENIED",
        "SCOPE_DENIED",
        "LEASE_SCOPE_DENIED",
        "NO_FOLLOW_PATH_DENIED",
        "APPROVAL_REQUIRED",
    }
)
UNKNOWN_CHECK_ARGV_DIGEST = Sha256DigestText("sha256:" + "0" * 64)
MAX_EXECUTOR_OUTPUT_BYTES = 65_536


class ToolValidationError(ValueError):
    pass


class CheckDefinitionError(ValueError):
    pass


class ToolEffectResultError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class ToolAuthorizationError(ValueError):
    pass


class SnapshotUnavailable(RuntimeError):
    pass


class SnapshotNoFollowDenied(RuntimeError):
    pass


class PatchExecutionUncertain(RuntimeError):
    pass


class SnapshotEntry(FrozenDocument):
    path: str
    size: int = Field(ge=0)


class SanitizedSnapshotEntry(FrozenDocument):
    path: str
    kind: Literal["regular"]
    content_digest: Sha256DigestText

    @model_validator(mode="after")
    def validate_path(self) -> Self:
        CanonicalPath.parse(self.path)
        return self


class SanitizedSnapshot(FrozenDocument):
    root: Path
    repository_id: str = Field(min_length=1)
    tree_digest: Sha256DigestText
    dependency_fingerprint_digest: Sha256DigestText
    entries: tuple[SanitizedSnapshotEntry, ...]
    materialized_paths: tuple[str, ...]

    @model_validator(mode="after")
    def validate_regular_file_manifest(self) -> Self:
        paths = tuple(entry.path for entry in self.entries)
        if paths != tuple(sorted(paths)) or len(paths) != len(set(paths)):
            raise ValueError("SANITIZED_SNAPSHOT_PATHS_INVALID")
        if self.materialized_paths != paths:
            raise ValueError("SANITIZED_SNAPSHOT_MANIFEST_MISMATCH")
        return self

    @classmethod
    def from_regular_files(
        cls,
        *,
        root: Path,
        repository_id: str,
        tree_digest: Sha256DigestText,
        dependency_fingerprint_digest: Sha256DigestText,
        entries: Sequence[SanitizedSnapshotEntry],
        secret_paths: SecretPathPolicy,
    ) -> SanitizedSnapshot:
        ordered = tuple(sorted(entries, key=lambda entry: entry.path))
        for entry in ordered:
            path = CanonicalPath.parse(entry.path)
            if secret_paths.inspect(path).code != "ALLOW":
                raise ToolValidationError("SANITIZED_SNAPSHOT_DENIED")
        return cls(
            root=root,
            repository_id=repository_id,
            tree_digest=tree_digest,
            dependency_fingerprint_digest=dependency_fingerprint_digest,
            entries=ordered,
            materialized_paths=tuple(entry.path for entry in ordered),
        )


def _token_names_secret_path(token: str, secret_paths: SecretPathPolicy) -> bool:
    normalized = token.replace("\\", "/").strip("'\"`()[]{}<>,:;")
    normalized = re.sub(r"^[A-Za-z]:", "", normalized).lstrip("/")
    segments = normalized.split("/")
    for index in range(len(segments)):
        candidate = "/".join(segments[index:])
        try:
            path = CanonicalPath.parse(candidate)
        except ValueError:
            continue
        if secret_paths.inspect(path).code != "ALLOW":
            return True
    return False


def _redact_secret_lines(raw: bytes, secret_paths: SecretPathPolicy) -> str:
    text = raw.decode("utf-8", errors="replace")
    lines: list[str] = []
    for line in text.splitlines(keepends=True):
        tokens = re.findall(r"[^\s]+", line)
        lines.append(
            "[redacted]\n"
            if any(_token_names_secret_path(token, secret_paths) for token in tokens)
            else line
        )
    return "".join(lines)


def _bounded_chunks(chunks: Iterable[bytes], remaining: int) -> tuple[bytes, int, bool]:
    captured = bytearray()
    truncated = False
    for chunk in chunks:
        if not isinstance(chunk, bytes):
            raise TypeError("EXECUTOR_OUTPUT_BYTES_REQUIRED")
        if remaining == 0:
            if chunk:
                truncated = True
                break
            continue
        if len(chunk) > remaining:
            captured.extend(chunk[:remaining])
            remaining = 0
            truncated = True
            break
        captured.extend(chunk)
        remaining -= len(chunk)
    return bytes(captured), remaining, truncated


class ExecutionResult(FrozenDocument):
    code: Literal[
        "CHECK_PASSED",
        "CHECK_FAILED",
        "EXECUTOR_UNAVAILABLE",
        "INFRASTRUCTURE_UNCERTAINTY",
    ]
    passed: bool | None
    timed_out: bool
    output_digest: Sha256DigestText | None = None
    output: str = ""
    output_bytes: int = Field(ge=0, le=MAX_EXECUTOR_OUTPUT_BYTES)
    output_truncated: bool = False
    timing_ms: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_outcome(self) -> Self:
        expected = {
            "CHECK_PASSED": (True, False),
            "CHECK_FAILED": (False, False),
            "EXECUTOR_UNAVAILABLE": (None, False),
            "INFRASTRUCTURE_UNCERTAINTY": (None, True),
        }[self.code]
        if (self.passed, self.timed_out) != expected:
            raise ValueError("CHECK_RESULT_BINDING_INVALID")
        if len(self.output.encode("utf-8")) > MAX_EXECUTOR_OUTPUT_BYTES:
            raise ValueError("EXECUTOR_OUTPUT_LIMIT_EXCEEDED")
        return self

    @classmethod
    def from_output(
        cls,
        *,
        exit_code: int | None,
        timed_out: bool,
        stdout_chunks: Iterable[bytes] = (),
        stderr_chunks: Iterable[bytes] = (),
        timing_ms: int,
        secret_paths: SecretPathPolicy,
        executor_unavailable: bool = False,
    ) -> ExecutionResult:
        if executor_unavailable and timed_out:
            raise ValueError("EXECUTOR_OUTCOME_BINDING_INVALID")
        if exit_code is None and not timed_out and not executor_unavailable:
            raise ValueError("EXECUTOR_OUTCOME_UNOBSERVABLE")
        stdout, remaining, stdout_truncated = _bounded_chunks(
            stdout_chunks, MAX_EXECUTOR_OUTPUT_BYTES
        )
        stderr, _, stderr_truncated = _bounded_chunks(stderr_chunks, remaining)
        captured = stdout + stderr
        output = _redact_secret_lines(captured, secret_paths)
        encoded = output.encode("utf-8")
        if len(encoded) > MAX_EXECUTOR_OUTPUT_BYTES:
            output = encoded[:MAX_EXECUTOR_OUTPUT_BYTES].decode("utf-8", errors="ignore")
        code: Literal[
            "CHECK_PASSED",
            "CHECK_FAILED",
            "EXECUTOR_UNAVAILABLE",
            "INFRASTRUCTURE_UNCERTAINTY",
        ]
        if timed_out:
            code = "INFRASTRUCTURE_UNCERTAINTY"
            passed = None
        elif executor_unavailable or exit_code == 125:
            code = "EXECUTOR_UNAVAILABLE"
            passed = None
        elif exit_code == 0:
            code = "CHECK_PASSED"
            passed = True
        else:
            code = "CHECK_FAILED"
            passed = False
        return cls(
            code=code,
            passed=passed,
            timed_out=timed_out,
            output_digest=(
                Sha256DigestText("sha256:" + sha256(captured).hexdigest()) if captured else None
            ),
            output=output,
            output_bytes=len(output.encode("utf-8")),
            output_truncated=stdout_truncated or stderr_truncated,
            timing_ms=timing_ms,
        )

    def to_tool_result(self, intent: ToolIntent) -> ToolResult:
        return ToolResult(
            code=self.code,
            run_id=intent.run_id,
            intent_id=intent.intent_id,
            passed=self.passed,
            timed_out=self.timed_out,
            bounded_payload={
                "output": self.output,
                "output_bytes": self.output_bytes,
                "output_truncated": self.output_truncated,
                "snapshot_digest": intent.snapshot_digest,
                "timing_ms": self.timing_ms,
            },
            content_digest=self.output_digest,
        )


class ExecutorPort(Protocol):
    def run(
        self,
        argv: Sequence[str],
        snapshot: SanitizedSnapshot,
        timeout_seconds: int,
    ) -> ExecutionResult: ...


class PatchExecutionResult(FrozenDocument):
    code: Literal[
        "PATCH_APPLIED",
        "PATCH_RESULT_UNCERTAIN",
        "LEASE_SCOPE_DENIED",
        "SECRET_PATH_DENIED",
    ]
    post_tree_digest: Sha256DigestText | None = None


class PatchExecutorPort(Protocol):
    def apply_patch(
        self, lease: WorkspaceLease, patches: Mapping[str, bytes]
    ) -> PatchExecutionResult: ...


class DeclaredCheckRegistry:
    def __init__(self, definitions: Mapping[str, CheckDefinition]) -> None:
        checked: dict[str, CheckDefinition] = {}
        for check_id, definition in definitions.items():
            if not check_id or check_id.strip() != check_id:
                raise CheckDefinitionError("DECLARED_CHECK_ID_INVALID")
            self._validate(definition)
            checked[check_id] = definition
        self._definitions = checked

    @staticmethod
    def _validate(definition: CheckDefinition) -> None:
        if (
            not definition.argv
            or not definition.argv[0]
            or any(character.isspace() for character in definition.argv[0])
            or any(character in ";&|<>\x00" for character in definition.argv[0])
            or any(not token or "\x00" in token for token in definition.argv)
        ):
            raise CheckDefinitionError("STRUCTURED_ARGV_REQUIRED")

    def require(self, check_id: str) -> CheckDefinition:
        try:
            return self._definitions[check_id]
        except KeyError as error:
            raise CheckDefinitionError("DECLARED_CHECK_NOT_FOUND") from error

    def get(self, check_id: str) -> CheckDefinition | None:
        return self._definitions.get(check_id)


class CheckDeadlineJournal(Protocol):
    def action_deadline(self, intent_id: IntentId) -> ActionDeadline | None: ...

    def audit_sequence(self, run_id: RunId) -> AuditSequence: ...


class CheckDeadlineAuthority(Protocol):
    def deadline_state(self, deadline: ActionDeadline) -> str: ...

    def settle_timeout(
        self,
        deadline: ActionDeadline,
        outcome_observable: bool,
        expected_sequence: AuditSequence,
    ) -> TimeoutDecision: ...


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


class GrantedActionObservation(FrozenDocument):
    state: Literal["EXACT_PRE", "EXACT_POST", "THIRD", "UNAVAILABLE"]
    digest: Sha256DigestText
    post_result: ToolResult | None = None

    @model_validator(mode="after")
    def validate_post_result(self) -> Self:
        if (self.state == "EXACT_POST") != (self.post_result is not None):
            raise ValueError("GRANTED_ACTION_OBSERVATION_RESULT_INVALID")
        return self

    def result_for(self, intent: GrantedActionIntent) -> ToolResult:
        if self.state != "EXACT_POST" or self.post_result is None:
            raise ValueError("GRANTED_ACTION_POST_RESULT_REQUIRED")
        if self.post_result.run_id not in {None, intent.bindings.run_id}:
            raise ToolAuthorizationError("GRANTED_RESULT_RUN_MISMATCH")
        if self.post_result.intent_id not in {None, intent.intent_id}:
            raise ToolAuthorizationError("GRANTED_RESULT_INTENT_MISMATCH")
        return self.post_result.model_copy(
            update={"run_id": intent.bindings.run_id, "intent_id": intent.intent_id}
        )


class GrantedWorkspacePort(Protocol):
    def observe(
        self, action: RiskyAction, expected: ActionPreState
    ) -> GrantedActionObservation: ...

    def delete_regular_file(self, action: RiskyAction, expected: ActionPreState) -> ToolResult: ...

    def rename_regular_file(self, action: RiskyAction, expected: ActionPreState) -> ToolResult: ...

    def set_executable(self, action: RiskyAction, expected: ActionPreState) -> ToolResult: ...

    def apply_protected_patch(
        self, action: RiskyAction, expected: ActionPreState
    ) -> ToolResult: ...


class GrantedActionToolPort(Protocol):
    def observe_granted_action(self, intent: GrantedActionIntent) -> GrantedActionObservation: ...

    def execute_granted(self, intent: GrantedActionIntent) -> ToolResult: ...


class GrantedActionJournal(Protocol):
    def next_unsettled_granted_action(self, run_id: RunId) -> GrantedActionIntent | None: ...

    def require_unsettled_granted_intent(self, intent_id: IntentId) -> GrantedActionIntent: ...

    def require_granted_action_for_recovery(self, intent_id: IntentId) -> GrantedActionIntent: ...

    def mark_granted_action_dispatched(
        self,
        *,
        run_id: RunId,
        intent_id: IntentId,
        applicable_revision_digests: ApplicableRevisionDigests,
        expected_sequence: AuditSequence,
    ) -> GrantedActionIntent: ...

    def settle_granted_action(
        self,
        *,
        run_id: RunId,
        intent_id: IntentId,
        result: ToolResult,
        applicable_revision_digests: ApplicableRevisionDigests,
        expected_sequence: AuditSequence,
    ) -> AuditSequence: ...

    def mark_granted_action_indeterminate(
        self,
        *,
        run_id: RunId,
        intent_id: IntentId,
        observation_digest: Sha256DigestText,
        applicable_revision_digests: ApplicableRevisionDigests,
        expected_sequence: AuditSequence,
    ) -> AuditSequence: ...

    def audit_sequence(self, run_id: RunId) -> AuditSequence: ...


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

    @model_validator(mode="after")
    def validate_check_outcome(self) -> Self:
        expected = {
            "CHECK_PASSED": (True, False),
            "CHECK_FAILED": (False, False),
            "EXECUTOR_UNAVAILABLE": (None, False),
            "INFRASTRUCTURE_UNCERTAINTY": (None, True),
        }.get(self.code)
        if expected is not None and (self.passed, self.timed_out) != expected:
            raise ValueError("CHECK_RESULT_BINDING_INVALID")
        if expected is None and (
            self.passed is not None or self.timed_out and self.code != "INDETERMINATE"
        ):
            raise ValueError("CHECK_RESULT_BINDING_INVALID")
        return self

    def to_effect_result(self, settled_sequence: AuditSequence) -> EffectResult:
        if self.run_id is None or self.intent_id is None:
            raise ValueError("TOOL_RESULT_OWNERSHIP_MISSING")
        payload = canonical_json(self.model_dump(mode="json", exclude_none=True))
        outcome: Literal["COMPLETED", "FAILED", "STALE", "CONFLICT", "INDETERMINATE"]
        if self.code in {
            "INDETERMINATE",
            "EXECUTOR_UNAVAILABLE",
            "INFRASTRUCTURE_UNCERTAINTY",
        }:
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


GrantedActionObservation.model_rebuild()


def validate_tool_effect_result(intent: EffectIntent, result: EffectResult) -> None:
    if intent.kind not in {"patch", "check"}:
        return
    try:
        tool_intent = ToolIntent.from_effect_intent(intent)
        tool_result = ToolResult.model_validate_json(result.bounded_result_json)
    except (ValueError, TypeError) as error:
        code = (
            "CHECK_RESULT_BINDING_INVALID"
            if intent.kind == "check"
            else "PATCH_RESULT_BINDING_INVALID"
        )
        raise ToolEffectResultError(code) from error
    if tool_result.run_id != intent.run_id or tool_result.intent_id != intent.intent_id:
        raise ToolEffectResultError("TOOL_RESULT_BINDING_INVALID")
    if intent.kind == "check":
        check_codes = {
            "CHECK_PASSED",
            "CHECK_FAILED",
            "EXECUTOR_UNAVAILABLE",
            "INFRASTRUCTURE_UNCERTAINTY",
        } | CHECK_DENIAL_CODES
        if not isinstance(tool_intent.action, CheckAction) or tool_result.code not in check_codes:
            raise ToolEffectResultError("CHECK_RESULT_BINDING_INVALID")
        if result.snapshot_digest != tool_intent.snapshot_digest:
            raise ToolEffectResultError("CHECK_RESULT_BINDING_INVALID")
        if tool_result.code in CHECK_DENIAL_CODES:
            if tool_result.bounded_payload.get("snapshot_digest") != tool_intent.snapshot_digest:
                raise ToolEffectResultError("CHECK_RESULT_BINDING_INVALID")
            if result.outcome != "FAILED":
                raise ToolEffectResultError("CHECK_RESULT_BINDING_INVALID")
            return
        expected_outcome = (
            "INDETERMINATE"
            if tool_result.code in {"EXECUTOR_UNAVAILABLE", "INFRASTRUCTURE_UNCERTAINTY"}
            else "COMPLETED"
        )
        output = tool_result.bounded_payload.get("output", "")
        output_bytes = tool_result.bounded_payload.get("output_bytes", 0)
        if (
            result.outcome != expected_outcome
            or not isinstance(output, str)
            or not isinstance(output_bytes, int)
            or output_bytes != len(output.encode("utf-8"))
            or output_bytes > MAX_EXECUTOR_OUTPUT_BYTES
        ):
            raise ToolEffectResultError("CHECK_RESULT_BINDING_INVALID")
        return
    if not isinstance(tool_intent.action, PatchAction):
        raise ToolEffectResultError("PATCH_RESULT_BINDING_INVALID")
    if tool_result.code == "INFRASTRUCTURE_UNCERTAINTY":
        if (
            result.outcome != "INDETERMINATE"
            or result.snapshot_digest != tool_intent.snapshot_digest
        ):
            raise ToolEffectResultError("PATCH_RESULT_BINDING_INVALID")
        return
    if (
        tool_result.code != "PATCH_APPLIED"
        or result.outcome != "COMPLETED"
        or result.snapshot_digest != tool_intent.snapshot_digest
    ):
        raise ToolEffectResultError("PATCH_RESULT_BINDING_INVALID")


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
        recovery_snapshot_digest: Sha256DigestText | None = None,
        max_file_bytes: int = 131_072,
        max_search_matches: int = 200,
        max_search_bytes: int = 65_536,
        denial_journal: ToolDenialJournal | None = None,
        denial_expected_sequence: AuditSequence | None = None,
        executor: ExecutorPort | None = None,
        patch_executor: PatchExecutorPort | None = None,
        declared_checks: DeclaredCheckRegistry | None = None,
        sanitized_snapshot: SanitizedSnapshot | None = None,
        materialized_snapshot_digest: Sha256DigestText | None = None,
        deadline_journal: CheckDeadlineJournal | None = None,
        deadline_authority: CheckDeadlineAuthority | None = None,
        workspace_lease: WorkspaceLease | None = None,
        granted_workspace: GrantedWorkspacePort | None = None,
    ) -> None:
        self._snapshot = snapshot
        self._read_globs = tuple(GlobPattern.parse(pattern) for pattern in read_globs)
        self._policy = ActionPolicy.default(secret_paths)
        self._authorization_binding_digest = authorization_binding_digest
        self._applicable_revision_digests = applicable_revision_digests
        self._repository_id = repository_id
        self._snapshot_digest = snapshot_digest
        self._recovery_snapshot_digest = (
            snapshot_digest if recovery_snapshot_digest is None else recovery_snapshot_digest
        )
        self._scope_digest = scope_digest
        self._dependency_fingerprint_basis = dependency_fingerprint_basis
        self._max_file_bytes = max_file_bytes
        self._max_search_matches = max_search_matches
        self._max_search_bytes = max_search_bytes
        self._executor = executor
        self._patch_executor = patch_executor
        self._declared_checks = declared_checks
        self._sanitized_snapshot = sanitized_snapshot
        self._materialized_snapshot_digest = (
            materialized_snapshot_digest
            if materialized_snapshot_digest is not None
            else sanitized_snapshot.tree_digest
            if sanitized_snapshot is not None
            else snapshot_digest
        )
        self._deadline_journal = deadline_journal
        self._deadline_authority = deadline_authority
        self._workspace_lease = workspace_lease
        self._granted_workspace = granted_workspace
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
        if isinstance(intent.action, PatchAction):
            return self._patch(intent)
        if isinstance(intent.action, CheckAction):
            return self._check(intent)
        if self._policy.classify(intent.action) == "REQUIRE_APPROVAL":
            return ToolResult.approval_required(intent)
        return self._denied(intent, "LEASE_SCOPE_DENIED")

    def observe_recovery(
        self, intent: ToolIntent
    ) -> tuple[
        Literal["EXACT_POST", "EXACT_PRE", "EXACT_RECEIPT", "THIRD_STATE", "UNAVAILABLE"],
        ToolResult | None,
    ]:
        """Return bounded recovery evidence without replaying a patch."""
        if not self._current_authorization(intent):
            return "UNAVAILABLE", None
        if isinstance(intent.action, PatchAction):
            expected_post = intent.expected_poststate_digest
            observed_snapshot_digest = self._recovery_snapshot_digest
            if expected_post is not None and observed_snapshot_digest == expected_post:
                result = ToolResult(
                    code="PATCH_APPLIED",
                    run_id=intent.run_id,
                    intent_id=intent.intent_id,
                    bounded_payload={
                        "post_tree_digest": expected_post,
                        "pre_tree_digest": intent.snapshot_digest,
                        "snapshot_digest": intent.snapshot_digest,
                    },
                    content_digest=expected_post,
                )
                return "EXACT_POST", result
            if observed_snapshot_digest == intent.snapshot_digest:
                return "EXACT_PRE", None
            return "THIRD_STATE", None
        if isinstance(intent.action, CheckAction):
            result = self.execute(intent)
            if result.code not in {"CHECK_PASSED", "CHECK_FAILED"} | CHECK_DENIAL_CODES:
                return "UNAVAILABLE", None
            definition = (
                self._declared_checks.get(intent.action.check_id) if self._declared_checks else None
            )
            argv_digest = (
                UNKNOWN_CHECK_ARGV_DIGEST
                if definition is None
                else sha256_digest(canonical_json({"argv": list(definition.argv)}))
            )
            receipt_digest = result.content_digest or sha256_digest(
                canonical_json(result.model_dump(mode="json"))
            )
            bounded_payload = dict(result.bounded_payload)
            bounded_payload.update(
                {
                    "argv_digest": argv_digest,
                    "check_id": intent.action.check_id,
                    "receipt_digest": receipt_digest,
                    "snapshot_digest": intent.snapshot_digest,
                }
            )
            return "EXACT_RECEIPT", result.model_copy(
                update={"bounded_payload": bounded_payload, "content_digest": receipt_digest}
            )
        return "UNAVAILABLE", None

    def execute_granted(self, intent: GrantedActionIntent) -> ToolResult:
        if canonical_action_json(intent.action) != intent.normalized_action_json:
            raise ToolAuthorizationError("GRANTED_INTENT_MISMATCH")
        if self._granted_workspace is None:
            raise ToolAuthorizationError("GRANTED_WORKSPACE_NOT_CONFIGURED")
        handler = {
            "delete": self._granted_workspace.delete_regular_file,
            "rename": self._granted_workspace.rename_regular_file,
            "set_executable": self._granted_workspace.set_executable,
            "protected_patch": self._granted_workspace.apply_protected_patch,
        }[intent.action.operation]
        result = handler(intent.action, intent.expected_pre_state)
        if result.run_id not in {None, intent.bindings.run_id} or result.intent_id not in {
            None,
            intent.intent_id,
        }:
            raise ToolAuthorizationError("GRANTED_RESULT_OWNERSHIP_MISMATCH")
        return result.model_copy(
            update={"run_id": intent.bindings.run_id, "intent_id": intent.intent_id}
        )

    def observe_granted_action(self, intent: GrantedActionIntent) -> GrantedActionObservation:
        if canonical_action_json(intent.action) != intent.normalized_action_json:
            raise ToolAuthorizationError("GRANTED_INTENT_MISMATCH")
        if self._granted_workspace is None:
            raise ToolAuthorizationError("GRANTED_WORKSPACE_NOT_CONFIGURED")
        return self._granted_workspace.observe(intent.action, intent.expected_pre_state)

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

    def _patch(self, intent: ToolIntent) -> ToolResult:
        action = intent.action
        assert isinstance(action, PatchAction)
        lease = self._workspace_lease
        if (
            lease is None
            or self._patch_executor is None
            or lease.state != "ACTIVE"
            or lease.run_id != intent.run_id
            or lease.task_id != intent.task_id
            or lease.attempt_id != intent.attempt_id
        ):
            return self._denied(intent, "LEASE_SCOPE_DENIED")
        try:
            path = CanonicalPath.parse(action.path)
        except ValueError:
            return self._denied(intent, "LEASE_SCOPE_DENIED")
        if self._policy.classify(action) != "ALLOW" or not any(
            pattern.matches(path) for pattern in lease.write_globs
        ):
            return self._denied(intent, "LEASE_SCOPE_DENIED")
        try:
            result = self._patch_executor.apply_patch(
                lease, {str(path): action.unified_diff.encode("utf-8")}
            )
        except PatchExecutionUncertain:
            return self._uncertain_patch_result(intent)
        if result.code == "PATCH_RESULT_UNCERTAIN":
            return self._uncertain_patch_result(intent)
        if result.code != "PATCH_APPLIED":
            denial: ToolDenialCode = (
                "SECRET_PATH_DENIED"
                if result.code == "SECRET_PATH_DENIED"
                else "LEASE_SCOPE_DENIED"
            )
            return self._denied(intent, denial)
        return ToolResult(
            code="PATCH_APPLIED",
            run_id=intent.run_id,
            intent_id=intent.intent_id,
            bounded_payload={
                "post_tree_digest": result.post_tree_digest,
                "snapshot_digest": intent.snapshot_digest,
            },
            content_digest=result.post_tree_digest,
        )

    @staticmethod
    def _uncertain_patch_result(intent: ToolIntent) -> ToolResult:
        return ToolResult(
            code="INFRASTRUCTURE_UNCERTAINTY",
            run_id=intent.run_id,
            intent_id=intent.intent_id,
            timed_out=True,
            bounded_payload={
                "reason": "PATCH_RESULT_UNCERTAIN",
                "snapshot_digest": intent.snapshot_digest,
            },
        )

    def _check(self, intent: ToolIntent) -> ToolResult:
        action = intent.action
        assert isinstance(action, CheckAction)
        if (
            self._executor is None
            or self._declared_checks is None
            or self._sanitized_snapshot is None
            or self._deadline_journal is None
            or self._deadline_authority is None
        ):
            return self._denied(intent, "LEASE_SCOPE_DENIED")
        definition = self._declared_checks.get(action.check_id)
        if definition is None:
            return self._denied(intent, "SCOPE_DENIED")
        snapshot = self._sanitized_snapshot
        allowed_snapshot_globs = definition.input_globs
        if self._workspace_lease is not None:
            allowed_snapshot_globs += self._workspace_lease.write_globs
        if (
            snapshot.repository_id != intent.repository_id
            or snapshot.tree_digest != self._materialized_snapshot_digest
            or any(
                self._policy.classify(ReadAction(path=entry.path)) != "ALLOW"
                or not any(
                    pattern.matches(CanonicalPath.parse(entry.path))
                    for pattern in allowed_snapshot_globs
                )
                for entry in snapshot.entries
            )
        ):
            return self._denied(intent, "SCOPE_DENIED")
        deadline = self._deadline_journal.action_deadline(intent.intent_id)
        if (
            deadline is None
            or deadline.run_id != intent.run_id
            or deadline.applicable_revision_digests != intent.applicable_revision_digests
            or deadline.action_class != ActionClass.DECLARED_CHECK
            or deadline.check_id != action.check_id
            or deadline.snapshot_digest != intent.snapshot_digest
            or deadline.snapshot_digest != self._snapshot_digest
        ):
            raise ToolValidationError("CURRENT_CHECK_DEADLINE_REQUIRED")
        timeout_seconds = int((deadline.expires_at - deadline.started_at).total_seconds())
        execution = self._executor.run(definition.argv, snapshot, timeout_seconds)
        if not execution.timed_out:
            return execution.to_tool_result(intent)
        if self._deadline_authority.deadline_state(deadline) != "TIMED_OUT":
            raise ToolValidationError("EXECUTOR_TIMEOUT_BEFORE_TRUSTED_DEADLINE")
        decision = self._deadline_authority.settle_timeout(
            deadline,
            outcome_observable=False,
            expected_sequence=self._deadline_journal.audit_sequence(intent.run_id),
        )
        return ToolResult.from_timeout(deadline, decision)

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
        bounded_payload = (
            {"snapshot_digest": intent.snapshot_digest}
            if isinstance(intent.action, CheckAction)
            else {}
        )
        return ToolResult(
            code=code,
            run_id=intent.run_id,
            intent_id=intent.intent_id,
            bounded_payload=bounded_payload,
        )
