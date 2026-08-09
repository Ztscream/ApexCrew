# R4.3-04 Candidate Gates Correction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: implement task-by-task with red/green evidence and independent SPEC and quality reviews.

**Goal:** Make Task Candidate preparation and private Run Head promotion satisfy the two-level gate, recovery, lifecycle, and bounded-runtime requirements in SPEC 5.7, 5.8, 6.2, and 6.3.

**Architecture:** Keep Candidate validation and private CAS inside Admission. The state adapters expose the existing typed provenance and lifecycle records; a short OS-held Run-ref lock surrounds only the final private CAS observation and issuance. Runtime records the step-cap pause durably before returning. No target ref is changed by this task.

**Tech Stack:** Python 3.12, Pydantic domain models, SQLite/Memory state adapters, typed Git adapter operations, pytest, Ruff, mypy.

## Termination Conditions

- The implementation pass ends when the focused selectors for every review finding pass and the full offline suite, lint, secret scan, compile check, and diff check pass.
- This task permits one correction commit before the SPEC rerun and one correction commit before the quality rerun. A review finding may not trigger an unbounded retry loop.
- Each review is run once after the corresponding correction. If the same blocking finding remains across three consecutive task turns, stop the task as blocked and report the exact finding and evidence.
- Runtime phase processing has a persisted maximum of 256 phase transitions per invocation. Reaching the cap commits `PAUSED` with a stable reason and invalidates further continuation until an exact reason-bound resume; a second call cannot silently resume the old permit or re-enter the same capped invocation.
- R4.3-04 is complete only after the ledger records the implementation/correction commit and both final reviewer IDs. It does not freeze a Run Candidate or update the user target ref; those are R4.3-05 responsibilities.

## File Map

- Modify `src/apexcrew/domain/admission.py`: validate the complete Task Candidate binding and private promotion preconditions, including evidence, freshness, approval, scope, lease provenance, hazard order, and Run-ref safety.
- Modify `src/apexcrew/adapters/state/memory.py` and `src/apexcrew/adapters/state/sqlite.py`: expose/commit the matching lifecycle and recovery transitions without weakening CAS or idempotency checks.
- Modify `src/apexcrew/adapters/repository/git.py` and `src/apexcrew/adapters/repository/candidate_preparation.py`: hold and validate the canonical private-ref lock and make preparation filesystem operations no-follow and handle-bound.
- Modify `src/apexcrew/application/runtime.py`: persist the bounded phase-cap pause and prevent repeated continuation of a capped invocation.
- Modify `tests/integration/test_task_candidate_promotion.py`, `tests/integration/test_candidate_preparation.py`, and the nearest runtime tests: capture each invalid binding, recovery class, lifecycle transition, and cap behavior before implementation.
- Modify `PLAN.md` and `AGENT_LOG.md`: record observed red/green evidence, commit attribution, final reviewer IDs/configuration, and the task completion SHA.

## Task 1: Candidate Gate Tests

**Files:**

- Test: `tests/integration/test_task_candidate_promotion.py`
- Test: `tests/integration/test_candidate_preparation.py`

- [ ] Add failing selectors for mismatched Attempt provenance, patch post-state, Evidence Bundle, Freshness Assessment, exact action approval, check-workspace digest, scope/Policy binding, and lease provenance.
- [ ] Add failing selectors proving promotion requires a short Run-ref lock, exact direct private-ref identity, unchanged target/user refs and checkout safety, and hazard predecessors.
- [ ] Add failing selectors for recovery at exact old OID (replay), prepared OID (success), and third-state (conflict), plus Task/Attempt transition on success and failure.
- [ ] Run only the new selectors and record their failure output in `AGENT_LOG.md` before implementation.

## Task 2: Candidate Admission Implementation

**Files:**

- Modify: `src/apexcrew/domain/admission.py`
- Modify: `src/apexcrew/adapters/state/memory.py`
- Modify: `src/apexcrew/adapters/state/sqlite.py`

- [ ] Make preparation accept a candidate only when all bindings name the same Run Head, Attempt, Task Contract, post-tree state, Evidence Bundle, Freshness Assessment, approvals, and check workspace digest.
- [ ] Validate lease provenance at the recorded generation/head and permit normal release/expiry after a successful Attempt; reject any invalidated input or later dependency change.
- [ ] Advance the Task from `ACTIVE` to `CANDIDATE_READY`, and on successful private CAS advance the Attempt to terminal success and the Task to `PROMOTED` atomically with the durable Run Head update.
- [ ] Preserve a still-fresh Candidate on known CAS failure, classify exact old OID as replayable, exact prepared OID as success, and any other observation as conflict/indeterminate according to SPEC 5.8.
- [ ] Run the focused tests to green, then run the existing candidate and lifecycle suites.

## Task 3: Repository Safety and Runtime Cap

**Files:**

- Modify: `src/apexcrew/adapters/repository/git.py`
- Modify: `src/apexcrew/adapters/repository/candidate_preparation.py`
- Modify: `src/apexcrew/application/runtime.py`
- Test: nearest existing runtime and repository safety suites

- [ ] Create the Run-ref lock only under the canonical private data root, validate the resolved lock path and handle identity, and release it on every exit path.
- [ ] Use handle-relative/no-follow reads for temporary index directories and hash materialized workspace files through the already validated handle or immutable workspace snapshot, closing pathname races.
- [ ] Persist `PAUSED` and the stable cap reason when the phase counter reaches 256; return the persisted stop and reject continuation while the cap cause remains unresolved.
- [ ] Run focused no-follow, runtime-permit, recovery, and target-safety tests to green.

## Task 4: Verification and Review Closeout

**Files:**

- Modify: `PLAN.md`
- Modify: `AGENT_LOG.md`

- [ ] Run `make test`, `make lint`, `make secret-scan`, `uv run --python 3.12 python -m compileall -q src tests`, and `git diff --check`; retain observed output.
- [ ] Commit the implementation with Conventional Commit text containing `PLAN-Task`, `Subagent`, and `Human-Changes` attribution.
- [ ] Run one independent SPEC review with reviewer configuration `gpt-5.6-luna-max`, fix Critical/High findings in one bounded correction pass, and rerun once.
- [ ] Run one independent quality review with the same configuration, fix Critical/High findings in one bounded correction pass, and rerun once.
- [ ] Update the R4.3-04 ledger row only after both final reviews pass; record all commit hashes and evidence, then stop this task.

## Self-Review Checklist

- [ ] No code path in this task accepts a missing prepared OID or silently falls back to the current head.
- [ ] No private CAS can update a target ref, user ref, or checked-out private ref.
- [ ] No recovery branch labels exact old pre-state as a third state.
- [ ] No runtime call can exceed the persisted phase cap or continue from an unresolved cap pause.
- [ ] Tests assert zero repository/state side effects for every rejected binding.
