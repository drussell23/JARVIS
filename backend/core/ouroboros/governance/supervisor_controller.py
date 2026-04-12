"""
Supervisor Ouroboros Controller — Lifecycle Authority
=====================================================

The single authority for starting, stopping, pausing, and resuming
Ouroboros autonomy.  ``unified_supervisor.py`` delegates all autonomy
lifecycle decisions to this controller.

State machine::

    DISABLED ──start()──► SANDBOX ──enable_governed_autonomy()──► GOVERNED
       ▲                    │  ▲                                      │
       │                    │  │                                      │
     stop()            pause() resume()                           pause()
       │                    │  │                                      │
       │                    ▼  │                                      ▼
       ◄──── stop() ◄── READ_ONLY ◄─────────────────────────────  READ_ONLY
                            │
                     emergency_stop()
                            │
                            ▼
                     EMERGENCY_STOP  (resume() raises RuntimeError)

    GOVERNED ◄───wake_from_hibernation()─── HIBERNATION ◄──enter_hibernation()── GOVERNED

    If ``_safe_mode`` is True, start() enters SAFE_MODE instead of SANDBOX.

HIBERNATION_MODE
----------------
A special sibling of READ_ONLY that the controller enters when the
provider substrate (DoubleWord, Claude) is unreachable.  The BG pool is
paused, the idle watchdog is frozen, and no new sandbox or governed
operations are accepted — but interactive surfaces (REPL, voice, CLI)
remain responsive so Derek can still inspect state.  When health probes
confirm providers are back, ``wake_from_hibernation()`` restores the
prior mode (GOVERNED) and resumes the DAG exactly where it left off.
"""

from __future__ import annotations

import asyncio
import enum
import inspect
import logging
import os
import time
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("Ouroboros.Controller")


# ---------------------------------------------------------------------------
# HIBERNATION hook plumbing (step 6.5)
# ---------------------------------------------------------------------------
#
# The controller owns the authoritative lifecycle mode, but it does NOT
# directly know about the BackgroundAgentPool, IdleWatchdog, SerpentFlow TUI,
# or any other subsystem that needs to react to hibernation transitions.
# Rather than injecting concrete refs (and introducing circular imports),
# we expose a tiny pub/sub so any subsystem can register callbacks that
# fire after the mode flip completes. GovernedLoopService uses this to wire
# pool.pause()/watchdog.freeze() without coupling the controller to either.


HibernationHook = Callable[..., Any]
"""Signature for hibernation enter/wake callbacks.

Hooks receive a single keyword argument ``reason`` and may be sync or
async. Return values are ignored: async returns are detected at call
time via ``inspect.isawaitable`` and awaited, sync returns are dropped.
This deliberately accepts ``Callable[..., Any]`` so lambda adapters
that wrap multiple sync side-effects (e.g. returning a tuple from
``(pool.pause(), watchdog.freeze())``) don't trip the type checker.
"""


_ENV_HOOK_TIMEOUT = "JARVIS_HIBERNATION_HOOK_TIMEOUT_S"
_DEFAULT_HOOK_TIMEOUT_S = 10.0


def _resolve_hook_timeout() -> float:
    """Resolve per-hook timeout from env var, falling back to default."""
    raw = os.environ.get(_ENV_HOOK_TIMEOUT, "").strip()
    if not raw:
        return _DEFAULT_HOOK_TIMEOUT_S
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "%s=%r is not a float — falling back to default %s",
            _ENV_HOOK_TIMEOUT, raw, _DEFAULT_HOOK_TIMEOUT_S,
        )
        return _DEFAULT_HOOK_TIMEOUT_S
    if value <= 0:
        logger.warning(
            "%s=%s is non-positive — falling back to default %s",
            _ENV_HOOK_TIMEOUT, value, _DEFAULT_HOOK_TIMEOUT_S,
        )
        return _DEFAULT_HOOK_TIMEOUT_S
    return value


async def _call_maybe_async(fn: Callable[..., Any], **kwargs: Any) -> Any:
    """Invoke *fn* and await its result if it is awaitable.

    Handles both ``async def`` callables and sync callables that return
    a coroutine (common for lambda adapters that defer to an async impl).
    Plain sync callables are called directly on the event loop thread, so
    they must not perform blocking I/O.
    """
    result = fn(**kwargs)
    if inspect.isawaitable(result):
        return await result
    return result


class AutonomyMode(enum.Enum):
    """Operating modes for the Ouroboros autonomy lifecycle."""

    DISABLED = "DISABLED"
    SANDBOX = "SANDBOX"
    READ_ONLY = "READ_ONLY"
    GOVERNED = "GOVERNED"
    EMERGENCY_STOP = "EMERGENCY_STOP"
    SAFE_MODE = "SAFE_MODE"
    # HIBERNATION: entered when the provider substrate is exhausted.
    # No writes, no sandbox ops, no new generation — but interactive
    # surfaces still work so the operator can inspect state and the
    # health prober can wake the organism when providers recover.
    HIBERNATION = "HIBERNATION"


class SupervisorOuroborosController:
    """Single lifecycle authority for JARVIS self-programming autonomy.

    Only this class may start, stop, pause, or resume the Ouroboros loop.
    ``unified_supervisor.py`` delegates to an instance of this controller
    rather than managing autonomy state directly.
    """

    def __init__(self) -> None:
        self._mode: AutonomyMode = AutonomyMode.DISABLED
        self._safe_mode: bool = False
        self._gates_passed: bool = False
        self._emergency_reason: Optional[str] = None
        # HIBERNATION_MODE state — tracked here so the controller can
        # restore the exact pre-outage mode on wake. _hibernation_reason
        # is surfaced in logs and health() for postmortem.
        self._pre_hibernation_mode: Optional[AutonomyMode] = None
        self._hibernation_reason: Optional[str] = None
        self._hibernation_count: int = 0
        # HIBERNATION_MODE step 6.5 — hook registry fired around enter/wake
        # transitions so external subsystems (BG pool, idle watchdog, TUI)
        # can react without the controller needing direct references.
        self._on_hibernate_hooks: List[HibernationHook] = []
        self._on_wake_hooks: List[HibernationHook] = []
        self._hook_lock: asyncio.Lock = asyncio.Lock()
        self._hook_timeout_s: float = _resolve_hook_timeout()
        self._hook_stats: Dict[str, Any] = {
            "hibernate_fires": 0,
            "wake_fires": 0,
            "hibernate_hook_failures": 0,
            "wake_hook_failures": 0,
            "last_hibernate_elapsed_ms": None,
            "last_wake_elapsed_ms": None,
        }
        logger.info(
            "SupervisorOuroborosController initialised — mode=%s "
            "(hook_timeout=%.1fs)",
            self._mode.value,
            self._hook_timeout_s,
        )

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def mode(self) -> AutonomyMode:
        """Current autonomy mode."""
        return self._mode

    @property
    def writes_allowed(self) -> bool:
        """True only when in GOVERNED mode — the only mode that permits writes.

        HIBERNATION explicitly returns False: the provider substrate is
        down, no generation is possible, and the BG pool is paused.
        """
        return self._mode is AutonomyMode.GOVERNED

    @property
    def sandbox_allowed(self) -> bool:
        """True in SANDBOX or GOVERNED — modes that permit sandboxed execution.

        HIBERNATION excluded: new sandbox ops cannot make progress without
        providers, so admitting them would only pile up stale work.
        """
        return self._mode in (AutonomyMode.SANDBOX, AutonomyMode.GOVERNED)

    @property
    def interactive_allowed(self) -> bool:
        """True in every mode except DISABLED.

        HIBERNATION keeps interactive surfaces live so the operator can
        still inspect health, read logs, and force a wake if needed.
        """
        return self._mode is not AutonomyMode.DISABLED

    @property
    def is_hibernating(self) -> bool:
        """True while the controller is in HIBERNATION mode."""
        return self._mode is AutonomyMode.HIBERNATION

    # ------------------------------------------------------------------
    # Lifecycle transitions
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the autonomy loop.

        If ``_safe_mode`` is set, enters SAFE_MODE (read-only with
        interactive access).  Otherwise enters SANDBOX.
        """
        if self._safe_mode:
            self._mode = AutonomyMode.SAFE_MODE
            logger.warning("start() — entering SAFE_MODE (safe-mode flag is set)")
        else:
            self._mode = AutonomyMode.SANDBOX
            logger.info("start() — entering SANDBOX")

    async def stop(self) -> None:
        """Stop the autonomy loop and reset all transient state.

        Any pending hibernation state is cleared — stop() is the ultimate
        teardown and supersedes hibernation even mid-outage. Registered
        hibernation hooks are also cleared so a subsequent ``start()`` /
        re-registration cycle doesn't reference stale pool/watchdog
        closures from the previous lifecycle.
        """
        previous = self._mode
        self._mode = AutonomyMode.DISABLED
        self._gates_passed = False
        self._pre_hibernation_mode = None
        self._hibernation_reason = None
        self.clear_hibernation_hooks()
        logger.info("stop() — %s → DISABLED (gates_passed reset)", previous.value)

    async def pause(self) -> None:
        """Pause autonomy — switch to READ_ONLY.

        Refuses to run during HIBERNATION: pause() and hibernation serve
        different purposes (operator-initiated vs. provider-outage), and
        mixing them corrupts the state machine. Use wake_from_hibernation()
        to leave HIBERNATION.
        """
        if self._mode is AutonomyMode.HIBERNATION:
            logger.error("pause() blocked — controller is HIBERNATING")
            raise RuntimeError(
                "Cannot pause while HIBERNATING — use wake_from_hibernation() first"
            )
        previous = self._mode
        self._mode = AutonomyMode.READ_ONLY
        logger.info("pause() — %s → READ_ONLY", previous.value)

    async def resume(self) -> None:
        """Resume from pause.

        Raises ``RuntimeError`` if the controller is in EMERGENCY_STOP —
        a human must clear the emergency before resuming.  Also refuses
        HIBERNATION: the dedicated ``wake_from_hibernation()`` entry
        point (landing in a later step) is the only way out.
        """
        if self._mode is AutonomyMode.EMERGENCY_STOP:
            logger.error(
                "resume() blocked — EMERGENCY_STOP is active (reason: %s)",
                self._emergency_reason,
            )
            raise RuntimeError(
                f"Cannot resume from emergency stop: {self._emergency_reason}"
            )
        if self._mode is AutonomyMode.HIBERNATION:
            logger.error("resume() blocked — controller is HIBERNATING")
            raise RuntimeError(
                "Cannot resume from HIBERNATION — use wake_from_hibernation()"
            )
        previous = self._mode
        self._mode = AutonomyMode.SANDBOX
        logger.info("resume() — %s → SANDBOX", previous.value)

    async def enable_governed_autonomy(self) -> None:
        """Promote to GOVERNED mode (writes allowed).

        Raises ``RuntimeError`` if the governance gates have not been
        passed via :meth:`mark_gates_passed`, or if the controller is
        currently HIBERNATING (wake first).
        """
        if self._mode is AutonomyMode.HIBERNATION:
            logger.error(
                "enable_governed_autonomy() blocked — controller is HIBERNATING"
            )
            raise RuntimeError(
                "Cannot enable governed autonomy while HIBERNATING — "
                "wake_from_hibernation() first"
            )
        if not self._gates_passed:
            logger.error("enable_governed_autonomy() blocked — gates not passed")
            raise RuntimeError(
                "Cannot enable governed autonomy: gates have not been passed"
            )
        previous = self._mode
        self._mode = AutonomyMode.GOVERNED
        logger.info("enable_governed_autonomy() — %s → GOVERNED", previous.value)

    async def mark_gates_passed(self) -> None:
        """Record that all governance gates have been satisfied."""
        self._gates_passed = True
        logger.info("mark_gates_passed() — governance gates satisfied")

    # ------------------------------------------------------------------
    # Hibernation hook registration (step 6.5)
    # ------------------------------------------------------------------

    def register_hibernation_hooks(
        self,
        *,
        on_hibernate: Optional[HibernationHook] = None,
        on_wake: Optional[HibernationHook] = None,
        name: str = "unnamed",
    ) -> None:
        """Register callbacks fired around HIBERNATION transitions.

        Parameters
        ----------
        on_hibernate:
            Called after the controller enters HIBERNATION, with a single
            keyword argument ``reason``. Sync or async.
        on_wake:
            Called after the controller leaves HIBERNATION (either via
            :meth:`wake_from_hibernation` or via :meth:`emergency_stop`
            superseding hibernation), with keyword ``reason``.
        name:
            Human-readable tag used in logs so we can tell which subsystem
            owns a given hook. Pass a unique name per registration site.

        Duplicate registrations (same callable object) are deduped — it
        is safe to call this twice from an idempotent boot path. Hooks
        are fired in the order they were registered.
        """
        registered: List[str] = []
        if on_hibernate is not None and on_hibernate not in self._on_hibernate_hooks:
            self._on_hibernate_hooks.append(on_hibernate)
            registered.append("on_hibernate")
        if on_wake is not None and on_wake not in self._on_wake_hooks:
            self._on_wake_hooks.append(on_wake)
            registered.append("on_wake")
        if registered:
            logger.info(
                "[Controller] hibernation hooks registered "
                "(name=%s, kinds=%s, total=%d/%d)",
                name,
                ",".join(registered),
                len(self._on_hibernate_hooks),
                len(self._on_wake_hooks),
            )

    def unregister_hibernation_hooks(
        self,
        *,
        on_hibernate: Optional[HibernationHook] = None,
        on_wake: Optional[HibernationHook] = None,
    ) -> None:
        """Remove previously-registered hooks. No-op if callable not found.

        Useful for teardown paths and tests that rebuild the controller
        without leaving stale references pointing at disposed objects.
        """
        if on_hibernate is not None:
            try:
                self._on_hibernate_hooks.remove(on_hibernate)
            except ValueError:
                pass
        if on_wake is not None:
            try:
                self._on_wake_hooks.remove(on_wake)
            except ValueError:
                pass

    def clear_hibernation_hooks(self) -> None:
        """Drop every registered hook. Used by ``stop()`` and tests."""
        self._on_hibernate_hooks.clear()
        self._on_wake_hooks.clear()

    def hibernation_hook_snapshot(self) -> Dict[str, Any]:
        """Lock-free observability dict for health()/TUI surfaces."""
        return {
            "hibernate_hooks_registered": len(self._on_hibernate_hooks),
            "wake_hooks_registered": len(self._on_wake_hooks),
            "hook_timeout_s": self._hook_timeout_s,
            **self._hook_stats,
        }

    async def _fire_hibernation_hooks(
        self,
        hooks: List[HibernationHook],
        *,
        kind: str,
        reason: str,
    ) -> None:
        """Run a list of hibernation hooks sequentially under the hook lock.

        Each hook runs via :func:`asyncio.wait_for` with
        ``self._hook_timeout_s`` so a stuck hook cannot block the
        transition indefinitely. Individual timeouts and exceptions are
        logged and counted in ``_hook_stats`` but otherwise swallowed —
        one broken hook must never prevent the others from firing, and
        hook failures must never propagate back to the transition caller
        because the controller state is already authoritative.

        The list is snapshotted before firing so a concurrent
        :meth:`unregister_hibernation_hooks` / :meth:`clear_hibernation_hooks`
        cannot mutate it mid-iteration.
        """
        if not hooks:
            return
        async with self._hook_lock:
            pending = list(hooks)
            start = time.monotonic()
            failures = 0
            for hook in pending:
                hook_name = getattr(
                    hook,
                    "__qualname__",
                    getattr(hook, "__name__", repr(hook)),
                )
                try:
                    await asyncio.wait_for(
                        _call_maybe_async(hook, reason=reason),
                        timeout=self._hook_timeout_s,
                    )
                except asyncio.TimeoutError:
                    failures += 1
                    logger.error(
                        "[Controller] %s hook %s timed out after %.1fs",
                        kind, hook_name, self._hook_timeout_s,
                    )
                except Exception:  # noqa: BLE001
                    failures += 1
                    logger.exception(
                        "[Controller] %s hook %s raised — continuing",
                        kind, hook_name,
                    )
            elapsed_ms = (time.monotonic() - start) * 1000.0
            self._hook_stats[f"{kind}_fires"] += 1
            self._hook_stats[f"{kind}_hook_failures"] += failures
            self._hook_stats[f"last_{kind}_elapsed_ms"] = elapsed_ms
            logger.info(
                "[Controller] fired %d %s hook(s) in %.1fms (failures=%d)",
                len(pending), kind, elapsed_ms, failures,
            )

    async def enter_hibernation(self, reason: str) -> bool:
        """Transition into HIBERNATION mode, preserving the prior mode.

        Called by the provider-exhaustion watcher (step 5) when DoubleWord
        and Claude both become unreachable. The controller records the
        current mode so ``wake_from_hibernation()`` can restore it exactly.

        Refuses when:
          - Already HIBERNATING (idempotent no-op returning False)
          - In EMERGENCY_STOP — a human must clear the emergency first
          - In DISABLED — nothing to hibernate
        """
        if self._mode is AutonomyMode.HIBERNATION:
            logger.debug("enter_hibernation() no-op — already hibernating")
            return False
        if self._mode is AutonomyMode.EMERGENCY_STOP:
            logger.error(
                "enter_hibernation() blocked — EMERGENCY_STOP active (reason=%r)",
                self._emergency_reason,
            )
            raise RuntimeError(
                "Cannot hibernate from EMERGENCY_STOP — clear the emergency first"
            )
        if self._mode is AutonomyMode.DISABLED:
            logger.warning("enter_hibernation() rejected — controller is DISABLED")
            return False

        self._pre_hibernation_mode = self._mode
        self._hibernation_reason = reason
        self._hibernation_count += 1
        self._mode = AutonomyMode.HIBERNATION
        logger.warning(
            "enter_hibernation() — %s → HIBERNATION (reason=%r, cycle #%d)",
            self._pre_hibernation_mode.value,
            reason,
            self._hibernation_count,
        )
        # Fire on_hibernate hooks AFTER the mode flip so hooks that
        # inspect ``controller.mode`` observe the new state. Failures
        # are swallowed inside _fire_hibernation_hooks — the transition
        # itself must remain authoritative.
        await self._fire_hibernation_hooks(
            self._on_hibernate_hooks,
            kind="hibernate",
            reason=reason,
        )
        return True

    async def wake_from_hibernation(self, *, reason: str = "") -> bool:
        """Restore the pre-hibernation mode after providers recover.

        The prober (step 6) calls this once health probes pass. The
        controller transitions back to the exact mode it was in before
        the outage — GOVERNED, SANDBOX, READ_ONLY, or SAFE_MODE — so the
        DAG resumes without losing its capability envelope.

        Returns False if not currently hibernating (idempotent).
        """
        if self._mode is not AutonomyMode.HIBERNATION:
            logger.debug(
                "wake_from_hibernation() no-op — current mode is %s",
                self._mode.value,
            )
            return False
        target = self._pre_hibernation_mode or AutonomyMode.SANDBOX
        self._mode = target
        self._pre_hibernation_mode = None
        self._hibernation_reason = None
        logger.info(
            "wake_from_hibernation() — HIBERNATION → %s (reason=%r)",
            target.value,
            reason or "unspecified",
        )
        # Fire on_wake hooks AFTER the mode flip so hooks see the
        # restored mode. Pool.resume / watchdog.unfreeze are idempotent
        # so duplicate fires across wake paths (explicit wake vs.
        # emergency_stop) are safe.
        await self._fire_hibernation_hooks(
            self._on_wake_hooks,
            kind="wake",
            reason=reason or "unspecified",
        )
        return True

    async def emergency_stop(self, reason: str) -> None:
        """Immediately halt all autonomy.

        Stores *reason* and transitions to EMERGENCY_STOP.  Any
        subsequent :meth:`resume` will raise ``RuntimeError`` until
        the emergency is manually cleared.

        If the controller was hibernating, the wake hooks are fired so
        that the BG pool pause / idle watchdog freeze acquired on the
        way in are released on the way out. EMERGENCY_STOP's own guards
        still prevent new work from being scheduled — we're just not
        leaving subsystems stranded in paused state.
        """
        self._emergency_reason = reason
        previous = self._mode
        was_hibernating = previous is AutonomyMode.HIBERNATION
        self._mode = AutonomyMode.EMERGENCY_STOP
        # Hibernation state is discarded — emergency supersedes.
        self._pre_hibernation_mode = None
        self._hibernation_reason = None
        logger.critical(
            "emergency_stop() — %s → EMERGENCY_STOP (reason: %s)",
            previous.value,
            reason,
        )
        if was_hibernating:
            await self._fire_hibernation_hooks(
                self._on_wake_hooks,
                kind="wake",
                reason=f"emergency_stop supersedes hibernation: {reason}",
            )
