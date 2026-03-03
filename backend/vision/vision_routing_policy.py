"""
Vision Routing Policy — Single Source of Truth (v290.0)
======================================================

All vision analysis requests route through this module.
No one reimplements routing. Deterministic, typed, traceable.

J-Prime is PRIMARY (free, GCP). Claude API is SECONDARY (costs money).

Decision precedence:
  1. cloud_offload_required → J-Prime (mandatory, memory gate)
  2. priority="high" AND NOT cloud_offload → Claude (explicit escalation)
  3. J-Prime healthy → J-Prime (default)
  4. J-Prime degraded/down → Claude (fallback)
  5. Both down → cooldown error
"""

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

class ProviderState(Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    COOLDOWN = "cooldown"
    HARD_DOWN = "hard_down"


@dataclass
class VisionRequest:
    """Typed input for vision routing."""
    image_base64: str
    prompt: str
    priority: str = "normal"  # "normal" | "high"
    cloud_offload_required: bool = False
    max_tokens: int = 512
    temperature: float = 0.1
    timeout: float = 120.0
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class VisionRoutingDecision:
    """Typed, traceable routing decision."""
    provider: str           # "jprime" | "claude_api"
    reason: str             # Human-readable decision trace
    confidence: float       # 0-1 routing confidence
    fallback_chain: List[str] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)


@dataclass
class VisionResponse:
    """Typed output from vision execution."""
    content: str
    provider_used: str
    decision: VisionRoutingDecision
    latency_seconds: float = 0.0
    fallback_used: bool = False
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Provider health tracker
# ---------------------------------------------------------------------------

class _ProviderHealth:
    """Tracks a single provider's health with explicit state transitions."""

    def __init__(
        self,
        name: str,
        *,
        failure_threshold: int = 3,
        cooldown_seconds: float = 60.0,
    ):
        self.name = name
        self._state = ProviderState.HEALTHY
        self._consecutive_failures = 0
        self._failure_threshold = failure_threshold
        self._cooldown_seconds = float(
            os.environ.get("JARVIS_VISION_PROVIDER_COOLDOWN", str(cooldown_seconds))
        )
        self._cooldown_until: float = 0.0
        self._last_success: float = 0.0

    @property
    def state(self) -> ProviderState:
        return self._state

    def check_and_update_cooldown(self) -> ProviderState:
        """Check cooldown expiry and transition state if needed.

        Call this explicitly before routing decisions — NOT inside
        the ``state`` property, to avoid mutation-on-read.
        """
        if self._state == ProviderState.COOLDOWN and time.time() >= self._cooldown_until:
            self._state = ProviderState.DEGRADED
            self._consecutive_failures = 0
        return self._state

    def record_success(self) -> None:
        self._consecutive_failures = 0
        self._state = ProviderState.HEALTHY
        self._last_success = time.time()

    def record_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= self._failure_threshold:
            self._state = ProviderState.COOLDOWN
            self._cooldown_until = time.time() + self._cooldown_seconds
            logger.warning(
                "[VisionRoutingPolicy] %s entered COOLDOWN for %.0fs "
                "(%d consecutive failures)",
                self.name, self._cooldown_seconds, self._consecutive_failures,
            )
        else:
            self._state = ProviderState.DEGRADED

    def mark_hard_down(self) -> None:
        """External signal: provider is definitively unavailable."""
        self._state = ProviderState.HARD_DOWN


# ---------------------------------------------------------------------------
# Vision Routing Policy (singleton)
# ---------------------------------------------------------------------------

class VisionRoutingPolicy:
    """Deterministic vision provider selection.

    Consumers call ``execute()`` which routes + executes + handles fallback.
    """

    _instance: Optional['VisionRoutingPolicy'] = None
    _instance_lock: threading.Lock = threading.Lock()

    def __init__(self) -> None:
        self._jprime = _ProviderHealth("jprime")
        self._claude = _ProviderHealth("claude_api")
        self._both_failed_until: float = 0.0
        self._dual_fail_cooldown = float(
            os.environ.get("VISION_DUAL_FAIL_COOLDOWN", "60")
        )
        self._jprime_min_response_len = int(
            os.environ.get("VISION_JPRIME_MIN_RESPONSE_LENGTH", "20")
        )
        self._jprime_fallback_enabled = (
            os.environ.get("VISION_JPRIME_FALLBACK", "true").lower() == "true"
        )

    # -- singleton --------------------------------------------------------

    @classmethod
    def get_instance(cls) -> 'VisionRoutingPolicy':
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    # -- public API -------------------------------------------------------

    def route(self, request: VisionRequest) -> VisionRoutingDecision:
        """Deterministic routing with full decision trace."""

        # Dual-fail cooldown gate
        if time.time() < self._both_failed_until:
            remaining = self._both_failed_until - time.time()
            return VisionRoutingDecision(
                provider="none",
                reason=f"Both providers failed, cooldown {remaining:.0f}s remaining",
                confidence=0.0,
                fallback_chain=[],
            )

        cloud_offload = request.cloud_offload_required
        force_claude = request.priority == "high" and not cloud_offload
        jprime_state = self._jprime.check_and_update_cooldown()
        claude_state = self._claude.check_and_update_cooldown()

        # 1. Cloud offload required → J-Prime mandatory
        if cloud_offload:
            return VisionRoutingDecision(
                provider="jprime",
                reason="cloud_offload_required: local memory insufficient, "
                       "J-Prime mandatory",
                confidence=0.95,
                fallback_chain=["claude_api"] if self._jprime_fallback_enabled else [],
            )

        # 2. Explicit high-priority escalation → Claude
        if force_claude:
            return VisionRoutingDecision(
                provider="claude_api",
                reason="priority=high: explicit escalation to Claude API",
                confidence=0.90,
                fallback_chain=["jprime"],
            )

        # 3. J-Prime healthy → J-Prime (default)
        if jprime_state == ProviderState.HEALTHY:
            return VisionRoutingDecision(
                provider="jprime",
                reason="J-Prime healthy (default primary)",
                confidence=0.95,
                fallback_chain=(
                    ["claude_api"] if self._jprime_fallback_enabled else []
                ),
            )

        # 4. J-Prime degraded/cooldown/down → Claude fallback
        if jprime_state in (ProviderState.DEGRADED, ProviderState.COOLDOWN,
                            ProviderState.HARD_DOWN):
            if claude_state in (ProviderState.HEALTHY, ProviderState.DEGRADED):
                return VisionRoutingDecision(
                    provider="claude_api",
                    reason=f"J-Prime {jprime_state.value}, falling back to Claude",
                    confidence=0.80,
                    fallback_chain=[],
                )

        # 5. Both down
        return VisionRoutingDecision(
            provider="none",
            reason=f"Both providers unavailable (jprime={jprime_state.value}, "
                   f"claude={claude_state.value})",
            confidence=0.0,
            fallback_chain=[],
        )

    async def execute(
        self,
        request: VisionRequest,
        *,
        jprime_caller,
        claude_caller,
    ) -> VisionResponse:
        """Route + execute + quality gate + fallback chain.

        Args:
            request: Typed vision request.
            jprime_caller: ``async (image_b64, prompt, timeout) -> str``
            claude_caller: ``async (image_b64, prompt) -> str``
        """
        decision = self.route(request)
        logger.info(
            "[VisionRoutingPolicy] Routed to %s — %s",
            decision.provider, decision.reason,
        )

        if decision.provider == "none":
            return VisionResponse(
                content="",
                provider_used="none",
                decision=decision,
                error=decision.reason,
            )

        start = time.time()
        try:
            content = await self._call_provider(
                decision.provider, request, jprime_caller, claude_caller,
            )
            latency = time.time() - start

            # Quality gate for J-Prime: short response → escalate
            if (
                decision.provider == "jprime"
                and self._jprime_fallback_enabled
                and len(content.strip()) < self._jprime_min_response_len
                and "claude_api" in decision.fallback_chain
            ):
                logger.info(
                    "[VisionRoutingPolicy] J-Prime response too short "
                    "(%d < %d), escalating to Claude",
                    len(content.strip()), self._jprime_min_response_len,
                )
                content = await self._call_provider(
                    "claude_api", request, jprime_caller, claude_caller,
                )
                # Short response is a quality signal, not a health failure.
                # Don't penalize J-Prime health state — the provider itself
                # responded successfully, just with brief content that may
                # be correct for simple queries.
                return VisionResponse(
                    content=content,
                    provider_used="claude_api",
                    decision=decision,
                    latency_seconds=time.time() - start,
                    fallback_used=True,
                )

            self._record_success(decision.provider)
            return VisionResponse(
                content=content,
                provider_used=decision.provider,
                decision=decision,
                latency_seconds=latency,
            )

        except Exception as primary_err:
            logger.warning(
                "[VisionRoutingPolicy] %s failed: %s",
                decision.provider, primary_err,
            )
            self._record_failure(decision.provider)

            # Try fallback chain
            for fallback in decision.fallback_chain:
                try:
                    content = await self._call_provider(
                        fallback, request, jprime_caller, claude_caller,
                    )
                    self._record_success(fallback)
                    return VisionResponse(
                        content=content,
                        provider_used=fallback,
                        decision=decision,
                        latency_seconds=time.time() - start,
                        fallback_used=True,
                    )
                except Exception as fb_err:
                    logger.warning(
                        "[VisionRoutingPolicy] Fallback %s also failed: %s",
                        fallback, fb_err,
                    )
                    self._record_failure(fallback)

            # Set dual-fail cooldown ONLY when both providers are
            # actually unhealthy (not just because fallback chain was empty)
            both_unhealthy = (
                self._jprime.state != ProviderState.HEALTHY
                and self._claude.state != ProviderState.HEALTHY
            )
            if both_unhealthy:
                self._both_failed_until = time.time() + self._dual_fail_cooldown
                logger.error(
                    "[VisionRoutingPolicy] Both providers unhealthy. "
                    "Cooldown %.0fs", self._dual_fail_cooldown,
                )
            else:
                logger.warning(
                    "[VisionRoutingPolicy] Primary %s failed (fallback "
                    "chain exhausted), but other provider may still be healthy",
                    decision.provider,
                )
            return VisionResponse(
                content="",
                provider_used="none",
                decision=decision,
                latency_seconds=time.time() - start,
                error=f"All providers failed: {primary_err}",
            )

    # -- provider health public API ---------------------------------------

    def get_provider_state(self, provider: str) -> ProviderState:
        if provider == "jprime":
            return self._jprime.state
        elif provider == "claude_api":
            return self._claude.state
        return ProviderState.HARD_DOWN

    # -- internals --------------------------------------------------------

    async def _call_provider(self, provider, request, jprime_caller, claude_caller):
        if provider == "jprime":
            return await jprime_caller(
                request.image_base64, request.prompt, request.timeout,
            )
        elif provider == "claude_api":
            return await claude_caller(request.image_base64, request.prompt)
        raise ValueError(f"Unknown provider: {provider}")

    def _record_success(self, provider: str) -> None:
        if provider == "jprime":
            self._jprime.record_success()
        elif provider == "claude_api":
            self._claude.record_success()

    def _record_failure(self, provider: str) -> None:
        if provider == "jprime":
            self._jprime.record_failure()
        elif provider == "claude_api":
            self._claude.record_failure()


# ---------------------------------------------------------------------------
# Module-level accessor
# ---------------------------------------------------------------------------

def get_vision_routing_policy() -> VisionRoutingPolicy:
    """Get the singleton VisionRoutingPolicy instance."""
    return VisionRoutingPolicy.get_instance()
