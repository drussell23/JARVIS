# Golden Image Readiness Protocol Fix

**Date**: 2026-03-21
**Status**: Design
**Scope**: Cross-repo (JARVIS-AI-Agent, jarvis-prime)

## Problem Statement

Golden image boot verification gets stuck at `verifying_attempt_7` (94%) with ETA 0s. The root cause is a circular readiness deadlock where J-Prime's health endpoint reads stale APARS data that overrides its internal readiness signal. This cascades into PrimeRouter flapping, VBI health degradation, email fetch timeouts, and voice synthesis failures.

## Causal Chain

```
APARS file has ready_for_inference=false (written by startup script every 2s)
    |
    v
J-Prime server.py reads APARS, OVERRIDES internal ready_for_inference=true -> false
    |
    v
Supervisor _ping_health_endpoint sees ready_for_inference=false
    |                                    |
    v                                    v
Startup script health check         Supervisor falls through to
catches via status=="healthy"        status+phase fallback (adds latency)
(works, but masks the bug)                |
    |                                     v
    v                              Hysteresis: 3x READY at 5s = 15s penalty
GCP endpoint appears flaky               |
    |                                     v
    v                              GCP readiness delayed 30-45s
PrimeRouter.demote_gcp_endpoint()
blocked by flapping protection (0.0s < 30s)
    |
    v
VBIHealthMonitor: healthy -> degraded
    |
    v
Email fetch timeout (resource contention during degraded state)
```

Separately, `safe_say` exits 1 because voice "Daniel" may not be installed on macOS 25.3.

## Root Causes

### RC-1: APARS Readiness Override in J-Prime (PRIMARY)
**File**: `jarvis-prime/jarvis_prime/server.py:1416-1421`

J-Prime's `/health` handler reads the APARS progress file and overrides top-level `ready_for_inference` and `model_loaded` with APARS values. During the startup script's phase 6 health check loop, `update_apars` writes `ready_for_inference=false` (default arg) every 2 seconds. Even when J-Prime's model finishes loading (`phase="ready"`), the APARS override sets `ready_for_inference=false` in the health response.

The ASGI enrichment middleware (INV-1/INV-2 comments at line 10080-10083) correctly avoids this override, but it never activates because J-Prime's fast-startup pattern (`app=None` at import time) causes the launcher to fall back to `python -m jarvis_prime.server` directly.

### RC-2: No ETA in APARS Progress File
The `update_apars()` bash function doesn't write `eta_seconds`. The APARS stub HTTP handler computed ETA on-the-fly, but it's killed before phase 6. J-Prime passes raw APARS data without ETA, so the supervisor sees ETA=0s.

### RC-3: Sequential Golden Image Phases
Code validation (phase 4, ~30s) runs before model loading (phase 5). On a golden image where everything is pre-baked, validation could run concurrently with J-Prime startup, saving 30+ seconds.

### RC-4: Rigid Hysteresis
`readiness_hysteresis_up=3` at 5-second poll intervals adds 15 seconds of unnecessary delay after J-Prime is already ready. Golden image deployments have reliable readiness signals that don't need this protection.

### RC-5: Wrong Timeout Profile
Startup script uses `GCP_SERVICE_HEALTH_TIMEOUT=90` (production default). The config defines a `golden_image` profile at 120s, but golden image boots don't auto-select it.

### RC-6: safe_say Voice Availability
`safe_say()` defaults to voice "Daniel" which may not be installed on macOS 25.3 (Darwin Tahoe). Exit code 1 with no fallback.

## Design

### Fix 1: J-Prime Health — APARS is Metadata Only (RC-1)

**File**: `jarvis-prime/jarvis_prime/server.py`

Remove lines 1416-1421 where APARS overrides `ready_for_inference` and `model_loaded`. APARS data is still attached as `result["apars"]` for observability, but the top-level readiness fields come solely from J-Prime's internal state.

The comment block should reference INV-3: J-Prime is the single source of truth for readiness. APARS is observational metadata only. The APARS file can be stale (written by startup script, not J-Prime) and must NEVER override live readiness signals.

### Fix 2: APARS Progress File — Add ETA (RC-2)

**File**: `gcp_vm_manager.py` (startup script `update_apars()` function)

Add phase-aware ETA calculation directly in the `update_apars()` bash function. Write `eta_seconds` to the JSON so J-Prime can propagate it. Use the same phase duration table as the APARS stub (Phase 0: 5s, 1: 20s, 4: 30s, 5: 120s, 6: 30s = 205s total). Compute remaining time based on current phase + phase_progress.

### Fix 3: Parallel Golden Image Startup (RC-3)

**File**: `gcp_vm_manager.py` (golden image startup script, phases 4-5)

Restructure to start J-Prime immediately after environment loading (phase 1), then run validation concurrently:

```
CURRENT (sequential):
  Phase 1: Load env (20s)
  Phase 4: Validate code + model (30s)
  Phase 5: Start J-Prime + model loading (60s)
  Phase 6: Health verify (variable)
  Total: ~110s + verify

NEW (parallel):
  Phase 1: Load env (20s)
  Phase 5: Start J-Prime (background, begins model loading immediately)
  Phase 4: Validate code + model (30s, runs concurrently with J-Prime startup)
  Phase 6: Health verify (model likely already loaded during phase 4)
  Total: ~50s + verify (saved ~30s from parallel validation)
```

Implementation:
- After phase 1 env loading, kill APARS stub and start J-Prime immediately
- Run validation checks while J-Prime loads the model
- If validation fails, kill J-Prime and report error
- Phase 6 health check starts after validation passes (J-Prime has had 30s head start)

### Fix 4: Adaptive Hysteresis for Golden Image (RC-4)

**File**: `gcp_vm_manager.py` (`_poll_health_until_ready`)

When APARS data in the health response shows `deployment_mode=golden_image`:
- Use `readiness_hysteresis_up=1` (single READY verdict suffices)
- Golden images have reliable readiness signals (pre-baked deps, known model)
- Fall back to standard hysteresis (3) for non-golden deployments
- Configurable via `GCP_GOLDEN_HYSTERESIS_UP` env var (default 1)

### Fix 5: Auto-Select Golden Image Timeout (RC-5)

**File**: `gcp_vm_manager.py` (golden image startup script, phase 6)

Use golden_image timeout profile (120s) when running on a golden image. Change the default from 90 to 120 in the golden image startup script only. The non-golden startup script retains 90.

### Fix 6: safe_say Voice Availability + Fallback (RC-6)

**File**: `backend/core/supervisor/unified_voice_orchestrator.py`

Add voice availability detection with graceful fallback:
- Define a voice fallback chain: ["Daniel", "Samantha", "Alex", "Fred"]
- On first `safe_say` call, validate the preferred voice by running `say -v <voice> ""` and checking exit code
- Cache the validated voice for the session
- If preferred voice fails, walk the chain until one works
- Also: grep for `safe_say(` calls missing `source=` parameter and add appropriate tags

## Files Changed

| Repo | File | Change |
|------|------|--------|
| jarvis-prime | `jarvis_prime/server.py` | Remove APARS readiness override (lines 1416-1421) |
| JARVIS-AI-Agent | `backend/core/gcp_vm_manager.py` | Startup script: parallel phases, ETA in APARS, golden timeout |
| JARVIS-AI-Agent | `backend/core/gcp_vm_manager.py` | `_poll_health_until_ready`: adaptive hysteresis |
| JARVIS-AI-Agent | `backend/core/supervisor/unified_voice_orchestrator.py` | Voice fallback chain |

## Testing Strategy

1. **Unit**: Mock J-Prime health response with APARS data, verify `ready_for_inference` comes from internal state not APARS
2. **Unit**: Verify `_poll_health_until_ready` uses hysteresis=1 when `deployment_mode=golden_image`
3. **Unit**: Verify `safe_say` falls back to available voice when primary voice exits 1
4. **Contract**: Existing `test_gcp_vm_startup_script_contract.py` validates APARS JSON schema (add `eta_seconds`)
5. **Integration**: Boot golden image VM, verify readiness in less than 60s (was ~90-120s)

## Expected Impact

- **Boot time**: ~60s reduction (30s from parallel phases + 15s from reduced hysteresis + 15s from eliminated readiness deadlock)
- **Flapping elimination**: PrimeRouter won't see false unhealthy signals
- **VBI stability**: No more healthy->degraded transitions from GCP readiness delays
- **Progress accuracy**: ETA in APARS file gives real estimates instead of 0s
- **Voice reliability**: safe_say auto-discovers available voice instead of failing silently
