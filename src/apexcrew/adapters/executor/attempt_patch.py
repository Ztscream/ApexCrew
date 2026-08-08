from __future__ import annotations

import os
import threading
from collections.abc import Mapping
from hashlib import sha256
from pathlib import Path

from apexcrew.adapters.repository.attempt_workspace import (
    MAX_CLEANUP_DEPTH,
    MAX_WORKSPACE_BYTES,
    MAX_WORKSPACE_FILE_BYTES,
    MaterializedWorkspace,
)
from apexcrew.adapters.repository.no_follow import (
    OpenedNode,
    RepositoryUnsafeError,
    StableHandleTree,
)
from apexcrew.adapters.repository.no_follow_posix import PosixNoFollowBackend
from apexcrew.adapters.repository.no_follow_windows import WindowsNoFollowBackend
from apexcrew.adapters.repository.snapshot import MAX_SNAPSHOT_ENTRIES
from apexcrew.adapters.repository.unified_diff import apply_unified_diff
from apexcrew.domain.authority import WorkspaceLease
from apexcrew.domain.effects import canonical_json, sha256_digest
from apexcrew.domain.plan import CanonicalPath
from apexcrew.domain.policy import SecretPathPolicy
from apexcrew.domain.revisions import Sha256DigestText
from apexcrew.domain.tools import (
    PatchExecutionResult,
    PatchExecutionUncertain,
    PatchExecutorPort,
    SnapshotUnavailable,
)


class AttemptPatchExecutionError(PatchExecutionUncertain):
    """The replacement may have committed but its post-state is not observable."""


class AttemptPatchExecutor(PatchExecutorPort):
    """Apply one bounded unified diff inside an isolated attempt workspace."""

    def __init__(
        self, workspace: MaterializedWorkspace | Path, secret_paths: SecretPathPolicy
    ) -> None:
        root = workspace.root if isinstance(workspace, MaterializedWorkspace) else workspace
        if not root.is_absolute():
            raise ValueError("ATTEMPT_PATCH_ROOT_MUST_BE_ABSOLUTE")
        self._root = root
        self._secret_paths = secret_paths
        self._lock = threading.RLock()

    def apply_patch(
        self, lease: WorkspaceLease, patches: Mapping[str, bytes]
    ) -> PatchExecutionResult:
        if lease.state != "ACTIVE" or len(patches) != 1:
            return PatchExecutionResult(code="LEASE_SCOPE_DENIED")
        raw_path, raw_diff = next(iter(patches.items()))
        try:
            path = CanonicalPath.parse(raw_path)
        except (TypeError, ValueError):
            return PatchExecutionResult(code="LEASE_SCOPE_DENIED")
        if self._secret_paths.inspect(path).code != "ALLOW":
            return PatchExecutionResult(code="SECRET_PATH_DENIED")
        if not any(pattern.matches(path) for pattern in lease.write_globs):
            return PatchExecutionResult(code="LEASE_SCOPE_DENIED")
        if not isinstance(raw_diff, bytes):
            return PatchExecutionResult(code="LEASE_SCOPE_DENIED")

        with self._lock:
            tree: StableHandleTree | None = None
            created_node: OpenedNode | None = None
            creation_in_progress = False
            mutation_started = False
            post_tree_digest: Sha256DigestText | None = None
            try:
                tree = self._tree()
                node = tree.try_open(str(path), "file")
                original = (
                    b"" if node is None else tree.read_bytes(node, MAX_WORKSPACE_FILE_BYTES + 1)
                )
                if len(original) > MAX_WORKSPACE_FILE_BYTES:
                    return PatchExecutionResult(code="LEASE_SCOPE_DENIED")
                try:
                    updated = apply_unified_diff(
                        original, raw_diff.decode("utf-8", errors="strict")
                    )
                except (UnicodeDecodeError, RepositoryUnsafeError, ValueError):
                    return PatchExecutionResult(code="LEASE_SCOPE_DENIED")
                if len(updated) > MAX_WORKSPACE_FILE_BYTES:
                    return PatchExecutionResult(code="LEASE_SCOPE_DENIED")

                # There is no cross-platform compare-and-swap replace for an existing
                # final name. Keep the final handle and write through it so a final-name
                # swap cannot redirect the mutation; any post-write mismatch is uncertain.
                parent = self._parent_path(path)
                if parent:
                    tree.open(parent, "directory")
                tree.assert_name_bindings()
                if node is None:
                    creation_in_progress = True
                    node = tree.create_file(str(path))
                    creation_in_progress = False
                    created_node = node
                else:
                    expected_identity = node.identity
                    node = tree.open_for_write(str(path))
                    if node.identity != expected_identity:
                        raise RepositoryUnsafeError("GIT_STORAGE_IDENTITY_CHANGED")
                tree.assert_name_bindings()
                mutation_started = True
                created_node = None
                tree.write_bytes(node, updated)
                tree.assert_name_bindings()
                post_tree_digest = self._tree_digest(tree)
            except (
                OSError,
                RepositoryUnsafeError,
                SnapshotUnavailable,
                ValueError,
            ) as error:
                if created_node is not None and tree is not None:
                    try:
                        tree.remove_file(str(path), created_node.identity)
                        created_node = None
                    except (OSError, RepositoryUnsafeError) as cleanup_error:
                        raise AttemptPatchExecutionError(
                            "PATCH_RESULT_UNCERTAIN"
                        ) from cleanup_error
                if creation_in_progress:
                    raise AttemptPatchExecutionError("PATCH_RESULT_UNCERTAIN") from error
                if mutation_started:
                    raise AttemptPatchExecutionError("PATCH_RESULT_UNCERTAIN") from error
                return PatchExecutionResult(code="LEASE_SCOPE_DENIED")
            finally:
                if tree is not None:
                    try:
                        tree.close()
                    except (OSError, RepositoryUnsafeError) as error:
                        if mutation_started or created_node is not None or creation_in_progress:
                            raise AttemptPatchExecutionError("PATCH_RESULT_UNCERTAIN") from error
                        raise

            if post_tree_digest is None:
                raise AttemptPatchExecutionError("PATCH_RESULT_UNCERTAIN")
            return PatchExecutionResult(code="PATCH_APPLIED", post_tree_digest=post_tree_digest)

    def tree_digest(self) -> Sha256DigestText:
        tree = self._tree()
        try:
            return self._tree_digest(tree)
        finally:
            tree.close()

    def _tree_digest(self, tree: StableHandleTree) -> Sha256DigestText:
        files: dict[str, str] = {}
        pending: list[tuple[str, OpenedNode, int]] = [("", tree.root_node, 0)]
        visited_entries = 0
        total_bytes = 0
        while pending:
            parent, node, depth = pending.pop()
            for name in reversed(tree.list_names(node, MAX_SNAPSHOT_ENTRIES)):
                visited_entries += 1
                if visited_entries > MAX_SNAPSHOT_ENTRIES:
                    raise RepositoryUnsafeError("SNAPSHOT_ENTRY_LIMIT")
                relative = name if not parent else f"{parent}/{name}"
                child = tree.try_open_any(relative)
                if child is None:
                    raise RepositoryUnsafeError("SNAPSHOT_ENTRY_CHANGED")
                if child.identity.kind == "directory":
                    child_depth = depth + 1
                    if child_depth > MAX_CLEANUP_DEPTH:
                        raise RepositoryUnsafeError("SNAPSHOT_DEPTH_LIMIT")
                    pending.append((relative, child, child_depth))
                else:
                    path = CanonicalPath.parse(relative)
                    content = tree.read_bytes(child, MAX_WORKSPACE_FILE_BYTES)
                    total_bytes += len(content)
                    if total_bytes > MAX_WORKSPACE_BYTES:
                        raise RepositoryUnsafeError("SNAPSHOT_BYTE_LIMIT")
                    files[str(path)] = "sha256:" + sha256(content).hexdigest()
        tree.assert_name_bindings()
        return sha256_digest(canonical_json(files))

    def _tree(self) -> StableHandleTree:
        backend = PosixNoFollowBackend() if os.name == "posix" else WindowsNoFollowBackend()
        return StableHandleTree(self._root, backend)

    @staticmethod
    def _parent_path(path: CanonicalPath) -> str:
        parts = str(path).split("/")
        return "/".join(parts[:-1])


__all__ = ["AttemptPatchExecutionError", "AttemptPatchExecutor"]
