# R4-01A Singer Quality/Security Correction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Harden the R4-01A bootstrap CLI and repository metadata parsing against bounded failures and `.apexcrew` control-path replacement races.

**Architecture:** Keep the correction inside CLI delivery, the repository no-follow adapters, configuration parsing, bootstrap parsing, and their tests. A dedicated control-path guard will hold the repository/control directory identity through no-follow handles, use atomic platform primitives for create/open/write where available, and reject operations when the platform cannot provide those primitives.

**Tech Stack:** Python 3.12, Typer, SQLite, pytest, mypy, Ruff.

---

### Task 1: Add failing regression selectors

**Files:**
- Modify: `tests/unit/test_cli.py`
- Modify: `tests/unit/application/test_configuration.py`
- Modify: `tests/contract/test_repository_bootstrap.py`

- [ ] **Step 1: Add tests for the requested failure boundaries.** Cover each in-scope exception family in `init` and `run-create`, store cleanup after constructor and migration failures, rejected symlink/non-regular status and doctor config paths, tuple/list-only sequences, exact one-line OIDs, and a deterministic injected control-path failure.
- [ ] **Step 2: Run the focused selectors and record the expected failures.**

Run: `uv run --python 3.12 pytest tests/unit/test_cli.py tests/unit/application/test_configuration.py tests/contract/test_repository_bootstrap.py -q`

Expected: the new selectors fail because the guard and stricter parsers are absent.

### Task 2: Implement the no-follow control-path guard

**Files:**
- Create: `src/apexcrew/adapters/repository/control_path.py`
- Modify: `src/apexcrew/adapters/repository/no_follow.py`
- Modify: `src/apexcrew/adapters/repository/no_follow_posix.py`
- Modify: `src/apexcrew/adapters/repository/no_follow_windows.py`

- [ ] **Step 1: Add the guard API and backend primitives.** Hold the root/control directory handles, atomically create missing control entries where supported, open existing entries with no-follow semantics, compare handle identities, and write configuration through the held parent handle. Raise `RepositoryUnsafeError` when required no-follow primitives are unavailable.
- [ ] **Step 2: Wire the guard to SQLite path preparation with immediate containment/identity revalidation.** Preserve the existing `SqliteStateStore` API; provide a guarded database path only after no-follow open/identity checks and revalidate after the store opens.
- [ ] **Step 3: Run control-path and CLI selectors.**

Run: `uv run --python 3.12 pytest tests/unit/test_cli.py -q`

Expected: all new control-path and CLI selectors are green.

### Task 3: Harden CLI/config/bootstrap behavior

**Files:**
- Modify: `src/apexcrew/delivery/cli.py`
- Modify: `src/apexcrew/application/configuration.py`
- Modify: `src/apexcrew/adapters/repository/bootstrap.py`
- Modify: `src/apexcrew/adapters/state/sqlite.py`

- [ ] **Step 1: Map all in-scope init/run-create failures to stable JSON codes.** Initialize `store` to `None`, close it on constructor/migration failure, and catch repository, SQLite, timeout, Unicode, OS, and value failures without tracebacks.
- [ ] **Step 2: Make status/doctor use the same guarded config validation.** Symlink and non-regular config nodes must fail closed with stable output.
- [ ] **Step 3: Restrict `_text_sequence` to `tuple` and `list`, and require `_parse_target_oid` to match exactly `^[0-9a-f]{40}\\n$` without stripping.**
- [ ] **Step 4: Run the focused red/green suite.**

Run: `uv run --python 3.12 pytest tests/unit/test_cli.py tests/unit/application/test_configuration.py tests/contract/test_repository_bootstrap.py tests/integration/test_no_follow_paths.py -q`

Expected: all focused selectors exit 0, with only platform-availability skips.

### Task 4: Verify and document evidence

**Files:**
- Modify: `AGENT_LOG.md`

- [ ] **Step 1: Run relevant contract/integration tests, mypy, Ruff check/format, and `git diff --check`.** Record observed commands and outcomes without claiming Spec-Review or Quality-Review completion.
- [ ] **Step 2: Inspect the final diff for composition/runtime/provider changes.**
- [ ] **Step 3: Create one clearly named Conventional Commit with trailers:** `PLAN-Task: R4-01A`, `Subagent: Einstein`, `Human-Changes: Codex correction`, `Spec-Review: pending`, `Quality-Review: pending`.
