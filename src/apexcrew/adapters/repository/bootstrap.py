from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Protocol

from apexcrew.adapters.repository.git import (
    GitCommandRunner,
    GitRepositoryPreflight,
    GitShowRefVerify,
    GitWorktreeListPorcelain,
    RepositoryInstance,
    RepositoryUnsafeError,
    parse_worktree_porcelain_nul,
)
from apexcrew.adapters.repository.no_follow import HandleIdentity
from apexcrew.application.control import BootstrapRepositoryAuthority
from apexcrew.domain.effects import canonical_json, sha256_digest
from apexcrew.domain.types import GitOid, RepositoryId


class RepositoryBootstrapError(ValueError):
    pass


class RepositoryPreflightPort(Protocol):
    def inspect(self, root: Path) -> RepositoryInstance: ...


class RepositoryBootstrapAuthorityService:
    def __init__(
        self,
        *,
        preflight: RepositoryPreflightPort | None = None,
        runner: GitCommandRunner | None = None,
        git_executable: Path | None = None,
        trusted_empty_dir: Path | None = None,
    ) -> None:
        self._preflight = GitRepositoryPreflight() if preflight is None else preflight
        if runner is not None:
            self._runner = runner
        else:
            executable = git_executable or _find_git_executable()
            self._runner = GitCommandRunner(executable, trusted_empty_dir)

    def close(self) -> None:
        close = getattr(self._runner, "close", None)
        if callable(close):
            close()

    def validate_repository(self, root: str | Path) -> None:
        try:
            repository = self._preflight.inspect(Path(root))
        except (OSError, RepositoryUnsafeError, ValueError) as error:
            raise RepositoryBootstrapError("repository preflight rejected root") from error
        repository.close()

    def inspect(self, repository_root: str, target_ref: str) -> BootstrapRepositoryAuthority:
        _require_direct_target_ref(target_ref)
        root = Path(repository_root)
        try:
            repository = self._preflight.inspect(root)
        except (OSError, RepositoryUnsafeError, ValueError) as error:
            raise RepositoryBootstrapError("repository preflight rejected root") from error
        try:
            worktrees = self._runner.run_bytes(repository, GitWorktreeListPorcelain())
            if worktrees.returncode != 0:
                raise RepositoryBootstrapError("worktree observation failed")
            try:
                records = parse_worktree_porcelain_nul(worktrees.stdout)
            except ValueError as error:
                raise RepositoryBootstrapError("worktree observation failed") from error
            if any(record.branch == target_ref for record in records):
                raise RepositoryBootstrapError("target ref is checked out")
            _validate_loose_target_ref(repository, target_ref)
            result = self._runner.run(repository, GitShowRefVerify(target_ref))
            if result.returncode != 0:
                raise RepositoryBootstrapError("target ref does not resolve")
            target_oid = _parse_target_oid(result.stdout)
            repository_identity = _repository_identity(repository)
            return BootstrapRepositoryAuthority(
                repository_root=str(repository.root),
                repository_id=RepositoryId(
                    "sha256:" + hashlib.sha256(_canonical_bytes(repository_identity)).hexdigest()
                ),
                repository_instance_digest=sha256_digest(
                    canonical_json(
                        {
                            "identity": repository_identity,
                            "storage": _storage_snapshot(repository),
                        }
                    )
                ),
                target_ref=target_ref,
                target_oid=target_oid,
            )
        finally:
            repository.close()


def _find_git_executable() -> Path:
    executable = shutil.which("git")
    if executable is None:
        raise RepositoryBootstrapError("absolute Git executable is unavailable")
    return Path(executable).resolve()


def _require_direct_target_ref(value: str) -> None:
    if (
        not value.startswith("refs/heads/")
        or value == "refs/heads/"
        or any(character.isspace() or character == "\x00" for character in value)
    ):
        raise RepositoryBootstrapError("direct target ref is required")


def _validate_loose_target_ref(repository: RepositoryInstance, target_ref: str) -> None:
    try:
        repository.assert_stable()
        node = repository.handles.try_open(".git/" + target_ref, "file")
        if node is None:
            return
        content = repository.handles.read_bytes(node, 128)
    except (OSError, RepositoryUnsafeError, ValueError) as error:
        raise RepositoryBootstrapError("target ref storage is unsafe") from error
    if content.startswith(b"ref:"):
        raise RepositoryBootstrapError("symbolic direct local ref is not allowed")
    if (
        len(content) != 41
        or content[-1:] != b"\n"
        or any(character not in b"0123456789abcdef" for character in content[:-1])
    ):
        raise RepositoryBootstrapError("target ref storage is malformed")


def _parse_target_oid(stdout: str) -> GitOid:
    value = stdout.strip()
    if len(value) != 40 or any(character not in "0123456789abcdef" for character in value):
        raise RepositoryBootstrapError("Git returned an invalid target OID")
    return GitOid(value)


def _repository_identity(repository: RepositoryInstance) -> dict[str, object]:
    return {
        "root": _identity(repository.root_identity),
        "git_dir": _identity(repository.git_dir_identity),
        "config": _identity(repository.config_identity),
        "index": None
        if repository.index_identity is None
        else _identity(repository.index_identity),
    }


def _storage_snapshot(repository: RepositoryInstance) -> list[dict[str, object]]:
    return [
        {
            "relative": node.relative,
            "identity": _identity(node.identity),
            "names": node.names,
        }
        for node in repository.storage_snapshot.nodes
    ]


def _identity(value: HandleIdentity) -> dict[str, object]:
    return {
        "platform": value.platform,
        "volume": value.volume,
        "file_id": value.file_id,
        "kind": value.kind,
    }


def _canonical_bytes(value: dict[str, object]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
