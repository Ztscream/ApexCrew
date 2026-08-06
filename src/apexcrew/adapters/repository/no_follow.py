from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol


class RepositoryUnsafeError(RuntimeError):
    pass


type HandleToken = int
NodeKind = Literal["file", "directory"]


@dataclass(frozen=True, slots=True)
class HandleIdentity:
    platform: Literal["posix", "windows"]
    volume: int
    file_id: int
    kind: NodeKind


@dataclass(frozen=True, slots=True)
class OpenedNode:
    components: tuple[str, ...]
    handle: HandleToken
    identity: HandleIdentity


class NoFollowBackend(Protocol):
    def open_root_chain(self, root: Path) -> tuple[OpenedNode, ...]: ...
    def open_child(self, parent: OpenedNode, name: str, kind: NodeKind) -> OpenedNode: ...
    def open_child_for_delete(
        self, parent: OpenedNode, name: str, kind: NodeKind
    ) -> OpenedNode: ...
    def create_child_directory(self, parent: OpenedNode, name: str) -> OpenedNode: ...
    def create_child_file(self, parent: OpenedNode, name: str) -> OpenedNode: ...
    def try_open_child(
        self, parent: OpenedNode, name: str, kind: NodeKind
    ) -> OpenedNode | None: ...
    def try_open_child_any(self, parent: OpenedNode, name: str) -> OpenedNode | None: ...
    def read_bytes(self, node: OpenedNode, maximum: int) -> bytes: ...
    def write_bytes(self, node: OpenedNode, value: bytes) -> None: ...
    def list_names(self, node: OpenedNode, maximum: int) -> tuple[str, ...]: ...

    def unlink_child(self, parent: OpenedNode, name: str, expected: OpenedNode) -> None: ...

    def remove_child_directory(
        self, parent: OpenedNode, name: str, expected: OpenedNode
    ) -> None: ...

    def close(self, node: OpenedNode) -> None: ...


class StableHandleTree:
    def __init__(self, root: Path, backend: NoFollowBackend) -> None:
        self.root = root
        self._backend = backend
        self._root_chain = backend.open_root_chain(root)
        self._nodes: dict[tuple[str, ...], OpenedNode] = {(): self._root_chain[-1]}
        self._pending_closes: dict[int, OpenedNode] = {}

    @property
    def root_node(self) -> OpenedNode:
        return self._nodes[()]

    def open(self, relative: str, kind: NodeKind) -> OpenedNode:
        parts = tuple(relative.split("/"))
        if not parts or any(part in {"", ".", ".."} or "\\" in part for part in parts):
            raise RepositoryUnsafeError("INVALID_HANDLE_RELATIVE_PATH")
        parent = self._nodes[()]
        for index, part in enumerate(parts):
            key = parts[: index + 1]
            expected = kind if index == len(parts) - 1 else "directory"
            node = self._nodes.get(key)
            if node is None:
                node = self._backend.open_child(parent, part, expected)
                self._nodes[key] = node
            if node.identity.kind != expected:
                raise RepositoryUnsafeError("GIT_STORAGE_KIND_CHANGED")
            parent = node
        return parent

    def try_open(self, relative: str, kind: NodeKind) -> OpenedNode | None:
        parts = tuple(relative.split("/"))
        if not parts or any(part in {"", ".", ".."} or "\\" in part for part in parts):
            raise RepositoryUnsafeError("INVALID_HANDLE_RELATIVE_PATH")
        parent = self._nodes[()]
        for index, part in enumerate(parts):
            key = parts[: index + 1]
            expected = kind if index == len(parts) - 1 else "directory"
            node = self._nodes.get(key)
            if node is None:
                node = self._backend.try_open_child(parent, part, expected)
                if node is None:
                    return None
                self._nodes[key] = node
            if node.identity.kind != expected:
                raise RepositoryUnsafeError("GIT_STORAGE_KIND_CHANGED")
            parent = node
        return parent

    def try_open_any(self, relative: str) -> OpenedNode | None:
        parts = tuple(relative.split("/"))
        if not parts or any(part in {"", ".", ".."} or "\\" in part for part in parts):
            raise RepositoryUnsafeError("INVALID_HANDLE_RELATIVE_PATH")
        parent = self._nodes[()]
        for index, part in enumerate(parts):
            key = parts[: index + 1]
            node = self._nodes.get(key)
            if node is None:
                node = self._backend.try_open_child_any(parent, part)
                if node is None:
                    return None
                self._nodes[key] = node
            parent = node
        return parent

    def read_bytes(self, node: OpenedNode, maximum: int) -> bytes:
        return self._backend.read_bytes(node, maximum)

    def list_names(self, node: OpenedNode, maximum: int) -> tuple[str, ...]:
        return self._backend.list_names(node, maximum)

    def remove_file(self, relative: str, expected: HandleIdentity) -> None:
        parts = self._parts(relative)
        parent = self.open("/".join(parts[:-1]), "directory") if len(parts) > 1 else self.root_node
        self.release_cached(relative)
        node = self._backend.open_child_for_delete(parent, parts[-1], "file")
        try:
            if node.identity != expected:
                raise RepositoryUnsafeError("DELETE_IDENTITY_CHANGED")
            self._backend.unlink_child(parent, parts[-1], node)
        finally:
            self._backend.close(node)

    def remove_directory(self, relative: str, expected: HandleIdentity) -> None:
        parts = self._parts(relative)
        parent = self.open("/".join(parts[:-1]), "directory") if len(parts) > 1 else self.root_node
        self.release_cached(relative)
        node = self._backend.open_child_for_delete(parent, parts[-1], "directory")
        try:
            if node.identity != expected:
                raise RepositoryUnsafeError("DELETE_IDENTITY_CHANGED")
            self._backend.remove_child_directory(parent, parts[-1], node)
        finally:
            self._backend.close(node)

    @staticmethod
    def _parts(relative: str) -> tuple[str, ...]:
        parts = tuple(relative.split("/"))
        if not parts or any(part in {"", ".", ".."} or "\\" in part for part in parts):
            raise RepositoryUnsafeError("INVALID_HANDLE_RELATIVE_PATH")
        return parts

    def assert_name_bindings(self) -> None:
        self._retry_pending_closes()
        probes = self._backend.open_root_chain(self.root)
        primary_error: Exception | None = None
        try:
            if tuple(node.identity for node in probes) != tuple(
                node.identity for node in self._root_chain
            ):
                raise RepositoryUnsafeError("REPOSITORY_ANCESTOR_IDENTITY_CHANGED")
            probe_by_parts: dict[tuple[str, ...], OpenedNode] = {(): probes[-1]}
            for parts, expected in sorted(
                self._nodes.items(), key=lambda item: (len(item[0]), item[0])
            ):
                if not parts:
                    continue
                probe = self._backend.open_child(
                    probe_by_parts[parts[:-1]], parts[-1], expected.identity.kind
                )
                probes += (probe,)
                if probe.identity != expected.identity:
                    raise RepositoryUnsafeError("GIT_STORAGE_IDENTITY_CHANGED")
                probe_by_parts[parts] = probe
        except (OSError, RepositoryUnsafeError) as error:
            primary_error = error
        finally:
            try:
                self._close_many(reversed(probes))
            except (OSError, RepositoryUnsafeError) as cleanup_error:
                if primary_error is None:
                    primary_error = cleanup_error
                else:
                    primary_error.add_note(f"probe cleanup failed: {cleanup_error}")
        if primary_error is not None:
            raise primary_error

    def close(self) -> None:
        owned = {node.handle: node for node in (*self._nodes.values(), *self._root_chain)}
        try:
            self._close_many((*self._pending_closes.values(), *reversed(tuple(owned.values()))))
        finally:
            pending_handles = set(self._pending_closes)
            self._nodes = {
                parts: node for parts, node in self._nodes.items() if node.handle in pending_handles
            }
            self._root_chain = tuple(
                node for node in self._root_chain if node.handle in pending_handles
            )

    def release_cached(self, relative: str) -> None:
        parts = tuple(relative.split("/"))
        if not parts or any(part in {"", ".", ".."} or "\\" in part for part in parts):
            raise RepositoryUnsafeError("INVALID_HANDLE_RELATIVE_PATH")
        selected = {
            key: node
            for key, node in self._nodes.items()
            if len(key) >= len(parts) and key[: len(parts)] == parts
        }
        if not selected:
            return
        try:
            self._close_many(
                node
                for _key, node in sorted(
                    selected.items(), key=lambda item: len(item[0]), reverse=True
                )
            )
        finally:
            pending_handles = set(self._pending_closes)
            self._nodes = {
                key: node
                for key, node in self._nodes.items()
                if key not in selected or node.handle in pending_handles
            }

    def _retry_pending_closes(self) -> None:
        if self._pending_closes:
            self._close_many(tuple(self._pending_closes.values()))

    def _close_many(self, nodes: Iterable[OpenedNode]) -> None:
        first_error: Exception | None = None
        attempted: set[int] = set()
        for node in nodes:
            if node.handle in attempted:
                continue
            attempted.add(node.handle)
            try:
                self._backend.close(node)
            except (OSError, RepositoryUnsafeError) as error:
                self._pending_closes[node.handle] = node
                if first_error is None:
                    first_error = error
            else:
                self._pending_closes.pop(node.handle, None)
        if first_error is not None:
            raise first_error
