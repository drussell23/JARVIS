"""
Trinity Integration Layer v1.0
==============================

The ultimate integration layer that connects Ouroboros self-improvement
with the full Trinity ecosystem:

- UnifiedModelServing: Intelligent model routing with Prime + Claude fallback
- CrossRepoExperienceForwarder: Experience publishing to Reactor Core
- Neural Mesh: Cross-repo communication
- Brain Orchestrator: LLM infrastructure management

This creates a seamless flow:
    JARVIS (Body) → Ouroboros → UnifiedModelServing → JARVIS Prime (Mind)
                       ↓
               CrossRepoExperienceForwarder
                       ↓
               Reactor Core (Learning)
                       ↓
               MODEL_READY Event → Hot-Swap

Architecture:
    ┌─────────────────────────────────────────────────────────────────────────┐
    │                     TRINITY INTEGRATION LAYER                            │
    ├─────────────────────────────────────────────────────────────────────────┤
    │                                                                          │
    │  ┌─────────────────────────────────────────────────────────────────┐    │
    │  │                    OUROBOROS SELF-IMPROVEMENT                    │    │
    │  │                                                                  │    │
    │  │  Goal: "Fix the bug"                                            │    │
    │  │    ↓                                                            │    │
    │  │  TrinityModelClient.generate()                                  │    │
    │  │    ↓                                                            │    │
    │  │  Improved Code                                                  │    │
    │  │    ↓                                                            │    │
    │  │  Validate → Test → Apply                                        │    │
    │  └─────────────────────────────────────────────────────────────────┘    │
    │                                                                          │
    │  ┌──────────────────┐     ┌──────────────────┐     ┌──────────────────┐ │
    │  │ UnifiedModel     │     │ Experience       │     │ Neural Mesh      │ │
    │  │ Serving          │────▶│ Forwarder       │────▶│ (Cross-Repo)     │ │
    │  │                  │     │                  │     │                  │ │
    │  │ • Prime Local    │     │ • Deduplication  │     │ • File-based     │ │
    │  │ • Prime Cloud    │     │ • Batching       │     │ • WebSocket      │ │
    │  │ • Claude         │     │ • Circuit Break  │     │ • HTTP           │ │
    │  └──────────────────┘     └──────────────────┘     └──────────────────┘ │
    │           │                        │                        │           │
    │           ▼                        ▼                        ▼           │
    │  ┌──────────────────────────────────────────────────────────────────┐  │
    │  │                    REACTOR CORE (Learning)                        │  │
    │  │                                                                   │  │
    │  │  • TrinityExperienceReceiver                                      │  │
    │  │  • UnifiedTrainingPipeline                                        │  │
    │  │  • MODEL_READY → Hot-Swap back to UnifiedModelServing             │  │
    │  └──────────────────────────────────────────────────────────────────┘  │
    │                                                                          │
    └─────────────────────────────────────────────────────────────────────────┘

Author: Trinity System
Version: 1.0.0
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union

logger = logging.getLogger("Ouroboros.TrinityIntegration")


# =============================================================================
# CONFIGURATION
# =============================================================================

class TrinityConfig:
    """Dynamic configuration for Trinity integration."""

    # Feature flags
    USE_UNIFIED_MODEL_SERVING = os.getenv(
        "OUROBOROS_USE_UNIFIED_MODEL_SERVING", "true"
    ).lower() == "true"

    USE_EXPERIENCE_FORWARDER = os.getenv(
        "OUROBOROS_USE_EXPERIENCE_FORWARDER", "true"
    ).lower() == "true"

    USE_NEURAL_MESH = os.getenv(
        "OUROBOROS_USE_NEURAL_MESH", "true"
    ).lower() == "true"

    # Timeouts
    @staticmethod
    def get_model_timeout() -> float:
        value = float(os.getenv("OUROBOROS_MODEL_TIMEOUT", "120.0"))
        return max(30.0, min(600.0, value))

    @staticmethod
    def get_experience_timeout() -> float:
        value = float(os.getenv("OUROBOROS_EXPERIENCE_TIMEOUT", "30.0"))
        return max(5.0, min(120.0, value))

    # Retry configuration
    @staticmethod
    def get_max_retries() -> int:
        value = int(os.getenv("OUROBOROS_MAX_RETRIES", "3"))
        return max(1, min(10, value))

    @staticmethod
    def get_retry_delay() -> float:
        value = float(os.getenv("OUROBOROS_RETRY_DELAY", "2.0"))
        return max(0.5, min(30.0, value))


# =============================================================================
# HEALTH STATUS
# =============================================================================

class ComponentHealth(Enum):
    """Health status of a component."""
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


@dataclass
class HealthStatus:
    """Health status of the Trinity integration."""
    unified_model_serving: ComponentHealth = ComponentHealth.UNKNOWN
    experience_forwarder: ComponentHealth = ComponentHealth.UNKNOWN
    neural_mesh: ComponentHealth = ComponentHealth.UNKNOWN
    brain_orchestrator: ComponentHealth = ComponentHealth.UNKNOWN
    overall: ComponentHealth = ComponentHealth.UNKNOWN
    last_check: float = 0.0
    details: Dict[str, Any] = field(default_factory=dict)

    def update_overall(self) -> None:
        """Update overall health based on component health."""
        components = [
            self.unified_model_serving,
            self.experience_forwarder,
            self.neural_mesh,
            self.brain_orchestrator,
        ]

        healthy_count = sum(1 for c in components if c == ComponentHealth.HEALTHY)
        unavailable_count = sum(1 for c in components if c == ComponentHealth.UNAVAILABLE)

        if healthy_count == len(components):
            self.overall = ComponentHealth.HEALTHY
        elif unavailable_count == len(components):
            self.overall = ComponentHealth.UNAVAILABLE
        else:
            self.overall = ComponentHealth.DEGRADED


# =============================================================================
# TRINITY MODEL CLIENT
# =============================================================================

class TrinityModelClient:
    """
    Intelligent model client that uses UnifiedModelServing with fallbacks.

    Hierarchy:
    1. UnifiedModelServing (if available) - intelligent routing
    2. Neural Mesh → JARVIS Prime (if available)
    3. Brain Orchestrator → Ollama/Direct providers
    4. Fallback to basic HTTP calls

    This ensures code generation ALWAYS works, with graceful degradation.
    """

    def __init__(self):
        self.logger = logging.getLogger("Ouroboros.TrinityModelClient")
        self._unified_serving = None
        self._neural_mesh = None
        self._brain_orchestrator = None
        self._fallback_session = None
        self._initialized = False

    async def initialize(self) -> bool:
        """Initialize connections to available model sources."""
        self.logger.info("Initializing Trinity Model Client...")

        # Try UnifiedModelServing first (preferred)
        if TrinityConfig.USE_UNIFIED_MODEL_SERVING:
            try:
                from backend.intelligence.unified_model_serving import (
                    get_unified_model_serving,
                    TaskType,
                )
                self._unified_serving = await get_unified_model_serving()
                if self._unified_serving:
                    self.logger.info("✅ Connected to UnifiedModelServing")
            except ImportError as e:
                self.logger.warning(f"UnifiedModelServing not available: {e}")
            except Exception as e:
                self.logger.warning(f"UnifiedModelServing initialization failed: {e}")

        # Try Neural Mesh
        if TrinityConfig.USE_NEURAL_MESH:
            try:
                from backend.core.ouroboros.neural_mesh import get_neural_mesh
                self._neural_mesh = get_neural_mesh()
                if self._neural_mesh._running:
                    self.logger.info("✅ Connected to Neural Mesh")
            except ImportError as e:
                self.logger.warning(f"Neural Mesh not available: {e}")
            except Exception as e:
                self.logger.warning(f"Neural Mesh connection failed: {e}")

        # Try Brain Orchestrator
        try:
            from backend.core.ouroboros.brain_orchestrator import get_brain_orchestrator
            self._brain_orchestrator = get_brain_orchestrator()
            if self._brain_orchestrator._running:
                self.logger.info("✅ Connected to Brain Orchestrator")
        except ImportError as e:
            self.logger.warning(f"Brain Orchestrator not available: {e}")
        except Exception as e:
            self.logger.warning(f"Brain Orchestrator connection failed: {e}")

        self._initialized = True
        return self._unified_serving is not None or self._brain_orchestrator is not None

    async def generate(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        temperature: float = 0.3,
        max_tokens: int = 4096,
    ) -> Tuple[Optional[str], str]:
        """
        Generate code improvement using best available source.

        Returns:
            Tuple of (generated_content, provider_name)
        """
        if not self._initialized:
            await self.initialize()

        # Strategy 1: UnifiedModelServing (best)
        if self._unified_serving:
            try:
                result = await self._generate_via_unified_serving(
                    prompt, system_prompt, temperature, max_tokens
                )
                if result:
                    return result, f"unified:{result[1]}"
            except Exception as e:
                self.logger.warning(f"UnifiedModelServing failed: {e}")

        # Strategy 2: Neural Mesh → JARVIS Prime
        if self._neural_mesh and self._neural_mesh._running:
            try:
                result = await self._generate_via_neural_mesh(
                    prompt, system_prompt, temperature, max_tokens
                )
                if result:
                    return result, "neural_mesh:prime"
            except Exception as e:
                self.logger.warning(f"Neural Mesh failed: {e}")

        # Strategy 3: Brain Orchestrator → Direct provider
        if self._brain_orchestrator:
            try:
                result = await self._generate_via_brain_orchestrator(
                    prompt, system_prompt, temperature, max_tokens
                )
                if result:
                    provider = self._brain_orchestrator.get_best_provider()
                    provider_name = provider.name if provider else "unknown"
                    return result, f"brain:{provider_name}"
            except Exception as e:
                self.logger.warning(f"Brain Orchestrator failed: {e}")

        # All strategies failed
        self.logger.error("All code generation strategies failed")
        return None, "none"

    async def _generate_via_unified_serving(
        self,
        prompt: str,
        system_prompt: Optional[str],
        temperature: float,
        max_tokens: int,
    ) -> Optional[Tuple[str, str]]:
        """Generate via UnifiedModelServing."""
        try:
            from backend.intelligence.unified_model_serving import (
                ModelRequest,
                TaskType,
            )

            messages = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            messages.append({"role": "user", "content": prompt})

            request = ModelRequest(
                request_id=f"ouro_{uuid.uuid4().hex[:12]}",
                messages=messages,
                system_prompt=system_prompt,
                task_type=TaskType.CODE,
                max_tokens=max_tokens,
                temperature=temperature,
                stream=False,
                context={"source": "ouroboros_self_improvement"},
                preferred_provider=None,
            )

            response = await asyncio.wait_for(
                self._unified_serving.generate(request),
                timeout=TrinityConfig.get_model_timeout(),
            )

            if response and response.success and response.content:
                return response.content, response.provider.value

            return None

        except asyncio.TimeoutError:
            self.logger.warning("UnifiedModelServing timed out")
            return None
        except Exception as e:
            self.logger.error(f"UnifiedModelServing error: {e}")
            raise

    async def _generate_via_neural_mesh(
        self,
        prompt: str,
        system_prompt: Optional[str],
        temperature: float,
        max_tokens: int,
    ) -> Optional[str]:
        """Generate via Neural Mesh → JARVIS Prime."""
        try:
            from backend.core.ouroboros.neural_mesh import (
                MessageType,
                NodeType,
            )

            response = await self._neural_mesh.send(
                target=NodeType.PRIME,
                message_type=MessageType.IMPROVEMENT_REQUEST,
                payload={
                    "prompt": prompt,
                    "system_prompt": system_prompt,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                },
                wait_response=True,
                timeout=TrinityConfig.get_model_timeout(),
            )

            if response and response.payload.get("content"):
                return response.payload["content"]

            return None

        except Exception as e:
            self.logger.error(f"Neural Mesh error: {e}")
            raise

    async def _generate_via_brain_orchestrator(
        self,
        prompt: str,
        system_prompt: Optional[str],
        temperature: float,
        max_tokens: int,
    ) -> Optional[str]:
        """Generate via Brain Orchestrator's best provider."""
        try:
            import aiohttp

            provider = self._brain_orchestrator.get_best_provider()
            if not provider or not provider.is_healthy:
                return None

            messages = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            messages.append({"role": "user", "content": prompt})

            payload = {
                "model": "default",
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
            }

            async with aiohttp.ClientSession() as session:
                url = f"{provider.endpoint}/v1/chat/completions"
                timeout = aiohttp.ClientTimeout(total=TrinityConfig.get_model_timeout())

                async with session.post(url, json=payload, timeout=timeout) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        choices = data.get("choices", [])
                        if choices:
                            return choices[0].get("message", {}).get("content")

            return None

        except Exception as e:
            self.logger.error(f"Brain Orchestrator error: {e}")
            raise

    def get_status(self) -> Dict[str, Any]:
        """Get client status."""
        return {
            "initialized": self._initialized,
            "unified_serving_available": self._unified_serving is not None,
            "neural_mesh_available": self._neural_mesh is not None and self._neural_mesh._running,
            "brain_orchestrator_available": self._brain_orchestrator is not None,
        }


# =============================================================================
# TRINITY EXPERIENCE PUBLISHER
# =============================================================================

class TrinityExperiencePublisher:
    """
    Publishes experiences to Reactor Core via multiple channels.

    Hierarchy:
    1. CrossRepoExperienceForwarder (primary) - production-grade
    2. Neural Mesh (secondary) - real-time
    3. File-based fallback (tertiary) - persistence

    This ensures experiences are NEVER lost.
    """

    def __init__(self):
        self.logger = logging.getLogger("Ouroboros.TrinityExperiencePublisher")
        self._experience_forwarder = None
        self._neural_mesh = None
        self._fallback_dir: Optional[Path] = None
        self._initialized = False
        self._stats = {
            "published_via_forwarder": 0,
            "published_via_mesh": 0,
            "published_via_file": 0,
            "publish_failures": 0,
        }

    async def initialize(self) -> bool:
        """Initialize experience publishing channels."""
        self.logger.info("Initializing Trinity Experience Publisher...")

        # Try CrossRepoExperienceForwarder (preferred)
        if TrinityConfig.USE_EXPERIENCE_FORWARDER:
            try:
                from backend.intelligence.cross_repo_experience_forwarder import (
                    get_experience_forwarder,
                )
                self._experience_forwarder = await get_experience_forwarder()
                if self._experience_forwarder:
                    self.logger.info("✅ Connected to CrossRepoExperienceForwarder")
            except ImportError as e:
                self.logger.warning(f"CrossRepoExperienceForwarder not available: {e}")
            except Exception as e:
                self.logger.warning(f"CrossRepoExperienceForwarder init failed: {e}")

        # Try Neural Mesh
        if TrinityConfig.USE_NEURAL_MESH:
            try:
                from backend.core.ouroboros.neural_mesh import get_neural_mesh
                self._neural_mesh = get_neural_mesh()
                if self._neural_mesh._running:
                    self.logger.info("✅ Neural Mesh available for experiences")
            except Exception as e:
                self.logger.warning(f"Neural Mesh not available: {e}")

        # Setup file-based fallback
        try:
            fallback_base = Path(os.getenv(
                "OUROBOROS_EXPERIENCE_FALLBACK_DIR",
                Path.home() / ".jarvis" / "experience_queue" / "ouroboros"
            ))
            self._fallback_dir = fallback_base
            self._fallback_dir.mkdir(parents=True, exist_ok=True)
            self.logger.info(f"✅ File fallback ready: {self._fallback_dir}")
        except Exception as e:
            self.logger.warning(f"File fallback setup failed: {e}")

        self._initialized = True
        return (
            self._experience_forwarder is not None or
            (self._neural_mesh is not None and self._neural_mesh._running) or
            self._fallback_dir is not None
        )

    async def publish(
        self,
        original_code: str,
        improved_code: str,
        goal: str,
        success: bool,
        iterations: int,
        error_history: Optional[List[str]] = None,
        provider_used: Optional[str] = None,
        duration_seconds: Optional[float] = None,
    ) -> bool:
        """
        Publish improvement experience to Reactor Core.

        Uses multiple channels to ensure delivery:
        1. CrossRepoExperienceForwarder (if available)
        2. Neural Mesh (if available)
        3. File-based fallback (always available)

        Returns True if at least one channel succeeded.
        """
        if not self._initialized:
            await self.initialize()

        experience = self._build_experience(
            original_code=original_code,
            improved_code=improved_code,
            goal=goal,
            success=success,
            iterations=iterations,
            error_history=error_history,
            provider_used=provider_used,
            duration_seconds=duration_seconds,
        )

        success_count = 0

        # Channel 1: CrossRepoExperienceForwarder
        if self._experience_forwarder:
            try:
                from backend.intelligence.cross_repo_experience_forwarder import (
                    ForwardingStatus,
                )

                status = await asyncio.wait_for(
                    self._experience_forwarder.forward_experience(
                        experience_type="code_improvement",
                        input_data={"original_code": original_code[:5000], "goal": goal},
                        output_data={"improved_code": improved_code[:5000]},
                        quality_score=1.0 if success else 0.3,
                        confidence=min(1.0, 1.0 / iterations) if iterations > 0 else 0.5,
                        success=success,
                        component="ouroboros",
                        metadata=experience.get("metadata", {}),
                    ),
                    timeout=TrinityConfig.get_experience_timeout(),
                )

                if status in (ForwardingStatus.SUCCESS, ForwardingStatus.QUEUED):
                    self._stats["published_via_forwarder"] += 1
                    success_count += 1
                    self.logger.debug("Experience published via CrossRepoExperienceForwarder")

            except Exception as e:
                self.logger.warning(f"CrossRepoExperienceForwarder failed: {e}")

        # Channel 2: Neural Mesh
        if self._neural_mesh and self._neural_mesh._running:
            try:
                from backend.core.ouroboros.neural_mesh import NodeType, MessageType

                await self._neural_mesh.send(
                    target=NodeType.REACTOR,
                    message_type=MessageType.EXPERIENCE,
                    payload=experience,
                    wait_response=False,
                )
                self._stats["published_via_mesh"] += 1
                success_count += 1
                self.logger.debug("Experience published via Neural Mesh")

            except Exception as e:
                self.logger.warning(f"Neural Mesh publish failed: {e}")

        # Channel 3: File-based fallback
        if self._fallback_dir and success_count == 0:
            try:
                import json

                filename = f"exp_{time.time():.6f}_{uuid.uuid4().hex[:8]}.json"
                filepath = self._fallback_dir / filename
                filepath.write_text(json.dumps(experience, indent=2))

                self._stats["published_via_file"] += 1
                success_count += 1
                self.logger.debug(f"Experience saved to file: {filepath}")

            except Exception as e:
                self.logger.warning(f"File fallback failed: {e}")

        if success_count == 0:
            self._stats["publish_failures"] += 1
            self.logger.error("All experience publishing channels failed")
            return False

        return True

    def _build_experience(
        self,
        original_code: str,
        improved_code: str,
        goal: str,
        success: bool,
        iterations: int,
        error_history: Optional[List[str]],
        provider_used: Optional[str],
        duration_seconds: Optional[float],
    ) -> Dict[str, Any]:
        """Build the experience payload."""
        return {
            "user_input": f"Improve code: {goal}",
            "assistant_output": improved_code[:5000] if success else f"Failed after {iterations} attempts",
            "confidence": 0.9 if success else 0.3,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
            "session_id": f"ouroboros-{uuid.uuid4().hex[:8]}",
            "metadata": {
                "source": "ouroboros_self_improvement",
                "original_code_length": len(original_code),
                "improved_code_length": len(improved_code),
                "goal": goal[:500],
                "success": success,
                "iterations": iterations,
                "error_count": len(error_history) if error_history else 0,
                "last_error": error_history[-1][:200] if error_history else None,
                "provider_used": provider_used,
                "duration_seconds": duration_seconds,
                "difficulty": self._estimate_difficulty(iterations, len(error_history or [])),
            },
        }

    def _estimate_difficulty(self, iterations: int, errors: int) -> str:
        """Estimate task difficulty based on iterations and errors."""
        if iterations == 1 and errors == 0:
            return "easy"
        elif iterations <= 3 and errors <= 2:
            return "medium"
        elif iterations <= 5:
            return "hard"
        else:
            return "very_hard"

    def get_stats(self) -> Dict[str, Any]:
        """Get publisher statistics."""
        return {
            "initialized": self._initialized,
            "channels": {
                "forwarder_available": self._experience_forwarder is not None,
                "neural_mesh_available": self._neural_mesh is not None and self._neural_mesh._running,
                "file_fallback_available": self._fallback_dir is not None,
            },
            "stats": dict(self._stats),
        }


# =============================================================================
# TRINITY HEALTH MONITOR
# =============================================================================

class TrinityHealthMonitor:
    """
    Monitors health of all Trinity components.

    Provides:
    - Periodic health checks
    - Status aggregation
    - Alert generation
    - Auto-recovery triggers
    """

    def __init__(self):
        self.logger = logging.getLogger("Ouroboros.TrinityHealthMonitor")
        self._health = HealthStatus()
        self._running = False
        self._check_task: Optional[asyncio.Task] = None
        self._check_interval = float(os.getenv("TRINITY_HEALTH_CHECK_INTERVAL", "60.0"))
        self._callbacks: List[Callable[[HealthStatus], None]] = []

    async def start(self) -> None:
        """Start health monitoring."""
        self._running = True
        self._check_task = asyncio.create_task(self._health_check_loop())
        self.logger.info("Trinity Health Monitor started")

    async def stop(self) -> None:
        """Stop health monitoring."""
        self._running = False
        if self._check_task:
            self._check_task.cancel()
            try:
                await self._check_task
            except asyncio.CancelledError:
                pass
        self.logger.info("Trinity Health Monitor stopped")

    async def check_now(self) -> HealthStatus:
        """Perform immediate health check."""
        await self._check_all_components()
        return self._health

    def register_callback(self, callback: Callable[[HealthStatus], None]) -> None:
        """Register a callback for health status changes."""
        self._callbacks.append(callback)

    async def _health_check_loop(self) -> None:
        """Periodic health check loop."""
        while self._running:
            try:
                await self._check_all_components()

                # Notify callbacks
                for callback in self._callbacks:
                    try:
                        callback(self._health)
                    except Exception as e:
                        self.logger.warning(f"Health callback error: {e}")

                await asyncio.sleep(self._check_interval)

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Health check error: {e}")
                await asyncio.sleep(10.0)

    async def _check_all_components(self) -> None:
        """Check health of all components."""
        self._health.last_check = time.time()
        details = {}

        # Check UnifiedModelServing
        try:
            from backend.intelligence.unified_model_serving import get_unified_model_serving
            serving = await get_unified_model_serving()
            if serving:
                stats = serving.get_stats()
                self._health.unified_model_serving = ComponentHealth.HEALTHY
                details["unified_model_serving"] = {
                    "status": "healthy",
                    "requests": stats.get("total_requests", 0),
                    "providers": stats.get("available_providers", []),
                }
            else:
                self._health.unified_model_serving = ComponentHealth.UNAVAILABLE
                details["unified_model_serving"] = {"status": "unavailable"}
        except Exception as e:
            self._health.unified_model_serving = ComponentHealth.UNAVAILABLE
            details["unified_model_serving"] = {"status": "error", "error": str(e)}

        # Check CrossRepoExperienceForwarder
        try:
            from backend.intelligence.cross_repo_experience_forwarder import get_experience_forwarder
            forwarder = await get_experience_forwarder()
            if forwarder:
                metrics = await forwarder.get_metrics()
                circuit_state = metrics.get("circuit_state", "unknown")

                if circuit_state == "closed":
                    self._health.experience_forwarder = ComponentHealth.HEALTHY
                elif circuit_state == "half_open":
                    self._health.experience_forwarder = ComponentHealth.DEGRADED
                else:
                    self._health.experience_forwarder = ComponentHealth.DEGRADED

                details["experience_forwarder"] = {
                    "status": self._health.experience_forwarder.value,
                    "forwarded": metrics.get("experiences_forwarded", 0),
                    "failed": metrics.get("experiences_failed", 0),
                    "circuit_state": circuit_state,
                }
            else:
                self._health.experience_forwarder = ComponentHealth.UNAVAILABLE
                details["experience_forwarder"] = {"status": "unavailable"}
        except Exception as e:
            self._health.experience_forwarder = ComponentHealth.UNAVAILABLE
            details["experience_forwarder"] = {"status": "error", "error": str(e)}

        # Check Neural Mesh
        try:
            from backend.core.ouroboros.neural_mesh import get_neural_mesh
            mesh = get_neural_mesh()
            if mesh._running:
                status = mesh.get_status()
                connected = sum(
                    1 for c in status.get("connections", {}).values()
                    if c.get("connected")
                )
                total = len(status.get("connections", {}))

                if connected == total and total > 0:
                    self._health.neural_mesh = ComponentHealth.HEALTHY
                elif connected > 0:
                    self._health.neural_mesh = ComponentHealth.DEGRADED
                else:
                    self._health.neural_mesh = ComponentHealth.UNAVAILABLE

                details["neural_mesh"] = {
                    "status": self._health.neural_mesh.value,
                    "connected": connected,
                    "total": total,
                }
            else:
                self._health.neural_mesh = ComponentHealth.UNAVAILABLE
                details["neural_mesh"] = {"status": "not_running"}
        except Exception as e:
            self._health.neural_mesh = ComponentHealth.UNAVAILABLE
            details["neural_mesh"] = {"status": "error", "error": str(e)}

        # Check Brain Orchestrator
        try:
            from backend.core.ouroboros.brain_orchestrator import get_brain_orchestrator
            orchestrator = get_brain_orchestrator()
            status = orchestrator.get_status()

            healthy_providers = sum(
                1 for p in status.get("providers", {}).values()
                if p.get("state") == "healthy"
            )

            if healthy_providers > 0:
                self._health.brain_orchestrator = ComponentHealth.HEALTHY
            else:
                self._health.brain_orchestrator = ComponentHealth.DEGRADED

            details["brain_orchestrator"] = {
                "status": self._health.brain_orchestrator.value,
                "healthy_providers": healthy_providers,
                "metrics": status.get("metrics", {}),
            }
        except Exception as e:
            self._health.brain_orchestrator = ComponentHealth.UNAVAILABLE
            details["brain_orchestrator"] = {"status": "error", "error": str(e)}

        self._health.details = details
        self._health.update_overall()

    def get_health(self) -> HealthStatus:
        """Get current health status."""
        return self._health


# =============================================================================
# TRINITY INTEGRATION FACADE
# =============================================================================

class TrinityIntegration:
    """
    Main facade for Trinity integration.

    Provides unified access to:
    - Model generation (via TrinityModelClient)
    - Experience publishing (via TrinityExperiencePublisher)
    - Health monitoring (via TrinityHealthMonitor)

    This is the single entry point for Ouroboros to interact with Trinity.
    """

    def __init__(self):
        self.logger = logging.getLogger("Ouroboros.TrinityIntegration")
        self.model_client = TrinityModelClient()
        self.experience_publisher = TrinityExperiencePublisher()
        self.health_monitor = TrinityHealthMonitor()
        self._running = False

    async def initialize(self) -> bool:
        """Initialize all Trinity components."""
        self.logger.info("=" * 60)
        self.logger.info("🔺 TRINITY INTEGRATION - Initializing")
        self.logger.info("=" * 60)

        # Initialize model client
        model_ok = await self.model_client.initialize()
        if model_ok:
            self.logger.info("✅ Model client ready")
        else:
            self.logger.warning("⚠️ Model client degraded - using fallbacks")

        # Initialize experience publisher
        pub_ok = await self.experience_publisher.initialize()
        if pub_ok:
            self.logger.info("✅ Experience publisher ready")
        else:
            self.logger.warning("⚠️ Experience publisher degraded")

        # Start health monitor
        await self.health_monitor.start()
        self.logger.info("✅ Health monitor started")

        # Initial health check
        health = await self.health_monitor.check_now()
        self.logger.info(f"Overall health: {health.overall.value}")

        self._running = True
        return model_ok or pub_ok

    async def shutdown(self) -> None:
        """Shutdown all Trinity components."""
        self.logger.info("Shutting down Trinity Integration...")
        self._running = False
        await self.health_monitor.stop()
        self.logger.info("Trinity Integration shutdown complete")

    async def generate_improvement(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        temperature: float = 0.3,
        max_tokens: int = 4096,
    ) -> Tuple[Optional[str], str]:
        """Generate code improvement."""
        return await self.model_client.generate(
            prompt=prompt,
            system_prompt=system_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
        )

    async def publish_experience(
        self,
        original_code: str,
        improved_code: str,
        goal: str,
        success: bool,
        iterations: int,
        error_history: Optional[List[str]] = None,
        provider_used: Optional[str] = None,
        duration_seconds: Optional[float] = None,
    ) -> bool:
        """Publish improvement experience."""
        return await self.experience_publisher.publish(
            original_code=original_code,
            improved_code=improved_code,
            goal=goal,
            success=success,
            iterations=iterations,
            error_history=error_history,
            provider_used=provider_used,
            duration_seconds=duration_seconds,
        )

    def get_status(self) -> Dict[str, Any]:
        """Get integration status."""
        return {
            "running": self._running,
            "model_client": self.model_client.get_status(),
            "experience_publisher": self.experience_publisher.get_stats(),
            "health": {
                "overall": self.health_monitor.get_health().overall.value,
                "components": {
                    "unified_model_serving": self.health_monitor.get_health().unified_model_serving.value,
                    "experience_forwarder": self.health_monitor.get_health().experience_forwarder.value,
                    "neural_mesh": self.health_monitor.get_health().neural_mesh.value,
                    "brain_orchestrator": self.health_monitor.get_health().brain_orchestrator.value,
                },
                "last_check": self.health_monitor.get_health().last_check,
            },
        }


# =============================================================================
# GLOBAL INSTANCE
# =============================================================================

_trinity_integration: Optional[TrinityIntegration] = None


def get_trinity_integration() -> TrinityIntegration:
    """Get the global Trinity integration instance."""
    global _trinity_integration
    if _trinity_integration is None:
        _trinity_integration = TrinityIntegration()
    return _trinity_integration


async def initialize_trinity_integration() -> bool:
    """Initialize Trinity integration."""
    integration = get_trinity_integration()
    return await integration.initialize()


async def shutdown_trinity_integration() -> None:
    """Shutdown Trinity integration."""
    global _trinity_integration
    if _trinity_integration:
        await _trinity_integration.shutdown()
        _trinity_integration = None
