import os
import shutil
import subprocess
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from helpers.application import make_permitted_draft_runtime

from apexcrew.adapters.repository.git import (
    GitCommandRunner,
    GitRepositoryPreflight,
    GitTargetReservationRepository,
    NoFollowTargetReservationWorktreeGuard,
    RepositoryInstance,
    reservation_for_operation,
)
from apexcrew.adapters.repository.no_follow import StableHandleTree
from apexcrew.adapters.repository.no_follow_posix import PosixNoFollowBackend
from apexcrew.adapters.repository.no_follow_windows import WindowsNoFollowBackend
from apexcrew.adapters.state.memory import InMemoryStateStore
from apexcrew.adapters.state.sqlite import SqliteStateStore
from apexcrew.adapters.system import ReservationPathInspector
from apexcrew.application.runtime import RuntimeStateStore
from apexcrew.domain.admission import (
    RepositoryEffectError,
    TargetReservationAdmissionService,
    TargetReservationCreationIntent,
    TargetReservationCreationOutcome,
    TargetReservationOperation,
    TargetReservationOperationResult,
)
from apexcrew.domain.commands import ApplicableRevisionDigests, RuntimeDecision, RuntimePermit
from apexcrew.domain.effects import ReservationObservation, TargetReservation
from apexcrew.domain.model import RecoveredModelAction
from apexcrew.domain.types import GitOid, IntentId, RepositoryId, RunId, RunState, RunStopReason


@dataclass
class RecordingReservationGit:
    calls: list[str] = field(default_factory=list)
    crash_after_add: bool = False

    def apply(self, operation: TargetReservationOperation) -> TargetReservationOperationResult:
        self.calls.append(operation.kind)
        if operation.kind == "ADD_NO_CHECKOUT" and self.crash_after_add:
            raise InjectedCrash("after durable add")
        return operation.applied()


@dataclass
class ScriptedReservationObserver:
    observations: deque[ReservationObservation]

    def observe(self, reservation: TargetReservation) -> ReservationObservation:
        del reservation
        return self.observations.popleft()


class InjectedCrash(RuntimeError):
    pass


def reservation(tmp_path: Path) -> TargetReservation:
    return TargetReservation(
        reservation_id="reservation-1",
        run_id=RunId("run-1"),
        target_ref="refs/heads/main",
        pinned_target_oid=GitOid("1" * 40),
        path=tmp_path / "data" / "reservations" / "reservation-1",
        phase="ALLOCATED",
    )


def owned_registration(locked: bool) -> ReservationObservation:
    return ReservationObservation(
        True,
        True,
        locked,
        True,
        True,
        admin_entry_name="reservation-1",
        admin_binding_digest="sha256:" + "b" * 64,
    )


def make_creation_intent(allocated: TargetReservation) -> TargetReservationCreationIntent:
    return TargetReservationCreationIntent(
        intent_id=IntentId("intent-reservation-1"),
        run_id=allocated.run_id,
        reservation_id=allocated.reservation_id,
        repository_id=RepositoryId("repository-1"),
        target_ref=allocated.target_ref,
        pinned_target_oid=allocated.pinned_target_oid,
        reservation_path=str(allocated.path),
        repository_instance_digest="sha256:" + "a" * 64,
        applicable_revision_digests=ApplicableRevisionDigests(),
        target_authority_digest="sha256:" + "c" * 64,
        idempotency_key=(
            f"target-reservation-create:{allocated.run_id}:{allocated.reservation_id}"
        ),
    )


def seeded_reservation_store(database: Path, allocated: TargetReservation) -> SqliteStateStore:
    store = SqliteStateStore(database)
    store.create_draft_with_reservation(
        allocated.run_id,
        RepositoryId("repository-1"),
        "sha256:" + "a" * 64,
        allocated,
    )
    return store


def real_git_reservation_adapter(
    tmp_path: Path,
    *,
    pinned_target_oid: GitOid | None = None,
    target_ref: str = "refs/heads/main",
) -> tuple[
    GitTargetReservationRepository,
    TargetReservationOperation,
    StableHandleTree,
    GitOid,
    RepositoryInstance,
]:
    git_executable_text = shutil.which("git")
    assert git_executable_text is not None
    git_executable = Path(git_executable_text)
    repository_root = tmp_path / "repository"
    subprocess.run(
        (str(git_executable), "init", "--quiet", "--initial-branch=main", str(repository_root)),
        check=True,
    )
    subprocess.run(
        (
            str(git_executable),
            "-C",
            str(repository_root),
            "-c",
            "user.name=ApexCrew Test",
            "-c",
            "user.email=apexcrew@example.invalid",
            "commit",
            "--quiet",
            "--allow-empty",
            "-m",
            "base",
        ),
        check=True,
    )
    base_oid = GitOid(
        subprocess.run(
            (str(git_executable), "-C", str(repository_root), "rev-parse", "HEAD"),
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    )
    if target_ref != "refs/heads/main":
        subprocess.run(
            (
                str(git_executable),
                "-C",
                str(repository_root),
                "branch",
                target_ref.removeprefix("refs/heads/"),
            ),
            check=True,
        )
    subprocess.run(
        (
            str(git_executable),
            "-C",
            str(repository_root),
            "switch",
            "--quiet",
            "--detach",
        ),
        check=True,
    )
    repository = GitRepositoryPreflight().inspect(repository_root)
    data_root = tmp_path / "data"
    (data_root / "reservations").mkdir(parents=True)
    data_backend = WindowsNoFollowBackend() if os.name == "nt" else PosixNoFollowBackend()
    data_handles = StableHandleTree(data_root, data_backend)
    trusted_empty_dir = tmp_path / "trusted-empty"
    trusted_empty_dir.mkdir()
    repository_id = RepositoryId("repository-1")
    repository_instance_digest = "sha256:" + "a" * 64
    target_authority_digest = "sha256:" + "c" * 64
    guard = NoFollowTargetReservationWorktreeGuard(
        repository, data_root, data_handles, repository_root
    )
    adapter = GitTargetReservationRepository(
        repository,
        repository_id,
        repository_instance_digest,
        GitCommandRunner(git_executable, trusted_empty_dir),
        guard,
        data_root,
        target_authority_digest,
    )
    operation = TargetReservationOperation(
        intent_id=IntentId("intent-reservation-1"),
        run_id=RunId("run-1"),
        reservation_id="reservation-1",
        kind="ADD_NO_CHECKOUT",
        repository_id=repository_id,
        repository_instance_digest=repository_instance_digest,
        target_ref=target_ref,
        pinned_target_oid=base_oid if pinned_target_oid is None else pinned_target_oid,
        reservation_path=str(data_root / "reservations" / "reservation-1"),
        target_authority_digest=target_authority_digest,
        lock_reason="run-1",
    )
    return adapter, operation, data_handles, base_oid, repository


def git_stdout(tmp_path: Path, *arguments: str) -> str:
    git_executable_text = shutil.which("git")
    assert git_executable_text is not None
    return subprocess.run(
        (git_executable_text, "-C", str(tmp_path / "repository"), *arguments),
        check=True,
        capture_output=True,
        encoding="utf-8",
        text=True,
    ).stdout


def test_real_git_adapter_reserves_symbolic_pinned_target_and_refreshes_guard(
    tmp_path: Path,
) -> None:
    adapter, add, data_handles, base_oid, repository = real_git_reservation_adapter(tmp_path)
    try:
        assert adapter.apply(add) == add.applied()
        lock = add.model_copy(update={"kind": "LOCK"})
        assert adapter.apply(lock) == lock.applied()
        admin_directory = tmp_path / "repository" / ".git" / "worktrees" / "reservation-1"
        admin_names = {entry.name for entry in admin_directory.iterdir()}
        # Expected names come from real Git 2.47.1.windows.1 output, not the plan's model.
        assert admin_names == {
            "HEAD",
            "commondir",
            "gitdir",
            "locked",
            "logs",
            "refs",
        }
        registration = git_stdout(tmp_path, "worktree", "list", "--porcelain")
        assert f"HEAD {base_oid}\n" in registration
        assert "branch refs/heads/main\n" in registration
        assert "locked run-1\n" in registration

        git_executable_text = shutil.which("git")
        assert git_executable_text is not None
        occupied = subprocess.run(
            (
                git_executable_text,
                "-C",
                str(tmp_path / "repository"),
                "worktree",
                "add",
                "--no-checkout",
                str(tmp_path / "other"),
                "main",
            ),
            check=False,
            capture_output=True,
            text=True,
        )
        assert occupied.returncode != 0
    finally:
        repository.close()
        data_handles.close()


def test_real_git_adapter_observes_exact_locked_registration_and_gitfile(
    tmp_path: Path,
) -> None:
    adapter, add, data_handles, _, repository = real_git_reservation_adapter(tmp_path)
    try:
        adapter.apply(add)
        adapter.apply(add.model_copy(update={"kind": "LOCK"}))
        allocated = reservation_for_operation(add)

        registration = adapter.observe_registration(allocated)
        assert registration.registration_present
        assert registration.locked
        assert registration.exact_identity
        assert not registration.unexpected_registration
        assert registration.observable

        backend = WindowsNoFollowBackend() if os.name == "nt" else PosixNoFollowBackend()
        paths = ReservationPathInspector(
            tmp_path / "data",
            lambda value: (
                b"gitdir: "
                + os.fsencode(
                    (repository.root / ".git" / "worktrees" / value.reservation_id).as_posix()
                )
                + b"\n"
            ),
            backend,
        )
        observed_path = paths.observe_path(allocated)
        assert observed_path.path_present
        assert observed_path.gitfile_only
        assert observed_path.exact_back_reference
        assert observed_path.observable
    finally:
        repository.close()
        data_handles.close()


def test_real_git_adapter_rejects_wrong_pinned_target_before_add(tmp_path: Path) -> None:
    adapter, add, data_handles, _, repository = real_git_reservation_adapter(
        tmp_path, pinned_target_oid=GitOid("2" * 40)
    )
    try:
        with pytest.raises(
            RepositoryEffectError, match="^TARGET_RESERVATION_PINNED_TARGET_MISMATCH$"
        ):
            adapter.apply(add)
        assert data_handles.try_open("reservations/reservation-1", "directory") is None
    finally:
        repository.close()
        data_handles.close()


def test_real_git_adapter_reserves_unicode_direct_ref_without_untyped_failure(
    tmp_path: Path,
) -> None:
    target_ref = "refs/heads/réservation"
    adapter, add, data_handles, _, repository = real_git_reservation_adapter(
        tmp_path, target_ref=target_ref
    )
    try:
        assert adapter.apply(add) == add.applied()
        lock = add.model_copy(update={"kind": "LOCK"})
        assert adapter.apply(lock) == lock.applied()
        assert f"branch {target_ref}\n" in git_stdout(tmp_path, "worktree", "list", "--porcelain")
    finally:
        repository.close()
        data_handles.close()


def test_creation_intent_is_committed_before_add(tmp_path: Path) -> None:
    store = SqliteStateStore(tmp_path / "state.db")
    allocated = reservation(tmp_path)
    store.create_draft_with_reservation(
        RunId("run-1"),
        RepositoryId("repository-1"),
        "sha256:" + "a" * 64,
        allocated,
    )
    intent = store._record_or_load_target_reservation_creation_intent(
        RunId("run-1"), expected_sequence=store.audit_sequence(RunId("run-1"))
    )
    assert store.effect_intent(intent.intent_id) == intent.to_effect_intent(
        intent.recorded_sequence
    )
    assert store.target_reservation(allocated.reservation_id).phase == "CREATION_INTENT_RECORDED"
    admission = TargetReservationAdmissionService(
        ScriptedReservationObserver(
            deque(
                (
                    ReservationObservation(False, False, False, False, False),
                    owned_registration(False),
                    owned_registration(True),
                )
            )
        ),
        RecordingReservationGit(),
    )
    assert admission.execute_creation(intent).result_class == "REGISTERED_LOCKED"


@pytest.mark.parametrize(
    "post_add",
    [
        ReservationObservation(True, False, False, False, False),
        ReservationObservation(True, True, False, False, True),
    ],
    ids=("mixed", "third"),
)
def test_post_add_wrong_registration_never_locks(
    tmp_path: Path, post_add: ReservationObservation
) -> None:
    git = RecordingReservationGit()
    admission = TargetReservationAdmissionService(
        ScriptedReservationObserver(
            deque(
                (
                    ReservationObservation(False, False, False, False, False),
                    post_add,
                )
            )
        ),
        git,
    )
    outcome = admission.execute_creation(make_creation_intent(reservation(tmp_path)))
    assert outcome.result_class == "CONFLICT"
    assert git.calls == ["ADD_NO_CHECKOUT"]


def test_restart_after_add_reuses_one_intent_and_locks_once(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    first = seeded_reservation_store(database, reservation(tmp_path))
    intent = first._record_or_load_target_reservation_creation_intent(
        RunId("run-1"), expected_sequence=first.audit_sequence(RunId("run-1"))
    )
    crashing_git = RecordingReservationGit(crash_after_add=True)
    with pytest.raises(InjectedCrash, match="after durable add"):
        TargetReservationAdmissionService(
            ScriptedReservationObserver(
                deque((ReservationObservation(False, False, False, False, False),))
            ),
            crashing_git,
        ).execute_creation(intent)
    first.close()
    reopened = SqliteStateStore(database)
    restored = reopened.unsettled_target_reservation_creation(RunId("run-1"))
    assert restored == intent
    resuming_git = RecordingReservationGit()
    outcome = TargetReservationAdmissionService(
        ScriptedReservationObserver(deque((owned_registration(False), owned_registration(True)))),
        resuming_git,
    ).execute_creation(restored)
    reopened._settle_target_reservation_creation(
        restored,
        outcome,
        expected_sequence=reopened.audit_sequence(RunId("run-1")),
    )
    assert resuming_git.calls == ["LOCK"]
    assert reopened.target_reservation("reservation-1").phase == "REGISTERED_LOCKED"


def test_restart_after_lock_settles_without_duplicate_git(tmp_path: Path) -> None:
    store = seeded_reservation_store(tmp_path / "state.db", reservation(tmp_path))
    intent = store._record_or_load_target_reservation_creation_intent(
        RunId("run-1"), expected_sequence=store.audit_sequence(RunId("run-1"))
    )
    store.close()
    reopened = SqliteStateStore(tmp_path / "state.db")
    git = RecordingReservationGit()
    outcome = TargetReservationAdmissionService(
        ScriptedReservationObserver(deque((owned_registration(True),))), git
    ).execute_creation(reopened.unsettled_target_reservation_creation(RunId("run-1")))
    reopened._settle_target_reservation_creation(
        intent,
        outcome,
        expected_sequence=reopened.audit_sequence(RunId("run-1")),
    )
    assert git.calls == []


def test_unobservable_creation_result_is_durable_indeterminate(tmp_path: Path) -> None:
    store = seeded_reservation_store(tmp_path / "state.db", reservation(tmp_path))
    intent = store._record_or_load_target_reservation_creation_intent(
        RunId("run-1"), expected_sequence=store.audit_sequence(RunId("run-1"))
    )
    outcome = TargetReservationAdmissionService(
        ScriptedReservationObserver(
            deque(
                (
                    ReservationObservation(False, False, False, False, False),
                    ReservationObservation(False, False, False, False, False, observable=False),
                )
            )
        ),
        RecordingReservationGit(),
    ).execute_creation(intent)
    store._settle_target_reservation_creation(
        intent,
        outcome,
        expected_sequence=store.audit_sequence(RunId("run-1")),
    )
    assert store.run_record(RunId("run-1")).state == "INDETERMINATE"


def test_known_git_error_is_observed_and_settled_without_escaping(tmp_path: Path) -> None:
    class RejectingGit:
        def apply(self, operation: TargetReservationOperation) -> TargetReservationOperationResult:
            del operation
            raise RepositoryEffectError("TARGET_RESERVATION_ADD_NO_CHECKOUT_FAILED")

    store = seeded_reservation_store(tmp_path / "state.db", reservation(tmp_path))
    intent = store._record_or_load_target_reservation_creation_intent(
        RunId("run-1"), expected_sequence=store.audit_sequence(RunId("run-1"))
    )
    outcome = TargetReservationAdmissionService(
        ScriptedReservationObserver(
            deque(
                (
                    ReservationObservation(False, False, False, False, False),
                    ReservationObservation(False, False, False, False, False),
                )
            )
        ),
        RejectingGit(),
    ).execute_creation(intent)
    store._settle_target_reservation_creation(
        intent,
        outcome,
        expected_sequence=store.audit_sequence(RunId("run-1")),
    )
    assert outcome.result_class == "UNOBSERVABLE"
    assert store.run_record(RunId("run-1")).state == "INDETERMINATE"


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_conflict_settlement_allows_a_fresh_creation_intent(tmp_path: Path, backend: str) -> None:
    store = InMemoryStateStore() if backend == "memory" else SqliteStateStore(tmp_path / "state.db")
    allocated = reservation(tmp_path)
    store.create_draft_with_reservation(
        allocated.run_id,
        RepositoryId("repository-1"),
        "sha256:" + "a" * 64,
        allocated,
    )
    first = store._record_or_load_target_reservation_creation_intent(
        allocated.run_id, expected_sequence=store.audit_sequence(allocated.run_id)
    )
    conflict = TargetReservationCreationOutcome(
        intent_id=first.intent_id,
        run_id=first.run_id,
        result_class="CONFLICT",
        observed=ReservationObservation(True, False, False, False, False),
    )
    store._settle_target_reservation_creation(
        first,
        conflict,
        expected_sequence=store.audit_sequence(allocated.run_id),
    )

    second = store._record_or_load_target_reservation_creation_intent(
        allocated.run_id, expected_sequence=store.audit_sequence(allocated.run_id)
    )
    assert second.intent_id != first.intent_id
    assert second.idempotency_key != first.idempotency_key


@dataclass
class RecordingPlanningProvider:
    trace: list[str]
    call_count: int = 0

    def complete(self) -> None:
        self.trace.append("provider")
        self.call_count += 1


class RecordingPlanningCoordinator:
    def __init__(
        self,
        provider: RecordingPlanningProvider,
        state: RuntimeStateStore,
    ) -> None:
        self._provider = provider
        self._state = state

    def run_planning_turn(self, run_id: RunId) -> RuntimeDecision:
        self._provider.complete()
        return RuntimeDecision.pause(
            "TEST_AFTER_TARGET_RESERVATION", self._state.audit_sequence(run_id)
        )

    def resume_recovered_planning_action(
        self, run_id: RunId, permit: RuntimePermit, action: RecoveredModelAction
    ) -> RuntimeDecision:
        raise AssertionError("no recovered planning action is valid in DRAFT")

    def schedule(self, run_id: RunId) -> RuntimeDecision:
        raise AssertionError("DRAFT delivery must not schedule a Worker")


@dataclass
class TracingReservationGit:
    trace: list[str]

    def apply(self, operation: TargetReservationOperation) -> TargetReservationOperationResult:
        self.trace.append("add" if operation.kind == "ADD_NO_CHECKOUT" else "lock")
        return operation.applied()


@dataclass
class RuntimeScriptedReservationObserver:
    trace: list[str]
    observations: deque[ReservationObservation]

    def observe(self, reservation: TargetReservation) -> ReservationObservation:
        del reservation
        if len(self.observations) == 3:
            self.trace.append("intent")
        self.trace.append("observe")
        observed = self.observations.popleft()
        if not self.observations and "intent" in self.trace:
            self.trace.append("settle")
        return observed


def exact_reservation_observation(locked: bool) -> ReservationObservation:
    return ReservationObservation(
        True,
        True,
        locked,
        True,
        True,
        admin_entry_name="reservation-1",
        admin_binding_digest="sha256:" + "b" * 64,
    )


def test_draft_permit_locks_exact_reservation_before_planning_provider_call(
    tmp_path: Path,
) -> None:
    trace: list[str] = []
    provider = RecordingPlanningProvider(trace)
    app = make_permitted_draft_runtime(
        tmp_path,
        coordinator_factory=lambda store: RecordingPlanningCoordinator(provider, store),
        reservation_git=TracingReservationGit(trace),
        reservation_observer=RuntimeScriptedReservationObserver(
            trace,
            deque(
                (
                    ReservationObservation(False, False, False, False, False),
                    ReservationObservation(False, False, False, False, False),
                    exact_reservation_observation(False),
                    exact_reservation_observation(True),
                )
            ),
        ),
    )

    stop = app.runtime.run_until_blocked(app.run_id)

    assert stop.reason == RunStopReason.PAUSED
    assert trace == [
        "observe",
        "intent",
        "observe",
        "add",
        "observe",
        "lock",
        "observe",
        "settle",
        "provider",
    ]
    assert provider.call_count == 1
    assert app.store.run_record(app.run_id).state == RunState.PLANNING
    assert app.store.target_reservation_for_run(app.run_id).phase == "REGISTERED_LOCKED"


@pytest.mark.parametrize(
    "observation",
    (
        ReservationObservation(True, False, False, False, False),
        ReservationObservation(True, True, False, False, False),
    ),
    ids=("mixed", "third"),
)
def test_draft_reservation_mixed_or_third_state_pauses_before_provider_call(
    tmp_path: Path, observation: ReservationObservation
) -> None:
    trace: list[str] = []
    provider = RecordingPlanningProvider(trace)
    app = make_permitted_draft_runtime(
        tmp_path,
        coordinator_factory=lambda store: RecordingPlanningCoordinator(provider, store),
        reservation_git=TracingReservationGit(trace),
        reservation_observer=RuntimeScriptedReservationObserver(trace, deque((observation,))),
    )

    stop = app.runtime.run_until_blocked(app.run_id)

    assert stop.reason == RunStopReason.PAUSED
    assert trace == ["observe"]
    assert provider.call_count == 0
    assert app.store.run_record(app.run_id).state == RunState.DRAFT
