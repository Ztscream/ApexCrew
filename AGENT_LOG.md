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
