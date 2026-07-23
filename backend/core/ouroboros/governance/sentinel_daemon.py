"""Headless recovery Sentinel — a read-only daemon with ONE SQLite mutation.

Post-#70033, the Sentinel is stripped of ALL git / filesystem-write / subprocess
privileges. Its entire contract:

  1. Deep-probe DW's inference lane at forecaster-predicted intervals (staged
     2-pass verification, injected as ``probe_fn``) — never spams, never
     generates side effects.
  2. On ``stability`` consecutive HEALTHY probes, write ONE row to the
     ``provider_state`` table (``DEGRADED`` → ``HEALTHY`` + timestamp) — its ONLY
     permitted mutation, the Single-Writer handoff.
  3. Self-terminate immediately (freeing the socket pool). The orchestrator —
     the exclusive owner of the git index + ``SagaApplyStrategy`` — polls the
     table and does the fixture-seed + AIMD slow-start launch itself.

Composes the #70030 forecaster (predicted intervals) + ``provider_state`` IPC
(#70021 DB). It imports NO git/os/subprocess machinery. Never raises.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from backend.core.ouroboros.governance.dw_outage_forecaster import (
    forecast_ttr,
    predict_sleep_s,
)
from backend.core.ouroboros.governance.provider_state import (
    mark_degraded,
    mark_healthy,
)

logger = logging.getLogger("Ouroboros.SentinelDaemon")

ProbeFn = Callable[[], Awaitable[bool]]
NowFn = Callable[[], float]
SleepFn = Callable[[float], Awaitable[None]]


@dataclass
class ProbeSignal:
    """Rich probe outcome for the Health Gradient + Pulse cadence. A plain bool
    is still accepted by run_sentinel (legacy); this carries the extra signal."""
    healthy: bool
    pass1_ok: bool = False           # did the lightweight 5-token wake pass?
    ttft_s: Optional[float] = None   # Time-To-First-Token of a successful probe
    error_class: Optional[str] = None


CADENCE_PASSIVE = "PASSIVE"
CADENCE_PULSE = "PULSE_ACCELERATION"


def _normalize_signal(result) -> ProbeSignal:
    if isinstance(result, bool):
        return ProbeSignal(healthy=result, pass1_ok=result)
    return ProbeSignal(
        healthy=bool(getattr(result, "healthy", False)),
        pass1_ok=bool(getattr(result, "pass1_ok", getattr(result, "healthy", False))),
        ttft_s=getattr(result, "ttft_s", None),
        error_class=getattr(result, "error_class", None),
    )


@dataclass
class SentinelResult:
    recovered: bool
    probes: int
    reason: str
    cadence: str = CADENCE_PASSIVE


async def run_sentinel(
    state_conn,
    probe_fn: ProbeFn,
    *,
    provider: str = "doubleword",
    forecast_conn=None,
    now_fn: Optional[NowFn] = None,
    sleep_fn: Optional[SleepFn] = None,
    terminate: Optional[Callable[[], None]] = None,
    max_wait_s: float = 10800.0,
    stability: int = 2,
    adaptive_hysteresis: bool = True,
    jitter_window_s: float = 1800.0,
    jitter_cap: int = 5,
    jitter_now_fn: Optional[NowFn] = None,
    pulse_enabled: bool = True,
    pulse_interval_s: float = 15.0,
) -> SentinelResult:
    """The read-only watch loop. The ONLY writes it performs are the two
    ``provider_state`` upserts (start → DEGRADED, recovery → HEALTHY). It issues
    NO git / os.system / subprocess / file-write call — those belong solely to
    the orchestrator (Single-Writer). On stable HEALTHY it writes HEALTHY and
    calls ``terminate`` (the process then exits).

    Adaptive Hysteresis: the consecutive-pass requirement is recomputed each loop
    as ``stability + jitter_index`` (clamped to ``jitter_cap``) from the
    ``provider_jitter_events`` SQLite window — so a flapping (warm-up-jittering)
    provider must clear a HIGHER bar, and the requirement decays back to
    ``stability`` on its own as errors age out of the window. Never raises."""
    import asyncio as _asyncio

    now = now_fn or time.monotonic
    sleep = sleep_fn or _asyncio.sleep
    jnow = jitter_now_fn or time.time
    fconn = forecast_conn if forecast_conn is not None else state_conn

    def _record_ttft_and_slope(sig: "ProbeSignal") -> Optional[float]:
        try:
            from backend.core.ouroboros.governance.dw_outage_forecaster import (
                record_probe_ttft, ttft_slope,
            )
            if sig.pass1_ok and sig.ttft_s is not None:
                record_probe_ttft(state_conn, provider, sig.ttft_s,
                                  ts=jnow(), window_s=jitter_window_s)
            return ttft_slope(state_conn, provider, window_s=jitter_window_s, now=jnow())
        except Exception:  # noqa: BLE001
            return None

    def _record_jitter(sig: "ProbeSignal") -> None:
        if not sig.error_class:
            return
        try:
            from backend.core.ouroboros.governance.provider_jitter import (
                is_transient_class, record_jitter_event,
            )
            if is_transient_class(sig.error_class):
                record_jitter_event(state_conn, provider, sig.error_class,
                                    ts=jnow(), window_s=jitter_window_s)
        except Exception:  # noqa: BLE001
            pass

    def _required() -> int:
        if not adaptive_hysteresis:
            return stability
        try:
            from backend.core.ouroboros.governance.provider_jitter import (
                required_consecutive_passes,
            )
            return required_consecutive_passes(
                state_conn, provider, base=stability, cap=jitter_cap,
                window_s=jitter_window_s, now=jnow(),
            )
        except Exception:  # noqa: BLE001
            return stability

    t0 = now()
    mark_degraded(state_conn, provider, reason="sentinel_watch_start")
    logger.info(
        "[Sentinel] headless watch START provider=%s forecast_ttr≈%.0fs "
        "base_stability=%d adaptive=%s (read-only; SQLite IPC handoff)",
        provider, forecast_ttr(fconn), stability, adaptive_hysteresis,
    )
    healthy_streak = 0
    probes = 0
    cadence = CADENCE_PASSIVE
    interval = predict_sleep_s(fconn, 0.0)   # initial passive cadence
    while now() - t0 < max_wait_s:
        await sleep(interval)
        probes += 1
        try:
            sig = _normalize_signal(await probe_fn())
        except Exception:  # noqa: BLE001 — a failed probe is just "still down"
            sig = ProbeSignal(healthy=False, pass1_ok=False, error_class="probe_exception")

        # Health Gradient: record TTFT of a successful wake, read the ΔTTFT slope.
        slope = _record_ttft_and_slope(sig)
        required = _required()   # Adaptive Hysteresis — recomputed each loop

        if sig.healthy:
            healthy_streak += 1
            if healthy_streak >= required:
                mark_healthy(
                    state_conn, provider,
                    reason=f"staged_2pass_ok x{required} (jitter-adaptive+pulse, probe #{probes})",
                )
                logger.info(
                    "[Sentinel] %s HEALTHY written after %d consecutive passes "
                    "(required=%d, cadence=%s) — terminating (Single-Writer handoff)",
                    provider, healthy_streak, required, cadence,
                )
                if terminate is not None:
                    terminate()
                return SentinelResult(recovered=True, probes=probes,
                                      reason="healthy_written", cadence=cadence)
        else:
            healthy_streak = 0
            _record_jitter(sig)   # Jitter Deceleration Guard: log the flap

        # ── Pulse Acceleration cadence state machine ──
        # Accelerate on a Pass-1 wake OR a stabilizing gradient (ΔTTFT < 0);
        # decelerate the instant a probe fails (no traffic against a dead lane).
        stabilizing = slope is not None and slope < 0.0
        if pulse_enabled and (sig.pass1_ok or stabilizing):
            if cadence != CADENCE_PULSE:
                logger.info(
                    "[Sentinel] %s → PULSE_ACCELERATION (%.0fs burst; pass1_ok=%s "
                    "ΔTTFT=%s) — catching the stabilization window",
                    provider, pulse_interval_s, sig.pass1_ok,
                    (f"{slope:.4f}" if slope is not None else "n/a"),
                )
            cadence = CADENCE_PULSE
            interval = pulse_interval_s
        else:
            if cadence == CADENCE_PULSE:
                logger.info(
                    "[Sentinel] %s ← decelerate to PASSIVE (probe failed mid-pulse: %s)",
                    provider, sig.error_class or "degraded",
                )
            cadence = CADENCE_PASSIVE
            interval = predict_sleep_s(fconn, now() - t0)

        logger.info(
            "[Sentinel] %s pass=%d/%d cadence=%s next_interval=%.0fs",
            provider, healthy_streak, required, cadence, interval,
        )

    logger.info("[Sentinel] gave up after max_wait_s=%.0f (stayed DEGRADED)", max_wait_s)
    return SentinelResult(recovered=False, probes=probes,
                          reason="max_wait_exceeded", cadence=cadence)


__all__ = [
    "CADENCE_PASSIVE",
    "CADENCE_PULSE",
    "ProbeSignal",
    "SentinelResult",
    "run_sentinel",
    "main",
]


# ---------------------------------------------------------------------------
# Spawnable entry point — ``python -m backend.core.ouroboros.governance.sentinel_daemon``
#
# The Autonomous Supervisor spawns this via ``create_subprocess_exec`` and pipes
# our stdout/stderr into the TUI's unified event router. This reuses the real
# ``dw_deep_probe.staged_health_check`` as ``probe_fn`` and ``run_sentinel``
# verbatim (DRY) — it just adds an OS-process wrapper + stdout logging (the
# supervisor reads those lines). No git / filesystem-write / subprocess egress.
# ---------------------------------------------------------------------------


def _sentinel_log(msg: str) -> None:
    """Emit a structured line to STDOUT (line-buffered) so the supervisor's
    telemetry pump surfaces it in the TUI. Prefixed for the event router."""
    import sys as _sys
    print(f"[sentinel] {msg}", flush=True)
    _sys.stdout.flush()


def _build_staged_probe(model: str):
    """Construct the real DW deep-probe (staged 2-pass) as a ``probe_fn`` — the
    same egress the scratchpad daemon used, now DRY inside the module."""
    from backend.core.ouroboros.governance.dw_deep_probe import staged_health_check

    async def _dispatch(payload):
        import aiohttp
        import os as _os
        key = _os.environ.get("DOUBLEWORD_API_KEY", "")
        if not key and _os.path.exists(".env"):
            for line in open(".env"):
                if line.startswith("DOUBLEWORD_API_KEY="):
                    key = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
        payload = {**payload, "model": model}
        session = aiohttp.ClientSession()
        resp = await session.post(
            "https://api.doubleword.ai/v1/chat/completions", json=payload,
            headers={"Authorization": f"Bearer {key}", "Accept": "text/event-stream"},
            timeout=aiohttp.ClientTimeout(total=None, sock_connect=15),
        )
        if resp.status != 200:
            body = await resp.text()
            await session.close()
            raise RuntimeError(f"HTTP {resp.status}: {body[:100]}")

        async def _readline():
            line = await resp.content.readline()
            if not line:
                resp.release()
                await session.close()
            return line

        return (_readline, resp)

    async def _probe() -> "ProbeSignal":
        try:
            res = await staged_health_check(
                dispatch_fn=_dispatch, model=model, ttft_bound_s=90.0,
                itl_hard_s=8.0, itl_safe_s=2.0, pass2_max_tokens=150,
                pass2_itl_safe_s=3.0,
            )
            p2 = res.pass2.reason if res.pass2 else "skipped"
            _sentinel_log(
                f"probe {'HEALTHY' if res.healthy else 'DEGRADED'} "
                f"stage={res.stage} p1=({res.pass1.reason}) p2=({p2})"
            )
            ttft = None
            if res.pass2 and res.pass2.ttft_s and res.pass2.ttft_s > 0:
                ttft = res.pass2.ttft_s
            elif res.pass1.ttft_s and res.pass1.ttft_s > 0:
                ttft = res.pass1.ttft_s
            err = None
            if not res.healthy:
                err = p2 if (res.pass2 and res.stage == "pass2_degraded") else res.pass1.reason
            return ProbeSignal(healthy=res.healthy, pass1_ok=res.pass1.healthy,
                               ttft_s=ttft, error_class=err)
        except Exception as exc:  # noqa: BLE001 — a failed probe is just "still down"
            _sentinel_log(f"probe error {type(exc).__name__}")
            return ProbeSignal(healthy=False, pass1_ok=False,
                               error_class=type(exc).__name__)

    return _probe


async def main() -> int:
    """Headless recovery watch. Exit 0 on HEALTHY handoff, 2 on max-wait give-up.
    (The supervisor's watchdog reads the exit code + provider_state to decide
    whether an exit was the expected recovery handoff or an unexpected crash.)"""
    import os as _os
    from backend.core.ouroboros.governance.dw_outage_forecaster import open_forecast_db
    from backend.core.ouroboros.governance.provider_state import get_provider_state

    model = _os.environ.get("JARVIS_DW_SURFACE_PROBE_MODEL", "Qwen/Qwen3.5-397B-A17B-FP8")
    max_wait_s = float(_os.environ.get("DW_WATCH_MAX_WAIT_S", "10800"))
    stability = int(_os.environ.get("DW_WATCH_STABILITY", "2"))

    conn = open_forecast_db()
    _sentinel_log(
        f"headless Sentinel START model={model} base_stability={stability} "
        f"max_wait={max_wait_s:.0f}s (SQLite IPC, adaptive hysteresis, zero git/exec)"
    )
    result = await run_sentinel(
        conn, _build_staged_probe(model), provider="doubleword", forecast_conn=conn,
        terminate=lambda: _sentinel_log("HEALTHY written → self-terminating (orchestrator owns launch)"),
        max_wait_s=max_wait_s, stability=stability, adaptive_hysteresis=True,
        pulse_enabled=True, pulse_interval_s=15.0,
    )
    st = get_provider_state(conn, "doubleword")
    _sentinel_log(
        f"exit recovered={result.recovered} probes={result.probes} "
        f"state={st['state'] if st else '?'}"
    )
    return 0 if result.recovered else 2


if __name__ == "__main__":  # pragma: no cover — exercised as a subprocess
    import asyncio as _asyncio
    import sys as _sys
    _sys.exit(_asyncio.run(main()))
