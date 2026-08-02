from pathlib import Path

from helpers.application import (
    approve_current_policy_budget_and_model,
    create_draft_with_three_proposals,
    make_application,
    make_begin_planning_command,
)

from apexcrew.adapters.state.sqlite import SqliteStateStore


def test_bootstrap_revisions_and_permit_survive_sqlite_restart(tmp_path: Path) -> None:
    app = make_application(tmp_path)
    run_id = create_draft_with_three_proposals(app)
    approve_current_policy_budget_and_model(app, run_id)
    accepted = app.control.handle(make_begin_planning_command(app, run_id, request_id="begin-1"))
    assert accepted.status == "ACCEPTED"
    app.store.close()
    reopened = SqliteStateStore(app.database)
    assert reopened.current_revision_digests(run_id) == app.current_revision_digests
    assert reopened.approved_revision_classes(run_id) == (
        "POLICY",
        "BUDGET",
        "MODEL_CONFIGURATION",
    )
    permit = reopened.unconsumed_permit(run_id)
    assert permit.source_request_id == "begin-1"
    assert permit.allowed_phase == "DRAFT"
    assert permit.applicable_revision_digests == app.current_revision_digests
    assert permit.target_authority_digest == app.target_authority_digest
    snapshot = reopened.public_run_snapshot(run_id, None)
    assert snapshot is not None
    assert snapshot.state == "DRAFT"
