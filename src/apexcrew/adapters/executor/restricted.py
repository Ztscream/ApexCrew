from __future__ import annotations

from collections.abc import Sequence

from apexcrew.domain.revisions import ExecutorProfileDocument


class RestrictedDockerExecutor:
    """Builds the closed Docker invocation; host process execution is deferred."""

    def __init__(self, profile: ExecutorProfileDocument) -> None:
        self._profile = profile

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
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            f"--cpus={self._profile.cpu_limit}",
            f"--memory={self._profile.memory_limit_bytes}b",
            f"--pids-limit={self._profile.pids_limit}",
            self._profile.image_digest,
            *argv,
        )

    def run(self, argv: Sequence[str]) -> None:
        del argv
        # DEBT-M2-005: connect the closed argv builder to a restricted process runner.
        raise RuntimeError("RESTRICTED_EXECUTOR_RUNNER_NOT_CONNECTED")
