# ApexCrew Recovery Resolution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete the M1 final-user runtime path by implementing action-class recovery, exact set-bound indeterminate resolution, and independently safe terminal reservation cleanup recovery.

**Architecture:** Keep recovery decisions in the domain and keep all durable transitions in the existing SQLite and in-memory state-store transaction seams. `CrewControl` validates an exact resolution command and issues the existing one-use Permit; `CrewRuntime` consumes that Permit and invokes a concrete resolution strategy. Git cleanup remains identity-bound to one `TargetReservation` and can delete only an independently verified exact path or exact admin entry.

**Tech Stack:** Python 3.12, Pydantic, pytest, SQLite, typed Git argv adapters, existing `CrewControl`/`CrewRuntime`/`RunQueries` interfaces.

---

## Scope and File Map

- Modify `src/apexcrew/domain/effects.py` to classify each intent by action class, compute a canonical unresolved set, and expose safe recovery decisions without guessing external outcomes.
- Modify `src/apexcrew/domain/indeterminate.py` to validate member and set resolution bindings and precedence.
- Modify `src/apexcrew/application/control.py` and `src/apexcrew/domain/commands.py` to validate `ResolveIndeterminatePayload`, construct the exact resolution subject, and issue its `INDETERMINATE` Runtime Permit.
- Modify `src/apexcrew/application/runtime.py` to execute one member resolution per consumed Permit and return the correct successor stop.
- Modify `src/apexcrew/adapters/state/sqlite.py` and `src/apexcrew/adapters/state/memory.py` to settle, retry, or abandon one exact intent atomically, update owning task/ref/candidate state, and preserve the unresolved set digest.
- Modify `src/apexcrew/application/composition.py` to install the concrete recovery strategy in the production bundle.
- Modify `src/apexcrew/adapters/repository/git.py` and the composition cleanup adapter to support exact path-only and admin-only cleanup, with zero deletion for mixed, altered, or unobservable states.
- Add focused unit/contract/integration tests under `tests/unit/domain/`, `tests/contract/`, and `tests/integration/`.
- Update `PLAN.md`, `AGENT_LOG.md`, `README.md`, and `SECURITY.md` with observed evidence and the remaining live-credential boundary only after code verification.

## Invariants

- A member command must bind `intent_id`, `recovery_generation`, and the current canonical unresolved-set digest. A stale generation or digest has zero mutation.
- `RECONCILE_OBSERVED` settles only an authoritative exact post-state. `RETRY_SAME_INTENT` is accepted only for an authoritative exact pre-state and a class whose journaled idempotency key is replay-safe. `ABANDON_INTENT` is accepted only when observation proves no authoritative effect remains.
- Set-bound `FAIL_RUN` and `CANCEL_RUN` can close the complete set only when every member is independently proven abandonable. Otherwise the command is denied and the set remains intact.
- Resolving a member never opens dispatch while another member remains. After the final member, objective target success takes precedence, then persisted cancel/pause, then the class-specific paused successor.
- Cleanup operations are exact reservation operations. They never call `git worktree prune`, never inspect/delete another entry, and never delete content unless the identity and expected bytes are verified.

### Task 1: Domain Recovery Classification

**Files:**
- Modify: `src/apexcrew/domain/effects.py`
- Modify: `src/apexcrew/domain/indeterminate.py`
- Test: `tests/unit/domain/test_recovery.py`
- Test: `tests/unit/domain/test_indeterminate.py`

- [ ] **Step 1: Write failing tests** for model, read/search, patch, check, private-ref, target-CAS, and reservation intents. Assert exact post, exact pre, stale binding, third state, and recovery-safe replay classification. Add a test that canonical members produce a stable set digest and that generation/digest mismatches raise the closed error.
- [ ] **Step 2: Run the focused selector**

```powershell
uv run --python 3.12 pytest tests/unit/domain/test_recovery.py tests/unit/domain/test_indeterminate.py -q
```

Expected: the new action-class and set-binding tests fail because the strategy and binding helpers do not exist.
- [ ] **Step 3: Implement the minimum domain strategy.** Derive class from the typed payload (`ModelRequestIntent`, `ToolIntent`, `RefCasIntent`, and reservation payloads), preserve the captured digest/generation, and return only `SETTLED`, `RETRY`, `STALE`, `CONFLICT`, or `INDETERMINATE` decisions justified by observations.
- [ ] **Step 4: Re-run the same selector** and require all focused tests to pass.
- [ ] **Step 5: Commit** with `feat(recovery): classify intent recovery by action class` and record red/green evidence.

### Task 2: Atomic State Resolution and Control Permit

**Files:**
- Modify: `src/apexcrew/application/control.py`
- Modify: `src/apexcrew/adapters/state/sqlite.py`
- Modify: `src/apexcrew/adapters/state/memory.py`
- Test: `tests/contract/test_state_store.py`
- Test: `tests/integration/test_indeterminate_resolution.py`

- [ ] **Step 1: Write failing contract tests** that create two indeterminate members, resolve one with the exact set digest, reject a stale digest/generation without sequence change, and verify SQLite reopen gives the same remaining set. Cover member settlement, safe retry, abandon, and denied set-bound cancel/fail when one member is not provably safe.
- [ ] **Step 2: Run**

```powershell
uv run --python 3.12 pytest tests/contract/test_state_store.py -k "indeterminate or recovery" tests/integration/test_indeterminate_resolution.py -q
```

Expected: FAIL because no state-store resolution transaction and no control branch issue a resolution Permit.
- [ ] **Step 3: Add one atomic store method** with a closed request type containing the run, member, generation, set digest, strategy, and authoritative observation. In one expected-sequence transaction, validate the current member and set, append the resolution Audit event, update the effect result/owner successor, and recompute the remaining set. Do not accept human-supplied result payloads as evidence.
- [ ] **Step 4: Add the `ResolveIndeterminatePayload` control branch**. It must require `RunState.INDETERMINATE`, exact current bindings, and member/set shape, then issue an `INDETERMINATE` Permit only after the command receipt is accepted.
- [ ] **Step 5: Re-run the same selector** and require both state-store implementations and reopen behavior to pass.
- [ ] **Step 6: Commit** with `feat(recovery): atomically resolve indeterminate members` and update the R4.1 ledger.

### Task 3: Runtime Resolution Strategy and Production Wiring

**Files:**
- Modify: `src/apexcrew/application/runtime.py`
- Modify: `src/apexcrew/application/composition.py`
- Test: `tests/unit/application/test_runtime_resolution.py`
- Test: `tests/integration/test_indeterminate_resolution.py`

- [ ] **Step 1: Write failing runtime tests** for `RECONCILE_OBSERVED`, `RETRY_SAME_INTENT`, and `ABANDON_INTENT`. Assert the consumed Permit is required, exactly one state transition occurs, dispatch stays closed while members remain, and the final successor is `PAUSED`/class-specific or resumes only when the set is empty and all precedence rules pass.
- [ ] **Step 2: Run**

```powershell
uv run --python 3.12 pytest tests/unit/application/test_runtime_resolution.py tests/integration/test_indeterminate_resolution.py -q
```

Expected: FAIL because `ResolutionRuntime` currently only calls `reconcile()` and pauses.
- [ ] **Step 3: Implement RecoveryObservationPort and a concrete resolution registry** that obtains the current unresolved set, dispatches to the named planning snapshot, tool, ref/CAS, reservation, or provider lookup adapter, and delegates the validated internal observation to the store. Normal runtime recovery may settle observable effects automatically; only an accepted human strategy may retry or abandon a previously indeterminate effect.
- [ ] **Step 4: Wire the concrete strategy registry into `build_application_bundle`.** Remove the reachable deferred recovery adapter from the production graph while keeping test-only fail-closed fixtures explicit.
- [ ] **Step 5: Re-run focused tests plus composition contracts**, then run mypy, Ruff, and `git diff --check`.
- [ ] **Step 6: Commit** with `feat(runtime): execute exact indeterminate resolutions`.

### Task 4: Asymmetric Terminal Cleanup Recovery

**Files:**
- Modify: `src/apexcrew/adapters/repository/git.py`
- Modify: `src/apexcrew/application/composition.py`
- Modify: `src/apexcrew/application/runtime.py`
- Test: `tests/unit/adapters/repository/test_target_reservation_cleanup.py`
- Test: `tests/integration/test_composed_runtime_lifecycle.py`

- [ ] **Step 1: Write failing tests** for exact path-only and exact admin-only states. Assert only the reservation-bound `.git` file and empty directory, or only the reservation-bound admin entry, is removed. Add mixed/third/unobservable cases and assert zero deletion and terminal state preservation.
- [ ] **Step 2: Run**

```powershell
uv run --python 3.12 pytest tests/unit/adapters/repository/test_target_reservation_cleanup.py tests/integration/test_composed_runtime_lifecycle.py -q
```

Expected: FAIL because cleanup currently requires registration and path to coexist.
- [ ] **Step 3: Add typed exact partial-state operations** to the Git/OS adapter. Revalidate repository identity, reservation ID, admin binding digest, file bytes, back-reference, and path contents immediately before deletion. Use no generic Git prune and no broad recursive deletion.
- [ ] **Step 4: Make `TerminalCleanupRuntime` settle only after the adapter reports exact absence.** Map cleanup conflict/failure to an administrative stop without changing the terminal Run state.
- [ ] **Step 5: Re-run the same selector**, then run the complete offline lifecycle and static checks.
- [ ] **Step 6: Commit** with `fix(runtime): recover asymmetric target cleanup safely`.

### Task 5: Independent Reviews and Final Verification

**Files:**
- Modify: `PLAN.md`
- Modify: `AGENT_LOG.md`
- Modify: `README.md`
- Modify: `SECURITY.md`
- Test: `tests/integration/test_live_cli_run_lifecycle.py` for the existing opt-in selector

- [ ] **Step 1: Run spec-compliance review** against SPEC 5.8, 5.9, 10.2, and the R4.1 plan. Fix every critical/high finding in a separate correction commit.
- [ ] **Step 2: Run quality review** after spec review is green. Fix every critical/high finding in a separate correction commit.
- [ ] **Step 3: Run the observed verification set:**

```powershell
uv run --python 3.12 pytest -q
uv run --python 3.12 mypy src
uv run --python 3.12 ruff check src tests
uv run --python 3.12 ruff format --check src tests
git diff --check
uv run --python 3.12 python scripts/secret_scan.py
```

Expected: offline tests and static checks pass; secret scan prints `secret-scan: clean`. The live DeepSeek selector remains skipped unless the owner supplies credentials and explicitly sets `APEXCREW_LIVE_SMOKE=1`; a skip is recorded as a boundary, never as live success.
- [ ] **Step 4: Run a fresh-process CLI lifecycle** through `CrewControl`, `CrewRuntime`, and `RunQueries`, reopen SQLite, and verify Permit replay resistance, target OID ordering, exact cleanup, and sanitized output.
- [ ] **Step 5: Record each implementation/review commit and agent/human attribution** in `PLAN.md` and `AGENT_LOG.md`. Mark final-user release only if all required offline and authorized live evidence exists; otherwise list the exact remaining boundary.

## Self-Review

- SPEC 5.8 coverage: Tasks 1-3 cover action-class recovery, exact set digest/generation, member resolution, safe retry/abandon, and precedence; Task 4 covers the reservation cleanup clauses in SPEC 5.3, 6.1, and 10.4.
- No automatic model retry is introduced. Model uncertainty remains indeterminate unless an authoritative provider lookup returns the exact response.
- No generic Git cleanup or broad OS deletion is introduced. All deletion stays tied to one reservation identity and verified bytes.
- The unresolved implementation boundary is intentionally the authorized real DeepSeek provider request; ordinary verification stays offline.

## R4.1 Document Review Correction Addendum

This addendum supersedes any less-specific wording above and is binding for execution.

The four implementation tasks use these exact sequential worktrees: R4.1-01 uses branch codex/m1-r4-1-recovery-domain at .worktrees/m1-r4-1-recovery-domain from reviewed base bc8f7e0; R4.1-02 uses codex/m1-r4-2-resolution-state at .worktrees/m1-r4-2-resolution-state from the exact Final-Reviewed-Base-SHA recorded by the R4.1-01 ledger commit; R4.1-03 uses codex/m1-r4-3-resolution-runtime at .worktrees/m1-r4-3-resolution-runtime from the exact Final-Reviewed-Base-SHA recorded by the R4.1-02 ledger commit; R4.1-04 uses codex/m1-r4-4-asymmetric-cleanup at .worktrees/m1-r4-4-asymmetric-cleanup from the exact Final-Reviewed-Base-SHA recorded by the R4.1-03 ledger commit. Each task has one implementation commit plus separate correction commits and one docs-only ledger commit. No push is authorized.

Each task is blocked until this serial gate completes: implementation red/green evidence; implementation commit with PLAN-Task, Subagent, Human-Changes, Spec-Review, and Quality-Review body fields; independent SPEC review; correction commit for every critical/high issue; independent quality review; correction commit for every critical/high issue; immediate PLAN.md and AGENT_LOG.md ledger update. The next task worktree is created only after the gate.

The closed domain boundary is explicit. RecoveryActionClass is derived from the persisted ModelRequestIntent, ToolIntent, RefCasIntent, TargetReservationCreationIntent, GrantedActionIntent, or typed effect payload. RecoveryObservation is a closed internal union for model normalized response/provider lookup, read/search snapshot and scope, patch pre/post tree, exact check receipt and argv, ref/CAS repository-registration-target-safety-OID tuple, and reservation registration/path identity. No CommandEnvelope field can construct an observation. The store accepts only an internal ApplyResolutionRequest carrying the consumed Permit subject and an observer-created observation.

The Permit resolution subject is persisted and includes selection, intent_id when member-bound, recovery_generation when member-bound, and unresolved_set_digest. Control validates the complete current set for FAIL_RUN and CANCEL_RUN, stores the full envelope digest, and issues exactly one INDETERMINATE Permit. A different command cannot replace an unconsumed Permit; an identical replay can only deliver that original Permit; a consumed Permit cannot issue another. Crash tests cover both after-command-commit/before-consumption and after-consumption.

FAIL_RUN and CANCEL_RUN are set-bound atomic store operations, not member shortcuts. The store re-observes every member internally and rejects the operation unless every member is proven abandonable. On success it closes the complete set in one expected-sequence transaction, applies objective target completion before persisted cancel/pause before class-specific successor, and preserves the unresolved set on denial. Member resolution updates only its owner and leaves dispatch closed until the last member closes.

The action-class matrix is mandatory: model uncertainty is never automatic retry; read/search replays only an unchanged snapshot/scope with canonical bounded ordering and returns no stale content; patch uses exact pre/post tree digests; checks rerun the exact typed argv and collapse receipt idempotency; private-ref and target-CAS require repository identity, direct ref, registration digest, target-safety digest, and old/prepared/current OIDs; target reservation creation/cleanup requires exact reservation observations. Every matrix row has a named red/green test before implementation.

Task 3 must test RuntimeService Permit consumption, owner retention, active-time interval closure, budget/interrupt barrier, sanitized Audit, and RunQueries redaction. Task 4 owns adapters/state for asymmetric cleanup: adapters/system.py, no_follow.py, no_follow_posix.py, no_follow_windows.py, repository/git.py, composition.py, runtime.py, sqlite.py, and memory.py. PATH_ONLY and ADMIN_ONLY use exact handle-bound deletion; MIXED, third, altered, and unobservable states delete nothing and preserve terminal Run state. No generic prune or recursive computed-path deletion is allowed.

Task 5 includes uv sync --frozen --all-groups, make test, make lint, make build, make secret-scan, make demo, full pytest, mypy, Ruff, format check, diff check, reachable-history secret scan evidence, and fresh-process CLI/reopen verification. Existing live selectors are recorded as skipped unless explicit owner authorization and APEXCREW_LIVE_SMOKE=1 produce observed one-request evidence; skipped live output never changes an offline or release status to green.

The reviewed base for R4.1-01 is bc8f7e0, the current document correction commit. Before creating each later worktree, the docs-only ledger commit records one exact Final-Reviewed-Base-SHA for the previous task. The worktree command is git worktree add .worktrees/m1-r4-N branch-name Final-Reviewed-Base-SHA; an unexpanded phrase such as reviewed task commit is not an acceptable base. Each task has one corresponding PR identifier: PR-R4.1-01, PR-R4.1-02, PR-R4.1-03, and PR-R4.1-04.

The observation/transaction boundary is explicit. Runtime adapters collect the complete member observation tuple outside the SQLite transaction, immediately re-observe the external object before submitting it, and compute an internal observation-set digest. ApplyResolutionRequest contains run_id, selection, ordered intent_ids, ordered payload digests, ordered recovery generations, unresolved_set_digest, observation_set_digest, typed observations, permit_generation, owner_id, and expected_sequence. The state transaction performs only durable CAS validation against those fields and the current set; it never calls Git, a provider, Docker, or an OS observer. If any bound field changed, it returns STALE with zero mutation.

RecoveryDecision is closed: COMPLETED with an EffectResult for authoritative post-state, RETRY_SAME_INTENT for an authoritative pre-state whose action class is replay-safe, ABANDONED with a class-specific successor, STALE, CONFLICT, or INDETERMINATE. ABANDONED is never written as an EffectResult outcome; the state transaction writes the typed owner successor and a sanitized Audit result class. No plan step uses an undefined RETRY or ABANDON result.

The exact matrix selectors are mandatory. Task 1 runs these nodes and expects red 8 failed then green 8 passed: test_model_unknown_provider_is_not_retryable, test_read_search_replays_only_same_snapshot, test_patch_replays_only_exact_prestate, test_check_reuses_exact_receipt, test_private_ref_and_target_cas_require_all_safety_fields, test_reservation_observation_classifies_asymmetric_states, test_resolution_selection_requires_exact_member_or_set_shape, and test_unresolved_set_digest_changes_after_member_close. Task 2 expects red 7 failed then green 7 passed for its seven named resolution integration nodes. Task 3 expects red 6 failed then green 6 passed for its six named runtime/wiring nodes. Task 4 expects red 6 failed then green 6 passed for its six named cleanup nodes. Full-file or keyword-only selectors are not evidence for these tasks.

Runtime resolution tests assert that consume_current_runtime_permit installs the owner and opens the active-time interval before external observation, and record_runtime_delivery_stop closes the interval in the same stop transaction. Resolution Audit events allow only intent ID, action class, selection, observation trust label, result class, bounded digest, budget delta, timing, and resulting sequence; RunQueries returns none of the observation payload, provider data, credential, nonce, or raw result.
