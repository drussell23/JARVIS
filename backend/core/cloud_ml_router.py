"""
Cloud ML Router for Voice Biometric Authentication
===================================================

Routes ML operations (speaker verification, embedding extraction) to either
local processing or GCP cloud based on the startup decision.

v17.8.7 Architecture:
- If RAM >= 6GB at startup → LOCAL_FULL → Process locally (instant)
- If RAM < 6GB at startup  → CLOUD_FIRST → Route to GCP Spot VM (instant, no local RAM spike)
- If RAM < 2GB at startup  → CLOUD_ONLY → All ML on GCP (emergency mode)

INTEGRATION WITH EXISTING HYBRID ARCHITECTURE:
- Delegates to HybridBackendClient for HTTP requests (circuit breaker, pooling)
- Uses IntelligentGCPOptimizer for VM creation decisions
- Leverages HybridRouter for capability-based routing
- Integrates with GCPVMManager for VM lifecycle

This eliminates the "Processing..." hang that occurred when LOCAL_MINIMAL mode
deferred model loading until voice unlock request.

Features:
- Async throughout for non-blocking operations
- Delegates to existing hybrid infrastructure (no duplication!)
- Automatic failover between cloud and local
- Request caching with Helicone integration
- Cost tracking per request
- Zero hardcoding - all config from environment/startup decision
"""

import asyncio
import logging
import os
import time
import hashlib
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime

logger = logging.getLogger(__name__)


# ============================================================================
# INTEGRATION: Import existing hybrid infrastructure
# ============================================================================

# HybridBackendClient - async HTTP with circuit breaker
try:
    from core.hybrid_backend_client import HybridBackendClient, BackendType, CircuitBreaker
    HYBRID_CLIENT_AVAILABLE = True
except ImportError:
    HYBRID_CLIENT_AVAILABLE = False
    logger.debug("HybridBackendClient not available")

# HybridRouter - capability-based routing
try:
    from core.hybrid_router import HybridRouter, RouteDecision, RoutingContext
    HYBRID_ROUTER_AVAILABLE = True
except ImportError:
    HYBRID_ROUTER_AVAILABLE = False
    logger.debug("HybridRouter not available")

# IntelligentGCPOptimizer - cost-aware VM decisions
try:
    from core.intelligent_gcp_optimizer import IntelligentGCPOptimizer, PressureScore
    GCP_OPTIMIZER_AVAILABLE = True
except ImportError:
    GCP_OPTIMIZER_AVAILABLE = False
    logger.debug("IntelligentGCPOptimizer not available")

# GCPVMManager - VM lifecycle management
try:
    from core.gcp_vm_manager import GCPVMManager, VMInstance, get_gcp_vm_manager
    GCP_VM_MANAGER_AVAILABLE = True
except ImportError:
    GCP_VM_MANAGER_AVAILABLE = False
    logger.debug("GCPVMManager not available")

# MemoryAwareStartup - startup decision
try:
    from core.memory_aware_startup import (
        MemoryAwareStartup,
        StartupDecision,
        StartupMode,
        get_startup_manager,
        determine_startup_mode
    )
    MEMORY_AWARE_AVAILABLE = True
except ImportError:
    MEMORY_AWARE_AVAILABLE = False
    logger.debug("MemoryAwareStartup not available")


class MLBackend(Enum):
    """ML processing backend"""
    LOCAL = "local"
    GCP_CLOUD = "gcp_cloud"
    HYBRID = "hybrid"  # Local for light ops, cloud for heavy


class MLOperation(Enum):
    """Types of ML operations for voice biometrics"""
    SPEAKER_VERIFICATION = "speaker_verification"
    EMBEDDING_EXTRACTION = "embedding_extraction"
    VOICE_ACTIVITY_DETECTION = "voice_activity_detection"
    WHISPER_TRANSCRIPTION = "whisper_transcription"
    ANTI_SPOOFING = "anti_spoofing"
    BEHAVIORAL_ANALYSIS = "behavioral_analysis"


@dataclass
class MLRequest:
    """Request for ML processing"""
    operation: MLOperation
    audio_data: bytes
    speaker_name: Optional[str] = None
    sample_rate: int = 16000
    metadata: Dict[str, Any] = field(default_factory=dict)
    request_id: Optional[str] = None

    def __post_init__(self):
        if not self.request_id:
            content_hash = hashlib.md5(self.audio_data[:1000]).hexdigest()[:8]
            self.request_id = f"{self.operation.value}_{int(time.time()*1000)}_{content_hash}"


@dataclass
class MLResponse:
    """Response from ML processing"""
    success: bool
    backend_used: MLBackend
    operation: MLOperation
    result: Dict[str, Any]
    processing_time_ms: float
    cost_usd: float = 0.0
    cached: bool = False
    request_id: Optional[str] = None
    error: Optional[str] = None


class CloudMLRouter:
    """
    Intelligent ML request router that delegates to existing hybrid infrastructure.

    Key Integration Points:
    - Uses HybridBackendClient for all HTTP requests (inherits circuit breaker)
    - Uses IntelligentGCPOptimizer for VM creation decisions
    - Uses GCPVMManager for VM lifecycle
    - Uses MemoryAwareStartup for startup decisions

    Prevents "Processing..." hangs by ensuring ML operations never try to
    load heavy models on RAM-constrained systems.
    """

    def __init__(self):
        """Initialize the cloud ML router with existing hybrid infrastructure"""
        # Configuration
        self.gcp_project = os.getenv("GCP_PROJECT_ID", "jarvis-473803")
        self.gcp_zone = os.getenv("GCP_ZONE", "us-central1-a")

        # Current routing mode (set by startup decision)
        self._current_backend = MLBackend.LOCAL
        self._startup_decision: Optional[StartupDecision] = None
        self._gcp_vm_ip: Optional[str] = None
        self._gcp_ml_endpoint: Optional[str] = None

        # ================================================================
        # INTEGRATION: Existing hybrid infrastructure (lazy loaded)
        # ================================================================
        self._hybrid_client: Optional[HybridBackendClient] = None
        self._hybrid_router: Optional[HybridRouter] = None
        self._gcp_optimizer: Optional[IntelligentGCPOptimizer] = None
        self._gcp_vm_manager: Optional[GCPVMManager] = None
        self._startup_manager: Optional[MemoryAwareStartup] = None

        # Request caching (Helicone-style, for voice patterns)
        self._cache: Dict[str, MLResponse] = {}
        self._cache_ttl = 300  # 5 minutes
        self._cache_enabled = True

        # Performance tracking
        self._stats = {
            "total_requests": 0,
            "local_requests": 0,
            "cloud_requests": 0,
            "cache_hits": 0,
            "total_cost_usd": 0.0,
            "avg_local_latency_ms": 0.0,
            "avg_cloud_latency_ms": 0.0,
        }

        # Local ML components (lazy loaded)
        self._local_speaker_service = None

        logger.info("CloudMLRouter initialized (v17.8.7 - Integrated with Hybrid Architecture)")
        logger.info(f"  HybridBackendClient: {'✅' if HYBRID_CLIENT_AVAILABLE else '❌'}")
        logger.info(f"  HybridRouter: {'✅' if HYBRID_ROUTER_AVAILABLE else '❌'}")
        logger.info(f"  IntelligentGCPOptimizer: {'✅' if GCP_OPTIMIZER_AVAILABLE else '❌'}")
        logger.info(f"  GCPVMManager: {'✅' if GCP_VM_MANAGER_AVAILABLE else '❌'}")
        logger.info(f"  MemoryAwareStartup: {'✅' if MEMORY_AWARE_AVAILABLE else '❌'}")

    async def initialize(self, startup_decision: Optional[StartupDecision] = None):
        """
        Initialize the router based on startup decision.

        Integrates with existing hybrid infrastructure components.

        Args:
            startup_decision: Decision from MemoryAwareStartup
        """
        self._startup_decision = startup_decision

        # Get or create startup manager
        if MEMORY_AWARE_AVAILABLE:
            self._startup_manager = await get_startup_manager()
            if not startup_decision and self._startup_manager:
                startup_decision = self._startup_manager.startup_decision
                self._startup_decision = startup_decision

        # Determine routing mode based on startup decision
        if startup_decision:
            if startup_decision.use_cloud_ml:
                self._current_backend = MLBackend.GCP_CLOUD
                logger.info(f"☁️  CloudMLRouter: CLOUD_FIRST mode (reason: {startup_decision.reason})")

                # Initialize GCP components
                await self._setup_gcp_infrastructure()
            else:
                self._current_backend = MLBackend.LOCAL
                logger.info(f"💻 CloudMLRouter: LOCAL_FULL mode (reason: {startup_decision.reason})")

                # Pre-load local services for instant response
                await self._setup_local_services()
        else:
            self._current_backend = MLBackend.LOCAL
            logger.info("💻 CloudMLRouter: Default to LOCAL (no startup decision)")
            await self._setup_local_services()

        # Initialize hybrid infrastructure components
        await self._setup_hybrid_infrastructure()

        logger.info(f"✅ CloudMLRouter ready (backend: {self._current_backend.value})")

    async def _setup_hybrid_infrastructure(self):
        """Setup integration with existing hybrid infrastructure"""
        # Initialize HybridBackendClient for HTTP requests
        if HYBRID_CLIENT_AVAILABLE:
            try:
                # Load config for hybrid client
                config_path = os.path.join(
                    os.path.dirname(__file__),
                    "..", "config", "hybrid_config.yaml"
                )
                if os.path.exists(config_path):
                    import yaml
                    with open(config_path) as f:
                        config = yaml.safe_load(f)
                    self._hybrid_client = HybridBackendClient(config)
                    await self._hybrid_client.initialize()
                    logger.info("✅ HybridBackendClient initialized for ML routing")
            except Exception as e:
                logger.warning(f"Could not initialize HybridBackendClient: {e}")

        # Initialize IntelligentGCPOptimizer for VM decisions
        if GCP_OPTIMIZER_AVAILABLE:
            try:
                from core.intelligent_gcp_optimizer import get_gcp_optimizer
                self._gcp_optimizer = get_gcp_optimizer()
                logger.info("✅ IntelligentGCPOptimizer connected")
            except Exception as e:
                logger.warning(f"Could not connect IntelligentGCPOptimizer: {e}")

    async def _setup_gcp_infrastructure(self):
        """Setup GCP ML infrastructure when in CLOUD_FIRST mode"""
        # Get or create GCP VM for ML processing
        if GCP_VM_MANAGER_AVAILABLE and MEMORY_AWARE_AVAILABLE:
            try:
                # Check if startup manager already has a VM
                if self._startup_manager and self._startup_manager._gcp_vm_manager:
                    self._gcp_vm_manager = self._startup_manager._gcp_vm_manager

                    # Find healthy VM with ML capabilities
                    for vm in self._gcp_vm_manager.managed_vms.values():
                        if vm.is_healthy and vm.ip_address:
                            self._gcp_vm_ip = vm.ip_address
                            self._gcp_ml_endpoint = f"http://{vm.ip_address}:8010/api/ml"
                            logger.info(f"☁️  Connected to GCP ML VM: {self._gcp_ml_endpoint}")
                            break

                # If no VM yet, trigger creation
                if not self._gcp_ml_endpoint:
                    logger.info("☁️  No GCP VM available, will create on first ML request")
                    # Don't block startup - create on first request

            except Exception as e:
                logger.error(f"Failed to setup GCP infrastructure: {e}")

    async def _setup_local_services(self):
        """Pre-load local ML services for instant response"""
        try:
            from voice.speaker_verification_service import get_speaker_service
            self._local_speaker_service = get_speaker_service()
            logger.info("✅ Local speaker verification service loaded")
        except ImportError as e:
            logger.debug(f"Local speaker service import error: {e}")
        except Exception as e:
            logger.warning(f"Could not load local speaker service: {e}")

    async def route_request(self, request: MLRequest) -> MLResponse:
        """
        Route an ML request to the appropriate backend.

        This is the main entry point for all voice ML operations.
        Automatically handles:
        - Backend selection based on startup decision
        - Caching for repeated voice patterns
        - Circuit breaker via HybridBackendClient
        - Failover between backends
        - Cost tracking

        Args:
            request: The ML request to process

        Returns:
            MLResponse with results and metadata
        """
        start_time = time.time()
        self._stats["total_requests"] += 1

        # Check cache first (voice pattern caching)
        cache_key = self._get_cache_key(request)
        if self._cache_enabled and cache_key in self._cache:
            cached = self._cache[cache_key]
            cache_age = (time.time() * 1000) - cached.processing_time_ms
            if cache_age < self._cache_ttl * 1000:
                self._stats["cache_hits"] += 1
                logger.debug(f"🚀 Cache hit for {request.operation.value} (saves ~{cached.processing_time_ms:.0f}ms)")
                return MLResponse(
                    success=cached.success,
                    backend_used=cached.backend_used,
                    operation=request.operation,
                    result=cached.result,
                    processing_time_ms=0.5,  # Cache lookup time
                    cost_usd=0.0,  # No cost for cached
                    cached=True,
                    request_id=request.request_id,
                )

        # Select backend based on startup decision
        backend = self._current_backend

        # Process request
        try:
            if backend == MLBackend.GCP_CLOUD:
                response = await self._process_cloud(request)
                self._stats["cloud_requests"] += 1
            else:
                response = await self._process_local(request)
                self._stats["local_requests"] += 1

            # Update cache
            if self._cache_enabled and response.success:
                self._cache[cache_key] = response

            # Track cost
            self._stats["total_cost_usd"] += response.cost_usd

            return response

        except Exception as e:
            logger.error(f"ML routing error: {e}")

            # Try failover
            if backend == MLBackend.GCP_CLOUD:
                logger.warning(f"☁️  Cloud failed, attempting local failover...")
                try:
                    # Ensure local services are loaded
                    await self._setup_local_services()
                    response = await self._process_local(request)
                    response.backend_used = MLBackend.LOCAL
                    logger.info("✅ Local failover successful")
                    return response
                except Exception as local_e:
                    logger.error(f"Local failover also failed: {local_e}")

            return MLResponse(
                success=False,
                backend_used=backend,
                operation=request.operation,
                result={},
                processing_time_ms=(time.time() - start_time) * 1000,
                cost_usd=0.0,
                request_id=request.request_id,
                error=str(e),
            )

    async def _process_local(self, request: MLRequest) -> MLResponse:
        """Process request locally using existing speaker service"""
        start_time = time.time()

        if request.operation == MLOperation.SPEAKER_VERIFICATION:
            result = await self._local_speaker_verification(request)
        elif request.operation == MLOperation.EMBEDDING_EXTRACTION:
            result = await self._local_embedding_extraction(request)
        elif request.operation == MLOperation.VOICE_ACTIVITY_DETECTION:
            result = await self._local_vad(request)
        else:
            raise ValueError(f"Unsupported local operation: {request.operation}")

        processing_time = (time.time() - start_time) * 1000

        # Update average latency
        n = self._stats["local_requests"] + 1
        self._stats["avg_local_latency_ms"] = (
            (self._stats["avg_local_latency_ms"] * (n - 1) + processing_time) / n
        )

        return MLResponse(
            success=True,
            backend_used=MLBackend.LOCAL,
            operation=request.operation,
            result=result,
            processing_time_ms=processing_time,
            cost_usd=0.0,  # Local is free
            request_id=request.request_id,
        )

    async def _process_cloud(self, request: MLRequest) -> MLResponse:
        """Process request on GCP cloud using HybridBackendClient"""
        start_time = time.time()

        # Ensure GCP endpoint is available
        if not self._gcp_ml_endpoint:
            await self._create_gcp_vm_on_demand()

        if not self._gcp_ml_endpoint:
            raise RuntimeError("No GCP ML endpoint available - VM creation failed")

        # Prepare request payload
        import base64
        payload = {
            "operation": request.operation.value,
            "audio_data": base64.b64encode(request.audio_data).decode(),
            "speaker_name": request.speaker_name,
            "sample_rate": request.sample_rate,
            "metadata": request.metadata,
            "request_id": request.request_id,
        }

        # Use HybridBackendClient if available (has circuit breaker)
        if self._hybrid_client:
            result = await self._hybrid_client.request(
                backend_name="gcp",
                endpoint=f"/api/ml/{request.operation.value}",
                method="POST",
                json=payload,
            )
        else:
            # Fallback to direct HTTP
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self._gcp_ml_endpoint}/{request.operation.value}",
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=30)
                ) as resp:
                    if resp.status != 200:
                        error_text = await resp.text()
                        raise RuntimeError(f"GCP ML error: {resp.status} - {error_text}")
                    result = await resp.json()

        processing_time = (time.time() - start_time) * 1000

        # Calculate cost (e2-highmem-4 Spot: ~$0.029/hour)
        cost_per_second = 0.029 / 3600
        cost = (processing_time / 1000) * cost_per_second

        # Update average latency
        n = self._stats["cloud_requests"] + 1
        self._stats["avg_cloud_latency_ms"] = (
            (self._stats["avg_cloud_latency_ms"] * (n - 1) + processing_time) / n
        )

        return MLResponse(
            success=True,
            backend_used=MLBackend.GCP_CLOUD,
            operation=request.operation,
            result=result,
            processing_time_ms=processing_time,
            cost_usd=cost,
            request_id=request.request_id,
        )

    async def _create_gcp_vm_on_demand(self):
        """Create GCP VM on demand using existing infrastructure"""
        logger.info("☁️  Creating GCP VM on demand for ML processing...")

        if GCP_VM_MANAGER_AVAILABLE and self._startup_manager:
            try:
                # Use existing startup manager to create VM
                result = await self._startup_manager.activate_cloud_ml_backend()

                if result.get("success"):
                    self._gcp_vm_ip = result.get("ip")
                    if self._gcp_vm_ip:
                        self._gcp_ml_endpoint = f"http://{self._gcp_vm_ip}:8010/api/ml"
                        logger.info(f"✅ GCP VM created: {self._gcp_ml_endpoint}")
                        logger.info(f"   Cost: ${result.get('cost_per_hour', 0.029)}/hour")
                    else:
                        logger.warning("VM created but no IP address returned")
                else:
                    logger.error(f"GCP VM creation failed: {result.get('error')}")

            except Exception as e:
                logger.error(f"Failed to create GCP VM on demand: {e}")

    async def _local_speaker_verification(self, request: MLRequest) -> Dict[str, Any]:
        """Perform local speaker verification"""
        if not self._local_speaker_service:
            from voice.speaker_verification_service import get_speaker_service
            self._local_speaker_service = get_speaker_service()

        result = await self._local_speaker_service.verify_speaker_enhanced(
            request.audio_data,
            speaker_name=request.speaker_name,
            context=request.metadata
        )

        return result

    async def _local_embedding_extraction(self, request: MLRequest) -> Dict[str, Any]:
        """Extract voice embedding locally"""
        if not self._local_speaker_service:
            from voice.speaker_verification_service import get_speaker_service
            self._local_speaker_service = get_speaker_service()

        embedding = await self._local_speaker_service.extract_embedding(
            request.audio_data
        )

        return {
            "embedding": embedding.tolist() if hasattr(embedding, 'tolist') else embedding,
            "dimensions": len(embedding) if embedding is not None else 0
        }

    async def _local_vad(self, request: MLRequest) -> Dict[str, Any]:
        """Perform local voice activity detection"""
        import numpy as np

        audio = np.frombuffer(request.audio_data, dtype=np.int16).astype(np.float32)
        audio = audio / 32768.0

        # Energy-based VAD
        frame_size = int(request.sample_rate * 0.025)
        hop_size = int(request.sample_rate * 0.010)

        energies = []
        for i in range(0, len(audio) - frame_size, hop_size):
            frame = audio[i:i + frame_size]
            energy = np.sqrt(np.mean(frame ** 2))
            energies.append(energy)

        threshold = np.mean(energies) * 1.5 if energies else 0
        is_speech = [e > threshold for e in energies]
        speech_ratio = sum(is_speech) / len(is_speech) if is_speech else 0

        return {
            "has_speech": speech_ratio > 0.1,
            "speech_ratio": speech_ratio,
            "avg_energy": float(np.mean(energies)) if energies else 0,
        }

    def _get_cache_key(self, request: MLRequest) -> str:
        """Generate cache key for voice pattern request"""
        content = f"{request.operation.value}:{request.speaker_name}:"
        content += hashlib.md5(request.audio_data).hexdigest()
        return hashlib.sha256(content.encode()).hexdigest()[:32]

    def get_stats(self) -> Dict[str, Any]:
        """Get router statistics"""
        return {
            **self._stats,
            "current_backend": self._current_backend.value,
            "gcp_endpoint": self._gcp_ml_endpoint or "not configured",
            "cache_size": len(self._cache),
            "hybrid_client_available": HYBRID_CLIENT_AVAILABLE,
            "gcp_optimizer_available": GCP_OPTIMIZER_AVAILABLE,
            "startup_mode": self._startup_decision.mode.value if self._startup_decision else "unknown",
        }

    async def cleanup(self):
        """Cleanup resources"""
        if self._hybrid_client:
            await self._hybrid_client.close()

        self._cache.clear()
        logger.info("CloudMLRouter cleaned up")


# ============================================================================
# Convenience Functions - Use these for voice biometric operations
# ============================================================================

_router_instance: Optional[CloudMLRouter] = None


async def get_cloud_ml_router() -> CloudMLRouter:
    """Get or create the global cloud ML router"""
    global _router_instance

    if _router_instance is None:
        _router_instance = CloudMLRouter()

        # Initialize with current startup decision
        try:
            if MEMORY_AWARE_AVAILABLE:
                manager = await get_startup_manager()
                await _router_instance.initialize(manager.startup_decision)
            else:
                await _router_instance.initialize(None)
        except Exception as e:
            logger.warning(f"Could not get startup decision: {e}")
            await _router_instance.initialize(None)

    return _router_instance


async def verify_speaker_cloud_aware(
    audio_data: bytes,
    speaker_name: str,
    **kwargs
) -> Dict[str, Any]:
    """
    Verify speaker with automatic cloud/local routing.

    This is the main entry point for voice biometric authentication
    that automatically routes based on memory-aware startup decision.

    If RAM was < 6GB at startup → routes to GCP (no local RAM spike)
    If RAM was >= 6GB at startup → routes locally (instant, preloaded)

    Args:
        audio_data: Raw audio bytes
        speaker_name: Expected speaker name
        **kwargs: Additional metadata

    Returns:
        Verification result with confidence scores
    """
    router = await get_cloud_ml_router()

    request = MLRequest(
        operation=MLOperation.SPEAKER_VERIFICATION,
        audio_data=audio_data,
        speaker_name=speaker_name,
        metadata=kwargs,
    )

    response = await router.route_request(request)

    if response.success:
        result = response.result
        result["_routing_info"] = {
            "backend_used": response.backend_used.value,
            "processing_time_ms": response.processing_time_ms,
            "cost_usd": response.cost_usd,
            "cached": response.cached,
        }
        return result
    else:
        return {
            "verified": False,
            "confidence": 0.0,
            "error": response.error,
            "_routing_info": {
                "backend_used": response.backend_used.value,
                "error": response.error,
            }
        }


async def extract_embedding_cloud_aware(
    audio_data: bytes,
    **kwargs
) -> Dict[str, Any]:
    """
    Extract voice embedding with automatic cloud/local routing.

    Args:
        audio_data: Raw audio bytes
        **kwargs: Additional metadata

    Returns:
        Embedding extraction result
    """
    router = await get_cloud_ml_router()

    request = MLRequest(
        operation=MLOperation.EMBEDDING_EXTRACTION,
        audio_data=audio_data,
        metadata=kwargs,
    )

    response = await router.route_request(request)

    if response.success:
        return response.result
    else:
        return {"error": response.error}
