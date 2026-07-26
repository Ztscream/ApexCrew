# ApexCrew Agent Log

This chronological log records material agent work and human decisions. Future implementation entries must include the `PLAN.md` task, Superpowers skill, red/green evidence, commit or PR, and any manual correction. Never record credentials, private prompt text, or secret-bearing command output.

## 2026-07-25 / DISCOVERY-001

- **Skills**: `research`, `domain-modeling`.
- **Human context**: the user selected A-class Coding Agent Harness, named it ApexCrew, proposed repository-bounded action, HITL for dangerous operations, test-feedback correction, multi-agent cooperation, long context, and continuous cowork. Provider choice is deferred.
- **Agent work**: read both course requirement files; inspected the docs-only workspace and local toolchain; delegated primary-source GitHub landscape research and an independent scope review.
- **Finding**: generic multi-agent/worktree/persistence/evidence orchestration overlaps OpenHands, Vibe Kanban, Gas Town, h5i, Bernstein, Codex, and Ruflo. This invalidated novelty claims for individual durability features, not the user's authority to select the product direction.
- **Decision proposed, not accepted**: research suggested adversarial acceptance as an alternative mainline. It remained advisory.
- **Human intervention**: none during the research pass. User sign-off remained required before formal `SPEC.md` brainstorming.
- **Artifacts**: `docs/research/github-agent-landscape.md`, `INITIALIZATION.md`, `CONTEXT.md`, `docs/learning/README.md`, and revised `AGENTS.md`.
- **Lesson**: feature combinations that sounded distinctive were already implemented by direct competitors. Research must precede scope freeze, and provenance must not be confused with semantic correctness.

## 2026-07-26 / INIT-002 - Product direction accepted

- **Skills**: no Superpowers workflow was invoked; this was a human product decision informed by the completed research agents.
- **Key context**: choose one mainline, cap v1 at three Workers, use Python + TypeScript fixtures, and keep the assessed loop independent of host Coding Agent CLIs.
- **Human decision**: accepted **Evidence-Driven Durable Crew** as the only mainline; limited v1 to one repository and at most three Workers; selected Python + TypeScript fixtures and a public personal GitHub repository; prioritized backend and Agent-engineering interview value.
- **Core constraint**: real providers use low-level model APIs. Codex, Claude Code, Gemini CLI, and high-level agent frameworks cannot replace the self-built WorkerLoop.
- **Correction**: adversarial acceptance and weak-oracle challenge became supporting experiments only. The research report was marked advisory and its conflicting Codex-backend recommendation was removed.
- **Artifacts**: `docs/adr/0001-evidence-driven-durable-crew.md`, the decision callout in `docs/research/github-agent-landscape.md`, and reconciled initialization documents.
- **Lesson**: research may challenge novelty and refine claims, but it does not silently supersede an explicit human product decision.

## 2026-07-26 / INIT-003 - Superpowers setup

- **Skills**: plugin installation and filesystem/configuration verification; no newly installed Superpowers workflow was claimed in the installation session.
- **Key context**: the course requires the seven-step Superpowers workflow, so the complete official plugin must be present before formal brainstorming.
- **Installation target**: `superpowers@openai-curated` through the Codex plugin registry.
- **Verification**: enabled in Codex configuration; manifest version `5.1.3`; cache revision `11c74d6b`; required workflow skill directories are present.
- **Agent output**: installed plugin cache and enabled configuration outside the repository; package/version evidence is recorded here and in `INITIALIZATION.md`.
- **Human intervention**: the user authorized completing initialization; no credential or provider choice was required.
- **Operational note**: Stage 2 starts in a task/session where the newly installed plugin skills are loaded. No workflow step is claimed before it is actually run.
- **Lesson**: installation, activation, and actual workflow use are separate facts and must not be conflated.

## 2026-07-26 / INIT-004 - Local governance baseline

- **Skill**: `domain-modeling`, used to keep `CONTEXT.md` glossary-only and record the accepted direction in ADR-0001.
- **Key context**: finish a documentation-only initialization; do not create `SPEC.md`, `PLAN.md`, fixtures, source, tests, or CI before their gates.
- **Agent work**: reconciled product status, repository guidance, architecture boundaries, experiments, process records, and security-oriented ignore rules.
- **Environment verification**: Git author identity is configured; Docker daemon 29.6.1 responds; GitHub CLI is absent but optional.
- **Artifacts**: `README.md`, `SPEC_PROCESS.md`, `CONTEXT.md`, `INITIALIZATION.md`, repository policies, ADR-0001, system overview, experiment plan, research, and learning index.
- **Human intervention still needed**: GitHub/NJU remote details, course schedule, final WebUI constraint, and license choice.
- **Implementation state**: none. `SPEC.md`, `PLAN.md`, source, tests, and CI are deliberately absent.
- **Lesson**: an initialization baseline should make unknowns and gates executable for the next task rather than filling them with guessed decisions.

## 2026-07-26 / INIT-005 - Verification and baseline commit

- **Skills**: `domain-modeling` consistency review plus verification-before-completion discipline; the Superpowers skill itself was installed but not falsely claimed as loaded in this session.
- **Key context**: accept only a repository state that preserves the sole mainline, contains no implementation, leaks no likely credential, and has valid local document links.
- **Agent output**: maintained Markdown files passed local-link checks; all Markdown fences close; `AGENTS.md` is within its required 200-400-word range; common secret patterns and machine-local paths were absent; forbidden implementation paths were absent; all seven required Superpowers skill directories were verified; `git diff --cached --check` passed.
- **Independent review**: a fresh read-only subagent confirmed the mainline/scope/loop constraints and identified missing process-log fields, a cold-start gate deadlock, late provider selection, an incomplete Stage 2 checklist, and executable-looking archived research. All were corrected before commit.
- **Human intervention**: none during verification. GitHub/NJU remotes and license remain external follow-ups.
- **Commit**: `chore: initialize ApexCrew project governance`; the immutable hash is read from Git history because a commit cannot contain its own final hash.
- **Lesson**: process documents need the same evidence loop as code: audit found an omission, the log was corrected, and checks are rerun before completion.

## 2026-07-26 / GOAL-001 - GitHub publication and durable objective

- **Skill**: `domain-modeling`, used to record the hard-to-reverse public-license choice in ADR-0002 without adding implementation details to `CONTEXT.md`.
- **Key context**: publish through the user's personal repository, defer NJU/GitLab, choose a license autonomously, and establish a goal that persists through v0.1 delivery.
- **Human decision**: `https://github.com/Ztscream/ApexCrew.git` is the public repository; NJU/GitLab is out of current scope; license selection is delegated to the agent.
- **Agent output**: configured `origin`, verified the remote contained no refs, selected Apache-2.0 for its permissive terms and explicit patent grant, added `NOTICE` to exclude course-provided documents, and created the long-running v0.1 goal.
- **Safety**: publication uses an ordinary non-force push only after the committed tree passes link, secret, license-integrity, and Git checks.
- **Commit**: `chore: configure GitHub publication and licensing`.
- **Lesson**: a repository-level license must distinguish original project work from bundled reference material the project owner cannot relicense.

## 2026-07-26 / SPEC-R1 - Problem and scenario approval

- **Skill**: Superpowers `brainstorming`, loaded from the installed official plugin; its visual companion was accepted and initialized for later diagram/layout questions.
- **Key context**: define one primary user journey, failure, completion boundary, decomposition owner, and falsifiable comparison before discussing mechanisms.
- **Human decisions**: selected option A for all five questions and explicitly approved Round 1: one developer delegates one hours-long cross-module task; stale integration is the main failure; success ends at a human-approved Integration Candidate (the provisional term later split into Task Candidate and Run Candidate); the Coordinator proposes a bounded DAG; freshness-disabled ablation is the primary baseline.
- **Agent output**: recorded the full question/decision/consequence table and approved conclusion in `SPEC_PROCESS.md`; added `.superpowers/` to `.gitignore` so visual session state remains local.
- **Implementation state**: none. The Superpowers design hard gate remains active.
- **Commit**: `docs: approve brainstorming round 1`.
- **Lesson**: the main contribution is strongest when evaluated by a controlled ablation, not by broad claims that multi-agent execution is generally better.

## 2026-07-26 / SPEC-R2 - Mechanism and state approval

- **Skills**: Superpowers `brainstorming` with its accepted visual companion; `domain-modeling` for the approved vocabulary correction.
- **Key context**: turn the Round 1 freshness claim into a self-owned WorkerLoop, invalidation algorithm, evidence boundary, recoverable state model, and cross-language falsification fixtures without creating implementation.
- **Human decisions**: selected declared dependencies with conservative fallback; prepared-snapshot verification; one typed action per model turn; reconciliation-based recovery; private Run Branch promotion; action-boundary stale termination; and immutable Plan Revisions with bounded automatic correction. The user then explicitly approved the consolidated Round 2 design.
- **Independent review**: three parallel reviewers challenged integration identity, crash windows, state terminology, and fixture falsifiability. Their accepted corrections split Task Candidate from Run Candidate, separated immutable Evidence Receipts from Freshness Assessments, required a final run-wide gate, and bound integration to prepared commit and expected target OIDs.
- **Fixtures approved**: Python changes money units from cents to decimal dollars; TypeScript changes timestamps from milliseconds to seconds. Both preserve text-level mergeability while making old green evidence semantically invalid.
- **Rejected**: worker-tip receipts, merge-then-test, dynamic-only dependency inference, multi-action/free-form Worker execution, unconditional replay, hard process interruption, per-check human approval, and silent DAG/contract mutation.
- **Implementation state**: none. Round 3, consolidated `SPEC.md`, `writing-plans`, and independent cold-start review remain mandatory gates.
- **Commit**: `docs: approve brainstorming round 2`.
- **Lesson**: evidence is immutable history; current authority is a separate judgment over the exact prospective revision, dependency graph, contract, policy, and checks.

## 2026-07-26 / SPEC-R2A - Post-commit specification correction

- **Trigger**: an independent review of `e70b3e3..bdfc56c` found that Round 2 was recorded without the required `SPEC.md` diff or legal lifecycle/data-model definitions. It also found mixed freshness/grant terminology, Plan/Policy version coupling, ambiguous private promotion wording, and a stale README fixture TODO.
- **Correction**: created a not-implementation-ready `SPEC.md` containing only approved Round 1/2 behavior plus explicit Round 3 gaps; defined legal Run, Task, Attempt, Candidate, and lease transitions and logical entity relationships; separated Policy Revision and Grant Validation; clarified that automatic promotion touches only the private Run Branch.
- **Implementation state**: none. This correction closes documentation-process gaps and does not authorize source, tests, fixtures, or planning.
- **Commit**: `docs: add approved round 2 spec sections`.
- **Lesson**: approval of choices is not approval of an underspecified artifact. Each round must leave an inspectable normative diff, and independent review must check the process contract as well as prose consistency.

## 2026-07-26 / SPEC-R3 - Operations and acceptance approval

- **Skills**: Superpowers `brainstorming` and visual companion for consolidated architecture review; `openai-docs` for current model/API/pricing facts; `codebase-design` for deep-module seams; `domain-modeling` for canonical Round 3 language and qualifying ADRs.
- **Prompt/context**: the agent received the two course requirement files, approved Round 1/2 records, the A/B/C1 decision sequence, the documentation-only gate, and a request to consolidate all thirteen operations choices without creating implementation. The user reviewed the rendered Round 3 page rather than approving an unseen prose rewrite.
- **Human decisions**: trusted host control plane plus restricted Docker command executor; OpenAI Responses API with `gpt-5.6-terra`; keyring credentials with headless/CI environment injection; adaptive C1 budgets; CLI-only mutations and read-only WebUI; Open Design as a design-stage tool; two-tier observability; untrusted repository/Worker threat model; GitHub Pages fixture replay; Windows 11 and Ubuntu 24.04 x86_64; approved Run Check Set; balanced quantitative acceptance thresholds.
- **Schedule**: deadline supplied as 2026-08-10 and interpreted as 23:59 Asia/Shanghai; 25 hours/week yields approximately 53 hours. Optional weak-oracle, extra providers/platforms, writable WebUI, and hosted execution are cut before required core or course work.
- **Independent research**: a read-only Open Design agent confirmed that upstream is an application/workbench rather than a stable UI library. The accepted workflow pins `open-design-v0.16.1` at commit `276b4d8e970bc143d7ad060181a89a834e3d9caf`, authors the custom ApexCrew Operational system through `design-md`, uses disposable `dashboard` and `design-review` artifacts, keeps the design contract original, and excludes Open Design packages/daemon/generated source from runtime and CI.
- **Subagent evidence**: the Open Design review concluded, "Open Design is more suitable as ApexCrew's development-time design workbench; it should not become a runtime dependency," and identified its private workspace packages and fast-moving `0.x` surface as the reason to transfer only reviewed information architecture and original tokens.
- **Conservative defaults approved**: token-protected loopback UI; Tier 1 retained until explicit purge and Tier 2 retained 30 days/1 GiB; regular-file actions/checks allowed, protected mutations revision-bound and one-use approved, escape/shell/network/socket/push/destructive Git hard-denied; evidence bound to an exact Execution Fingerprint and model actions to immutable provenance.
- **Agent output**: replaced every Round 3 gap in `SPEC.md` with normative operations, security, delivery, non-functional, schedule, risk, and acceptance clauses; reconciled process, glossary, architecture, initialization, README, contributor guidance, and ADRs.
- **Implementation state at Round 3 close**: none. `PLAN.md` was gated on architecture comparison, independent spec review, and final written-spec sign-off; source, fixtures, tests, and CI additionally required planning and cold-start review.
- **Human approval**: the user explicitly approved the complete Round 3 review page. This approves the design round and documentation update, not implementation or push.
- **Lesson**: durability is only credible when operational limits, threat assumptions, evidence environment, redaction, public-demo authority, and delivery time are as explicit and testable as the state machine.

## 2026-07-26 / SPEC-R3A - Independent Round 3 correction

- **Skills and context**: `code-review` ran independent Standards and Spec axes over `git diff --cached HEAD` from fixed point `fa780b7`; standards were `AGENTS.md`, both course requirement files, and `SPEC_PROCESS.md`, while the approved Round 3 record was the spec source.
- **Subagent evidence**: Standards reported a deadlock between retaining `PLAN.md` and using it for cold-start review; Spec reported that Tier 2 could exceed 1 GiB during an active Run and that the public replay lacked objective behavior.
- **Corrections**: made `PLAN.md` the allowed post-sign-off review input; added candid process reflection and technical-choice rationale; bounded every Tier 2 payload and repository total; specified replay controls and acceptance; restored the inclusive ten-minute CI bound and major-change design review; clarified bootstrap/no-progress pauses, credential injection risk, check timeouts, and sanitized disposable executor snapshots.
- **Human intervention**: none in this correction pass. It applies the already approved Round 3 choices and course rules; it does not add a product decision, implementation authority, or push approval.
- **Implementation state**: none. The correction changes documentation and review evidence only; no source, fixture, test, CI, or `PLAN.md` was created.
- **Lesson**: a high-level safety choice is incomplete until retention overflow, malicious check writes, secret-bearing untracked files, retry accounting, and user-visible replay behavior have explicit failure-closed outcomes.

## 2026-07-26 / SPEC-ARCH - A-Hybrid architecture approval

- **Skills and context**: `codebase-design` Design It Twice compared module interfaces after Round 3; `domain-modeling` kept implementation names out of the domain glossary and recorded the qualifying decision in ADR-0006. Two design agents ran in parallel; the journey-facade brief ran as a read-only follow-up after the agent-thread limit rejected a third concurrent spawn. Each pass used `SPEC.md`, `CONTEXT.md`, `INITIALIZATION.md`, and the system overview.
- **Subagent evidence**: the minimal kernel offered `execute/read` but warned of a swollen kernel and blocking interruption semantics; the flexible dual reactor improved locality but exposed many ordering-sensitive interfaces; the journey facade simplified CLI use but warned its continuation token could become a generic command bus.
- **Agent recommendation**: combine the minimal external surface with internal dual-reactor locality: `CrewControl.handle`, `CrewRuntime.run_until_blocked`, and `RunQueries.get`, backed by internal Coordinator, WorkerLoop, Admission, Authority, EffectJournal/recovery, tools, and projection modules.
- **Human decision**: the user explicitly approved **A-Hybrid**. This selects implementation module ownership and interface placement; it does not approve the complete written SPEC, create `PLAN.md`, authorize implementation, or authorize push.
- **Output**: updated the normative module contracts, architecture maps, initialization/process status, contributor guidance, README, learning-note index, and ADR-0006. `CONTEXT.md` remains unchanged because the new names are implementation interfaces rather than domain language.
- **Implementation state**: none. No package directory, fixture, test, CI, or `PLAN.md` was created.
- **Lesson**: the strongest shape is not the smallest interface alone; depth also requires local internal ownership so freshness/admission and recovery changes do not turn one kernel into a maintenance hotspot.

## 2026-07-26 / SPEC-ARCH-R1 - Whole-spec cold-review correction

- **Trigger**: after A-Hybrid approval, a fresh implementer/state-safety read of the complete SPEC found nine blocking ambiguities that the earlier architecture-diff review did not expose.
- **Skills and agents**: `codebase-design` preserved the approved deep-module seams; `domain-modeling` synchronized changed meanings in `CONTEXT.md`; read-only lifecycle/durability and concurrency/authority agents independently specified failure cases and deterministic guards.
- **Blocking evidence**: bootstrap planning had no executable owner/sequence; provider calls preceded durable intent; Plan approval bypassed Policy/Budget/Model Configuration start guards; future writers could stale already-promoted work; consumed Grants, old-head leases, unscoped reads, moved targets, and human uncertainty resolution had contradictory or open semantics.
- **Correction**: added a bounded Coordinator planning protocol, explicit `PLANNING`/`READY_TO_START` gates, durable model request reservations, promotion-hazard validation, scoped `R`/`D`/`W`/`Q` access, classified-head lease admissibility, pinned-base behavior, exact-intent Grant settlement, and closed objective `INDETERMINATE` choices. Candidate CAS remains Admission-owned; bootstrap CLI flows have no Run/model/repository effect authority.
- **Implementation state**: none. The changes are documentation only; `PLAN.md`, source, fixtures, tests, and CI remain prohibited.
- **Review state**: correction prepared and awaiting a new whole-SPEC cold read. A-Hybrid approval remains valid, but this entry is not final written-spec approval or push authorization.
- **Lesson**: interface shape can pass a diff review while lifecycle ordering remains impossible; every external effect and pre-activation phase needs the same durable authority and failure semantics as the main Worker loop.

## 2026-07-26 / SPEC-ARCH-R2 - Authority and host-safety closure

- **Trigger**: continued whole-document implementer, safety, and course reads found that the first correction still allowed divergent implementations around CAS ownership, released leases, invalid Grants, approval-wait races, active Policy changes, returned model IDs, symlinks/secrets, purge recovery, checked-out target branches, and repository-controlled host Git execution.
- **Skills and agents**: `codebase-design` kept A-Hybrid at three Run-facing interfaces; `domain-modeling` synchronized only resolved domain terms; three read-only reviewer roles supplied lifecycle, host-security, and course-process counterexamples.
- **Correction**: Coordinator now schedules while Admission exclusively validates/prepares/issues CAS and a sanitized Git adapter executes it. Plan/Policy freeze at `ACTIVE`; Candidate promotion relies on proven lease provenance; bad Grants do not destroy fresh Candidates; pause/cancel and Grant-delivery crashes have ordered outcomes; returned-model IDs are allowlisted before loop release.
- **Security closure**: v0.1 denies all symlinks, binds fixed plus host-local secret rules without disclosure, rejects checked-out targets and unsafe/external Git storage, disables hooks/filters/config/network paths, defines tombstone-backed terminal purge, and requires tracked-tree plus full-reachable-history secret scanning with planted-secret negatives.
- **Process correction**: cold-start review is mandatory for a different agent type in a fresh session using only SPEC/PLAN, with 1-2 disposable tasks for about 1-2 hours and a required pause on ambiguity.
- **Implementation/review state**: documentation only; no `PLAN.md`, source, fixture, test, or CI exists. A new independent whole-document review must still find zero blockers before final written-spec approval is requested.
- **Lesson**: a typed adapter is not a containment guarantee until configuration, hooks, storage indirection, model routing, lifecycle races, and destructive-retention recovery all have explicit fail-closed contracts.

## 2026-07-26 / SPEC-ARCH-R3 - Runtime authority and reservation cleanup closure

- **Trigger**: continued fresh implementer, security, and course-document reads found that the second correction still allowed direct/old-command runtime replay, incomplete planning and multi-intent transitions, undefined known-CAS failure, ambiguous post-purge queries, and a Target Reservation that could not be safely removed or recovered.
- **Skills and agents**: `domain-modeling` captured Target Reservation and Runtime Permit language; `codebase-design` kept the approved three-interface Run surface; three read-only reviewers independently exercised lifecycle, Git/security, and course/process counterexamples.
- **Runtime correction**: accepted runtime-driving commands now issue one persisted one-use Runtime Permit, consumed with ownership before reconciliation or Coordinator work. Old begin/start/resume/Grant/integration replays cannot restart progress; only a new exact `continue` closes a genuine orphaned phase. State tables now cover planning stops, multi-intent barriers, and private/target CAS known-failure successors.
- **Git correction**: one reservation identity/ownership row is persisted before Git and reused across `DRAFT` retries. v0.1 rejects pre-existing linked worktrees and config includes before Git, never follows admin-record paths, and closes terminal cleanup with exact unlock, revalidation, one intent-bound forced remove, and explicit mixed-component crash recovery.
- **Retention correction**: purge waits for administrative cleanup, retains only its confirmation receipt/tombstone, returns deterministic outcomes for old request IDs, and exposes only minimal tombstone query state after Audit history is removed.
- **Implementation state**: none. No `PLAN.md`, source, fixture, test, or CI artifact was created; all work remains documentation-only.
- **Review state**: corrections are written and a new whole-document cold read is in progress. This entry is not final written approval or push authorization.
- **Lesson**: durable commands require separate delivery authority, and a safety guard is incomplete until both its normal teardown and every partial teardown state are executable without trusting repository-controlled routing.

## 2026-07-26 / SPEC-ARCH-R4 - Independent specification review passed

- **Review subject**: the complete `SPEC.md` frozen at SHA-256 `9751BEA572112994034AEA2AB265CFE6AD54195CFB513C18C587AB35AA2F3EB4`, together with its process, glossary, initialization, architecture, learning, and contributor companions.
- **Independent evidence**: a fresh implementer/state-machine cold read, a course/companion-document cold read, and a security/Git-containment cold read each reported `ZERO BLOCKERS`.
- **Closeout**: synchronized only review status, exact private-ref/Attempt-workspace terminology, and the documented rejection of unsupported linked-worktree, sparse/split, graft/shallow/partial, alternate, and externally routed Git layouts.
- **Implementation state**: none. No `PLAN.md`, source, fixture, test, build, or CI artifact exists; final written-spec approval, planning, and the separate implementation cold-start review still gate persistent implementation.
- **Authority state**: this review result is not final written-spec approval, implementation authorization, commit publication, or push authorization.
- **Lesson**: a frozen review result must identify both the artifact digest and reviewer perspective, while process-status edits must remain visibly separate from product approval.
