from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from apexcrew.adapters.repository.no_follow import RepositoryUnsafeError, StableHandleTree
from apexcrew.adapters.repository.no_follow_posix import PosixNoFollowBackend
from apexcrew.adapters.repository.no_follow_windows import WindowsNoFollowBackend
from apexcrew.domain.plan import CanonicalPath
from apexcrew.domain.tools import (
    SnapshotEntry,
    SnapshotNoFollowDenied,
    SnapshotUnavailable,
)

MAX_SNAPSHOT_ENTRIES = 2_000


class MemoryRepositorySnapshot:
    def __init__(self, files: Mapping[str, bytes]) -> None:
        self._files = {CanonicalPath.parse(path): bytes(content) for path, content in files.items()}

    def entries(self) -> tuple[SnapshotEntry, ...]:
        return tuple(
            SnapshotEntry(path=str(path), size=len(content))
            for path, content in sorted(self._files.items(), key=lambda item: str(item[0]))
        )

    def read(self, path: CanonicalPath, maximum: int) -> bytes:
        content = self._files.get(path)
        if content is None or len(content) > maximum:
            raise SnapshotUnavailable("SNAPSHOT_PATH_UNAVAILABLE")
        return content


class FilesystemRepositorySnapshot:
    def __init__(self, root: Path) -> None:
        self._root = root.resolve(strict=True)

    @staticmethod
    def _tree(root: Path) -> StableHandleTree:
        backend = WindowsNoFollowBackend() if os.name == "nt" else PosixNoFollowBackend()
        return StableHandleTree(root, backend)

    def entries(self) -> tuple[SnapshotEntry, ...]:
        try:
            tree = self._tree(self._root)
            try:
                entries: list[SnapshotEntry] = []
                pending = [""]
                while pending:
                    parent = pending.pop()
                    node = tree.root_node if not parent else tree.open(parent, "directory")
                    for name in reversed(tree.list_names(node, MAX_SNAPSHOT_ENTRIES)):
                        relative = name if not parent else f"{parent}/{name}"
                        opened = tree.try_open_any(relative)
                        if opened is None:
                            raise SnapshotNoFollowDenied("SNAPSHOT_ENTRY_CHANGED")
                        if opened.identity.kind == "directory":
                            pending.append(relative)
                        else:
                            path = CanonicalPath.parse(relative)
                            entries.append(SnapshotEntry(path=str(path), size=0))
                        if len(entries) + len(pending) > MAX_SNAPSHOT_ENTRIES:
                            raise SnapshotNoFollowDenied("SNAPSHOT_ENTRY_LIMIT")
                tree.assert_name_bindings()
                return tuple(sorted(entries, key=lambda entry: entry.path))
            finally:
                tree.close()
        except (RepositoryUnsafeError, OSError, ValueError) as error:
            raise SnapshotNoFollowDenied("SNAPSHOT_NO_FOLLOW_DENIED") from error

    def read(self, path: CanonicalPath, maximum: int) -> bytes:
        try:
            tree = self._tree(self._root)
            try:
                node = tree.try_open_any(str(path))
                if node is None:
                    raise SnapshotUnavailable("SNAPSHOT_PATH_UNAVAILABLE")
                if node.identity.kind != "file":
                    raise SnapshotNoFollowDenied("SNAPSHOT_REGULAR_FILE_REQUIRED")
                content = tree.read_bytes(node, maximum)
                tree.assert_name_bindings()
                return content
            finally:
                tree.close()
        except SnapshotUnavailable:
            raise
        except (RepositoryUnsafeError, OSError) as error:
            raise SnapshotNoFollowDenied("SNAPSHOT_NO_FOLLOW_DENIED") from error
