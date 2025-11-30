"""
Display Routes for Multi-Monitor API
=====================================

REST API endpoints for display detection and management.

Endpoints:
- GET /vision/displays - Get all displays with space mappings
- GET /vision/displays/{display_id} - Get specific display info
- POST /vision/displays/{display_id}/capture - Capture screenshot of display

Author: Derek Russell
Date: 2025-10-14
"""

from fastapi import APIRouter, HTTPException
from typing import Dict, Any
import logging

router = APIRouter(prefix="/vision", tags=["displays"])
logger = logging.getLogger(__name__)


@router.get("/displays")
async def get_displays() -> Dict[str, Any]:
    """
    Get all connected displays with space mappings

    Returns:
        {
            "total_displays": int,
            "displays": [
                {
                    "id": int,
                    "name": str,
                    "resolution": [width, height],
                    "position": [x, y],
                    "is_primary": bool,
                    "spaces": [space_ids]
                }
            ],
            "space_mappings": {space_id: display_id}
        }
    """
    try:
        from vision.multi_monitor_detector import MultiMonitorDetector

        detector = MultiMonitorDetector()
        summary = await detector.get_display_summary()

        return summary

    except Exception as e:
        logger.error(f"Error getting displays: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/displays/stats")
async def get_display_stats() -> Dict[str, Any]:
    """
    Get performance statistics for display detection and capture

    Returns:
        Performance metrics and statistics
    """
    try:
        from vision.multi_monitor_detector import MultiMonitorDetector

        detector = MultiMonitorDetector()
        stats = detector.get_performance_stats()

        return stats

    except Exception as e:
        logger.error(f"Error getting display stats: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/displays/{display_id}")
async def get_display(display_id: int) -> Dict[str, Any]:
    """
    Get specific display information
    
    Args:
        display_id: Display ID to query
        
    Returns:
        Display information including resolution, position, etc.
    """
    try:
        from vision.multi_monitor_detector import MultiMonitorDetector
        
        detector = MultiMonitorDetector()
        displays = await detector.detect_displays()
        
        for display in displays:
            if display.display_id == display_id:
                return {
                    "display_id": display.display_id,
                    "name": display.name,
                    "resolution": list(display.resolution),
                    "position": list(display.position),
                    "is_primary": display.is_primary,
                    "refresh_rate": display.refresh_rate,
                    "color_depth": display.color_depth
                }
        
        raise HTTPException(status_code=404, detail=f"Display {display_id} not found")
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting display {display_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/displays/{display_id}/capture")
async def capture_display(display_id: int) -> Dict[str, Any]:
    """
    Capture screenshot of specific display
    
    Args:
        display_id: Display ID to capture
        
    Returns:
        Capture result with success status
    """
    try:
        from vision.multi_monitor_detector import MultiMonitorDetector
        
        detector = MultiMonitorDetector()
        result = await detector.capture_all_displays()
        
        if display_id in result.displays_captured:
            screenshot = result.displays_captured[display_id]
            return {
                "success": True,
                "display_id": display_id,
                "captured": True,
                "capture_time": result.capture_time,
                "screenshot_shape": list(screenshot.shape) if screenshot is not None else None
            }
        else:
            return {
                "success": False,
                "display_id": display_id,
                "error": f"Failed to capture display {display_id}",
                "failed_displays": result.failed_displays
            }
            
    except Exception as e:
        logger.error(f"Error capturing display {display_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))
