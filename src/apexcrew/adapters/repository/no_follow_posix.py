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
        observed = os.fstat(handle)
        kind: NodeKind
        if stat.S_ISREG(observed.st_mode):
            kind = "file"
        elif stat.S_ISDIR(observed.st_mode):
            kind = "directory"
        else:
            os.close(handle)
            raise RepositoryUnsafeError("UNSUPPORTED_GIT_STORAGE_KIND")
        if kind != expected:
            os.close(handle)
            raise RepositoryUnsafeError("GIT_STORAGE_KIND_CHANGED")
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
        except BaseException:
            for node in reversed(chain):
                os.close(node.handle)
            raise

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

    def close(self, node: OpenedNode) -> None:
        os.close(node.handle)
