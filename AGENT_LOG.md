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

## 2026-07-27 / SPEC-SIGNOFF - Final written specification approval

- **Human decision**: in Codex task `019f99cb-b307-75a3-8059-6e12908ff4b8`, the repository owner stated `批准最终 SPEC.md，哈希 2F1434AB29C3B7205B13CA96FE35D18C7666729F633EDF984F1DFCA54F0663BC`.
- **Approved artifact**: root `SPEC.md`, SHA-256 `2F1434AB29C3B7205B13CA96FE35D18C7666729F633EDF984F1DFCA54F0663BC`, 128,418 bytes, source commit `aabdd4fb664eb82dcc34c41391dbdb48872254c8`, Git blob `9d32355e610e08a75bf89a3a0fc23785441cda24`.
- **Review basis**: implementer/state-machine, course/companion-document, and security/Git-containment reviewers each reconfirmed `ZERO BLOCKERS` on the final digest and closeout delta.
- **Integrity rule**: `SPEC.md` was rehashed unchanged while the worktree was clean. Its embedded pre-approval status is superseded by the external sign-off record; any byte change requires a new digest and explicit approval.
- **Scope**: Stage 2 is complete. The approval authorizes `writing-plans` to create `PLAN.md`; it does not authorize persistent implementation, credentials, publication, push, release, or a Runtime Grant.
- **Planning state**: `writing-plans` 5.1.3 is installed at cache revision `11c74d6b` but is absent from this session's available-skills catalog, so no `PLAN.md` was created. Planning must resume in a session where the plugin skill is loaded.
- **Recorder**: Codex at 2026-07-27T09:26:27+08:00. SHA-256 identifies content but does not itself prove approver identity.

## 2026-07-27 / PLAN-R1 - Stage 3 implementation plan and document review

- **Skill and authority**: pinned personal `writing-plans` at upstream commit `f2cbfbefebbfef77321e4c9abc9e949826bea9d7`, normalized equal to repository-recorded Superpowers 5.1.3 cache revision `11c74d6b`; frozen approved `SPEC.md` SHA-256 `2F1434AB29C3B7205B13CA96FE35D18C7666729F633EDF984F1DFCA54F0663BC`.
- **Output**: created only root `PLAN.md` plus planning/process records. The plan covers the approved A-Hybrid interfaces, ApexCrew-owned Coordinator/WorkerLoop, low-level ModelPort and offline `ScriptedMockLLM`, SQLite recovery, typed admission/authority/tools, hostile Git/Docker containment, both fixtures, CLI/read-only WebUI, CI/public replay, evaluation, and delivery gates.
- **Schedule/cut line**: retained 2026-08-10 23:59 Asia/Shanghai, 25 hours/week, and approximately 53 hours; required core, safety, deterministic fixtures, delivery, public replay, and course evidence precede optional experiments/providers/platforms/UI scope.
- **Independent plan review**: found five blockers: missing wheel backend/source-package configuration; unreachable Policy/Budget/Model Configuration flows; fixture commands without deterministic provisioned imports/tools; no `dist/pages` producer; and task commits omitting `AGENT_LOG.md`. The plan was corrected before cold-start dispatch.
- **Implementation state**: none retained; no commit or push authorized or performed.

## 2026-07-27 / COLDSTART-1 - Stage 4 disposable attempt paused

- **Review isolation**: detached worktree `C:\Users\29119\.codex\worktrees\stage4-apexcrew-87fd-review` at `07f20aa92de97088d56d07c1a8528f93a220cf91`; fresh different-model reviewer received only `SPEC.md` and copied untracked `PLAN.md` as normative inputs.
- **Task 1 red evidence**: `uv run pytest tests/unit/test_package.py::test_package_exposes_initial_version -q` failed with the expected `ModuleNotFoundError: No module named 'apexcrew'`, but selected Python 3.11 rather than required Python 3.12.
- **Blocking probe**: `uv python find 3.12` failed because no CPython 3.12 interpreter was discoverable. The reviewer correctly paused instead of inferring authority for `uv python install 3.12`.
- **Task 2 result**: not attempted because Task 1 could not complete. Read-only inspection additionally found undefined canonical revision-document fields and lifecycle-dependent enforcement assigned to immutable type files.
- **Disposable changes**: only `tests/unit/test_package.py`, ignored pytest/bytecode caches, and the copied `PLAN.md`; no package source, configuration, lockfile, credentials, commit, push, or user-target effect.
- **Plan correction**: authorized an explicit conditional Python 3.12 host prerequisite; made the initial red command select Python 3.12; defined exact closed Policy/Budget/Model Configuration documents, digests, payloads, revision bindings, and confirmation codes; moved state-dependent revision rules to `CrewControlService` and the runtime action barrier.
- **Status**: Attempt 1 rejected as incomplete. Remove its disposable artifacts/worktree, then rerun Tasks 1-2 in a new detached worktree and fresh context. Stage 4 remains open.

## 2026-07-28 / PLAN-R2 - execution-readiness audit and correction

- **Authority and scope**: documentation-only correction under the frozen approved `SPEC.md` digest `2F1434AB29C3B7205B13CA96FE35D18C7666729F633EDF984F1DFCA54F0663BC`; no source, fixture, test, CI, lockfile, credential, commit, or push was created by this audit.
- **Independent findings**: dependency review reported no forward references or missing legacy task commit commands. Schedule review found the claimed 53-hour all-scope completion infeasible from 757 two-to-five-minute actions before TDD/review/commit overhead; it also found non-atomic Tasks 2, 7, 11, 12, and 27, Task 28's unsequenced provider work, a premature eight-job Task 35 release assertion, and a partial `TASK-*` ledger presentation.
- **Plan correction**: `PLAN.md` now records M0-M4 milestones, requires owner capacity review at every boundary, keeps M4 requirements deferred rather than waived, splits the five legacy groups into independent execution slices, designates Task 28 as the offline-contract-tested M4 provider profile, makes Task 35A report `PENDING_FINAL_CI_TOPOLOGY`, and adds Task 35B after Task 36C for the eight-job release verifier.
- **Cold-start scope**: the next disposable review is limited to the Python 3.12 probe, Task 1, and Task 2A. It stops before ledger/commit steps and cannot close retained implementation until an independent re-review accepts the R2 plan and its generated worktree is removed.
- **Observed host prerequisite**: `uv python find 3.12` passed and selected `C:\Users\29119\AppData\Roaming\uv\python\cpython-3.12.12-windows-x86_64-none\python.exe`.
- **Implementation state**: none retained. The old Attempt 2 worktree remains quarantined because it contains a stale untracked plan; it is not an authorized review input.
- **R2 independent re-review**: schedule, structural, and dependency auditors reported zero blockers for an M0 Stage 4 evaluator. They verified that only Task 1 and the exact `2A` procedure are executable, that `2A` has independent red/green/mypy/diff commands, that M1-M4 require future reviewed plan revisions, that 35B consumes committed CI definitions rather than completed remote jobs, and that the Python 3.12 host probe passes. The remaining 66-task roadmap is not current execution authority.

## 2026-07-28 / COLDSTART-2 - Stage 4 M0 attempt paused and corrected

- **Review isolation**: fresh user-owned evaluator in detached disposable worktree `C:\Users\29119\.codex\worktrees\bbcb\AI4SE` at documentation commit `6d219a91413e6de52e8f5b86ee87d15c149544a8`; normative inputs were only frozen `SPEC.md` and R2 `PLAN.md`.
- **Prerequisite and red evidence**: `uv python find 3.12` selected CPython 3.12.12. The initial and configuration-only package selectors both failed with the expected `ModuleNotFoundError: No module named 'apexcrew'`. The `.gitignore` scoped diff contained exactly the allowed added `.tmp/` line.
- **Pause**: `uv lock --python 3.12` exited 0, resolved 51 packages, and created `uv.lock`, but emitted previously unspecified nonfatal third-party metadata warnings about ignored legacy Jinja2/keyring/tqdm artifacts and corrected invalid version specifiers. The evaluator correctly treated unlisted output as an ambiguity and stopped before sync, green tests, `2A`, ledger/staging, or commit.
- **Disposable changes**: only the Task 1 files `pyproject.toml`, `.python-version`, `.gitignore`, `uv.lock`, `src/apexcrew/__init__.py`, `tests/conftest.py`, `tests/unit/test_package.py`, plus ignored pytest cache; no credentials, provider calls, source outside M0, staging, commit, push, or publication.
- **Plan correction**: Task 1 Step 8 and the Stage 4 gate now explicitly permit those exit-zero resolver metadata diagnostics while retaining stop conditions for lock failure, missing lockfile, unsupported Python, or missing direct dependency. Attempt 2 is rejected as incomplete; its worktree must be removed and a fresh evaluator must rerun M0 against the amended plan.
- **Cleanup state**: `git worktree remove --force` removed the disposable worktree registration but Windows returned `Permission denied` while deleting its directory. The completed evaluator task was archived; repeated exact-path removal and recoverable move both remain blocked by an external file handle. The orphaned directory is outside every Git worktree and contains only the listed disposable M0 files. No new cold-start attempt may begin until it is removed or the owner explicitly accepts a recorded cleanup exception.

## 2026-07-31 / CLEANUP-1 - Attempt 2 cleanup closed and a duplicate plan retracted

- **Session context**: Claude Code (Opus 5) was asked to assess the project's architecture, stack, and stage, then authorized to proceed on its own judgment.
- **Assessment error**: the initial reading ran `git log` on `main` only and never ran `git branch -a`. `main` was clean and its last commit was the 2026-07-27 spec sign-off, so the session incorrectly reported that no engineering had occurred since sign-off, that `writing-plans` had never produced `PLAN.md`, and that Python 3.12 was the outstanding host prerequisite. All three were false: PLAN-R1, COLDSTART-1, PLAN-R2, and COLDSTART-2 had already happened on unmerged branch `codex/stage4-m0-plan`, and `uv python find 3.12` had already passed on 2026-07-28.
- **Consequence**: acting on that wrong reading, the session wrote a separate 823-line `PLAN.md` and committed it to `main` as `08b067f`, together with status edits to `README.md`, `INITIALIZATION.md`, `SPEC_PROCESS.md`, and `AGENTS.md`.
- **Retraction**: the repository owner chose to discard that work. `main` was reset to `07f20aa` and fast-forwarded to `b1c1d47`, so the reviewed R2/R3 `PLAN.md` is the sole plan of record. The discarded draft was kept only outside the repository as a non-authoritative reference and is not a second plan truth.
- **Cleanup performed**: the orphaned Attempt 2 directory `C:\Users\29119\.codex\worktrees\bbcb\AI4SE` recorded in COLDSTART-2 as undeletable was observed empty on 2026-07-31, removed, and its empty parent `bbcb` removed with it. Git had already deregistered that worktree, so no Git operation was involved. The COLDSTART-2 precondition that blocked a new cold-start attempt is now satisfied without a cleanup exception.
- **Also removed**: a disposable review worktree this session had created at `C:\Users\29119\Desktop\apexcrew-coldstart-review` from the retracted plan. `git worktree list` now shows only the main worktree plus the two pre-existing Codex worktrees.
- **Host state**: `uv python install 3.12` was re-run this session and was a no-op; `cpython-3.12.12-windows-x86_64-none` was already present, consistent with the COLDSTART-2 probe.
- **Publication state**: unchanged and unauthorized. `origin/main` remains at `e70b3e3`; local `main` is ahead and was not pushed.
- **Implementation state**: none retained. No `src/`, `tests/`, `fixtures/`, or CI artifact exists on `main`.
- **Next authorized action**: a new user-owned Stage 4 M0 evaluator, different agent type, fresh session, disposable worktree at the current documentation commit, receiving only frozen `SPEC.md` and R3 `PLAN.md`, attempting Task 1 plus exact `2A` and stopping before ledger, staging, or commit.
- **Lesson**: a clean working tree on the default branch is not evidence that no work exists. Enumerate refs before judging project state, and treat an unexpectedly quiet history as a prompt to look wider rather than as a finding.

## 2026-07-31 / CLEANUP-2 - Stale Attempt 1 worktree removed

- **Target inspected before deletion**: `C:\Users\29119\.codex\worktrees\stage4-apexcrew-87fd-attempt2`, detached at `07f20aa`. `git status` showed exactly one untracked file, the superseded R1 `PLAN.md`; every other entry was tracked content already present at that commit.
- **Superseded artifact identified, not silently destroyed**: the R1 plan measured 7,385 lines at SHA-256 `835231CA29C880F875F3FE42C142B2B711CEEF46995448F5B780D0EB3165A676`. That digest is recorded in `SPEC_PROCESS.md` so the artifact Attempt 1 reviewed stays identifiable, while the repository keeps only one plan of record. Its substantive findings were already captured in PLAN-R1 and COLDSTART-1.
- **Removal**: `git worktree remove --force` deregistered and deleted it without the Windows file-handle failure seen in COLDSTART-2. `git worktree list` now shows only the main worktree and `C:\Users\29119\.codex\worktrees\87fd\AI4SE` on `codex/stage4-m0-plan`, whose commits are merged into `main`.
- **Rationale**: `SPEC_PROCESS.md` had recorded that this worktree held a pre-R2 plan that must not reach an evaluator. Removing it converts a documented prohibition into a structural impossibility before Attempt 3 is dispatched.
- **Implementation state**: none retained. `main` remains documentation-only at a clean tree.
- **Publication state**: unchanged. `origin/main` is still at `e70b3e3`; no push was authorized or performed.

## 2026-07-31 / COLDSTART-3 - Stage 4 M0 review passed with zero blockers

- **Dispatch**: fresh Claude Sonnet 5 evaluator, no inherited context, isolated disposable worktree at `d0ebc62`, inputs restricted to frozen `SPEC.md` (`2F1434AB...663BC`) and R3 `PLAN.md` (`93ADDFE7...90CE`). The Execution Gate's prohibition on loading `superpowers:executing-plans` was passed through explicitly and observed.
- **Result**: both authorized M0 slices completed with no pause. Task 1 red reproduced the specified `ModuleNotFoundError`; `uv lock --python 3.12` exited 0 resolving 51 packages; Task 1 and `2A` focused selectors turned green; `mypy` succeeded on `types.py` and `revisions.py`; `git diff --check` was clean; `commands.py` was confirmed absent. Verdict `ZERO BLOCKERS` with four non-blocking findings.
- **Independent verification**: the recorder re-ran the Task 1 and `2A` green selectors and `mypy` inside the evaluator's worktree and observed the same passing output before destroying it. The report was not accepted on its own assertion.
- **Process-strength qualification**: Attempts 1 and 2 were owner-dispatched Codex evaluators. Attempt 3 was dispatched by the assisting agent inside the same session. Cold context, a different model from both dispatcher and prior evaluators, worktree isolation, and the two-file input restriction all held, and the evaluator disclosed that an early directory listing incidentally exposed forbidden filenames without opening their content. It is still weaker evidence than an owner-opened session because the dispatcher was not independent of the plan. An owner-dispatched Codex confirmation run is recommended before this is cited as sole Stage 4 closure.
- **Unexercised amendment**: `uv lock` emitted zero warnings, so the R3 correction permitting resolver metadata diagnostics was never triggered. It is written but unvalidated by observation.
- **Cleanup**: `git worktree remove` reported the same Windows permission error seen in COLDSTART-2 after Git had already deregistered the worktree. The empty directory was removed on retry, `.claude/worktrees/` was removed, and the disposable branch `worktree-agent-adb25cb7a7b91acb4` was deleted. `main` contains no `src/`, `tests/`, `pyproject.toml`, `uv.lock`, or `.python-version`; `git status` is clean.
- **Implementation state**: none retained. Stage 4 success now authorizes retained re-execution of Task 1 and `2A` under M1, which itself still requires a new exact M1 `PLAN.md` revision, an independent document review, and the owner capacity decision below.

### M0 milestone capacity review (required by `PLAN.md` Milestones section)

| Field | Value |
|---|---|
| Elapsed against the planned budget | ~17.9 h of the ~53 h notionally available from 2026-07-26, at the stated 25 h/week |
| Hours available before the deadline | ~35.7 h (2026-07-31 to 2026-08-10 23:59 Asia/Shanghai at 25 h/week) |
| Completed retained evidence | None. M0 is disposable by definition; its artifacts were destroyed |
| Conservative remaining estimate for M1-M4 | Exceeds the hours available. `PLAN.md` already records that the 757 listed 2-5 minute actions alone total 25.2-63.1 raw hours **before** red/green reruns, full regressions, review, evidence recording, and 66 commit boundaries |
| Owner decision | **PENDING.** Not decided by an agent |

The remaining schedule does not close for the full signed scope. Per the Milestones section the only valid outcomes are a new approved scope revision or additional capacity/time; silently omitting a required signed item is prohibited. The owner must record an explicit `GO` or `HOLD` for M1, and if `GO`, state which signed deliverables move behind the deterministic offline core.

## 2026-07-31 / CAPACITY-M0 - Owner capacity decision and M1 readiness audit

- **Owner decision**: capacity raised from 25 h/week to **30-40 h/week**, with the instruction not to over-weight the deadline and to aim for full implementation. This supersedes the 25 h/week figure in the M0 capacity table above for planning purposes; it does not alter `SPEC.md`, which records the original assumption.
- **Recomputed budget**: 2026-07-31 to 2026-08-10 23:59 Asia/Shanghai is ~10 days, giving **~42.9 h at 30 h/week and ~57.1 h at 40 h/week**. `PLAN.md` records 25.2-63.1 raw hours for the 757 listed actions before red/green reruns, full regressions, review, evidence recording, and 66 commit boundaries. The full signed scope therefore closes only at the optimistic end of that range with negligible rework; M1-M3 is a defensible commitment and M4 remains stretch.
- **Host prerequisites re-verified 2026-07-31, all PASS**: Git 2.47.1 (SPEC requires 2.43+), CPython 3.12.12 via uv, uv 0.9.29, Node 22.14, Docker server 29.6.1 responding, and OS keyring resolving to `keyring.backends.Windows.WinVaultKeyring`. Keyring had not previously been verified and is required by SPEC 5.3 for the Secret Path Set installation key.
- **Blocking M1 start**: `PLAN.md` requires a new exact M1 `PLAN.md` revision plus an independent document review before retained M1 execution, and requires preserving the superseded M0 plan in a documented course-artifact location. Neither has been done. The R1/R2/R3 plans were produced with a pinned personal `writing-plans` under Codex; that skill is still not loadable in the Claude Code session, so the M1 revision should be produced the same way to keep plan provenance consistent.
- **Owner-only external enablement, none yet done**: GitHub Pages, GHCR publication, and any package trusted publisher require repository-owner action; no `.github/workflows/` exists yet; the `gh` CLI is not installed on this host. The NJU/GitLab remote is still unconfigured - `origin` is the only remote - and remains a hard final course-delivery dependency rather than an optional one.
- **Promised-but-absent artifacts**: `SECURITY.md` (named in the INITIALIZATION documentation table), `DESIGN.md` (named in SPEC 10.5), `REFLECTION.md` (final phase), `.gitlab-ci.yml` with its exact `unit-test` job, `Makefile`, `pyproject.toml`, `fixtures/`, and all CI workflows. All are expected to be absent at this stage except `SECURITY.md`, which the documentation table implies should already exist.
- **Carried design question**: the Budget Revision field-scope ambiguity raised as non-blocking finding 1 in COLDSTART-3 must be resolved before Task 10 implements `propose_budget`/`approve_budget`.
- **Implementation state**: none retained. `main` is documentation-only and clean.

## 2026-07-31 / COURSE-GAP-1 - Graded course requirements not carried by the current plan

A re-read of the course brief against the repository found requirements that `SPEC.md` and the R3 `PLAN.md` do not fully carry. These are graded deliverables, not preferences.

- **PR workflow is mandatory and is currently unmet.** Brief 4.6 requires one worktree per independent feature/large module, each corresponding to one PR; 4.7 rejects a single commit containing everything and requires a complete commit and PR history. All 12 commits to date went directly to `main` with zero PRs, and `.github/PULL_REQUEST_TEMPLATE.md` has never been used. Documentation-only commits are defensible, but M1 implementation must move to worktree-plus-PR boundaries.
- **Attribution format**: 4.7 requires commit or PR text to state which subagent performed the work and which parts a human changed. Existing commits carry `Co-Authored-By` but not that split.
- **Two-phase per-task review**: 4.6.4 requires a spec-compliance check followed by a code-quality check after every task, with critical issues fixed before the next task. The current sizing does not visibly include this cost.
- **Continuous `PLAN.md` updates**: 4.7 requires marking each task complete with its commit hash as it lands.
- **Submission channel**: deliverable list 5 submits through one NJU GitLab repository link; GitHub is development only. No NJU remote is configured - `origin` is the sole remote. Deliverable 6 requires `.gitlab-ci.yml` with a job named exactly `unit-test`, and deliverable 7 requires the **last CI run to be green**, which is execution work rather than a file drop.
- **Deliverable 9 tension with the signed scope**: the course requires a publicly reachable URL exposing an accessible WebUI. `SPEC.md` v0.1 excludes hosted backend, production public execution, and a writable WebUI, and its only public surface is a sanitized static fixture replay on GitHub Pages. Whether that satisfies deliverable 9 is a grader judgment. It must be decided explicitly and described accurately in `README.md` rather than left implicit.
- **`REFLECTION.md` authorship**: brief section 6 requires the student to write it and forbids AI ghost-writing, permitting disclosed AI polish. No agent may draft its content. Planning its schedule and required questions is permitted.
- **`SECURITY.md`** is named in the `INITIALIZATION.md` documentation table but does not exist; either create it or correct the table.

These findings are inputs to the M1 `PLAN.md` revision, not authorization to change `SPEC.md`. Where a requirement conflicts with the signed specification, the resolution is an explicit owner decision, recorded, and if necessary a proposed specification amendment with a new digest.
