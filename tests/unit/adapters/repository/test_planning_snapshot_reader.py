import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

import pytest

from apexcrew.adapters.repository.git import (
    GitLsTreePath,
    GitOperation,
    RepositoryInstance,
    RepositoryUnsafeError,
    SubprocessGitSpawner,
)
from apexcrew.adapters.repository.planning import GitPlanningSnapshotReader, PlanningReadDenied
from apexcrew.domain.coordination import PlanningTurnBinding
from apexcrew.domain.plan import CanonicalPath
from apexcrew.domain.revisions import HardDeniedPathClass, PlanningReadAuthorizationDocument
from apexcrew.domain.types import GitOid, RepositoryId


@dataclass
class ScriptedBytesRunner:
    outputs: list[bytes]
    calls: list[GitOperation] = field(default_factory=list)

    def run_bytes(
        self, repository: RepositoryInstance, operation: GitOperation
    ) -> subprocess.CompletedProcess[bytes]:
        del repository
        self.calls.append(operation)
        return subprocess.CompletedProcess([], 0, self.outputs.pop(0), b"")


class UnsafeBytesRunner:
    def run_bytes(
        self, repository: RepositoryInstance, operation: GitOperation
    ) -> subprocess.CompletedProcess[bytes]:
        del repository, operation
        raise RepositoryUnsafeError("GIT_OUTPUT_LIMIT_EXCEEDED")


class AllowingPlanningPathGate:
    def require_allowed(
        self, path: CanonicalPath, authorization: PlanningReadAuthorizationDocument
    ) -> None:
        del path, authorization

    def require_manifest_allowed(self, path: CanonicalPath, binding: PlanningTurnBinding) -> None:
        del path, binding


class DenyingPlanningPathGate(AllowingPlanningPathGate):
    def require_allowed(
        self, path: CanonicalPath, authorization: PlanningReadAuthorizationDocument
    ) -> None:
        del path, authorization
        raise ValueError("SECRET_PATH_DENIED")


def planning_read_authorization() -> PlanningReadAuthorizationDocument:
    return PlanningReadAuthorizationDocument(
        matcher_version="apexcrew-path-v1",
        positive_globs=("src/**",),
        hard_denied_path_classes=tuple(HardDeniedPathClass),
        max_manifest_entries=2_000,
        max_manifest_bytes=131_072,
        max_file_bytes=131_072,
        max_total_returned_bytes=2_097_152,
        max_search_matches=200,
        max_search_bytes=65_536,
    )


def test_snapshot_reader_rejects_nonregular_tree_entry_before_blob_read() -> None:
    base = GitOid("1" * 40)
    runner = ScriptedBytesRunner([b"120000 blob " + b"2" * 40 + b"\tsrc/link\0"])
    reader = GitPlanningSnapshotReader(
        cast(RepositoryInstance, object()),
        RepositoryId("repository-1"),
        runner,
        AllowingPlanningPathGate(),
    )
    with pytest.raises(PlanningReadDenied, match="PLANNING_TRACKED_REGULAR_FILE_REQUIRED"):
        reader.read_tracked_file(
            base, CanonicalPath.parse("src/link"), planning_read_authorization()
        )
    assert runner.calls == [GitLsTreePath(base, CanonicalPath.parse("src/link"))]


def test_snapshot_reader_checks_blob_size_before_reading_content() -> None:
    base = GitOid("1" * 40)
    runner = ScriptedBytesRunner(
        [
            b"100644 blob " + b"2" * 40 + b"\tsrc/a.py\0",
            b"131073\n",
        ]
    )
    reader = GitPlanningSnapshotReader(
        cast(RepositoryInstance, object()),
        RepositoryId("repository-1"),
        runner,
        AllowingPlanningPathGate(),
    )
    with pytest.raises(PlanningReadDenied, match="PLANNING_BLOB_TOO_LARGE"):
        reader.read_tracked_file(
            base, CanonicalPath.parse("src/a.py"), planning_read_authorization()
        )
    assert len(runner.calls) == 2


def test_snapshot_reader_converts_path_policy_failures_to_opaque_denials() -> None:
    runner = ScriptedBytesRunner([])
    reader = GitPlanningSnapshotReader(
        cast(RepositoryInstance, object()),
        RepositoryId("repository-1"),
        runner,
        DenyingPlanningPathGate(),
    )
    with pytest.raises(PlanningReadDenied, match="PLANNING_PATH_DENIED"):
        reader.read_tracked_file(
            GitOid("1" * 40), CanonicalPath.parse("src/secret.py"), planning_read_authorization()
        )
    assert runner.calls == []


def test_snapshot_reader_converts_repository_safety_failures_to_planning_denials() -> None:
    reader = GitPlanningSnapshotReader(
        cast(RepositoryInstance, object()),
        RepositoryId("repository-1"),
        UnsafeBytesRunner(),
        AllowingPlanningPathGate(),
    )
    with pytest.raises(PlanningReadDenied, match="PLANNING_REPOSITORY_READ_DENIED"):
        reader.read_tracked_file(
            GitOid("1" * 40), CanonicalPath.parse("src/a.py"), planning_read_authorization()
        )


def test_git_spawner_rejects_oversized_output_without_unbounded_capture(tmp_path: Path) -> None:
    spawner = SubprocessGitSpawner()
    with pytest.raises(RepositoryUnsafeError, match="GIT_OUTPUT_LIMIT_EXCEEDED"):
        spawner.run(
            (sys.executable, "-c", "import sys; sys.stdout.write('x' * 2000001)"),
            tmp_path,
            os.environ,
            text=False,
        )
