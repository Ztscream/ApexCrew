# ApexCrew M2-M4 Final Production Implementation Plan

> **For agentic workers:** Follow this plan task-by-task. Every task uses TDD, an isolated worktree/branch, one Conventional Commit, and ordered spec-compliance then quality review evidence.

**Goal:** Turn the existing M2-M4 offline skeleton into a locally usable, fail-closed ApexCrew release that can run the composed `CrewControl`/`CrewRuntime` graph with `ScriptedMockLLM` or an explicitly authorized DeepSeek request, while preserving Runtime Permit, approval, Grant, Admission, CAS, recovery, retention, executor, replay, and release boundaries.

**Architecture:** Keep the A-Hybrid application surface (`CrewControl`, `CrewRuntime`, `RunQueries`) unchanged. Complete missing behavior behind existing ports and composition adapters; keep CLI as the sole mutation entry point and WebUI/Pages as sanitized read-only replay. Unsupported host capabilities remain typed failures, never successful stubs.

**Tech Stack:** Python 3.12, Pydantic, SQLite, Typer, FastAPI, Docker, Git, pytest, Ruff, mypy, GitHub Actions, and the existing DeepSeek Responses adapter.

---

## Scope and completion contract

`SPEC.md` is frozen and is not modified. The old SPRINT depth labels are historical evidence only; M2-M4 completion requires no active production path to return a deferred-boundary success, no `DEBT-M2-*` marker in source, green offline verification, and explicit documentation of any owner-only external action. `NotImplementedError` in Protocol methods is allowed when the method is an abstract port and has a concrete production implementation in the active composition graph.

Every implementation task must record: base SHA, red selector and observed failure, green selector, `uv run mypy src`, `uv run ruff check src tests`, `uv run ruff format --check src tests`, `git diff --check`, spec review, quality review, `PLAN-Task`, `Subagent`, and `Human-Changes` in `AGENT_LOG.md`.

## File map

- `src/apexcrew/application/composition.py`, `runtime.py`, `control.py`, `queries.py`: application wiring, Permit delivery, runtime ownership, and public reads.
- `src/apexcrew/domain/indeterminate.py`, `retention.py`, `model.py`, `recovery.py`, `tools.py`: durable policy and typed domain contracts.
- `src/apexcrew/adapters/executor/restricted.py`, `src/apexcrew/adapters/executor/runner.py`, `src/apexcrew/adapters/repository/detached_workspace.py`, `adapters/system.py`, `adapters/state/sqlite.py`: host/process and persistence adapters.
- `src/apexcrew/delivery/cli.py`, `replay.py`, `web.py`: user commands and read-only projections.
- `tests/unit`, `tests/integration`, `tests/contract`, `tests/acceptance`: deterministic proof; live provider and Docker image tests are explicitly opt-in only.
- `.github/workflows/ci.yml`, `scripts/`, `Makefile`, `README.md`, `SECURITY.md`, `docs/deployment.md`, `docs/design-workbench.md`: release and operator boundary.

## Execution tasks

### Task M2-01: Complete Production Composition and Permit-Gated CLI

**Files:** `src/apexcrew/application/composition.py`, `src/apexcrew/application/runtime.py`, `src/apexcrew/application/control.py`, `src/apexcrew/application/queries.py`, `src/apexcrew/adapters/state/sqlite.py`, `src/apexcrew/delivery/cli.py`; tests in `tests/integration/test_production_wiring.py`, `tests/integration/test_composed_runtime_lifecycle.py`, `tests/integration/test_cli_run_lifecycle.py`, `tests/contract/test_composition.py`.

- [x] Add red tests proving normal planning proposals do not carry recovery bindings, active bundles contain concrete Worker/phase adapters, and CLI delivery consumes exactly one current Runtime Permit.
- [x] Bind live Run revision digests to planning, Worker, Git, and model requests; validate repository identity before constructing effect adapters.
- [x] Route typed CLI commands through `CrewControl.handle`, pass only the accepted Permit to one `CrewRuntime.run_until_blocked`, and render only `RunQueries` fields.
- [x] Prove approval stops, stale/replayed commands, process restart, target reservation, and terminal cleanup through public interfaces.
- [x] Commit `feat(runtime): complete production composition and cli lifecycle` with ordered reviews; final detached workspace wiring landed in `dd7b192`.

### Task M2-02: Complete Multi-Intent Recovery and Cross-Process Runtime Ownership

**Files:** `src/apexcrew/domain/indeterminate.py`, `src/apexcrew/application/runtime.py`, `src/apexcrew/adapters/system.py`; tests in `tests/integration/test_indeterminate_resolution.py`, `tests/integration/test_runtime_lock_lifecycle.py`, and new recovery selectors.

- [x] Add red tests for an objectively observable single member, ambiguous members remaining `INDETERMINATE`, second-process lock rejection, invalid-Permit zero side effects, and dead-holder reconciliation.
- [x] Implement observation-only resolution selection with canonical set/generation binding; no model output or wall-clock guess may choose a member.
- [x] Make the OS lock acquisition validate Permit before creating lock paths and expose holder loss as an indeterminate recovery result.
- [x] Commit `feat(recovery): complete observed resolution and runtime ownership` with ordered reviews.

### Task M2-03: Implement Retention, Redaction, Quarantine, and Eviction

**Files:** `src/apexcrew/domain/retention.py`, `src/apexcrew/domain/projection.py`, `src/apexcrew/adapters/state/sqlite.py`, `src/apexcrew/delivery/replay.py`; tests in new `tests/integration/test_retention_tiers.py` and `tests/integration/test_retention_eviction.py`.

- [x] Add red tests for credential replacement before persistence, token/private-key quarantine, byte-preserving capped previews, Tier 1-only export, expiry-before-terminal eviction, active-run preservation, and metadata-only overflow tombstones.
- [x] Implement typed retention records with known-secret replacement, pattern quarantine, fixed preview caps, digest/length metadata, and export filtering.
- [x] Implement the exact eviction order and idempotent durable tombstone behavior without deleting active Run content.
- [x] Remove only the closed M2 retention debt markers and update README/SECURITY from observed behavior.
- [x] Commit `feat(retention): implement redaction and eviction policy` with ordered reviews.

### Task M2-04: Connect the Restricted Executor Process Runner

**Files:** `src/apexcrew/adapters/executor/restricted.py`, `src/apexcrew/domain/tools.py`, `src/apexcrew/domain/revisions.py`; tests in `tests/contract/test_executor.py` and new Docker integration selectors.

- [x] Add red tests that run the committed image when Docker is available and otherwise assert a typed, fail-closed capability result: non-root, no network, read-only root, dropped capabilities, no-new-privileges, PID/CPU/memory limits, allowlisted environment, and discarded writes.
- [x] Implement structured-argv execution through the already closed command builder, bounded output and timeout handling, and typed unobservable outcomes. Never invoke shell text or pass credentials.
- [x] Keep Docker tests explicitly skipped with reason when the daemon/image prerequisite is absent; never convert that skip to success.
- [x] Commit `feat(executor): connect restricted process runner` with ordered reviews; final snapshot/runner hardening landed in `dd7b192`.

### Task M2-05: Finish Acceptance Fixtures and Fresh-Process Evidence

**Files:** `fixtures/`, `tests/acceptance/`, `tests/integration/`, `AGENT_LOG.md`, `SPRINT.md`; preserve fixture threat model and no-follow rules.

- [x] Replace boundary-only fixture assertions with end-to-end money-unit and timestamp-unit runs through the public application interfaces.
- [x] Add restart, crash-reconciliation, reservation-cleanup, replay, and hostile-repository evidence selectors.
- [x] Commit `test(acceptance): prove fixture and restart workflows` with ordered reviews.

### Task M3-01: Harden Static Replay and Read-Only WebUI

**Files:** `src/apexcrew/delivery/replay.py`, `src/apexcrew/delivery/web.py`, `scripts/build_webui.py`, `tests/unit/test_replay_web.py`, `tests/unit/test_webui_build.py`, `docs/deployment.md`, `README.md`.

- [x] Add red tests for hostile HTML/attribute/URL content, no network fetches, one-time loopback session, and absence of mutation/provider/credential paths.
- [x] Render only sanitized `RunQueries` frames with fixed escaping and bounded fields; static export must be deterministic and contain no secrets or restricted payloads.
- [x] Commit `feat(web): harden sanitized read-only replay` with ordered reviews.

### Task M3-02: Complete CI, Packaging, Secret Scan, and Performance Contracts

**Files:** `.github/workflows/ci.yml`, `.gitlab-ci.yml`, `Makefile`, `scripts/`, `tests/contract/test_bootstrap_ci.py`, `tests/contract/test_release_artifacts.py`, new performance contract tests.

- [x] Add red contract tests for exact quality/unit/integration/build/pages/browser/performance job names, same-SHA artifact identity, tracked-tree/history secret scanning, wheel/image/static build outputs, and bounded performance reports.
- [x] Implement deterministic offline CI and release-verifier contracts with no local push/tag/publication. External Pages/GHCR/PyPI actions remain owner-authorized release operations.
- [x] Commit `build(ci): complete delivery and performance gates` with ordered reviews.

### Task M4-01: Deliver the Design Workbench Artifact

**Files:** `docs/design-workbench.md`, `tests/contract/test_design_workbench.py`, `README.md`.

- [x] Replace the status-only stub with a concrete, reviewable read-only workbench artifact: goal, constraints, candidate graph, evidence requirements, approval states, and UI state catalogue.
- [x] Add contract tests proving it cannot issue Permit/Grant/CAS/model/Git/Docker/credential operations and remains subordinate to `CrewControl`/`RunQueries`.
- [x] Commit `docs(design): deliver read-only workbench artifact` with ordered reviews.

### Task M4-02: Final Provider and End-User Documentation Gate

**Files:** `src/apexcrew/adapters/model/deepseek_responses.py`, `src/apexcrew/domain/revisions.py`, `src/apexcrew/domain/model.py`, `tests/contract/test_deepseek_responses_adapter.py`, `tests/integration/test_live_provider_smoke.py`, `tests/integration/test_live_cli_run_lifecycle.py`, `README.md`, `SECURITY.md`, `Makefile`.

- [x] Add red tests for inference settings coming from the approved revision, returned-model/usage/status fail-closed settlement, zero network on missing credentials, and default-skipped live smoke.
- [x] Record effective inference parameters in attempts, enforce `max_retries=0`, request-time credential resolution, one bounded request, and no ordinary-run live provider path.
- [x] Document the exact local DeepSeek flow and explicitly state that live smoke requires operator authorization; do not execute it in this task.
- [x] Commit `feat(provider): close final DeepSeek user path` with ordered reviews.

### Task M4-03: Final Same-Revision Release Audit

**Files:** `scripts/`, `tests/contract/test_release_artifacts.py`, `AGENT_LOG.md`, `SPRINT.md`, `README.md`, `SECURITY.md`.

- [x] Run offline suite, type/lint/format/diff checks, demo, secret scan, package/static/image build checks, and fresh-process checks; record exact outputs and platform skips.
- [x] Verify no active source `DEBT-M2-*` markers and classify abstract Protocol `NotImplementedError` separately from deferred production behavior.
- [x] Record final SHA, changed paths, task commit map, ordered review verdicts, and owner-only external release prerequisites. Do not claim hosted release without observed owner action.
- [x] Commit `docs(release): close m2-m4 evidence ledger` with ordered reviews; implementation base is `dd7b192`.

## Self-review checklist

- [x] Every M2 S5-S14 capability maps to M2-01 through M2-05.
- [x] M3 replay, CI, performance, and artifact requirements map to M3-01 and M3-02.
- [x] M4 provider, workbench, and same-revision release requirements map to M4-01 through M4-03.
- [x] No task authorizes push, PR merge, Pages enablement, release tag, credential acquisition, or live smoke.
- [x] No task treats a Protocol abstract method as an unfinished production path.
