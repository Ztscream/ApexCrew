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
5. Specialized approval commands (`approve-policy`, `approve-budget`, `approve-model`, `approve-plan`, `grant`, and `integrate`) submit only their exact typed pending approval and cannot create authority for a different revision, action, or Run. The old generic `approve` stub is removed from the supported R4 CLI.
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
- Create `tests/contract/test_composition.py`, `tests/integration/test_cli_run_lifecycle.py`, `tests/integration/test_live_provider_smoke.py`, `tests/integration/test_live_cli_run_lifecycle.py`, and focused unit tests beside each new adapter.

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

- [ ] Write failing tests for: `run-create` returning a Run ID; `run` refusing when no current Permit exists; `run` consuming one Permit and returning the real stop reason; stale/replayed `continue`, `resume`, and each specialized approval command producing zero mutation; and `show` omitting secret/restricted fields.
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
- [ ] Implement `test_live_cli_run_lifecycle.py` as a separately gated live test: with `APEXCREW_LIVE_SMOKE=1` and a real keyring credential, invoke the CLI approval, Permit, and runtime sequence through the `deepseek_responses` composition, allow at most one provider request, assert the observed stop state, and prove the sanitized output contains no credential or restricted transcript. Missing credentials fail closed before network dispatch. This test is never collected by normal offline CI.
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

Each module owns one worktree, branch, and corresponding PR. Within that module worktree, every A/B task uses a fresh implementation subagent, one distinct Conventional Commit, an `AGENT_LOG.md` entry, and the required `PLAN-Task`, `Subagent`, `Human-Changes`, `Spec-Review`, and `Quality-Review` trailers. The paired tasks never share a commit or reviewer identity. The spec-compliance review runs before the quality review. No live smoke, push, PR creation, or credential value is performed by default. The final claim requires observed command output for every success criterion.

## Task-Level Delivery Matrix

The module rows above are not sufficient task specifications. Each row below is one implementation commit in the module worktree, with a fresh implementation subagent and two ordered fresh reviewers. The red command is run after the failing tests are written and before implementation; the green command is the same selector after the minimum implementation. A red result is expected and must be recorded, not suppressed.

| Task | Exact files | Implementation points | Exact red/green selector and expected output |
| --- | --- | --- | --- |
| `R4-01A` | Create `src/apexcrew/application/configuration.py`, `src/apexcrew/adapters/repository/bootstrap.py`, `tests/unit/application/test_configuration.py`, `tests/contract/test_repository_bootstrap.py`; modify `src/apexcrew/delivery/cli.py` | Add immutable `RunOptions`/non-sensitive config parsing, `RepositoryBootstrapAuthorityService`, direct-ref and no-follow preflight delegation, and typed `CreateRunPayload` construction with zero provider calls. | `uv run --python 3.12 pytest tests/unit/application/test_configuration.py::test_run_options_reject_unknown_keys tests/contract/test_repository_bootstrap.py::test_bootstrap_rejects_non_direct_target_ref -q`; red: `2 failed` with missing production symbols; green: `2 passed`. |
| `R4-01B` | Create `tests/contract/test_cli_approvals.py`; modify `src/apexcrew/delivery/cli.py`, `src/apexcrew/domain/commands.py` only where an existing typed payload needs delivery wiring | Add `approve-policy`, `approve-budget`, and `approve-model` previews/submission using exact revision digests and confirmation codes; reject replay, wrong Run, wrong revision, and any unsupported legacy command form without mutation. | `uv run --python 3.12 pytest tests/contract/test_cli_approvals.py::test_specialized_approval_commands_bind_exact_revision tests/contract/test_cli_approvals.py::test_replayed_approval_is_side_effect_free -q`; red: `2 failed` because commands are absent or unbound; green: `2 passed`. |
| `R4-02A` | Create `src/apexcrew/application/composition.py`, `src/apexcrew/adapters/model/factory.py`, `tests/contract/test_composition.py`, `tests/integration/test_provider_selection.py`; modify `src/apexcrew/adapters/model/deepseek_responses.py` | Build the production `ApplicationBundle`, select only approved `scripted_mock`/`deepseek_responses` providers, inject one SQLite state store and credential port, and keep composition free of approvals and Permit issuance. | `uv run --python 3.12 pytest tests/contract/test_composition.py::test_bundle_shares_one_state_store tests/integration/test_provider_selection.py::test_scripted_selection_never_calls_network -q`; red: `2 failed` because no production factory exists; green: `2 passed`. |
| `R4-02B` | Create `tests/integration/test_production_wiring.py`; modify `src/apexcrew/application/composition.py`, `src/apexcrew/application/control.py`, `src/apexcrew/application/queries.py`, `src/apexcrew/application/runtime.py`, `src/apexcrew/domain/coordination.py`, `src/apexcrew/domain/worker.py`, and `src/apexcrew/adapters/state/sqlite.py` | Wire `CrewControl`, `CrewRuntime`, `RunQueries`, Coordinator, WorkerLoop, Authority, recovery, phase drivers, and model/tool ports behind the A-Hybrid boundary, preserving one durable state identity across reopen. | `uv run --python 3.12 pytest tests/integration/test_production_wiring.py::test_reopened_bundle_preserves_run_bindings tests/contract/test_composition.py::test_bundle_exposes_only_public_interfaces -q`; red: `2 failed` because wiring/reopen contracts are absent; green: `2 passed`. |
| `R4-03A` | Modify `src/apexcrew/delivery/cli.py`, `src/apexcrew/application/queries.py`, `tests/unit/test_cli.py`; create `tests/integration/test_cli_run_lifecycle.py` | Implement thin CLI adapters for create, specialized approvals, planning, start, run, status, and show; consume exactly one current Permit; map stop states to explicit exit codes; render only `RunQueries.get`. | `uv run --python 3.12 pytest tests/unit/test_cli.py::test_run_create_returns_durable_run_id tests/unit/test_cli.py::test_run_without_current_permit_is_side_effect_free -q`; red: `2 failed` against the hard-coded/no-bundle CLI; green: `2 passed`. |
| `R4-03B` | Modify `src/apexcrew/delivery/cli.py`, `src/apexcrew/application/queries.py`; extend `tests/integration/test_cli_run_lifecycle.py` | Prove the complete offline approval/Permit/Worker/Grant/Integrate flow, one tool effect, final Admission CAS, unchanged target OID before integration, SQLite reopen, and zero effects from replaying all prior specialized commands. | `uv run --python 3.12 pytest tests/integration/test_cli_run_lifecycle.py::test_reopened_cli_lifecycle_rejects_all_replays -q`; red: `1 failed` before Worker/Grant/integration composition; green: `1 passed`. |
| `R4-04A` | Create `src/apexcrew/adapters/runtime/file_lock.py`, `tests/unit/adapters/runtime/test_file_lock.py`; modify `src/apexcrew/application/runtime.py`, `src/apexcrew/adapters/repository/planning.py`, `src/apexcrew/adapters/repository/snapshot.py`, `tests/integration/test_real_repository_run.py` | Add cross-platform process ownership, validate Permit before lock state, and connect regular-file/no-follow planning snapshots and bounded repository reads without target-worktree effects. | `uv run --python 3.12 pytest tests/unit/adapters/runtime/test_file_lock.py::test_cross_process_lock_is_exclusive tests/integration/test_real_repository_run.py::test_planning_uses_regular_file_snapshot -q`; red: `2 failed` because production lock/snapshot path is absent; green: `2 passed`. |
| `R4-04B` | Modify `src/apexcrew/adapters/executor/restricted.py`, `src/apexcrew/domain/worker.py`, `tests/unit/adapters/executor/test_restricted.py`, `tests/integration/test_real_repository_run.py` | Connect typed Worker tool envelopes to the closed argv builder and restricted runner; enforce digest-pinned non-root/networkless containment and retain fail-closed unsupported-host behavior. | `uv run --python 3.12 pytest tests/unit/adapters/executor/test_restricted.py::test_argv_and_container_policy_is_fail_closed tests/integration/test_real_repository_run.py::test_worker_tool_effect_requires_grant -q`; red: `2 failed` because the runner remains disconnected; green: `2 passed` only with observed restricted-runner evidence. |
| `R4-05A` | Create `tests/integration/test_live_provider_smoke.py`, `tests/integration/test_live_cli_run_lifecycle.py`; modify `src/apexcrew/adapters/model/deepseek_responses.py`, `src/apexcrew/application/composition.py`, `src/apexcrew/delivery/cli.py`, `tests/contract/test_cli_credentials.py` | Add the explicit `APEXCREW_LIVE_SMOKE=1` gate, request-time real credential resolution, and one bounded DeepSeek request owned by the live CLI approval/Permit/runtime lifecycle through `deepseek_responses`; the provider-smoke test covers gate and credential failure without dispatch. Assert response/model/usage and sanitized output without retries or secret bytes. | Red: `uv run --python 3.12 pytest tests/integration/test_live_provider_smoke.py::test_live_smoke_is_opt_in tests/integration/test_live_cli_run_lifecycle.py::test_live_cli_approval_permit_runtime_lifecycle -q` -> `1 failed, 1 skipped` because the gate/composition contract is absent. Default green -> `1 passed, 1 skipped`; gated owner-authorized green -> `2 passed` with exactly one provider request total; missing credential is a fail-closed pre-dispatch result. |
| `R4-05B` | Modify `README.md`, `SECURITY.md`, `Makefile`; extend `tests/contract/test_documentation_delivery.py`, `tests/contract/test_release_artifacts.py` | Document the usable local CLI versus read-only WebUI, credential lifecycle, explicit live-smoke command, unsupported host executor path, and release/build/scan gates; keep live smoke out of ordinary test/CI targets. | `uv run --python 3.12 pytest tests/contract/test_documentation_delivery.py::test_local_cli_workflow_is_documented tests/contract/test_release_artifacts.py::test_live_smoke_is_not_in_default_targets -q`; red: `2 failed` for missing workflow/gate documentation; green: `2 passed`. |

`R4-05A` is the only task allowed to use a real credential, and only under the explicit gate. Its live selector is never part of `make test`, normal CI, or the complete offline lifecycle evidence. The complete lifecycle remains deterministic under `ScriptedMockLLM`; the live test is a bounded provider/composition smoke, not a replacement for offline replay coverage.

## Review Correction Addendum

The first independent review found six blockers. This addendum is binding with the R4 section in `PLAN.md` and closes those gaps before the next review.

### Authority and Worktree Matrix

After a fresh independent zero-blocker review and owner `M1 GO`, R4 supersedes the old M1-R3 execution cut line only for final-user-runtime tasks. R3 remains historical evidence. The serial dependency is:

```text
R4-01A -> R4-01B -> R4-02A -> R4-02B -> R4-03A -> R4-03B
       -> R4-04A -> R4-04B -> R4-05A -> R4-05B -> R4-CLOSE
```

| Tasks | Branch | Worktree | PR title |
| --- | --- | --- | --- |
| R4-01A/R4-01B | `codex/m1-r4-01-bootstrap` | `.worktrees/m1-r4-01-bootstrap` | `feat(cli): bootstrap approved DeepSeek runs` |
| R4-02A/R4-02B | `codex/m1-r4-02-composition` | `.worktrees/m1-r4-02-composition` | `feat(runtime): compose production Run services` |
| R4-03A/R4-03B | `codex/m1-r4-03-cli-lifecycle` | `.worktrees/m1-r4-03-cli-lifecycle` | `feat(cli): deliver Permit-gated Run lifecycle` |
| R4-04A/R4-04B | `codex/m1-r4-04-execution` | `.worktrees/m1-r4-04-execution` | `feat(executor): run scoped Worker actions safely` |
| R4-05A/R4-05B | `codex/m1-r4-05-provider-delivery` | `.worktrees/m1-r4-05-provider-delivery` | `test(delivery): verify live DeepSeek path` |

Each A/B row is one subagent-sized task and one implementation commit. The two tasks in a module share that module's worktree/branch and one PR, but have distinct fresh subagents, commits, reviews, and ledger rows. Module closeout updates the R4 ledger and corresponding `AGENT_LOG.md` rows only.

### Complete CLI Sequence

`run-create` stores the exact Policy, Budget, and Model Configuration revisions in `DRAFT` and makes zero model calls. It does not approve them. The supported sequence is:

```text
apexcrew init --root <repo>
apexcrew credentials set
apexcrew credentials status
apexcrew run-create --root <repo> --target-ref refs/heads/main --goal "..." --acceptance "..."
apexcrew show <run-id> --root <repo>
apexcrew approve-policy <run-id> --root <repo> --digest <policy-digest> --confirmation-code <code>
apexcrew approve-budget <run-id> --root <repo> --digest <budget-digest> --confirmation-code <code>
apexcrew approve-model <run-id> --root <repo> --digest <model-digest> --confirmation-code <code>
apexcrew begin-planning <run-id> --root <repo>
apexcrew run <run-id> --root <repo>
apexcrew show <run-id> --root <repo>
apexcrew approve-plan <run-id> --root <repo> --digest <plan-digest> --confirmation-code <code>
apexcrew start <run-id> --root <repo> --plan-digest <plan-digest>
apexcrew run <run-id> --root <repo>
apexcrew show <run-id> --root <repo>
apexcrew grant <run-id> --root <repo> --pending-action-id <id> --action-digest <digest> --confirmation-code <code>
apexcrew run <run-id> --root <repo>
apexcrew integrate <run-id> --root <repo> --candidate-id <id> --prepared-oid <oid> --expected-target-oid <oid> --evidence-digest <digest> --confirmation-code <code>
apexcrew run <run-id> --root <repo>
apexcrew show <run-id> --root <repo>
apexcrew credentials clear
```

`show` is the only source for the next exact digest, pending action, candidate, evidence, and confirmation-code preview; it never exposes a nonce. `status` reports only initialization and credential source/presence. `continue`, `resume`, `resolve-indeterminate`, `pause`, `cancel`, and purge commands use the same typed `CommandEnvelope` and sequence/revision binding.

### Required End-to-End Assertions

`test_cli_run_lifecycle.py` must observe: DRAFT/zero calls; three revision approvals; `begin-planning` Permit issuance and `run` consumption; planning stop at `AWAITING_PLAN_APPROVAL`; plan approval plus `start` fresh Permit; Worker attempt and one typed model action; `AWAITING_ACTION_APPROVAL`; exact Grant and one tool effect; fresh Evidence Bundle/Candidate; `AWAITING_FINAL_APPROVAL`; exact Integrate Permit and one Admission CAS; changed target OID only at the final integration; and a new-process reopen where every old command replay produces zero second effect.

### Provider and Executor Assertions

The provider tests must observe exact DeepSeek base URL and model ID, no SDK retries, 32,000/4,096 token caps, storage off, schema digest, completed status, response ID, exact returned-ID allowlist, usage including reasoning tokens, typed one-object output, USD 0.28/0.56 pricing, USD 0.672 worst-case reservation, full charge for missing/unexpected settlement data, known-closed retry bounds, and `INDETERMINATE` for unknown transport outcome. Credential tests cover set/status/clear/replacement and prove no secret bytes enter prompts, journal, executor environment, logs, or projections.

Executor tests must assert digest-pinned image, non-root UID/GID, read-only root, no network, no socket/credential mount, dropped capabilities, no-new-privileges, CPU/memory/PID/scratch ceilings, minimal environment, regular-file-only snapshots, no `.git`/symlink/reparse/secret paths, and discard of command-created files. Unsupported host execution remains fail closed and is never counted as a successful Worker action.
