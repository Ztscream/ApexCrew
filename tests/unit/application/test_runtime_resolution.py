from __future__ import annotations

from hashlib import sha256

import pytest

from apexcrew.application.runtime import ResolutionRuntime, _stop_reason_for_decision
from apexcrew.domain.commands import ApplicableRevisionDigests, RuntimeDecision, RuntimePermit
from apexcrew.domain.effects import (
    ApplyResolutionRequest,
    EffectIntent,
    RecoveryActionClass,
    RecoveryObservation,
    StateConflict,
    observation_set_digest,
)
from apexcrew.domain.indeterminate import (
    ResolutionApplication,
    ResolutionSelection,
    UnresolvedIntentBinding,
    UnresolvedIntentSet,
)
from apexcrew.domain.revisions import Sha256DigestText
from apexcrew.domain.types import AuditSequence, IntentId, RequestId, RunId, RuntimeOwnerId

RUN_ID = RunId("runtime-resolution-run")
OWNER_ID = RuntimeOwnerId("runtime-resolution-owner")
PAYLOAD_DIGEST = Sha256DigestText("sha256:" + "1" * 64)


def _intent(intent_id: str = "intent-1") -> EffectIntent:
    payload = '{"kind":"search"}'
    return EffectIntent(
        intent_id=IntentId(intent_id),
        run_id=RUN_ID,
        kind="search",
        idempotency_key=f"search:{intent_id}",
        applicable_revision_digests=ApplicableRevisionDigests(),
        payload_digest=Sha256DigestText("sha256:" + sha256(payload.encode()).hexdigest()),
        normalized_payload_json=payload,
        recorded_sequence=AuditSequence(1),
    )


def _set(intent: EffectIntent) -> UnresolvedIntentSet:
    return UnresolvedIntentSet.from_members(
        (
            UnresolvedIntentBinding(
                intent_id=str(intent.intent_id),
                recovery_generation=1,
                intent_digest=intent.payload_digest,
            ),
        )
    )


def _selection(strategy: str, unresolved: UnresolvedIntentSet) -> ResolutionSelection:
    return ResolutionSelection(
        resolution=strategy,  # type: ignore[arg-type]
        unresolved_set_digest=unresolved.set_digest,
        intent_id=IntentId("intent-1"),
        recovery_generation=1,
    )


def _permit(selection: ResolutionSelection, *, state: str = "CONSUMED") -> RuntimePermit:
    return RuntimePermit(
        run_id=RUN_ID,
        generation=1,
        source_request_id=RequestId("resolve-request"),
        source_envelope_digest=Sha256DigestText("sha256:" + "2" * 64),
        issued_sequence=AuditSequence(1),
        allowed_phase="INDETERMINATE",
        applicable_revision_digests=ApplicableRevisionDigests(),
        target_authority_digest=Sha256DigestText("sha256:" + "3" * 64),
        expected_runtime_progress_generation=1,
        state=state,  # type: ignore[arg-type]
        consumed_owner_id=OWNER_ID if state == "CONSUMED" else None,
        consumed_sequence=AuditSequence(2) if state == "CONSUMED" else None,
        resolution_selection=selection,
    )


def _observation(intent: EffectIntent, state: str, sequence: int = 2) -> RecoveryObservation:
    values: dict[str, object] = {
        "kind": RecoveryActionClass.READ_SEARCH,
        "intent_id": intent.intent_id,
        "recovery_generation": 1,
        "source_payload_digest": intent.payload_digest,
        "state": state,
        "idempotency_key": intent.idempotency_key,
        "snapshot_digest": PAYLOAD_DIGEST,
        "scope_digest": PAYLOAD_DIGEST,
        "ordering_digest": PAYLOAD_DIGEST,
    }
    if state == "EXACT_SNAPSHOT":
        result = '{"items":[]}'
        values.update(
            {
                "run_id": RUN_ID,
                "settled_sequence": AuditSequence(sequence),
                "applicable_revision_digests": ApplicableRevisionDigests(),
                "bounded_result_json": result,
                "bounded_result_digest": Sha256DigestText(
                    "sha256:" + sha256(result.encode()).hexdigest()
                ),
            }
        )
    return RecoveryObservation.create(**values)


class _Journal:
    def __init__(self, intent: EffectIntent, observation: RecoveryObservation) -> None:
        self.intent = intent
        self.observation = observation
        self.applied: ApplyResolutionRequest | None = None

    def audit_sequence(self, run_id: RunId) -> AuditSequence:
        assert run_id == RUN_ID
        return AuditSequence(1)

    def unresolved_intent_set(self, run_id: RunId) -> UnresolvedIntentSet:
        assert run_id == RUN_ID
        return _set(self.intent)

    def effect_intent(self, intent_id: IntentId) -> EffectIntent:
        assert intent_id == self.intent.intent_id
        return self.intent

    def apply_indeterminate_resolution(
        self, request: ApplyResolutionRequest
    ) -> ResolutionApplication:
        self.applied = request
        return ResolutionApplication(
            status="ABANDONED",
            resulting_sequence=AuditSequence(2),
            remaining_set_digest=None,
            successor="PAUSED/READ_ABANDONED",
        )


class _Observer:
    def __init__(self, observation: RecoveryObservation) -> None:
        self.observation = observation
        self.observed: list[EffectIntent] = []

    def observe(self, intent: EffectIntent, recovery_generation: int) -> RecoveryObservation:
        assert recovery_generation == self.observation.recovery_generation
        self.observed.append(intent)
        return self.observation


def test_resolution_runtime_requires_consumed_subject_bound_permit() -> None:
    intent = _intent()
    unresolved = _set(intent)
    journal = _Journal(intent, _observation(intent, "STALE"))
    observer = _Observer(journal.observation)
    runtime = ResolutionRuntime(journal, observer)

    with pytest.raises(ValueError, match="PERMIT_PHASE_MISMATCH"):
        runtime.resume(
            RUN_ID, _permit(_selection("ABANDON_INTENT", unresolved), state="UNCONSUMED")
        )
    assert observer.observed == []


def test_reconcile_observed_uses_internal_observation_not_command_data() -> None:
    intent = _intent()
    unresolved = _set(intent)
    observation = _observation(intent, "EXACT_SNAPSHOT")
    journal = _Journal(intent, observation)
    observer = _Observer(observation)
    runtime = ResolutionRuntime(journal, observer)
    selection = _selection("RECONCILE_OBSERVED", unresolved)

    decision = runtime.resume(RUN_ID, _permit(selection))

    assert isinstance(decision, RuntimeDecision)
    assert decision.stop_reason == "PAUSED/READ_ABANDONED"
    assert observer.observed == [intent]
    assert journal.applied is not None
    assert journal.applied.observations == (observation,)
    assert journal.applied.observation_set_digest == observation_set_digest((observation,))


def test_retry_same_intent_requires_exact_prestate() -> None:
    intent = _intent()
    unresolved = _set(intent)
    journal = _Journal(intent, _observation(intent, "STALE"))
    runtime = ResolutionRuntime(journal, _Observer(journal.observation))

    with pytest.raises(StateConflict, match="RETRY_RECOVERY_PROOF_REQUIRED"):
        runtime.resume(RUN_ID, _permit(_selection("RETRY_SAME_INTENT", unresolved)))
    assert journal.applied is None


def test_abandon_returns_class_specific_successor() -> None:
    intent = _intent()
    unresolved = _set(intent)
    observation = _observation(intent, "STALE")
    journal = _Journal(intent, observation)
    runtime = ResolutionRuntime(journal, _Observer(observation))

    decision = runtime.resume(RUN_ID, _permit(_selection("ABANDON_INTENT", unresolved)))

    assert decision.stop_reason == "PAUSED/READ_ABANDONED"


def test_ready_for_approval_successor_has_public_final_approval_stop() -> None:
    assert _stop_reason_for_decision("READY_FOR_APPROVAL").value == "AWAITING_FINAL_APPROVAL"


def test_set_resolution_rejects_unabandonable_observation_before_store_apply() -> None:
    intent = _intent()
    unresolved = _set(intent)
    selection = ResolutionSelection(
        resolution="FAIL_RUN",
        unresolved_set_digest=unresolved.set_digest,
    )

    with pytest.raises(StateConflict, match="SET_RESOLUTION_OBSERVATION_NOT_ABANDONABLE"):
        ResolutionRuntime._validate_member_strategy(
            selection,
            (_observation(intent, "UNAVAILABLE"),),
            expected_sequence=AuditSequence(1),
        )
