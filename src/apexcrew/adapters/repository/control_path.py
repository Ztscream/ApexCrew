from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Self

from apexcrew.adapters.repository.no_follow import (
    HandleIdentity,
    NoFollowBackend,
    OpenedNode,
    RepositoryUnsafeError,
    StableHandleTree,
)
from apexcrew.adapters.repository.no_follow_posix import PosixNoFollowBackend
from apexcrew.adapters.repository.no_follow_windows import WindowsNoFollowBackend


@dataclass(frozen=True, slots=True)
class ControlPathState:
    config: Path
    database: Path


class ControlPathGuard:
    """Bind `.apexcrew` entries to no-follow parent handles and identities."""

    def __init__(self, root: Path, backend: NoFollowBackend | None = None) -> None:
        selected = (
            (WindowsNoFollowBackend() if os.name == "nt" else PosixNoFollowBackend())
            if backend is None
            else backend
        )
        self._tree = StableHandleTree(root, selected)
        self._backend = selected
        self._control: OpenedNode | None = None
        self._config_identity: HandleIdentity | None = None
        self._database_identity: HandleIdentity | None = None
        self._database_node: OpenedNode | None = None
        self._pending_closes: dict[int, OpenedNode] = {}
        self.state = ControlPathState(
            root / ".apexcrew" / "config.json", root / ".apexcrew" / "state.db"
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(self, _type: object, value: BaseException | None, _traceback: object) -> None:
        try:
            self.close()
        except (OSError, RepositoryUnsafeError) as cleanup_error:
            if value is None:
                raise
            value.add_note(f"control path cleanup failed: {cleanup_error}")

    def ensure(self) -> None:
        if self._control is not None:
            self.assert_current()
            return
        self._tree.assert_name_bindings()
        if self._control is None:
            control = self._backend.try_open_child(self._tree.root_node, ".apexcrew", "directory")
            if control is None:
                control = self._backend.create_child_directory(self._tree.root_node, ".apexcrew")
            self._control = control
        self._config_identity = self._observe_entry("config.json")
        self._database_identity = self._observe_entry("state.db")
        self.assert_current()

    def config_exists(self) -> bool:
        control = self._backend.try_open_child(self._tree.root_node, ".apexcrew", "directory")
        if control is None:
            return False
        try:
            node = self._backend.try_open_child(control, "config.json", "file")
            if node is None:
                return False
            self._close_node(node)
            return True
        finally:
            self._close_node(control)

    def write_config_if_missing(self) -> None:
        self.ensure()
        if self._config_identity is not None:
            self.assert_current()
            return
        control = self._require_control()
        node = self._backend.create_child_file(control, "config.json")
        try:
            self._backend.write_bytes(node, b'{"schema_version":"cli-config-v1"}\n')
            self._config_identity = node.identity
        finally:
            self._close_node(node)
        self.assert_current()

    def prepare_database(self) -> Path:
        self.ensure()
        if self._database_node is None:
            node = self._backend.create_child_file(self._require_control(), "state.db")
            self._database_node = node
            self._database_identity = node.identity
        self.assert_current()
        return self.state.database

    def open_database(self) -> sqlite3.Connection:
        """Open SQLite through the already-bound state database handle."""
        self.prepare_database()
        node = self._database_node
        if node is None:
            raise RepositoryUnsafeError("CONTROL_PATH_NOT_BOUND")
        if os.name == "posix":
            for descriptor_root in ("/proc/self/fd", "/dev/fd"):
                descriptor = os.path.join(descriptor_root, str(node.handle))
                if os.path.exists(descriptor):
                    connection = sqlite3.connect(
                        f"file:{descriptor}?mode=rwc",
                        uri=True,
                        isolation_level=None,
                        check_same_thread=False,
                    )
                    try:
                        self.assert_current()
                    except BaseException:
                        connection.close()
                        raise
                    return connection
            raise RepositoryUnsafeError("POSIX_SQLITE_HANDLE_REFERENCE_REQUIRED")
        connection = sqlite3.connect(
            self.state.database,
            isolation_level=None,
            check_same_thread=False,
        )
        try:
            self.assert_current()
        except BaseException:
            connection.close()
            raise
        return connection

    def open_existing_database_read_only(self) -> sqlite3.Connection:
        """Open an existing bound state database without materializing control paths."""
        self._tree.assert_name_bindings()
        control = self._backend.try_open_child(self._tree.root_node, ".apexcrew", "directory")
        if control is None:
            raise RepositoryUnsafeError("CONTROL_PATH_NOT_INITIALIZED")
        database: OpenedNode | None = None
        try:
            database = self._backend.try_open_child(control, "state.db", "file")
            if database is None:
                raise RepositoryUnsafeError("CONTROL_DATABASE_NOT_FOUND")
            self._control = control
            self._database_node = database
            self._database_identity = database.identity
            self._config_identity = self._observe_entry("config.json")
            self.assert_current()
        except BaseException:
            if self._control is None:
                try:
                    self._close_node(control)
                except (OSError, RepositoryUnsafeError):
                    pass
            if self._database_node is None and database is not None:
                try:
                    self._close_node(database)
                except (OSError, RepositoryUnsafeError):
                    pass
            raise
        if os.name == "posix":
            for descriptor_root in ("/proc/self/fd", "/dev/fd"):
                descriptor = os.path.join(descriptor_root, str(database.handle))
                if os.path.exists(descriptor):
                    connection = sqlite3.connect(
                        f"file:{descriptor}?mode=ro",
                        uri=True,
                        isolation_level=None,
                        check_same_thread=False,
                    )
                    try:
                        self.assert_current()
                    except BaseException:
                        connection.close()
                        raise
                    return connection
            raise RepositoryUnsafeError("POSIX_SQLITE_HANDLE_REFERENCE_REQUIRED")
        connection = sqlite3.connect(
            f"{self.state.database.resolve().as_uri()}?mode=ro",
            uri=True,
            isolation_level=None,
            check_same_thread=False,
        )
        try:
            self.assert_current()
        except BaseException:
            connection.close()
            raise
        return connection

    def assert_current(self) -> None:
        self._tree.assert_name_bindings()
        control = self._backend.open_child(self._tree.root_node, ".apexcrew", "directory")
        primary_error: Exception | None = None
        try:
            if self._control is None or control.identity != self._control.identity:
                raise RepositoryUnsafeError("CONTROL_PATH_IDENTITY_CHANGED")
            self._assert_entry(control, "config.json", self._config_identity)
            self._assert_entry(control, "state.db", self._database_identity)
        except (OSError, RepositoryUnsafeError) as error:
            primary_error = error
        finally:
            try:
                self._close_node(control)
            except (OSError, RepositoryUnsafeError) as cleanup_error:
                if primary_error is None:
                    primary_error = cleanup_error
                else:
                    primary_error.add_note(f"control probe cleanup failed: {cleanup_error}")
        try:
            self._tree.assert_name_bindings()
        except (OSError, RepositoryUnsafeError) as final_error:
            if primary_error is None:
                primary_error = final_error
            else:
                primary_error.add_note(f"final ancestor check failed: {final_error}")
        if primary_error is not None:
            raise primary_error

    def close(self) -> None:
        first_error: Exception | None = None
        try:
            self._close_many(
                (
                    *self._pending_closes.values(),
                    *(() if self._database_node is None else (self._database_node,)),
                    *(() if self._control is None else (self._control,)),
                )
            )
        except (OSError, RepositoryUnsafeError) as error:
            first_error = error
        pending_handles = set(self._pending_closes)
        if self._database_node is not None and self._database_node.handle not in pending_handles:
            self._database_node = None
        if self._control is not None and self._control.handle not in pending_handles:
            self._control = None
        try:
            self._tree.close()
        except (OSError, RepositoryUnsafeError) as error:
            if first_error is None:
                first_error = error
        if first_error is not None:
            raise first_error

    def _observe_entry(self, name: str) -> HandleIdentity | None:
        node = self._backend.try_open_child(self._require_control(), name, "file")
        if node is None:
            return None
        if name == "state.db":
            self._database_node = node
            return node.identity
        self._close_node(node)
        return node.identity

    def _assert_entry(
        self,
        control: OpenedNode,
        name: str,
        expected: HandleIdentity | None,
    ) -> None:
        node = self._backend.try_open_child(control, name, "file")
        if expected is None:
            if node is not None:
                appeared_error = RepositoryUnsafeError("CONTROL_PATH_APPEARED")
                try:
                    self._close_node(node)
                except (OSError, RepositoryUnsafeError) as cleanup_error:
                    appeared_error.add_note(f"unexpected entry cleanup failed: {cleanup_error}")
                raise appeared_error
            return
        if node is None:
            raise RepositoryUnsafeError("CONTROL_PATH_DISAPPEARED")
        primary_error: Exception | None = None
        try:
            if node.identity != expected:
                raise RepositoryUnsafeError("CONTROL_PATH_IDENTITY_CHANGED")
        except (OSError, RepositoryUnsafeError) as error:
            primary_error = error
        finally:
            try:
                self._close_node(node)
            except (OSError, RepositoryUnsafeError) as cleanup_error:
                if primary_error is None:
                    primary_error = cleanup_error
                else:
                    primary_error.add_note(f"entry probe cleanup failed: {cleanup_error}")
        if primary_error is not None:
            raise primary_error

    def _close_node(self, node: OpenedNode) -> None:
        self._close_many((node,))

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

    def _require_control(self) -> OpenedNode:
        if self._control is None:
            raise RepositoryUnsafeError("CONTROL_PATH_NOT_BOUND")
        return self._control
