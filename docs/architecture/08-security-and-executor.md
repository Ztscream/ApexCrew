# 安全边界与受限执行

## 目标

Agent 接受的是不可信模型输出，也会处理不可信目标仓库。本文从攻击面出发说明 ApexCrew 的防护层，而不是把“模型被提示不要做坏事”误当作安全控制。

## 信任边界

| 边界 | 信任程度 | ApexCrew 的控制 |
| --- | --- | --- |
| 用户 CLI / host control plane | 受信任但仍经类型校验 | 类型化命令、confirmation、Permit |
| 模型输出 | 不可信 | 单动作 schema、Authority、scope、Grant |
| 目标仓库文件和脚本 | 不可信 | path policy、secret policy、snapshot、restricted executor |
| Git metadata | 不可信 | preflight、封闭 Git argv、repository identity |
| provider credential | 高敏感 | request-time keyring/env resolution、无日志/无 executor mount |
| WebUI/replay | 公开/低信任 | `RunQueries` only、脱敏、无 command capability |

## 路径与 workspace 防护

路径首先规范化，再判断 scope；v0.1 拒绝绝对路径、drive-qualified path、`.`、`..`、反斜杠、control characters、colon/NTFS ADS 等非普通路径形式。secret path 在读取内容之前过滤，错误和日志不能回显受保护路径或内容。

普通 `Path.resolve()` 只检查一个瞬间，无法阻止“预检查后替换祖先目录”的 TOCTOU。ApexCrew 的 no-follow adapter 通过稳定 handle 和前后 identity/name binding 检查降低这个窗口；POSIX 和 Windows 使用不同实现。symlink、submodule 和不符合受限 regular-file 模型的对象默认拒绝。

## Git 防护

- 不接受模型提供的 raw shell；Git operation 是封闭类型，转为 structured argv。
- preflight 拒绝 v0.1 不支持的 linked worktree、config include、alternates、graft、shallow/partial history、sparse/split index 和外部存储路由。
- Target Reservation 是 locked no-checkout evidence，Worker 不向其写入。
- Worker 不能直接更新任何 target ref；只有 Admission 可以签发 typed CAS。
- `git` 子进程不会继承任意仓库配置或 hook 路径来执行额外代码。

## Restricted Docker Executor

`RestrictedDockerExecutor.command_for` 验证允许的 executable 和 token 后构造封闭 Docker argv，包括：

```text
--network=none
--read-only
--cap-drop=ALL
--security-opt=no-new-privileges
--user=<fixed uid:gid>
--cpus / --memory / --pids-limit
<digest-pinned image>
```

它不允许 Docker socket、host network、任意环境变量或 host-subprocess fallback。Docker daemon、image、timeout 或 process 结果不可用时必须返回有界失败/不确定状态。

### Open boundary: `DEBT-M2-005`

当前 root checkout 中 `RestrictedDockerExecutor.run` 仍显式拒绝并抛出 `RESTRICTED_EXECUTOR_RUNNER_NOT_CONNECTED`；它只证明 closed argv builder。真实 restricted Docker process 的观测和对应 R4.3-03 closure 尚不能被当前 checkout 声称完成。文档不应把“构造安全命令”写成“已经在容器执行”。

## 凭据与模型安全

- 默认核心测试使用 `ScriptedMockLLM`，不读取凭据或网络。
- DeepSeek credential 在请求时从 OS keyring 或唯一 CI environment variable 解析。
- 凭据不进入 `repr`、日志、模型 payload、Git workspace 或 Docker executor。
- real live smoke 需要显式 `APEXCREW_LIVE_SMOKE=1`，默认 suite/CI 不执行。
- returned model ID、usage、pricing/reservation 和 schema 都需要满足配置/结算契约。

## Read-only delivery

WebUI 和 static replay 只依赖 sanitized `RunQueries` projection。它们没有 Control/Runtime 引用，因此不能签发 Permit、消费 Grant、读取 credential、执行模型或修改 Git。The read-only WebUI is not an execution service.

## 源码与测试映射

| 防护 | 源码 | 测试 |
| --- | --- | --- |
| canonical path / secret policy | `domain/plan.py`、`domain/policy.py` | `tests/unit/domain/test_path_grammar.py`、`test_secret_policy.py` |
| no-follow | `adapters/repository/no_follow*.py` | `tests/unit/adapters/repository/test_no_follow_handles.py` |
| Git preflight | `adapters/repository/git.py` | `tests/integration/test_git_preflight.py` |
| restricted argv | `adapters/executor/restricted.py` | `tests/unit/adapters/executor/test_restricted.py` |
| credential | `adapters/credentials/` | `tests/contract/test_model_credentials.py`、`test_cli_credentials.py` |
| Web projection | `delivery/web.py` | `tests/unit/test_replay_web.py` |

完整信任边界见 [`SECURITY.md`](../../SECURITY.md)。

## 当前状态边界

canonical path、secret policy、no-follow handles、Git preflight、credential isolation 和 query-only delivery 都有当前源码及测试入口。`RestrictedDockerExecutor.run` 的真实进程执行仍由 `DEBT-M2-005` 限定；R4.3-03 的更完整 composition 工作必须以其任务分支审查记录为准，不能覆盖当前 root checkout 的显式拒绝行为。
