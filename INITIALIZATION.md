# ApexCrew Initialization Baseline

> Status: accepted discovery baseline, 2026-07-26. **Evidence-Driven Durable Crew is the only product mainline.** This is an initialization record, not `SPEC.md`; three formal brainstorming rounds and a cold-start review are still required before implementation may be merged or retained.

## 1. Product Thesis

**Accepted direction**: ApexCrew is a local-first, recoverable, evidence-driven collaboration harness for at most three coding Workers in one Git repository.

The value hypothesis is narrower than “multi-agent + long context + continuous cowork,” which existing projects already cover:

> During long-running dependent coding tasks, revision-bound context and verification evidence, dependency-aware invalidation, and durable recovery reduce stale handoffs and unverified integration compared with ordinary task/worktree orchestration.

This is falsifiable and is not a claim of global novelty. Bernstein, h5i, and other projects overlap many individual mechanisms, so ApexCrew's defensible contribution is a small, self-built combination of evidence validity and durable coordination. Adversarial acceptance and weak-oracle checks remain supporting experiments for evidence quality, not a second direction. See [ADR-0001](docs/adr/0001-evidence-driven-durable-crew.md) and [the primary-source landscape](docs/research/github-agent-landscape.md).

## 2. Course Boundary

The A-class submission must implement its own:

- **Coordinator loop**: advance a bounded task DAG, allocate leases, invalidate stale work, pause/resume safely, and serialize integration;
- **worker loop**: assemble context, make one model call, parse one structured action, execute a tool, return feedback, and stop;
- deterministic tool, feedback, governance, memory, stop, and configuration mechanisms.

`ScriptedMockLLM` is the first provider. A production adapter may later expose a low-level single-completion interface through `ModelPort`. Codex, Claude Code, Gemini CLI, AutoGen, CrewAI, LangGraph, or another hosted agent runner may be an experiment only after the core is complete; none may implement or substantiate the assessed loops.

## 3. MVP Scope

The first usable slice supports one local user, one repository, one process, at most three Workers, an explicit bounded task DAG, file/path-glob write sets, per-attempt Git worktrees, SQLite persistence, and serial integration. Python and TypeScript micro-repositories are the accepted fixture matrix.

Workers receive typed Context Capsules rather than full chat history. Checks use repository-declared structured `argv`; an Evidence Receipt records the command, output digest, exit status, and exact Git revision. A dependency, contract, policy, or revision change marks affected capsules and receipts stale before they can be reused or injected into another Worker.

Risk policy returns `ALLOW`, `DENY`, or `REQUIRE_APPROVAL`. Workspace escape and secret access are hard denials. An Approval Grant freezes the action and binds run, action digest, workspace revision, policy revision, expiry, and one use.

Non-goals for v0.1: symbol-level ownership, arbitrary shell, automatic push/merge/release, vector memory, dynamic agent societies, more than three Workers, remote/multi-user operation, A2A, Kubernetes, plugin marketplace, production public execution, or provider breadth. Weak-oracle challenge is an experiment, not a mandatory alternate workflow.

## 4. Decisive Demonstrations

1. **Feedback correction**: a scripted Worker applies a wrong patch; a real check fails; structured evidence returns to the Worker; its next action fixes the patch; only fresh green evidence permits handoff.
2. **Coordination freshness**: two isolated worktrees execute dependent Task Contracts. Upstream integration invalidates the downstream capsule and evidence, forces refresh and revalidation, and prevents a stale candidate from entering the integration queue. Run the same scenario on Python and TypeScript fixtures.
3. **Governance and recovery**: workspace escape is denied with zero side effects; risky-action approval cannot be modified, replayed, or reused; injected crashes resume without duplicate action or integration. Uncertain external side effects enter `INDETERMINATE` for human resolution.
4. **Supporting evidence-quality challenge**: a known flawed patch that passes visible checks is not described as proven correct; a bounded challenger may add a reproducible counterexample and measure its cost. This experiment cannot redefine the product mainline.

All mechanism demos run offline with `ScriptedMockLLM` and real temporary repositories/processes. Go/no-go conditions are objective: stale evidence is never accepted, unapproved dangerous actions have zero side effects, recorded actions are not replayed after restart, and no LLM-as-judge determines correctness.

## 5. Architecture Baseline

```mermaid
flowchart TB
    UI["CLI / FastAPI + HTMX"] --> API["ApexCrew start / advance / inspect"]
    API --> COORD["Coordinator Loop"]
    COORD --> BOARD["Task Contracts, DAG, leases"]
    COORD --> WORKER["ApexCrew WorkerLoop, max 3"]
    WORKER --> CAPSULE["Context Capsule builder"]
    WORKER --> POLICY["Governance and approval"]
    WORKER --> RUNTIME["Repository / worktree / process tools"]
    RUNTIME --> GATE["Verifier and evidence gate"]
    BOARD --> INVALIDATE["Freshness / invalidation graph"]
    GATE --> INVALIDATE
    COORD --> STORE["SQLite run and event store"]
    CAPSULE --> STORE
    POLICY --> STORE
    WORKER --> MODEL["Low-level ModelPort: scripted first"]
```

`core/` owns both loops, contracts, state transitions, freshness, evidence rules, and stop budgets. `adapters/` owns Git/worktrees, subprocesses, SQLite, credentials, and model integrations. `bootstrap/` composes validated configuration. `delivery/` exposes the same public harness interface through CLI and the required WebUI. Provider SDKs and FastAPI never enter `core/`. See [the system overview](docs/architecture/system-overview.md).

## 6. Documentation System

| Artifact | Purpose | Update trigger |
|---|---|---|
| `SPEC.md` | Single design truth and acceptance criteria | Approved requirement/design change |
| `PLAN.md` | Tasks, dependencies, red/green commands, commit hashes | Every task transition |
| `SPEC_PROCESS.md` | Three brainstorming iterations and cold-start defects | Each spec iteration/review |
| `AGENT_LOG.md` | Timestamped facts, prompts, failures, human fixes | Every agent task/session |
| `CONTEXT.md` | Canonical domain vocabulary only | A term is resolved or corrected |
| `docs/adr/` | Few hard-to-reverse trade-offs | A qualifying decision is accepted |
| `docs/architecture/` | Explanatory component and flow maps subordinate to SPEC | An accepted structural boundary changes |
| `docs/experiments/` | Falsifiable hypotheses, fixtures, measures, and results | Before and after each experiment |
| `docs/research/` | Primary-source landscape and value validation | Research checkpoint |
| `docs/learning/` | Interview explanations linked to code/tests | A mechanism reaches green/refactor |
| `README.md` / `SECURITY.md` | Operation, demo, and trust boundary | User-facing/security change |
| `REFLECTION.md` | Student-authored final critique | Final phase only |

Do not create parallel architecture truths. Learning notes explain concepts and link to the SPEC, ADRs, commits, and tests; they do not restate requirements.

## 7. Technical Baseline and Prerequisites

| Prerequisite | State on 2026-07-26 | Consequence |
|---|---|---|
| A-class project and direction | Accepted | Evidence-Driven Durable Crew is the only mainline. |
| Scope | Accepted | One repository, at most three Workers, Python + TypeScript fixtures. |
| Core ownership | Accepted | ApexCrew owns both loops; only low-level model completion APIs sit behind `ModelPort`. |
| LLM provider decision | Required by Stage 2 exit | `SPEC.md` must name the provider/model and rationale, or explicitly justify a MockLLM-only submission; the API key value is not needed for design. |
| Local toolchain | Available | Git 2.47.1, Python 3.11.5, uv 0.9.29, Node 22.14, Make 4.4.1, Docker CLI 29.6.1, and WSL2 Ubuntu were observed. |
| Docker daemon | Verified | Local server 29.6.1 responded; image/build behavior is tested during scaffold and distribution stages. |
| Superpowers | Installed and enabled | `superpowers@openai-curated`, manifest 5.1.3, cache revision `11c74d6b`; required workflow skill directories are present. Start design in a session where the plugin skills are loaded. |
| Local Git baseline | Completed by this initialization | Branch `main`; governance and documentation only. |
| Public personal GitHub remote | Configured | `origin` is `https://github.com/Ztscream/ApexCrew.git`; it was verified empty before the first non-force publication. |
| Course submission remote | Explicitly deferred | NJU/GitLab is not considered during current GitHub development but remains a final course-delivery dependency. |
| License | Accepted | Original ApexCrew work uses Apache-2.0; `NOTICE` excludes course-provided requirement documents from relicensing. |
| LLM credential value | Not required | `SPEC.md` defines storage and threat handling; an actual secret is supplied only for the provider slice and is never committed. |

Recommended implementation defaults, subject to formal brainstorming, are Python 3.11, uv, Pydantic, stdlib SQLite, pytest, Ruff, mypy, FastAPI/HTMX, OS keyring, and a non-root Linux Docker image. Windows is the development host; tests and distribution target Linux portability.

No `src/`, tests, `SPEC.md`, or `PLAN.md` belongs in the initialization commit.

## 8. Stages and Gates

| Stage | Output | Exit gate |
|---|---|---|
| 0. Discovery (complete) | Landscape, value hypothesis, alternatives, vocabulary | User accepted one direction and non-goals |
| 1. Repository/process setup (complete) | Git/GitHub, Superpowers, license, ignore/security baseline, `AGENT_LOG.md` | GitHub `main` contains the verified governance baseline |
| 2. Brainstorming (next) | `SPEC.md`, `SPEC_PROCESS.md`, scenarios, state/data/threat model | Three user-approved iterations and every `SPEC_PROCESS.md` Stage 2 checklist item satisfied |
| 3. Planning and fixtures | Python/TypeScript fixtures; 2-5 minute tasks in `PLAN.md` | Every task has dependencies and red/green evidence |
| 4. Independent cold start | Different Agent attempts 1-2 tasks from SPEC/PLAN only | Revisions remove all blocking ambiguity |
| 5. Scaffold | Package, offline test harness, lint/type CI, `ScriptedMockLLM` | One red-green vertical smoke slice |
| 6. Core vertical slices | Worker feedback, contracts/leases, freshness gate, approval, recovery | Decisive demos pass offline on both fixtures |
| 7. Productization | WebUI, credentials, Docker, GitHub Actions, `.gitlab-ci.yml` `unit-test` | Fresh-machine run, public demo, and required CI pass |
| 8. Evaluation | Comparisons, docs, demo script, security review | Spec and quality reviews pass; student writes reflection |

## 9. Information Still Needed

GitHub publication and licensing are resolved. The NJU/GitLab remote is explicitly deferred by the user; retain it as a final delivery dependency rather than a current blocker.

Before Stage 2 can exit: provide the course deadline, weekly time budget, selected LLM provider/model and selection rationale, whether the required final WebUI may use MockLLM only, and whether Windows development plus Linux Docker distribution is acceptable. Approve or replace the proposed fixture problems during brainstorming; both Python and TypeScript are already accepted fixture ecosystems. The credential storage design and threat model also belong in `SPEC.md`.

Deferrable until the provider implementation slice: the actual API credential and production rate/cost values. The offline core must still become green with `ScriptedMockLLM` before tests depend on a real provider.
