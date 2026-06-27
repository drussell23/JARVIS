---
title: Project Json Extractor Hardening
modules: [backend/core/ouroboros/governance/providers.py]
status: historical
source: project_json_extractor_hardening.md
---

Battle test `bt-2026-04-11-065233` burned ~$0.23 with 0 completed ops. Debug log traced 9/10 failures to `claude-api_schema_invalid:json_parse_error` — not a provider cascade bug.

Root causes (two independent bugs, both in `backend/core/ouroboros/governance/providers.py`):

1. **Prose leakage** — lean codegen prompt had a ```json ... ``` fence as the schema example. Claude mimicked the example, wrapping output in fences or emitting reasoning text before the `{`. `_extract_json_block` step 6 fallback returned input unchanged for prose-prefixed content.
2. **Truncated JSON** — `tool_round=yes` calls use `max_tokens=1024` (too tight for `full_content` responses). Claude's stream cut off mid-string inside a Python regex, leaving unbalanced strings + braces + brackets. `_repair_json` step 6 had two independent depth counters which lost LIFO container order (`{[{` closed as `]}}` instead of `}]}`), and never closed dangling strings.

**Why:** `full_content` schema (no unified diffs) + 1024-token tool_round budget is fundamentally fragile. The parser can't fix the budget, but it CAN recover gracefully from the truncation when it happens, and the prompt can stop teaching the model to emit fences.

**How to apply:**
- Fix A: `_build_lean_codegen_prompt` schema_instruction now shows plain indented JSON (no fence) and explicitly forbids ```json wrappers + prose preamble. First char must be `{`.
- Fix B: `_extract_json_block` step 6 fallback strips prose prefix to first `{` and trims trailing stray backticks.
- Fix C: `_repair_json` step 6 uses a STACK (not counters) to track container nesting, closes dangling strings before closing braces, strips dangling backslashes, and pops via `"".join(reversed(stack))` for LIFO correctness.
- Fix D: Claude provider now logs `stop_reason` on non-`end_turn` completions (Manifesto §7) so future truncations can be distinguished from refusals / end-of-turn.

**Verification:** 9/9 smoke tests in `/tmp/claude/test_extract_json_block_hardening.py` (includes round-trip of real parse_failures/ samples). 81/81 existing `test_self_critique.py` + `test_parser_multi_object.py` still green.

**P1 — fixed (Apr 11):** Root cause was `is_tool_round = (round_index > 0)` being set *before* the call in tool_executor.py:3092, but the model decides per-response whether a given round is an intermediate tool-call JSON (~200 tokens) or the final `full_content` candidate (thousands of tokens). Capping at 1024 truncated the terminal round's patch mid-string. **Anthropic and DW both bill on actual output tokens, not `max_tokens`**, so a generous cap on every round costs nothing when intermediate rounds naturally stop short. Fix: removed the `is_tool_round` branch in both `ClaudeProvider._compute_output_budget` (providers.py:4150) and `DoublewordProvider._compute_dynamic_max_tokens` (doubleword_provider.py:511). Flag kept for observability/logging but no longer affects budget. Zero test regressions (3056 passed, same 64 pre-existing failures).

**P2 — fixed (Apr 11):** Battle test bt-2026-04-11-075739 exposed a second root cause masked by the P0 parse failures: `APITimeoutError` / `APIConnectionError` firing 17–36 s into stream calls, before any tokens had arrived. Root cause: our custom `httpx.Timeout` override set `write=30s, pool=10s` — way tighter than Anthropic's own SDK defaults of `Timeout(connect=5, read=600, write=600, pool=600)`. During extended-thinking's silent pre-first-token window, `httpx.WriteTimeout` / `httpx.PoolTimeout` fired, and the Anthropic SDK (`_base_client.py:169`) wraps all `httpx.TimeoutException` subclasses as `APITimeoutError`. Fix: raised write and pool defaults to 600 s to match Anthropic's defaults. `_CLAUDE_HTTP_WRITE_TIMEOUT_S` and `_CLAUDE_HTTP_POOL_TIMEOUT_S` at providers.py:3251.

**Why:** Anthropic's SDK catches all `httpx.TimeoutException` (parent of Read/Connect/Write/Pool timeouts) and raises `APITimeoutError`. Our override values were *less* resilient than the SDK defaults. Verified by inspecting `anthropic._base_client.AsyncAPIClient` source + `httpx.*Timeout.__mro__` containing `TimeoutException`.

**How to apply:** When configuring `httpx.Timeout` for a vendor SDK, first look up the SDK's default timeout and use it as the floor. Tighter values for read timeout only make sense if the app is actively restricting idle time; tightening write/pool is rarely correct because they govern initial upload + connection-pool acquisition, which have no cost benefit to restricting.

**Still outstanding (deferred as P2):** `summary.json.attempted` counter only increments on APPLY, so the battle test reports 0 attempted when it actually ran 10 ops that all failed at GENERATE. Reporting gap, not a functional bug.
