# Durability、Intent 与 Recovery

## 目标

Agent 工作流跨越 SQLite、模型 provider、文件系统、Docker 和 Git。这些系统没有一个共同事务。本文说明 ApexCrew 如何在崩溃、超时和未知网络结果下保存真实知识边界，而不是将“无法确认”变成错误重试。

## Intent-before-effect

每个外部效果采用三段式协议：

```text
1. durable transaction: record intent, identity, expected pre-state/post-state
2. external effect: provider call / tool / Git ref operation
3. durable transaction: observe and settle result
```

故障可能发生在 1/2/3 任意边界。恢复不依赖内存，也不使用“可能没执行”的猜测；它读取 persisted intent 后观察权威外部状态。

## 对账分类

| 观察结果 | 含义 | 恢复动作 |
| --- | --- | --- |
| 精确 pre-state | 效果尚未发生或可安全重新执行 | 受限重试 |
| 精确 expected post-state | 效果已发生但未结算 | 补记成功，不重复效果 |
| 明确失败状态 | 外部系统证明失败 | 结算失败/暂停 |
| 不可观测或第三种状态 | 无法证明成功或未执行 | `INDETERMINATE`，等待 resolution |

这提供的是“不可重复授权 + 可幂等对账”，不是在所有故障模型下的数学 exactly-once。

## Runtime 恢复顺序

`RuntimeService._run_consumed_permit` 将恢复置于常规 phase 驱动之前。高层顺序为：

1. 恢复已释放的下游 model action。
2. 恢复未结算的 Grant action。
3. 处理 terminal-administration Permit。
4. 运行 effect recovery；如果需要人类选择，停止为 `INDETERMINATE`。
5. 恢复已提交但未下游处理的 model turn。
6. 最后才驱动正常 phase。

这样可以防止新 Worker turn 在旧 effect 未对账时开始，避免两个操作竞争同一 Task/Run 状态。

## Recovery 与 human resolution

人类 resolution 不是“手动标记成功”。它只能选择预定义、可观察的策略，且不能凭空产生 Evidence、CAS 成功或 admission。混合状态、篡改状态、缺失观察或多意图歧义必须保留 terminal/paused Run 状态和审计记录。

## Durable identity

关键对象带有 request、intent、logical turn、action、candidate、lease generation 和 Audit sequence 等 identity。它们用于：

- 将重放请求识别为已有 outcome。
- 把 provider/model response 绑定到正确 logical turn。
- 确保 result 只能结算它对应的 intent。
- 将 concurrent update 转成 `CONFLICT`，而不是最后写入者覆盖。

## 源码与测试映射

| 行为 | 源码 | 测试 |
| --- | --- | --- |
| Runtime recovery boundary | `application/runtime.py` | `tests/integration/test_runtime_permits.py` |
| effect intents / audit | `domain/effects.py`、state adapters | `tests/contract/test_state_store.py` |
| model recovery | `domain/model.py`、`application/runtime.py` | `tests/integration/test_model_restart.py` |
| granted action recovery | repository/workspace adapters | `tests/integration/test_granted_action_recovery.py` |
| indeterminate | `domain/indeterminate.py` | `tests/unit/domain/test_indeterminate.py` |
| reservation cleanup | `domain/reservation_cleanup.py` | `tests/unit/domain/test_reservation_cleanup.py` |

## 当前状态边界与 Open Boundaries

当前 checkout 对多意图 precedence、retention 和 Docker process runner 仍保留明确债务；`README.md` 与 `SECURITY.md` 逐项记录 `DEBT-M2-*`。R4.3/R4.1 的恢复设计不意味着所有真实外部观察都已经完成。任何无法观察的结果都仍应停在 `INDETERMINATE`。

详细顺序见 [Runtime 与 Recovery 伪代码](pseudocode/02-runtime-recovery.md)。
