---
title: Root cause
modules: [backend/core/ouroboros/governance/aegis_provider_bridge.py, backend/core/ouroboros/governance/doubleword_provider.py]
status: historical
source: project_slice_31_aegis_session_bearer.md
---

PR #59096 squash-merged 2026-05-27 at `f1f62d89ab`. Branch `ouroboros/slice-31-aegis-generation-auth-bridge`. Closes v24 (`bt-2026-05-27-183704`) wedge: every outbound DW HTTP call returned `401 missing_session_bearer` from `aegis/passthrough.py:_bearer_session`.

# Root cause

`aegis_provider_bridge.dw_authorization_header()` (legacy sync) returned `{}` when Aegis enabled, on the now-falsified assumption that the daemon injects the bearer server-side. The passthrough extracts `Authorization: Bearer <session_token>` from the *client* request. Every /files upload, /batches POST/GET, /files retrieve, /chat/completions (RT + non-streaming), /models probe died at the gate.

# Slice 31 substrate

New async helper `dw_session_auth_header()` in `aegis_provider_bridge.py`:
- Aegis enabled → `{"Authorization": "Bearer <session_token>"}` via cached `AegisClient._ensure_session_token()` (no daemon round-trip steady-state)
- Aegis disabled → byte-identical legacy DW API key Bearer
- Aegis client error → `{}` (defensive — existing 401 path surfaces real error)

# Wiring (8 sites in `doubleword_provider.py`)

Every lease-acquiring async function now composes the helper BEFORE acquiring its per-call lease. AST pin enforces invariant `lease_acquire(f) → session_bearer_compose(f)` for every f.

1. RT streaming chat completions (~L1935)
2. Non-streaming chat completions (~L2270)
3. `_upload_file` multipart (~L2634 — v24 401 site)
4. `_create_batch` (~L2679)
5. `_await_batch_result` (~L2790)
6. `_retrieve_result` (~L2863)
7. `complete()` sync (~L3240)
8. `health_probe` (~L3348)

Per-call lease (X-JARVIS-Lease, Slice 2B-ii) layers on top via `merge_lease_into_session_headers`. Per-call headers override session-level in aiohttp — session-level `_aegis_dw_auth_header()` returning `{}` is correct; per-call bearer takes precedence.

# Why not session-level

Session bearer has TTL + may rotate. Per-call fetch reads cached state (zero round-trip steady-state) and avoids stale baked-in token surviving across aiohttp reconnects.

# Verification

11/11 Slice 31 tests (4 AST + 7 spine). 193/193 Slice 20+ + Aegis-bridge baseline. AST pin walks every async fn in `doubleword_provider.py` — any future lease-acquiring fn without bearer compose fails CI.

# v25 expected behavior

For the first time the Aegis-routed Slice 28 adaptive 75s heavy-model budget can actually reach DW. v22→v24 every Slice 28 timer was killed by 401 before TTFT — Slice 31 unblocks that. If 397B finally streams to APPLY/VERIFY, the methodology bar (Slices 19→31) earns its capability artifact.

Related: [[project_slice_30_explicit_parameter_threading]] (transport-param ContextVar→explicit), [[project_aegis_zero_trust_arc_closed]] (parent arc — Slice 31 closes coordination gap not visible at Aegis graduation), [[feedback_no_preresult_euphoria]] (Slice 31 = methodology; v25 RESOLVED is capability bar).
