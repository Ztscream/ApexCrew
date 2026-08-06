from __future__ import annotations

import subprocess
from collections.abc import Mapping, Sequence

from apexcrew.domain.policy import SecretPathPolicy
from apexcrew.domain.revisions import ExecutorProfileDocument
from apexcrew.domain.tools import ExecutionResult, SanitizedSnapshot


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
        if (
            not argv
            or any(not token or "\x00" in token for token in argv)
            or argv[0] not in self._profile.allowed_executables
        ):
            raise ValueError("EXECUTABLE_NOT_ALLOWED")
        return (
            "docker",
            "run",
            "--rm",
            "--network=none",
            f"--user={self._profile.run_as_uid}:{self._profile.run_as_gid}",
            "--read-only",
            f"--tmpfs=/tmp:rw,size={self._profile.scratch_limit_bytes}b",
            "--workdir=/tmp",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            f"--cpus={self._profile.cpu_limit}",
            f"--memory={self._profile.memory_limit_bytes}b",
            f"--pids-limit={self._profile.pids_limit}",
            self._profile.image_digest,
            *argv,
        )

    def run(
        self,
        argv: Sequence[str],
        snapshot: SanitizedSnapshot | None = None,
        timeout_seconds: int = 600,
        environment: Mapping[str, str] | None = None,
    ) -> ExecutionResult:
        del snapshot
        if timeout_seconds <= 0:
            raise ValueError("EXECUTOR_TIMEOUT_INVALID")
        supplied_environment = dict(environment or {})
        if any(name not in self._profile.environment_allowlist for name in supplied_environment):
            raise ValueError("EXECUTOR_ENVIRONMENT_NOT_ALLOWED")
        base_command = self.command_for(argv)
        image_index = base_command.index(self._profile.image_digest)
        env_flags = tuple(
            item
            for name, value in sorted(supplied_environment.items())
            for item in ("--env", f"{name}={value}")
        )
        command = base_command[:image_index] + env_flags + base_command[image_index:]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                timeout=timeout_seconds,
                check=False,
                shell=False,
                env={},
            )
        except subprocess.TimeoutExpired as error:
            return ExecutionResult.from_output(
                exit_code=None,
                timed_out=True,
                stdout_chunks=((error.stdout or b""),),
                stderr_chunks=((error.stderr or b""),),
                timing_ms=timeout_seconds * 1000,
                secret_paths=self._secret_paths,
            )
        except (FileNotFoundError, OSError) as error:
            raise RuntimeError("RESTRICTED_EXECUTOR_UNAVAILABLE") from error
        return ExecutionResult.from_output(
            exit_code=completed.returncode,
            timed_out=False,
            stdout_chunks=((completed.stdout or b""),),
            stderr_chunks=((completed.stderr or b""),),
            timing_ms=0,
            secret_paths=self._secret_paths,
        )
