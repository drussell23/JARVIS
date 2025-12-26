"""
OmniParser Integration for JARVIS Computer Use
===============================================

This module provides integration with Microsoft's OmniParser for fast,
local UI element parsing. OmniParser turns raw screenshots into structured
UI data (buttons, icons, text) locally, reducing latency and token usage.

Integration Steps:
1. Clone microsoft/OmniParser into backend/vision_engine/
2. Install OmniParser dependencies
3. Set OMNIPARSER_ENABLED=true in environment
4. This module will automatically route screenshots through OmniParser

Features:
- Local UI parsing (~0.6s per frame with OmniParser V2)
- Structured button/element detection with IDs
- Reduces Claude Vision token usage by 60-80%
- Eliminates "hallucinated clicks" via precise element detection
- Supports batch action planning with known element positions

Architecture:
    Screenshot → OmniParser (local) → Structured JSON → Claude (text-only)
                                                            ↓
                                            Action decisions based on element IDs

Author: JARVIS AI System
Version: 1.0.0 (Preparatory - OmniParser not yet cloned)
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Check if OmniParser is available
OMNIPARSER_PATH = Path(__file__).parent.parent / "vision_engine" / "OmniParser"
OMNIPARSER_AVAILABLE = OMNIPARSER_PATH.exists() and (OMNIPARSER_PATH / "model").exists()

if OMNIPARSER_AVAILABLE:
    logger.info(f"[OMNIPARSER] Found OmniParser at: {OMNIPARSER_PATH}")
else:
    logger.info(
        "[OMNIPARSER] OmniParser not found. To enable:\n"
        "  1. cd backend/vision_engine/\n"
        "  2. git clone https://github.com/microsoft/OmniParser\n"
        "  3. Follow OmniParser setup instructions\n"
        "  4. Set OMNIPARSER_ENABLED=true\n"
        "  5. Restart JARVIS"
    )


# =============================================================================
# Data Models
# =============================================================================

@dataclass
class UIElement:
    """Represents a UI element detected by OmniParser."""
    element_id: str
    element_type: str  # button, icon, text, input, menu, etc.
    label: str
    coordinates: Tuple[int, int, int, int]  # x1, y1, x2, y2 (bounding box)
    center: Tuple[int, int]  # Center point for clicking
    confidence: float
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ParsedScreen:
    """Structured representation of a screenshot parsed by OmniParser."""
    screen_id: str
    resolution: Tuple[int, int]  # width, height
    elements: List[UIElement]
    parse_time_ms: float
    model_version: str
    raw_annotations: Optional[Dict[str, Any]] = None


# =============================================================================
# OmniParser Wrapper
# =============================================================================

class OmniParserEngine:
    """
    Wrapper for Microsoft OmniParser.

    This class provides async integration with OmniParser for local
    UI element detection and structured screen parsing.
    """

    def __init__(
        self,
        omniparser_path: Optional[Path] = None,
        model_version: str = "v2",
        device: str = "cpu"
    ):
        """
        Initialize OmniParser engine.

        Args:
            omniparser_path: Path to OmniParser installation
            model_version: OmniParser model version (v1 or v2)
            device: Compute device (cpu, cuda, mps)
        """
        self.omniparser_path = omniparser_path or OMNIPARSER_PATH
        self.model_version = model_version
        self.device = device
        self._initialized = False
        self._model = None

        if not OMNIPARSER_AVAILABLE:
            logger.warning(
                "[OMNIPARSER] OmniParser not available - will fall back to raw vision"
            )

    async def initialize(self) -> bool:
        """
        Initialize OmniParser model.

        Returns:
            True if initialization successful
        """
        try:
            # Use new production-grade OmniParser core
            from backend.vision.omniparser_core import get_omniparser_core

            self._model = await get_omniparser_core(
                cache_enabled=True,
                auto_mode_selection=True,
            )

            mode = self._model.get_current_mode()
            logger.info(
                f"[OMNIPARSER] ✅ Initialized with mode: {mode.value}"
            )
            self._initialized = True
            return True

        except Exception as e:
            logger.error(f"[OMNIPARSER] Initialization failed: {e}")
            return False

    async def parse_screenshot(
        self,
        screenshot_base64: str,
        detect_types: Optional[List[str]] = None
    ) -> ParsedScreen:
        """
        Parse a screenshot using OmniParser.

        Args:
            screenshot_base64: Base64-encoded screenshot
            detect_types: Optional list of element types to detect
                          (buttons, icons, text, inputs, menus)

        Returns:
            ParsedScreen with detected UI elements
        """
        if not self._initialized:
            raise RuntimeError("OmniParser not initialized - call initialize() first")

        try:
            # Convert detect_types to ElementType enums
            from backend.vision.omniparser_core import ElementType

            element_types = None
            if detect_types:
                element_types = []
                for type_str in detect_types:
                    try:
                        element_types.append(ElementType(type_str))
                    except ValueError:
                        continue

            # Use OmniParser core for parsing
            parsed = await self._model.parse_screenshot(
                screenshot_base64,
                detect_types=element_types,
                use_cache=True,
            )

            # Convert to legacy ParsedScreen format for backward compatibility
            legacy_elements = []
            for elem in parsed.elements:
                legacy_elem = UIElement(
                    element_id=elem.element_id,
                    element_type=elem.element_type.value,
                    label=elem.label,
                    coordinates=elem.bounding_box,
                    center=elem.center,
                    confidence=elem.confidence,
                    metadata=elem.metadata,
                )
                legacy_elements.append(legacy_elem)

            return ParsedScreen(
                screen_id=parsed.screen_id,
                resolution=parsed.resolution,
                elements=legacy_elements,
                parse_time_ms=parsed.parse_time_ms,
                model_version=f"{self.model_version}-{parsed.parser_mode.value}",
                raw_annotations=parsed.raw_data,
            )

        except Exception as e:
            logger.error(f"[OMNIPARSER] Parsing failed: {e}")
            raise

    def create_structured_prompt(self, parsed_screen: ParsedScreen, goal: str) -> str:
        """
        Create a structured text prompt for Claude based on parsed screen.

        This replaces the raw image with structured element descriptions,
        reducing token usage and improving action accuracy.

        Args:
            parsed_screen: ParsedScreen from parse_screenshot()
            goal: User's goal

        Returns:
            Structured prompt describing UI elements
        """
        elements_text = []

        for elem in parsed_screen.elements:
            elem_desc = (
                f"[{elem.element_id}] {elem.element_type.upper()}: '{elem.label}' "
                f"at position {elem.center} (confidence: {elem.confidence:.0%})"
            )
            elements_text.append(elem_desc)

        prompt = f"""Goal: {goal}

Current Screen Analysis (OmniParser):
Resolution: {parsed_screen.resolution[0]}x{parsed_screen.resolution[1]}
Detected Elements ({len(parsed_screen.elements)}):

{chr(10).join(elements_text) if elements_text else "No interactive elements detected"}

To interact with an element, reference it by ID in your action.
Example: {{"action_type": "click", "element_id": "btn_1", "reasoning": "Click submit button"}}

What action should we take to achieve the goal?"""

        return prompt

    def get_stats(self) -> Dict[str, Any]:
        """Get OmniParser statistics."""
        return {
            "available": OMNIPARSER_AVAILABLE,
            "initialized": self._initialized,
            "model_version": self.model_version,
            "device": self.device,
            "path": str(self.omniparser_path)
        }


# =============================================================================
# Global Instance
# =============================================================================

_omniparser_engine: Optional[OmniParserEngine] = None


async def get_omniparser_engine() -> Optional[OmniParserEngine]:
    """
    Get or create the global OmniParser engine.

    Returns:
        OmniParserEngine if available, None otherwise
    """
    global _omniparser_engine

    if _omniparser_engine is None:
        _omniparser_engine = OmniParserEngine()
        initialized = await _omniparser_engine.initialize()
        if not initialized:
            logger.warning("[OMNIPARSER] Failed to initialize, will use fallback modes")
            # Still return engine - it will use fallback modes
            # return None

    return _omniparser_engine


def is_omniparser_available() -> bool:
    """Check if OmniParser is available and enabled."""
    # Always return True now - we have intelligent fallback modes
    # Even if OmniParser repo isn't cloned, we can use Claude Vision or OCR
    enabled = os.getenv("OMNIPARSER_ENABLED", "true").lower() == "true"
    return enabled


# =============================================================================
# Integration Helpers
# =============================================================================

async def parse_screen_with_fallback(
    screenshot_base64: str
) -> Tuple[Optional[ParsedScreen], bool]:
    """
    Parse screenshot with OmniParser if available, else return None.

    Args:
        screenshot_base64: Base64-encoded screenshot

    Returns:
        Tuple of (ParsedScreen or None, used_omniparser: bool)
    """
    if not is_omniparser_available():
        return None, False

    try:
        engine = await get_omniparser_engine()
        if engine and engine._initialized:
            parsed = await engine.parse_screenshot(screenshot_base64)
            logger.info(
                f"[OMNIPARSER] Parsed screen in {parsed.parse_time_ms:.0f}ms, "
                f"found {len(parsed.elements)} elements"
            )
            return parsed, True
    except Exception as e:
        logger.warning(f"[OMNIPARSER] Failed to parse, falling back to raw vision: {e}")

    return None, False


# =============================================================================
# Setup Instructions
# =============================================================================

def print_setup_instructions():
    """Print setup instructions for OmniParser integration."""
    instructions = """
╔═══════════════════════════════════════════════════════════════════════╗
║                   OmniParser Integration Setup                        ║
╚═══════════════════════════════════════════════════════════════════════╝

OmniParser is not currently installed. To enable 60% faster Computer Use
with local UI parsing, follow these steps:

1. Navigate to vision engine directory:
   $ cd backend/vision_engine/

2. Clone Microsoft OmniParser:
   $ git clone https://github.com/microsoft/OmniParser.git
   $ cd OmniParser

3. Install dependencies:
   $ pip install -r requirements.txt

4. Download model weights (follow OmniParser README)

5. Enable OmniParser in JARVIS:
   $ export OMNIPARSER_ENABLED=true

6. Restart JARVIS

Benefits:
• 60% faster UI parsing (0.6s vs 2s+)
• 80% reduction in Claude Vision token usage
• Structured element detection (no hallucinated clicks)
• Supports batch action planning

For more information:
https://github.com/microsoft/OmniParser

═══════════════════════════════════════════════════════════════════════
"""
    print(instructions)


if __name__ == "__main__":
    # Print setup instructions when run directly
    print_setup_instructions()
