from __future__ import annotations

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
    def try_open_child(
        self, parent: OpenedNode, name: str, kind: NodeKind
    ) -> OpenedNode | None: ...
    def try_open_child_any(self, parent: OpenedNode, name: str) -> OpenedNode | None: ...
    def read_bytes(self, node: OpenedNode, maximum: int) -> bytes: ...
    def list_names(self, node: OpenedNode, maximum: int) -> tuple[str, ...]: ...
    def close(self, node: OpenedNode) -> None: ...


class StableHandleTree:
    def __init__(self, root: Path, backend: NoFollowBackend) -> None:
        self.root = root
        self._backend = backend
        self._root_chain = backend.open_root_chain(root)
        self._nodes: dict[tuple[str, ...], OpenedNode] = {(): self._root_chain[-1]}

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

    def assert_name_bindings(self) -> None:
        probes = self._backend.open_root_chain(self.root)
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
        finally:
            for node in reversed(probes):
                self._backend.close(node)

    def close(self) -> None:
        owned = {node.handle: node for node in (*self._nodes.values(), *self._root_chain)}
        for node in reversed(tuple(owned.values())):
            self._backend.close(node)
