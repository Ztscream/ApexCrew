# ApexCrew Specification Process

This file records how ApexCrew moves from an idea to an approved `SPEC.md` and `PLAN.md`. The initialization work below is **Stage 0 discovery**; it does not count as the three Superpowers brainstorming iterations required by the course.

## Stage 0 Decision Record

| Question | Initial hypothesis | Evidence or challenge | Accepted decision |
|---|---|---|---|
| Product value | Multi-agent collaboration, long context, continuous cowork | These capabilities are already common across Coding Agent products. | Focus on an Evidence-Driven Durable Crew with revision-bound handoffs. |
| Differentiation | Durable state, worktrees, evidence, approval | Bernstein and h5i substantially overlap; individual mechanisms are not novel. | Treat the contribution as a small, testable combination and avoid first-of-kind claims. |
| Evidence quality | Passing repository checks is enough | Weak checks may admit wrong patches; mutation/property approaches also have prior art. | Keep weak-oracle challenge as a supporting experiment, not a product rename. |
| Core execution | Reuse host Coding Agent defaults | A-class rules require the submitted harness to own the loop. | Implement Coordinator and WorkerLoop; providers expose only low-level completions. |
| v1 scope | Broad capabilities and provider choice later | Breadth would hide the main mechanism and weaken deterministic tests. | One repository, at most three Workers, Python + TypeScript fixtures. |

The accepted decision is recorded in [ADR-0001](docs/adr/0001-evidence-driven-durable-crew.md). The landscape report remains evidence, not authority.

## Formal Brainstorming - Pending

Run the installed Superpowers `brainstorming` workflow in three explicit, user-approved rounds. Preserve concise excerpts and record both accepted and rejected suggestions.

1. **Round 1 - Problem and scenarios**: target user, painful long-running collaboration failures, user stories, non-goals, and measurable value hypothesis.
2. **Round 2 - Mechanisms and state**: WorkerLoop protocol, task/lease lifecycle, context freshness, evidence gate, approvals, crash boundaries, data model, and Python/TypeScript fixture behavior.
3. **Round 3 - Operations and acceptance**: credentials, WebUI, distribution, observability, budgets, failure handling, security threat model, and objective acceptance criteria.

Each round must log questions asked, the user's decision, alternatives rejected, remaining ambiguity, and the resulting `SPEC.md` diff. Do not mark a round complete based only on an agent-generated draft.

### Round 1 - Problem and scenarios (approved 2026-07-26)

| # | Question | User decision | Design consequence |
|---|---|---|---|
| 1 | Which user task is the v0.1 primary journey? | **A**: one developer delegates a cross-module task lasting hours, leaves, and later returns. | The run must survive absence/restart and may integrate only changes backed by evidence fresh for the current revision. Real-time supervision and multi-issue throughput are secondary. |
| 2 | Which failure most clearly makes v0.1 worthless? | **A**: integrating a change using context or check results that no longer apply to the current revision. | Revision-bound evidence, dependency-aware invalidation, and mandatory revalidation are the main contribution. Recovery, conflict control, and low-supervision operation remain supporting requirements. |
| 3 | What should await the returning developer after a successful run? | **A**: a fresh-evidence Integration Candidate, timeline, and summary; final merge still needs one human approval. | Successful execution ends at an approval-ready state. v0.1 never auto-merges or pushes, and it must make the evidence and change history inspectable. Round 2 later split this provisional term into Task Candidate and Run Candidate. |
| 4 | Who creates the initial Task DAG and Task Contracts? | **A**: the developer provides the goal, constraints, and acceptance requirements; the Coordinator proposes a bounded DAG and write scopes; the developer approves once before Workers start. | Decomposition is an ApexCrew capability, but execution cannot begin from an unapproved plan. Unbounded autonomous task creation is out of scope. |
| 5 | Which baseline should test the main value claim? | **A**: the same scripted trajectory, Workers, and fixtures with revision binding and dependency invalidation disabled. | The primary evaluation is an ablation that isolates evidence freshness. Multi-Worker and human baselines may be secondary, but cannot substitute for this test. |

#### Approved Round 1 conclusion

- The primary user is one developer delegating one cross-module task that runs for hours in an existing repository.
- The Coordinator proposes a bounded DAG and Task Contracts from a human goal and acceptance requirements; Workers start only after one human confirmation.
- The product's main failure is accepting context or checks that no longer apply to the current revision.
- A successful Crew Run stops with what Round 1 provisionally called a fresh-evidence Integration Candidate, plus a timeline and summary awaiting final human merge approval; Round 2 later named this final object the Run Candidate. It never auto-merges or pushes.
- The main contribution is revision-bound evidence, dependency-aware invalidation, and mandatory revalidation. The primary experiment is an otherwise-identical ablation with those freshness rules disabled.
- Recovery, lease isolation, risky-action approval, and low-supervision execution are required support. Multi-issue throughput, real-time supervision, and unbounded task creation are not the v0.1 focus.

The user explicitly approved this round. Its problem and scenario section is accepted input to the future consolidated `SPEC.md`.

### Round 2 - Mechanisms and state (approved 2026-07-26)

| # | Question | User decision | Design consequence |
|---|---|---|---|
| 1 | How should repository changes invalidate context and evidence? | **B**: a declared dependency graph with conservative global fallback. | Versioned Task edges, read/write path globs, and required checks drive affected-closure invalidation. Unknown paths, renames, or graph/policy mismatches invalidate all non-terminal work. |
| 2 | Which revision must final evidence verify? | **B**: an isolated integration snapshot prepared from the current integration head. | A receipt for a Worker tip cannot authorize promotion. Checks bind to the prospective prepared commit and expected parent; head movement forces preparation and verification again. |
| 3 | How many actions may one WorkerLoop model call drive? | **A**: exactly one typed action. | ApexCrew persists one action intent and result per turn, then returns structured tool feedback to the next low-level completion. Free-form CLI sessions cannot implement the assessed loop. |
| 4 | How should recovery handle an action that may already have caused a side effect? | **B**: reconcile by action class and pause on uncertainty. | File, Git, and check actions use idempotency keys plus expected pre/post state. Unconfirmable external effects enter `INDETERMINATE`; ApexCrew makes no universal exactly-once claim. |
| 5 | How should dependent tasks advance before final human integration? | **B**: serial promotion to a private local Run Branch, followed by one final approval. | Verified Task Candidates advance only `refs/apexcrew/runs/<run-id>` under a lock and CAS. A frozen Run Candidate receives run-wide checks before a revision-bound Grant may update the user target; ApexCrew never pushes. |
| 6 | What happens when a running Worker Attempt becomes stale? | **B**: let the current atomic action settle, then stop and refresh from the latest Run Head. | The old Attempt becomes `STALE`, loses its lease, and cannot hand off. Known changes restart only the affected closure; unknown, plan, or policy changes trigger global invalidation and a human pause. |
| 7 | Which failures may be corrected without a new human approval? | **B**: retry within an immutable Plan Revision and Task Contract; reapprove structural changes. | Objective failures feed the Worker within a fixed budget. Changing the DAG, dependency/write scope, required checks, or policy creates a reviewable new Plan Revision; exact budgets belong to Round 3. |

#### Approved Round 2 conclusion

- The execution path is `approved Plan Revision -> Worker Attempts -> prepared Task Candidates -> private serial promotions -> frozen Run Candidate -> run-wide verification -> one final human-approved CAS integration`.
- A versioned declared dependency graph enables targeted invalidation, but unknown changes fall back to global invalidation. Dependency pruning is an optimization, not a claim to discover every semantic dependency; final run-wide checks are mandatory.
- `ModelPort` exposes low-level structured completion only. Each turn yields one typed `ActionEnvelope`; ApexCrew owns action validation, policy, execution, persistence, feedback, budgets, and stopping.
- SQLite will keep transactional state and an append-only audit trail, while Git object IDs identify repository snapshots. Action recovery reconciles recorded intent against observable state and never guesses about uncertain external effects.
- Run, Task, Worker Attempt, Task Candidate, and Run Candidate have separate lifecycles. An immutable Evidence Receipt is never edited to become stale; a separate Freshness Assessment determines current admissibility.
- Task promotion is private and automatic only after its current prepared commit passes the Task Contract gate. Final target integration is never automatic: its Approval Grant binds the prepared commit, expected target OID, Evidence Bundle, contract, and policy, and is single-use.
- The Python fixture changes a fee API from integer cents to decimal dollars; the TypeScript fixture changes session timestamps from milliseconds to seconds. In both, branches are individually green and merge without text conflict, but old evidence must be rejected and refreshed checks expose a deterministic semantic failure.

#### Rejected suggestions and limits

- A global revision fence remains the conservative fallback, not the normal path; fully dynamic dependency inference is too complex and potentially unsound for v0.1.
- Worker-tip-only verification, merge-then-test rollback, model-generated action batches, free-form external agent sessions, unconditional replay, hard-killing an active tool, and silent Coordinator replanning were rejected because they break the freshness, recovery, course-ownership, or human-control boundaries.
- Per-Task human promotion and pausing on every red check were rejected because they prevent the approved hours-long unattended journey without improving final-revision evidence.
- Declared dependencies can omit a semantic relationship. ApexCrew detects declaration and revision mismatches, not arbitrary hidden coupling; the final run-wide suite and explicit documentation bound this residual risk.

#### Remaining ambiguity for Round 3

- Concrete step, token, time, retry, and spend budgets; timeout and escalation defaults.
- Selected real LLM provider/model and environment fingerprint policy; offline acceptance still uses `ScriptedMockLLM`.
- Credential lifecycle, WebUI approval interaction, observability/redaction, supported distribution platforms, and the full threat model.
- Exact run-wide command set, usability targets, performance thresholds, and final objective acceptance matrix.

There is intentionally no partial `SPEC.md` diff yet. The accepted Round 1 and Round 2 sections will be consolidated only after Round 3 closes the operational and acceptance requirements.

## Stage 2 Exit Checklist

- [ ] State the problem, target user, value hypothesis, and at least five INVEST user stories.
- [ ] Specify each functional module's input, behavior, output, boundary conditions, and errors.
- [ ] Cover performance, security, usability, and observability requirements.
- [ ] Define architecture, data model, external dependencies, and the selected LLM provider/model with rationale; a MockLLM-only choice requires an explicit course-compliance rationale.
- [ ] Design the four domain mechanisms: tools/actions, objective feedback, risky actions/HITL, and cross-session memory/context.
- [ ] Give decision, tools, memory, governance, feedback, and configuration a testable minimum implementation; select one mechanism-dense dimension as the main contribution.
- [ ] Show how every core mechanism remains deterministically testable with `ScriptedMockLLM` and no network.
- [ ] Define the credential threat model and lifecycle, distribution target, supported platforms, and required WebUI.
- [ ] Attach objective acceptance criteria, risks, open questions, and the Python/TypeScript fixture contract.
- [ ] Record three user-approved iterations, adopted/rejected AI suggestions, and a candid reflection on the brainstorming workflow.

## Planning and Cold-Start Review - Pending

After `SPEC.md` is signed off, use Superpowers `writing-plans` to create 2-5 minute TDD tasks with explicit paths, dependencies, failing tests, commands, and expected evidence. Then give only `SPEC.md` and `PLAN.md` to a different agent type in a fresh session. That reviewer may attempt 1-2 tasks only in a disposable isolated worktree. Record every pause, incorrect interpretation, output gap, and resulting revision; then remove the review worktree without merging or retaining its code. This is the sole exception to the pre-implementation gate.
