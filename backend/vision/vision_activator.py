"""
Vision Activator — lazy on-demand vision pipeline startup.

Instead of gating vision behind JARVIS_VISION_LOOP_ENABLED env var,
the activator starts vision components on first ACTION command that
requires UI automation. Subsequent calls are instant (already running).

Usage:
    activator = VisionActivator.get_instance()
    result = await activator.run_goal("Open WhatsApp and send Zach 'what's up!'")
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class VisionActivator:
    """Lazy-start vision pipeline on first action command."""

    _instance: Optional["VisionActivator"] = None

    def __init__(self) -> None:
        self._activated = False
        self._activating = False
        self._lock = asyncio.Lock()
        self._jarvis_cu: Any = None
        self._intel_hub: Any = None
        self._frame_pipeline: Any = None

    @classmethod
    def get_instance(cls) -> "VisionActivator":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def set_instance(cls, inst: "VisionActivator") -> None:
        cls._instance = inst

    @property
    def is_active(self) -> bool:
        return self._activated

    async def ensure_vision(self) -> bool:
        """Ensure vision pipeline is running. Idempotent."""
        if self._activated:
            return True

        async with self._lock:
            if self._activated:
                return True
            if self._activating:
                return False

            self._activating = True
            try:
                logger.info("[VisionActivator] On-demand vision activation starting...")

                # 1. Start SHM capture pipeline (SCK in background thread)
                from backend.vision.realtime.frame_pipeline import FramePipeline
                self._frame_pipeline = FramePipeline(
                    use_sck=True, window_id=0, motion_detect=True,
                )
                await self._frame_pipeline.start()
                logger.info("[VisionActivator] FramePipeline: STARTED (60fps SHM)")

                # 2. Start JARVIS-CU orchestrator
                from backend.vision.jarvis_cu import JarvisCU
                self._jarvis_cu = JarvisCU()
                JarvisCU.set_instance(self._jarvis_cu)
                logger.info("[VisionActivator] JarvisCU: READY")

                # 3. Start Intelligence Hub and subscribe to frame feed
                # Non-fatal: CU works fine without the intelligence modules.
                try:
                    from backend.vision.intelligence.vision_intelligence_hub import (
                        VisionIntelligenceHub,
                    )
                    self._intel_hub = VisionIntelligenceHub()
                    # VisionIntelligenceHub uses __new__ singleton (no set_instance)
                    self._frame_pipeline.subscribe(self._intel_hub.on_frame)
                    logger.info("[VisionActivator] Intelligence Hub: WIRED (5 modules)")
                except Exception as hub_exc:
                    logger.warning(
                        "[VisionActivator] Intelligence Hub unavailable (non-fatal): %s",
                        hub_exc,
                    )

                self._activated = True
                logger.info("[VisionActivator] Vision pipeline ACTIVE")
                return True

            except Exception as exc:
                logger.error("[VisionActivator] Activation failed: %s", exc)
                return False
            finally:
                self._activating = False

    async def run_goal(self, goal: str) -> Dict[str, Any]:
        """Ensure vision is active, then run a goal via JARVIS-CU."""
        if not await self.ensure_vision():
            return {"success": False, "error": "Vision activation failed"}

        return await self._jarvis_cu.run(goal)

    async def shutdown(self) -> None:
        """Stop all vision components."""
        if self._frame_pipeline:
            await self._frame_pipeline.stop()
        self._activated = False
        logger.info("[VisionActivator] Vision pipeline stopped")
