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
| 7 | Which failures may be corrected without a new human approval? | **B**: retry within an immutable Plan Revision and Task Contract; reapprove structural changes. | Objective failures feed the Worker within a fixed budget. DAG, scope, check, or Policy change requires reapproval before execution and, after `ACTIVE`, cancellation/new Run; exact budgets belong to Round 3. |

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
- An adaptive Budget Revision combines non-raiseable v0.1 Run/per-Task maxima, evidence-based tranche renewal, deterministic no-progress stops, and approval for any increase from a lower revision only up to those maxima.
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
- At Round 3 close, implementation module shape was deferred to the post-Round-3 architecture comparison, and the complete written specification still required independent review plus separate final human sign-off. Architecture, independent review, and final sign-off are resolved below.

#### Resulting `SPEC.md` diff

Round 3 replaced the open-requirements section with normative provider/provenance and credential behavior, adaptive ceilings and stop rules, a host/container threat model and action taxonomy, dual-tier observability with retention/export, CLI/read-only UI authority, Open Design and GitHub Pages workflows, distribution/platform/CI contracts, exact performance/accessibility thresholds, an objective acceptance matrix, a 53-hour delivery timebox, and explicit residual risks. It also updated module contracts, data entities, architecture flow, scope, and acceptance invariants. At Round 3 close the artifact remained gated by architecture comparison, independent review, final written-spec sign-off, planning, and cold-start review; the architecture comparison is resolved below.

## Brainstorming Workflow Reflection

The workflow was strongest when it forced one bounded decision at a time. It converted a broad "multi-agent, long-context, continuous cowork" idea into a falsifiable stale-evidence failure, exposed crowded prior art, and made the user choose explicit safety, budget, delivery, and non-goal trade-offs. The consolidated visual review also made thirteen interacting Round 3 choices easier to inspect than another long prose draft.

It was weakest at preserving reasoning and enforcing its own exit gates. Letter-only approvals captured the selected option but little of the user's rationale, and Round 2 was initially marked approved before its normative `SPEC.md` diff, lifecycles, and data model existed. The growing vocabulary also created cross-document drift that only independent review caught. Future design rounds must present the exact normative diff and a process/terminology consistency checklist before requesting approval, and must record a short rationale in addition to the option letter.

## Post-Round-3 Architecture Comparison (approved 2026-07-26)

The `codebase-design` Design It Twice workflow ran three read-only design passes against the same SPEC, glossary, dependency categories, and containment constraints. Two agents ran the minimal-kernel and flexible-module briefs in parallel; the third journey-facade brief ran as a follow-up after the agent-thread limit prevented a third concurrent spawn. This is a recorded workflow deviation, not three-agent parallel evidence:

| Design | Strength | Rejected risk |
|---|---|---|
| Minimal kernel: `execute/read` | Maximum external depth and simple end-to-end tests | A giant implementation and ambiguous long-blocking `execute`/interrupt behavior |
| Flexible dual reactor | Strong locality for Coordinator, WorkerLoop, Admission, governance, and adapters | Too many public interfaces could let callers reproduce ordering and scatter rules |
| Journey facade: `propose/continue/inspect` | Trivial primary CLI journey | An overloaded continuation token/generic command bus could carry mutation authority into read projections |

The recommended **A-Hybrid** combines a three-interface Run surface with internal dual-reactor locality. The user explicitly approved it. `CrewControl.handle` accepts exact idempotent human commands; `CrewRuntime.run_until_blocked` owns Coordinator planning and Coordinator/WorkerLoop execution until an external wait or terminal state; `RunQueries.get` is the sole sanitized read interface. CLI may compose control plus runtime, while local/static WebUI receives queries only. Internal deep modules own Coordinator, WorkerLoop, Admission, Authority, EffectJournal/recovery, tools, and projection; adapters never own domain ordering.

The decision is recorded in [ADR-0006](docs/adr/0006-use-a-hybrid-control-runtime-query-interfaces.md). The resulting `SPEC.md` diff replaces the provisional module grouping with exact interfaces, package ownership, stop/error behavior, adapter categories, and interface-level testing rules. No implementation or `PLAN.md` was created.

## Post-Approval Whole-Spec Cold Reviews (completed 2026-07-26)

A whole-document cold read deliberately ignored the approved architecture diff and tried to implement the current SPEC from scratch. It found nine blockers: no executable bootstrap-planning protocol; a pre-approval provider/budget cycle; non-durable model calls; a future-writer Candidate promotion hazard; contradictory consumed-Grant recovery; undefined lease validity after unrelated promotion; Worker reads escaping declared dependencies; target movement before the Run gate; and unconstrained `INDETERMINATE` resolution.

The correction keeps A-Hybrid unchanged while making its ordering executable. It adds `PLANNING` and `READY_TO_START`; exact Policy/Budget/Model Configuration bootstrap approvals and start guards; an eight-request read-only Coordinator planning schema; pre-dispatch Model Request Intents/reservations; separate execution and promotion-hazard graphs; `R`/`D`/`W`/`Q` scope enforcement; lease admissibility through classified heads; pinned-target behavior; consumed-Grant settlement authority for one intent; and closed, objectively guarded uncertainty resolution. It also assigns candidate CAS only to Admission and classifies doctor/configuration/credential/UI-server commands as non-Run auxiliary flows with no model/repository authority.

A second lifecycle, course-consistency, and security read found further implementation forks: CAS ownership drifted back to Coordinator/CLI in companion prose; Candidate promotion required a lease that had already been released; invalid Grants terminally rejected a fresh Candidate; approval waits had pause/cancel and crash-window races; Policy mutability conflicted with a frozen Plan; returned provider model IDs were recorded but not authorized; symlink and secret-path behavior was incomplete; purge could delete its own recovery authority; a checked-out target branch and hostile Git configuration/hooks could escape the safety model; and the acceptance matrix referenced an undefined secret scan.

The correction makes Coordinator schedule, Admission exclusively validate/prepare/issue CAS, and the sanitized host Git adapter execute only typed Admission requests. It freezes Plan and Policy at `ACTIVE`; defines returned-model allowlisting, non-removable plus host-local secret paths, v0.1 symlink denial, terminal tombstone-backed purge, checked-out-target rejection, non-extensible Git storage/invocation, Candidate lease provenance, approval-race precedence, and tracked-tree/full-history secret scanning. These rules narrow and close the approved design; they do not add a new public interface or product mainline.

A third implementer/security/course pass found remaining execution forks around one-command runtime authority, planning and multi-intent state tables, known CAS failure, post-purge reads/idempotency, and Git-native target occupancy. In particular, a locked `--no-checkout` worktree could not be removed by the old allowlist; local Git config could follow external includes before overrides; record-supplied linked-worktree paths could route reads; and cleanup crashes left mixed admin/path states with no bounded repair.

The correction introduces a persisted one-use Runtime Permit between control and runtime; closes planning, `PROMOTING`/`APPLYING`, and unresolved-set transitions; and defines tombstone-only post-purge behavior. One preallocated Target Reservation is reused across `DRAFT` retries. v0.1 rejects pre-existing linked worktrees, parses local config/admin state before Git without dereferencing record paths, and performs terminal cleanup through journaled unlock, exact revalidation, one intent-bound forced remove, and component-specific crash repair. The ownership row exists transactionally before any Git effect. These are failure-closed implementation prerequisites, not broader product scope.

### Final Independent Review (passed 2026-07-26)

Three fresh, read-only reviewers independently evaluated the complete specification frozen at SHA-256 `9751BEA572112994034AEA2AB265CFE6AD54195CFB513C18C587AB35AA2F3EB4`. The implementer/state-machine review, course/companion-document review, and security/Git-containment review each reported `ZERO BLOCKERS`. No reviewer relied on implementation code, and no source, fixture, test, CI, or `PLAN.md` artifact was created. A final delta review found zero blockers on the later status and terminology closeout at the digest signed below.

These edits are specification corrections, not new product scope. No `PLAN.md`, implementation, fixture, test, or CI artifact was created. At review close, final human sign-off remained a separate gate; it is resolved by the following record.

### Final Written-Spec Sign-Off (approved 2026-07-27)

In Codex task `019f99cb-b307-75a3-8059-6e12908ff4b8`, the repository owner explicitly stated: `批准最终 SPEC.md，哈希 2F1434AB29C3B7205B13CA96FE35D18C7666729F633EDF984F1DFCA54F0663BC`. The task channel is the approval authority for this workflow; the digest proves content identity, not cryptographic identity of the approver.

| Field | Approved value |
|---|---|
| Repository | `https://github.com/Ztscream/ApexCrew.git` |
| Artifact | Root `SPEC.md`, 128,418 bytes |
| SHA-256 | `2F1434AB29C3B7205B13CA96FE35D18C7666729F633EDF984F1DFCA54F0663BC` |
| Source commit | `aabdd4fb664eb82dcc34c41391dbdb48872254c8` |
| Git blob locator | `9d32355e610e08a75bf89a3a0fc23785441cda24` |
| Recorder | Codex, 2026-07-27T09:26:27+08:00 |

This sign-off completes Stage 2 and authorizes only Superpowers `writing-plans` to create `PLAN.md`. It does not authorize persistent implementation, credentials, publication, push, release, or any ApexCrew Runtime Grant. `SPEC.md` remains byte-for-byte unchanged: its embedded pre-approval status statements are superseded by this external record. Any byte change, including line-ending normalization, invalidates this approval and requires a new digest and explicit sign-off; future approval records append or supersede rather than rewrite this one.

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
- [x] Compare implementation architectures and obtain explicit approval for A-Hybrid.
- [x] Complete final independent review with zero blockers on the frozen written specification.
- [x] Obtain final human sign-off on the exact written `SPEC.md` digest.

## Stage 3 Planning and Stage 4 Cold-Start Review

The pinned Superpowers `writing-plans` workflow produced root `PLAN.md` from the approved frozen specification. The plan keeps the 2026-08-10 23:59 Asia/Shanghai deadline and 25 hours/week constraint explicit, orders required core before optional scope, and defines red/green commands plus Conventional Commit boundaries without creating retained implementation. Its independent plan-document review initially found five blockers: missing wheel backend/source-package configuration, unreachable Policy/Budget/Model Configuration command flows, non-runnable fixture toolchains, no producer for `dist/pages`, and omission of `AGENT_LOG.md` from task commit commands. All five were corrected before cold-start dispatch.

### Stage 4 Attempt 1 - paused and rejected (2026-07-27)

| Field | Evidence |
|---|---|
| Review snapshot | Detached disposable worktree at commit `07f20aa92de97088d56d07c1a8528f93a220cf91`; untracked root `PLAN.md` copied in as the second normative input |
| Reviewer context | Fresh `gpt-5.6-terra` agent with no inherited task context; instructed to use only `SPEC.md` and `PLAN.md` and attempt Tasks 1-2 |
| Frozen specification | SHA-256 `2F1434AB29C3B7205B13CA96FE35D18C7666729F633EDF984F1DFCA54F0663BC` |
| Task 1 red command | `uv run pytest tests/unit/test_package.py::test_package_exposes_initial_version -q` |
| Observed red | Expected `ModuleNotFoundError: No module named 'apexcrew'`, but `uv` selected system Python 3.11 rather than required Python 3.12 |
| Prerequisite probe | `uv python find 3.12` failed because no managed, PATH, virtualenv, registry, or other discoverable CPython 3.12 interpreter was installed |
| Reviewer action | Paused instead of inferring authorization for the user-level mutation `uv python install 3.12`; Task 2 was not attempted |
| Disposable files | Only `tests/unit/test_package.py`, `.pytest_cache`, bytecode cache, and the copied untracked `PLAN.md`; no package/configuration/lockfile, credential access, commit, push, or repository effect |

Attempt 1 is not a successful review. It exposed an environment gap and an independent plan ambiguity: Task 2 named canonical Policy/Budget/Model Configuration documents without defining their fields, put lifecycle-dependent replacement rules in immutable type files, and omitted the envelope's applicable revision bindings and approval confirmation code required by the specification.

The resulting correction explicitly authorizes the review-host prerequisite sequence `uv python find 3.12`, conditional `uv python install 3.12`, and a repeated successful find; makes the disposable exception distinct from retained implementation; and makes Task 1's initial red command select CPython 3.12. Task 2 now defines closed Planning Read Authorization, Secret Path Binding, Executor Profile, Policy, Budget/pricing, inference/returned-model alias, and Model Configuration documents, canonical digests, exact proposal/approval fields, applicable revision bindings, and confirmation codes. State-dependent bootstrap approval, Plan/Policy freeze, and bounded Budget/Model Configuration replacement moved to Task 10 `CrewControlService`, with barrier application in Task 11.

The review remains open until a clean disposable Attempt 2 implements Tasks 1-2 without guessing, all blocking findings are corrected, independent plan review passes again, and every generated implementation artifact/worktree is removed without merge.

### Stage 3 R2 plan correction and renewed Stage 4 precondition (2026-07-28)

Two further independent document audits ran before a renewed cold-start dispatch. The dependency audit found no missing task-level commit action, no unresolved `Consumes` forward dependency, and a valid Task 1 to Task 2 foundation order. The schedule audit found a blocking execution-readiness problem: 757 stated 2-5 minute actions already imply 25.2-63.1 raw hours, which cannot support an honest 53-hour all-scope commitment once red/green cycles, regressions, evidence, review, and 66 legacy commit boundaries are included. It also found non-atomic legacy groups (2, 7, 11, 12, and 27), unsequenced provider work, a Task 35 release verifier that asserted future jobs before Tasks 36A/36C defined them, and a partial `TASK-*` ledger convention.

`PLAN.md` R2 preserves the frozen specification but adds M0-M4 milestones, explicit owner capacity checks, execution slices, Task 28 M4 provider sequencing, and the Task 35A/35B CI topology split. M4 remains required for a complete signed v0.1 claim; it is deferred, not silently cut. The cold-start probe is correspondingly narrowed to Task 1 plus the `2A` immutable revision foundation, which is sufficient to test zero-context setup and the first vertical domain slice inside the stated 1-2 hour disposable window.

Before a new independent reviewer begins, the recorder must verify the current committed `PLAN.md` hash, the unchanged frozen `SPEC.md` hash, `uv python find 3.12`, and the absence of any retained implementation changes. The reviewer receives only those two normative documents in a fresh disposable worktree, runs no execution sub-skill, does not alter process records, stage, or commit, and stops on any ambiguity. A successful disposable probe alone does not authorize retained Task 1: the R2 plan must receive a second independent document review, its disposable worktree/code must be removed without merge, and the owner must accept the post-M0 capacity report before M1 retained execution.

The R2 re-review closed all M0 document blockers: the M0 procedure has standalone red/green/type/diff checks, the old oversized task groups are explicitly roadmap rather than current authority, Task 35B consumes committed CI definitions rather than impossible completed remote jobs, and the required CPython 3.12 probe resolves successfully. The next authorized action is therefore a new, user-owned Stage 4 M0 evaluator task at the exact documentation commit. It may attempt only Task 1 and `2A` in a disposable worktree and cannot retain, stage, commit, push, or activate any later milestone.
