from __future__ import annotations

from hashlib import sha256
from pathlib import Path

from helpers.application import (
    FixtureRepositoryBootstrapAuthorityService,
    FixtureTargetAuthorityDigestService,
    create_draft_with_three_proposals,
    make_application,
    make_create_run_command,
    make_runtime_application,
)

from apexcrew.adapters.state.memory import InMemoryStateStore
from apexcrew.adapters.system import SystemMonotonicClock
from apexcrew.application.control import ControlCommandService, CrewControlService
from apexcrew.application.runtime import ResolutionRuntime
from apexcrew.domain.commands import (
    ApplicableRevisionDigests,
    CommandEnvelope,
    ResolveIndeterminatePayload,
)
from apexcrew.domain.effects import (
    ApplyResolutionRequest,
    EffectIntent,
    EffectResult,
    RecoveryActionClass,
    RecoveryObservation,
    StateConflict,
    observation_set_digest,
)
from apexcrew.domain.indeterminate import UnresolvedIntentSet
from apexcrew.domain.revisions import Sha256DigestText
from apexcrew.domain.types import AuditSequence, IntentId, RunId, RuntimeOwnerId


def _intent(run_id: RunId, intent_id: str, sequence: AuditSequence) -> EffectIntent:
    payload = '{"kind":"test"}'
    return EffectIntent(
        intent_id=IntentId(intent_id),
        run_id=run_id,
        kind="read",
        idempotency_key=f"test:{intent_id}",
        applicable_revision_digests=ApplicableRevisionDigests(),
        payload_digest="sha256:" + sha256(payload.encode()).hexdigest(),
        normalized_payload_json=payload,
        recorded_sequence=sequence,
    )


def _indeterminate_result(intent: EffectIntent, sequence: AuditSequence) -> EffectResult:
    payload = '{"state":"UNOBSERVABLE"}'
    return EffectResult(
        intent_id=intent.intent_id,
        run_id=intent.run_id,
        outcome="INDETERMINATE",
        result_class="UNOBSERVABLE",
        result_digest="sha256:" + sha256(payload.encode()).hexdigest(),
        bounded_result_json=payload,
        settled_sequence=sequence,
    )


def _abandon_observation(
    intent: EffectIntent, generation: int, run_id: RunId
) -> RecoveryObservation:
    return RecoveryObservation.create(
        kind=RecoveryActionClass.READ_SEARCH,
        intent_id=intent.intent_id,
        recovery_generation=generation,
        source_payload_digest=intent.payload_digest,
        state="STALE",
        run_id=run_id,
        idempotency_key=intent.idempotency_key,
        snapshot_digest="sha256:" + "a" * 64,
        scope_digest="sha256:" + "b" * 64,
        ordering_digest="sha256:" + "c" * 64,
    )


def _observed_completion(
    intent: EffectIntent, generation: int, run_id: RunId, settled_sequence: AuditSequence
) -> RecoveryObservation:
    bounded_result = '{"items":[]}'
    return RecoveryObservation.create(
        kind=RecoveryActionClass.READ_SEARCH,
        intent_id=intent.intent_id,
        recovery_generation=generation,
        source_payload_digest=intent.payload_digest,
        state="EXACT_SNAPSHOT",
        run_id=run_id,
        settled_sequence=settled_sequence,
        applicable_revision_digests=ApplicableRevisionDigests(),
        idempotency_key=intent.idempotency_key,
        snapshot_digest="sha256:" + "a" * 64,
        scope_digest="sha256:" + "b" * 64,
        ordering_digest="sha256:" + "c" * 64,
        bounded_result_json=bounded_result,
        bounded_result_digest="sha256:" + sha256(bounded_result.encode()).hexdigest(),
    )


def _bindings(
    unresolved: UnresolvedIntentSet,
) -> tuple[tuple[IntentId, ...], tuple[Sha256DigestText, ...], tuple[int, ...]]:
    return (
        tuple(IntentId(member.intent_id) for member in unresolved.member_bindings),
        tuple(member.intent_digest for member in unresolved.member_bindings),
        tuple(member.recovery_generation for member in unresolved.member_bindings),
    )


def _resolve_command(
    run_id: RunId,
    sequence: AuditSequence,
    digest: str,
    *,
    intent_id: str | None = None,
    recovery_generation: int | None = None,
    resolution: str = "ABANDON_INTENT",
    bindings: ApplicableRevisionDigests | None = None,
) -> CommandEnvelope:
    return CommandEnvelope(
        request_id=f"resolve-{sequence}-{resolution}-{intent_id or 'set'}-{digest[-8:]}",
        expected_sequence=sequence,
        applicable_revision_digests=ApplicableRevisionDigests() if bindings is None else bindings,
        payload=ResolveIndeterminatePayload(
            run_id=run_id,
            unresolved_set_digest=digest,
            resolution=resolution,  # type: ignore[arg-type]
            intent_id=intent_id,
            recovery_generation=recovery_generation,
        ),
    )


def test_control_persists_exact_resolution_subject_and_rejects_stale_bindings(
    tmp_path: Path,
) -> None:
    app = make_application(tmp_path, monotonic_clock=SystemMonotonicClock())
    run_id = create_draft_with_three_proposals(app)

    first = _intent(run_id, "indeterminate-1", AuditSequence(app.store.audit_sequence(run_id) + 1))
    app.store.record_intent(first, app.store.audit_sequence(run_id))
    first_result = _indeterminate_result(first, AuditSequence(app.store.audit_sequence(run_id) + 1))
    app.store.settle_intent(
        run_id,
        first.intent_id,
        first_result,
        ApplicableRevisionDigests(),
        app.store.audit_sequence(run_id),
    )
    second = _intent(run_id, "indeterminate-2", AuditSequence(app.store.audit_sequence(run_id) + 1))
    app.store.record_intent(second, app.store.audit_sequence(run_id))
    second_result = _indeterminate_result(
        second, AuditSequence(app.store.audit_sequence(run_id) + 1)
    )
    app.store.settle_intent(
        run_id,
        second.intent_id,
        second_result,
        ApplicableRevisionDigests(),
        app.store.audit_sequence(run_id),
    )

    unresolved = app.store.unresolved_intent_set(run_id)
    assert unresolved is not None
    before = app.store.audit_sequence(run_id)
    stale = app.control.handle(
        _resolve_command(
            run_id, before, "sha256:" + "0" * 64, intent_id="indeterminate-1", recovery_generation=1
        )
    )
    assert stale.status == "STALE"
    assert app.store.audit_sequence(run_id) == before
    assert app.store.unconsumed_permit_count(run_id) == 0

    member = unresolved.member_bindings[0]
    accepted = app.control.handle(
        _resolve_command(
            run_id,
            before,
            str(unresolved.set_digest),
            intent_id=member.intent_id,
            recovery_generation=member.recovery_generation,
            bindings=app.store.current_revision_digests(run_id),
        )
    )
    assert accepted.status == "ACCEPTED", accepted.model_dump()
    permit = app.store.unconsumed_permit(run_id)
    assert permit.allowed_phase == "INDETERMINATE"
    assert permit.resolution_selection is not None
    assert permit.resolution_selection.unresolved_set_digest == unresolved.set_digest
    assert permit.resolution_selection.intent_id == member.intent_id
    assert permit.resolution_selection.recovery_generation == member.recovery_generation

    consumed = app.store.consume_current_runtime_permit(
        run_id,
        RuntimeOwnerId("resolution-owner"),
        app.store.audit_sequence(run_id),
    )
    assert consumed is not None
    observation = _abandon_observation(
        first,
        member.recovery_generation,
        run_id,
    )
    applied = app.store.apply_indeterminate_resolution(
        ApplyResolutionRequest(
            run_id=run_id,
            selection=permit.resolution_selection,
            permit_generation=permit.generation,
            owner_id=RuntimeOwnerId("resolution-owner"),
            expected_sequence=app.store.audit_sequence(run_id),
            intent_ids=_bindings(unresolved)[0],
            payload_digests=_bindings(unresolved)[1],
            recovery_generations=_bindings(unresolved)[2],
            observations=(observation,),
            observation_set_digest=observation_set_digest((observation,)),
        )
    )
    assert applied.status == "ABANDONED"
    assert applied.remaining_set_digest is not None
    after = app.store.audit_sequence(run_id)
    remaining_member = app.store.unresolved_intent_set(run_id)
    assert remaining_member is not None
    current_member = remaining_member.member_bindings[0]
    stale_generation_selection = permit.resolution_selection.model_copy(
        update={
            "intent_id": current_member.intent_id,
            "unresolved_set_digest": remaining_member.set_digest,
            "recovery_generation": current_member.recovery_generation + 1,
        }
    )
    try:
        app.store.apply_indeterminate_resolution(
            ApplyResolutionRequest(
                run_id=run_id,
                selection=stale_generation_selection,
                permit_generation=permit.generation,
                owner_id=RuntimeOwnerId("resolution-owner"),
                expected_sequence=after,
                intent_ids=_bindings(remaining_member)[0],
                payload_digests=_bindings(remaining_member)[1],
                recovery_generations=_bindings(remaining_member)[2],
                observations=(_abandon_observation(second, 1, run_id),),
                observation_set_digest=observation_set_digest(
                    (_abandon_observation(second, 1, run_id),)
                ),
            )
        )
    except StateConflict as error:
        assert str(error) == "STALE_UNRESOLVED_MEMBER"
    else:
        raise AssertionError("stale resolution unexpectedly mutated state")
    assert app.store.audit_sequence(run_id) == after

    try:
        app.store.apply_indeterminate_resolution(
            ApplyResolutionRequest(
                run_id=run_id,
                selection=permit.resolution_selection,
                permit_generation=permit.generation,
                owner_id=RuntimeOwnerId("resolution-owner"),
                expected_sequence=after,
                intent_ids=_bindings(unresolved)[0],
                payload_digests=_bindings(unresolved)[1],
                recovery_generations=_bindings(unresolved)[2],
                observations=(),
                observation_set_digest=observation_set_digest(()),
            )
        )
    except StateConflict as error:
        assert str(error) == "STALE_UNRESOLVED_SET"
    else:
        raise AssertionError("stale set resolution unexpectedly mutated state")
    assert app.store.audit_sequence(run_id) == after

    remaining = app.store.unresolved_intent_set(run_id)
    assert remaining is not None
    app.close()
    reopened = type(app.store)(app.database)
    persisted = reopened.unresolved_intent_set(run_id)
    assert persisted == remaining
    assert (
        reopened.runtime_permit(run_id, permit.generation).resolution_selection
        == permit.resolution_selection
    )
    reopened.close()


def test_runtime_routes_indeterminate_permit_to_resolution_before_recovery(
    tmp_path: Path,
) -> None:
    app = make_runtime_application(tmp_path, monotonic_clock=SystemMonotonicClock())
    run_id = app.run_id
    before_intent = app.store.audit_sequence(run_id)
    intent = _intent(run_id, "runtime-resolution-route", AuditSequence(before_intent + 1))
    app.store.record_intent(intent, before_intent)
    before_result = app.store.audit_sequence(run_id)
    app.store.settle_intent(
        run_id,
        intent.intent_id,
        _indeterminate_result(intent, AuditSequence(before_result + 1)),
        ApplicableRevisionDigests(),
        before_result,
    )
    unresolved = app.store.unresolved_intent_set(run_id)
    assert unresolved is not None
    member = unresolved.member_bindings[0]
    accepted = app.control.handle(
        _resolve_command(
            run_id,
            app.store.audit_sequence(run_id),
            str(unresolved.set_digest),
            intent_id=member.intent_id,
            recovery_generation=member.recovery_generation,
            bindings=app.store.current_revision_digests(run_id),
        )
    )
    assert accepted.status == "ACCEPTED"

    class UnexpectedRecoveryCall:
        def reconcile(self, run_id: RunId) -> object:
            raise AssertionError(f"generic recovery intercepted {run_id}")

    class Observer:
        def __init__(self) -> None:
            self.observed: list[IntentId] = []

        def observe(self, observed_intent: EffectIntent, generation: int) -> RecoveryObservation:
            self.observed.append(observed_intent.intent_id)
            return _abandon_observation(observed_intent, generation, run_id)

    observer = Observer()
    app.runtime._recovery = UnexpectedRecoveryCall()  # type: ignore[assignment]
    app.runtime._phase_drivers._resolution = ResolutionRuntime(app.store, observer)  # type: ignore[attr-defined]

    stop = app.runtime.run_until_blocked(run_id)

    assert stop.reason == "PAUSED"
    assert observer.observed == [intent.intent_id]


def test_memory_resolution_matches_sqlite_member_cas() -> None:
    store = InMemoryStateStore(monotonic_clock=SystemMonotonicClock())
    created = store.create_bootstrap_run(
        make_create_run_command(request_id="memory-resolution-create"),
        FixtureRepositoryBootstrapAuthorityService(),
    )
    assert created.run_id is not None
    run_id = created.run_id
    first = _intent(run_id, "memory-indeterminate-1", AuditSequence(2))
    store.record_intent(first, AuditSequence(1))
    store.settle_intent(
        run_id,
        first.intent_id,
        _indeterminate_result(first, AuditSequence(3)),
        ApplicableRevisionDigests(),
        AuditSequence(2),
    )
    second = _intent(run_id, "memory-indeterminate-2", AuditSequence(4))
    store.record_intent(second, AuditSequence(3))
    store.settle_intent(
        run_id,
        second.intent_id,
        _indeterminate_result(second, AuditSequence(5)),
        ApplicableRevisionDigests(),
        AuditSequence(4),
    )
    control = CrewControlService(
        ControlCommandService(
            state=store,
            target_authority=FixtureTargetAuthorityDigestService(store),
            repository_authority=FixtureRepositoryBootstrapAuthorityService(),
        )
    )
    unresolved = store.unresolved_intent_set(run_id)
    assert unresolved is not None
    member = unresolved.member_bindings[0]
    accepted = control.handle(
        _resolve_command(
            run_id,
            AuditSequence(5),
            str(unresolved.set_digest),
            intent_id=member.intent_id,
            recovery_generation=member.recovery_generation,
            bindings=store.current_revision_digests(run_id),
        )
    )
    assert accepted.status == "ACCEPTED"
    permit = store.unconsumed_permit(run_id)
    consumed = store.consume_current_runtime_permit(
        run_id, RuntimeOwnerId("memory-resolution-owner"), AuditSequence(6)
    )
    assert consumed is not None
    applied = store.apply_indeterminate_resolution(
        ApplyResolutionRequest(
            run_id=run_id,
            selection=permit.resolution_selection,
            permit_generation=permit.generation,
            owner_id=RuntimeOwnerId("memory-resolution-owner"),
            expected_sequence=AuditSequence(7),
            intent_ids=_bindings(unresolved)[0],
            payload_digests=_bindings(unresolved)[1],
            recovery_generations=_bindings(unresolved)[2],
            observations=(_abandon_observation(first, member.recovery_generation, run_id),),
            observation_set_digest=observation_set_digest(
                (_abandon_observation(first, member.recovery_generation, run_id),)
            ),
        )
    )
    assert applied.status == "ABANDONED"
    assert store.unresolved_intent_set(run_id) is not None


def test_sqlite_observed_reconciliation_settles_intent_and_survives_restart(
    tmp_path: Path,
) -> None:
    app = make_application(tmp_path, monotonic_clock=SystemMonotonicClock())
    run_id = create_draft_with_three_proposals(app)
    intent = _intent(run_id, "observed-reconcile", AuditSequence(2))
    app.store.record_intent(intent, AuditSequence(1))
    indeterminate = _indeterminate_result(intent, AuditSequence(3))
    app.store.settle_intent(
        run_id,
        intent.intent_id,
        indeterminate,
        ApplicableRevisionDigests(),
        AuditSequence(2),
    )
    unresolved = app.store.unresolved_intent_set(run_id)
    assert unresolved is not None
    member = unresolved.member_bindings[0]
    accepted = app.control.handle(
        _resolve_command(
            run_id,
            AuditSequence(3),
            str(unresolved.set_digest),
            intent_id=str(intent.intent_id),
            recovery_generation=member.recovery_generation,
            resolution="RECONCILE_OBSERVED",
            bindings=app.store.current_revision_digests(run_id),
        )
    )
    assert accepted.status == "ACCEPTED"
    permit = app.store.unconsumed_permit(run_id)
    owner = RuntimeOwnerId("observed-reconcile-owner")
    assert app.store.consume_current_runtime_permit(run_id, owner, AuditSequence(4))
    observation = _observed_completion(intent, member.recovery_generation, run_id, AuditSequence(6))
    applied = app.store.apply_indeterminate_resolution(
        ApplyResolutionRequest(
            run_id=run_id,
            selection=permit.resolution_selection,
            permit_generation=permit.generation,
            owner_id=owner,
            expected_sequence=AuditSequence(5),
            intent_ids=_bindings(unresolved)[0],
            payload_digests=_bindings(unresolved)[1],
            recovery_generations=_bindings(unresolved)[2],
            observations=(observation,),
            observation_set_digest=observation_set_digest((observation,)),
        )
    )
    assert applied.status == "SETTLED"
    assert intent not in app.store.indeterminate_intents(run_id)
    assert intent not in app.store.unsettled_intents(run_id)
    assert app.store.effect_result(intent.intent_id).outcome == "COMPLETED"
    app.close()

    reopened = type(app.store)(app.database)
    assert intent not in reopened.indeterminate_intents(run_id)
    assert intent not in reopened.unsettled_intents(run_id)
    assert reopened.effect_result(intent.intent_id).outcome == "COMPLETED"
    assert reopened.unresolved_intent_set(run_id) is None
    reopened.close()


def test_set_bound_fail_denies_without_complete_abandonability_observations(
    tmp_path: Path,
) -> None:
    app = make_application(tmp_path, monotonic_clock=SystemMonotonicClock())
    run_id = create_draft_with_three_proposals(app)
    first = _intent(run_id, "set-indeterminate-1", AuditSequence(2))
    app.store.record_intent(first, AuditSequence(1))
    app.store.settle_intent(
        run_id,
        first.intent_id,
        _indeterminate_result(first, AuditSequence(3)),
        ApplicableRevisionDigests(),
        AuditSequence(2),
    )
    second = _intent(run_id, "set-indeterminate-2", AuditSequence(4))
    app.store.record_intent(second, AuditSequence(3))
    app.store.settle_intent(
        run_id,
        second.intent_id,
        _indeterminate_result(second, AuditSequence(5)),
        ApplicableRevisionDigests(),
        AuditSequence(4),
    )
    unresolved = app.store.unresolved_intent_set(run_id)
    assert unresolved is not None
    accepted = app.control.handle(
        _resolve_command(
            run_id,
            AuditSequence(5),
            str(unresolved.set_digest),
            resolution="FAIL_RUN",
            bindings=app.store.current_revision_digests(run_id),
        )
    )
    assert accepted.status == "ACCEPTED"
    permit = app.store.unconsumed_permit(run_id)
    owner = RuntimeOwnerId("set-resolution-owner")
    assert app.store.consume_current_runtime_permit(run_id, owner, AuditSequence(6)) is not None
    observation = _abandon_observation(first, 1, run_id)
    before = app.store.audit_sequence(run_id)
    denied = app.store.apply_indeterminate_resolution(
        ApplyResolutionRequest(
            run_id=run_id,
            selection=permit.resolution_selection,
            permit_generation=permit.generation,
            owner_id=owner,
            expected_sequence=before,
            intent_ids=_bindings(unresolved)[0],
            payload_digests=_bindings(unresolved)[1],
            recovery_generations=_bindings(unresolved)[2],
            observations=(observation,),
            observation_set_digest=observation_set_digest((observation,)),
        )
    )
    assert denied.status == "DENIED"
    assert app.store.audit_sequence(run_id) == before
    assert app.store.unresolved_intent_set(run_id) == unresolved
    assert app.store.run_record(run_id).state.value == "INDETERMINATE"
