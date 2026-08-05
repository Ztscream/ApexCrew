# ApexCrew M1-M4 完整化执行计划(2026-08-05)

**目标:** 把 M1-M4 从「端到端可运行、边缘标注为债务」提升到「全部 REAL、零 DEBT 标记」,并接入真实 provider。

**这份文件的地位:** 按 `PLAN.md:99` 的 Streamlined Execution Protocol 粒度写成的任务清单——每任务给出目标、文件、实现要点、先写哪个失败测试。语义争议仍以 `SPEC.md`(**revision 3**)为准。本文件不替代 `PLAN.md` 的需求契约。

**授权状态:**
- `PLAN.md:99` 与 `PLAN.md:323` 已确立:M2-M4 **不需要**单独的独立评审版本或所有者 `GO`,按本清单直接执行。
- `SPEC.md` 已于 2026-08-05 升至 **revision 3**,SHA-256 `E4385008CD75E4E3B0E70B25A6EBDFD976F3E1031F2ACD81FF0B6284EF6668AB`,131,813 字节,636 行。依据 `docs/proposals/0002-replace-model-provider-with-deepseek.md`。**从此刻起 SPEC 重新冻结,任何智能体不得再改一个字节。**
- **仍需所有者单独动作的两件事:** ① 合并 PR #13 与启用 Pages 部署 ② `PLAN.md:359` 要求的 live smoke 单独授权。任务 P5、W1 停在这两点前,不得自行越过。

---

## 一、不可破的规则

1. **Fail closed。** 未实现分支必须拒绝或抛异常,绝不返回放行。
2. **未覆盖的恢复场景一律落 `INDETERMINATE`**,不猜成功、不用墙钟、不猜崩溃余量。
3. **TDD。** 先写失败测试并观察到红,再写最小实现。
4. **不改 `SPEC.md`。** revision 3 已冻结。
5. **不碰凭据值、不启用 Pages、不改工作流权限。push 与 PR 是所有者动作。**
6. **观察到真实输出才能声称通过。**
7. **本轮目标是清零 DEBT。** 每关掉一个 `DEBT-` 标记,必须同步从 `README.md` 与 `SECURITY.md` 的债务清单里移除该条。收尾时 `grep -rn "DEBT-" src/` 必须为空。

## 二、每任务协议

1. 记录 base SHA 与打算改的路径
2. 写失败测试 → 跑 → **观察到红**
3. 最小实现 → 跑焦点选择器 → 绿
4. `uv run --python 3.12 mypy src` + `ruff check .` + `ruff format --check .` + `git diff --check`
5. REAL 任务:有序 spec 评审 → quality 评审
6. 一个 Conventional Commit,含实现 + 测试 + `AGENT_LOG.md` 条目,带 trailer:
   ```
   PLAN-Task: <任务号>
   Subagent: <标识>
   Human-Changes: none
   Spec-Review: <标识>
   Quality-Review: <标识>
   ```
7. 模块收尾更新本文件台账 + 一个 `docs(plan): record <模块> task commits` 提交

## 三、并行与依赖

改动路径集不重叠的模块可并行开 worktree。观察到的依赖:

```
P1 → P2 → P3 → P4 → P5(卡所有者授权)
R1  ────────────────────────┐
T1 / T2 → T3 / T4  ─────────┤→ Z1(DEBT 清零核对)
W2 / W3 / W4 ───────────────┤
X1 → X2 ────────────────────┘
W1 卡所有者合并 PR #13
```

- **P 模块串行**:P4 消费 P1 的凭据端口与 P3 的定价配置。
- **R1、T1-T4、W2-W4、X1 之间无路径重叠**,可并行。
- **T2 → T3**:驱逐顺序依赖保留分级的持久化结构。
- **Z1 必须最后做。**

---

## 四、模块 P — Provider 接入

### P1 — 模型凭据端口

**深度:** REAL

**目标:** 建立 `SPEC.md:473` 要求的凭据边界。当前 `src/` 里**完全没有**模型密钥的存取路径,`adapters/credentials/keyring.py` 只有 secret-path 策略的 HMAC key,与模型无关。

**文件:**
- 新建 `src/apexcrew/adapters/credentials/model_key.py`
- 新建 `tests/contract/test_model_credentials.py`

**实现要点:**
- `ModelCredentialPort` Protocol:`resolve(profile: str) -> str`,只在**请求时刻**读取。
- `KeyringModelCredentialStore`:keyring service `apexcrew`,account `model-credential-<profile>`,profile 初值 `deepseek`。
- `MemoryCredentialStore`:测试替身,同一 Protocol。
- 解析顺序:keyring 优先;缺失时才看环境变量 `APEXCREW_DEEPSEEK_API_KEY`(`SPEC.md:473` 只允许 headless CI 走环境变量)。
- 禁止:缓存到实例属性、写日志、进 `repr`/`str`、传给子进程、进 executor 环境。
- 仓库 `.env` **不得**加载(`SPEC.md:473` 明确说仓库及其脚本不可信)。

**先写的失败测试:**
1. `test_missing_credential_fails_closed_with_zero_side_effects` — keyring 与环境变量都空时抛 `MODEL_CREDENTIAL_MISSING`,且**不创建任何目录或文件**。
2. `test_credential_never_appears_in_repr` — 构造已配置的 store,断言 `repr(store)` 与 `str(store)` 不含密钥字节。
3. `test_repository_dotenv_is_not_loaded` — 工作目录放一个含 `APEXCREW_DEEPSEEK_API_KEY` 的 `.env`,断言仍抛 `MODEL_CREDENTIAL_MISSING`。

**提交:** `feat(credentials): add model credential boundary`

---

### P2 — CLI 凭据命令与 doctor

**深度:** REAL

**目标:** `SPEC.md:473` 要求的 `set`(隐藏输入)/ `status`(只报来源与存在性)/ `clear`(显式命令)。

**文件:**
- 修改 `src/apexcrew/delivery/cli.py`
- 新建 `tests/contract/test_cli_credentials.py`

**实现要点:**
- `apexcrew credentials set` 用 `typer.prompt(..., hide_input=True)`,**绝不接受命令行参数传值**(会进 shell 历史)。
- `status` 只输出 `source=keyring|env|absent`,**不输出任何密钥字节,连前缀都不输出**。
- `clear` 删除 keyring 条目,幂等。
- `doctor` 增加一行凭据存在性检查,同样只报存在性。

**先写的失败测试:**
1. `test_status_output_contains_no_key_bytes` — 配置一个哨兵密钥,跑 `status`,断言输出不含该哨兵的任何 4 字节以上子串。
2. `test_set_rejects_value_as_argv` — `credentials set --value X` 必须报错退出,不得接受。
3. `test_clear_is_idempotent` — 连续两次 `clear` 都退出 0。

**提交:** `feat(cli): add credential commands and doctor check`

---

### P3 — Model Configuration 与 Budget 换绑

**深度:** REAL

**目标:** 把模型身份与定价数据从 `gpt-5.6-terra` 切到 `deepseek-v4-flash`。**`src/` 无硬编码**,改动集中在配置构造与测试夹具。

**文件:**
- 修改 `tests/contract/test_scripted_model.py`、`tests/contract/test_state_store.py` 及其余夹具中的模型 ID
- 修改任何构造默认 `BudgetRevisionDocument` 的位置

**实现要点:**
- 规范模型 ID:`deepseek-v4-flash`。精确返回 ID 白名单**单成员**。
- 定价条目按 `SPEC.md:493` revision 3:输入 USD 0.28/百万,输出 USD 0.56/百万,`pricing_observed_on = 2026-08-05`。
- 操作性成本上限建议由 in-band Budget Revision 设为 **USD 1**(SPEC 表最大值 USD 10 不变,下调无需改 SPEC)。
- **不要**把 `deepseek-v4-flash-0731` 之类日期版本加进白名单。

**先写的失败测试:**
1. `test_budget_missing_price_for_allowed_id_is_rejected` — `allowed_model_ids` 含 `deepseek-v4-flash` 但 pricing 只有旧 ID → 必须在派发前拒绝(`authority.py:632` 已有该判断,本测试锁定它)。
2. `test_worst_case_reservation_matches_revision_3_rates` — 按 token 天花板断言预留 = USD 0.672。

**提交:** `feat(budget): bind deepseek pricing snapshot`

---

### P4 — DeepSeek Responses adapter(关闭 `DEBT-M4-001`)

**深度:** REAL

**目标:** 把 `openai_responses.py` 的 12 行 stub 换成真实 transport。

**文件:**
- 新建 `src/apexcrew/adapters/model/deepseek_responses.py`
- 删除 `src/apexcrew/adapters/model/openai_responses.py`
- 新建 `tests/contract/test_deepseek_responses_adapter.py`

**实现要点:**
- 用已声明的 `openai>=1.0,<2` SDK,`base_url="https://api.deepseek.com"`,**不新增依赖**。
- `max_retries=0`——`SPEC.md:469` 禁止 adapter 内部重试。
- 调用 `client.responses.create(...)`,参数:`model`、`input`、`instructions`、`max_output_tokens`、`temperature`、`text.format` 用 typed action schema、`reasoning.effort` **显式钉死**并记入推理参数。
- **核心约束(`SPEC.md:469` revision 3 新增):** 该 provider 静默忽略不支持的参数。因此**任何安全属性都不得只依赖请求参数**,所有结算输入必须从**观察到的响应**派生。
- 结算输入:`status`、`model`、`usage.input_tokens`、`usage.output_tokens`、`usage.output_tokens_details.reasoning_tokens`、`id`。
- reasoning tokens 计入输出 token 与成本(`SPEC.md:493` revision 3)。

**先写的失败测试(注入式假客户端,不发真实请求):**
1. `test_request_pins_no_sdk_retries` — 断言构造客户端时 `max_retries == 0`。
2. `test_returned_model_mismatch_releases_nothing` — 假客户端返回 `deepseek-v4-flash-0731` → 必须落 `RETURNED_MODEL_MISMATCH`、不放行输出、不自动重试、不静默别名。
3. `test_incomplete_status_is_closed_failure` — `status="incomplete"` 且 `incomplete_details` 非空 → known-closed,不推进状态机。
4. `test_missing_usage_consumes_full_reservation` — usage 缺失 → 扣满预留(`SPEC.md:198`)。
5. `test_reasoning_tokens_count_as_output` — `reasoning_tokens=1000` 必须计入输出量与成本。
6. `test_non_conformant_payload_fails_closed` — 返回不符合 typed schema 的载荷 → 关闭失败,**不得宽容解析**。

> **注意:** provider 两份文档对 `text.format: json_schema` 是否支持 v4-flash 说法冲突(接口参考列了,使用指南没提)。测试 6 让这个冲突无害化——无论哪份对,不符合 schema 都 fail closed。真实答案由 P5 的 smoke 实证。

**提交:** `feat(model): connect deepseek responses transport`

---

### P5 — Live smoke(**卡所有者授权**)

**深度:** REAL

**目标:** `PLAN.md:359` 要求的单独授权 smoke。

**文件:** 新建 `tests/integration/test_provider_smoke.py`

**实现要点:**
- env 门控(`APEXCREW_SMOKE=1`),**默认 skip**,绝不进常规 CI。
- 单次调用,跑前打印预留成本。
- 记录实测的 `model` 字段字面值与 `text.format` 是否生效,写进 `AGENT_LOG.md`。

**先写的失败测试:** `test_smoke_is_skipped_without_authorization` — 无 env 时必须 skip 而非通过。

**执行前必须停下来等所有者授权。不得自行运行。**

**提交:** `test(model): add authorized provider smoke`

---

## 五、模块 R — M1 债务

### R1 — 跨进程运行时所有权(关闭 `DEBT-M1-006`)

**深度:** REAL

**目标:** 当前只有进程内所有权,多进程并发无防护(`application/runtime.py:75`)。

**文件:**
- 修改 `src/apexcrew/application/runtime.py`、`src/apexcrew/adapters/system.py`
- 修改 `tests/integration/test_runtime_lock_lifecycle.py`

**实现要点:**
- POSIX 用 `fcntl.flock(LOCK_EX|LOCK_NB)`,Windows 用 `msvcrt.locking(LK_NBLCK)`,置于 `adapters/system.py` 的端口之后。
- **先校验 Permit,再建 `runtime-locks` 目录**——现存缺陷是顺序反了,必须一并修正。
- 持有者进程已死时,锁的回收结果落 `INDETERMINATE`,**不猜崩溃余量**。

**先写的失败测试:**
1. `test_second_process_cannot_acquire` — 用真实子进程持锁,主进程 `acquire()` 必须失败。
2. `test_acquire_without_permit_has_zero_side_effects` — 无有效 Permit 时不得创建 `runtime-locks` 目录。
3. `test_dead_holder_recovers_as_indeterminate` — 持有者被杀后恢复必须落 `INDETERMINATE`,不得判定为成功。

**提交:** `fix(runtime): add cross-process ownership locks`

---

## 六、模块 T — M2 债务

### T1 — 多意图优先级消解(关闭 `DEBT-M2-001`)

**深度:** REAL · 对应 `PLAN.md` Task 24E-24F

**文件:** `src/apexcrew/domain/indeterminate.py`、`tests/integration/test_multi_intent_resolution.py`(新建)

**实现要点:** 建立**客观**优先级表——只从可观测状态消解单个成员,不从模型输出消解。无法客观判定的组合仍落 `INDETERMINATE` 并停。

**先写的失败测试:**
1. `test_single_observable_member_resolves` — 集合中恰有一个成员有可观测终态 → 消解为该成员。
2. `test_ambiguous_set_still_fails_closed` — 两个成员都无可观测终态 → 仍抛 `MULTIPLE_INTENTS_UNRESOLVED`。
3. `test_resolution_never_reads_model_output` — 断言消解路径不接受模型载荷参数。

**提交:** `feat(recovery): resolve multi-intent sets by observation`

### T2 — 保留分级与 Tier 2 脱敏(关闭 `DEBT-M2-002`/`DEBT-M2-003`)

**深度:** REAL · 对应 Task 26A-26D

**文件:** `src/apexcrew/domain/retention.py`、`tests/integration/test_retention_tiers.py`(新建)

**实现要点:** 按 `SPEC.md:543/547`——Tier 1 只含白名单字段;Tier 2 落盘前替换所有已知凭据值并扫描 token/私钥模式;不可解析或可疑内容落 `QUARANTINED` 只暴露元数据与摘要;预览上限 prompt/response 各 128 KiB、diff 256 KiB、stdout/stderr 各 64 KiB,**保留原始字节长度与内容摘要**。导出永远排除 Tier 2 与隔离内容。

**先写的失败测试:**
1. `test_known_credential_value_is_redacted_before_persistence`(与 P1 联动)
2. `test_quarantined_content_exposes_only_metadata`
3. `test_preview_caps_preserve_original_length_and_digest`
4. `test_sanitized_export_excludes_tier_two`

**提交:** `feat(retention): implement tier redaction and quarantine`

### T3 — 过期与驱逐顺序(关闭 `DEBT-M2-004`)

**深度:** REAL · 对应 Task 26E · **依赖 T2**

**文件:** `src/apexcrew/domain/retention.py`、`tests/integration/test_retention_eviction.py`(新建)

**实现要点:** `SPEC.md:549` 的**精确顺序**——先删过期载荷,再删最旧的终态 Run 产物;仍不足则新产物只存元数据/长度/摘要 + `DROPPED_BY_RETENTION`。Tier 2 载荷 30 天过期,每仓库硬上限 1 GiB。**非过期的活跃 Run 内容不得为容纳新诊断而删除。**

**先写的失败测试:**
1. `test_eviction_order_expired_then_oldest_terminal`
2. `test_active_run_content_survives_pressure`
3. `test_overflow_persists_only_metadata_with_reason`

**提交:** `feat(retention): enforce expiry and eviction order`

### T4 — 受限执行器进程运行(关闭 `DEBT-M2-005`)

**深度:** REAL · 对应 Task 29B-29D

**文件:** `src/apexcrew/adapters/executor/restricted.py`、`tests/contract/test_executor.py`

**实现要点:** `command_for()` 已建好闭合 argv,只差 `run()` 接上受限进程执行。`SPEC.md:515` 全部约束必须在实测中成立:非 root、只读根文件系统、`--network=none`、无 docker socket、dropped caps、`no-new-privileges`、CPU/内存/PID/临时区上限。宿主只传最小白名单环境。命令产生的文件全部丢弃。**Docker daemon 不可用时 skip 并注明,不得伪造通过。**

**先写的失败测试:**
1. `test_container_has_no_network` — 容器内访问网络必须失败。
2. `test_container_runs_as_non_root`
3. `test_command_created_files_are_discarded`
4. `test_host_credentials_are_not_visible_in_container`

**提交:** `feat(executor): connect restricted process runner`

---

## 七、模块 W — M3 完整化

### W1 — Pages 部署(**卡所有者合并 PR #13**)

**深度:** REAL · **交付物 9**

**文件:** `.github/workflows/ci.yml` 或新建 `.github/workflows/pages.yml`

**现状:** Pages 已配置(`build_type: workflow`,source `main`),但 `status: null`,从未构建成功,`https://ztscream.github.io/ApexCrew/` 实测 404。`ci.yml` 只有 `quality`/`unit-ubuntu`/`unit-windows`/`build`,**没有 Pages 部署 job**。

**实现要点:** 加 `deploy-pages` job,消费 `make web-build` 的 `dist/webui` 产物。`README.md` 必须写明该站点是**脱敏确定性 fixture 回放,不是执行服务**,无命令/仓库/凭据/审批/模型调用/托管后端路径(`PLAN.md:507`)。

**智能体只准备 workflow 与文档。启用与合并是所有者动作。**

**提交:** `build(ci): add pages deployment job`

### W2 — 静态回放加固

**深度:** REAL · 对应 Task 33B-33D

**先写的失败测试:** 敌意读模型内容(含 `<script>`、事件属性、`javascript:` URL)渲染进本地与静态 HTML 后必须不可执行;静态页无任何网络取数;loopback 会话一次性。

**提交:** `feat(web): contain hostile content in replay`

### W3 — CI 八 job 拓扑

**深度:** REAL · 对应 Task 35B(依赖 36C)

**提交:** `build(ci): finalize same-sha release topology`

### W4 — 性能与可达性预算

**深度:** REAL · 对应 Task 36A-36C

**提交:** `test(perf): enforce query and web budgets`

---

## 八、模块 X — M4 完整化

### X1 — 设计工作台

**深度:** REAL · 对应 Task 36D-36G。当前 `docs/design-workbench.md` 只是文档形态 STUB。

**提交:** `feat(design): transfer accepted design system`

### X2 — 同版本发布闸门

**深度:** REAL · 对应 Task 28 收尾与外部同 revision 发布闸门。

**提交:** `build(release): close same-revision delivery gate`

---

## 九、模块 Z — 收尾

### Z1 — DEBT 清零核对

**深度:** REAL · **必须最后做**

**验证步骤:**
1. `grep -rn "DEBT-" src/` → **必须为空**
2. `README.md` 与 `SECURITY.md` 的债务清单同步清空或改写为「已关闭」
3. 修 `verify.py` 唯一 FAIL:在 `PLAN.md` 里给 `_read_oldest_unsettled_granted_action` 补散文定义
4. `uv run --python 3.12 pytest -q` 全绿、`mypy src` 零错误、`ruff` 双通过
5. `make demo` 确定性复现 A.6 三行为
6. 托管 CI 最后一次 pass

**提交:** `docs: close m1-m4 debt ledger`

---

## 十、完成定义

- [ ] `grep -rn "DEBT-" src/` 为空
- [ ] `pytest -q` 全绿,跳过项按平台注明
- [ ] `mypy src` 零错误;`ruff check .` 与 `ruff format --check .` 通过
- [ ] `make demo` 确定性复现 A.6 三行为
- [ ] `verify.py` 18/18
- [ ] 真实 provider 单次 smoke 在所有者授权下观察到成功,结果写进 `AGENT_LOG.md`
- [ ] Pages URL 可访问(所有者动作完成后)
- [ ] 每任务一个 commit,带完整 trailer

## 十一、说话的边界

清单全绿后可以说的是:

> **M1-M4 全部 REAL,零 DEBT 标记,harness 内核与治理机制有 mock-LLM 确定性测试,真实 provider 经单次授权 smoke 验证。**

在此之前**不可以**说 M2-M4 完成,不可以说生产可用,不可以把 STUB 描述成已实现。诚实标注债务是加分项——本项目已有一次「独立评审推翻自己全绿交付」的记录,那才是可信度的来源。

---

## 十二、任务台账

每完成一个任务,把该行改为 `DONE` 并填入观察到的完整 40 位 SHA。

| 任务 | 深度 | 关闭的债务 | 状态 | 实现提交 |
| --- | --- | --- | --- | --- |
| P1 模型凭据端口 | REAL | — | TODO | |
| P2 CLI 凭据命令 | REAL | — | TODO | |
| P3 定价换绑 | REAL | — | DONE | `a0eb48e6485ba8a1a87577687409117f1bb985a2` |
| P4 DeepSeek adapter | REAL | `DEBT-M4-001` | TODO | |
| P5 live smoke | REAL | — | BLOCKED(所有者授权) | |
| R1 跨进程锁 | REAL | `DEBT-M1-006` | TODO | |
| T1 多意图消解 | REAL | `DEBT-M2-001` | TODO | |
| T2 保留分级脱敏 | REAL | `DEBT-M2-002`/`003` | TODO | |
| T3 过期与驱逐 | REAL | `DEBT-M2-004` | TODO | |
| T4 执行器运行 | REAL | `DEBT-M2-005` | TODO | |
| W1 Pages 部署 | REAL | — | BLOCKED(所有者合并 PR #13) | |
| W2 回放加固 | REAL | — | TODO | |
| W3 CI 拓扑 | REAL | — | TODO | |
| W4 性能预算 | REAL | — | TODO | |
| X1 设计工作台 | REAL | — | TODO | |
| X2 发布闸门 | REAL | — | TODO | |
| Z1 DEBT 清零 | REAL | — | TODO | |
