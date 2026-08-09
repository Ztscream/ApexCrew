from __future__ import annotations

import os
import re
import stat
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from apexcrew.adapters.repository.git import (
    GitCommitTree,
    GitHashObjectWrite,
    GitReadTree,
    GitRemoveIndexPath,
    GitUpdateIndexCacheInfo,
    GitWriteTree,
    RepositoryInstance,
)
from apexcrew.adapters.repository.no_follow import RepositoryUnsafeError, StableHandleTree
from apexcrew.adapters.repository.no_follow_posix import PosixNoFollowBackend
from apexcrew.adapters.repository.no_follow_windows import WindowsNoFollowBackend
from apexcrew.domain.admission import TaskCandidate
from apexcrew.domain.plan import CanonicalPath, PathValidationError
from apexcrew.domain.types import AttemptId, GitOid, RunId, TaskId

MAX_CANDIDATE_FILE_BYTES = 64 * 1024 * 1024
_OID = re.compile(r"^[0-9a-f]{40}$")


class CandidatePreparationError(RuntimeError):
    """A fail-closed preparation error; no candidate is produced."""


class CandidatePreparationGitRunner(Protocol):
    def run(
        self,
        repository: RepositoryInstance,
        operation: object,
        *,
        index_file: Path | None = None,
    ) -> subprocess.CompletedProcess[str]: ...


class CandidatePreparationAdapter:
    """Build one deterministic commit from an Attempt workspace and a Run Head."""

    def __init__(
        self,
        repository: RepositoryInstance,
        runner: CandidatePreparationGitRunner,
        data_root: Path,
    ) -> None:
        if not data_root.is_absolute():
            raise ValueError("CANDIDATE_DATA_ROOT_MUST_BE_ABSOLUTE")
        self._repository = repository
        self._runner = runner
        self._data_root = data_root

    def prepare_task_candidate(
        self,
        *,
        run_id: RunId,
        task_id: TaskId,
        attempt_id: AttemptId,
        run_head_oid: GitOid,
        workspace: Path | object,
        changed_paths: Sequence[str | CanonicalPath],
        message: str,
    ) -> TaskCandidate:
        run_component = self._component(run_id, "RUN_ID_INVALID")
        attempt_component = self._component(attempt_id, "ATTEMPT_ID_INVALID")
        if not message or "\x00" in message or "\r" in message or "\n" in message:
            raise CandidatePreparationError("COMMIT_MESSAGE_INVALID")
        workspace_root = self._workspace_root(workspace)
        paths = self._canonical_paths(changed_paths)
        index_file = self._index_file(run_component, attempt_component)
        self._reserve_index_path(index_file)
        tree: StableHandleTree | None = None
        try:
            tree = self._open_workspace(workspace_root)
            self._run(GitReadTree(run_head_oid), index_file=index_file)
            for path in paths:
                self._prepare_path(tree, workspace_root, path, index_file)
            tree.assert_name_bindings()
            tree_oid = self._output_oid(
                self._run(GitWriteTree(), index_file=index_file), "WRITE_TREE_FAILED"
            )
            prepared_oid = self._output_oid(
                self._run(GitCommitTree(tree_oid, run_head_oid, message), index_file=index_file),
                "COMMIT_TREE_FAILED",
            )
            return TaskCandidate.create(
                run_id=run_id,
                task_id=task_id,
                attempt_id=attempt_id,
                expected_run_head_oid=run_head_oid,
                prepared_oid=prepared_oid,
                changed_paths=tuple(str(path) for path in paths),
            )
        except CandidatePreparationError:
            raise
        except (OSError, RepositoryUnsafeError, PathValidationError, ValueError) as error:
            raise CandidatePreparationError("WORKSPACE_ENTRY_NOT_REGULAR") from error
        finally:
            if tree is not None:
                tree.close()
            self._remove_index(index_file)

    def _prepare_path(
        self,
        tree: StableHandleTree,
        workspace_root: Path,
        path: CanonicalPath,
        index_file: Path,
    ) -> None:
        node = tree.try_open_any(str(path))
        if node is None:
            self._run(GitRemoveIndexPath(path), index_file=index_file)
            return
        if node.identity.kind != "file":
            raise CandidatePreparationError("WORKSPACE_ENTRY_NOT_REGULAR")
        content = tree.read_bytes(node, MAX_CANDIDATE_FILE_BYTES)
        del content
        tree.assert_name_bindings()
        mode = "100644"
        if os.name == "posix" and stat.S_IMODE(os.fstat(node.handle).st_mode) & 0o111:
            mode = "100755"
        blob_path = workspace_root.joinpath(*str(path).split("/"))
        blob_result = self._run(GitHashObjectWrite(blob_path), index_file=index_file)
        blob_oid = self._output_oid(blob_result, "HASH_OBJECT_FAILED")
        tree.assert_name_bindings()
        self._run(
            GitUpdateIndexCacheInfo(mode, blob_oid, path),
            index_file=index_file,
        )

    def _run(
        self,
        operation: object,
        *,
        index_file: Path,
    ) -> subprocess.CompletedProcess[str]:
        try:
            result = self._runner.run(self._repository, operation, index_file=index_file)
        except (OSError, RepositoryUnsafeError, ValueError) as error:
            raise CandidatePreparationError("GIT_PREPARATION_DENIED") from error
        if result.returncode != 0:
            code = {
                GitReadTree: "READ_TREE_FAILED",
                GitHashObjectWrite: "HASH_OBJECT_FAILED",
                GitUpdateIndexCacheInfo: "UPDATE_INDEX_FAILED",
                GitRemoveIndexPath: "REMOVE_INDEX_FAILED",
                GitWriteTree: "WRITE_TREE_FAILED",
                GitCommitTree: "COMMIT_TREE_FAILED",
            }.get(type(operation), "GIT_PREPARATION_FAILED")
            raise CandidatePreparationError(code)
        self._repository = self._repository.refresh_after_verified_owned_transition()
        return result

    @staticmethod
    def _output_oid(result: subprocess.CompletedProcess[str], code: str) -> GitOid:
        value = result.stdout.strip()
        if not _OID.fullmatch(value):
            raise CandidatePreparationError(code)
        return GitOid(value)

    def _index_file(self, run_id: str, attempt_id: str) -> Path:
        return self._data_root / "index" / run_id / f"{attempt_id}.idx"

    @staticmethod
    def _component(value: object, code: str) -> str:
        text = str(value)
        if (
            not text
            or len(text) > 128
            or any(
                character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-"
                for character in text
            )
        ):
            raise CandidatePreparationError(code)
        return text

    @staticmethod
    def _canonical_paths(
        values: Sequence[str | CanonicalPath],
    ) -> tuple[CanonicalPath, ...]:
        try:
            paths = tuple(sorted({CanonicalPath.parse(str(value)) for value in values}, key=str))
        except (PathValidationError, TypeError, ValueError) as error:
            raise CandidatePreparationError("CHANGED_PATH_INVALID") from error
        if not paths:
            raise CandidatePreparationError("CHANGED_PATHS_REQUIRED")
        return paths

    @staticmethod
    def _workspace_root(workspace: object) -> Path:
        root = (
            workspace
            if isinstance(workspace, (str, os.PathLike))
            else getattr(workspace, "root", None)
        )
        if not isinstance(root, (str, os.PathLike)):
            raise CandidatePreparationError("WORKSPACE_ROOT_INVALID")
        path = Path(root)
        if not path.is_absolute():
            raise CandidatePreparationError("WORKSPACE_ROOT_INVALID")
        return path

    @staticmethod
    def _open_workspace(root: Path) -> StableHandleTree:
        backend = WindowsNoFollowBackend() if os.name == "nt" else PosixNoFollowBackend()
        try:
            return StableHandleTree(root, backend)
        except (OSError, RepositoryUnsafeError) as error:
            raise CandidatePreparationError("WORKSPACE_ROOT_INVALID") from error

    def _reserve_index_path(self, index_file: Path) -> None:
        current = self._data_root
        if current.exists() and (current.is_symlink() or not current.is_dir()):
            raise CandidatePreparationError("CANDIDATE_DATA_ROOT_UNSAFE")
        current.mkdir(parents=True, exist_ok=True)
        index_root = index_file.parent
        index_root.mkdir(parents=True, exist_ok=True)
        if index_root.is_symlink() or not index_root.is_dir() or index_file.exists():
            raise CandidatePreparationError("INDEX_PATH_UNSAFE")

    @staticmethod
    def _remove_index(index_file: Path) -> None:
        try:
            if index_file.is_symlink() or index_file.exists():
                index_file.unlink()
        except OSError as error:
            raise CandidatePreparationError("INDEX_CLEANUP_FAILED") from error


__all__ = ["CandidatePreparationAdapter", "CandidatePreparationError"]
