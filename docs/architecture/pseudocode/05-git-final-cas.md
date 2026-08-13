# 伪代码：Git Candidate 与 Final CAS

## 解决的问题

多个 Task 的 verified patch 必须私有组合，最终只把一个经过 Run-wide verification 的 commit 集成到目标 ref。Worker 不能直接移动 target，且 target 在准备过程中可能被外部用户移动。

## Source mapping

- Observed baseline：`src/apexcrew/domain/candidate.py`、`domain/admission.py`、`adapters/repository/target_cas.py`、`adapters/repository/git.py`。
- Planned R4.3：`PLAN.md` 的 R4.3-04、R4.3-05；对应 task-branch 的 `candidate_preparation.py` 和 integration tests。
- Baseline tests：`tests/unit/adapters/test_target_cas.py`、`tests/integration/test_target_reservation.py`。

## Planned R4.3：Task Candidate 私有推进

```python
def promote_task_candidate(run_id, task_candidate):
    require(task_candidate.evidence_is_fresh)
    require(task_candidate.parent_oid == current_private_run_head(run_id))
    require(task_candidate.is_not_a_run_candidate)

    intent = RefCasIntent(
        ref_name=private_ref(run_id),
        expected_old_oid=current_private_run_head(run_id),
        new_oid=task_candidate.prepared_oid,
    )
    observed = git.execute_private_ref_cas(intent)

    if observed.ref_oid == task_candidate.prepared_oid:
        state.advance_private_run_head(run_id, task_candidate.prepared_oid)
        return CONTINUE
    if observed.ref_oid == intent.expected_old_oid:
        return PAUSE("PRIVATE_REF_CAS_NOT_APPLIED")
    return PAUSE("PRIVATE_REF_CONFLICT_OR_INDETERMINATE")
```

The target ref is not read/write input to this function except for preserving its pinned safety binding. It must remain byte-identical during private promotion.

## Planned R4.3：冻结 Run Candidate

```python
def freeze_run_candidate(run_id):
    T0 = state.pinned_target_oid(run_id)
    H = current_private_run_head(run_id)

    require(target_ref_oid() == T0)
    require(all_required_task_candidates_promoted(run_id, H))
    require(all_task_evidence_is_fresh_against(H))

    R = git.create_commit(
        tree=git.tree_of(H),
        first_parent=T0,
        message=run_candidate_message(run_id),
    )
    require(git.first_parent_of(R) == T0)
    require(R != H)  # separate private history from final integration object

    run_evidence = execute_run_wide_declared_checks(R)
    require(run_evidence.is_complete_and_fresh)
    return state.store_frozen_run_candidate(
        run_id, head_oid=H, prepared_oid=R, target_base_oid=T0, evidence=run_evidence
    )
```

## Planned R4.3：最终 Grant-bound CAS

```python
def integrate_frozen_run_candidate(run_id, grant):
    candidate = state.require_frozen_run_candidate(run_id)
    require(grant.matches_exact_final_integration(candidate))
    require(candidate.evidence_is_fresh)
    require(target_ref_oid() == candidate.target_base_oid)

    intent = TargetCasIntent(
        ref_name=target_ref(run_id),
        expected_old_oid=candidate.target_base_oid,
        new_oid=candidate.prepared_oid,
        candidate_id=candidate.id,
    )
    observed = git.execute_target_ref_cas(intent)

    if observed.ref_oid == candidate.prepared_oid:
        state.settle_integrated_once(run_id, candidate, grant)
        return COMPLETED
    if observed.ref_oid == candidate.target_base_oid:
        return PAUSE("TARGET_CAS_NOT_APPLIED")
    return PAUSE("TARGET_MOVED_OR_INDETERMINATE")
```

## 不变量

1. 只有 private ref CAS 能推进 `H`；Task Candidate 不能直接调用 target CAS。
2. `R` 的 tree 来自完整 `H`，但 first parent 必须是 `T0`，且 `R != H`。
3. 最终 CAS 前 target 仍必须是 `T0`；外部移动得到 conflict，绝不覆盖。
4. final Grant、candidate、evidence 和 target safety 必须在同一精确 binding 上匹配。
5. replay 不得签发第二个 CAS 或第二次结算 integrated 状态。

## 当前状态边界

这篇主体是 R4.3-04/05 的目标伪代码。对应本地实现提交分别为 `0019165` 与 `be9f48d`，但均仍待独立 SPEC review、quality review 和 ledger closeout。它们因此不是对当前 root、`main`、当前脏 checkout 或远端 release 的完成声明。阅读时必须查看 `PLAN.md` 任务 ledger、相关 task branch review evidence 与 [v0.1 闭环状态](../10-v0.1-closure-status.md)。
