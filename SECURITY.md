# Security Policy

## Status

ApexCrew is a pre-release local-first coding harness. The current implementation
status is defined by the completed task/commit ledger in `PLAN.md`; roadmap items
must not be treated as delivered controls.

## Trust Boundary

The single operator, host OS, ApexCrew control plane, OS keyring, SQLite adapter,
sanitized Git adapter, and configured Docker daemon are trusted. Repository files,
Git metadata, model output, dependency scripts, check output, and imported fixture
content are untrusted. The WebUI is read-only; CLI is the sole command and approval
surface.

Workspace escape, symlink or reparse traversal, `.git/**`, `.apexcrew/**`, effective
secret paths, raw shell, host network, Docker socket, push, destructive Git, and
target mutation outside Admission's typed CAS are denied. A risky effect requires
an exact one-use Grant. This file describes the target boundary; only controls whose
PLAN task is marked `DONE` are implemented.

## Credentials

Never commit, log, display, export, mount, or send a credential to a model. Interactive
provider credentials use the OS keyring. CI may receive the documented environment
variable only through the CI secret store. Repository `.env` files are never loaded.

## Reporting A Vulnerability

Use the repository's private GitHub Security Advisory channel. If it is unavailable,
open a minimal issue that contains no secret, exploit payload, private repository
content, or restricted transcript, and ask the owner for a private channel. Rotate
any exposed credential outside this repository before sharing diagnostic metadata.

## Delivery Status

M2-M4 local delivery artifacts and production boundaries are implemented at the
depth recorded in the final-production plan and `AGENT_LOG.md`. The secret
scanner checks the tracked tree and reachable history; replay/WebUI delivery is
projection-only; Docker execution remains fail-closed when the daemon or image
is unavailable.

## Known Operational Boundaries

Observed multi-intent members are selectable only when exactly one member is
identified by authoritative external observation; no observation or multiple
observations remain `INDETERMINATE`. Tier 2 export, retention, and eviction are
implemented fail-closed through the typed retention manager; Tier 2 and
quarantined content remain excluded from exports. The restricted executor
launches only the closed digest-pinned Docker argv and reports daemon/process
unavailability as a typed failure.

`DEBT-M3-001`: malformed unified diffs in the offline demo are denied with the
existing `LEASE_SCOPE_DENIED` result because the closed patch-result contract
does not have a distinct malformed-diff code. The denial is fail-closed and
requires a reviewed `SPEC.md` amendment before the result vocabulary changes.

`DEBT-R4-RECOVERY-001`: the R4.1 action-class recovery and exact resolution paths
are implemented and offline-verified. The remaining boundary is the explicitly
authorized real-provider request; ordinary verification continues to use
`ScriptedMockLLM`.

`DEBT-R4-CLEANUP-001`: terminal cleanup now handles exact path-only and admin-only
crash states with no-follow, identity/digest-bound deletion. Mixed, altered,
malformed, and unobservable states remain fail-closed, record a conflict, and
require operator repair before a new cleanup retry. The terminal Run state is
preserved while cleanup is unresolved.

The production runtime requires an unconsumed Runtime Permit before attempting
the per-Run OS lock, then validates and consumes that Permit in the SQLite
transaction before installing durable owner state. The lock is cross-process and
platform-backed; an absent, stale, or already-consumed Permit cannot create
durable ownership. This control is covered by the runtime lock and Permit
lifecycle tests.

Real DeepSeek dispatch is an explicit operator action. The offline suite never
resolves a provider credential. `tests/integration/test_live_provider_smoke.py`
requires `APEXCREW_LIVE_SMOKE=1`, performs at most one provider request, and does
not print credential or prompt bytes. The CLI remains the writable control surface;
WebUI and Pages replay remain read-only projections.

The production composition root passes that gate into the DeepSeek adapter, and
`CrewRuntime` rechecks it after locating a Runtime Permit but before acquiring
ownership or consuming the Permit. An unauthorized `run` therefore cannot create
runtime ownership, consume a Permit, resolve a credential, construct a provider
client, or dispatch a request.
