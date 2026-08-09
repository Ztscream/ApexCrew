"""Production application composition root."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from collections.abc import Callable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Literal, cast

from apexcrew.adapters.credentials.keyring import (
    KeyringSecretPolicyStore,
    SecretPolicyConfigurationError,
)
from apexcrew.adapters.credentials.model_key import ModelCredentialPort
from apexcrew.adapters.executor.attempt_patch import AttemptPatchExecutor
from apexcrew.adapters.executor.restricted import RestrictedDockerExecutor
from apexcrew.adapters.model.deepseek_responses import ClientFactory
from apexcrew.adapters.model.factory import build_model_port
from apexcrew.adapters.model.scripted import ScriptedMockLLM
from apexcrew.adapters.repository.attempt_workspace import (
    AttemptWorkspaceAdapter,
    AttemptWorkspaceError,
    MaterializedWorkspace,
)
from apexcrew.adapters.repository.bootstrap import (
    RepositoryBootstrapAuthorityService as RepositoryBootstrapAdapter,
)
from apexcrew.adapters.repository.bootstrap import (
    repository_binding,
)
from apexcrew.adapters.repository.detached_workspace import DetachedWorkspace
from apexcrew.adapters.repository.git import (
    GitCommandRunner,
    GitPrivateRefStartGuard,
    GitRepositoryPreflight,
    GitTargetReservationRepository,
    NoFollowTargetReservationWorktreeGuard,
    RepositoryInstance,
)
from apexcrew.adapters.repository.granted_workspace import GrantedWorkspaceAdapter
from apexcrew.adapters.repository.no_follow import (
    OpenedNode,
    RepositoryUnsafeError,
    StableHandleTree,
)
from apexcrew.adapters.repository.no_follow_posix import PosixNoFollowBackend
from apexcrew.adapters.repository.no_follow_windows import WindowsNoFollowBackend
from apexcrew.adapters.repository.planning import GitPlanningSnapshotReader
from apexcrew.adapters.repository.snapshot import FilesystemRepositorySnapshot
from apexcrew.adapters.repository.target_cas import GitTargetCasAdapter
from apexcrew.adapters.state.sqlite import SqliteStateStore
from apexcrew.adapters.system import ReservationPathInspector, SystemMonotonicClock
from apexcrew.application.configuration import RevisionDocuments, default_revision_documents
from apexcrew.application.control import (
    ControlCommandService,
    CrewControlService,
    TargetAuthorityDigestService,
)
from apexcrew.application.control import (
    RepositoryBootstrapAuthorityService as RepositoryBootstrapPort,
)
from apexcrew.application.queries import RunQueryService
from apexcrew.application.runtime import (
    FileRunOwnership,
    GrantedActionResolutionObserver,
    GrantedActionRuntime,
    LocalFileLockBackend,
    ModelResolutionObserver,
    PrivateRefInitializer,
    ProcessRuntimeOwnerIds,
    RecoveredActionRouter,
    ResolutionObservationRegistry,
    ResolutionRuntime,
    RuntimePhaseDriverService,
    RuntimeService,
    SnapshotResolutionObserver,
    TargetReservationDriver,
    TerminalCleanupRuntime,
    ToolActionResolutionObserver,
)
from apexcrew.domain.actions import CheckAction, PatchAction, RiskyAction, ToolActionEnvelope
from apexcrew.domain.admission import (
    RefCasIntent,
    RefEffectBinding,
    RuntimeStartBinding,
    StartGuardDecision,
    TargetCasIntent,
    TargetReservationAdmissionService,
    TargetReservationBootstrapAdmissionService,
    TargetReservationCreationIntent,
    TargetReservationObservationService,
)
from apexcrew.domain.authority import (
    NO_PROGRESS,
    AuthorityService,
    GrantedActionIntent,
    SystemUtcClock,
    TaskAuthority,
    WorkspaceLease,
)
from apexcrew.domain.commands import ApplicableRevisionDigests, RuntimeDecision, RuntimePermit
from apexcrew.domain.coordination import (
    AuthorityModelClient,
    BoundedPlanningContextBuilder,
    BoundedPlanningReadGateway,
    CoordinatorService,
    PlanningActionApplier,
    PlanningAuthorization,
    PlanningManifest,
    PlanningReadGateway,
    PlanningReadIntent,
    PlanningReadResult,
    PlanningTurnBinding,
    TaskDispatchSelection,
    planning_snapshot_digest,
)
from apexcrew.domain.effects import (
    EffectIntent,
    RecoveryActionClass,
    RecoveryObservation,
    RecoveryService,
    RunRecord,
    StateConflict,
    TargetReservation,
    canonical_json,
    sha256_digest,
)
from apexcrew.domain.evidence import ContextCapsule
from apexcrew.domain.model import DurableModelClient, ModelRequest, RecoveredModelAction
from apexcrew.domain.plan import CanonicalPath, CheckDefinition, GlobPattern, TaskContract
from apexcrew.domain.policy import PlanningPathPolicy, SecretPathPolicy
from apexcrew.domain.projection import ProjectionService
from apexcrew.domain.reservation_cleanup import CleanupObservation, CleanupObservationKind
from apexcrew.domain.revisions import (
    BudgetRevisionDocument,
    ModelConfigurationRevisionDocument,
    PlanningReadAuthorizationDocument,
    PolicyRevisionDocument,
    Sha256DigestText,
)
from apexcrew.domain.tools import (
    ActionPreState,
    DeclaredCheckRegistry,
    ExecutorPort,
    GrantedActionObservation,
    GrantedWorkspacePort,
    PatchExecutionResult,
    SanitizedSnapshot,
    SanitizedSnapshotEntry,
    ScopedToolRuntime,
    ToolIntent,
    ToolResult,
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
from apexcrew.domain.worker import WorkerActionCodec, WorkerLoopService, WorkerTurnBinding


class _TargetAuthority(TargetAuthorityDigestService):
    def __init__(self, store: SqliteStateStore) -> None:
        self._store = store

    def current_for(self, run_id: RunId) -> Sha256DigestText:
        return self._store.target_authority_digest(run_id)


def _check_aliases(task_id: TaskId, ordinal: int) -> tuple[str, ...]:
    canonical = check_id_for(task_id, ordinal)
    number = ordinal + 1
    return (
        canonical,
        f"{task_id}-check-{number}",
        f"check-{number}",
        f"task-check-{number}",
    )


def check_id_for(task_id: TaskId | str, ordinal: int) -> str:
    if ordinal < 0:
        raise ValueError("CHECK_ORDINAL_INVALID")
    return f"{task_id}:check-{ordinal + 1}"


def _declared_check_definitions(
    task_id: TaskId, checks: tuple[CheckDefinition, ...]
) -> dict[str, CheckDefinition]:
    definitions: dict[str, CheckDefinition] = {}
    for ordinal, definition in enumerate(checks):
        for alias in _check_aliases(task_id, ordinal):
            definitions.setdefault(alias, definition)
    return definitions


MAX_WORKER_CONTEXT_FILE_BYTES = 131_072
MAX_WORKER_CONTEXT_BYTES = 512 * 1024
_CONTEXT_PATH_FRAGMENT = re.compile(
    r"(?<![A-Za-z0-9_.-])[A-Za-z0-9_.~+*-]+(?:/[A-Za-z0-9_.~+*-]+)+"
)
_CONTEXT_BARE_FRAGMENT = re.compile(r"[A-Za-z0-9_.~+*-]+")


def _context_token_candidates(token: str) -> list[str]:
    normalized = token.replace("\\", "/")
    candidates = [normalized, *re.split(r"[=,:]", normalized)]
    candidates.extend(match.group(0) for match in _CONTEXT_PATH_FRAGMENT.finditer(normalized))
    candidates.extend(match.group(0) for match in _CONTEXT_BARE_FRAGMENT.finditer(normalized))
    expanded: list[str] = []
    for candidate in candidates:
        parts = candidate.split("/")
        expanded.extend("/".join(parts[index:]) for index in range(len(parts)))
        trimmed = candidate.rstrip(".,;:!?)]}")
        if trimmed != candidate:
            trimmed_parts = trimmed.split("/")
            expanded.extend("/".join(trimmed_parts[index:]) for index in range(len(trimmed_parts)))
    return expanded


def _contains_secret_context_token(token: str, secret_policy: SecretPathPolicy) -> bool:
    for candidate in _context_token_candidates(token):
        candidate = candidate.strip("'\"`()[]{}<>,:;").lstrip("/")
        if not candidate:
            continue
        try:
            path = CanonicalPath.parse(candidate)
        except ValueError:
            try:
                selector = GlobPattern.parse(candidate)
            except ValueError:
                continue
            if secret_policy.inspect_selector(selector).code != "ALLOW":
                return True
        else:
            if secret_policy.inspect(path).code != "ALLOW":
                return True
    return False


def _redact_secret_context_argv(
    argv: tuple[str, ...], secret_policy: SecretPathPolicy
) -> list[str]:
    return [
        "[redacted]" if _contains_secret_context_token(token, secret_policy) else token
        for token in argv
    ]


def _redact_secret_context_text(value: str, secret_policy: SecretPathPolicy) -> str:
    lines: list[str] = []
    for line in value.splitlines(keepends=True):
        if _contains_secret_context_token(line, secret_policy):
            ending = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
            lines.append("[redacted]" + ending)
        else:
            lines.append(line)
    return "".join(lines)


class _CompositionWorkerContext:
    def __init__(
        self,
        store: SqliteStateStore,
        resources: _CompositionRepositoryResources,
        secret_policy: SecretPathPolicy,
        *,
        max_file_bytes: int = MAX_WORKER_CONTEXT_FILE_BYTES,
        max_context_bytes: int = MAX_WORKER_CONTEXT_BYTES,
    ) -> None:
        if max_file_bytes <= 0 or max_context_bytes <= 0:
            raise ValueError("WORKER_CONTEXT_LIMIT_INVALID")
        self._store = store
        self._resources = resources
        self._secret_policy = secret_policy
        self._max_file_bytes = max_file_bytes
        self._max_context_bytes = max_context_bytes

    def build_current(self, attempt_id: AttemptId) -> ContextCapsule:
        binding = self._store.current_worker_turn_binding(attempt_id)
        contract = next(
            (
                item
                for item in self._store.task_contracts(binding.plan_digest)
                if item.task_id == str(binding.task_id)
            ),
            None,
        )
        if contract is None:
            raise RuntimeError("WORKER_TASK_CONTRACT_NOT_FOUND")
        bootstrap = self._store.bootstrap_inputs(binding.run_id)
        run = self._store.run_record(binding.run_id)
        adapter = self._resources.attempt_workspace_adapter(
            RepositoryId(binding.repository_id),
            run.repository_instance_digest,
            self._secret_policy,
        )
        context_read_globs = self._allowed_context_globs(contract.read_globs)
        context_dependency_globs = self._allowed_context_globs(contract.dependency_globs)
        context_write_globs = self._allowed_context_globs(contract.write_globs)
        workspace = adapter.materialize_context(
            attempt_id=attempt_id,
            base_oid=GitOid(binding.admissible_head),
            read_globs=context_read_globs,
            dependency_globs=context_dependency_globs,
        )
        files, dependencies, truncated, reasons = self._read_files(
            workspace, (*context_read_globs, *context_dependency_globs)
        )
        checks = [
            {
                "argv": _redact_secret_context_argv(definition.argv, self._secret_policy),
                "check_id": _check_aliases(binding.task_id, ordinal)[0],
            }
            for ordinal, definition in enumerate(contract.checks)
        ]
        content = canonical_json(
            {
                "acceptance_criteria": [
                    _redact_secret_context_text(item, self._secret_policy)
                    for item in bootstrap.acceptance_criteria
                ],
                "checks": checks,
                "constraints": [
                    _redact_secret_context_text(item, self._secret_policy)
                    for item in bootstrap.constraints
                ],
                "context_tree_digest": str(workspace.tree_digest),
                "dependency_fingerprint_basis": str(binding.dependency_fingerprint_basis),
                "files": files,
                "goal": _redact_secret_context_text(bootstrap.goal, self._secret_policy),
                "task_contract": {
                    "constraints": [
                        _redact_secret_context_text(item, self._secret_policy)
                        for item in contract.constraints
                    ],
                    "dependency_globs": [item.value for item in context_dependency_globs],
                    "dependency_task_ids": [str(item) for item in contract.dependency_task_ids],
                    "read_globs": [item.value for item in context_read_globs],
                    "task_id": str(contract.task_id),
                    "write_globs": [item.value for item in context_write_globs],
                },
                "task_id": str(binding.task_id),
                "truncated": truncated,
                "truncation": (
                    {"marker": "CONTEXT_TRUNCATED", "reasons": reasons} if truncated else None
                ),
            }
        )
        return ContextCapsule.create(
            run_id=str(binding.run_id),
            task_id=str(binding.task_id),
            revision_digest=str(binding.plan_digest),
            dependencies=(str(binding.dependency_fingerprint_basis), *dependencies),
            content=content,
        )

    def _read_files(
        self, workspace: MaterializedWorkspace, allowed_globs: tuple[GlobPattern, ...]
    ) -> tuple[list[dict[str, object]], tuple[str, ...], bool, list[str]]:
        backend = PosixNoFollowBackend() if os.name == "posix" else WindowsNoFollowBackend()
        try:
            tree = StableHandleTree(workspace.root, backend)
        except (OSError, ValueError) as error:
            raise RuntimeError("WORKER_CONTEXT_READ_DENIED") from error
        files: list[dict[str, object]] = []
        dependencies: list[str] = []
        reasons: list[str] = []
        total_bytes = 0
        try:
            for entry in workspace.entries:
                path = CanonicalPath.parse(entry.path)
                if not any(pattern.matches(path) for pattern in allowed_globs):
                    continue
                if self._secret_policy.inspect(path).code != "ALLOW":
                    continue
                try:
                    node = tree.open(str(path), "file")
                    raw, file_truncated = self._read_bounded(tree, node)
                except (OSError, ValueError, RepositoryUnsafeError) as error:
                    raise RuntimeError("WORKER_CONTEXT_READ_DENIED") from error
                if file_truncated:
                    reasons.append("FILE_LIMIT")
                    raw = raw[: self._max_file_bytes]
                remaining = self._max_context_bytes - total_bytes
                if remaining < len(raw):
                    raw = raw[: max(0, remaining)]
                    file_truncated = True
                    reasons.append("AGGREGATE_LIMIT")
                total_bytes += len(raw)
                dependency_digest = self._observed_dependency_digest(
                    path, entry.content_digest, raw, file_truncated
                )
                dependencies.append(str(dependency_digest))
                files.append(
                    {
                        "content": _redact_secret_context_text(
                            raw.decode("utf-8", errors="replace"), self._secret_policy
                        ),
                        "dependency_digest": str(dependency_digest),
                        "path": str(path),
                        "truncated": file_truncated,
                    }
                )
            try:
                tree.assert_name_bindings()
            except (OSError, RepositoryUnsafeError) as error:
                raise RuntimeError("WORKER_CONTEXT_READ_DENIED") from error
        finally:
            tree.close()
        return files, tuple(dependencies), bool(reasons), sorted(set(reasons))

    def _allowed_context_globs(self, patterns: tuple[GlobPattern, ...]) -> tuple[GlobPattern, ...]:
        return tuple(
            pattern
            for pattern in patterns
            if self._secret_policy.inspect_selector(pattern).code == "ALLOW"
        )

    @staticmethod
    def _observed_dependency_digest(
        path: CanonicalPath,
        materialized_digest: Sha256DigestText,
        raw: bytes,
        truncated: bool,
    ) -> Sha256DigestText:
        return sha256_digest(
            canonical_json(
                {
                    "materialized_digest": str(materialized_digest),
                    "observed_digest": "sha256:" + hashlib.sha256(raw).hexdigest(),
                    "observed_bytes": len(raw),
                    "path": str(path),
                    "truncated": truncated,
                }
            )
        )

    def _read_bounded(self, tree: StableHandleTree, node: OpenedNode) -> tuple[bytes, bool]:
        raw = tree.read_prefix(node, self._max_file_bytes + 1)
        return raw[: self._max_file_bytes], len(raw) > self._max_file_bytes


class _CompositionWorkerRequests:
    def __init__(
        self,
        store: SqliteStateStore,
        model_configuration: ModelConfigurationRevisionDocument,
        budget: BudgetRevisionDocument,
    ) -> None:
        self._store = store
        self.requested_model_id = model_configuration.requested_model_id
        self.allowed_model_ids = frozenset(
            alias.returned_model_id for alias in model_configuration.returned_model_aliases
        )
        self.max_input_tokens = model_configuration.inference_settings.max_input_tokens
        self.max_output_tokens = model_configuration.inference_settings.max_output_tokens
        self.reserved_cost_usd = _worst_case_reservation(model_configuration, budget)

    def for_attempt(self, attempt_id: AttemptId, capsule: object) -> ModelRequest:
        binding = self._store.current_worker_turn_binding(attempt_id)
        model_configuration = cast(
            ModelConfigurationRevisionDocument,
            self._store.current_revision_document(binding.run_id, "MODEL_CONFIGURATION"),
        )
        budget = cast(
            BudgetRevisionDocument,
            self._store.current_revision_document(binding.run_id, "BUDGET"),
        )
        allowed_model_ids = frozenset(
            alias.returned_model_id for alias in model_configuration.returned_model_aliases
        )
        content = capsule.content if isinstance(capsule, ContextCapsule) else str(capsule)
        digest = sha256_digest(canonical_json({"attempt_id": str(attempt_id), "content": content}))
        return ModelRequest(
            run_id=binding.run_id,
            plan_digest=RevisionDigest(str(binding.plan_digest)),
            policy_digest=RevisionDigest(str(binding.policy_digest)),
            budget_digest=RevisionDigest(str(binding.budget_digest)),
            model_configuration_digest=RevisionDigest(str(binding.model_configuration_digest)),
            requested_model_id=model_configuration.requested_model_id,
            allowed_model_ids=allowed_model_ids,
            prompt=({"role": "user", "content": content},),
            tool_schema_digest=str(binding.tool_schema_digest),
            request_digest=digest,
            idempotency_key=f"worker-request:{attempt_id}:{digest}",
            max_input_tokens=model_configuration.inference_settings.max_input_tokens,
            max_output_tokens=model_configuration.inference_settings.max_output_tokens,
            reserved_cost_usd=_worst_case_reservation(model_configuration, budget),
            temperature=model_configuration.inference_settings.temperature,
            reasoning_effort=model_configuration.inference_settings.reasoning_effort,
            owner_kind="WORKER",
            task_id=binding.task_id,
            attempt_id=binding.attempt_id,
            tranche_id=binding.tranche_id,
        )


class _CompositionPlanningPathGate:
    def __init__(self, policy: PlanningPathPolicy) -> None:
        self._policy = policy

    def require_allowed(self, path: CanonicalPath, authorization: object) -> None:
        self._policy.require_allowed(path, cast(PlanningReadAuthorizationDocument, authorization))

    def require_manifest_allowed(self, path: CanonicalPath, binding: PlanningTurnBinding) -> None:
        self._policy.require_manifest_allowed(path, binding)


class _CompositionPlanningRevisionContext:
    """Carries the planning run identity across the existing manifest port."""

    def __init__(self, store: SqliteStateStore) -> None:
        self._store = store
        self._run_id: ContextVar[RunId | None] = ContextVar("planning_run_id", default=None)

    def bind(self, run_id: RunId) -> None:
        self._run_id.set(run_id)

    def run(self) -> RunRecord:
        run_id = self._run_id.get()
        if run_id is None:
            raise RuntimeError("PLANNING_RUN_NOT_BOUND")
        return self._store.run_record(run_id)

    def policy(self) -> PlanningReadAuthorizationDocument:
        policy = self._store.current_revision_document(self.run().run_id, "POLICY")
        return cast(PolicyRevisionDocument, policy).planning_read_authorization


class _CompositionRepositoryResources:
    """Lazily owns one preflight-bound repository and its internal Git adapters."""

    def __init__(
        self,
        root: Path,
        authority: RepositoryBootstrapPort,
        data_root: Path,
    ) -> None:
        self._root = root
        self._authority = authority
        self._data_root = data_root
        self._repository: RepositoryInstance | None = None
        self._runner: GitCommandRunner | None = None
        self._owns_runner = False
        self._data_handles: StableHandleTree | None = None
        self._attempt_adapter: AttemptWorkspaceAdapter | None = None
        self._validated_binding: tuple[RepositoryId, Sha256DigestText] | None = None
        self._allowed_worktree_admin_entries: set[str] = set()

    def _allow_reservation_worktree(self, reservation_id: str) -> None:
        self._allowed_worktree_admin_entries.add(reservation_id)

    def _ensure(self) -> tuple[RepositoryInstance, GitCommandRunner, StableHandleTree]:
        if self._repository is None:
            if isinstance(self._authority, RepositoryBootstrapAdapter):
                self._repository = self._authority.open_repository(
                    self._root,
                    allowed_worktree_admin_entries=tuple(
                        sorted(self._allowed_worktree_admin_entries)
                    ),
                )
                self._runner = self._authority.git_runner
            else:
                self._repository = GitRepositoryPreflight().inspect(
                    self._root,
                    allowed_worktree_admin_entries=tuple(
                        sorted(self._allowed_worktree_admin_entries)
                    ),
                )
                executable = shutil.which("git")
                if executable is None:
                    self._repository.close()
                    self._repository = None
                    raise RuntimeError("GIT_EXECUTABLE_UNAVAILABLE")
                self._runner = GitCommandRunner(Path(executable).resolve())
                self._owns_runner = True
        if self._runner is None or self._repository is None:
            raise RuntimeError("REPOSITORY_RUNTIME_NOT_INITIALIZED")
        if self._data_handles is None:
            self._data_root.mkdir(parents=True, exist_ok=True)
            (self._data_root / "reservations").mkdir(exist_ok=True)
            backend = WindowsNoFollowBackend() if os.name == "nt" else PosixNoFollowBackend()
            self._data_handles = StableHandleTree(self._data_root, backend)
        return self._repository, self._runner, self._data_handles

    def validate_repository_binding(
        self,
        repository_id: RepositoryId,
        repository_instance_digest: Sha256DigestText,
    ) -> None:
        repository, _, _ = self._ensure()
        if self._validated_binding is not None:
            repository.assert_stable()
            if self._validated_binding != (repository_id, repository_instance_digest):
                raise RuntimeError("REPOSITORY_BINDING_MISMATCH")
            return
        actual_repository_id, actual_instance_digest = repository_binding(
            repository,
            allowed_worktree_admin_entries=tuple(sorted(self._allowed_worktree_admin_entries)),
        )
        if (
            actual_repository_id != repository_id
            or actual_instance_digest != repository_instance_digest
        ):
            repository.close()
            self._repository = None
            raise RuntimeError("REPOSITORY_BINDING_MISMATCH")
        self._validated_binding = (repository_id, repository_instance_digest)

    def planning_reader(
        self,
        repository_id: RepositoryId,
        repository_instance_digest: Sha256DigestText,
        policy: PlanningPathPolicy,
    ) -> GitPlanningSnapshotReader:
        self.validate_repository_binding(repository_id, repository_instance_digest)
        repository, runner, _ = self._ensure()
        return GitPlanningSnapshotReader(
            repository,
            repository_id,
            runner,
            _CompositionPlanningPathGate(policy),
        )

    def worker_workspace(
        self,
        reservation: TargetReservation,
        repository_id: RepositoryId,
        repository_instance_digest: Sha256DigestText,
        tree_oid: GitOid,
        secret_policy: SecretPathPolicy,
    ) -> DetachedWorkspace:
        self.validate_repository_binding(repository_id, repository_instance_digest)
        repository, runner, _ = self._ensure()
        workspace_root = self._data_root / "workspaces" / reservation.reservation_id
        workspace = DetachedWorkspace(repository, runner, workspace_root, secret_policy)
        workspace.ensure_materialized(tree_oid)
        return workspace

    def attempt_workspace_adapter(
        self,
        repository_id: RepositoryId,
        repository_instance_digest: Sha256DigestText,
        secret_policy: SecretPathPolicy,
    ) -> AttemptWorkspaceAdapter:
        self.validate_repository_binding(repository_id, repository_instance_digest)
        repository, runner, _ = self._ensure()
        if self._attempt_adapter is None:
            self._attempt_adapter = AttemptWorkspaceAdapter(
                repository, runner, self._data_root, secret_policy
            )
        return self._attempt_adapter

    def refresh(self) -> None:
        if self._repository is None:
            return
        self._attempt_adapter = None
        self._repository = self._repository.refresh_after_verified_owned_transition()

    def reservation_adapters(
        self,
        reservation: TargetReservation,
        repository_id: RepositoryId,
        repository_instance_digest: Sha256DigestText,
        target_authority_digest: Sha256DigestText,
    ) -> tuple[
        TargetReservationObservationService,
        GitTargetReservationRepository,
        ReservationPathInspector,
    ]:
        self._allow_reservation_worktree(reservation.reservation_id)
        self.validate_repository_binding(repository_id, repository_instance_digest)
        repository, runner, data_handles = self._ensure()
        guard = NoFollowTargetReservationWorktreeGuard(
            repository,
            self._data_root,
            data_handles,
            repository.root,
        )
        git = GitTargetReservationRepository(
            repository=repository,
            repository_id=repository_id,
            repository_instance_digest=repository_instance_digest,
            runner=runner,
            worktree_guard=guard,
            data_root=self._data_root,
            target_authority_digest=target_authority_digest,
        )
        backend = WindowsNoFollowBackend() if os.name == "nt" else PosixNoFollowBackend()
        path_reader = ReservationPathInspector(
            self._data_root,
            lambda item: (
                b"gitdir: "
                + os.fsencode(
                    (repository.root / ".git" / "worktrees" / item.reservation_id).as_posix()
                )
                + b"\n"
            ),
            backend,
        )
        return TargetReservationObservationService(git, path_reader), git, path_reader

    def private_ref_guard(
        self,
        *,
        reservation: TargetReservation,
        repository_id: RepositoryId,
        repository_instance_digest: Sha256DigestText,
        target_safety_digest: Sha256DigestText,
    ) -> GitPrivateRefStartGuard:
        self._allow_reservation_worktree(reservation.reservation_id)
        repository, runner, _ = self._ensure()
        observer, git, _path_reader = self.reservation_adapters(
            reservation,
            repository_id,
            repository_instance_digest,
            target_safety_digest,
        )
        del git
        return GitPrivateRefStartGuard(
            repository=repository,
            repository_id=repository_id,
            repository_instance_digest=repository_instance_digest,
            runner=runner,
            reservation_observer=observer,
            reservation=reservation,
            target_safety_digest=target_safety_digest,
            reflog_message=f"apexcrew private run {reservation.run_id}",
        )

    def target_cas(
        self,
        reservation: TargetReservation,
        repository_id: RepositoryId,
        repository_instance_digest: Sha256DigestText,
    ) -> GitTargetCasAdapter:
        self.validate_repository_binding(repository_id, repository_instance_digest)
        repository, runner, data_handles = self._ensure()
        guard = NoFollowTargetReservationWorktreeGuard(
            repository,
            self._data_root,
            data_handles,
            repository.root,
        )
        return GitTargetCasAdapter(repository, runner, guard, reservation)

    def close(self) -> None:
        first_error: BaseException | None = None
        self._attempt_adapter = None
        if self._data_handles is not None:
            try:
                self._data_handles.close()
            except BaseException as error:  # noqa: BLE001
                first_error = error
            self._data_handles = None
        if self._repository is not None:
            try:
                self._repository.close()
            except BaseException as error:  # noqa: BLE001
                if first_error is None:
                    first_error = error
            self._repository = None
            self._validated_binding = None
        if self._owns_runner and self._runner is not None:
            try:
                self._runner.close()
            except BaseException as error:  # noqa: BLE001
                if first_error is None:
                    first_error = error
        self._runner = None
        if first_error is not None:
            raise first_error


@dataclass
class _CheckWorkspaceState:
    cache_key: tuple[str, ...]
    check_id: str
    workspace: MaterializedWorkspace
    patch_executor: AttemptPatchExecutor
    tree_digest: Sha256DigestText


@dataclass(frozen=True)
class _PatchMutation:
    patches: tuple[tuple[str, bytes], ...]


@dataclass(frozen=True)
class _GrantedMutation:
    action: RiskyAction
    expected: ActionPreState


@dataclass
class _AttemptWorkspaceState:
    adapter: AttemptWorkspaceAdapter
    context: MaterializedWorkspace | None = None
    check_workspaces: dict[tuple[str, ...], _CheckWorkspaceState] = field(default_factory=dict)
    mutation_history: list[_PatchMutation | _GrantedMutation] = field(default_factory=list)
    patch_sync_uncertain: bool = False


class _AttemptPatchRouter(AttemptPatchExecutor):
    """Apply a patch to the primary root and keep cached check roots aligned."""

    def __init__(
        self,
        state: _AttemptWorkspaceState,
        primary: _CheckWorkspaceState,
        secret_policy: SecretPathPolicy,
    ) -> None:
        super().__init__(primary.workspace, secret_policy)
        self._state = state
        self._primary = primary

    def apply_patch(
        self, lease: WorkspaceLease, patches: Mapping[str, bytes]
    ) -> PatchExecutionResult:
        if self._state.patch_sync_uncertain:
            return PatchExecutionResult(code="PATCH_RESULT_UNCERTAIN")
        result = self._primary.patch_executor.apply_patch(lease, patches)
        if result.code == "PATCH_RESULT_UNCERTAIN":
            self._state.patch_sync_uncertain = True
            return result
        if result.code != "PATCH_APPLIED" or result.post_tree_digest is None:
            return result

        propagated: dict[tuple[str, ...], Sha256DigestText] = {}
        for key, candidate in self._state.check_workspaces.items():
            if candidate is self._primary:
                continue
            for path, unified_diff in patches.items():
                candidate_result = candidate.patch_executor.apply_patch(lease, {path: unified_diff})
                if (
                    candidate_result.code != "PATCH_APPLIED"
                    or candidate_result.post_tree_digest is None
                ):
                    self._state.patch_sync_uncertain = True
                    return PatchExecutionResult(code="PATCH_RESULT_UNCERTAIN")
                propagated[key] = candidate_result.post_tree_digest

        self._state.mutation_history.append(_PatchMutation(tuple(patches.items())))
        self._primary.tree_digest = result.post_tree_digest
        for key, tree_digest in propagated.items():
            self._state.check_workspaces[key].tree_digest = tree_digest
        return result


class _TrackingGrantedWorkspace:
    def __init__(
        self,
        workspace: GrantedWorkspacePort,
        after_result: Callable[[RiskyAction, ActionPreState, ToolResult], ToolResult],
    ) -> None:
        self._workspace = workspace
        self._after_result = after_result

    def observe(self, action: RiskyAction, expected: ActionPreState) -> GrantedActionObservation:
        return self._workspace.observe(action, expected)

    def _record(
        self, action: RiskyAction, expected: ActionPreState, result: ToolResult
    ) -> ToolResult:
        return self._after_result(action, expected, result)

    def delete_regular_file(self, action: RiskyAction, expected: ActionPreState) -> ToolResult:
        return self._record(action, expected, self._workspace.delete_regular_file(action, expected))

    def rename_regular_file(self, action: RiskyAction, expected: ActionPreState) -> ToolResult:
        return self._record(action, expected, self._workspace.rename_regular_file(action, expected))

    def set_executable(self, action: RiskyAction, expected: ActionPreState) -> ToolResult:
        return self._record(action, expected, self._workspace.set_executable(action, expected))

    def apply_protected_patch(self, action: RiskyAction, expected: ActionPreState) -> ToolResult:
        return self._record(
            action, expected, self._workspace.apply_protected_patch(action, expected)
        )


class _CompositionWorkerTools(ScopedToolRuntime):
    def __init__(
        self,
        store: SqliteStateStore,
        resources: _CompositionRepositoryResources,
        secret_policy: SecretPathPolicy,
        authority: AuthorityService,
        executor: ExecutorPort | None = None,
    ) -> None:
        self._store = store
        self._resources = resources
        self._secret_policy = secret_policy
        self._authority = authority
        self._executor = executor
        self._attempt_workspaces: dict[tuple[str, str], _AttemptWorkspaceState] = {}
        zero = Sha256DigestText("sha256:" + "0" * 64)
        super().__init__(
            snapshot=FilesystemRepositorySnapshot(resources._root),
            read_globs=("**",),
            secret_paths=secret_policy,
            authorization_binding_digest=zero,
            applicable_revision_digests=ApplicableRevisionDigests(),
            repository_id="composition-placeholder",
            snapshot_digest=zero,
            scope_digest=zero,
            dependency_fingerprint_basis=zero,
        )
        self._executor = executor

    def _runtime(self, intent: ToolIntent | GrantedActionIntent) -> ScopedToolRuntime:
        if isinstance(intent, ToolIntent):
            attempt_id = intent.attempt_id
            authorization_binding_digest = intent.authorization_binding_digest
        else:
            attempt_id = intent.bindings.attempt_id
            authorization_binding_digest = intent.bindings.authorization_binding_digest
        if attempt_id is None:
            raise RuntimeError("WORKER_ATTEMPT_BINDING_MISSING")
        binding = self._store.current_worker_turn_binding(attempt_id)
        lease = self._store.workspace_lease(binding.run_id, binding.lease_id)
        if lease is None:
            raise RuntimeError("WORKER_LEASE_NOT_FOUND")
        self._resources.validate_repository_binding(
            RepositoryId(binding.repository_id),
            self._store.run_record(binding.run_id).repository_instance_digest,
        )
        contract = self._contract(binding)
        attempt_state = self._attempt_state(binding)
        context_workspace = self._context_workspace(attempt_state, binding, contract)
        policy = cast(
            PolicyRevisionDocument,
            self._store.current_revision_document(binding.run_id, "POLICY"),
        )
        check_definitions = _declared_check_definitions(binding.task_id, contract.checks)
        declared_checks = DeclaredCheckRegistry(check_definitions)
        check_snapshot: SanitizedSnapshot | None = None
        patch_executor: AttemptPatchExecutor | None = None
        granted_workspace: GrantedWorkspacePort | None = None
        runtime_executor: ExecutorPort | None = None
        runtime_snapshot_digest = binding.snapshot_digest
        if isinstance(intent, ToolIntent) and isinstance(intent.action, CheckAction):
            definition = declared_checks.get(intent.action.check_id)
            runtime_executor = (
                self._executor
                if self._executor is not None
                else RestrictedDockerExecutor(policy.executor_profile, self._secret_policy)
            )
            if definition is not None:
                ordinal = self._check_ordinal(binding.task_id, contract, intent.action.check_id)
                check_state = self._check_workspace(
                    attempt_state,
                    binding,
                    lease,
                    check_id=check_id_for(binding.task_id, ordinal),
                    input_globs=definition.input_globs,
                    write_globs=contract.write_globs,
                )
                check_snapshot = self._build_sanitized_snapshot(
                    root=check_state.workspace.root,
                    repository_id=binding.repository_id,
                    tree_digest=check_state.tree_digest,
                    input_globs=definition.input_globs,
                    write_globs=contract.write_globs,
                    maximum_bytes=policy.executor_profile.scratch_limit_bytes,
                )
                patch_executor = check_state.patch_executor
                runtime_snapshot_digest = check_state.tree_digest
        elif isinstance(intent, GrantedActionIntent):
            primary = self._primary_workspace(attempt_state, binding, lease, contract)
            patch_executor = primary.patch_executor
            granted_workspace = self._tracking_granted_workspace(attempt_state, primary)
        elif isinstance(intent, ToolIntent) and isinstance(intent.action, PatchAction):
            primary = self._primary_workspace(attempt_state, binding, lease, contract)
            patch_executor = _AttemptPatchRouter(attempt_state, primary, self._secret_policy)
        return ScopedToolRuntime(
            snapshot=FilesystemRepositorySnapshot(context_workspace.root),
            read_globs=tuple(
                pattern.value for pattern in (*contract.read_globs, *contract.dependency_globs)
            ),
            secret_paths=self._secret_policy,
            authorization_binding_digest=Sha256DigestText(str(authorization_binding_digest)),
            applicable_revision_digests=binding.applicable_revision_digests,
            repository_id=binding.repository_id,
            snapshot_digest=runtime_snapshot_digest,
            scope_digest=binding.scope_digest,
            dependency_fingerprint_basis=binding.dependency_fingerprint_basis,
            denial_journal=self._store,
            denial_expected_sequence=self._store.audit_sequence(binding.run_id),
            executor=(runtime_executor),
            declared_checks=declared_checks,
            sanitized_snapshot=check_snapshot,
            materialized_snapshot_digest=(
                None if check_snapshot is None else check_snapshot.tree_digest
            ),
            patch_executor=patch_executor,
            deadline_journal=self._store,
            deadline_authority=self._authority,
            workspace_lease=lease,
            granted_workspace=granted_workspace,
        )

    def capture_expected_prestate(
        self, binding: WorkerTurnBinding, action: ToolActionEnvelope
    ) -> ActionPreState:
        if not isinstance(action, RiskyAction):
            return ActionPreState()
        lease = self._store.workspace_lease(binding.run_id, binding.lease_id)
        if lease is None:
            raise RuntimeError("WORKER_LEASE_NOT_FOUND")
        contract = self._contract(binding)
        attempt_state = self._attempt_state(binding)
        primary = self._primary_workspace(attempt_state, binding, lease, contract)
        try:
            return GrantedWorkspaceAdapter(
                primary.workspace.root, self._secret_policy
            ).expected_prestate(action)
        except ValueError:
            return ActionPreState()

    def capture_snapshot_digest(
        self, binding: WorkerTurnBinding, action: ToolActionEnvelope
    ) -> Sha256DigestText:
        if not isinstance(action, CheckAction):
            return binding.snapshot_digest
        lease = self._store.workspace_lease(binding.run_id, binding.lease_id)
        if lease is None:
            raise RuntimeError("WORKER_LEASE_NOT_FOUND")
        contract = self._contract(binding)
        declared_checks = DeclaredCheckRegistry(
            _declared_check_definitions(binding.task_id, contract.checks)
        )
        definition = declared_checks.get(action.check_id)
        if definition is None:
            return binding.snapshot_digest
        ordinal = self._check_ordinal(binding.task_id, contract, action.check_id)
        state = self._attempt_state(binding)
        return self._check_workspace(
            state,
            binding,
            lease,
            check_id=check_id_for(binding.task_id, ordinal),
            input_globs=definition.input_globs,
            write_globs=contract.write_globs,
        ).tree_digest

    @staticmethod
    def _check_ordinal(task_id: TaskId, contract: TaskContract, check_id: str) -> int:
        ordinal = next(
            (
                index
                for index, _definition in enumerate(contract.checks)
                if check_id in _check_aliases(task_id, index)
            ),
            None,
        )
        if ordinal is None:
            raise RuntimeError("WORKER_CHECK_DEFINITION_NOT_BOUND")
        return ordinal

    def _contract(self, binding: WorkerTurnBinding) -> TaskContract:
        contract = next(
            (
                item
                for item in self._store.task_contracts(binding.plan_digest)
                if item.task_id == str(binding.task_id)
            ),
            None,
        )
        if contract is None:
            raise RuntimeError("WORKER_TASK_CONTRACT_NOT_FOUND")
        return contract

    def _attempt_state(self, binding: WorkerTurnBinding) -> _AttemptWorkspaceState:
        key = (str(binding.attempt_id), str(binding.admissible_head))
        state = self._attempt_workspaces.get(key)
        if state is not None:
            return state
        run = self._store.run_record(binding.run_id)
        adapter = self._resources.attempt_workspace_adapter(
            RepositoryId(binding.repository_id),
            run.repository_instance_digest,
            self._secret_policy,
        )
        state = _AttemptWorkspaceState(adapter=adapter)
        self._attempt_workspaces[key] = state
        return state

    def _context_workspace(
        self,
        state: _AttemptWorkspaceState,
        binding: WorkerTurnBinding,
        contract: TaskContract,
    ) -> MaterializedWorkspace:
        if state.context is None:
            read_globs = tuple(
                pattern
                for pattern in contract.read_globs
                if self._secret_policy.inspect_selector(pattern).code == "ALLOW"
            )
            dependency_globs = tuple(
                pattern
                for pattern in contract.dependency_globs
                if self._secret_policy.inspect_selector(pattern).code == "ALLOW"
            )
            state.context = state.adapter.materialize_context(
                attempt_id=binding.attempt_id,
                base_oid=GitOid(binding.admissible_head),
                read_globs=read_globs,
                dependency_globs=dependency_globs,
            )
        self._verify_workspace_digest(
            state.adapter, state.context, "WORKER_CONTEXT_WORKSPACE_CHANGED"
        )
        return state.context

    def _check_workspace(
        self,
        state: _AttemptWorkspaceState,
        binding: WorkerTurnBinding,
        lease: WorkspaceLease,
        *,
        check_id: str,
        input_globs: tuple[GlobPattern, ...],
        write_globs: tuple[GlobPattern, ...],
    ) -> _CheckWorkspaceState:
        cache_key = self._check_cache_key(binding, check_id, input_globs, write_globs)
        cached = state.check_workspaces.get(cache_key)
        if cached is not None:
            self._verify_check_workspace(state, cached)
            return cached
        workspace = state.adapter.materialize_check(
            attempt_id=binding.attempt_id,
            base_oid=GitOid(binding.admissible_head),
            input_globs=input_globs,
            write_globs=write_globs,
            workspace_key=self._workspace_key(cache_key),
            reject_existing=True,
        )
        candidate = _CheckWorkspaceState(
            cache_key=cache_key,
            check_id=check_id,
            workspace=workspace,
            patch_executor=AttemptPatchExecutor(workspace, self._secret_policy),
            tree_digest=workspace.tree_digest,
        )
        state.check_workspaces[cache_key] = candidate
        self._replay_mutation_history(state, candidate, lease)
        self._verify_check_workspace(state, candidate)
        return candidate

    def _primary_workspace(
        self,
        state: _AttemptWorkspaceState,
        binding: WorkerTurnBinding,
        lease: WorkspaceLease,
        contract: TaskContract,
    ) -> _CheckWorkspaceState:
        if contract.checks:
            definition = contract.checks[0]
            check_id = check_id_for(binding.task_id, 0)
            input_globs = definition.input_globs
        else:
            check_id = f"{binding.task_id}:patch-root"
            input_globs = ()
        return self._check_workspace(
            state,
            binding,
            lease,
            check_id=check_id,
            input_globs=input_globs,
            write_globs=contract.write_globs,
        )

    @staticmethod
    def _check_cache_key(
        binding: WorkerTurnBinding,
        check_id: str,
        input_globs: tuple[GlobPattern, ...],
        write_globs: tuple[GlobPattern, ...],
    ) -> tuple[str, ...]:
        input_digest = sha256_digest(
            canonical_json({"globs": [item.value for item in input_globs]})
        )
        write_digest = sha256_digest(
            canonical_json({"globs": [item.value for item in write_globs]})
        )
        return (
            str(binding.attempt_id),
            str(binding.admissible_head),
            check_id,
            str(input_digest),
            str(write_digest),
        )

    @staticmethod
    def _workspace_key(cache_key: tuple[str, ...]) -> str:
        return str(sha256_digest(canonical_json({"cache_key": list(cache_key)})))

    def _replay_mutation_history(
        self,
        state: _AttemptWorkspaceState,
        candidate: _CheckWorkspaceState,
        lease: WorkspaceLease,
    ) -> None:
        if state.patch_sync_uncertain:
            raise RuntimeError("WORKER_CHECK_WORKSPACE_SYNC_UNCERTAIN")
        for mutation in state.mutation_history:
            if isinstance(mutation, _PatchMutation):
                for path, unified_diff in mutation.patches:
                    result = candidate.patch_executor.apply_patch(lease, {path: unified_diff})
                    if result.code != "PATCH_APPLIED" or result.post_tree_digest is None:
                        state.patch_sync_uncertain = True
                        raise RuntimeError("WORKER_CHECK_WORKSPACE_SYNC_UNCERTAIN")
            elif not self._apply_granted_mutation(candidate, mutation):
                state.patch_sync_uncertain = True
                raise RuntimeError("WORKER_CHECK_WORKSPACE_SYNC_UNCERTAIN")
        if state.mutation_history:
            candidate.tree_digest = candidate.patch_executor.tree_digest()

    def _verify_check_workspace(
        self, state: _AttemptWorkspaceState, candidate: _CheckWorkspaceState
    ) -> None:
        if state.patch_sync_uncertain:
            raise RuntimeError("WORKER_CHECK_WORKSPACE_SYNC_UNCERTAIN")
        try:
            state.adapter.verify_workspace_identity(candidate.workspace)
            observed = candidate.patch_executor.tree_digest()
        except (AttemptWorkspaceError, OSError, RepositoryUnsafeError, ValueError) as error:
            raise RuntimeError("WORKER_CHECK_WORKSPACE_UNAVAILABLE") from error
        if observed != candidate.tree_digest:
            raise RuntimeError("WORKER_CHECK_WORKSPACE_CHANGED")

    def _verify_workspace_digest(
        self,
        adapter: AttemptWorkspaceAdapter,
        workspace: MaterializedWorkspace,
        reason: str,
    ) -> None:
        try:
            adapter.verify_workspace_identity(workspace)
            observed = AttemptPatchExecutor(workspace, self._secret_policy).tree_digest()
        except (AttemptWorkspaceError, OSError, RepositoryUnsafeError, ValueError) as error:
            raise RuntimeError("WORKER_WORKSPACE_UNAVAILABLE") from error
        if observed != workspace.tree_digest:
            raise RuntimeError(reason)

    def _tracking_granted_workspace(
        self,
        state: _AttemptWorkspaceState,
        primary: _CheckWorkspaceState,
    ) -> GrantedWorkspacePort:
        inner = GrantedWorkspaceAdapter(primary.workspace.root, self._secret_policy)

        def after_result(
            action: RiskyAction, expected: ActionPreState, result: ToolResult
        ) -> ToolResult:
            if result.code in {
                "DELETED",
                "RENAMED",
                "EXECUTABLE_CHANGED",
                "PROTECTED_PATCH_APPLIED",
            }:
                mutation = _GrantedMutation(action=action, expected=expected)
                try:
                    for candidate in state.check_workspaces.values():
                        if candidate is primary:
                            continue
                        if not self._apply_granted_mutation(candidate, mutation):
                            state.patch_sync_uncertain = True
                            return self._granted_sync_uncertain(result)
                        candidate.tree_digest = candidate.patch_executor.tree_digest()
                    primary.tree_digest = primary.patch_executor.tree_digest()
                except (OSError, RepositoryUnsafeError, ValueError):
                    state.patch_sync_uncertain = True
                    return self._granted_sync_uncertain(result)
                state.mutation_history.append(mutation)
            elif result.code == "INDETERMINATE":
                state.patch_sync_uncertain = True
            return result

        return _TrackingGrantedWorkspace(inner, after_result)

    @staticmethod
    def _granted_sync_uncertain(result: ToolResult) -> ToolResult:
        return ToolResult(
            code="INFRASTRUCTURE_UNCERTAINTY",
            run_id=result.run_id,
            intent_id=result.intent_id,
            timed_out=True,
            bounded_payload={"reason": "WORKER_CHECK_WORKSPACE_SYNC_UNCERTAIN"},
        )

    def _apply_granted_mutation(
        self, candidate: _CheckWorkspaceState, mutation: _GrantedMutation
    ) -> bool:
        workspace = GrantedWorkspaceAdapter(candidate.workspace.root, self._secret_policy)
        handler, expected_code = {
            "delete": (workspace.delete_regular_file, "DELETED"),
            "rename": (workspace.rename_regular_file, "RENAMED"),
            "set_executable": (workspace.set_executable, "EXECUTABLE_CHANGED"),
            "protected_patch": (
                workspace.apply_protected_patch,
                "PROTECTED_PATCH_APPLIED",
            ),
        }[mutation.action.operation]
        result = handler(mutation.action, mutation.expected)
        return result.code == expected_code

    def _build_sanitized_snapshot(
        self,
        *,
        root: Path,
        repository_id: str,
        tree_digest: Sha256DigestText,
        input_globs: tuple[GlobPattern, ...],
        write_globs: tuple[GlobPattern, ...],
        maximum_bytes: int,
    ) -> SanitizedSnapshot:
        filesystem = FilesystemRepositorySnapshot(root)
        entries: list[SanitizedSnapshotEntry] = []
        total_bytes = 0
        allowed_globs = (*input_globs, *write_globs)
        for observed in filesystem.entries():
            path = CanonicalPath.parse(observed.path)
            if not any(pattern.matches(path) for pattern in allowed_globs):
                raise RuntimeError("SANITIZED_SNAPSHOT_SCOPE_DENIED")
            if self._secret_policy.inspect(path).code != "ALLOW":
                raise RuntimeError("SANITIZED_SNAPSHOT_DENIED")
            remaining = maximum_bytes - total_bytes
            if remaining < 0:
                raise RuntimeError("SANITIZED_SNAPSHOT_TOO_LARGE")
            content = filesystem.read(path, remaining + 1)
            if len(content) > remaining:
                raise RuntimeError("SANITIZED_SNAPSHOT_TOO_LARGE")
            entries.append(
                SanitizedSnapshotEntry(
                    path=str(path),
                    kind="regular",
                    content_digest=Sha256DigestText(
                        "sha256:" + hashlib.sha256(content).hexdigest()
                    ),
                )
            )
            total_bytes += len(content)
        observed_tree_digest = sha256_digest(
            canonical_json({entry.path: str(entry.content_digest) for entry in entries})
        )
        if observed_tree_digest != tree_digest:
            raise RuntimeError("SANITIZED_SNAPSHOT_WORKSPACE_CHANGED")
        dependency_fingerprint_digest = sha256_digest(
            canonical_json(
                {
                    "entries": [
                        {"path": entry.path, "content_digest": str(entry.content_digest)}
                        for entry in entries
                    ],
                    "tree_digest": str(tree_digest),
                }
            )
        )
        return SanitizedSnapshot.from_regular_files(
            root=root,
            repository_id=repository_id,
            tree_digest=tree_digest,
            dependency_fingerprint_digest=dependency_fingerprint_digest,
            entries=entries,
            secret_paths=self._secret_policy,
        )

    def execute(self, intent: ToolIntent) -> ToolResult:
        return self._runtime(intent).execute(intent)

    def observe_recovery(
        self, intent: ToolIntent
    ) -> tuple[
        Literal["EXACT_POST", "EXACT_PRE", "EXACT_RECEIPT", "THIRD_STATE", "UNAVAILABLE"],
        ToolResult | None,
    ]:
        return self._runtime(intent).observe_recovery(intent)

    def observe_granted_action(self, intent: GrantedActionIntent) -> GrantedActionObservation:
        return self._runtime(intent).observe_granted_action(intent)

    def execute_granted(self, intent: GrantedActionIntent) -> ToolResult:
        return self._runtime(intent).execute_granted(intent)


class _CompositionPlanningAuthorization:
    def __init__(
        self,
        store: SqliteStateStore,
        secret_policy: SecretPathPolicy | None = None,
        revisions: _CompositionPlanningRevisionContext | None = None,
    ) -> None:
        self._store = store
        self._provided_secret_policy = secret_policy
        self._revisions = (
            _CompositionPlanningRevisionContext(store) if revisions is None else revisions
        )

    @staticmethod
    def _secret_policy() -> SecretPathPolicy:
        try:
            key, rules = KeyringSecretPolicyStore().load()
        except SecretPolicyConfigurationError as error:
            raise RuntimeError(str(error)) from error
        return SecretPathPolicy.from_host_rules(rules, key)

    def _effective_secret_policy(self) -> SecretPathPolicy:
        return (
            self._provided_secret_policy
            if self._provided_secret_policy is not None
            else self._secret_policy()
        )

    def current(self, run_id: RunId) -> PlanningAuthorization:
        return self._build(
            run_id, planning_request_count=self._store.planning_request_count(run_id)
        )

    def current_for_recovery(
        self, run_id: RunId, action: RecoveredModelAction
    ) -> PlanningAuthorization:
        del action
        return self._build(
            run_id,
            planning_request_count=max(0, self._store.planning_request_count(run_id) - 1),
        )

    def _build(self, run_id: RunId, planning_request_count: int) -> PlanningAuthorization:
        self._revisions.bind(run_id)
        run = self._store.run_record(run_id)
        reservation = self._store.target_reservation_for_run(run_id)
        current = self._store.current_revision_digests(run_id)
        approved = set(self._store.approved_revision_classes(run_id))
        required = {"POLICY", "BUDGET", "MODEL_CONFIGURATION"}
        if (
            not required.issubset(approved)
            or current.policy_digest is None
            or current.budget_digest is None
            or current.model_configuration_digest is None
        ):
            return PlanningAuthorization(
                run_id=run_id,
                decision="PAUSE",
                reason="PLANNING_REVISIONS_NOT_APPROVED",
                applicable_revision_digests=current,
                target_safety_digest=self._store.target_authority_digest(run_id),
                credential_profile=None,
                read_authorization=None,
                turn_binding=None,
                planning_request_count=planning_request_count,
                planning_request_ceiling=8,
            )
        try:
            secret_policy = self._effective_secret_policy()
        except RuntimeError:
            return PlanningAuthorization(
                run_id=run_id,
                decision="PAUSE",
                reason="SECRET_POLICY_CONFIGURATION_MISSING",
                applicable_revision_digests=current,
                target_safety_digest=self._store.target_authority_digest(run_id),
                credential_profile=None,
                read_authorization=None,
                turn_binding=None,
                planning_request_count=planning_request_count,
                planning_request_ceiling=8,
            )
        policy = self._store.current_revision_document(run_id, "POLICY")
        budget = self._store.current_revision_document(run_id, "BUDGET")
        model = self._store.current_revision_document(run_id, "MODEL_CONFIGURATION")
        assert hasattr(policy, "planning_read_authorization")
        assert hasattr(budget, "planning_request_ceiling")
        assert hasattr(model, "provider")
        read_authorization = policy.planning_read_authorization
        scope_digest = Sha256DigestText(
            PlanningPathPolicy(read_authorization, secret_policy).scope_digest
        )
        binding = PlanningTurnBinding(
            repository_id=run.repository_id,
            pinned_base_oid=run.pinned_target_oid,
            scope_digest=scope_digest,
            snapshot_digest=planning_snapshot_digest(
                run.repository_id,
                run.pinned_target_oid,
                scope_digest,
            ),
        )
        return PlanningAuthorization(
            run_id=run_id,
            decision="ALLOW"
            if planning_request_count < budget.planning_request_ceiling
            else "PAUSE",
            reason=(
                None
                if planning_request_count < budget.planning_request_ceiling
                else "PLANNING_REQUEST_LIMIT"
            ),
            applicable_revision_digests=current,
            target_safety_digest=(
                reservation.admin_binding_digest or self._store.target_authority_digest(run_id)
            ),
            credential_profile=(
                "deepseek" if model.provider == "deepseek_responses" else "scripted_mock"
            ),
            read_authorization=read_authorization
            if planning_request_count < budget.planning_request_ceiling
            else None,
            turn_binding=binding
            if planning_request_count < budget.planning_request_ceiling
            else None,
            planning_request_count=planning_request_count,
            planning_request_ceiling=budget.planning_request_ceiling,
        )


class _CompositionPlanningManifests:
    def __init__(
        self,
        resources: _CompositionRepositoryResources,
        revisions: _CompositionPlanningRevisionContext,
        secret_policy: SecretPathPolicy | None = None,
    ) -> None:
        self._resources = resources
        self._revisions = revisions
        self._secret_policy = secret_policy

    def manifest(self, binding: PlanningTurnBinding) -> PlanningManifest:
        run = self._revisions.run()
        return self._resources.planning_reader(
            binding.repository_id,
            run.repository_instance_digest,
            PlanningPathPolicy(
                self._revisions.policy(),
                _secret_policy_for_runtime(self._secret_policy),
            ),
        ).manifest(binding)


class _CompositionPlanningReads(PlanningReadGateway):
    def __init__(
        self,
        store: SqliteStateStore,
        resources: _CompositionRepositoryResources,
        secret_policy: SecretPathPolicy | None = None,
    ) -> None:
        self._store = store
        self._resources = resources
        self._secret_policy = secret_policy

    def execute(
        self, intent: PlanningReadIntent, authorization: PlanningAuthorization
    ) -> PlanningReadResult:
        read_authorization = authorization.read_authorization
        if read_authorization is None:
            raise ValueError("PLANNING_READ_AUTHORIZATION_NOT_ALLOWED")
        run = self._store.run_record(intent.run_id)
        reader = self._resources.planning_reader(
            intent.repository_id,
            run.repository_instance_digest,
            PlanningPathPolicy(
                read_authorization,
                _secret_policy_for_runtime(self._secret_policy),
            ),
        )
        return BoundedPlanningReadGateway(reader).execute(intent, authorization)


class _ProductionTargetReservationDriver(TargetReservationDriver):
    def __init__(self, store: SqliteStateStore, resources: _CompositionRepositoryResources) -> None:
        self._store = store
        self._resources = resources

    def initialize(self, run_id: RunId, permit: RuntimePermit) -> RuntimeDecision:
        run = self._store.run_record(run_id)
        reservation = self._store.target_reservation_for_run(run_id)
        observer, git, _path_reader = self._resources.reservation_adapters(
            reservation,
            run.repository_id,
            run.repository_instance_digest,
            self._store.target_authority_digest(run_id),
        )
        admission = TargetReservationBootstrapAdmissionService(
            state=self._store,
            observer=observer,
            effects=TargetReservationAdmissionService(observer, git),
        )
        decision = TargetReservationDriver(admission).initialize(run_id, permit)
        if decision.phase_transition == "TARGET_RESERVATION_INITIALIZED":
            self._resources.refresh()
        return decision


class _ProductionPrivateRefDriver:
    def __init__(self, store: SqliteStateStore, resources: _CompositionRepositoryResources) -> None:
        self._store = store
        self._resources = resources

    def initialize(self, run_id: RunId, permit: RuntimePermit) -> RuntimeDecision:
        run = self._store.run_record(run_id)
        reservation = self._store.target_reservation_for_run(run_id)
        guard = self._resources.private_ref_guard(
            reservation=reservation,
            repository_id=run.repository_id,
            repository_instance_digest=run.repository_instance_digest,
            target_safety_digest=self._store.target_authority_digest(run_id),
        )
        decision = PrivateRefInitializer(self._store, guard, guard).initialize(run_id, permit)
        if decision.phase_transition == "PRIVATE_REF_INITIALIZED":
            self._resources.refresh()
        return decision


class _ProductionTargetReservationResolutionObserver:
    """Project the existing reservation observer into bounded recovery evidence."""

    def __init__(self, store: SqliteStateStore, resources: _CompositionRepositoryResources) -> None:
        self._store = store
        self._resources = resources

    def observe(self, intent: EffectIntent, recovery_generation: int) -> RecoveryObservation:
        try:
            creation = TargetReservationCreationIntent.from_effect_intent(intent)
            run = self._store.run_record(intent.run_id)
            if creation.run_id != intent.run_id:
                raise StateConflict("RESERVATION_OBSERVATION_RUN_BINDING_MISMATCH")
            reservation = TargetReservation(
                reservation_id=creation.reservation_id,
                run_id=creation.run_id,
                target_ref=creation.target_ref,
                pinned_target_oid=creation.pinned_target_oid,
                path=Path(creation.reservation_path),
                phase="CREATION_INTENT_RECORDED",
            )
            observer, _, _path_reader = self._resources.reservation_adapters(
                reservation,
                run.repository_id,
                run.repository_instance_digest,
                creation.target_authority_digest,
            )
            observed = observer.observe(reservation)
        except (OSError, RuntimeError, StateConflict, ValueError, KeyError):
            from apexcrew.application.runtime import _unavailable_resolution_observation

            return _unavailable_resolution_observation(intent, recovery_generation)

        state = _reservation_recovery_state(observed)
        zero = Sha256DigestText("sha256:" + "0" * 64)
        if observed.registration_present and observed.admin_binding_digest is None:
            from apexcrew.application.runtime import _unavailable_resolution_observation

            return _unavailable_resolution_observation(intent, recovery_generation)
        if observed.path_present and (
            observed.path_identity is None or observed.gitfile_digest is None
        ):
            from apexcrew.application.runtime import _unavailable_resolution_observation

            return _unavailable_resolution_observation(intent, recovery_generation)
        admin_digest = observed.admin_binding_digest or zero
        path_identity = observed.path_identity or str(reservation.path)
        gitfile_digest = observed.gitfile_digest or zero
        values: dict[str, object] = {
            "kind": RecoveryActionClass.TARGET_RESERVATION,
            "intent_id": intent.intent_id,
            "recovery_generation": recovery_generation,
            "source_payload_digest": intent.payload_digest,
            "state": state,
            "idempotency_key": intent.idempotency_key,
            "registration_identity": reservation.reservation_id,
            "reservation_operation": "CREATE",
            "admin_binding_digest": admin_digest,
            "path_identity": path_identity,
            "gitfile_digest": gitfile_digest,
        }
        if state == "BOTH_PRESENT_LOCKED":
            proof = {
                "state": state,
                "registration_identity": reservation.reservation_id,
                "reservation_operation": "CREATE",
                "admin_binding_digest": admin_digest,
                "path_identity": path_identity,
                "gitfile_digest": gitfile_digest,
            }
            proof_json = canonical_json(proof)
            values.update(
                {
                    "run_id": intent.run_id,
                    "settled_sequence": AuditSequence(
                        self._store.audit_sequence(intent.run_id) + 1
                    ),
                    "applicable_revision_digests": intent.applicable_revision_digests,
                    "completion_proof_json": proof_json,
                    "completion_proof_digest": sha256_digest(proof_json),
                }
            )
        return RecoveryObservation.create(**values)


class _ProductionRefResolutionObserver:
    """Observe private and target refs through the repository-owned CAS adapters."""

    def __init__(self, store: SqliteStateStore, resources: _CompositionRepositoryResources) -> None:
        self._store = store
        self._resources = resources

    def observe(self, intent: EffectIntent, recovery_generation: int) -> RecoveryObservation:
        if intent.kind in {"private_ref_init", "private_ref_cas"}:
            return self._observe_private(intent, recovery_generation)
        if intent.kind == "target_ref_cas":
            return self._observe_target(intent, recovery_generation)
        from apexcrew.application.runtime import _unavailable_resolution_observation

        return _unavailable_resolution_observation(intent, recovery_generation)

    def _observe_private(
        self, intent: EffectIntent, recovery_generation: int
    ) -> RecoveryObservation:
        from apexcrew.application.runtime import _unavailable_resolution_observation

        try:
            payload = json.loads(intent.normalized_payload_json)
            if intent.kind == "private_ref_init":
                typed = RefCasIntent.from_effect_intent(intent)
                repository_id = typed.repository_id
                ref_name = typed.ref_name
                expected_old_oid = typed.expected_old_oid
                prepared_oid = typed.prepared_oid
                safety_digest = typed.target_safety_digest
                binding = typed.ref_effect_binding
                reservation_id = typed.target_reservation_id
            else:
                repository_id = RepositoryId(str(payload["repository_id"]))
                ref_name = str(payload["ref_name"])
                expected_old_oid = payload.get("expected_old_oid")
                prepared_oid = payload["prepared_oid"]
                safety_digest = Sha256DigestText(str(payload["target_safety_digest"]))
                binding = RefEffectBinding.model_validate(payload["ref_effect_binding"])
                reservation_id = str(payload["target_reservation_id"])
            run = self._store.run_record(intent.run_id)
            reservation = self._store.target_reservation(reservation_id)
            guard = self._resources.private_ref_guard(
                reservation=reservation,
                repository_id=repository_id,
                repository_instance_digest=run.repository_instance_digest,
                target_safety_digest=safety_digest,
            )
            state, current, registration = guard.observe_resolution(
                ref_name=ref_name,
                expected_old_oid=expected_old_oid,
                prepared_oid=prepared_oid,
                expected_binding=binding,
            )
            old_oid = expected_old_oid or type(prepared_oid)("0" * 40)
            current_oid = current or old_oid
            return self._ref_observation(
                intent=intent,
                recovery_generation=recovery_generation,
                state=state,
                repository_id=repository_id,
                repository_instance_digest=run.repository_instance_digest,
                ref_name=ref_name,
                registration_digest=registration,
                safety_digest=safety_digest,
                old_oid=old_oid,
                prepared_oid=prepared_oid,
                current_oid=current_oid,
                settled_sequence=AuditSequence(self._store.audit_sequence(intent.run_id) + 1),
            )
        except (OSError, KeyError, StateConflict, TypeError, ValueError, RuntimeError):
            return _unavailable_resolution_observation(intent, recovery_generation)

    def _observe_target(
        self, intent: EffectIntent, recovery_generation: int
    ) -> RecoveryObservation:
        from apexcrew.application.runtime import _unavailable_resolution_observation

        try:
            typed = TargetCasIntent.from_effect_intent(intent)
            run = self._store.run_record(intent.run_id)
            reservation = self._store.target_reservation_for_run(intent.run_id)
            recorded_repository_id = typed.repository_id
            recorded_instance_digest = typed.repository_instance_digest
            recorded_ref_name = typed.ref_name
            recorded_old_oid = typed.expected_old_oid
            recorded_prepared_oid = typed.prepared_oid
            recorded_safety_digest = typed.target_safety_digest
            recorded_registration_digest = typed.registration_digest
            if (
                typed.run_id != intent.run_id
                or typed.applicable_revision_digests != intent.applicable_revision_digests
                or typed.idempotency_key != intent.idempotency_key
                or recorded_repository_id != run.repository_id
                or recorded_instance_digest != run.repository_instance_digest
                or recorded_ref_name != run.target_ref
                or recorded_safety_digest != self._store.target_authority_digest(intent.run_id)
            ):
                return _unavailable_resolution_observation(intent, recovery_generation)
            adapter = self._resources.target_cas(
                reservation, run.repository_id, run.repository_instance_digest
            )
            observed = adapter.observe_resolution(
                target_ref=recorded_ref_name,
                expected_old_oid=recorded_old_oid,
                prepared_oid=recorded_prepared_oid,
            )
            if observed.registration_digest != recorded_registration_digest:
                return _unavailable_resolution_observation(intent, recovery_generation)
            return self._ref_observation(
                intent=intent,
                recovery_generation=recovery_generation,
                state=observed.state,
                repository_id=run.repository_id,
                repository_instance_digest=run.repository_instance_digest,
                ref_name=recorded_ref_name,
                registration_digest=observed.registration_digest,
                safety_digest=recorded_safety_digest,
                old_oid=recorded_old_oid,
                prepared_oid=recorded_prepared_oid,
                current_oid=observed.observed_oid or recorded_old_oid,
                settled_sequence=AuditSequence(self._store.audit_sequence(intent.run_id) + 1),
            )
        except (OSError, KeyError, StateConflict, TypeError, ValueError, RuntimeError):
            return _unavailable_resolution_observation(intent, recovery_generation)

    @staticmethod
    def _ref_observation(
        *,
        intent: EffectIntent,
        recovery_generation: int,
        state: str,
        repository_id: RepositoryId,
        repository_instance_digest: Sha256DigestText,
        ref_name: str,
        registration_digest: Sha256DigestText | None,
        safety_digest: Sha256DigestText,
        old_oid: object,
        prepared_oid: object,
        current_oid: object,
        settled_sequence: AuditSequence,
    ) -> RecoveryObservation:
        from apexcrew.application.runtime import _unavailable_resolution_observation

        if registration_digest is None or state not in {
            "EXACT_POST",
            "EXACT_PRE",
            "THIRD_STATE",
        }:
            return _unavailable_resolution_observation(intent, recovery_generation)
        values: dict[str, object] = {
            "kind": RecoveryActionClass.PRIVATE_REF
            if intent.kind.startswith("private_ref")
            else RecoveryActionClass.TARGET_CAS,
            "intent_id": intent.intent_id,
            "recovery_generation": recovery_generation,
            "source_payload_digest": intent.payload_digest,
            "state": state,
            "idempotency_key": intent.idempotency_key,
            "repository_id": str(repository_id),
            "repository_instance_digest": repository_instance_digest,
            "ref_name": ref_name,
            "registration_digest": registration_digest,
            "target_safety_digest": safety_digest,
            "old_oid": old_oid,
            "prepared_oid": prepared_oid,
            "current_oid": current_oid,
        }
        if state == "EXACT_POST":
            proof = canonical_json(
                {
                    "state": state,
                    "repository_id": str(repository_id),
                    "repository_instance_digest": repository_instance_digest,
                    "ref_name": ref_name,
                    "registration_digest": registration_digest,
                    "target_safety_digest": safety_digest,
                    "old_oid": old_oid,
                    "prepared_oid": prepared_oid,
                    "current_oid": current_oid,
                }
            )
            values.update(
                {
                    "run_id": intent.run_id,
                    "settled_sequence": settled_sequence,
                    "applicable_revision_digests": intent.applicable_revision_digests,
                    "completion_proof_json": proof,
                    "completion_proof_digest": sha256_digest(proof),
                }
            )
        return RecoveryObservation.create(**values)


def _reservation_recovery_state(observed: object) -> str:
    if not getattr(observed, "observable", False):
        return "UNAVAILABLE"
    registration_present = bool(getattr(observed, "registration_present", False))
    path_present = bool(getattr(observed, "path_present", False))
    if not registration_present and not path_present:
        return "BOTH_ABSENT"
    if registration_present and path_present and getattr(observed, "exact_identity", False):
        return (
            "BOTH_PRESENT_LOCKED" if getattr(observed, "locked", False) else "BOTH_PRESENT_UNLOCKED"
        )
    if registration_present and not path_present:
        return "ADMIN_ONLY"
    if path_present and not registration_present:
        return "PATH_ONLY"
    return "MIXED"


class _CompositionIntegrationDriver:
    def __init__(self, store: SqliteStateStore, resources: _CompositionRepositoryResources) -> None:
        self._store = store
        self._resources = resources

    def integrate(self, run_id: RunId, permit: RuntimePermit) -> RuntimeDecision:
        if permit.state != "CONSUMED" or permit.consumed_owner_id is None:
            return RuntimeDecision.pause("FINAL_INTEGRATION_PERMIT_INVALID")
        candidate = self._store.final_candidate(run_id)
        prepared_oid = self._store.final_candidate_prepared_oid(run_id)
        run = self._store.run_record(run_id)
        result = self._resources.target_cas(
            self._store.target_reservation_for_run(run_id),
            run.repository_id,
            run.repository_instance_digest,
        ).apply(
            target_ref=candidate.target_ref,
            expected_old_oid=candidate.head_oid,
            prepared_oid=prepared_oid,
            reflog_message=f"apexcrew integrate {run_id}",
        )
        sequence = self._store.settle_final_integration(
            run_id=run_id,
            owner_id=permit.consumed_owner_id,
            permit_generation=permit.generation,
            candidate_id=candidate.candidate_id,
            result_class=result.result_class,
            observed_oid=result.observed_oid,
            expected_sequence=self._store.audit_sequence(run_id),
        )
        if result.result_class == "APPLIED":
            return RuntimeDecision.continued(sequence)
        return RuntimeDecision.pause(
            "INDETERMINATE"
            if result.result_class == "UNOBSERVABLE"
            else "FINAL_INTEGRATION_CONFLICT",
            sequence,
        )


class _CompositionReservationCleanup:
    def __init__(self, store: SqliteStateStore, resources: _CompositionRepositoryResources) -> None:
        self._store = store
        self._resources = resources

    def observe(self, reservation: TargetReservation) -> CleanupObservation:
        run = self._store.run_record(reservation.run_id)
        observer, _git, _path_reader = self._resources.reservation_adapters(
            reservation,
            run.repository_id,
            run.repository_instance_digest,
            self._store.target_authority_digest(reservation.run_id),
        )
        observed = observer.observe(reservation)
        conflict = CleanupObservation(CleanupObservationKind.CONFLICT, reservation.reservation_id)
        if not observed.observable:
            return conflict
        if not observed.registration_present and not observed.path_present:
            return CleanupObservation(
                CleanupObservationKind.BOTH_ABSENT, reservation.reservation_id
            )
        admin_exact = (
            observed.registration_present
            and observed.registration_exact_identity
            and observed.admin_entry_name == reservation.reservation_id
            and observed.admin_binding_digest is not None
            and observed.admin_binding_digest == reservation.admin_binding_digest
        )
        path_exact = (
            observed.path_present
            and observed.gitfile_only
            and observed.path_exact_back_reference
            and observed.path_identity is not None
            and observed.gitfile_digest is not None
        )
        if observed.registration_present and observed.path_present and admin_exact and path_exact:
            kind = (
                CleanupObservationKind.BOTH_EXACT_LOCKED
                if observed.locked
                else CleanupObservationKind.BOTH_EXACT_UNLOCKED
            )
            return CleanupObservation(
                kind,
                reservation.reservation_id,
                observed.path_identity,
                observed.gitfile_digest,
                observed.admin_binding_digest,
                observed.lock_digest,
            )
        if not observed.registration_present and path_exact:
            return CleanupObservation(
                CleanupObservationKind.PATH_ONLY_EXACT_GITFILE,
                reservation.reservation_id,
                observed.path_identity,
                observed.gitfile_digest,
                None,
                None,
            )
        if not observed.path_present and admin_exact:
            return CleanupObservation(
                CleanupObservationKind.ADMIN_ONLY_EXACT,
                reservation.reservation_id,
                None,
                None,
                observed.admin_binding_digest,
                observed.lock_digest,
            )
        return conflict

    def apply_exact(self, reservation: TargetReservation, observation: CleanupObservation) -> None:
        run = self._store.run_record(reservation.run_id)
        observer, git, path_reader = self._resources.reservation_adapters(
            reservation,
            run.repository_id,
            run.repository_instance_digest,
            self._store.target_authority_digest(reservation.run_id),
        )
        if observation.kind is CleanupObservationKind.BOTH_ABSENT:
            return
        if observation.kind is CleanupObservationKind.PATH_ONLY_EXACT_GITFILE:
            git.release_cached_reservation(reservation)
            path = path_reader.observe_path(reservation)
            if (
                path.path_identity != observation.path_identity_digest
                or path.gitfile_digest != observation.gitfile_digest
            ):
                raise RuntimeError("TARGET_RESERVATION_CLEANUP_CONFLICT")
            path_reader.remove_exact_gitfile(reservation, path)
        elif observation.kind is CleanupObservationKind.ADMIN_ONLY_EXACT:
            git.remove_exact_admin_entry(
                reservation, observation.admin_identity_digest, observation.lock_digest
            )
        elif observation.kind is CleanupObservationKind.BOTH_EXACT_LOCKED:
            git.unlock(reservation)
            unlocked = observer.observe(reservation)
            if not (
                unlocked.observable
                and unlocked.registration_present
                and unlocked.path_present
                and unlocked.registration_exact_identity
                and unlocked.path_exact_back_reference
                and unlocked.gitfile_only
                and unlocked.path_identity is not None
                and unlocked.gitfile_digest is not None
                and unlocked.path_identity == observation.path_identity_digest
                and unlocked.gitfile_digest == observation.gitfile_digest
                and unlocked.admin_binding_digest == observation.admin_identity_digest
                and not unlocked.locked
            ):
                raise RuntimeError("TARGET_RESERVATION_CLEANUP_CONFLICT")
            git.remove_force(
                reservation,
                expected_path_identity=unlocked.path_identity,
                expected_gitfile_digest=unlocked.gitfile_digest,
            )
        elif observation.kind is CleanupObservationKind.BOTH_EXACT_UNLOCKED:
            unlocked = observer.observe(reservation)
            if not (
                unlocked.observable
                and unlocked.registration_present
                and unlocked.path_present
                and unlocked.registration_exact_identity
                and unlocked.path_exact_back_reference
                and unlocked.gitfile_only
                and unlocked.path_identity is not None
                and unlocked.gitfile_digest is not None
                and unlocked.path_identity == observation.path_identity_digest
                and unlocked.gitfile_digest == observation.gitfile_digest
                and unlocked.admin_binding_digest == observation.admin_identity_digest
                and not unlocked.locked
            ):
                raise RuntimeError("TARGET_RESERVATION_CLEANUP_CONFLICT")
            git.remove_force(
                reservation,
                expected_path_identity=unlocked.path_identity,
                expected_gitfile_digest=unlocked.gitfile_digest,
            )
        else:
            raise RuntimeError("TARGET_RESERVATION_CLEANUP_CONFLICT")
        after = observer.observe(reservation)
        if after.observable and not after.registration_present and not after.path_present:
            self._resources.refresh()
            return
        if observation.kind is CleanupObservationKind.PATH_ONLY_EXACT_GITFILE:
            raise RuntimeError("TARGET_RESERVATION_CLEANUP_CONFLICT")
        if observation.kind is CleanupObservationKind.ADMIN_ONLY_EXACT:
            raise RuntimeError("TARGET_RESERVATION_CLEANUP_CONFLICT")
        raise RuntimeError("TARGET_RESERVATION_CLEANUP_CONFLICT")


class _ProductionStartGuard:
    def __init__(self, store: SqliteStateStore, resources: _CompositionRepositoryResources) -> None:
        self._store = store
        self._resources = resources

    def _guard(self, run_id: RunId) -> GitPrivateRefStartGuard:
        run = self._store.run_record(run_id)
        reservation = self._store.target_reservation_for_run(run_id)
        return self._resources.private_ref_guard(
            reservation=reservation,
            repository_id=run.repository_id,
            repository_instance_digest=run.repository_instance_digest,
            target_safety_digest=self._store.target_authority_digest(run_id),
        )

    def inspect(
        self,
        *,
        run_id: RunId,
        applicable_revision_digests: ApplicableRevisionDigests,
        expected_sequence: AuditSequence,
    ) -> StartGuardDecision:
        return self._guard(run_id).inspect(
            run_id=run_id,
            applicable_revision_digests=applicable_revision_digests,
            expected_sequence=expected_sequence,
        )

    def validate_consumed(
        self,
        *,
        binding: RuntimeStartBinding,
        permit: RuntimePermit,
        expected_sequence: AuditSequence,
    ) -> StartGuardDecision:
        return self._guard(permit.run_id).validate_consumed(
            binding=binding, permit=permit, expected_sequence=expected_sequence
        )


def _secret_policy_for_runtime(provided: SecretPathPolicy | None = None) -> SecretPathPolicy:
    if provided is not None:
        return provided
    try:
        key, rules = KeyringSecretPolicyStore().load()
    except SecretPolicyConfigurationError as error:
        raise RuntimeError(str(error)) from error
    return SecretPathPolicy.from_host_rules(rules, key)


class _CompositionPlanningContext:
    def __init__(self, store: SqliteStateStore) -> None:
        self._store = store

    def _documents(
        self, run_id: RunId
    ) -> tuple[ModelConfigurationRevisionDocument, BudgetRevisionDocument]:
        model_configuration = cast(
            ModelConfigurationRevisionDocument,
            self._store.current_revision_document(run_id, "MODEL_CONFIGURATION"),
        )
        budget = cast(
            BudgetRevisionDocument,
            self._store.current_revision_document(run_id, "BUDGET"),
        )
        return model_configuration, budget

    def build_planning_request(
        self,
        run_id: RunId,
        authorization: PlanningAuthorization,
        manifest: PlanningManifest,
    ) -> ModelRequest:
        model_configuration, budget = self._documents(run_id)
        self._requested_model_id = model_configuration.requested_model_id
        self._allowed_model_ids = frozenset(
            alias.returned_model_id for alias in model_configuration.returned_model_aliases
        )
        self._max_input_tokens = model_configuration.inference_settings.max_input_tokens
        self._max_output_tokens = model_configuration.inference_settings.max_output_tokens
        self._tool_schema_digest = model_configuration.tool_schema_digest
        self._reserved_cost_usd = _worst_case_reservation(model_configuration, budget)
        inputs = self._store.bootstrap_inputs(run_id)
        prompt_document = {
            "acceptance_criteria": list(inputs.acceptance_criteria),
            "constraints": list(inputs.constraints),
            "goal": inputs.goal,
            "manifest": [
                {"path": str(path), "digest": str(digest), "size": size}
                for path, digest, size in manifest.entries
            ],
            "instructions": (
                "Return exactly one submit_plan action now with a complete plan_document "
                "derived from the goal, constraints, acceptance criteria, and manifest. "
                "Do not return read_tracked_file, search_tracked_content, or a Worker action. "
                "plan_document must contain exactly tasks, run_checks, and "
                "proposed_promotion_order. Each task must contain task_id, read_globs, "
                "dependency_globs, dependency_task_ids, write_globs, checks, and "
                "constraints. Each check and run_check must contain argv and input_globs. "
                "Use task-01 as the task_id and proposed promotion order when one task is "
                "sufficient."
            ),
        }
        prompt = canonical_json(prompt_document)
        current = authorization.applicable_revision_digests
        if (
            current.policy_digest is None
            or current.budget_digest is None
            or current.model_configuration_digest is None
        ):
            raise ValueError("PLANNING_REVISION_BINDING_INCOMPLETE")
        request_digest = sha256_digest(
            canonical_json(
                {
                    "run_id": run_id,
                    "prompt": prompt,
                    "snapshot_digest": authorization.turn_binding.snapshot_digest
                    if authorization.turn_binding is not None
                    else None,
                }
            )
        )
        return ModelRequest(
            run_id=run_id,
            plan_digest=None,
            policy_digest=current.policy_digest,
            budget_digest=current.budget_digest,
            model_configuration_digest=current.model_configuration_digest,
            requested_model_id=self._requested_model_id,
            allowed_model_ids=self._allowed_model_ids,
            prompt=({"role": "user", "content": prompt},),
            tool_schema_digest=str(self._tool_schema_digest),
            request_digest=request_digest,
            idempotency_key=f"planning-request:{run_id}:{request_digest}",
            max_input_tokens=self._max_input_tokens,
            max_output_tokens=self._max_output_tokens,
            reserved_cost_usd=self._reserved_cost_usd,
            temperature=model_configuration.inference_settings.temperature,
            reasoning_effort=model_configuration.inference_settings.reasoning_effort,
        )


class _CompositionPlanningRequests:
    def __init__(self, store: SqliteStateStore) -> None:
        self._context = _CompositionPlanningContext(store)

    def create(
        self,
        *,
        run_id: RunId,
        authorization: PlanningAuthorization,
        manifest: PlanningManifest,
    ) -> ModelRequest:
        return self._context.build_planning_request(run_id, authorization, manifest)


class _RuntimeIds:
    def __init__(self) -> None:
        self._next = 0

    def _value(self, prefix: str, run_id: RunId) -> str:
        self._next += 1
        return f"{prefix}-{run_id}-{self._next}"

    def next_action_id(self, run_id: RunId) -> str:
        return self._value("action", run_id)

    def next_intent_id(self, run_id: RunId) -> IntentId:
        return IntentId(self._value("intent", run_id))


class _CompositionWorkerScheduling:
    """Allocate the first bounded worker tranche through the authority service."""

    def __init__(self, store: SqliteStateStore, authority: AuthorityService) -> None:
        self._store = store
        self._authority = authority

    def next_dispatchable(self, run_id: RunId) -> TaskDispatchSelection | RuntimeDecision:
        state = self._store.load_runtime_state(run_id)
        if state.state == "ACTIVE" and state.plan_digest is not None:
            for contract in self._store.task_contracts(state.plan_digest):
                counters = self._store.task_budget_state(run_id, TaskId(contract.task_id))
                if (
                    contract.dependency_task_ids
                    or counters.allocated_calls != 0
                    or counters.tranche_count != 0
                ):
                    continue
                task = TaskAuthority(
                    run_id=run_id,
                    task_id=TaskId(contract.task_id),
                    attempt_id=AttemptId(f"bootstrap-{run_id}-{contract.task_id}"),
                )
                self._authority.allocate_tranche(
                    task,
                    NO_PROGRESS,
                    expected_sequence=self._store.audit_sequence(run_id),
                )
                break
        selection = self._store.next_dispatchable(run_id)
        if (
            isinstance(selection, RuntimeDecision)
            and selection.stop_reason == "NO_DISPATCHABLE_TASK"
        ):
            try:
                sequence = self._store.prepare_final_candidate(
                    run_id, self._store.audit_sequence(run_id)
                )
            except StateConflict as error:
                if str(error) not in {
                    "FINAL_CANDIDATE_TASKS_INCOMPLETE",
                    "FINAL_CANDIDATE_TASKS_MISSING",
                    "FINAL_CANDIDATE_PLAN_NOT_FOUND",
                }:
                    raise
                return selection
            return RuntimeDecision.pause("AWAITING_FINAL_APPROVAL", sequence)
        return selection


class ApplicationBundle:
    """The only concrete object exposed by the application composition root."""

    __slots__ = ("_closeables", "_closed", "_closed_indices", "control", "queries", "runtime")

    def __init__(
        self,
        *,
        control: CrewControlService,
        runtime: RuntimeService,
        queries: RunQueryService,
        closeables: tuple[object, ...],
    ) -> None:
        self.control = control
        self.runtime = runtime
        self.queries = queries
        self._closeables = closeables
        self._closed = False
        self._closed_indices: set[int] = set()

    def close(self) -> None:
        if self._closed:
            return
        first_error: BaseException | None = None
        for index in range(len(self._closeables) - 1, -1, -1):
            if index in self._closed_indices:
                continue
            closeable = self._closeables[index]
            close = getattr(closeable, "close", None)
            if callable(close):
                try:
                    close()
                except BaseException as error:  # noqa: BLE001 - cleanup must continue
                    if first_error is None:
                        first_error = error
                else:
                    self._closed_indices.add(index)
            else:
                self._closed_indices.add(index)
        if first_error is not None:
            raise first_error
        self._closed = True


def build_application_bundle(
    root: Path,
    *,
    repository_authority: RepositoryBootstrapPort | None = None,
    model_configuration: ModelConfigurationRevisionDocument | None = None,
    budget: BudgetRevisionDocument | None = None,
    scripted_model: ScriptedMockLLM | None = None,
    credential_source: ModelCredentialPort | None = None,
    secret_policy: SecretPathPolicy | None = None,
    response_schemas: Mapping[str, Mapping[str, object]] | None = None,
    client_factory: ClientFactory | None = None,
    allow_live_provider: bool = False,
) -> ApplicationBundle:
    """Build the production application graph with the restricted executor boundary."""
    return _build_application_bundle(
        root,
        repository_authority=repository_authority,
        model_configuration=model_configuration,
        budget=budget,
        scripted_model=scripted_model,
        credential_source=credential_source,
        secret_policy=secret_policy,
        response_schemas=response_schemas,
        client_factory=client_factory,
        executor=None,
        allow_live_provider=allow_live_provider,
    )


def build_test_application_bundle(
    root: Path,
    *,
    repository_authority: RepositoryBootstrapPort | None = None,
    model_configuration: ModelConfigurationRevisionDocument | None = None,
    budget: BudgetRevisionDocument | None = None,
    scripted_model: ScriptedMockLLM | None = None,
    credential_source: ModelCredentialPort | None = None,
    secret_policy: SecretPathPolicy | None = None,
    response_schemas: Mapping[str, Mapping[str, object]] | None = None,
    client_factory: ClientFactory | None = None,
    executor: ExecutorPort,
    allow_live_provider: bool = False,
) -> ApplicationBundle:
    """Build the deterministic test seam with an explicitly supplied executor."""
    return _build_application_bundle(
        root,
        repository_authority=repository_authority,
        model_configuration=model_configuration,
        budget=budget,
        scripted_model=scripted_model,
        credential_source=credential_source,
        secret_policy=secret_policy,
        response_schemas=response_schemas,
        client_factory=client_factory,
        executor=executor,
        allow_live_provider=allow_live_provider,
    )


def _build_application_bundle(
    root: Path,
    *,
    repository_authority: RepositoryBootstrapPort | None = None,
    model_configuration: ModelConfigurationRevisionDocument | None = None,
    budget: BudgetRevisionDocument | None = None,
    scripted_model: ScriptedMockLLM | None = None,
    credential_source: ModelCredentialPort | None = None,
    secret_policy: SecretPathPolicy | None = None,
    response_schemas: Mapping[str, Mapping[str, object]] | None = None,
    client_factory: ClientFactory | None = None,
    executor: ExecutorPort | None = None,
    allow_live_provider: bool = False,
) -> ApplicationBundle:
    """Build one shared, closeable application object graph."""
    root = root.resolve()
    data_root = root / ".apexcrew"
    data_root.mkdir(parents=True, exist_ok=True)
    store = SqliteStateStore(data_root / "state.db", monotonic_clock=SystemMonotonicClock())
    repository = (
        RepositoryBootstrapAdapter() if repository_authority is None else repository_authority
    )
    resources = _CompositionRepositoryResources(root, repository, store.data_root)
    revisions = _default_revisions()
    selected_budget = revisions.budget if budget is None else budget
    selected_model_configuration = (
        revisions.model_configuration if model_configuration is None else model_configuration
    )
    try:
        model = build_model_port(
            model_configuration=selected_model_configuration,
            budget=selected_budget,
            scripted_model=scripted_model,
            credential_source=credential_source,
            response_schemas=response_schemas,
            client_factory=client_factory,
            allow_live_provider=allow_live_provider,
        )
        runtime_secret_policy = (
            _secret_policy_for_runtime(secret_policy)
            if secret_policy is not None
            else SecretPathPolicy.from_host_rules((), b"k" * 32)
        )
        authority = AuthorityService(store, secret_paths=runtime_secret_policy)
        worker_model = AuthorityModelClient(model, store, authority, SystemUtcClock())
        ids = _RuntimeIds()
        worker_tools = _CompositionWorkerTools(
            store,
            resources,
            runtime_secret_policy,
            authority,
            executor,
        )
        planning_revisions = _CompositionPlanningRevisionContext(store)
        worker = WorkerLoopService(
            attempts=store,
            capsules=_CompositionWorkerContext(store, resources, runtime_secret_policy),
            requests=_CompositionWorkerRequests(
                store, selected_model_configuration, selected_budget
            ),
            models=worker_model,
            actions=WorkerActionCodec(
                worker_tools.capture_expected_prestate,
                worker_tools.capture_snapshot_digest,
            ),
            authority=authority,
            tools=worker_tools,
            journal=store,
            ids=ids,
            clock=SystemUtcClock().now,
        )
        planning = PlanningActionApplier(
            state=store,
            reads=_CompositionPlanningReads(store, resources, secret_policy),
            ids=ids,
        )
        coordinator = CoordinatorService(
            planning_authorization=_CompositionPlanningAuthorization(
                store, secret_policy, planning_revisions
            ),
            context=BoundedPlanningContextBuilder(
                manifests=_CompositionPlanningManifests(
                    resources, planning_revisions, secret_policy
                ),
                requests=_CompositionPlanningRequests(store),
            ),
            models=worker_model,
            planning_actions=planning,
            journal=store,
            state=store,
            clock=SystemUtcClock(),
            scheduling=_CompositionWorkerScheduling(store, authority),
            attempts=store,
            workers=worker,
        )
        control = CrewControlService(
            ControlCommandService(
                state=store,
                target_authority=_TargetAuthority(store),
                repository_authority=repository,
                start_guard=_ProductionStartGuard(store, resources),
            )
        )
        target_reservations = _ProductionTargetReservationDriver(store, resources)
        recovery = RecoveryService(store)
        resolution_observers = ResolutionObservationRegistry(
            {
                "model": ModelResolutionObserver(store, model),
                "model_request": ModelResolutionObserver(store, model),
                "granted_risky_action": GrantedActionResolutionObserver(store, worker_tools),
                "read": SnapshotResolutionObserver(store, worker_tools),
                "search": SnapshotResolutionObserver(store, worker_tools),
                "patch": ToolActionResolutionObserver(store, worker_tools),
                "check": ToolActionResolutionObserver(store, worker_tools),
                "private_ref_init": _ProductionRefResolutionObserver(store, resources),
                "private_ref_cas": _ProductionRefResolutionObserver(store, resources),
                "target_ref_cas": _ProductionRefResolutionObserver(store, resources),
                "target_reservation_creation": _ProductionTargetReservationResolutionObserver(
                    store, resources
                ),
            }
        )
        phase_drivers = RuntimePhaseDriverService(
            recovered_actions=RecoveredActionRouter(coordinator, worker),
            target_reservations=target_reservations,
            private_refs=_ProductionPrivateRefDriver(store, resources),
            resolution=ResolutionRuntime(store, resolution_observers),
            integration=_CompositionIntegrationDriver(store, resources),
            cleanup=TerminalCleanupRuntime(store, _CompositionReservationCleanup(store, resources)),
            granted_actions=GrantedActionRuntime(store, worker_tools),
        )
        runtime = RuntimeService(
            store=store,
            ownership=FileRunOwnership(
                store.data_root,
                LocalFileLockBackend(),
                ProcessRuntimeOwnerIds(),
            ),
            journal=store,
            authority=authority,
            recovery=recovery,
            coordinator=coordinator,
            model_client=DurableModelClient(model=model, journal=store),
            tools=_ToolSchema(selected_model_configuration.tool_schema_digest),
            phase_drivers=phase_drivers,
            provider_dispatch_authorized=(
                selected_model_configuration.provider != "deepseek_responses" or allow_live_provider
            ),
        )
        queries = RunQueryService(ProjectionService(store))
    except BaseException:
        try:
            store.close()
        finally:
            resources.close()
            close = getattr(repository, "close", None)
            if callable(close):
                close()
        raise
    return ApplicationBundle(
        control=control,
        runtime=runtime,
        queries=queries,
        closeables=(repository, resources, store),
    )


class _ToolSchema:
    def __init__(self, digest: Sha256DigestText) -> None:
        self._digest = digest

    @property
    def schema_digest(self) -> Sha256DigestText:
        return self._digest


def _default_revisions() -> RevisionDocuments:
    return default_revision_documents()


def _worst_case_reservation(
    model_configuration: ModelConfigurationRevisionDocument,
    budget: BudgetRevisionDocument,
) -> Decimal:
    priced = {
        entry.returned_model_id: entry
        for entry in budget.pricing_entries
        if entry.returned_model_id
        in {alias.returned_model_id for alias in model_configuration.returned_model_aliases}
    }
    if len(priced) != len(model_configuration.returned_model_aliases):
        raise ValueError("MODEL_CONFIGURATION_PRICING_INCOMPLETE")
    input_tokens = Decimal(model_configuration.inference_settings.max_input_tokens)
    output_tokens = Decimal(model_configuration.inference_settings.max_output_tokens)
    return max(
        (
            input_tokens * entry.input_usd_per_million / Decimal(1_000_000)
            + output_tokens * entry.output_usd_per_million / Decimal(1_000_000)
            for entry in priced.values()
        ),
        default=Decimal(0),
    )


__all__ = ["ApplicationBundle", "build_application_bundle", "build_test_application_bundle"]
