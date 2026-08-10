# ApexCrew

ApexCrew 是一个 local-first、evidence-driven 的 Coding Agent Harness。它在单个 Git 仓库中协调 Coordinator 和最多三个 Worker，把模型调用、工具执行、人工审批、验证证据和 Git 集成组织成可审计、可恢复、默认拒绝的持久化工作流。

项目不依赖外部 Coding Agent CLI 或高层 Agent Framework 来承担核心编排。Coordinator、WorkerLoop、授权、恢复和准入逻辑均由 ApexCrew 自身实现；模型供应商只通过窄 `ModelPort` 提供结构化补全能力。

> 当前定位：ApexCrew 已经拥有可运行的 Harness 内核和较完整的离线验证，但 R4.3 最终集成与同版本发布门禁仍在进行中。它是预发布工程项目，不是生产可用声明。

## 项目简介

普通 Coding Agent 容易把“模型生成了一段修改”误当成“修改已经安全完成”。ApexCrew 重点解决的是模型之外的工程控制问题：

- 谁可以启动或恢复一次 Run，旧命令能否重放。
- Worker 可以看到哪些文件、修改哪些路径、运行哪些检查。
- 模型输出、补丁、检查结果和 Git revision 是否属于同一个上下文。
- 进程崩溃后如何判断外部副作用是成功、失败还是无法确定。
- 风险操作如何绑定一次性人工授权，而不是获得宽泛权限。
- 最终候选如何在目标分支未移动且证据仍新鲜时只集成一次。

项目的核心原则是 **fail closed**：缺少权限、证据过期、状态冲突、外部结果不可观测或安全边界不完整时，Run 停止并保留可诊断状态，而不是猜测成功。

## 核心能力

| 能力 | 实现方式 |
| --- | --- |
| 自建多 Agent 编排 | `CoordinatorService` 负责规划与调度，`WorkerLoopService` 负责模型-工具-反馈循环 |
| 稳定公共接口 | `CrewControl.handle`、`CrewRuntime.run_until_blocked`、`RunQueries.get` 组成 A-Hybrid 应用表面 |
| 持久状态与审计 | SQLite 记录 Run、Task、Attempt、Intent、Permit、Grant、Evidence 和单调 Audit sequence |
| 防重放运行权限 | 一次性 Runtime Permit 绑定 Run、阶段、revision 和预期 sequence，消费后不能复用 |
| 风险动作审批 | Action Policy 将动作分类；高风险动作需要绑定精确前置状态的一次性 Grant |
| Worker 隔离 | lease 约束可写范围；Context Capsule 只包含批准的 `R union D`；检查工作区使用独立的 `Q union W` |
| 客观验证 | Evidence Receipt、Evidence Bundle 和 Freshness Assessment 将检查结果绑定到精确 revision |
| 安全 Git 集成 | Target Reservation、私有 Run Head、Task Candidate、Run Candidate 和 typed CAS 分离中间提交与最终目标更新 |
| 崩溃恢复 | 先记录 Intent，再执行外部效果，再按可观测后置状态结算；不确定结果进入 `INDETERMINATE` |
| 受限执行 | Git 使用结构化 `argv`；检查通过 digest-pinned、无网络、无 Docker socket 的受限执行器运行 |
| 模型边界 | 离线测试使用 `ScriptedMockLLM`；真实适配器限定为 DeepSeek Responses API |
| 只读交付 | CLI 是唯一写控制面；FastAPI WebUI 和静态 replay 只消费脱敏后的 `RunQueries` 投影 |

## 架构

```text
User
  |
  v
Typer CLI -------------------------------> credentials / doctor
  |
  v
CrewControl.handle(CommandEnvelope)
  |  validates command, revision and expected sequence
  |  persists one-use Runtime Permit or Pending Action
  v
CrewRuntime.run_until_blocked(run_id)
  |
  +--> CoordinatorService
  |      planning -> task scheduling -> attempt creation
  |
  +--> WorkerLoopService
  |      context -> model action -> authority -> tool result -> feedback
  |
  +--> Admission
         evidence/freshness -> private Run Head -> frozen Run Candidate -> target CAS

Adapters
  +--> SQLite state and audit journal
  +--> sanitized Git argv and no-follow filesystem handles
  +--> restricted Docker executor
  +--> ScriptedMockLLM / DeepSeek Responses

RunQueries.get(run_id)
  +--> CLI show
  +--> read-only FastAPI WebUI
  +--> sanitized static replay
```

### 三个公共接口

- `CrewControl` 接收类型化命令，只负责校验、持久化请求和签发内部 Permit，不直接执行 Coordinator 或 Worker。
- `CrewRuntime` 消费有效 Permit，取得每 Run 所有权并运行到下一个阻塞点，不暴露任意 tick/step 后门。
- `RunQueries` 从审计状态构造脱敏只读投影，不具备命令、凭据或模型调用能力。

这种形状把“请求变更”“执行副作用”和“读取状态”拆开，便于证明重放安全、并发冲突和只读交付边界。

### 一次 Worker 循环

1. Coordinator 从已批准 Plan 中选择可调度 Task，并创建带 generation 的 workspace lease。
2. Worker 从允许读取的文件构建有界 Context Capsule，并绑定依赖与 revision digest。
3. ModelPort 返回一个结构化动作；未知字段、多动作或不符合 schema 的输出被拒绝。
4. Authority 按 Policy、lease、预算、deadline、前置状态和 Grant 判断动作。
5. Tool Runtime 执行 read/search/patch/check；失败结果作为结构化 tool feedback 注入下一轮。
6. 只有通过声明检查并形成新鲜 Evidence Bundle 的 Attempt 才能成为 Task Candidate。
7. Task Candidate 先推进私有 Run Head；所有 Task 完成后另行冻结 Run Candidate。
8. 最终目标 ref 只能通过 Admission 签发、Grant 绑定的一次 typed compare-and-swap 更新。

## 安装

### 环境要求

- CPython 3.12
- `uv`
- Git
- Docker daemon，仅在构建或运行受限执行器时需要
- Windows 11 或 Ubuntu 24.04 x86_64 是当前验证目标

```powershell
uv sync --frozen --all-groups
uv run --python 3.12 apexcrew --help
```

离线测试、demo、静态 WebUI 和普通只读命令不需要模型凭据。

## 运行

### 确认 CLI 和离线演示

```powershell
uv run --python 3.12 apexcrew --help
uv run --python 3.12 python -m apexcrew.demo
```

`apexcrew.demo` 使用确定性的 `ScriptedMockLLM`，演示危险动作拦截、失败检查反馈和 revision freshness，不访问网络或凭据。

### CLI 生命周期

CLI 当前公开以下类型化命令：

```text
init
run-create
approve-policy / approve-budget / approve-model
begin-planning
approve-plan
start
run
show
grant
integrate
reconcile-cleanup
status / doctor / credentials
```

典型流程是：初始化本地配置，创建 DRAFT Run，批准三类 revision，开始规划并批准 Plan，启动 Worker，使用 `show` 检查精确 digest 和 Pending Action，按需签发 Grant，冻结并集成候选，最后对终态 Run 执行精确 Target Reservation cleanup。

`run-create` 本身不会调用模型。没有当前 Runtime Permit、revision 不匹配、旧请求重放或运行所有权冲突时，`run` 必须零副作用停止。

### 凭据安全配置

真实 DeepSeek 调用只允许出现在显式开启的 live smoke 中：

```powershell
uv run --python 3.12 apexcrew credentials set
uv run --python 3.12 apexcrew credentials status
uv run --python 3.12 apexcrew credentials clear
uv run --python 3.12 apexcrew doctor
```

凭据优先从操作系统 keyring 的 service `apexcrew`、account `model-credential-deepseek` 读取；无交互 CI 可以使用唯一允许的环境变量 `APEXCREW_DEEPSEEK_API_KEY`。仓库 `.env` 文件不会被加载，因为目标仓库及其脚本属于不可信输入。

凭据在请求时读取，不缓存到对象、不进入日志、`repr`、模型 payload、子进程环境或受限执行器。缺失凭据返回 `MODEL_CREDENTIAL_MISSING`，不会退化成未认证请求。

显式授权一次真实 smoke：

```powershell
$env:APEXCREW_LIVE_SMOKE="1"
make live-smoke
```

该测试默认跳过，不属于 `make test` 或普通 CI。它最多发送一次请求，且不打印 prompt 或 credential。

## 分发命令

```powershell
make test          # deterministic offline suite
make coverage      # terminal and XML coverage
make lint          # Ruff format/check + strict mypy
make demo          # deterministic mechanism trace
make secret-scan   # tracked tree + reachable Git history
make web-build     # static read-only replay
make build         # wheel + local executor image
```

测试按风险分层：

- `tests/unit/`：路径语法、授权、预算、revision、Worker loop 等纯领域行为。
- `tests/contract/`：状态存储、模型适配器、CLI、composition 和发布契约。
- `tests/integration/`：临时 Git 仓库、SQLite、Permit/Grant race、no-follow 路径、恢复与 CAS。
- `tests/acceptance/`：Python 金额单位漂移和 TypeScript 时间戳单位漂移 fixture。

截至 2026-08-10，最近一个完成规格与质量评审的 R4.3 基线 `58609e8` 收集了 749 个测试，全量 suite 退出码为 0；66 个源码文件通过 strict mypy，Ruff、格式和 diff 检查通过。这是该独立任务分支的观测证据，不代表当前脏 checkout、远端 `main`、尚未评审的 R4.3-04/05 或 hosted release 已通过。

## 目录结构

```text
src/apexcrew/
  domain/          领域状态、权限、证据、Coordinator、WorkerLoop
  application/     CrewControl、CrewRuntime、RunQueries 与 composition
  adapters/        SQLite、Git、filesystem、Docker、model、credentials
  delivery/        Typer CLI 和只读 FastAPI WebUI
tests/
  unit/ contract/ integration/ acceptance/
fixtures/
  python-money/ typescript-time/
docs/
  adr/ architecture/ experiments/ learning/ proposals/ research/
scripts/            secret scan 与静态 WebUI 构建
webui/              只读 replay 资源
```

根目录中的 `SPEC.md` 是规范真相源；`PLAN.md` 记录执行权限和任务账本；`SPEC_PROCESS.md` 与 `AGENT_LOG.md` 保存评审、red/green 和纠正证据。

## 安全边界

- CLI 是唯一可变更控制面；WebUI、Pages replay 和 `RunQueries` 全部只读。
- 模型不能选择任意 shell；仓库命令以封闭类型转换为结构化 `argv`。
- workspace escape、绝对路径、`..`、反斜杠、NTFS ADS、symlink、submodule 和 secret path 默认拒绝。
- 未批准的网络、host filesystem、Docker socket、push 和 destructive Git 是硬拒绝。
- 风险动作必须匹配一个未消费 Grant 的动作 digest、路径、前置状态和 revision。
- Git 目标分支不会被 Worker 直接操作；Admission 独占候选准备和 typed CAS。
- 外部效果采用 intent-before-effect，恢复时只根据权威可观测状态结算。
- 不可确定结果保留为 `INDETERMINATE`，不会通过自动重试伪造 exactly-once。

完整信任边界和凭据策略见 [SECURITY.md](SECURITY.md)。

The read-only WebUI is not an execution service.

## 当前状态

更新于 2026-08-10：

| 范围 | 状态 | 可宣称内容 |
| --- | --- | --- |
| SPEC revision 3 / Stage 4 | 完成 | 规范已签署，冷启动审查已通过 |
| M1-M4 Sprint baseline | 完成但深度混合 | REAL、SKELETON、STUB 按 `SPRINT.md` 边界交付 |
| R4.3-00 ~ R4.3-03 | 完成双评审 | 真实 demo loop、scoped workspace、patch/context、restricted executor composition 已在顺序任务分支验证 |
| R4.3-04 | 实现已绿，评审待完成 | Task Candidate 准备和私有 Run Head 推进尚不能计入门禁完成 |
| R4.3-05 | 实现中 | Run Candidate freeze 和最终 target CAS 仍有静态检查问题 |
| R4.3-06 / R4.3-07 | 未开始 | acceptance/purge 与 same-revision release 尚未闭环 |
| 远端交付 | 未同步到最终状态 | R4.3 本地分支尚未形成对应远端 PR；旧 PR 的绿色 CI 不覆盖当前增量 |

因此，当前可以描述为“具有持久化治理和确定性测试的 Coding Agent Harness 预发布实现”，不能描述为“完整 v0.1”“生产可用”或“已经完成真实线上发布”。

## 已知限制

- R4.3-04 必须先完成独立 SPEC review、quality review 和账本关闭，后续任务才能成为权威增量。
- R4.3-05 到 R4.3-07 尚未完成最终候选集成、retention purge 和同一 SHA 发布证明。
- `DEBT-M2-001`：当前 checkout 的多意图 precedence 仍有 fail-closed 边界，无法唯一权威判定时保持 `INDETERMINATE`。
- `DEBT-M2-002`：Tier 2 diagnostic export 在当前 checkout 中保持禁用。
- `DEBT-M2-003`：retention-tier export 在当前 checkout 中保持不可用。
- `DEBT-M2-004`：durable retention eviction 仍需要最终 tombstone/purge 闭环。
- `DEBT-M2-005` 只有在真实受限 Docker 进程被观测且文档同步后才能关闭；daemon/image 不可用仍按类型化不确定结果停止。
- demo 中 malformed unified diff 暂时复用 fail-closed 的 scope denial 结果；扩展结果枚举需要独立 SPEC 修订。
- live DeepSeek smoke、GitHub Pages 启用、push、merge 和 package publication 都是 owner-only 外部动作。
- v0.1 只支持一个本地仓库、一个用户和最多三个 ApexCrew Worker，不支持分布式队列或多租户服务。
- 带 linked worktree、外部 Git storage、alternates、grafts、sparse/split index、shallow/partial history 的目标仓库在 v0.1 中拒绝进入。

## 开发流程

每个独立 feature 或大模块使用一个 Git worktree 和一个对应 PR。每个任务执行 vertical TDD：先观察 red，再做最小 green，然后运行 strict mypy、Ruff 和 diff check；随后依次进行 SPEC compliance review 和 code quality review，修复 Critical/High 后更新 `PLAN.md` 与 `AGENT_LOG.md`。

提交使用 Conventional Commits，并在正文中记录：

```text
PLAN-Task: <task-id>
Subagent: <agent-id>
Human-Changes: <description-or-none>
Spec-Review: <review-id-and-verdict>
Quality-Review: <review-id-and-verdict>
```

未经仓库所有者明确授权不得 push。

## 项目文档

- [SPEC.md](SPEC.md)：冻结的产品、安全和验收规范。
- [PLAN.md](PLAN.md)：里程碑、任务设计、执行权限和 commit ledger。
- [SPEC_PROCESS.md](SPEC_PROCESS.md)：规范讨论、修订、冷启动和独立评审记录。
- [AGENT_LOG.md](AGENT_LOG.md)：任务 red/green、评审与纠正证据。
- [SPRINT.md](SPRINT.md)：M1-M4 混合深度历史交付边界。
- [SECURITY.md](SECURITY.md)：信任边界、凭据、执行器和运行债务。
- [CONTEXT.md](CONTEXT.md)：项目统一领域语言。
- [`docs/architecture/`](docs/architecture/README.md)：源码映射的架构、运行机制、安全边界与项目伪代码；语义从属于 SPEC。
- `docs/adr/`：不可轻易逆转的设计决策。
- `docs/research/`：Agent Harness 和竞品研究。
- `docs/experiments/`：可证伪实验与 fixture 设计。

## License

ApexCrew 原创内容以 [Apache License 2.0](LICENSE) 发布。课程提供材料的例外范围见 [NOTICE](NOTICE)。
