from __future__ import annotations

from collections.abc import Mapping
from hashlib import sha256

from apexcrew.adapters.repository.no_follow import RepositoryUnsafeError
from apexcrew.adapters.repository.unified_diff import apply_unified_diff
from apexcrew.domain.authority import WorkspaceLease
from apexcrew.domain.effects import canonical_json, sha256_digest
from apexcrew.domain.plan import CanonicalPath
from apexcrew.domain.policy import SecretPathPolicy
from apexcrew.domain.revisions import Sha256DigestText
from apexcrew.domain.tools import PatchExecutionResult


class MemoryPatchExecutor:
    """Deterministic in-memory PatchExecutor used only by the offline demo."""

    def __init__(self, files: Mapping[str, bytes], *, secret_paths: SecretPathPolicy) -> None:
        self._secret_paths = secret_paths
        self._workspace_files = {
            str(CanonicalPath.parse(path)): bytes(content) for path, content in files.items()
        }

    def apply_patch(
        self, lease: WorkspaceLease, patches: Mapping[str, bytes]
    ) -> PatchExecutionResult:
        if lease.state != "ACTIVE" or len(patches) != 1:
            return PatchExecutionResult(code="LEASE_SCOPE_DENIED")
        raw_path, raw_diff = next(iter(patches.items()))
        try:
            path = CanonicalPath.parse(raw_path)
        except ValueError:
            return PatchExecutionResult(code="LEASE_SCOPE_DENIED")
        if self._secret_paths.inspect(path).code != "ALLOW":
            return PatchExecutionResult(code="SECRET_PATH_DENIED")
        if not any(pattern.matches(path) for pattern in lease.write_globs):
            return PatchExecutionResult(code="LEASE_SCOPE_DENIED")
        try:
            current = self._workspace_files[str(path)]
            updated = apply_unified_diff(current, raw_diff.decode("utf-8"))
        except (KeyError, UnicodeDecodeError, RepositoryUnsafeError):
            return PatchExecutionResult(code="LEASE_SCOPE_DENIED")
        self._workspace_files[str(path)] = updated
        return PatchExecutionResult(
            code="PATCH_APPLIED",
            post_tree_digest=self._tree_digest(),
        )

    def workspace_files(self) -> dict[str, bytes]:
        return self._workspace_files.copy()

    def _tree_digest(self) -> Sha256DigestText:
        payload = canonical_json(
            {
                path: "sha256:" + sha256(content).hexdigest()
                for path, content in sorted(self._workspace_files.items())
            }
        )
        return sha256_digest(payload)
