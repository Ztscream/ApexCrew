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

There are no build, test, or run commands yet. Proposed commands in `AGENTS.md` are interface commitments for later planning, not current capabilities.

## External Prerequisites

Publishing remains pending: provide the personal GitHub repository URL (or username and repository name), the NJU/GitLab remote details, and a license choice. No LLM API key is needed until the offline MockLLM core is specified and green.
