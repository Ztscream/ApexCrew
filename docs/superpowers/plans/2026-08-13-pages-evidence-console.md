# Pages Evidence Console Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Execute this plan task-by-task with red/green verification. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the one-word Pages status view with a deterministic, read-only Crew Run evidence console that demonstrates ApexCrew's coordination, evidence, freshness, and authority model.

**Architecture:** Keep the GitHub Pages artifact as three same-origin static files with no runtime network access. Embed one sanitized replay document in `index.html`; use framework-free JavaScript with `textContent`, native controls, and hidden-state changes to project the selected Audit sequence into lifecycle, task, evidence, authority, and ledger views.

**Tech Stack:** Semantic HTML, CSS, framework-free JavaScript, embedded JSON, Python build/check scripts, pytest, Playwright.

---

### Task 1: Define the Evidence Console Contract

**Files:**
- Modify: `tests/unit/test_webui_build.py`

- [x] **Step 1: Assert the built page exposes the Run identity, lifecycle, Worker/Task, evidence, authority, Audit ledger, and all five replay controls.**
- [x] **Step 2: Assert the embedded replay has a bounded public schema and the JavaScript contains no network, mutation, or HTML interpretation API.**
- [x] **Step 3: Run `uv run --python 3.12 pytest tests/unit/test_webui_build.py -q` and observe failure against the current one-word page.**

### Task 2: Build the Deterministic Replay Console

**Files:**
- Modify: `webui/index.html`
- Modify: `webui/styles.css`
- Modify: `webui/app.js`
- Modify: `scripts/check_static_replay.py`
- Modify: `docs/deployment.md`

- [x] **Step 1: Embed one sanitized run with Run metadata, three Tasks, two Workers, nine ordered Audit frames, evidence status, and authority decisions.**
- [x] **Step 2: Render a compact operational layout with a Run header, lifecycle rail, replay toolbar, Task topology, evidence and authority panels, and filterable Audit ledger.**
- [x] **Step 3: Implement play, pause, step, sequence scrub, Worker filter, and Task filter using only DOM text/attribute state.**
- [x] **Step 4: Extend the static checker to validate the public replay schema, ordered unique sequences, control markers, and forbidden browser APIs.**
- [x] **Step 5: Document the Evidence Console as a sanitized recorded run rather than an execution service.**

### Task 3: Verify and Deliver

**Files:**
- Modify: `AGENT_LOG.md`

- [x] **Step 1: Run the focused unit and Pages workflow contract tests, static checker, full offline suite, Ruff, and diff checks.**
- [x] **Step 2: Build into a clean directory and verify exactly one HTML and two content-hashed assets.**
- [x] **Step 3: Use Playwright at 1440x900 and 390x844 to verify content, controls, filtering, no horizontal overflow, and zero dynamic requests.**
- [x] **Step 4: Record red/green, specification, quality, attribution, and browser evidence in `AGENT_LOG.md`.**
- [x] **Step 5: Commit as `feat(pages): add run evidence console`; pushing, PR creation, merge, and deployment remain separate owner-authorized actions.**
