# ApexCrew v0.1 Completion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete the remaining ApexCrew v0.1 loop so an offline deterministic run observes repository bytes, repairs a seeded defect, verifies it in the restricted executor, promotes a Task Candidate to the private Run Head, freezes a separate Run Candidate, and integrates it once through the existing Permit, Grant, Admission, and target-CAS boundaries.

**Architecture:** Keep `CrewControl`, `CrewRuntime`, and `RunQueries` as the only Run-facing interfaces. Materialize two separately bound views from one Git-pinned tree: Worker context from `R union D`, and the mutable patch/check workspace from `Q union W`. Admission owns the distinction between Task Candidate/private Run-Head promotion and Run Candidate/final target CAS. All declared checks use the digest-pinned restricted Docker executor; Docker absence is uncertainty, never permission to run on the host.

**Tech Stack:** Python 3.12, Pydantic, pytest, SQLite, typed Git argv adapters, Docker, uv, Ruff, mypy, ScriptedMockLLM, browser checks, GitHub Actions, and the existing CLI/WebUI layers.

---

## Scope and Review Gates

`SPEC.md` remains byte-frozen. The formal implementation base is `9e7648f` on `codex/m2-m4-final-production`, containing the reviewed R4.1 recovery/cleanup chain and verified M2-M4 provider, Docker, retention, CI, WebUI, and build work. The primary checkout at `3676526` has an uncommitted diagnostic/demo draft; it is not an implementation base and no unreviewed source/test/fixture/CI change may be copied from it. The zero-blocker document review is recorded in formal-base docs-only commit `e8461003ee3c8da888683fb1975bb1523803096c`; the current docs-only review closeout is its descendant, and the first implementation worktree must be created from that closeout while proving ancestry before any source/test/fixture/CI change.

The abandoned `b4d802e` R4.2 Git object skeleton is diagnostic only. Its stdin `GitMkTree` design is superseded by a temporary-index implementation. The existing private Run Head ref is the only ref R4.3-04 may update, and only through the typed Admission port. The user target ref remains unchanged until the final CAS in R4.3-05.

Before source, test, fixture, or CI work begins, an independent document reviewer must check this plan and the R4.3 section in `PLAN.md` against SPEC sections 3, 5, 6, 7, 9, and 10. The reviewer must reject host execution, raw shell text, ref writes before the allowed private-promotion/final-CAS boundary, Permit/Grant bypasses, reservation-worktree writes, secret disclosure, merged candidate levels, shared `R`-only check workspaces, retention-content purge blockers, missing per-task PR/review records, and unsupported release claims. The prior `3676526` red claims are invalid; fresh red output must be captured in a disposable checkout rooted at `9e7648f` with only each task's named red tests. The review digest, reviewed base, verdict, and owner M1 GO must be recorded in the docs-only reviewed-plan commit before Task 2.

After every implementation task: observe red selectors first; commit one task in its own worktree; run an independent SPEC review on that exact commit; fix all Critical/High findings; run a separate quality review on the corrected commit; fix all Critical/High findings; record reviewer identities and correction SHAs; update the ledger before creating the next task worktree. A final module review cannot replace a missing task review.

Each task maps to exactly one PR (`PR-R4.3-00` through `PR-R4.3-07`) and no PR may combine two task rows. Push, PR creation, and merge are owner-only actions; the local ledger records the intended mapping, exact commit ancestry, review SHAs, and human/agent attribution without claiming that an unpushed PR exists.

## Definition of Done

- Real Worker feedback contains a persisted failed check and is bound into the next model request.
- Worker context contains the goal, contract, bounded `R union D` regular-file facts, and dependency bindings; the check snapshot is independently bound to `Q union W`.
- Attempt patching uses one unified-diff implementation, stable no-follow handles, atomic replacement, and zero side effects for denied/malformed patches.
- Production checks use only `RestrictedDockerExecutor`; no host executor switch or fallback exists.
- Task Candidate preparation creates a commit with the private Run Head as parent; private promotion and Run Candidate final integration are distinct transitions.
- Run Candidate creation re-constructs an independent commit `R` from the complete private Run Head tree with pinned target `T0` as its first parent, stores `head_oid=H` and non-null `prepared_oid=R`, and runs every Run check against its own `Q_i union W_i`; missing OIDs never fall back to the current head.
- Final target CAS changes the target ref exactly once only after the exact final Grant and Runtime Permit.
- Python and TypeScript acceptance fixtures repair their seeded defects with zero strict xfails.
- Retained, quarantined, expired, and `DROPPED_BY_RETENTION` artifact metadata enters the frozen Purge Manifest without requiring content to remain readable.
- Performance, MockLLM onboarding, static replay-size, WebUI accessibility/responsive at 360/1440 CSS pixels, Windows/Ubuntu CI, GitLab `unit-test`, same-revision release, wheel, WebUI, Docker, lint, type, diff, and secret gates have observed output. Hosted actions remain owner-gated.

## File Map

- Demo and patch: `src/apexcrew/demo.py`, `src/apexcrew/domain/worker.py`, `src/apexcrew/domain/tools.py`, `src/apexcrew/adapters/repository/unified_diff.py`, `src/apexcrew/adapters/repository/granted_workspace.py`, `src/apexcrew/adapters/executor/memory_patch.py`.
- Attempt and checks: `src/apexcrew/adapters/repository/attempt_workspace.py`, `src/apexcrew/adapters/executor/attempt_patch.py`, `src/apexcrew/application/composition.py`, `src/apexcrew/adapters/executor/restricted.py`.
- Candidates and refs: `src/apexcrew/domain/admission.py`, `src/apexcrew/adapters/repository/git.py`, `src/apexcrew/adapters/repository/candidate_preparation.py`, `src/apexcrew/adapters/repository/target_cas.py`, `src/apexcrew/application/runtime.py`, `src/apexcrew/adapters/state/sqlite.py`, `src/apexcrew/adapters/state/memory.py`, `src/apexcrew/delivery/cli.py`.
- Retention and delivery: `src/apexcrew/domain/retention.py`, `src/apexcrew/domain/commands.py`, `src/apexcrew/application/control.py`, `src/apexcrew/domain/projection.py`, `src/apexcrew/delivery/replay.py`, `src/apexcrew/delivery/web.py`.
- Tests and release: `tests/unit/`, `tests/contract/`, `tests/integration/`, `tests/acceptance/`, `.github/workflows/ci.yml`, `.github/workflows/release.yml`, `scripts/measure_performance.py`, `scripts/check_static_replay.py`, `scripts/check_required_ci_duration.py`, `README.md`, `SPRINT.md`, `AGENT_LOG.md`.

### Task 1: Independent Plan Review and Clean Boundary

**Files:** `PLAN.md`, `AGENT_LOG.md` only after review.

- [ ] Reconcile the dependency chain: formal base `9e7648f`; `3676526` and `b4d802e` diagnostic only; R4.3-04 private promotion and R4.3-05 final target CAS separate; Run Candidate `R` is an independent commit with parent `T0`, not `H`.
- [ ] Run the independent SPEC document review and obtain `PASS` with zero Critical/High findings.
- [ ] Record both plan SHA-256 digests, reviewed base, reviewer ID, verdict, owner M1 GO, and the preserved primary draft. Use `Get-FileHash` and `git diff --check`; do not stage source/test/fixture/CI changes.
- [ ] Create a docs-only review closeout descended from formal-base reviewed-plan commit `e8461003ee3c8da888683fb1975bb1523803096c`, prove its changed paths are only `PLAN.md`, this companion plan, and the review/GO record in `AGENT_LOG.md`, then create `.worktrees/m1-r4-3-demo-loop` from that exact closeout commit and verify `git status --short --branch` is empty.

### Task 2: R4.3-00 Real Demonstration Feedback Loop

**Files:** Create `src/apexcrew/adapters/repository/unified_diff.py` and `src/apexcrew/adapters/executor/memory_patch.py`; modify `granted_workspace.py`, `demo.py`, `tests/unit/test_demo.py`; test `tests/unit/adapters/executor/test_memory_patch.py`.

- [ ] Add red selectors requiring two Worker turns, two actual tool executions, `CHECK_FAILED` then `PATCH_APPLIED`, tool-role feedback containing the structured failure code and expected value, and repaired `src/money.py` bytes.
- [ ] Run `uv run --python 3.12 pytest tests/unit/test_demo.py tests/unit/adapters/executor/test_memory_patch.py -q`; expected red is missing fields/module.
- [ ] Export exactly `apply_unified_diff(original: bytes, unified_diff: str) -> bytes` and `reverse_unified_diff(current: bytes, unified_diff: str) -> bytes`, preserving existing `RepositoryUnsafeError` codes. Make `GrantedWorkspaceAdapter` delegate with no second hunk implementation.
- [ ] Implement `MemoryPatchExecutor` with the existing denial order, secret/write-scope checks, and `FakeExecutor` tree-digest algorithm. Drive `build_demo_trace` through `WorkerLoopService`, real tool results, and `bounded_worker_feedback`, not manual prompt replacement.
- [ ] Run selectors, mypy, Ruff, diff check, independent SPEC review, independent quality review, corrections, and the R4.3-00 ledger update before the next worktree.

### Task 3: R4.3-01 Separate Attempt Context and Check Workspaces

**Files:** Create/modify `src/apexcrew/adapters/repository/attempt_workspace.py` and `src/apexcrew/application/composition.py`; test `tests/integration/test_attempt_workspace.py`.

- [ ] Add red selectors for `materialize_context()` using `R union D`, `materialize_check()` using `Q union W`, distinct digests, scope enforcement, secret denial without path echo, non-regular blob rejection, idempotent rebuild, and reservation-worktree immutability.
- [ ] Implement exact interfaces:

```text
materialize_context(*, attempt_id, base_oid, read_globs, dependency_globs) -> MaterializedWorkspace
materialize_check(*, attempt_id, base_oid, input_globs, write_globs) -> MaterializedWorkspace
```

- [ ] Use typed `GitLsTreeRecursive`/`GitCatFileBlob`, `StableHandleTree`, separate `context/` and `check/` roots, 2,000-entry and byte caps, and fail-closed secret/symlink/submodule checks. Filter by canonical membership in `R union D` or `Q union W` before inspecting mode, secret policy, or blob contents; only in-scope symlink/submodule entries are rejected. `Q union W` includes every write path.
- [ ] Run `uv run --python 3.12 pytest tests/integration/test_attempt_workspace.py -q`, mypy, Ruff, and diff checks; complete both reviews and the ledger row.

### Task 4: R4.3-02 Atomic Attempt Patch and Worker Context

**Files:** Create `src/apexcrew/adapters/executor/attempt_patch.py`; modify `src/apexcrew/application/composition.py`; test `tests/integration/test_attempt_patch_executor.py` and `tests/integration/test_worker_context.py`.

- [ ] Add red selectors for real byte replacement, outside-scope denial, malformed-diff zero side effects, ancestor replacement denial, contract/file facts, secret exclusion, and truncation markers.
- [ ] Implement `AttemptPatchExecutor`: no-follow reads, shared diff application, same-directory `mkstemp` plus atomic replacement, stable-name checks before/after, and check-workspace digest recomputation. Keep the context digest and every check-workspace digest independently bound.
- [ ] Make `_CompositionWorkerContext.build_current` emit canonical goal/constraints/acceptance/task contract/check data and bounded regular-file content only from `R union D`; bind one dependency digest per included file. Never include `Q union W` implicitly.
- [ ] Run the focused tests, `uv run --python 3.12 mypy src`, `ruff check .`, `ruff format --check .`, `git diff --check`, both reviews, corrections, and ledger update.

### Task 5: R4.3-03 Restricted Docker Composition

**Files:** Modify `src/apexcrew/application/composition.py`, `src/apexcrew/adapters/executor/restricted.py`, `README.md`, `SECURITY.md`, `SPRINT.md`, and `AGENT_LOG.md`; test `tests/integration/test_composed_worker_tools.py` and `tests/integration/test_restricted_executor_docker.py`.

- [ ] Add red selectors for check-ID derivation, composed patch/check bindings, distinct context/check snapshots, and the production Docker boundary.
- [ ] Wire the real `AttemptPatchExecutor`, `RestrictedDockerExecutor`, approved `DeclaredCheckRegistry`, `SanitizedSnapshot` from `Q union W`, existing deadline journal, and deadline authority into `ScopedToolRuntime`.
- [ ] Keep `RestrictedDockerExecutor` as the only production executor: digest-pinned image, no network/socket, read-only input, bounded scratch/output, dropped capabilities, no-new-privileges, and allowlisted environment. Docker absence, timeout, and unobservable outcome return uncertainty. Do not add `LocalSubprocessExecutor`, `APEXCREW_HOST_EXECUTOR`, or a host fallback.
- [ ] Run a real restricted Docker process on a supported host and update every `DEBT-M2-005` mention in README, SECURITY, SPRINT, and AGENT_LOG to the observed state. If the daemon is unavailable, record the debt as OPEN and do not claim v0.1 completion; a platform skip is not debt closure. Daemon/platform skips must carry their explicit reason. Run mypy, Ruff, diff check, both reviews, corrections, and the ledger update.

### Task 6: R4.3-04 Task Candidate and Private Run-Head Promotion

**Files:** Modify `src/apexcrew/domain/admission.py`, `src/apexcrew/adapters/repository/git.py`, state adapters, and `src/apexcrew/application/runtime.py`; create `src/apexcrew/adapters/repository/candidate_preparation.py`; test `tests/integration/test_candidate_preparation.py` and `tests/integration/test_task_candidate_promotion.py`.

- [ ] Add red selectors for temporary-index preparation, exact parent/changed bytes, unchanged-blob reuse, deterministic OID, index cleanup, preparation failure, private-ref CAS, target-ref immutability, and rejection of a Task Candidate by final integration.
- [ ] Add typed Git operations for `read-tree`, `hash-object -w`, `update-index --cacheinfo`, typed index removal, `write-tree`, and `commit-tree`; pin author/committer identity and `GIT_INDEX_FILE`; allow only modes `100644`/`100755`; bind delete/rename/executable-bit changes to exact `REQUIRE_APPROVAL`; touch no ref during preparation.
- [ ] Implement `prepare_task_candidate(*, run_id, task_id, attempt_id, run_head_oid, workspace, changed_paths, message) -> TaskCandidate`. Bind the Attempt lease, patch post-state, Task Evidence Bundle, Task Freshness Assessment, exact action approvals, and `Q union W` digest to one Run Head. Represent rename as approved delete-plus-add. Failure creates no candidate and stays recoverable.
- [ ] Implement `promote_task_candidate()` using `RefCasIntent`, `private_ref(run_id)`, `expected_old_oid=run_head_oid`, and the candidate OID under the Run-ref lock. Advance durable Run Head only after exact private-ref post-state. It cannot issue a target CAS or consume the final Grant.
- [ ] Run focused tests, assert target/unrelated refs unchanged, complete both reviews/corrections, and update the ledger before Task 7.

### Task 7: R4.3-05 Run Candidate and Final Target CAS

**Files:** Modify `src/apexcrew/domain/admission.py`, state adapters, `src/apexcrew/application/runtime.py`, `src/apexcrew/delivery/cli.py`, and `src/apexcrew/adapters/repository/target_cas.py`; test `tests/integration/test_run_candidate.py` and `tests/integration/test_composed_runtime_lifecycle.py`.

- [ ] Add red selectors for all promoted Task Candidates, fresh run-wide evidence, explicit non-null `prepared_oid`, missing-OID refusal, Grant/Permit binding, target CAS once, parent binding, replay resistance, and private-ref immutability during final integration.
- [ ] Implement `prepare_run_candidate(run_id) -> RunCandidate`: read private Run Head `H`, assert the target remains pinned at `T0`, revalidate all Task Candidates, construct an independent full-tree commit `R` with first parent `T0`, and re-run the fresh run-wide checks against `R`, materializing each check's own `Q_i union W_i`. Store `head_oid=H`, `prepared_oid=R`, and `target_base_oid=T0`; absence, parent mismatch, or reuse of `H` is invalid.
- [ ] Implement `integrate_run_candidate()` with only the exact final Grant and Runtime Permit. Create one `TargetCasIntent(expected_old_oid=T0, prepared_oid=R)` and delegate the sole target writer to `GitTargetCasAdapter.apply`. Remove any `candidate_head_oid` fallback and forbid private-ref updates in final integration.
- [ ] Run lifecycle and recovery/CAS selectors, mypy, Ruff, diff, both reviews/corrections, and the ledger update.

### Task 8: R4.3-06 Acceptance and Metadata-First Retention Purge

**Files:** Modify lifecycle/acceptance fixtures, `src/apexcrew/domain/retention.py`, `src/apexcrew/domain/commands.py`, `src/apexcrew/application/control.py`, state adapters, `README.md`, `SECURITY.md`, `SPRINT.md`, and `AGENT_LOG.md`; test purge/redaction/retention integration files.

- [ ] Remove strict xfails and drive the real Worker sequence `read -> patch -> check -> finish`. Python money and TypeScript timestamp bytes must be repaired, and the final commit must be a child of the pinned target OID.
- [ ] Add `purge_inventory(run_id) -> tuple[PurgeLocalArtifactEntry, ...]` returning all terminal-Run artifact metadata, including `STORED`, `QUARANTINED`, expired, and `DROPPED_BY_RETENTION`, sorted deterministically without reading content. `prepare_purge` freezes all eligible entries even if preview/content is absent.
- [ ] Keep purge blockers exactly to SPEC 10.4: non-terminal state, owner, unsettled/recovery-required intent, `INDETERMINATE`, live Pending Action/lease/Grant delivery, or unsettled Target Reservation cleanup. Retention bytes are not an extra blocker.
- [ ] Confirm/recover idempotently by deleting only frozen database rows and validated data-root paths. Missing artifact content is already deleted; never run Git or touch refs/worktrees. Preserve the minimal tombstone projection.
- [ ] Run acceptance, purge, retention, full offline, both reviews/corrections, and update the ledger with zero strict xfails.

### Task 9: R4.3-07 Same-Revision Release Verification

**Files:** Modify `.github/workflows/ci.yml`, `.github/workflows/release.yml`, `.gitlab-ci.yml`, performance/static-size scripts, `README.md`, `SECURITY.md`, and `AGENT_LOG.md`; test delivery, performance, and WebUI quality contracts.

- [ ] Add red selectors for Windows/Ubuntu jobs, same-SHA release evidence, static replay size, maximum-shape performance, MockLLM onboarding, and browser accessibility/responsive behavior.
- [ ] Run performance and static-size scripts against the current revision and require the exact SPEC thresholds: warmed status/inspect p95 <= 750 ms, cold restart <= 3 s, offline suite <= 90 s, required PR CI <= 10 min, WebUI first response p95 <= 1 s, compressed static assets <= 2 MiB, and MockLLM onboarding <= 10 min. Stale historical measurements do not qualify.
- [ ] Build the static WebUI and use the local browser at exactly 360 and 1440 CSS pixels. Verify load, no horizontal overflow, visible keyboard focus, WCAG 2.2 AA, Lighthouse Accessibility >=95, non-color status/decision expression, accessible names/landmarks, no overlap/clipping, and screenshots. The WebUI remains read-only.
- [ ] Require the release verifier to find exactly one successful same-SHA CI run with `quality`, `unit-ubuntu`, `unit-windows`, `integration`, `build`, `pages`, `browser-quality`, and `reference-performance`, plus the same revision's `.gitlab-ci.yml:unit-test` definition/result when hosted evidence exists, exact artifact IDs/digests, and duration. Missing hosted evidence is pending external work, not permission to publish.
- [ ] Run `uv sync --frozen --all-groups`, `make test`, `make lint`, `make demo`, `make secret-scan`, `make web-build`, `make build`, and `git diff --check`; run Docker build and the restricted-executor selector; complete final reviews and record every ledger SHA. Do not push, tag, publish, enter credentials, or issue a live provider request.

## Final Self-Review

- SPEC coverage includes Worker/Coordinator ownership, scoped actions, freshness/evidence, two-level candidate flow, recovery, cleanup, retention, purge, CLI-only authority, read-only WebUI, credential timing, restricted Docker, fixture repair, and no-push CAS.
- No task includes a host execution escape, raw shell text, a ref fallback, a shared `R`-only check workspace, or content-dependent purge eligibility.
- `TaskCandidate` and `RunCandidate` signatures and state bindings remain consistent across tasks.
- Every task has its own worktree, commit, red/green evidence, SPEC review, quality review, correction SHAs, and ledger update before the next task.
- Final release claims distinguish local verification from owner-authorized hosted CI/provider/publication actions.

## Document Review Record

| Field | Value |
| --- | --- |
| Reviewed implementation base | `9e7648f` |
| Primary diagnostic draft | `3676526` (uncommitted; invalid implementation/red-evidence base) |
| Reviewer | `019fe0e5-77f9-7050-bde1-d3b42d1da1a1` (`gpt-5.6-luna`, max reasoning) |
| Verdict | `PASS` (second independent review; zero Critical/High findings) |
| Findings | All first-review findings corrected and rechecked: independent Run Candidate `R` parent `T0`; clean-base red evidence; reviewed-plan ancestor; union filter order; per-check `Q_i`; typed delete/rename/mode approval; per-task PR mapping; exact browser viewports; `DEBT-M2-005`; GitLab `unit-test` |
| Review-input PLAN.md SHA-256 (before this PASS record) | `98D0005BFB0930745A3A35369E6732260387D798F91844FC06EE26290F7398A5` |
| Review-input companion plan SHA-256 (before this PASS record) | `21B6BD802224F9E950C6D25A60732D49D02493C619AF0EF39E274796EAB32E7C` |
| Reviewed-plan commit / owner M1 GO | `e8461003ee3c8da888683fb1975bb1523803096c`; `GO` granted for R4.3 source work under this plan |
