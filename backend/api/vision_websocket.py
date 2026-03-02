"""
Vision WebSocket Fix - Provides vision_manager for vision_command_handler.
"""

import inspect
import logging
import sys
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Canonicalize module identity so mixed `api.*` / `backend.api.*` imports
# share one global vision_manager instance instead of silently forking state.
_this_module = sys.modules.get(__name__)
if _this_module is not None:
    if __name__.startswith("backend."):
        sys.modules.setdefault("api.vision_websocket", _this_module)
    elif __name__ == "api.vision_websocket":
        sys.modules.setdefault("backend.api.vision_websocket", _this_module)


def _is_valid_vision_analyzer(analyzer: Any) -> bool:
    """Return True when the published analyzer satisfies the capture contract."""
    if analyzer is None or inspect.isclass(analyzer):
        return False
    return callable(getattr(analyzer, "capture_screen", None))

class VisionManager:
    """Simple vision manager that wraps the vision analyzer"""
    
    def __init__(self):
        self.vision_analyzer = None
        self._monitoring_active = False

    def get_vision_analyzer(self) -> Optional[Any]:
        """Return the bound analyzer only when it satisfies the runtime contract."""
        if _is_valid_vision_analyzer(self.vision_analyzer):
            return self.vision_analyzer
        if self.vision_analyzer is not None:
            logger.error(
                "Rejected invalid vision analyzer publication: %r",
                self.vision_analyzer,
            )
            self.vision_analyzer = None
        return None

    def set_vision_analyzer(self, analyzer):
        """Set the vision analyzer."""
        if analyzer is None:
            self.vision_analyzer = None
            logger.info("Vision analyzer cleared")
            return False
        if not _is_valid_vision_analyzer(analyzer):
            logger.error(
                "Refusing to publish invalid vision analyzer: %r",
                analyzer,
            )
            return False
        self.vision_analyzer = analyzer
        logger.info("Vision analyzer set: True")
        return True

    async def start_monitoring(self):
        """Start screen monitoring"""
        analyzer = self.get_vision_analyzer()
        if not analyzer:
            raise Exception("Vision analyzer not available")
        start_video_streaming = getattr(analyzer, "start_video_streaming", None)
        if not callable(start_video_streaming):
            raise TypeError("Vision analyzer does not implement start_video_streaming()")

        # Start video streaming
        result = await start_video_streaming()
        if result.get('success'):
            self._monitoring_active = True
            logger.info("Screen monitoring started successfully")
        else:
            raise Exception(f"Failed to start monitoring: {result.get('error')}")

    async def stop_monitoring(self):
        """Stop screen monitoring"""
        analyzer = self.get_vision_analyzer()
        if not analyzer:
            raise Exception("Vision analyzer not available")
        stop_video_streaming = getattr(analyzer, "stop_video_streaming", None)
        if not callable(stop_video_streaming):
            raise TypeError("Vision analyzer does not implement stop_video_streaming()")

        # Stop video streaming
        await stop_video_streaming()
        self._monitoring_active = False
        logger.info("Screen monitoring stopped")

    async def capture_screen(self, multi_space=False, space_number=None):
        """Capture current screen with multi-space support"""
        analyzer = self.get_vision_analyzer()
        if not analyzer:
            raise Exception("Vision analyzer not available")

        # Use the correct method name and pass multi-space parameters
        return await analyzer.capture_screen(
            multi_space=multi_space,
            space_number=space_number
        )

    async def analyze_screen(self, prompt: str):
        """Analyze screen with prompt"""
        analyzer = self.get_vision_analyzer()
        if not analyzer:
            raise Exception("Vision analyzer not available")

        # Capture screen first
        screenshot = await self.capture_screen()
        if screenshot is None:
            raise Exception("Failed to capture screen")

        analyze_image_with_prompt = getattr(analyzer, "analyze_image_with_prompt", None)
        if not callable(analyze_image_with_prompt):
            raise TypeError("Vision analyzer does not implement analyze_image_with_prompt()")

        # Analyze with prompt
        result = await analyze_image_with_prompt(screenshot, prompt)
        return result

# Create global vision manager instance
vision_manager = VisionManager()

def set_vision_analyzer(analyzer):
    """Set the vision analyzer in the global vision manager"""
    vision_manager.set_vision_analyzer(analyzer)
    logger.info("Vision analyzer set in vision_websocket module")
