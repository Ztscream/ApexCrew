---
status: accepted
date: 2026-07-26
---

# Gate prepared commits at Task and Run levels

ApexCrew will verify immutable prepared commits at two levels. A Task Candidate is materialized with the current private Run Head as its parent, checked, and promoted to that private ref only through a lock and compare-and-swap. After all Tasks are promoted, a Run Candidate is prepared against the expected user target, receives a mandatory run-wide Evidence Bundle, and waits for a single-use human Approval Grant before an exact compare-and-swap integration. ApexCrew does not push.

Worker branch names, tips, patch hashes, and receipts from an earlier parent cannot authorize either transition. Declared dependencies allow targeted invalidation after private promotions, while unknown changes trigger global invalidation; final run-wide checks remain mandatory because declarations cannot prove that every semantic dependency was captured.

This rejects worker-only verification, merge-then-test rollback, and per-Task human integration. The consequence is additional snapshot preparation and verification work, but the revision that passed checks is the revision admitted, dependent Tasks can advance while the developer is absent, and target movement closes safely by invalidating the candidate and approval rather than guessing.
