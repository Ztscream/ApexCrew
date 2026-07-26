# ApexCrew Specification Process

This file records how ApexCrew moves from an idea to an approved `SPEC.md` and `PLAN.md`. The initialization work below is **Stage 0 discovery**; it does not count as the three Superpowers brainstorming iterations required by the course.

## Stage 0 Decision Record

| Question | Initial hypothesis | Evidence or challenge | Accepted decision |
|---|---|---|---|
| Product value | Multi-agent collaboration, long context, continuous cowork | These capabilities are already common across Coding Agent products. | Focus on an Evidence-Driven Durable Crew with revision-bound handoffs. |
| Differentiation | Durable state, worktrees, evidence, approval | Bernstein and h5i substantially overlap; individual mechanisms are not novel. | Treat the contribution as a small, testable combination and avoid first-of-kind claims. |
| Evidence quality | Passing repository checks is enough | Weak checks may admit wrong patches; mutation/property approaches also have prior art. | Keep weak-oracle challenge as a supporting experiment, not a product rename. |
| Core execution | Reuse host Coding Agent defaults | A-class rules require the submitted harness to own the loop. | Implement Coordinator and WorkerLoop; providers expose only low-level completions. |
| v1 scope | Broad capabilities and provider choice later | Breadth would hide the main mechanism and weaken deterministic tests. | One repository, at most three Workers, Python + TypeScript fixtures. |

The accepted decision is recorded in [ADR-0001](docs/adr/0001-evidence-driven-durable-crew.md). The landscape report remains evidence, not authority.

## Formal Brainstorming - Three Rounds Approved

The installed Superpowers `brainstorming` workflow ran in three explicit, user-approved rounds. This record preserves decisions, consequences, rejected suggestions, remaining ambiguity, and the resulting normative changes.

1. **Round 1 - Problem and scenarios**: target user, painful long-running collaboration failures, user stories, non-goals, and measurable value hypothesis.
2. **Round 2 - Mechanisms and state**: WorkerLoop protocol, task/lease lifecycle, context freshness, evidence gate, approvals, crash boundaries, data model, and Python/TypeScript fixture behavior.
3. **Round 3 - Operations and acceptance**: credentials, WebUI, distribution, observability, budgets, failure handling, security threat model, and objective acceptance criteria.

Each round must log questions asked, the user's decision, alternatives rejected, remaining ambiguity, and the resulting `SPEC.md` diff. Do not mark a round complete based only on an agent-generated draft.

### Round 1 - Problem and scenarios (approved 2026-07-26)

| # | Question | User decision | Design consequence |
|---|---|---|---|
| 1 | Which user task is the v0.1 primary journey? | **A**: one developer delegates a cross-module task lasting hours, leaves, and later returns. | The run must survive absence/restart and may integrate only changes backed by evidence fresh for the current revision. Real-time supervision and multi-issue throughput are secondary. |
| 2 | Which failure most clearly makes v0.1 worthless? | **A**: integrating a change using context or check results that no longer apply to the current revision. | Revision-bound evidence, dependency-aware invalidation, and mandatory revalidation are the main contribution. Recovery, conflict control, and low-supervision operation remain supporting requirements. |
| 3 | What should await the returning developer after a successful run? | **A**: a fresh-evidence Integration Candidate, timeline, and summary; final merge still needs one human approval. | Successful execution ends at an approval-ready state. v0.1 never auto-merges or pushes, and it must make the evidence and change history inspectable. Round 2 later split this provisional term into Task Candidate and Run Candidate. |
| 4 | Who creates the initial Task DAG and Task Contracts? | **A**: the developer provides the goal, constraints, and acceptance requirements; the Coordinator proposes a bounded DAG and write scopes; the developer approves once before Workers start. | Decomposition is an ApexCrew capability, but execution cannot begin from an unapproved plan. Unbounded autonomous task creation is out of scope. |
| 5 | Which baseline should test the main value claim? | **A**: the same scripted trajectory, Workers, and fixtures with revision binding and dependency invalidation disabled. | The primary evaluation is an ablation that isolates evidence freshness. Multi-Worker and human baselines may be secondary, but cannot substitute for this test. |

#### Approved Round 1 conclusion

- The primary user is one developer delegating one cross-module task that runs for hours in an existing repository.
- The Coordinator proposes a bounded DAG and Task Contracts from a human goal and acceptance requirements; Workers start only after one human confirmation.
- The product's main failure is accepting context or checks that no longer apply to the current revision.
- A successful Crew Run stops with what Round 1 provisionally called a fresh-evidence Integration Candidate, plus a timeline and summary awaiting final human merge approval; Round 2 later named this final object the Run Candidate. It never auto-merges or pushes.
- The main contribution is revision-bound evidence, dependency-aware invalidation, and mandatory revalidation. The primary experiment is an otherwise-identical ablation with those freshness rules disabled.
- Recovery, lease isolation, risky-action approval, and low-supervision execution are required support. Multi-issue throughput, real-time supervision, and unbounded task creation are not the v0.1 focus.

The user explicitly approved this round. Its problem and scenario section is accepted input to the future consolidated `SPEC.md`.

### Round 2 - Mechanisms and state (approved 2026-07-26)

| # | Question | User decision | Design consequence |
|---|---|---|---|
| 1 | How should repository changes invalidate context and evidence? | **B**: a declared dependency graph with conservative global fallback. | Versioned Task edges, read/write path globs, and required checks drive affected-closure invalidation. Unknown paths, renames, or graph/Plan/Policy mismatches invalidate all non-terminal work. |
| 2 | Which revision must final evidence verify? | **B**: an isolated integration snapshot prepared from the current integration head. | A receipt for a Worker tip cannot authorize promotion. Checks bind to the prospective prepared commit and expected parent; head movement forces preparation and verification again. |
| 3 | How many actions may one WorkerLoop model call drive? | **A**: exactly one typed action. | ApexCrew persists one action intent and result per turn, then returns structured tool feedback to the next low-level completion. Free-form CLI sessions cannot implement the assessed loop. |
| 4 | How should recovery handle an action that may already have caused a side effect? | **B**: reconcile by action class and pause on uncertainty. | File, Git, and check actions use idempotency keys plus expected pre/post state. Unconfirmable external effects enter `INDETERMINATE`; ApexCrew makes no universal exactly-once claim. |
| 5 | How should dependent tasks advance before final human integration? | **B**: serial promotion to a private local Run Branch, followed by one final approval. | Verified Task Candidates advance only `refs/apexcrew/runs/<run-id>` under a lock and CAS. A frozen Run Candidate receives run-wide checks before a revision-bound Grant may update the user target; ApexCrew never pushes. |
| 6 | What happens when a running Worker Attempt becomes stale? | **B**: let the current atomic action settle, then stop and refresh from the latest Run Head. | The old Attempt becomes `STALE`, loses its lease, and cannot hand off. Known changes restart only the affected closure; unknown, plan, or policy changes trigger global invalidation and a human pause. |
| 7 | Which failures may be corrected without a new human approval? | **B**: retry within an immutable Plan Revision and Task Contract; reapprove structural changes. | Objective failures feed the Worker within a fixed budget. DAG, dependency/write scope, or required-check changes create a new Plan Revision; policy changes create a separate Policy Revision. Either requires applicable human approval; exact budgets belong to Round 3. |

#### Approved Round 2 conclusion

- The execution path is `approved Plan Revision -> Worker Attempts -> prepared Task Candidates -> private serial promotions -> frozen Run Candidate -> run-wide verification -> one final human-approved CAS integration`.
- A versioned declared dependency graph enables targeted invalidation, but unknown changes fall back to global invalidation. Dependency pruning is an optimization, not a claim to discover every semantic dependency; final run-wide checks are mandatory.
- `ModelPort` exposes low-level structured completion only. Each turn yields one typed `ActionEnvelope`; ApexCrew owns action validation, policy, execution, persistence, feedback, budgets, and stopping.
- SQLite will keep transactional state and an append-only audit trail, while Git object IDs identify repository snapshots. Action recovery reconciles recorded intent against observable state and never guesses about uncertain external effects.
- Run, Task, Worker Attempt, Task Candidate, and Run Candidate have separate lifecycles, defined in [SPEC.md section 6](SPEC.md#6-state-model). An immutable Evidence Receipt is never edited to become stale; a separate Freshness Assessment determines current admissibility. Attempt `STALE` is a terminal lifecycle state, while an immutable Approval Grant is accepted or rejected by Grant Validation.
- Task promotion may automatically advance only ApexCrew's disposable private Run Branch after its current prepared commit passes the Task Contract gate. This is not an automatic merge to the user target. Final target integration always requires a single-use Approval Grant bound to the prepared commit, expected target OID, Evidence Bundle, contract, and policy; ApexCrew never pushes.
- The Python fixture changes a fee API from integer cents to decimal dollars; the TypeScript fixture changes session timestamps from milliseconds to seconds. In both, branches are individually green and merge without text conflict, but old evidence must be rejected and refreshed checks expose a deterministic semantic failure.

#### Rejected suggestions and limits

- A global revision fence remains the conservative fallback, not the normal path; fully dynamic dependency inference is too complex and potentially unsound for v0.1.
- Worker-tip-only verification, merge-then-test rollback, model-generated action batches, free-form external agent sessions, unconditional replay, hard-killing an active tool, and silent Coordinator replanning were rejected because they break the freshness, recovery, course-ownership, or human-control boundaries.
- Per-Task human promotion and pausing on every red check were rejected because they prevent the approved hours-long unattended journey without improving final-revision evidence.
- Declared dependencies can omit a semantic relationship. ApexCrew detects declaration and revision mismatches, not arbitrary hidden coupling; the final run-wide suite and explicit documentation bound this residual risk.

#### Remaining ambiguity for Round 3

- Concrete step, token, time, retry, and spend budgets; timeout and escalation defaults.
- Selected real LLM provider/model and environment fingerprint policy; offline acceptance still uses `ScriptedMockLLM`.
- Credential lifecycle, WebUI approval interaction, observability/redaction, supported distribution platforms, and the full threat model.
- Exact run-wide command set, usability targets, performance thresholds, and final objective acceptance matrix.

#### Resulting `SPEC.md` diff

Rounds 1 and 2 created the draft [SPEC.md](SPEC.md) sections for product definition, scope, seven user stories, module contracts and component flow, the six required harness dimensions with feedback/admission as the primary contribution, core protocols, legal state transitions, logical entity relationships, both fixture contracts, and currently approved acceptance invariants. Section 10 explicitly reserves provider, credentials, budgets, containment, WebUI, observability, distribution, and final thresholds for Round 3. The draft is marked not implementation-ready and requires a separate final written-spec approval.

### Round 3 - Operations and acceptance (approved 2026-07-26)

| # | Question | User decision | Design consequence |
|---|---|---|---|
| 1 | Where may repository-owned commands execute? | **B**: trusted host control plane plus restricted Docker executor. | Git, SQLite, credentials, and policy remain on the host; repo commands run non-root in a digest-pinned, networkless container with no host secrets/socket and only a sanitized current snapshot mounted read-only. |
| 2 | Which real low-level model integration is v0.1? | **A**: OpenAI Responses API with `gpt-5.6-terra`. | One OpenAI adapter and `ScriptedMockLLM` satisfy the same one-completion `ModelPort`; no Coding Agent CLI or high-level framework enters the assessed loop. |
| 3 | How are credentials supplied? | **B**: OS keyring interactively, environment override for headless/CI. | Hidden CLI set/status/update/clear never reveals the value; repository `.env` is not loaded; no secret reaches executor, model context, logs, UI, or export. |
| 4 | How is long-running work bounded? | **C1**: adaptive eight-call tranches inside hard ceilings. | 8 active hours, 12 Tasks, 240 calls, 2M/200k tokens, USD 10 reserve, and three Workers are immutable ceilings. A 16-call bootstrap precedes progress-gated renewal; repeated/no-progress states pause. |
| 5 | Which WebUI authority model applies? | **A**: CLI-first with read-only WebUI. | Every mutation, approval, credential, recovery, and final integration command is CLI-only; WebUI consumes a sanitized read model. |
| 6 | How is Open Design used? | **A**: design contract plus one-time/major-change prototypes. | Use a custom ApexCrew Operational system with `design-md`, a disposable `dashboard` prototype, and `design-review`; keep briefs/screenshots/critique, but never make Open Design a runtime/CI/source dependency. |
| 7 | How are traceability and redaction balanced? | **B**: allowlisted Audit Ledger plus restricted local transcripts. | CLI/UI/export use Tier 1 only; Tier 2 is redacted, bounded, local, retained for 30 days/1 GiB, and quarantined on suspicion. |
| 8 | What is the v0.1 threat boundary? | **B**: repo content and Worker output are untrusted; operator/host/control plane/Docker/keyring/TLS are trusted. | Prompt/script/path/log injection, exfiltration, approval replay, cross-Worker interference, and resource exhaustion receive controls/tests; compromised trusted roots are explicit non-goals. |
| 9 | How is the mandatory public WebUI delivered? | **A**: GitHub Pages static interactive Run replay. | CI renders sanitized `ScriptedMockLLM` fixture records through the same read model/templates; no public backend, credentials, commands, or real repository data. |
| 10 | Which platforms and distribution are supported? | **B**: Windows 11 and Ubuntu 24.04 x86_64; Python 3.12 wheel/uv plus GHCR executor. | Ubuntu runs full Docker integration/performance CI; Windows runs offline core/Git/path CI; macOS/ARM are out of scope; GitLab retains the exact `unit-test` job. |
| 11 | Which checks authorize the final Candidate? | **B**: human-approved Run Check Set in the Plan Revision. | Coordinator discovery is only a proposal; the frozen Run Candidate reruns exact structured checks under a bound Execution Fingerprint. Failure rejects; timeout/infrastructure uncertainty cannot pass. |
| 12 | What non-functional threshold is credible? | **B**: balanced quantitative v0.1 gates. | 10k-event latency/recovery, 90-second offline suite, ten-minute full CI/onboarding bounds, 2 MiB static assets, WCAG 2.2 AA, Lighthouse 95, keyboard, and responsive tests are normative. |
| 13 | What delivery constraint controls scope? | Deadline **2026-08-10**, **25 hours/week**. | Treat deadline as 23:59 Asia/Shanghai and approximately 53 total hours; cut optional experiments/platforms before core evidence, safety, CI, public demo, or course artifacts. |

#### Approved Round 3 conclusion

- The trusted host owns authority and durable state; untrusted repository commands execute only in a restricted Docker adapter. Typed capabilities and current revisions, not prompts or human vigilance, enforce the safety claim.
- OpenAI Responses API supplies the one real low-level adapter, while every core gate and demo remains deterministic and offline through `ScriptedMockLLM`.
- An adaptive Budget Revision combines hard Run ceilings, per-Task call/attempt/refresh maxima, evidence-based tranche renewal, deterministic no-progress stops, and approval for any increase.
- CLI is the sole command surface. The token-protected loopback WebUI and GitHub Pages fixture replay are read-only projections from the allowlisted Audit Ledger.
- The public demo, Open Design workflow, Windows/Linux support matrix, wheel plus executor distribution, CI jobs, Run Check Set, performance, accessibility, and onboarding thresholds are explicit acceptance work, not aspirational follow-ups.
- The full action taxonomy, threat assumptions, environment fingerprint, redaction/quarantine, retention/export, and residual risks are normative in [SPEC.md sections 10-11](SPEC.md#10-operations-security-and-delivery).

The user explicitly approved the consolidated Round 3 design after reviewing all selected decisions and the conservative defaults for authentication, retention, action classification, and provenance.

#### Rejected suggestions and limits

- Host-only command execution cannot substantiate repository confinement; containerizing the whole control plane would require unsafe Docker-socket/nested orchestration and complicate local Git/keyring recovery.
- Fixed equal Task budgets waste calls on stopped Tasks; unrestricted adaptive allocation turns model self-confidence into authority. The accepted allocator uses hard ceilings and objective receipts/lifecycle progress only.
- Writable/HTMX parity and a React SPA both duplicate command authority and expand the attack/test surface. Public FastAPI hosting and temporary tunnels add ongoing operations or unstable URLs without improving the evidence mechanism.
- A single sanitized event log loses diagnostic context; raw full transcripts persist secrets. Two-tier storage keeps authority allowlisted and diagnostics local/quarantinable.
- Treating the repository as trusted makes the containment claim hollow; zero-trust host/provider security would require VM/confidential-computing scope that cannot be honestly delivered in v0.1.
- Replaying only Task checks can miss cross-module coupling; automatically replaying arbitrary CI actions adds network/third-party semantics. An approved structured Run Check Set is the bounded middle.
- Linux-only delivery ignores the development host; Windows/Linux/macOS binaries and multi-architecture images exceed the 53-hour budget. Windows plus Ubuntu x86_64 is the tested compromise.
- Production-scale latency and 100,000-event targets were rejected because they would displace mechanism correctness; omitting all quantitative targets would make non-functional claims unverifiable.

#### Remaining ambiguity and external dependencies

- The repository owner supplies the actual OpenAI credential/quota and confirms then-current model availability/pricing only during the opt-in provider slice.
- GitHub Pages, GHCR, and package trusted-publishing settings require repository-owner enablement. The NJU/GitLab remote remains deferred but is a final course-submission dependency.
- The supplied deadline omitted a time; the specification transparently assumes 23:59 Asia/Shanghai unless corrected.
- Implementation module shape is deliberately deferred to the post-Round-3 architecture comparison. The complete written specification still requires independent review and a separate final human sign-off.

#### Resulting `SPEC.md` diff

Round 3 replaced the open-requirements section with normative provider/provenance and credential behavior, adaptive ceilings and stop rules, a host/container threat model and action taxonomy, dual-tier observability with retention/export, CLI/read-only UI authority, Open Design and GitHub Pages workflows, distribution/platform/CI contracts, exact performance/accessibility thresholds, an objective acceptance matrix, a 53-hour delivery timebox, and explicit residual risks. It also updated module contracts, data entities, architecture flow, scope, and acceptance invariants. The artifact remains not implementation-ready until architecture comparison, independent review, final written-spec sign-off, planning, and cold-start gates complete.

## Brainstorming Workflow Reflection

The workflow was strongest when it forced one bounded decision at a time. It converted a broad "multi-agent, long-context, continuous cowork" idea into a falsifiable stale-evidence failure, exposed crowded prior art, and made the user choose explicit safety, budget, delivery, and non-goal trade-offs. The consolidated visual review also made thirteen interacting Round 3 choices easier to inspect than another long prose draft.

It was weakest at preserving reasoning and enforcing its own exit gates. Letter-only approvals captured the selected option but little of the user's rationale, and Round 2 was initially marked approved before its normative `SPEC.md` diff, lifecycles, and data model existed. The growing vocabulary also created cross-document drift that only independent review caught. Future design rounds must present the exact normative diff and a process/terminology consistency checklist before requesting approval, and must record a short rationale in addition to the option letter.

## Stage 2 Exit Checklist

- [x] State the problem, target user, value hypothesis, and at least five INVEST user stories.
- [x] Specify each functional module's input, behavior, output, boundary conditions, and errors.
- [x] Cover performance, security, usability, and observability requirements.
- [x] Define architecture, data model, external dependencies, and the selected LLM provider/model with rationale.
- [x] Design the four domain mechanisms: tools/actions, objective feedback, risky actions/HITL, and cross-session memory/context.
- [x] Give decision, tools, memory, governance, feedback, and configuration a testable minimum implementation; select one mechanism-dense dimension as the main contribution.
- [x] Show how every core mechanism remains deterministically testable with `ScriptedMockLLM` and no network.
- [x] Define the credential threat model and lifecycle, distribution target, supported platforms, and required WebUI.
- [x] Attach objective acceptance criteria, risks, open questions, and the Python/TypeScript fixture contract.
- [x] Record three user-approved iterations, adopted/rejected AI suggestions, and a candid reflection on the brainstorming workflow.
- [ ] Compare implementation architectures, complete independent review, and obtain final human sign-off on the written `SPEC.md`.

## Planning and Cold-Start Review - Pending

After `SPEC.md` is signed off, use Superpowers `writing-plans` to create 2-5 minute TDD tasks with explicit paths, dependencies, failing tests, commands, and expected evidence. Then give only `SPEC.md` and `PLAN.md` to a different agent type in a fresh session. That reviewer may attempt 1-2 tasks only in a disposable isolated worktree. Record every pause, incorrect interpretation, output gap, and resulting revision; then remove the review worktree without merging or retaining its code. This is the sole exception to the pre-implementation gate.
