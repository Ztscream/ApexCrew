# 伪代码：Worker Turn 与 Tool Feedback

## 解决的问题

模型返回 action 不是执行权限。WorkerLoop 必须将一个已绑定的 Attempt 依次经过上下文、预算、结构化解析、pre-state、Authority、Intent、Tool Runtime 和结果结算，才允许形成下一轮反馈。

## Source mapping

- `src/apexcrew/domain/worker.py`：`WorkerLoopService.run_turn`、`_consume_released_worker_action`。
- `src/apexcrew/domain/tools.py`：typed tool runtime 和 action validation。
- `src/apexcrew/domain/authority.py`：authorization request、lease、Grant、deadline。
- `tests/unit/domain/test_worker_loop.py`、`tests/integration/test_worker_feedback.py`、`tests/unit/domain/test_tool_validation.py`。

## Observed：一轮 Worker

```python
def run_turn(attempt_id) -> RuntimeDecision:
    binding = attempts.current_worker_turn_binding(attempt_id)
    capsule = context.build_current(attempt_id)

    request = worker_request_with_feedback(
        requests.for_attempt(attempt_id, capsule),
        attempts.latest_worker_feedback(attempt_id),
    )
    reservation = build_model_reservation(
        run_id=binding.run_id,
        task_id=binding.task_id,
        attempt_id=binding.attempt_id,
        revisions=binding.revisions,
        target_safety=binding.target_safety_digest,
        counters=journal.current_counters(binding.run_id, binding.task_id),
        deadline=clock.now() + ordinary_action_timeout,
    )
    model_outcome = models.complete(reservation.with_request(request))

    if model_outcome.is_reservation_denial:
        return PAUSE(model_outcome.reason)
    if model_outcome.is_not_completed or model_outcome.action is None:
        return PAUSE(model_outcome.outcome)

    return consume_released_action(binding, model_outcome)
```

## Observed：从 action 到工具效果

```python
def consume_released_action(binding, model_outcome) -> RuntimeDecision:
    action = action_codec.parse_exactly_one(model_outcome.action)
    if action is None:
        return attempts.record_malformed_worker_action(binding, model_outcome)

    pre_state = tools.capture_expected_prestate(binding, action)
    authorization = authority.authorize(
        run_id=binding.run_id,
        task_id=binding.task_id,
        attempt_id=binding.attempt_id,
        action=action,
        action_digest=normalized_action_digest(action),
        expected_prestate_digest=digest(pre_state),
        lease_id=binding.lease_id,
        lease_generation=binding.lease_generation,
        admissible_head=binding.admissible_head,
        revisions=binding.revisions,
        deadline=deadline_for(action),
    )

    if authorization.denied:
        return persist_denial_or_pause(authorization)
    if authorization.requires_grant:
        return persist_pending_action_then_pause(authorization)

    intent = journal.record_authorized_tool_intent(binding, action, pre_state)
    result = tool_runtime.execute(intent)
    journal.settle_tool_intent(intent, result)

    feedback = bounded_worker_feedback(result)
    attempts.record_worker_feedback(binding.attempt_id, feedback)
    return decision_from(result)
```

## 反馈不改变 authority

```python
check_result = CHECK_FAILED
feedback = {"code": "CHECK_FAILED", "bounded_output": "..."}

# The next model request can see feedback, but it receives no extra permission.
next_action = model.next(feedback)
authorize_again(next_action)  # policy, lease, budget, grant, pre-state all rechecked
```

## Planned R4.3 边界

真实 context/check workspace、AttemptPatchExecutor、restricted Docker composition 和 candidate preparation 由 R4.3-01 至 R4.3-05 分段完成。WorkerLoop 的控制结构存在，但“真实 patch 通过真实 check 并完成 final target CAS”的全链路不能在这些任务全部审查完成前称为已交付。

下一篇：[Evidence、Freshness 与 Candidate](04-evidence-candidate.md)。
