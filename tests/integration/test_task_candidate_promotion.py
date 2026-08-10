from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
from dataclasses import dataclass, replace
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from apexcrew.adapters.repository.candidate_preparation import CandidatePreparationAdapter
from apexcrew.adapters.repository.git import (
    GitCommandRunner,
    GitPrivateRefStartGuard,
    GitRepositoryPreflight,
)
from apexcrew.adapters.state.memory import InMemoryStateStore
from apexcrew.adapters.state.sqlite import SqliteStateStore
from apexcrew.domain.admission import (
    PrivateRefCasOutcome,
    RefCasIntent,
    RefEffectBinding,
    RefPathBinding,
    TaskCandidate,
    TaskCandidateGateBinding,
    private_ref,
    task_candidate_lease_provenance_digest,
)
from apexcrew.domain.commands import ApplicableRevisionDigests
from apexcrew.domain.coordination import task_contract_digest, task_contract_json
from apexcrew.domain.effects import (
    AuditEvent,
    ReservationObservation,
    RunRefRecord,
    TargetReservation,
)
from apexcrew.domain.plan import TaskContract
from apexcrew.domain.revisions import (
    BudgetRevisionDocument,
    ModelPricingEntryDocument,
    Sha256DigestText,
)
from apexcrew.domain.types import (
    AttemptId,
    CandidateId,
    GitOid,
    RepositoryId,
    RevisionDigest,
    RunId,
    RunState,
    TaskId,
)
from apexcrew.domain.worker import WorkerTurnBinding


def _fixture_budget() -> BudgetRevisionDocument:
    return BudgetRevisionDocument(
        schema_version="budget-revision-v1",
        active_run_seconds_ceiling=28_800,
        task_ceiling=12,
        planning_request_ceiling=8,
        model_call_ceiling=240,
        input_token_ceiling=2_000_000,
        output_token_ceiling=200_000,
        cost_reserve_usd=Decimal(1),
        concurrent_worker_ceiling=3,
        pricing_observed_on=date(2026, 8, 5),
        pricing_entries=(
            ModelPricingEntryDocument(
                returned_model_id="deepseek-v4-flash",
                input_usd_per_million=Decimal("0.28"),
                output_usd_per_million=Decimal("0.56"),
            ),
        ),
    )


def _git(root: Path, *argv: str) -> str:
    environment = os.environ.copy()
    environment.update(
        {
            "GIT_AUTHOR_DATE": "2000-01-01T00:00:00Z",
            "GIT_COMMITTER_DATE": "2000-01-01T00:00:00Z",
        }
    )
    result = subprocess.run(
        ("git", "-C", str(root), *argv),
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    return result.stdout.strip()


@dataclass
class _ReservationObserver:
    reservation: TargetReservation
    binding_digest: Sha256DigestText

    def observe(self, reservation: TargetReservation) -> ReservationObservation:
        assert reservation == self.reservation
        return ReservationObservation(
            registration_present=True,
            path_present=True,
            locked=True,
            exact_identity=True,
            gitfile_only=True,
            admin_entry_name=reservation.admin_entry_name,
            admin_binding_digest=self.binding_digest,
        )


def _fixture(
    tmp_path: Path, *, private_oid: GitOid | str | None = None
) -> tuple[Path, GitOid, GitOid, GitPrivateRefStartGuard, RefCasIntent]:
    root = tmp_path / "repository"
    root.mkdir(parents=True)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.name", "ApexCrew Test")
    _git(root, "config", "user.email", "apexcrew@example.test")
    (root / "src").mkdir()
    (root / "src" / "task.py").write_text("value = 1\n", encoding="utf-8")
    (root / "README.md").write_text("unchanged\n", encoding="utf-8")
    _git(root, "add", "--all")
    _git(root, "commit", "-qm", "initial")
    head = GitOid(_git(root, "rev-parse", "HEAD"))
    _git(root, "update-ref", "refs/heads/unrelated", str(head))

    workspace = tmp_path / "workspace"
    (workspace / "src").mkdir(parents=True)
    (workspace / "src" / "task.py").write_text("value = 2\n", encoding="utf-8")
    executable = shutil.which("git")
    assert executable is not None
    repository = GitRepositoryPreflight().inspect(root)
    runner = GitCommandRunner(Path(executable).resolve())
    candidate = CandidatePreparationAdapter(
        repository, runner, tmp_path / "data"
    ).prepare_task_candidate(
        run_id=RunId("run-01"),
        task_id=TaskId("task-01"),
        attempt_id=AttemptId("attempt-01"),
        run_head_oid=head,
        workspace=workspace,
        changed_paths=("src/task.py",),
        message="prepare task-01",
    )
    repository.close()
    selected_private_oid = (
        candidate.prepared_oid if private_oid == "prepared" else private_oid or head
    )
    _git(
        root,
        "update-ref",
        "--create-reflog",
        private_ref(RunId("run-01")),
        str(selected_private_oid),
    )

    repository = GitRepositoryPreflight().inspect(root)
    reservation = TargetReservation(
        reservation_id="reservation-01",
        run_id=RunId("run-01"),
        target_ref="refs/heads/main",
        pinned_target_oid=head,
        path=tmp_path / "reservation",
        phase="ALLOCATED",
    )
    private_data_root = tmp_path / "private-data"
    private_data_root.mkdir()
    observer = _ReservationObserver(reservation, reservation.admin_binding_digest)
    guard = GitPrivateRefStartGuard(
        repository=repository,
        repository_id=RepositoryId("repository-01"),
        repository_instance_digest=Sha256DigestText("sha256:" + "b" * 64),
        runner=runner,
        reservation_observer=observer,
        reservation=reservation,
        target_safety_digest=Sha256DigestText("sha256:" + "c" * 64),
        reflog_message="apexcrew private run run-01",
        private_data_root=private_data_root,
    )
    binding = guard._effect_binding(RunId("run-01"))  # type: ignore[attr-defined]
    intent = RefCasIntent(
        intent_id="private-ref-cas-run-01-attempt-01",
        run_id=RunId("run-01"),
        kind="private_ref_cas",
        candidate_id=CandidateId(str(candidate.candidate_id)),
        repository_id=RepositoryId("repository-01"),
        ref_name=private_ref(RunId("run-01")),
        expected_old_oid=head,
        prepared_oid=candidate.prepared_oid,
        target_safety_digest=Sha256DigestText("sha256:" + "c" * 64),
        ref_effect_binding=binding,
        target_reservation_id=reservation.reservation_id,
        permit_generation=1,
        applicable_revision_digests=ApplicableRevisionDigests(),
        idempotency_key="private-ref-cas:run-01:attempt-01",
    )
    return root, head, candidate.prepared_oid, guard, intent


def test_private_promotion_changes_only_run_head(tmp_path: Path) -> None:
    root, head, prepared, guard, intent = _fixture(tmp_path)

    outcome = guard.promote_private_ref(intent)

    assert isinstance(outcome, PrivateRefCasOutcome)
    assert outcome.result_class == "PRIVATE_REF_PROMOTED"
    assert outcome.observed_oid == prepared
    assert _git(root, "rev-parse", "refs/heads/main") == str(head)
    assert _git(root, "rev-parse", "refs/heads/unrelated") == str(head)
    assert _git(root, "rev-parse", private_ref(RunId("run-01"))) == str(prepared)


def test_private_promotion_uses_canonical_private_data_root_lock(tmp_path: Path) -> None:
    _root, _head, _prepared, guard, intent = _fixture(tmp_path)

    outcome = guard.promote_private_ref(intent)

    assert outcome.result_class == "PRIVATE_REF_PROMOTED"
    assert (tmp_path / "private-data" / "run-ref-locks" / "run-01.lock").is_file()


def test_private_promotion_conflict_does_not_change_any_ref(tmp_path: Path) -> None:
    root, head, prepared, guard, intent = _fixture(tmp_path, private_oid="prepared")

    outcome = guard.promote_private_ref(intent)

    assert outcome.result_class == "PRIVATE_REF_CONFLICT"
    assert outcome.observed_oid == prepared
    assert _git(root, "rev-parse", "refs/heads/main") == str(head)
    assert _git(root, "rev-parse", "refs/heads/unrelated") == str(head)
    assert _git(root, "rev-parse", private_ref(RunId("run-01"))) == str(prepared)


def test_private_promotion_observation_failure_is_unobservable(tmp_path: Path) -> None:
    _root, _head, _prepared, guard, intent = _fixture(tmp_path)
    delegate = guard._runner  # type: ignore[attr-defined]

    class _FailingRunner:
        def run_bytes(self, repository, operation, *, index_file=None):  # type: ignore[no-untyped-def]
            return delegate.run_bytes(repository, operation, index_file=index_file)

        def run(self, _repository, _operation):  # type: ignore[no-untyped-def]
            raise OSError("observation unavailable")

    guard._runner = _FailingRunner()  # type: ignore[assignment, attr-defined]

    outcome = guard.promote_private_ref(intent)

    assert outcome.result_class == "PRIVATE_REF_UNOBSERVABLE"
    assert outcome.observed_oid is None


def _promotion_ref_binding() -> RefEffectBinding:
    regular = RefPathBinding(
        state="REGULAR_FILE", identity_digest=Sha256DigestText("sha256:" + "d" * 64)
    )
    absent = RefPathBinding(state="ABSENT")
    return RefEffectBinding(
        repository_instance_digest=Sha256DigestText("sha256:" + "b" * 64),
        checkout_registration_digest=Sha256DigestText("sha256:" + "c" * 64),
        ref_file=regular,
        ref_lock=absent,
        reflog=regular,
        reflog_lock=absent,
        reflog_exists=True,
        reflog_message="apexcrew private run run-01",
    )


def _fixture_digest(character: str) -> Sha256DigestText:
    return Sha256DigestText("sha256:" + character * 64)


def _state_fixture(
    tmp_path: Path, store: InMemoryStateStore | SqliteStateStore
) -> tuple[TaskCandidate, ApplicableRevisionDigests, RefEffectBinding]:
    run_id = RunId("run-01")
    head = GitOid("1" * 40)
    prepared = GitOid("2" * 40)
    revisions = ApplicableRevisionDigests(
        plan_digest=RevisionDigest(str(_fixture_digest("1"))),
        policy_digest=RevisionDigest(str(_fixture_digest("2"))),
        budget_digest=RevisionDigest(str(_fixture_digest("3"))),
        model_configuration_digest=RevisionDigest(str(_fixture_digest("4"))),
    )
    contract = TaskContract.from_strings(
        "task-01",
        read_globs=("src/**",),
        write_globs=("src/**",),
    )
    reservation = TargetReservation(
        reservation_id="reservation-01",
        run_id=run_id,
        target_ref="refs/heads/main",
        pinned_target_oid=head,
        path=tmp_path / "reservations" / "reservation-01",
        phase="ALLOCATED",
    )
    store.create_draft_with_reservation(
        run_id,
        RepositoryId("repository-01"),
        Sha256DigestText("sha256:" + "b" * 64),
        reservation,
    )
    expected = store.audit_sequence(run_id)

    if isinstance(store, InMemoryStateStore):

        def mutate(copied: InMemoryStateStore) -> None:
            copied._target_reservations[reservation.reservation_id] = replace(
                reservation,
                phase="REGISTERED_LOCKED",
                admin_entry_name="reservation-01",
                admin_binding_digest=Sha256DigestText("sha256:" + "a" * 64),
            )
            copied._runs[run_id] = replace(
                copied._runs[run_id],
                state=RunState.ACTIVE,
                run_head_oid=head,
                current_plan_digest=revisions.plan_digest,
                current_policy_digest=revisions.policy_digest,
                current_budget_digest=revisions.budget_digest,
                current_model_configuration_digest=revisions.model_configuration_digest,
            )
            copied._approved_budgets[run_id] = (revisions.budget_digest, _fixture_budget())
            copied._plan_task_contracts[revisions.plan_digest] = (contract,)
            copied._plan_dependency_edges[revisions.plan_digest] = ()
            copied._plan_hazard_edges[revisions.plan_digest] = ()
            copied._run_refs[(run_id, "PRIVATE")] = RunRefRecord(
                run_id,
                "PRIVATE",
                private_ref(run_id),
                head,
                head,
                "PRESENT",
                None,
            )

    else:

        def mutate(connection: sqlite3.Connection) -> None:
            connection.execute(
                "UPDATE target_reservations SET phase = 'REGISTERED_LOCKED', "
                "admin_entry_name = reservation_id, admin_binding_digest = ? "
                "WHERE run_id = ?",
                ("sha256:" + "a" * 64, run_id),
            )
            connection.execute(
                "UPDATE runs SET state = 'ACTIVE', run_head_oid = ? WHERE run_id = ?",
                (head, run_id),
            )
            connection.execute(
                "UPDATE runs SET current_plan_digest = ?, current_policy_digest = ?, "
                "current_budget_digest = ?, current_model_configuration_digest = ? "
                "WHERE run_id = ?",
                (
                    revisions.plan_digest,
                    revisions.policy_digest,
                    revisions.budget_digest,
                    revisions.model_configuration_digest,
                    run_id,
                ),
            )
            connection.execute(
                "INSERT INTO approved_budgets_for_test(run_id, budget_digest, budget_json) "
                "VALUES (?, ?, ?)",
                (run_id, revisions.budget_digest, _fixture_budget().model_dump_json()),
            )
            connection.execute(
                "INSERT INTO plans(run_id, plan_digest, base_run_head_oid, policy_digest, "
                "budget_digest, model_configuration_digest, run_check_set_digest, "
                "planning_request_count, state, proposal_json) VALUES (?, ?, ?, ?, ?, ?, ?, 1, "
                "'APPROVED', '{}')",
                (
                    run_id,
                    revisions.plan_digest,
                    head,
                    revisions.policy_digest,
                    revisions.budget_digest,
                    revisions.model_configuration_digest,
                    _fixture_digest("5"),
                ),
            )
            connection.execute(
                "INSERT INTO task_contracts(plan_digest, task_id, task_revision, "
                "contract_digest, contract_json, state) VALUES (?, ?, 1, ?, ?, 'READY')",
                (
                    revisions.plan_digest,
                    contract.task_id,
                    task_contract_digest(contract),
                    task_contract_json(contract),
                ),
            )
            connection.execute(
                "INSERT INTO run_refs(run_id, ref_kind, ref_name, expected_old_oid, current_oid, state) "
                "VALUES (?, 'PRIVATE', ?, ?, ?, 'PRESENT')",
                (run_id, private_ref(run_id), head, head),
            )

    store._commit_state_and_event(
        run_id=run_id,
        expected_sequence=expected,
        event=AuditEvent.kind("TEST_PRIVATE_REF_PRESENT"),
        mutate=mutate,
    )
    target_safety_digest = store.target_authority_digest(run_id)
    binding = WorkerTurnBinding(
        run_id=run_id,
        task_id=contract.task_id,
        attempt_id=AttemptId("attempt-01"),
        tranche_id="tranche-01",
        lease_id="lease-01",
        lease_generation=1,
        admissible_head=str(head),
        task_contract_digest=task_contract_digest(contract),
        plan_digest=revisions.plan_digest,
        policy_digest=revisions.policy_digest,
        budget_digest=revisions.budget_digest,
        model_configuration_digest=revisions.model_configuration_digest,
        tool_schema_digest=_fixture_digest("9"),
        target_safety_digest=target_safety_digest,
        credential_profile=None,
        repository_id="repository-01",
        snapshot_digest=_fixture_digest("7"),
        scope_digest=_fixture_digest("6"),
        dependency_fingerprint_basis=_fixture_digest("8"),
    )
    store.install_worker_attempt_for_test(binding)
    if isinstance(store, InMemoryStateStore):
        store._worker_attempts[binding.attempt_id] = replace(
            store._worker_attempts[binding.attempt_id], state="SUCCEEDED"
        )
        store._workspace_leases[(run_id, binding.lease_id)] = replace(
            store._workspace_leases[(run_id, binding.lease_id)], state="RELEASED"
        )
    else:
        with store._transaction("IMMEDIATE") as connection:
            connection.execute(
                "UPDATE worker_attempts SET state = 'SUCCEEDED' WHERE attempt_id = ?",
                (binding.attempt_id,),
            )
            connection.execute(
                "UPDATE workspace_leases SET state = 'RELEASED' WHERE lease_id = ?",
                (binding.lease_id,),
            )
    lease = store.workspace_lease(run_id, binding.lease_id)
    assert lease is not None
    prepared_at = lease.issued_at + timedelta(seconds=1)
    gate = TaskCandidateGateBinding(
        attempt_id=binding.attempt_id,
        task_contract_digest=binding.task_contract_digest,
        base_run_head_oid=head,
        post_tree_oid=GitOid("3" * 40),
        evidence_bundle_digest="sha256:" + "a" * 64,
        freshness_assessment_digest=_fixture_digest("b"),
        freshness_status="FRESH",
        applicable_revision_digests=revisions,
        target_safety_digest=target_safety_digest,
        scope_digest=binding.scope_digest,
        check_workspace_digest=binding.snapshot_digest,
        policy_decision="ALLOW",
        lease_id=binding.lease_id,
        lease_generation=lease.generation,
        lease_base_head_oid=GitOid(lease.base_head),
        lease_admissible_head_oid=GitOid(lease.admissible_head),
        lease_issued_at_utc=lease.issued_at,
        lease_expires_at_utc=lease.expires_at,
        prepared_at_utc=prepared_at,
        lease_provenance_digest=task_candidate_lease_provenance_digest(
            attempt_id=binding.attempt_id,
            lease_id=binding.lease_id,
            lease_generation=lease.generation,
            lease_base_head_oid=GitOid(lease.base_head),
            lease_admissible_head_oid=GitOid(lease.admissible_head),
            lease_issued_at_utc=lease.issued_at,
            lease_expires_at_utc=lease.expires_at,
            prepared_at_utc=prepared_at,
        ),
    )
    candidate = TaskCandidate.create(
        run_id=run_id,
        task_id=contract.task_id,
        attempt_id=binding.attempt_id,
        expected_run_head_oid=head,
        prepared_oid=prepared,
        prepared_tree_oid=gate.post_tree_oid,
        changed_paths=("src/task.py",),
        gate_binding=gate,
    )
    store.persist_task_candidate(candidate, store.audit_sequence(run_id))
    return candidate, revisions, _promotion_ref_binding()


@pytest.mark.parametrize("store_kind", ["memory", "sqlite"])
def test_state_promotion_intent_and_post_state_are_restart_visible(
    tmp_path: Path, store_kind: str
) -> None:
    database = tmp_path / "state.db"
    store: InMemoryStateStore | SqliteStateStore = (
        InMemoryStateStore() if store_kind == "memory" else SqliteStateStore(database)
    )
    candidate, revisions, binding = _state_fixture(tmp_path, store)

    intent = store.begin_task_promotion(
        run_id=candidate.run_id,
        candidate_id=candidate.candidate_id,
        applicable_revision_digests=revisions,
        expected_sequence=store.audit_sequence(candidate.run_id),
        ref_effect_binding=binding,
        permit_generation=2,
    )
    assert store.task_candidate(candidate.candidate_id).state == "PROMOTING"
    assert store.task_promotion_intent(candidate.candidate_id) == intent
    if isinstance(store, SqliteStateStore):
        store.close()
        store = SqliteStateStore(database)
        assert store.task_candidate(candidate.candidate_id).state == "PROMOTING"
        assert store.task_promotion_intent(candidate.candidate_id) == intent

    sequence = store.settle_task_promotion(
        run_id=candidate.run_id,
        candidate_id=candidate.candidate_id,
        intent=intent,
        outcome=PrivateRefCasOutcome(
            intent_id=intent.intent_id,
            run_id=candidate.run_id,
            result_class="PRIVATE_REF_PROMOTED",
            observed_oid=candidate.prepared_oid,
        ),
        applicable_revision_digests=revisions,
        expected_sequence=store.audit_sequence(candidate.run_id),
    )

    assert sequence == store.audit_sequence(candidate.run_id)
    assert store.task_candidate(candidate.candidate_id).state == "PROMOTED"
    assert store.run_record(candidate.run_id).run_head_oid == candidate.prepared_oid
    assert store.run_ref(candidate.run_id, "PRIVATE").current_oid == candidate.prepared_oid


@pytest.mark.parametrize("store_kind", ["memory", "sqlite"])
def test_known_private_cas_failure_returns_candidate_to_ready_and_pauses_run(
    tmp_path: Path, store_kind: str
) -> None:
    database = tmp_path / "state.db"
    store: InMemoryStateStore | SqliteStateStore = (
        InMemoryStateStore() if store_kind == "memory" else SqliteStateStore(database)
    )
    candidate, revisions, binding = _state_fixture(tmp_path, store)
    intent = store.begin_task_promotion(
        run_id=candidate.run_id,
        candidate_id=candidate.candidate_id,
        applicable_revision_digests=revisions,
        expected_sequence=store.audit_sequence(candidate.run_id),
        ref_effect_binding=binding,
    )

    store.rollback_known_private_cas_failure(
        run_id=candidate.run_id,
        candidate_id=candidate.candidate_id,
        intent=intent,
        applicable_revision_digests=revisions,
        expected_sequence=store.audit_sequence(candidate.run_id),
    )

    assert store.task_candidate(candidate.candidate_id).state == "READY"
    assert store.run_record(candidate.run_id).state == RunState.PAUSED
    assert store.run_ref(candidate.run_id, "PRIVATE").current_oid == candidate.expected_run_head_oid


@pytest.mark.parametrize(
    ("result_class", "candidate_state", "run_state"),
    [
        ("PRIVATE_REF_CONFLICT", "CONFLICT", RunState.PAUSED),
        ("PRIVATE_REF_UNOBSERVABLE", "INDETERMINATE", RunState.INDETERMINATE),
    ],
)
def test_private_promotion_conflict_and_unobservable_are_not_success(
    tmp_path: Path,
    result_class: str,
    candidate_state: str,
    run_state: RunState,
) -> None:
    store = SqliteStateStore(tmp_path / "state.db")
    candidate, revisions, binding = _state_fixture(tmp_path, store)
    intent = store.begin_task_promotion(
        run_id=candidate.run_id,
        candidate_id=candidate.candidate_id,
        applicable_revision_digests=revisions,
        expected_sequence=store.audit_sequence(candidate.run_id),
        ref_effect_binding=binding,
    )

    store.settle_task_promotion(
        run_id=candidate.run_id,
        candidate_id=candidate.candidate_id,
        intent=intent,
        outcome=PrivateRefCasOutcome(
            intent_id=intent.intent_id,
            run_id=candidate.run_id,
            result_class=result_class,  # type: ignore[arg-type]
            observed_oid=GitOid("3" * 40) if result_class == "PRIVATE_REF_CONFLICT" else None,
        ),
        applicable_revision_digests=revisions,
        expected_sequence=store.audit_sequence(candidate.run_id),
    )

    assert store.task_candidate(candidate.candidate_id).state == candidate_state
    assert store.run_record(candidate.run_id).state == run_state
