---
status: APPROVED AND APPLIED
date: 2026-07-31
approved: 2026-07-31, owner approved all three gaps as written
applied: SPEC.md revision 1 `2F1434AB…663BC` -> revision 2 `97E9652D874B606C1673867923C97C29834F63B43ADB3F3E89779B13183E26D6`
supersedes: nothing
affects: SPEC.md sections 7, 10.2
---

> **Disposition**: the owner approved all three gaps as written on 2026-07-31. The text below was applied verbatim, producing `SPEC.md` revision 2 at SHA-256 `97E9652D874B606C1673867923C97C29834F63B43ADB3F3E89779B13183E26D6`, 131,011 bytes, 636 lines. The applied diff was 12 insertions and 2 deletions and touched nothing outside the wording proposed here. The pre-amendment bytes remain identifiable by revision 1's digest.

# Proposal 0001: Clarify Budget Revision field scope, table ceiling floors, and active Run time

## Status and authority

This is a **proposal**, not an amendment. `SPEC.md` remains frozen and byte-unchanged at SHA-256 `2F1434AB29C3B7205B13CA96FE35D18C7666729F633EDF984F1DFCA54F0663BC`. No file in this proposal has been applied.

If the owner approves, the changes below are applied to `SPEC.md`, a new SHA-256 is computed, and that new digest is recorded as an approved specification revision in `SPEC_PROCESS.md` and `AGENT_LOG.md`. The old digest remains the historical record for everything decided before this point.

## Why this exists

The owner-dispatched independent review of the M1-R1 plan returned 10 blockers on 2026-07-31. Three of them cannot be fixed in a plan because the specification itself is underdetermined. Two competent implementers reading the current text would build different systems. This proposal closes exactly those three gaps and nothing else.

Blockers 4-10 are plan defects and are **not** addressed here; they belong to the M1-R2 plan revision.

---

## Gap 1 — Budget Revision field scope

### Current text

Section 10.2 line 481:

> The initial approved Budget Revision uses these values, which are also non-raiseable v0.1 administrative maxima:

Section 10.2 line 493:

> A human-approved Budget Revision may lower any table value or restore a previously lowered value up to the table maximum and may replace an allowed-ID price mapping, but it cannot exceed 8 hours, 12 Tasks, 8 planning requests, 240 calls, 2,000,000/200,000 tokens, USD 10, or three Workers in the same v0.1 Run.

Section 10.2 line 499:

> The per-Task limits of 48 calls, five Attempts, three stale refreshes, and two manual resumes are likewise non-raiseable v0.1 administrative caps

Section 7 line 397:

> | Budget Revision | Immutable limits, allocation rules, pricing snapshot, and approval metadata | Independently versioned; governs Run and Task allocation without changing Plan structure |

### The ambiguity

Both readings have direct textual support.

**Narrow**: line 493 enumerates what a Budget Revision may do — lower a table value, restore a lowered table value, replace a price mapping. Nothing else is listed, so nothing else is proposable.

**Broad**: line 481 states the table values *are also* non-raiseable maxima, proving that being non-raiseable does not remove something from the Budget document. Line 499 says the per-Task limits are "likewise" non-raiseable, which most naturally points at that same dual status. Line 397 names "allocation rules" as Budget Revision content and says it "governs Run **and Task** allocation".

The M1-R1 plan adopted the narrow reading on the inference that non-raiseable implies not-a-Budget-field. Line 481 defeats that inference. The narrow reading may still be the right design, but it cannot be reached from the current text without choosing.

### Proposed text

Append to section 10.2, immediately after line 493:

> A `BudgetRevisionDocument` contains exactly the eight scalar ceilings represented by the seven table rows above — input and output tokens are separate fields — plus `pricing_observed_on` and the complete allowed-returned-ID price mapping. It contains nothing else. "Allocation rules" in section 7 denotes fixed schema-versioned mechanism behavior, not mutable Budget fields. The per-Task tranche sizes, call, Attempt, stale-refresh, manual-resume, no-progress and repeated-action limits, the ordinary-action and declared-check timeouts, the provider retry limit, and the warning threshold are outside Budget Revision. They cannot be proposed, lowered, restored, or replaced by one, and an otherwise valid proposal carrying any of them is rejected before state mutation.

Amend section 7 line 397's Budget Revision row to read:

> | Budget Revision | Immutable table ceilings, pricing snapshot, and approval metadata | Independently versioned; governs Run and Task allocation through fixed mechanism rules without changing Plan structure |

### Impact

- Selects the **narrow** reading, matching the M1-R1 plan's `V01_MECHANISM_LIMITS` design, so that plan's Task 2A and Task 10 survive.
- `BudgetRevisionDocument` keeps `extra="forbid"`; Task 10 must prove `propose_budget` rejects a management-cap key before state mutation.
- Removes the wording that made the broad reading defensible, so the ambiguity does not return.
- Closes review blocker 1.

### If you prefer the broad reading instead

Say so and this gap gets the opposite text: the per-Task limits become proposable-but-non-raiseable fields. That is also a valid design. It costs more — `BudgetRevisionDocument` grows to ~22 fields, Task 10 gains per-field validation, and the M1-R1 `limits.py` design is discarded. The narrow reading is recommended because it keeps caller-controlled input away from mechanism limits, which is a real attack surface: a model that can propose its own timeout or retry ceiling can extend its own budget.

---

## Gap 2 — Whether a table ceiling may be lowered to zero

### Current text

Line 493 permits lowering "any table value" but never states a floor.

### The ambiguity

A proposal setting model calls, tokens, or concurrent Workers to zero can be implemented as a valid stop-budget that pauses the Run at its next action, or as an invalid Revision rejected at proposal time. Both are defensible. The M1-R1 plan decided `ge=1` for every scalar except cost on its own authority, which is a specification decision made in a plan.

### Proposed text

Append to section 10.2, immediately after the Gap 1 text:

> Every scalar table ceiling other than the cost reserve is a positive integer; a proposal setting one to zero is invalid and is rejected before state mutation. The cost reserve may be zero, which reserves nothing and therefore pauses the Run before the next real provider call.

### Impact

- Ratifies the `ge=1` floor the M1-R1 plan had already assumed, so no plan rework is needed for this gap.
- Makes the cost-reserve exception explicit and gives it a defined consequence rather than leaving it to inference.
- Closes review blocker 2.

---

## Gap 3 — Active Run time semantics

### Current text

Line 485:

> | Cumulative active Run time (human-wait and paused states excluded) | 8 hours |

Line 310 establishes that the Run remains `ACTIVE` while Attempts are `WAITING_APPROVAL`. Line 312 describes orphaned phases after a crash with no live runtime.

### The ambiguity

"Human-wait and paused states excluded" identifies two exclusions but does not define the accounting. Billing by lifecycle wall clock and billing only the intervals during which runtime ownership is held produce different stop moments, and they differ sharply across a crash: lifecycle wall clock charges the entire dead interval, ownership-interval accounting charges none of it. The current text also does not say which transaction starts or stops accumulation, what happens to an interval interrupted by a crash, or which clock is authoritative.

The M1-R1 plan has only a DTO and synthetic threshold tests — no trusted clock, no persistent accumulation, no production check. That is a consequence of the gap, not an oversight.

### Proposed text

Replace line 485's table row label with:

> | Cumulative active Run time, accumulated only while runtime ownership is held | 8 hours |

and append to section 10.2, immediately after the Gap 2 text:

> Active Run time accumulates only across intervals during which one process holds runtime ownership for that Run. An interval opens in the same transaction that consumes a Runtime Permit, installs the owner ID, and increments runtime progress; it closes in the transaction that records the resulting `RunStop`. States with no runtime owner therefore contribute nothing, which is why human-wait and paused states are excluded, and `DRAFT`, `READY_TO_START`, every approval wait, `PAUSED`, `INDETERMINATE` awaiting a human strategy, and terminal states accumulate no time even though some remain lifecycle-`ACTIVE`.
>
> Duration is measured with a monotonic clock supplied by the clock adapter, never with wall-clock differences, so host clock adjustment can neither inflate nor reverse an interval. Each closed interval's duration is added to a durable cumulative total in the same transaction that records its `RunStop`.
>
> If the process dies without recording a `RunStop`, that interval has no observed end. Recovery closes it at the timestamp of the last Audit Event committed under that ownership generation and adds only that bounded portion; the unobservable remainder is never charged and never guessed. A subsequent permitted invocation opens a new interval. The ceiling is evaluated at each action boundary against the durable cumulative total plus the open interval's elapsed monotonic duration; reaching it lets the current atomic action settle and then pauses under the existing section 10.2 stop rule.

### Impact

- Makes the measure objectively observable from the journal rather than from wall-clock reasoning about lifecycle states.
- Makes crash accounting deterministic and conservative — an unobservable interval is never charged, consistent with the specification's existing refusal to guess unobservable outcomes.
- Requires the M1-R2 plan to add real work the M1-R1 plan did not have: a monotonic clock adapter behind a port, a durable cumulative field, interval open/close inside the existing permit-consumption and `RunStop` transactions, recovery closure from the last Audit Event, and an action-boundary check. Estimate 1.5-3 hours across Tasks 9/9A and the runtime tasks.
- Closes review blocker 3.

---

## What approval means

Approving this proposal authorizes exactly: applying the text above to `SPEC.md`, recomputing its SHA-256, and recording the new digest as an approved revision. It authorizes nothing else — no implementation, no `M1 GO`, no push, no publication.

After application the sequence is: M1-R2 plan revision correcting review blockers 4-10 and adding the Gap 3 work, then a further independent review returning zero blockers, then an explicit owner `M1 GO`.

## Owner decision

- [ ] Approve all three gaps as written
- [ ] Approve with the broad reading substituted for Gap 1
- [ ] Approve individual gaps: ______
- [ ] Reject and request revision
