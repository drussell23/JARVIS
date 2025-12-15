#!/usr/bin/env python3
"""
Display-Aware Situational Awareness Intelligence (SAI) for Voice Unlock
========================================================================

LangGraph-powered intelligent display detection and adaptive typing strategy
for JARVIS voice unlock. Handles external displays (especially mirrored 85" Sony TV)
with situational awareness.

Features:
- Multi-display detection (built-in, HDMI, AirPlay, mirrored)
- LangGraph reasoning for optimal typing strategy
- Adaptive timing based on display configuration
- Self-learning from success/failure patterns
- Async-first architecture
- Dynamic configuration (no hardcoding)

Author: Derek Russell
Date: 2025-11-24
"""

import asyncio
import ctypes
import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from pathlib import Path
from typing import Dict, Any, Optional, List, Tuple, TypedDict

# LangGraph imports
try:
    from langgraph.graph import StateGraph, END
    LANGGRAPH_AVAILABLE = True
except ImportError:
    LANGGRAPH_AVAILABLE = False
    StateGraph = None
    END = None

logger = logging.getLogger(__name__)


# =============================================================================
# Display Configuration Enums
# =============================================================================

class DisplayType(Enum):
    """Types of display connections"""
    BUILT_IN = auto()      # MacBook built-in display
    HDMI = auto()          # HDMI connected (like Sony TV)
    THUNDERBOLT = auto()   # Thunderbolt/DisplayPort
    USB_C = auto()         # USB-C display
    AIRPLAY = auto()       # AirPlay wireless
    SIDECAR = auto()       # iPad Sidecar
    UNKNOWN = auto()


class DisplayMode(Enum):
    """Display arrangement modes"""
    SINGLE = auto()        # Only built-in display
    EXTENDED = auto()      # Extended desktop
    MIRRORED = auto()      # Mirrored displays
    CLAMSHELL = auto()     # Lid closed, external only


class TypingStrategy(Enum):
    """Password typing strategies based on display config"""
    CORE_GRAPHICS_FAST = auto()      # CG events, fast timing (single display)
    CORE_GRAPHICS_SLOW = auto()      # CG events, slower timing (extended)
    CORE_GRAPHICS_CAUTIOUS = auto()  # CG events, very slow (mirrored)
    APPLESCRIPT_DIRECT = auto()      # AppleScript keystroke (most reliable for mirrored)
    HYBRID_CG_APPLESCRIPT = auto()   # Try CG, fallback to AppleScript


# =============================================================================
# Data Classes
# =============================================================================

@dataclass
class DisplayInfo:
    """Information about a single display"""
    display_id: int
    name: str
    display_type: DisplayType
    is_main: bool
    is_mirrored: bool
    width: int
    height: int
    scale_factor: float = 1.0
    is_builtin: bool = False
    refresh_rate: int = 60
    vendor_id: Optional[str] = None
    model_name: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "display_id": self.display_id,
            "name": self.name,
            "display_type": self.display_type.name,
            "is_main": self.is_main,
            "is_mirrored": self.is_mirrored,
            "width": self.width,
            "height": self.height,
            "scale_factor": self.scale_factor,
            "is_builtin": self.is_builtin,
            "refresh_rate": self.refresh_rate,
            "vendor_id": self.vendor_id,
            "model_name": self.model_name,
        }


@dataclass
class DisplayContext:
    """Complete display context for SAI decision making"""
    displays: List[DisplayInfo] = field(default_factory=list)
    display_mode: DisplayMode = DisplayMode.SINGLE
    total_displays: int = 1
    has_external: bool = False
    is_mirrored: bool = False
    is_tv_connected: bool = False
    tv_name: Optional[str] = None
    tv_brand: Optional[str] = None
    tv_confidence: float = 0.0
    tv_detection_reasons: List[str] = field(default_factory=list)
    primary_display: Optional[DisplayInfo] = None
    external_displays: List[DisplayInfo] = field(default_factory=list)
    detection_time_ms: float = 0.0
    detection_method: str = "unknown"

    def to_dict(self) -> Dict[str, Any]:
        # Build TV info dict if TV is connected
        tv_info = None
        if self.is_tv_connected:
            tv_display = None
            for d in self.external_displays:
                if d.display_type == DisplayType.TV:
                    tv_display = d
                    break

            tv_info = {
                "name": self.tv_name,
                "brand": self.tv_brand,
                "confidence": self.tv_confidence,
                "reasons": self.tv_detection_reasons,
                "width": tv_display.width if tv_display else None,
                "height": tv_display.height if tv_display else None,
                "is_tv": True,
            }

        # Build primary display dict
        primary_dict = None
        if self.primary_display:
            primary_dict = {
                **self.primary_display.to_dict(),
                "is_builtin": self.primary_display.is_builtin,
                "type": self.primary_display.display_type.name,
            }

        # Build external display dict (first external)
        external_dict = None
        if self.external_displays:
            ext = self.external_displays[0]
            external_dict = {
                **ext.to_dict(),
                "type": ext.display_type.name,
            }

        return {
            "display_mode": self.display_mode.name,
            "total_displays": self.total_displays,
            "has_external": self.has_external,
            "is_mirrored": self.is_mirrored,
            "is_tv_connected": self.is_tv_connected,
            "tv_name": self.tv_name,
            "tv_info": tv_info,
            "primary_display": primary_dict,
            "external_display": external_dict,
            "external_displays": [d.to_dict() for d in self.external_displays],
            "detection_time_ms": self.detection_time_ms,
            "detection_method": self.detection_method,
        }


@dataclass
class TypingConfig:
    """Adaptive typing configuration based on display context"""
    strategy: TypingStrategy
    base_keystroke_delay_ms: float
    key_press_duration_ms: float
    shift_register_delay_ms: float
    wake_delay_ms: float
    submit_delay_ms: float
    retry_count: int
    use_applescript_fallback: bool
    post_to_specific_display: bool = False
    target_display_id: Optional[int] = None
    reasoning: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "strategy": self.strategy.name,
            "base_keystroke_delay_ms": self.base_keystroke_delay_ms,
            "key_press_duration_ms": self.key_press_duration_ms,
            "shift_register_delay_ms": self.shift_register_delay_ms,
            "wake_delay_ms": self.wake_delay_ms,
            "submit_delay_ms": self.submit_delay_ms,
            "retry_count": self.retry_count,
            "use_applescript_fallback": self.use_applescript_fallback,
            "reasoning": self.reasoning,
        }


# =============================================================================
# LangGraph State for Display-Aware Reasoning
# =============================================================================

class DisplayAwareState(TypedDict, total=False):
    """State for LangGraph display-aware reasoning"""
    # Input
    raw_display_data: Dict[str, Any]
    system_profiler_data: Optional[str]

    # Detected context
    display_context: Optional[Dict[str, Any]]

    # Analysis
    is_mirrored: bool
    is_tv_connected: bool
    tv_type: Optional[str]
    risk_level: str  # low, medium, high

    # Strategy selection
    recommended_strategy: Optional[str]
    typing_config: Optional[Dict[str, Any]]

    # Reasoning
    reasoning_steps: List[str]
    confidence: float

    # Output
    final_decision: Optional[str]
    error: Optional[str]


# =============================================================================
# Display Detector (Core Graphics + System Profiler)
# =============================================================================

class DisplayDetector:
    """
    Multi-method display detector for macOS.
    Uses Core Graphics, System Profiler, and AppleScript for comprehensive detection.

    UNIVERSAL TV DETECTION:
    - Works with ANY TV brand (Sony, Samsung, LG, TCL, Hisense, Vizio, etc.)
    - Works with ANY TV size (32" to 100"+)
    - Uses multiple heuristics: resolution, refresh rate, connection type, naming patterns
    - Self-learning: remembers displays you've identified as TVs
    """

    # Dynamic TV detection configuration
    TV_DETECTION_CONFIG = {
        # Resolution thresholds (TVs are typically large resolution)
        "min_tv_width": 1920,       # Full HD minimum
        "min_tv_height": 1080,
        "likely_tv_width": 3840,    # 4K is very likely a TV
        "likely_tv_height": 2160,

        # Aspect ratios typical for TVs (16:9, 21:9 ultrawide)
        "tv_aspect_ratios": [
            (16, 9),    # Standard TV
            (21, 9),    # Ultrawide
            (32, 9),    # Super ultrawide
        ],
        "aspect_ratio_tolerance": 0.05,  # 5% tolerance

        # Refresh rates (TVs often have specific rates)
        "tv_refresh_rates": [24, 30, 50, 60, 120],

        # Connection types that suggest TV
        "tv_connection_types": ["hdmi", "airplay", "wireless"],

        # Display types that are NOT TVs
        "non_tv_indicators": [
            "built-in", "retina", "internal", "laptop",
            "thunderbolt display", "studio display", "pro display",
            "dell", "benq", "asus", "acer", "viewsonic",  # Monitor brands
            "ultrasharp", "predator", "rog",  # Monitor product lines
        ],
    }

    # Brand patterns for TV manufacturers (comprehensive, case-insensitive)
    TV_BRAND_PATTERNS = [
        # Major TV brands
        r"\bsony\b", r"\bsamsung\b", r"\blg\b", r"\bvizio\b",
        r"\btcl\b", r"\bhisense\b", r"\bphilips\b", r"\bpanasonic\b",
        r"\bsharp\b", r"\btoshiba\b", r"\bsanyo\b", r"\binsignia\b",
        r"\belement\b", r"\bwestinghouse\b", r"\bsceptre\b",
        r"\bonn\b", r"\bhitachi\b", r"\bjvc\b", r"\bfunai\b",
        r"\bskyworth\b", r"\bhaier\b", r"\bkonka\b", r"\bchanghong\b",

        # TV product lines
        r"\bbravia\b", r"\bqled\b", r"\boled\b", r"\bneo\s*qled\b",
        r"\bnanocell\b", r"\buhd\b", r"\bcrystal\s*uhd\b",
        r"\bfire\s*tv\b", r"\broku\s*tv\b", r"\bandroid\s*tv\b",
        r"\bgoogle\s*tv\b", r"\bwebos\b", r"\btizen\b",

        # Generic TV indicators
        r"\btv\b", r"\btelevision\b", r"\bhdtv\b",
        r"\bsmart\s*tv\b", r"\b4k\s*tv\b", r"\b8k\b",

        # Room-based naming (user might name their TV)
        r"\bliving\s*room\b", r"\bbedroom\b", r"\bden\b",
        r"\bfamily\s*room\b", r"\bbasement\b", r"\bgame\s*room\b",
    ]

    # Learned TV displays (persisted to disk)
    LEARNED_TVS_FILE = Path.home() / ".jarvis" / "learned_tv_displays.json"

    def __init__(self):
        self._cg_available = self._check_core_graphics()
        self._cache: Optional[DisplayContext] = None
        self._cache_time: Optional[datetime] = None
        self._cache_duration_ms = 5000  # Cache for 5 seconds
        self._learned_tvs: Dict[str, bool] = {}  # display_id -> is_tv
        self._compiled_patterns = [re.compile(p, re.IGNORECASE) for p in self.TV_BRAND_PATTERNS]
        self._load_learned_tvs()

    def _load_learned_tvs(self):
        """Load previously learned TV identifications from disk"""
        try:
            if self.LEARNED_TVS_FILE.exists():
                with open(self.LEARNED_TVS_FILE, 'r') as f:
                    data = json.load(f)
                    self._learned_tvs = data.get("learned_displays", {})
                    logger.debug(f"Loaded {len(self._learned_tvs)} learned TV identifications")
        except Exception as e:
            logger.warning(f"Failed to load learned TVs: {e}")

    async def _save_learned_tvs(self):
        """Save learned TV identifications to disk"""
        try:
            self.LEARNED_TVS_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(self.LEARNED_TVS_FILE, 'w') as f:
                json.dump({
                    "learned_displays": self._learned_tvs,
                    "updated_at": datetime.now().isoformat(),
                }, f, indent=2)
        except Exception as e:
            logger.warning(f"Failed to save learned TVs: {e}")

    async def learn_display_is_tv(self, display_id: str, is_tv: bool = True):
        """
        Teach the system that a specific display is (or is not) a TV.
        This learning persists across sessions.

        Args:
            display_id: The display identifier (name or ID)
            is_tv: Whether this display is a TV
        """
        self._learned_tvs[display_id.lower()] = is_tv
        await self._save_learned_tvs()
        logger.info(f"Learned: Display '{display_id}' is {'a TV' if is_tv else 'NOT a TV'}")

    def _check_core_graphics(self) -> bool:
        """Check if Core Graphics is available"""
        try:
            self._cg = ctypes.CDLL('/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics')
            self._cg.CGMainDisplayID.restype = ctypes.c_uint32
            self._cg.CGGetActiveDisplayList.argtypes = [
                ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_uint32)
            ]
            self._cg.CGGetActiveDisplayList.restype = ctypes.c_int32
            self._cg.CGDisplayBounds.argtypes = [ctypes.c_uint32]
            self._cg.CGDisplayBounds.restype = ctypes.c_double * 4  # CGRect
            self._cg.CGDisplayIsMain.argtypes = [ctypes.c_uint32]
            self._cg.CGDisplayIsMain.restype = ctypes.c_bool
            self._cg.CGDisplayMirrorsDisplay.argtypes = [ctypes.c_uint32]
            self._cg.CGDisplayMirrorsDisplay.restype = ctypes.c_uint32
            self._cg.CGDisplayIsBuiltin.argtypes = [ctypes.c_uint32]
            self._cg.CGDisplayIsBuiltin.restype = ctypes.c_bool
            return True
        except Exception as e:
            logger.warning(f"Core Graphics not available: {e}")
            return False

    async def detect_displays(self, use_cache: bool = True) -> DisplayContext:
        """
        Detect all connected displays with comprehensive information.

        Args:
            use_cache: Use cached result if available and fresh

        Returns:
            DisplayContext with full display information
        """
        start_time = datetime.now()

        # Check cache
        if use_cache and self._cache and self._cache_time:
            age_ms = (datetime.now() - self._cache_time).total_seconds() * 1000
            if age_ms < self._cache_duration_ms:
                logger.debug(f"Using cached display context (age: {age_ms:.0f}ms)")
                return self._cache

        context = DisplayContext()

        try:
            # Method 1: Core Graphics (fast, accurate for display list)
            if self._cg_available:
                displays = await self._detect_via_core_graphics()
                context.displays = displays
                context.detection_method = "core_graphics"
            else:
                # Fallback to System Profiler
                displays = await self._detect_via_system_profiler()
                context.displays = displays
                context.detection_method = "system_profiler"

            # Analyze display configuration
            context.total_displays = len(context.displays)
            context.has_external = any(not d.is_builtin for d in context.displays)
            context.is_mirrored = any(d.is_mirrored for d in context.displays)

            # Find primary display
            for display in context.displays:
                if display.is_main:
                    context.primary_display = display
                    break

            # Identify external displays
            context.external_displays = [d for d in context.displays if not d.is_builtin]

            # Check for TV connection using universal detection
            tv_detection_details = []
            for display in context.external_displays:
                is_tv, confidence, reasons = self._is_tv(display)
                tv_detection_details.append({
                    "display": display.name,
                    "is_tv": is_tv,
                    "confidence": confidence,
                    "reasons": reasons,
                })
                if is_tv:
                    context.is_tv_connected = True
                    context.tv_name = display.name
                    context.tv_confidence = confidence
                    context.tv_detection_reasons = reasons

                    # Extract brand from name using known brand patterns
                    detected_brand = self._extract_tv_brand(display.name)
                    context.tv_brand = detected_brand

                    logger.info(
                        f"🖥️ TV DETECTED: '{display.name}' "
                        f"(brand: {detected_brand or 'Unknown'}, confidence: {confidence:.0%}, reasons: {reasons})"
                    )
                    break

            # Determine display mode
            if context.total_displays == 1:
                context.display_mode = DisplayMode.SINGLE
            elif context.is_mirrored:
                context.display_mode = DisplayMode.MIRRORED
            elif not any(d.is_builtin for d in context.displays):
                context.display_mode = DisplayMode.CLAMSHELL
            else:
                context.display_mode = DisplayMode.EXTENDED

            context.detection_time_ms = (datetime.now() - start_time).total_seconds() * 1000

            # Update cache
            self._cache = context
            self._cache_time = datetime.now()

            logger.info(
                f"Display detection complete: {context.total_displays} displays, "
                f"mode={context.display_mode.name}, mirrored={context.is_mirrored}, "
                f"tv_connected={context.is_tv_connected} ({context.detection_time_ms:.1f}ms)"
            )

            return context

        except Exception as e:
            logger.error(f"Display detection failed: {e}", exc_info=True)
            context.detection_method = "failed"
            return context

    async def _detect_via_core_graphics(self) -> List[DisplayInfo]:
        """Detect displays using Core Graphics API"""
        displays = []

        try:
            # Get display count
            max_displays = 16
            display_array = (ctypes.c_uint32 * max_displays)()
            display_count = ctypes.c_uint32()

            result = self._cg.CGGetActiveDisplayList(
                max_displays,
                display_array,
                ctypes.byref(display_count)
            )

            if result != 0:
                logger.error(f"CGGetActiveDisplayList failed with code {result}")
                return displays

            main_display_id = self._cg.CGMainDisplayID()

            for i in range(display_count.value):
                display_id = display_array[i]

                # Get display bounds (CGRect: origin.x, origin.y, width, height)
                # Note: CGDisplayBounds returns a struct, need proper handling
                try:
                    # Use subprocess for display info since CGDisplayBounds struct handling is complex
                    is_main = self._cg.CGDisplayIsMain(display_id)
                    is_builtin = self._cg.CGDisplayIsBuiltin(display_id)
                    mirrors_display = self._cg.CGDisplayMirrorsDisplay(display_id)
                    is_mirrored = mirrors_display != 0

                    # Get display name via system_profiler for this display
                    name = await self._get_display_name(display_id, is_builtin)

                    # Determine display type
                    if is_builtin:
                        display_type = DisplayType.BUILT_IN
                    elif "airplay" in name.lower():
                        display_type = DisplayType.AIRPLAY
                    elif "sidecar" in name.lower():
                        display_type = DisplayType.SIDECAR
                    else:
                        display_type = DisplayType.HDMI  # Most common external

                    display_info = DisplayInfo(
                        display_id=display_id,
                        name=name,
                        display_type=display_type,
                        is_main=is_main,
                        is_mirrored=is_mirrored,
                        width=0,  # Will be filled by system_profiler
                        height=0,
                        is_builtin=is_builtin,
                    )

                    displays.append(display_info)

                except Exception as e:
                    logger.warning(f"Failed to get info for display {display_id}: {e}")

            # Enhance with system_profiler data
            await self._enhance_with_system_profiler(displays)

            return displays

        except Exception as e:
            logger.error(f"Core Graphics detection failed: {e}")
            return displays

    async def _get_display_name(self, display_id: int, is_builtin: bool) -> str:
        """Get display name"""
        if is_builtin:
            return "Built-in Retina Display"

        # Try to get name from ioreg
        try:
            proc = await asyncio.create_subprocess_exec(
                "ioreg", "-lw0", "-r", "-c", "IODisplayConnect",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await proc.communicate()
            output = stdout.decode()

            # Parse for display names
            if "DisplayProductName" in output:
                import re
                matches = re.findall(r'"DisplayProductName"\s*=\s*"([^"]+)"', output)
                if matches:
                    for match in matches:
                        if match != "Color LCD":  # Skip built-in
                            return match

            return f"External Display {display_id}"

        except Exception:
            return f"External Display {display_id}"

    async def _detect_via_system_profiler(self) -> List[DisplayInfo]:
        """Detect displays using system_profiler"""
        displays = []

        try:
            proc = await asyncio.create_subprocess_exec(
                "system_profiler", "SPDisplaysDataType", "-json",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await proc.communicate()

            data = json.loads(stdout.decode())
            graphics_data = data.get("SPDisplaysDataType", [])

            display_id = 1
            for gpu in graphics_data:
                ndrvs = gpu.get("spdisplays_ndrvs", [])
                for display in ndrvs:
                    name = display.get("_name", f"Display {display_id}")
                    resolution = display.get("_spdisplays_resolution", "0 x 0")

                    # Parse resolution
                    try:
                        width, height = resolution.replace(" ", "").split("x")[:2]
                        width = int(width)
                        height = int(height.split("@")[0]) if "@" in height else int(height)
                    except:
                        width, height = 0, 0

                    is_builtin = "built-in" in name.lower() or "retina" in name.lower()
                    is_main = display.get("spdisplays_main", "").lower() == "yes"
                    is_mirrored = display.get("spdisplays_mirror", "").lower() == "on"

                    # Determine type
                    if is_builtin:
                        display_type = DisplayType.BUILT_IN
                    elif "airplay" in name.lower():
                        display_type = DisplayType.AIRPLAY
                    elif "thunderbolt" in display.get("spdisplays_connection_type", "").lower():
                        display_type = DisplayType.THUNDERBOLT
                    else:
                        display_type = DisplayType.HDMI

                    display_info = DisplayInfo(
                        display_id=display_id,
                        name=name,
                        display_type=display_type,
                        is_main=is_main,
                        is_mirrored=is_mirrored,
                        width=width,
                        height=height,
                        is_builtin=is_builtin,
                        vendor_id=display.get("_spdisplays_display-vendor-id"),
                        model_name=display.get("_spdisplays_display-product-id"),
                    )

                    displays.append(display_info)
                    display_id += 1

            return displays

        except Exception as e:
            logger.error(f"System profiler detection failed: {e}")
            return displays

    async def _enhance_with_system_profiler(self, displays: List[DisplayInfo]):
        """Enhance Core Graphics display info with system_profiler data"""
        try:
            sp_displays = await self._detect_via_system_profiler()

            # Match by is_builtin flag and name similarity
            for cg_display in displays:
                for sp_display in sp_displays:
                    if cg_display.is_builtin == sp_display.is_builtin:
                        cg_display.width = sp_display.width
                        cg_display.height = sp_display.height
                        cg_display.vendor_id = sp_display.vendor_id
                        cg_display.model_name = sp_display.model_name
                        if sp_display.name and not cg_display.name.startswith("External Display"):
                            cg_display.name = sp_display.name
                        break

        except Exception as e:
            logger.debug(f"System profiler enhancement failed: {e}")

    def _is_tv(self, display: DisplayInfo) -> Tuple[bool, float, List[str]]:
        """
        UNIVERSAL TV DETECTION - Works with ANY TV brand/size

        Uses a scoring system with multiple heuristics:
        1. Learned history (highest priority)
        2. Brand/name pattern matching
        3. Resolution analysis
        4. Aspect ratio analysis
        5. Connection type analysis
        6. Non-TV exclusion patterns

        Returns:
            Tuple of (is_tv, confidence_score, reasons)
        """
        config = self.TV_DETECTION_CONFIG
        name_lower = display.name.lower()
        reasons: List[str] = []
        score = 0.0

        # =====================================================================
        # PRIORITY 1: Check learned history (user has explicitly told us)
        # =====================================================================
        for learned_id, is_tv in self._learned_tvs.items():
            if learned_id in name_lower or name_lower in learned_id:
                if is_tv:
                    reasons.append(f"LEARNED: Previously identified as TV")
                    return True, 1.0, reasons
                else:
                    reasons.append(f"LEARNED: Previously identified as NOT a TV")
                    return False, 1.0, reasons

        # =====================================================================
        # PRIORITY 2: Check for NON-TV indicators (exclusion)
        # =====================================================================
        for indicator in config["non_tv_indicators"]:
            if indicator in name_lower:
                reasons.append(f"EXCLUDED: Contains non-TV indicator '{indicator}'")
                return False, 0.9, reasons

        # =====================================================================
        # HEURISTIC 1: Brand/Name Pattern Matching (high confidence)
        # =====================================================================
        for pattern in self._compiled_patterns:
            if pattern.search(name_lower):
                score += 0.4
                match = pattern.pattern.replace(r'\b', '').replace(r'\s*', ' ')
                reasons.append(f"BRAND MATCH: Pattern '{match}' found in name")
                break  # One brand match is enough

        # =====================================================================
        # HEURISTIC 2: Resolution Analysis
        # =====================================================================
        if display.width > 0 and display.height > 0:
            # 4K or higher is very likely a TV
            if display.width >= config["likely_tv_width"] and display.height >= config["likely_tv_height"]:
                score += 0.35
                reasons.append(f"RESOLUTION: 4K+ ({display.width}x{display.height}) - likely TV")
            # 1080p+ external is possibly a TV
            elif display.width >= config["min_tv_width"] and display.height >= config["min_tv_height"]:
                if not display.is_builtin:
                    score += 0.15
                    reasons.append(f"RESOLUTION: HD+ external ({display.width}x{display.height})")
            # 8K is definitely a TV
            if display.width >= 7680:
                score += 0.2
                reasons.append(f"RESOLUTION: 8K detected - definitely TV")

        # =====================================================================
        # HEURISTIC 3: Aspect Ratio Analysis
        # =====================================================================
        if display.width > 0 and display.height > 0:
            actual_ratio = display.width / display.height
            tolerance = config["aspect_ratio_tolerance"]

            for target_w, target_h in config["tv_aspect_ratios"]:
                target_ratio = target_w / target_h
                if abs(actual_ratio - target_ratio) <= tolerance:
                    # Standard 16:9 with large resolution
                    if target_w == 16 and target_h == 9:
                        if display.width >= 1920 and not display.is_builtin:
                            score += 0.1
                            reasons.append(f"ASPECT: 16:9 ratio - common TV format")
                    else:
                        score += 0.1
                        reasons.append(f"ASPECT: {target_w}:{target_h} ultrawide ratio")
                    break

        # =====================================================================
        # HEURISTIC 4: Connection Type Analysis
        # =====================================================================
        display_type_str = display.display_type.name.lower()
        for conn_type in config["tv_connection_types"]:
            if conn_type in display_type_str:
                score += 0.15
                reasons.append(f"CONNECTION: {conn_type.upper()} - common TV connection")
                break

        # External non-built-in displays get a small boost
        if not display.is_builtin and display.display_type != DisplayType.BUILT_IN:
            score += 0.05
            reasons.append("EXTERNAL: Non-built-in display")

        # =====================================================================
        # HEURISTIC 5: Size inference from resolution
        # =====================================================================
        # Typical PPI: Monitors ~100-150 PPI, TVs ~40-60 PPI
        # So same resolution on TV = larger physical size
        if display.width >= 3840:  # 4K
            # At 4K, this is likely a TV (monitors at 4K are typically <32")
            score += 0.1
            reasons.append("SIZE: 4K resolution suggests large display (TV territory)")

        # =====================================================================
        # FINAL DECISION
        # =====================================================================
        # Threshold: 0.5 = likely TV, 0.7 = probably TV, 0.9 = definitely TV
        is_tv = score >= 0.5

        if not reasons:
            reasons.append("NO INDICATORS: Could not determine display type")

        confidence = min(score, 1.0)

        logger.debug(
            f"TV Detection for '{display.name}': is_tv={is_tv}, "
            f"confidence={confidence:.2f}, reasons={reasons}"
        )

        return is_tv, confidence, reasons

    def _is_tv_simple(self, display: DisplayInfo) -> bool:
        """Simple boolean wrapper for _is_tv"""
        is_tv, _, _ = self._is_tv(display)
        return is_tv

    def _extract_tv_brand(self, display_name: str) -> Optional[str]:
        """
        Extract TV brand from display name.

        Args:
            display_name: Name of the display

        Returns:
            Brand name in title case, or None if not detected
        """
        if not display_name:
            return None

        name_lower = display_name.lower()

        # Known major TV brands (order matters - check specific before generic)
        tv_brands = [
            ("sony", "Sony"),
            ("samsung", "Samsung"),
            ("lg", "LG"),
            ("vizio", "Vizio"),
            ("tcl", "TCL"),
            ("hisense", "Hisense"),
            ("philips", "Philips"),
            ("panasonic", "Panasonic"),
            ("sharp", "Sharp"),
            ("toshiba", "Toshiba"),
            ("sanyo", "Sanyo"),
            ("insignia", "Insignia"),
            ("element", "Element"),
            ("westinghouse", "Westinghouse"),
            ("sceptre", "Sceptre"),
            ("onn", "Onn"),
            ("hitachi", "Hitachi"),
            ("jvc", "JVC"),
            ("funai", "Funai"),
            ("skyworth", "Skyworth"),
            ("haier", "Haier"),
            ("konka", "Konka"),
            ("changhong", "Changhong"),
            ("bravia", "Sony"),  # Bravia is Sony
            ("qled", "Samsung"),  # QLED is typically Samsung
            ("neo qled", "Samsung"),
            ("nanocell", "LG"),  # NanoCell is LG
            ("roku tv", "Roku"),
            ("fire tv", "Amazon"),
            ("android tv", "Android TV"),
            ("google tv", "Google TV"),
            ("webos", "LG"),  # WebOS is LG
            ("tizen", "Samsung"),  # Tizen is Samsung
        ]

        for search_term, brand_name in tv_brands:
            if search_term in name_lower:
                return brand_name

        return None


# =============================================================================
# LangGraph Reasoning Engine for Typing Strategy
# =============================================================================

class DisplayAwareReasoningEngine:
    """
    LangGraph-powered reasoning engine for display-aware typing strategy selection.
    """

    def __init__(self):
        self._graph = None
        if LANGGRAPH_AVAILABLE:
            self._build_graph()

    def _build_graph(self):
        """Build the LangGraph state machine for display-aware reasoning"""
        if not LANGGRAPH_AVAILABLE:
            return

        # Create the graph
        graph = StateGraph(DisplayAwareState)

        # Add nodes
        graph.add_node("detect_displays", self._node_detect_displays)
        graph.add_node("analyze_configuration", self._node_analyze_configuration)
        graph.add_node("assess_risk", self._node_assess_risk)
        graph.add_node("select_strategy", self._node_select_strategy)
        graph.add_node("generate_config", self._node_generate_config)

        # Add edges
        graph.set_entry_point("detect_displays")
        graph.add_edge("detect_displays", "analyze_configuration")
        graph.add_edge("analyze_configuration", "assess_risk")
        graph.add_edge("assess_risk", "select_strategy")
        graph.add_edge("select_strategy", "generate_config")
        graph.add_edge("generate_config", END)

        self._graph = graph.compile()

    async def _node_detect_displays(self, state: DisplayAwareState) -> Dict[str, Any]:
        """Node: Detect display configuration"""
        reasoning = ["Starting display detection..."]

        try:
            detector = DisplayDetector()
            context = await detector.detect_displays()

            reasoning.append(f"Detected {context.total_displays} display(s)")
            reasoning.append(f"Display mode: {context.display_mode.name}")

            return {
                "display_context": context.to_dict(),
                "is_mirrored": context.is_mirrored,
                "is_tv_connected": context.is_tv_connected,
                "tv_type": context.tv_name,
                "reasoning_steps": reasoning,
            }

        except Exception as e:
            reasoning.append(f"Detection failed: {e}")
            return {
                "error": str(e),
                "reasoning_steps": reasoning,
            }

    async def _node_analyze_configuration(self, state: DisplayAwareState) -> Dict[str, Any]:
        """Node: Analyze display configuration"""
        reasoning = list(state.get("reasoning_steps", []))

        context = state.get("display_context", {})
        is_mirrored = state.get("is_mirrored", False)
        is_tv_connected = state.get("is_tv_connected", False)

        reasoning.append("Analyzing display configuration...")

        if is_mirrored and is_tv_connected:
            reasoning.append("ALERT: TV mirroring detected - highest risk for keyboard event routing")
        elif is_mirrored:
            reasoning.append("WARNING: Display mirroring active - keyboard events may route incorrectly")
        elif is_tv_connected:
            reasoning.append("NOTE: TV connected in extended mode - moderate risk")
        else:
            reasoning.append("Single/extended display without TV - low risk")

        return {
            "reasoning_steps": reasoning,
        }

    async def _node_assess_risk(self, state: DisplayAwareState) -> Dict[str, Any]:
        """Node: Assess risk level for password typing"""
        reasoning = list(state.get("reasoning_steps", []))

        is_mirrored = state.get("is_mirrored", False)
        is_tv_connected = state.get("is_tv_connected", False)

        reasoning.append("Assessing risk level...")

        # Risk assessment logic
        if is_mirrored and is_tv_connected:
            risk_level = "high"
            reasoning.append("Risk Level: HIGH - Mirrored TV may intercept or delay keyboard events")
        elif is_mirrored:
            risk_level = "high"
            reasoning.append("Risk Level: HIGH - Mirroring can cause event routing issues")
        elif is_tv_connected:
            risk_level = "medium"
            reasoning.append("Risk Level: MEDIUM - Extended TV display may affect focus")
        else:
            risk_level = "low"
            reasoning.append("Risk Level: LOW - Single display, optimal conditions")

        return {
            "risk_level": risk_level,
            "reasoning_steps": reasoning,
        }

    async def _node_select_strategy(self, state: DisplayAwareState) -> Dict[str, Any]:
        """Node: Select optimal typing strategy based on risk"""
        reasoning = list(state.get("reasoning_steps", []))
        risk_level = state.get("risk_level", "low")

        reasoning.append(f"Selecting strategy for risk level: {risk_level}")

        if risk_level == "high":
            # For mirrored displays (especially TV), use AppleScript which is more reliable
            strategy = TypingStrategy.APPLESCRIPT_DIRECT
            reasoning.append("Selected: APPLESCRIPT_DIRECT - Most reliable for mirrored displays")
            reasoning.append("Reason: AppleScript keystroke events go through System Events")
            reasoning.append("        which properly routes to the active application regardless")
            reasoning.append("        of display configuration")
        elif risk_level == "medium":
            # Extended display - use hybrid approach
            strategy = TypingStrategy.HYBRID_CG_APPLESCRIPT
            reasoning.append("Selected: HYBRID_CG_APPLESCRIPT - Try CG first, fallback to AppleScript")
        else:
            # Low risk - use fast Core Graphics
            strategy = TypingStrategy.CORE_GRAPHICS_FAST
            reasoning.append("Selected: CORE_GRAPHICS_FAST - Optimal for single display")

        return {
            "recommended_strategy": strategy.name,
            "reasoning_steps": reasoning,
        }

    async def _node_generate_config(self, state: DisplayAwareState) -> Dict[str, Any]:
        """Node: Generate final typing configuration"""
        reasoning = list(state.get("reasoning_steps", []))
        strategy_name = state.get("recommended_strategy", "CORE_GRAPHICS_FAST")
        risk_level = state.get("risk_level", "low")

        reasoning.append("Generating typing configuration...")

        strategy = TypingStrategy[strategy_name]

        # Generate timing config based on strategy
        if strategy == TypingStrategy.APPLESCRIPT_DIRECT:
            config = TypingConfig(
                strategy=strategy,
                base_keystroke_delay_ms=150,  # Slower for reliability
                key_press_duration_ms=80,
                shift_register_delay_ms=100,
                wake_delay_ms=1500,  # Longer wake for TV
                submit_delay_ms=200,
                retry_count=3,
                use_applescript_fallback=True,
                reasoning="AppleScript strategy for mirrored/TV display"
            )
        elif strategy == TypingStrategy.HYBRID_CG_APPLESCRIPT:
            config = TypingConfig(
                strategy=strategy,
                base_keystroke_delay_ms=120,
                key_press_duration_ms=60,
                shift_register_delay_ms=80,
                wake_delay_ms=1200,
                submit_delay_ms=150,
                retry_count=2,
                use_applescript_fallback=True,
                reasoning="Hybrid strategy for extended display"
            )
        elif strategy == TypingStrategy.CORE_GRAPHICS_CAUTIOUS:
            config = TypingConfig(
                strategy=strategy,
                base_keystroke_delay_ms=100,
                key_press_duration_ms=50,
                shift_register_delay_ms=60,
                wake_delay_ms=1000,
                submit_delay_ms=100,
                retry_count=3,
                use_applescript_fallback=True,
                reasoning="Cautious CG strategy with fallback"
            )
        else:  # CORE_GRAPHICS_FAST
            config = TypingConfig(
                strategy=strategy,
                base_keystroke_delay_ms=80,
                key_press_duration_ms=40,
                shift_register_delay_ms=50,
                wake_delay_ms=800,
                submit_delay_ms=100,
                retry_count=2,
                use_applescript_fallback=True,
                reasoning="Fast CG strategy for optimal conditions"
            )

        reasoning.append(f"Generated config: {config.strategy.name}")
        reasoning.append(f"Keystroke delay: {config.base_keystroke_delay_ms}ms")
        reasoning.append(f"Wake delay: {config.wake_delay_ms}ms")

        return {
            "typing_config": config.to_dict(),
            "final_decision": f"Use {strategy.name} with {config.base_keystroke_delay_ms}ms delays",
            "confidence": 0.95 if risk_level == "high" else 0.9,
            "reasoning_steps": reasoning,
        }

    async def determine_typing_strategy(self) -> Tuple[TypingConfig, DisplayContext, List[str]]:
        """
        Run the full LangGraph reasoning pipeline to determine optimal typing strategy.

        Returns:
            Tuple of (TypingConfig, DisplayContext, reasoning_steps)
        """
        if not LANGGRAPH_AVAILABLE or not self._graph:
            # Fallback without LangGraph
            return await self._fallback_strategy()

        try:
            # Run the graph
            initial_state: DisplayAwareState = {
                "reasoning_steps": [],
            }

            final_state = await self._graph.ainvoke(initial_state)

            # Extract results
            config_dict = final_state.get("typing_config", {})
            strategy = TypingStrategy[config_dict.get("strategy", "CORE_GRAPHICS_FAST")]

            config = TypingConfig(
                strategy=strategy,
                base_keystroke_delay_ms=config_dict.get("base_keystroke_delay_ms", 80),
                key_press_duration_ms=config_dict.get("key_press_duration_ms", 40),
                shift_register_delay_ms=config_dict.get("shift_register_delay_ms", 50),
                wake_delay_ms=config_dict.get("wake_delay_ms", 800),
                submit_delay_ms=config_dict.get("submit_delay_ms", 100),
                retry_count=config_dict.get("retry_count", 2),
                use_applescript_fallback=config_dict.get("use_applescript_fallback", True),
                reasoning=config_dict.get("reasoning", ""),
            )

            context_dict = final_state.get("display_context", {})

            # Extract TV info from context dict
            tv_info = context_dict.get("tv_info", {}) or {}

            context = DisplayContext(
                display_mode=DisplayMode[context_dict.get("display_mode", "SINGLE")],
                total_displays=context_dict.get("total_displays", 1),
                has_external=context_dict.get("has_external", False),
                is_mirrored=context_dict.get("is_mirrored", False),
                is_tv_connected=context_dict.get("is_tv_connected", False),
                tv_name=context_dict.get("tv_name"),
                tv_brand=tv_info.get("brand"),
                tv_confidence=tv_info.get("confidence", 0.0),
                tv_detection_reasons=tv_info.get("reasons", []),
            )

            reasoning = final_state.get("reasoning_steps", [])

            logger.info(f"SAI Decision: {config.strategy.name} for {context.display_mode.name}")
            for step in reasoning:
                logger.debug(f"  {step}")

            return config, context, reasoning

        except Exception as e:
            logger.error(f"LangGraph reasoning failed: {e}", exc_info=True)
            return await self._fallback_strategy()

    async def _fallback_strategy(self) -> Tuple[TypingConfig, DisplayContext, List[str]]:
        """Fallback strategy without LangGraph"""
        reasoning = ["LangGraph unavailable, using fallback detection"]

        detector = DisplayDetector()
        context = await detector.detect_displays()

        reasoning.append(f"Detected: {context.total_displays} displays, mirrored={context.is_mirrored}")

        # Simple rule-based strategy selection
        if context.is_mirrored or context.is_tv_connected:
            strategy = TypingStrategy.APPLESCRIPT_DIRECT
            config = TypingConfig(
                strategy=strategy,
                base_keystroke_delay_ms=150,
                key_press_duration_ms=80,
                shift_register_delay_ms=100,
                wake_delay_ms=1500,
                submit_delay_ms=200,
                retry_count=3,
                use_applescript_fallback=True,
                reasoning="Fallback: AppleScript for external/mirrored display"
            )
            reasoning.append("Selected AppleScript strategy for mirrored/TV")
        else:
            strategy = TypingStrategy.CORE_GRAPHICS_FAST
            config = TypingConfig(
                strategy=strategy,
                base_keystroke_delay_ms=80,
                key_press_duration_ms=40,
                shift_register_delay_ms=50,
                wake_delay_ms=800,
                submit_delay_ms=100,
                retry_count=2,
                use_applescript_fallback=True,
                reasoning="Fallback: Fast CG for single display"
            )
            reasoning.append("Selected fast CG strategy for single display")

        return config, context, reasoning


# =============================================================================
# Main SAI Class
# =============================================================================

class DisplayAwareSAI:
    """
    Main Situational Awareness Intelligence class for display-aware voice unlock.

    Usage:
        sai = DisplayAwareSAI()
        config, context, reasoning = await sai.get_optimal_typing_config()

        # Use config to type password
        if config.strategy == TypingStrategy.APPLESCRIPT_DIRECT:
            await type_via_applescript(password)
        else:
            await type_via_core_graphics(password, config)
    """

    def __init__(self):
        self._reasoning_engine = DisplayAwareReasoningEngine()
        self._detector = DisplayDetector()
        self._last_context: Optional[DisplayContext] = None
        self._last_config: Optional[TypingConfig] = None

    async def get_optimal_typing_config(self) -> Tuple[TypingConfig, DisplayContext, List[str]]:
        """
        Get the optimal typing configuration based on current display setup.

        Returns:
            Tuple of (TypingConfig, DisplayContext, reasoning_steps)
        """
        config, context, reasoning = await self._reasoning_engine.determine_typing_strategy()

        self._last_context = context
        self._last_config = config

        return config, context, reasoning

    async def get_display_context(self) -> DisplayContext:
        """Get current display context (fast, cached)"""
        return await self._detector.detect_displays()

    def is_tv_mode(self) -> bool:
        """Quick check if TV mode is active"""
        if self._last_context:
            return self._last_context.is_tv_connected
        return False

    def is_mirrored(self) -> bool:
        """Quick check if mirroring is active"""
        if self._last_context:
            return self._last_context.is_mirrored
        return False

    @property
    def last_config(self) -> Optional[TypingConfig]:
        """Get the last computed typing config"""
        return self._last_config

    @property
    def last_context(self) -> Optional[DisplayContext]:
        """Get the last detected display context"""
        return self._last_context


# =============================================================================
# 🖥️ DYNAMIC SAI: Display Connection Monitor
# =============================================================================

class DisplayConnectionMonitor:
    """
    Proactive display connection monitoring for dynamic SAI awareness.

    This class provides real-time TV connection detection without waiting
    for unlock attempts. It can:
    - Detect TV connections/disconnections
    - Record connection events to SQLite
    - Start/end TV sessions
    - Provide instant TV state awareness

    Usage:
        monitor = DisplayConnectionMonitor()
        await monitor.check_and_record()  # Manual check
        await monitor.start_periodic_monitoring(interval_seconds=30)  # Background
    """

    def __init__(self):
        self._detector = DisplayDetector()
        self._last_state: Optional[Dict[str, Any]] = None
        self._monitoring = False
        self._monitor_task: Optional[asyncio.Task] = None

    async def check_and_record(self, trigger_source: str = "manual_check") -> Dict[str, Any]:
        """
        Check current display state and record any changes.

        This is the core SAI awareness function - it detects the current
        display configuration and records events when changes occur.

        Args:
            trigger_source: What triggered this check

        Returns:
            Current display state dict with change info
        """
        try:
            # Detect current displays
            context = await self._detector.detect_displays()
            current_state = context.to_dict()

            # Determine if this is a change
            event_type = None
            is_tv = context.is_tv_connected

            if self._last_state is None:
                # First check - record as SAI startup
                event_type = 'SAI_CHECK'
                if is_tv:
                    event_type = 'CONNECTED'
            else:
                last_tv = self._last_state.get('is_tv_connected', False)

                if is_tv and not last_tv:
                    # TV was just connected
                    event_type = 'CONNECTED'
                    logger.info(f"📺 [SAI-MONITOR] TV CONNECTED: {context.tv_name}")
                elif not is_tv and last_tv:
                    # TV was just disconnected
                    event_type = 'DISCONNECTED'
                    logger.info(f"📺 [SAI-MONITOR] TV DISCONNECTED")
                elif self._has_config_changed(current_state, self._last_state):
                    # Configuration changed (e.g., mirror mode toggled)
                    event_type = 'CONFIG_CHANGED'
                    logger.info(f"📺 [SAI-MONITOR] Config changed: {context.display_mode.name}")

            # Record event to database
            if event_type:
                reasoning = [
                    f"SAI Monitor: {trigger_source}",
                    f"Displays: {context.total_displays}",
                    f"Mode: {context.display_mode.name}",
                    f"TV: {context.tv_name or 'None'}",
                    f"Mirrored: {context.is_mirrored}",
                ]

                try:
                    from voice_unlock.metrics_database import get_metrics_database
                    db = get_metrics_database()

                    await db.record_connection_event(
                        event_type=event_type,
                        display_context=current_state,
                        sai_reasoning=reasoning,
                        trigger_source=trigger_source
                    )
                except Exception as e:
                    logger.error(f"Failed to record connection event: {e}")

            # Update last state
            self._last_state = current_state

            # Return state with event info
            return {
                **current_state,
                'event_type': event_type,
                'is_tv_connected': is_tv,
                'tv_name': context.tv_name,
                'tv_brand': context.tv_brand,
                'display_mode': context.display_mode.name,
            }

        except Exception as e:
            logger.error(f"Display check failed: {e}", exc_info=True)
            return {'error': str(e)}

    def _has_config_changed(self, current: Dict, last: Dict) -> bool:
        """Check if display configuration has changed significantly"""
        keys_to_check = [
            'display_mode', 'is_mirrored', 'total_displays'
        ]
        for key in keys_to_check:
            if current.get(key) != last.get(key):
                return True
        return False

    async def get_current_tv_awareness(self) -> Dict[str, Any]:
        """
        Get JARVIS's current awareness of TV connection state.

        This is a fast query that returns what SAI currently knows about
        the TV connection state.

        Returns:
            Dict with TV awareness info
        """
        try:
            from voice_unlock.metrics_database import get_metrics_database
            db = get_metrics_database()

            # Get current state from DB
            state = await db.get_current_tv_state()

            # Enhance with fresh detection if needed
            if not state.get('is_tv_currently_connected'):
                # Quick check if TV might have connected
                context = await self._detector.detect_displays()
                if context.is_tv_connected:
                    # TV connected but DB not updated - update now
                    await self.check_and_record(trigger_source='awareness_check')
                    state = await db.get_current_tv_state()

            return state

        except Exception as e:
            logger.error(f"Failed to get TV awareness: {e}")
            return {'error': str(e)}

    async def start_periodic_monitoring(self, interval_seconds: float = 30):
        """
        Start background monitoring for display changes.

        Args:
            interval_seconds: How often to check (default 30s)
        """
        if self._monitoring:
            logger.warning("Monitor already running")
            return

        self._monitoring = True

        async def monitor_loop():
            logger.info(f"📺 [SAI-MONITOR] Starting periodic monitoring (every {interval_seconds}s)")
            while self._monitoring:
                try:
                    await self.check_and_record(trigger_source='auto_monitor')
                except Exception as e:
                    logger.error(f"Monitor check failed: {e}")

                await asyncio.sleep(interval_seconds)

        self._monitor_task = asyncio.create_task(monitor_loop())
        logger.info("📺 [SAI-MONITOR] Background monitoring started")

    async def stop_monitoring(self):
        """Stop background monitoring"""
        self._monitoring = False
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
            self._monitor_task = None
        logger.info("📺 [SAI-MONITOR] Background monitoring stopped")

    @property
    def is_monitoring(self) -> bool:
        """Check if background monitoring is active"""
        return self._monitoring


# Singleton monitor instance
_monitor_instance: Optional[DisplayConnectionMonitor] = None


async def get_display_monitor() -> DisplayConnectionMonitor:
    """Get or create the singleton monitor instance"""
    global _monitor_instance
    if _monitor_instance is None:
        _monitor_instance = DisplayConnectionMonitor()
    return _monitor_instance


async def check_tv_connection() -> Dict[str, Any]:
    """
    Convenience function to check TV connection state.

    This can be called from anywhere to get JARVIS's current
    awareness of whether a TV is connected.

    Returns:
        Dict with TV connection info
    """
    monitor = await get_display_monitor()
    return await monitor.check_and_record(trigger_source='external_check')


async def get_tv_awareness() -> Dict[str, Any]:
    """
    Get JARVIS's current TV awareness state.

    Returns the most recent known state without forcing a new detection.
    """
    monitor = await get_display_monitor()
    return await monitor.get_current_tv_awareness()


# =============================================================================
# Singleton and Convenience Functions
# =============================================================================

_sai_instance: Optional[DisplayAwareSAI] = None


async def get_display_sai() -> DisplayAwareSAI:
    """Get or create the singleton SAI instance"""
    global _sai_instance
    if _sai_instance is None:
        _sai_instance = DisplayAwareSAI()
    return _sai_instance


async def get_optimal_typing_strategy() -> Tuple[TypingConfig, DisplayContext, List[str]]:
    """Convenience function to get optimal typing strategy"""
    sai = await get_display_sai()
    return await sai.get_optimal_typing_config()


# =============================================================================
# Test Function
# =============================================================================

async def test_display_sai():
    """Test the Display-Aware SAI"""
    logging.basicConfig(level=logging.INFO)

    print("=" * 60)
    print("Display-Aware SAI Test")
    print("=" * 60)

    sai = DisplayAwareSAI()

    print("\n1. Detecting displays...")
    context = await sai.get_display_context()
    print(f"   Total displays: {context.total_displays}")
    print(f"   Mode: {context.display_mode.name}")
    print(f"   Mirrored: {context.is_mirrored}")
    print(f"   TV Connected: {context.is_tv_connected}")
    if context.tv_name:
        print(f"   TV Name: {context.tv_name}")

    print("\n2. Running LangGraph reasoning...")
    config, _, reasoning = await sai.get_optimal_typing_config()

    print(f"\n3. Optimal Strategy: {config.strategy.name}")
    print(f"   Keystroke delay: {config.base_keystroke_delay_ms}ms")
    print(f"   Wake delay: {config.wake_delay_ms}ms")
    print(f"   Use AppleScript fallback: {config.use_applescript_fallback}")

    print("\n4. Reasoning steps:")
    for step in reasoning:
        print(f"   - {step}")

    print("\n" + "=" * 60)
    print("Test complete!")


if __name__ == "__main__":
    asyncio.run(test_display_sai())
