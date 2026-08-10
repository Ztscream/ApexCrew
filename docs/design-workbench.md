# Design Workbench

## Status: READ-ONLY ARTIFACT

This workbench is a reviewable planning artifact for a local ApexCrew Run. It
describes intent and evidence without becoming a second command surface.

## Goal

Capture one bounded developer goal, the constraints that must remain true, and
the acceptance conditions that make completion observable. A goal record names
the repository identity, target branch and pinned base OID, but never contains a
credential or unrestricted repository content.

## Constraints

- The Run is local-first and uses one pinned repository instance.
- Planning is read-only and all model actions are typed and budgeted.
- Risky actions require human approval, an exact one-use Grant, and Admission.
- Integration is a typed CAS operation against the exact target OID; push is
  outside ApexCrew authority.
- The WebUI and static replay read only sanitized `RunQueries` projections.

## Candidate Graph

Each candidate is represented by a stable ID, parent candidate IDs, changed-path
scope, dependency IDs, freshness binding, required checks, evidence state, and
promotion state. The graph is valid only when it is acyclic and every parent is
known. A changed dependency or revision marks the affected candidate stale;
there is no implicit promotion from a stale node.

The review view shows four lanes: proposed, checked, evidence-complete, and
frozen-for-integration. A candidate moves between lanes only through the normal
Coordinator, WorkerLoop, Admission, and Authority transitions.

## Evidence Requirements

Every candidate lists its required checks, snapshot digest, check receipts,
applicable Policy/Budget/Model revision digests, freshness assessment, and
confirmation code. Missing, stale, failed, or unobservable evidence is shown as
blocked and cannot satisfy an Evidence Bundle. Evidence previews are bounded,
redacted, and excluded from Tier 2/quarantined content.

## UI State Catalogue

| State | Meaning | Allowed next review action |
| --- | --- | --- |
| `DRAFT` | Goal or bootstrap revisions are still being prepared | inspect or propose exact revisions |
| `AWAITING_PLAN_APPROVAL` | A typed plan is waiting for human approval | inspect plan or reject exact plan |
| `READY_TO_START` | Approved plan is ready for the start gate | inspect bindings or start exact Run |
| `ACTIVE` | Workers may proceed under current leases and budgets | inspect evidence, approve a Grant, or pause |
| `INDETERMINATE` | An external effect cannot be safely classified | inspect observations and submit exact resolution |
| `AWAITING_FINAL_APPROVAL` | Candidate/evidence is waiting for final approval | inspect evidence or reject exact candidate |
| `COMPLETED` | Typed integration has settled | inspect or prepare terminal cleanup/purge |

## Authority Boundary

The workbench does not issue Runtime Permit, Grant, typed CAS request, model call,
Git command, Docker command, credential lookup, or repository mutation. It does
not parse model output or infer approval. Any accepted action must be submitted
through `CrewControl`; any runtime transition must be performed by
`CrewRuntime`; reads must come from `RunQueries`. This document is therefore
safe to render beside the sanitized read-only WebUI.
