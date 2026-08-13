# 伪代码：Evidence、Freshness 与 Candidate

## 解决的问题

证据只在产生它的 revision 和依赖集合中有意义。Candidate promotion 必须先证明 evidence 对当前对象仍新鲜，不能把历史 green 结果当作通用通行证。

## Source mapping

- `src/apexcrew/domain/evidence.py`：Evidence Receipt。
- `src/apexcrew/domain/freshness.py`：`FreshnessAssessment.assess`、`promote_candidate`。
- `src/apexcrew/domain/candidate.py`、`domain/admission.py`：candidate/admission baseline。
- `tests/unit/domain/test_evidence.py`、`test_freshness.py`、`test_candidate.py`。

## Observed：freshness assessment

```python
def assess(receipt, current_revision, current_dependencies):
    if receipt.revision_digest != current_revision:
        return Freshness(
            status="STALE",
            reason="REVISION_CHANGED",
            observed_revision=current_revision,
            observed_dependencies=current_dependencies,
        )

    if receipt.dependencies != current_dependencies:
        return Freshness(
            status="STALE",
            reason="DEPENDENCY_CHANGED",
            observed_revision=current_revision,
            observed_dependencies=current_dependencies,
        )

    return Freshness(
        status="FRESH",
        reason="NO_DEPENDENCY_CHANGE",
        observed_revision=current_revision,
        observed_dependencies=current_dependencies,
    )


def promote_candidate(receipt, freshness):
    if freshness.status != "FRESH":
        raise STALE_EVIDENCE
    return PromotedCandidate(
        receipt=receipt,
        freshness=freshness,
        candidate_digest=sha256(canonical_json(receipt, freshness)),
    )
```

## Planned R4.3：Task Candidate Gate

```python
def prepare_task_candidate(binding, patched_workspace, task_checks):
    require(binding.lease_is_current)
    require(patched_workspace.digest == binding.check_workspace_digest)
    require(all(check.is_declared_for(binding.task) for check in task_checks))

    receipts = []
    for check in task_checks:
        result = run_check_in_its_own_Q_union_W_workspace(check)
        require(result.completed_successfully)
        receipts.append(make_receipt(check, result, patched_workspace))

    evidence = bundle(receipts)
    freshness = reassess(evidence, current_run_head(), current_revisions())
    require(freshness.is_fresh)

    prepared_oid = git.prepare_commit_from_workspace(
        parent=current_private_run_head(),
        changed_paths=patched_workspace.changed_paths,
    )
    return TaskCandidate.bind(binding, prepared_oid, evidence, freshness)
```

The R4.3 block is target pseudocode, not a declaration that all of these methods are complete in the current checkout. R4.3-04 and R4.3-05 have corresponding local implementation commits (`0019165` and `be9f48d`), but their required independent reviews and ledger closeouts are pending; read the block as task-worktree behavior rather than root or release behavior.

## Candidate rejection matrix

| Condition | Outcome |
| --- | --- |
| receipt revision differs | `STALE_EVIDENCE` |
| dependency digest differs | `STALE_EVIDENCE` |
| check definition/argv is not declared | deny before execution |
| check workspace digest differs from binding | deny candidate |
| patch result uncertain | preserve recoverable state; do not candidate-promote |
| task evidence incomplete/failing | candidate absent |
| current private head moved | conflict or stale; do not reuse old candidate |

## Interview reading point

Freshness is not cache invalidation for performance; it is an authorization condition for whether evidence can participate in an integration decision.

下一篇：[Git Candidate 与 Final CAS](05-git-final-cas.md)。
