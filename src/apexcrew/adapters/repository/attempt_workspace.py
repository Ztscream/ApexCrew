from __future__ import annotations

import os
import stat
import subprocess
import threading
from collections.abc import Sequence
from dataclasses import dataclass
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
from apexcrew.adapters.repository.no_follow import (
    HandleIdentity,
    StableHandleTree,
)
from apexcrew.adapters.repository.no_follow import RepositoryUnsafeError as NoFollowError
from apexcrew.adapters.repository.no_follow_posix import PosixNoFollowBackend
from apexcrew.adapters.repository.no_follow_windows import WindowsNoFollowBackend
from apexcrew.domain.effects import canonical_json, sha256_digest
from apexcrew.domain.plan import CanonicalPath, GlobPattern, PathValidationError
from apexcrew.domain.policy import SecretPathPolicy
from apexcrew.domain.revisions import Sha256DigestText
from apexcrew.domain.tools import SanitizedSnapshotEntry
from apexcrew.domain.types import AttemptId, GitOid

MAX_TREE_OUTPUT_BYTES = 8 * 1024 * 1024
MAX_SNAPSHOT_ENTRIES = 2_000
MAX_WORKSPACE_FILE_BYTES = 16 * 1024 * 1024
MAX_WORKSPACE_BYTES = 256 * 1024 * 1024
MAX_CLEANUP_NODES = 8_000
MAX_CLEANUP_DEPTH = 256


class AttemptWorkspaceGitRunner(Protocol):
    def run_bytes(
        self, repository: RepositoryInstance, operation: GitOperation
    ) -> subprocess.CompletedProcess[bytes]: ...


class AttemptWorkspaceError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class MaterializedWorkspace:
    root: Path
    entries: tuple[SanitizedSnapshotEntry, ...]
    tree_digest: Sha256DigestText
    identity_chain: tuple[HandleIdentity, ...] = ()


@dataclass(frozen=True, slots=True)
class _TreeEntry:
    mode: bytes
    object_type: bytes
    object_id: bytes
    path: CanonicalPath


def _parse_tree(raw: bytes) -> tuple[_TreeEntry, ...]:
    if len(raw) > MAX_TREE_OUTPUT_BYTES or (raw and not raw.endswith(b"\0")):
        raise AttemptWorkspaceError("TREE_OUTPUT_INVALID")
    entries: list[_TreeEntry] = []
    for record in raw[:-1].split(b"\0") if raw else ():
        metadata, separator, encoded_path = record.partition(b"\t")
        fields = metadata.split(b" ")
        if not separator or len(fields) != 3:
            raise AttemptWorkspaceError("TREE_OUTPUT_INVALID")
        try:
            path = CanonicalPath.parse(encoded_path.decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, PathValidationError) as error:
            raise AttemptWorkspaceError("TREE_PATH_INVALID") from error
        entries.append(_TreeEntry(fields[0], fields[1], fields[2], path))
    if len({str(entry.path) for entry in entries}) != len(entries):
        raise AttemptWorkspaceError("TREE_DUPLICATE_PATH")
    ordered = tuple(sorted(entries, key=lambda entry: str(entry.path)))
    if tuple(entry.path for entry in entries) != tuple(entry.path for entry in ordered):
        raise AttemptWorkspaceError("TREE_ORDER_INVALID")
    return ordered


def _attempt_component(attempt_id: AttemptId) -> str:
    value = str(attempt_id)
    if (
        not value
        or len(value) > 128
        or any(
            character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-"
            for character in value
        )
    ):
        raise AttemptWorkspaceError("ATTEMPT_ID_INVALID")
    return value


class AttemptWorkspaceAdapter:
    """Materialize one pinned Git tree into separate bounded attempt roots."""

    def __init__(
        self,
        repository: RepositoryInstance,
        runner: AttemptWorkspaceGitRunner,
        data_root: Path,
        secret_paths: SecretPathPolicy,
    ) -> None:
        if not data_root.is_absolute():
            raise ValueError("ATTEMPT_DATA_ROOT_MUST_BE_ABSOLUTE")
        self._repository = repository
        self._runner = runner
        self._data_root = data_root
        self._secret_paths = secret_paths
        self._materialization_lock = threading.RLock()
        self._context_cache: dict[
            tuple[str, str, tuple[str, ...], tuple[str, ...]], MaterializedWorkspace
        ] = {}

    def materialize_context(
        self,
        *,
        attempt_id: AttemptId,
        base_oid: GitOid,
        read_globs: Sequence[GlobPattern],
        dependency_globs: Sequence[GlobPattern],
    ) -> MaterializedWorkspace:
        read_values = tuple(pattern.value for pattern in read_globs)
        dependency_values = tuple(pattern.value for pattern in dependency_globs)
        cache_key = (str(attempt_id), str(base_oid), read_values, dependency_values)
        with self._materialization_lock:
            cached = self._context_cache.get(cache_key)
            if cached is not None:
                self.verify_workspace_identity(cached)
                return cached
            for key in tuple(self._context_cache):
                if key[0] == cache_key[0]:
                    del self._context_cache[key]
            workspace = self._materialize(
                attempt_id=attempt_id,
                base_oid=base_oid,
                globs=(*read_globs, *dependency_globs),
                kind="context",
            )
            self._context_cache[cache_key] = workspace
            return workspace

    def materialize_check(
        self,
        *,
        attempt_id: AttemptId,
        base_oid: GitOid,
        input_globs: Sequence[GlobPattern],
        write_globs: Sequence[GlobPattern],
        workspace_key: str | None = None,
        reject_existing: bool = False,
    ) -> MaterializedWorkspace:
        kind = (
            "check"
            if workspace_key is None
            else "check-" + sha256(workspace_key.encode("utf-8")).hexdigest()
        )
        return self._materialize(
            attempt_id=attempt_id,
            base_oid=base_oid,
            globs=(*input_globs, *write_globs),
            kind=kind,
            reject_existing=reject_existing,
        )

    def verify_workspace_identity(self, workspace: MaterializedWorkspace) -> None:
        if not workspace.identity_chain:
            return
        try:
            relative = workspace.root.relative_to(self._data_root).as_posix()
        except ValueError as error:
            raise AttemptWorkspaceError("WORKSPACE_ROOT_BINDING_INVALID") from error
        with self._materialization_lock:
            tree = self._tree()
            try:
                observed = tree.identity_chain(relative)
                if observed != workspace.identity_chain:
                    raise AttemptWorkspaceError("WORKSPACE_IDENTITY_CHANGED")
                tree.assert_name_bindings()
            except (NoFollowError, OSError, ValueError) as error:
                raise AttemptWorkspaceError("WORKSPACE_IDENTITY_CHANGED") from error
            finally:
                tree.close()

    def close(self) -> None:
        """The repository and Git runner are borrowed from the composition root."""

    def _materialize(
        self,
        *,
        attempt_id: AttemptId,
        base_oid: GitOid,
        globs: Sequence[GlobPattern],
        kind: str,
        reject_existing: bool = False,
    ) -> MaterializedWorkspace:
        with self._materialization_lock:
            component = _attempt_component(attempt_id)
            entries_and_content = self._load_manifest(base_oid, globs)
            root = self._workspace_root(component, kind)
            self._ensure_data_root()
            tree = self._tree()
            relative_root = f"attempts/{component}/{kind}"
            try:
                tree.ensure_directory("attempts")
                tree.ensure_directory(f"attempts/{component}")
                if reject_existing and tree.try_open_any(relative_root) is not None:
                    raise AttemptWorkspaceError("WORKSPACE_CACHE_STATE_UNAVAILABLE")
                self._remove_existing_directory(tree, relative_root)
                tree.ensure_directory(relative_root)
                for entry, content in entries_and_content:
                    node = tree.create_file(f"{relative_root}/{entry.path}")
                    tree.write_bytes(node, content)
                tree.assert_name_bindings()
                identity_chain = tree.identity_chain(relative_root)
            except (NoFollowError, OSError, ValueError) as error:
                raise AttemptWorkspaceError("WORKSPACE_WRITE_DENIED") from error
            finally:
                tree.close()
            entries = tuple(entry for entry, _content in entries_and_content)
            return MaterializedWorkspace(
                root=root,
                entries=entries,
                tree_digest=self._tree_digest(entries_and_content),
                identity_chain=identity_chain,
            )

    def _load_manifest(
        self, base_oid: GitOid, globs: Sequence[GlobPattern]
    ) -> tuple[tuple[SanitizedSnapshotEntry, bytes], ...]:
        result = self._run(GitLsTreeRecursive(base_oid))
        if result.returncode != 0:
            raise AttemptWorkspaceError("GIT_TREE_READ_FAILED")
        selected: list[_TreeEntry] = []
        for entry in _parse_tree(result.stdout):
            if any(pattern.matches(entry.path) for pattern in globs):
                selected.append(entry)
        folded_paths = {str(entry.path).casefold() for entry in selected}
        if len(folded_paths) != len(selected):
            raise AttemptWorkspaceError("CASEFOLD_PATH_COLLISION")
        if len(selected) > MAX_SNAPSHOT_ENTRIES:
            raise AttemptWorkspaceError("SNAPSHOT_ENTRY_LIMIT")

        materialized: list[tuple[SanitizedSnapshotEntry, bytes]] = []
        total_bytes = 0
        for entry in selected:
            if entry.mode == b"120000":
                raise AttemptWorkspaceError("SYMLINK_MODE_DENIED")
            if entry.mode == b"160000":
                raise AttemptWorkspaceError("SUBMODULE_MODE_DENIED")
            if entry.mode not in {b"100644", b"100755"} or entry.object_type != b"blob":
                raise AttemptWorkspaceError("REGULAR_BLOB_REQUIRED")
            if self._secret_paths.inspect(entry.path).code != "ALLOW":
                raise AttemptWorkspaceError("SECRET_PATH_DENIED")
            object_id = self._object_id(entry.object_id)
            content = self._read_blob(object_id)
            if len(content) > MAX_WORKSPACE_FILE_BYTES:
                raise AttemptWorkspaceError("WORKSPACE_FILE_TOO_LARGE")
            total_bytes += len(content)
            if total_bytes > MAX_WORKSPACE_BYTES:
                raise AttemptWorkspaceError("WORKSPACE_TOO_LARGE")
            materialized.append(
                (
                    SanitizedSnapshotEntry(
                        path=str(entry.path),
                        kind="regular",
                        content_digest=Sha256DigestText("sha256:" + sha256(content).hexdigest()),
                    ),
                    content,
                )
            )
        return tuple(materialized)

    def _read_blob(self, object_id: GitOid) -> bytes:
        size_result = self._run(GitCatFileSize(object_id))
        if size_result.returncode != 0 or not size_result.stdout.endswith(b"\n"):
            raise AttemptWorkspaceError("GIT_BLOB_SIZE_UNAVAILABLE")
        try:
            size = int(size_result.stdout[:-1].decode("ascii", errors="strict"))
        except (UnicodeDecodeError, ValueError) as error:
            raise AttemptWorkspaceError("GIT_BLOB_SIZE_INVALID") from error
        if size < 0 or size > MAX_WORKSPACE_FILE_BYTES:
            raise AttemptWorkspaceError("WORKSPACE_FILE_TOO_LARGE")
        result = self._run(GitCatFileBlob(object_id))
        if (
            result.returncode != 0
            or not isinstance(result.stdout, bytes)
            or len(result.stdout) != size
        ):
            raise AttemptWorkspaceError("GIT_BLOB_READ_FAILED")
        return result.stdout

    @staticmethod
    def _object_id(raw: bytes) -> GitOid:
        if len(raw) != 40 or any(value not in b"0123456789abcdef" for value in raw):
            raise AttemptWorkspaceError("GIT_OBJECT_ID_INVALID")
        try:
            return GitOid(raw.decode("ascii", errors="strict"))
        except UnicodeDecodeError as error:
            raise AttemptWorkspaceError("GIT_OBJECT_ID_INVALID") from error

    def _run(self, operation: GitOperation) -> subprocess.CompletedProcess[bytes]:
        try:
            return self._runner.run_bytes(self._repository, operation)
        except (RepositoryUnsafeError, OSError) as error:
            raise AttemptWorkspaceError("GIT_READ_DENIED") from error

    def _workspace_root(self, attempt_component: str, kind: str) -> Path:
        return self._data_root / "attempts" / attempt_component / kind

    def _ensure_data_root(self) -> None:
        if not self._data_root.is_absolute():
            raise AttemptWorkspaceError("ATTEMPT_DATA_ROOT_MUST_BE_ABSOLUTE")
        current = Path(self._data_root.anchor)
        for component in self._data_root.parts[1:]:
            current /= component
            try:
                metadata = current.lstat()
            except FileNotFoundError:
                try:
                    current.mkdir()
                except OSError as error:
                    raise AttemptWorkspaceError("ATTEMPT_DATA_ROOT_UNSAFE") from error
                continue
            except OSError as error:
                raise AttemptWorkspaceError("ATTEMPT_DATA_ROOT_UNSAFE") from error
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise AttemptWorkspaceError("ATTEMPT_DATA_ROOT_UNSAFE")

    def _tree(self) -> StableHandleTree:
        backend = WindowsNoFollowBackend() if os.name == "nt" else PosixNoFollowBackend()
        try:
            return StableHandleTree(self._data_root, backend)
        except (NoFollowError, OSError) as error:
            raise AttemptWorkspaceError("ATTEMPT_DATA_ROOT_UNSAFE") from error

    def _remove_existing_directory(self, tree: StableHandleTree, relative: str) -> None:
        node = tree.try_open_any(relative)
        if node is None:
            return
        if node.identity.kind != "directory":
            raise AttemptWorkspaceError("WORKSPACE_ROOT_UNSAFE")
        pending: list[tuple[str, HandleIdentity, tuple[str, ...], int]] = [
            (relative, node.identity, tree.list_names(node, MAX_SNAPSHOT_ENTRIES), 0)
        ]
        visited = 0
        while pending:
            current, identity, names, index = pending[-1]
            if index == len(names):
                tree.remove_directory(current, identity)
                pending.pop()
                continue
            pending[-1] = (current, identity, names, index + 1)
            visited += 1
            if visited > MAX_CLEANUP_NODES:
                raise AttemptWorkspaceError("WORKSPACE_CLEANUP_LIMIT")
            child_relative = f"{current}/{names[index]}"
            child = tree.try_open_any(child_relative)
            if child is None:
                raise AttemptWorkspaceError("WORKSPACE_ENTRY_CHANGED")
            if child.identity.kind == "directory":
                if len(pending) >= MAX_CLEANUP_DEPTH:
                    raise AttemptWorkspaceError("WORKSPACE_CLEANUP_DEPTH")
                pending.append(
                    (
                        child_relative,
                        child.identity,
                        tree.list_names(child, MAX_SNAPSHOT_ENTRIES),
                        0,
                    )
                )
            else:
                tree.remove_file(child_relative, child.identity)

    @staticmethod
    def _tree_digest(
        entries_and_content: Sequence[tuple[SanitizedSnapshotEntry, bytes]],
    ) -> Sha256DigestText:
        files = {
            entry.path: "sha256:" + sha256(content).hexdigest()
            for entry, content in entries_and_content
        }
        return sha256_digest(canonical_json(files))


__all__ = [
    "AttemptWorkspaceAdapter",
    "AttemptWorkspaceError",
    "MaterializedWorkspace",
]
