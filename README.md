# ApexCrew

ApexCrew is a local-first Coding Agent Harness for an **Evidence-Driven Durable Crew**. It coordinates bounded work in one Git repository, requires human approval for risky actions, and accepts changes only when objective evidence is fresh for the exact revision being integrated.

## Status

This repository is intentionally documentation-only. Discovery, all three specification-brainstorming rounds, the A-Hybrid implementation architecture, independent review, final written-spec sign-off, the R3 `PLAN.md`, and the Stage 4 implementation cold-start review are complete; that review returned zero blockers on 2026-07-31 and its disposable code was destroyed without merge. Do not add persistent implementation until a new M1 `PLAN.md` revision passes its own independent document review.

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
- OpenAI Responses API with `gpt-5.6-terra` as the sole real adapter; deterministic core tests use `ScriptedMockLLM`.
- CLI-only commands, a read-only loopback WebUI, and a sanitized fixture replay published through GitHub Pages.
- Required checks run offline from sanitized regular-file snapshots; symlinks and fixed plus host-local secret paths are hard denied.
- A locked Git-native Target Reservation is reused for the Run and removed by exact journaled terminal cleanup before purge.
- Publication scans both the tracked tree and full reachable Git history; terminal-only purge is approval-bound and crash-idempotent without touching Git, and purged queries expose only a minimal tombstone view.
- No external Coding Agent CLI or high-level agent framework in the assessed core.

The target user is a developer who needs long-running agent collaboration to remain inspectable and recoverable. Novelty is a hypothesis, not a claim: established projects already cover many individual mechanisms.

## Repository Map

- `SPEC.md`: frozen normative design, revision 2, signed at SHA-256 `97E9652D874B606C1673867923C97C29834F63B43ADB3F3E89779B13183E26D6`. Revision 1 (`2F1434AB…663BC`) was signed 2026-07-27 and superseded 2026-07-31 by approved proposal 0001. Approval is recorded externally so the bytes remain unchanged between revisions.
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

There are no build, test, or run commands yet. Proposed commands in `AGENTS.md` are interface commitments, not current capabilities. The selected target is a Python 3.12 wheel/`uv` CLI plus a digest-pinned GHCR executor for Windows 11 and Ubuntu 24.04 x86_64. Every `SPEC.md` section 10.5 host prerequisite was re-verified on 2026-07-31: Git 2.47.1, CPython 3.12.12, uv 0.9.29, a responding Docker daemon, and OS keyring support.

## Repository and License

The public GitHub remote is `https://github.com/Ztscream/ApexCrew.git` and is the sole remote. Original ApexCrew material is available under [Apache License 2.0](LICENSE); see [NOTICE](NOTICE) for the course-document exception.

The repository owner decided on 2026-07-31 that no NJU/GitLab remote will be configured and that delivery runs through GitHub. The course brief is internally inconsistent here: section 4.7 is titled "GitHub 仓库要求" and mandates a public GitHub repository with GitHub Actions CI, while section 5 describes submission through an NJU Git link. The owner resolved that inconsistency in favour of GitHub. A `.gitlab-ci.yml` carrying a job named exactly `unit-test` is still produced, because deliverable 6 names that file directly and because frozen `SPEC.md` section 10.5 requires the file's existence and job name, not a GitLab remote.

The course deadline is assumed to be 2026-08-10 23:59 Asia/Shanghai. The owner raised available capacity to 30-40 hours/week on 2026-07-31. Stages 2 and 3 are complete and the Stage 4 cold-start review returned zero blockers; the immediate next gate is a new M1 `PLAN.md` revision with its own independent document review. The Python money-unit and TypeScript timestamp-unit fixture problems are approved. No API credential value is needed until the offline `ScriptedMockLLM` core is green and the opt-in provider slice begins.
