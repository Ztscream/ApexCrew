from __future__ import annotations

import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from typing import Protocol

from apexcrew.adapters.repository.git import (
    GitCatFileBlob,
    GitCatFileSize,
    GitLsTreePath,
    GitLsTreeRecursive,
    GitOperation,
    RepositoryInstance,
    RepositoryUnsafeError,
)
from apexcrew.domain.coordination import (
    PlanningContextOverflow,
    PlanningManifest,
    PlanningReadDenied,
    PlanningSnapshotReader,
    PlanningTurnBinding,
)
from apexcrew.domain.plan import CanonicalPath, PathValidationError
from apexcrew.domain.revisions import PlanningReadAuthorizationDocument, Sha256DigestText
from apexcrew.domain.types import GitOid, RepositoryId

__all__ = ["GitPlanningSnapshotReader", "PlanningReadDenied"]


class PlanningGitRunner(Protocol):
    def run_bytes(
        self, repository: RepositoryInstance, operation: GitOperation
    ) -> subprocess.CompletedProcess[bytes]: ...


class PlanningPathGate(Protocol):
    def require_allowed(
        self, path: CanonicalPath, authorization: PlanningReadAuthorizationDocument
    ) -> None: ...

    def require_manifest_allowed(
        self, path: CanonicalPath, binding: PlanningTurnBinding
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class LsTreeRecord:
    mode: bytes
    object_type: bytes
    oid: bytes
    path: str


def _parse_ls_tree_records(raw: bytes, *, maximum: int = 131_072) -> tuple[LsTreeRecord, ...]:
    if raw == b"":
        return ()
    if len(raw) > maximum or not raw.endswith(b"\0"):
        raise PlanningReadDenied("PLANNING_TREE_OUTPUT_INVALID")
    records: list[LsTreeRecord] = []
    for record in raw[:-1].split(b"\0"):
        metadata, separator, encoded_path = record.partition(b"\t")
        fields = metadata.split(b" ")
        if not separator or len(fields) != 3:
            raise PlanningReadDenied("PLANNING_TREE_OUTPUT_INVALID")
        try:
            path = encoded_path.decode("utf-8", errors="strict")
        except UnicodeDecodeError as error:
            raise PlanningReadDenied("PLANNING_TREE_OUTPUT_INVALID") from error
        records.append(LsTreeRecord(fields[0], fields[1], fields[2], path))
    return tuple(records)


class GitPlanningSnapshotReader(PlanningSnapshotReader):
    def __init__(
        self,
        repository: RepositoryInstance,
        repository_id: RepositoryId,
        runner: PlanningGitRunner,
        paths: PlanningPathGate,
    ) -> None:
        self._repository = repository
        self._repository_id = repository_id
        self._runner = runner
        self._paths = paths

    def manifest(self, binding: PlanningTurnBinding) -> PlanningManifest:
        if binding.repository_id != self._repository_id:
            raise PlanningReadDenied("PLANNING_MANIFEST_REPOSITORY_MISMATCH")
        result = self._run(GitLsTreeRecursive(binding.pinned_base_oid))
        if result.returncode != 0 or len(result.stdout) > 2_000_000:
            raise PlanningContextOverflow()
        entries: list[tuple[CanonicalPath, Sha256DigestText, int]] = []
        total = 0
        for record in _parse_ls_tree_records(result.stdout, maximum=2_000_000):
            if record.mode not in {b"100644", b"100755"} or record.object_type != b"blob":
                continue
            try:
                path = CanonicalPath.parse(record.path)
            except PathValidationError as error:
                raise PlanningReadDenied("PLANNING_MANIFEST_PATH_INVALID") from error
            try:
                self._paths.require_manifest_allowed(path, binding)
            except (PlanningReadDenied, ValueError):
                continue
            oid = GitOid(record.oid.decode("ascii", errors="strict"))
            size = self._blob_size(oid)
            entries.append(
                (path, Sha256DigestText("sha256:" + sha256(record.oid).hexdigest()), size)
            )
            total += size
            if len(entries) > 2_000 or total > 131_072:
                raise PlanningContextOverflow()
        if tuple(item[0] for item in entries) != tuple(sorted(item[0] for item in entries)):
            raise PlanningReadDenied("PLANNING_MANIFEST_ORDER_INVALID")
        return PlanningManifest(entries=tuple(entries), total_bytes=total)

    def read_tracked_file(
        self,
        base_oid: GitOid,
        path: CanonicalPath,
        authorization: PlanningReadAuthorizationDocument,
    ) -> tuple[str, Sha256DigestText, bool]:
        self._require_allowed(path, authorization)
        raw = self._read_blob(
            self._single_regular_blob(base_oid, path), authorization.max_file_bytes
        )
        try:
            content = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError as error:
            raise PlanningReadDenied("PLANNING_READ_NOT_UTF8") from error
        return content, Sha256DigestText("sha256:" + sha256(raw).hexdigest()), False

    def search_tracked_content(
        self,
        base_oid: GitOid,
        query: str,
        paths: tuple[CanonicalPath, ...],
        authorization: PlanningReadAuthorizationDocument,
    ) -> tuple[Mapping[str, object], ...]:
        if not query or not paths:
            raise PlanningReadDenied("PLANNING_SEARCH_INPUT_INVALID")
        needle = query.encode("utf-8")
        matches: list[Mapping[str, object]] = []
        returned = 0
        for path in sorted(paths, key=str):
            self._require_allowed(path, authorization)
            raw = self._read_blob(
                self._single_regular_blob(base_oid, path), authorization.max_file_bytes
            )
            start = 0
            while (offset := raw.find(needle, start)) != -1:
                encoded_path = str(path).encode("utf-8")
                if (
                    len(matches) == authorization.max_search_matches
                    or returned + len(encoded_path) > authorization.max_search_bytes
                ):
                    return tuple(matches)
                matches.append(
                    {
                        "byte_offset": offset,
                        "content_digest": "sha256:" + sha256(raw).hexdigest(),
                        "path": str(path),
                    }
                )
                returned += len(encoded_path)
                start = offset + len(needle)
        return tuple(matches)

    def _require_allowed(
        self,
        path: CanonicalPath,
        authorization: PlanningReadAuthorizationDocument,
    ) -> None:
        try:
            self._paths.require_allowed(path, authorization)
        except ValueError as error:
            raise PlanningReadDenied("PLANNING_PATH_DENIED") from error

    def _single_regular_blob(self, base_oid: GitOid, path: CanonicalPath) -> GitOid:
        result = self._run(GitLsTreePath(base_oid, path))
        records = _parse_ls_tree_records(result.stdout)
        if result.returncode != 0 or len(records) != 1 or records[0].path != str(path):
            raise PlanningReadDenied("PLANNING_TRACKED_REGULAR_FILE_REQUIRED")
        record = records[0]
        if record.mode not in {b"100644", b"100755"} or record.object_type != b"blob":
            raise PlanningReadDenied("PLANNING_TRACKED_REGULAR_FILE_REQUIRED")
        return GitOid(record.oid.decode("ascii", errors="strict"))

    def _blob_size(self, oid: GitOid) -> int:
        result = self._run(GitCatFileSize(oid))
        if result.returncode != 0 or not result.stdout.endswith(b"\n"):
            raise PlanningReadDenied("PLANNING_BLOB_SIZE_UNAVAILABLE")
        try:
            return int(result.stdout[:-1].decode("ascii", errors="strict"))
        except ValueError as error:
            raise PlanningReadDenied("PLANNING_BLOB_SIZE_INVALID") from error

    def _read_blob(self, oid: GitOid, maximum: int) -> bytes:
        size = self._blob_size(oid)
        if size < 0 or size > maximum:
            raise PlanningReadDenied("PLANNING_BLOB_TOO_LARGE")
        result = self._run(GitCatFileBlob(oid))
        if result.returncode != 0 or len(result.stdout) != size:
            raise PlanningReadDenied("PLANNING_BLOB_READ_FAILED")
        return result.stdout

    def _run(self, operation: GitOperation) -> subprocess.CompletedProcess[bytes]:
        try:
            return self._runner.run_bytes(self._repository, operation)
        except RepositoryUnsafeError as error:
            raise PlanningReadDenied("PLANNING_REPOSITORY_READ_DENIED") from error
