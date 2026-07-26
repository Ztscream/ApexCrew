# GitHub Coding-Agent Landscape 与 ApexCrew 方向建议

> 调研快照：2026-07-25。事实仅取自项目官方 README、文档、源码和测试；“未见”表示本轮一手材料中没有找到明确承诺，不等于不存在。GitHub 项目变化很快，进入 `SPEC.md` 前应复核关键链接。

> **决策状态（2026-07-26）**：本报告是竞争研究，不是产品方向的事实来源。用户已接受 [Evidence-Driven Durable Crew](../adr/0001-evidence-driven-durable-crew.md) 作为唯一主线。下文的 Adversarial Acceptance Crew 是研究提出、但未被采纳为主线的备选；其弱 oracle / challenger 思路仅作为证据质量的辅助实验。Bernstein 的重叠也意味着 ApexCrew 不得把 durability、worktree、mutation gate 或 evidence lineage 的单项能力宣称为首创。

## 结论先行

“多 Agent + 长上下文 + 持续 cowork”本身已经不是差异化定位：OpenHands 已定义可接多种 Agent 的 **always-on engineering team**；Vibe Kanban、Superset、Daintree、dmux 都已管理异构 Agent 与隔离 worktree；Codex 源码已有 subagent 与 thread resume；h5i 已做 context audit、独立实现、交叉评审和中立 sandbox 验证；Bernstein 更直接覆盖确定性调度、40+ CLI adapter、worktree、测试门禁、context capsule、跨机器 durable ledger、replay、lineage 和 chaos tests。[OpenHands](https://github.com/OpenHands/OpenHands/blob/main/README.md) · [Superset](https://github.com/superset-sh/superset/blob/main/README.md) · [h5i](https://github.com/h5i-dev/h5i/blob/main/README.md) · [Bernstein](https://github.com/sipyourdrink-ltd/bernstein/blob/main/README.md) · [Bernstein chaos tests](https://github.com/sipyourdrink-ltd/bernstein/tree/main/tests/chaos)

研究阶段曾建议把 ApexCrew 改为以下备选定位，但该建议未被采纳：

> **面向 Coding Agent 团队的对抗式验收 Harness：builder、challenger、verifier 围绕可执行 Acceptance Contract 协作，用差分测试、性质测试和 mutation testing 主动寻找“现有测试全绿但实现仍错误”的补丁，并只把仍对当前 revision 有效的已验证事实带入长期上下文。**

ApexCrew 的课程评分核心仍自己实现两层循环：**crew loop** 驱动“提出契约 → 实现 → 挑战 → 验证 → 修订”，**worker loop** 负责组织上下文、单次调用模型、解析动作、执行工具、回灌反馈和停机。Codex/Claude Code/Gemini CLI 或 ACP Agent 只能作为后续实验 adapter，不能替代 worker loop；暂无 LLM key 时以 `ScriptedMockLLM` 完成全部离线机制验证。

## 比较口径

- **Loop 归属**：谁决定下一动作，是产品本身、外部 Coding Agent，还是应用开发者。
- **长上下文**：区分窗口压缩、检索记忆、跨会话持久化；三者不是同一能力。
- **持续性**：区分 UI 重连、session 恢复、进程崩溃恢复和整个多 Agent 任务图恢复。
- **治理**：区分展示确认框、规则化 allow/deny，以及审批是否绑定不可变动作。
- **客观反馈**：区分框架自身单测、LLM 评分，以及目标仓库测试是否成为完成门禁。

## 直接竞品：Coding Agent 与上层 Harness

| 项目 | 定位与 loop 归属 | 协作、上下文与恢复 | 隔离、HITL 与客观反馈 | 对 ApexCrew 的含义 |
|---|---|---|---|---|
| **OpenHands** | Agent Canvas 是自托管控制中心，能运行多个本地/远程/cloud backend；Canvas 编排会话，具体 loop 可由 OpenHands Agent 或第三方 ACP Agent 拥有。[README](https://github.com/OpenHands/OpenHands/blob/main/README.md) | SDK 有 LLM summary condenser、磁盘 conversation、两级 `MEMORY.md`、并行 subagent 压测；ACP backend 不可原生恢复时还能从 durable event 重放 bootstrap transcript。[condenser](https://github.com/OpenHands/software-agent-sdk/blob/main/examples/01_standalone_sdk/14_context_condenser.py) · [persistent memory](https://github.com/OpenHands/software-agent-sdk/blob/main/examples/01_standalone_sdk/55_persistent_memory.py) · [resume transcript](https://github.com/OpenHands/software-agent-sdk/blob/main/openhands-sdk/openhands/sdk/event/resume_transcript.py) · [parallel test](https://github.com/OpenHands/software-agent-sdk/blob/main/tests/agent_server/stress/test_parallel_subagents.py) | 支持 Docker/VM；`ConfirmationPolicy` 有 always/never/risk-threshold；测试分 unit/integration/stress。[confirmation policy](https://github.com/OpenHands/software-agent-sdk/blob/main/openhands-sdk/openhands/sdk/security/confirmation_policy.py) · [tests](https://github.com/OpenHands/software-agent-sdk/blob/main/tests/README.md) | **最直接竞品之一**。不能再宣称“首个 always-on、多 backend 的 Coding Agent 控制台”。本轮材料未见“每次跨 Agent 交接的证据契约 + revision 验证门禁”。 |
| **SWE-agent** | 面向 GitHub issue/SWE-bench 的自主修复；`Agent.forward()` 负责提示、解析动作并经 SWE-ReX shell 执行，是单任务 loop。[README](https://github.com/princeton-nlp/SWE-agent/blob/main/README.md) · [architecture](https://github.com/princeton-nlp/SWE-agent/blob/main/docs/background/architecture.md) | 全历史经 `HistoryProcessor` 压缩；有 RetryAgent 和 trajectory replay，但 replay 是轨迹复现证据，不等同于多 Agent 任务图的跨崩溃续跑。[agents.py](https://github.com/princeton-nlp/SWE-agent/blob/main/sweagent/agent/agents.py) · [replay test](https://github.com/princeton-nlp/SWE-agent/blob/main/tests/test_run_replay.py) | 环境隔离和 SWE-bench 是强客观反馈；提供 human model 配置。未见 worktree 协作与动作审批账本。[batch benchmark](https://github.com/princeton-nlp/SWE-agent/blob/main/README.md#running-on-batches) · [human config](https://github.com/princeton-nlp/SWE-agent/blob/main/config/human/human.yaml) | 可借鉴 trajectory、可复现实验和 issue-level oracle；不是异构 Agent 协作控制面。 |
| **Aider** | 终端 pair programmer，自有单 Agent 编辑 loop。[README](https://github.com/Aider-AI/aider/blob/main/README.md) | Repo map 在 token budget 内挑选全仓符号上下文；聊天可清理、保存重建命令，但不是 durable crew state。[repo map](https://github.com/Aider-AI/aider/blob/main/aider/website/docs/repomap.md) · [commands](https://github.com/Aider-AI/aider/blob/main/aider/website/docs/usage/commands.md) | 每次编辑可自动 commit，支持 undo；lint/test 失败会回灌并尝试修复。[Git integration](https://github.com/Aider-AI/aider/blob/main/aider/website/docs/git.md) · [lint/test](https://github.com/Aider-AI/aider/blob/main/aider/website/docs/usage/lint-test.md) | “修改后测试自修正”已有成熟实现；ApexCrew 的新意必须落在多 Agent 边界、恢复与证据，而不是这一闭环本身。 |
| **goose** | 本地通用 Agent，提供 Desktop/CLI/API；内部拥有 loop，也可通过 ACP 使用订阅型 provider、通过 MCP 接工具。[README](https://github.com/block/goose/blob/main/README.md) | 源码有 structured compaction、session 导入导出/恢复和有 turn 上限的 subagent task。[structured context](https://github.com/block/goose/blob/main/crates/goose/src/context_mgmt/structured.rs) · [sessions](https://github.com/block/goose/blob/main/crates/goose-cli/src/commands/session.rs) · [subagent config](https://github.com/block/goose/blob/main/crates/goose/src/agents/subagent_task_config.rs) | 工具权限分 `AlwaysAllow / AskBefore / NeverAllow`；仓库有录制 provider 场景测试，但本轮未见 per-task worktree 与 merge evidence gate。[permission](https://github.com/block/goose/blob/main/crates/goose/src/config/permission.rs) · [scenario tests](https://github.com/block/goose/tree/main/crates/goose-cli/src/scenario_tests) | 证明“不提供 API key，复用本机 Agent 配置”可行；也说明 session、压缩、subagent 不是空白。 |
| **OpenAI Codex** | README 称轻量终端 Coding Agent；核心源码拥有 loop。[README](https://github.com/openai/codex/blob/main/README.md) | 源码暴露 spawn/message/follow-up/wait/resume/list 等多 Agent 工具；thread API 支持 resume/fork/rollback/compact，且有 cold-root resume 恢复 worker identity 的 mock 集成测试。[multi-agent spec](https://github.com/openai/codex/blob/main/codex-rs/core/src/tools/handlers/multi_agents_spec.rs) · [thread protocol](https://github.com/openai/codex/blob/main/codex-rs/app-server-protocol/src/protocol/v2/thread.rs) · [resume test](https://github.com/openai/codex/blob/main/codex-rs/core/tests/suite/multi_agent_resume.rs) | 有 workspace root、sandbox、命令/补丁/权限审批及集中 reviewer routing；大量 mock server integration tests。[approvals](https://github.com/openai/codex/blob/main/codex-rs/core/src/tools/approvals.rs) · [compaction](https://github.com/openai/codex/blob/main/codex-rs/core/src/compact.rs) | “多 Agent + resume + approval”已直接撞车。ApexCrew 必须以低层模型 API 自研 WorkerLoop；Codex 只能用于开发过程或核心完成后的非评分实验。 |
| **Vibe Kanban** | 规划和审查 Coding Agent 的控制台；明确支持切换多种 Agent，loop 留在 Claude Code/Codex/Gemini 等 executor 中。[README](https://github.com/BloopAI/vibe-kanban/blob/main/README.md) | 每个 task attempt 是一次 Agent session，可换 Agent/base branch；每个 attempt 建独立临时 worktree，支持 subtasks 和多次尝试。[attempts](https://github.com/BloopAI/vibe-kanban/blob/main/docs/core-features/new-task-attempts.mdx) · [execution](https://github.com/BloopAI/vibe-kanban/blob/main/docs/core-features/monitoring-task-execution.mdx) | 实时日志、diff inline review、setup/cleanup script；文档当前明确 action approval 支持 Codex，Claude Code “coming soon”。[review](https://github.com/BloopAI/vibe-kanban/blob/main/docs/core-features/reviewing-code-changes.mdx) · [approval](https://github.com/BloopAI/vibe-kanban/blob/main/docs/core-features/monitoring-task-execution.mdx#4-action-approvals) | **另一最直接竞品**。单纯做看板、外部 Agent launcher 或 worktree 管理没有新意；应强化自动协作协议、崩溃一致性和可验证交接。 |
| **Superset / Daintree / dmux** | 三者都把内部 loop 留给外部 CLI Agent，并提供并行 worktree 控制面；Superset 还有 persistent terminal、diff 和 schedule automation，Daintree 有权限层/audit/idempotency，dmux 提供 lifecycle hooks 与 merge/PR。[Superset](https://github.com/superset-sh/superset/blob/main/README.md) · [Daintree](https://github.com/daintreehq/daintree/blob/main/README.md) · [dmux](https://github.com/standardagents/dmux/blob/main/README.md) | 重点是多终端、多 workspace 的操作效率；Daintree 能做 structured context injection。[Daintree features](https://github.com/daintreehq/daintree/blob/main/README.md#features) | 已具备 diff/review/merge 或 action authorization；官方 README 未把增强目标仓库 oracle 设为主线。 | UI、并行 launcher、worktree、通知和 schedule 都是拥挤赛道。 |
| **h5i** | Auditable workspace：每个 Agent 有 sandboxed worktree，Git 中记录 prompt、commands、logs、policies、reviews。[README](https://github.com/h5i-dev/h5i/blob/main/README.md) | 持久 context/memory；Orchestra 示例让 Claude/Codex 独立实现、冻结互不影响的 round、交叉 review。[orchestra](https://github.com/h5i-dev/h5i/blob/main/README.md#25-programmable-multi-agent-orchestration) | 在 fresh neutral sandbox 跑 pytest，并从通过者中选最小 diff；另有 prompt/context audit。[context audit](https://github.com/h5i-dev/h5i/blob/main/README.md#22-track-prompts-and-contexts) | “独立候选 + peer review + neutral verification”已被占据。可追问的是：pytest 本身是否足以发现错误，memory 中的事实何时失效。 |
| **Bernstein** | 确定性异构 CLI Agent orchestrator；协调 loop 不用 LLM，支持 40+ adapter、per-task worktree 与 merge gates。[README](https://github.com/sipyourdrink-ltd/bernstein/blob/main/README.md) | 官方列出 replay journal、artifact lineage、tamper-evident memory、compaction receipt、hash-bound context capsule 和可跨机器恢复的 work ledger。[capabilities](https://github.com/sipyourdrink-ltd/bernstein/blob/main/README.md#full-capabilities) | 有 audit/approval、lint/type/test gate、fake adapter、WAL crash recovery 和 chaos suite。[chaos](https://github.com/sipyourdrink-ltd/bernstein/tree/main/tests/chaos) · [approval test](https://github.com/sipyourdrink-ltd/bernstein/blob/main/tests/integration/test_approval_workflow_e2e.py) · [WAL test](https://github.com/sipyourdrink-ltd/bernstein/blob/main/tests/integration/storage/test_wal_crash_recovery.py) | **与原“durable evidence crew”几乎正面重合**。官方 limitation 明确承认验证质量取决于目标项目已有 checks；这是更可信的切入点。[limitation](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/reference/KNOWN_LIMITATIONS.md#5-verification-quality-depends-on-project-quality) |
| **Ruflo / Claude Flow** | 自称 Claude Code/Codex 上层 meta-harness，拥有 swarm/router/memory/background loop，而底层模型 Agent 负责具体工作。[README](https://github.com/ruvnet/claude-flow/blob/main/README.md) | 官方状态页列出长驻 agent、共享向量记忆、swarm 与联邦；team gateway 规定跨平台共享 namespace。[status](https://github.com/ruvnet/claude-flow/blob/main/docs/STATUS.md) · [gateway](https://github.com/ruvnet/claude-flow/blob/main/docs/TEAM-GATEWAY-CHECKLIST.md) | 有大规模自身测试、加密/安全审计、per-merge signed witness；同时文档承认 shared namespace 默认没有访问控制、部分 per-tool witness/smoke 尚 pending。[status](https://github.com/ruvnet/claude-flow/blob/main/docs/STATUS.md#whats-next) · [gateway](https://github.com/ruvnet/claude-flow/blob/main/docs/TEAM-GATEWAY-CHECKLIST.md#3-memory-namespace-sharing) | 不能宣称“首个 swarm/memory/audit harness”；应研究已记录事实的**语义有效性**与目标程序 oracle，而非再做 provenance ledger。 |
| **Continue** | 曾提供 CLI、VS Code、JetBrains Coding Agent；官方 README 现明确仓库只读且不再活跃维护。[README](https://github.com/continuedev/continue/blob/main/README.md) | 源码留下丰富 context provider/MCP/代码检索能力，但当前不宜作为长期基础依赖。[context source](https://github.com/continuedev/continue/tree/main/core/context) | 有完整工程测试资产；没有当前维护承诺。[testing guide](https://github.com/continuedev/continue/blob/main/TESTING.md) | 可作为历史界面/上下文设计参考，不应作为 ApexCrew 核心 adapter 的首选。 |

## 相邻竞品：多 Agent、持久状态与记忆框架

| 项目 | 已覆盖能力 | 关键边界/空白 |
|---|---|---|
| **AutoGen** | 多 Agent application framework，支持 AgentTool、group chat、Studio 和 AutoGen Bench。[README](https://github.com/microsoft/autogen/blob/main/README.md) | 官方已将 AutoGen 标为 maintenance mode，并推荐 Microsoft Agent Framework；它提供编排积木，不提供 Git worktree、代码 merge 和 revision 验收的不变量。[README maintenance notice](https://github.com/microsoft/autogen/blob/main/README.md#autogen) |
| **CrewAI** | Crews/Flows 拥有编排；checkpoint 捕获配置、memory、任务进度、输出和事件，可 resume/fork；统一 memory 支持共享/私有 scope；支持 HITL。[checkpointing](https://github.com/crewAIInc/crewAI/blob/main/docs/edge/en/concepts/checkpointing.mdx) · [memory](https://github.com/crewAIInc/crewAI/blob/main/docs/edge/en/concepts/memory.mdx) · [HITL](https://github.com/crewAIInc/crewAI/blob/main/docs/edge/en/learn/human-in-the-loop.mdx) | 通用工作流而非 repo harness；内置 `crewai test` 是多次运行与模型评分，官方文档当前称测试 provider 仅 OpenAI，不等同于目标仓库的离线验收门禁。[testing](https://github.com/crewAIInc/crewAI/blob/main/docs/edge/en/concepts/testing.mdx) |
| **MetaGPT** | 以软件公司角色/SOP 组织 Agent；示例含 coder/tester/reviewer/human，Role 有可恢复的 long-term memory。[README](https://github.com/FoundationAgents/MetaGPT/blob/main/README.md) · [custom team](https://github.com/FoundationAgents/MetaGPT/blob/main/examples/build_customized_multi_agents.py) · [long-term memory](https://github.com/FoundationAgents/MetaGPT/blob/main/metagpt/memory/longterm_memory.py) | 角色流水线和 memory 已有；本轮材料未见异构 CLI session、per-agent worktree、动作审批与崩溃一致性的统一实现。 |
| **ChatDev 2.0** | 已从虚拟软件公司变为零代码多 Agent workflow；YAML/画布定义图，含 Human node、多种共享/跨 session memory。[README](https://github.com/OpenBMB/ChatDev/blob/main/README.md) · [memory](https://github.com/OpenBMB/ChatDev/blob/main/docs/user_guide/en/modules/memory.md) · [human node](https://github.com/OpenBMB/ChatDev/blob/main/docs/user_guide/en/nodes/human.md) | 主线是通用 workflow；当前 `WorkflowSessionStore` 是进程内字典和有限 replay buffer，不能据此宣称进程崩溃后恢复整个 workflow。[session store](https://github.com/OpenBMB/ChatDev/blob/main/server/services/session_store.py) |
| **LangGraph** | 低层 stateful orchestration，官方承诺 durable execution、精确恢复、HITL、短期/长期 memory。[README](https://github.com/langchain-ai/langgraph/blob/main/README.md) | 它解决通用状态机持久化，不规定代码工作区、Git 隔离、审批绑定或测试验收；若 ApexCrew直接使用它，A 类“自研 agent loop”贡献需要谨慎论证。 |
| **Letta** | Stateful Agent 平台；当前 Agent/SDK 支持 memory、subagent、local/cloud backend 和 `resumeSession`。[README](https://github.com/letta-ai/letta/blob/main/README.md) | 强项是 Agent 内记忆与持续学习，不是多 worktree 的代码协作/集成治理；“长期记忆”不能作为 ApexCrew 单独卖点。 |

## 协议不是产品：MCP、A2A、ACP 的正确位置

| 协议 | 明确解决什么 | 不解决什么 | ApexCrew 用法 |
|---|---|---|---|
| **MCP** | Host-client-server 的 JSON-RPC 协议；标准化 tools/resources/prompts、sampling、elicitation 与通知。[architecture](https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/docs/docs/learn/architecture.mdx) | 不规定 Agent 内部 loop 或多 Agent 任务分解。Tasks 能提供 crash-resilient handle、轮询、`input_required`，但当前官方页面仍标为 experimental extension。[Tasks](https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/docs/extensions/tasks/overview.mdx) | MVP 仅把工具/只读状态暴露为 MCP；不要把核心 durable crew state 寄托于实验扩展。 |
| **A2A** | 让不暴露内部 memory/tools 的远程 Agent 通过 Agent Card、Message、Task、Artifact 协作；`contextId` 可包含并行 task。[README](https://github.com/a2aproject/A2A/blob/main/README.md) · [task lifecycle](https://github.com/a2aproject/A2A/blob/main/docs/topics/life-of-a-task.md) | 协议明确把 artifact mutation/version linkage 留给 client；也不提供本地 Git/worktree 和完成 oracle。[artifact tracking](https://github.com/a2aproject/A2A/blob/main/docs/topics/life-of-a-task.md#tracking-artifact-mutation) | 作为远程 Agent 的未来 adapter；MVP 不做 A2A server，以免引入 auth、网络和分布式故障。 |
| **ACP** | 标准化 editor/client 与 Coding Agent；同一连接可有并发 session，支持 tool progress、permission request、filesystem、`session/load` 与无历史重放的 `session/resume`。[architecture](https://github.com/agentclientprotocol/agent-client-protocol/blob/main/docs/get-started/architecture.mdx) · [session setup](https://github.com/agentclientprotocol/agent-client-protocol/blob/main/docs/protocol/v1/session-setup.mdx) · [tool calls](https://github.com/agentclientprotocol/agent-client-protocol/blob/main/docs/protocol/v1/tool-calls.mdx) | 它不定义 Agent-to-Agent 任务图、memory 质量、worktree 策略或客观验收；有效 root 只是 tool 操作的 `SHOULD` boundary，仍需 Harness 强制执行。[workspace roots](https://github.com/agentclientprotocol/agent-client-protocol/blob/main/docs/protocol/v1/session-setup.mdx#working-directory) | 仅作为核心完成后的实验性外部 Agent adapter；评分核心通过低层 `ModelPort` 调用单次模型响应，并由 ApexCrew 自己拥有 worker loop。 |

## 仍有价值的空白

Bernstein/h5i 的补充材料说明，worktree、replay、lineage、context capsule、chaos、peer review 和 neutral test gate 都不能再作为主要新意。更可信的空白在“证据是否真的说明实现正确”：

1. **弱 oracle 问题**：测试绿只说明通过现有测试。Bernstein 官方也明确承认 verification quality 取决于项目 checks；h5i 的示例同样以 pytest 作为择优依据。[Bernstein limitation](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/reference/KNOWN_LIMITATIONS.md#5-verification-quality-depends-on-project-quality) · [h5i verify](https://github.com/h5i-dev/h5i/blob/main/README.md#25-programmable-multi-agent-orchestration)
2. **验收契约会被“自证”**：同一个 Agent 同时写实现和测试，可能让测试适配错误实现。需要独立 challenger 产生反例，并用 baseline/candidate 对照、性质测试和 mutation score 约束测试质量。
3. **provenance 不等于 truth**：hash/签名能证明某条 memory 没被篡改，却不能证明它对当前 commit 仍正确。长期 cowork 需要 dependency-aware invalidation 和 re-validation。
4. **HITL 多停在 tool permission**：真正高价值的人工节点是先批准“什么行为算完成”，再让 Agent 工作；否则用户只是在逐条批准命令，却没有掌控验收语义。
5. **跨 Agent 反馈缺少反例资产**：失败命令常被当作一次性文本回灌。可把最小反例、回归测试、被杀死的 mutant 与适用 revision 变成可查询、可失效的长期证据。

## 三个可落地方向

### 方向 A：Adversarial Acceptance Crew（研究备选，未采纳为主线）

**用户价值**：开发者把 issue 和现有测试交给 ApexCrew；系统不满足于“绿了”，而是让独立角色主动挑战补丁，补强能够区分错误/正确实现的 oracle，再给出为什么可以接受或仍不可信的证据。

**核心机制**：

- `AcceptanceContract`：从 issue 提炼 examples、invariants、允许变化和必跑 checks，由人一次确认验收语义。
- `builder → challenger → verifier` 状态机：builder 只能提交 patch；challenger 在隔离 worktree 生成反例/测试/性质；verifier 在中立环境运行，任何角色都不能自行宣布完成。
- Oracle ladder：回归测试先在 buggy baseline 失败、在 candidate 通过；性质测试用 Hypothesis；mutation testing 用成熟工具，不手写 mutation engine。[Hypothesis](https://github.com/HypothesisWorks/hypothesis) · [mutmut](https://github.com/boxed/mutmut)
- `EvidenceMemory`：只存可复现 command、counterexample、test 与 dependency fingerprint；相关文件/contract 变化后自动标 stale，重新验证前不得注入为“事实”。
- 停机由确定性预算决定：required checks、最低 mutation score、challenger budget、重复反例 fingerprint 和最大轮数；LLM 不决定 gate。
- 治理仍保留 workspace 围栏、结构化 argv、动作绑定审批和审计，但这些是安全基线，不作为新颖性主张。

**竞品重叠**：LLM 测试生成、property-based testing、mutation testing 各自都不是新技术；h5i 已有双候选与交叉 review。可 defend 的贡献是把 **oracle quality 变成 multi-agent harness 的一等状态、反馈和长期记忆失效规则**。本轮一手材料未见上述直接竞品以此为主产品，但仍只能表述为“差异化组合”，不能声称全球首创。

**MVP**：只支持 Python/pytest；单仓库、3 个逻辑 workers、隔离 worktree；自研两层 loop；`ScriptedMockLLM` + 1 个低层单次补全 adapter；SQLite evidence store；pytest + Hypothesis + mutmut；CLI 和薄 Web timeline；3 个带 hidden oracle 的确定性 fixture。

### 方向 B：Semantic Concurrency Control for Agent Patches

**用户价值**：多个 Agent 并行改同一仓库时，系统像数据库一样发现 stale read、write conflict 和不可序列化的 patch，避免“Git 能 merge，但语义已经过期”。

**核心机制**：为任务声明/观测 read-set 与 write-set；记录 base revision；构建 happens-before 图；集成前重放 read assertions 和受影响 tests；冲突时生成 rebase contract 而非直接文本合并。

**竞品重叠**：worktree 和 serial merge queue 已被 Bernstein/h5i/Vibe/Superset 覆盖；差异必须是可测的 semantic serializability。它有鲜明系统设计，但语言分析和动态 read-set 捕获使 MVP 风险高于方向 A。

### 方向 C：Coding-Agent Continuity Conformance Lab

**用户价值**：为 Codex/Claude/Gemini/ACP adapter 提供同一套离线场景，测量结构化输出、权限、取消、resume、compaction、超时和重复消息的语义差异，避免长任务到一半才发现 backend 不可恢复。

**核心机制**：scenario DSL、fake terminal/MCP server、golden transcript、capability matrix、fault injection、adapter contract report。

**竞品重叠**：Bernstein 已有 40+ adapters、`test-adapter` 和 adapter conformance；ACP 自身提供 schema/SDK。它适合作为 ApexCrew 的 supporting test suite，不适合做主线。[Bernstein limitations](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/reference/KNOWN_LIMITATIONS.md#1-adapter-parity-is-not-perfect) · [ACP schema](https://github.com/agentclientprotocol/agent-client-protocol)

## 已归档的研究建议与可证伪验收（未采纳为产品主线）

> **归档边界**：从本节到文末均记录当时的 Adversarial Acceptance 提案，包括 Python-only、角色、预算和 fixture 假设。它们不可作为当前执行要求；现行范围只以 ADR-0001、`INITIALIZATION.md` 和未来签字后的 `SPEC.md` 为准。

本轮研究曾建议选择 **方向 A** 并把副标题改为 “Adversarial verification for coding-agent teams”。用户随后明确保留 Evidence-Driven Durable Crew；因此以下验收项只作为辅助实验候选，不改变产品身份。

首个 demo 应可离线重复证明：

1. 一个 flawed patch 能通过原测试；challenger 生成的反例在 baseline/patch 上呈现契约要求的区分，并迫使 builder 修正。
2. 一组“只覆盖 happy path”的生成测试 mutation score 不达标，因此不能完成；补强后分数跨过固定阈值。
3. builder 不能修改 verifier 的已批准 contract，且不能用删除/放宽测试让自己变绿。
4. 已验证事实所依赖的源码变化后自动变 stale；新 worker 不会把它当作有效上下文，重新验证后才恢复。
5. 同一 scripted trajectory 在 crash/restart 后从持久状态继续，check 不重复计数；危险动作仍满足审批篡改/重放测试。
6. 在至少 3 个 hidden-oracle fixtures 上，对比 single-worker baseline，记录发现额外缺陷数、轮数、token/调用预算；结果不依赖 LLM-as-judge。

若这些不变量无法在 `ScriptedMockLLM` 下确定性通过，就不应先接真实供应商。真实 `ModelPort` 只是外部 seam；主贡献必须在无 API key 的 CI 中完整验证。

### 归档提案的 MVP 非目标（不可执行）

- 不训练模型，不做 prompt marketplace，不宣称自主“软件公司”。
- 不与 Bernstein、h5i、Vibe Kanban、Superset 竞争通用多 Agent 控制台、40+ adapter、完整 provenance/replay 平台。
- 不实现通用 LLM provider SDK；只定义窄 `ModelPort`，首版做 fake + 一个低层单次补全 adapter。ACP/CLI backend 延后且不计入评分核心。
- 不做公网、多租户、跨机器 federation、A2A server、Kubernetes、云计费或定时 repo steward。
- 不做无限层级 swarm；固定 2–3 个 worker 和有界 DAG。
- 不做向量数据库或“记住一切”；只保存带 dependency fingerprint 的可复现实验证据。
- （已被后续决策替代）不在 MVP 支持任意语言；当前已接受 Python + TypeScript fixture 矩阵。
- 不自动 push 或发布；私有 Run Branch 可按已批准规则自动推进，但用户 target ref 的最终 CAS 仍需一次性人工 Grant。
- 不把任意 shell 直接开放给模型；checks 使用仓库声明的 argv，危险命令走不可变审批。
- 不用 LLM-as-judge 代替目标仓库测试；LLM 评分只能是附加指标。

### 归档提案当时的待确认信息（不可执行）

进入 `SPEC.md` brainstorming 前，只需用户确认五项，不需要 API key：

1. ~~是否接受唯一主线 Adversarial Acceptance Crew~~：已由用户否决；Evidence-Driven Durable Crew 是唯一主线，弱 oracle 挑战仅作辅助实验。
2. ~~后续低层真实模型供应商及模型~~：Round 3 已选 OpenAI Responses API 与 `gpt-5.6-terra`；离线核心仍使用 `ScriptedMockLLM`。
3. ~~课程截止日期和每周可投入时间~~：已确定 2026-08-10、25 小时/周；公网仅部署 sanitized fixture 的只读 GitHub Pages。
4. ~~Python oracle 与 mutation 阈值~~：弱 oracle/challenger 已降为可选实验，不得占用主机制与课程交付时限。
5. ~~fixture 选择~~：已批准 Python money-unit drift 与 TypeScript timestamp-unit drift 两个跨生态 fixture。

根目录贡献指南已改为 ApexCrew 的探索期边界；方向确认后仍须在正式 brainstorming 中与新 `SPEC.md` 一起复核。本报告只是方向证据，不替代三轮需求确认。
