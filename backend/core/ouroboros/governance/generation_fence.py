"""GenerationFence -- the Brain-side ACTIVE fence (Stage-4 Task 4, split-brain).

The keeper (Stage-4 Task 3, ``brain_keeper.py``) mints monotonic Brain
generations and the Body publishes them as ``console.keeper_heartbeat`` on the
trinity bus (``scripts/run_body_mode.py`` census tick, transiting the mac-side
``TrinityBusBridge`` outbound allowlist ``console.*``). A Brain that observes a
HIGHER current generation than its own is, by definition, a superseded twin: a
newer Brain has been resurrected in its place and THIS node's mutations are no
longer sovereign. On that observation this fence structurally terminates the
node's mutation pathways -- deterministically, pure code, no LLM.

Operator Mandate 2 (verbatim): the transition "must not just set an
``is_fenced=True`` flag; it must structurally terminate the mutation execution
pathways." We COMPOSE the existing, proven termination machinery rather than
inventing a new one:

  1. BOUNDARY ARM -- ``autonomous_workspace._arm_boundary_flags()`` (the LR-A
     deterministic-lock force-arm of the Async Yield Matrix Sovereign Execution
     Boundary, PRD s51.11.35 / PR #69638). It arms the flag PAIR
     ``JARVIS_FILE_ISOLATION_ENABLED`` + ``JARVIS_EXECUTION_BOUNDARY_ENABLED``
     in-process -- exactly the way its own regression spine arms it
     (``tests/governance/test_deterministic_isolation_lock.py``) -- so Stage A
     (commit denial: AutoCommit refuses the primary checkout) and Stage B
     (file isolation: APPLY routes to a quarantine worktree, never the shared
     tree) both read armed EVEN IF the flags were explicitly false. This is the
     physical decoupling of the AutoCommit and APPLY pathways.
  2. SHUTDOWN REQUEST -- ``cooperative_shutdown.request()``: the process-global
     wind-down signal the harness itself uses at graceful shutdown. In-flight
     coroutines yield at their next safe boundary; no new work starts.
  3. CHECKPOINT CAPTURE -- ``fsm_checkpoint.capture_inflight(reason=
     "generation_fenced")``: SUSPEND every in-flight op into a signed
     checkpoint so the SUCCESSOR generation resumes the thought (Mandate 3:
     crypto/payload untouched -- only a new reason STRING flows through the
     existing signer).
  4. IDLE TOUCH -- ``stream_heartbeat.pulse()``: best-effort beat on the
     idle fast-path marker (the Move-2-v4 staleness watchdog input, with an
     optional cross-process file mirror) so the idle machinery observes the
     fence transition as fresh activity at a deterministic timestamp instead
     of misreading the wind-down as a stall.

Each step is fail-soft INDIVIDUALLY (one failing arm never stops the others)
but the transition is deterministic: all four are ALWAYS attempted, in order,
exactly once per process (idempotent -- a second higher-gen observation
no-ops). Structured evidence line::

    [GenerationFence] FENCED own_gen=%d observed_gen=%d arms=...

Authority posture: this module is a pure-code consumer of a bus signal. It
imports NO LLM/provider machinery (AST-pinned by
``tests/governance/test_generation_fence.py``) and never mints authority --
it only revokes this node's own.
"""
from __future__ import annotations

import inspect
import logging
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# The keeper-heartbeat topic. The Body publishes it on its local trinity bus;
# the mac-side TrinityBusBridge outbound allowlist ("console.*",
# scripts/run_body_mode.py::_do_bridge) carries it across the WS link onto the
# Brain organism's trinity bus, where this fence subscribes.
HEARTBEAT_TOPIC = "console.keeper_heartbeat"

_FENCE_REASON = "generation_fenced"


# --------------------------------------------------------------------------- #
# Live default arms -- each a lazy import so constructing a fence with injected
# seams (tests) never pays for (or trips over) the governance stack.
# --------------------------------------------------------------------------- #
def _default_boundary_arm() -> None:
    """Force-arm the Sovereign Execution Boundary flag pair (LR-A)."""
    from backend.core.ouroboros.governance.autonomous_workspace import (  # noqa: PLC0415
        _arm_boundary_flags,
    )
    _arm_boundary_flags()


def _default_shutdown() -> None:
    """Request the process-global cooperative wind-down."""
    from backend.core.ouroboros.governance import cooperative_shutdown  # noqa: PLC0415
    cooperative_shutdown.request(reason=_FENCE_REASON)


def _default_capture() -> None:
    """SUSPEND in-flight ops into signed checkpoints for the successor.

    Mandate 3: crypto and payload untouched -- ``reason`` is a plain string
    threaded through the EXISTING capture path (it defaults to
    ``wall_clock_cap``; this just names the new cause)."""
    from backend.core.ouroboros.governance.fsm_checkpoint import (  # noqa: PLC0415
        capture_inflight,
    )
    capture_inflight(reason=_FENCE_REASON)


def _default_idle_touch() -> None:
    """Best-effort touch of the idle fast-path marker.

    ``stream_heartbeat.pulse()`` is the simple existing marker: it stamps the
    idle/staleness watchdog input (plus the optional cross-process mirror
    file) so the fence transition itself is the last recorded activity."""
    from backend.core.ouroboros.governance import stream_heartbeat  # noqa: PLC0415
    stream_heartbeat.pulse()


class GenerationFence:
    """Subscribe to keeper heartbeats; fence on a higher observed generation.

    All four action seams are injectable (tests use recorders); ``None``
    resolves to the live default lazily AT FENCE TIME (never at construction).

    Args:
        bus: the organism's ``TrinityEventBus`` (``subscribe``/``unsubscribe``).
        own_gen: THIS Brain's minted generation (``JARVIS_BRAIN_GENERATION``).
        boundary_arm_fn: seam for step 1 (Sovereign Execution Boundary arm).
        shutdown_fn: seam for step 2 (cooperative shutdown request).
        capture_fn: seam for step 3 (in-flight checkpoint capture).
        idle_touch_fn: seam for step 4 (idle fast-path marker touch).
    """

    def __init__(
        self,
        bus: Any,
        own_gen: int,
        *,
        boundary_arm_fn: Optional[Callable[[], Any]] = None,
        shutdown_fn: Optional[Callable[[], Any]] = None,
        capture_fn: Optional[Callable[[], Any]] = None,
        idle_touch_fn: Optional[Callable[[], Any]] = None,
    ) -> None:
        self._bus = bus
        self._own_gen = int(own_gen)
        self._boundary_arm_fn = boundary_arm_fn
        self._shutdown_fn = shutdown_fn
        self._capture_fn = capture_fn
        self._idle_touch_fn = idle_touch_fn
        self._sub_id: Optional[str] = None
        self._fenced = False

    @property
    def own_gen(self) -> int:
        return self._own_gen

    @property
    def fenced(self) -> bool:
        return self._fenced

    async def start(self) -> None:
        """Subscribe the heartbeat topic on the organism trinity bus."""
        if self._sub_id is not None:
            return
        self._sub_id = await self._bus.subscribe(
            HEARTBEAT_TOPIC, self._on_heartbeat)
        logger.info(
            "[GenerationFence] armed own_gen=%d topic=%s",
            self._own_gen, HEARTBEAT_TOPIC,
        )

    async def stop(self) -> None:
        """Unsubscribe. Never raises (fail-soft teardown)."""
        sub_id, self._sub_id = self._sub_id, None
        if sub_id is None:
            return
        try:
            await self._bus.unsubscribe(sub_id)
        except Exception:  # noqa: BLE001 -- teardown is best-effort
            logger.debug("[GenerationFence] unsubscribe failed", exc_info=True)

    async def _on_heartbeat(self, event: Any) -> None:
        """TrinityEvent handler: parse gen; fence EXACTLY ONCE on higher."""
        payload = getattr(event, "payload", None) or {}
        raw = payload.get("gen")
        try:
            gen = int(raw)
        except (TypeError, ValueError):
            logger.debug(
                "[GenerationFence] ignoring malformed heartbeat gen=%r", raw)
            return
        if gen <= self._own_gen:
            return
        if self._fenced:
            return  # idempotent -- the transition already ran
        # Latch BEFORE the first await inside _fence so a concurrent second
        # observation can never start a duplicate transition.
        self._fenced = True
        await self._fence(gen)

    async def _fence(self, observed_gen: int) -> None:
        """The deterministic ordered transition -- see the module docstring.

        Each step is fail-soft individually, but ALL are attempted, in order.
        """
        steps = (
            ("boundary", self._boundary_arm_fn or _default_boundary_arm),
            ("shutdown", self._shutdown_fn or _default_shutdown),
            ("capture", self._capture_fn or _default_capture),
            ("idle", self._idle_touch_fn or _default_idle_touch),
        )
        results = []
        for name, fn in steps:
            ok = False
            try:
                result = fn()
                if inspect.isawaitable(result):
                    await result
                ok = True
            except Exception:  # noqa: BLE001 -- one arm must not stop the rest
                logger.warning(
                    "[GenerationFence] arm %s FAILED (continuing)", name,
                    exc_info=True,
                )
            results.append((name, ok))
        logger.warning(
            "[GenerationFence] FENCED own_gen=%d observed_gen=%d arms=%s",
            self._own_gen,
            observed_gen,
            ",".join("%s=%s" % (n, o) for n, o in results),
        )


__all__ = ["GenerationFence", "HEARTBEAT_TOPIC"]
