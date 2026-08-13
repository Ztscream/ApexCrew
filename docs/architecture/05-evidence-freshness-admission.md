# Evidence、Freshness 与 Admission

## 目标

说明为什么 ApexCrew 不接受“模型说修好了”或“某次测试曾通过”作为集成依据。可靠准入要求将模型上下文、客观检查、依赖、revision、候选对象和目标安全条件绑定起来，并在最终操作前重新判断这些绑定是否仍然成立。

## 四个相互关联的对象

| 对象 | 回答的问题 | 不回答的问题 |
| --- | --- | --- |
| Context Capsule | 模型当时能看到什么目标、约束和受限文件？ | 当前 Git tree 是否仍等于当时上下文 |
| Evidence Receipt | 某个检查在某组输入、revision、依赖下发生了什么？ | 该检查能否证明当前 candidate |
| Freshness Assessment | Receipt 与当前 revision/dependencies 是否仍相容？ | candidate 是否拥有集成 authority |
| Candidate / Evidence Bundle | 哪些经验证对象和 receipts 一起参加准入？ | target ref 是否仍是期望 old OID |

这四个对象应不可变。更新代码、依赖或配置时，系统重新创建 assessment/candidate，而不是改写旧 evidence 使其看起来仍然新鲜。

## Observed baseline：FreshnessAssessment

当前 `domain/freshness.py` 中的 `FreshnessAssessment.assess` 已展示最小模型：

```text
receipt.revision != current_revision  -> STALE: REVISION_CHANGED
receipt.dependencies != current_deps  -> STALE: DEPENDENCY_CHANGED
otherwise                              -> FRESH: NO_DEPENDENCY_CHANGE
```

`promote_candidate` 拒绝 `STALE` assessment，并基于 receipt + assessment 的 canonical JSON 生成 candidate digest。这已经避免将 revision 或依赖变化后的旧测试结果直接提升为候选。

## 当前状态边界：Planned R4.3 完整 Admission Gate

R4.3 计划将准入扩展到真实 patch/check/Git candidate 链路。最终逻辑需要验证：

1. Attempt binding、lease generation、Task contract 与当前 private Run Head 一致。
2. patch 的 post-tree digest 与 Task Candidate 输入一致。
3. 所有声明 check 的 `Q_i union W_i` workspace 各自绑定自己的 digest。
4. Task Evidence Bundle 完整、成功且重新计算后仍 fresh。
5. Task Candidate 只能通过私有 ref CAS 推进 private Run Head，不能充当 Run Candidate。
6. 所有 Task Candidate 已私有推进后，重新对完整 Run Head 构造 Run Candidate 并运行 run-wide checks。
7. 仅当 final Grant、pinned target `T0`、target safety 和 Evidence Bundle 仍匹配时，才发出 final target CAS。

R4.3-04、R4.3-05 和 R4.3-06 各有本地实现提交（分别为 `0019165`、`be9f48d`、`a846b3f`），但三者均未完成独立 review 和 ledger closeout；R4.3-07 尚未开始。因此这些项目仍不能被混同为当前 checkout、root/`main` 或 release 的已交付行为。任务级证据与门禁见 [v0.1 闭环状态](10-v0.1-closure-status.md)。

## 为什么每个检查有自己的工作区绑定

不同 check 的输入和写范围可能不同。若把 Run 级别的所有 `Q union W` 合并成一个 workspace/digest，某个检查可能在未经批准的额外输入上运行，或一个 check 的 digest 被另一个 check 重用。正确模型是：

```text
for each declared check i:
    workspace_i = materialize(Q_i union W_i)
    digest_i = digest(workspace_i)
    receipt_i binds check definition, argv, digest_i, candidate revision
```

Context workspace 同样不能替代 check workspace；前者服务于模型可见性，后者服务于 mutation/check authorization。

## Admission 不拥有的职责

- 不决定模型 prompt 或 scheduling。
- 不替人类批准 Grant。
- 不执行任意 shell 或直接传入 raw Git command。
- 不把证据缺失解释为“模型应该再试一次”。
- 不改变 target ref；它只构造/签发 typed CAS 请求，由 Git adapter 执行。

## 源码与测试映射

| 行为 | 源码 | 测试 |
| --- | --- | --- |
| receipt/freshness/candidate | `domain/evidence.py`、`domain/freshness.py` | `tests/unit/domain/test_evidence.py`、`test_freshness.py`、`test_candidate.py` |
| candidate/admission baseline | `domain/admission.py`、`domain/candidate.py` | `tests/unit/domain/test_candidate.py` |
| context scope | `domain/worker.py`、`domain/tools.py` | `tests/integration/test_scoped_reads.py` |
| planned real workspace | R4.3 task branches / `PLAN.md` R4.3-01/02 | planned `test_attempt_workspace.py`、`test_attempt_patch_executor.py` |
| planned candidate promotion | R4.3-04 | `test_candidate_preparation.py`、`test_task_candidate_promotion.py` |

## 面试表达

“测试通过”是一个没有上下文的事实；“该 check 在这个 revision、这个输入集合、这个工具定义下通过，并且相关依赖和 target 仍未变化”才是可用于集成的 evidence。ApexCrew 将这种差别编码为 immutable receipt、freshness 和 candidate gate。

下一篇：[Git Candidate 与 CAS](06-git-candidates-cas.md)。
