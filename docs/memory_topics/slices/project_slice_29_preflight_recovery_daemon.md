---
title: Architecture — Shape A (Daemon Inside Preflight)
modules: []
status: historical
source: project_slice_29_preflight_recovery_daemon.md
---

PR #59085 squash-merged 2026-05-27 at `909075da00`. Branch `ouroboros/slice-29-preflight-daemon-backoff`. Closes v22 (`bt-2026-05-27-034646`) fragility: terminal halt on transient status=0 transport blip across all 3 trusted DW models.

# Architecture — Shape A (Daemon Inside Preflight)

Operator chose Shape A over Shape B after architectural clarification. `run_boot_preflight` wraps `run_preflight` (with `halt_on_all_fail=False`) in an exponential-backoff polling loop. GLS.start() awaits the entire loop; BG pool/sensors aren't constructed until preflight succeeds — functionally equivalent to "BG pool SUSPENDED" (nothing dispatches because nothing exists yet to dispatch).

# Backoff sequence

- base=30s × 2.0 each cycle, cap 300s
- max_attempts=0 (unbounded; outer harness `--max-wall-seconds` is authoritative)
- 30 → 60 → 120 → 240 → 300 → 300 → ...

5 env knobs (all default-tuned to operator spec):
- `JARVIS_PREFLIGHT_RECOVERY_DAEMON_ENABLED` (master, default TRUE — "un-killable background asset")
- `JARVIS_PREFLIGHT_DAEMON_BASE_BACKOFF_S` (30)
- `JARVIS_PREFLIGHT_DAEMON_BACKOFF_MULTIPLIER` (2.0)
- `JARVIS_PREFLIGHT_DAEMON_MAX_BACKOFF_S` (300)
- `JARVIS_PREFLIGHT_DAEMON_MAX_ATTEMPTS` (0 = unbounded)

# §5 verbatim attestation (AST-pinned)

- **Heartbeat** on every backoff sleep: `[PreflightDaemon] Active provider fleet empty. Entering backoff cycle. Next probe attempt in X seconds.`
- **Recovery** on first ACTIVE: `[PreflightDaemon] Upstream line recovery confirmed. Un-pausing agent pool and executing delayed boot-strap component initialization. attempt=N active=M total=K`

# Backwards compatibility

`JARVIS_PREFLIGHT_RECOVERY_DAEMON_ENABLED=false` → byte-identical pre-Slice-29 fail-fast (`PreflightAllFailedError` raises immediately on all-fail).

# Composition discipline

- No new state — composes existing `run_preflight` with `halt_on_all_fail=False`
- No hardcoding — all 5 thresholds env-tunable
- AST-pinned: 3 pins (substrate symbols / verbatim log messages / dispatch wiring)
- New `_envi()` helper mirrors `_envb/_envf` shape

# Verification

12 tests (3 AST + 9 spine). 297/297 regression (exceeds operator's 285 target).

# v23 status

v23 (bt-2026-05-27-045049, PID 60760) launched 2026-05-27T04:50:43Z. Slice 26 power assertion fires, caffeinate bound. v23 will exercise the daemon's full polling path if all preflight models fail — the boot will WAIT instead of dying.

Related: [[project_slice_25b_preflight_boot_wiring]] (the preflight Slice 29 wraps), [[project_slice_25_preflight_probe]] (substrate), [[feedback_no_preresult_euphoria]] (v23 = architecture proves daemon works; RESOLVED requires DW endpoint recovery).
