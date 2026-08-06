# ApexCrew

ApexCrew is a local-first Coding Agent Harness for an **Evidence-Driven Durable Crew**. It coordinates bounded work in one Git repository, requires human approval for risky actions, and accepts changes only when objective evidence is fresh for the exact revision being integrated.

## 项目简介

This repository contains the M1-M4 offline harness core and delivery artifacts. The durable application surface is `CrewControl`, `CrewRuntime`, and read-only `RunQueries`; deterministic tests use `ScriptedMockLLM`.

## 安装

Requirements: Python 3.12 and `uv`.

```text
uv sync --frozen --all-groups
```

No provider credential is required for the offline suite.

## 运行

```text
uv run --python 3.12 apexcrew --help
uv run --python 3.12 python -m apexcrew.demo
```

The demo is deterministic and local-only. The CLI refuses runtime or approval actions when no composed authority/Permit exists.

The supported end-user path is the CLI. `run-create` is offline and does not call
DeepSeek; after the three revision approvals, `begin-planning` issues a Runtime
Permit and `run` consumes it before planning, Worker, or repository effects. Use
`show` for the next exact digest, pending action, Candidate, and confirmation
code preview. The read-only WebUI and Pages replay never execute a Run.
After a terminal Run reaches `COMPLETED`, issue `reconcile-cleanup` and then
`run` to consume the terminal-administrative Permit and settle the exact Git
Target Reservation. Cleanup preserves the terminal Run state.

## 凭据安全配置

The offline suite, the demo, and ordinary delivery commands run without a provider credential. The real DeepSeek adapter is runtime-gated: only an explicitly authorized `live-smoke` run with `APEXCREW_LIVE_SMOKE=1` may dispatch it; an ordinary `run` is rejected before Permit consumption.

```text
uv run --python 3.12 apexcrew credentials set      # hidden interactive input
uv run --python 3.12 apexcrew credentials status   # prints source only, never the value
uv run --python 3.12 apexcrew credentials clear    # idempotent removal
uv run --python 3.12 apexcrew doctor               # reports presence, not the value
```

The value is stored in the OS keyring under service `apexcrew`, account `model-credential-deepseek`. `set` accepts the secret only through hidden interactive input — there is deliberately no `--value` flag, because a command-line argument would land in shell history and the process table.

Resolution order is keyring first, then the single environment variable `APEXCREW_DEEPSEEK_API_KEY`. That fallback exists for headless CI, which should inject it from a secret store; other headless use should supply it through a supervisor or secret manager rather than an interactive shell. Repository `.env` files are deliberately **not** loaded, because the repository and its scripts are untrusted input.

The credential is read at request time and never cached on an instance, never written to logs, never included in `repr`/`str`, never passed to a child process, and never mounted into the restricted executor. Missing or unreadable credentials fail closed with `MODEL_CREDENTIAL_MISSING` rather than degrading to an unauthenticated call. `SECURITY.md` documents the full trust boundary.

The approved Model Configuration also binds the DeepSeek `temperature` and
`reasoning_effort` values. They are copied into each typed model request and
settled attempt; the adapter has no fallback inference parameters.

To authorize the one-request live smoke explicitly, configure a credential and set
the gate in the current shell. The smoke is skipped by default and is excluded
from `make test` and ordinary CI:

```text
$env:APEXCREW_LIVE_SMOKE="1"       # PowerShell
make live-smoke
```

On POSIX shells use `APEXCREW_LIVE_SMOKE=1 make live-smoke`. The smoke records
only response status, response ID, returned model ID, usage, and structured-action
validity in test assertions; it never prints the prompt or credential.

## 分发命令

```text
make test
make lint
make demo
make live-smoke             # explicit opt-in only; normally skipped
make secret-scan
make web-build
make build
```

`make build` creates the wheel and the digest-pinned executor image. CI repeats quality, tests, wheel build, and image build.
`make web-build` creates the static read-only bundle described in `docs/deployment.md`.
The GitHub Pages site is a sanitized, deterministic fixture replay for inspection; it is not an execution service and cannot run agents, accept commands, or mutate repositories.

## 目录结构

```text
src/apexcrew/       domain, application, adapters, delivery
tests/              unit, contract, integration, acceptance coverage
fixtures/           Python money and TypeScript timestamp repositories
scripts/             release/security helpers
docs/                architecture, research, decisions, learning
```

## 安全边界

Commands and approvals are CLI-only. The WebUI and replay export consume only sanitized `RunQueries` projections. Raw shell, host access, network, Docker socket, push, destructive Git, secret paths, and untyped target mutation are denied. Credentials are never read by the offline demo or committed to the repository; see `SECURITY.md` for the full trust boundary.

The WebUI is a read-only projection, not an execution service.

## Status

The frozen specification, reviewed M1 plan amendment, and Stage 4 cold-start gate authorize this implementation slice. M1-M4 are delivered at the SPRINT depth levels recorded in `AGENT_LOG.md`; SKELETON and STUB items remain explicitly bounded.

R4.1-04 asymmetric terminal cleanup is implemented at commit `27f1b81` and has
passed the offline, packaging, secret-scan, Docker, and fresh-process checks recorded
in `AGENT_LOG.md`. The deterministic release path is locally verified. A real DeepSeek
request is deliberately not part of ordinary verification: configure a keyring
credential and set `APEXCREW_LIVE_SMOKE=1` only when the operator explicitly authorizes
the one-request live smoke.

## Accepted v0.1 Boundary

- One local repository and one user.
- At most three ApexCrew-owned Workers.
- A self-built Coordinator loop and WorkerLoop using low-level model completion APIs through a narrow `ModelPort`.
- An A-Hybrid Run surface: `CrewControl.handle`, `CrewRuntime.run_until_blocked`, and read-only `RunQueries.get` over internal deep modules.
- One-use internal Runtime Permits bind accepted control commands to exact runtime phases; direct calls and old command replays cannot restart work.
- A guarded read-only planning phase, pinned local target branch that is not checked out, durable provider reservations, returned-model allowlist, and explicit start gate before Workers exist.
- Repositories with pre-existing linked Git worktrees, config includes, sparse/split indexes, grafts, shallow/partial history, alternates, or externally routed Git storage are rejected in v0.1.
- Python and TypeScript micro-repositories as acceptance fixtures.
- Revision-bound context, checks, approvals, and integration evidence.
- Coordinator-scheduled, Admission-owned candidate preparation/CAS through a sanitized host Git adapter, with repository commands confined to a restricted networkless executor.
- DeepSeek Responses API with `deepseek-v4-flash` as the sole real adapter; deterministic core tests use `ScriptedMockLLM`.
- CLI-only commands, a read-only loopback WebUI, and a sanitized fixture replay published through GitHub Pages.
- Required checks run offline from sanitized regular-file snapshots; symlinks and fixed plus host-local secret paths are hard denied.
- A locked Git-native Target Reservation is reused for the Run and removed by exact journaled terminal cleanup before purge.
- Publication scans both the tracked tree and full reachable Git history; terminal-only purge is approval-bound and crash-idempotent without touching Git, and purged queries expose only a minimal tombstone view.
- No external Coding Agent CLI or high-level agent framework in the assessed core.

The target user is a developer who needs long-running agent collaboration to remain inspectable and recoverable. Novelty is a hypothesis, not a claim: established projects already cover many individual mechanisms.

## Repository Map

- `SPEC.md`: frozen normative design, revision 3, signed at SHA-256 `E4385008CD75E4E3B0E70B25A6EBDFD976F3E1031F2ACD81FF0B6284EF6668AB`. Revision 1 (`2F1434AB…663BC`) was signed 2026-07-27 and superseded 2026-07-31 by approved proposal 0001; revision 2 (`97E9652D…E26D6`) was signed 2026-07-31 and superseded 2026-08-05 by approved proposal 0002, which replaced the model provider. Approval is recorded externally so the bytes remain unchanged between revisions.
- `docs/proposals/`: specification amendment proposals and their disposition.
- `INITIALIZATION.md`: accepted scope, prerequisites, stages, and remaining inputs.
- `CONTEXT.md`: canonical domain vocabulary.
- `docs/adr/`: accepted, hard-to-reverse decisions.
- `docs/architecture/`: explanatory system maps, subordinate to `SPEC.md`.
- `docs/research/`: primary-source landscape and value-hypothesis evidence.
- `docs/experiments/`: falsifiable experiments and fixture matrix.
- `docs/learning/`: interview-oriented notes created only after executable evidence exists.
- `SPEC_PROCESS.md` and `AGENT_LOG.md`: required development-process evidence.
- `LICENSE` and `NOTICE`: Apache-2.0 terms and the explicit scope exception for course-provided documents.

The selected target is a Python 3.12 wheel/`uv` CLI plus a digest-pinned executor for Windows 11 and Ubuntu 24.04 x86_64. Every `SPEC.md` section 10.5 host prerequisite was re-verified on 2026-07-31: Git 2.47.1, CPython 3.12.12, uv 0.9.29, a responding Docker daemon, and OS keyring support.

## Repository and License

The public GitHub remote is `https://github.com/Ztscream/ApexCrew.git` and is the sole remote. Original ApexCrew material is available under [Apache License 2.0](LICENSE); see [NOTICE](NOTICE) for the course-document exception.

The repository owner decided on 2026-07-31 that no NJU/GitLab remote will be configured and that delivery runs through GitHub. The course brief is internally inconsistent here: section 4.7 is titled "GitHub 仓库要求" and mandates a public GitHub repository with GitHub Actions CI, while section 5 describes submission through an NJU Git link. The owner resolved that inconsistency in favour of GitHub. A `.gitlab-ci.yml` carrying a job named exactly `unit-test` is still produced, because deliverable 6 names that file directly and because frozen `SPEC.md` section 10.5 requires the file's existence and job name, not a GitLab remote.

The course deadline is assumed to be 2026-08-10 23:59 Asia/Shanghai. The Python money-unit and TypeScript timestamp-unit fixture problems are included. No API credential value is needed for the offline core or delivery commands.

## 已知债务

Multi-intent recovery selects a member only from exactly one authoritative observation; ambiguity remains `INDETERMINATE`. Retention redacts/quarantines restricted diagnostics and applies the documented expiry/eviction order. The restricted executor launches only the digest-pinned, networkless Docker command and fails closed when Docker is unavailable.
