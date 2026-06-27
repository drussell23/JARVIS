---
title: Architectural pattern (single canonical seam — AST-pinned)
modules: [backend/core/ouroboros/governance/aegis_provider_bridge.py, backend/core/ouroboros/, backend/core/ouroboros/aegis/upstream_registry.py, backend/core/ouroboros/governance/providers.py, backend/core/ouroboros/governance/doubleword_provider.py, backend/core/ouroboros/claude_fallback.py, backend/core/ouroboros/governance/self_critique.py, backend/core/ouroboros/governance/general_driver.py, backend/core/ouroboros/governance/fast_path_qa.py, backend/core/ouroboros/governance/visual_comprehension.py]
status: historical
source: project_slice_2b_ii_aegis_provider_bridge.md
---

**Slice 2B-ii (Provider Proxy Bridge) MERGED into main 2026-05-24 at `f1c5f1ebb6` via PR #56360.** Closes the consumer-side gap left after Aegis-1 substrate (PR #53861) + Aegis-2B-i forwarding shipped: provider modules used to construct `AsyncAnthropic(api_key=ANTHROPIC_API_KEY)` + `aiohttp.ClientSession(headers={"Authorization": f"Bearer {DOUBLEWORD_API_KEY}"})` directly and POST to api.anthropic.com / api.doubleword.ai. Now (when `JARVIS_AEGIS_ENABLED=true`) all credentialed upstream calls route through Aegis daemon `/v1/*` and the O+V clients/sessions hold NO real keys.

# Architectural pattern (single canonical seam — AST-pinned)

**New module:** `backend/core/ouroboros/governance/aegis_provider_bridge.py` (~268 lines). NOT under `aegis/` by design — the bridge imports the `anthropic` SDK, which the Aegis substrate's `test_ast_pin_no_anthropic_or_openai_sdk_import` correctly forbids under `aegis/` (credential-confiscation invariant: Aegis must NEVER import SDKs that hold real keys). Bridge is a CONSUMER of `aegis.client`, not part of substrate. Caught mid-execution during initial test run — moved before commit.

**Public API (5 callables + 1 constant):**
* `make_async_anthropic_client(*, api_key=None, http_client=None, **kwargs) -> AsyncAnthropic` — when Aegis enabled: `base_url=JARVIS_AEGIS_URL` (host root; SDK appends `/v1/messages` internally → final wire = `{AEGIS}/v1/messages`, NOT `/v1/v1/messages`; this is operator's flagged bug, proven correct by `test_anthropic_request_path_exactly_v1_messages_under_aegis` via httpx.MockTransport capture) + `api_key="aegis-managed-no-real-key-do-not-use"` placeholder. Disabled: byte-identical to direct `AsyncAnthropic(api_key=..., http_client=..., **kwargs)`.
* `dw_aegis_base_url() -> str` — enabled: `f"{JARVIS_AEGIS_URL}/v1"` (the `/v1` suffix is REQUIRED because DW provider composes `f"{base}/chat/completions"` — different from Anthropic SDK which appends paths itself). Disabled: env `DOUBLEWORD_BASE_URL` or `https://api.doubleword.ai/v1` (legacy default kept in sync intentionally).
* `dw_authorization_header() -> Dict[str, str]` — enabled: `{}` (Aegis injects real DW bearer server-side). Disabled: `{"Authorization": f"Bearer {DOUBLEWORD_API_KEY}"}` via private `_compose_bearer()` so the literal stays concentrated in one ~10-char function (AST pin discipline).
* `acquire_call_lease(*, op_id, route, estimated_cost_usd, causal_lineage_hash="") -> Optional[str]` — PER-CALL only (operator correction #4). Returns lease token string when enabled; `None` when disabled (caller skips header). **RAISES `AegisClientError` on lease failure** — no silent fallback to direct upstream credentials (operator correction #5). Proven by `test_lease_acquire_failure_raises_not_falls_back`.
* `merge_lease_header(extra_headers, lease) -> Dict` + `merge_lease_into_session_headers(base_headers, lease) -> Dict` — header composers. `LEASE_HEADER_NAME = "X-JARVIS-Lease"` is canonical single source of truth.

# Consumer rewires — 9 files modified

| File | What changed |
|---|---|
| `providers.py` (+122 lines) | `_ensure_client()` both `stdlib_default` + `custom` AsyncAnthropic constructor branches → `make_async_anthropic_client()`. ContextVars `_aegis_op_id_var` + `_aegis_route_var` set at `ClaudeProvider.generate()` entry (asyncio-task-local — no plumbing op_id through ~20 internal methods). All 6 SDK call sites (5 `messages.create` + 1 `messages.stream`) inject `extra_headers=await _aegis_extra_headers_for_call(...)` as **explicit kwarg** at the call site (AST pin requires literal kwarg, not dict-injection via `**create_kwargs` — refactored after first pin failure). Prefill-retry path gets a FRESH lease (correction #4: never reuse). `health_probe()` + `plan()` set their own synthetic op_id ContextVars.
| `doubleword_provider.py` (+191 lines) | Constructor `base_url: Optional[str] = None` (deferred resolution to instance-construct time, NOT module-import — preflight env must be set first). Session construction: `headers=dict(dw_authorization_header())` (empty when Aegis enabled — operator correction #6 "no real DW key in session"). All 7 credentialed endpoints rewired (3× `/chat/completions`, `/files` POST, `/batches` POST, `/batches/{id}` GET, `/files/{id}/content` GET, `/models` GET). `op_id` threaded through `_upload_file`/`_create_batch`/`_adaptive_poll_batch`/`_await_batch_result` method signatures (default `"dw-batch-*"` synthetic ids for orphan callers). `health_probe()` uses `"dw-health-probe"` synthetic. `complete_sync()` uses `f"dw-complete-sync:{caller_id}"`. `prompt_only` batch path uses `f"dw-prompt-only:{caller_id}:{custom_id}"`.
| `claude_fallback.py` (+19 lines) | `AsyncAnthropic(api_key=api_key)` → `make_async_anthropic_client(api_key=api_key)` + per-call lease bound to `f"claude-fallback:{caller_id}"`.
| `self_critique.py` (+14 lines) | Receives client via injection; only adds per-call lease + `extra_headers=`. Op_id from `getattr(request, 'op_id', '')` fallback to `'unscoped'`.
| `general_driver.py` (+15 lines) | Same pattern as self_critique — uses `sub_id` (already in scope at line 264) for `f"general-driver:{sub_id}"` op_id.
| `fast_path_qa.py` (+18 lines) | `anthropic.AsyncAnthropic(api_key=...)` → `make_async_anthropic_client(api_key=...)` + synthetic op_id `"fast-path-qa"` (read-only Q&A surface).
| `visual_comprehension.py` (+22 lines) | Same factory swap + `"vision-comprehension"` synthetic op_id.
| `m10/bridge_adapters.py` (+16 lines) | Same factory swap + `"m10-synthesis"` synthetic op_id.

# Test surface — 15 tests (5 AST + 9 spine + 1 bonus), all green; 25 Aegis AST pins regression-clean; 167 surrounding governance tests green

**AST pins** (single-seam enforcement):
1. `test_no_raw_async_anthropic_outside_bridge` — walks ALL `.py` under `backend/core/ouroboros/` (skipping `" " in name` backup files + `__pycache__` + tests); asserts NO `AsyncAnthropic(...)` constructor call outside `aegis_provider_bridge.py`. Caught `claude_fallback.py:69` originally missed in initial caller-file list — added.
2. `test_no_raw_dw_authorization_header_outside_bridge` — AST-walks `doubleword_provider.py` for any `ast.JoinedStr` (f-string) whose literals contain `"Bearer "`. Only `dw_authorization_header()` (in bridge) may compose this.
3. `test_every_messages_create_has_extra_headers_kwarg` — walks 7 caller files (providers + 5 aux + claude_fallback); every `.messages.create(...)` Call node must have `extra_headers=` kwarg literal at the call site. **Required refactor of providers.py from `create_kwargs["extra_headers"] = ...; messages.create(**create_kwargs)` → `messages.create(**create_kwargs, extra_headers=...)` because dict-injection doesn't satisfy AST pin** (Call.keywords lookup, not surrounding context).
4. `test_every_messages_stream_has_extra_headers_kwarg` — same for `messages.stream(...)`.
5. `test_every_dw_v1_session_call_has_headers_kwarg` — DW `session.post`/`session.get` to `/v1/*`-style paths. Filter requires receiver to be `session`/`_session` (not `dict.get`/`os.environ.get`) AND first positional arg composes a URL-ish path fragment — without these filters initial run flagged 73 false positives from dict/environ/response `.get()` calls.

**Spine tests** (httpx.MockTransport wire-behavior proof — operator correction #6):
6. `test_anthropic_request_path_exactly_v1_messages_under_aegis` — proves `/v1/v1/messages` bug is absent. Sets `JARVIS_AEGIS_URL=http://aegis-test:9999`, constructs client via factory, captures actual outbound request URL string, asserts `str(req.url) == "http://aegis-test:9999/v1/messages"`.
7. `test_dw_base_url_composes_aegis_root_when_enabled` — proves DW base composes correctly for all 6 endpoint suffixes.
8. `test_lease_header_present_per_call_with_distinct_tokens` — fake `acquire_lease` returns `["lease-A", "lease-B", "lease-C"]`; 3 sequential calls assert headers list is exactly `["lease-A", "lease-B", "lease-C"]`.
9. `test_streaming_path_carries_lease_header` — fake SSE response (`message_start` + `message_stop`), `async with client.messages.stream(...) as stream: async for _ in stream: pass` — asserts captured request carries `X-JARVIS-Lease: stream-lease-7`.
10. `test_aegis_enabled_anthropic_client_holds_placeholder_api_key` — sets `ANTHROPIC_API_KEY="sk-real-anthropic-key-DO-NOT-LEAK"`, asserts `client.api_key != real_key` AND `client.api_key` is non-empty (SDK requirement).
11. `test_aegis_enabled_dw_session_has_no_real_bearer` — asserts `"Authorization" not in auth` AND real DW key doesn't appear in any header value.
12. `test_lease_acquire_failure_raises_not_falls_back` — monkeypatches `AegisClient.acquire_lease` + `AegisClient.get` to raise `AegisClientError("simulated daemon unreachable")`; asserts `await acquire_call_lease(...)` raises (no silent None return).
13. `test_aegis_disabled_yields_byte_identical_legacy_construction` — disabled env, asserts `client.api_key == "sk-legacy-anthropic-key"` AND `"api.anthropic.com" in str(client.base_url)`.
14. `test_aegis_disabled_dw_base_url_matches_legacy` — asserts `dw_aegis_base_url() == "https://api.doubleword.ai/v1"` AND `dw_authorization_header() == {"Authorization": "Bearer sk-legacy-dw-key"}`.
15. `test_acquire_call_lease_returns_none_when_disabled` — bonus: confirms disabled path returns `None` cleanly so callers can skip header injection.

# Operator corrections — all 6 honored with test evidence

| # | Correction | Honored by |
|---|---|---|
| 1 | base_url is host root; SDK appends /v1/messages | Spine test #6 (real httpx URL capture) |
| 2 | `messages.stream(...)` covered alongside `messages.create(...)` | AST pin #4 + spine test #9 |
| 3 | ALL DW endpoints (files/batches/models) routed through Aegis | 7 sites edited + spine test #7 |
| 4 | Per-call lease only, no client-wide injection | ContextVar + per-call helper; prefill-retry gets fresh lease; spine test #8 |
| 5 | Lease failure RAISES, no silent fallback | `acquire_call_lease()` raises AegisClientError; spine test #12 |
| 6 | Tests prove wire behavior, not just import-graph | 9 spine tests with httpx.MockTransport capture (not just module-presence checks) |

# What's NOT in this slice (deferred)

* **Graduating `JARVIS_AEGIS_ENABLED` to default-TRUE** — Slice 2B-iii pending empirical soak proof. Operator's directive grouped Phases 1+2 but I split per `feedback-no-preresult-euphoria` ("graduate only on artifact, not methodology").
* **SSE parsing / streaming buffering / chunk handling** — 0 changes (operator binding "transport-layer swap only").
* **Any modifications to `aegis/*` substrate** — substrate is shipped; we are the consumer.

# How to apply / future slices

* Slice 2B-iii (Aegis-activate / graduation): operator-controlled. Requires (a) live soak with Aegis daemon running + JARVIS_AEGIS_ENABLED=true proving end-to-end O+V cycle completes through Aegis forwarding; (b) `--cost-cap`-bounded confirmation that lease + reservation + cap interactions don't break (the lease is fire-and-forget for streaming per `aegis/client.py:redeem_lease` docstring; SSE forwarder reconciles via usage parser).
* For any NEW caller adding an Anthropic call site: import `make_async_anthropic_client` from `governance.aegis_provider_bridge`, pass `extra_headers=` literal kwarg at every `.messages.create(...)` / `.messages.stream(...)`. AST pins will block direct `AsyncAnthropic(...)` constructions OR missing `extra_headers=` automatically.
* For any NEW DW endpoint: register in `backend/core/ouroboros/aegis/upstream_registry.py` allowlist (Aegis-side), add `session.post`/`get` site with `headers=_aegis_merge_lease_headers(...)` (provider-side). AST pin #5 enforces the kwarg.
* The ContextVar pattern (`_aegis_op_id_var`) is the canonical way to pass op-context without threading — reuse it if you add new entry points; set at the public boundary, read at the call site.

Related arcs: [[project-operator-commit-authority]] (OCA Iron Gate + sovereignty marker workflow used to commit this), Aegis-1 (PR #53861 substrate), Aegis-2B-i (forwarding surface — already in upstream_registry).
