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

### Stage 4 Attempt 2 - paused and rejected (2026-07-28)

| Field | Evidence |
|---|---|
| Review snapshot | Detached disposable worktree `C:\Users\29119\.codex\worktrees\bbcb\AI4SE` at R2 documentation commit `6d219a91413e6de52e8f5b86ee87d15c149544a8` |
| Reviewer context | Fresh user-owned evaluator, instructed to use only frozen `SPEC.md` and R2 `PLAN.md`, execute Task 1 plus exact `2A`, and stop before evidence/staging/commit |
| Prerequisite | `uv python find 3.12` passed and selected CPython 3.12.12 |
| Red evidence | Both initial package selectors failed with the expected `ModuleNotFoundError: No module named 'apexcrew'`; the only `.gitignore` diff was `+.tmp/` |
| Pause point | Task 1 Step 8: `uv lock --python 3.12` exited 0 and wrote `uv.lock`, but emitted undeclared warnings about ignored legacy Jinja2/keyring/tqdm artifacts and corrected third-party version specifiers |
| Reviewer action | Correctly stopped before sync, green tests, `2A`, process records, staging, commit, push, credentials, or later work |
| Disposable files | Task 1 configuration/package/test files, `uv.lock`, and ignored pytest cache only; no retained repository change |
| Cleanup | Git deregistered the worktree but Windows retained the exact directory under an external file handle; evaluator task archived and both exact Git removal and recoverable move were denied |

The warnings are normal resolver metadata diagnostics when the lock command exits 0; the plan had failed to distinguish them from a resolver failure. R3 explicitly permits those warnings while preserving mandatory stops for a nonzero exit, missing lockfile, unsupported Python, or absent direct dependency. Attempt 2 is not a successful cold-start review. Its Git worktree registration is gone, but its generated directory remains locked by an external process and cannot yet be deleted or moved. Release that file handle and remove the exact directory before a new fresh evaluator reruns Task 1 plus `2A` against the amended plan; do not carry over generated code or the old evaluator context.

#### Attempt 2 cleanup closed (2026-07-31)

The external file handle was released at some point after 2026-07-28. On 2026-07-31 the exact path `C:\Users\29119\.codex\worktrees\bbcb\AI4SE` was observed to contain zero entries, was removed, and its then-empty parent `bbcb` was removed with it. Git had already deregistered the worktree, so no Git operation was required or performed. The recorded precondition blocking a new cold-start attempt is therefore satisfied: no orphaned Attempt 2 artifact remains, and no cleanup exception needs to be accepted.

The stale worktree `C:\Users\29119\.codex\worktrees\stage4-apexcrew-87fd-attempt2`, detached at `07f20aa`, was also removed on 2026-07-31. Its only content outside that commit was an untracked superseded R1 `PLAN.md` of 7,385 lines at SHA-256 `835231CA29C880F875F3FE42C142B2B711CEEF46995448F5B780D0EB3165A676`; that digest is recorded here so the superseded artifact stays identifiable without keeping a second plan inside the repository. Removing it eliminates the risk of handing a pre-R2 plan to an evaluator.

One Codex worktree remains registered and is **not** a cold-start input: `C:\Users\29119\.codex\worktrees\87fd\AI4SE` on branch `codex/stage4-m0-plan`, whose commits are now merged into `main`.

### Stage 4 Attempt 3 - zero blockers (2026-07-31)

| Field | Evidence |
|---|---|
| Review snapshot | Disposable isolated worktree at documentation commit `d0ebc62`, branch `worktree-agent-adb25cb7a7b91acb4` |
| Reviewer context | Fresh Claude Sonnet 5 evaluator with no inherited task context; received only the dispatch text, and used only frozen `SPEC.md` and R3 `PLAN.md` |
| Frozen specification | SHA-256 `2F1434AB29C3B7205B13CA96FE35D18C7666729F633EDF984F1DFCA54F0663BC` |
| Plan under review | `PLAN.md` SHA-256 `93ADDFE784DC510E5D621E3CFABFD65814C5352B72D6DA3A79C876700C7490CE` |
| Prerequisite | `uv python find 3.12` PASS, selecting CPython 3.12.12 |
| Task 1 red | `ModuleNotFoundError: No module named 'apexcrew'` as specified, persisting after `pyproject.toml` |
| Task 1 Step 8 | `uv lock --python 3.12` exit 0, `Resolved 51 packages`, `uv.lock` written at 73,519 bytes, **zero warnings emitted** |
| Task 1 green | `pytest tests/unit/test_package.py::test_package_exposes_initial_version` passed |
| `2A` red | `ModuleNotFoundError: No module named 'apexcrew.domain'` as specified |
| `2A` green | Focused revision selector passed; the revision-document import check exited 0; `mypy` reported success on both files; `git diff --check` clean |
| Produced boundary | `src/apexcrew/domain/commands.py` confirmed absent after `2A` |
| Stop discipline | Stopped before each slice's evidence/commit step; nothing staged, committed, or pushed; root process documents untouched |
| Verdict | **ZERO BLOCKERS**, with four non-blocking findings |

Attempt 3 is the first Stage 4 attempt to complete both authorized M0 slices without a pause. The recorder independently re-ran the `2A` and Task 1 green selectors and `mypy` in the evaluator's worktree before destroying it, and observed the same passing output rather than accepting the report unverified.

**Process-strength qualification, recorded rather than glossed.** Attempts 1 and 2 used user-owned Codex evaluators dispatched by the repository owner. Attempt 3 used a subagent dispatched by the assisting agent inside the same working session. Cold context, a different model from both the dispatcher and the prior evaluators, an isolated disposable worktree, and the `SPEC.md`/`PLAN.md`-only input restriction were all satisfied, and the evaluator disclosed that an early directory listing incidentally exposed forbidden **filenames** while no forbidden file content was opened. It nevertheless remains weaker process evidence than an owner-opened fresh session, because the dispatcher was not independent of the plan under review. A confirming owner-dispatched Codex run is recommended before this result is cited as the sole Stage 4 closure.

**The R3 resolver-diagnostic amendment remains unexercised.** `uv lock` emitted no warnings at all in this run, so the exact condition that stopped Attempt 2 did not recur and the amendment permitting those warnings was never tested by observation. It is written correctly but unvalidated.

The four non-blocking findings are: an unclear boundary in `SPEC.md` §10.2 between the seven headline Budget values and the per-Task administrative caps, which `PLAN.md` models as one document of independently proposable fields and which Task 10 will have to resolve; `SPEC.md` §4.1's `domain/` list being a subset of `PLAN.md`'s File Structure rather than an exhaustive manifest; the specified red state being a pytest collection error with exit code 4 rather than a test failure with exit code 1, which converges on the same observable message text; and the plan's per-action sizing excluding a cold reader's comprehension cost across a 626-line specification and a 26,981-line plan.

Every generated artifact was destroyed without merge. Git had already deregistered the worktree when `git worktree remove` reported a Windows permission error, reproducing the COLDSTART-2 failure mode; the empty directory was removed on a later retry, the disposable branch was deleted, and `main` was confirmed to contain no `src/`, `tests/`, `pyproject.toml`, `uv.lock`, or `.python-version`.

## Stage 5 M1 Planning - candidate rejected, 10 blockers (2026-07-31)

The M1-R1 candidate `PLAN.md` at SHA-256 `C19C351A877351214C9D915A6EE23A79AA9FC9EE6C52ADAA879B91F03B6EE5AD` received an owner-dispatched independent document review. Verdict: **10 blockers, M1 stops before Task 1**. No implementation was authorized and none was created.

What passed: module coverage is exact. Tasks 1-17 map to 27 slices across 8 serial module worktrees and pull requests with no omission, duplication, or unassigned slice. The per-task protocol, commit trailer grammar, attribution rules, and task commit ledger satisfy the course workflow requirements, and the capacity statement is honest with its dependencies named and no unconditional fit claim.

### Three blockers reopen the frozen specification

The signed `SPEC.md` was approved after three independent reviews each reporting zero blockers. A fourth reader, working from an implementation plan rather than from the specification alone, found three genuine gaps. This is recorded plainly because it qualifies the earlier sign-off: the specification was complete enough to approve and not complete enough to implement from.

1. **Budget Revision field boundary.** Line 481 states the section 10.2 table values "are also non-raiseable v0.1 administrative maxima", proving a value can be a Budget Revision field and non-raiseable simultaneously. Line 499 calls the per-Task limits "likewise" non-raiseable, which points at that same dual status. The narrow reading adopted by M1-R1 assumed non-raiseable implies not-a-Budget-field, and line 481 defeats that inference. Line 493's enumeration of what a Budget Revision "may lower" nonetheless supports the narrow reading, while line 397's "allocation rules" and "governs Run and Task allocation" support the broad one. Both are defensible; only an approved clarification closes it.
2. **Zero floor for table ceilings.** Line 493 permits lowering any table value but never says whether zero is reachable. A zero calls/tokens/workers proposal can be implemented as a valid stop-budget or as an invalid Revision.
3. **Active Run time semantics.** Line 485 excludes human-wait and paused states, but lines 310 and 312 describe approval waits and orphaned phases whose lifecycle state remains `ACTIVE`. Billing by lifecycle wall clock and billing only intervals holding a Runtime Permit produce different stop moments.

### Seven blockers are plan defects

Undefined helper functions making two claimed red states unreachable; a recovery ordering that contradicts line 499's approved-higher-Budget resume path; a cross-module forward reference to a test file created three modules later under strictly serial execution; a type consumed two modules before it is defined; three stale post-split selector paths; a slice whose mandatory test edit is missing from its exact stage set; and GitHub Actions deferred to M3 although `SPEC.md` line 561 and course section 4.8 require CI on every push while M1 performs eight module pushes first.

### Required sequence before `M1 GO`

A separately hashed `SPEC.md` clarification proposal covering the three specification gaps, owner approval producing a new digest, an M1-R2 plan correcting the seven plan defects and aligning to the amended specification, and a further independent review returning zero blockers. Only then may the owner give `M1 GO` and authorize the first worktree.

## Specification Revision 2 - approved and applied (2026-07-31)

The first amendment to the frozen specification. Revision 1, SHA-256 `2F1434AB29C3B7205B13CA96FE35D18C7666729F633EDF984F1DFCA54F0663BC`, signed 2026-07-27, is superseded but remains the authoritative identity for every decision recorded before this point.

**Revision 2**: SHA-256 `97E9652D874B606C1673867923C97C29834F63B43ADB3F3E89779B13183E26D6`, 131,011 bytes, 636 lines.

**Basis**: proposal `docs/proposals/0001-spec-clarification-budget-and-run-time.md`, raised because the M1 plan review found three gaps that no plan could close. The owner approved all three as written.

**Applied changes**, 12 insertions and 2 deletions, confined to sections 7 and 10.2:

1. **Budget Revision field scope.** Section 10.2 now states that a `BudgetRevisionDocument` contains exactly the eight scalar ceilings represented by the seven table rows plus `pricing_observed_on` and the allowed-returned-ID price mapping, and nothing else; that "allocation rules" in section 7 denotes fixed schema-versioned mechanism behavior rather than mutable fields; and that per-Task tranche/call/Attempt/stale-refresh/manual-resume/no-progress/repeated-action limits, action and check timeouts, the provider retry limit, and the warning threshold sit outside Budget Revision and are rejected before state mutation. Section 7's Budget Revision row was amended to match. This selects the **narrow** reading, chosen because keeping caller-controlled input away from mechanism limits removes a real attack surface: a model able to propose its own timeout or retry ceiling could extend its own budget.
2. **Table ceiling floor.** Every scalar table ceiling other than the cost reserve is a positive integer and zero is rejected before state mutation; the cost reserve may be zero, which reserves nothing and pauses the Run before the next real provider call.
3. **Active Run time accounting.** Time accumulates only across intervals holding runtime ownership, opening in the transaction that consumes a Runtime Permit and closing in the transaction recording the resulting `RunStop`. Duration uses a monotonic clock, never wall-clock differences. A crash with no recorded `RunStop` is closed conservatively at the last Audit Event committed under that ownership generation, and the unobservable remainder is never charged or guessed.

**What this revision does not do**: it authorizes no implementation, gives no `M1 GO`, and does not address review blockers 4-10, which are plan defects belonging to the M1-R2 revision.

## Specification Revision 3 - approved and applied (2026-08-05)

The second amendment to the frozen specification. Revision 2, SHA-256 `97E9652D874B606C1673867923C97C29834F63B43ADB3F3E89779B13183E26D6`, signed 2026-07-31, is superseded but remains the authoritative identity for every decision recorded before this point, including the whole of M1 and the M1-M4 sprint delivered under it.

**Revision 3**: SHA-256 `E4385008CD75E4E3B0E70B25A6EBDFD976F3E1031F2ACD81FF0B6284EF6668AB`, 131,813 bytes, 636 lines.

**Basis**: proposal `docs/proposals/0002-replace-model-provider-with-deepseek.md`, raised because the owner elected to change the model provider and provider identity is frozen text rather than configuration. The owner approved the replacement and authorized the edit on 2026-08-05.

**Applied changes**, 7 insertions and 7 deletions across sections 2, 3, 4, 10.1, 10.2, and 12. Line count is deliberately unchanged at 636 so that every existing line-number citation in `AGENT_LOG.md`, `SPEC_PROCESS.md`, and `PLAN.md` remains valid:

1. **Provider identity.** Lines 25, 61, 137, 469, 473, and 569 now name the DeepSeek Responses API and `deepseek-v4-flash` in place of the OpenAI Responses API and `gpt-5.6-terra`, reached through an OpenAI-compatible client pinned to the DeepSeek base URL. The headless CI credential variable becomes `APEXCREW_DEEPSEEK_API_KEY`. The exact-returned-ID allowlist still has exactly one member, so line 198's `RETURNED_MODEL_MISMATCH` machinery is unchanged in force and the provider's dated `DeepSeek-V4-Flash-0731` build is **not** pre-authorized.
2. **Pricing snapshot.** Line 493 now maps to the 2026-08-05 `deepseek-v4-flash` peak-hour rates, USD 0.28 per million input tokens and USD 0.56 per million output tokens, pinned at peak rather than standard so published time-of-day pricing variation cannot under-reserve a Run crossing a peak boundary. Provider-reported reasoning tokens are declared output tokens for both the output ceiling and cost. Worst-case reservation against the token ceilings falls from USD 8.00 to USD 0.672. The USD 10 table maximum and every other non-raiseable cap are unchanged.
3. **Silently ignored request parameters.** Line 469 now states that because this provider silently ignores unsupported request parameters instead of rejecting them, no safety property may rest on a request parameter alone; the adapter derives every settlement input from the observed response, and an absent or unexpected completion status, returned model ID, usage object, or schema-conformant payload is a closed failure releasing no output. This also makes the provider's own documentation conflict over `text.format: json_schema` support harmless, because a non-conformant payload fails closed either way.

**What this revision does not do**: it authorizes no live provider call. Section 10.1's credential rules and `PLAN.md` line 359's separately authorized smoke both remain in force, and the credential boundary they presuppose does not exist in `src/` yet. No `src/` file required modification for this revision, because model IDs and prices already travel as data through `allowed_model_ids` and `BudgetRevisionDocument.pricing_entries`.

**Consequence for the rejected M1-R1 candidate**: its `Frozen input` gate requires revision 1's digest, so that plan now fails its own precondition and cannot be executed. This is the intended outcome; M1-R2 must cite revision 2.

### Standing qualification on the revision-1 sign-off

Revision 1 was approved after three independent reviews each reporting zero blockers, yet a fourth reader working from an implementation plan found three genuine gaps. Specification review that reads only the specification cannot substitute for review that attempts to build from it. Future specification gates should treat implementation-facing review as capable of reopening specification text, and should not read an earlier zero-blocker verdict as proof of implementability.

## Stage 5 M1 Planning - M1-R2 corrected candidate awaiting independent review (2026-07-31)

M1-R1 remains rejected and is archived byte-for-byte at `docs/architecture/PLAN-M1-R1-2026-07-31.md`, SHA-256 `C19C351A877351214C9D915A6EE23A79AA9FC9EE6C52ADAA879B91F03B6EE5AD`, 1,228,522 bytes. No implementation was retained from that candidate.

The M1-R2 candidate is root `PLAN.md` SHA-256 `659A8097A712F06D086103A3D20B5E83A147A06AF300D5BDB3B1E08E8DAE70F0`, 1,351,845 bytes and 29,987 total lines. It cites approved `SPEC.md` revision 2, preserves the previously accepted 27-slice/8-module boundary and serial review protocol, closes plan blockers B4-B10, absorbs proposal 0001's runtime-ownership time accounting into Tasks 9/9A and 11A/11B, and reports the reduced capacity without claiming the deadline closes.

Before this identity was recorded, a verification pass found three real defects requiring a correction round: four Task 11 SQLite private helpers had no definitions; Task 9D's exhaustion serializer emitted caller order while its reader required canonical order; and Task 17 sent successful `ACTION_RECORDED` recovery with no stop reason into a mapper that rejects it. The corrected candidate defines the four helpers with SQLite `BEGIN IMMEDIATE` locking semantics, canonicalizes exhaustion writes and tests direct unsorted database corruption, and routes successful exact-post Granted-Action recovery back through the permitted phase while reserving the stop mapper for actual stop codes.

That first correction fixed exactly the four helpers named in its input but falsely described the result as a systemic private-helper sweep. A second exact fence-aware verification found 44 unresolved private calls: 11 attributed in owning prose and 33 silently undefined. `_require_expected_sequence` was still missing from the first line of the same Permit function, and the original report had named only four of its five missing helpers. The second round defines every safety-load-bearing helper it identified in the owning task, records the exhaustive convention for the remainder, and finishes at 121 distinct definitions, 96 distinct calls, 13 prose-attributed unresolved calls, and zero silently undefined calls.

After a zero-blocker verification pass performed by the assisting agent, an M1-R2 pre-review polish expanded line 30 from compressed ranges to the ledger's exact 27-slice order and documented why `ACTIVE_RUN_SECONDS` remains decimal despite its integer ceiling; that pass is not the independent review required by the Execution Gate because the assisting agent co-authored the correction rounds, so the gate is unchanged and an owner-dispatched independent document review must still return zero blockers before the owner may write `M1 GO`.

The same round resolved the reported dispatch-closure gap from frozen SPEC revision-2 lines 299, 300, 302, 459, and 460 rather than from owner preference: the durable Run flag/cause set is distinct from lifecycle state, closure occurs in the current action's settlement transaction, runtime barriers own only in-flight settlement, and reopening is restricted to the exact human-resume path after the cause's gates pass. The owner may reverse this record if they read those frozen lines differently; this paragraph is not owner approval and does not grant `M1 GO`.

An owner-dispatched independent review by Gemini 3.1 Pro on 2026-08-01 returned four claimed blockers; three were not sustained on cross-check, because two arose from imprecise wording in the review prompt rather than plan defects and one is contradicted by the writers at `PLAN.md` lines 10947 and 11051, which already canonicalize before the readers validate; the fourth was genuine but was a test-diagnostics defect rather than a blocker and was fixed here together with two further instances the review did not find; the review verdict is not a pass and the gate is unchanged, so a further owner-dispatched independent review must return zero blockers before the owner may write `M1 GO`.

This section records a candidate, not a review verdict or execution approval. The gate sequence is unchanged and must occur in order: the owner dispatches a fresh independent M1-R2 document review against this exact identity; the review returns zero blockers; the owner records explicit `M1 GO`; only then may the owner separately authorize the first module worktree. Until all four events occur, M1 stops before Task 1 and M2-M4 remain unauthorized roadmap.
