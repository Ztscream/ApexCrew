from pathlib import Path

import pytest
from helpers.application import (
    approve_current_policy_budget_and_model,
    create_draft_with_three_proposals,
    fixture_budget,
    make_active_application,
    make_application,
    make_approve_budget_replacement,
    make_approve_model_replacement,
    make_begin_planning_command,
    make_bounded_budget_replacement,
    make_create_run_command,
    make_paused_application,
    make_plan_reapproval,
    make_policy_replacement,
    make_priced_model_replacement,
    make_resume_command,
    make_unpriced_model_replacement,
)
from pydantic import ValidationError

import apexcrew
import apexcrew.application as application_surface
from apexcrew.domain.authority import AtomicAction, DispatchCloseCause
from apexcrew.domain.commands import (
    ApplicableRevisionDigests,
    BeginPlanningPayload,
    CommandEnvelope,
    ProposeBudgetPayload,
)
from apexcrew.domain.types import GitOid, RunId


def test_task_10_exports_only_control_and_query_application_protocols() -> None:
    assert application_surface.__all__ == ["CrewControl", "RunQueries"]
    assert apexcrew.__all__ == ["__version__"]
    assert not hasattr(apexcrew, "CrewControl")
    assert not hasattr(apexcrew, "CrewRuntime")
    assert not hasattr(apexcrew, "RunQueries")


def test_unknown_run_returns_closed_control_and_query_models(tmp_path: Path) -> None:
    app = make_application(tmp_path)
    run_id = RunId("run-does-not-exist")
    outcome = app.control.handle(
        CommandEnvelope(
            request_id="unknown-run",
            expected_sequence=0,
            applicable_revision_digests=ApplicableRevisionDigests(),
            payload=BeginPlanningPayload(run_id=run_id),
        )
    )
    assert outcome.status == "INVALID"
    assert outcome.failed_invariant == "RUN_NOT_FOUND"
    assert app.queries.get(run_id).availability == "RUN_NOT_FOUND"


def test_create_run_rejects_caller_target_that_disagrees_with_preflight(tmp_path: Path) -> None:
    app = make_application(tmp_path)
    command = make_create_run_command(request_id="mismatched-target")
    payload = command.payload.model_copy(update={"expected_target_oid": GitOid("b" * 40)})
    outcome = app.control.handle(command.model_copy(update={"payload": payload}))
    assert outcome.status == "INVALID"
    assert outcome.failed_invariant == "CREATE_RUN_BINDING_INVALID"
    assert app.store.run_count() == 0


def test_create_run_command_is_idempotent_and_query_is_side_effect_free(
    tmp_path: Path,
) -> None:
    app = make_application(tmp_path)
    command = make_create_run_command(request_id="create-1")
    outcome = app.control.handle(command)
    replay = app.control.handle(command)
    assert outcome.run_id is not None
    before = app.store.audit_sequence(outcome.run_id)
    projection = app.queries.get(outcome.run_id)
    after = app.store.audit_sequence(outcome.run_id)
    assert outcome.status == "ACCEPTED"
    assert replay == outcome
    assert app.store.run_count() == 1
    assert app.store.target_reservation_count(outcome.run_id) == 1
    assert projection.run_id == outcome.run_id
    assert projection.model_dump(exclude_none=True) == {
        "availability": "AVAILABLE",
        "run_id": outcome.run_id,
        "sequence": before,
        "state": "DRAFT",
    }
    assert before == after


def test_begin_planning_requires_all_three_exact_bootstrap_approvals(
    tmp_path: Path,
) -> None:
    app = make_application(tmp_path)
    run_id = create_draft_with_three_proposals(app)
    denied = app.control.handle(make_begin_planning_command(app, run_id))
    assert denied.status == "DENIED"
    assert denied.failed_invariant == "BOOTSTRAP_REVISIONS_NOT_APPROVED"
    approve_current_policy_budget_and_model(app, run_id)
    accepted = app.control.handle(make_begin_planning_command(app, run_id, request_id="begin-1"))
    assert accepted.status == "ACCEPTED"
    assert app.store.unconsumed_permit(run_id).allowed_phase == "DRAFT"
    snapshot = app.store.public_run_snapshot(run_id, None)
    assert snapshot is not None
    assert snapshot.state == "DRAFT"


def test_new_begin_planning_cannot_supersede_pending_delivery(tmp_path: Path) -> None:
    app = make_application(tmp_path)
    run_id = create_draft_with_three_proposals(app)
    approve_current_policy_budget_and_model(app, run_id)
    accepted = app.control.handle(make_begin_planning_command(app, run_id, request_id="begin-1"))
    assert accepted.status == "ACCEPTED"
    original = app.store.unconsumed_permit(run_id)
    conflict = app.control.handle(make_begin_planning_command(app, run_id, request_id="begin-2"))
    assert conflict.status == "CONFLICT"
    assert conflict.failed_invariant == "RUNTIME_DELIVERY_PENDING"
    assert app.store.unconsumed_permit(run_id) == original


def test_active_revision_mutability_is_closed_and_bounded(tmp_path: Path) -> None:
    app, run_id = make_active_application(tmp_path)
    assert app.control.handle(make_policy_replacement(app, run_id)).status == "INVALID"
    assert app.control.handle(make_plan_reapproval(app, run_id)).status == "INVALID"
    assert app.control.handle(make_unpriced_model_replacement(app, run_id)).status == "INVALID"
    assert app.control.handle(make_bounded_budget_replacement(app, run_id)).status == "ACCEPTED"
    assert app.control.handle(make_approve_budget_replacement(app, run_id)).status == "ACCEPTED"
    assert app.control.handle(make_priced_model_replacement(app, run_id)).status == "ACCEPTED"
    assert app.control.handle(make_approve_model_replacement(app, run_id)).status == "ACCEPTED"
    assert app.store.current_budget_digest(run_id) == app.proposed_budget_digest
    assert app.store.current_model_configuration_digest(run_id) == app.proposed_model_digest


def test_active_revision_replacement_waits_behind_atomic_action(tmp_path: Path) -> None:
    app, run_id = make_active_application(tmp_path)
    current_budget = app.store.current_budget_digest(run_id)
    assert current_budget is not None
    expected = app.store.audit_sequence(run_id)
    app.store.begin_atomic_action(
        AtomicAction(
            run_id=run_id,
            action_id="in-flight-revision-barrier",
            budget_digest=current_budget,
            state="IN_FLIGHT",
            opened_sequence=expected + 1,
        ),
        expected,
    )
    assert app.control.handle(make_bounded_budget_replacement(app, run_id)).status == "ACCEPTED"
    assert app.control.handle(make_approve_budget_replacement(app, run_id)).status == "ACCEPTED"
    assert app.store.current_budget_digest(run_id) == current_budget
    assert app.store.pending_budget_replacement(run_id) == app.proposed_budget_digest
    assert not app.store.new_dispatch_open(run_id)
    assert DispatchCloseCause.REVISION_REPLACEMENT in app.store.dispatch_close_causes(run_id)


def test_budget_proposal_rejects_fixed_v01_mechanism_fields_before_state(
    tmp_path: Path,
) -> None:
    app, run_id = make_active_application(tmp_path)
    before = app.store.audit_sequence(run_id)
    invalid_budget = fixture_budget().model_dump(mode="python")
    invalid_budget["task_call_ceiling"] = 47
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ProposeBudgetPayload.model_validate({"run_id": run_id, "budget_revision": invalid_budget})
    assert app.store.audit_sequence(run_id) == before
    assert app.store.pending_budget_replacement(run_id) is None


def test_resume_command_requires_the_exact_recorded_task_pause(tmp_path: Path) -> None:
    app, pause = make_paused_application(tmp_path, reason="REPEATED_CHECKPOINT")
    assert app.run_id is not None
    before = app.store.task_counters(app.run_id, pause.task_id)
    stale = app.control.handle(
        make_resume_command(
            app,
            app.run_id,
            task_id=pause.task_id,
            pause_sequence=pause.pause_sequence + 1,
            pause_reason=pause.pause_reason,
            request_id="resume-stale",
        )
    )
    assert stale.status == "STALE"
    assert app.store.task_counters(app.run_id, pause.task_id) == before
    accepted = app.control.handle(
        make_resume_command(
            app,
            app.run_id,
            task_id=pause.task_id,
            pause_sequence=pause.pause_sequence,
            pause_reason=pause.pause_reason,
        )
    )
    assert accepted.status == "ACCEPTED"
    assert app.store.task_counters(app.run_id, pause.task_id).model_calls == before.model_calls
    assert app.store.unconsumed_permit(app.run_id).allowed_phase == "PAUSED"
