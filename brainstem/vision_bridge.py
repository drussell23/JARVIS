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

    async def execute_goal(self, goal: str) -> Optional[dict]:
        """Execute a vision task via JarvisCU.

        Auto-activates the pipeline if not already running.
        """
        if not self._active:
            activated = await self.activate()
            if not activated:
                logger.warning("[Vision] Cannot execute goal — pipeline failed to start")
                return None

        if self._jarvis_cu is None:
            logger.warning("[Vision] JarvisCU not available")
            return None

        logger.info("[Vision] Executing goal: %s", goal[:80])
        try:
            result = await self._jarvis_cu.execute_goal(goal)
            return result
        except Exception as e:
            logger.error("[Vision] Goal execution failed: %s", e)
            return None

    async def _start_pipeline(self) -> bool:
        """Initialize FramePipeline and JarvisCU from backend modules.

        These imports are lazy to avoid loading heavy backend deps at boot.
        The vision pipeline only starts when actually needed.
        """
        try:
            from backend.vision.realtime.frame_pipeline import FramePipeline
            self._frame_pipeline = FramePipeline(
                use_sck=True,
                motion_detect=True,
            )
            await self._frame_pipeline.start()
            logger.info("[Vision] FramePipeline started (60fps SHM)")
        except ImportError as e:
            logger.warning("[Vision] FramePipeline not available: %s", e)
            return False
        except Exception as e:
            logger.error("[Vision] FramePipeline start failed: %s", e)
            return False

        try:
            from backend.vision.jarvis_cu import JarvisCU
            self._jarvis_cu = JarvisCU()
            logger.info("[Vision] JarvisCU initialized")
        except ImportError as e:
            logger.warning("[Vision] JarvisCU not available: %s", e)
            # Pipeline runs without CU — can still capture frames
        except Exception as e:
            logger.error("[Vision] JarvisCU init failed: %s", e)

        return True

    def should_auto_activate(self) -> bool:
        """Check if vision should start automatically at boot."""
        return os.environ.get(
            "JARVIS_VISION_LOOP_ENABLED", "false"
        ).lower() in ("true", "1", "yes")
