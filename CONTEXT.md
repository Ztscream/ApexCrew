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

**Workspace Lease**:
A time-bounded claim that authorizes one Worker attempt to modify a declared write set.
_Avoid_: File ownership, permanent lock

**Context Capsule**:
A bounded, provenance-bearing handoff containing only the goal, constraints, decisions, and repository facts needed for a Task Contract.
_Avoid_: Chat history, memory dump, summary

**Evidence Receipt**:
An immutable record of one objective check, bound to the exact repository revision and command that produced it.
_Avoid_: Test log, confidence score

**Evidence Bundle**:
The complete set of fresh Evidence Receipts required by a Task Contract before handoff or integration.
_Avoid_: Agent report, review comment

**Stale**:
The state of a Context Capsule or Evidence Receipt whose referenced revision, dependency, contract, or policy has changed.
_Avoid_: Failed, old

**Integration Candidate**:
A Worker change whose current Task Contract, lease history, approvals, and Evidence Bundle satisfy the integration gate.
_Avoid_: Completed task, pull request

**Approval Grant**:
A single-use human authorization bound to one frozen risky action, Crew Run revision, and policy revision.
_Avoid_: Confirmation, blanket permission
