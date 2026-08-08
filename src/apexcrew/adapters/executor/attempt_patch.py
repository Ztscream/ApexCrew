from __future__ import annotations

import os
import tempfile
import threading
from collections.abc import Mapping
from hashlib import sha256
from pathlib import Path

from apexcrew.adapters.repository.attempt_workspace import (
    MAX_WORKSPACE_FILE_BYTES,
    MaterializedWorkspace,
)
from apexcrew.adapters.repository.no_follow import RepositoryUnsafeError, StableHandleTree
from apexcrew.adapters.repository.no_follow_posix import PosixNoFollowBackend
from apexcrew.adapters.repository.no_follow_windows import WindowsNoFollowBackend
from apexcrew.adapters.repository.snapshot import FilesystemRepositorySnapshot
from apexcrew.adapters.repository.unified_diff import apply_unified_diff
from apexcrew.domain.authority import WorkspaceLease
from apexcrew.domain.effects import canonical_json, sha256_digest
from apexcrew.domain.plan import CanonicalPath
from apexcrew.domain.policy import SecretPathPolicy
from apexcrew.domain.revisions import Sha256DigestText
from apexcrew.domain.tools import PatchExecutionResult, PatchExecutorPort, SnapshotNoFollowDenied


class AttemptPatchExecutionError(RuntimeError):
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
            temporary_path: str | None = None
            temporary_fd: int | None = None
            replaced = False
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

                parent = self._parent_path(path)
                parent_node = tree.open(parent, "directory") if parent else tree.root_node
                target = self._root.joinpath(*str(path).split("/"))
                temporary_fd, temporary_path = tempfile.mkstemp(
                    prefix=f".{str(path).split('/')[-1]}.",
                    suffix=".tmp",
                    dir=str(self._root.joinpath(*parent.split("/"))) if parent else str(self._root),
                )
                with os.fdopen(temporary_fd, "wb") as temporary:
                    temporary_fd = None
                    temporary.write(updated)
                    temporary.flush()
                    os.fsync(temporary.fileno())

                # The first probe still includes the original target identity. The
                # second probe is the last check before path-based replacement.
                tree.assert_name_bindings()
                tree.release_cached(str(path))
                tree.assert_name_bindings()
                if os.name == "posix" and os.replace in os.supports_dir_fd:
                    os.replace(
                        Path(temporary_path).name,
                        str(path).split("/")[-1],
                        src_dir_fd=parent_node.handle,
                        dst_dir_fd=parent_node.handle,
                    )
                else:
                    # WindowsNoFollowBackend holds ancestor handles without delete
                    # sharing, so an ancestor cannot be replaced while this tree is open.
                    os.replace(temporary_path, target)
                temporary_path = None
                replaced = True
                tree.assert_name_bindings()
            except (OSError, RepositoryUnsafeError, ValueError) as error:
                if replaced:
                    raise AttemptPatchExecutionError("PATCH_RESULT_UNCERTAIN") from error
                return PatchExecutionResult(code="LEASE_SCOPE_DENIED")
            finally:
                if temporary_fd is not None:
                    try:
                        os.close(temporary_fd)
                    except OSError:
                        pass
                if temporary_path is not None:
                    try:
                        os.unlink(temporary_path)
                    except FileNotFoundError:
                        pass
                    except OSError:
                        pass
                if tree is not None:
                    try:
                        tree.close()
                    except (OSError, RepositoryUnsafeError) as error:
                        if replaced:
                            raise AttemptPatchExecutionError("PATCH_RESULT_UNCERTAIN") from error
                        raise

            try:
                post_tree_digest = self.tree_digest()
            except (OSError, SnapshotNoFollowDenied, ValueError) as error:
                raise AttemptPatchExecutionError("PATCH_RESULT_UNCERTAIN") from error
            return PatchExecutionResult(code="PATCH_APPLIED", post_tree_digest=post_tree_digest)

    def tree_digest(self) -> Sha256DigestText:
        snapshot = FilesystemRepositorySnapshot(self._root)
        files: dict[str, str] = {}
        for entry in snapshot.entries():
            path = CanonicalPath.parse(entry.path)
            content = snapshot.read(path, MAX_WORKSPACE_FILE_BYTES)
            files[str(path)] = "sha256:" + sha256(content).hexdigest()
        return sha256_digest(canonical_json(files))

    def _tree(self) -> StableHandleTree:
        backend = PosixNoFollowBackend() if os.name == "posix" else WindowsNoFollowBackend()
        return StableHandleTree(self._root, backend)

    @staticmethod
    def _parent_path(path: CanonicalPath) -> str:
        parts = str(path).split("/")
        return "/".join(parts[:-1])


__all__ = ["AttemptPatchExecutionError", "AttemptPatchExecutor"]
