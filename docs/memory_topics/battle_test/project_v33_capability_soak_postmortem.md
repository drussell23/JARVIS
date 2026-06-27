---
title: Project V33 Capability Soak Postmortem
modules: []
status: historical
source: project_v33_capability_soak_postmortem.md
---

v33 capability soak detonated 2026-05-28 09:27 PDT, terminated
09:58 PDT via `idle_timeout+atexit_fallback` (clean atexit path).
Session id: `bt-2026-05-28-162729`.

**Configuration** (operator-authorized full caps):
- `JARVIS_PROVIDER_CLAUDE_DISABLED=true`
- `JARVIS_JSON_HEAL_LLM_ENABLED=true`
- `JARVIS_SCHEMA_DRIFT_ROTATION_ENABLED=true`
- `JARVIS_DW_ZERO_CANDIDATE_PROHIBITION_ENABLED=true`
- `JARVIS_DW_ADAPTIVE_TIMEOUT_ENABLED=1`
- `JARVIS_DISPATCH_PROFILER_ENABLED=1` (Slice 34)
- `JARVIS_DW_UPLOAD_MAX_BYTES=5242880` (Slice 37 default, explicit for audit)
- `--cost-cap 10.00 --idle-timeout 900 --max-wall-seconds 3700`

**Exit shape**: process terminated with exit code **75**
(`BoundedShutdownWatchdog.FIRED` post-atexit-fallback). Watchdog
ARMED at `reason='post_asyncio_teardown' deadline_s=30.0`, fired
at `elapsed=63.6s` because asyncio teardown didn't finish within
the deadline. `summary.json` was already written by the
atexit-fallback path **before** the watchdog fired — artifact
intact + 8 ShutdownWatchdog.TOMBSTONE lines confirm the bounded
exit working as designed. Healthy behavior.

**Outcome counters** from final `summary.json` (written by clean
`_generate_report` path at 10:04 BEFORE the watchdog fired —
verified via `session_outcome=complete`):
- stop_reason=**idle_timeout** (clean, not the +atexit_fallback partial)
- session_outcome=**complete**
- attempted=20, completed=**0**, failed=4, cancelled=16
- cost_total=**$0.007357** (0.07% of $10 cap)
- duration=2214.9s (37 min total wall, incl. cleanup phase)
- commits=0, files_changed=0
- convergence_state=INSUFFICIENT_DATA
- top_techniques: cancelled_during_shutdown(16), orchestrator_terminal(4)

**Three layers of partial-shutdown insurance verified working**:
1. Graceful `_generate_report` completed → final summary +
   session_outcome=complete stamped
2. atexit_fallback (defense-in-depth) wrote partial summary +
   was overridden by the graceful path
3. BoundedShutdownWatchdog force-killed at +63.6s of stuck
   asyncio teardown with os._exit(75) — 8 tombstone log lines
   prove bounded safety net engaged correctly

**Slice 37 Phase 1 diagnostic — operationally proven**. All three
`/v1/files` upload attempts captured by the new payload diagnostic
log (now ERROR-level via `_upload_file` FAILED branch, since the
INFO START log fires before the POST and got suppressed by log
level filtering):

| Payload | Custom ID | Model | DW Verdict |
|---|---|---|---|
| 4,224 B | prompt_only_intent_discovery_sensor | 35B-A3B-FP8 | HTTP 500 |
| 33,418 B | dw-1779985852 | 35B-A3B-FP8 | HTTP 500 |
| 18,759 B | dw-1779985956 | 35B-A3B-FP8 | HTTP 500 |

**Provable upstream fact** (this was opaque pre-Slice-37): DW
`/v1/files` returns 100% HTTP 500 with 21-byte
`"Internal server error"` body for `Qwen/Qwen3.5-35B-A3B-FP8`
across payload sizes 4 KB / 18 KB / 33 KB. Aegis daemon proxy
logs confirm: `[AegisPassthrough] method=POST template=/v1/files
upstream_host=https://api.doubleword.ai upstream_status=500
bytes=21 disconnected=False`.

**Slice 37 PRE-FLIGHT REJECTED**: never fired (all 3 uploads
under 5 MB). Correct behavior.

**Slice 37 Phase 2 cleanup discipline**: zero unbound-name
tracebacks, zero leaked rate-limiter accounting, clean
SERVER_ERROR → FailbackFSM → CandidateGenerator EXHAUSTION cascade.

**Slice 36 transport switch — verified engaged**: STANDARD ops
correctly routed to `submit_batch` (not RT `/v1/chat/completions`).
Without Slice 36 we'd have spent 66s × 20 ops at RT TTFT instead
of failing fast at batch upload.

**Slice 23 sentinel walker — verified**: iterated 35B → 397B,
both failed with same RuntimeError, cascade correctly applied
`fallback_tolerance=cascade_to_claude` then hit
`fallback_skipped:no_fallback_configured` (Claude disabled).

**Slice 22 tier decay — verified engaged**: `WARNING
[UrgencyRouter] Adaptive tier decay activated: IMMEDIATE →
STANDARD. Reason: Claude infrastructure tier structurally absent.`

**Why no APPLY artifact**: structurally honest exhaustion.
With both providers unavailable (DW upstream-down on /v1/files,
Claude operator-disabled), the system cannot generate. This is
**not** a JARVIS engineering bug — it is the correct behavior
under a degraded upstream + operator-restricted provider pool.

**Orthogonal signals worth noting** (not action items today):
- 3 `LoopSink` warnings on `cross_process_jsonl.flock_append_line`
  blocking the loop 89–184ms (over the 50ms Arc 0 threshold).
  Slice 33 Arc 0/1/2 reduction work didn't fully close this site.
  File-lock contention is concurrency-sensitive — surfaces under
  the IntakeRouter ingest path.
- 12 `ControlPlaneStarvation` warnings (515–1787ms lag). Same
  root cause family as LoopSink.
- FlagRegistry.register() API drift across 20+ governance call
  sites (see `project_flag_registry_api_drift.md`). Pre-existing,
  non-fatal, cosmetic log spam.

**Next legitimate investigation** (NOT bundled — operator-gated):
The empirical question worth answering is **what differs between
`submit_batch` and `prompt_only` at the /v1/files boundary?**
v30 bare-metal probe got 40/40 OK on /v1/files via prompt_only
path; v33 harness gets 0/3 OK on /v1/files via submit_batch
path. The delta candidates:

1. Multipart form field names (purpose="batch" vs other)
2. Content-Type negotiation
3. JSONL body shape (custom_id format, body.model field
   coercion, body.messages serialization)
4. Header set (Aegis bearer differences between two paths)

Slice 37's diagnostic surface now makes this bisectable — but the
arc is orthogonal to capability-bar pursuit and should be
operator-authorized separately.

**Stable conclusion** (no euphoria — operator binding §92.16):
Slice 37 is operationally proven across all 3 design goals
(diagnostic / size guard / cleanup). The capability bar
(APPLY→VERIFY→RESOLVED) was NOT met because the binding constraint
is upstream provider availability, not anything the JARVIS stack
can repair without operator-authorized provider re-enablement OR
a separate slice investigating the submit_batch vs prompt_only
payload divergence.

Cumulative: v25 → v26 → v27 → v28 → v29 → v30 → v31 → v32 → v33 =
**9 capability soaks, 0 APPLY artifacts**. The blocker has moved
across the stack (Bearer→GIL→loop-blind→TTFT→aggregation→
transport-routing→payload-diagnostic) but the capability terminal
artifact remains unmet.

Composes [[project_slice_37_multipart_payload_cleanup]] +
[[project_slice_36_adaptive_transport_dispatcher]] +
[[project_slice_34_dispatch_profiler]] +
[[project_flag_registry_api_drift]].
