# ApexCrew Documentation and Interview Guide Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Align the repository documentation with the current implementation stage and create a private, resume-oriented ApexCrew interview guide.

**Architecture:** Keep public project facts in `AGENTS.md` and `README.md`, while keeping personal interview material in one ignored Markdown file under `docs/learning/`. Document only observed repository behavior and distinguish reviewed implementation, work in progress, and owner-only release actions.

**Tech Stack:** Markdown, Git ignore rules, PowerShell verification, repository CLI and test evidence.

---

### Task 1: Correct Repository Instructions

**Files:**
- Modify: `AGENTS.md`

- [x] **Step 1: Replace the obsolete specification-only status**

State that source, tests, fixtures, CI, and delivery artifacts now exist; preserve the frozen `SPEC.md`, self-built Coordinator/WorkerLoop, worktree, review, attribution, and no-push rules.

- [x] **Step 2: Correct present-tense repository guidance**

Change "eventual package" and "planned entry points" language to describe the implemented package and available commands.

- [x] **Step 3: Verify obsolete language is absent**

Run:

```powershell
rg -n "specification-only|eventual package|Planned entry points" AGENTS.md
```

Expected: no matches.

### Task 2: Rewrite the Public README

**Files:**
- Modify: `README.md`

- [x] **Step 1: Establish honest positioning and status**

Describe ApexCrew as a local-first, evidence-driven Coding Agent Harness; distinguish the completed M1-M4 mixed-depth baseline, reviewed R4.3 tasks, in-progress final integration, and owner-only external actions.

- [x] **Step 2: Explain architecture and core mechanisms**

Cover `CrewControl`, `CrewRuntime`, `RunQueries`, Coordinator, WorkerLoop, typed effects, Runtime Permits, Grants, leases, revision-bound evidence, private Run Head, frozen candidates, CAS, restricted execution, recovery, and read-only delivery.

- [x] **Step 3: Make setup and workflows executable**

Use only commands observed from `Makefile`, `pyproject.toml`, and `apexcrew --help`; document offline defaults, credential handling, CLI lifecycle, verification, repository layout, security boundaries, and known limitations.

- [x] **Step 4: Remove stale claims**

Do not claim production readiness, completed R4.3 release gates, hosted Pages publication, live DeepSeek success, or debt closure that is not present on the documented branch.

- [x] **Step 5: Verify README commands and required sections**

Run:

```powershell
uv run --python 3.12 apexcrew --help
rg -n "^## (项目简介|核心能力|架构|安装|运行|分发命令|目录结构|安全边界|当前状态|已知限制|项目文档)" README.md
```

Expected: CLI help exits 0 and every named section is present.

### Task 3: Create the Private Interview Guide

**Files:**
- Create: `docs/learning/APEXCREW_INTERVIEW_GUIDE.md`
- Modify: `.gitignore`

- [x] **Step 1: Ignore only the personal guide**

Add this exact rule:

```gitignore
/docs/learning/APEXCREW_INTERVIEW_GUIDE.md
```

- [x] **Step 2: Write the Harness/Agent knowledge base**

Explain agent loops, Harness responsibilities, orchestration, context and evidence, durable effects, idempotency, recovery, human approval, containment, evaluation, and the distinction from high-level agent frameworks.

- [x] **Step 3: Map ApexCrew implementation highlights**

Connect each interview topic to concrete ApexCrew modules and mechanisms, including trade-offs, failure modes, test strategy, current limitations, and claims that must not be made.

- [x] **Step 4: Add resume-ready material**

Include a one-line summary, three resume bullets, a detailed project description, a STAR narrative, a 30-second introduction, a 2-minute introduction, likely follow-up questions, Chinese and English variants, and measurable evidence with revision caveats.

- [x] **Step 5: Verify privacy and content coverage**

Run:

```powershell
git check-ignore -v docs/learning/APEXCREW_INTERVIEW_GUIDE.md
rg -n "Harness|STAR|简历|Coordinator|WorkerLoop|Runtime Permit|CAS|面试" docs/learning/APEXCREW_INTERVIEW_GUIDE.md
```

Expected: the exact `.gitignore` rule owns the file and all required topics are present.

### Task 4: Documentation Quality Check

**Files:**
- Verify: `AGENTS.md`
- Verify: `README.md`
- Verify: `.gitignore`
- Verify local-only: `docs/learning/APEXCREW_INTERVIEW_GUIDE.md`

- [x] **Step 1: Inspect the final diff**

Run:

```powershell
git diff -- AGENTS.md README.md .gitignore docs/superpowers/plans/2026-08-10-documentation-and-interview-guide.md
git status --short --ignored
```

Expected: only intended documentation changes are shown; the interview guide appears as ignored and is absent from the tracked diff.

- [x] **Step 2: Check whitespace**

Run:

```powershell
git diff --check
```

Expected: exit 0.

- [x] **Step 3: Reconcile claims against observed evidence**

Confirm the README does not convert local reviewed-branch evidence into a claim about `main`, the current dirty checkout, hosted CI, Pages, package publication, or a live provider request.
