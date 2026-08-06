from __future__ import annotations

import os
import stat
from collections.abc import Callable
from pathlib import Path
from typing import cast

from apexcrew.adapters.repository.no_follow import (
    HandleIdentity,
    NodeKind,
    OpenedNode,
    RepositoryUnsafeError,
)

_O_CLOEXEC = cast(int | None, getattr(os, "O_CLOEXEC", None))
_O_DIRECTORY = cast(int | None, getattr(os, "O_DIRECTORY", None))
_O_NOFOLLOW = cast(int | None, getattr(os, "O_NOFOLLOW", None))
_PREAD = cast(Callable[[int, int, int], bytes] | None, getattr(os, "pread", None))


def _required_open_flags() -> tuple[int, int, int]:
    if _O_CLOEXEC is None or _O_DIRECTORY is None or _O_NOFOLLOW is None:
        raise RepositoryUnsafeError("POSIX_OPENAT_REQUIRED")
    return _O_CLOEXEC, _O_DIRECTORY, _O_NOFOLLOW


class PosixNoFollowBackend:
    def _node(self, components: tuple[str, ...], handle: int, expected: NodeKind) -> OpenedNode:
        try:
            observed = os.fstat(handle)
        except OSError as fstat_error:
            self._close_rejected(handle, fstat_error)
            raise AssertionError("unreachable")
        kind: NodeKind
        if stat.S_ISREG(observed.st_mode):
            kind = "file"
        elif stat.S_ISDIR(observed.st_mode):
            kind = "directory"
        else:
            rejection = RepositoryUnsafeError("UNSUPPORTED_GIT_STORAGE_KIND")
            self._close_rejected(handle, rejection)
            raise AssertionError("unreachable")
        if kind != expected:
            rejection = RepositoryUnsafeError("GIT_STORAGE_KIND_CHANGED")
            self._close_rejected(handle, rejection)
            raise AssertionError("unreachable")
        return OpenedNode(
            components,
            handle,
            HandleIdentity("posix", observed.st_dev, observed.st_ino, kind),
        )

    def open_root_chain(self, root: Path) -> tuple[OpenedNode, ...]:
        if os.name != "posix" or os.open not in os.supports_dir_fd:
            raise RepositoryUnsafeError("POSIX_OPENAT_REQUIRED")
        if not root.is_absolute() or root.anchor != "/":
            raise RepositoryUnsafeError("ABSOLUTE_REPOSITORY_ROOT_REQUIRED")
        close_on_exec, directory, no_follow = _required_open_flags()
        flags = os.O_RDONLY | close_on_exec | directory | no_follow
        chain = [self._node((), os.open("/", flags), "directory")]
        try:
            components: tuple[str, ...] = ()
            for name in root.parts[1:]:
                components += (name,)
                handle = os.open(name, flags, dir_fd=chain[-1].handle)
                chain.append(self._node(components, handle, "directory"))
            return tuple(chain)
        except Exception as error:
            cleanup_error: OSError | None = None
            for node in reversed(chain):
                try:
                    os.close(node.handle)
                except OSError as close_error:
                    if cleanup_error is None:
                        cleanup_error = close_error
            if cleanup_error is not None:
                error.add_note(f"root chain cleanup failed: {cleanup_error}")
            raise

    @staticmethod
    def _close_rejected(handle: int, error: BaseException) -> None:
        try:
            os.close(handle)
        except OSError as cleanup_error:
            error.add_note(f"rejected handle cleanup failed: {cleanup_error}")
        raise error

    def open_child(self, parent: OpenedNode, name: str, kind: NodeKind) -> OpenedNode:
        close_on_exec, directory, no_follow = _required_open_flags()
        flags = os.O_RDONLY | close_on_exec | no_follow
        if kind == "directory":
            flags |= directory
        try:
            handle = os.open(name, flags, dir_fd=parent.handle)
        except OSError as error:
            raise RepositoryUnsafeError("NO_FOLLOW_OPEN_DENIED") from error
        return self._node(parent.components + (name,), handle, kind)

    def open_child_for_write(self, parent: OpenedNode, name: str) -> OpenedNode:
        close_on_exec, _directory, no_follow = _required_open_flags()
        try:
            handle = os.open(
                name,
                os.O_RDWR | close_on_exec | no_follow,
                dir_fd=parent.handle,
            )
        except OSError as error:
            raise RepositoryUnsafeError("NO_FOLLOW_WRITE_OPEN_DENIED") from error
        return self._node(parent.components + (name,), handle, "file")

    def open_child_for_delete(self, parent: OpenedNode, name: str, kind: NodeKind) -> OpenedNode:
        return self.open_child(parent, name, kind)

    def create_child_directory(self, parent: OpenedNode, name: str) -> OpenedNode:
        _required_open_flags()
        if os.mkdir not in os.supports_dir_fd:
            raise RepositoryUnsafeError("POSIX_OPENAT_REQUIRED")
        try:
            os.mkdir(name, 0o700, dir_fd=parent.handle)
        except OSError as error:
            raise RepositoryUnsafeError("NO_FOLLOW_CREATE_DENIED") from error
        return self.open_child(parent, name, "directory")

    def create_child_file(self, parent: OpenedNode, name: str) -> OpenedNode:
        close_on_exec, _directory, no_follow = _required_open_flags()
        if os.open not in os.supports_dir_fd:
            raise RepositoryUnsafeError("POSIX_OPENAT_REQUIRED")
        try:
            handle = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | close_on_exec | no_follow,
                0o600,
                dir_fd=parent.handle,
            )
        except OSError as error:
            raise RepositoryUnsafeError("NO_FOLLOW_CREATE_DENIED") from error
        return self._node(parent.components + (name,), handle, "file")

    def try_open_child(self, parent: OpenedNode, name: str, kind: NodeKind) -> OpenedNode | None:
        try:
            return self.open_child(parent, name, kind)
        except RepositoryUnsafeError as error:
            if isinstance(error.__cause__, FileNotFoundError):
                return None
            raise

    def try_open_child_any(self, parent: OpenedNode, name: str) -> OpenedNode | None:
        try:
            return self.open_child(parent, name, "directory")
        except RepositoryUnsafeError as directory_error:
            if isinstance(directory_error.__cause__, FileNotFoundError):
                return None
            if not isinstance(directory_error.__cause__, NotADirectoryError):
                raise
        return self.try_open_child(parent, name, "file")

    def read_bytes(self, node: OpenedNode, maximum: int) -> bytes:
        if _PREAD is None:
            raise RepositoryUnsafeError("POSIX_OPENAT_REQUIRED")
        size = os.fstat(node.handle).st_size
        if size > maximum:
            raise RepositoryUnsafeError("GIT_METADATA_TOO_LARGE")
        value = _PREAD(node.handle, maximum + 1, 0)
        if len(value) > maximum:
            raise RepositoryUnsafeError("GIT_METADATA_TOO_LARGE")
        return value

    def write_bytes(self, node: OpenedNode, value: bytes) -> None:
        if not stat.S_ISREG(os.fstat(node.handle).st_mode):
            raise RepositoryUnsafeError("GIT_STORAGE_KIND_CHANGED")
        try:
            os.lseek(node.handle, 0, os.SEEK_SET)
            os.ftruncate(node.handle, 0)
            written = 0
            while written < len(value):
                written += os.write(node.handle, value[written:])
            os.fsync(node.handle)
        except OSError as error:
            raise RepositoryUnsafeError("NO_FOLLOW_WRITE_DENIED") from error

    def list_names(self, node: OpenedNode, maximum: int) -> tuple[str, ...]:
        names = tuple(sorted(os.listdir(node.handle)))
        if len(names) > maximum or any(name in {"", ".", ".."} for name in names):
            raise RepositoryUnsafeError("GIT_DIRECTORY_INVENTORY_INVALID")
        return names

    @staticmethod
    def _require_identity(node: OpenedNode) -> None:
        try:
            observed = os.fstat(node.handle)
        except OSError as error:
            raise RepositoryUnsafeError("DELETE_IDENTITY_QUERY_FAILED") from error
        if observed.st_dev != node.identity.volume or observed.st_ino != node.identity.file_id:
            raise RepositoryUnsafeError("DELETE_IDENTITY_CHANGED")

    def unlink_child(self, parent: OpenedNode, name: str, expected: OpenedNode) -> None:
        self._require_identity(expected)
        if expected.identity.kind != "file" or "/" in name or "\\" in name:
            raise RepositoryUnsafeError("DELETE_TARGET_INVALID")
        try:
            os.unlink(name, dir_fd=parent.handle)
        except OSError as error:
            raise RepositoryUnsafeError("DELETE_FAILED") from error

    def remove_child_directory(self, parent: OpenedNode, name: str, expected: OpenedNode) -> None:
        self._require_identity(expected)
        if expected.identity.kind != "directory" or "/" in name or "\\" in name:
            raise RepositoryUnsafeError("DELETE_TARGET_INVALID")
        try:
            os.rmdir(name, dir_fd=parent.handle)
        except OSError as error:
            raise RepositoryUnsafeError("DELETE_FAILED") from error

    def close(self, node: OpenedNode) -> None:
        os.close(node.handle)
