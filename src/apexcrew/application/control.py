from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from apexcrew.domain.commands import CommandEnvelope, CommandOutcome
from apexcrew.domain.revisions import Sha256DigestText
from apexcrew.domain.types import GitOid, RepositoryId, RunId


@dataclass(frozen=True, slots=True)
class BootstrapRepositoryAuthority:
    repository_root: str
    repository_id: RepositoryId
    repository_instance_digest: Sha256DigestText
    target_ref: str
    target_oid: GitOid


class RepositoryBootstrapAuthorityService(Protocol):
    def inspect(self, repository_root: str, target_ref: str) -> BootstrapRepositoryAuthority:
        raise NotImplementedError


class CommandHandler(Protocol):
    def apply(self, command: CommandEnvelope) -> CommandOutcome:
        raise NotImplementedError


class TargetAuthorityDigestService(Protocol):
    def current_for(self, run_id: RunId) -> Sha256DigestText:
        raise NotImplementedError


class ControlState(Protocol):
    def apply_control_command(
        self,
        command: CommandEnvelope,
        target_authority: TargetAuthorityDigestService,
        repository_authority: RepositoryBootstrapAuthorityService,
    ) -> CommandOutcome:
        raise NotImplementedError


class ControlCommandService(CommandHandler):
    def __init__(
        self,
        state: ControlState,
        target_authority: TargetAuthorityDigestService,
        repository_authority: RepositoryBootstrapAuthorityService,
    ) -> None:
        self._state = state
        self._target_authority = target_authority
        self._repository_authority = repository_authority

    def apply(self, command: CommandEnvelope) -> CommandOutcome:
        return self._state.apply_control_command(
            command,
            self._target_authority,
            self._repository_authority,
        )


class CrewControlService:
    def __init__(self, commands: CommandHandler) -> None:
        self._commands = commands

    def handle(self, command: CommandEnvelope) -> CommandOutcome:
        return self._commands.apply(command)
