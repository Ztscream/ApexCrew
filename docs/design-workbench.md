# Design Workbench

## Status: STUB

The design workbench is a planning document, not an execution feature. A future
implementation may collect a goal, constraints, a candidate graph, evidence
requirements, and proposed UI states.

It does not issue a Runtime Permit, Grant, typed CAS request, model call, Git
command, Docker command, credential lookup, or repository mutation. Any future
implementation must route approved actions through `CrewControl` and preserve
the read-only `RunQueries` WebUI boundary.

## Open Design Questions

- How should candidate dependencies and freshness hazards be visualized?
- Which Tier 1 fields are useful for comparing replay frames?
- Which human review checkpoints need explicit confirmation codes?
