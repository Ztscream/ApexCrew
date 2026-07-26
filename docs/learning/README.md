# ApexCrew Learning Notes

This directory is an interview-oriented engineering notebook, not a second specification. Create a topic file only when the corresponding mechanism has executable evidence. Link decisions to `SPEC.md` or an ADR, behavior to tests, and lessons to the relevant `AGENT_LOG.md` entry.

## Planned Topics

- `01-harness-loops.md`: Coordinator planning/execution and Worker loops, action protocols, start/stop conditions, and why ApexCrew owns both loops.
- `02-multi-agent-coordination.md`: execution DAGs, promotion hazards, head-admissible leases, worktree isolation, and serial integration.
- `03-context-provenance.md`: Context Capsules, token budgets, freshness, contradictions, and handoff loss.
- `04-revision-bound-evidence.md`: Evidence Receipts/Bundles, dependency fingerprints, invalidation, and what green checks cannot prove.
- `05-git-worktrees.md`: detached workspace lifecycle, Target Reservation cleanup, Git config/index/history-metadata preflight, prepared commits, and conflict recovery.
- `06-governance-hitl.md`: frozen Policy, hard denial, Secret Path Set, exact-intent Approval Grants, replay resistance, credentials, and trust boundaries.
- `07-feedback-and-tdd.md`: structured failures, self-correction, deterministic MockLLM tests, and Python/TypeScript fixture differences.
- `08-persistence-recovery.md`: state machines, Runtime Permits, pre-dispatch intents/reservations, multi-intent resolution, purge tombstone queries, crash injection, and replay.
- `09-evidence-quality.md`: weak oracles, hidden checks, counterexamples, property/mutation testing, and why adversarial acceptance is only a supporting experiment.
- `10-budget-progress.md`: hard ceilings, returned-model authorization/pricing, tranche allocation, objective progress, cost reservation, and deterministic stopping.
- `11-security-containment.md`: untrusted-repository threat model, typed capabilities, Git/Docker containment, full-history secret scanning, and residual risk.
- `12-delivery-observability.md`: Audit Ledger versus Restricted Transcript, read-only WebUI, static fixture replay, distribution, and CI/CD.
- `13-deep-module-architecture.md`: A-Hybrid interfaces, internal module depth/locality, seam placement, and why minimal-kernel/flexible/journey alternatives were rejected.

## Required Shape for Each Note

1. **Interview question** and a 30-60 second answer.
2. **Concept** in plain language, including the problem it solves.
3. **ApexCrew decision** and its explicit invariant.
4. **Alternatives and trade-offs**, including why a plausible option was rejected.
5. **Executable evidence**: links to tests, demo commands, code, commit, and measured output.
6. **Failure diary**: one real failure, diagnosis, correction, and prevention.
7. **Follow-up questions** that probe limitations and future work.

## Maintenance Rule

Update the relevant note during the refactor step of each `PLAN.md` task, after tests are green. Do not paste generated tutorials or full chat transcripts. Prefer one concrete diagram or trace, one small code excerpt, and one verified failure story. At release time, use these notes to prepare the README demo narrative and your own `REFLECTION.md`; the reflection itself must remain student-authored.
