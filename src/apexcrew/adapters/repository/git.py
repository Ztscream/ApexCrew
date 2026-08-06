from __future__ import annotations

import hashlib
import hmac
import os
import struct
import subprocess
import tempfile
import threading
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import IO, Literal, Protocol, cast

from apexcrew.adapters.repository.no_follow import (
    HandleIdentity,
    OpenedNode,
    StableHandleTree,
)
from apexcrew.adapters.repository.no_follow import RepositoryUnsafeError as _RepositoryUnsafeError
from apexcrew.adapters.repository.no_follow_posix import PosixNoFollowBackend
from apexcrew.adapters.repository.no_follow_windows import WindowsNoFollowBackend
from apexcrew.domain.admission import (
    PrivateRefAdmissionPort,
    PrivateRefCasOutcome,
    RefCasIntent,
    RefEffectBinding,
    RefPathBinding,
    RepositoryEffectError,
    ReservationAdminObservation,
    ReservationRegistrationObservation,
    RuntimeStartBinding,
    StartGuardBinding,
    StartGuardDecision,
    TargetReservationGitPort,
    TargetReservationObserver,
    TargetReservationOperation,
    TargetReservationOperationResult,
    TargetReservationWorktreeGuard,
)
from apexcrew.domain.commands import ApplicableRevisionDigests, RuntimePermit
from apexcrew.domain.effects import TargetReservation, canonical_json, sha256_digest
from apexcrew.domain.plan import CanonicalPath, PathValidationError
from apexcrew.domain.revisions import Sha256DigestText
from apexcrew.domain.types import AuditSequence, GitOid, RepositoryId, RunId


class GitPrivateRefStartGuard(PrivateRefAdmissionPort):
    """Observes and initializes only one Run-owned private ref."""

    def __init__(
        self,
        *,
        repository: RepositoryInstance,
        repository_id: RepositoryId,
        repository_instance_digest: Sha256DigestText,
        runner: GitCommandRunner,
        reservation_observer: TargetReservationObserver,
        reservation: TargetReservation,
        target_safety_digest: Sha256DigestText,
        reflog_message: str,
    ) -> None:
        self._repository = repository
        self._repository_id = repository_id
        self._repository_instance_digest = repository_instance_digest
        self._runner = runner
        self._reservation_observer = reservation_observer
        self._reservation = reservation
        self._target_safety_digest = target_safety_digest
        self._reflog_message = reflog_message

    @staticmethod
    def _identity_binding(node: OpenedNode | None) -> RefPathBinding:
        if node is None:
            return RefPathBinding(state="ABSENT")
        if node.identity.kind != "file":
            raise RepositoryEffectError("PRIVATE_REF_STORAGE_KIND_INVALID")
        identity = node.identity
        return RefPathBinding(
            state="REGULAR_FILE",
            identity_digest=sha256_digest(
                canonical_json(
                    {
                        "file_id": identity.file_id,
                        "kind": identity.kind,
                        "platform": identity.platform,
                        "volume": identity.volume,
                    }
                )
            ),
        )

    def _effect_binding(self, run_id: RunId) -> RefEffectBinding:
        ref_component = str(run_id)
        GitCommandRunner._require_private_ref(f"refs/apexcrew/runs/{ref_component}")
        handles = self._repository.handles
        records_result = self._runner.run_bytes(self._repository, GitWorktreeListPorcelain())
        if records_result.returncode != 0:
            raise RepositoryEffectError("PRIVATE_REF_WORKTREE_LIST_FAILED")
        records = parse_worktree_porcelain_nul(records_result.stdout)
        if any(record.branch == f"refs/apexcrew/runs/{ref_component}" for record in records):
            raise RepositoryEffectError("PRIVATE_REF_CHECKED_OUT")
        checkout_digest = sha256_digest(
            canonical_json(
                {
                    "records": [
                        {
                            "branch": record.branch,
                            "detached": record.detached,
                            "head_oid": record.head_oid,
                            "locked": record.locked,
                            "path": record.path,
                        }
                        for record in records
                    ]
                }
            )
        )
        ref_path = f".git/refs/apexcrew/runs/{ref_component}"
        reflog_path = f".git/logs/refs/apexcrew/runs/{ref_component}"
        ref_file = self._identity_binding(handles.try_open_any(ref_path))
        ref_lock = self._identity_binding(handles.try_open_any(ref_path + ".lock"))
        reflog = self._identity_binding(handles.try_open_any(reflog_path))
        reflog_lock = self._identity_binding(handles.try_open_any(reflog_path + ".lock"))
        return RefEffectBinding(
            repository_instance_digest=self._repository_instance_digest,
            checkout_registration_digest=checkout_digest,
            ref_file=ref_file,
            ref_lock=ref_lock,
            reflog=reflog,
            reflog_lock=reflog_lock,
            reflog_exists=reflog.state == "REGULAR_FILE",
            reflog_message=self._reflog_message,
        )

    def inspect(
        self,
        *,
        run_id: RunId,
        applicable_revision_digests: ApplicableRevisionDigests,
        expected_sequence: AuditSequence,
    ) -> StartGuardDecision:
        del expected_sequence
        if run_id != self._reservation.run_id:
            return StartGuardDecision(ok=False, reason="START_GUARD_RUN_MISMATCH")
        observed = self._reservation_observer.observe(self._reservation)
        if not (
            observed.observable
            and observed.registration_present
            and observed.path_present
            and observed.locked
            and observed.exact_identity
            and observed.gitfile_only
            and observed.admin_entry_name == self._reservation.reservation_id
            and observed.admin_binding_digest is not None
        ):
            return StartGuardDecision(ok=False, reason="TARGET_UNSAFE")
        target = self._runner.run(self._repository, GitShowRefVerify(self._reservation.target_ref))
        if target.returncode != 0 or target.stdout != f"{self._reservation.pinned_target_oid}\n":
            return StartGuardDecision(ok=False, reason="TARGET_MOVED")
        private_name = f"refs/apexcrew/runs/{run_id}"
        private = self._runner.run(self._repository, GitShowRefVerify(private_name))
        if private.returncode == 0:
            return StartGuardDecision(ok=False, reason="PRIVATE_REF_CONFLICT")
        absent_ref = private.returncode == 1 or (
            private.returncode == 128 and private.stderr.strip().endswith("not a valid ref")
        )
        if not absent_ref:
            return StartGuardDecision(ok=False, reason="PRIVATE_REF_UNOBSERVABLE")
        try:
            effect_binding = self._effect_binding(run_id)
        except (RepositoryEffectError, RepositoryUnsafeError, ValueError):
            return StartGuardDecision(ok=False, reason="PRIVATE_REF_UNOBSERVABLE")
        if any(
            item.state != "ABSENT"
            for item in (
                effect_binding.ref_file,
                effect_binding.ref_lock,
                effect_binding.reflog,
                effect_binding.reflog_lock,
            )
        ):
            return StartGuardDecision(ok=False, reason="PRIVATE_REF_CONFLICT")
        return StartGuardDecision(
            ok=True,
            binding=StartGuardBinding(
                run_id=run_id,
                repository_id=self._repository_id,
                target_reservation_id=self._reservation.reservation_id,
                pinned_target_oid=self._reservation.pinned_target_oid,
                target_safety_digest=self._target_safety_digest,
                ref_effect_binding=effect_binding,
                applicable_revision_digests=applicable_revision_digests,
            ),
        )

    def validate_consumed(
        self,
        *,
        binding: RuntimeStartBinding,
        permit: RuntimePermit,
        expected_sequence: AuditSequence,
    ) -> StartGuardDecision:
        if (
            binding.sequence != expected_sequence
            or binding.permit_generation != permit.generation
            or binding.consumed_owner_id != permit.consumed_owner_id
            or binding.consumed_sequence != permit.consumed_sequence
            or permit.applicable_revision_digests != binding.guard.applicable_revision_digests
            or permit.target_authority_digest != binding.guard.target_safety_digest
        ):
            return StartGuardDecision(ok=False, reason="START_GUARD_DENIED")
        current = self.inspect(
            run_id=binding.run_id,
            applicable_revision_digests=permit.applicable_revision_digests,
            expected_sequence=expected_sequence,
        )
        if not current.ok or current.binding != binding.guard:
            return StartGuardDecision(
                ok=False,
                reason=current.reason or "START_GUARD_BINDING_CHANGED",
            )
        return current

    def initialize_private_ref(self, intent: RefCasIntent) -> PrivateRefCasOutcome:
        try:
            current_binding = self._effect_binding(intent.run_id)
        except (RepositoryEffectError, RepositoryUnsafeError, ValueError):
            return PrivateRefCasOutcome(
                intent_id=intent.intent_id,
                run_id=intent.run_id,
                result_class="PRIVATE_REF_UNOBSERVABLE",
                observed_oid=None,
            )
        if (
            intent.run_id != self._reservation.run_id
            or intent.repository_id != self._repository_id
            or intent.prepared_oid != self._reservation.pinned_target_oid
            or intent.target_safety_digest != self._target_safety_digest
            or intent.target_reservation_id != self._reservation.reservation_id
            or intent.ref_effect_binding != current_binding
        ):
            return PrivateRefCasOutcome(
                intent_id=intent.intent_id,
                run_id=intent.run_id,
                result_class="PRIVATE_REF_CONFLICT",
                observed_oid=None,
            )
        result = self._runner.run(
            self._repository,
            GitCreatePrivateRef(
                intent.ref_name,
                intent.prepared_oid,
                intent.ref_effect_binding.reflog_message,
            ),
        )
        self._repository = self._repository.refresh_after_verified_owned_transition()
        observed = self._runner.run(self._repository, GitShowRefVerify(intent.ref_name))
        if observed.returncode == 0 and observed.stdout == f"{intent.prepared_oid}\n":
            result_class: Literal[
                "PRIVATE_REF_INITIALIZED",
                "PRIVATE_REF_ABSENT_FAILED",
                "PRIVATE_REF_CONFLICT",
                "PRIVATE_REF_UNOBSERVABLE",
            ] = "PRIVATE_REF_INITIALIZED"
            oid: GitOid | None = intent.prepared_oid
        elif observed.returncode == 1 and result.returncode != 0:
            result_class = "PRIVATE_REF_ABSENT_FAILED"
            oid = None
        elif observed.returncode == 0:
            result_class = "PRIVATE_REF_CONFLICT"
            oid = GitOid(observed.stdout.strip())
        else:
            result_class = "PRIVATE_REF_UNOBSERVABLE"
            oid = None
        return PrivateRefCasOutcome(
            intent_id=intent.intent_id,
            run_id=intent.run_id,
            result_class=result_class,
            observed_oid=oid,
        )

    def observe_resolution(
        self,
        *,
        ref_name: str,
        expected_old_oid: GitOid | None,
        prepared_oid: GitOid,
        expected_binding: RefEffectBinding,
    ) -> tuple[
        Literal["EXACT_POST", "EXACT_PRE", "THIRD_STATE", "UNAVAILABLE"],
        GitOid | None,
        Sha256DigestText | None,
    ]:
        try:
            current_binding = self._effect_binding(self._reservation.run_id)
            result = self._runner.run(self._repository, GitShowRefVerify(ref_name))
            registration_digest = current_binding.checkout_registration_digest
            if current_binding != expected_binding:
                current_oid = None
                if result.returncode == 0:
                    current_oid = GitOid(result.stdout.strip())
                return "THIRD_STATE", current_oid, registration_digest
            if result.returncode == 0:
                current_oid = GitOid(result.stdout.strip())
                if current_oid == prepared_oid:
                    return "EXACT_POST", current_oid, registration_digest
                return "THIRD_STATE", current_oid, registration_digest
            absent = result.returncode == 1 or (
                result.returncode == 128 and result.stderr.strip().endswith("not a valid ref")
            )
            if absent and expected_old_oid is None:
                return "EXACT_PRE", None, registration_digest
            return "THIRD_STATE", None, registration_digest
        except (OSError, RepositoryEffectError, RepositoryUnsafeError, ValueError):
            return "UNAVAILABLE", None, None


RepositoryUnsafeError = _RepositoryUnsafeError

MAX_GIT_CONFIG_BYTES = 1_048_576
MAX_GIT_CONFIG_LOGICAL_LINE_BYTES = 65_536
MAX_GIT_CONFIG_PHYSICAL_LINES = 16_384
MAX_GIT_CONFIG_ENTRIES = 4_096
MAX_GIT_INDEX_BYTES = 67_108_864
MAX_GIT_INDEX_ENTRIES = 1_000_000
MAX_GIT_ADMIN_ENTRIES = 4_096
MAX_WORKTREE_ADMIN_ENTRIES = 2
MAX_TARGET_RESERVATION_ADMIN_FILE_BYTES = 4_096
MAX_TARGET_RESERVATION_ADMIN_ENTRIES = 6
MAX_TARGET_RESERVATION_LOG_HEAD_BYTES = 65_536
MAX_WORKTREE_PORCELAIN_BYTES = 1_048_576
MAX_GIT_STDOUT_BYTES = 2_000_000
MAX_GIT_STDERR_BYTES = 65_536
_TARGET_RESERVATION_REQUIRED_ADMIN_FILE_NAMES = frozenset({"gitdir", "commondir", "HEAD"})
_TARGET_RESERVATION_REQUIRED_ADMIN_DIRECTORY_NAMES = frozenset({"logs", "refs"})
_TARGET_RESERVATION_OPTIONAL_ADMIN_FILE_NAMES = frozenset({"locked"})


@dataclass(frozen=True, slots=True)
class WorktreePorcelainRecord:
    path: str
    head_oid: str
    branch: str | None
    locked: bool
    detached: bool


def parse_worktree_porcelain_nul(output: bytes) -> tuple[WorktreePorcelainRecord, ...]:
    if not output or len(output) > MAX_WORKTREE_PORCELAIN_BYTES or not output.endswith(b"\0\0"):
        raise ValueError("WORKTREE_PORCELAIN_UNTERMINATED")
    records: list[WorktreePorcelainRecord] = []
    for raw_record in output[:-2].split(b"\0\0"):
        if not raw_record:
            raise ValueError("WORKTREE_PORCELAIN_INVALID")
        fields: dict[bytes, bytes] = {}
        for raw_field in raw_record.split(b"\0"):
            key, separator, value = raw_field.partition(b" ")
            if key in {b"locked", b"detached"} and not separator:
                value = b""
            elif key not in {b"worktree", b"HEAD", b"branch", b"locked"} or not separator:
                raise ValueError("WORKTREE_PORCELAIN_INVALID")
            if key in fields or len(value) > 32_768:
                raise ValueError("WORKTREE_PORCELAIN_INVALID")
            fields[key] = value
        required = {b"worktree", b"HEAD"}
        if not required.issubset(fields) or (b"branch" in fields) == (b"detached" in fields):
            raise ValueError("WORKTREE_PORCELAIN_INVALID")
        if not set(fields).issubset(required | {b"branch", b"locked", b"detached"}):
            raise ValueError("WORKTREE_PORCELAIN_INVALID")
        try:
            path = fields[b"worktree"].decode("utf-8", errors="strict")
            head_oid = fields[b"HEAD"].decode("ascii", errors="strict")
            branch = (
                None
                if b"branch" not in fields
                else fields[b"branch"].decode("utf-8", errors="strict")
            )
            if b"locked" in fields:
                fields[b"locked"].decode("utf-8", errors="strict")
        except UnicodeDecodeError as error:
            raise ValueError("WORKTREE_PORCELAIN_NON_UTF8") from error
        if (
            not path
            or any(character in path for character in "\r\n\x00")
            or len(head_oid) != 40
            or any(character not in "0123456789abcdef" for character in head_oid)
            or (
                branch is not None
                and (
                    not branch.startswith(("refs/heads/", "refs/apexcrew/runs/"))
                    or branch in {"refs/heads/", "refs/apexcrew/runs/"}
                )
            )
        ):
            raise ValueError("WORKTREE_PORCELAIN_INVALID")
        records.append(
            WorktreePorcelainRecord(
                path,
                head_oid,
                branch,
                b"locked" in fields,
                b"detached" in fields,
            )
        )
    return tuple(records)


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


def reject_unsafe_layout(
    layout: GitLayout, *, allowed_worktree_admin_entries: tuple[str, ...] = ()
) -> None:
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
    unexpected_worktrees = set(layout.worktree_admin_entries) - set(allowed_worktree_admin_entries)
    if unexpected_worktrees:
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
    def capture(
        cls,
        handles: StableHandleTree,
        *,
        excluded_prefixes: tuple[str, ...] = (),
    ) -> GitStorageSnapshot:
        captured: list[BoundGitStorageNode] = []

        def visit(relative: str, depth: int) -> None:
            if depth > MAX_GIT_STORAGE_DEPTH or len(captured) >= MAX_GIT_STORAGE_PATHS:
                raise RepositoryUnsafeError("GIT_STORAGE_INVENTORY_LIMIT_EXCEEDED")
            if any(
                relative == prefix or relative.startswith(prefix + "/")
                for prefix in excluded_prefixes
            ):
                return
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

    def assert_current(
        self,
        handles: StableHandleTree,
        *,
        excluded_prefixes: tuple[str, ...] = (),
    ) -> None:
        handles.assert_name_bindings()
        expected = tuple(
            node
            for node in self.nodes
            if not any(
                node.relative == prefix or node.relative.startswith(prefix + "/")
                for prefix in excluded_prefixes
            )
        )
        if (
            GitStorageSnapshot.capture(handles, excluded_prefixes=excluded_prefixes).nodes
            != expected
        ):
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
        if isinstance(operation, (GitWorktreeUnlock, GitWorktreeRemoveForce)):
            self.storage_snapshot.assert_current(
                self.handles,
                excluded_prefixes=(f".git/worktrees/{operation.admin_entry_name}",),
            )
            return
        self.assert_stable()

    def refresh_after_verified_owned_transition(self) -> RepositoryInstance:
        self.handles.assert_name_bindings()
        return replace(self, storage_snapshot=GitStorageSnapshot.capture(self.handles))

    def close(self) -> None:
        self.handles.close()


class GitRepositoryPreflight:
    def inspect(
        self, root: Path, *, allowed_worktree_admin_entries: tuple[str, ...] = ()
    ) -> RepositoryInstance:
        layout = inspect_no_follow_git_layout(root)
        try:
            reject_unsafe_layout(
                layout, allowed_worktree_admin_entries=allowed_worktree_admin_entries
            )
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
class GitShowRefVerify:
    direct_ref: str


@dataclass(frozen=True, slots=True)
class GitCreatePrivateRef:
    direct_ref: str
    prepared_oid: GitOid
    reflog_message: str


@dataclass(frozen=True, slots=True)
class GitUpdateRefCas:
    direct_ref: str
    prepared_oid: GitOid
    expected_old_oid: GitOid
    reflog_message: str


@dataclass(frozen=True, slots=True)
class GitWorktreeAddNoCheckout:
    reservation_path: Path
    target_ref: str


@dataclass(frozen=True, slots=True)
class GitWorktreeLock:
    reservation_path: Path
    reason: str


@dataclass(frozen=True)
class GitWorktreeUnlock:
    reservation_path: Path
    admin_entry_name: str


@dataclass(frozen=True)
class GitWorktreeRemoveForce:
    reservation_path: Path
    admin_entry_name: str


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
    | GitShowRefVerify
    | GitCreatePrivateRef
    | GitUpdateRefCas
    | GitWorktreeAddNoCheckout
    | GitWorktreeLock
    | GitWorktreeUnlock
    | GitWorktreeRemoveForce
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
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            env=dict(environment),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert process.stdout is not None and process.stderr is not None
        overflow = threading.Event()
        stdout = bytearray()
        stderr = bytearray()

        def kill_if_running() -> None:
            try:
                process.kill()
            except OSError:
                pass

        def drain(stream: IO[bytes], destination: bytearray, maximum: int) -> None:
            while True:
                chunk = stream.read(65_536)
                if not chunk:
                    return
                remaining = maximum - len(destination)
                destination.extend(chunk[: max(remaining, 0)])
                if len(chunk) > remaining:
                    overflow.set()
                    kill_if_running()
                    return

        threads = (
            threading.Thread(
                target=drain,
                args=(process.stdout, stdout, MAX_GIT_STDOUT_BYTES),
                daemon=True,
            ),
            threading.Thread(
                target=drain,
                args=(process.stderr, stderr, MAX_GIT_STDERR_BYTES),
                daemon=True,
            ),
        )
        for thread in threads:
            thread.start()
        try:
            returncode = process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            kill_if_running()
            process.wait()
            raise
        finally:
            for thread in threads:
                thread.join()
        if overflow.is_set():
            raise RepositoryUnsafeError("GIT_OUTPUT_LIMIT_EXCEEDED")
        raw_stdout = bytes(stdout)
        raw_stderr = bytes(stderr)
        if text:
            return subprocess.CompletedProcess(
                argv,
                returncode,
                raw_stdout.decode("utf-8", errors="strict"),
                raw_stderr.decode("utf-8", errors="replace"),
            )
        return subprocess.CompletedProcess(argv, returncode, raw_stdout, raw_stderr)


class GitCommandRunner:
    def __init__(
        self,
        git_executable: Path,
        trusted_empty_dir: Path | None = None,
        spawner: GitSpawner | None = None,
    ) -> None:
        if not git_executable.is_absolute():
            raise ValueError("ABSOLUTE_GIT_EXECUTABLE_REQUIRED")
        self._git = git_executable
        self._owned_trusted_empty_dir = (
            tempfile.TemporaryDirectory(prefix="apexcrew-git-")
            if trusted_empty_dir is None
            else None
        )
        trusted_config_dir = (
            Path(self._owned_trusted_empty_dir.name)
            if self._owned_trusted_empty_dir is not None
            else trusted_empty_dir
        )
        assert trusted_config_dir is not None
        self._trusted_empty_dir = trusted_config_dir
        self._spawner = SubprocessGitSpawner() if spawner is None else spawner

    def close(self) -> None:
        if self._owned_trusted_empty_dir is not None:
            self._owned_trusted_empty_dir.cleanup()
            self._owned_trusted_empty_dir = None

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
                GitShowRefVerify,
                GitCreatePrivateRef,
                GitUpdateRefCas,
                GitWorktreeAddNoCheckout,
                GitWorktreeLock,
                GitWorktreeUnlock,
                GitWorktreeRemoveForce,
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
            case GitShowRefVerify(direct_ref=target):
                self._require_readable_ref(target)
                return ("show-ref", "--verify", "--hash", "--", target)
            case GitCreatePrivateRef(direct_ref=target, prepared_oid=oid, reflog_message=message):
                self._require_private_ref(target)
                self._require_reason(message)
                return (
                    "update-ref",
                    "--create-reflog",
                    "-m",
                    message,
                    target,
                    self._require_oid(oid),
                    "",
                )
            case GitUpdateRefCas(
                direct_ref=target,
                prepared_oid=new_oid,
                expected_old_oid=old_oid,
                reflog_message=message,
            ):
                self._require_direct_ref(target)
                self._require_reason(message)
                return (
                    "update-ref",
                    "-m",
                    message,
                    target,
                    self._require_oid(new_oid),
                    self._require_oid(old_oid),
                )
            case GitWorktreeAddNoCheckout(reservation_path=path, target_ref=target):
                self._require_direct_ref(target)
                self._require_operand_path(path)
                short_branch = target.removeprefix("refs/heads/")
                return ("worktree", "add", "--no-checkout", "--", str(path), short_branch)
            case GitWorktreeLock(reservation_path=path, reason=reason):
                self._require_operand_path(path)
                self._require_reason(reason)
                return ("worktree", "lock", "--reason", reason, "--", str(path))
            case GitWorktreeUnlock(reservation_path=path):
                self._require_operand_path(path)
                return ("worktree", "unlock", "--", str(path))
            case GitWorktreeRemoveForce(reservation_path=path):
                self._require_operand_path(path)
                return ("worktree", "remove", "--force", "--", str(path))
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
    def _require_private_ref(value: str) -> None:
        prefix = "refs/apexcrew/runs/"
        component = value.removeprefix(prefix)
        if (
            not value.startswith(prefix)
            or not component
            or len(component) > 128
            or any(
                character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-"
                for character in component
            )
        ):
            raise RepositoryUnsafeError("GIT_PRIVATE_REF_OPERAND_INVALID")

    @classmethod
    def _require_readable_ref(cls, value: str) -> None:
        if value.startswith("refs/heads/"):
            cls._require_direct_ref(value)
        else:
            cls._require_private_ref(value)

    @staticmethod
    def _require_operand_path(value: Path) -> None:
        text = str(value)
        if not value.is_absolute() or text.startswith("-") or "\x00" in text:
            raise RepositoryUnsafeError("GIT_PATH_OPERAND_INVALID")

    @staticmethod
    def _require_reason(value: str) -> None:
        if not value or "\x00" in value or "\n" in value or "\r" in value:
            raise RepositoryUnsafeError("GIT_LOCK_REASON_INVALID")


@dataclass(frozen=True, slots=True)
class TargetReservationAdminRecord:
    entry_name: str
    gitdir: bytes
    commondir: bytes
    head: bytes
    logs_head: bytes
    locked: bytes | None
    binding_digest: Sha256DigestText


def _target_reservation_component(value: str) -> str:
    if (
        not value
        or len(value) > 128
        or value in {".", ".."}
        or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789-" for character in value)
    ):
        raise RepositoryEffectError("TARGET_RESERVATION_ADMIN_ENTRY_INVALID")
    return value


def _exact_admin_line(raw: bytes, *, label: str) -> bytes:
    if (
        not raw
        or len(raw) > MAX_TARGET_RESERVATION_ADMIN_FILE_BYTES
        or not raw.endswith(b"\n")
        or raw.count(b"\n") != 1
        or b"\x00" in raw
        or b"\r" in raw
    ):
        raise RepositoryEffectError("TARGET_RESERVATION_" + label + "_INVALID")
    return raw


def reservation_for_operation(
    operation: TargetReservationOperation,
) -> TargetReservation:
    return TargetReservation(
        reservation_id=operation.reservation_id,
        run_id=operation.run_id,
        target_ref=operation.target_ref,
        pinned_target_oid=operation.pinned_target_oid,
        path=Path(operation.reservation_path),
        phase="CREATION_INTENT_RECORDED",
    )


class NoFollowTargetReservationWorktreeGuard(TargetReservationWorktreeGuard):
    """Validates only preallocated admin/data-root paths before a Git subprocess."""

    def __init__(
        self,
        repository: RepositoryInstance,
        data_root: Path,
        data_handles: StableHandleTree,
        expected_main_worktree: Path,
    ) -> None:
        self._repository = repository
        self._data_root = data_root
        self._data_handles = data_handles
        self._expected_main_worktree = expected_main_worktree

    def require_safe_before_list(self, reservation: TargetReservation) -> None:
        self._repository.assert_stable()
        self._data_handles.assert_name_bindings()
        if self._repository.root != self._expected_main_worktree:
            raise RepositoryEffectError("TARGET_RESERVATION_MAIN_WORKTREE_CHANGED")
        self._require_reservation_path(reservation)
        self._worktree_admin_names()

    def require_compatible_observation(
        self, reservation: TargetReservation
    ) -> ReservationAdminObservation:
        self.require_safe_before_list(reservation)
        names = self._worktree_admin_names()
        if not names:
            if self._reservation_path_state(reservation)[0]:
                raise RepositoryEffectError("TARGET_RESERVATION_DATA_INVENTORY_INVALID")
            return ReservationAdminObservation(admin_entry_name=None, admin_binding_digest=None)
        expected = _target_reservation_component(reservation.reservation_id)
        if names != (expected,):
            raise RepositoryEffectError("TARGET_RESERVATION_ADMIN_INVENTORY_INVALID")
        record = self._read_exact_admin_record(reservation)
        path_present, gitfile_exact = self._reservation_path_state(reservation)
        if not path_present or not gitfile_exact:
            raise RepositoryEffectError("TARGET_RESERVATION_DATA_INVENTORY_INVALID")
        return ReservationAdminObservation(
            admin_entry_name=record.entry_name,
            admin_binding_digest=record.binding_digest,
        )

    def require_absent_before_add(self, operation: TargetReservationOperation) -> None:
        reservation = reservation_for_operation(operation)
        self.require_safe_before_list(reservation)
        if self._worktree_admin_names() or self._reservation_path_state(reservation)[0]:
            raise RepositoryEffectError("TARGET_RESERVATION_ADD_PRESTATE_NOT_ABSENT")

    def require_exact_registered_unlocked(self, operation: TargetReservationOperation) -> None:
        reservation = reservation_for_operation(operation)
        self.require_safe_before_list(reservation)
        record = self._read_exact_admin_record(reservation)
        path_present, gitfile_exact = self._reservation_path_state(reservation)
        if (
            not path_present
            or not gitfile_exact
            or record.locked is not None
            or record.entry_name != _target_reservation_component(operation.reservation_id)
        ):
            raise RepositoryEffectError("TARGET_RESERVATION_LOCK_PRESTATE_NOT_EXACT")

    def require_exact_post_operation(self, operation: TargetReservationOperation) -> None:
        reservation = reservation_for_operation(operation)
        self._repository.handles.assert_name_bindings()
        self._data_handles.assert_name_bindings()
        if self._repository.root != self._expected_main_worktree:
            raise RepositoryEffectError("TARGET_RESERVATION_MAIN_WORKTREE_CHANGED")
        self._require_reservation_path(reservation)
        record = self._read_exact_admin_record(reservation)
        path_present, gitfile_exact = self._reservation_path_state(reservation)
        expected_locked = (
            None
            if operation.kind == "ADD_NO_CHECKOUT"
            else operation.lock_reason.encode("utf-8") + b"\n"
        )
        if (
            not path_present
            or not gitfile_exact
            or record.entry_name != _target_reservation_component(operation.reservation_id)
            or record.locked != expected_locked
        ):
            raise RepositoryEffectError("TARGET_RESERVATION_POSTSTATE_NOT_EXACT")
        self._repository = self._repository.refresh_after_verified_owned_transition()

    def release_cached_admin_entry(self, reservation: TargetReservation) -> None:
        self.require_safe_before_list(reservation)
        self._repository.handles.release_cached(
            ".git/worktrees/" + _target_reservation_component(reservation.reservation_id)
        )

    def release_cached_reservation(self, reservation: TargetReservation) -> None:
        self.release_cached_admin_entry(reservation)
        self._data_handles.release_cached(
            "reservations/" + _target_reservation_component(reservation.reservation_id)
        )

    def refresh_after_git_transition(self) -> None:
        self._repository = self._repository.refresh_after_verified_owned_transition()

    def _worktree_admin_names(self) -> tuple[str, ...]:
        directory = self._repository.handles.try_open(".git/worktrees", "directory")
        if directory is None:
            return ()
        names = self._repository.handles.list_names(directory, MAX_TARGET_RESERVATION_ADMIN_ENTRIES)
        for name in names:
            _target_reservation_component(name)
        return names

    def _require_reservation_path(self, reservation: TargetReservation) -> None:
        expected = (
            self._data_root
            / "reservations"
            / _target_reservation_component(reservation.reservation_id)
        )
        if not reservation.path.is_absolute() or reservation.path != expected:
            raise RepositoryEffectError("TARGET_RESERVATION_PATH_BINDING_INVALID")

    def _read_exact_admin_record(
        self, reservation: TargetReservation
    ) -> TargetReservationAdminRecord:
        name = _target_reservation_component(reservation.reservation_id)
        if self._worktree_admin_names() != (name,):
            raise RepositoryEffectError("TARGET_RESERVATION_ADMIN_INVENTORY_INVALID")
        prefix = ".git/worktrees/" + name
        entry = self._repository.handles.open(prefix, "directory")
        names = self._repository.handles.list_names(entry, MAX_TARGET_RESERVATION_ADMIN_ENTRIES)
        observed = set(names)
        required = (
            _TARGET_RESERVATION_REQUIRED_ADMIN_FILE_NAMES
            | _TARGET_RESERVATION_REQUIRED_ADMIN_DIRECTORY_NAMES
        )
        if (
            len(names) != len(observed)
            or len({item.casefold() for item in names}) != len(names)
            or observed not in (required, required | _TARGET_RESERVATION_OPTIONAL_ADMIN_FILE_NAMES)
        ):
            raise RepositoryEffectError("TARGET_RESERVATION_ADMIN_FILES_INVALID")
        gitdir = _exact_admin_line(
            self._repository.handles.read_bytes(
                self._repository.handles.open(prefix + "/gitdir", "file"),
                MAX_TARGET_RESERVATION_ADMIN_FILE_BYTES,
            ),
            label="GITDIR",
        )
        commondir = _exact_admin_line(
            self._repository.handles.read_bytes(
                self._repository.handles.open(prefix + "/commondir", "file"),
                MAX_TARGET_RESERVATION_ADMIN_FILE_BYTES,
            ),
            label="COMMONDIR",
        )
        head = _exact_admin_line(
            self._repository.handles.read_bytes(
                self._repository.handles.open(prefix + "/HEAD", "file"),
                MAX_TARGET_RESERVATION_ADMIN_FILE_BYTES,
            ),
            label="HEAD",
        )
        logs = self._repository.handles.open(prefix + "/logs", "directory")
        if self._repository.handles.list_names(logs, MAX_TARGET_RESERVATION_ADMIN_ENTRIES) != (
            "HEAD",
        ):
            raise RepositoryEffectError("TARGET_RESERVATION_ADMIN_FILES_INVALID")
        logs_head = self._repository.handles.read_bytes(
            self._repository.handles.open(prefix + "/logs/HEAD", "file"),
            MAX_TARGET_RESERVATION_LOG_HEAD_BYTES,
        )
        refs = self._repository.handles.open(prefix + "/refs", "directory")
        if self._repository.handles.list_names(refs, MAX_TARGET_RESERVATION_ADMIN_ENTRIES):
            raise RepositoryEffectError("TARGET_RESERVATION_ADMIN_FILES_INVALID")
        locked_node = self._repository.handles.try_open(prefix + "/locked", "file")
        locked = (
            None
            if locked_node is None
            else _exact_admin_line(
                self._repository.handles.read_bytes(
                    locked_node, MAX_TARGET_RESERVATION_ADMIN_FILE_BYTES
                ),
                label="LOCK",
            )
        )
        if gitdir != os.fsencode((reservation.path / ".git").as_posix()) + b"\n":
            raise RepositoryEffectError("TARGET_RESERVATION_GITDIR_MISMATCH")
        if commondir != b"../..\n":
            raise RepositoryEffectError("TARGET_RESERVATION_COMMONDIR_MISMATCH")
        if (
            self._repository.handles.open(".git", "directory").identity
            != self._repository.git_dir_identity
        ):
            raise RepositoryEffectError("TARGET_RESERVATION_COMMONDIR_IDENTITY_CHANGED")
        expected_head = b"ref: " + os.fsencode(reservation.target_ref) + b"\n"
        if head != expected_head:
            raise RepositoryEffectError("TARGET_RESERVATION_HEAD_MISMATCH")
        digest = sha256_digest(
            canonical_json(
                {
                    "entry_name": name,
                    "gitdir_hex": gitdir.hex(),
                    "commondir_hex": commondir.hex(),
                    "head_hex": head.hex(),
                    "logs_head_hex": logs_head.hex(),
                    "locked_hex": None if locked is None else locked.hex(),
                }
            )
        )
        return TargetReservationAdminRecord(
            name, gitdir, commondir, head, logs_head, locked, digest
        )

    def _reservation_path_state(self, reservation: TargetReservation) -> tuple[bool, bool]:
        relative = "reservations/" + _target_reservation_component(reservation.reservation_id)
        directory = self._data_handles.try_open(relative, "directory")
        if directory is None:
            return False, False
        if self._data_handles.list_names(directory, 2) != (".git",):
            return True, False
        gitfile = self._data_handles.try_open(relative + "/.git", "file")
        if gitfile is None:
            return True, False
        expected = (
            b"gitdir: "
            + os.fsencode(
                (
                    self._repository.root / ".git" / "worktrees" / reservation.reservation_id
                ).as_posix()
            )
            + b"\n"
        )
        actual = self._data_handles.read_bytes(gitfile, MAX_TARGET_RESERVATION_ADMIN_FILE_BYTES)
        return True, actual == expected


class GitTargetReservationRepository(TargetReservationGitPort):
    def __init__(
        self,
        repository: RepositoryInstance,
        repository_id: RepositoryId,
        repository_instance_digest: Sha256DigestText,
        runner: GitCommandRunner,
        worktree_guard: TargetReservationWorktreeGuard,
        data_root: Path,
        target_authority_digest: Sha256DigestText,
    ) -> None:
        self._repository = repository
        self._repository_id = repository_id
        self._repository_instance_digest = repository_instance_digest
        self._runner = runner
        self._worktree_guard = worktree_guard
        self._data_root = data_root
        self._target_authority_digest = target_authority_digest

    def _require_safe_reservation_state(
        self, operation: TargetReservationOperation, *, post_operation: bool
    ) -> None:
        try:
            if post_operation:
                self._worktree_guard.require_exact_post_operation(operation)
            elif operation.kind == "ADD_NO_CHECKOUT":
                self._worktree_guard.require_absent_before_add(operation)
            else:
                self._worktree_guard.require_exact_registered_unlocked(operation)
        except (RepositoryEffectError, RepositoryUnsafeError) as error:
            raise RepositoryEffectError("TARGET_UNSAFE") from error

    def _run_with_inventory_snapshots(
        self,
        operation: TargetReservationOperation,
        git_operation: GitOperation,
        *,
        pre_state_is_post_operation: bool,
        post_state_on_success: bool,
    ) -> subprocess.CompletedProcess[str]:
        """Run one Git command between the required no-follow inventory snapshots."""
        self._require_safe_reservation_state(operation, post_operation=pre_state_is_post_operation)
        try:
            result = self._runner.run(self._repository, git_operation)
        except (RepositoryEffectError, RepositoryUnsafeError) as error:
            raise RepositoryEffectError("TARGET_UNSAFE") from error
        self._require_safe_reservation_state(
            operation,
            post_operation=post_state_on_success if result.returncode == 0 else False,
        )
        return result

    @staticmethod
    def _operation_command(
        operation: TargetReservationOperation, expected: Path
    ) -> GitWorktreeAddNoCheckout | GitWorktreeLock:
        if operation.kind == "ADD_NO_CHECKOUT":
            return GitWorktreeAddNoCheckout(expected, operation.target_ref)
        return GitWorktreeLock(expected, operation.lock_reason)

    def apply(self, operation: TargetReservationOperation) -> TargetReservationOperationResult:
        expected = self._data_root / "reservations" / operation.reservation_id
        if (
            Path(operation.reservation_path) != expected
            or operation.repository_id != self._repository_id
            or operation.repository_instance_digest != self._repository_instance_digest
            or operation.target_authority_digest != self._target_authority_digest
            or not operation.target_ref.startswith("refs/heads/")
        ):
            raise RepositoryEffectError("TARGET_RESERVATION_OPERATION_BINDING_INVALID")
        git_operation = self._operation_command(operation, expected)
        self._require_pinned_target(operation, post_operation=False)
        result = self._run_with_inventory_snapshots(
            operation,
            git_operation,
            pre_state_is_post_operation=False,
            post_state_on_success=True,
        )
        if result.returncode != 0:
            raise RepositoryEffectError("TARGET_RESERVATION_" + operation.kind + "_FAILED")
        try:
            self._repository = self._repository.refresh_after_verified_owned_transition()
        except RepositoryUnsafeError as error:
            raise RepositoryEffectError("TARGET_UNSAFE") from error
        self._require_pinned_target(operation, post_operation=True)
        return TargetReservationOperationResult(intent_id=operation.intent_id, kind=operation.kind)

    def observe_registration(
        self, reservation: TargetReservation
    ) -> ReservationRegistrationObservation:
        try:
            self._worktree_guard.require_safe_before_list(reservation)
            admin_before = self._worktree_guard.require_compatible_observation(reservation)
            result = self._runner.run_bytes(self._repository, GitWorktreeListPorcelain())
            admin_after = self._worktree_guard.require_compatible_observation(reservation)
            if admin_after != admin_before:
                raise RepositoryEffectError("TARGET_RESERVATION_OBSERVATION_CHANGED")
        except (RepositoryEffectError, RepositoryUnsafeError):
            return ReservationRegistrationObservation(False, False, False, True, False)
        if result.returncode != 0:
            return ReservationRegistrationObservation(False, False, False, True, False)
        try:
            records = parse_worktree_porcelain_nul(result.stdout)
        except ValueError:
            return ReservationRegistrationObservation(False, False, False, True, False)

        by_path = tuple(record for record in records if Path(record.path) == reservation.path)
        by_target = tuple(record for record in records if record.branch == reservation.target_ref)
        record = by_path[0] if len(by_path) == 1 else None
        others = tuple(item for item in records if item is not record)
        main = others[0] if len(others) == 1 else None
        safe_main = (
            main is not None
            and Path(main.path) == self._repository.root
            and main.detached
            and not main.locked
            and main.head_oid == reservation.pinned_target_oid
        )
        unexpected = (
            len(by_path) > 1
            or len(by_target) > 1
            or (record is None and bool(by_target))
            or (
                record is not None
                and (
                    len(records) != 2
                    or len(by_target) != 1
                    or by_target[0] != record
                    or not safe_main
                )
            )
            or (record is None and (len(records) != 1 or not safe_main))
        )
        exact = (
            not unexpected
            and record is not None
            and record.head_oid == reservation.pinned_target_oid
        )
        return ReservationRegistrationObservation(
            registration_present=record is not None,
            locked=False if record is None else record.locked,
            exact_identity=exact,
            unexpected_registration=unexpected,
            observable=True,
            admin_entry_name=admin_after.admin_entry_name,
            admin_binding_digest=admin_after.admin_binding_digest,
        )

    def unlock(self, reservation: TargetReservation) -> None:
        expected = self._data_root / "reservations" / reservation.reservation_id
        if reservation.path != expected:
            raise RepositoryEffectError("TARGET_RESERVATION_CLEANUP_PATH_INVALID")
        before = self.observe_registration(reservation)
        if (
            not before.observable
            or not before.registration_present
            or not before.exact_identity
            or not before.locked
        ):
            raise RepositoryEffectError("TARGET_RESERVATION_UNLOCK_PRESTATE_NOT_EXACT")
        self._worktree_guard.release_cached_admin_entry(reservation)
        result = self._runner.run(
            self._repository,
            GitWorktreeUnlock(expected, _target_reservation_component(reservation.reservation_id)),
        )
        self._repository = self._repository.refresh_after_verified_owned_transition()
        self._worktree_guard.refresh_after_git_transition()
        if result.returncode != 0:
            raise RepositoryEffectError("TARGET_RESERVATION_UNLOCK_FAILED")
        after = self.observe_registration(reservation)
        if (
            not after.observable
            or not after.registration_present
            or not after.exact_identity
            or after.locked
        ):
            raise RepositoryEffectError("TARGET_RESERVATION_UNLOCK_POSTSTATE_NOT_EXACT")

    def remove_force(self, reservation: TargetReservation) -> None:
        expected = self._data_root / "reservations" / reservation.reservation_id
        if reservation.path != expected:
            raise RepositoryEffectError("TARGET_RESERVATION_CLEANUP_PATH_INVALID")
        before = self.observe_registration(reservation)
        if (
            not before.observable
            or not before.registration_present
            or not before.exact_identity
            or before.locked
        ):
            raise RepositoryEffectError("TARGET_RESERVATION_REMOVE_PRESTATE_NOT_EXACT")
        self._worktree_guard.release_cached_reservation(reservation)
        result = self._runner.run(
            self._repository,
            GitWorktreeRemoveForce(
                expected, _target_reservation_component(reservation.reservation_id)
            ),
        )
        self._repository = self._repository.refresh_after_verified_owned_transition()
        self._worktree_guard.refresh_after_git_transition()
        if result.returncode != 0:
            raise RepositoryEffectError("TARGET_RESERVATION_REMOVE_FAILED")
        after = self.observe_registration(reservation)
        if not after.observable or after.registration_present or after.unexpected_registration:
            raise RepositoryEffectError("TARGET_RESERVATION_REMOVE_POSTSTATE_NOT_EXACT")

    def _require_pinned_target(
        self, operation: TargetReservationOperation, *, post_operation: bool
    ) -> None:
        result = self._run_with_inventory_snapshots(
            operation,
            GitShowRefVerify(operation.target_ref),
            pre_state_is_post_operation=post_operation,
            post_state_on_success=post_operation,
        )
        if result.returncode != 0 or result.stdout != f"{operation.pinned_target_oid}\n":
            raise RepositoryEffectError("TARGET_RESERVATION_PINNED_TARGET_MISMATCH")
