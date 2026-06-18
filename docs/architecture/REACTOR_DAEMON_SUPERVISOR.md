# Sovereign Lifecycle Daemon Supervisor (reactor-core Soul)

> **Status:** IMPLEMENTED + live-verified. 2026-06-18. `backend/core/reactor_daemon_supervisor.py`.

Programmatic, in-process, resource-aware supervisor for the reactor-core control plane
(`run_reactor.py` on :8090) — no external launchers (launchd/systemd), no static plists. Keeps the
Trinity Soul online for the life of the JARVIS control plane and tears it down cleanly with it.

## Phase 1 — Asynchronous subprocess daemonization
`asyncio.create_subprocess_exec` launches `run_reactor.py --port 8090` in its own session (detached),
draining the child's merged stdout/stderr line-by-line through an async task into a **size-rotated**
structured log (`logs/reactor_daemon.log`, `RotatingFileHandler`, `PYTHONUNBUFFERED=1`) — the primary
terminal plane stays pristine. `start()` adopts an already-serving Soul (no double-spawn) and returns
only once `/health` is up.

## Phase 2 — Signal-driven POSIX boundary & state control
`install_signal_handlers()` registers SIGTERM/SIGHUP/SIGINT on the loop. On any, `stop()` runs the
graceful cascade: **SIGTERM to the child's process group** (the reactor closes its :8090 listener) →
wait `grace_s` → **verify the port is released** → **SIGKILL fallback** on the group. Idempotent +
fail-soft → no orphaned background listener sockets survive a JARVIS shutdown/crash.

## Phase 3 — Adaptive M1 resource profiling & niceness modulation
A supervise loop wires to the existing `MemoryPressureGate`: `pressure()` → nice via
`{ok:0, warn:5, high:10, critical:15}` (env-tunable), applied with `os.setpriority(PRIO_PROCESS, pid,
nice)`. Under HIGH/CRITICAL host memory pressure (e.g. heavy 29k-file Oracle graph traversals) the
background reactor is **deprioritized** so the main control plane keeps headroom; at OK it returns to
full throttle (nice 0). An optional injectable `busy_signal` (e.g. OperationalVelocityScore) composes
strictest-wins. Re-nice is a no-op when the target is unchanged; fail-soft on `setpriority` errors.

## Safety / discipline
- **No external launchers** — fully programmatic via asyncio.
- **Fail-soft** everywhere; `daemon_enabled()` (`JARVIS_REACTOR_DAEMON_ENABLED`, default OFF) gates
  auto-start integration. The class is always importable + invoked explicitly.
- **No duplication** — reuses `run_reactor.py` (reactor's canonical launcher) + `MemoryPressureGate`.

## Live verification
`start()` → healthy on :8090 (logs rotating) → `apply_adaptive_nice()` → 10 (memory HIGH) →
`stop()` → port released clean. 16 unit tests (nice mapping, adaptive compose, setpriority dispatch +
fail-soft, log rotation config, signal registration, gating).
