# Repository Guidelines

This repository contains the ApexCrew requirements and discovery baseline. Do not add persistent implementation until `SPEC.md`, `PLAN.md`, and an independent cold-start review are complete. The review may generate disposable code in an isolated worktree, but it must never be merged or retained. ApexCrew must own its Coordinator and Worker loops; external agent runners cannot replace them.

## Project Structure & Module Organization

Keep course artifacts at the root: `SPEC.md`, `PLAN.md`, `SPEC_PROCESS.md`, `AGENT_LOG.md`, `README.md`, and `REFLECTION.md`. Store comparisons in `docs/research/`, interview notes in `docs/learning/`, experiments in `docs/experiments/`, and durable decisions in `docs/adr/`. The proposed Python package uses `src/apexcrew/`: domain rules and loops in `core/`, integrations in `adapters/`, wiring in `bootstrap/`, and CLI/WebUI endpoints in `delivery/`. Mirror behavior under `tests/unit/`, `tests/integration/`, and `tests/acceptance/`; keep Python and TypeScript target repositories under `fixtures/` only after planning approves them.

## Build, Test, and Development Commands

The scaffold does not exist. Preserve these future entry points:

- `uv sync --all-groups`: install locked Python 3.11 dependencies.
- `make test`: run all offline tests, including fixture scenarios.
- `make lint`: run Ruff formatting/checks and mypy.
- `make run`: start the local WebUI with `ScriptedMockLLM`.
- `docker build -t apexcrew .`: build the non-root image.

## Coding Style & Naming Conventions

Use four-space indentation and complete Python type hints. Use `snake_case` for modules/functions, `PascalCase` for types, and `UPPER_SNAKE_CASE` for constants. Keep provider SDKs and FastAPI out of `core/`. Pass commands as structured `argv` with a fixed working directory and `shell=False`.

## Testing Guidelines

Use pytest and `test_<behavior>.py`. Follow vertical TDD: observe a relevant failure, add minimum implementation, then refactor. Drive the harness with `ScriptedMockLLM`, while using real temporary Git repositories, worktrees, SQLite databases, and subprocesses. Prove revision-bound evidence, dependency-aware invalidation, lease isolation, approval replay resistance, crash recovery, and test-feedback correction. Core tests stay offline and deterministic; never use an LLM as the correctness oracle.

## Commit & Pull Request Guidelines

Use Conventional Commits, for example `feat(evidence): invalidate stale receipts`. Keep one commit per `PLAN.md` task and record its hash. PRs link the issue and spec items, show red/green evidence, identify agent and human changes, and include relevant WebUI screenshots.

## Security & Agent Workflow

Never commit credentials or secret-bearing logs. Workspace escape and secret access are hard denials; risky actions require a revision-bound, one-use approval. Public demos use MockLLM and disposable repositories only. Update `AGENT_LOG.md` continuously and run spec-compliance review before code-quality review.
