# ApexCrew Experiments

Experiments falsify the product hypothesis; they do not serve as demonstrations chosen only after the implementation succeeds. Every experiment must run offline with `ScriptedMockLLM`, use disposable Git repositories, produce machine-readable results, and bind evidence to exact revisions. Do not use an LLM-as-judge for pass/fail.

## Fixture Matrix

Maintain two intentionally small Apache-compatible target repositories with fast deterministic checks and dependent two-Task changes. The Python fixture changes a fee interface from integer cents to decimal dollars while a dependent formatter still divides by 100. The TypeScript fixture changes session timestamps from milliseconds to seconds while a dependent countdown still applies millisecond conversion. In both cases the branches merge without text conflict and old focused checks are green, but refreshing against the promoted dependency exposes a deterministic failure. A hidden oracle may support the optional weak-evidence experiment; it is not part of the Worker context or the primary admission gate.

## E1 - Context Freshness Across Ecosystems

**Hypothesis**: after an upstream change is integrated, a dependent Worker cannot hand off using a capsule or checks tied to the old revision.

Run the same scripted two-Worker trajectory against both fixtures. Measure stale artifacts rejected, forced refreshes, repeated work, and final fresh receipts. Failure is any stale candidate entering integration.

## E2 - Revision-Bound Evidence and Weak Oracles

**Hypothesis**: a green but weak repository check is recorded accurately without being overstated as proof of correctness, and a separate challenger can add a reproducible counterexample tied to the candidate revision.

Compare baseline checks with a bounded adversarial acceptance step. Record defects found, extra calls/rounds, counterexample reproducibility, and evidence invalidation after source changes. This supports the mainline; it is not a competing product direction.

## E3 - Recovery and Approval Replay

**Hypothesis**: crashes at declared persistence boundaries resume without duplicate action or integration, while an approval cannot be altered, replayed, or reused.

Inject crashes before and after intent/result persistence. Measure duplicate side effects, duplicate integration, terminal state, and required human interventions. Any automatic replay of an indeterminate external action is failure.

## Result Record

For each run, store fixture revision, scenario version, seed, scripted model trajectory, commands, exit codes, evidence digests, timings, state transitions, and final verdict. Later reports must include negative results and limitations, not only successful traces.
