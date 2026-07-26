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
The immutable agreement for one unit of work: its dependencies, allowed write set, supplied context, and required checks.
_Avoid_: Prompt, ticket, task description

**Plan Revision**:
An immutable, human-approved version of a bounded Task Contract DAG for one Crew Run.
_Avoid_: Mutable plan, Coordinator scratch plan

**Policy Revision**:
An immutable, human-approved version of the rules that allow, deny, or require approval for actions and integration.
_Avoid_: Plan Revision, mutable policy

**Workspace Lease**:
A time-bounded claim that authorizes one Worker attempt to modify a declared write set.
_Avoid_: File ownership, permanent lock

**Context Capsule**:
A bounded, provenance-bearing handoff containing only the goal, constraints, decisions, and repository facts needed for a Task Contract.
_Avoid_: Chat history, memory dump, summary

**Run Head**:
The current immutable revision of a Crew Run's private branch, used as the expected parent for the next Task Candidate.
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

**Freshness Assessment**:
A gate-time judgment of whether an immutable Context Capsule, Evidence Receipt, Task Candidate, or Run Candidate still applies to the current head, revisions, dependency graph, and checks.
_Avoid_: Receipt mutation, cache timestamp

**Stale**:
An artifact's negative Freshness Assessment or a Worker Attempt's terminal outcome after its inputs become invalid; neither may authorize further work or integration.
_Avoid_: Failed, old

**Task Candidate**:
A Worker Attempt's change prepared and verified against the current Run Head, eligible for promotion only to the Crew Run's private branch.
_Avoid_: Integration Candidate, completed task

**Run Candidate**:
The complete frozen Crew Run revision whose run-wide Evidence Bundle is fresh and which awaits final human approval for exact integration.
_Avoid_: Integration Candidate, pull request

**Approval Grant**:
A single-use human authorization bound to one frozen risky action or final integration and to the exact applicable run, target, evidence, and policy revisions.
_Avoid_: Confirmation, blanket permission

**Grant Validation**:
A gate-time judgment that an immutable Approval Grant exactly matches the pending action or integration and remains unexpired and unused.
_Avoid_: Freshness Assessment, stale approval
