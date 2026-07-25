---
status: accepted
date: 2026-07-26
---

# Make durable, revision-bound evidence the mainline

ApexCrew will pursue **Evidence-Driven Durable Crew** as its only product mainline: one local repository, at most three ApexCrew-owned Workers, and integration controlled by context, approval, and verification evidence bound to the current revision. The assessed core owns both the Coordinator loop and WorkerLoop and calls models only through a low-level completion port; host Coding Agent CLIs and high-level agent runners are development tools or later experiments, never substitutes for the core.

This choice favors a bounded backend and Agent-engineering problem that can be tested offline over a broad agent platform. Python and TypeScript micro-repositories provide cross-ecosystem fixtures without making the harness multi-language internally.

Competitor research found direct overlap with durable ledgers, worktrees, evidence gates, recovery, context capsules, mutation testing, and adversarial checks. Therefore ApexCrew will not claim those mechanisms are individually novel. Adversarial acceptance and weak-oracle challenges remain supporting experiments for judging evidence quality; they do not rename or replace the accepted mainline. A later change to the mainline must explicitly supersede this ADR.
