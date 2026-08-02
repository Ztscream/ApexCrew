from __future__ import annotations

import struct
import subprocess
import sys
from collections.abc import Mapping
from ctypes import POINTER, addressof, c_long, c_void_p, cast, memmove, string_at, wintypes
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal

import pytest

from apexcrew.adapters.repository.git import (
    GitCommandRunner,
    GitLayout,
    GitStatusPorcelain,
    RepositoryInstance,
)
from apexcrew.adapters.repository.no_follow import (
    HandleIdentity,
    NodeKind,
    OpenedNode,
    RepositoryUnsafeError,
    StableHandleTree,
)
from apexcrew.adapters.repository.no_follow_windows import (
    IO_STATUS_BLOCK,
    OBJECT_ATTRIBUTES,
    STATUS_NO_MORE_FILES,
    STATUS_NOT_A_DIRECTORY,
    STATUS_OBJECT_NAME_NOT_FOUND,
    STATUS_OBJECT_PATH_NOT_FOUND,
    UNICODE_STRING,
    WindowsNoFollowBackend,
)

FILE_OPEN_REPARSE_POINT = 0x00200000
FILE_SHARE_DELETE = 0x00000004
POSIX_O_NOFOLLOW = 0x00020000
POSIX_O_CLOEXEC = 0x00080000


@dataclass(frozen=True, slots=True)
class _RecordedOpen:
    parent: int
    name: str
    flags: int
    share_mask: int


class _RecordingBackend:
    volume_handle = 10
    repo_handle = 20
    git_handle = 30
    config_handle = 40

    def __init__(self, platform: Literal["posix", "windows"]) -> None:
        self.platform = platform
        self.absolute_root = Path("C:/repo") if platform == "windows" else Path("/repo")
        self.opens: list[_RecordedOpen] = []
        self.followed_link_count = 0
        self.git_executable = Path("C:/git.exe").resolve()
        self.empty_dir = Path("C:/empty")
        self._replacement_ids: dict[tuple[str, ...], int] = {}

    @property
    def opened_components(self) -> list[tuple[int, str]]:
        return [(item.parent, item.name) for item in self.opens]

    def _flags(self) -> int:
        if self.platform == "windows":
            return FILE_OPEN_REPARSE_POINT
        return POSIX_O_NOFOLLOW | POSIX_O_CLOEXEC

    def _share_mask(self) -> int:
        return 0x00000003 if self.platform == "windows" else 0

    def _record(self, parent: int, name: str, kind: NodeKind) -> OpenedNode:
        self.opens.append(_RecordedOpen(parent, name, self._flags(), self._share_mask()))
        handle = {
            "repo": self.repo_handle,
            ".git": self.git_handle,
            "config": self.config_handle,
        }.get(name, 50 + len(self.opens))
        binding = {
            (self.volume_handle, "repo"): ("repo",),
            (self.repo_handle, ".git"): ("repo", ".git"),
            (self.git_handle, "config"): ("repo", ".git", "config"),
        }.get((parent, name), (name,))
        file_id = self._replacement_ids.get(binding, handle)
        components = () if name == "repo" else (name,)
        return OpenedNode(
            components,
            handle,
            HandleIdentity(self.platform, 1, file_id, kind),
        )

    def open_root_chain(self, root: Path) -> tuple[OpenedNode, ...]:
        volume = OpenedNode(
            (),
            self.volume_handle,
            HandleIdentity(self.platform, 1, self.volume_handle, "directory"),
        )
        repo = self._record(self.volume_handle, "repo", "directory")
        return volume, repo

    def open_child(self, parent: OpenedNode, name: str, kind: NodeKind) -> OpenedNode:
        node = self._record(parent.handle, name, kind)
        return OpenedNode(
            parent.components + (name,),
            node.handle,
            node.identity,
        )

    def try_open_child(self, parent: OpenedNode, name: str, kind: NodeKind) -> OpenedNode | None:
        return self.open_child(parent, name, kind)

    def try_open_child_any(self, parent: OpenedNode, name: str) -> OpenedNode | None:
        return self.open_child(parent, name, "file")

    def read_bytes(self, node: OpenedNode, maximum: int) -> bytes:
        return b""

    def list_names(self, node: OpenedNode, maximum: int) -> tuple[str, ...]:
        return ()

    def close(self, node: OpenedNode) -> None:
        return None

    def every_open_has(self, flags: int) -> bool:
        return all(item.flags & flags == flags for item in self.opens)

    def every_share_mask_excludes(self, flags: int) -> bool:
        return all(item.share_mask & flags == 0 for item in self.opens)

    def every_child_open_has_dir_fd(self) -> bool:
        return all(
            item.parent in {self.volume_handle, self.repo_handle, self.git_handle}
            for item in self.opens
        )

    def replace_name_binding(self, replaced: tuple[str, ...], new_file_id: int) -> None:
        self._replacement_ids[replaced] = new_file_id


def recording_backend(
    platform: Literal["posix", "windows"], tree: tuple[str, ...]
) -> _RecordingBackend:
    assert tree == ("repo", ".git", "config")
    return _RecordingBackend(platform)


def recording_windows_backend() -> _RecordingBackend:
    return recording_backend("windows", ("repo", ".git", "config"))


def recording_posix_backend() -> _RecordingBackend:
    return recording_backend("posix", ("repo", ".git", "config"))


def bound_repository_from_backend(backend: _RecordingBackend) -> RepositoryInstance:
    handles = StableHandleTree(backend.absolute_root, backend)
    git_dir = handles.open(".git", "directory")
    config = handles.open(".git/config", "file")
    return RepositoryInstance.from_layout(
        GitLayout(handles, handles.root_node, git_dir, config, None, ())
    )


class RecordingGitSpawner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def run(
        self,
        argv: tuple[str, ...],
        cwd: Path,
        environment: Mapping[str, str],
        *,
        text: bool,
    ) -> subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes]:
        del cwd, environment
        self.calls.append(argv)
        if text:
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.CompletedProcess(argv, 0, b"", b"")


@pytest.mark.parametrize("platform", ["posix", "windows"])
def test_every_component_is_opened_relative_to_its_held_parent(
    platform: Literal["posix", "windows"],
) -> None:
    backend = recording_backend(platform, tree=("repo", ".git", "config"))
    handles = StableHandleTree(backend.absolute_root, backend)
    handles.open(".git/config", "file")
    assert backend.opened_components == [
        (backend.volume_handle, "repo"),
        (backend.repo_handle, ".git"),
        (backend.git_handle, "config"),
    ]
    assert backend.followed_link_count == 0


def test_windows_open_flags_pin_names_and_open_the_reparse_point() -> None:
    backend = recording_windows_backend()
    StableHandleTree(backend.absolute_root, backend).open(".git/config", "file")
    assert backend.every_open_has(FILE_OPEN_REPARSE_POINT)
    assert backend.every_share_mask_excludes(FILE_SHARE_DELETE)


def test_posix_open_flags_use_dir_fd_and_no_follow() -> None:
    backend = recording_posix_backend()
    StableHandleTree(backend.absolute_root, backend).open(".git/config", "file")
    assert backend.every_open_has(POSIX_O_NOFOLLOW | POSIX_O_CLOEXEC)
    assert backend.every_child_open_has_dir_fd()


@pytest.mark.parametrize("platform", ["posix", "windows"])
@pytest.mark.parametrize("replaced", [("repo", ".git"), ("repo", ".git", "config")])
def test_replaced_bound_component_stops_before_git_spawn(
    platform: Literal["posix", "windows"], replaced: tuple[str, ...]
) -> None:
    backend = recording_backend(platform, tree=("repo", ".git", "config"))
    repository = bound_repository_from_backend(backend)
    spawner = RecordingGitSpawner()
    backend.replace_name_binding(replaced, new_file_id=999)
    with pytest.raises(
        RepositoryUnsafeError,
        match="REPOSITORY_ANCESTOR_IDENTITY_CHANGED|GIT_STORAGE_IDENTITY_CHANGED",
    ):
        GitCommandRunner(backend.git_executable, backend.empty_dir, spawner).run(
            repository, GitStatusPorcelain()
        )
    assert spawner.calls == []


@pytest.mark.parametrize("relative", ["..", ""])
def test_try_open_rejects_traversal_components(relative: str) -> None:
    backend = recording_windows_backend()
    handles = StableHandleTree(backend.absolute_root, backend)

    with pytest.raises(RepositoryUnsafeError, match="INVALID_HANDLE_RELATIVE_PATH"):
        handles.try_open(relative, "file")


@pytest.mark.parametrize("operation", ["open", "try_open", "try_open_any"])
def test_backslash_cannot_hide_unchecked_windows_components(operation: str) -> None:
    backend = recording_windows_backend()
    handles = StableHandleTree(backend.absolute_root, backend)

    with pytest.raises(RepositoryUnsafeError, match="INVALID_HANDLE_RELATIVE_PATH"):
        if operation == "try_open_any":
            handles.try_open_any(r".git\config")
        else:
            getattr(handles, operation)(r".git\config", "file")

    assert backend.opened_components == [(backend.volume_handle, "repo")]


def test_try_open_rejects_a_cached_node_of_the_wrong_kind() -> None:
    backend = recording_windows_backend()
    handles = StableHandleTree(backend.absolute_root, backend)
    handles.open("cache", "directory")

    with pytest.raises(RepositoryUnsafeError, match="GIT_STORAGE_KIND_CHANGED"):
        handles.try_open("cache", "file")


@pytest.mark.parametrize(
    "status",
    [STATUS_OBJECT_NAME_NOT_FOUND, STATUS_OBJECT_PATH_NOT_FOUND],
)
def test_windows_missing_name_status_is_exact(status: int) -> None:
    backend = WindowsNoFollowBackend.__new__(WindowsNoFollowBackend)
    backend._last_ntstatus = status
    assert backend._last_status_is_object_name_not_found()


@pytest.mark.parametrize("status", [0, STATUS_NOT_A_DIRECTORY, c_long(0xC0000022).value])
def test_windows_other_status_is_not_a_missing_name(status: int) -> None:
    backend = WindowsNoFollowBackend.__new__(WindowsNoFollowBackend)
    backend._last_ntstatus = status
    assert not backend._last_status_is_object_name_not_found()


def test_windows_not_a_directory_status_is_exact() -> None:
    backend = WindowsNoFollowBackend.__new__(WindowsNoFollowBackend)
    backend._last_ntstatus = STATUS_NOT_A_DIRECTORY
    assert backend._last_status_is_not_a_directory()

    for status in (0, STATUS_OBJECT_NAME_NOT_FOUND, c_long(0xC0000022).value):
        backend._last_ntstatus = status
        assert not backend._last_status_is_not_a_directory()


class _SuccessfulNtCreate:
    def __init__(self) -> None:
        self.calls = 0
        self.names: list[tuple[int, int, str]] = []

    def __call__(self, *arguments: Any) -> int:
        self.calls += 1
        attributes = cast(arguments[2], POINTER(OBJECT_ATTRIBUTES)).contents
        unicode_name = attributes.ObjectName.contents
        buffer_pointer = c_void_p.from_address(
            addressof(unicode_name) + UNICODE_STRING.Buffer.offset
        ).value
        assert buffer_pointer is not None
        encoded = string_at(buffer_pointer, unicode_name.Length)
        self.names.append(
            (
                unicode_name.Length,
                unicode_name.MaximumLength,
                encoded.decode("utf-16-le"),
            )
        )
        handle = cast(arguments[0], POINTER(wintypes.HANDLE)).contents
        handle.value = 99
        return 0


def _reject_reparse(handle: int, components: tuple[str, ...], kind: NodeKind) -> OpenedNode:
    raise RepositoryUnsafeError("SYMLINK_OR_REPARSE_DENIED")


def _accept_identity(handle: int, components: tuple[str, ...], kind: NodeKind) -> OpenedNode:
    return OpenedNode(components, handle, HandleIdentity("windows", 1, handle, kind))


def test_windows_unicode_string_lengths_use_utf16_bytes() -> None:
    backend = WindowsNoFollowBackend.__new__(WindowsNoFollowBackend)
    create = _SuccessfulNtCreate()
    backend.__dict__["_ntdll"] = SimpleNamespace(NtCreateFile=create)
    backend.__dict__["_identity"] = _accept_identity
    backend._last_ntstatus = None

    backend._open_relative(1, "\U0001f600x", ("\U0001f600x",), "file")

    assert create.names == [(6, 8, "\U0001f600x")]


def test_windows_stale_not_directory_status_cannot_enable_file_fallback() -> None:
    backend = WindowsNoFollowBackend.__new__(WindowsNoFollowBackend)
    create = _SuccessfulNtCreate()
    backend.__dict__["_ntdll"] = SimpleNamespace(NtCreateFile=create)
    backend.__dict__["_identity"] = _reject_reparse
    backend._last_ntstatus = STATUS_NOT_A_DIRECTORY
    parent = OpenedNode((), 1, HandleIdentity("windows", 1, 1, "directory"))

    with pytest.raises(RepositoryUnsafeError, match="SYMLINK_OR_REPARSE_DENIED"):
        backend.try_open_child_any(parent, "link")

    assert create.calls == 1


@pytest.mark.skipif(sys.platform != "win32", reason="Windows DLL signatures require Win32")
def test_windows_wrapped_calls_have_explicit_ctypes_signatures() -> None:
    backend = WindowsNoFollowBackend()

    expected_restypes = [
        (backend._kernel32.CreateFileW, wintypes.HANDLE),
        (backend._kernel32.GetFileInformationByHandle, wintypes.BOOL),
        (backend._kernel32.SetFilePointerEx, wintypes.BOOL),
        (backend._kernel32.ReadFile, wintypes.BOOL),
        (backend._kernel32.CloseHandle, wintypes.BOOL),
        (backend._ntdll.NtCreateFile, c_long),
        (backend._ntdll.NtQueryDirectoryFile, c_long),
    ]
    for function, restype in expected_restypes:
        assert function.argtypes is not None
        assert function.restype is restype


def _directory_record(name: str, *, next_offset: int = 0) -> bytes:
    encoded = name.encode("utf-16-le")
    value = bytearray(104 + len(encoded))
    struct.pack_into("<I", value, 0, next_offset)
    struct.pack_into("<I", value, 60, len(encoded))
    value[104:] = encoded
    return bytes(value)


def _directory_payload(*names: str) -> bytes:
    payload = b""
    for index, name in enumerate(names):
        record = _directory_record(name)
        if index < len(names) - 1:
            padding = (-len(record)) % 8
            record = _directory_record(name, next_offset=len(record) + padding)
            record += b"\x00" * padding
        payload += record
    return payload


class _DirectoryQuery:
    def __init__(self, payloads: list[tuple[int, bytes]]) -> None:
        self._payloads = iter(payloads)
        self.handles: list[int] = []

    def __call__(self, *arguments: Any) -> int:
        status, payload = next(self._payloads)
        self.handles.append(int(arguments[0].value))
        io = cast(arguments[4], POINTER(IO_STATUS_BLOCK)).contents
        io.Information = len(payload)
        if payload:
            memmove(arguments[5], payload, len(payload))
        return status


def _directory_backend(payload: bytes) -> tuple[WindowsNoFollowBackend, _DirectoryQuery]:
    backend = WindowsNoFollowBackend.__new__(WindowsNoFollowBackend)
    query = _DirectoryQuery([(0, payload), (STATUS_NO_MORE_FILES, b"")])
    backend.__dict__["_ntdll"] = SimpleNamespace(NtQueryDirectoryFile=query)
    return backend, query


def test_windows_directory_inventory_uses_the_open_handle() -> None:
    backend, query = _directory_backend(_directory_payload("config", "objects"))

    assert backend._query_directory_handle_names(1234, 2) == ("config", "objects")
    assert query.handles == [1234, 1234]


@pytest.mark.parametrize(
    "payload,maximum",
    [
        (_directory_payload("config", "config"), 2),
        (_directory_record("config"), 0),
        (b"\x00" * 80, 2),
    ],
)
def test_windows_directory_inventory_rejects_duplicate_over_limit_and_malformed_data(
    payload: bytes, maximum: int
) -> None:
    backend, _query = _directory_backend(payload)

    with pytest.raises(RepositoryUnsafeError, match="GIT_DIRECTORY_INVENTORY_INVALID"):
        backend._query_directory_handle_names(1234, maximum)
