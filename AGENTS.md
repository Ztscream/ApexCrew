# Repository Guidelines

ApexCrew is specification-only. Do not add source, fixtures, tests, or CI until `SPEC.md` is signed off, `writing-plans` produces `PLAN.md`, and cold-start review closes blocking ambiguity. `PLAN.md` is required review input; reviewer code stays disposable. ApexCrew must own its Coordinator and WorkerLoop; external agent CLIs and high-level frameworks cannot replace them.

## Project Structure & Module Organization

Keep course artifacts at the root: `SPEC.md`, `PLAN.md`, `SPEC_PROCESS.md`, `AGENT_LOG.md`, `README.md`, and `REFLECTION.md`. Store research in `docs/research/`, interview notes in `docs/learning/`, experiments in `docs/experiments/`, architecture maps in `docs/architecture/`, and qualifying decisions in `docs/adr/`. The eventual package will live under `src/apexcrew/`; exact module shape remains an architecture decision. Add Python and TypeScript repositories under `fixtures/` only after cold-start review.

## Build, Test, and Development Commands

The scaffold does not exist. Preserve these planned entry points:

- `uv sync --frozen --all-groups`: install locked Python 3.12 dependencies.
- `make test`: run the deterministic offline suite and fixture scenarios.
- `make lint`: run Ruff formatting/checks, mypy, and documentation checks.
- `make demo`: generate the `ScriptedMockLLM` mechanism demonstration.
- `make build`: build the wheel, restricted executor image, and static WebUI.

Never claim these commands pass before they exist and their output is observed.

## Coding Style & Naming Conventions

Use four-space indentation and complete Python type hints. Name modules/functions `snake_case`, types `PascalCase`, and constants `UPPER_SNAKE_CASE`. Keep provider SDKs, Git, SQLite, Docker, credentials, and FastAPI behind adapters. Pass repository commands as structured `argv`; never invoke arbitrary shell text.

## Testing Guidelines

Use pytest and `test_<behavior>.py`. Follow vertical TDD: capture a relevant failure, add the minimum implementation, then refactor. Core tests use `ScriptedMockLLM`, real temporary Git repositories/worktrees and SQLite, but no network or real model. Prove freshness rejection, feedback correction, lease isolation, approval replay resistance, containment, and crash reconciliation through public module interfaces.

## Commit & Pull Request Guidelines

Use Conventional Commits, for example `feat(evidence): reject stale receipt`. Keep one commit per future `PLAN.md` task and record red/green evidence in `AGENT_LOG.md`. PRs link spec items, distinguish agent and human changes, and include read-only WebUI screenshots when relevant.

## Security & Agent Workflow

Never commit credentials or restricted transcripts. Commands and approvals are CLI-only; WebUI is read-only. Workspace/write-set escape, secret paths, raw shell, host network, Docker socket, push, and destructive Git are hard denials. Risky actions require an exact, revision-bound, one-use Grant. Never push without explicit human authorization.
