# R4.3-03 Restricted Docker Composition Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven development or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Connect the production Worker tool runtime to independent attempt context/check workspaces and the fail-closed restricted Docker executor without losing patch state or weakening deadline and snapshot bindings.

**Architecture:** `R union D` is materialized as a read/search context root. Each declared check receives its own `Q_i union W_i` check root, snapshot manifest, digest, and check binding; those roots are cached for the Attempt so a later check sees earlier patches. `AttemptPatchExecutor` mutates the selected check workspace, while `RestrictedDockerExecutor` is the only production `ExecutorPort` and reports Docker/process uncertainty without a host fallback.

**Tech Stack:** Python 3.12, pytest, Pydantic domain documents, typed Git operations, no-follow workspace adapters, Docker CLI, SQLite state journal, Ruff, mypy.

---

## Scope and Seams

Files are deliberately limited to the existing composition and executor adapters, the new integration contract test, and the four task documents required by the R4.3 ledger.

- Modify `src/apexcrew/application/composition.py` to build/cache context and per-check workspaces, derive one canonical check ID, construct each `SanitizedSnapshot`, and inject every `ScopedToolRuntime` dependency.
- Modify `src/apexcrew/adapters/executor/restricted.py` only where the process boundary needs a bounded uncertainty or environment correction exposed by composition tests. Keep structured `argv`, `env={}`, digest-pinned image, and no host subprocess fallback.
- Create `tests/integration/test_composed_worker_tools.py` at the composition seam. Observe behavior through `ScopedToolRuntime.execute()` and the production bundle's Worker tool graph; do not mock internal collaborators.
- Extend `tests/integration/test_restricted_executor_docker.py` with the production-only executor selector and explicit Docker/daemon skip reasons.
- Synchronize `README.md`, `SECURITY.md`, `SPRINT.md`, and `AGENT_LOG.md` only after observed Docker verification. `DEBT-M2-005` remains OPEN if the daemon/image cannot be exercised.

The behavior seams under test are: a scoped patch result, a declared check result, independent materialized roots/digests, action-deadline binding, and the absence of any host-executor path. Private helper names are not the assertion surface.

## Task 1: Write red composition selectors

**Files:** Create `tests/integration/test_composed_worker_tools.py`; modify no source files.

- [ ] Add a deterministic fixture that creates a temporary Git repository, a minimal approved run/lease/contract, and a `ScriptedMockLLM` composition. Use an injected recording `ExecutorPort` only in the test-only composition seam; the production bundle path must be checked separately.
- [ ] Add the following selectors with independent expected literals:

```python
def test_check_id_derivation_is_shared(...):
    # The ID emitted in the Worker context must be the ID accepted by the
    # declared-check registry used by ScopedToolRuntime.
    assert context_check_id == "task-01:check-1"
    assert registry.require(context_check_id).argv == ("pytest", "-q")

def test_composed_patch_is_not_lease_scope_denied(...):
    result = composed_tools.execute(patch_intent)
    assert result.code == "PATCH_APPLIED"

def test_composed_check_resolves_declared_definition(...):
    result = composed_tools.execute(check_intent)
    assert result.code == "CHECK_PASSED"
    assert result.bounded_payload["snapshot_digest"] == check_snapshot_digest

def test_context_and_check_workspace_bindings_are_distinct(...):
    assert context_root != check_root
    assert context_digest != check_snapshot_digest
    assert check_snapshot_digest != context_digest

def test_docker_executor_is_the_only_composed_check_path(...):
    assert type(production_tools_executor).__name__ == "RestrictedDockerExecutor"
    assert "LocalSubprocessExecutor" not in repr(production_tools_executor)
```

- [ ] Run the named selectors before implementation:

```text
uv run --python 3.12 pytest tests/integration/test_composed_worker_tools.py::test_check_id_derivation_is_shared tests/integration/test_composed_worker_tools.py::test_composed_patch_is_not_lease_scope_denied tests/integration/test_composed_worker_tools.py::test_composed_check_resolves_declared_definition tests/integration/test_composed_worker_tools.py::test_context_and_check_workspace_bindings_are_distinct tests/integration/test_restricted_executor_docker.py::test_docker_executor_is_the_only_composed_check_path -q
```

Expected red evidence is collection failure for the new module and/or the current `LEASE_SCOPE_DENIED`/missing-dependency behavior. Do not weaken an assertion to make an existing skeleton pass.

## Task 2: Implement independent workspace wiring

**Files:** Modify `src/apexcrew/application/composition.py`; tests remain the selectors from Task 1.

- [ ] Add one canonical helper for check IDs. The helper must be used both when context check metadata is emitted and when the `DeclaredCheckRegistry` is constructed. Preserve only explicitly supported aliases if existing persisted/model actions require them; the canonical emitted ID remains `f"{task_id}:check-{ordinal + 1}"`.
- [ ] Obtain one `AttemptWorkspaceAdapter` per composition runtime binding. Materialize context with:

```python
adapter.materialize_context(
    attempt_id=attempt_id,
    base_oid=GitOid(binding.admissible_head),
    read_globs=contract.read_globs,
    dependency_globs=contract.dependency_globs,
)
```

- [ ] Materialize each check root with its exact input/write union. Cache the materialized root by `(attempt_id, admissible_head, check_id, check-input-glob-digest, write-glob-digest)` and reuse it after a patch. A cache hit must verify the root and manifest still have the expected no-follow identities; it must not silently rematerialize over a mutated root.
- [ ] Construct the read/search `FilesystemRepositorySnapshot` only from the context root. Construct each check `SanitizedSnapshot` from that check root, including only canonical `Q_i union W_i` entries, independently hashing exact bytes and binding its own tree/dependency digest.
- [ ] Build `AttemptPatchExecutor` over the mutable check root used by the action/check binding. A patch result's post-state must stay tied to the action's original snapshot digest; any changed root or binding returns the existing uncertainty/denial result rather than crossing roots.
- [ ] Supply `ScopedToolRuntime` with the existing `DeclaredCheckRegistry`, `deadline_journal=self._store`, `deadline_authority=self._authority`, active lease, and granted workspace. The `_runtime()` result must reject a context/check digest swap through the existing authorization checks.
- [ ] Keep `capture_expected_prestate()` on the same attempt workspace adapter and binding path; it must not reintroduce a shared `DetachedWorkspace` for ordinary read/check/patch actions.

## Task 3: Enforce the production Docker boundary

**Files:** Modify `src/apexcrew/application/composition.py`, `src/apexcrew/adapters/executor/restricted.py`, and the Docker integration test.

- [ ] Ensure the default production branch always instantiates `RestrictedDockerExecutor(policy.executor_profile, self._secret_policy)`. A supplied executor is allowed only for the existing deterministic test composition seam and is never selected by production factory configuration.
- [ ] Preserve the closed command contract: `docker run --rm --network=none`, approved non-root UID/GID, `--read-only`, bounded `/tmp` tmpfs, `--cap-drop=ALL`, `no-new-privileges`, CPU/memory/PID limits, digest-pinned image, read-only snapshot mount, empty host environment plus explicitly allowlisted values, and `shell=False`.
- [ ] Verify Docker CLI absence, daemon/image failure, process timeout, and unobservable process state produce `EXECUTOR_UNAVAILABLE` or `INFRASTRUCTURE_UNCERTAINTY` through `ExecutionResult`; no local subprocess or environment switch may be added. Timeout settlement remains delegated to the existing deadline authority.
- [ ] Run the restricted process selector on a supported Docker host. If unavailable, retain an explicit skip reason and leave `DEBT-M2-005` OPEN in all four documents; only an observed restricted process closes it.

## Task 4: Verify and commit

- [ ] Run the focused composition and executor selectors, then the related Worker/patch/check/recovery selectors.
- [ ] Run `uv run --python 3.12 mypy src`, `ruff check .`, `ruff format --check .`, and `git diff --check`.
- [ ] Run the full offline suite and the Docker integration selector; report platform/daemon skips separately from passes.
- [ ] Re-read `SPEC.md`, `AGENTS.md`, and this plan. Record the exact observed implementation commit, red/green commands, subagent name, and human changes in `PLAN.md` and `AGENT_LOG.md` only after the ordered SPEC and quality reviews pass.
- [ ] Commit implementation and tests with one Conventional Commit. The commit message/trailers must identify the implementation subagent and state `Human-Changes: none` unless the human actually edits the files.

## Self-Review

- The read context uses only `R union D`; check snapshots use each exact `Q_i union W_i`, and every write path is included before mode/secret/content inspection.
- The cache prevents a second Worker action from erasing a prior patch, while distinct roots prevent check execution from mutating read/search context.
- Check IDs are generated once and consumed consistently; deadline journal and authority are the existing durable services bound to the exact check ID and snapshot digest.
- Docker unavailability is an uncertainty/availability result, never permission to run a host process. No credential, socket, network, push, or destructive Git operation is introduced.
- The task remains incomplete until `DEBT-M2-005` is either closed by observed Docker evidence or explicitly recorded OPEN with the daemon blocker.
