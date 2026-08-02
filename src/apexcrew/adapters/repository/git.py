from __future__ import annotations

import hashlib
import hmac
import os
import struct
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal, Protocol, cast

from apexcrew.adapters.repository.no_follow import (
    HandleIdentity,
    OpenedNode,
    StableHandleTree,
)
from apexcrew.adapters.repository.no_follow import RepositoryUnsafeError as _RepositoryUnsafeError
from apexcrew.adapters.repository.no_follow_posix import PosixNoFollowBackend
from apexcrew.adapters.repository.no_follow_windows import WindowsNoFollowBackend
from apexcrew.domain.plan import CanonicalPath, PathValidationError
from apexcrew.domain.types import GitOid

RepositoryUnsafeError = _RepositoryUnsafeError

MAX_GIT_CONFIG_BYTES = 1_048_576
MAX_GIT_CONFIG_LOGICAL_LINE_BYTES = 65_536
MAX_GIT_CONFIG_PHYSICAL_LINES = 16_384
MAX_GIT_CONFIG_ENTRIES = 4_096
MAX_GIT_INDEX_BYTES = 67_108_864
MAX_GIT_INDEX_ENTRIES = 1_000_000
MAX_GIT_ADMIN_ENTRIES = 4_096
MAX_WORKTREE_ADMIN_ENTRIES = 2


@dataclass(frozen=True, slots=True)
class GitConfigEntry:
    section: bytes
    subsection: bytes | None
    name: bytes
    value: bytes


_IDENTIFIER_FIRST = frozenset(range(ord("A"), ord("Z") + 1)) | frozenset(
    range(ord("a"), ord("z") + 1)
)
_IDENTIFIER_REST = _IDENTIFIER_FIRST | frozenset(range(ord("0"), ord("9") + 1)) | {ord("-")}
_QUOTED_ESCAPES = {
    ord('"'): ord('"'),
    ord("\\"): ord("\\"),
    ord("n"): ord("\n"),
    ord("t"): ord("\t"),
    ord("b"): ord("\b"),
}


def _config_syntax_error() -> RepositoryUnsafeError:
    return RepositoryUnsafeError("GIT_CONFIG_SYNTAX_INVALID")


def _without_comment(line: bytes) -> bytes:
    quoted = False
    escaped = False
    for index, byte in enumerate(line):
        if escaped:
            escaped = False
        elif byte == ord("\\"):
            escaped = True
        elif byte == ord('"'):
            quoted = not quoted
        elif not quoted and byte in {ord("#"), ord(";")}:
            return line[:index]
    return line


def _logical_config_lines(raw: bytes) -> tuple[bytes, ...]:
    if len(raw) > MAX_GIT_CONFIG_BYTES:
        raise RepositoryUnsafeError("GIT_CONFIG_LIMIT_EXCEEDED")
    if b"\x00" in raw:
        raise _config_syntax_error()
    physical = raw.split(b"\n")
    if len(physical) > MAX_GIT_CONFIG_PHYSICAL_LINES:
        raise RepositoryUnsafeError("GIT_CONFIG_LIMIT_EXCEEDED")
    logical: list[bytes] = []
    pending = bytearray()
    for index, physical_line in enumerate(physical):
        line = physical_line
        if line.endswith(b"\r"):
            line = line[:-1]
        if b"\r" in line:
            raise _config_syntax_error()
        if pending:
            line = line.lstrip(b" \t")
        continuation = line.endswith(b"\\") and len(_without_comment(line)) == len(line)
        pending.extend(line[:-1] if continuation else line)
        if len(pending) > MAX_GIT_CONFIG_LOGICAL_LINE_BYTES:
            raise RepositoryUnsafeError("GIT_CONFIG_LIMIT_EXCEEDED")
        if continuation:
            if index == len(physical) - 1:
                raise _config_syntax_error()
            continue
        logical.append(bytes(pending))
        pending.clear()
    return tuple(logical)


def _identifier(raw: bytes) -> bytes:
    if (
        not raw
        or raw[0] not in _IDENTIFIER_FIRST
        or any(byte not in _IDENTIFIER_REST for byte in raw[1:])
    ):
        raise _config_syntax_error()
    return raw.lower()


def _quoted(raw: bytes, offset: int) -> tuple[bytes, int]:
    if offset >= len(raw) or raw[offset] != ord('"'):
        raise _config_syntax_error()
    value = bytearray()
    offset += 1
    while offset < len(raw):
        byte = raw[offset]
        if byte == ord('"'):
            return bytes(value), offset + 1
        if byte == ord("\\"):
            offset += 1
            if offset >= len(raw) or raw[offset] not in _QUOTED_ESCAPES:
                raise _config_syntax_error()
            value.append(_QUOTED_ESCAPES[raw[offset]])
        elif byte < 32 and byte != ord("\t"):
            raise _config_syntax_error()
        else:
            value.append(byte)
        offset += 1
    raise _config_syntax_error()


def _section_header(line: bytes) -> tuple[bytes, bytes | None]:
    payload = _without_comment(line).strip(b" \t")
    if not payload.startswith(b"[") or not payload.endswith(b"]"):
        raise _config_syntax_error()
    inner = payload[1:-1].strip(b" \t")
    boundary = 0
    while boundary < len(inner) and inner[boundary] not in {
        ord("."),
        ord(" "),
        ord("\t"),
    }:
        boundary += 1
    section = _identifier(inner[:boundary])
    if boundary == len(inner):
        return section, None
    if inner[boundary] == ord("."):
        subsection = inner[boundary + 1 :]
        if not subsection or any(byte < 32 or byte in {ord("["), ord("]")} for byte in subsection):
            raise _config_syntax_error()
        return section, subsection.lower()
    remainder = inner[boundary:].lstrip(b" \t")
    subsection, end = _quoted(remainder, 0)
    if remainder[end:].strip(b" \t") or any(byte < 32 for byte in subsection):
        raise _config_syntax_error()
    return section, subsection


def _config_value(raw: bytes) -> bytes:
    source = raw.strip(b" \t")
    value = bytearray()
    quoted = False
    offset = 0
    while offset < len(source):
        byte = source[offset]
        if byte == ord('"'):
            quoted = not quoted
        elif byte == ord("\\"):
            offset += 1
            if offset >= len(source) or source[offset] not in _QUOTED_ESCAPES:
                raise _config_syntax_error()
            value.append(_QUOTED_ESCAPES[source[offset]])
        elif byte < 32 and byte != ord("\t"):
            raise _config_syntax_error()
        else:
            value.append(byte)
        offset += 1
    if quoted:
        raise _config_syntax_error()
    return bytes(value)


def parse_git_config(raw: bytes) -> tuple[GitConfigEntry, ...]:
    current_section: tuple[bytes, bytes | None] | None = None
    entries: list[GitConfigEntry] = []
    for logical_line in _logical_config_lines(raw):
        payload = _without_comment(logical_line).strip(b" \t")
        if not payload:
            continue
        if payload.startswith(b"["):
            current_section = _section_header(payload)
            continue
        if current_section is None:
            raise _config_syntax_error()
        separator = payload.find(b"=")
        if separator < 0:
            name = _identifier(payload.strip(b" \t"))
            value = b"true"
        else:
            name = _identifier(payload[:separator].strip(b" \t"))
            value = _config_value(payload[separator + 1 :])
        entries.append(GitConfigEntry(current_section[0], current_section[1], name, value))
        if len(entries) > MAX_GIT_CONFIG_ENTRIES:
            raise RepositoryUnsafeError("GIT_CONFIG_LIMIT_EXCEEDED")
    return tuple(entries)


_UNSUPPORTED_CONFIG_KEYS = frozenset(
    {
        (b"extensions", b"worktreeconfig"),
        (b"core", b"sparsecheckout"),
        (b"core", b"sparsecheckoutcone"),
        (b"index", b"sparse"),
        (b"core", b"splitindex"),
        (b"core", b"commitgraph"),
        (b"core", b"multipackindex"),
        (b"pack", b"usebitmap"),
    }
)
_ROUTING_CONFIG_KEYS = frozenset(
    {
        (b"safe", b"barerepository"),
        (b"core", b"worktree"),
        (b"core", b"hookspath"),
        (b"core", b"fsmonitor"),
        (b"core", b"sshcommand"),
        (b"core", b"askpass"),
        (b"core", b"pager"),
        (b"core", b"editor"),
        (b"core", b"attributesfile"),
        (b"core", b"excludesfile"),
        (b"core", b"alternaterefscommand"),
        (b"http", b"proxy"),
        (b"http", b"extraheader"),
        (b"credential", b"helper"),
        (b"gpg", b"program"),
        (b"user", b"signingkey"),
        (b"commit", b"gpgsign"),
        (b"tag", b"gpgsign"),
    }
)
_ROUTING_NAMES_BY_SECTION = {
    b"remote": frozenset(
        {b"promisor", b"partialclonefilter", b"proxy", b"uploadpack", b"receivepack"}
    ),
    b"url": frozenset({b"insteadof", b"pushinsteadof"}),
    b"filter": frozenset({b"clean", b"smudge", b"process", b"required"}),
    b"diff": frozenset({b"command", b"textconv"}),
    b"merge": frozenset({b"driver", b"recursive"}),
}


def reject_unsafe_config(entries: tuple[GitConfigEntry, ...]) -> None:
    for entry in entries:
        key = (entry.section, entry.name)
        if entry.section in {b"include", b"includeif"}:
            raise RepositoryUnsafeError("CONFIG_INCLUDE_DENIED")
        if key in _UNSUPPORTED_CONFIG_KEYS or entry.section == b"commitgraph":
            raise RepositoryUnsafeError("UNSUPPORTED_GIT_CONFIG_FEATURE")
        routed_names = _ROUTING_NAMES_BY_SECTION.get(entry.section, frozenset())
        if (
            key in _ROUTING_CONFIG_KEYS
            or entry.section in {b"alias", b"pager"}
            or entry.name in routed_names
        ):
            raise RepositoryUnsafeError("EXTERNAL_GIT_ROUTING_DENIED")


def _object_hash_name(
    entries: tuple[GitConfigEntry, ...],
) -> Literal["sha1", "sha256"]:
    configured = {
        entry.value.lower()
        for entry in entries
        if (entry.section, entry.name) == (b"extensions", b"objectformat")
    }
    if not configured:
        return "sha1"
    if configured == {b"sha1"}:
        return "sha1"
    if configured == {b"sha256"}:
        return "sha256"
    raise RepositoryUnsafeError("GIT_OBJECT_FORMAT_UNSUPPORTED")


def _u32(raw: bytes, offset: int) -> int:
    if offset + 4 > len(raw):
        raise RepositoryUnsafeError("GIT_INDEX_INVALID")
    return cast(int, struct.unpack_from(">I", raw, offset)[0])


def _v4_strip_count(raw: bytes, offset: int) -> tuple[int, int]:
    value = 0
    while offset < len(raw):
        byte = raw[offset]
        offset += 1
        value = (value << 7) + (byte & 0x7F)
        if byte & 0x80 == 0:
            return value, offset
        value += 1
        if value > MAX_GIT_INDEX_BYTES:
            break
    raise RepositoryUnsafeError("GIT_INDEX_INVALID")


def validate_git_index(raw: bytes, object_hash: Literal["sha1", "sha256"]) -> None:
    if len(raw) > MAX_GIT_INDEX_BYTES:
        raise RepositoryUnsafeError("GIT_INDEX_LIMIT_EXCEEDED")
    digest_size = hashlib.new(object_hash).digest_size
    if len(raw) < 12 + digest_size:
        raise RepositoryUnsafeError("GIT_INDEX_INVALID")
    payload, expected_checksum = raw[:-digest_size], raw[-digest_size:]
    if not hmac.compare_digest(hashlib.new(object_hash, payload).digest(), expected_checksum):
        raise RepositoryUnsafeError("GIT_INDEX_CHECKSUM_INVALID")
    if payload[:4] != b"DIRC":
        raise RepositoryUnsafeError("GIT_INDEX_INVALID")
    version = _u32(payload, 4)
    entry_count = _u32(payload, 8)
    if version not in {2, 3, 4} or entry_count > MAX_GIT_INDEX_ENTRIES:
        raise RepositoryUnsafeError("GIT_INDEX_INVALID")
    cursor = 12
    previous_path = b""
    for _ in range(entry_count):
        entry_start = cursor
        fixed_size = 40 + digest_size + 2
        if cursor + fixed_size > len(payload):
            raise RepositoryUnsafeError("GIT_INDEX_INVALID")
        mode = _u32(payload, cursor + 24)
        flags = struct.unpack_from(">H", payload, cursor + 40 + digest_size)[0]
        cursor += fixed_size
        if flags & 0x4000:
            if version < 3 or cursor + 2 > len(payload):
                raise RepositoryUnsafeError("GIT_INDEX_INVALID")
            cursor += 2
        if version == 4:
            strip_count, cursor = _v4_strip_count(payload, cursor)
            end = payload.find(b"\x00", cursor)
            if end < 0 or strip_count > len(previous_path):
                raise RepositoryUnsafeError("GIT_INDEX_INVALID")
            path = previous_path[: len(previous_path) - strip_count] + payload[cursor:end]
            cursor = end + 1
        else:
            end = payload.find(b"\x00", cursor)
            if end < 0:
                raise RepositoryUnsafeError("GIT_INDEX_INVALID")
            path = payload[cursor:end]
            declared_length = flags & 0x0FFF
            if declared_length != 0x0FFF and declared_length != len(path):
                raise RepositoryUnsafeError("GIT_INDEX_INVALID")
            cursor = end + 1
            entry_size = cursor - entry_start
            cursor = entry_start + ((entry_size + 7) // 8) * 8
            if cursor > len(payload):
                raise RepositoryUnsafeError("GIT_INDEX_INVALID")
        try:
            CanonicalPath.parse(path.decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, PathValidationError) as error:
            raise RepositoryUnsafeError("GIT_INDEX_PATH_INVALID") from error
        if mode & 0o170000 == 0o040000:
            raise RepositoryUnsafeError("SPARSE_INDEX_DENIED")
        previous_path = path
    while cursor < len(payload):
        if cursor + 8 > len(payload):
            raise RepositoryUnsafeError("GIT_INDEX_INVALID")
        signature = payload[cursor : cursor + 4]
        extension_size = _u32(payload, cursor + 4)
        cursor += 8
        if cursor + extension_size > len(payload):
            raise RepositoryUnsafeError("GIT_INDEX_INVALID")
        if signature == b"link":
            raise RepositoryUnsafeError("SPLIT_INDEX_DENIED")
        if signature == b"sdir":
            raise RepositoryUnsafeError("SPARSE_INDEX_DENIED")
        if signature and ord("a") <= signature[0] <= ord("z"):
            raise RepositoryUnsafeError("GIT_INDEX_REQUIRED_EXTENSION_UNSUPPORTED")
        cursor += extension_size


@dataclass(frozen=True, slots=True)
class GitLayout:
    handles: StableHandleTree
    root: OpenedNode
    git_dir: OpenedNode
    config: OpenedNode
    index: OpenedNode | None
    worktree_admin_entries: tuple[str, ...]


def inspect_no_follow_git_layout(root: Path) -> GitLayout:
    backend = WindowsNoFollowBackend() if os.name == "nt" else PosixNoFollowBackend()
    handles = StableHandleTree(root, backend)
    try:
        git_dir = handles.open(".git", "directory")
        config = handles.open(".git/config", "file")
        index = handles.try_open(".git/index", "file")
        worktrees = handles.try_open(".git/worktrees", "directory")
        entries = (
            () if worktrees is None else handles.list_names(worktrees, MAX_WORKTREE_ADMIN_ENTRIES)
        )
        return GitLayout(handles, handles.root_node, git_dir, config, index, entries)
    except BaseException:
        handles.close()
        raise


def reject_unsafe_layout(layout: GitLayout) -> None:
    raw_config = layout.handles.read_bytes(layout.config, MAX_GIT_CONFIG_BYTES)
    config_entries = parse_git_config(raw_config)
    reject_unsafe_config(config_entries)
    object_hash = _object_hash_name(config_entries)

    git_names = set(layout.handles.list_names(layout.git_dir, MAX_GIT_ADMIN_ENTRIES))
    if any(name.startswith("sharedindex.") for name in git_names):
        raise RepositoryUnsafeError("SPLIT_INDEX_DENIED")
    if "config.worktree" in git_names:
        raise RepositoryUnsafeError("UNSUPPORTED_GIT_CONFIG_FEATURE")
    if {"shallow", "shallow.lock", "commondir"} & git_names:
        raise RepositoryUnsafeError("UNSUPPORTED_GIT_STORAGE")
    if "index.lock" in git_names:
        raise RepositoryUnsafeError("GIT_OPERATION_IN_PROGRESS")

    info = layout.handles.try_open(".git/info", "directory")
    info_names = (
        set() if info is None else set(layout.handles.list_names(info, MAX_GIT_ADMIN_ENTRIES))
    )
    if {"sparse-checkout", "sparse-checkout.lock"} & info_names:
        raise RepositoryUnsafeError("SPARSE_INDEX_DENIED")
    if {"grafts", "grafts.lock"} & info_names:
        raise RepositoryUnsafeError("UNSUPPORTED_GIT_STORAGE")

    objects_info = layout.handles.try_open(".git/objects/info", "directory")
    objects_info_names = (
        set()
        if objects_info is None
        else set(layout.handles.list_names(objects_info, MAX_GIT_ADMIN_ENTRIES))
    )
    if "alternates" in objects_info_names:
        raise RepositoryUnsafeError("UNSUPPORTED_GIT_STORAGE")

    if (
        layout.index is None
        and {
            "MERGE_HEAD",
            "CHERRY_PICK_HEAD",
            "REVERT_HEAD",
            "rebase-merge",
            "rebase-apply",
            "sequencer",
            "BISECT_LOG",
        }
        & git_names
    ):
        raise RepositoryUnsafeError("GIT_OPERATION_IN_PROGRESS")
    if layout.index is not None:
        validate_git_index(
            layout.handles.read_bytes(layout.index, MAX_GIT_INDEX_BYTES),
            object_hash,
        )
    if layout.worktree_admin_entries:
        raise RepositoryUnsafeError("UNSUPPORTED_LINKED_WORKTREE")


MAX_GIT_STORAGE_DEPTH = 16
MAX_GIT_STORAGE_PATHS = 32_768


@dataclass(frozen=True, slots=True)
class BoundGitStorageNode:
    relative: str
    identity: HandleIdentity
    names: tuple[str, ...] | None


@dataclass(frozen=True, slots=True)
class GitStorageSnapshot:
    nodes: tuple[BoundGitStorageNode, ...]

    @classmethod
    def capture(cls, handles: StableHandleTree) -> GitStorageSnapshot:
        captured: list[BoundGitStorageNode] = []

        def visit(relative: str, depth: int) -> None:
            if depth > MAX_GIT_STORAGE_DEPTH or len(captured) >= MAX_GIT_STORAGE_PATHS:
                raise RepositoryUnsafeError("GIT_STORAGE_INVENTORY_LIMIT_EXCEEDED")
            node = handles.try_open_any(relative)
            if node is None:
                return
            names: tuple[str, ...] | None = None
            if node.identity.kind == "directory":
                names = handles.list_names(node, MAX_GIT_STORAGE_PATHS - len(captured))
            captured.append(BoundGitStorageNode(relative, node.identity, names))
            if names is not None:
                for name in names:
                    visit(f"{relative}/{name}", depth + 1)

        visit(".git", 0)
        return cls(tuple(captured))

    def assert_current(self, handles: StableHandleTree) -> None:
        handles.assert_name_bindings()
        if GitStorageSnapshot.capture(handles) != self:
            raise RepositoryUnsafeError("GIT_TRAVERSED_STORAGE_CHANGED")


@dataclass(frozen=True, slots=True)
class RepositoryInstance:
    root: Path
    handles: StableHandleTree
    root_identity: HandleIdentity
    git_dir_identity: HandleIdentity
    config_identity: HandleIdentity
    index_identity: HandleIdentity | None
    storage_snapshot: GitStorageSnapshot

    @classmethod
    def from_layout(cls, layout: GitLayout) -> RepositoryInstance:
        return cls(
            layout.handles.root,
            layout.handles,
            layout.root.identity,
            layout.git_dir.identity,
            layout.config.identity,
            None if layout.index is None else layout.index.identity,
            GitStorageSnapshot.capture(layout.handles),
        )

    def assert_stable(self) -> None:
        self.storage_snapshot.assert_current(self.handles)

    def assert_stable_for(self, operation: object) -> None:
        del operation
        self.assert_stable()

    def refresh_after_verified_owned_transition(self) -> RepositoryInstance:
        self.handles.assert_name_bindings()
        return replace(self, storage_snapshot=GitStorageSnapshot.capture(self.handles))

    def close(self) -> None:
        self.handles.close()


class GitRepositoryPreflight:
    def inspect(self, root: Path) -> RepositoryInstance:
        layout = inspect_no_follow_git_layout(root)
        try:
            reject_unsafe_layout(layout)
            layout.handles.assert_name_bindings()
            return RepositoryInstance.from_layout(layout)
        except BaseException:
            layout.handles.close()
            raise


def closed_git_environment(trusted_empty_dir: Path) -> dict[str, str]:
    return {
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": str(trusted_empty_dir / "global.gitconfig"),
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "",
        "SSH_ASKPASS": "",
        "GIT_SSH_COMMAND": "false",
        "GIT_PAGER": "cat",
        "GIT_EDITOR": "false",
        "GIT_OPTIONAL_LOCKS": "0",
    }


@dataclass(frozen=True, slots=True)
class GitStatusPorcelain:
    pass


@dataclass(frozen=True, slots=True)
class GitWorktreeListPorcelain:
    pass


@dataclass(frozen=True, slots=True)
class GitWorktreeAddNoCheckout:
    reservation_path: Path
    target_ref: str


@dataclass(frozen=True, slots=True)
class GitWorktreeLock:
    reservation_path: Path
    reason: str


@dataclass(frozen=True, slots=True)
class GitLsTreeRecursive:
    tree_oid: GitOid


@dataclass(frozen=True, slots=True)
class GitLsTreePath:
    tree_oid: GitOid
    path: CanonicalPath


@dataclass(frozen=True, slots=True)
class GitCatFileBlob:
    blob_oid: GitOid


@dataclass(frozen=True, slots=True)
class GitCatFileSize:
    blob_oid: GitOid


type GitOperation = (
    GitStatusPorcelain
    | GitWorktreeListPorcelain
    | GitWorktreeAddNoCheckout
    | GitWorktreeLock
    | GitLsTreeRecursive
    | GitLsTreePath
    | GitCatFileBlob
    | GitCatFileSize
)


class GitSpawner(Protocol):
    def run(
        self,
        argv: tuple[str, ...],
        cwd: Path,
        environment: Mapping[str, str],
        *,
        text: bool,
    ) -> subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes]: ...


class SubprocessGitSpawner:
    def run(
        self,
        argv: tuple[str, ...],
        cwd: Path,
        environment: Mapping[str, str],
        *,
        text: bool,
    ) -> subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes]:
        return cast(
            subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes],
            subprocess.run(
                argv,
                cwd=cwd,
                env=dict(environment),
                check=False,
                capture_output=True,
                text=text,
                timeout=30,
            ),
        )


class GitCommandRunner:
    def __init__(
        self,
        git_executable: Path,
        trusted_empty_dir: Path,
        spawner: GitSpawner | None = None,
    ) -> None:
        if not git_executable.is_absolute():
            raise ValueError("ABSOLUTE_GIT_EXECUTABLE_REQUIRED")
        self._git = git_executable
        self._trusted_empty_dir = trusted_empty_dir
        self._spawner = SubprocessGitSpawner() if spawner is None else spawner

    def run(
        self, repository: RepositoryInstance, operation: GitOperation
    ) -> subprocess.CompletedProcess[str]:
        result = self._run(repository, operation, text=True)
        assert isinstance(result.stdout, str)
        return result

    def run_bytes(
        self, repository: RepositoryInstance, operation: GitOperation
    ) -> subprocess.CompletedProcess[bytes]:
        result = self._run(repository, operation, text=False)
        assert isinstance(result.stdout, bytes)
        return result

    def _run(
        self, repository: RepositoryInstance, operation: GitOperation, *, text: bool
    ) -> subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes]:
        if not isinstance(
            operation,
            (
                GitStatusPorcelain,
                GitWorktreeListPorcelain,
                GitWorktreeAddNoCheckout,
                GitWorktreeLock,
                GitLsTreeRecursive,
                GitLsTreePath,
                GitCatFileBlob,
                GitCatFileSize,
            ),
        ):
            raise RepositoryUnsafeError("RAW_GIT_ARGUMENTS_DENIED")
        repository.assert_stable_for(operation)
        command = (
            str(self._git),
            "-c",
            "core.hooksPath=" + str(self._trusted_empty_dir / "hooks"),
            "-c",
            "commit.gpgSign=false",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "diff.external=",
            "-c",
            "credential.helper=",
            *self._argv_for(operation),
        )
        return self._spawner.run(
            command,
            repository.root,
            closed_git_environment(self._trusted_empty_dir),
            text=text,
        )

    def _argv_for(self, operation: GitOperation) -> tuple[str, ...]:
        match operation:
            case GitStatusPorcelain():
                return ("status", "--porcelain=v1", "--untracked-files=no")
            case GitWorktreeListPorcelain():
                return ("worktree", "list", "--porcelain", "-z")
            case GitWorktreeAddNoCheckout(reservation_path=path, target_ref=target):
                self._require_direct_ref(target)
                self._require_operand_path(path)
                return ("worktree", "add", "--no-checkout", "--", str(path), target)
            case GitWorktreeLock(reservation_path=path, reason=reason):
                self._require_operand_path(path)
                self._require_reason(reason)
                return ("worktree", "lock", "--reason", reason, "--", str(path))
            case GitLsTreeRecursive(tree_oid=tree_oid):
                return ("ls-tree", "-r", "-z", self._require_oid(tree_oid))
            case GitLsTreePath(tree_oid=tree_oid, path=path):
                return ("ls-tree", "-z", self._require_oid(tree_oid), "--", str(path))
            case GitCatFileBlob(blob_oid=blob_oid):
                return ("cat-file", "blob", self._require_oid(blob_oid))
            case GitCatFileSize(blob_oid=blob_oid):
                return ("cat-file", "-s", self._require_oid(blob_oid))
        raise AssertionError("GIT_OPERATION_UNION_EXHAUSTIVENESS")

    @staticmethod
    def _require_oid(value: GitOid) -> str:
        text = str(value)
        if len(text) != 40 or any(character not in "0123456789abcdef" for character in text):
            raise RepositoryUnsafeError("GIT_OID_OPERAND_INVALID")
        return text

    @staticmethod
    def _require_direct_ref(value: str) -> None:
        if (
            not value.startswith("refs/heads/")
            or value == "refs/heads/"
            or any(character.isspace() or character == "\x00" for character in value)
        ):
            raise RepositoryUnsafeError("GIT_REF_OPERAND_INVALID")

    @staticmethod
    def _require_operand_path(value: Path) -> None:
        text = str(value)
        if not value.is_absolute() or text.startswith("-") or "\x00" in text:
            raise RepositoryUnsafeError("GIT_PATH_OPERAND_INVALID")

    @staticmethod
    def _require_reason(value: str) -> None:
        if not value or "\x00" in value or "\n" in value or "\r" in value:
            raise RepositoryUnsafeError("GIT_LOCK_REASON_INVALID")
