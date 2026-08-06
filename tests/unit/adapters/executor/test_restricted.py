from __future__ import annotations

import subprocess
from decimal import Decimal
from subprocess import CompletedProcess

import pytest

from apexcrew.adapters.executor.restricted import RestrictedDockerExecutor
from apexcrew.domain.revisions import ExecutorProfileDocument, ToolVersionDocument


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
        memory_limit_bytes=1_000_000,
        pids_limit=64,
        scratch_limit_bytes=1_000_000,
        drop_all_capabilities=True,
        no_new_privileges=True,
    )


def test_restricted_command_is_digest_pinned_non_root_and_networkless() -> None:
    command = RestrictedDockerExecutor(profile()).command_for(("pytest", "-q"))
    assert command[:4] == ("docker", "run", "--rm", "--network=none")
    assert "--user=1000:1000" in command
    assert "--read-only" in command
    assert "--cap-drop=ALL" in command
    assert "--security-opt=no-new-privileges" in command
    assert "sha256:" + "1" * 64 in command
    assert "docker.sock" not in command


def test_restricted_executor_rejects_unapproved_executable() -> None:
    with pytest.raises(ValueError, match="EXECUTABLE_NOT_ALLOWED"):
        RestrictedDockerExecutor(profile()).command_for(("bash",))


def test_restricted_executor_runs_closed_argv_without_shell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    def fake_run(command: tuple[str, ...], **kwargs: object) -> CompletedProcess[bytes]:
        observed["command"] = command
        observed.update(kwargs)
        return CompletedProcess(command, 0, stdout=b"ok\n", stderr=b"")

    monkeypatch.setattr("apexcrew.adapters.executor.restricted.subprocess.run", fake_run)

    result = RestrictedDockerExecutor(profile()).run(("pytest", "-q"))

    assert result.code == "CHECK_PASSED"
    assert result.output == "ok\n"
    assert observed["shell"] is False
    assert "--network=none" in observed["command"]


def test_restricted_executor_timeout_is_infrastructure_uncertainty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(command: tuple[str, ...], **kwargs: object) -> CompletedProcess[bytes]:
        del command, kwargs
        raise subprocess.TimeoutExpired(("docker",), 1, output=b"partial")

    monkeypatch.setattr("apexcrew.adapters.executor.restricted.subprocess.run", fake_run)

    result = RestrictedDockerExecutor(profile()).run(("pytest",))

    assert result.code == "INFRASTRUCTURE_UNCERTAINTY"
    assert result.timed_out is True
