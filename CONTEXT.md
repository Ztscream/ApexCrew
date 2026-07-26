# ApexCrew

ApexCrew is a local-first Coding Agent Harness that coordinates bounded code changes and admits them for integration only when their context, approval, and verification evidence are current.

## Language

**Crew Run**:
A durable execution of one developer goal across one repository and a bounded set of Workers.
_Avoid_: Job, workflow, project

**Coordinator**:
The ApexCrew-owned decision maker that advances a Crew Run, assigns ready work, and serializes integration.
_Avoid_: Manager agent, orchestrator LLM

**Worker**:
A logical coding participant whose decision loop is owned by ApexCrew and whose changes occur in an isolated workspace.
_Avoid_: External CLI agent, role-play agent

**Task Contract**:
The immutable agreement for one unit of work: its execution dependencies, read/dependency/write/check-input scopes, supplied constraints, and required checks.
_Avoid_: Prompt, ticket, task description

**Plan Revision**:
An immutable, human-approved version of a bounded Task execution DAG, promotion-hazard graph, and Run Check Set for one Crew Run.
_Avoid_: Mutable plan, Coordinator scratch plan

**Promotion Hazard**:
An ordering relation that prevents a Task Candidate from promoting before another Task that may still write one of its declared inputs; it constrains promotion without necessarily blocking speculative execution.
_Avoid_: Task dependency, merge conflict

**Policy Revision**:
An immutable, human-approved version of planning-read authority, Secret Path Set identity, and the rules that allow, deny, or require approval for actions and integration.
_Avoid_: Plan Revision, mutable policy

**Secret Path Set**:
The effective union of non-removable default secret-path denials and operator-added private path rules whose identity is bound to a Policy Revision without disclosing those rules.
_Avoid_: Known-secret list, repository ignore file

**Planning Read Authorization**:
The Policy-bound path scope and disclosure caps under which the Coordinator may inspect a pinned tracked snapshot before a Plan exists.
_Avoid_: Repository access, default full-repository context

**Budget Revision**:
An immutable, human-approved version of the hard resource ceilings and objective allocation rules for one Crew Run.
_Avoid_: Usage counter, model estimate

**Model Configuration Revision**:
An immutable, human-approved record of the provider, requested and acceptable returned-model identities, model settings, and typed-action contract used for planning or a Worker Attempt; it never contains credentials.
_Avoid_: API key, provider session

**Model Request Intent**:
A durable pre-dispatch record that binds one provider attempt to its request identity and reserved budget before any external request occurs.
_Avoid_: Prompt log, provider session

**Workspace Lease**:
A time-bounded claim that authorizes one Worker Attempt to modify a declared write set while intervening Run Head changes remain outside its sensitivity scope.
_Avoid_: File ownership, permanent lock

**Context Capsule**:
A bounded, provenance-bearing handoff containing only the goal, constraints, decisions, and repository facts needed for a Task Contract.
_Avoid_: Chat history, memory dump, summary

**Run Head**:
The immutable commit currently named by a Crew Run's private `refs/apexcrew/runs/<run-id>` ref, used as the expected parent for the next Task Candidate.
_Avoid_: Main, integration branch

**Worker Attempt**:
One bounded execution of a Task Contract by a Worker against a specific Run Head under one Workspace Lease.
_Avoid_: Worker session, retry

**Verification Snapshot**:
An immutable prospective repository revision prepared from an expected parent and used as the subject of objective checks.
_Avoid_: Worker branch, current workspace, patch

**Evidence Receipt**:
An immutable record of one objective check, bound to its Verification Snapshot, check definition, and observed result.
_Avoid_: Test log, confidence score

**Evidence Bundle**:
The complete set of fresh Evidence Receipts required by the current Task or Run gate.
_Avoid_: Agent report, review comment

**Run Check Set**:
The exact human-approved set of objective checks that the frozen Run Candidate must pass, independent of any Task's focused checks.
_Avoid_: Task-check union, discovered CI jobs

**Execution Fingerprint**:
The immutable identity of the check definition and execution environment to which an Evidence Receipt applies.
_Avoid_: Machine name, mutable environment snapshot

**Freshness Assessment**:
A gate-time judgment of whether an immutable Context Capsule, Evidence Receipt, Task Candidate, or Run Candidate still applies to the current head, revisions, dependency graph, and checks.
_Avoid_: Receipt mutation, cache timestamp

**Stale**:
An artifact's negative Freshness Assessment or a Worker Attempt's terminal outcome after its inputs become invalid; neither may authorize further work or integration.
_Avoid_: Failed, old

**Task Candidate**:
A Worker Attempt's change prepared and verified against the current Run Head, eligible for promotion only by CAS to the Crew Run's private `refs/apexcrew/runs/<run-id>` ref.
_Avoid_: Integration Candidate, completed task

**Run Candidate**:
The complete frozen Crew Run revision whose run-wide Evidence Bundle is fresh and which awaits final human approval for exact integration.
_Avoid_: Integration Candidate, pull request

**Approval Grant**:
A single-use human authorization bound to one frozen risky action, final integration, or Purge Manifest and its exact applicable state; after consumption it can settle only its already-journaled intent.
_Avoid_: Confirmation, blanket permission

**Grant Validation**:
A gate-time judgment that an Approval Grant is unexpired/unused for the exact new intent, or is already consumed by the exact journaled intent being settled; no other reuse qualifies.
_Avoid_: Freshness Assessment, stale approval

**Audit Ledger**:
The authoritative, allowlisted chronological record from which a Crew Run's inspectable state and sanitized exports are projected.
_Avoid_: Debug log, transcript

**Restricted Transcript**:
Redacted local diagnostic material that may explain model or tool behavior but can never authorize admission or enter a public export.
_Avoid_: Evidence Receipt, Audit Ledger

**Target Reservation**:
A Run-owned branch-occupancy claim that keeps the pinned target out of user worktrees until terminal administrative cleanup.
_Avoid_: Execution worktree, target lock

**Runtime Permit**:
A one-use internal authority that lets one exact accepted command start its matching runtime phase; command replay cannot recreate it after consumption.
_Avoid_: Continuation token, idempotency key, session

**Purge Manifest**:
A frozen inventory of one terminal Crew Run's removable ApexCrew state, bound to its exact terminal Audit position before destructive retention cleanup is approved.
_Avoid_: Delete request, retention policy

**Purge Tombstone**:
The minimal durable proof that an exact Purge Manifest is pending or complete, retained after the Crew Run's removable state is gone so recovery and idempotency remain authoritative.
_Avoid_: Audit Ledger, deleted Run
