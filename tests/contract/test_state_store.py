import sqlite3
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import Decimal
from hashlib import sha256
from pathlib import Path

import pytest

from apexcrew.adapters.state.memory import InMemoryStateStore
from apexcrew.adapters.state.sqlite import SqliteStateStore
from apexcrew.domain.commands import (
    ApplicableRevisionDigests,
    CommandEnvelope,
    CommandOutcome,
    PausePayload,
)
from apexcrew.domain.effects import (
    AuditEvent,
    EffectIntent,
    EffectJournal,
    EffectResult,
    RecoveryOutcome,
    RecoveryService,
    StateCommitFault,
    StateConflict,
    TargetReservation,
)
from apexcrew.domain.model import (
    LogicalModelTurn,
    ModelRequest,
    ModelRequestIntent,
    ProviderAttemptResult,
)
from apexcrew.domain.types import (
    AttemptId,
    AuditSequence,
    CommandStatus,
    GitOid,
    IntentId,
    RepositoryId,
    RevisionDigest,
    RunId,
    TaskId,
)


def make_model_request(allowed_model_ids: set[str]) -> ModelRequest:
    return ModelRequest(
        run_id="run-1",
        plan_digest=None,
        policy_digest="sha256:" + "3" * 64,
        budget_digest="sha256:" + "4" * 64,
        model_configuration_digest="sha256:" + "5" * 64,
        requested_model_id="gpt-5.6-terra",
        allowed_model_ids=frozenset(allowed_model_ids),
        prompt=({"role": "user", "content": "finish"},),
        tool_schema_digest="sha256:" + "1" * 64,
        request_digest="sha256:" + "2" * 64,
        idempotency_key="request-1",
        max_input_tokens=1_000,
        max_output_tokens=200,
        reserved_cost_usd=Decimal("0.01"),
    )


def memory_store_factory(tmp_path: Path) -> InMemoryStateStore:
    del tmp_path
    return InMemoryStateStore()


def sqlite_store_factory(tmp_path: Path) -> SqliteStateStore:
    return SqliteStateStore(tmp_path / "state.db")


def test_sqlite_model_reservation_survives_restart(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    request = make_model_request(allowed_model_ids={"gpt-5.6-terra"})
    first = SqliteStateStore(database)
    before = first.audit_sequence(request.run_id)
    intent = first.reserve_model_request(request, expected_sequence=before)
    assert first.audit_sequence(request.run_id) == before + 1
    first.close()
    reopened = SqliteStateStore(database)
    restored = reopened.model_request(request.run_id, intent.intent_id)
    assert restored.intent_id == intent.intent_id
    assert restored.logical_turn_id == intent.logical_turn_id
    assert restored.request.request_digest == request.request_digest
    assert restored.request.allowed_model_ids == frozenset({"gpt-5.6-terra"})
    assert reopened.reserved_call_count(request.run_id) == 1


def test_sqlite_worker_model_owner_survives_restart(tmp_path: Path) -> None:
    request = replace(
        make_model_request(allowed_model_ids={"gpt-5.6-terra"}),
        owner_kind="WORKER",
        task_id=TaskId("task-1"),
        attempt_id=AttemptId("attempt-1"),
        tranche_id="tranche-1",
    )
    database = tmp_path / "state.db"
    first = SqliteStateStore(database)
    intent = first.reserve_model_request(
        request, expected_sequence=first.audit_sequence(request.run_id)
    )
    first.close()
    reopened = SqliteStateStore(database)
    restored = reopened.model_request(request.run_id, intent.intent_id)
    assert restored.request == request
    assert restored.request.owner_kind == "WORKER"


def test_sqlite_model_backoff_survives_restart(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    store = SqliteStateStore(database)
    request = make_model_request(allowed_model_ids={"gpt-5.6-terra"})
    turn, intent = store.begin_model_turn_and_reserve(
        request, expected_sequence=store.audit_sequence(request.run_id)
    )
    store.settle_model_attempt(
        intent,
        ProviderAttemptResult.known_closed("reject-1", "TRANSIENT_REJECTION"),
        expected_sequence=store.audit_sequence(request.run_id),
    )
    store.record_model_backoff(
        request.run_id,
        intent.intent_id,
        seconds=1,
        expected_sequence=store.audit_sequence(request.run_id),
    )
    store.close()
    attempts = SqliteStateStore(database).model_attempts(request.run_id, turn.logical_turn_id)
    assert len(attempts) == 1
    assert attempts[0].outcome == "KNOWN_CLOSED_REJECTION"
    assert attempts[0].backoff_seconds == 1
    assert attempts[0].charged_amounts == attempts[0].reserved_amounts


@pytest.mark.parametrize("store_factory", [memory_store_factory, sqlite_store_factory])
def test_model_attempt_cannot_be_settled_twice(
    tmp_path: Path,
    store_factory: Callable[[Path], InMemoryStateStore | SqliteStateStore],
) -> None:
    store = store_factory(tmp_path)
    request = make_model_request(allowed_model_ids={"gpt-5.6-terra"})
    _, intent = store.begin_model_turn_and_reserve(
        request, expected_sequence=store.audit_sequence(request.run_id)
    )
    result = ProviderAttemptResult.known_closed("reject-1", "TRANSIENT_REJECTION")
    store.settle_model_attempt(
        intent, result, expected_sequence=store.audit_sequence(request.run_id)
    )
    before = store.audit_sequence(request.run_id)
    counters = store.model_counters(request.run_id)
    with pytest.raises(StateConflict, match="MODEL_ATTEMPT_ALREADY_SETTLED"):
        store.settle_model_attempt(intent, result, expected_sequence=before)
    assert store.audit_sequence(request.run_id) == before
    assert store.model_counters(request.run_id) == counters


@pytest.mark.parametrize("store_factory", [memory_store_factory, sqlite_store_factory])
def test_unjournaled_model_attempt_cannot_be_settled(
    tmp_path: Path,
    store_factory: Callable[[Path], InMemoryStateStore | SqliteStateStore],
) -> None:
    store = store_factory(tmp_path)
    request = make_model_request(allowed_model_ids={"gpt-5.6-terra"})
    turn = LogicalModelTurn.new(request)
    intent = ModelRequestIntent.reserve(turn, request)
    with pytest.raises(StateConflict, match="MODEL_ATTEMPT_BINDING_MISMATCH"):
        store.settle_model_attempt(
            intent,
            ProviderAttemptResult.known_closed("reject-1", "TRANSIENT_REJECTION"),
            expected_sequence=store.audit_sequence(request.run_id),
        )
    assert store.audit_sequence(request.run_id) == 0


@pytest.mark.parametrize("store_factory", [memory_store_factory, sqlite_store_factory])
def test_model_reservation_and_counters_roll_back_together(
    tmp_path: Path,
    store_factory: Callable[[Path], InMemoryStateStore | SqliteStateStore],
) -> None:
    store = store_factory(tmp_path)
    request = make_model_request(allowed_model_ids={"gpt-5.6-terra"})
    before = store.audit_sequence(request.run_id)
    store.fail_next_commit_after_state_write_for_test()
    with pytest.raises(StateCommitFault, match="TEST_FAULT_AFTER_STATE_WRITE"):
        store.begin_model_turn_and_reserve(request, expected_sequence=before)
    assert store.audit_sequence(request.run_id) == before
    assert store.reserved_call_count(request.run_id) == 0
    assert store.model_counters(request.run_id).calls == 0


def make_test_effect_intent(
    *, run_id: RunId, intent_id: str, recorded_sequence: AuditSequence
) -> EffectIntent:
    payload = '{"kind":"test"}'
    return EffectIntent(
        intent_id=IntentId(intent_id),
        run_id=run_id,
        kind="TEST_EFFECT",
        idempotency_key=f"test-effect:{run_id}:{intent_id}",
        applicable_revision_digests=ApplicableRevisionDigests(),
        payload_digest="sha256:" + sha256(payload.encode("utf-8")).hexdigest(),
        normalized_payload_json=payload,
        recorded_sequence=recorded_sequence,
    )


def make_test_effect_result(
    intent: EffectIntent, *, settled_sequence: AuditSequence
) -> EffectResult:
    payload = '{"result":"POST_STATE"}'
    return EffectResult(
        intent_id=intent.intent_id,
        run_id=intent.run_id,
        outcome="COMPLETED",
        result_class="POST_STATE",
        result_digest="sha256:" + sha256(payload.encode("utf-8")).hexdigest(),
        bounded_result_json=payload,
        settled_sequence=settled_sequence,
    )


def make_pause_command(
    *,
    request_id: str,
    expected_sequence: int,
    run_id: str = "run-command",
    reason: str = "operator",
) -> CommandEnvelope:
    return CommandEnvelope(
        request_id=request_id,
        expected_sequence=expected_sequence,
        payload=PausePayload(run_id=RunId(run_id), reason=reason),
    )


def accepted_outcome(*, sequence: int, run_id: str = "run-command") -> CommandOutcome:
    return CommandOutcome(
        status=CommandStatus.ACCEPTED,
        run_id=RunId(run_id),
        resulting_sequence=AuditSequence(sequence),
    )


def seed_command_run(
    store: InMemoryStateStore | SqliteStateStore,
    tmp_path: Path,
    run_id: str,
) -> None:
    store.create_draft_with_reservation(
        RunId(run_id),
        RepositoryId(f"repository-{run_id}"),
        "sha256:" + "a" * 64,
        TargetReservation(
            reservation_id=f"reservation-{run_id}",
            run_id=RunId(run_id),
            target_ref="refs/heads/main",
            pinned_target_oid=GitOid("1" * 40),
            path=tmp_path / "data" / "reservations" / f"reservation-{run_id}",
            phase="ALLOCATED",
        ),
    )


@pytest.mark.parametrize("store_factory", [memory_store_factory, sqlite_store_factory])
def test_record_command_requires_existing_run_and_uses_real_repository_binding(
    tmp_path: Path,
    store_factory: Callable[[Path], InMemoryStateStore | SqliteStateStore],
) -> None:
    store = store_factory(tmp_path)
    command = make_pause_command(request_id="cmd-bound", expected_sequence=0)
    with pytest.raises(StateConflict, match="^RUN_NOT_FOUND$"):
        store.record_command(command, accepted_outcome(sequence=1))
    assert store.audit_sequence(RunId("run-command")) == 0

    seed_command_run(store, tmp_path, "run-command")
    bound_command = make_pause_command(request_id="cmd-bound", expected_sequence=1)
    outcome = store.record_command(bound_command, accepted_outcome(sequence=2))
    assert outcome.resulting_sequence == 2
    assert store.audit_sequence(RunId("run-command")) == 2


@pytest.mark.parametrize("store_factory", [memory_store_factory, sqlite_store_factory])
def test_identical_command_replay_returns_committed_outcome(
    tmp_path: Path,
    store_factory: Callable[[Path], InMemoryStateStore | SqliteStateStore],
) -> None:
    store = store_factory(tmp_path)
    run_id = RunId("run-command")
    seed_command_run(store, tmp_path, str(run_id))
    for expected_sequence in range(1, 4):
        store.append_event(
            run_id,
            AuditEvent.kind(f"PRIOR_EVENT_{expected_sequence}"),
            AuditSequence(expected_sequence),
        )
    envelope = make_pause_command(request_id="cmd-7", expected_sequence=4)
    first = store.record_command(envelope, accepted_outcome(sequence=5))
    replay = store.record_command(envelope, accepted_outcome(sequence=99))
    assert first.resulting_sequence == 5
    assert replay == first


@pytest.mark.parametrize("store_factory", [memory_store_factory, sqlite_store_factory])
def test_request_id_reuse_conflicts_without_appending_audit(
    tmp_path: Path,
    store_factory: Callable[[Path], InMemoryStateStore | SqliteStateStore],
) -> None:
    store = store_factory(tmp_path)
    seed_command_run(store, tmp_path, "run-command")
    seed_command_run(store, tmp_path, "run-other")
    original = make_pause_command(request_id="cmd-reused", expected_sequence=1)
    store.record_command(original, accepted_outcome(sequence=2))
    changed = make_pause_command(
        request_id="cmd-reused",
        expected_sequence=1,
        run_id="run-other",
        reason="different payload",
    )
    conflict = store.record_command(changed, accepted_outcome(sequence=2, run_id="run-other"))
    assert conflict.status == CommandStatus.CONFLICT
    assert conflict.failed_invariant == "IDEMPOTENCY_KEY_REUSE"
    assert conflict.resulting_sequence == 1
    assert store.audit_sequence(RunId("run-command")) == 2
    assert store.audit_sequence(RunId("run-other")) == 1


def test_sqlite_command_replay_survives_restart(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    first_store = SqliteStateStore(database)
    seed_command_run(first_store, tmp_path, "run-command")
    command = make_pause_command(request_id="cmd-restart", expected_sequence=1)
    first = first_store.record_command(command, accepted_outcome(sequence=2))
    first_store.close()
    reopened = SqliteStateStore(database)
    assert reopened.record_command(command, accepted_outcome(sequence=99)) == first
    assert reopened.audit_sequence(RunId("run-command")) == 2


@pytest.mark.parametrize("store_factory", [memory_store_factory, sqlite_store_factory])
def test_audit_sequence_is_contiguous_and_rejects_stale_writes(
    tmp_path: Path, store_factory: Callable[[Path], EffectJournal]
) -> None:
    store = store_factory(tmp_path)
    run_id = RunId("run-sequence")
    assert store.append_event(run_id, AuditEvent.kind("FIRST"), AuditSequence(0)) == 1
    assert store.append_event(run_id, AuditEvent.kind("SECOND"), AuditSequence(1)) == 2
    with pytest.raises(StateConflict, match="STALE_SEQUENCE"):
        store.append_event(run_id, AuditEvent.kind("STALE"), AuditSequence(0))
    assert store.audit_sequence(run_id) == 2


def test_sqlite_expected_sequence_serializes_two_connections(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    first = SqliteStateStore(database)
    second = SqliteStateStore(database)
    run_id = RunId("run-race")

    def append(store: SqliteStateStore, kind: str) -> AuditSequence | str:
        try:
            return store.append_event(run_id, AuditEvent.kind(kind), AuditSequence(0))
        except StateConflict as error:
            return str(error)

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = tuple(
            executor.map(
                lambda item: append(*item),
                ((first, "FIRST_WRITER"), (second, "SECOND_WRITER")),
            )
        )
    assert sorted(str(outcome) for outcome in outcomes) == ["1", "STALE_SEQUENCE"]
    assert first.audit_sequence(run_id) == 1
    assert second.audit_sequence(run_id) == 1


@pytest.mark.parametrize("store_factory", [memory_store_factory, sqlite_store_factory])
def test_empty_recovery_is_stable_before_runtime_exists(
    tmp_path: Path, store_factory: Callable[[Path], EffectJournal]
) -> None:
    store = store_factory(tmp_path)
    run_id = RunId("run-empty")
    assert store.unsettled_intents(run_id) == ()
    assert RecoveryService(store).reconcile(run_id) == RecoveryOutcome.empty()


def test_sqlite_effect_intent_and_result_survive_restart(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    first = SqliteStateStore(database)
    run_id = RunId("run-journal")
    intent = make_test_effect_intent(
        run_id=run_id,
        intent_id="intent-1",
        recorded_sequence=AuditSequence(first.audit_sequence(run_id) + 1),
    )
    first.record_intent(intent, expected_sequence=first.audit_sequence(run_id))
    result = make_test_effect_result(
        intent,
        settled_sequence=AuditSequence(first.audit_sequence(run_id) + 1),
    )
    first.settle_intent(
        run_id=intent.run_id,
        intent_id=intent.intent_id,
        result=result,
        applicable_revision_digests=intent.applicable_revision_digests,
        expected_sequence=first.audit_sequence(run_id),
    )
    sequence = first.audit_sequence(intent.run_id)
    first.close()
    reopened = SqliteStateStore(database)
    assert reopened.effect_intent(intent.intent_id) == intent
    assert reopened.effect_result(intent.intent_id) == result
    assert reopened.audit_sequence(intent.run_id) == sequence


def test_sqlite_round_trips_every_effect_owner_and_binding_field(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    store = SqliteStateStore(database)
    intent = replace(
        make_test_effect_intent(
            run_id=RunId("run-fields"),
            intent_id="intent-fields",
            recorded_sequence=AuditSequence(1),
        ),
        applicable_revision_digests=ApplicableRevisionDigests(
            plan_digest=RevisionDigest("sha256:" + "1" * 64),
            policy_digest=RevisionDigest("sha256:" + "2" * 64),
            budget_digest=RevisionDigest("sha256:" + "3" * 64),
            model_configuration_digest=RevisionDigest("sha256:" + "4" * 64),
        ),
        expected_prestate_json='{"oid":"abc123"}',
        task_id=TaskId("task-1"),
        attempt_id=AttemptId("attempt-1"),
        action_id="action-1",
    )
    store.record_intent(intent, AuditSequence(0))
    result = replace(
        make_test_effect_result(intent, settled_sequence=AuditSequence(2)),
        snapshot_digest="sha256:" + "5" * 64,
    )
    store.settle_intent(
        intent.run_id,
        intent.intent_id,
        result,
        intent.applicable_revision_digests,
        AuditSequence(1),
    )
    store.close()
    reopened = SqliteStateStore(database)
    assert reopened.effect_intent(intent.intent_id) == intent
    assert reopened.effect_result(intent.intent_id) == result


def test_sqlite_rejects_a_result_whose_typed_columns_do_not_match_json(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.db"
    store = SqliteStateStore(database)
    intent = make_test_effect_intent(
        run_id=RunId("run-result-binding"),
        intent_id="intent-result-binding",
        recorded_sequence=AuditSequence(1),
    )
    store.record_intent(intent, AuditSequence(0))
    result = make_test_effect_result(intent, settled_sequence=AuditSequence(2))
    store.settle_intent(
        intent.run_id,
        intent.intent_id,
        result,
        intent.applicable_revision_digests,
        AuditSequence(1),
    )
    store.close()
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE effect_results SET result_class = 'ALTERED' WHERE intent_id = ?",
            (intent.intent_id,),
        )
    reopened = SqliteStateStore(database)
    with pytest.raises(StateConflict, match="EFFECT_RESULT_STORAGE_BINDING_MISMATCH"):
        reopened.effect_result(intent.intent_id)


@pytest.mark.parametrize("store_factory", [memory_store_factory, sqlite_store_factory])
def test_state_and_audit_roll_back_together(
    tmp_path: Path, store_factory: Callable[[Path], EffectJournal]
) -> None:
    store = store_factory(tmp_path)
    run_id = RunId("run-rollback")
    before = store.audit_sequence(run_id)
    intent = make_test_effect_intent(
        run_id=run_id,
        intent_id="intent-2",
        recorded_sequence=AuditSequence(before + 1),
    )
    store.fail_next_commit_after_state_write_for_test()
    with pytest.raises(StateCommitFault, match="TEST_FAULT_AFTER_STATE_WRITE"):
        store.record_intent(intent, expected_sequence=before)
    assert store.effect_intent_or_none(intent.intent_id) is None
    assert store.audit_sequence(intent.run_id) == before


@pytest.mark.parametrize("store_factory", [memory_store_factory, sqlite_store_factory])
def test_invalid_intent_digest_rolls_back_state_and_audit(
    tmp_path: Path, store_factory: Callable[[Path], EffectJournal]
) -> None:
    store = store_factory(tmp_path)
    intent = replace(
        make_test_effect_intent(
            run_id=RunId("run-bad-digest"),
            intent_id="intent-bad-digest",
            recorded_sequence=AuditSequence(1),
        ),
        payload_digest="sha256:" + "0" * 64,
    )
    with pytest.raises(StateConflict, match="EFFECT_INTENT_PAYLOAD_DIGEST_MISMATCH"):
        store.record_intent(intent, AuditSequence(0))
    assert store.effect_intent_or_none(intent.intent_id) is None
    assert store.audit_sequence(intent.run_id) == 0


@pytest.mark.parametrize("store_factory", [memory_store_factory, sqlite_store_factory])
def test_malformed_intent_payload_has_the_same_closed_error(
    tmp_path: Path, store_factory: Callable[[Path], EffectJournal]
) -> None:
    store = store_factory(tmp_path)
    malformed_payload = "{"
    intent = replace(
        make_test_effect_intent(
            run_id=RunId("run-malformed"),
            intent_id="intent-malformed",
            recorded_sequence=AuditSequence(1),
        ),
        normalized_payload_json=malformed_payload,
        payload_digest="sha256:" + sha256(malformed_payload.encode("utf-8")).hexdigest(),
    )
    with pytest.raises(StateConflict, match="^EFFECT_INTENT_PAYLOAD_NOT_CANONICAL$"):
        store.record_intent(intent, AuditSequence(0))
    assert store.effect_intent_or_none(intent.intent_id) is None
    assert store.audit_sequence(intent.run_id) == 0


@pytest.mark.parametrize("store_factory", [memory_store_factory, sqlite_store_factory])
def test_revision_mismatch_cannot_settle_an_intent(
    tmp_path: Path, store_factory: Callable[[Path], EffectJournal]
) -> None:
    store = store_factory(tmp_path)
    intent = make_test_effect_intent(
        run_id=RunId("run-revision"),
        intent_id="intent-revision",
        recorded_sequence=AuditSequence(1),
    )
    store.record_intent(intent, AuditSequence(0))
    result = make_test_effect_result(intent, settled_sequence=AuditSequence(2))
    wrong_revisions = ApplicableRevisionDigests(policy_digest=RevisionDigest("sha256:" + "7" * 64))
    with pytest.raises(StateConflict, match="EFFECT_RESULT_REVISION_BINDING_MISMATCH"):
        store.settle_intent(
            intent.run_id,
            intent.intent_id,
            result,
            wrong_revisions,
            AuditSequence(1),
        )
    assert store.unsettled_intents(intent.run_id) == (intent,)
    assert store.audit_sequence(intent.run_id) == 1


@pytest.mark.parametrize("store_factory", [memory_store_factory, sqlite_store_factory])
def test_duplicate_idempotency_key_cannot_create_a_second_intent(
    tmp_path: Path, store_factory: Callable[[Path], EffectJournal]
) -> None:
    store = store_factory(tmp_path)
    run_id = RunId("run-duplicate")
    first = make_test_effect_intent(
        run_id=run_id,
        intent_id="intent-first",
        recorded_sequence=AuditSequence(1),
    )
    store.record_intent(first, AuditSequence(0))
    duplicate = replace(
        make_test_effect_intent(
            run_id=run_id,
            intent_id="intent-second",
            recorded_sequence=AuditSequence(2),
        ),
        idempotency_key=first.idempotency_key,
    )
    with pytest.raises(StateConflict, match="EFFECT_INTENT_DUPLICATE"):
        store.record_intent(duplicate, AuditSequence(1))
    assert store.unsettled_intents(run_id) == (first,)
    assert store.audit_sequence(run_id) == 1


@pytest.mark.parametrize("store_factory", [memory_store_factory, sqlite_store_factory])
def test_result_digest_and_run_bindings_are_checked_before_settlement(
    tmp_path: Path, store_factory: Callable[[Path], EffectJournal]
) -> None:
    store = store_factory(tmp_path)
    intent = make_test_effect_intent(
        run_id=RunId("run-result-validation"),
        intent_id="intent-result-validation",
        recorded_sequence=AuditSequence(1),
    )
    store.record_intent(intent, AuditSequence(0))
    result = make_test_effect_result(intent, settled_sequence=AuditSequence(2))
    with pytest.raises(StateConflict, match="EFFECT_RESULT_DIGEST_MISMATCH"):
        store.settle_intent(
            intent.run_id,
            intent.intent_id,
            replace(result, result_digest="sha256:" + "0" * 64),
            intent.applicable_revision_digests,
            AuditSequence(1),
        )
    with pytest.raises(StateConflict, match="EFFECT_RESULT_RUN_OR_INTENT_MISMATCH"):
        store.settle_intent(
            intent.run_id,
            intent.intent_id,
            replace(result, run_id=RunId("run-other")),
            intent.applicable_revision_digests,
            AuditSequence(1),
        )
    assert store.unsettled_intents(intent.run_id) == (intent,)
    assert store.audit_sequence(intent.run_id) == 1


@pytest.mark.parametrize("store_factory", [memory_store_factory, sqlite_store_factory])
def test_unsettled_intents_have_identical_deterministic_order(
    tmp_path: Path, store_factory: Callable[[Path], EffectJournal]
) -> None:
    store = store_factory(tmp_path)
    run_id = RunId("run-order")
    first = make_test_effect_intent(
        run_id=run_id,
        intent_id="intent-z",
        recorded_sequence=AuditSequence(1),
    )
    second = make_test_effect_intent(
        run_id=run_id,
        intent_id="intent-a",
        recorded_sequence=AuditSequence(2),
    )
    store.record_intent(first, AuditSequence(0))
    store.record_intent(second, AuditSequence(1))
    assert store.unsettled_intents(run_id) == (first, second)
