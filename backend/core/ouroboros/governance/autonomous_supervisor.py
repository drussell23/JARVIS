"""State-Reactive Autonomous Supervisor — O+V manages its own recovery lifecycle.

The philosophical line this crosses: HITL becomes an *override*, never a
*dependency*. O+V reads its own intent from the substrate and drives the whole
recovery lifecycle hands-off — no manual ``/arm``, no static env as the trigger.

The declarative dependency state machine (root cause of process orphanage +
missed recovery windows was NOT having one):

    DORMANT ──[pending soak intent AND DW DEGRADED]──▶ ARMED
    ARMED   ──[soak queue cleared / soak complete]───▶ DORMANT   (SIGTERM Sentinel)
    ARMED   ──[Sentinel exits unexpectedly, queue still pending]──▶ ARMED (restart, backoff)
    ARMED   ──[Sentinel exits after HEALTHY handoff]─▶ (expected — AWE fires; disarm on clear)

Three reflexes (Manifesto §2 progressive awakening + §3 async tendrils + §7):

  * **Intent-driven auto-arming** — at boot (and on demand) ``evaluate()`` queries
    the SQLite substrate: pending soak intent (:mod:`soak_intent`) AND
    ``provider_state == DEGRADED`` → spawn ``sentinel_daemon`` as a real
    ``create_subprocess_exec`` task and arm the AWE listener. Zero human input.

  * **Compute hygiene (auto-disarm)** — subscribes to the ``StreamEventBroker``
    for terminal/soak-complete events; when the queue clears it sends a clean
    ``SIGTERM`` to the Sentinel subprocess and disarms the AWE listener, freeing
    the socket pool + the watcher task.

  * **Self-healing watchdog** — while armed, if the Sentinel exits UNEXPECTEDLY
    (crash / SIGKILL / network death) with the queue still pending and DW still
    DEGRADED, it warns the unified event router and restarts with BINARY
    EXPONENTIAL BACKOFF (capped). A clean exit AFTER the HEALTHY handoff is the
    expected terminus, not a crash — the watchdog distinguishes the two by
    reading ``provider_state`` + the exit code, never blindly respawning.

DRY: reuses the existing ``sentinel_daemon`` (now spawnable via ``-m``), the
``chunk_strategy.db`` substrate, the :class:`AWETrigger` (#70049), and pipes the
Sentinel's stdout/stderr into the unified event router (#70045). The subprocess
spawn is an injected seam so the whole lifecycle is deterministically testable.
Master gate ``JARVIS_AUTONOMOUS_SUPERVISOR_ENABLED`` (default TRUE — but INERT
until real intent exists: an empty queue arms nothing). Fable is never
referenced. Never raises on the hot path.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger("Ouroboros.AutonomousSupervisor")

# Injected seam: spawn the Sentinel subprocess. Returns an asyncio subprocess
# (or any object exposing returncode / wait() / send_signal() / stdout / stderr).
SpawnFn = Callable[[], Awaitable[Any]]
DbFactory = Callable[[], Any]
BreadcrumbFn = Callable[[str, dict], None]

STATE_DORMANT = "DORMANT"
STATE_ARMED = "ARMED"

_SENTINEL_MODULE = "backend.core.ouroboros.governance.sentinel_daemon"


def supervisor_enabled() -> bool:
    """Master gate. Default TRUE — autonomy is the default, but the supervisor is
    inert until pending intent exists (the intent is the trigger, not the flag)."""
    return os.environ.get(
        "JARVIS_AUTONOMOUS_SUPERVISOR_ENABLED", "true",
    ).strip().lower() in ("1", "true", "yes", "on")


def _backoff_base_s() -> float:
    try:
        return max(0.1, float(os.environ.get("JARVIS_SUPERVISOR_BACKOFF_BASE_S", "1.0")))
    except (TypeError, ValueError):
        return 1.0


def _backoff_cap_s() -> float:
    try:
        return max(1.0, float(os.environ.get("JARVIS_SUPERVISOR_BACKOFF_CAP_S", "60.0")))
    except (TypeError, ValueError):
        return 60.0


def _max_restarts() -> int:
    try:
        return max(0, int(os.environ.get("JARVIS_SUPERVISOR_MAX_RESTARTS", "8")))
    except (TypeError, ValueError):
        return 8


def _default_db_factory():
    try:
        from backend.core.ouroboros.governance.dw_outage_forecaster import (
            open_forecast_db,
        )
        return open_forecast_db()
    except Exception:  # noqa: BLE001
        return None


def _default_breadcrumb_fn(event_type: str, payload: dict) -> None:
    try:
        from backend.core.ouroboros.governance.ide_observability_stream import (
            publish_task_event,
        )
        op_id = str(payload.get("provider", "doubleword")) or "doubleword"
        publish_task_event(event_type, op_id, dict(payload))
    except Exception:  # noqa: BLE001
        pass


def build_sentinel_spawn_fn(*, extra_env: Optional[dict] = None) -> SpawnFn:
    """Default spawn seam: run the real Sentinel module as a subprocess with its
    stdout/stderr PIPED so the supervisor can pump telemetry into the event
    router. Reuses the module's ``-m`` entry (DRY)."""

    async def _spawn():
        env = dict(os.environ)
        if extra_env:
            env.update({str(k): str(v) for k, v in extra_env.items()})
        return await asyncio.create_subprocess_exec(
            sys.executable, "-m", _SENTINEL_MODULE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )

    return _spawn


class AutonomousSupervisor:
    """Owns the arm/disarm/self-heal lifecycle around the Sentinel + AWE trigger."""

    def __init__(
        self,
        *,
        spawn_fn: Optional[SpawnFn] = None,
        db_factory: Optional[DbFactory] = None,
        breadcrumb_fn: Optional[BreadcrumbFn] = None,
        awe_factory: Optional[Callable[[], Any]] = None,
        provider: str = "doubleword",
        max_priority: Optional[int] = None,
    ) -> None:
        self._spawn_fn = spawn_fn or build_sentinel_spawn_fn()
        self._db_factory = db_factory or _default_db_factory
        self._breadcrumb_fn = breadcrumb_fn or _default_breadcrumb_fn
        self._awe_factory = awe_factory or self._default_awe_factory
        self._provider = provider
        self._max_priority = max_priority

        self.state = STATE_DORMANT
        self._proc: Any = None
        self._awe: Any = None
        self._watchdog_task: Optional[asyncio.Task] = None
        self._telemetry_task: Optional[asyncio.Task] = None
        self._completion_task: Optional[asyncio.Task] = None
        self._restart_count = 0
        self._disarming = False
        # Lets a test/caller wait for the async watchdog to settle deterministically.
        self._settled: asyncio.Event = asyncio.Event()
        self._settled.set()

    # -- helpers --------------------------------------------------------

    def _emit(self, event_type: str, payload: dict) -> None:
        try:
            self._breadcrumb_fn(event_type, payload)
        except Exception:  # noqa: BLE001
            logger.debug("[Supervisor] breadcrumb failed", exc_info=True)

    def _open_db(self):
        try:
            return self._db_factory()
        except Exception:  # noqa: BLE001
            return None

    def _pending(self, conn=None) -> int:
        from backend.core.ouroboros.governance.soak_intent import pending_soak_count
        own = conn is None
        c = conn if conn is not None else self._open_db()
        try:
            return pending_soak_count(c, max_priority=self._max_priority)
        finally:
            if own and c is not None:
                try:
                    c.close()
                except Exception:  # noqa: BLE001
                    pass

    def _dw_state(self, conn=None) -> str:
        from backend.core.ouroboros.governance.provider_state import get_provider_state
        own = conn is None
        c = conn if conn is not None else self._open_db()
        try:
            st = get_provider_state(c, self._provider)
            return str((st or {}).get("state", "UNKNOWN")).upper()
        finally:
            if own and c is not None:
                try:
                    c.close()
                except Exception:  # noqa: BLE001
                    pass

    def _default_awe_factory(self):
        """Build an AWE trigger bound to the in-process broker. The supervisor
        arms/disarms it in lockstep with the Sentinel — the trigger fires the
        soak on the recovery edge the Sentinel produces."""
        from backend.core.ouroboros.governance.awe_trigger import (
            AWETrigger,
            build_manifest_aware_launch_fn,
        )
        # Substrate-aware dispatch: the supervisor armed BECAUSE an intent is
        # pending, so the recovery edge must run THAT intent's manifest — not a
        # generic soak that leaves it pending forever. Same db_factory, so the
        # dispatcher reads the very queue the arm gate counted.
        return AWETrigger(
            launch_fn=build_manifest_aware_launch_fn(db_factory=self._db_factory),
            db_factory=self._db_factory,
            provider=self._provider,
        )

    # -- intent-driven arming -------------------------------------------

    async def evaluate(self) -> bool:
        """The declarative arm gate. Arms IFF there is pending soak intent AND DW
        is DEGRADED and we are not already armed. Returns whether it armed now.
        Never raises."""
        try:
            if self.state == STATE_ARMED:
                return False
            conn = self._open_db()
            try:
                pending = self._pending(conn)
                dw = self._dw_state(conn)
            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:  # noqa: BLE001
                        pass
            if pending > 0 and dw == "DEGRADED":
                await self.arm(pending=pending, dw_state=dw)
                return True
            logger.debug(
                "[Supervisor] evaluate: no-arm (pending=%d dw=%s)", pending, dw,
            )
            return False
        except Exception:  # noqa: BLE001
            logger.debug("[Supervisor] evaluate raised", exc_info=True)
            return False

    async def arm(self, *, pending: int = 0, dw_state: str = "DEGRADED") -> None:
        """Spawn the Sentinel subprocess, arm the AWE listener, and start the
        watchdog + telemetry pump + completion listener. Never raises."""
        if self.state == STATE_ARMED:
            return
        self._disarming = False
        self._restart_count = 0
        try:
            self._proc = await self._spawn_fn()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Supervisor] Sentinel spawn failed: %s", exc)
            self._emit("sentinel_restarted", {
                "provider": self._provider, "attempt": 0, "backoff_s": 0,
                "reason": f"initial spawn failed: {exc}",
            })
            self._proc = None
            return
        self.state = STATE_ARMED
        # Arm the AWE listener (fires the soak on the recovery edge).
        try:
            self._awe = self._awe_factory()
            if self._awe is not None:
                self._awe.start()
        except Exception:  # noqa: BLE001
            logger.debug("[Supervisor] AWE arm failed", exc_info=True)
            self._awe = None
        pid = getattr(self._proc, "pid", "?")
        self._emit("supervisor_armed", {
            "provider": self._provider, "pending": pending,
            "provider_state": dw_state, "pid": pid,
        })
        logger.info(
            "[Supervisor] ARMED — pending=%d DW=%s → Sentinel pid=%s + AWE listener",
            pending, dw_state, pid,
        )
        self._telemetry_task = asyncio.ensure_future(self._pump_telemetry())
        self._completion_task = asyncio.ensure_future(self._completion_listener())
        self._watchdog_task = asyncio.ensure_future(self._watchdog())

    # -- compute hygiene: auto-disarm -----------------------------------

    async def _completion_listener(self) -> None:
        """Subscribe to the broker; on any terminal/soak-complete event, re-check
        the queue. Empty → disarm (compute hygiene). Best-effort."""
        broker = None
        sub = None
        try:
            from backend.core.ouroboros.governance.ide_observability_stream import (
                EVENT_TYPE_AWE_SOAK_COMPLETE,
                EVENT_TYPE_OPERATION_TERMINAL,
                get_default_broker,
            )
            triggers = {EVENT_TYPE_AWE_SOAK_COMPLETE, EVENT_TYPE_OPERATION_TERMINAL}
            broker = get_default_broker()
            sub = broker.subscribe()
            if sub is None:
                return
            async for event in broker.stream_iter(sub, heartbeat_s=0):
                if getattr(event, "event_type", "") not in triggers:
                    continue
                try:
                    if self.state == STATE_ARMED and self._pending() == 0:
                        await self.disarm(reason="soak queue cleared")
                        return
                except Exception:  # noqa: BLE001
                    continue
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            return
        finally:
            try:
                if broker is not None and sub is not None:
                    broker.unsubscribe(sub)
            except Exception:  # noqa: BLE001
                pass

    async def disarm(self, *, reason: str = "queue cleared") -> None:
        """Clean shutdown: SIGTERM the Sentinel, disarm AWE, cancel the watchdog +
        listeners, return to DORMANT. Idempotent. Never raises."""
        if self._disarming or self.state != STATE_ARMED:
            return
        self._disarming = True
        # Cancel the watchdog FIRST so the SIGTERM exit isn't misread as a crash.
        for attr in ("_watchdog_task", "_telemetry_task", "_completion_task"):
            t = getattr(self, attr, None)
            if t is not None and t is not asyncio.current_task():
                t.cancel()
        await self._terminate_proc()
        # Disarm AWE.
        if self._awe is not None:
            try:
                await self._awe.stop()
            except Exception:  # noqa: BLE001
                pass
            self._awe = None
        self.state = STATE_DORMANT
        self._emit("supervisor_disarmed", {"provider": self._provider, "reason": reason})
        logger.info("[Supervisor] DISARMED — %s", reason)
        self._disarming = False

    async def _terminate_proc(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        try:
            if getattr(proc, "returncode", None) is None:
                proc.send_signal(signal.SIGTERM)
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5.0)
                except (asyncio.TimeoutError, Exception):  # noqa: BLE001
                    try:
                        proc.kill()
                    except Exception:  # noqa: BLE001
                        pass
        except Exception:  # noqa: BLE001
            logger.debug("[Supervisor] terminate_proc failed", exc_info=True)

    # -- self-healing watchdog ------------------------------------------

    async def _watchdog(self) -> None:
        """Await the Sentinel's exit. Distinguish the expected HEALTHY handoff
        (returncode 0 or DW now HEALTHY → don't respawn) from an unexpected crash
        (queue still pending AND DW still DEGRADED → warn + exponential-backoff
        restart, capped). Never raises."""
        try:
            while True:
                proc = self._proc
                if proc is None:
                    return
                self._settled.clear()
                try:
                    rc = await proc.wait()
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001
                    rc = None

                # A disarm() in flight owns the teardown — do not respawn.
                if self._disarming or self.state != STATE_ARMED:
                    self._settled.set()
                    return

                dw = self._dw_state()
                pending = self._pending()

                # Expected terminus: the Sentinel wrote HEALTHY and self-exited.
                # AWE fires on the recovery edge; disarm happens on queue-clear.
                if rc == 0 or dw == "HEALTHY":
                    logger.info(
                        "[Supervisor] Sentinel exited cleanly (rc=%s dw=%s) — "
                        "expected recovery handoff, not respawning", rc, dw,
                    )
                    self._settled.set()
                    return
                # Queue already emptied out from under us → nothing to guard.
                if pending == 0:
                    self._settled.set()
                    await self.disarm(reason="queue emptied during watch")
                    return

                # Unexpected crash while armed → self-heal with backoff.
                self._restart_count += 1
                if self._restart_count > _max_restarts():
                    logger.warning(
                        "[Supervisor] Sentinel exceeded max restarts (%d) — giving up",
                        _max_restarts(),
                    )
                    self._emit("sentinel_restarted", {
                        "provider": self._provider, "attempt": self._restart_count,
                        "backoff_s": 0, "reason": "max restarts exceeded — supervisor stands down",
                    })
                    self._settled.set()
                    await self.disarm(reason="sentinel unrecoverable")
                    return
                backoff = min(
                    _backoff_cap_s(),
                    _backoff_base_s() * (2 ** (self._restart_count - 1)),
                )
                self._emit("sentinel_restarted", {
                    "provider": self._provider, "attempt": self._restart_count,
                    "backoff_s": round(backoff, 2),
                    "reason": f"unexpected exit rc={rc} (DW still {dw}, {pending} pending)",
                })
                logger.warning(
                    "[Supervisor] Sentinel crashed (rc=%s) — self-heal restart "
                    "#%d after %.1fs backoff", rc, self._restart_count, backoff,
                )
                try:
                    await asyncio.sleep(backoff)
                except asyncio.CancelledError:
                    raise
                if self._disarming or self.state != STATE_ARMED:
                    self._settled.set()
                    return
                try:
                    self._proc = await self._spawn_fn()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[Supervisor] respawn failed: %s", exc)
                    self._proc = None
                    self._settled.set()
                    await self.disarm(reason="sentinel respawn failed")
                    return
                # Re-pump telemetry for the new process.
                self._telemetry_task = asyncio.ensure_future(self._pump_telemetry())
                self._settled.set()
        except asyncio.CancelledError:
            self._settled.set()
            raise
        except Exception:  # noqa: BLE001
            logger.debug("[Supervisor] watchdog raised", exc_info=True)
            self._settled.set()

    # -- telemetry pipe -------------------------------------------------

    async def _pump_telemetry(self) -> None:
        """Read the Sentinel subprocess's stdout line-by-line and surface each as
        a ``sentinel_telemetry`` breadcrumb — native REPL visibility of the
        subprocess (DRY: the unified event router renders it). Best-effort."""
        proc = self._proc
        stream = getattr(proc, "stdout", None) if proc is not None else None
        if stream is None:
            return
        try:
            while True:
                line = await stream.readline()
                if not line:
                    return
                try:
                    text = line.decode("utf-8", "replace").rstrip() if isinstance(line, (bytes, bytearray)) else str(line).rstrip()
                except Exception:  # noqa: BLE001
                    continue
                if text:
                    self._emit("sentinel_telemetry", {
                        "provider": self._provider, "line": text,
                    })
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            return

    # -- lifecycle ------------------------------------------------------

    async def stop(self) -> None:
        """Tear everything down (used on REPL shutdown). Never raises."""
        try:
            await self.disarm(reason="supervisor stop")
        except Exception:  # noqa: BLE001
            pass
        for attr in ("_watchdog_task", "_telemetry_task", "_completion_task"):
            t = getattr(self, attr, None)
            if t is not None:
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
                setattr(self, attr, None)


def start_autonomous_supervisor(**kwargs: Any) -> Optional[AutonomousSupervisor]:
    """Construct the supervisor and kick off the intent-driven ``evaluate()`` IFF
    ``JARVIS_AUTONOMOUS_SUPERVISOR_ENABLED``. Returns the live supervisor (whose
    ``.stop()`` the caller tears down) or ``None`` when disabled / on error. It
    arms nothing unless real intent exists. Never raises."""
    if not supervisor_enabled():
        return None
    try:
        sup = AutonomousSupervisor(**kwargs)
        asyncio.ensure_future(sup.evaluate())
        logger.info("[Supervisor] online — evaluating workload intent (autonomy default)")
        return sup
    except Exception:  # noqa: BLE001
        logger.debug("[Supervisor] start failed", exc_info=True)
        return None


__all__ = [
    "AutonomousSupervisor",
    "STATE_ARMED",
    "STATE_DORMANT",
    "build_sentinel_spawn_fn",
    "start_autonomous_supervisor",
    "supervisor_enabled",
]
