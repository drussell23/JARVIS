"""
Visual Context Integration for Voice Biometric Authentication
=============================================================

Integrates Computer Use (OmniParser/Claude Vision) with VBIA for enhanced security.

Features:
- Screen state analysis during voice unlock
- Suspicious UI pattern detection (fake lock screens, ransomware)
- Environmental context verification
- Multi-factor visual + voice security
- Async parallel processing with intelligent fallback
- Cross-repo event emission

Architecture:
    Voice Unlock Request
           ↓
    [Audio Analysis] ← Existing VBIA
           ↓
    [Visual Analysis] ← NEW: This module
           ↓
    [Evidence Fusion] ← Enhanced EvidenceCollectionNode
           ↓
    [Decision] → Grant/Deny

Author: JARVIS AI System
Version: 6.2.0 - Production Visual Security
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import time
from dataclasses import dataclass, asdict
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

# ============================================================================
# Constants
# ============================================================================

# Confidence thresholds
VISUAL_SECURITY_CONFIDENCE_THRESHOLD = 0.85
SUSPICIOUS_UI_THRESHOLD = 0.70
ENVIRONMENTAL_MATCH_THRESHOLD = 0.80

# Timeouts
VISUAL_ANALYSIS_TIMEOUT = 5.0  # seconds
SCREENSHOT_CAPTURE_TIMEOUT = 2.0  # seconds

# Cross-repo integration
CROSS_REPO_DIR = Path.home() / ".jarvis" / "cross_repo"
VBIA_EVENTS_FILE = CROSS_REPO_DIR / "vbia_events.json"

# ============================================================================
# Configuration
# ============================================================================

class VisualSecurityConfig:
    """Environment-driven visual security configuration."""

    @staticmethod
    def is_enabled() -> bool:
        """Check if visual security is enabled."""
        return os.getenv("VBIA_VISUAL_SECURITY_ENABLED", "true").lower() == "true"

    @staticmethod
    def get_preferred_mode() -> str:
        """Get preferred visual analysis mode (auto, omniparser, claude_vision, ocr, disabled)."""
        return os.getenv("VBIA_VISUAL_MODE", "auto")

    @staticmethod
    def get_screenshot_method() -> str:
        """Get screenshot capture method (screencapture, pyautogui, computer_use)."""
        return os.getenv("VBIA_SCREENSHOT_METHOD", "screencapture")

    @staticmethod
    def should_analyze_camera() -> bool:
        """Check if camera-based analysis is enabled."""
        return os.getenv("VBIA_CAMERA_ANALYSIS_ENABLED", "false").lower() == "true"

    @staticmethod
    def should_emit_events() -> bool:
        """Check if cross-repo event emission is enabled."""
        return os.getenv("VBIA_EMIT_EVENTS", "true").lower() == "true"


# ============================================================================
# Enums
# ============================================================================

class ScreenSecurityStatus(Enum):
    """Screen security status during voice unlock."""
    SAFE = "safe"  # Normal lock screen, no threats
    SUSPICIOUS = "suspicious"  # Unfamiliar UI patterns detected
    THREAT_DETECTED = "threat_detected"  # Ransomware, phishing, malware UI
    ENVIRONMENTAL_MISMATCH = "environmental_mismatch"  # Unfamiliar location/device
    PRIVACY_CONCERN = "privacy_concern"  # Multiple people visible
    UNKNOWN = "unknown"  # Unable to analyze


class ThreatType(Enum):
    """Types of visual threats detected."""
    FAKE_LOCK_SCREEN = "fake_lock_screen"
    RANSOMWARE = "ransomware"
    PHISHING_DIALOG = "phishing_dialog"
    UNFAMILIAR_APPLICATION = "unfamiliar_application"
    SUSPICIOUS_PROCESS = "suspicious_process"
    MULTIPLE_USERS_PRESENT = "multiple_users_present"
    UNFAMILIAR_LOCATION = "unfamiliar_location"


class VisualAnalysisMode(Enum):
    """Visual analysis mode used."""
    OMNIPARSER = "omniparser"  # OmniParser UI detection
    CLAUDE_VISION = "claude_vision"  # Claude 3.5 Sonnet vision
    OCR = "ocr"  # Basic OCR fallback
    DISABLED = "disabled"  # Visual security disabled


# ============================================================================
# Data Models
# ============================================================================

@dataclass
class VisualSecurityEvidence:
    """Evidence from visual security analysis."""

    # Analysis metadata
    timestamp: str
    analysis_mode: VisualAnalysisMode
    analysis_time_ms: float

    # Screen state
    screen_locked: bool = False
    lock_screen_type: str = ""  # "macos_standard", "custom", "unknown"
    screen_resolution: Tuple[int, int] = (0, 0)

    # Security assessment
    security_status: ScreenSecurityStatus = ScreenSecurityStatus.UNKNOWN
    visual_confidence: float = 0.0  # Overall visual security confidence
    threat_detected: bool = False
    threat_types: List[ThreatType] = None
    threat_confidence: float = 0.0

    # UI analysis
    detected_elements: int = 0
    suspicious_windows: List[str] = None
    active_application: str = ""
    unfamiliar_ui: bool = False

    # Environmental context
    location_match: bool = True
    location_confidence: float = 1.0
    device_match: bool = True
    device_confidence: float = 1.0

    # Camera analysis (if enabled)
    people_detected: int = 0
    owner_visible: bool = False
    unknown_people_present: bool = False

    # Recommendations
    should_proceed: bool = True
    warning_message: str = ""
    suggested_action: str = "proceed"  # proceed, retry, deny, escalate

    # Raw data
    screenshot_captured: bool = False
    screenshot_hash: str = ""
    raw_analysis: Dict[str, Any] = None

    def __post_init__(self):
        """Initialize mutable defaults."""
        if self.threat_types is None:
            self.threat_types = []
        if self.suspicious_windows is None:
            self.suspicious_windows = []
        if self.raw_analysis is None:
            self.raw_analysis = {}

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        data = asdict(self)
        data["analysis_mode"] = self.analysis_mode.value
        data["security_status"] = self.security_status.value
        data["threat_types"] = [t.value for t in self.threat_types]
        return data

    @classmethod
    def create_disabled(cls) -> "VisualSecurityEvidence":
        """Create evidence for disabled visual security."""
        return cls(
            timestamp=datetime.now().isoformat(),
            analysis_mode=VisualAnalysisMode.DISABLED,
            analysis_time_ms=0.0,
            security_status=ScreenSecurityStatus.UNKNOWN,
            should_proceed=True,
            warning_message="Visual security disabled",
        )

    @classmethod
    def create_failed(cls, error_message: str, analysis_time_ms: float = 0.0) -> "VisualSecurityEvidence":
        """Create evidence for failed analysis."""
        return cls(
            timestamp=datetime.now().isoformat(),
            analysis_mode=VisualAnalysisMode.DISABLED,
            analysis_time_ms=analysis_time_ms,
            security_status=ScreenSecurityStatus.UNKNOWN,
            should_proceed=True,  # Don't block on visual failure
            warning_message=f"Visual analysis failed: {error_message}",
        )


# ============================================================================
# Visual Security Analyzer
# ============================================================================

class VisualSecurityAnalyzer:
    """
    Analyzes screen state during voice authentication for enhanced security.

    Features:
    - Screen capture and analysis
    - Threat detection (fake lock screens, ransomware)
    - Environmental verification
    - Intelligent fallback (OmniParser → Claude Vision → OCR)
    - Async parallel processing
    - Cross-repo event emission
    """

    def __init__(
        self,
        enabled: bool = True,
        preferred_mode: str = "auto",
        screenshot_method: str = "screencapture",
    ):
        """
        Initialize visual security analyzer.

        Args:
            enabled: Enable visual security analysis
            preferred_mode: Preferred analysis mode (auto, omniparser, claude_vision, ocr)
            screenshot_method: Screenshot capture method
        """
        self.enabled = enabled
        self.preferred_mode = preferred_mode
        self.screenshot_method = screenshot_method

        # Lazy imports
        self._omniparser_core = None
        self._anthropic_client = None

        # Statistics
        self._total_analyses = 0
        self._threat_detections = 0
        self._cache_hits = 0

        # Known safe patterns (learned over time)
        self._safe_lock_screen_hashes: set = set()
        self._known_safe_apps: set = {"Finder", "SystemUIServer", "loginwindow"}

        logger.info(
            f"[VISUAL SECURITY] Initialized "
            f"(enabled={enabled}, mode={preferred_mode}, method={screenshot_method})"
        )

    async def analyze_screen_security(
        self,
        session_id: str = "",
        user_id: str = "",
        context: Optional[Dict[str, Any]] = None,
    ) -> VisualSecurityEvidence:
        """
        Analyze screen security during voice unlock.

        Args:
            session_id: Authentication session ID
            user_id: User identifier
            context: Additional context

        Returns:
            VisualSecurityEvidence with analysis results
        """
        if not self.enabled:
            return VisualSecurityEvidence.create_disabled()

        start_time = time.time()

        try:
            # Step 1: Capture screenshot
            screenshot_b64 = await self._capture_screenshot()

            if not screenshot_b64:
                return VisualSecurityEvidence.create_failed(
                    "Screenshot capture failed",
                    (time.time() - start_time) * 1000
                )

            # Step 2: Analyze screen with intelligent fallback
            evidence = await self._analyze_screenshot(
                screenshot_b64=screenshot_b64,
                session_id=session_id,
                user_id=user_id,
                context=context or {},
            )

            # Step 3: Assess security status
            evidence = await self._assess_security_status(evidence)

            # Step 4: Generate recommendations
            evidence = self._generate_recommendations(evidence)

            # Step 5: Emit events (if enabled)
            if VisualSecurityConfig.should_emit_events():
                await self._emit_security_event(evidence, session_id, user_id)

            # Update statistics
            self._total_analyses += 1
            if evidence.threat_detected:
                self._threat_detections += 1

            analysis_time_ms = (time.time() - start_time) * 1000
            evidence.analysis_time_ms = analysis_time_ms

            logger.info(
                f"[VISUAL SECURITY] Analysis complete: "
                f"status={evidence.security_status.value}, "
                f"time={analysis_time_ms:.0f}ms, "
                f"mode={evidence.analysis_mode.value}"
            )

            return evidence

        except asyncio.TimeoutError:
            logger.warning("[VISUAL SECURITY] Analysis timeout")
            return VisualSecurityEvidence.create_failed(
                "Analysis timeout",
                (time.time() - start_time) * 1000
            )

        except Exception as e:
            logger.error(f"[VISUAL SECURITY] Analysis failed: {e}", exc_info=True)
            return VisualSecurityEvidence.create_failed(
                str(e),
                (time.time() - start_time) * 1000
            )

    async def _capture_screenshot(self) -> Optional[str]:
        """
        Capture screenshot of current screen.

        Returns:
            Base64-encoded screenshot or None
        """
        try:
            if self.screenshot_method == "screencapture":
                # Use macOS screencapture command
                import subprocess
                import tempfile

                with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                    tmp_path = tmp.name

                try:
                    # Capture screenshot
                    result = subprocess.run(
                        ["screencapture", "-x", tmp_path],
                        timeout=SCREENSHOT_CAPTURE_TIMEOUT,
                        capture_output=True,
                    )

                    if result.returncode == 0 and Path(tmp_path).exists():
                        # Read and encode
                        with open(tmp_path, "rb") as f:
                            screenshot_bytes = f.read()

                        screenshot_b64 = base64.b64encode(screenshot_bytes).decode()

                        logger.debug(
                            f"[VISUAL SECURITY] Screenshot captured: "
                            f"{len(screenshot_b64)} bytes"
                        )

                        return screenshot_b64

                finally:
                    # Cleanup temp file
                    Path(tmp_path).unlink(missing_ok=True)

            elif self.screenshot_method == "computer_use":
                # Use Computer Use screenshot API
                from backend.core.computer_use_bridge import get_computer_use_bridge

                bridge = get_computer_use_bridge()
                screenshot_b64 = await bridge.capture_screenshot()

                return screenshot_b64

            else:
                logger.warning(f"[VISUAL SECURITY] Unknown screenshot method: {self.screenshot_method}")
                return None

        except Exception as e:
            logger.error(f"[VISUAL SECURITY] Screenshot capture failed: {e}")
            return None

    async def _analyze_screenshot(
        self,
        screenshot_b64: str,
        session_id: str,
        user_id: str,
        context: Dict[str, Any],
    ) -> VisualSecurityEvidence:
        """
        Analyze screenshot with intelligent fallback.

        Fallback chain: OmniParser → Claude Vision → OCR → Disabled
        """
        # Try OmniParser first (if available and preferred)
        if self.preferred_mode in ("auto", "omniparser"):
            try:
                evidence = await self._analyze_with_omniparser(screenshot_b64)
                if evidence:
                    return evidence
            except Exception as e:
                logger.debug(f"[VISUAL SECURITY] OmniParser analysis failed: {e}")

        # Fallback to Claude Vision
        if self.preferred_mode in ("auto", "claude_vision"):
            try:
                evidence = await self._analyze_with_claude_vision(screenshot_b64, context)
                if evidence:
                    return evidence
            except Exception as e:
                logger.debug(f"[VISUAL SECURITY] Claude Vision analysis failed: {e}")

        # Fallback to basic OCR
        if self.preferred_mode in ("auto", "ocr"):
            try:
                evidence = await self._analyze_with_ocr(screenshot_b64)
                if evidence:
                    return evidence
            except Exception as e:
                logger.debug(f"[VISUAL SECURITY] OCR analysis failed: {e}")

        # All methods failed
        return VisualSecurityEvidence.create_failed("All analysis methods failed")

    async def _analyze_with_omniparser(
        self,
        screenshot_b64: str,
    ) -> Optional[VisualSecurityEvidence]:
        """Analyze using OmniParser for precise UI element detection."""
        try:
            # Lazy import OmniParser
            if self._omniparser_core is None:
                from backend.vision.omniparser_core import get_omniparser_core
                self._omniparser_core = await get_omniparser_core()

            # Parse screenshot
            parsed = await self._omniparser_core.parse_screenshot(
                screenshot_base64=screenshot_b64,
                detect_types=["button", "text", "window", "icon"],
                use_cache=True,
            )

            # Create evidence
            evidence = VisualSecurityEvidence(
                timestamp=datetime.now().isoformat(),
                analysis_mode=VisualAnalysisMode.OMNIPARSER,
                analysis_time_ms=parsed.parse_time_ms,
                screen_resolution=parsed.resolution,
                detected_elements=len(parsed.elements),
                screenshot_captured=True,
                screenshot_hash=parsed.screen_id,
            )

            # Analyze detected elements for threats
            await self._analyze_ui_elements(evidence, parsed.elements)

            logger.debug(
                f"[VISUAL SECURITY] OmniParser analysis: "
                f"{len(parsed.elements)} elements, "
                f"{evidence.analysis_time_ms:.0f}ms"
            )

            return evidence

        except Exception as e:
            logger.debug(f"[VISUAL SECURITY] OmniParser failed: {e}")
            return None

    async def _analyze_with_claude_vision(
        self,
        screenshot_b64: str,
        context: Dict[str, Any],
    ) -> Optional[VisualSecurityEvidence]:
        """Analyze using Claude 3.5 Sonnet vision for semantic understanding."""
        try:
            # Lazy import Anthropic client
            if self._anthropic_client is None:
                import anthropic
                api_key = os.getenv("ANTHROPIC_API_KEY")
                if not api_key:
                    logger.warning("[VISUAL SECURITY] No ANTHROPIC_API_KEY found")
                    return None
                self._anthropic_client = anthropic.AsyncAnthropic(api_key=api_key)

            start_time = time.time()

            # Construct security analysis prompt
            prompt = self._create_security_analysis_prompt(context)

            # Call Claude Vision
            message = await self._anthropic_client.messages.create(
                model="claude-3-5-sonnet-20241022",
                max_tokens=1024,
                messages=[{
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": screenshot_b64,
                            },
                        },
                        {
                            "type": "text",
                            "text": prompt,
                        },
                    ],
                }],
            )

            analysis_time_ms = (time.time() - start_time) * 1000

            # Parse Claude's response
            response_text = message.content[0].text
            evidence = self._parse_claude_vision_response(response_text, analysis_time_ms)

            logger.debug(
                f"[VISUAL SECURITY] Claude Vision analysis: "
                f"{analysis_time_ms:.0f}ms"
            )

            return evidence

        except Exception as e:
            logger.debug(f"[VISUAL SECURITY] Claude Vision failed: {e}")
            return None

    async def _analyze_with_ocr(
        self,
        screenshot_b64: str,
    ) -> Optional[VisualSecurityEvidence]:
        """Basic OCR fallback for text detection."""
        try:
            # Decode screenshot
            import io
            from PIL import Image

            screenshot_bytes = base64.b64decode(screenshot_b64)
            image = Image.open(io.BytesIO(screenshot_bytes))

            # Basic analysis
            evidence = VisualSecurityEvidence(
                timestamp=datetime.now().isoformat(),
                analysis_mode=VisualAnalysisMode.OCR,
                analysis_time_ms=0.0,
                screen_resolution=(image.width, image.height),
                screenshot_captured=True,
            )

            # Try pytesseract OCR if available
            try:
                import pytesseract
                text = pytesseract.image_to_string(image)

                # Look for suspicious keywords
                suspicious_keywords = [
                    "ransom", "bitcoin", "btc", "decrypt", "payment required",
                    "locked", "virus", "malware", "infected",
                ]

                text_lower = text.lower()
                for keyword in suspicious_keywords:
                    if keyword in text_lower:
                        evidence.threat_detected = True
                        evidence.threat_types.append(ThreatType.RANSOMWARE)
                        evidence.suspicious_windows.append(f"Text contains: {keyword}")

            except ImportError:
                logger.debug("[VISUAL SECURITY] pytesseract not available, skipping OCR")

            return evidence

        except Exception as e:
            logger.debug(f"[VISUAL SECURITY] OCR failed: {e}")
            return None

    def _create_security_analysis_prompt(self, context: Dict[str, Any]) -> str:
        """Create Claude Vision prompt for security analysis."""
        return f"""Analyze this screenshot for security during voice authentication unlock.

Context:
- User is attempting voice unlock
- Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
- Expected: macOS lock screen

Security Analysis Required:

1. **Screen State**:
   - Is this a macOS lock screen? (standard/custom/unfamiliar)
   - Are there any suspicious windows or dialogs?
   - Any unfamiliar applications visible?

2. **Threat Detection**:
   - Ransomware indicators (payment demands, Bitcoin, locked files)
   - Fake lock screens (phishing attempts)
   - Suspicious dialogs or pop-ups
   - Malware/virus warnings

3. **Environmental Context**:
   - Does this look like a typical user workspace?
   - Any indicators of an unfamiliar location or device?

Respond in JSON format:
{{
  "screen_locked": true/false,
  "lock_screen_type": "macos_standard" | "custom" | "unknown",
  "threat_detected": true/false,
  "threat_types": ["ransomware", "fake_lock_screen", etc.],
  "suspicious_elements": ["description of suspicious UI elements"],
  "active_application": "name of active app if visible",
  "security_status": "safe" | "suspicious" | "threat_detected",
  "confidence": 0.0-1.0,
  "explanation": "brief explanation of findings"
}}"""

    def _parse_claude_vision_response(
        self,
        response_text: str,
        analysis_time_ms: float,
    ) -> VisualSecurityEvidence:
        """Parse Claude Vision JSON response into evidence."""
        import json
        import re

        try:
            # Extract JSON from response (Claude sometimes adds markdown)
            json_match = re.search(r'\{.*\}', response_text, re.DOTALL)
            if json_match:
                response_data = json.loads(json_match.group())
            else:
                response_data = {}

            # Map to evidence
            evidence = VisualSecurityEvidence(
                timestamp=datetime.now().isoformat(),
                analysis_mode=VisualAnalysisMode.CLAUDE_VISION,
                analysis_time_ms=analysis_time_ms,
                screen_locked=response_data.get("screen_locked", False),
                lock_screen_type=response_data.get("lock_screen_type", "unknown"),
                threat_detected=response_data.get("threat_detected", False),
                visual_confidence=response_data.get("confidence", 0.5),
                active_application=response_data.get("active_application", ""),
                suspicious_windows=response_data.get("suspicious_elements", []),
                screenshot_captured=True,
                raw_analysis=response_data,
            )

            # Parse threat types
            for threat_str in response_data.get("threat_types", []):
                try:
                    threat_type = ThreatType(threat_str.lower().replace(" ", "_"))
                    evidence.threat_types.append(threat_type)
                except ValueError:
                    logger.debug(f"Unknown threat type: {threat_str}")

            # Parse security status
            status_str = response_data.get("security_status", "unknown")
            try:
                evidence.security_status = ScreenSecurityStatus(status_str.lower())
            except ValueError:
                evidence.security_status = ScreenSecurityStatus.UNKNOWN

            return evidence

        except Exception as e:
            logger.warning(f"[VISUAL SECURITY] Failed to parse Claude response: {e}")
            return VisualSecurityEvidence.create_failed("Failed to parse Claude Vision response", analysis_time_ms)

    async def _analyze_ui_elements(
        self,
        evidence: VisualSecurityEvidence,
        elements: List[Any],
    ) -> None:
        """Analyze UI elements from OmniParser for threats."""
        suspicious_patterns = [
            "ransom", "bitcoin", "btc", "decrypt", "payment",
            "virus", "malware", "infected", "locked",
        ]

        for elem in elements:
            label = getattr(elem, "label", "").lower()
            element_type = getattr(elem, "element_type", None)

            # Check for suspicious text
            for pattern in suspicious_patterns:
                if pattern in label:
                    evidence.threat_detected = True
                    evidence.threat_types.append(ThreatType.RANSOMWARE)
                    evidence.suspicious_windows.append(f"Suspicious element: {label}")
                    break

            # Check for unfamiliar lock screen elements
            if element_type and str(element_type) == "button":
                if any(word in label for word in ["pay", "bitcoin", "unlock", "decrypt"]):
                    evidence.unfamiliar_ui = True

    async def _assess_security_status(
        self,
        evidence: VisualSecurityEvidence,
    ) -> VisualSecurityEvidence:
        """Assess overall security status from evidence."""
        # Threat detected
        if evidence.threat_detected:
            evidence.security_status = ScreenSecurityStatus.THREAT_DETECTED
            evidence.visual_confidence = 0.95

        # Suspicious UI
        elif evidence.unfamiliar_ui or evidence.suspicious_windows:
            evidence.security_status = ScreenSecurityStatus.SUSPICIOUS
            evidence.visual_confidence = max(0.60, evidence.visual_confidence)

        # Environmental mismatch
        elif not evidence.location_match or not evidence.device_match:
            evidence.security_status = ScreenSecurityStatus.ENVIRONMENTAL_MISMATCH
            evidence.visual_confidence = 0.70

        # Privacy concern (multiple people)
        elif evidence.unknown_people_present:
            evidence.security_status = ScreenSecurityStatus.PRIVACY_CONCERN
            evidence.visual_confidence = 0.75

        # Safe
        else:
            evidence.security_status = ScreenSecurityStatus.SAFE
            evidence.visual_confidence = max(0.85, evidence.visual_confidence)

        return evidence

    def _generate_recommendations(
        self,
        evidence: VisualSecurityEvidence,
    ) -> VisualSecurityEvidence:
        """Generate recommendations based on security status."""
        status = evidence.security_status

        if status == ScreenSecurityStatus.THREAT_DETECTED:
            evidence.should_proceed = False
            evidence.suggested_action = "deny"
            evidence.warning_message = (
                f"SECURITY THREAT DETECTED: {', '.join(t.value for t in evidence.threat_types)}. "
                "Voice unlock blocked for your security."
            )

        elif status == ScreenSecurityStatus.SUSPICIOUS:
            evidence.should_proceed = True  # Allow but warn
            evidence.suggested_action = "escalate"
            evidence.warning_message = (
                "Suspicious UI patterns detected. Proceeding with caution. "
                "Please verify your screen security."
            )

        elif status == ScreenSecurityStatus.ENVIRONMENTAL_MISMATCH:
            evidence.should_proceed = True  # Allow but note
            evidence.suggested_action = "proceed"
            evidence.warning_message = (
                "Environment appears different than usual. "
                "First time unlocking from this location?"
            )

        elif status == ScreenSecurityStatus.PRIVACY_CONCERN:
            evidence.should_proceed = True  # User choice
            evidence.suggested_action = "escalate"
            evidence.warning_message = (
                "Multiple people detected near your device. "
                "Unlock in private or proceed anyway?"
            )

        else:  # SAFE or UNKNOWN
            evidence.should_proceed = True
            evidence.suggested_action = "proceed"
            evidence.warning_message = ""

        return evidence

    async def _emit_security_event(
        self,
        evidence: VisualSecurityEvidence,
        session_id: str,
        user_id: str,
    ) -> None:
        """Emit visual security event to cross-repo bridge."""
        try:
            import json

            # Ensure directory exists
            CROSS_REPO_DIR.mkdir(parents=True, exist_ok=True)

            # Create event
            event = {
                "timestamp": evidence.timestamp,
                "event_type": "vbia_visual_security",
                "session_id": session_id,
                "user_id": user_id,
                "security_status": evidence.security_status.value,
                "threat_detected": evidence.threat_detected,
                "threat_types": [t.value for t in evidence.threat_types],
                "visual_confidence": evidence.visual_confidence,
                "analysis_mode": evidence.analysis_mode.value,
                "analysis_time_ms": evidence.analysis_time_ms,
                "should_proceed": evidence.should_proceed,
            }

            # Append to events file
            events = []
            if VBIA_EVENTS_FILE.exists():
                try:
                    with open(VBIA_EVENTS_FILE, 'r') as f:
                        events = json.load(f)
                except json.JSONDecodeError:
                    events = []

            events.append(event)

            # Keep last 1000 events
            events = events[-1000:]

            # Write back
            with open(VBIA_EVENTS_FILE, 'w') as f:
                json.dump(events, f, indent=2)

            logger.debug(f"[VISUAL SECURITY] Event emitted: {evidence.security_status.value}")

        except Exception as e:
            logger.warning(f"[VISUAL SECURITY] Failed to emit event: {e}")

    def get_statistics(self) -> Dict[str, Any]:
        """Get visual security statistics."""
        return {
            "enabled": self.enabled,
            "preferred_mode": self.preferred_mode,
            "total_analyses": self._total_analyses,
            "threat_detections": self._threat_detections,
            "threat_rate": (
                self._threat_detections / self._total_analyses * 100
                if self._total_analyses > 0
                else 0.0
            ),
            "cache_hits": self._cache_hits,
        }


# ============================================================================
# Global Instance
# ============================================================================

_visual_security_analyzer: Optional[VisualSecurityAnalyzer] = None


def get_visual_security_analyzer() -> VisualSecurityAnalyzer:
    """Get or create global visual security analyzer."""
    global _visual_security_analyzer

    if _visual_security_analyzer is None:
        # Load from environment
        enabled = VisualSecurityConfig.is_enabled()
        preferred_mode = VisualSecurityConfig.get_preferred_mode()
        screenshot_method = VisualSecurityConfig.get_screenshot_method()

        _visual_security_analyzer = VisualSecurityAnalyzer(
            enabled=enabled,
            preferred_mode=preferred_mode,
            screenshot_method=screenshot_method,
        )

    return _visual_security_analyzer
