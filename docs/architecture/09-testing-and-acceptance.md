# 测试、验收与证据口径

## 目标

说明 ApexCrew 如何验证 Harness 机制，而不是只测试“模型是否恰好修好一道题”。测试策略围绕重放、并发、崩溃、scope、证据、Git ref 和安全 containment 的不变量展开。

## 测试分层

| 层级 | 目录 | 目的 | 典型问题 |
| --- | --- | --- | --- |
| Unit | `tests/unit/` | 纯领域规则、规范化、状态转换 | path 是否 canonical、freshness 是否 stale、Grant 是否匹配 |
| Contract | `tests/contract/` | adapter/public interface 契约 | SQLite 与 memory 是否满足同一语义、CLI/docs/build 是否守约 |
| Integration | `tests/integration/` | 临时 Git/SQLite、跨模块控制流 | Permit replay、lease/Grant race、Git preflight、recovery |
| Acceptance | `tests/acceptance/` | 最接近用户目标的 fixture workflow | Python money / TypeScript timestamp drift 的修复链路 |

## Deterministic model testing

`ScriptedMockLLM` 是核心机制测试的默认 ModelPort。它允许测试精确指定第 N 次调用返回哪一个结构化 action、usage、错误或不可确定结果，并断言下一次请求是否包含正确的 tool feedback。这样可以稳定复现：

- malformed action。
- 第一次 check 失败、第二次 patch 修复。
- budget reservation/settlement。
- provider/model binding mismatch。
- crash/restart 后恢复已记录 model turn。

真实 provider smoke 只验证 provider contract，不能取代 offline deterministic suite。

## 高风险测试矩阵

| 风险 | 需要证明的行为 | 代表测试 |
| --- | --- | --- |
| Permit replay | 已消费 Permit 不能再次驱动 Runtime | `test_runtime_permits.py` |
| Grant race | 同一 Grant 不能授权两次 action | `test_grant_consumption.py` |
| lease drift | 旧 generation 不能继续写 | `test_leases.py`、worker integration |
| stale evidence | revision/dependency 改变时拒绝 candidate | `test_freshness.py` |
| hostile Git | linked worktree/config/symlink 等拒绝 | `test_git_preflight.py` |
| path TOCTOU | 祖先替换不能把写入导向仓库外 | `test_no_follow_handles.py`、granted-action tests |
| recovery | model/patch/ref 的未知状态不盲目重试 | `test_model_restart.py`、`test_granted_action_recovery.py` |
| final ref | target ref 只按 expected old OID CAS | `test_target_cas.py` |
| secret containment | secret bytes不进入查询/日志/执行器 | secret policy / credential contracts |

## 验收 fixture

仓库包含两个独立微型项目：

- `fixtures/python-money/`：金额单位漂移。
- `fixtures/typescript-time/`：时间戳单位漂移。

它们的价值不是测试 Python/TypeScript 语法，而是验证 Harness 是否能在不同生态中保持相同安全链路：有界 context、patch、声明 check、evidence、candidate 和最终 Git 效果。R4.3-06 的独立工作树提交 `a846b3f` 已在两者上观察到 deterministic repair/integration assertion 与 retention purge selectors 通过；该提交仍待独立 SPEC/quality review、ledger closeout 和 root 集成，因此不能把 fixture 的存在或该工作树的绿色结果描述为当前 checkout 的完整 end-to-end 交付证据。

## 如何阅读测试结果

应同时记录 selector、Git revision、平台、skip 原因和命令退出码。尤其注意：

- “749 collected，suite exit 0” 不等于 749 个无条件 passed；其中可以有平台 skip 或显式 live-smoke skip。
- 通过某个 R4.3 任务分支的测试，不等于当前 dirty checkout、`main`、远端 PR 或发布 artifact 已通过。
- static checks、secret scan、wheel build、Docker observation、hosted CI 是不同证据，不能相互替代。

截至 2026-08-10，最近完成规格和质量审查的 R4.3-03 baseline `58609e8` 收集 749 个测试且 suite 退出码为 0；66 个源码文件通过 strict mypy，Ruff、format、diff check 通过。这是该分支的观察记录，不是未完成 R4.3-04 至 R4.3-07 的完成声明。

## 推荐验证命令

```powershell
make test
make lint
make demo
make secret-scan
make web-build
make build
```

`make live-smoke` 需要显式 gate 和 owner 授权，不属于默认成功路径。

## 源码与测试映射

| 验证入口 | 位置 | 作用 |
| --- | --- | --- |
| 默认测试 | `Makefile` 的 `test` target | 运行 deterministic offline suite |
| 静态检查 | `Makefile` 的 `lint` target、`pyproject.toml` | Ruff format/check 与 strict mypy |
| 机制 demo | `src/apexcrew/demo.py` | 无网络的 guardrail/feedback/freshness trace |
| Secret scan | `scripts/secret_scan.py` | 扫描 tracked tree 与 reachable history |
| Web build | `scripts/build_webui.py` | 构建只读静态 replay |
| Release artifact | `Dockerfile`、`.github/workflows/ci.yml`、`.gitlab-ci.yml` | wheel、restricted image 与 CI 定义 |

## 文档与流程证据

- `AGENT_LOG.md` 记录 red/green、审查与纠正证据。
- `PLAN.md` 记录哪个 task 已完成实现、SPEC review、quality review 和 ledger closeout。
- `SPEC_PROCESS.md` 记录规范修订与 cold-start review。
- 本目录解释机制；不能替代上述历史和授权记录。

## 当前状态边界

测试分层和基本命令已存在，但 release readiness 不能从一次 local test exit code 推导。R4.3-06 已有待审查的本地 acceptance/purge 证据；R4.3-07 仍承担 same-revision build/scan/performance/static replay/WebUI/hosted evidence 的收尾责任。真实 live provider、远端 CI 观测和发布仍是显式 owner action。完整状态见 [v0.1 闭环状态](10-v0.1-closure-status.md)。

下一步可结合 [项目伪代码](pseudocode/README.md) 阅读对应测试的行为断言。
