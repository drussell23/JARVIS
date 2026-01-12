#!/usr/bin/env python3
"""
Coordinate Calculator - Enhanced Vision Pipeline v1.0
Stage 3: Coordinate Calculation
================================================

Physics-based coordinate calculation with:
- Vector mathematics
- DPI/Retina scaling correction
- Boundary clamping
- Sub-pixel precision

Author: Derek J. Russell
Date: October 2025
"""

import asyncio
import logging
import math
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass
import numpy as np

logger = logging.getLogger(__name__)


@dataclass
class CoordinateResult:
    """Calculated coordinate result"""
    global_x: int
    global_y: int
    local_x: int
    local_y: int
    precision_error: float
    dpi_corrected: bool
    metadata: Dict[str, Any]


class CoordinateCalculator:
    """
    Coordinate Calculator
    
    Converts bounding boxes to global screen coordinates with:
    - Vector math for pixel-perfect center calculation
    - DPI/Retina scaling adjustments
    - Boundary clamping for screen edges
    - Sub-pixel precision tracking
    """
    
    def __init__(self, config: Dict[str, Any]):
        """Initialize coordinate calculator"""
        self.config = config
        self.calc_config = config.get('coordinate_calculation', {})
        
        # Precision settings
        self.target_precision_px = self.calc_config.get('target_precision_px', 2)
        self.enable_subpixel = self.calc_config.get('enable_subpixel', True)
        
        # Screen bounds (will be detected)
        self.screen_width = None
        self.screen_height = None
        
        logger.info("[COORD CALC] Coordinate Calculator initialized")
    
    async def initialize(self):
        """Initialize calculator"""
        # Detect screen dimensions
        await self._detect_screen_dimensions()
        logger.info(f"[COORD CALC] Screen: {self.screen_width}x{self.screen_height}")
    
    async def calculate_coordinates(
        self,
        detection_results: Dict[str, Any],
        context: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Calculate global coordinates from detection results
        
        Args:
            detection_results: Results from Stage 2
            context: Optional context
            
        Returns:
            Dict with calculated coordinates
        """
        logger.info("[COORD CALC] Calculating coordinates")
        
        try:
            best_detection = detection_results['best']
            
            if not best_detection.found:
                return {'success': False, 'error': 'No detection to calculate coordinates from'}
            
            # Get bounding box
            bbox = best_detection.bounding_box
            if not bbox:
                return {'success': False, 'error': 'No bounding box in detection result'}
            
            x, y, w, h = bbox
            
            # Calculate center point using vector math
            center = self._calculate_centroid(bbox)
            
            # Apply DPI correction if needed
            dpi_scale = context.get('dpi_scale', 1.0) if context else 1.0
            corrected = self._apply_dpi_correction(center, dpi_scale)

            # Convert to global coordinates
            # CRITICAL: region_offset is also in physical pixels, must be corrected too!
            region_offset = context.get('region_offset', (0, 0)) if context else (0, 0)

            # Apply DPI correction to region offset as well
            if dpi_scale != 1.0:
                corrected_offset = (region_offset[0] / dpi_scale, region_offset[1] / dpi_scale)
                logger.info(f"[COORD CALC] Region offset corrected: {region_offset} -> {corrected_offset} (DPI scale={dpi_scale})")
                region_offset = corrected_offset

            logger.info(f"[COORD CALC] Region offset: {region_offset}")
            logger.info(f"[COORD CALC] Local coords: {corrected}")

            global_coords = self._to_global_coordinates(corrected, region_offset)
            logger.info(f"[COORD CALC] Global coords: {global_coords}")
            
            # Clamp to screen bounds
            clamped = self._clamp_to_screen(global_coords)
            
            # Calculate precision error
            precision_error = self._calculate_precision_error(center, corrected)
            
            result = CoordinateResult(
                global_x=clamped[0],
                global_y=clamped[1],
                local_x=center[0],
                local_y=center[1],
                precision_error=precision_error,
                dpi_corrected=dpi_scale != 1.0,
                metadata={
                    'bounding_box': bbox,
                    'dpi_scale': dpi_scale,
                    'region_offset': region_offset,
                    'detection_method': best_detection.method,
                    'detection_confidence': best_detection.confidence
                }
            )
            
            logger.info(f"[COORD CALC] ✅ Coordinates: ({result.global_x}, {result.global_y})")
            logger.info(f"[COORD CALC] Precision error: ±{result.precision_error:.2f}px")
            
            return {
                'success': True,
                'calculated_coordinates': result,
                'meets_precision_target': precision_error <= self.target_precision_px
            }
            
        except Exception as e:
            logger.error(f"[COORD CALC] Calculation failed: {e}", exc_info=True)
            return {'success': False, 'error': str(e)}
    
    def _calculate_centroid(self, bbox: Tuple[int, int, int, int]) -> Tuple[float, float]:
        """Calculate geometric centroid of bounding box"""
        x, y, w, h = bbox
        
        # Vector math for center point
        center_x = x + w / 2.0
        center_y = y + h / 2.0
        
        return (center_x, center_y)
    
    def _apply_dpi_correction(
        self,
        coords: Tuple[float, float],
        dpi_scale: float
    ) -> Tuple[float, float]:
        """Apply DPI/Retina scaling correction"""
        if dpi_scale == 1.0:
            return coords

        # CRITICAL FIX: Screenshots on Retina displays are captured at physical pixel resolution
        # (e.g., 2880x1800), but PyAutoGUI uses logical pixels (e.g., 1440x900).
        # We MUST divide coordinates by the DPI scale to convert from physical to logical pixels.

        x, y = coords
        logical_x = x / dpi_scale
        logical_y = y / dpi_scale

        logger.info(
            f"[COORD CALC] DPI correction: Physical pixels ({x:.1f}, {y:.1f}) "
            f"-> Logical pixels ({logical_x:.1f}, {logical_y:.1f}) [scale={dpi_scale}]"
        )

        return (logical_x, logical_y)
    
    def _to_global_coordinates(
        self,
        local_coords: Tuple[float, float],
        region_offset: Tuple[int, int]
    ) -> Tuple[int, int]:
        """Convert local region coordinates to global screen coordinates"""
        x, y = local_coords
        offset_x, offset_y = region_offset
        
        global_x = int(round(x + offset_x))
        global_y = int(round(y + offset_y))
        
        logger.info(f"[COORD CALC] Converting ({x:.1f}, {y:.1f}) + offset ({offset_x}, {offset_y}) = ({global_x}, {global_y})")
        
        return (global_x, global_y)
    
    def _clamp_to_screen(self, coords: Tuple[int, int]) -> Tuple[int, int]:
        """Clamp coordinates to screen boundaries"""
        x, y = coords
        
        if self.screen_width and self.screen_height:
            x = max(0, min(x, self.screen_width - 1))
            y = max(0, min(y, self.screen_height - 1))
        
        return (x, y)
    
    def _calculate_precision_error(
        self,
        original: Tuple[float, float],
        corrected: Tuple[float, float]
    ) -> float:
        """Calculate precision error (Euclidean distance)"""
        dx = original[0] - corrected[0]
        dy = original[1] - corrected[1]
        
        return math.sqrt(dx**2 + dy**2)
    
    async def _detect_screen_dimensions(self):
        """Detect screen dimensions"""
        try:
            import pyautogui
            self.screen_width, self.screen_height = pyautogui.size()
        except Exception:
            # Fallback
            self.screen_width = 1440
            self.screen_height = 900
            logger.warning("[COORD CALC] Using default screen dimensions")
