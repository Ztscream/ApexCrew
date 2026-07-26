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
| 3 | What should await the returning developer after a successful run? | **A**: a fresh-evidence Integration Candidate, timeline, and summary; final merge still needs one human approval. | Successful execution ends at an approval-ready state. v0.1 never auto-merges or pushes, and it must make the evidence and change history inspectable. |
| 4 | Who creates the initial Task DAG and Task Contracts? | **A**: the developer provides the goal, constraints, and acceptance requirements; the Coordinator proposes a bounded DAG and write scopes; the developer approves once before Workers start. | Decomposition is an ApexCrew capability, but execution cannot begin from an unapproved plan. Unbounded autonomous task creation is out of scope. |
| 5 | Which baseline should test the main value claim? | **A**: the same scripted trajectory, Workers, and fixtures with revision binding and dependency invalidation disabled. | The primary evaluation is an ablation that isolates evidence freshness. Multi-Worker and human baselines may be secondary, but cannot substitute for this test. |

#### Approved Round 1 conclusion

- The primary user is one developer delegating one cross-module task that runs for hours in an existing repository.
- The Coordinator proposes a bounded DAG and Task Contracts from a human goal and acceptance requirements; Workers start only after one human confirmation.
- The product's main failure is accepting context or checks that no longer apply to the current revision.
- A successful Crew Run stops with a fresh-evidence Integration Candidate, timeline, and summary awaiting final human merge approval; it never auto-merges or pushes.
- The main contribution is revision-bound evidence, dependency-aware invalidation, and mandatory revalidation. The primary experiment is an otherwise-identical ablation with those freshness rules disabled.
- Recovery, lease isolation, risky-action approval, and low-supervision execution are required support. Multi-issue throughput, real-time supervision, and unbounded task creation are not the v0.1 focus.

The user explicitly approved this round. No final design section or `SPEC.md` text is approved yet.

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
