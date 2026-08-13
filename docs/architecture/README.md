# ApexCrew Architecture Guide

这组文档解释 ApexCrew 的实现形状、运行顺序和验证方式。它们服务于代码阅读、设计评审和项目答辩，不替代规范或执行账本。

## 文档权威关系

| 问题 | 首选来源 | 本目录的作用 |
| --- | --- | --- |
| 产品语义、安全不变量、v0.1 范围 | [`SPEC.md`](../../SPEC.md) | 用实现语言解释，不重新定义规范 |
| 当前授权任务、完成状态、审查账本 | [`PLAN.md`](../../PLAN.md)、[`AGENT_LOG.md`](../../AGENT_LOG.md) | 链接相关阶段，不把计划写成完成事实 |
| 实际可执行行为 | `src/apexcrew/` 和 `tests/` | 提供源文件、类型和测试映射 |
| 用户安装与日常命令 | [`README.md`](../../README.md) | 承载设计细节的入口链接 |
| 历史决策 | [`docs/adr/`](../adr/) | 解释已选方案和拒绝方案 |

任何冲突都按上表优先级处理。特别是，`SPEC.md` 不因解释文档而改变；`PLAN.md` 中的 pending、blocked、review pending 状态不能被描述为已交付。

## 阅读顺序

1. [Harness 与系统边界](01-harness-overview.md)：理解 ApexCrew 解决什么问题，以及模型、Harness、Git 与用户各自拥有什么权力。
2. [Control、Runtime 与 Query](02-control-runtime-query.md)：理解三个公共接口、Runtime Permit 和只读投影。
3. [Coordinator 与 WorkerLoop](03-coordinator-worker-loop.md)：理解规划、任务调度、模型动作和工具反馈循环。
4. [Authority、Permit、Lease 与 Grant](04-authority-permit-grant.md)：理解多层授权和重放保护。
5. [Evidence、Freshness 与 Admission](05-evidence-freshness-admission.md)：理解为什么“测试通过”仍不足以集成。
6. [Git Candidate 与 CAS](06-git-candidates-cas.md)：理解私有 Run Head、候选隔离和最终 ref 更新。
7. [Durability 与 Recovery](07-recovery-and-durability.md)：理解 Intent、外部副作用和 `INDETERMINATE`。
8. [安全与受限执行](08-security-and-executor.md)：理解路径、Git、Docker、凭据和只读 WebUI 的边界。
9. [测试与验收](09-testing-and-acceptance.md)：理解分层测试、fixture 和证据口径。
10. [v0.1 闭环状态](10-v0.1-closure-status.md)：区分已审查基线、独立工作树的本地证据、release gate 与 owner-only 外部交付。
11. [项目伪代码](pseudocode/README.md)：按真实源文件阅读关键控制流。

## 当前实现状态

本文档反映 2026-08-10 的工作树事实：M1-M4 Sprint baseline 已按 REAL、SKELETON、STUB 的混合深度记录；R4.3-00 至 R4.3-03 在顺序任务分支上完成规格和质量审查；R4.3-04、R4.3-05 和 R4.3-06 各有本地绿色实现提交，但独立审查和 ledger closeout 仍待完成；R4.3-07 尚未开始。公开文档可以描述已观测的代码和已审查基线，但不能把未合并分支、未完成任务、真实 provider smoke、Pages、push、merge 或 package publication 描述为完成。详细矩阵见 [v0.1 闭环状态](10-v0.1-closure-status.md)。

## 阅读约定

- `Observed` 表示当前对应源码或测试已存在。
- `Planned R4.3` 表示已获计划授权但尚未完成全部审查/验证的目标行为。
- `Open boundary` 表示明确 fail-closed 的限制或技术债务。
- 伪代码是解释代码路径的 Python-like 表达，不是可复制的实现，也不增加新的 authority。
- 每篇文档均给出源码和测试入口；测试名是行为证据，不是规范的替代物。

## 最小术语

| 术语 | 含义 |
| --- | --- |
| Run | 一个持久化、版本绑定的 Agent 工作旅程 |
| Runtime Permit | Control 命令签发、Runtime 消费的一次性内部 capability |
| Lease | Worker 对 Task workspace scope 的带 generation 所有权 |
| Grant | 对一个精确高风险动作的一次性人工批准 |
| Evidence | 与 revision、dependency、check 和 candidate 绑定的客观结果 |
| Freshness | 证据是否仍能证明当前候选，而非历史候选 |
| Admission | 唯一能准备候选并签发 Git ref CAS 请求的领域边界 |
| `INDETERMINATE` | 外部效果无法被权威观察，系统必须停止而非猜测 |
