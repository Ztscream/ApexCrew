from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
from base64 import b32encode
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import typer

from apexcrew.adapters.credentials.model_key import (
    DEEPSEEK_PROFILE,
    KeyringModelCredentialStore,
)
from apexcrew.adapters.repository.bootstrap import (
    RepositoryBootstrapAuthorityService,
    RepositoryBootstrapError,
)
from apexcrew.adapters.repository.control_path import ControlPathGuard
from apexcrew.adapters.repository.no_follow import RepositoryUnsafeError
from apexcrew.adapters.state.sqlite import SqliteStateStore
from apexcrew.application.composition import ApplicationBundle, build_application_bundle
from apexcrew.application.configuration import (
    ConfigurationError,
    RunOptions,
    build_create_run_payload,
)
from apexcrew.application.control import (
    ControlCommandService,
    CrewControlService,
    TargetAuthorityDigestService,
)
from apexcrew.domain.commands import (
    ApplicableRevisionDigests,
    ApproveBudgetPayload,
    ApproveModelConfigurationPayload,
    ApprovePlanPayload,
    ApprovePolicyPayload,
    BeginPlanningPayload,
    CommandEnvelope,
    CommandOutcome,
    CommandPayload,
    CreateRunPayload,
    GrantPayload,
    IntegratePayload,
    ReconcileCleanupPayload,
    StartPayload,
)
from apexcrew.domain.effects import StateConflict, canonical_json
from apexcrew.domain.revisions import Sha256DigestText
from apexcrew.domain.types import (
    AuditSequence,
    CandidateId,
    EvidenceBundleDigest,
    GitOid,
    PendingActionId,
    RevisionDigest,
    RunId,
)

app = typer.Typer(no_args_is_help=True, add_completion=False)
credentials_app = typer.Typer(no_args_is_help=True, add_completion=False)
app.add_typer(credentials_app, name="credentials")


def _emit(status: str, **fields: object) -> None:
    typer.echo(json.dumps({"status": status, **fields}, sort_keys=True))


def _failed_invariant(error: BaseException) -> str:
    if isinstance(error, ConfigurationError):
        return "CONFIGURATION_INVALID"
    if isinstance(error, RepositoryUnsafeError):
        return "CONTROL_PATH_UNSAFE"
    if isinstance(error, RepositoryBootstrapError):
        return "REPOSITORY_BOOTSTRAP_REJECTED"
    if isinstance(error, sqlite3.Error):
        return "STATE_STORE_UNAVAILABLE"
    if isinstance(error, StateConflict):
        return "STATE_CONFLICT"
    if isinstance(error, OSError):
        return "CONTROL_PATH_UNSAFE"
    if isinstance(error, (subprocess.TimeoutExpired, TimeoutError, UnicodeError, ValueError)):
        return "REPOSITORY_BOOTSTRAP_REJECTED"
    return "BOOTSTRAP_FAILED"


def _reject(status: str, error: BaseException) -> None:
    _emit(status, failed_invariant=_failed_invariant(error))
    raise typer.Exit(code=1)


@dataclass(frozen=True, slots=True)
class _RunCommandContext:
    run_id: RunId
    sequence: AuditSequence
    current: ApplicableRevisionDigests
    approved: ApplicableRevisionDigests
    proposed_plan_digest: RevisionDigest | None
    candidate_id: CandidateId | None = None
    evidence_bundle_digest: EvidenceBundleDigest | None = None
    candidate_head_oid: GitOid | None = None
    prepared_oid: GitOid | None = None


def _read_run_context(root: Path, run_id: RunId) -> _RunCommandContext:
    with ControlPathGuard(root.resolve()) as control_paths:
        connection = control_paths.open_existing_database_read_only()
        try:
            row = connection.execute(
                "SELECT runs.current_plan_digest, runs.current_policy_digest, "
                "runs.current_budget_digest, runs.current_model_configuration_digest, "
                "run_sequences.current_sequence FROM runs "
                "JOIN run_sequences USING(run_id) WHERE runs.run_id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise StateConflict("RUN_NOT_FOUND")
            approved_rows = connection.execute(
                "SELECT revision_class, revision_digest FROM revision_approvals WHERE run_id = ?",
                (run_id,),
            ).fetchall()
            approved_classes = {str(item[0]): str(item[1]) for item in approved_rows}
            current = ApplicableRevisionDigests(
                plan_digest=None if row[0] is None else RevisionDigest(str(row[0])),
                policy_digest=RevisionDigest(str(row[1])),
                budget_digest=RevisionDigest(str(row[2])),
                model_configuration_digest=RevisionDigest(str(row[3])),
            )
            approved = ApplicableRevisionDigests(
                plan_digest=(
                    current.plan_digest
                    if approved_classes.get("PLAN") == str(current.plan_digest)
                    else None
                ),
                policy_digest=(
                    current.policy_digest
                    if approved_classes.get("POLICY") == str(current.policy_digest)
                    else None
                ),
                budget_digest=(
                    current.budget_digest
                    if approved_classes.get("BUDGET") == str(current.budget_digest)
                    else None
                ),
                model_configuration_digest=(
                    current.model_configuration_digest
                    if approved_classes.get("MODEL_CONFIGURATION")
                    == str(current.model_configuration_digest)
                    else None
                ),
            )
            proposal = connection.execute(
                "SELECT plan_digest FROM plans WHERE run_id = ? AND state = 'PROPOSED' "
                "ORDER BY rowid DESC LIMIT 1",
                (run_id,),
            ).fetchone()
            candidate = connection.execute(
                "SELECT candidate_id, evidence_bundle_digest, candidate_json, prepared_oid "
                "FROM run_candidates WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            candidate_id: CandidateId | None = None
            evidence: EvidenceBundleDigest | None = None
            head_oid: GitOid | None = None
            prepared: GitOid | None = None
            if candidate is not None:
                candidate_id = CandidateId(str(candidate[0]))
                evidence = EvidenceBundleDigest(str(candidate[1]))
                candidate_json = json.loads(str(candidate[2]))
                head_oid = GitOid(str(candidate_json["head_oid"]))
                if candidate[3] is not None:
                    prepared = GitOid(str(candidate[3]))
            return _RunCommandContext(
                run_id=run_id,
                sequence=AuditSequence(int(row[4])),
                current=current,
                approved=approved,
                proposed_plan_digest=(
                    None if proposal is None else RevisionDigest(str(proposal[0]))
                ),
                candidate_id=candidate_id,
                evidence_bundle_digest=evidence,
                candidate_head_oid=head_oid,
                prepared_oid=prepared,
            )
        finally:
            connection.close()


def _open_delivery_bundle(root: Path) -> tuple[ControlPathGuard, ApplicationBundle]:
    guard = ControlPathGuard(root.resolve())
    try:
        connection = guard.open_existing_database_read_only()
        connection.close()
        bundle = build_application_bundle(root.resolve())
    except BaseException:
        guard.close()
        raise
    return guard, bundle


def _command_request_id(payload: CommandPayload) -> str:
    digest = hashlib.sha256(canonical_json(payload.model_dump(mode="json")).encode("utf-8"))
    return "cli-command-" + digest.hexdigest()


def _handle_run_command(
    root: Path,
    payload: CommandPayload,
    *,
    bindings: Literal["current", "approved"],
) -> CommandOutcome:
    payload_run_id = getattr(payload, "run_id", None)
    if not isinstance(payload_run_id, str):
        raise TypeError("RUN_ID_REQUIRED")
    run_id = RunId(payload_run_id)
    context = _read_run_context(root, run_id)
    guard, bundle = _open_delivery_bundle(root)
    try:
        command = CommandEnvelope(
            request_id=_command_request_id(payload),
            expected_sequence=context.sequence,
            applicable_revision_digests=(
                context.current if bindings == "current" else context.approved
            ),
            payload=payload,
        )
        outcome = bundle.control.handle(command)
        guard.assert_current()
        return outcome
    finally:
        bundle.close()
        guard.close()


def _emit_outcome(outcome: CommandOutcome) -> None:
    status = str(outcome.status)
    fields: dict[str, object] = {
        "run_id": outcome.run_id,
        "resulting_sequence": outcome.resulting_sequence,
    }
    failed = outcome.failed_invariant
    if failed is not None:
        fields["failed_invariant"] = failed
    if status != "ACCEPTED":
        _emit("COMMAND_REJECTED", command_status=status, **fields)
        raise typer.Exit(code=1)
    _emit("COMMAND_ACCEPTED", command_status=status, **fields)


class _SqliteTargetAuthorityDigestService(TargetAuthorityDigestService):
    def __init__(self, store: SqliteStateStore) -> None:
        self._store = store

    def current_for(self, run_id: RunId) -> Sha256DigestText:
        return self._store.target_authority_digest(run_id)


@app.command()
def init(root: Path = typer.Option(Path("."), exists=True, file_okay=False)) -> None:  # noqa: B008
    """Create non-sensitive local ApexCrew configuration."""
    try:
        root = root.resolve()
        repository_authority = RepositoryBootstrapAuthorityService()
        try:
            repository_authority.validate_repository(root)
        finally:
            repository_authority.close()
        with ControlPathGuard(root) as control_paths:
            control_paths.ensure()
            control_paths.write_config_if_missing()
            config = control_paths.state.config
    except (
        ConfigurationError,
        RepositoryBootstrapError,
        RepositoryUnsafeError,
        sqlite3.Error,
        subprocess.TimeoutExpired,
        TimeoutError,
        UnicodeError,
        OSError,
        ValueError,
        StateConflict,
    ) as error:
        _reject("INIT_REJECTED", error)
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
    store: SqliteStateStore | None = None
    try:
        root = root.resolve()
        options = RunOptions(
            goal=goal,
            constraints=tuple(constraints),
            acceptance_criteria=tuple(acceptance),
            repository_root=root,
            target_ref=target_ref,
        )
        repository_authority = RepositoryBootstrapAuthorityService()
        try:
            authority = repository_authority.inspect(str(options.repository_root), target_ref)
            payload = build_create_run_payload(options, target_oid=authority.target_oid)
            command = CommandEnvelope(
                request_id=_run_create_request_id(payload),
                expected_sequence=None,
                applicable_revision_digests=ApplicableRevisionDigests(),
                payload=payload,
            )
            with ControlPathGuard(root) as control_paths:
                database = control_paths.prepare_database()
                store = SqliteStateStore(database, connection=control_paths.open_database())
                control = CrewControlService(
                    ControlCommandService(
                        state=store,
                        target_authority=_SqliteTargetAuthorityDigestService(store),
                        repository_authority=repository_authority,
                    )
                )
                outcome = control.handle(command)
                control_paths.assert_current()
        finally:
            if store is not None:
                store.close()
            repository_authority.close()
    except (
        ConfigurationError,
        RepositoryBootstrapError,
        RepositoryUnsafeError,
        sqlite3.Error,
        subprocess.TimeoutExpired,
        TimeoutError,
        UnicodeError,
        OSError,
        ValueError,
        StateConflict,
    ) as error:
        _reject("RUN_CREATE_REJECTED", error)
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
    guard = None
    bundle = None
    try:
        guard, bundle = _open_delivery_bundle(root)
        stop = bundle.runtime.run_until_blocked(RunId(run_id))
        guard.assert_current()
    except RepositoryUnsafeError as error:
        if str(error) == "CONTROL_DATABASE_NOT_FOUND":
            _emit("NO_RUNTIME_PERMIT", run_id=run_id)
            return
        _reject("RUN_REJECTED", error)
    except (
        ConfigurationError,
        RepositoryBootstrapError,
        sqlite3.Error,
        subprocess.TimeoutExpired,
        TimeoutError,
        UnicodeError,
        OSError,
        ValueError,
        StateConflict,
        RuntimeError,
    ) as error:
        _reject("RUN_REJECTED", error)
    finally:
        if bundle is not None:
            bundle.close()
        if guard is not None:
            guard.close()
    fields: dict[str, object] = {
        "run_id": stop.run_id,
        "state": stop.state,
        "reason": stop.reason,
        "last_sequence": stop.last_sequence,
    }
    if stop.pending is not None:
        fields["pending"] = stop.pending.model_dump(mode="json")
    _emit(str(stop.reason), **fields)


@app.command("reconcile-cleanup")
def reconcile_cleanup(
    run_id: str,
    root: Path = typer.Option(Path("."), exists=True, file_okay=False),  # noqa: B008
) -> None:
    """Issue the terminal-administrative Permit for exact target cleanup."""
    try:
        outcome = _handle_run_command(
            root,
            ReconcileCleanupPayload(run_id=RunId(run_id)),
            bindings="current",
        )
    except (
        ConfigurationError,
        RepositoryBootstrapError,
        RepositoryUnsafeError,
        sqlite3.Error,
        OSError,
        ValueError,
        StateConflict,
        RuntimeError,
    ) as error:
        _reject("RECONCILE_CLEANUP_REJECTED", error)
    _emit_outcome(outcome)


@app.command()
def show(run_id: str, root: Path = typer.Option(Path("."), exists=True, file_okay=False)) -> None:  # noqa: B008
    """Render the sanitized RunQueries projection."""
    guard = None
    bundle = None
    try:
        guard, bundle = _open_delivery_bundle(root)
        view = bundle.queries.get(RunId(run_id))
        guard.assert_current()
    except (
        ConfigurationError,
        RepositoryBootstrapError,
        RepositoryUnsafeError,
        sqlite3.Error,
        OSError,
        ValueError,
        StateConflict,
        RuntimeError,
    ) as error:
        _reject("SHOW_REJECTED", error)
    finally:
        if bundle is not None:
            bundle.close()
        if guard is not None:
            guard.close()
    _emit("RUN_VIEW", **view.model_dump(mode="json"))


@app.command("begin-planning")
def begin_planning(
    run_id: str,
    root: Path = typer.Option(Path("."), exists=True, file_okay=False),  # noqa: B008
) -> None:
    """Issue one planning Runtime Permit after bootstrap approvals."""
    try:
        outcome = _handle_run_command(
            root,
            BeginPlanningPayload(run_id=RunId(run_id)),
            bindings="current",
        )
    except (
        ConfigurationError,
        RepositoryBootstrapError,
        RepositoryUnsafeError,
        sqlite3.Error,
        OSError,
        ValueError,
        StateConflict,
        RuntimeError,
    ) as error:
        _reject("BEGIN_PLANNING_REJECTED", error)
    _emit_outcome(outcome)


@app.command("approve-plan")
def approve_plan(
    run_id: str,
    root: Path = typer.Option(Path("."), exists=True, file_okay=False),  # noqa: B008
    digest: str | None = typer.Option(None, "--digest"),
    confirmation_code: str | None = typer.Option(None, "--confirmation-code"),
    preview: bool = typer.Option(False, "--preview"),
) -> None:
    """Preview or approve the exact proposed Plan revision."""
    try:
        context = _read_run_context(root, RunId(run_id))
        plan_digest = context.proposed_plan_digest
        if plan_digest is None:
            raise StateConflict("PLAN_PROPOSAL_NOT_FOUND")
        if digest is not None and RevisionDigest(digest) != plan_digest:
            _emit("APPROVAL_REJECTED", failed_invariant="REVISION_DIGEST_MISMATCH")
            raise typer.Exit(code=1)
        expected_code = _approval_confirmation_code(
            "approve_plan", RunId(run_id), "PLAN", plan_digest
        )
        if preview:
            _emit(
                "APPROVAL_PREVIEW",
                confirmation_code=expected_code,
                revision_digest=plan_digest,
                revision_kind="plan",
                run_id=run_id,
            )
            return
        if digest is None or confirmation_code is None:
            _emit("APPROVAL_REJECTED", failed_invariant="APPROVAL_ARGUMENTS_REQUIRED")
            raise typer.Exit(code=2)
        outcome = _handle_run_command(
            root,
            ApprovePlanPayload(
                run_id=RunId(run_id),
                plan_digest=plan_digest,
                confirmation_code=confirmation_code,
            ),
            bindings="current",
        )
    except typer.Exit:
        raise
    except (
        ConfigurationError,
        RepositoryBootstrapError,
        RepositoryUnsafeError,
        sqlite3.Error,
        OSError,
        ValueError,
        StateConflict,
        RuntimeError,
    ) as error:
        _reject("APPROVE_PLAN_REJECTED", error)
    _emit_outcome(outcome)


@app.command("start")
def start(
    run_id: str,
    plan_digest: str = typer.Option(..., "--plan-digest"),
    root: Path = typer.Option(Path("."), exists=True, file_okay=False),  # noqa: B008
) -> None:
    """Issue one start Runtime Permit for an approved plan."""
    try:
        outcome = _handle_run_command(
            root,
            StartPayload(run_id=RunId(run_id), plan_digest=RevisionDigest(plan_digest)),
            bindings="current",
        )
    except (
        ConfigurationError,
        RepositoryBootstrapError,
        RepositoryUnsafeError,
        sqlite3.Error,
        OSError,
        ValueError,
        StateConflict,
        RuntimeError,
    ) as error:
        _reject("START_REJECTED", error)
    _emit_outcome(outcome)


@app.command("grant")
def grant(
    run_id: str,
    pending_action_id: str = typer.Option(..., "--pending-action-id"),
    pending_action_digest: str = typer.Option(..., "--pending-action-digest"),
    confirmation_code: str = typer.Option(..., "--confirmation-code"),
    root: Path = typer.Option(Path("."), exists=True, file_okay=False),  # noqa: B008
) -> None:
    """Consume the exact one-use Grant for a pending risky action."""
    try:
        outcome = _handle_run_command(
            root,
            GrantPayload(
                run_id=RunId(run_id),
                pending_action_id=PendingActionId(pending_action_id),
                pending_action_digest=Sha256DigestText(pending_action_digest),
                confirmation_code=confirmation_code,
            ),
            bindings="current",
        )
    except (
        ConfigurationError,
        RepositoryBootstrapError,
        RepositoryUnsafeError,
        sqlite3.Error,
        OSError,
        ValueError,
        StateConflict,
        RuntimeError,
    ) as error:
        _reject("GRANT_REJECTED", error)
    _emit_outcome(outcome)


@app.command("integrate")
def integrate(
    run_id: str,
    candidate_id: str | None = typer.Option(None, "--candidate-id"),
    evidence_bundle_digest: str | None = typer.Option(None, "--evidence-bundle-digest"),
    expected_target_oid: str | None = typer.Option(None, "--expected-target-oid"),
    prepared_oid: str | None = typer.Option(None, "--prepared-oid"),
    confirmation_code: str | None = typer.Option(None, "--confirmation-code"),
    preview: bool = typer.Option(False, "--preview"),
    root: Path = typer.Option(Path("."), exists=True, file_okay=False),  # noqa: B008
) -> None:
    """Preview or submit the exact frozen Candidate integration command."""
    try:
        context = _read_run_context(root, RunId(run_id))
        if (
            context.candidate_id is None
            or context.evidence_bundle_digest is None
            or context.candidate_head_oid is None
        ):
            raise StateConflict("FINAL_CANDIDATE_NOT_FOUND")
        expected_code = _approval_confirmation_code(
            "integrate",
            RunId(run_id),
            "FINAL_CANDIDATE",
            RevisionDigest(str(context.evidence_bundle_digest)),
        )
        if preview:
            _emit(
                "INTEGRATION_PREVIEW",
                candidate_id=context.candidate_id,
                evidence_bundle_digest=context.evidence_bundle_digest,
                expected_target_oid=context.candidate_head_oid,
                prepared_oid=context.prepared_oid or context.candidate_head_oid,
                confirmation_code=expected_code,
                run_id=run_id,
            )
            return
        if any(
            value is None
            for value in (
                candidate_id,
                evidence_bundle_digest,
                expected_target_oid,
                prepared_oid,
                confirmation_code,
            )
        ):
            _emit("INTEGRATION_REJECTED", failed_invariant="INTEGRATION_ARGUMENTS_REQUIRED")
            raise typer.Exit(code=2)
        assert candidate_id is not None
        assert evidence_bundle_digest is not None
        assert expected_target_oid is not None
        assert prepared_oid is not None
        assert confirmation_code is not None
        outcome = _handle_run_command(
            root,
            IntegratePayload(
                run_id=RunId(run_id),
                candidate_id=CandidateId(candidate_id),
                evidence_bundle_digest=EvidenceBundleDigest(evidence_bundle_digest),
                expected_target_oid=GitOid(expected_target_oid),
                prepared_oid=GitOid(prepared_oid),
                confirmation_code=confirmation_code,
            ),
            bindings="current",
        )
    except typer.Exit:
        raise
    except (
        ConfigurationError,
        RepositoryBootstrapError,
        RepositoryUnsafeError,
        sqlite3.Error,
        OSError,
        ValueError,
        StateConflict,
        RuntimeError,
    ) as error:
        _reject("INTEGRATE_REJECTED", error)
    _emit_outcome(outcome)


@app.command()
def status(root: Path = typer.Option(Path("."), exists=True, file_okay=False)) -> None:  # noqa: B008
    """Read local CLI bootstrap status without reconciling a Run."""
    try:
        with ControlPathGuard(root.resolve()) as control_paths:
            initialized = control_paths.config_exists()
    except (
        RepositoryUnsafeError,
        OSError,
        ValueError,
    ) as error:
        _reject("STATUS_REJECTED", error)
    _emit("INITIALIZED" if initialized else "NOT_INITIALIZED")


@app.command("approve-policy")
def approve_policy(
    run_id: str,
    root: Path = typer.Option(Path("."), exists=True, file_okay=False),  # noqa: B008
    digest: str | None = typer.Option(None, "--digest"),
    confirmation_code: str | None = typer.Option(None, "--confirmation-code"),
    preview: bool = typer.Option(False, "--preview"),
) -> None:
    """Preview or submit the exact current Policy revision approval."""
    _approve_revision(
        "approve_policy", "policy", "POLICY", run_id, root, digest, confirmation_code, preview
    )


@app.command("approve-budget")
def approve_budget(
    run_id: str,
    root: Path = typer.Option(Path("."), exists=True, file_okay=False),  # noqa: B008
    digest: str | None = typer.Option(None, "--digest"),
    confirmation_code: str | None = typer.Option(None, "--confirmation-code"),
    preview: bool = typer.Option(False, "--preview"),
) -> None:
    """Preview or submit the exact current Budget revision approval."""
    _approve_revision(
        "approve_budget", "budget", "BUDGET", run_id, root, digest, confirmation_code, preview
    )


@app.command("approve-model")
def approve_model(
    run_id: str,
    root: Path = typer.Option(Path("."), exists=True, file_okay=False),  # noqa: B008
    digest: str | None = typer.Option(None, "--digest"),
    confirmation_code: str | None = typer.Option(None, "--confirmation-code"),
    preview: bool = typer.Option(False, "--preview"),
) -> None:
    """Preview or submit the exact current Model Configuration approval."""
    _approve_revision(
        "approve_model_configuration",
        "model_configuration",
        "MODEL_CONFIGURATION",
        run_id,
        root,
        digest,
        confirmation_code,
        preview,
    )


@app.command(
    "approve",
    hidden=True,
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def approve_legacy(ctx: typer.Context, run_id: str | None = None) -> None:
    """Reject the removed generic approval command without state access."""
    del ctx, run_id
    _emit("UNSUPPORTED_COMMAND", command="approve")
    raise typer.Exit(code=2)


def _approved_revision_bindings(
    store: SqliteStateStore, run_id: RunId
) -> ApplicableRevisionDigests:
    current = store.current_revision_digests(run_id)
    approved = frozenset(store.approved_revision_classes(run_id))
    return ApplicableRevisionDigests(
        plan_digest=current.plan_digest if "PLAN" in approved else None,
        policy_digest=current.policy_digest if "POLICY" in approved else None,
        budget_digest=current.budget_digest if "BUDGET" in approved else None,
        model_configuration_digest=current.model_configuration_digest
        if "MODEL_CONFIGURATION" in approved
        else None,
    )


def _approval_confirmation_code(
    command_kind: str,
    run_id: RunId,
    revision_class: str,
    digest: RevisionDigest,
) -> str:
    value = canonical_json(
        {
            "command_kind": command_kind,
            "revision_class": revision_class,
            "revision_digest": digest,
            "run_id": run_id,
        }
    ).encode("utf-8")
    return b32encode(hashlib.sha256(value).digest()).decode("ascii")[:6]


def _approval_payload(
    command_kind: Literal["approve_policy", "approve_budget", "approve_model_configuration"],
    run_id: RunId,
    digest: RevisionDigest,
    confirmation_code: str,
) -> CommandPayload:
    if command_kind == "approve_policy":
        return ApprovePolicyPayload(
            run_id=run_id,
            policy_digest=digest,
            confirmation_code=confirmation_code,
        )
    if command_kind == "approve_budget":
        return ApproveBudgetPayload(
            run_id=run_id,
            budget_digest=digest,
            confirmation_code=confirmation_code,
        )
    return ApproveModelConfigurationPayload(
        run_id=run_id,
        model_configuration_digest=digest,
        confirmation_code=confirmation_code,
    )


def _approval_request_id(payload: CommandPayload) -> str:
    digest = hashlib.sha256(canonical_json(payload.model_dump(mode="json")).encode("utf-8"))
    return "cli-approval-" + digest.hexdigest()


def _approve_revision(
    command_kind: Literal["approve_policy", "approve_budget", "approve_model_configuration"],
    revision_kind: Literal["policy", "budget", "model_configuration"],
    revision_class: Literal["POLICY", "BUDGET", "MODEL_CONFIGURATION"],
    run_id_text: str,
    root: Path,
    digest_text: str | None,
    confirmation_code: str | None,
    preview: bool,
) -> None:
    store: SqliteStateStore | None = None
    repository_authority = RepositoryBootstrapAuthorityService()
    try:
        run_id = RunId(run_id_text)
        with ControlPathGuard(root.resolve()) as control_paths:
            if preview:
                connection = control_paths.open_existing_database_read_only()
                try:
                    current_digest = SqliteStateStore.current_revision_digest_from_read_only(
                        connection, run_id, revision_class
                    )
                finally:
                    connection.close()
            else:
                database = control_paths.prepare_database()
                store = SqliteStateStore(database, connection=control_paths.open_database())
                current = store.current_revision_digests(run_id)
                current_digest_candidate = {
                    "POLICY": current.policy_digest,
                    "BUDGET": current.budget_digest,
                    "MODEL_CONFIGURATION": current.model_configuration_digest,
                }[revision_class]
                if current_digest_candidate is None:
                    raise StateConflict("REVISION_NOT_FOUND")
                current_digest = current_digest_candidate
            if digest_text is not None and RevisionDigest(digest_text) != current_digest:
                _emit(
                    "APPROVAL_REJECTED",
                    failed_invariant="REVISION_DIGEST_MISMATCH",
                    run_id=run_id_text,
                )
                raise typer.Exit(code=1)
            expected_code = _approval_confirmation_code(
                command_kind, run_id, revision_class, current_digest
            )
            if preview:
                _emit(
                    "APPROVAL_PREVIEW",
                    confirmation_code=expected_code,
                    revision_digest=current_digest,
                    revision_kind=revision_kind,
                    run_id=run_id,
                )
                return
            if digest_text is None or confirmation_code is None:
                _emit(
                    "APPROVAL_REJECTED",
                    failed_invariant="APPROVAL_ARGUMENTS_REQUIRED",
                    run_id=run_id_text,
                )
                raise typer.Exit(code=2)
            if store is None:
                raise StateConflict("STATE_STORE_UNAVAILABLE")
            payload = _approval_payload(command_kind, run_id, current_digest, confirmation_code)
            command = CommandEnvelope(
                request_id=_approval_request_id(payload),
                expected_sequence=store.audit_sequence(run_id),
                applicable_revision_digests=_approved_revision_bindings(store, run_id),
                payload=payload,
            )
            control = CrewControlService(
                ControlCommandService(
                    state=store,
                    target_authority=_SqliteTargetAuthorityDigestService(store),
                    repository_authority=repository_authority,
                )
            )
            outcome = control.handle(command)
            control_paths.assert_current()
    except typer.Exit:
        raise
    except (
        ConfigurationError,
        RepositoryBootstrapError,
        RepositoryUnsafeError,
        sqlite3.Error,
        subprocess.TimeoutExpired,
        TimeoutError,
        UnicodeError,
        OSError,
        ValueError,
        StateConflict,
    ) as error:
        _reject("APPROVAL_REJECTED", error)
    finally:
        try:
            if store is not None:
                store.close()
        finally:
            repository_authority.close()
    if outcome.status != "ACCEPTED":
        _emit(
            "APPROVAL_REJECTED",
            failed_invariant=outcome.failed_invariant or "UNKNOWN",
            resulting_sequence=outcome.resulting_sequence,
            run_id=run_id_text,
        )
        raise typer.Exit(code=1)
    _emit(
        "APPROVED",
        resulting_sequence=outcome.resulting_sequence,
        revision_digest=current_digest,
        revision_kind=revision_kind,
        run_id=run_id_text,
    )


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
    try:
        with ControlPathGuard(root.resolve()) as control_paths:
            initialized = control_paths.config_exists()
        source = KeyringModelCredentialStore().source(DEEPSEEK_PROFILE)
    except (
        RepositoryUnsafeError,
        OSError,
        ValueError,
    ) as error:
        _reject("DOCTOR_REJECTED", error)
    _emit("READY" if initialized else "NOT_INITIALIZED", credential_source=source)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
