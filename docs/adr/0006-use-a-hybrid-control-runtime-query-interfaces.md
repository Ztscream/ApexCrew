---
status: accepted
date: 2026-07-26
---

# Use A-Hybrid control, runtime, and query interfaces

ApexCrew exposes only `CrewControl.handle`, `CrewRuntime.run_until_blocked`, and `RunQueries.get` for Run behavior. Coordinator owns guarded read-only planning and later scheduling; Admission alone validates/prepares candidates and issues typed CAS requests; WorkerLoop, Authority, EffectJournal/recovery, tools, and projection remain internal deep modules, while Git, SQLite, Docker, credentials, clocks/IDs, and model providers sit behind internal seams. The CLI submits commands, control issues an internal one-use Runtime Permit for the exact accepted phase, and runtime consumes it before mutation; the host Git adapter executes Admission requests without owning either decision. This shape keeps callers from reproducing safety-critical ordering while preserving focused interfaces for the owned loops and admission mechanism.

A single `execute/read` kernel was rejected because it would concentrate unrelated change and obscure long-running interruption semantics. Publishing every domain helper was rejected because callers could bypass ordering and create shallow modules. A `propose/continue/inspect` journey facade was rejected because its continuation token could become a generic command bus or leak mutation authority into read projections. The consequence is intentional asymmetry: CLI composes control and runtime through a non-exported, non-reusable Permit; Web/static delivery receives queries only; and tests exercise both the three Run interfaces and each internal deep module interface. Doctor/configuration/credential/UI-server bootstrap flows remain auxiliary and cannot dispatch Run, model, or repository effects.
