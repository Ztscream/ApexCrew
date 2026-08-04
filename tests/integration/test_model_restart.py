import json
from dataclasses import replace
from decimal import Decimal
from hashlib import sha256
from pathlib import Path

import pytest

from apexcrew.adapters.model.scripted import ScriptedMockLLM, ScriptedModelStep
from apexcrew.adapters.state.sqlite import SqliteStateStore
from apexcrew.domain.commands import ApplicableRevisionDigests
from apexcrew.domain.effects import EffectIntent, StateConflict
from apexcrew.domain.model import (
    DurableModelClient,
    LogicalTurnId,
    ModelCompletion,
    ModelRecoveryBinding,
    ModelRequest,
    ModelRequestIntent,
    ModelUsage,
    ProviderAttemptResult,
    SettledModelAttempt,
)
from apexcrew.domain.types import AuditSequence, IntentId, RunId


def completion(
    model_id: str,
    action: dict[str, str],
    *,
    requested_model_id: str = "gpt-5.6-terra",
) -> ModelCompletion:
    return ModelCompletion(
        response_id="response-1",
        requested_model_id=requested_model_id,
        returned_model_id=model_id,
        usage=ModelUsage(120, 12, Decimal("0.00048")),
        normalized_action=action,
    )


def make_model_request() -> ModelRequest:
    return ModelRequest(
        run_id="run-1",
        plan_digest="sha256:" + "2" * 64,
        policy_digest="sha256:" + "3" * 64,
        budget_digest="sha256:" + "4" * 64,
        model_configuration_digest="sha256:" + "5" * 64,
        requested_model_id="gpt-5.6-terra",
        allowed_model_ids=frozenset({"gpt-5.6-terra"}),
        prompt=({"role": "user", "content": "finish"},),
        tool_schema_digest="sha256:" + "1" * 64,
        request_digest="sha256:" + "6" * 64,
        idempotency_key="request-1",
        max_input_tokens=1_000,
        max_output_tokens=200,
        reserved_cost_usd=Decimal("0.01"),
    )


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


def committed_model_turn(
    tmp_path: Path,
) -> tuple[SqliteStateStore, ModelRequest, LogicalTurnId]:
    store = SqliteStateStore(tmp_path / "state.db")
    request = make_model_request()
    result = DurableModelClient(
        model=ScriptedMockLLM(
            [
                ScriptedModelStep.for_request(
                    request,
                    ProviderAttemptResult.completed(
                        completion("gpt-5.6-terra", {"kind": "finish"})
                    ),
                )
            ]
        ),
        journal=store,
    ).complete(request)
    return store, request, result.logical_turn_id


class SimulatedProcessCrash(RuntimeError):
    pass


class CrashAfterModelSettlement:
    def __init__(self, inner: SqliteStateStore) -> None:
        self._inner = inner
        self.logical_turn_id: LogicalTurnId | None = None

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)

    def settle_model_attempt(
        self,
        intent: ModelRequestIntent,
        result: ProviderAttemptResult,
        expected_sequence: AuditSequence,
    ) -> SettledModelAttempt:
        settled = self._inner.settle_model_attempt(intent, result, expected_sequence)
        self.logical_turn_id = settled.logical_turn_id
        raise SimulatedProcessCrash from None


def test_restart_releases_committed_completion_without_provider_redispatch(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.db"
    store = SqliteStateStore(database)
    request = make_model_request()
    first_model = ScriptedMockLLM(
        [
            ScriptedModelStep.for_request(
                request,
                ProviderAttemptResult.completed(
                    completion(model_id="gpt-5.6-terra", action={"kind": "finish"})
                ),
            )
        ]
    )
    crashing = CrashAfterModelSettlement(store)
    with pytest.raises(SimulatedProcessCrash):
        DurableModelClient(model=first_model, journal=crashing).complete(request)
    assert crashing.logical_turn_id is not None
    logical_turn_id = crashing.logical_turn_id
    store.close()

    reopened = SqliteStateStore(database)
    empty_model = ScriptedMockLLM([])
    recovered = DurableModelClient(model=empty_model, journal=reopened).recover_committed(
        request.run_id,
        logical_turn_id,
        ModelRecoveryBinding.from_request(request),
    )
    assert recovered.outcome == "COMPLETED"
    assert recovered.normalized_action == {"kind": "finish"}
    assert len(reopened.model_attempts(request.run_id, logical_turn_id)) == 1
    assert empty_model.call_count == 0


def test_recovery_binding_mismatch_releases_no_output(tmp_path: Path) -> None:
    store, request, logical_turn_id = committed_model_turn(tmp_path)
    wrong = replace(
        ModelRecoveryBinding.from_request(request),
        tool_schema_digest="sha256:" + "f" * 64,
    )
    model = ScriptedMockLLM([])
    recovered = DurableModelClient(model=model, journal=store).recover_committed(
        request.run_id, logical_turn_id, wrong
    )
    assert recovered.outcome == "RECOVERY_BINDING_MISMATCH"
    assert recovered.normalized_action is None
    assert model.call_count == 0


def test_legacy_committed_completion_without_response_requested_id_recovers(
    tmp_path: Path,
) -> None:
    store, request, logical_turn_id = committed_model_turn(tmp_path)
    row = store._connection.execute(
        "SELECT dispatch_result_json FROM model_turns WHERE run_id = ? AND logical_turn_id = ?",
        (request.run_id, logical_turn_id),
    ).fetchone()
    assert row is not None
    legacy_dispatch = json.loads(row[0])
    del legacy_dispatch["response_requested_model_id"]
    legacy_dispatch_json = json.dumps(legacy_dispatch, sort_keys=True, separators=(",", ":"))
    store._connection.execute(
        "UPDATE model_turns SET response_requested_model_id = NULL, dispatch_result_json = ? "
        "WHERE run_id = ? AND logical_turn_id = ?",
        (legacy_dispatch_json, request.run_id, logical_turn_id),
    )
    store._connection.execute(
        "UPDATE model_attempts SET response_requested_model_id = NULL, result_json = ? "
        "WHERE run_id = ? AND logical_turn_id = ?",
        (legacy_dispatch_json, request.run_id, logical_turn_id),
    )
    store.close()

    reopened = SqliteStateStore(tmp_path / "state.db")
    committed = reopened.committed_model_turn(request.run_id, logical_turn_id)
    recovered = DurableModelClient(model=ScriptedMockLLM([]), journal=reopened).recover_committed(
        request.run_id,
        logical_turn_id,
        ModelRecoveryBinding.from_request(request),
    )

    assert committed is not None
    assert committed.response_requested_model_id is None
    assert recovered.outcome == "COMPLETED"
    assert recovered.normalized_action == {"kind": "finish"}


def test_new_committed_completion_cannot_be_reclassified_as_legacy(
    tmp_path: Path,
) -> None:
    store, request, logical_turn_id = committed_model_turn(tmp_path)
    row = store._connection.execute(
        "SELECT dispatch_result_json FROM model_turns WHERE run_id = ? AND logical_turn_id = ?",
        (request.run_id, logical_turn_id),
    ).fetchone()
    assert row is not None
    tampered_dispatch = json.loads(row[0])
    tampered_dispatch["response_requested_model_id"] = None
    store._connection.execute(
        "UPDATE model_turns SET response_requested_model_id = NULL, dispatch_result_json = ? "
        "WHERE run_id = ? AND logical_turn_id = ?",
        (
            json.dumps(tampered_dispatch, sort_keys=True, separators=(",", ":")),
            request.run_id,
            logical_turn_id,
        ),
    )

    with pytest.raises(StateConflict, match="COMMITTED_MODEL_RESPONSE_REQUESTED_ID"):
        store.committed_model_turn(request.run_id, logical_turn_id)


def test_restart_preserves_requested_model_mismatch_without_releasing_action(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.db"
    store = SqliteStateStore(database)
    request = make_model_request()
    result = DurableModelClient(
        model=ScriptedMockLLM(
            [
                ScriptedModelStep.for_request(
                    request,
                    ProviderAttemptResult.completed(
                        completion(
                            model_id="gpt-5.6-terra",
                            action={"kind": "finish"},
                            requested_model_id="gpt-5.6-mini",
                        )
                    ),
                )
            ]
        ),
        journal=store,
    ).complete(request)
    store.close()

    reopened = SqliteStateStore(database)
    attempts = reopened.model_attempts(request.run_id, result.logical_turn_id)
    empty_model = ScriptedMockLLM([])
    recovered = DurableModelClient(model=empty_model, journal=reopened).recover_committed(
        request.run_id,
        result.logical_turn_id,
        ModelRecoveryBinding.from_request(request),
    )

    assert result.outcome == "REQUESTED_MODEL_MISMATCH"
    assert result.normalized_action is None
    assert len(attempts) == 1
    assert attempts[0].request.requested_model_id == "gpt-5.6-terra"
    assert attempts[0].dispatch_result.response_requested_model_id == "gpt-5.6-mini"
    assert attempts[0].dispatch_result.returned_model_id == "gpt-5.6-terra"
    assert attempts[0].dispatch_result.outcome == "REQUESTED_MODEL_MISMATCH"
    assert attempts[0].reported_usage == ModelUsage(120, 12, Decimal("0.00048"))
    assert recovered.outcome == "MODEL_COMPLETION_NOT_COMMITTED"
    assert recovered.normalized_action is None
    assert empty_model.call_count == 0


def test_committed_completion_with_downstream_intent_is_not_released_twice(
    tmp_path: Path,
) -> None:
    store, request, logical_turn_id = committed_model_turn(tmp_path)
    downstream = make_test_effect_intent(
        run_id=request.run_id,
        intent_id="tool-intent-1",
        recorded_sequence=AuditSequence(store.audit_sequence(request.run_id) + 1),
    )
    store.record_downstream_action_intent(
        request.run_id,
        logical_turn_id,
        downstream,
        expected_sequence=store.audit_sequence(request.run_id),
    )
    recovered = DurableModelClient(model=ScriptedMockLLM([]), journal=store).recover_committed(
        request.run_id,
        logical_turn_id,
        ModelRecoveryBinding.from_request(request),
    )
    assert recovered.outcome == "DOWNSTREAM_INTENT_REQUIRES_RECONCILIATION"
    assert recovered.normalized_action is None
