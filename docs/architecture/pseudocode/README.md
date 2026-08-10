# ApexCrew 项目伪代码

本目录用 Python-like 伪代码解释关键控制流。它不是可运行实现，也不替代 `SPEC.md`、`PLAN.md` 或源码；阅读时始终以列出的 source mapping 为准。

## 约定

| 标记 | 含义 |
| --- | --- |
| `Observed` | 当前源码已存在的行为，可能仍受 baseline 深度/债务限制 |
| `Planned R4.3` | 已在当前计划中定义，但未完成全部 task review/release evidence 的行为 |
| `STOP(reason)` | 持久化或投影一个 Runtime stop，不代表错误被自动修复 |
| `PAUSE(reason)` | 当前 Run 不继续产生副作用，等待合法下一步 |
| `INDETERMINATE` | 外部结果不能权威确认；禁止猜测或盲重试 |
| `CAS(old, new)` | 仅当前值等于 `old` 时原子写入 `new` |

## 阅读顺序

1. [命令与 Runtime Permit](01-command-permit.md)
2. [Runtime 与 Recovery](02-runtime-recovery.md)
3. [Worker Turn 与 Tool Feedback](03-worker-turn.md)
4. [Evidence、Freshness 与 Candidate](04-evidence-candidate.md)
5. [Git Candidate 与 Final CAS](05-git-final-cas.md)

每篇伪代码都包含：输入/状态、不可违反的前置条件、主流程、失败分支、源码映射、测试映射和当前状态边界。
