"""Vision Bridge — on-demand 60fps capture + JarvisCU orchestration.

The vision pipeline bypasses Vercel entirely. FramePipeline captures
frames to SHM locally. JarvisCU's 3-layer step executor calls
Doubleword VL-235B and Claude Vision DIRECTLY from the Mac.

Activation: lazy — starts when first vision_task arrives or when
JARVIS_VISION_LOOP_ENABLED=true.
"""

import asyncio
import logging
import os
from typing import Any, Optional

logger = logging.getLogger("jarvis.brainstem.vision")


class VisionBridge:
    """On-demand vision pipeline manager.

    Lazy-initializes FramePipeline (60fps SHM capture) and JarvisCU
    (Computer Use orchestrator) when activate() is called.
    """

    def __init__(self) -> None:
        self._frame_pipeline: Any = None
        self._jarvis_cu: Any = None
        self._active: bool = False

    @property
    def is_active(self) -> bool:
        return self._active

    async def activate(self) -> bool:
        """Start the vision pipeline on demand."""
        if self._active:
            return True

        logger.info("[Vision] Activating vision pipeline...")
        try:
            success = await self._start_pipeline()
            if success:
                self._active = True
                logger.info("[Vision] Vision pipeline active (60fps)")
            return success
        except Exception as e:
            logger.error("[Vision] Activation failed: %s", e)
            return False

    async def deactivate(self) -> None:
        """Stop the vision pipeline and release resources."""
        if not self._active:
            return

        logger.info("[Vision] Deactivating vision pipeline...")
        if self._frame_pipeline is not None:
            try:
                await self._frame_pipeline.stop()
            except Exception as e:
                logger.warning("[Vision] FramePipeline stop error: %s", e)
            self._frame_pipeline = None

        self._jarvis_cu = None
        self._active = False
        logger.info("[Vision] Vision pipeline deactivated")

    async def execute_goal(self, goal: str, screenshot_b64: Optional[str] = None) -> Optional[dict]:
        """Execute a vision task via JarvisCU.

        Auto-activates the pipeline if not already running.

        Parameters
        ----------
        goal:
            Natural language description of the action to perform.
        screenshot_b64:
            Optional base64-encoded JPEG screenshot from the HUD.
            If provided, JarvisCU uses it for the planning phase instead
            of capturing from SHM. Verification frames between steps
            still come from SHM or CoreGraphics fallback.
        """
        if not self._active:
            activated = await self.activate()
            if not activated:
                logger.warning("[Vision] Cannot execute goal — pipeline failed to start")
                return None

        if self._jarvis_cu is None:
            logger.warning("[Vision] JarvisCU not available")
            return None

        logger.info("[Vision] Executing goal: %s (screenshot=%s)", goal[:80], "hud" if screenshot_b64 else "shm")

        # Decode HUD screenshot to numpy if provided
        initial_frame = None
        if screenshot_b64:
            initial_frame = self._decode_screenshot(screenshot_b64)

        try:
            result = await self._jarvis_cu.run(goal, initial_frame=initial_frame)
            return result
        except Exception as e:
            logger.error("[Vision] Goal execution failed: %s", e)
            return None

    @staticmethod
    def _decode_screenshot(b64_data: str) -> Optional[Any]:
        """Decode a base64 JPEG into a numpy RGB array for JarvisCU."""
        try:
            import base64
            import io
            import numpy as np
            from PIL import Image

            raw = base64.b64decode(b64_data)
            img = Image.open(io.BytesIO(raw)).convert("RGB")
            return np.array(img)
        except Exception as e:
            logger.warning("[Vision] Screenshot decode failed: %s", e)
            return None

    async def _start_pipeline(self) -> bool:
        """Initialize FramePipeline and JarvisCU from backend modules.

        These imports are lazy to avoid loading heavy backend deps at boot.
        The vision pipeline only starts when actually needed.

        FramePipeline is **optional** — if SCK lacks Screen Recording permission
        or SHM isn't ready, JarvisCU still initializes in degraded mode using
        ShmFrameReader static reads or black-frame fallback for verification.
        HUD-forwarded screenshots are used for the planning phase regardless.
        """
        # FramePipeline (optional) — 60fps motion-aware verification + settling.
        # A 5s timeout guards against SCK permission prompts hanging indefinitely.
        try:
            from backend.vision.realtime.frame_pipeline import FramePipeline
            fp = FramePipeline(use_sck=True, motion_detect=True)
            await asyncio.wait_for(fp.start(), timeout=5.0)
            self._frame_pipeline = fp
            logger.info("[Vision] FramePipeline started (60fps SHM)")
        except asyncio.TimeoutError:
            logger.warning(
                "[Vision] FramePipeline start timed out (5s) — "
                "degraded mode (fixed-delay settling, black-frame verification)"
            )
        except ImportError as e:
            logger.warning(
                "[Vision] FramePipeline not available: %s — "
                "degraded mode (fixed-delay settling, black-frame verification)", e
            )
        except Exception as e:
            logger.warning(
                "[Vision] FramePipeline start failed: %s — "
                "degraded mode (fixed-delay settling, black-frame verification)", e
            )

        # JarvisCU (required) — Computer Use orchestrator.
        # Works with or without FramePipeline: planning uses HUD screenshot,
        # execution uses Accessibility API → Doubleword → Claude cascade.
        try:
            from backend.vision.jarvis_cu import JarvisCU
            self._jarvis_cu = JarvisCU(frame_pipeline=self._frame_pipeline)
            logger.info(
                "[Vision] JarvisCU initialized (frame_pipeline=%s)",
                "60fps" if self._frame_pipeline else "none — degraded mode",
            )
        except ImportError as e:
            logger.error("[Vision] JarvisCU not available: %s", e)
            return False
        except Exception as e:
            logger.error("[Vision] JarvisCU init failed: %s", e)
            return False

        return True

    def should_auto_activate(self) -> bool:
        """Check if vision should start automatically at boot."""
        return os.environ.get(
            "JARVIS_VISION_LOOP_ENABLED", "false"
        ).lower() in ("true", "1", "yes")
