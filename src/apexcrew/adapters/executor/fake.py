from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from apexcrew.domain.authority import WorkspaceLease
from apexcrew.domain.effects import canonical_json, sha256_digest
from apexcrew.domain.plan import CanonicalPath
from apexcrew.domain.policy import SecretPathPolicy
from apexcrew.domain.tools import (
    ExecutionResult,
    PatchExecutionResult,
    SanitizedSnapshot,
)


@dataclass(frozen=True, slots=True)
class FakeProcessResult:
    exit_code: int | None
    timed_out: bool
    timing_ms: int
    stdout_chunks: tuple[bytes, ...] = ()
    stderr_chunks: tuple[bytes, ...] = ()


class FakeExecutor:
    def __init__(self, workspace_root: Path, *, secret_paths: SecretPathPolicy) -> None:
        self._workspace_root = workspace_root
        self._secret_paths = secret_paths
        self._responses: dict[tuple[tuple[str, ...], str], FakeProcessResult] = {}
        self._workspace_files: dict[str, bytes] = {}

    def add_response(
        self,
        argv: Sequence[str],
        tree_digest: str,
        response: FakeProcessResult,
    ) -> None:
        self._responses[(tuple(argv), tree_digest)] = response

    def run(
        self,
        argv: Sequence[str],
        snapshot: SanitizedSnapshot,
        timeout_seconds: int,
    ) -> ExecutionResult:
        command = tuple(argv)
        if (
            not command
            or not command[0]
            or any(character.isspace() for character in command[0])
            or timeout_seconds <= 0
        ):
            raise ValueError("STRUCTURED_EXECUTOR_REQUEST_REQUIRED")
        if any(
            self._secret_paths.inspect(CanonicalPath.parse(entry.path)).code != "ALLOW"
            for entry in snapshot.entries
        ):
            raise ValueError("SANITIZED_SNAPSHOT_DENIED")
        try:
            response = self._responses[(command, snapshot.tree_digest)]
        except KeyError as error:
            raise RuntimeError("EXECUTOR_RESPONSE_NOT_CONFIGURED") from error
        return ExecutionResult.from_output(
            exit_code=response.exit_code,
            timed_out=response.timed_out,
            stdout_chunks=response.stdout_chunks,
            stderr_chunks=response.stderr_chunks,
            timing_ms=response.timing_ms,
            secret_paths=self._secret_paths,
        )

    def apply_patch(
        self,
        lease: WorkspaceLease,
        patches: Mapping[str, bytes],
    ) -> PatchExecutionResult:
        if lease.state != "ACTIVE" or len(patches) != 1:
            return PatchExecutionResult(code="LEASE_SCOPE_DENIED")
        canonical: dict[str, bytes] = {}
        for raw_path, content in patches.items():
            try:
                path = CanonicalPath.parse(raw_path)
            except ValueError:
                return PatchExecutionResult(code="LEASE_SCOPE_DENIED")
            if self._secret_paths.inspect(path).code != "ALLOW":
                return PatchExecutionResult(code="SECRET_PATH_DENIED")
            if not any(pattern.matches(path) for pattern in lease.write_globs):
                return PatchExecutionResult(code="LEASE_SCOPE_DENIED")
            canonical[str(path)] = bytes(content)
        next_files = self._workspace_files | canonical
        tree_payload = canonical_json(
            {
                path: "sha256:" + sha256(content).hexdigest()
                for path, content in sorted(next_files.items())
            }
        )
        self._workspace_files = next_files
        return PatchExecutionResult(
            code="PATCH_APPLIED",
            post_tree_digest=sha256_digest(tree_payload),
        )

    def workspace_files(self) -> dict[str, bytes]:
        return self._workspace_files.copy()
