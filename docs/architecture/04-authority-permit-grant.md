# Authority、Permit、Lease 与 Grant

## 目标

解释 ApexCrew 为什么不用一个笼统的“Agent 已授权”布尔值。安全执行需要多个独立条件同时成立：Run 是否可运行、当前 Runtime 是否拥有一次性 Permit、Worker 是否拥有 workspace、动作是否符合 policy 和预算、风险动作是否获得精确人类批准。

## 四种不同的能力

| 对象 | 谁创建 | 解决的问题 | 生命周期 | 不能替代 |
| --- | --- | --- | --- |
| Runtime Permit | `CrewControl` / state transaction | 某个已接受命令能否启动特定 Runtime phase | 一次消费 | Grant、lease 或 candidate evidence |
| Workspace Lease | 调度/Attempt 创建 | 哪个 Worker 可以在何种 scope 下工作 | generation-bound，可过期 | 人工风险批准 |
| Approval Grant | 用户经 Control 提交 | 某个 Pending Action 是否可执行 | exact one-use | Permit、policy、freshness |
| Evidence/Freshness | Tool/Admission | 当前候选是否仍可被准入 | revision/dependency-bound | 任何执行 authority |

这四类对象是正交的。例如，一个 Worker 即使持有有效 lease，也仍可能因预算耗尽、pre-state 改变或缺少 Grant 而不能写入；有 Grant 也不能让过期 Permit 重启 Runtime。

## 动作授权的组合条件

```text
ALLOW(action) =
  current Runtime Permit
  AND active Run / phase
  AND matching revision digests
  AND expected audit sequence
  AND valid Worker lease + generation + scope
  AND policy permits action
  AND budget/deadline permits action
  AND expected pre-state matches
  AND (Grant is not required OR exact unconsumed Grant matches)
```

任意一项不满足时，系统返回类型化 denial、stale、conflict、pause 或 pending approval；没有一个“兼容模式”把条件缺失转成 allow。

## Grant 的精确绑定

高风险动作不能批准“以后所有 patch”。一个 Grant 应绑定至少以下信息：

- Run、Task、Attempt 和 logical action identity。
- 规范化 action/payload digest。
- 目标路径、操作类型或 target ref。
- expected pre-state digest。
- 当前 Policy/Budget/Model/Plan revision。
- Pending Action confirmation code、过期和一次消费状态。

当用户批准后，Grant consumption 与被批准 intent 的持久化必须在同一原子边界内发生。否则 pause/cancel、文件漂移或两个 concurrent runtime 都可能将一份批准用于不同副作用。

## Lease 的 generation 语义

Lease 不只是“锁了一下目录”。它把 ownership 和 generation 写入 Attempt binding。调度器重新分配 Task、取消 Run 或恢复后产生的新 generation，会让旧 Worker 的 lease 无效。任何工具执行在写入前检查当前 lease 和 write globs，因此旧线程即使还拿着旧上下文也没有可用 authority。

## Policy、预算与 deadline

`ActionPolicy` 负责将动作分类为直接拒绝、允许或需要 Grant；Authority 还检查 budget counters、progress、deadline、Task state 和 scope。硬资源上限和 no-progress 规则不依赖模型自行判断“我是否应该停止”。

## 常见错误模型

| 错误设计 | ApexCrew 的替代 |
| --- | --- |
| “用户批准过一次，所以这个 Worker 一直可写” | one-use Grant + matching pre-state/revision |
| “Task 有锁，所以 worker 可以做任何事” | Lease 只赋予 scope/generation，仍需 policy/budget/Grant |
| “Runtime state 是 ACTIVE，所以直接执行” | ACTIVE 之外还需要 current Runtime Permit 和 ownership |
| “测试通过后任何 ref 都可以更新” | Admission-issued typed CAS，验证 expected old OID |

## 源码与测试映射

| 概念 | 源码 | 测试 |
| --- | --- | --- |
| Permit document/consumption | `domain/commands.py`、`application/runtime.py` | `tests/integration/test_runtime_permits.py` |
| Action policy | `domain/policy.py` | `tests/unit/domain/test_action_policy.py` |
| Grant / authorization | `domain/authority.py` | `tests/integration/test_grant_consumption.py` |
| Lease | `domain/authority.py` | `tests/unit/domain/test_leases.py` |
| deadline | `domain/authority.py` | `tests/unit/domain/test_action_deadlines.py` |
| command outcomes | `domain/commands.py`、state adapters | `tests/unit/domain/test_commands.py` |

## 当前状态边界

Permit、Grant、lease、budget 和 deadline 的类型边界在 baseline 中已存在。R4.3 后续任务仍需要将真实 Task/Run Candidate 端到端集成和 release evidence 完整闭环；本文描述授权模型，不把计划中的最终集成状态提前标为完成。

下一篇：[Evidence、Freshness 与 Admission](05-evidence-freshness-admission.md)。
