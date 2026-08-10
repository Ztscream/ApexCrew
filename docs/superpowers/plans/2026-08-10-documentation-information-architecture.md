# ApexCrew Documentation Information Architecture Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the monolithic project and interview documentation with source-mapped architecture guides, executable pseudocode, and private resume/interview study notes.

**Architecture:** Public Markdown under `docs/architecture/` explains observed system behavior and points to the normative `SPEC.md`, current `PLAN.md`, code, and tests. Personal material moves under one ignored `docs/learning/apexcrew-interview/` directory. Every pseudocode document states its source methods, preconditions, state transitions, stop cases, and current delivery status.

**Tech Stack:** Markdown, Mermaid, Python-like pseudocode, Git ignore rules, pytest documentation contracts.

---

### Task 1: Establish the Public Documentation Map

**Files:**
- Create: `docs/architecture/README.md`
- Modify: `README.md`
- Modify: `docs/architecture/system-overview.md`

- [x] **Step 1: Add the architecture reading map**

Create a map that declares `SPEC.md` normative, `PLAN.md`/`AGENT_LOG.md` operational, source/tests executable evidence, and architecture files explanatory. Link every numbered guide and pseudocode document.

- [x] **Step 2: Correct historical overview wording**

Mark the old Stage 4 source-absence statement as historical, update the provider wording to DeepSeek Responses, and link to the new map without rewriting frozen process history.

- [x] **Step 3: Add README navigation**

Link the public architecture map from the README documentation section and keep the README as onboarding rather than a duplicate design specification.

### Task 2: Write the Runtime and Authority Guides

**Files:**
- Create: `docs/architecture/01-harness-overview.md`
- Create: `docs/architecture/02-control-runtime-query.md`
- Create: `docs/architecture/03-coordinator-worker-loop.md`
- Create: `docs/architecture/04-authority-permit-grant.md`

- [x] **Step 1: Document system scope and public boundaries**

Map `CrewControl`, `CrewRuntime`, `RunQueries`, `CoordinatorService`, and `WorkerLoopService` to their source files. Explain the anti-bypass reason for each boundary.

- [x] **Step 2: Document the control/runtime/query lifecycle**

Describe command acceptance, Permit issuance/consumption, per-Run ownership, stop projections, and WebUI query-only access.

- [x] **Step 3: Document planning and worker turns**

Describe durable planning, task selection, attempt/lease creation, structured model action parsing, feedback, and stop states.

- [x] **Step 4: Document authority composition**

Describe Policy, budget, deadline, Lease, Grant, expected sequence, and exact pre-state binding as separate required conditions.

### Task 3: Write the Evidence, Git, Recovery, Security, and Test Guides

**Files:**
- Create: `docs/architecture/05-evidence-freshness-admission.md`
- Create: `docs/architecture/06-git-candidates-cas.md`
- Create: `docs/architecture/07-recovery-and-durability.md`
- Create: `docs/architecture/08-security-and-executor.md`
- Create: `docs/architecture/09-testing-and-acceptance.md`

- [x] **Step 1: Explain evidence and freshness gates**

Map Context Capsule, Evidence Receipt, Freshness Assessment, Task Candidate, and Run Candidate. State the difference between current checked-in behavior and R4.3 target behavior.

- [x] **Step 2: Explain Git candidate and CAS workflow**

Document target pinning, reservation, private Run Head, candidate separation, final CAS, and why the target ref is never a Worker write target.

- [x] **Step 3: Explain durable effects and recovery**

Document intent-before-effect, result settlement, recovery observation, and `INDETERMINATE`; do not call it exactly-once.

- [x] **Step 4: Explain security and execution containment**

Document canonical paths, no-follow defenses, typed Git argv, credential isolation, restricted Docker contract, and the open Docker execution debt.

- [x] **Step 5: Explain testing evidence**

Document unit/contract/integration/acceptance scopes, ScriptedMockLLM, temporary Git/SQLite, R4.3 reviewed-baseline result, and the difference between collected tests and passed tests.

### Task 4: Add Source-Mapped Pseudocode

**Files:**
- Create: `docs/architecture/pseudocode/README.md`
- Create: `docs/architecture/pseudocode/01-command-permit.md`
- Create: `docs/architecture/pseudocode/02-runtime-recovery.md`
- Create: `docs/architecture/pseudocode/03-worker-turn.md`
- Create: `docs/architecture/pseudocode/04-evidence-candidate.md`
- Create: `docs/architecture/pseudocode/05-git-final-cas.md`

- [x] **Step 1: Define pseudocode conventions**

Declare that snippets are explanatory only, identify source files/methods, distinguish observed code from R4.3 planned behavior, and use `STOP`, `PAUSE`, and `INDETERMINATE` consistently.

- [x] **Step 2: Add command and runtime pseudocode**

Show command application/Permit issuance and `RuntimeService.run_until_blocked`, including ownership, Permit consumption, recovery ordering, fault classification, and durable stop recording.

- [x] **Step 3: Add Worker and evidence pseudocode**

Show context construction, model reservation, one-action parsing, authorization, intent/settlement, feedback, freshness assessment, and candidate rejection.

- [x] **Step 4: Add Git candidate/CAS pseudocode**

Show R4.3 target-state design for Task Candidate/private head and Run Candidate/final CAS; label unreviewed or unfinished steps as target behavior.

### Task 5: Split Private Interview Material

**Files:**
- Modify: `.gitignore`
- Modify local-only: `docs/learning/APEXCREW_INTERVIEW_GUIDE.md`
- Create local-only: `docs/learning/apexcrew-interview/README.md`
- Create local-only: `docs/learning/apexcrew-interview/01-harness-foundations.md`
- Create local-only: `docs/learning/apexcrew-interview/02-project-deep-dive.md`
- Create local-only: `docs/learning/apexcrew-interview/03-pseudocode-explained.md`
- Create local-only: `docs/learning/apexcrew-interview/04-resume-star.md`
- Create local-only: `docs/learning/apexcrew-interview/05-interview-qa.md`
- Create local-only: `docs/learning/apexcrew-interview/06-whiteboard-demo.md`

- [x] **Step 1: Add an exact directory ignore beside the legacy entry-point rule**

Use `/docs/learning/apexcrew-interview/` so all personal pages remain local without ignoring tracked learning notes.

- [x] **Step 2: Turn the old guide into a local entry point**

Retain the existing filename as a short index that links to the split local pages.

- [x] **Step 3: Split learning, project, pseudocode, resume, Q&A, and whiteboard content**

Move material by audience rather than duplicating public architecture prose; local pages should link to public guides for source-level detail.

### Task 6: Verify Documentation Integrity

**Files:**
- Verify: all public architecture documents
- Verify: README and `.gitignore`
- Verify local-only: `docs/learning/apexcrew-interview/`

- [x] **Step 1: Check document links and headings**

Run a local Markdown-link scan and confirm every public architecture file has source mapping, invariants, pseudocode/test references, and a current-status boundary.

- [x] **Step 2: Run documentation contracts**

Run:

```powershell
uv run --python 3.12 pytest tests/contract/test_documentation_delivery.py tests/contract/test_release_artifacts.py -q
```

Expected: `4 passed`.

- [x] **Step 3: Check Git state and ignored personal material**

Run:

```powershell
git check-ignore -v docs/learning/apexcrew-interview/README.md
git diff --check
git status --short --ignored
```

Expected: public documents appear as intended changes, personal documents are ignored, and whitespace checking exits 0.
