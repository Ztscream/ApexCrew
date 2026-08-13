# ApexCrew Learning Notes

本目录是从项目源码和测试反推工程理解的学习入口，不是第二份规范，也不承担任务完成台账。产品语义以 [`SPEC.md`](../../SPEC.md) 为准，任务授权和完成状态以 [`PLAN.md`](../../PLAN.md) 与 [`AGENT_LOG.md`](../../AGENT_LOG.md) 为准。

## 学习路线

| 顺序 | 学习问题 | 公开材料 | 应能说明的证据 |
| --- | --- | --- | --- |
| 1 | Agent、Framework 与 Harness 有何边界？ | [Harness 与系统边界](../architecture/01-harness-overview.md)、[Control/Runtime/Query](../architecture/02-control-runtime-query.md) | 模型输出为何不是 authority，为什么 WebUI 没有写能力 |
| 2 | 一次 Worker action 如何变成可审计效果？ | [Coordinator 与 WorkerLoop](../architecture/03-coordinator-worker-loop.md)、[Authority](../architecture/04-authority-permit-grant.md) | Permit、Lease、Grant、budget、deadline 和 pre-state 各自解决什么问题 |
| 3 | 为什么测试绿不等于可集成？ | [Evidence、Freshness 与 Admission](../architecture/05-evidence-freshness-admission.md)、[Candidate 与 CAS](../architecture/06-git-candidates-cas.md) | Task Candidate、private Run Head、Run Candidate、final CAS 的区别 |
| 4 | 崩溃后如何避免重复副作用？ | [Durability 与 Recovery](../architecture/07-recovery-and-durability.md) | intent-before-effect、post-state observation 与 `INDETERMINATE` |
| 5 | 如何把不可信模型和仓库限制在边界内？ | [安全与受限执行](../architecture/08-security-and-executor.md) | schema、canonical path、no-follow、closed argv、restricted executor |
| 6 | 如何证明项目而不是复述设计？ | [测试与验收](../architecture/09-testing-and-acceptance.md)、[v0.1 闭环状态](../architecture/10-v0.1-closure-status.md) | 哪些是 reviewed baseline，哪些只是 task-worktree evidence，哪些仍为 owner-only gate |
| 7 | 如何在白板或答辩中说明控制流？ | [项目伪代码](../architecture/pseudocode/README.md) | Permit runtime、Worker turn、Evidence/Candidate、final CAS 的 stop case |

## 可复现的学习检查

从 root 基线运行下列无网络命令，先观察事实再准备表述：

```powershell
uv run --python 3.12 apexcrew --help
uv run --python 3.12 python -m apexcrew.demo
uv run --python 3.12 pytest tests/contract/test_documentation_delivery.py -q
```

`make test`、`make lint`、`make demo`、`make secret-scan`、`make web-build` 与 `make build` 分别是不同证据，不能以一个退出码替代另一个。真实 provider、remote CI、push、merge、Pages 和 package publication 都不是默认学习命令。

## 当前事实口径

截至 2026-08-10，R4.3-00 至 R4.3-03 是已完成独立 review 的基线。R4.3-04、R4.3-05 与 R4.3-06 分别有本地绿色实现提交 `0019165`、`be9f48d`、`a846b3f`，但仍待独立 SPEC review、quality review 和 ledger closeout；R4.3-07 尚未开始。学习材料可以解释这些提交的机制，但不得把它们写成 root/`main`、remote CI 或 release 已完成的证据。

## 本地面试材料

`APEXCREW_INTERVIEW_GUIDE.md` 与 `apexcrew-interview/` 是本地忽略的个人学习资料，用于准备简历、STAR、问答和白板演示，不随仓库发布。它们必须引用本页和公开架构文档的状态口径；个人材料不应包含真实凭据、私有仓库内容、受限 transcript 或未经验证的数字。

## 维护规则

新增学习主题前，先确认其对应行为有源码、测试或 ledger 证据。每个主题至少回答：它解决什么问题、ApexCrew 的不变量是什么、哪项测试或命令可观察到该行为、失败时系统如何 fail closed。不要复制生成式教程或完整聊天记录。
