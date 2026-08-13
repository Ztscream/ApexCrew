# 伪代码：Runtime 与 Recovery

## 解决的问题

Run 执行可能被重复调用、崩溃于外部效果中间，或在旧 intent 未结算时收到新命令。Runtime 必须先拥有一次性 Permit 和 per-Run ownership，再按恢复优先级工作。

## Source mapping

- `src/apexcrew/application/runtime.py`：`RuntimeService.run_until_blocked`、`_run_consumed_permit`。
- `src/apexcrew/domain/effects.py`：intent/result/audit 概念。
- `src/apexcrew/domain/indeterminate.py`：不可确定的状态边界。
- `tests/integration/test_runtime_permits.py`、`test_runtime_lock_lifecycle.py`、`test_model_restart.py`。

## Observed：Runtime 边界

```python
def run_until_blocked(run_id) -> RunStop:
    state = store.load_runtime_state(run_id)
    permit = store.unconsumed_permit_or_none(run_id)
    if permit is None:
        return STOP(run_id, state, "NO_RUNTIME_PERMIT")

    with ownership.acquire(run_id, permit) as owner:
        if owner is None:
            return STOP(run_id, state, "ALREADY_RUNNING")

        permit = store.consume_current_runtime_permit(
            run_id=run_id,
            owner_id=owner.id,
            expected_sequence=store.audit_sequence(run_id),
        )
        if permit is None:
            return STOP(run_id, state, "NO_RUNTIME_PERMIT")

        try:
            stop = run_consumed_permit(run_id, permit)
        except InjectedProcessCrash:
            raise  # tests intentionally model process loss
        except Exception as error:
            disposition = store.record_runtime_fault_and_classify_barrier(
                run_id, owner.id, permit.generation, error
            )
            stop = STOP(
                run_id,
                store.load_runtime_state(run_id),
                "INDETERMINATE" if disposition.is_indeterminate else "PAUSED",
            )

        return store.record_runtime_delivery_stop(
            run_id, owner.id, permit.generation, stop
        )
```

## Observed：恢复优先级

```python
def run_consumed_permit(run_id, permit) -> RunStop:
    state = store.load_runtime_state(run_id)

    if state.is_draft:
        return drive_phase_allowed_by(permit)

    recovered = journal.next_recovered_model_action(run_id)
    if recovered is not None:
        return phase_drivers.resume_recovered_model_action(run_id, permit, recovered)

    granted = journal.next_unsettled_granted_action(run_id)
    if granted is not None:
        return phase_drivers.execute_granted_action(run_id, permit, granted.intent_id)

    if permit.allowed_phase == "TERMINAL_ADMINISTRATION":
        return drive_phase_allowed_by(permit)

    effect_recovery = recovery.reconcile(run_id)
    if effect_recovery.requires_human_resolution:
        return STOP(run_id, store.load_runtime_state(run_id), "INDETERMINATE")

    committed_turn = journal.next_recoverable_model_turn(run_id)
    if committed_turn is not None:
        recovered_action = model_client.recover_committed(
            run_id, committed_turn.logical_turn_id, expected_binding(committed_turn)
        )
        if recovered_action.has_no_normalized_action:
            return STOP(run_id, store.load_runtime_state(run_id), "PAUSED")
        journal.record_downstream_action_intent(recovered_action)
        return phase_drivers.resume_recovered_model_action(
            run_id, permit, recovered_action
        )

    return drive_phase_allowed_by(permit)
```

## 恢复不做什么

```python
if external result is unknown:
    # Never do this:
    retry_effect_blindly()

    # Do this instead:
    persist_or_keep(INDETERMINATE)
    require_authoritative_observation_or_human_resolution()
```

## 状态边界

该伪代码说明 current `RuntimeService` 的恢复骨架。某些 action-class resolution、真实 workspace mutation history、R4.3 final candidate 和 Docker process observation 仍有计划/债务边界；不能从这里推导出所有外部效果已经生产级 exactly-once。

下一篇：[Worker Turn 与 Tool Feedback](03-worker-turn.md)。
