# ApexCrew Final User Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make ApexCrew usable by a developer who can configure DeepSeek, create a Run for a local Git repository, execute it through `CrewControl` and `CrewRuntime` with a one-use Runtime Permit, review required approvals, and inspect the resulting audit/query projection.

**Architecture:** Add one production composition root behind the existing A-Hybrid interfaces. The composition root owns adapter wiring only; scheduling, WorkerLoop behavior, authority, admission, recovery, and stop decisions remain in the existing domain/application modules. CLI commands submit typed commands and call the three public application interfaces; they do not coordinate internal modules or issue reusable authority.

**Tech Stack:** Python 3.12, Typer, Pydantic, SQLite, the existing Git adapters, restricted executor adapter, OS keyring, DeepSeek Responses API through `DeepSeekResponsesAdapter`, and `ScriptedMockLLM` for deterministic tests.

---

## Scope and Gate

The first user-facing acceptance path is one local repository, one user, one direct target branch, and at most three Workers. A Run must be able to reach a model-backed planning decision, pause for plan approval, resume through a fresh one-use Permit, schedule a Worker, and stop for action or final approval without mutating the target branch. Target mutation remains gated by the existing Admission/Grant contracts.

The frozen `SPEC.md` is not edited. Before source, fixture, test, or CI changes, the repository owner must create the required new M1 `PLAN.md` revision and obtain its independent document review. This plan is the implementation proposal to review; it is not itself that gate.

## Success Criteria

1. `apexcrew init --root <repo>` creates only non-sensitive config and validates the repository through the host Git adapter.
2. `apexcrew run-create --root <repo> --target-ref refs/heads/main ...` creates a durable Run with DeepSeek model configuration but makes no provider call.
3. `apexcrew run <run_id> --root <repo>` consumes exactly one current Runtime Permit and reaches an observable stop state; without a Permit it performs zero runtime mutation.
4. An opt-in live integration test can make one DeepSeek request when `APEXCREW_LIVE_SMOKE=1` and a credential is available; normal tests never use the network.
5. `apexcrew approve <run_id> ...` submits only the exact pending approval and cannot create a Grant for a different revision, action, or Run.
6. `apexcrew status` and `apexcrew show <run_id>` expose sanitized Run projections and never expose credentials, restricted transcripts, Grants, or quarantined content.
7. The offline composition uses `ScriptedMockLLM` with the same production composition root and proves the same Permit/approval lifecycle.

## File Map

- Create `src/apexcrew/application/composition.py`: production dependency graph and typed `ApplicationBundle`; no domain policy.
- Create `src/apexcrew/application/configuration.py`: parse and persist non-sensitive CLI configuration and user Run options.
- Create `src/apexcrew/adapters/repository/bootstrap.py`: real repository bootstrap authority using existing Git preflight and structured argv adapters.
- Create `src/apexcrew/adapters/model/factory.py`: select `DeepSeekResponsesAdapter` or `ScriptedMockLLM` from an approved model configuration.
- Create `src/apexcrew/adapters/runtime/file_lock.py`: concrete platform lock port required by production ownership.
- Modify `src/apexcrew/delivery/cli.py`: add typed Run creation, execution, approval, and sanitized query commands.
- Modify `src/apexcrew/application/runtime.py`: use the production lock port and keep invalid Permit acquisition side-effect free.
- Modify `src/apexcrew/application/queries.py`: expose the required sanitized single-Run projection.
- Modify `src/apexcrew/adapters/executor/restricted.py`: connect only the closed `argv` builder to the restricted runner contract; preserve fail-closed behavior on unsupported host platforms.
- Modify `README.md` and `SECURITY.md`: document the actual end-user flow, live-smoke opt-in, and remaining unsupported paths.
- Create `tests/contract/test_composition.py`, `tests/integration/test_cli_run_lifecycle.py`, `tests/integration/test_live_provider_smoke.py`, and focused unit tests beside each new adapter.

## Task 1: Production Configuration and Repository Bootstrap

**Files:**
- Create: `src/apexcrew/application/configuration.py`
- Create: `src/apexcrew/adapters/repository/bootstrap.py`
- Modify: `src/apexcrew/delivery/cli.py`
- Test: `tests/unit/application/test_configuration.py`
- Test: `tests/contract/test_repository_bootstrap.py`

- [ ] Write a failing test that parses a goal, constraints, acceptance criteria, repository root, and direct target ref while rejecting missing paths, non-Git roots, target worktree checkout, and unknown config keys.
- [ ] Run `uv run --python 3.12 pytest tests/unit/application/test_configuration.py tests/contract/test_repository_bootstrap.py -q`; observe the missing production configuration/bootstrap symbols.
- [ ] Implement immutable Pydantic configuration models and a `RepositoryBootstrapAuthorityService` that delegates inspection to `GitRepositoryPreflight`, returns the observed repository/target OIDs, and never reads `.env` or model credentials.
- [ ] Add `run-create` parsing that builds the existing typed `CreateRunPayload`; it must persist no credential value and must not dispatch a model call.
- [ ] Rerun the focused selector and assert `RUN_CREATED` with a durable Run ID and no provider calls.
- [ ] Run `uv run --python 3.12 mypy src`, `uv run --python 3.12 ruff check .`, and `git diff --check`.

## Task 2: Production Composition Root and DeepSeek Model Selection

**Files:**
- Create: `src/apexcrew/application/composition.py`
- Create: `src/apexcrew/adapters/model/factory.py`
- Modify: `src/apexcrew/adapters/model/deepseek_responses.py`
- Test: `tests/contract/test_composition.py`
- Test: `tests/integration/test_provider_selection.py`

- [ ] Write a failing test that builds an offline bundle with `ScriptedMockLLM`, creates a Run, and proves `CrewControl`, `CrewRuntime`, and `RunQueries` share the same SQLite state store and revision bindings.
- [ ] Run the selector and observe that no production bundle factory exists.
- [ ] Implement `ApplicationBundle` with only `control`, `runtime`, `queries`, and `close`; wire SQLite, repository bootstrap, authority, recovery, phase drivers, Coordinator, WorkerLoop, and the selected `ModelPort` through existing protocols.
- [ ] Implement model selection from the approved `ModelConfigurationRevisionDocument`: `scripted_mock` is available for tests; `deepseek_responses` constructs `DeepSeekResponsesAdapter` with the keyring port, exact schema, pricing, and versioned inference settings; unknown providers fail closed.
- [ ] Ensure the composition root does not issue a Runtime Permit, approve policy, or bypass `CrewControl`; callers must submit typed commands first.
- [ ] Rerun the offline selector and verify zero network calls with an exploding network fake.
- [ ] Run the focused type, lint, and diff checks.

## Task 3: Permit-Gated CLI Run Lifecycle

**Files:**
- Modify: `src/apexcrew/delivery/cli.py`
- Modify: `src/apexcrew/application/queries.py`
- Modify: `tests/unit/test_cli.py`
- Create: `tests/integration/test_cli_run_lifecycle.py`

- [ ] Write failing tests for: `run-create` returning a Run ID; `run` refusing when no current Permit exists; `run` consuming one Permit and returning the real stop reason; stale/replayed `continue` and `approve` commands producing zero mutation; and `show` omitting secret/restricted fields.
- [ ] Run the focused selector and observe the current hard-coded `NO_RUNTIME_PERMIT` behavior.
- [ ] Implement CLI commands as thin adapters: load the bundle, submit `CommandEnvelope` through `CrewControl.handle`, call `CrewRuntime.run_until_blocked` only after the control result supplies the current Permit, and render `RunQueries.get` as sanitized JSON.
- [ ] Add explicit exit codes for `AWAITING_PLAN_APPROVAL`, `AWAITING_ACTION_APPROVAL`, `AWAITING_FINAL_APPROVAL`, `INDETERMINATE`, and terminal success; do not print internal transcripts or credentials.
- [ ] Rerun the integration selector and verify a full offline lifecycle from create through approval pause using `ScriptedMockLLM`.
- [ ] Run the focused type, lint, and diff checks.

## Task 4: Real Repository and Worker Tool Path

**Files:**
- Create: `src/apexcrew/adapters/runtime/file_lock.py`
- Modify: `src/apexcrew/application/runtime.py`
- Modify: `src/apexcrew/adapters/executor/restricted.py`
- Modify: `src/apexcrew/adapters/repository/planning.py`
- Modify: `src/apexcrew/adapters/repository/snapshot.py`
- Test: `tests/unit/adapters/runtime/test_file_lock.py`
- Test: `tests/integration/test_real_repository_run.py`

- [ ] Write failing tests for cross-process ownership, manifest/read/search from a real regular-file Git snapshot, structured check execution, and refusal of symlinks, `.git`, secret paths, raw shell, network, and target-worktree mutation.
- [ ] Run the focused selector and observe the existing `RESTRICTED_EXECUTOR_RUNNER_NOT_CONNECTED` fail-closed boundary and missing production worker adapters.
- [ ] Implement platform file locking behind the adapter seam, keeping Permit validation before lock-directory creation.
- [ ] Connect the existing planning snapshot and closed argv builder to a restricted process runner that receives only sanitized snapshots and has no host credentials, network, or Docker socket.
- [ ] Wire the real repository implementation into the WorkerLoop tool port; Admission remains the sole issuer of typed CAS requests.
- [ ] Rerun the integration selector and verify that a Worker can produce a candidate/check result without mutating the user's checked-out target branch.
- [ ] Run the focused type, lint, and diff checks.

## Task 5: Opt-In DeepSeek Smoke and User Documentation

**Files:**
- Create: `tests/integration/test_live_provider_smoke.py`
- Modify: `README.md`
- Modify: `SECURITY.md`
- Modify: `Makefile`
- Test: `tests/contract/test_cli_credentials.py`

- [ ] Write a failing test proving the live smoke is skipped unless `APEXCREW_LIVE_SMOKE=1`, and that missing credentials fail closed without making a network call.
- [ ] Run the selector and observe the default skip.
- [ ] Implement one-call live smoke with an explicit opt-in, a bounded model request, observed response/model/usage checks, and no automatic test retry. Do not print the credential or prompt transcript.
- [ ] Document the exact workflow:

```text
uv sync --frozen --all-groups
uv run --python 3.12 apexcrew init --root <repo>
uv run --python 3.12 apexcrew credentials set
uv run --python 3.12 apexcrew run-create --root <repo> --target-ref refs/heads/main --goal "..."
uv run --python 3.12 apexcrew run <run-id> --root <repo>
uv run --python 3.12 apexcrew show <run-id> --root <repo>
```

- [ ] Add `make live-smoke` that requires the explicit environment gate and never runs in normal `make test` or CI.
- [ ] Run documentation checks, the complete offline suite, secret scan, wheel build, and `git diff --check`.

## Review and Delivery Protocol

Each task is implemented in its own worktree with one Conventional Commit, an `AGENT_LOG.md` entry, and the required `PLAN-Task`, `Subagent`, `Human-Changes`, `Spec-Review`, and `Quality-Review` trailers. The spec-compliance review runs before the quality review. No live smoke, push, PR creation, or credential value is performed by default. The final claim requires observed command output for every success criterion.

