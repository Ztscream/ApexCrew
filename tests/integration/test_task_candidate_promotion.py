from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
from dataclasses import dataclass, replace
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
    private_ref,
)
from apexcrew.domain.commands import ApplicableRevisionDigests
from apexcrew.domain.effects import (
    AuditEvent,
    ReservationObservation,
    RunRefRecord,
    TargetReservation,
)
from apexcrew.domain.revisions import Sha256DigestText
from apexcrew.domain.types import (
    AttemptId,
    CandidateId,
    GitOid,
    RepositoryId,
    RunId,
    RunState,
    TaskId,
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


def _state_fixture(
    tmp_path: Path, store: InMemoryStateStore | SqliteStateStore
) -> tuple[TaskCandidate, ApplicableRevisionDigests, RefEffectBinding]:
    run_id = RunId("run-01")
    head = GitOid("1" * 40)
    prepared = GitOid("2" * 40)
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
                copied._runs[run_id], state=RunState.ACTIVE, run_head_oid=head
            )
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
    candidate = TaskCandidate.create(
        run_id=run_id,
        task_id=TaskId("task-01"),
        attempt_id=AttemptId("attempt-01"),
        expected_run_head_oid=head,
        prepared_oid=prepared,
        changed_paths=("src/task.py",),
    )
    store.persist_task_candidate(candidate, store.audit_sequence(run_id))
    binding = _promotion_ref_binding()
    return candidate, ApplicableRevisionDigests(), binding


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
