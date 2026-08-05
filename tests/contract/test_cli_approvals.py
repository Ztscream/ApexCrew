from __future__ import annotations

import base64
import hashlib
import json
import subprocess
from pathlib import Path

from helpers.git_repository import commit_repository, make_git_repository
from typer.testing import CliRunner

from apexcrew.application.configuration import default_revision_documents
from apexcrew.delivery.cli import app
from apexcrew.domain.effects import canonical_json
from apexcrew.domain.revisions import revision_digest
from apexcrew.domain.types import RevisionDigest


def _repository(tmp_path: Path) -> Path:
    root = make_git_repository(tmp_path)
    (root / "README.md").write_text("approval contract\n", encoding="utf-8")
    commit_repository(root, "approval contract")
    subprocess.run(["git", "-C", str(root), "branch", "feature"], check=True)
    return root


def _run_create(root: Path) -> str:
    result = CliRunner().invoke(
        app,
        [
            "run-create",
            "--root",
            str(root),
            "--target-ref",
            "refs/heads/feature",
            "--goal",
            "approve revisions",
            "--acceptance",
            "one bounded approval",
        ],
    )
    assert result.exit_code == 0, result.stdout
    return str(json.loads(result.stdout)["run_id"])


def _confirmation_code(
    command_kind: str, run_id: str, revision_class: str, digest: RevisionDigest
) -> str:
    value = canonical_json(
        {
            "command_kind": command_kind,
            "revision_class": revision_class,
            "revision_digest": digest,
            "run_id": run_id,
        }
    ).encode("utf-8")
    return base64.b32encode(hashlib.sha256(value).digest()).decode("ascii")[:6]


def test_specialized_approval_commands_bind_exact_revision(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    run_id = _run_create(root)
    revisions = default_revision_documents()
    expected = (
        ("approve-policy", "policy", "POLICY", revision_digest(revisions.policy)),
        ("approve-budget", "budget", "BUDGET", revision_digest(revisions.budget)),
        (
            "approve-model",
            "model_configuration",
            "MODEL_CONFIGURATION",
            revision_digest(revisions.model_configuration),
        ),
    )

    runner = CliRunner()
    for command, kind, revision_class, digest in expected:
        preview = runner.invoke(app, [command, run_id, "--root", str(root), "--preview"])
        assert preview.exit_code == 0, preview.stdout
        preview_payload = json.loads(preview.stdout)
        command_kind = (
            "approve_model_configuration"
            if command == "approve-model"
            else command.replace("-", "_")
        )
        assert preview_payload == {
            "confirmation_code": _confirmation_code(command_kind, run_id, revision_class, digest),
            "revision_digest": digest,
            "revision_kind": kind,
            "run_id": run_id,
            "status": "APPROVAL_PREVIEW",
        }

        submitted = runner.invoke(
            app,
            [
                command,
                run_id,
                "--root",
                str(root),
                "--digest",
                str(digest),
                "--confirmation-code",
                str(preview_payload["confirmation_code"]),
            ],
        )
        assert submitted.exit_code == 0, submitted.stdout
        submitted_payload = json.loads(submitted.stdout)
        assert submitted_payload["status"] == "APPROVED"
        assert submitted_payload["revision_digest"] == digest
        assert submitted_payload["revision_kind"] == kind

    legacy = runner.invoke(app, ["approve", run_id])
    assert legacy.exit_code != 0
    assert json.loads(legacy.stdout) == {
        "command": "approve",
        "status": "UNSUPPORTED_COMMAND",
    }


def test_replayed_approval_is_side_effect_free(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    run_id = _run_create(root)
    digest = revision_digest(default_revision_documents().policy)
    runner = CliRunner()
    preview = runner.invoke(app, ["approve-policy", run_id, "--root", str(root), "--preview"])
    assert preview.exit_code == 0, preview.stdout
    code = str(json.loads(preview.stdout)["confirmation_code"])
    arguments = [
        "approve-policy",
        run_id,
        "--root",
        str(root),
        "--digest",
        str(digest),
        "--confirmation-code",
        code,
    ]

    first = runner.invoke(app, arguments)
    assert first.exit_code == 0, first.stdout
    first_payload = json.loads(first.stdout)
    assert first_payload["status"] == "APPROVED"

    replay = runner.invoke(app, arguments)
    assert replay.exit_code != 0
    replay_payload = json.loads(replay.stdout)
    assert replay_payload["status"] == "APPROVAL_REJECTED"
    assert replay_payload["failed_invariant"] == "IDEMPOTENCY_KEY_REUSE"
    assert replay_payload["resulting_sequence"] == first_payload["resulting_sequence"]
