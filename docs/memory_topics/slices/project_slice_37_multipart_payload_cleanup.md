---
title: Project Slice 37 Multipart Payload Cleanup
modules: [tests/governance/test_slice37_multipart_payload_alignment.py]
status: historical
source: project_slice_37_multipart_payload_cleanup.md
---

Slice 37 (Multipart Payload Diagnostic + Async Exception Clean-Room
Isolation) MERGED 2026-05-28 — main @ **c2de840edc** (PR #63659).

Closes the v32 wedge: Slice 36 transport switch routed
STANDARD/COMPLEX through batch (1.6s upload vs 66s RT TTFT) but
in-harness `_upload_file` returned HTTP 500 while a same-moment
bare-metal probe got 5/5 OK at /v1/files. Slice 37 ships two
complementary fixes.

**Phase 1 — Multipart Payload Alignment** in `_upload_file`:
- Pre-call payload diagnostic log line: `payload=%d bytes
  custom_id=%s model=%s op=%s` lets operators correlate HTTP 500s
  with payload shape from log greps (no soak re-run needed).
- Pre-flight size guard `JARVIS_DW_UPLOAD_MAX_BYTES` (default 5 MB,
  min 1024) returns `None` + `_last_error_status=413` BEFORE the
  HTTP round-trip — structural fail-fast, not retry workaround.
- Error response body widened 500 → 2000 chars across all four
  batch lifecycle methods.

**Phase 2 — Cleanup Discipline** uniform across all four batch
lifecycle methods (`_upload_file`, `_create_batch`,
`_adaptive_poll_batch` with per-iteration scoping,
`_retrieve_result`):

1. `_aegis_lease = None` BEFORE try block (no unbound name in
   finally on early-throw)
2. `_rate_limiter_recorded` sentinel — finally back-fills metric
   with `_last_error_status` on early-throw; never double-records.
3. Forward-looking `release_call_lease` in finally with
   `(ImportError, AttributeError)` suppression. Composes against
   existing `aegis_provider_bridge` (server-side cap-tracked
   acquire-only today; pattern ready when release helper lands).

**Test surface**: 10 new tests in
`tests/governance/test_slice37_multipart_payload_alignment.py` —
6 Phase 1 AST pins + 2 Phase 2 AST pins (rate-limiter sentinel +
init-before-try across all 4 methods) + 2 spine (env knob + START
log field completeness).

**Regression**: 241/241 green across Slices 11/11b/20A/20B-C/20D/
21/22/23/24/27/28/29/30/31/32/33 Arc 0/1/2/34/36/37 (148 sandboxed
+ 93 process-spawn under sandbox-off).

**Unblocks v33** capability detonation at
`JARVIS_SOAK_COST_CEILING=10.00` / `JARVIS_SOAK_WALL_CAP=3600`:
- Slice 34 dispatch profiler ON (per-stage timings)
- Slice 36 transport switch routing STANDARD/COMPLEX → batch
  under pure-DW
- Slice 37 payload diagnostics + cleanup discipline ironclad

First soak where HTTP 500s land with full payload context AND
structured cleanup guarantees no resource leak across retry
cycles.

Composes [[project_slice_36_adaptive_transport_dispatcher]] +
[[project_slice_34_dispatch_profiler]] +
[[project_slice_31_aegis_session_bearer]].
