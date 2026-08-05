from __future__ import annotations

import hashlib
import json
from pathlib import Path

import typer

from apexcrew.adapters.credentials.model_key import (
    DEEPSEEK_PROFILE,
    KeyringModelCredentialStore,
)
from apexcrew.adapters.repository.bootstrap import RepositoryBootstrapAuthorityService
from apexcrew.adapters.state.sqlite import SqliteStateStore
from apexcrew.application.configuration import RunOptions, build_create_run_payload
from apexcrew.application.control import (
    ControlCommandService,
    CrewControlService,
    TargetAuthorityDigestService,
)
from apexcrew.domain.commands import ApplicableRevisionDigests, CommandEnvelope, CreateRunPayload
from apexcrew.domain.effects import canonical_json
from apexcrew.domain.revisions import Sha256DigestText
from apexcrew.domain.types import RunId

app = typer.Typer(no_args_is_help=True, add_completion=False)
credentials_app = typer.Typer(no_args_is_help=True, add_completion=False)
app.add_typer(credentials_app, name="credentials")


def _emit(status: str, **fields: str) -> None:
    typer.echo(json.dumps({"status": status, **fields}, sort_keys=True))


def _config_path(root: Path) -> Path:
    return root / ".apexcrew" / "config.json"


class _SqliteTargetAuthorityDigestService(TargetAuthorityDigestService):
    def __init__(self, store: SqliteStateStore) -> None:
        self._store = store

    def current_for(self, run_id: RunId) -> Sha256DigestText:
        return self._store.target_authority_digest(run_id)


@app.command()
def init(root: Path = typer.Option(Path("."), exists=True, file_okay=False)) -> None:  # noqa: B008
    """Create non-sensitive local ApexCrew configuration."""
    config = _config_path(root)
    config.parent.mkdir(parents=True, exist_ok=True)
    if not config.exists():
        config.write_text('{"schema_version":"cli-config-v1"}\n', encoding="utf-8")
    _emit("INITIALIZED", path=str(config))


@app.command("run-create")
def run_create(
    root: Path = typer.Option(Path("."), exists=True, file_okay=False),  # noqa: B008
    target_ref: str = typer.Option(..., "--target-ref"),
    goal: str = typer.Option(..., "--goal"),
    constraints: list[str] = typer.Option([], "--constraint"),  # noqa: B008
    acceptance: list[str] = typer.Option([], "--acceptance"),  # noqa: B008
) -> None:
    """Create and persist one DRAFT Run without dispatching a model request."""
    root = root.resolve()
    options = RunOptions(
        goal=goal,
        constraints=tuple(constraints),
        acceptance_criteria=tuple(acceptance),
        repository_root=root,
        target_ref=target_ref,
    )
    repository_authority = RepositoryBootstrapAuthorityService()
    authority = repository_authority.inspect(str(options.repository_root), target_ref)
    payload = build_create_run_payload(options, target_oid=authority.target_oid)
    command = CommandEnvelope(
        request_id=_run_create_request_id(payload),
        expected_sequence=None,
        applicable_revision_digests=ApplicableRevisionDigests(),
        payload=payload,
    )
    database = _config_path(root).with_name("state.db")
    database.parent.mkdir(parents=True, exist_ok=True)
    store = SqliteStateStore(database)
    try:
        control = CrewControlService(
            ControlCommandService(
                state=store,
                target_authority=_SqliteTargetAuthorityDigestService(store),
                repository_authority=repository_authority,
            )
        )
        outcome = control.handle(command)
    finally:
        store.close()
    if outcome.status != "ACCEPTED" or outcome.run_id is None:
        _emit(
            "RUN_CREATE_REJECTED",
            failed_invariant=outcome.failed_invariant or "UNKNOWN",
        )
        raise typer.Exit(code=1)
    _emit(
        "RUN_CREATED",
        run_id=str(outcome.run_id),
        repository_root=payload.repository_root,
        target_ref=payload.target_ref,
        target_oid=str(payload.expected_target_oid),
    )


def _run_create_request_id(payload: CreateRunPayload) -> str:
    value = payload.model_dump(mode="json")
    digest = hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
    return "run-create-" + digest


@app.command()
def run(run_id: str, root: Path = typer.Option(Path("."), exists=True, file_okay=False)) -> None:  # noqa: B008
    """Deliver one run through the Permit-gated runtime."""
    del root
    _emit("NO_RUNTIME_PERMIT", run_id=run_id)


@app.command()
def status(root: Path = typer.Option(Path("."), exists=True, file_okay=False)) -> None:  # noqa: B008
    """Read local CLI bootstrap status without reconciling a Run."""
    _emit("INITIALIZED" if _config_path(root).is_file() else "NOT_INITIALIZED")


@app.command()
def approve(run_id: str) -> None:
    """Submit an approval through CrewControl when composed by a host."""
    _emit("APPROVAL_REQUIRED", run_id=run_id)


@credentials_app.command("set")
def credentials_set(
    profile: str = typer.Option(DEEPSEEK_PROFILE, "--profile"),
    root: Path = typer.Option(Path("."), exists=True, file_okay=False),  # noqa: B008
) -> None:
    """Store one model credential using hidden interactive input."""
    del root
    credential = typer.prompt("Model credential", hide_input=True)
    KeyringModelCredentialStore().set(profile, credential)
    _emit("CREDENTIAL_SET", profile=profile)


@credentials_app.command("status")
def credentials_status(profile: str = typer.Option(DEEPSEEK_PROFILE, "--profile")) -> None:
    """Report only model credential source and presence."""
    source = KeyringModelCredentialStore().source(profile)
    _emit("CREDENTIAL_STATUS", source=source)


@credentials_app.command("clear")
def credentials_clear(profile: str = typer.Option(DEEPSEEK_PROFILE, "--profile")) -> None:
    """Remove one model credential; missing entries are already clear."""
    KeyringModelCredentialStore().clear(profile)
    _emit("CREDENTIAL_CLEARED", profile=profile)


@app.command()
def doctor(root: Path = typer.Option(Path("."), exists=True, file_okay=False)) -> None:  # noqa: B008
    """Run read-only local configuration checks."""
    config = _config_path(root)
    source = KeyringModelCredentialStore().source(DEEPSEEK_PROFILE)
    _emit("READY" if config.is_file() else "NOT_INITIALIZED", credential_source=source)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
