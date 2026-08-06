from __future__ import annotations

import struct
import sys
from ctypes import (
    CDLL,
    POINTER,
    Structure,
    byref,
    c_long,
    c_longlong,
    c_size_t,
    c_ubyte,
    c_void_p,
    cast,
    create_string_buffer,
    sizeof,
    wintypes,
)
from pathlib import Path, PureWindowsPath
from typing import TYPE_CHECKING

from apexcrew.adapters.repository.no_follow import (
    HandleIdentity,
    NodeKind,
    OpenedNode,
    RepositoryUnsafeError,
)

if TYPE_CHECKING:
    if sys.platform == "win32":
        from ctypes import WinDLL
    else:
        WinDLL: type[CDLL] | None = None
elif sys.platform == "win32":
    from ctypes import WinDLL
else:
    WinDLL = None  # type: ignore[assignment,misc]


FILE_OPEN = 1
FILE_CREATE = 2
FILE_DIRECTORY_FILE = 0x00000001
FILE_NON_DIRECTORY_FILE = 0x00000040
FILE_OPEN_REPARSE_POINT = 0x00200000
FILE_SYNCHRONOUS_IO_NONALERT = 0x00000020
FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
FILE_SHARE_READ = 1
FILE_SHARE_WRITE = 2
FILE_SHARE_DELETE = 4
OBJ_CASE_INSENSITIVE = 0x40
SYNCHRONIZE = 0x00100000
FILE_READ_DATA = 0x0001
FILE_WRITE_DATA = 0x0002
FILE_LIST_DIRECTORY = 0x0001
FILE_READ_ATTRIBUTES = 0x0080
DELETE = 0x00010000
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
OPEN_EXISTING = 3
FILE_DISPOSITION_INFO = 4
INVALID_HANDLE_VALUE = c_void_p(-1).value
FILE_ID_BOTH_DIRECTORY_INFORMATION = 37
FILE_ID_BOTH_DIRECTORY_HEADER_SIZE = 104
DIRECTORY_QUERY_BUFFER_SIZE = 65_536

STATUS_SUCCESS = 0
STATUS_NO_MORE_FILES = -2_147_483_642
STATUS_OBJECT_NAME_NOT_FOUND = -1_073_741_772
STATUS_OBJECT_PATH_NOT_FOUND = -1_073_741_766
STATUS_NOT_A_DIRECTORY = -1_073_741_565
STATUS_OBJECT_NAME_COLLISION = c_long(0xC0000035).value


class UNICODE_STRING(Structure):
    _fields_ = [
        ("Length", wintypes.USHORT),
        ("MaximumLength", wintypes.USHORT),
        ("Buffer", wintypes.LPWSTR),
    ]


class OBJECT_ATTRIBUTES(Structure):
    _fields_ = [
        ("Length", wintypes.ULONG),
        ("RootDirectory", wintypes.HANDLE),
        ("ObjectName", POINTER(UNICODE_STRING)),
        ("Attributes", wintypes.ULONG),
        ("SecurityDescriptor", c_void_p),
        ("SecurityQualityOfService", c_void_p),
    ]


class IO_STATUS_BLOCK(Structure):
    _fields_ = [("Status", c_long), ("Information", c_size_t)]


class BY_HANDLE_FILE_INFORMATION(Structure):
    _fields_ = [
        ("dwFileAttributes", wintypes.DWORD),
        ("ftCreationTime", wintypes.FILETIME),
        ("ftLastAccessTime", wintypes.FILETIME),
        ("ftLastWriteTime", wintypes.FILETIME),
        ("dwVolumeSerialNumber", wintypes.DWORD),
        ("nFileSizeHigh", wintypes.DWORD),
        ("nFileSizeLow", wintypes.DWORD),
        ("nNumberOfLinks", wintypes.DWORD),
        ("nFileIndexHigh", wintypes.DWORD),
        ("nFileIndexLow", wintypes.DWORD),
    ]


class WindowsNoFollowBackend:
    def __init__(self, *, allow_delete_share: bool = False) -> None:
        if WinDLL is None:
            raise RepositoryUnsafeError("WINDOWS_NT_REQUIRED")
        self._allow_delete_share = allow_delete_share
        self._kernel32: CDLL = WinDLL("kernel32", use_last_error=True)
        self._ntdll: CDLL = WinDLL("ntdll")
        self._last_ntstatus: int | None = None
        self._declare_native_signatures()

    def _declare_native_signatures(self) -> None:
        self._kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        self._kernel32.CreateFileW.restype = wintypes.HANDLE
        self._kernel32.GetFileInformationByHandle.argtypes = [
            wintypes.HANDLE,
            POINTER(BY_HANDLE_FILE_INFORMATION),
        ]
        self._kernel32.GetFileInformationByHandle.restype = wintypes.BOOL
        self._kernel32.SetFilePointerEx.argtypes = [
            wintypes.HANDLE,
            c_longlong,
            POINTER(c_longlong),
            wintypes.DWORD,
        ]
        self._kernel32.SetFilePointerEx.restype = wintypes.BOOL
        self._kernel32.ReadFile.argtypes = [
            wintypes.HANDLE,
            c_void_p,
            wintypes.DWORD,
            POINTER(wintypes.DWORD),
            c_void_p,
        ]
        self._kernel32.ReadFile.restype = wintypes.BOOL
        self._kernel32.WriteFile.argtypes = [
            wintypes.HANDLE,
            c_void_p,
            wintypes.DWORD,
            POINTER(wintypes.DWORD),
            c_void_p,
        ]
        self._kernel32.WriteFile.restype = wintypes.BOOL
        self._kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel32.CloseHandle.restype = wintypes.BOOL
        self._kernel32.SetFileInformationByHandle.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            c_void_p,
            wintypes.DWORD,
        ]
        self._kernel32.SetFileInformationByHandle.restype = wintypes.BOOL
        self._ntdll.NtCreateFile.argtypes = [
            POINTER(wintypes.HANDLE),
            wintypes.ULONG,
            POINTER(OBJECT_ATTRIBUTES),
            POINTER(IO_STATUS_BLOCK),
            POINTER(c_longlong),
            wintypes.ULONG,
            wintypes.ULONG,
            wintypes.ULONG,
            wintypes.ULONG,
            c_void_p,
            wintypes.ULONG,
        ]
        self._ntdll.NtCreateFile.restype = c_long
        self._ntdll.NtQueryDirectoryFile.argtypes = [
            wintypes.HANDLE,
            wintypes.HANDLE,
            c_void_p,
            c_void_p,
            POINTER(IO_STATUS_BLOCK),
            c_void_p,
            wintypes.ULONG,
            wintypes.ULONG,
            c_ubyte,
            POINTER(UNICODE_STRING),
            c_ubyte,
        ]
        self._ntdll.NtQueryDirectoryFile.restype = c_long

    def _identity(self, handle: int, components: tuple[str, ...], kind: NodeKind) -> OpenedNode:
        information = BY_HANDLE_FILE_INFORMATION()
        native_handle = wintypes.HANDLE(handle)
        if not self._kernel32.GetFileInformationByHandle(native_handle, byref(information)):
            error = RepositoryUnsafeError("HANDLE_IDENTITY_QUERY_FAILED")
            self._close_rejected(native_handle, error)
        if information.dwFileAttributes & FILE_ATTRIBUTE_REPARSE_POINT:
            error = RepositoryUnsafeError("SYMLINK_OR_REPARSE_DENIED")
            self._close_rejected(native_handle, error)
        file_id = (information.nFileIndexHigh << 32) | information.nFileIndexLow
        return OpenedNode(
            components,
            handle,
            HandleIdentity("windows", information.dwVolumeSerialNumber, file_id, kind),
        )

    def _close_rejected(self, handle: wintypes.HANDLE, error: BaseException) -> None:
        if not self._kernel32.CloseHandle(handle):
            error.add_note("rejected handle cleanup failed")
        raise error

    def _open_relative(
        self,
        parent: int,
        name: str,
        components: tuple[str, ...],
        kind: NodeKind,
        *,
        create: bool = False,
        delete_access: bool = False,
        write_access: bool = False,
    ) -> OpenedNode:
        encoded_name = name.encode("utf-16-le")
        if len(encoded_name) > 65_532:
            raise RepositoryUnsafeError("INVALID_HANDLE_RELATIVE_PATH")
        buffer = create_string_buffer(encoded_name + b"\x00\x00")
        unicode_name = UNICODE_STRING(
            len(encoded_name),
            len(encoded_name) + 2,
            cast(buffer, wintypes.LPWSTR),
        )
        attributes = OBJECT_ATTRIBUTES(
            sizeof(OBJECT_ATTRIBUTES),
            wintypes.HANDLE(parent),
            POINTER(UNICODE_STRING)(unicode_name),
            OBJ_CASE_INSENSITIVE,
            None,
            None,
        )
        handle = wintypes.HANDLE()
        io = IO_STATUS_BLOCK()
        options = (
            FILE_OPEN_REPARSE_POINT
            | FILE_SYNCHRONOUS_IO_NONALERT
            | (FILE_DIRECTORY_FILE if kind == "directory" else FILE_NON_DIRECTORY_FILE)
        )
        self._last_ntstatus = None
        access = FILE_READ_ATTRIBUTES | SYNCHRONIZE
        access |= FILE_LIST_DIRECTORY if kind == "directory" else FILE_READ_DATA
        if delete_access:
            access |= DELETE
        if (create or write_access) and kind == "file":
            access |= FILE_WRITE_DATA
        share_mode = FILE_SHARE_READ | FILE_SHARE_WRITE
        if getattr(self, "_allow_delete_share", False):
            share_mode |= FILE_SHARE_DELETE
        status = self._ntdll.NtCreateFile(
            byref(handle),
            access,
            byref(attributes),
            byref(io),
            None,
            0,
            share_mode,
            FILE_CREATE if create else FILE_OPEN,
            options,
            None,
            0,
        )
        if status < 0:
            self._last_ntstatus = status
            raise RepositoryUnsafeError("NO_FOLLOW_OPEN_DENIED")
        if handle.value is None:
            raise RepositoryUnsafeError("NO_FOLLOW_OPEN_DENIED")
        return self._identity(int(handle.value), components, kind)

    def create_child_directory(self, parent: OpenedNode, name: str) -> OpenedNode:
        try:
            return self._open_relative(
                parent.handle, name, parent.components + (name,), "directory", create=True
            )
        except RepositoryUnsafeError as error:
            if self._last_ntstatus == STATUS_OBJECT_NAME_COLLISION:
                raise RepositoryUnsafeError("NO_FOLLOW_CREATE_RACE") from error
            raise

    def create_child_file(self, parent: OpenedNode, name: str) -> OpenedNode:
        try:
            return self._open_relative(
                parent.handle, name, parent.components + (name,), "file", create=True
            )
        except RepositoryUnsafeError as error:
            if self._last_ntstatus == STATUS_OBJECT_NAME_COLLISION:
                raise RepositoryUnsafeError("NO_FOLLOW_CREATE_RACE") from error
            raise

    def _open_volume_root(self, root: str) -> OpenedNode:
        share_mode = FILE_SHARE_READ | FILE_SHARE_WRITE
        if getattr(self, "_allow_delete_share", False):
            share_mode |= FILE_SHARE_DELETE
        handle = self._kernel32.CreateFileW(
            root,
            FILE_READ_ATTRIBUTES | FILE_LIST_DIRECTORY | SYNCHRONIZE,
            share_mode,
            None,
            OPEN_EXISTING,
            FILE_FLAG_OPEN_REPARSE_POINT | FILE_FLAG_BACKUP_SEMANTICS,
            None,
        )
        if handle is None or handle == INVALID_HANDLE_VALUE:
            raise RepositoryUnsafeError("NO_FOLLOW_VOLUME_OPEN_DENIED")
        return self._identity(int(handle), (), "directory")

    def _read_file_handle_bounded(self, handle: int, maximum: int) -> bytes:
        buffer = create_string_buffer(maximum + 1)
        read = wintypes.DWORD()
        if not self._kernel32.SetFilePointerEx(wintypes.HANDLE(handle), 0, None, 0):
            raise RepositoryUnsafeError("HANDLE_SEEK_FAILED")
        if not self._kernel32.ReadFile(
            wintypes.HANDLE(handle), buffer, maximum + 1, byref(read), None
        ):
            raise RepositoryUnsafeError("HANDLE_READ_FAILED")
        if read.value > maximum:
            raise RepositoryUnsafeError("GIT_METADATA_TOO_LARGE")
        return bytes(buffer.raw[: read.value])

    def _query_directory_handle_names(self, handle: int, maximum: int) -> tuple[str, ...]:
        if maximum < 0:
            raise RepositoryUnsafeError("GIT_DIRECTORY_INVENTORY_INVALID")
        names: set[str] = set()
        restart_scan = 1
        while True:
            buffer = create_string_buffer(DIRECTORY_QUERY_BUFFER_SIZE)
            io = IO_STATUS_BLOCK()
            status = self._ntdll.NtQueryDirectoryFile(
                wintypes.HANDLE(handle),
                None,
                None,
                None,
                byref(io),
                buffer,
                sizeof(buffer),
                FILE_ID_BOTH_DIRECTORY_INFORMATION,
                0,
                None,
                restart_scan,
            )
            restart_scan = 0
            if status == STATUS_NO_MORE_FILES:
                break
            if status != STATUS_SUCCESS or not 0 < io.Information <= sizeof(buffer):
                raise RepositoryUnsafeError("GIT_DIRECTORY_INVENTORY_INVALID")
            self._collect_directory_names(buffer.raw[: io.Information], names, maximum)
        return tuple(sorted(names))

    @staticmethod
    def _collect_directory_names(data: bytes, names: set[str], maximum: int) -> None:
        offset = 0
        while True:
            if len(data) - offset < FILE_ID_BOTH_DIRECTORY_HEADER_SIZE:
                raise RepositoryUnsafeError("GIT_DIRECTORY_INVENTORY_INVALID")
            next_offset = struct.unpack_from("<I", data, offset)[0]
            name_length = struct.unpack_from("<I", data, offset + 60)[0]
            if next_offset:
                if next_offset % 8 or next_offset < FILE_ID_BOTH_DIRECTORY_HEADER_SIZE:
                    raise RepositoryUnsafeError("GIT_DIRECTORY_INVENTORY_INVALID")
                entry_end = offset + next_offset
                if entry_end > len(data):
                    raise RepositoryUnsafeError("GIT_DIRECTORY_INVENTORY_INVALID")
            else:
                entry_end = len(data)
            name_start = offset + FILE_ID_BOTH_DIRECTORY_HEADER_SIZE
            name_end = name_start + name_length
            if name_length == 0 or name_length % 2 or name_end > entry_end:
                raise RepositoryUnsafeError("GIT_DIRECTORY_INVENTORY_INVALID")
            try:
                name = data[name_start:name_end].decode("utf-16-le", errors="strict")
            except UnicodeDecodeError as error:
                raise RepositoryUnsafeError("GIT_DIRECTORY_INVENTORY_INVALID") from error
            if name not in {".", ".."}:
                if not name or "\x00" in name or "/" in name or "\\" in name or name in names:
                    raise RepositoryUnsafeError("GIT_DIRECTORY_INVENTORY_INVALID")
                names.add(name)
                if len(names) > maximum:
                    raise RepositoryUnsafeError("GIT_DIRECTORY_INVENTORY_INVALID")
            if not next_offset:
                return
            offset = entry_end

    def _last_status_is_object_name_not_found(self) -> bool:
        return self._last_ntstatus in {
            STATUS_OBJECT_NAME_NOT_FOUND,
            STATUS_OBJECT_PATH_NOT_FOUND,
        }

    def _last_status_is_not_a_directory(self) -> bool:
        return self._last_ntstatus == STATUS_NOT_A_DIRECTORY

    def open_root_chain(self, root: Path) -> tuple[OpenedNode, ...]:
        parsed = PureWindowsPath(root)
        if not parsed.is_absolute() or parsed.root != "\\" or parsed.drive.startswith("\\"):
            raise RepositoryUnsafeError("LOCAL_DRIVE_REPOSITORY_ROOT_REQUIRED")
        volume = self._open_volume_root(parsed.drive + "\\")
        chain = [volume]
        try:
            components: tuple[str, ...] = ()
            for name in parsed.parts[1:]:
                components += (name,)
                chain.append(self._open_relative(chain[-1].handle, name, components, "directory"))
            return tuple(chain)
        except Exception as error:
            cleanup_error: RepositoryUnsafeError | None = None
            for node in reversed(chain):
                try:
                    self.close(node)
                except RepositoryUnsafeError as close_error:
                    if cleanup_error is None:
                        cleanup_error = close_error
            if cleanup_error is not None:
                error.add_note(f"root chain cleanup failed: {cleanup_error}")
            raise

    def open_child(self, parent: OpenedNode, name: str, kind: NodeKind) -> OpenedNode:
        return self._open_relative(parent.handle, name, parent.components + (name,), kind)

    def open_child_for_write(self, parent: OpenedNode, name: str) -> OpenedNode:
        return self._open_relative(
            parent.handle,
            name,
            parent.components + (name,),
            "file",
            write_access=True,
        )

    def open_child_for_delete(self, parent: OpenedNode, name: str, kind: NodeKind) -> OpenedNode:
        return self._open_relative(
            parent.handle,
            name,
            parent.components + (name,),
            kind,
            delete_access=True,
        )

    def try_open_child(self, parent: OpenedNode, name: str, kind: NodeKind) -> OpenedNode | None:
        try:
            return self.open_child(parent, name, kind)
        except RepositoryUnsafeError:
            if self._last_status_is_object_name_not_found():
                return None
            raise

    def try_open_child_any(self, parent: OpenedNode, name: str) -> OpenedNode | None:
        try:
            return self.open_child(parent, name, "directory")
        except RepositoryUnsafeError:
            if self._last_status_is_object_name_not_found():
                return None
            if not self._last_status_is_not_a_directory():
                raise
        return self.try_open_child(parent, name, "file")

    def read_bytes(self, node: OpenedNode, maximum: int) -> bytes:
        return self._read_file_handle_bounded(node.handle, maximum)

    def write_bytes(self, node: OpenedNode, value: bytes) -> None:
        buffer = create_string_buffer(value)
        written = wintypes.DWORD()
        if not self._kernel32.SetFilePointerEx(wintypes.HANDLE(node.handle), 0, None, 0):
            raise RepositoryUnsafeError("HANDLE_SEEK_FAILED")
        if not self._kernel32.WriteFile(
            wintypes.HANDLE(node.handle), buffer, len(value), byref(written), None
        ) or written.value != len(value):
            raise RepositoryUnsafeError("NO_FOLLOW_WRITE_DENIED")

    def list_names(self, node: OpenedNode, maximum: int) -> tuple[str, ...]:
        return self._query_directory_handle_names(node.handle, maximum)

    def _delete_handle(self, expected: OpenedNode) -> None:
        disposition = c_ubyte(1)
        if not self._kernel32.SetFileInformationByHandle(
            wintypes.HANDLE(expected.handle),
            FILE_DISPOSITION_INFO,
            byref(disposition),
            sizeof(disposition),
        ):
            raise RepositoryUnsafeError("DELETE_FAILED")

    def unlink_child(self, parent: OpenedNode, name: str, expected: OpenedNode) -> None:
        del parent
        if expected.identity.kind != "file" or not name or "/" in name or "\\" in name:
            raise RepositoryUnsafeError("DELETE_TARGET_INVALID")
        self._delete_handle(expected)

    def remove_child_directory(self, parent: OpenedNode, name: str, expected: OpenedNode) -> None:
        del parent
        if expected.identity.kind != "directory" or not name or "/" in name or "\\" in name:
            raise RepositoryUnsafeError("DELETE_TARGET_INVALID")
        self._delete_handle(expected)

    def close(self, node: OpenedNode) -> None:
        if not self._kernel32.CloseHandle(wintypes.HANDLE(node.handle)):
            raise RepositoryUnsafeError("HANDLE_CLOSE_FAILED")
