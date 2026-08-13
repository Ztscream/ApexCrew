---
status: APPROVED AND APPLIED
date: 2026-08-05
approved: 2026-08-05, owner approved the provider replacement and authorized application to SPEC.md
applied: SPEC.md revision 2 `97E9652D…E26D6` -> revision 3 `E4385008CD75E4E3B0E70B25A6EBDFD976F3E1031F2ACD81FF0B6284EF6668AB`
supersedes: nothing
affects: SPEC.md sections 2, 3, 4, 10.1, 10.2, 12
---

> **Disposition**: the owner approved the provider replacement and authorized the `SPEC.md` edit on 2026-08-05. The text below was applied verbatim, producing `SPEC.md` revision 3 at SHA-256 `E4385008CD75E4E3B0E70B25A6EBDFD976F3E1031F2ACD81FF0B6284EF6668AB`, 131,813 bytes, 636 lines. The applied diff was 7 insertions and 7 deletions confined to the seven lines quoted here, and the line count is unchanged so that every existing line-number citation remains valid. The pre-amendment bytes remain identifiable by revision 2's digest.

# Proposal 0002: Replace the frozen model provider with DeepSeek `deepseek-v4-flash`

## Status and authority

`SPEC.md` was frozen and byte-unchanged at SHA-256 `97E9652D874B606C1673867923C97C29834F63B43ADB3F3E89779B13183E26D6`, 131,011 bytes, 636 lines, when this proposal was written.

The owner requested the provider change and pre-approved the edit. Per the standing constraint that `SPEC.md` is never edited without an owner-approved proposal producing a new digest, this document is that proposal: it exists so the change has a citable rationale and a recorded before/after digest, not to re-litigate a decision the owner has already made.

## Why this exists

The specification names OpenAI and `gpt-5.6-terra` in **seven** normative places. Provider identity is therefore not a configuration value in this system — it is frozen text, and no Model Configuration or Budget Revision can reach it. Section 10.1 line 469 permits adding a *dated or provider alias* of the approved model to a new Model Configuration in-band, but `deepseek-v4-flash` is a different vendor, not an alias, and lines 25, 137, and 569 name OpenAI as the provider outright. Switching vendors is consequently a specification revision or it is nothing.

Two facts make the change cheap and one makes it safer than the status quo.

**It is cheap.** DeepSeek serves a Responses API, and `deepseek-v4-flash` is currently the only model on it. The mechanism contract in line 469 — single completion, no internal retries, no internal tool loop, provider-side storage off, recorded requested/returned model IDs, response ID, usage, inference parameters, and tool-schema digest — is satisfiable without loosening any clause. The `ModelPort` protocol, the two-transaction reserve/settle protocol of lines 196 and 198, and the entire `src/` tree are unaffected: no source file hardcodes a model ID or a price, because both already travel as data through `allowed_model_ids` and `BudgetRevisionDocument.pricing_entries`. The declared `openai>=1.0,<2` dependency is retained and re-pointed by base URL, so no dependency is added.

**It is safer on cost.** Against the section 10.2 token ceilings the worst-case reservation falls from USD 8.00 to USD 0.672 even at the provider's *peak-hour* rate. The blast radius of a live smoke shrinks by more than an order of magnitude, which is the difference between a provider activation that needs careful staging and one that does not.

**It introduces one new hazard, and closing it is the third change below.** DeepSeek silently ignores unsupported request parameters rather than rejecting them. For a fail-closed kernel this is materially dangerous: a safety-bearing parameter can be dropped in transit with no error, and the caller would never learn that the guarantee it thought it purchased was never applied.

---

## Change 1 — Provider identity

### Current text

Line 25:

> - A CLI-only command surface, a read-only local WebUI, a sanitized static public demonstration, and OpenAI Responses API plus `ScriptedMockLLM` adapters behind `ModelPort`.

Line 61:

>     MODELREQ --> MODEL["ModelPort: Scripted / OpenAI Responses"]

Line 137:

> `ModelPort` is the true-external seam with OpenAI and `ScriptedMockLLM` adapters.

Line 469:

> The sole real v0.1 adapter uses the OpenAI Responses API with `gpt-5.6-terra`.

and

> The initial configuration accepts only the exact returned ID `gpt-5.6-terra`; a dated/provider alias must be added explicitly to a new approved Model Configuration and priced in the Budget snapshot before its output is usable.

Line 473:

> The first attempt to select the OpenAI adapter without a credential stops before any request

and

> Headless CI MAY supply `APEXCREW_OPENAI_API_KEY` through its secret store

Line 569:

> The low-level OpenAI Responses adapter supplies structured single completions and usage metadata without importing an agent loop, while `gpt-5.6-terra` is the budget-aligned general coding choice behind a provider-independent `ModelPort`.

### Proposed text

Substitute, in place, without changing any line's position:

- Line 25: `OpenAI Responses API` becomes `DeepSeek Responses API`.
- Line 61: `ModelPort: Scripted / OpenAI Responses` becomes `ModelPort: Scripted / DeepSeek Responses`.
- Line 137: `the true-external seam with OpenAI and` becomes `the true-external seam with DeepSeek and`.
- Line 469: `uses the OpenAI Responses API with \`gpt-5.6-terra\`` becomes `uses the DeepSeek Responses API with \`deepseek-v4-flash\`, reached through an OpenAI-compatible client pinned to the DeepSeek base URL`; and `accepts only the exact returned ID \`gpt-5.6-terra\`` becomes `accepts only the exact returned ID \`deepseek-v4-flash\``.
- Line 473: `select the OpenAI adapter` becomes `select the DeepSeek adapter`; `APEXCREW_OPENAI_API_KEY` becomes `APEXCREW_DEEPSEEK_API_KEY`.
- Line 569: `The low-level OpenAI Responses adapter` becomes `The low-level DeepSeek Responses adapter`; `while \`gpt-5.6-terra\` is the budget-aligned` becomes `while \`deepseek-v4-flash\` is the budget-aligned`.

### Impact

- The exact-returned-ID allowlist keeps exactly one member, so the `RETURNED_MODEL_MISMATCH` machinery of line 198 is unchanged in force. The provider publishes a dated build (`DeepSeek-V4-Flash-0731`) while stating the calling method is unchanged; if the API ever returns that dated string instead of `deepseek-v4-flash`, line 469 already governs — it is a mismatch, it pauses, and it requires a new approved Model Configuration plus a Budget price mapping before its output is usable. This proposal deliberately does not pre-authorize the dated alias.
- No `src/` change is required by this change alone. Test fixtures carrying the literal `gpt-5.6-terra` are sample data and are updated for coherence, not correctness.

---

## Change 2 — Pricing snapshot

### Current text

Line 493:

> The pricing snapshot maps every approved returned model ID to the 2026-07-26 standard `gpt-5.6-terra` rates observed during design: USD 2.50 per million input tokens and USD 15 per million output tokens. The initial exact-ID set has one member. The token ceilings therefore reserve USD 8 without assuming cached-input discounts and leave USD 2 headroom.

### The problem beyond the rate change

DeepSeek publishes a planned peak-hour multiplier of 2x for 09:00–12:00 and 14:00–18:00 Beijing time, with the activation date pending announcement. A snapshot pinned at the standard rate would under-reserve any Run crossing a peak boundary — the reservation would be arithmetically correct at the moment it was taken and wrong by the time the call was billed. Line 493 already pauses a Run on *increased provider pricing*, but that control fires on an observed price change, not on a scheduled intraday one, so it does not close this gap by itself.

The model also has a Thinking mode whose reasoning tokens are reported under `output_tokens_details.reasoning_tokens` and billed as output. Reservation arithmetic that counted only visible completion tokens would understate both the output ceiling and the cost.

### Proposed text

Replace the quoted portion of line 493 with:

> The pricing snapshot maps every approved returned model ID to the 2026-08-05 `deepseek-v4-flash` peak-hour rates observed during design: USD 0.28 per million input tokens and USD 0.56 per million output tokens. The snapshot is pinned at the provider's peak-hour rate rather than its standard rate, so published time-of-day pricing variation can never under-reserve a Run that crosses a peak boundary. Provider-reported reasoning tokens are output tokens for both the output ceiling and cost. The initial exact-ID set has one member. The token ceilings therefore reserve USD 0.672 without assuming cached-input discounts and leave USD 9.328 headroom.

The remainder of line 493 — worst-case reservation before every real call, the pause conditions, and the Budget Revision rules and non-raiseable maxima — is unchanged.

### Impact

- Worst-case Run reservation falls from USD 8.00 to USD 0.672. The USD 10 table maximum is left untouched, so an operative cost reserve of USD 1 can be set by an in-band Budget Revision without any further specification change; this proposal recommends that and does not mandate it.
- The cached-input discount remains explicitly unclaimed. DeepSeek's cache-hit input rate is roughly fiftyfold cheaper than cache-miss, which makes conservative reservation cheap rather than costly.
- Reasoning tokens become explicitly chargeable, closing an accounting gap that the previous provider's profile did not present.

---

## Change 3 — Silently ignored request parameters

### Current text

Line 469 requires that requests "set provider-side storage off when the API supports it" and that the adapter record provenance, but it assumes a provider that rejects what it does not support.

### The problem

DeepSeek's Responses API documents that unsupported parameters are **silently ignored and do not cause errors**, and that several fields always take fixed values regardless of the request — `store` is always `false`, `previous_response_id` always `null`, `parallel_tool_calls` always `true`, with `max_tool_calls`, `truncation`, `metadata`, and `prompt_cache_key` among those ignored.

For `store` this is harmless and in fact stronger than the specification demands: provider-side storage is not merely disabled, it is unavailable. The danger is general rather than specific. A fail-closed kernel that expresses any safety property as a request parameter would, against this provider, believe it had constrained a call that was in fact unconstrained, and would receive no signal of the difference. The correct rule is that request parameters are advisory and only the observed response is evidence.

A second, narrower uncertainty belongs here. The provider's Responses API reference lists `text.format` values of `text`, `json_object`, and `json_schema`, while its Responses API guide does not mention schema-constrained output at all. The two documents disagree. The rule below makes the disagreement harmless: whichever is true, a payload that does not conform to the typed action schema is a closed failure rather than something to parse leniently.

### Proposed text

Insert into line 469, immediately before the sentence beginning "The initial configuration accepts only":

> Because this provider silently ignores unsupported request parameters instead of rejecting them, no safety property may rest on a request parameter alone: the adapter derives every settlement input from the observed response, and a response whose completion status, exact returned model ID, usage object, or schema-conformant payload is absent or unexpected is a closed failure that releases no output.

### Impact

- Makes the adapter's obligation to verify, rather than to assume, normative instead of a matter of implementation taste.
- Turns the `text.format` documentation conflict from a blocker into a test: the adapter fails closed on a non-conformant payload either way, so the live smoke resolves the question empirically without risking a lenient-parsing path reaching the Worker loop.
- Adds one required test per settlement input to the M4 adapter task.

---

## What approval means

Approving this proposal authorizes exactly: applying the text above to `SPEC.md`, recomputing its SHA-256, and recording the new digest as approved specification revision 3 in `SPEC_PROCESS.md`, `README.md`, and `AGENT_LOG.md`.

It authorizes nothing else. In particular it does not authorize a live provider call. Section 10.1's credential rules and the `PLAN.md` line 359 requirement of a separately authorized smoke both remain in force, and the credential boundary they presuppose does not exist yet.

## Owner decision

- [x] Approve the provider replacement as written — recorded 2026-08-05
- [ ] Approve identity and pricing but reject Change 3
- [ ] Reject and request revision
