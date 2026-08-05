# ApexCrew Final User Runtime Execution Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver a locally runnable ApexCrew Run that uses an approved DeepSeek or scripted model, consumes one Runtime Permit before effects, pauses for typed approvals, and exposes only a sanitized projection.

**Architecture:** Keep `CrewControl`, `CrewRuntime`, and `RunQueries` as the only application-facing interfaces. Make the composition root load current persisted revisions and bind real repository, planning, Worker, candidate, and integration adapters; CLI remains a thin typed-command adapter. Unsupported host capabilities fail closed and are documented, while the offline lifecycle uses the same graph with `ScriptedMockLLM`.

**Tech Stack:** Python 3.12, Typer, SQLite, Pydantic, existing Git no-follow adapters, existing authority/admission/recovery state machine, restricted executor boundary, OS keyring, DeepSeek Responses API, pytest, mypy, Ruff.

---

### Task 1: Fix Planning Submission and Revision Binding

**Files:**
- Modify: `src/apexcrew/adapters/state/sqlite.py:4963-5417`
- Modify: `src/apexcrew/application/composition.py:375-809`
- Modify: `src/apexcrew/adapters/repository/bootstrap.py:60-69`
- Test: `tests/integration/test_composed_runtime_lifecycle.py`
- Test: `tests/contract/test_composition.py`

- [ ] Add a regression assertion that a normal model `submit_plan` action reaches `AWAITING_PLAN_APPROVAL` with `recovered_marker=None` and `permit=None`.
- [ ] Run `uv run pytest tests/integration/test_composed_runtime_lifecycle.py::test_composed_runtime_reaches_final_approval_with_real_git_reservation -q` and observe `RECOVERED_MARKER_PERMIT_BINDING_MISMATCH` before implementation.
- [ ] Pass `recovered_logical_turn_id` to `persist_plan_proposal()` only when both recovery arguments are present; normal proposals pass all three recovery values as `None`.
- [ ] Make planning manifests, planning requests, and Worker requests derive Policy, Budget, and Model documents from the current Run revision digests. Do not use `default_revision_documents()` for a live Run after creation.
- [ ] Validate the opened repository's identity/storage digest against the persisted Run binding before constructing planning or Git effect adapters; reject a replaced repository instance before Git dispatch.
- [ ] Remove the lifecycle test's direct SQLite/coordinator tracing and keep only public-interface assertions.
- [ ] Rerun the regression and composition selectors; expected result is a planning approval stop with no binding mismatch.
- [ ] Run `uv run mypy src`, `uv run ruff check src tests`, `uv run ruff format --check src tests`, and `git diff --check`.

### Task 2: Replace Deferred Runtime Boundaries

**Files:**
- Modify: `src/apexcrew/application/composition.py:329-336,554-563,830-836`
- Modify: `src/apexcrew/domain/tools.py:708-1038`
- Modify: `src/apexcrew/adapters/repository/granted_workspace.py`
- Modify: `src/apexcrew/adapters/executor/restricted.py`
- Test: `tests/integration/test_production_wiring.py`
- Test: `tests/integration/test_real_repository_run.py`

- [ ] Add assertions that the active bundle's Worker tool port is a `ScopedToolRuntime`, phase drivers are concrete adapters, and no active path returns `RUNTIME_PHASE_NOT_IMPLEMENTED`.
- [ ] Run the new assertions and observe the existing `_CompositionWorkerTools`/`_CompositionPhaseDriver` behavior fail closed before implementation.
- [ ] Build the scoped tool runtime with the persisted reservation/workspace, current Policy/Plan bindings, Authority/Grant checks, planning snapshot reader, and typed action envelopes.
- [ ] Connect checks and patches to the existing restricted executor port. If the host cannot provide the required restricted runner, return `RESTRICTED_EXECUTOR_RUNNER_NOT_CONNECTED` as a typed result without claiming success.
- [ ] Implement concrete resolution, candidate/evidence, integration, and cleanup phase drivers by delegating to existing state/admission protocols; no driver may fabricate a Permit, Grant, Candidate, or CAS result.
- [ ] Prove one typed Worker action creates an approval stop and one granted action settles exactly one tool intent.
- [ ] Run `uv run pytest tests/integration/test_production_wiring.py tests/integration/test_real_repository_run.py -q` and require no deferred-boundary or successful-stub evidence.

### Task 3: Implement the Permit-Gated CLI Lifecycle

**Files:**
- Modify: `src/apexcrew/delivery/cli.py:190-480`
- Modify: `src/apexcrew/application/queries.py`
- Test: `tests/unit/test_cli.py`
- Test: `tests/integration/test_cli_run_lifecycle.py`

- [ ] Add failing CLI tests for `show`, `begin-planning`, `approve-plan`, `start`, `grant`, `integrate`, and `run` with no Permit.
- [ ] Run the selector and observe the current unconditional `NO_RUNTIME_PERMIT` implementation.
- [ ] Load one bundle against the repository root, submit typed `CommandEnvelope`s through `CrewControl.handle`, obtain the current Permit only from the accepted control outcome, and call `CrewRuntime.run_until_blocked` exactly once for that delivery.
- [ ] Render only `RunQueries.get` fields; map approval, indeterminate, terminal, and failure stops to stable exit codes without exposing nonce, credential, transcript, or restricted payload bytes.
- [ ] Prove replayed commands and stale approvals append no second effect, and reopening SQLite preserves the Run binding and rejects all old specialized commands.
- [ ] Run `uv run pytest tests/unit/test_cli.py tests/integration/test_cli_run_lifecycle.py -q`, then mypy/Ruff/diff checks.

### Task 4: DeepSeek Opt-In and Delivery Evidence

**Files:**
- Create: `tests/integration/test_live_provider_smoke.py`
- Create: `tests/integration/test_live_cli_run_lifecycle.py`
- Modify: `src/apexcrew/adapters/model/deepseek_responses.py`
- Modify: `README.md`
- Modify: `SECURITY.md`
- Modify: `Makefile`
- Test: `tests/contract/test_cli_credentials.py`

- [ ] Add a default-skipped live test and a missing-credential test that makes zero network calls.
- [ ] Require `APEXCREW_LIVE_SMOKE=1`, one bounded request, no SDK retries, request-time credential lookup, and sanitized observed response/usage evidence.
- [ ] Document the exact local CLI flow, the approval/Permit sequence, the read-only WebUI boundary, and every unsupported restricted-executor capability.
- [ ] Add a `make live-smoke` target excluded from `make test`, normal CI, and offline lifecycle evidence.
- [ ] Run offline tests, the default live selector, documentation checks, secret scan, wheel build, and `git diff --check`.

### Completion Audit

- [ ] Verify zero provider calls before revision approvals and exactly one current Permit consumption per accepted runtime delivery.
- [ ] Verify target branch OID is unchanged until the final typed integration CAS.
- [ ] Verify plan/action/final approval stops and sanitized `show` output from a reopened process.
- [ ] Verify DeepSeek is explicit opt-in and credentials never appear in request payloads, journal rows, logs, executor environment, or projections.
- [ ] Record red/green, spec review, quality review, and commit trailers in `AGENT_LOG.md` and update the R4 ledger only after evidence is observed.
