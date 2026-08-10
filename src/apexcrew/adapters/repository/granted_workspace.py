from __future__ import annotations

import os
import stat
import tempfile
from collections.abc import Callable
from hashlib import sha256
from pathlib import Path
from typing import Literal

from apexcrew.adapters.repository.no_follow import (
    OpenedNode,
    RepositoryUnsafeError,
    StableHandleTree,
)
from apexcrew.adapters.repository.no_follow_posix import PosixNoFollowBackend
from apexcrew.adapters.repository.no_follow_windows import WindowsNoFollowBackend
from apexcrew.adapters.repository.unified_diff import apply_unified_diff, reverse_unified_diff
from apexcrew.domain.actions import RiskyAction, ToolActionEnvelope
from apexcrew.domain.admission import RepositoryEffectUncertain
from apexcrew.domain.effects import canonical_json, sha256_digest
from apexcrew.domain.plan import CanonicalPath
from apexcrew.domain.policy import SecretPathPolicy
from apexcrew.domain.revisions import Sha256DigestText
from apexcrew.domain.tools import ActionPreState, GrantedActionObservation, ToolResult


class GrantedWorkspaceAdapter:
    def __init__(
        self,
        root: Path,
        secret_paths: SecretPathPolicy,
        *,
        before_mutation: Callable[[], None] | None = None,
    ) -> None:
        self._root = root.resolve(strict=True)
        self._secret_paths = secret_paths
        self._before_mutation = before_mutation

    def _authorized_parts(self, raw: str, *, require_protected: bool = False) -> tuple[str, ...]:
        canonical = CanonicalPath.parse(raw)
        if self._secret_paths.inspect(canonical).code != "ALLOW":
            raise RepositoryUnsafeError("GRANTED_SECRET_PATH_DENIED")
        protected = str(canonical) == ".gitlab-ci.yml" or str(canonical).startswith(
            ".github/workflows/"
        )
        if require_protected != protected:
            raise RepositoryUnsafeError("GRANTED_PROTECTED_SCOPE_MISMATCH")
        return tuple(str(canonical).split("/"))

    def _stable_handles(self) -> StableHandleTree:
        backend = PosixNoFollowBackend() if os.name == "posix" else WindowsNoFollowBackend()
        return StableHandleTree(self._root, backend)

    def _guard_mutation(self, tree: StableHandleTree) -> None:
        if self._before_mutation is not None:
            self._before_mutation()
        tree.assert_name_bindings()

    @staticmethod
    def _parent_and_name(tree: StableHandleTree, parts: tuple[str, ...]) -> tuple[OpenedNode, str]:
        parent = tree.root_node if len(parts) == 1 else tree.open("/".join(parts[:-1]), "directory")
        return parent, parts[-1]

    @staticmethod
    def _handle_regular(
        tree: StableHandleTree, parts: tuple[str, ...]
    ) -> tuple[OpenedNode, bytes, int]:
        node = tree.open("/".join(parts), "file")
        content = tree.read_bytes(node, 16 * 1024 * 1024)
        if os.name != "posix":
            raise RepositoryUnsafeError("GRANTED_HANDLE_MUTATION_UNSUPPORTED")
        mode = stat.S_IMODE(os.fstat(node.handle).st_mode)
        return node, content, mode

    @staticmethod
    def _is_link_or_reparse(metadata: os.stat_result) -> bool:
        attributes = getattr(metadata, "st_file_attributes", 0)
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        return stat.S_ISLNK(metadata.st_mode) or bool(attributes & reparse)

    def _path(self, raw: str, *, require_protected: bool = False) -> Path:
        canonical = CanonicalPath.parse(raw)
        if self._secret_paths.inspect(canonical).code != "ALLOW":
            raise RepositoryUnsafeError("GRANTED_SECRET_PATH_DENIED")
        protected = str(canonical) == ".gitlab-ci.yml" or str(canonical).startswith(
            ".github/workflows/"
        )
        if require_protected != protected:
            raise RepositoryUnsafeError("GRANTED_PROTECTED_SCOPE_MISMATCH")
        current = self._root
        for component in str(canonical).split("/")[:-1]:
            current = current / component
            metadata = current.lstat()
            if self._is_link_or_reparse(metadata) or not stat.S_ISDIR(metadata.st_mode):
                raise RepositoryUnsafeError("GRANTED_NO_FOLLOW_DENIED")
        return self._root.joinpath(*str(canonical).split("/"))

    def _regular(self, path: Path) -> tuple[bytes, int]:
        metadata = path.lstat()
        if self._is_link_or_reparse(metadata) or not stat.S_ISREG(metadata.st_mode):
            raise RepositoryUnsafeError("GRANTED_REGULAR_FILE_REQUIRED")
        content = path.read_bytes()
        observed = path.lstat()
        if (
            observed.st_dev,
            observed.st_ino,
            observed.st_mode,
            observed.st_size,
            observed.st_mtime_ns,
            observed.st_ctime_ns,
        ) != (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_mode,
            metadata.st_size,
            metadata.st_mtime_ns,
            metadata.st_ctime_ns,
        ):
            raise RepositoryUnsafeError("GRANTED_FILE_CHANGED_DURING_READ")
        return content, stat.S_IMODE(metadata.st_mode)

    def _regular_if_present(self, path: Path) -> tuple[bytes, int] | None:
        try:
            return self._regular(path)
        except FileNotFoundError:
            return None

    @staticmethod
    def _digest(content: bytes) -> Sha256DigestText:
        return Sha256DigestText("sha256:" + sha256(content).hexdigest())

    @classmethod
    def _matches_source(cls, content: bytes, mode: int, expected: ActionPreState) -> bool:
        return (
            expected.source_digest is not None
            and cls._digest(content) == expected.source_digest
            and (expected.source_mode is None or mode == expected.source_mode)
        )

    @staticmethod
    def _apply_unified_diff(original: bytes, unified_diff: str) -> bytes:
        return apply_unified_diff(original, unified_diff)

    @staticmethod
    def _reverse_unified_diff(current: bytes, unified_diff: str) -> bytes:
        return reverse_unified_diff(current, unified_diff)

    @staticmethod
    def _observation_digest(
        action: RiskyAction,
        state: str,
        source_digest: str | None,
        source_mode: int | None,
        destination_digest: str | None,
    ) -> Sha256DigestText:
        return sha256_digest(
            canonical_json(
                {
                    "destination_digest": destination_digest,
                    "operation": action.operation,
                    "path": action.path,
                    "source_digest": source_digest,
                    "source_mode": source_mode,
                    "state": state,
                }
            )
        )

    def observe(self, action: RiskyAction, expected: ActionPreState) -> GrantedActionObservation:
        try:
            source = self._path(
                action.path, require_protected=action.operation == "protected_patch"
            )
            source_entry = self._regular_if_present(source)
            source_exists = source_entry is not None
            source_content: bytes | None = None
            source_mode: int | None = None
            if source_entry is not None:
                source_content, source_mode = source_entry
            destination_content: bytes | None = None
            destination_exists = False
            if action.destination is not None:
                destination = self._path(action.destination)
                destination_entry = self._regular_if_present(destination)
                destination_exists = destination_entry is not None
                if destination_entry is not None:
                    destination_content, _ = destination_entry
            source_digest = None if source_content is None else str(self._digest(source_content))
            destination_digest = (
                None if destination_content is None else str(self._digest(destination_content))
            )
            exact_pre = (
                source_content is not None
                and source_mode is not None
                and self._matches_source(source_content, source_mode, expected)
                and (
                    action.destination is None
                    or expected.destination_absent
                    and not destination_exists
                )
            )
            post_result: ToolResult | None = None
            exact_post = False
            if action.operation == "delete":
                exact_post = not source_exists
                if exact_post:
                    post_result = ToolResult(code="DELETED")
            elif action.operation == "rename":
                exact_post = (
                    not source_exists
                    and expected.source_digest is not None
                    and destination_content is not None
                    and self._digest(destination_content) == expected.source_digest
                )
                if exact_post:
                    post_result = ToolResult(code="RENAMED", content_digest=expected.source_digest)
            elif action.operation == "set_executable" and source_content is not None:
                desired = bool(action.executable)
                exact_post = (
                    self._digest(source_content) == expected.source_digest
                    and bool(source_mode and source_mode & 0o111) == desired
                )
                if exact_post:
                    post_result = ToolResult(
                        code="EXECUTABLE_CHANGED",
                        content_digest=self._digest(source_content),
                    )
            elif action.operation == "protected_patch" and source_content is not None:
                assert action.unified_diff is not None
                if expected.source_digest is not None and not exact_pre:
                    try:
                        previous_content = self._reverse_unified_diff(
                            source_content, action.unified_diff
                        )
                    except RepositoryUnsafeError:
                        previous_content = None
                    exact_post = (
                        previous_content is not None
                        and self._digest(previous_content) == expected.source_digest
                    )
                if exact_post:
                    post_result = ToolResult(
                        code="PROTECTED_PATCH_APPLIED",
                        content_digest=self._digest(source_content),
                    )
            state: Literal["EXACT_PRE", "EXACT_POST", "THIRD"] = (
                "EXACT_PRE" if exact_pre else "EXACT_POST" if exact_post else "THIRD"
            )
            return GrantedActionObservation(
                state=state,
                digest=self._observation_digest(
                    action,
                    state,
                    source_digest,
                    source_mode,
                    destination_digest,
                ),
                post_result=post_result,
            )
        except (OSError, RepositoryUnsafeError, ValueError):
            return GrantedActionObservation(
                state="UNAVAILABLE",
                digest=self._observation_digest(action, "UNAVAILABLE", None, None, None),
            )

    def expected_prestate(self, action: ToolActionEnvelope) -> ActionPreState:
        if not isinstance(action, RiskyAction):
            return ActionPreState()
        source = self._path(action.path, require_protected=action.operation == "protected_patch")
        source_entry = self._regular_if_present(source)
        source_digest: Sha256DigestText | None = None
        source_mode: int | None = None
        if source_entry is not None:
            source_content, source_mode = source_entry
            source_digest = self._digest(source_content)
        destination_absent = True
        if action.destination is not None:
            destination = self._path(action.destination)
            destination_absent = self._regular_if_present(destination) is None
        return ActionPreState(
            source_digest=source_digest,
            source_mode=source_mode,
            destination_absent=destination_absent,
        )

    def delete_regular_file(self, action: RiskyAction, expected: ActionPreState) -> ToolResult:
        if os.name != "posix":
            try:
                path = self._path(action.path)
                content, mode = self._regular(path)
            except (OSError, RepositoryUnsafeError, ValueError):
                return ToolResult.indeterminate("GRANTED_PREFLIGHT_DENIED")
            if action.operation != "delete" or not self._matches_source(content, mode, expected):
                return ToolResult.indeterminate("GRANTED_PRESTATE_MISMATCH")
            try:
                path.unlink()
            except OSError as error:
                raise RepositoryEffectUncertain("GRANTED_DELETE_UNCERTAIN") from error
            return ToolResult(code="DELETED", content_digest=self._digest(content))
        tree: StableHandleTree | None = None
        try:
            parts = self._authorized_parts(action.path)
            tree = self._stable_handles()
            _node, content, mode = self._handle_regular(tree, parts)
        except (OSError, RepositoryUnsafeError, ValueError):
            if tree is not None:
                tree.close()
            return ToolResult.indeterminate("GRANTED_PREFLIGHT_DENIED")
        if action.operation != "delete" or not self._matches_source(content, mode, expected):
            tree.close()
            return ToolResult.indeterminate("GRANTED_PRESTATE_MISMATCH")
        parent, name = self._parent_and_name(tree, parts)
        try:
            self._guard_mutation(tree)
            os.unlink(name, dir_fd=parent.handle)
        except OSError as error:
            tree.close()
            raise RepositoryEffectUncertain("GRANTED_DELETE_UNCERTAIN") from error
        except RepositoryUnsafeError:
            tree.close()
            return ToolResult.indeterminate("GRANTED_PRESTATE_CHANGED")
        tree.close()
        return ToolResult(code="DELETED", content_digest=self._digest(content))

    def rename_regular_file(self, action: RiskyAction, expected: ActionPreState) -> ToolResult:
        if action.destination is None:
            return ToolResult.indeterminate("GRANTED_RENAME_DESTINATION_REQUIRED")
        if os.name != "posix":
            try:
                source = self._path(action.path)
                destination = self._path(action.destination)
                content, mode = self._regular(source)
                destination_entry = self._regular_if_present(destination)
            except (OSError, RepositoryUnsafeError, ValueError):
                return ToolResult.indeterminate("GRANTED_PREFLIGHT_DENIED")
            if (
                action.operation != "rename"
                or not expected.destination_absent
                or destination_entry is not None
                or not self._matches_source(content, mode, expected)
            ):
                return ToolResult.indeterminate("GRANTED_PRESTATE_MISMATCH")
            try:
                source.rename(destination)
            except OSError as error:
                raise RepositoryEffectUncertain("GRANTED_RENAME_UNCERTAIN") from error
            return ToolResult(code="RENAMED", content_digest=self._digest(content))
        tree: StableHandleTree | None = None
        try:
            source_parts = self._authorized_parts(action.path)
            destination_parts = self._authorized_parts(action.destination)
            tree = self._stable_handles()
            _source_node, content, mode = self._handle_regular(tree, source_parts)
            destination_handle = tree.try_open("/".join(destination_parts), "file")
            destination_exists = destination_handle is not None
        except (OSError, RepositoryUnsafeError, ValueError):
            if tree is not None:
                tree.close()
            return ToolResult.indeterminate("GRANTED_PREFLIGHT_DENIED")
        if (
            action.operation != "rename"
            or not expected.destination_absent
            or destination_exists
            or not self._matches_source(content, mode, expected)
        ):
            tree.close()
            return ToolResult.indeterminate("GRANTED_PRESTATE_MISMATCH")
        source_parent, source_name = self._parent_and_name(tree, source_parts)
        destination_parent, destination_name = self._parent_and_name(tree, destination_parts)
        try:
            self._guard_mutation(tree)
            os.rename(
                source_name,
                destination_name,
                src_dir_fd=source_parent.handle,
                dst_dir_fd=destination_parent.handle,
            )
        except OSError as error:
            tree.close()
            raise RepositoryEffectUncertain("GRANTED_RENAME_UNCERTAIN") from error
        except RepositoryUnsafeError:
            tree.close()
            return ToolResult.indeterminate("GRANTED_PRESTATE_CHANGED")
        tree.close()
        return ToolResult(code="RENAMED", content_digest=self._digest(content))

    def set_executable(self, action: RiskyAction, expected: ActionPreState) -> ToolResult:
        if os.name != "posix":
            try:
                path = self._path(action.path)
                content, mode = self._regular(path)
            except (OSError, RepositoryUnsafeError, ValueError):
                return ToolResult.indeterminate("GRANTED_PREFLIGHT_DENIED")
            if (
                action.operation != "set_executable"
                or action.executable is None
                or not self._matches_source(content, mode, expected)
            ):
                return ToolResult.indeterminate("GRANTED_PRESTATE_MISMATCH")
            if os.name == "nt":
                return ToolResult.indeterminate("GRANTED_EXECUTABLE_MODE_UNSUPPORTED")
            target_mode = mode | 0o111 if action.executable else mode & ~0o111
            try:
                path.chmod(target_mode, follow_symlinks=False)
            except OSError as error:
                raise RepositoryEffectUncertain("GRANTED_EXECUTABLE_CHANGE_UNCERTAIN") from error
            return ToolResult(code="EXECUTABLE_CHANGED", content_digest=self._digest(content))
        tree: StableHandleTree | None = None
        try:
            parts = self._authorized_parts(action.path)
            tree = self._stable_handles()
            _node, content, mode = self._handle_regular(tree, parts)
        except (OSError, RepositoryUnsafeError, ValueError):
            if tree is not None:
                tree.close()
            return ToolResult.indeterminate("GRANTED_PREFLIGHT_DENIED")
        if (
            action.operation != "set_executable"
            or action.executable is None
            or not self._matches_source(content, mode, expected)
        ):
            tree.close()
            return ToolResult.indeterminate("GRANTED_PRESTATE_MISMATCH")
        if os.name == "nt":
            tree.close()
            return ToolResult.indeterminate("GRANTED_EXECUTABLE_MODE_UNSUPPORTED")
        target_mode = mode | 0o111 if action.executable else mode & ~0o111
        try:
            parent, _ = self._parent_and_name(tree, parts)
            self._guard_mutation(tree)
            os.chmod(parts[-1], target_mode, dir_fd=parent.handle, follow_symlinks=False)
        except OSError as error:
            tree.close()
            raise RepositoryEffectUncertain("GRANTED_EXECUTABLE_CHANGE_UNCERTAIN") from error
        except RepositoryUnsafeError:
            tree.close()
            return ToolResult.indeterminate("GRANTED_PRESTATE_CHANGED")
        tree.close()
        return ToolResult(code="EXECUTABLE_CHANGED", content_digest=self._digest(content))

    def apply_protected_patch(self, action: RiskyAction, expected: ActionPreState) -> ToolResult:
        if os.name != "posix":
            try:
                path = self._path(action.path, require_protected=True)
                content, mode = self._regular(path)
                if action.operation != "protected_patch" or action.unified_diff is None:
                    return ToolResult.indeterminate("GRANTED_PRESTATE_MISMATCH")
                if not self._matches_source(content, mode, expected):
                    return ToolResult.indeterminate("GRANTED_PRESTATE_MISMATCH")
                patched = self._apply_unified_diff(content, action.unified_diff)
            except (OSError, RepositoryUnsafeError, ValueError):
                return ToolResult.indeterminate("GRANTED_PREFLIGHT_DENIED")
            temporary_fd: int | None = None
            temporary_path: str | None = None
            applied = False
            try:
                temporary_fd, temporary_path = tempfile.mkstemp(
                    prefix=".apexcrew-granted-", dir=path.parent
                )
                with os.fdopen(temporary_fd, "wb") as stream:
                    temporary_fd = None
                    stream.write(patched)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.chmod(temporary_path, mode)
                os.replace(temporary_path, path)
                applied = True
                temporary_path = None
            except OSError as error:
                if applied or not path.exists():
                    raise RepositoryEffectUncertain("GRANTED_PROTECTED_PATCH_UNCERTAIN") from error
                return ToolResult.indeterminate("GRANTED_PROTECTED_PATCH_FAILED")
            finally:
                if temporary_fd is not None:
                    try:
                        os.close(temporary_fd)
                    except OSError:
                        pass
                if temporary_path is not None:
                    try:
                        os.unlink(temporary_path)
                    except OSError:
                        pass
            return ToolResult(code="PROTECTED_PATCH_APPLIED", content_digest=self._digest(patched))
        tree: StableHandleTree | None = None
        try:
            parts = self._authorized_parts(action.path, require_protected=True)
            tree = self._stable_handles()
            _node, content, mode = self._handle_regular(tree, parts)
            if action.operation != "protected_patch" or action.unified_diff is None:
                tree.close()
                return ToolResult.indeterminate("GRANTED_PRESTATE_MISMATCH")
            if not self._matches_source(content, mode, expected):
                tree.close()
                return ToolResult.indeterminate("GRANTED_PRESTATE_MISMATCH")
            patched = self._apply_unified_diff(content, action.unified_diff)
        except (OSError, RepositoryUnsafeError, ValueError):
            if tree is not None:
                tree.close()
            return ToolResult.indeterminate("GRANTED_PREFLIGHT_DENIED")
        parent, name = self._parent_and_name(tree, parts)
        temporary_name: str | None = None
        applied = False
        try:
            self._guard_mutation(tree)
            for attempt in range(16):
                candidate = f".apexcrew-granted-{os.getpid()}-{attempt}"
                try:
                    fd = os.open(
                        candidate,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                        mode=mode,
                        dir_fd=parent.handle,
                    )
                    temporary_name = candidate
                    break
                except FileExistsError:
                    continue
            else:
                raise OSError("temporary name exhaustion")
            with os.fdopen(fd, "wb") as stream:
                stream.write(patched)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary_name, mode, dir_fd=parent.handle, follow_symlinks=False)
            os.replace(temporary_name, name, src_dir_fd=parent.handle, dst_dir_fd=parent.handle)
            applied = True
            temporary_name = None
        except OSError as error:
            if applied:
                tree.close()
                raise RepositoryEffectUncertain("GRANTED_PROTECTED_PATCH_UNCERTAIN") from error
            tree.close()
            return ToolResult.indeterminate("GRANTED_PROTECTED_PATCH_FAILED")
        finally:
            if temporary_name is not None:
                try:
                    os.unlink(temporary_name, dir_fd=parent.handle)
                except OSError:
                    pass
        tree.close()
        return ToolResult(code="PROTECTED_PATCH_APPLIED", content_digest=self._digest(patched))
