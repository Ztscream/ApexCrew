from __future__ import annotations

import json
import os
import shutil
import subprocess
from decimal import Decimal
from pathlib import Path

import pytest

from apexcrew.adapters.executor.restricted import RestrictedDockerExecutor
from apexcrew.domain.effects import sha256_digest
from apexcrew.domain.revisions import ExecutorProfileDocument, ToolVersionDocument
from apexcrew.domain.tools import SanitizedSnapshot, SanitizedSnapshotEntry


def _docker_image_digest() -> str:
    digest = os.environ.get("APEXCREW_EXECUTOR_IMAGE_DIGEST")
    if not digest:
        pytest.skip("APEXCREW_EXECUTOR_IMAGE_DIGEST is not set")
    if shutil.which("docker") is None:
        pytest.skip("Docker CLI is not installed")
    daemon = subprocess.run(("docker", "info"), capture_output=True, check=False, shell=False)
    if daemon.returncode != 0:
        pytest.skip("Docker daemon is unavailable")
    image = subprocess.run(
        ("docker", "image", "inspect", digest),
        capture_output=True,
        check=False,
        shell=False,
    )
    if image.returncode != 0:
        pytest.skip("configured executor image is unavailable")
    return digest


def test_docker_executor_is_the_only_composed_check_path() -> None:
    executor = RestrictedDockerExecutor(
        ExecutorProfileDocument(
            image_digest="sha256:" + "0" * 64,
            platform="linux",
            architecture="x86_64",
            tool_versions=(ToolVersionDocument(name="python", version="3.12"),),
            allowed_executables=("python",),
            environment_allowlist=("PATH",),
            run_as_uid=1000,
            run_as_gid=1000,
            root_filesystem_read_only=True,
            network_mode="none",
            cpu_limit=Decimal(1),
            memory_limit_bytes=64 * 1024 * 1024,
            pids_limit=64,
            scratch_limit_bytes=1024 * 1024,
            drop_all_capabilities=True,
            no_new_privileges=True,
        )
    )

    command = executor.command_for(("python", "-c", "pass"))

    assert isinstance(executor, RestrictedDockerExecutor)
    assert command[:3] == ("docker", "run", "--rm")
    assert "--network=none" in command
    assert "--read-only" in command
    assert "--cap-drop=ALL" in command
    assert "--security-opt=no-new-privileges" in command
    assert "LocalSubprocessExecutor" not in type(executor).__name__
    assert "APEXCREW_HOST_EXECUTOR" not in repr(executor)


def test_committed_image_enforces_restricted_executor_boundary(tmp_path: Path) -> None:
    digest = _docker_image_digest()
    marker = "print('approved snapshot')\n"
    input_root = tmp_path / "input"
    input_root.mkdir()
    (input_root / "check.py").write_bytes(marker.encode("utf-8"))
    snapshot = SanitizedSnapshot(
        root=input_root,
        repository_id="docker-fixture",
        tree_digest=sha256_digest(marker),
        dependency_fingerprint_digest=sha256_digest(marker),
        entries=(
            SanitizedSnapshotEntry(
                path="check.py",
                kind="regular",
                content_digest=sha256_digest(marker),
            ),
        ),
        materialized_paths=("check.py",),
    )
    profile = ExecutorProfileDocument(
        image_digest=digest,
        platform="linux",
        architecture="x86_64",
        tool_versions=(ToolVersionDocument(name="python", version="3.12"),),
        allowed_executables=("python",),
        environment_allowlist=("PATH",),
        run_as_uid=1000,
        run_as_gid=1000,
        root_filesystem_read_only=True,
        network_mode="none",
        cpu_limit=Decimal(1),
        memory_limit_bytes=64 * 1024 * 1024,
        pids_limit=64,
        scratch_limit_bytes=1024 * 1024,
        drop_all_capabilities=True,
        no_new_privileges=True,
    )
    executor = RestrictedDockerExecutor(profile)
    probe = """import json, os, pathlib, socket
root_write = ''
try:
    pathlib.Path('/app/restricted-probe').write_text('x')
    root_write = 'allowed'
except Exception as error:
    root_write = type(error).__name__
network = ''
try:
    socket.create_connection(('1.1.1.1', 80), 1)
    network = 'allowed'
except Exception as error:
    network = type(error).__name__
pathlib.Path('created-by-check').write_text('discard')
status = pathlib.Path('/proc/self/status').read_text()
print(json.dumps({'uid': os.getuid(), 'gid': os.getgid(), 'marker': pathlib.Path('check.py').read_text(), 'root_write': root_write, 'network': network, 'no_new_privs': status.split('NoNewPrivs:')[1].splitlines()[0].strip(), 'cap_eff': status.split('CapEff:')[1].splitlines()[0].strip()}))"""

    result = executor.run(
        ("python", "-c", probe),
        snapshot,
        timeout_seconds=30,
        environment={"PATH": "/usr/local/bin:/usr/bin:/bin"},
    )

    assert result.code == "CHECK_PASSED", result.output
    observed = json.loads(result.output)
    assert observed["uid"] == 1000
    assert observed["gid"] == 1000
    assert observed["marker"] == marker
    assert observed["root_write"] in {"OSError", "PermissionError"}
    assert observed["network"] == "OSError"
    assert observed["no_new_privs"] == "1"
    assert observed["cap_eff"] == "0000000000000000"
    assert not (input_root / "created-by-check").exists()
