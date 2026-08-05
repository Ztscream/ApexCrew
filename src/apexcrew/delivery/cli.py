from __future__ import annotations

import json
from pathlib import Path

import typer

from apexcrew.adapters.credentials.model_key import (
    DEEPSEEK_PROFILE,
    KeyringModelCredentialStore,
)

app = typer.Typer(no_args_is_help=True, add_completion=False)
credentials_app = typer.Typer(no_args_is_help=True, add_completion=False)
app.add_typer(credentials_app, name="credentials")


def _emit(status: str, **fields: str) -> None:
    typer.echo(json.dumps({"status": status, **fields}, sort_keys=True))


def _config_path(root: Path) -> Path:
    return root / ".apexcrew" / "config.json"


@app.command()
def init(root: Path = typer.Option(Path("."), exists=True, file_okay=False)) -> None:  # noqa: B008
    """Create non-sensitive local ApexCrew configuration."""
    config = _config_path(root)
    config.parent.mkdir(parents=True, exist_ok=True)
    if not config.exists():
        config.write_text('{"schema_version":"cli-config-v1"}\n', encoding="utf-8")
    _emit("INITIALIZED", path=str(config))


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
