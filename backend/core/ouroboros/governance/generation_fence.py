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
pathways." The LOAD-BEARING terminators (review-corrected, 2026-07-04):

  0. CHOKEPOINT LATCH (arm 0 -- the hard gate, set FIRST, before the graceful
     arms): the process-global :func:`is_fenced` latch, consumed FAIL-CLOSED
     at the two universal mutation chokepoints -- ``ChangeEngine.execute``
     (every governed disk write funnels through it; the anti-venom
     ``_pre_write_gate`` chokepoint doctrine) and ``AutoCommitter.commit``
     (every autonomous commit). A fenced process therefore CANNOT complete an
     APPLY write or a commit -- including ops already mid-flight past
     GENERATE, the gap the flag-arm alone left open.
  1. SHUTDOWN REQUEST -- ``cooperative_shutdown.request()``: the process-global
     wind-down signal the harness itself uses at graceful shutdown. In-flight
     coroutines yield at their next safe boundary; no new work starts.

Composed defense-in-depth (real machinery, but NOT load-bearing for the
current process in default config -- review finding Important-2):

  2. BOUNDARY ARM -- ``autonomous_workspace._arm_boundary_flags()`` (the LR-A
     deterministic-lock force-arm of the Async Yield Matrix Sovereign Execution
     Boundary, PRD s51.11.35 / PR #69638). Arms the flag PAIR
     ``JARVIS_FILE_ISOLATION_ENABLED`` + ``JARVIS_EXECUTION_BOUNDARY_ENABLED``
     in-process, exactly the way its own regression spine arms it
     (``tests/governance/test_deterministic_isolation_lock.py``). CAVEAT: the
     Stage-B file-isolation consult is BOOT-TIME only
     (``resolve_loop_project_root`` runs at harness config-construction), and
     Stage-A commit denial is gated behind
     ``JARVIS_OPERATOR_COMMIT_AUTHORITY_ENABLED`` (default false) and scopes
     primary/main -- so on an already-booted default-config node the pair
     hardens the NEXT boot and OCA-armed setups; arms 0-1 fence the CURRENT
     process.
  3. CHECKPOINT CAPTURE -- ``fsm_checkpoint.capture_inflight(reason=
     "generation_fenced")``: SUSPEND every in-flight op into a signed
     checkpoint so the SUCCESSOR generation resumes the thought (Mandate 3:
     crypto/payload untouched -- only a new reason STRING flows through the
     existing signer).
  4. IDLE TOUCH -- deliberate no-op by default (Minor-4: the only simple idle
     marker, ``stream_heartbeat.pulse()``, marks FRESH activity -- arguably
     DELAYING idle-kill of a superseded node; the keeper reaps by manifest
     regardless). The seam stays injectable for a future genuine surface.

Each step is fail-soft INDIVIDUALLY (one failing arm never stops the others)
but the transition is deterministic: latch first, then all four attempted, in
order, exactly once per process (idempotent -- a second higher-gen observation
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
import threading
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# The keeper-heartbeat topic. The Body publishes it on its local trinity bus;
# the mac-side TrinityBusBridge outbound allowlist ("console.*",
# scripts/run_body_mode.py::_do_bridge) carries it across the WS link onto the
# Brain organism's trinity bus, where this fence subscribes.
HEARTBEAT_TOPIC = "console.keeper_heartbeat"

_FENCE_REASON = "generation_fenced"


# --------------------------------------------------------------------------- #
# Process-global chokepoint latch (arm 0 -- the hard gate).
#
# Module-level, thread-safe, ZERO asyncio dependency (the cooperative_shutdown
# idiom): consumable from any thread/loop, including the synchronous top of
# ChangeEngine.execute and AutoCommitter.commit. One-way in production; tests
# reset via _reset_for_tests().
# --------------------------------------------------------------------------- #
_latch_lock = threading.Lock()
_latch_fenced: bool = False
_latch_reason: str = ""


def _mark_fenced(reason: str = _FENCE_REASON) -> None:
    """Set the process-global fence latch. Idempotent; first reason sticks."""
    global _latch_fenced, _latch_reason
    with _latch_lock:
        if not _latch_fenced:
            _latch_reason = str(reason or _FENCE_REASON)
        _latch_fenced = True


def is_fenced() -> bool:
    """True once this process observed a higher Brain generation. Consumed
    FAIL-CLOSED at the mutation chokepoints (ChangeEngine / AutoCommitter)."""
    with _latch_lock:
        return _latch_fenced


def fence_reason() -> str:
    with _latch_lock:
        return _latch_reason


def _reset_for_tests() -> None:
    """Test hook -- clear the latch (production never unfences a process)."""
    global _latch_fenced, _latch_reason
    with _latch_lock:
        _latch_fenced = False
        _latch_reason = ""


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
    """Deliberate no-op (Minor-4, review-corrected).

    The only simple idle fast-path marker is ``stream_heartbeat.pulse()``,
    but pulsing marks FRESH activity -- which arguably DELAYS the idle-kill
    of a node we want gone. No existing marker accelerates idle-kill, and
    the keeper reaps superseded nodes by manifest regardless, so the
    truthful default is: do nothing. The ``idle_touch_fn`` seam stays
    injectable should a genuine surface appear."""
    return None


class GenerationFence:
    """Subscribe to keeper heartbeats; fence on a higher observed generation.

    All four graceful action seams are injectable (tests use recorders);
    ``None`` resolves to the live default lazily AT FENCE TIME (never at
    construction). Arm 0 -- the process-global chokepoint latch
    (:func:`is_fenced`) -- is intrinsic, not a seam: it fires first on every
    fence transition regardless of injected arms.

    Args:
        bus: the organism's ``TrinityEventBus`` (``subscribe``/``unsubscribe``).
        own_gen: THIS Brain's minted generation (``JARVIS_BRAIN_GENERATION``).
        boundary_arm_fn: seam for the Sovereign Execution Boundary arm.
        shutdown_fn: seam for the cooperative shutdown request.
        capture_fn: seam for the in-flight checkpoint capture.
        idle_touch_fn: seam for the idle touch (default: truthful no-op).
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

        Arm 0 (the hard gate) fires FIRST: the process-global chokepoint
        latch, pure assignment, cannot fail. Then the four graceful arms,
        each fail-soft individually, ALL attempted, in order.
        """
        # Arm 0 -- the chokepoint latch. From this line on, ChangeEngine
        # refuses every APPLY write and AutoCommitter refuses every commit
        # in this process, mid-flight ops included.
        _mark_fenced(_FENCE_REASON)
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
            "[GenerationFence] FENCED own_gen=%d observed_gen=%d "
            "latch=True arms=%s",
            self._own_gen,
            observed_gen,
            ",".join("%s=%s" % (n, o) for n, o in results),
        )


__all__ = [
    "GenerationFence",
    "HEARTBEAT_TOPIC",
    "fence_reason",
    "is_fenced",
]
