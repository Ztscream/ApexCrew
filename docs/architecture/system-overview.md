# System Overview

> Initialization architecture, 2026-07-26. This map explains the accepted boundary; the future approved `SPEC.md` will own behavioral requirements and data details.

## Scope

ApexCrew coordinates one developer goal in one local Git repository with at most three logical Workers. The product owns both decision loops. A model provider returns one low-level completion at a time and never controls tools, approval, scheduling, or stop conditions.

```mermaid
flowchart LR
    USER["Developer / CLI / WebUI"] --> API["Harness API"]
    API --> COORD["Coordinator Loop"]
    COORD --> BOARD["Task Contracts and Leases"]
    COORD --> WORKER["WorkerLoop, max 3"]
    WORKER --> CONTEXT["Context Capsule Builder"]
    WORKER --> POLICY["Risk Policy and Approval"]
    WORKER --> TOOLS["Repository and Process Tools"]
    WORKER --> MODEL["Low-level ModelPort"]
    TOOLS --> EVIDENCE["Verifier and Evidence Gate"]
    CONTEXT --> FRESH["Freshness / Invalidation"]
    EVIDENCE --> FRESH
    COORD --> STORE["Durable Run Store"]
    WORKER --> STORE
    FRESH --> STORE
```

## Responsibilities

| Area | Owns | Must not own |
|---|---|---|
| Coordinator | DAG progress, leases, handoff, invalidation, serial integration, pause/resume | Model-provider behavior or arbitrary shell policy |
| WorkerLoop | Context assembly, one model call, structured action parsing, tool feedback, stop decisions | Scheduling other Workers or declaring its own integration success |
| Domain core | Contracts, revisions, evidence, approvals, state transitions, invariants | Git, SQLite, provider SDK, FastAPI, or OS calls |
| Adapters | Git/worktrees, subprocesses, persistence, credentials, model completion | Product policy and acceptance decisions |
| Delivery | CLI/WebUI commands and read models | Alternate business rules |

## Non-Negotiable Invariants

1. An Integration Candidate is admitted only with a complete, fresh Evidence Bundle for its exact repository and policy revisions.
2. A Worker writes only through an active Workspace Lease inside the configured repository.
3. A risky action executes only under an unmodified, unexpired, one-use Approval Grant; hard denials never reach the executor.
4. Restart does not duplicate a recorded action or integration. An uncertain external side effect becomes `INDETERMINATE` and requires human resolution.
5. The complete core remains deterministic under `ScriptedMockLLM` and requires no network.

## Proposed Module Shape

The implementation plan should evaluate, not blindly adopt, `src/apexcrew/core/`, `adapters/`, `bootstrap/`, and `delivery/`. Acceptance fixtures should live outside production modules, with separate Python and TypeScript repositories. No directories are created until specification, planning, and cold-start gates pass.
