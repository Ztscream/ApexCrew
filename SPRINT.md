# ApexCrew 冲刺执行计划(SPRINT)

**目标:** 14-20 小时内让 M1-M4 端到端可运行,课程交付物齐全。边缘情况与部分安全强化标记为已知债务,之后再优化。

**这份文件的地位:** 短程执行清单,规定**做什么、做到什么深度、如何验证**。详细需求契约仍以 `PLAN.md` 和冻结的 `SPEC.md` 为准——遇到语义问题查那两份,不要自己发明。本文件与 `PLAN.md` 的 Streamlined Execution Protocol 一致。

**执行模式:** 连续执行,按顺序做完整个清单。遇到阻塞记录到 `AGENT_LOG.md` 后跳过继续,不要停下来等人回答。**唯一例外是 push 和 PR 创建,那是仓库所有者的动作。**

---

## 一、不可破的规则(动手前读完)

1. **Fail closed。** 这是安全内核。未实现的分支必须拒绝或抛异常,**绝不返回放行**。半成品的权限判断静默放行,比没有判断更危险。
2. **未覆盖的恢复场景一律落 `INDETERMINATE`**,不猜成功、不用墙钟、不猜崩溃余量。
3. **TDD。** 先写失败测试并观察到红,再写最小实现。没有先于测试写出的实现。
4. **每个骨架处留标记:** `# DEBT-<编号>: <一行说明>`。之后的优化清单靠 `grep -rn "DEBT-" src/` 生成,不靠回忆。
5. **不改 `SPEC.md`。** 一个字节都不动。
6. **不推送、不创建 PR、不碰凭据、不启用 Pages、不改工作流权限。**
7. **观察到真实输出才能声称通过。** 不许写"应该能过"。
8. **不写长篇分析。** `AGENT_LOG.md` 每条只要课程要求的七个字段,不要 "Plan defects and interpretations" 段落。

## 二、深度分级

| 级别 | 含义 | 评审要求 |
|---|---|---|
| **REAL** | 完整实现 + 测试。安全相关或交付物必需。 | 有序 spec 评审 → quality 评审 |
| **SKELETON** | 主路径实现 + 测试;边缘/异常路径 fail closed + DEBT 标记。 | 一次自检 |
| **STUB** | 接口存在,调用即拒绝或抛 `NotImplementedError` + DEBT 标记。**必须有一个测试证明它 fail closed。** | 一次自检 |

## 三、每任务协议

1. 记录 base SHA 和打算改的路径
2. 写失败测试 → 跑 → **观察到红**
3. 最小实现 → 跑焦点选择器 → 绿
4. `uv run --python 3.12 mypy src` + `uv run --python 3.12 ruff check .` + `uv run --python 3.12 ruff format --check .` + `git diff --check`
5. 按深度分级做评审(REAL 双评审;SKELETON/STUB 自检)
6. 一个 Conventional Commit,含实现 + 测试 + `AGENT_LOG.md` 条目,带 trailer:
   ```
   PLAN-Task: <任务号>
   Subagent: <标识>
   Human-Changes: none
   Spec-Review: <标识或 self-check>
   Quality-Review: <标识或 self-check>
   ```
7. 模块收尾时批量更新本文件的台账 + 一个 `docs(plan): record <模块> task commits` 提交

---

## 四、任务清单

### 阶段 M1 — 收口(目标 2-3 h)

| # | 任务 | 深度 | 文件 | 失败测试 / 验证 |
|---|---|---|---|---|
| **S1** | 完成 `M1-FIX-005` 递归预留清点。worktree `.worktrees/m1-r3-reservation-guard` 已有未提交实现,规格评审已 PASS,只差质量评审 → 提交 | REAL | `adapters/repository/git.py`, `tests/integration/test_git_preflight.py`, `tests/integration/test_target_reservation.py` | 已有:`pytest tests/integration/test_git_preflight.py tests/integration/test_target_reservation.py -q`。提交信息 `fix(git): bind complete reservation inventory` |
| **S2** | 把 PR #8 的 M1-08 成果(tools/worker/executor/granted_workspace)合入当前 `main`。**分支落后 42 个提交,与 R3-01/R3-02 在 `sqlite.py`/`memory.py`/`model.py` 上有真实冲突**,逐个解决,以 `main` 的 R3 修复为准 | REAL | 新分支 `codex/m1-08-integrate`,来源 `codex/m1-08-worker-tools` | 合并后全量必须绿:`pytest -q`;冲突解决点写进 `AGENT_LOG.md` |
| **S3** | 修 TOCTOU:授权写入(delete/rename/chmod/replace)当前**按路径名分派**,存在祖先目录替换竞态,违反 `SPEC.md:164`。改为句柄化,复用 Task 7 的 no-follow 原语 | **REAL** | `adapters/repository/granted_workspace.py`, `tests/integration/test_granted_action_recovery.py` | 新测试:祖先在预检与写入之间被替换时必须拒绝且零副作用。Windows 无法构造的场景标 skip |
| **S4** | `M1-FIX-006/007`(进程互斥 + POSIX/Windows OS 文件锁)**降级为债务**。不实现。在 `SECURITY.md` 与 `SRC` 中如实记录:当前只有进程内所有权,多进程并发未防护 | STUB | `application/runtime.py`(加 DEBT 标记), `SECURITY.md` | 一个测试证明:无有效 Permit 时 `acquire()` 零副作用(**注意现存缺陷:它会先建 `runtime-locks` 目录再校验**,必须改成先校验) |

### 阶段 M2 — 骨架(目标 5-7 h)

| # | 任务 | 深度 | 对应 PLAN | 要点 |
|---|---|---|---|---|
| **S5** | Context Capsule + Evidence Receipt:不可变、哈希绑定、含 revision | SKELETON | Task 18-19 | 只做构造与持久化;准入判定放 S6 |
| **S6** | Freshness Assessment + Task 候选晋升 | SKELETON | Task 20-21 | 主路径:依赖或 revision 变化 → stale → 拒绝进门禁、拒绝注入模型上下文。复杂 hazard 矩阵标 DEBT |
| **S7** | Run 候选冻结 + 终审 Grant + 唯一目标 CAS | SKELETON | Task 22-23 | 保证:只有冻结候选 + 一次性 Grant 才能 CAS;绝不 push |
| **S8** | 崩溃恢复对账:model / patch / ref 三条常见路径 | SKELETON | Task 24A-24C | 重启后与可观测状态对账再决定重试 |
| **S9** | 多意图 `INDETERMINATE` 集合与优先级消解 | **STUB** | Task 24D-24F | **只 fail closed**:遇到多意图集合直接落 `INDETERMINATE` 并停,不尝试消解。测试证明它不放行 |
| **S10** | 目标预留清理 + 无 Git 清除/墓碑 | SKELETON | Task 25A-25D | 幂等清理主路径 |
| **S11** | 保留分级 / 脱敏 / 驱逐 | **STUB** | Task 26A-26E | 接口存在,Tier 2 诊断一律不导出 |
| **S12** | **CLI**:`init` / `run` / `status` / `approve` / `doctor` | **REAL** | Task 27A-27C | 机制演示与交付物 3/4 依赖它。CLI 是唯一变更入口 |
| **S13** | 受限 Docker 执行器:非 root、digest 固定、无网络、无 docker socket | SKELETON | Task 29A-29D | 敌意容器containment 证明标 DEBT |
| **S14** | 验收 fixture:Python 金额单位漂移 + TypeScript 时间戳单位漂移 | SKELETON | Task 30-32 | 各一个最小仓库;消融实验标 DEBT |

### 阶段 M3 — 交付物(目标 2-3 h)

| # | 任务 | 深度 | 要点 |
|---|---|---|---|
| **S15** | **机制演示脚本**(课程 A.6 强制,三个行为必须确定性复现) | **REAL** | ① 护栏拦截一个危险动作 ② 注入失败 → 反馈闭环改变下一步动作 ③ 新鲜度判定(重点维度)。`make demo` 或 `python -m apexcrew.demo`,mock LLM,无网络 |
| **S16** | 脱敏静态回放导出 + 只读 WebUI | SKELETON | 只读、无命令/凭据/模型调用路径。README 必须写明它不是执行服务 |
| **S17** | 密钥扫描(跟踪树 + 可达历史) | REAL | `make secret-scan`;必须真跑一次并记录结果 |
| **S18** | 打包与 CI:`Dockerfile`、wheel、`.gitlab-ci.yml`(**必须含名为 `unit-test` 的 job**)、扩展 `.github/workflows/ci.yml` 增加构建步骤 | **REAL** | 交付物 3/6/7。CI 最后一次必须 pass |
| **S19** | `README.md` 六个必需章节 + `SECURITY.md` 更新 + **DEBT 清单**(`grep -rn "DEBT-" src/` 生成) | **REAL** | 章节:项目简介 / 安装 / 运行 / 分发命令 / 目录结构 / 安全边界。已知限制如实写 |

### 阶段 M4 — 收尾(目标 1-2 h)

| # | 任务 | 深度 | 要点 |
|---|---|---|---|
| **S20** | OpenAI Responses adapter | SKELETON | `ModelPort` seam 已存在,只做薄封装。**不发起真实调用、不碰凭据**。默认仍是 `ScriptedMockLLM` |
| **S21** | WebUI 部署产物就绪(静态构建产物 + 部署说明写进 README) | SKELETON | **实际启用 Pages / 部署是所有者动作**,本任务只把产物和文档准备好 |
| **S22** | 设计工作台最小版 | STUB | 文档形态即可 |

---

## 五、完成定义

全部满足才算这轮结束:

- [x] `uv run --python 3.12 pytest -q` 全绿,跳过项按平台注明
- [x] `mypy src` 零错误;`ruff check .` 与 `ruff format --check .` 通过
- [x] `make demo`(或等价脚本)可重复运行,确定性复现 A.6 三个行为
- [ ] `.gitlab-ci.yml` 含 `unit-test` job;GitHub Actions 最后一次运行 pass (job 已声明,同 SHA hosted run 待所有者授权 push/PR)
- [x] `README.md` 六个必需章节齐全,含 DEBT/已知限制清单
- [x] `grep -rn "DEBT-" src/` 的每一条在 README 或 `SECURITY.md` 里有对应记录
- [ ] 每个任务一个 commit,带完整 trailer;每模块一个 PR(**PR 由所有者推送和创建;本地任务提交与 trailers 已完成**)

## 六、说话的边界

做完这轮,可以说的是:

> **M1-M4 端到端可运行,harness 内核与治理机制有 mock-LLM 确定性测试;边缘情况、多意图恢复消解、保留分级、运行时 OS 锁标记为已知债务。**

**不可以说 M2-M4 完成,不可以说生产可用,不可以把 STUB 描述成已实现。** 诚实标注债务是加分项——本项目已有一次"独立评审推翻自己全绿交付"的记录,那才是可信度的来源。

---

## 七、任务台账

每完成一个任务,把该行改为 `DONE` 并填入观察到的完整 40 位 SHA。

| 任务 | 深度 | 状态 | 实现提交 |
| --- | --- | --- | --- |
| S1 FIX-005 预留清点 | REAL | DONE | `3ab39aa750f256a7abd735984cae055968fcde23` + correction `d54d6d06356a7f6f3a57e6d334f1185349ffdda9` |
| S2 M1-08 合入 main | REAL | DONE | `e72bfea48abb3c23075587f3f0a98eddcb2795c8` |
| S3 TOCTOU 句柄化修复 | REAL | DONE | `8b5d7f4b68a8865e28c37637587fc27fd8efb28b` + correction `3d10d04d3e7fbf3418e3efac351ea51e1b632d5b` |
| S4 运行时锁降级为债务 | STUB | DONE | `82513d489583779ab1436043598ea1eba5fe97f6` |
| S5 Context Capsule / Receipt | SKELETON | DONE | `815fe75b21757e413a90be1cd788019d8c284594` |
| S6 Freshness / 候选晋升 | SKELETON | DONE | `509def8b80c57f93c2e10600ca0a8a8c03ff09dc` |
| S7 Run 候选 + 终审 Grant | SKELETON | DONE | `6edfea409180c70e4bd5545440ba16d1b3afb534` |
| S8 崩溃恢复三路径 | SKELETON | DONE | `0c79762e9b22b091cc1374a5ab69f9c02887ec18` |
| S9 多意图 INDETERMINATE | STUB | DONE | `e284c2e6a1cb9abc288e4dc7dd2f4fd98b02a2d9` |
| S10 预留清理 / 清除 | SKELETON | DONE | `cb3ec0d67da5408e53ae0d27a1eb5de409b81f2b` |
| S11 保留分级 / 脱敏 | STUB | DONE | `e32927c82ed12cd775e326708311d06763a65659` |
| S12 CLI | REAL | DONE | `eab0ea4d29358c8f1fc60189deb9a633c8c367b7` |
| S13 受限 Docker 执行器 | SKELETON | DONE | `278c0077184107e819a96ffe3bf68b491a232103` |
| S14 双生态 fixture | SKELETON | DONE | `4d1c30ab397891cfaa0c3715545ad03330e17c54` |
| S15 机制演示 | REAL | DONE | `3d3894fa2a47b9bde190778eb70352a5dfdd69fd` + correction `ee15003` |
| S16 静态回放 + WebUI | SKELETON | DONE | `fafbd369019455087a529f54cdee1683d37f7389` |
| S17 密钥扫描 | REAL | DONE | `8793268462a3f47b1b244eabee6353690354b666` |
| S18 打包与 CI | REAL | DONE | `ed22d3cd3a7d733a945567150ab9d0dd2402ba56` + correction `7bec4386f7cf0b1d9f2948993f325486afe1c800` + acceptance `1fac0a38da0b51a5bb67950116c1585c58aa466b` |
| S19 README / SECURITY / DEBT | REAL | DONE | `9434715cd86fd42657fa3f11580faa7eb71b987e` |
| S20 OpenAI adapter | SKELETON | DONE | `2b71a8cbfee82814cb49b664b6452d55e4cb9ece` |
| S21 WebUI 部署产物 | SKELETON | DONE | `5feb82f8c21ef40331b4336e65dce7b307239bb4` |
| S22 设计工作台 | STUB | DONE | `aef591522bac228d0c9b2511b665f7b441de5282` |
