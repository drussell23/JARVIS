"""
JARVIS Tiered Command Router - Three-Tier Security Architecture v2.0
=====================================================================

Routes voice commands to appropriate backends based on security tier:

Tier 0 - "JARVIS" (Instant Local Commands) [NEW in v2.0]:
    - Backend: JARVIS-Prime (local LLM)
    - Permissions: Read-only, Safe APIs
    - VBIA: Optional (low-security quick commands)
    - Latency: <100ms (instant muscle memory)
    - Examples: "What time is it?", "Unlock my screen", "What's my schedule?"
    - Fallback: Auto-escalates to Tier 1 if unavailable

Tier 1 - "JARVIS" (Standard Commands):
    - Backend: Gemini Flash (fast, cheap)
    - Permissions: Read-only, Safe APIs
    - VBIA: Optional or low-threshold (convenience)
    - Examples: "What's the weather?", "Play music", "Search for..."

Tier 2 - "JARVIS ACCESS" / "JARVIS EXECUTE" (Agentic Commands):
    - Backend: Claude 3.5 Sonnet (Computer Use)
    - Permissions: Full OS control (mouse, keyboard, file system)
    - VBIA: Strict enforcement + liveness check
    - Examples: "Open Safari and find that email", "Organize my desktop"

Architecture:
    ┌──────────────────────────────────────────────────────────────────┐
    │                    TieredCommandRouter v2.0                       │
    │  ┌──────────────┐    ┌──────────────┐    ┌───────────────────┐  │
    │  │   Wake Word  │ -> │    Intent    │ -> │   Authentication  │  │
    │  │   Parser     │    │   Classifier │    │   Gate            │  │
    │  └──────────────┘    └──────────────┘    └─────────┬─────────┘  │
    │                                                     │            │
    │  ┌──────────────────────────────────────────────────┼────────┐   │
    │  │                      │                           │        │   │
    │  ▼                      ▼                           ▼        │   │
    │  ┌──────────────┐ ┌──────────────┐          ┌──────────────┐ │   │
    │  │   Tier 0     │ │   Tier 1     │          │   Tier 2     │ │   │
    │  │   Handler    │ │   Handler    │          │   Handler    │ │   │
    │  │ (Local LLM)  │ │  (Gemini)    │          │  (Claude CU) │ │   │
    │  └──────────────┘ └──────────────┘          └──────────────┘ │   │
    └──────────────────────────────────────────────────────────────────┘

Security Features:
- Dynamic VBIA threshold based on command tier
- Intent classification prevents tier bypass
- Audit logging of all escalation attempts
- Watchdog integration for Tier 2 commands
- Automatic downgrade on auth failure
- Local-first routing with cloud fallback (Tier 0 → Tier 1)

Author: JARVIS AI System
Version: 2.0.0
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple, Awaitable

logger = logging.getLogger(__name__)


# =============================================================================
# Configuration
# =============================================================================

@dataclass
class TieredRouterConfig:
    """Configuration for the tiered command router."""

    # Wake word patterns
    tier1_wake_words: List[str] = field(default_factory=lambda: [
        "jarvis",
        "hey jarvis",
        "ok jarvis",
    ])

    tier2_wake_words: List[str] = field(default_factory=lambda: [
        "jarvis access",
        "jarvis execute",
        "jarvis control",
        "jarvis do",
        "jarvis take control",
        "jarvis active mode",
        "jarvis agent mode",
        "jarvis computer",
    ])

    # VBIA thresholds
    tier0_vbia_threshold: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_TIER0_VBIA_THRESHOLD", "0.0"))  # No auth for quick commands
    )
    tier1_vbia_threshold: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_TIER1_VBIA_THRESHOLD", "0.70"))
    )
    tier2_vbia_threshold: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_TIER2_VBIA_THRESHOLD", "0.85"))
    )
    tier2_require_liveness: bool = field(
        default_factory=lambda: os.getenv("JARVIS_TIER2_REQUIRE_LIVENESS", "true").lower() == "true"
    )

    # Intent classification
    dangerous_intent_keywords: List[str] = field(default_factory=lambda: [
        "delete", "remove", "erase", "format", "wipe",
        "sudo", "admin", "root", "password", "credential",
        "send money", "transfer funds", "payment",
        "login as", "impersonate",
    ])

    agentic_intent_keywords: List[str] = field(default_factory=lambda: [
        "click", "type", "scroll", "drag", "move mouse",
        "open app", "switch window", "organize",
        "find and", "locate and", "search and click",
        "fill form", "automate", "control",
    ])

    # Tier 0 quick commands (instant local response)
    tier0_quick_intents: List[str] = field(default_factory=lambda: [
        "unlock", "what time", "what's the time", "current time",
        "what day", "what's the date", "today's date",
        "what's my schedule", "my calendar", "next meeting",
        "battery", "battery level", "charge",
        "hello", "hi jarvis", "good morning", "good night",
        "thank you", "thanks jarvis", "good job",
    ])

    # Backend selection
    tier0_backend: str = field(
        default_factory=lambda: os.getenv("JARVIS_TIER0_BACKEND", "jarvis-prime")
    )
    tier1_backend: str = field(
        default_factory=lambda: os.getenv("JARVIS_TIER1_BACKEND", "gemini")
    )
    tier2_backend: str = field(
        default_factory=lambda: os.getenv("JARVIS_TIER2_BACKEND", "claude")
    )

    # Tier 0 settings
    tier0_enabled: bool = field(
        default_factory=lambda: os.getenv("JARVIS_TIER0_ENABLED", "true").lower() == "true"
    )
    tier0_fallback_to_tier1: bool = field(
        default_factory=lambda: os.getenv("JARVIS_TIER0_FALLBACK", "true").lower() == "true"
    )
    tier0_timeout_ms: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_TIER0_TIMEOUT_MS", "5000"))  # 5 second timeout
    )

    # Audio feedback
    tier2_auth_sound: str = field(
        default_factory=lambda: os.getenv("JARVIS_TIER2_AUTH_SOUND", "/System/Library/Sounds/Glass.aiff")
    )
    tier2_deny_sound: str = field(
        default_factory=lambda: os.getenv("JARVIS_TIER2_DENY_SOUND", "/System/Library/Sounds/Basso.aiff")
    )

    # Watchdog integration
    watchdog_enabled: bool = field(
        default_factory=lambda: os.getenv("JARVIS_WATCHDOG_ENABLED", "true").lower() == "true"
    )


# =============================================================================
# Enums and Data Classes
# =============================================================================

class CommandTier(str, Enum):
    """Security tier for commands."""
    TIER0_LOCAL = "tier0_local"         # Instant local brain (JARVIS-Prime)
    TIER1_STANDARD = "tier1_standard"   # Safe, read-only (Gemini)
    TIER2_AGENTIC = "tier2_agentic"     # Full Computer Use (Claude)
    BLOCKED = "blocked"                  # Dangerous command blocked


class AuthResult(str, Enum):
    """Result of authentication attempt."""
    PASSED = "passed"
    FAILED_THRESHOLD = "failed_threshold"
    FAILED_LIVENESS = "failed_liveness"
    SKIPPED = "skipped"  # For Tier 1 when auth is optional


@dataclass
class ParsedCommand:
    """A parsed voice command with tier classification."""
    raw_text: str
    wake_word: str
    command_body: str
    detected_tier: CommandTier
    intent_keywords: List[str]
    requires_auth: bool
    auth_threshold: float
    suggested_backend: str
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class RouteDecision:
    """Decision from the router."""
    tier: CommandTier
    backend: str
    command: str
    auth_required: bool
    auth_result: Optional[AuthResult]
    vbia_confidence: Optional[float]
    watchdog_armed: bool
    execution_allowed: bool
    denial_reason: Optional[str]
    timestamp: float = field(default_factory=time.time)


# =============================================================================
# Intent Classifier
# =============================================================================

class IntentClassifier:
    """
    Classifies command intent to prevent tier bypass.

    Even if someone says "JARVIS, delete all my files", we detect the
    dangerous intent and either block or escalate to Tier 2 with strict auth.

    v2.0: Added Tier 0 quick intent detection for instant local responses.
    """

    def __init__(self, config: TieredRouterConfig):
        self.config = config

        # Compile patterns for efficiency
        self._dangerous_pattern = re.compile(
            r'\b(' + '|'.join(re.escape(k) for k in config.dangerous_intent_keywords) + r')\b',
            re.IGNORECASE
        )
        self._agentic_pattern = re.compile(
            r'\b(' + '|'.join(re.escape(k) for k in config.agentic_intent_keywords) + r')\b',
            re.IGNORECASE
        )
        # v2.0: Tier 0 quick intents for instant local response
        self._tier0_pattern = re.compile(
            r'\b(' + '|'.join(re.escape(k) for k in config.tier0_quick_intents) + r')\b',
            re.IGNORECASE
        )

    def classify(self, command: str) -> Tuple[CommandTier, List[str], Optional[str]]:
        """
        Classify command intent.

        Returns:
            Tuple of (tier, detected_keywords, block_reason)
        """
        detected_keywords = []

        # Check for dangerous intents FIRST (highest priority)
        dangerous_matches = self._dangerous_pattern.findall(command.lower())
        if dangerous_matches:
            return CommandTier.BLOCKED, dangerous_matches, f"Dangerous intent detected: {', '.join(dangerous_matches)}"

        # Check for agentic intents (requires Tier 2)
        agentic_matches = self._agentic_pattern.findall(command.lower())
        if agentic_matches:
            detected_keywords.extend(agentic_matches)
            return CommandTier.TIER2_AGENTIC, detected_keywords, None

        # v2.0: Check for Tier 0 quick intents (instant local response)
        if self.config.tier0_enabled:
            tier0_matches = self._tier0_pattern.findall(command.lower())
            if tier0_matches:
                detected_keywords.extend(tier0_matches)
                return CommandTier.TIER0_LOCAL, detected_keywords, None

        # Default to Tier 1
        return CommandTier.TIER1_STANDARD, detected_keywords, None

    def is_tier0_candidate(self, command: str) -> bool:
        """
        Check if command is a Tier 0 candidate.

        This is a lightweight check for quick routing decisions.
        """
        if not self.config.tier0_enabled:
            return False
        return bool(self._tier0_pattern.search(command.lower()))


# =============================================================================
# Tiered Command Router
# =============================================================================

class TieredCommandRouter:
    """
    Routes voice commands to appropriate backends based on security tier.

    v2.0 Flow (Local-First):
    1. Parse wake word to determine initial tier
    2. Classify intent (may detect Tier 0 quick command)
    3. If Tier 0: Try JARVIS-Prime first, fallback to Tier 1 if unavailable
    4. Authenticate based on tier requirements
    5. Route to appropriate backend
    6. Arm watchdog for Tier 2 commands
    """

    def __init__(
        self,
        config: Optional[TieredRouterConfig] = None,
        vbia_callback: Optional[Callable[[float], Awaitable[Tuple[bool, float]]]] = None,
        liveness_callback: Optional[Callable[[], Awaitable[bool]]] = None,
        tts_callback: Optional[Callable[[str], Awaitable[None]]] = None,
    ):
        """
        Initialize the router.

        Args:
            config: Router configuration
            vbia_callback: Callback to verify voice (threshold) -> (passed, confidence)
            liveness_callback: Callback to verify liveness -> passed
            tts_callback: Callback for text-to-speech
        """
        self.config = config or TieredRouterConfig()
        self._vbia_callback = vbia_callback
        self._liveness_callback = liveness_callback
        self._tts_callback = tts_callback

        self._intent_classifier = IntentClassifier(self.config)
        self._watchdog = None  # Lazy loaded

        # v2.0: JARVIS-Prime client for Tier 0 (lazy loaded)
        self._jarvis_prime_client = None

        # Stats
        self._route_count = 0
        self._tier0_count = 0
        self._tier0_fallback_count = 0
        self._tier1_count = 0
        self._tier2_count = 0
        self._blocked_count = 0

        logger.info(
            "[TieredRouter] Initialized with tiers: T0={} (enabled={}), T1={}, T2={}".format(
                self.config.tier0_backend,
                self.config.tier0_enabled,
                self.config.tier1_backend,
                self.config.tier2_backend,
            )
        )

    async def _get_jarvis_prime_client(self):
        """Lazy load the JARVIS-Prime client."""
        if self._jarvis_prime_client is None and self.config.tier0_enabled:
            try:
                from core.jarvis_prime_client import get_jarvis_prime_client
                self._jarvis_prime_client = get_jarvis_prime_client()
            except ImportError:
                logger.warning("[TieredRouter] JARVIS-Prime client not available")
        return self._jarvis_prime_client

    async def _get_watchdog(self):
        """Lazy load the watchdog."""
        if self._watchdog is None and self.config.watchdog_enabled:
            try:
                from core.agentic_watchdog import get_watchdog
                self._watchdog = get_watchdog()
            except ImportError:
                logger.warning("[TieredRouter] Watchdog not available")
        return self._watchdog

    # =========================================================================
    # Main Routing
    # =========================================================================

    async def route(self, raw_command: str, audio_data: Optional[bytes] = None) -> RouteDecision:
        """
        Route a voice command to the appropriate backend.

        v2.0: Local-first routing - tries JARVIS-Prime (Tier 0) first for
        quick commands, with automatic fallback to Tier 1.

        Args:
            raw_command: The transcribed voice command
            audio_data: Optional raw audio for VBIA verification

        Returns:
            RouteDecision with routing details
        """
        self._route_count += 1

        # Step 1: Parse wake word
        parsed = self._parse_wake_word(raw_command)
        logger.info(f"[TieredRouter] Parsed: tier={parsed.detected_tier.value}, wake='{parsed.wake_word}'")

        # Step 2: Intent classification (may escalate, block, or detect Tier 0)
        intent_tier, intent_keywords, block_reason = self._intent_classifier.classify(parsed.command_body)

        if block_reason:
            self._blocked_count += 1
            logger.warning(f"[TieredRouter] BLOCKED: {block_reason}")
            await self._announce(f"Command blocked for safety: {block_reason}")
            return RouteDecision(
                tier=CommandTier.BLOCKED,
                backend="none",
                command=parsed.command_body,
                auth_required=False,
                auth_result=None,
                vbia_confidence=None,
                watchdog_armed=False,
                execution_allowed=False,
                denial_reason=block_reason,
            )

        # Determine final tier based on intent classification
        final_tier = intent_tier if intent_tier != CommandTier.TIER1_STANDARD else parsed.detected_tier

        # Intent may escalate Tier 0/1 to Tier 2
        if intent_tier == CommandTier.TIER2_AGENTIC:
            logger.info(f"[TieredRouter] Escalating to Tier 2 due to intent: {intent_keywords}")
            final_tier = CommandTier.TIER2_AGENTIC

        # v2.0: Handle Tier 0 (local brain) routing
        if final_tier == CommandTier.TIER0_LOCAL and self.config.tier0_enabled:
            tier0_available = await self._check_tier0_availability()

            if tier0_available:
                self._tier0_count += 1
                logger.info(f"[TieredRouter] Routing to Tier 0 (local brain): {parsed.command_body[:50]}...")

                return RouteDecision(
                    tier=CommandTier.TIER0_LOCAL,
                    backend=self.config.tier0_backend,
                    command=parsed.command_body,
                    auth_required=self.config.tier0_vbia_threshold > 0,
                    auth_result=AuthResult.SKIPPED,  # Tier 0 typically skips auth
                    vbia_confidence=None,
                    watchdog_armed=False,
                    execution_allowed=True,
                    denial_reason=None,
                )
            else:
                # Fallback to Tier 1
                if self.config.tier0_fallback_to_tier1:
                    self._tier0_fallback_count += 1
                    logger.info("[TieredRouter] Tier 0 unavailable, falling back to Tier 1")
                    final_tier = CommandTier.TIER1_STANDARD
                else:
                    return RouteDecision(
                        tier=CommandTier.TIER0_LOCAL,
                        backend=self.config.tier0_backend,
                        command=parsed.command_body,
                        auth_required=False,
                        auth_result=None,
                        vbia_confidence=None,
                        watchdog_armed=False,
                        execution_allowed=False,
                        denial_reason="JARVIS-Prime (local brain) unavailable",
                    )

        # Step 3: Determine auth requirements for Tier 1/2
        if final_tier == CommandTier.TIER2_AGENTIC:
            auth_required = True
            auth_threshold = self.config.tier2_vbia_threshold
            backend = self.config.tier2_backend
            self._tier2_count += 1
        else:
            auth_required = self.config.tier1_vbia_threshold > 0
            auth_threshold = self.config.tier1_vbia_threshold
            backend = self.config.tier1_backend
            self._tier1_count += 1

        # Step 4: Authenticate
        auth_result = AuthResult.SKIPPED
        vbia_confidence = None

        if auth_required and self._vbia_callback:
            try:
                passed, confidence = await self._vbia_callback(auth_threshold)
                vbia_confidence = confidence

                if not passed:
                    auth_result = AuthResult.FAILED_THRESHOLD
                    await self._play_sound(self.config.tier2_deny_sound)
                    await self._announce("Voice authentication failed. Access denied.")
                    return RouteDecision(
                        tier=final_tier,
                        backend=backend,
                        command=parsed.command_body,
                        auth_required=True,
                        auth_result=auth_result,
                        vbia_confidence=confidence,
                        watchdog_armed=False,
                        execution_allowed=False,
                        denial_reason=f"VBIA failed: {confidence:.2f} < {auth_threshold:.2f}",
                    )

                # Tier 2 requires liveness check
                if final_tier == CommandTier.TIER2_AGENTIC and self.config.tier2_require_liveness:
                    if self._liveness_callback:
                        liveness_passed = await self._liveness_callback()
                        if not liveness_passed:
                            auth_result = AuthResult.FAILED_LIVENESS
                            await self._announce("Liveness check failed. Please speak naturally.")
                            return RouteDecision(
                                tier=final_tier,
                                backend=backend,
                                command=parsed.command_body,
                                auth_required=True,
                                auth_result=auth_result,
                                vbia_confidence=confidence,
                                watchdog_armed=False,
                                execution_allowed=False,
                                denial_reason="Liveness check failed",
                            )

                auth_result = AuthResult.PASSED

            except Exception as e:
                logger.error(f"[TieredRouter] VBIA error: {e}")
                return RouteDecision(
                    tier=final_tier,
                    backend=backend,
                    command=parsed.command_body,
                    auth_required=True,
                    auth_result=AuthResult.FAILED_THRESHOLD,
                    vbia_confidence=None,
                    watchdog_armed=False,
                    execution_allowed=False,
                    denial_reason=f"Authentication error: {e}",
                )

        # Step 5: Arm watchdog for Tier 2
        watchdog_armed = False
        if final_tier == CommandTier.TIER2_AGENTIC:
            watchdog = await self._get_watchdog()
            if watchdog:
                if not watchdog.is_agentic_allowed():
                    return RouteDecision(
                        tier=final_tier,
                        backend=backend,
                        command=parsed.command_body,
                        auth_required=auth_required,
                        auth_result=auth_result,
                        vbia_confidence=vbia_confidence,
                        watchdog_armed=False,
                        execution_allowed=False,
                        denial_reason="Agentic control currently disabled (watchdog)",
                    )
                watchdog_armed = True

            # Play success sound
            await self._play_sound(self.config.tier2_auth_sound)
            await self._announce(f"Access granted. Activating agent mode for: {parsed.command_body[:50]}")

        logger.info(f"[TieredRouter] Routing to {backend}: {parsed.command_body[:50]}...")

        return RouteDecision(
            tier=final_tier,
            backend=backend,
            command=parsed.command_body,
            auth_required=auth_required,
            auth_result=auth_result,
            vbia_confidence=vbia_confidence,
            watchdog_armed=watchdog_armed,
            execution_allowed=True,
            denial_reason=None,
        )

    # =========================================================================
    # Wake Word Parsing
    # =========================================================================

    def _parse_wake_word(self, raw_text: str) -> ParsedCommand:
        """Parse the wake word and extract command body."""
        text_lower = raw_text.lower().strip()

        # Check Tier 2 wake words first (more specific)
        for wake_word in sorted(self.config.tier2_wake_words, key=len, reverse=True):
            if text_lower.startswith(wake_word):
                command_body = raw_text[len(wake_word):].strip()
                # Remove common follow-up words
                command_body = re.sub(r'^[,:\s]+', '', command_body)
                return ParsedCommand(
                    raw_text=raw_text,
                    wake_word=wake_word,
                    command_body=command_body,
                    detected_tier=CommandTier.TIER2_AGENTIC,
                    intent_keywords=[],
                    requires_auth=True,
                    auth_threshold=self.config.tier2_vbia_threshold,
                    suggested_backend=self.config.tier2_backend,
                )

        # Check Tier 1 wake words
        for wake_word in sorted(self.config.tier1_wake_words, key=len, reverse=True):
            if text_lower.startswith(wake_word):
                command_body = raw_text[len(wake_word):].strip()
                command_body = re.sub(r'^[,:\s]+', '', command_body)
                return ParsedCommand(
                    raw_text=raw_text,
                    wake_word=wake_word,
                    command_body=command_body,
                    detected_tier=CommandTier.TIER1_STANDARD,
                    intent_keywords=[],
                    requires_auth=self.config.tier1_vbia_threshold > 0,
                    auth_threshold=self.config.tier1_vbia_threshold,
                    suggested_backend=self.config.tier1_backend,
                )

        # No wake word detected - treat as Tier 1 with full text
        return ParsedCommand(
            raw_text=raw_text,
            wake_word="",
            command_body=raw_text,
            detected_tier=CommandTier.TIER1_STANDARD,
            intent_keywords=[],
            requires_auth=False,
            auth_threshold=0.0,
            suggested_backend=self.config.tier1_backend,
        )

    # =========================================================================
    # Handlers
    # =========================================================================

    async def _check_tier0_availability(self) -> bool:
        """
        Check if JARVIS-Prime (Tier 0) is available.

        Returns:
            True if available for routing
        """
        if not self.config.tier0_enabled:
            return False

        try:
            client = await self._get_jarvis_prime_client()
            if client is None:
                return False
            return await client.is_available()
        except Exception as e:
            logger.debug(f"[TieredRouter] Tier 0 availability check failed: {e}")
            return False

    async def execute_tier0(
        self,
        command: str,
        context: Optional[Dict[str, Any]] = None,
        timeout_ms: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        Execute a Tier 0 command via JARVIS-Prime (local brain).

        This is the fastest path - uses local LLM for instant responses.
        Automatically falls back to Tier 1 if execution fails.

        Args:
            command: The command to execute
            context: Optional context dictionary
            timeout_ms: Optional timeout (uses config default if not specified)

        Returns:
            Result dictionary with success status and response
        """
        logger.info(f"[TieredRouter] Executing Tier 0 (Local): {command[:50]}...")

        timeout = timeout_ms or self.config.tier0_timeout_ms

        try:
            client = await self._get_jarvis_prime_client()
            if client is None:
                raise ImportError("JARVIS-Prime client not available")

            # Build system prompt with context
            system_prompt = (
                "You are JARVIS, an intelligent AI assistant. "
                "Respond concisely and helpfully. "
                "If you need to perform an action, describe what you would do."
            )

            if context:
                if context.get("screen_locked"):
                    system_prompt += " The user's screen is currently locked."
                if context.get("active_app"):
                    system_prompt += f" The user is currently in {context['active_app']}."

            # Execute via JARVIS-Prime with timeout
            from core.jarvis_prime_client import ChatMessage

            response = await asyncio.wait_for(
                client.complete(
                    prompt=command,
                    system_prompt=system_prompt,
                ),
                timeout=timeout / 1000.0,  # Convert ms to seconds
            )

            if response.success:
                return {
                    "success": True,
                    "response": response.content,
                    "backend": "jarvis-prime",
                    "latency_ms": response.latency_ms,
                    "tier": "tier0",
                }
            else:
                # Try fallback to Tier 1
                if self.config.tier0_fallback_to_tier1:
                    logger.warning(f"[TieredRouter] Tier 0 failed, falling back to Tier 1: {response.error}")
                    self._tier0_fallback_count += 1
                    return await self.execute_tier1(command, context)
                else:
                    return {
                        "success": False,
                        "error": response.error or "Tier 0 execution failed",
                        "backend": "jarvis-prime",
                        "tier": "tier0",
                    }

        except asyncio.TimeoutError:
            logger.warning(f"[TieredRouter] Tier 0 timeout ({timeout}ms)")
            if self.config.tier0_fallback_to_tier1:
                self._tier0_fallback_count += 1
                return await self.execute_tier1(command, context)
            return {
                "success": False,
                "error": f"Tier 0 timeout ({timeout}ms)",
                "backend": "jarvis-prime",
                "tier": "tier0",
            }

        except ImportError as e:
            logger.warning(f"[TieredRouter] Tier 0 not available: {e}")
            if self.config.tier0_fallback_to_tier1:
                self._tier0_fallback_count += 1
                return await self.execute_tier1(command, context)
            return {"success": False, "error": f"Tier 0 not available: {e}"}

        except Exception as e:
            logger.error(f"[TieredRouter] Tier 0 execution failed: {e}")
            if self.config.tier0_fallback_to_tier1:
                self._tier0_fallback_count += 1
                return await self.execute_tier1(command, context)
            return {"success": False, "error": str(e)}

    async def execute_tier1(self, command: str, context: Dict[str, Any] = None) -> Dict[str, Any]:
        """
        Execute a Tier 1 command (standard, safe).

        Override this or set a callback for custom Tier 1 handling.
        """
        logger.info(f"[TieredRouter] Executing Tier 1: {command[:50]}...")

        # Default: Try to use Gemini/other safe backend
        try:
            # Import your Gemini/standard handler
            from api.unified_command_processor import process_command
            result = await process_command(command, context or {})
            return {"success": True, "response": result}
        except ImportError:
            return {"success": False, "error": "Tier 1 handler not configured"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def execute_tier2(self, command: str, context: Dict[str, Any] = None) -> Dict[str, Any]:
        """
        Execute a Tier 2 command (agentic, full control).

        This uses the AgenticTaskRunner with Computer Use.
        """
        logger.info(f"[TieredRouter] Executing Tier 2 (Agentic): {command[:50]}...")

        try:
            # Import agentic task runner
            from autonomy.computer_use_tool import get_computer_use_tool

            tool = get_computer_use_tool()

            # Arm watchdog
            watchdog = await self._get_watchdog()
            if watchdog:
                from core.agentic_watchdog import AgenticMode
                await watchdog.task_started(
                    task_id=f"tier2_{int(time.time())}",
                    goal=command,
                    mode=AgenticMode.AUTONOMOUS
                )

            try:
                result = await tool.run(
                    goal=command,
                    context=context,
                    narrate=True,
                )

                success = result.success

                if watchdog:
                    await watchdog.task_completed(
                        task_id=f"tier2_{int(time.time())}",
                        success=success
                    )

                return {
                    "success": success,
                    "response": result.final_message,
                    "actions_count": result.actions_count,
                    "duration_ms": result.total_duration_ms,
                }

            except Exception as e:
                if watchdog:
                    await watchdog.task_completed(
                        task_id=f"tier2_{int(time.time())}",
                        success=False
                    )
                raise

        except ImportError as e:
            return {"success": False, "error": f"Tier 2 handler not available: {e}"}
        except Exception as e:
            logger.error(f"[TieredRouter] Tier 2 execution failed: {e}")
            return {"success": False, "error": str(e)}

    # =========================================================================
    # Helpers
    # =========================================================================

    async def _announce(self, text: str):
        """Announce via TTS."""
        if self._tts_callback:
            try:
                await self._tts_callback(text)
            except Exception as e:
                logger.debug(f"[TieredRouter] TTS failed: {e}")

    async def _play_sound(self, sound_path: str):
        """Play a sound file (macOS)."""
        if not sound_path or not os.path.exists(sound_path):
            return

        try:
            process = await asyncio.create_subprocess_exec(
                "afplay", sound_path,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            # Don't wait - let it play in background
        except Exception as e:
            logger.debug(f"[TieredRouter] Sound playback failed: {e}")

    def get_stats(self) -> Dict[str, Any]:
        """Get routing statistics."""
        return {
            "total_routes": self._route_count,
            "tier0_count": self._tier0_count,
            "tier0_fallback_count": self._tier0_fallback_count,
            "tier1_count": self._tier1_count,
            "tier2_count": self._tier2_count,
            "blocked_count": self._blocked_count,
            "tier0_enabled": self.config.tier0_enabled,
            "tier0_backend": self.config.tier0_backend,
            "tier1_backend": self.config.tier1_backend,
            "tier2_backend": self.config.tier2_backend,
        }


# =============================================================================
# Singleton Access
# =============================================================================

_router_instance: Optional[TieredCommandRouter] = None


def get_tiered_router() -> TieredCommandRouter:
    """Get the global router instance."""
    global _router_instance
    if _router_instance is None:
        _router_instance = TieredCommandRouter()
    return _router_instance


def set_tiered_router(router: TieredCommandRouter):
    """Set the global router instance."""
    global _router_instance
    _router_instance = router
