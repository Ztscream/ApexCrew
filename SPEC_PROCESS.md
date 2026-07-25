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
