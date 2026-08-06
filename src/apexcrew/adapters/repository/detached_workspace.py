from __future__ import annotations

import os
import stat
import subprocess
from collections.abc import Mapping
from hashlib import sha256
from pathlib import Path
from typing import Protocol

from apexcrew.adapters.repository.git import (
    GitCatFileBlob,
    GitCatFileSize,
    GitLsTreeRecursive,
    GitOperation,
    RepositoryInstance,
    RepositoryUnsafeError,
)
from apexcrew.adapters.repository.granted_workspace import GrantedWorkspaceAdapter
from apexcrew.adapters.repository.no_follow import StableHandleTree
from apexcrew.adapters.repository.no_follow_posix import PosixNoFollowBackend
from apexcrew.adapters.repository.no_follow_windows import WindowsNoFollowBackend
from apexcrew.adapters.repository.snapshot import FilesystemRepositorySnapshot
from apexcrew.domain.actions import RiskyAction, ToolActionEnvelope
from apexcrew.domain.authority import WorkspaceLease
from apexcrew.domain.effects import canonical_json, sha256_digest
from apexcrew.domain.plan import CanonicalPath
from apexcrew.domain.policy import SecretPathPolicy
from apexcrew.domain.revisions import Sha256DigestText
from apexcrew.domain.tools import ActionPreState, PatchExecutionResult
from apexcrew.domain.types import GitOid

MAX_TREE_OUTPUT_BYTES = 8 * 1024 * 1024
MAX_WORKSPACE_FILE_BYTES = 16 * 1024 * 1024
MAX_WORKSPACE_BYTES = 256 * 1024 * 1024


class WorkspaceGitRunner(Protocol):
    def run_bytes(
        self, repository: RepositoryInstance, operation: GitOperation
    ) -> subprocess.CompletedProcess[bytes]: ...


class DetachedWorkspaceError(RuntimeError):
    pass


class _TreeEntry:
    def __init__(self, mode: bytes, object_type: bytes, oid: GitOid, path: CanonicalPath) -> None:
        self.mode = mode
        self.object_type = object_type
        self.oid = oid
        self.path = path


def _parse_tree(raw: bytes) -> tuple[_TreeEntry, ...]:
    if len(raw) > MAX_TREE_OUTPUT_BYTES or (raw and not raw.endswith(b"\0")):
        raise DetachedWorkspaceError("WORKSPACE_TREE_OUTPUT_INVALID")
    entries: list[_TreeEntry] = []
    for record in raw[:-1].split(b"\0") if raw else ():
        metadata, separator, encoded_path = record.partition(b"\t")
        fields = metadata.split(b" ")
        if not separator or len(fields) != 3 or len(fields[2]) != 40:
            raise DetachedWorkspaceError("WORKSPACE_TREE_OUTPUT_INVALID")
        if any(value not in b"0123456789abcdef" for value in fields[2]):
            raise DetachedWorkspaceError("WORKSPACE_TREE_OUTPUT_INVALID")
        try:
            path = CanonicalPath.parse(encoded_path.decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, ValueError) as error:
            raise DetachedWorkspaceError("WORKSPACE_TREE_PATH_INVALID") from error
        entries.append(_TreeEntry(fields[0], fields[1], GitOid(fields[2].decode("ascii")), path))
    ordered = tuple(sorted(entries, key=lambda entry: str(entry.path)))
    if tuple(str(entry.path) for entry in entries) != tuple(str(entry.path) for entry in ordered):
        raise DetachedWorkspaceError("WORKSPACE_TREE_ORDER_INVALID")
    if len({str(entry.path) for entry in entries}) != len(entries):
        raise DetachedWorkspaceError("WORKSPACE_TREE_DUPLICATE_PATH")
    return ordered


class DetachedWorkspace:
    """Materialize a pinned Git tree into a private, non-linked workspace."""

    def __init__(
        self,
        repository: RepositoryInstance,
        runner: WorkspaceGitRunner,
        root: Path,
        secret_paths: SecretPathPolicy,
    ) -> None:
        if not root.is_absolute():
            raise ValueError("DETACHED_WORKSPACE_ROOT_MUST_BE_ABSOLUTE")
        self._repository = repository
        self._runner = runner
        self.root = root
        self._secret_paths = secret_paths

    def ensure_materialized(self, tree_oid: GitOid) -> None:
        self._ensure_root()
        entries = _parse_tree(self._run(GitLsTreeRecursive(tree_oid)).stdout)
        regular: list[tuple[_TreeEntry, bytes]] = []
        total = 0
        for entry in entries:
            if entry.mode not in {b"100644", b"100755"} or entry.object_type != b"blob":
                raise DetachedWorkspaceError("WORKSPACE_REGULAR_FILE_REQUIRED")
            if self._secret_paths.inspect(entry.path).code != "ALLOW":
                continue
            content = self._read_blob(entry.oid)
            total += len(content)
            if total > MAX_WORKSPACE_BYTES:
                raise DetachedWorkspaceError("WORKSPACE_TOO_LARGE")
            regular.append((entry, content))
        existing = FilesystemRepositorySnapshot(self.root).entries()
        if existing:
            if any(item.path == ".git" or item.path.startswith(".git/") for item in existing):
                raise DetachedWorkspaceError("DETACHED_WORKSPACE_GIT_METADATA_DENIED")
            existing_paths = {item.path for item in existing}
            required_paths = {str(entry.path) for entry, _content in regular}
            if not required_paths.issubset(existing_paths):
                raise DetachedWorkspaceError("WORKSPACE_MATERIALIZATION_INCOMPLETE")
            return
        tree = self._tree()
        try:
            for entry, content in regular:
                node = tree.create_file(str(entry.path))
                tree.write_bytes(node, content)
            tree.assert_name_bindings()
        finally:
            tree.close()

    def snapshot(self) -> FilesystemRepositorySnapshot:
        self._ensure_root()
        return FilesystemRepositorySnapshot(self.root)

    def granted_workspace(self) -> GrantedWorkspaceAdapter:
        self._ensure_root()
        return GrantedWorkspaceAdapter(self.root, self._secret_paths)

    def expected_prestate(self, action: ToolActionEnvelope) -> ActionPreState:
        if not isinstance(action, RiskyAction):
            return ActionPreState()
        source_digest, source_mode = self._file_state(action.path)
        destination_absent = True
        if action.destination is not None:
            destination_digest, _ = self._file_state(action.destination)
            destination_absent = destination_digest is None
        return ActionPreState(
            source_digest=source_digest,
            source_mode=source_mode,
            destination_absent=destination_absent,
        )

    def apply_patch(
        self, lease: WorkspaceLease, patches: Mapping[str, bytes]
    ) -> PatchExecutionResult:
        if lease.state != "ACTIVE" or not patches:
            return PatchExecutionResult(code="LEASE_SCOPE_DENIED")
        tree = self._tree()
        try:
            for raw_path, raw_diff in patches.items():
                path = CanonicalPath.parse(raw_path)
                if self._secret_paths.inspect(path).code != "ALLOW":
                    return PatchExecutionResult(code="SECRET_PATH_DENIED")
                if not any(pattern.matches(path) for pattern in lease.write_globs):
                    return PatchExecutionResult(code="LEASE_SCOPE_DENIED")
                try:
                    unified_diff = raw_diff.decode("utf-8", errors="strict")
                    node = tree.try_open(str(path), "file")
                    original = (
                        b"" if node is None else tree.read_bytes(node, MAX_WORKSPACE_FILE_BYTES)
                    )
                    patched = GrantedWorkspaceAdapter._apply_unified_diff(original, unified_diff)
                except (UnicodeDecodeError, OSError, RepositoryUnsafeError, ValueError):
                    return PatchExecutionResult(code="LEASE_SCOPE_DENIED")
                if len(patched) > MAX_WORKSPACE_FILE_BYTES:
                    return PatchExecutionResult(code="LEASE_SCOPE_DENIED")
                if node is None:
                    node = tree.create_file(str(path))
                else:
                    node = tree.open_for_write(str(path))
                tree.write_bytes(node, patched)
            tree.assert_name_bindings()
        finally:
            tree.close()
        return PatchExecutionResult(code="PATCH_APPLIED", post_tree_digest=self.tree_digest())

    def tree_digest(self) -> Sha256DigestText:
        snapshot = self.snapshot()
        files: dict[str, str] = {}
        for entry in snapshot.entries():
            path = CanonicalPath.parse(entry.path)
            content = snapshot.read(path, MAX_WORKSPACE_FILE_BYTES)
            files[str(path)] = "sha256:" + sha256(content).hexdigest()
        return sha256_digest(canonical_json(files))

    def _file_state(self, raw_path: str) -> tuple[Sha256DigestText | None, int | None]:
        path = CanonicalPath.parse(raw_path)
        tree = self._tree()
        try:
            node = tree.try_open(str(path), "file")
            if node is None:
                return None, None
            content = tree.read_bytes(node, MAX_WORKSPACE_FILE_BYTES)
            mode = stat.S_IMODE(os.fstat(node.handle).st_mode) if os.name == "posix" else None
            tree.assert_name_bindings()
            return Sha256DigestText("sha256:" + sha256(content).hexdigest()), mode
        finally:
            tree.close()

    def _ensure_root(self) -> None:
        self.root.parent.mkdir(parents=True, exist_ok=True)
        try:
            metadata = self.root.lstat()
        except FileNotFoundError:
            self.root.mkdir()
            return
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise DetachedWorkspaceError("DETACHED_WORKSPACE_ROOT_UNSAFE")

    def _tree(self) -> StableHandleTree:
        backend = WindowsNoFollowBackend() if os.name == "nt" else PosixNoFollowBackend()
        return StableHandleTree(self.root, backend)

    def _run(self, operation: GitOperation) -> subprocess.CompletedProcess[bytes]:
        try:
            return self._runner.run_bytes(self._repository, operation)
        except RepositoryUnsafeError as error:
            raise DetachedWorkspaceError("WORKSPACE_GIT_READ_DENIED") from error

    def _read_blob(self, oid: GitOid) -> bytes:
        size_result = self._run(GitCatFileSize(oid))
        if size_result.returncode != 0 or not size_result.stdout.endswith(b"\n"):
            raise DetachedWorkspaceError("WORKSPACE_BLOB_SIZE_UNAVAILABLE")
        try:
            size = int(size_result.stdout[:-1].decode("ascii", errors="strict"))
        except (ValueError, UnicodeDecodeError) as error:
            raise DetachedWorkspaceError("WORKSPACE_BLOB_SIZE_INVALID") from error
        if size < 0 or size > MAX_WORKSPACE_FILE_BYTES:
            raise DetachedWorkspaceError("WORKSPACE_BLOB_TOO_LARGE")
        result = self._run(GitCatFileBlob(oid))
        content = result.stdout
        if result.returncode != 0 or len(content) != size:
            raise DetachedWorkspaceError("WORKSPACE_BLOB_READ_FAILED")
        return content


__all__ = ["DetachedWorkspace", "DetachedWorkspaceError"]
