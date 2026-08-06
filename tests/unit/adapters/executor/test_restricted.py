from __future__ import annotations

import subprocess
from decimal import Decimal
from io import BytesIO

import pytest

from apexcrew.adapters.executor.restricted import RestrictedDockerExecutor
from apexcrew.domain.effects import sha256_digest
from apexcrew.domain.revisions import ExecutorProfileDocument, ToolVersionDocument
from apexcrew.domain.tools import SanitizedSnapshot, SanitizedSnapshotEntry


def profile() -> ExecutorProfileDocument:
    return ExecutorProfileDocument(
        image_digest="sha256:" + "1" * 64,
        platform="linux",
        architecture="x86_64",
        tool_versions=(ToolVersionDocument(name="pytest", version="8"),),
        allowed_executables=("pytest",),
        environment_allowlist=("PATH",),
        run_as_uid=1000,
        run_as_gid=1000,
        root_filesystem_read_only=True,
        network_mode="none",
        cpu_limit=Decimal(1),
        memory_limit_bytes=64 * 1024 * 1024,
        pids_limit=64,
        scratch_limit_bytes=1_000_000,
        drop_all_capabilities=True,
        no_new_privileges=True,
    )


def snapshot(tmp_path):
    content = "print('fixture')\n"
    (tmp_path / "check.py").write_bytes(content.encode("utf-8"))
    digest = sha256_digest(content)
    return SanitizedSnapshot(
        root=tmp_path,
        repository_id="repository-1",
        tree_digest=digest,
        dependency_fingerprint_digest=digest,
        entries=(SanitizedSnapshotEntry(path="check.py", kind="regular", content_digest=digest),),
        materialized_paths=("check.py",),
    )


def test_restricted_command_is_digest_pinned_non_root_and_networkless() -> None:
    command = RestrictedDockerExecutor(profile()).command_for(("pytest", "-q"))
    assert command[:4] == ("docker", "run", "--rm", "--network=none")
    assert "--user=1000:1000" in command
    assert "--read-only" in command
    assert "--tmpfs=/tmp:rw,size=1000000" in command
    assert "--entrypoint=pytest" in command
    assert "--cap-drop=ALL" in command
    assert "--security-opt=no-new-privileges" in command
    assert "sha256:" + "1" * 64 in command
    assert "docker.sock" not in command


def test_restricted_executor_rejects_unapproved_executable() -> None:
    with pytest.raises(ValueError, match="EXECUTABLE_NOT_ALLOWED"):
        RestrictedDockerExecutor(profile()).command_for(("bash",))


def test_restricted_executor_runs_closed_argv_without_shell(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    observed: dict[str, object] = {}

    class FakeProcess:
        stdout = BytesIO(b"ok\n")
        stderr = BytesIO(b"")

        def wait(self, timeout: int) -> int:
            del timeout
            return 0

        def kill(self) -> None:
            return None

    def fake_popen(command: tuple[str, ...], **kwargs: object) -> FakeProcess:
        observed["command"] = command
        observed.update(kwargs)
        return FakeProcess()

    monkeypatch.setattr("apexcrew.adapters.executor.restricted.subprocess.Popen", fake_popen)

    result = RestrictedDockerExecutor(profile()).run(("pytest", "-q"), snapshot(tmp_path))

    assert result.code == "CHECK_PASSED"
    assert result.output == "ok\n"
    assert observed["shell"] is False
    assert observed["stdin"] is subprocess.DEVNULL
    assert "--network=none" in observed["command"]
    assert "--mount" in observed["command"]
    assert "--entrypoint=python" in observed["command"]


def test_restricted_executor_unavailable_is_typed_capability_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    def unavailable(command: tuple[str, ...], **kwargs: object) -> object:
        del command, kwargs
        raise FileNotFoundError("docker")

    monkeypatch.setattr("apexcrew.adapters.executor.restricted.subprocess.Popen", unavailable)

    result = RestrictedDockerExecutor(profile()).run(("pytest",), snapshot(tmp_path))

    assert result.code == "EXECUTOR_UNAVAILABLE"
    assert result.timed_out is False


def test_restricted_executor_exit_125_is_typed_capability_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    class UnavailableProcess:
        stdout = BytesIO(b"")
        stderr = BytesIO(b"docker unavailable\n")

        def wait(self, timeout: int) -> int:
            del timeout
            return 125

    monkeypatch.setattr(
        "apexcrew.adapters.executor.restricted.subprocess.Popen",
        lambda command, **kwargs: UnavailableProcess(),
    )

    result = RestrictedDockerExecutor(profile()).run(("pytest",), snapshot(tmp_path))

    assert result.code == "EXECUTOR_UNAVAILABLE"
    assert result.timed_out is False


def test_restricted_executor_mounts_materialized_snapshot_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    observed: dict[str, object] = {}

    class SuccessfulProcess:
        stdout = BytesIO(b"ok\n")
        stderr = BytesIO(b"")

        def wait(self, timeout: int) -> int:
            del timeout
            return 0

    def fake_popen(command: tuple[str, ...], **kwargs: object) -> SuccessfulProcess:
        observed["command"] = command
        return SuccessfulProcess()

    monkeypatch.setattr("apexcrew.adapters.executor.restricted.subprocess.Popen", fake_popen)

    result = RestrictedDockerExecutor(profile()).run(("pytest",), snapshot(tmp_path))

    assert result.code == "CHECK_PASSED"
    command = observed["command"]
    assert isinstance(command, tuple)
    mount = command[command.index("--mount") + 1]
    assert str(tmp_path).replace("\\", "/") not in mount


def test_restricted_executor_output_capture_is_bounded(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    class LargeOutputProcess:
        stdout = BytesIO(b"x" * 200_000)
        stderr = BytesIO(b"")

        def wait(self, timeout: int) -> int:
            del timeout
            return 1

        def kill(self) -> None:
            return None

    monkeypatch.setattr(
        "apexcrew.adapters.executor.restricted.subprocess.Popen",
        lambda command, **kwargs: LargeOutputProcess(),
    )

    result = RestrictedDockerExecutor(profile()).run(("pytest",), snapshot(tmp_path))

    assert result.code == "CHECK_FAILED"
    assert result.output_bytes <= 65_536
    assert result.output_truncated is True


def test_restricted_executor_timeout_is_infrastructure_uncertainty(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    class TimeoutProcess:
        stdout = BytesIO(b"partial")
        stderr = BytesIO(b"")

        def wait(self, timeout: int) -> int:
            del timeout
            raise subprocess.TimeoutExpired(("docker",), 1)

        def kill(self) -> None:
            return None

    monkeypatch.setattr(
        "apexcrew.adapters.executor.restricted.subprocess.Popen",
        lambda command, **kwargs: TimeoutProcess(),
    )

    result = RestrictedDockerExecutor(profile()).run(("pytest",), snapshot(tmp_path))

    assert result.code == "INFRASTRUCTURE_UNCERTAINTY"
    assert result.timed_out is True
