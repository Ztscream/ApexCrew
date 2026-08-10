# 伪代码：命令与 Runtime Permit

## 解决的问题

用户命令被接受后，不能直接执行模型、工具或 Git 副作用。Control 需要先把命令的语义、revision 和审计记录持久化，再只为该命令允许的 runtime phase 创建一个一次性 Permit。

## Source mapping

- `src/apexcrew/application/control.py`：`CrewControlService.handle`。
- `src/apexcrew/application/composition.py`：Control 与 state/authority 的组合。
- `src/apexcrew/domain/commands.py`：`CommandEnvelope`、payload、`RuntimePermit`、`CommandOutcome`。
- `src/apexcrew/adapters/state/sqlite.py`：持久化 command application。
- `tests/integration/test_runtime_permits.py`、`tests/unit/domain/test_commands.py`。

## 输入与前置条件

```text
CommandEnvelope:
  request_id            # 幂等 identity
  run_id
  payload               # create/approve/start/grant/integrate/... typed payload
  expected sequence     # 防止在旧审计位置写入
  exact revision digests where the command requires them

Control must never execute a model, tool, Git CAS, or executor directly.
```

## Observed：命令处理

```python
def handle(command: CommandEnvelope) -> CommandOutcome:
    # CrewControlService itself is deliberately thin.
    return command_handler.apply(command)


def apply(command) -> CommandOutcome:
    with state.transaction():
        existing = state.outcome_for_request(command.request_id)
        if existing is not None:
            return existing  # idempotent replay returns the durable outcome

        run = state.require_run(command.run_id)
        require(command.expected_sequence == run.audit_sequence)
        require_payload_shape_and_command_phase(command, run)
        require_exact_current_revisions(command, run)

        outcome = apply_typed_payload(command, run)
        state.append_audit(command, outcome)

        if outcome.accepted and payload_requires_runtime(command.payload):
            permit = RuntimePermit.bind(
                run_id=run.id,
                allowed_phase=phase_for(command.payload),
                revisions=current_revisions(run),
                generation=next_permit_generation(run),
                command_request_id=command.request_id,
                expected_sequence=next_audit_sequence(run),
            )
            state.store_unconsumed_permit(permit)

        return state.store_command_outcome(outcome)
```

## 关键拒绝分支

```python
if command.request_id was already applied:
    return previous_outcome_without_new_effect()

if command.expected_sequence != current.audit_sequence:
    return CONFLICT

if revision digest or confirmation code differs:
    return STALE_OR_DENIED

if payload is not legal for current lifecycle phase:
    return INVALID

if a runtime permit already conflicts with the requested transition:
    return CONFLICT
```

## 为什么 Permit 不等于命令结果

`CommandOutcome.ACCEPTED` 说明 Control 已经持久化了用户意图。Runtime Permit 才说明一个 Runtime 可以在精确 phase、精确 revision、精确 generation 下开始执行。后者消费后不可复用，因而旧 CLI 重放、直调 Runtime 和两个并发 runtime 都不能扩张 authority。

## 测试阅读点

- 先看 `test_runtime_permits.py` 中的首次运行与重放断言。
- 再看 `test_commands.py` 中 expected sequence、revision 和 outcome 的断言。
- 最后看 `test_cli.py`，确认 CLI 不绕过 public Control surface。

下一篇：[Runtime 与 Recovery](02-runtime-recovery.md)。
