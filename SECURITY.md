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

M1-M4 delivery artifacts are present at the depth recorded in `SPRINT.md` and
`AGENT_LOG.md`. SKELETON and STUB tasks are intentionally not described as
production-complete. The secret scanner checks the tracked tree and reachable
history; replay/WebUI delivery is projection-only; Docker argv construction is
closed but its process runner remains a debt.

## Known Runtime Debt

`DEBT-M1-006`: runtime ownership currently provides only the in-process ownership
path. The production process mutex plus POSIX/Windows OS file-lock backend is not
implemented in this sprint. `FileRunOwnership.acquire` fails closed for an explicit
invalid Permit and never creates a lock path; a missing pre-provisioned lock path
does not grant ownership. Multi-process exclusivity is therefore not claimed.

`DEBT-M2-001`: multiple unresolved effects are represented as `INDETERMINATE`
because no precedence table exists. `DEBT-M2-002` through `DEBT-M2-004` keep
Tier 2 export, retention export, and durable eviction disabled. `DEBT-M2-005`
builds a restricted digest-pinned argv but does not launch a Docker process.
