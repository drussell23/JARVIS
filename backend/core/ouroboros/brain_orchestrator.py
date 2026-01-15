"""
Brain Orchestrator - LLM Infrastructure Manager
================================================

Advanced orchestrator for managing LLM infrastructure:
- Automatic provider discovery and startup
- Health monitoring with auto-recovery
- Load balancing across providers
- Graceful degradation and failover
- Model warm-up and preloading
- Resource-aware scheduling

This is the "Defibrillator" that ensures intelligence infrastructure
is always available for Ouroboros self-improvement operations.

Architecture:
    ┌─────────────────────────────────────────────────────────────────────────┐
    │                      BRAIN ORCHESTRATOR v2.0                             │
    ├─────────────────────────────────────────────────────────────────────────┤
    │                                                                          │
    │   ┌─────────────┐     ┌─────────────┐     ┌─────────────┐               │
    │   │   JARVIS    │     │   OLLAMA    │     │  ANTHROPIC  │               │
    │   │   PRIME     │     │   LOCAL     │     │    API      │               │
    │   │  (Primary)  │     │ (Fallback)  │     │  (Emergency)│               │
    │   │             │     │             │     │             │               │
    │   │ Port 8000   │     │ Port 11434  │     │   Cloud     │               │
    │   └──────┬──────┘     └──────┬──────┘     └──────┬──────┘               │
    │          │                   │                   │                      │
    │          └───────────────────┴───────────────────┘                      │
    │                              │                                          │
    │                    ┌─────────▼─────────┐                                │
    │                    │  LOAD BALANCER    │                                │
    │                    │                   │                                │
    │                    │ • Health checks   │                                │
    │                    │ • Failover logic  │                                │
    │                    │ • Request routing │                                │
    │                    └───────────────────┘                                │
    │                                                                          │
    └─────────────────────────────────────────────────────────────────────────┘

Author: Trinity System
Version: 2.0.0
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

try:
    import aiohttp
except ImportError:
    aiohttp = None

try:
    import psutil
except ImportError:
    psutil = None

logger = logging.getLogger("Ouroboros.BrainOrchestrator")


# =============================================================================
# CONFIGURATION
# =============================================================================

class BrainConfig:
    """Configuration for brain orchestrator."""

    # Provider endpoints
    PRIME_HOST = os.getenv("JARVIS_PRIME_HOST", "localhost")
    PRIME_PORT = int(os.getenv("JARVIS_PRIME_PORT", "8000"))
    OLLAMA_HOST = os.getenv("OLLAMA_HOST", "localhost")
    OLLAMA_PORT = int(os.getenv("OLLAMA_PORT", "11434"))

    # Timeouts
    STARTUP_TIMEOUT = float(os.getenv("BRAIN_STARTUP_TIMEOUT", "120.0"))
    HEALTH_CHECK_INTERVAL = float(os.getenv("BRAIN_HEALTH_INTERVAL", "10.0"))
    HEALTH_CHECK_TIMEOUT = float(os.getenv("BRAIN_HEALTH_TIMEOUT", "5.0"))

    # Auto-recovery
    AUTO_RESTART_ENABLED = os.getenv("BRAIN_AUTO_RESTART", "true").lower() == "true"
    MAX_RESTART_ATTEMPTS = int(os.getenv("BRAIN_MAX_RESTARTS", "3"))
    RESTART_COOLDOWN = float(os.getenv("BRAIN_RESTART_COOLDOWN", "30.0"))

    # Required models for Ollama
    REQUIRED_MODELS = os.getenv("BRAIN_REQUIRED_MODELS", "deepseek-coder-v2,codellama").split(",")

    # Paths
    PRIME_SCRIPT_PATHS = [
        Path(os.getenv("JARVIS_PRIME_SCRIPT", "")),
        Path.home() / "Documents/repos/JARVIS-AI-Agent/backend/ai/prime_server.py",
        Path.home() / "Documents/repos/JARVIS-Prime/server.py",
    ]


# =============================================================================
# ENUMS
# =============================================================================

class ProviderType(Enum):
    """Type of LLM provider."""
    JARVIS_PRIME = "jarvis_prime"
    OLLAMA = "ollama"
    ANTHROPIC = "anthropic"


class ProviderState(Enum):
    """State of an LLM provider."""
    UNKNOWN = "unknown"
    STARTING = "starting"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    STOPPED = "stopped"


class LoadBalancerStrategy(Enum):
    """Strategy for load balancing."""
    PRIMARY_FAILOVER = "primary_failover"  # Use primary, failover if down
    ROUND_ROBIN = "round_robin"            # Distribute across healthy providers
    LEAST_LATENCY = "least_latency"        # Route to fastest provider
    WEIGHTED = "weighted"                   # Weighted distribution


# =============================================================================
# DATA STRUCTURES
# =============================================================================

@dataclass
class ProviderInfo:
    """Information about an LLM provider."""
    type: ProviderType
    name: str
    host: str
    port: int
    state: ProviderState = ProviderState.UNKNOWN
    process: Optional[subprocess.Popen] = None
    pid: Optional[int] = None
    last_health_check: float = 0.0
    latency_ms: float = float("inf")
    consecutive_failures: int = 0
    restart_count: int = 0
    last_restart: float = 0.0
    models: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def endpoint(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def is_healthy(self) -> bool:
        return self.state == ProviderState.HEALTHY

    @property
    def is_local(self) -> bool:
        return self.type in (ProviderType.JARVIS_PRIME, ProviderType.OLLAMA)


@dataclass
class HealthCheckResult:
    """Result of a health check."""
    provider: ProviderType
    healthy: bool
    latency_ms: float = 0.0
    models: List[str] = field(default_factory=list)
    error: Optional[str] = None
    timestamp: float = field(default_factory=time.time)


# =============================================================================
# PROVIDER MANAGER
# =============================================================================

class ProviderManager:
    """
    Manages a single LLM provider.

    Handles:
    - Health checking
    - Process management (start/stop)
    - Model verification
    - Auto-recovery
    """

    def __init__(self, info: ProviderInfo):
        self.info = info
        self._session: Optional[aiohttp.ClientSession] = None
        self._health_task: Optional[asyncio.Task] = None
        self._callbacks: List[Callable[[ProviderState], None]] = []

    async def _get_session(self) -> Optional[aiohttp.ClientSession]:
        """Get or create aiohttp session."""
        if not aiohttp:
            return None
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        """Close the manager."""
        if self._health_task:
            self._health_task.cancel()
            try:
                await self._health_task
            except asyncio.CancelledError:
                pass

        if self._session and not self._session.closed:
            await self._session.close()

    async def check_health(self) -> HealthCheckResult:
        """Check provider health."""
        start_time = time.time()

        try:
            session = await self._get_session()
            if not session:
                return HealthCheckResult(
                    provider=self.info.type,
                    healthy=False,
                    error="aiohttp not available",
                )

            # Determine endpoint based on provider type
            if self.info.type == ProviderType.OLLAMA:
                url = f"{self.info.endpoint}/api/tags"
            else:
                url = f"{self.info.endpoint}/v1/models"

            async with session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=BrainConfig.HEALTH_CHECK_TIMEOUT),
            ) as resp:
                latency = (time.time() - start_time) * 1000
                self.info.latency_ms = latency
                self.info.last_health_check = time.time()

                if resp.status == 200:
                    data = await resp.json()

                    # Extract models
                    models = []
                    if self.info.type == ProviderType.OLLAMA:
                        models = [m.get("name", "") for m in data.get("models", [])]
                    else:
                        models = [m.get("id", "") for m in data.get("data", [])]

                    self.info.models = models
                    self.info.consecutive_failures = 0
                    self._update_state(ProviderState.HEALTHY)

                    return HealthCheckResult(
                        provider=self.info.type,
                        healthy=True,
                        latency_ms=latency,
                        models=models,
                    )
                else:
                    self.info.consecutive_failures += 1
                    self._update_state(ProviderState.DEGRADED)

                    return HealthCheckResult(
                        provider=self.info.type,
                        healthy=False,
                        latency_ms=latency,
                        error=f"HTTP {resp.status}",
                    )

        except asyncio.TimeoutError:
            self.info.consecutive_failures += 1
            self._update_state(ProviderState.UNHEALTHY)
            return HealthCheckResult(
                provider=self.info.type,
                healthy=False,
                error="Timeout",
            )
        except Exception as e:
            self.info.consecutive_failures += 1
            self._update_state(ProviderState.UNHEALTHY)
            return HealthCheckResult(
                provider=self.info.type,
                healthy=False,
                error=str(e),
            )

    def _update_state(self, new_state: ProviderState) -> None:
        """Update provider state and notify callbacks."""
        if self.info.state != new_state:
            old_state = self.info.state
            self.info.state = new_state
            logger.info(f"Provider {self.info.name} state: {old_state.value} -> {new_state.value}")
            for callback in self._callbacks:
                try:
                    callback(new_state)
                except Exception as e:
                    logger.error(f"Callback error: {e}")

    async def start(self) -> bool:
        """Start the provider if it's local and not running."""
        if not self.info.is_local:
            return False

        if self.info.is_healthy:
            return True

        self._update_state(ProviderState.STARTING)

        if self.info.type == ProviderType.OLLAMA:
            return await self._start_ollama()
        elif self.info.type == ProviderType.JARVIS_PRIME:
            return await self._start_prime()

        return False

    async def _start_ollama(self) -> bool:
        """Start Ollama service."""
        try:
            # Check if ollama is installed
            ollama_path = shutil.which("ollama")
            if not ollama_path:
                logger.warning("Ollama not installed")
                return False

            # Start ollama serve
            process = subprocess.Popen(
                ["ollama", "serve"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )

            self.info.process = process
            self.info.pid = process.pid

            # Wait for startup
            for _ in range(30):
                await asyncio.sleep(1)
                result = await self.check_health()
                if result.healthy:
                    logger.info(f"Ollama started (PID: {process.pid})")
                    return True

            logger.warning("Ollama startup timeout")
            return False

        except Exception as e:
            logger.error(f"Failed to start Ollama: {e}")
            return False

    async def _start_prime(self) -> bool:
        """Start JARVIS Prime server."""
        try:
            # Find prime script
            script_path = None
            for path in BrainConfig.PRIME_SCRIPT_PATHS:
                if path and path.exists():
                    script_path = path
                    break

            if not script_path:
                logger.warning("JARVIS Prime script not found")
                return False

            # Start server
            process = subprocess.Popen(
                [sys.executable, str(script_path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )

            self.info.process = process
            self.info.pid = process.pid

            # Wait for startup
            for _ in range(60):
                await asyncio.sleep(1)
                result = await self.check_health()
                if result.healthy:
                    logger.info(f"JARVIS Prime started (PID: {process.pid})")
                    return True

            logger.warning("JARVIS Prime startup timeout")
            return False

        except Exception as e:
            logger.error(f"Failed to start JARVIS Prime: {e}")
            return False

    async def stop(self) -> bool:
        """Stop the provider if it's local."""
        if not self.info.is_local or not self.info.process:
            return False

        try:
            self.info.process.terminate()
            try:
                self.info.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.info.process.kill()

            self.info.process = None
            self.info.pid = None
            self._update_state(ProviderState.STOPPED)
            return True

        except Exception as e:
            logger.error(f"Failed to stop {self.info.name}: {e}")
            return False

    async def restart(self) -> bool:
        """Restart the provider."""
        if not self.info.is_local:
            return False

        # Check cooldown
        if time.time() - self.info.last_restart < BrainConfig.RESTART_COOLDOWN:
            logger.warning(f"Restart cooldown for {self.info.name}")
            return False

        # Check max restarts
        if self.info.restart_count >= BrainConfig.MAX_RESTART_ATTEMPTS:
            logger.error(f"Max restart attempts reached for {self.info.name}")
            return False

        logger.info(f"Restarting {self.info.name}...")
        await self.stop()
        await asyncio.sleep(2)

        success = await self.start()
        if success:
            self.info.restart_count += 1
            self.info.last_restart = time.time()

        return success

    def register_callback(self, callback: Callable[[ProviderState], None]) -> None:
        """Register a state change callback."""
        self._callbacks.append(callback)


# =============================================================================
# LOAD BALANCER
# =============================================================================

class LoadBalancer:
    """
    Load balancer for distributing requests across providers.

    Supports multiple strategies:
    - Primary failover (default)
    - Round robin
    - Least latency
    - Weighted
    """

    def __init__(self, strategy: LoadBalancerStrategy = LoadBalancerStrategy.PRIMARY_FAILOVER):
        self.strategy = strategy
        self._providers: Dict[ProviderType, ProviderManager] = {}
        self._priority_order: List[ProviderType] = [
            ProviderType.JARVIS_PRIME,
            ProviderType.OLLAMA,
            ProviderType.ANTHROPIC,
        ]
        self._round_robin_index = 0
        self._weights: Dict[ProviderType, float] = {
            ProviderType.JARVIS_PRIME: 1.0,
            ProviderType.OLLAMA: 0.8,
            ProviderType.ANTHROPIC: 0.5,
        }

    def register_provider(self, manager: ProviderManager) -> None:
        """Register a provider manager."""
        self._providers[manager.info.type] = manager

    def get_best_provider(self) -> Optional[ProviderManager]:
        """Get the best available provider based on strategy."""
        healthy = [p for p in self._providers.values() if p.info.is_healthy]

        if not healthy:
            return None

        if self.strategy == LoadBalancerStrategy.PRIMARY_FAILOVER:
            return self._get_primary_failover(healthy)
        elif self.strategy == LoadBalancerStrategy.ROUND_ROBIN:
            return self._get_round_robin(healthy)
        elif self.strategy == LoadBalancerStrategy.LEAST_LATENCY:
            return self._get_least_latency(healthy)
        elif self.strategy == LoadBalancerStrategy.WEIGHTED:
            return self._get_weighted(healthy)

        return healthy[0] if healthy else None

    def _get_primary_failover(self, healthy: List[ProviderManager]) -> Optional[ProviderManager]:
        """Get highest priority healthy provider."""
        for provider_type in self._priority_order:
            for manager in healthy:
                if manager.info.type == provider_type:
                    return manager
        return healthy[0] if healthy else None

    def _get_round_robin(self, healthy: List[ProviderManager]) -> Optional[ProviderManager]:
        """Get next provider in round robin."""
        if not healthy:
            return None
        manager = healthy[self._round_robin_index % len(healthy)]
        self._round_robin_index += 1
        return manager

    def _get_least_latency(self, healthy: List[ProviderManager]) -> Optional[ProviderManager]:
        """Get provider with lowest latency."""
        if not healthy:
            return None
        return min(healthy, key=lambda p: p.info.latency_ms)

    def _get_weighted(self, healthy: List[ProviderManager]) -> Optional[ProviderManager]:
        """Get provider based on weights."""
        import random

        if not healthy:
            return None

        total_weight = sum(self._weights.get(p.info.type, 0.5) for p in healthy)
        r = random.uniform(0, total_weight)

        current = 0
        for manager in healthy:
            current += self._weights.get(manager.info.type, 0.5)
            if r <= current:
                return manager

        return healthy[0]

    def get_all_healthy(self) -> List[ProviderManager]:
        """Get all healthy providers."""
        return [p for p in self._providers.values() if p.info.is_healthy]

    def get_status(self) -> Dict[str, Any]:
        """Get load balancer status."""
        return {
            "strategy": self.strategy.value,
            "providers": {
                p.info.name: {
                    "state": p.info.state.value,
                    "latency_ms": p.info.latency_ms,
                    "models": p.info.models,
                }
                for p in self._providers.values()
            },
        }


# =============================================================================
# BRAIN ORCHESTRATOR
# =============================================================================

class BrainOrchestrator:
    """
    Main orchestrator for LLM infrastructure.

    Features:
    - Auto-discovery of providers
    - Auto-startup of local providers
    - Continuous health monitoring
    - Auto-recovery on failures
    - Load balancing
    - Model preloading
    """

    def __init__(self):
        self.logger = logging.getLogger("Ouroboros.BrainOrchestrator")

        # Provider managers
        self._managers: Dict[ProviderType, ProviderManager] = {}

        # Load balancer
        self._load_balancer = LoadBalancer()

        # State
        self._running = False
        self._health_task: Optional[asyncio.Task] = None
        self._recovery_task: Optional[asyncio.Task] = None

        # Metrics
        self._metrics = {
            "health_checks": 0,
            "auto_restarts": 0,
            "failovers": 0,
        }

        # Initialize providers
        self._init_providers()

    def _init_providers(self) -> None:
        """Initialize provider managers."""
        # JARVIS Prime
        prime_info = ProviderInfo(
            type=ProviderType.JARVIS_PRIME,
            name="jarvis-prime",
            host=BrainConfig.PRIME_HOST,
            port=BrainConfig.PRIME_PORT,
        )
        self._managers[ProviderType.JARVIS_PRIME] = ProviderManager(prime_info)

        # Ollama
        ollama_info = ProviderInfo(
            type=ProviderType.OLLAMA,
            name="ollama",
            host=BrainConfig.OLLAMA_HOST,
            port=BrainConfig.OLLAMA_PORT,
        )
        self._managers[ProviderType.OLLAMA] = ProviderManager(ollama_info)

        # Anthropic (cloud - no process management)
        anthropic_info = ProviderInfo(
            type=ProviderType.ANTHROPIC,
            name="anthropic",
            host="api.anthropic.com",
            port=443,
            state=ProviderState.HEALTHY if os.getenv("ANTHROPIC_API_KEY") else ProviderState.STOPPED,
        )
        self._managers[ProviderType.ANTHROPIC] = ProviderManager(anthropic_info)

        # Register with load balancer
        for manager in self._managers.values():
            self._load_balancer.register_provider(manager)
            manager.register_callback(self._on_provider_state_change)

    async def initialize(self) -> bool:
        """
        Initialize the brain orchestrator.

        This is the "Defibrillator" - ensures at least one LLM is available.
        """
        self.logger.info("=" * 60)
        self.logger.info("🧠 BRAIN ORCHESTRATOR - Ignition Sequence")
        self.logger.info("=" * 60)

        # Phase 1: Discovery - Check what's already running
        self.logger.info("Phase 1: Provider Discovery")
        await self._discover_running_providers()

        # Phase 2: Startup - Start local providers if needed
        healthy_count = len(self._load_balancer.get_all_healthy())
        if healthy_count == 0:
            self.logger.info("Phase 2: Starting Local Providers")
            await self._start_local_providers()
        else:
            self.logger.info(f"Phase 2: {healthy_count} provider(s) already running")

        # Phase 3: Model Verification
        self.logger.info("Phase 3: Model Verification")
        await self._verify_models()

        # Phase 4: Start monitoring
        self.logger.info("Phase 4: Starting Health Monitor")
        self._running = True
        self._health_task = asyncio.create_task(self._health_monitor_loop())

        if BrainConfig.AUTO_RESTART_ENABLED:
            self._recovery_task = asyncio.create_task(self._recovery_loop())

        # Final status
        healthy = self._load_balancer.get_all_healthy()
        if healthy:
            self.logger.info("=" * 60)
            self.logger.info("🧠 INTELLIGENCE INFRASTRUCTURE ONLINE")
            self.logger.info("=" * 60)
            for manager in healthy:
                self.logger.info(f"  ✅ {manager.info.name}: {manager.info.endpoint}")
            return True
        else:
            self.logger.error("=" * 60)
            self.logger.error("⚠️  NO LLM PROVIDERS AVAILABLE")
            self.logger.error("=" * 60)
            return False

    async def shutdown(self) -> None:
        """Shutdown the orchestrator."""
        self.logger.info("Shutting down Brain Orchestrator...")
        self._running = False

        # Cancel tasks
        if self._health_task:
            self._health_task.cancel()
            try:
                await self._health_task
            except asyncio.CancelledError:
                pass

        if self._recovery_task:
            self._recovery_task.cancel()
            try:
                await self._recovery_task
            except asyncio.CancelledError:
                pass

        # Close managers
        for manager in self._managers.values():
            await manager.close()

        self.logger.info("Brain Orchestrator shutdown complete")

    async def _discover_running_providers(self) -> None:
        """Discover already running providers."""
        tasks = [manager.check_health() for manager in self._managers.values()]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for result in results:
            if isinstance(result, HealthCheckResult) and result.healthy:
                self.logger.info(f"  ✅ Found running: {result.provider.value}")

    async def _start_local_providers(self) -> None:
        """Start local LLM providers."""
        # Try Ollama first (simpler)
        ollama = self._managers.get(ProviderType.OLLAMA)
        if ollama and not ollama.info.is_healthy:
            self.logger.info("  Starting Ollama...")
            if await ollama.start():
                self.logger.info("  ✅ Ollama started")
            else:
                self.logger.warning("  ⚠️ Ollama startup failed")

        # Then try JARVIS Prime
        prime = self._managers.get(ProviderType.JARVIS_PRIME)
        if prime and not prime.info.is_healthy:
            self.logger.info("  Starting JARVIS Prime...")
            if await prime.start():
                self.logger.info("  ✅ JARVIS Prime started")
            else:
                self.logger.warning("  ⚠️ JARVIS Prime startup failed")

    async def _verify_models(self) -> None:
        """Verify required models are available."""
        ollama = self._managers.get(ProviderType.OLLAMA)
        if not ollama or not ollama.info.is_healthy:
            return

        available_models = ollama.info.models
        for model in BrainConfig.REQUIRED_MODELS:
            model_name = model.strip()
            if not model_name:
                continue

            if any(model_name in m for m in available_models):
                self.logger.info(f"  ✅ Model available: {model_name}")
            else:
                self.logger.warning(f"  ⚠️ Model not found: {model_name}")
                # Could add auto-pull here

    async def _health_monitor_loop(self) -> None:
        """Continuous health monitoring loop."""
        while self._running:
            try:
                for manager in self._managers.values():
                    if manager.info.is_local or manager.info.state != ProviderState.STOPPED:
                        await manager.check_health()
                        self._metrics["health_checks"] += 1

                await asyncio.sleep(BrainConfig.HEALTH_CHECK_INTERVAL)

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Health monitor error: {e}")
                await asyncio.sleep(5)

    async def _recovery_loop(self) -> None:
        """Auto-recovery loop for failed providers."""
        while self._running:
            try:
                for manager in self._managers.values():
                    if (
                        manager.info.is_local
                        and manager.info.state == ProviderState.UNHEALTHY
                        and manager.info.consecutive_failures >= 3
                    ):
                        self.logger.warning(f"Attempting recovery for {manager.info.name}")
                        if await manager.restart():
                            self._metrics["auto_restarts"] += 1
                            self.logger.info(f"Successfully recovered {manager.info.name}")

                await asyncio.sleep(30)

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Recovery loop error: {e}")
                await asyncio.sleep(10)

    def _on_provider_state_change(self, new_state: ProviderState) -> None:
        """Handle provider state changes."""
        if new_state == ProviderState.UNHEALTHY:
            # Check if we need to failover
            healthy = self._load_balancer.get_all_healthy()
            if not healthy:
                self.logger.warning("No healthy providers - entering degraded mode")
            else:
                self._metrics["failovers"] += 1

    def get_best_provider(self) -> Optional[ProviderInfo]:
        """Get the best available provider."""
        manager = self._load_balancer.get_best_provider()
        return manager.info if manager else None

    def get_status(self) -> Dict[str, Any]:
        """Get orchestrator status."""
        return {
            "running": self._running,
            "providers": {
                name: {
                    "state": manager.info.state.value,
                    "endpoint": manager.info.endpoint,
                    "latency_ms": manager.info.latency_ms,
                    "models": manager.info.models,
                    "restart_count": manager.info.restart_count,
                }
                for name, manager in [
                    (m.info.name, m) for m in self._managers.values()
                ]
            },
            "load_balancer": self._load_balancer.get_status(),
            "metrics": dict(self._metrics),
        }


# =============================================================================
# GLOBAL INSTANCE
# =============================================================================

_orchestrator: Optional[BrainOrchestrator] = None


def get_brain_orchestrator() -> BrainOrchestrator:
    """Get global brain orchestrator instance."""
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = BrainOrchestrator()
    return _orchestrator


async def ignite_brains() -> bool:
    """
    Ignite the brain infrastructure.

    This is the main entry point for ensuring LLM availability.
    Returns True if at least one provider is available.
    """
    orchestrator = get_brain_orchestrator()
    return await orchestrator.initialize()


async def shutdown_brains() -> None:
    """Shutdown brain infrastructure."""
    global _orchestrator
    if _orchestrator:
        await _orchestrator.shutdown()
        _orchestrator = None


def is_brain_orchestrator_running() -> bool:
    """
    Check if the brain orchestrator exists and is running
    WITHOUT creating a new instance.

    This is critical for probes/health checks that need to verify state
    without side effects.

    Returns:
        True if orchestrator exists and is running, False otherwise
    """
    return _orchestrator is not None and getattr(_orchestrator, '_running', False)


def get_brain_orchestrator_if_exists() -> Optional[BrainOrchestrator]:
    """
    Get the brain orchestrator instance if it exists, without creating a new one.

    Returns:
        The existing orchestrator instance or None if not initialized
    """
    return _orchestrator


# =============================================================================
# CLI
# =============================================================================

async def main():
    """CLI entry point."""
    import argparse

    parser = argparse.ArgumentParser(description="Brain Orchestrator - LLM Infrastructure Manager")
    parser.add_argument("--status", action="store_true", help="Show status and exit")
    parser.add_argument("--wait", action="store_true", help="Wait for providers to be healthy")

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.status:
        orchestrator = get_brain_orchestrator()
        # Quick health check without full init
        for manager in orchestrator._managers.values():
            await manager.check_health()
        print(orchestrator.get_status())
        return

    success = await ignite_brains()

    if success:
        if args.wait:
            print("Press Ctrl+C to exit...")
            try:
                while True:
                    await asyncio.sleep(1)
            except KeyboardInterrupt:
                pass

        await shutdown_brains()
        return 0
    else:
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
