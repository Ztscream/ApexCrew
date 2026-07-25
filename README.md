# ApexCrew

ApexCrew is a local-first Coding Agent Harness for an **Evidence-Driven Durable Crew**. It coordinates bounded work in one Git repository, requires human approval for risky actions, and accepts changes only when objective evidence is fresh for the exact revision being integrated.

## Status

This repository is intentionally documentation-only. Discovery and local repository initialization are complete; formal specification has not started. Do not add persistent implementation until three Superpowers brainstorming rounds produce an approved `SPEC.md`, `writing-plans` produces `PLAN.md`, and a different agent completes the required cold-start review. That review may generate code only in a disposable isolated worktree; its code is never merged or retained.

## Accepted v1 Boundary

- One local repository and one user.
- At most three ApexCrew-owned Workers.
- A self-built Coordinator loop and WorkerLoop using low-level model completion APIs through a narrow `ModelPort`.
- Python and TypeScript micro-repositories as acceptance fixtures.
- Revision-bound context, checks, approvals, and integration evidence.
- No external Coding Agent CLI or high-level agent framework in the assessed core.

The target user is a developer who needs long-running agent collaboration to remain inspectable and recoverable. Novelty is a hypothesis, not a claim: established projects already cover many individual mechanisms.

## Repository Map

- `INITIALIZATION.md`: accepted scope, prerequisites, stages, and remaining inputs.
- `CONTEXT.md`: canonical domain vocabulary.
- `docs/adr/`: accepted, hard-to-reverse decisions.
- `docs/architecture/`: explanatory system maps, subordinate to the future spec.
- `docs/research/`: primary-source landscape and value-hypothesis evidence.
- `docs/experiments/`: falsifiable experiments and fixture matrix.
- `docs/learning/`: interview-oriented notes created only after executable evidence exists.
- `SPEC_PROCESS.md` and `AGENT_LOG.md`: required development-process evidence.
- `LICENSE` and `NOTICE`: Apache-2.0 terms and the explicit scope exception for course-provided documents.

There are no build, test, or run commands yet. Proposed commands in `AGENTS.md` are interface commitments for later planning, not current capabilities.

## Repository and License

The public GitHub remote is `https://github.com/Ztscream/ApexCrew.git`. Original ApexCrew material is available under [Apache License 2.0](LICENSE); see [NOTICE](NOTICE) for the course-document exception. The NJU/GitLab remote is explicitly deferred and does not block current GitHub development.

Before Stage 2 exits, select the low-level LLM provider/model, course schedule, final WebUI mode, and fixture problems. No API credential value is needed until the offline `ScriptedMockLLM` core is specified and green.
