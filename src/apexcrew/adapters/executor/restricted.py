from __future__ import annotations

import hashlib
import os
import signal
import stat
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from threading import Thread
from time import perf_counter
from typing import BinaryIO

from apexcrew.adapters.repository.snapshot import FilesystemRepositorySnapshot
from apexcrew.domain.plan import CanonicalPath
from apexcrew.domain.policy import SecretPathPolicy
from apexcrew.domain.revisions import ExecutorProfileDocument
from apexcrew.domain.tools import (
    MAX_EXECUTOR_OUTPUT_BYTES,
    ExecutionResult,
    SanitizedSnapshot,
    SnapshotNoFollowDenied,
    SnapshotUnavailable,
)


@dataclass
class _BoundedCapture:
    chunks: list[bytes] = field(default_factory=list)
    captured_bytes: int = 0
    truncated: bool = False


def _drain_stream(stream: BinaryIO, capture: _BoundedCapture) -> None:
    while True:
        chunk = stream.read(64 * 1024)
        if not chunk:
            return
        remaining = MAX_EXECUTOR_OUTPUT_BYTES - capture.captured_bytes
        if remaining > 0:
            capture.chunks.append(chunk[:remaining])
            capture.captured_bytes += min(len(chunk), remaining)
        if len(chunk) > remaining:
            capture.truncated = True


def _capture_chunks(capture: _BoundedCapture) -> tuple[bytes, ...]:
    if capture.truncated:
        return tuple(capture.chunks) + (b"\x00",)
    return tuple(capture.chunks)


class RestrictedDockerExecutor:
    """Run one structured check in the digest-pinned restricted image."""

    def __init__(
        self,
        profile: ExecutorProfileDocument,
        secret_paths: SecretPathPolicy | None = None,
    ) -> None:
        self._profile = profile
        self._secret_paths = secret_paths or SecretPathPolicy.from_host_rules(
            (), installation_key=b"restricted-executor-output-key"
        )

    def command_for(self, argv: Sequence[str]) -> tuple[str, ...]:
        self._validate_argv(argv)
        return (
            *self._base_command(),
            f"--entrypoint={argv[0]}",
            self._profile.image_digest,
            *argv[1:],
        )

    def _base_command(self) -> tuple[str, ...]:
        return (
            "docker",
            "run",
            "--rm",
            "--network=none",
            f"--user={self._profile.run_as_uid}:{self._profile.run_as_gid}",
            "--read-only",
            f"--tmpfs=/tmp:rw,size={self._profile.scratch_limit_bytes}",
            "--workdir=/tmp",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            f"--cpus={self._profile.cpu_limit}",
            f"--memory={self._profile.memory_limit_bytes}b",
            f"--pids-limit={self._profile.pids_limit}",
        )

    def _validate_argv(self, argv: Sequence[str]) -> None:
        if (
            not argv
            or any(not token or "\x00" in token for token in argv)
            or argv[0] not in self._profile.allowed_executables
        ):
            raise ValueError("EXECUTABLE_NOT_ALLOWED")

    def _materialize_snapshot(self, snapshot: SanitizedSnapshot, destination: Path) -> Path:
        try:
            root = snapshot.root
            if stat.S_ISLNK(root.lstat().st_mode) or not root.is_dir():
                raise ValueError("SANITIZED_SNAPSHOT_ROOT_INVALID")
            filesystem = FilesystemRepositorySnapshot(root)
            expected_paths = tuple(entry.path for entry in snapshot.entries)
            if expected_paths != snapshot.materialized_paths:
                raise ValueError("SANITIZED_SNAPSHOT_MANIFEST_MISMATCH")
            destination.mkdir(parents=True, exist_ok=False)
            total_bytes = 0
            for entry in snapshot.entries:
                path = CanonicalPath.parse(entry.path)
                if self._secret_paths.inspect(path).code != "ALLOW":
                    raise ValueError("SANITIZED_SNAPSHOT_DENIED")
                remaining = self._profile.scratch_limit_bytes - total_bytes
                if remaining < 0:
                    raise ValueError("SANITIZED_SNAPSHOT_TOO_LARGE")
                content = filesystem.read(path, remaining + 1)
                if len(content) > remaining:
                    raise ValueError("SANITIZED_SNAPSHOT_TOO_LARGE")
                digest = "sha256:" + hashlib.sha256(content).hexdigest()
                if digest != entry.content_digest:
                    raise ValueError("SANITIZED_SNAPSHOT_DIGEST_MISMATCH")
                total_bytes += len(content)
                target = destination.joinpath(*str(path).split("/"))
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("xb") as handle:
                    handle.write(content)
            return destination.resolve(strict=True)
        except (OSError, SnapshotNoFollowDenied, SnapshotUnavailable, ValueError) as error:
            if str(error).startswith("SANITIZED_SNAPSHOT_"):
                raise
            raise ValueError("SANITIZED_SNAPSHOT_INVALID") from error

    def _command_for_snapshot(self, argv: Sequence[str], snapshot_root: Path) -> tuple[str, ...]:
        self._validate_argv(argv)
        mount_source = snapshot_root.as_posix()
        if "," in mount_source:
            raise ValueError("SANITIZED_SNAPSHOT_ROOT_INVALID")
        return (
            *self._base_command(),
            "--mount",
            f"type=bind,source={mount_source},destination=/apexcrew-input,readonly",
            "--entrypoint=python",
            self._profile.image_digest,
            "-m",
            "apexcrew.adapters.executor.runner",
            "--input",
            "/apexcrew-input",
            "--workspace",
            "/tmp/workspace",
            "--",
            *argv,
        )

    def run(
        self,
        argv: Sequence[str],
        snapshot: SanitizedSnapshot | None = None,
        timeout_seconds: int = 600,
        environment: Mapping[str, str] | None = None,
    ) -> ExecutionResult:
        if timeout_seconds <= 0:
            raise ValueError("EXECUTOR_TIMEOUT_INVALID")
        if snapshot is None:
            raise ValueError("SANITIZED_SNAPSHOT_REQUIRED")
        supplied_environment = dict(environment or {})
        if any(
            not name
            or "\x00" in name
            or "\x00" in value
            or name not in self._profile.environment_allowlist
            for name, value in supplied_environment.items()
        ):
            raise ValueError("EXECUTOR_ENVIRONMENT_NOT_ALLOWED")
        with tempfile.TemporaryDirectory(prefix="apexcrew-executor-") as temporary_root:
            snapshot_root = self._materialize_snapshot(snapshot, Path(temporary_root) / "input")
            base_command = self._command_for_snapshot(argv, snapshot_root)
            image_index = base_command.index(self._profile.image_digest)
            env_flags = tuple(
                item
                for name, value in sorted(supplied_environment.items())
                for item in ("--env", f"{name}={value}")
            )
            command = base_command[:image_index] + env_flags + base_command[image_index:]
            started = perf_counter()
            stdout_capture = _BoundedCapture()
            stderr_capture = _BoundedCapture()
            try:
                if os.name == "nt":
                    process = subprocess.Popen(
                        command,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        shell=False,
                        env={},
                        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
                    )
                else:
                    process = subprocess.Popen(
                        command,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        shell=False,
                        env={},
                        start_new_session=True,
                    )
            except (FileNotFoundError, OSError):
                return ExecutionResult.from_output(
                    exit_code=None,
                    timed_out=False,
                    timing_ms=max(0, int((perf_counter() - started) * 1000)),
                    secret_paths=self._secret_paths,
                    executor_unavailable=True,
                )
            assert process.stdout is not None
            assert process.stderr is not None
            stdout_thread = Thread(
                target=_drain_stream, args=(process.stdout, stdout_capture), daemon=True
            )
            stderr_thread = Thread(
                target=_drain_stream, args=(process.stderr, stderr_capture), daemon=True
            )
            stdout_thread.start()
            stderr_thread.start()
            timed_out = False
            executor_unavailable = False
            try:
                exit_code = process.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
                self._kill_process_tree(process)
                try:
                    process.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    pass
                exit_code = None
            except OSError:
                executor_unavailable = True
                exit_code = None
            stdout_thread.join(timeout=5)
            stderr_thread.join(timeout=5)
            process.stdout.close()
            process.stderr.close()
            return ExecutionResult.from_output(
                exit_code=exit_code,
                timed_out=timed_out,
                stdout_chunks=_capture_chunks(stdout_capture),
                stderr_chunks=_capture_chunks(stderr_capture),
                timing_ms=max(0, int((perf_counter() - started) * 1000)),
                secret_paths=self._secret_paths,
                executor_unavailable=executor_unavailable,
            )

    @staticmethod
    def _kill_process_tree(process: subprocess.Popen[bytes]) -> None:
        pid = getattr(process, "pid", None)
        if isinstance(pid, int):
            try:
                if os.name == "nt":
                    subprocess.run(
                        ("taskkill", "/PID", str(pid), "/T", "/F"),
                        check=False,
                        shell=False,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=5,
                    )
                else:
                    kill_process_group = getattr(os, "killpg", None)
                    kill_signal = getattr(signal, "SIGKILL", None)
                    if callable(kill_process_group) and kill_signal is not None:
                        kill_process_group(pid, kill_signal)
            except (OSError, subprocess.SubprocessError):
                pass
        try:
            process.kill()
        except OSError:
            pass
