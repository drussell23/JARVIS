"""
JarvisCU -- Local Computer Use Orchestrator
============================================

Main entry point for JARVIS local Computer Use.  Orchestrates:

1. **Planning** -- Claude Vision decomposes a natural-language goal into
   atomic ``CUStep`` objects via :class:`CUTaskPlanner`.
2. **Execution** -- Each step runs through a 3-layer cascade
   (Accessibility API -> Doubleword 235B -> Claude Vision) via
   :class:`CUStepExecutor`.
3. **Retry** -- Failed steps are retried up to ``MAX_RETRIES`` times with
   a fresh screen frame between attempts.
4. **Verification** -- 60fps SHM frames are captured between steps so the
   executor always has the latest screen state.

Singleton pattern: ``JarvisCU()`` auto-registers as the global instance.
Retrieve later with ``JarvisCU.get_instance()``.

Environment variables (all optional)
-------------------------------------
``JARVIS_CU_MAX_RETRIES``  -- max retries per failed step (default 1)
``JARVIS_CU_STEP_DELAY_S`` -- pause between steps in seconds (default 0.3)
``JARVIS_CU_TIMEOUT_S``    -- total run timeout in seconds (default 120)
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import defaultdict
from typing import Any, Dict, List, Optional

import numpy as np

from backend.vision.cu_task_planner import CUTaskPlanner
from backend.vision.cu_step_executor import CUStepExecutor
from backend.vision.shm_frame_reader import ShmFrameReader

# Optional: FramePipeline for 60fps motion-aware verification
try:
    from backend.vision.realtime.frame_pipeline import FramePipeline, MotionDetector
    _HAS_FRAME_PIPELINE = True
except ImportError:
    _HAS_FRAME_PIPELINE = False

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Environment-driven tunables -- zero hardcoding
# ---------------------------------------------------------------------------

def _env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, str(default)))


def _env_float(name: str, default: float) -> float:
    return float(os.environ.get(name, str(default)))


# ---------------------------------------------------------------------------
# Black frame fallback (used when SHM is unavailable)
# ---------------------------------------------------------------------------

_BLACK_FRAME_H = 1080
_BLACK_FRAME_W = 1920
_BLACK_FRAME_C = 4


def _make_black_frame() -> np.ndarray:
    """Return a 1920x1080 BGRA black frame (no allocation cache -- cheap)."""
    return np.zeros((_BLACK_FRAME_H, _BLACK_FRAME_W, _BLACK_FRAME_C), dtype=np.uint8)


# ---------------------------------------------------------------------------
# JarvisCU
# ---------------------------------------------------------------------------

class JarvisCU:
    """Local Computer Use orchestrator.

    Usage::

        cu = JarvisCU()
        result = await cu.run("Open WhatsApp and send Zach 'what's up!'")
        print(result)

    The constructor auto-registers the instance as the singleton so
    ``JarvisCU.get_instance()`` returns it immediately.
    """

    _instance: Optional["JarvisCU"] = None

    # -- Singleton helpers --------------------------------------------------

    @classmethod
    def get_instance(cls) -> Optional["JarvisCU"]:
        """Return the current singleton, or ``None`` if never created."""
        return cls._instance

    @classmethod
    def set_instance(cls, instance: "JarvisCU") -> None:
        """Explicitly set the singleton (useful in tests)."""
        cls._instance = instance

    # -- Construction -------------------------------------------------------

    def __init__(self, frame_pipeline: Optional[Any] = None) -> None:
        self._planner = CUTaskPlanner()
        self._executor = CUStepExecutor()

        # 60fps FramePipeline — motion-aware verification between steps.
        # When available, replaces both SHM static reads AND fixed delays.
        self._frame_pipeline = frame_pipeline

        # Fallback: SHM static reader (no motion awareness, but still 60fps frames)
        self._shm: Optional[ShmFrameReader] = None
        if self._frame_pipeline is None:
            try:
                reader = ShmFrameReader()
                if reader.open():
                    self._shm = reader
                    logger.info("[JarvisCU] SHM frame reader attached (static mode)")
                else:
                    logger.info("[JarvisCU] SHM not available -- will use black frames")
            except Exception as exc:
                logger.debug("[JarvisCU] SHM init failed: %s", exc)
        else:
            logger.info("[JarvisCU] FramePipeline attached (60fps motion-aware mode)")

        # Motion detector for settling detection (used with FramePipeline)
        self._motion_detector: Optional[Any] = None
        if _HAS_FRAME_PIPELINE and self._frame_pipeline is not None:
            self._motion_detector = MotionDetector(
                threshold=_env_float("VISION_MOTION_THRESHOLD", 0.05),
                debounce_ms=0,  # No debounce for settle detection — we want every change
            )

        # Read tunables from environment at construction time so they can be
        # overridden per-instance in tests via env-var patching before __init__.
        self._max_retries = _env_int("JARVIS_CU_MAX_RETRIES", 1)
        self._step_delay_s = _env_float("JARVIS_CU_STEP_DELAY_S", 0.3)
        self._timeout_s = _env_float("JARVIS_CU_TIMEOUT_S", 120.0)
        # Max time to wait for screen to settle after an action (motion-aware mode)
        self._settle_timeout_s = _env_float("JARVIS_CU_SETTLE_TIMEOUT_S", 2.0)
        self._settle_stable_count = _env_int("JARVIS_CU_SETTLE_STABLE_COUNT", 3)

        # Self-register as singleton (callers WILL bypass factory)
        self.__class__._instance = self

    # -- Frame acquisition --------------------------------------------------

    def _get_frame(self) -> np.ndarray:
        """Get the latest screen frame.

        Priority:
        1. FramePipeline.latest_frame (60fps, motion-filtered)
        2. SHM static read (60fps, no motion awareness)
        3. Black frame fallback
        """
        # 60fps FramePipeline — best path
        if self._frame_pipeline is not None:
            latest = self._frame_pipeline.latest_frame
            if latest is not None:
                return latest.data

        # SHM static read — still 60fps but no motion filtering
        if self._shm is not None:
            try:
                frame, _counter = self._shm.read_latest()
                if frame is not None:
                    return frame
            except Exception as exc:
                logger.debug("[JarvisCU] SHM read failed: %s", exc)

        return _make_black_frame()

    async def _wait_for_settle(self) -> np.ndarray:
        """Wait for the screen to stop changing after an action.

        Uses the FramePipeline's 60fps stream + MotionDetector to detect
        when the screen has stabilized (no motion for N consecutive frames).
        Falls back to fixed delay when FramePipeline is not available.

        Returns the settled frame for the next step's verification.
        """
        # No FramePipeline — fall back to fixed delay + static frame grab
        if self._frame_pipeline is None or self._motion_detector is None:
            if self._step_delay_s > 0:
                await asyncio.sleep(self._step_delay_s)
            return self._get_frame()

        # Motion-aware settling: poll 60fps frames until N consecutive
        # frames show no motion (screen has stopped changing).
        self._motion_detector.reset()
        stable_count = 0
        t_start = time.monotonic()
        settled_frame = self._get_frame()

        while (time.monotonic() - t_start) < self._settle_timeout_s:
            frame_data = await self._frame_pipeline.get_frame(timeout_s=0.1)
            if frame_data is None:
                # No new frame in 100ms — screen is static
                stable_count += 1
            else:
                settled_frame = frame_data.data
                if self._motion_detector.detect_change(frame_data.data):
                    # Screen still changing — reset stability counter
                    stable_count = 0
                else:
                    stable_count += 1

            if stable_count >= self._settle_stable_count:
                elapsed_ms = (time.monotonic() - t_start) * 1000
                logger.debug(
                    "[JarvisCU] Screen settled after %.0fms (%d stable frames)",
                    elapsed_ms, stable_count,
                )
                return settled_frame

        # Timeout — use whatever we have
        elapsed_ms = (time.monotonic() - t_start) * 1000
        logger.debug("[JarvisCU] Settle timeout after %.0fms — proceeding", elapsed_ms)
        return settled_frame

    # -- Main entry point ---------------------------------------------------

    async def run(
        self,
        goal: str,
        initial_frame: Optional[np.ndarray] = None,
    ) -> Dict[str, Any]:
        """Plan and execute *goal*, returning a result dict.

        Parameters
        ----------
        goal:
            Natural language description of the task to accomplish.
        initial_frame:
            Optional pre-captured screenshot (numpy RGB array) from the HUD.
            If provided, used for the planning phase instead of reading from
            SHM. Verification frames between execution steps still come from
            SHM or the black-frame fallback.

        Returns
        -------
        dict
            ``success``          -- bool, True only if ALL steps completed.
            ``steps_completed``  -- int, how many steps finished successfully.
            ``steps_total``      -- int, total planned steps.
            ``step_results``     -- list of per-step result objects.
            ``elapsed_s``        -- float, wall-clock seconds.
            ``error``            -- str or None, first fatal error message.
            ``layers_used``      -- dict mapping layer name -> count.
        """
        t_start = time.monotonic()
        step_results: List[Any] = []
        layers_used: Dict[str, int] = defaultdict(int)
        steps_completed = 0
        error: Optional[str] = None

        # -- Phase 1: Plan --------------------------------------------------
        try:
            # Use HUD screenshot for planning if provided, else capture from SHM
            frame = initial_frame if initial_frame is not None else self._get_frame()
            if initial_frame is not None:
                logger.info("[JarvisCU] Using HUD screenshot for planning (%s)", initial_frame.shape)
            steps = await self._planner.plan_goal(goal, frame)
        except Exception as exc:
            elapsed = time.monotonic() - t_start
            logger.error("[JarvisCU] Planning failed: %s", exc)
            return {
                "success": False,
                "steps_completed": 0,
                "steps_total": 0,
                "step_results": [],
                "elapsed_s": elapsed,
                "error": str(exc),
                "layers_used": dict(layers_used),
            }

        steps_total = len(steps)
        if steps_total == 0:
            elapsed = time.monotonic() - t_start
            # v308.0: 0 planned steps means the planner couldn't figure out
            # what to do (likely bad/black frame).  This is NOT success.
            logger.warning("[JarvisCU] Planner returned 0 steps for goal: %s", goal)
            return {
                "success": False,
                "steps_completed": 0,
                "steps_total": 0,
                "step_results": [],
                "elapsed_s": elapsed,
                "error": "Planner generated 0 steps — likely bad screenshot or unclear goal",
                "layers_used": dict(layers_used),
            }

        logger.info("[JarvisCU] Plan: %d steps for goal: %s", steps_total, goal)

        # -- Phase 2: Execute -----------------------------------------------
        for idx, step in enumerate(steps):
            # Timeout guard
            elapsed_so_far = time.monotonic() - t_start
            if elapsed_so_far >= self._timeout_s:
                error = (
                    f"Timeout after {elapsed_so_far:.1f}s "
                    f"(limit {self._timeout_s}s) at step {idx}/{steps_total}"
                )
                logger.warning("[JarvisCU] %s", error)
                break

            # Execute with retries
            step_ok = False
            last_result = None

            for attempt in range(1 + self._max_retries):
                frame = self._get_frame()
                result = await self._executor.execute_step(step, frame)
                last_result = result

                if getattr(result, "success", False):
                    step_ok = True
                    layer = getattr(result, "layer_used", "unknown")
                    layers_used[layer] += 1
                    logger.info(
                        "[JarvisCU] Step %d/%d (%s) succeeded via %s (attempt %d)",
                        idx + 1, steps_total,
                        getattr(step, "step_id", "?"),
                        layer, attempt + 1,
                    )
                    break
                else:
                    logger.warning(
                        "[JarvisCU] Step %d/%d (%s) failed attempt %d/%d: %s",
                        idx + 1, steps_total,
                        getattr(step, "step_id", "?"),
                        attempt + 1, 1 + self._max_retries,
                        getattr(result, "error", "unknown"),
                    )

            step_results.append(last_result)

            if step_ok:
                steps_completed += 1
            else:
                step_id = getattr(step, "step_id", f"step-{idx}")
                error = (
                    f"Step {step_id} failed after {1 + self._max_retries} "
                    f"attempt(s): {getattr(last_result, 'error', 'unknown')}"
                )
                logger.error("[JarvisCU] %s", error)
                break

            # Brief pause between steps (let UI settle)
            if self._step_delay_s > 0 and idx < steps_total - 1:
                await asyncio.sleep(self._step_delay_s)

        elapsed = time.monotonic() - t_start
        success = steps_completed == steps_total and error is None

        logger.info(
            "[JarvisCU] Done: %d/%d steps, success=%s, %.1fs",
            steps_completed, steps_total, success, elapsed,
        )

        return {
            "success": success,
            "steps_completed": steps_completed,
            "steps_total": steps_total,
            "step_results": step_results,
            "elapsed_s": elapsed,
            "error": error,
            "layers_used": dict(layers_used),
        }
