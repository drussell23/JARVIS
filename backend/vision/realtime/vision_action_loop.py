"""
VisionActionLoop -- the real-time vision orchestrator.

Wires: FramePipeline + StateMachine + VisionRouter + PrecheckGate +
       Fusion + ActionExecutor + Verifier + KnowledgeFabric + Metrics.

``execute_action()`` is the **single public entry point** for all
vision-based UI actions.  Callers never need to touch internal components.

Lifecycle::

    loop = VisionActionLoop(use_sck=False)
    await loop.start()                         # IDLE -> WATCHING
    result = await loop.execute_action(
        target_description="the blue Submit button",
        action_type="click",
    )
    await loop.stop()                          # -> IDLE

Retry policy: bounded by ``VisionStateMachine.MAX_RETRIES`` (default 2).
Verification failures trigger a re-route + re-execute cycle.  Action-
executor failures are terminal (no retry).

Metrics: every ``execute_action()`` emits a flat dict via
``on_action_record`` when set.  See :mod:`backend.vision.realtime.metrics`.

Spec: Sections 2-9 of realtime-vision-action-loop-design.md
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple

import numpy as np

from backend.knowledge.fabric import KnowledgeFabric
from backend.vision.realtime.action_executor import (
    ActionExecutor,
    ActionRequest,
    ActionResult,
    ActionType,
)
from backend.vision.realtime.frame_pipeline import FramePipeline
from backend.vision.realtime.metrics import build_action_record
from backend.vision.realtime.precheck_gate import PrecheckGate
from backend.vision.realtime.states import (
    VisionEvent,
    VisionState,
    VisionStateMachine,
    TransitionError,
)
from backend.vision.realtime.verification import (
    ActionVerifier,
    VerificationResult,
    VerificationStatus,
)
from backend.vision.realtime.vision_router import (
    VisionQuery,
    VisionRouter,
    VisionRouterResult,
    VisionTier,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Environment-driven tunables -- no hardcoding
# ---------------------------------------------------------------------------
_VERIFICATION_DELAY_S = float(
    os.environ.get("VISION_VERIFICATION_DELAY_S", "0.15")
)
"""Seconds to wait after action dispatch before capturing the verification frame."""

_DEFAULT_SCROLL_AMOUNT = int(
    os.environ.get("VISION_DEFAULT_SCROLL_AMOUNT", "-3")
)
"""Default scroll clicks when action_type is scroll and no explicit amount."""


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class VisionActionResult:
    """Outcome of a single ``execute_action()`` call.

    Every field is populated regardless of success/failure so callers can
    always inspect the full decision trail.
    """

    success: bool
    coords: Optional[Tuple[int, int]]
    action_type: str
    action_id: str
    confidence: float
    tier_used: str
    verification_status: str
    latency_ms: float
    failed_guards: List[str] = field(default_factory=list)
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# VisionActionLoop
# ---------------------------------------------------------------------------

class VisionActionLoop:
    """Real-time vision orchestrator.

    Wires every vision subsystem into a coherent async pipeline.

    Parameters
    ----------
    use_sck:
        Forwarded to :class:`FramePipeline`.  ``False`` for tests.
    knowledge_fabric:
        Optional pre-built fabric instance.  Created automatically if
        ``None``.
    """

    def __init__(
        self,
        use_sck: bool = False,
        knowledge_fabric: Optional[KnowledgeFabric] = None,
    ) -> None:
        # --- Core components ---
        self._state_machine = VisionStateMachine()
        self._frame_pipeline = FramePipeline(use_sck=use_sck)
        self._knowledge_fabric = (
            knowledge_fabric if knowledge_fabric is not None
            else KnowledgeFabric()
        )
        self._vision_router = VisionRouter(fabric=self._knowledge_fabric)
        self._precheck = PrecheckGate()
        self._action_executor = ActionExecutor()
        self._verifier = ActionVerifier()

        # --- Metrics callback ---
        self.on_action_record: Optional[Callable[[dict], None]] = None

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def state(self) -> VisionState:
        """Current state of the underlying state machine."""
        return self._state_machine.state

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the loop: IDLE -> WATCHING.

        Idempotent -- safe to call when already started.
        """
        if self._state_machine.state != VisionState.IDLE:
            return
        await self._frame_pipeline.start()
        self._state_machine.transition(VisionEvent.START)
        logger.info("VisionActionLoop started (state=%s)", self.state)

    async def stop(self) -> None:
        """Stop the loop and return to IDLE.

        Idempotent -- safe to call when already stopped.
        """
        if self._state_machine.state == VisionState.IDLE:
            return
        await self._frame_pipeline.stop()
        self._state_machine.transition(VisionEvent.STOP)
        logger.info("VisionActionLoop stopped (state=%s)", self.state)

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    async def execute_action(
        self,
        target_description: str,
        action_type: str = "click",
        target_text: str = "",
        coords_hint: Optional[Tuple[int, int]] = None,
        intent_timestamp: Optional[float] = None,
        scroll_amount: Optional[int] = None,
    ) -> VisionActionResult:
        """Execute a vision-guided UI action.

        This is the **only** public method callers need.

        Parameters
        ----------
        target_description:
            Natural-language description of the target element.
        action_type:
            One of ``"click"``, ``"type"``, ``"scroll"``.
        target_text:
            Text to type (for ``"type"`` actions) or text-match hint for
            element lookup.
        coords_hint:
            Approximate ``(x, y)`` if known -- seeds L1 spatial lookup.
        intent_timestamp:
            ``time.time()``-style float.  Defaults to now.
        scroll_amount:
            Number of scroll clicks (negative = down).  Defaults to env
            ``VISION_DEFAULT_SCROLL_AMOUNT``.

        Returns
        -------
        VisionActionResult
        """
        t0 = time.monotonic()
        action_id = str(uuid.uuid4())
        if intent_timestamp is None:
            intent_timestamp = time.time()

        retry_count = 0
        max_retries = self._state_machine.MAX_RETRIES
        last_error: Optional[str] = None
        last_failed_guards: List[str] = []
        last_tier: str = ""
        last_confidence: float = 0.0
        last_coords: Optional[Tuple[int, int]] = None
        last_verification_status: str = ""

        while retry_count <= max_retries:
            # ----------------------------------------------------------
            # 1. Build vision query
            # ----------------------------------------------------------
            query = VisionQuery(
                target_description=target_description,
                target_text=target_text,
                coords_hint=coords_hint,
            )

            # ----------------------------------------------------------
            # 2. Route through VisionRouter
            # ----------------------------------------------------------
            try:
                router_result: VisionRouterResult = await self._vision_router.route(query)
            except Exception as exc:
                logger.error("VisionRouter raised: %s", exc)
                last_error = f"VisionRouter error: {exc}"
                break

            last_tier = str(router_result.tier)
            last_confidence = router_result.confidence
            last_coords = router_result.coords

            # ----------------------------------------------------------
            # 3. Check for DEGRADED / no coords
            # ----------------------------------------------------------
            if router_result.tier == VisionTier.DEGRADED or router_result.coords is None:
                last_error = (
                    "Vision unavailable (DEGRADED)" if router_result.tier == VisionTier.DEGRADED
                    else "Router returned no coordinates"
                )
                break

            # ----------------------------------------------------------
            # 4. PRECHECK gate
            # ----------------------------------------------------------
            precheck_result = self._precheck.check(
                frame_age_ms=router_result.latency_ms,
                fused_confidence=router_result.confidence,
                action_id=action_id,
                action_type=action_type,
                target_task_type=action_type,
                intent_timestamp=intent_timestamp,
                is_degraded=False,
            )

            if not precheck_result.passed:
                last_failed_guards = precheck_result.failed_guards
                last_error = f"PRECHECK failed: {precheck_result.failed_guards}"
                logger.info(
                    "PRECHECK blocked action %s: %s",
                    action_id, precheck_result.failed_guards,
                )
                break

            # ----------------------------------------------------------
            # 5. Capture pre-action frame
            # ----------------------------------------------------------
            pre_frame = await self._capture_pre_action_frame()

            # ----------------------------------------------------------
            # 6. Build and execute action
            # ----------------------------------------------------------
            action_type_enum = self._resolve_action_type(action_type)
            request = ActionRequest(
                action_id=action_id,
                action_type=action_type_enum,
                coords=router_result.coords if action_type_enum == ActionType.CLICK else None,
                text=target_text if action_type_enum == ActionType.TYPE else None,
                scroll_amount=(
                    scroll_amount if scroll_amount is not None else _DEFAULT_SCROLL_AMOUNT
                ) if action_type_enum == ActionType.SCROLL else None,
            )

            action_result: ActionResult = await self._action_executor.execute(request)

            if not action_result.success:
                last_error = action_result.error or "Action execution failed"
                logger.warning(
                    "Action %s failed: %s", action_id, last_error,
                )
                break  # No retry on executor failure

            # Commit action for idempotency
            self._precheck.commit_action(action_id)

            # ----------------------------------------------------------
            # 7. Verification
            # ----------------------------------------------------------
            await asyncio.sleep(_VERIFICATION_DELAY_S)
            post_frame = await self._capture_verification_frame()

            verification = self._run_verification(
                action_type=action_type,
                pre_frame=pre_frame,
                post_frame=post_frame,
                coords=router_result.coords,
                element_data=router_result.element_data,
            )

            last_verification_status = str(verification.status) if verification else ""

            # ----------------------------------------------------------
            # 8. Check verification outcome
            # ----------------------------------------------------------
            if verification is None or str(verification.status).upper() in (
                "SUCCESS", VerificationStatus.SUCCESS.value.upper(),
                "INCONCLUSIVE", VerificationStatus.INCONCLUSIVE.value.upper(),
            ):
                # Success or inconclusive -- accept the action
                latency_ms = (time.monotonic() - t0) * 1000
                self._emit_record(
                    action_id=action_id,
                    target_description=target_description,
                    coords=router_result.coords,
                    confidence=router_result.confidence,
                    precheck_passed=True,
                    failed_guards=[],
                    action_type=action_type,
                    backend_used=router_result.backend_used,
                    latency_ms=latency_ms,
                    verification_result=last_verification_status,
                    retry_count=retry_count,
                    tier_used=last_tier,
                    success=True,
                )
                return VisionActionResult(
                    success=True,
                    coords=router_result.coords,
                    action_type=action_type,
                    action_id=action_id,
                    confidence=router_result.confidence,
                    tier_used=last_tier,
                    verification_status=last_verification_status,
                    latency_ms=latency_ms,
                )

            # Verification failed -- retry
            retry_count += 1
            # Generate a new action_id for the retry so idempotency doesn't block
            action_id = str(uuid.uuid4())
            # Refresh intent timestamp for the retry
            intent_timestamp = time.time()
            logger.info(
                "Verification failed (status=%s), retry %d/%d",
                verification.status, retry_count, max_retries,
            )

        # ----------------------------------------------------------
        # Exhausted retries or broke out of loop on error
        # ----------------------------------------------------------
        latency_ms = (time.monotonic() - t0) * 1000
        self._emit_record(
            action_id=action_id,
            target_description=target_description,
            coords=last_coords,
            confidence=last_confidence,
            precheck_passed=len(last_failed_guards) == 0,
            failed_guards=last_failed_guards,
            action_type=action_type,
            backend_used=router_result.backend_used if 'router_result' in dir() else "none",
            latency_ms=latency_ms,
            verification_result=last_verification_status,
            retry_count=retry_count,
            tier_used=last_tier,
            success=False,
            error=last_error,
        )
        return VisionActionResult(
            success=False,
            coords=last_coords,
            action_type=action_type,
            action_id=action_id,
            confidence=last_confidence,
            tier_used=last_tier,
            verification_status=last_verification_status,
            latency_ms=latency_ms,
            failed_guards=last_failed_guards,
            error=last_error,
        )

    # ------------------------------------------------------------------
    # Frame capture helpers
    # ------------------------------------------------------------------

    async def _capture_pre_action_frame(self) -> Optional[np.ndarray]:
        """Capture a frame for the pre-action baseline.

        Tries the frame pipeline first; falls back to None (verification
        will be INCONCLUSIVE).
        """
        frame_data = await self._frame_pipeline.get_frame(timeout_s=0.5)
        if frame_data is not None:
            return frame_data.data
        return None

    async def _capture_verification_frame(self) -> Optional[np.ndarray]:
        """Capture a frame after the action for verification.

        Same strategy as pre-action capture.
        """
        frame_data = await self._frame_pipeline.get_frame(timeout_s=0.5)
        if frame_data is not None:
            return frame_data.data
        return None

    # ------------------------------------------------------------------
    # Verification dispatch
    # ------------------------------------------------------------------

    def _run_verification(
        self,
        action_type: str,
        pre_frame: Optional[np.ndarray],
        post_frame: Optional[np.ndarray],
        coords: Optional[Tuple[int, int]],
        element_data: Optional[dict],
    ) -> Optional[VerificationResult]:
        """Dispatch to the correct verify_* method based on action_type.

        Returns None if frames are unavailable (treated as INCONCLUSIVE by
        the caller).
        """
        if pre_frame is None or post_frame is None:
            return None

        if action_type == "click" and coords is not None:
            return self._verifier.verify_click(
                before=pre_frame,
                after=post_frame,
                coords=coords,
            )

        if action_type == "type":
            # Try to extract a bounding box from element_data
            bbox = None
            if element_data and "bbox" in element_data:
                bbox = element_data["bbox"]
            if bbox and len(bbox) == 4:
                return self._verifier.verify_type(
                    before=pre_frame,
                    after=post_frame,
                    target_region=tuple(bbox),
                )
            # Fallback: use full frame as region
            h, w = pre_frame.shape[:2]
            return self._verifier.verify_type(
                before=pre_frame,
                after=post_frame,
                target_region=(0, 0, w, h),
            )

        if action_type == "scroll":
            return self._verifier.verify_scroll(
                before=pre_frame,
                after=post_frame,
                direction="down",
            )

        # Unknown action type -- skip verification
        return None

    # ------------------------------------------------------------------
    # Action type resolution
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_action_type(action_type: str) -> ActionType:
        """Map a string action type to the ActionType enum.

        Falls back to CLICK for unrecognised values.
        """
        _MAP = {
            "click": ActionType.CLICK,
            "type": ActionType.TYPE,
            "scroll": ActionType.SCROLL,
        }
        return _MAP.get(action_type.lower(), ActionType.CLICK)

    # ------------------------------------------------------------------
    # Metrics emission
    # ------------------------------------------------------------------

    def _emit_record(self, **kwargs) -> None:
        """Build and emit an action record if the callback is set."""
        if self.on_action_record is None:
            return
        record = build_action_record(**kwargs)
        try:
            self.on_action_record(record)
        except Exception as exc:
            logger.warning("on_action_record callback raised: %s", exc)
