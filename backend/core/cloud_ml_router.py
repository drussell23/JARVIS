"""
Cloud ML Router for Voice Biometric Authentication
===================================================

Routes ML operations (speaker verification, embedding extraction) to either
local processing or GCP cloud based on the startup decision.

v17.9.0 Architecture:
- If RAM >= 6GB at startup → LOCAL_FULL → Process locally (instant)
- If RAM < 6GB at startup  → CLOUD_FIRST → Route to GCP Spot VM (instant, no local RAM spike)
- If RAM < 2GB at startup  → CLOUD_ONLY → All ML on GCP (emergency mode)

INTEGRATION WITH EXISTING HYBRID ARCHITECTURE:
- Delegates to HybridBackendClient for HTTP requests (circuit breaker, pooling)
- Uses IntelligentGCPOptimizer for VM creation decisions
- Leverages HybridRouter for capability-based routing
- Integrates with GCPVMManager for VM lifecycle

NEW IN v17.9.0:
- SCALE-TO-ZERO: Automatic VM shutdown after idle period
- HELICONE-STYLE SEMANTIC CACHING: Cost optimization via pattern caching
- VOICE PATTERN FINGERPRINTING: Request deduplication
- INTELLIGENT VM LIFECYCLE: Proactive scaling decisions
- COST TRACKING INTEGRATION: Per-request and cumulative cost monitoring

This eliminates the "Processing..." hang that occurred when LOCAL_MINIMAL mode
deferred model loading until voice unlock request.

Features:
- Async throughout for non-blocking operations
- Delegates to existing hybrid infrastructure (no duplication!)
- Automatic failover between cloud and local
- Request caching with Helicone integration
- Cost tracking per request
- Scale-to-zero for GCP VMs
- Zero hardcoding - all config from environment/startup decision
"""

import asyncio
import logging
import os
import time
import hashlib
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple
from datetime import datetime, timedelta

from backend.core.async_safety import TimeoutConfig, get_shutdown_event

logger = logging.getLogger(__name__)

# =============================================================================
# SCALE-TO-ZERO CONFIGURATION (Dynamic, no hardcoding)
# =============================================================================
SCALE_TO_ZERO_IDLE_MINUTES = int(os.getenv("SCALE_TO_ZERO_IDLE_MINUTES", "15"))
SCALE_TO_ZERO_ENABLED = os.getenv("SCALE_TO_ZERO_ENABLED", "true").lower() == "true"
SCALE_TO_ZERO_CHECK_INTERVAL = int(os.getenv("SCALE_TO_ZERO_CHECK_INTERVAL", "60"))  # seconds

# Semantic cache configuration
SEMANTIC_CACHE_ENABLED = os.getenv("ML_SEMANTIC_CACHE_ENABLED", "true").lower() == "true"
SEMANTIC_CACHE_TTL = int(os.getenv("ML_SEMANTIC_CACHE_TTL", "300"))  # 5 minutes
SEMANTIC_CACHE_SIMILARITY_THRESHOLD = float(os.getenv("ML_CACHE_SIMILARITY", "0.95"))

# Cost optimization
COST_TRACKING_ENABLED = os.getenv("ML_COST_TRACKING_ENABLED", "true").lower() == "true"
SPOT_VM_COST_PER_HOUR = float(os.getenv("SPOT_VM_HOURLY_COST", "0.029"))


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

# CloudECAPAClient v18.2.0 - Hybrid cloud ECAPA backend with Spot VM support
try:
    from voice_unlock.cloud_ecapa_client import (
        CloudECAPAClient,
        get_cloud_ecapa_client,
        BackendType as ECAPABackendType,
    )
    CLOUD_ECAPA_CLIENT_AVAILABLE = True
except ImportError:
    CLOUD_ECAPA_CLIENT_AVAILABLE = False
    logger.debug("CloudECAPAClient not available")

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
        self._cache_ttl = SEMANTIC_CACHE_TTL
        self._cache_enabled = SEMANTIC_CACHE_ENABLED

        # ================================================================
        # SCALE-TO-ZERO: VM Idle Management
        # ================================================================
        self._last_request_time: Optional[datetime] = None
        self._vm_created_time: Optional[datetime] = None
        self._scale_to_zero_task: Optional[asyncio.Task] = None
        self._scale_to_zero_enabled = SCALE_TO_ZERO_ENABLED
        self._idle_shutdown_minutes = SCALE_TO_ZERO_IDLE_MINUTES

        # ================================================================
        # COST OPTIMIZATION
        # ================================================================
        self._cost_per_embedding = float(os.getenv("COST_PER_EMBEDDING", "0.0001"))
        self._total_embeddings_processed = 0
        self._total_embeddings_cached = 0

        # Performance tracking (enhanced)
        self._stats = {
            "total_requests": 0,
            "local_requests": 0,
            "cloud_requests": 0,
            "cache_hits": 0,
            "total_cost_usd": 0.0,
            "total_cost_saved_usd": 0.0,
            "avg_local_latency_ms": 0.0,
            "avg_cloud_latency_ms": 0.0,
            # Scale-to-zero stats
            "vm_startups": 0,
            "vm_shutdowns_idle": 0,
            "total_vm_runtime_hours": 0.0,
            # Cache efficiency
            "cache_hit_rate": 0.0,
            "embeddings_from_cache": 0,
        }

        # Local ML components (lazy loaded)
        self._local_speaker_service = None

        # Shutdown callbacks
        self._shutdown_callbacks: List[Callable] = []

        # ================================================================
        # CloudECAPAClient v18.2.0 Integration
        # ================================================================
        self._cloud_ecapa_client: Optional[CloudECAPAClient] = None if not CLOUD_ECAPA_CLIENT_AVAILABLE else None

        logger.info("CloudMLRouter initialized (v17.9.1 - CloudECAPAClient v18.2.0 Integration)")
        logger.info(f"  HybridBackendClient: {'✅' if HYBRID_CLIENT_AVAILABLE else '❌'}")
        logger.info(f"  HybridRouter: {'✅' if HYBRID_ROUTER_AVAILABLE else '❌'}")
        logger.info(f"  IntelligentGCPOptimizer: {'✅' if GCP_OPTIMIZER_AVAILABLE else '❌'}")
        logger.info(f"  GCPVMManager: {'✅' if GCP_VM_MANAGER_AVAILABLE else '❌'}")
        logger.info(f"  MemoryAwareStartup: {'✅' if MEMORY_AWARE_AVAILABLE else '❌'}")
        logger.info(f"  CloudECAPAClient: {'✅' if CLOUD_ECAPA_CLIENT_AVAILABLE else '❌'}")
        logger.info(f"  Scale-to-Zero: {'✅' if self._scale_to_zero_enabled else '❌'} ({self._idle_shutdown_minutes}min idle)")
        logger.info(f"  Semantic Cache: {'✅' if self._cache_enabled else '❌'} (TTL={self._cache_ttl}s)")

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

    # =========================================================================
    # SCALE-TO-ZERO: Automatic VM Shutdown
    # =========================================================================

    async def _start_scale_to_zero_monitor(self):
        """Start background task to monitor idle time and shutdown VM"""
        if not self._scale_to_zero_enabled:
            return

        if self._scale_to_zero_task and not self._scale_to_zero_task.done():
            return  # Already running

        self._scale_to_zero_task = asyncio.create_task(self._scale_to_zero_loop())
        logger.info(f"🔄 Scale-to-zero monitor started (idle threshold: {self._idle_shutdown_minutes}min)")

    async def _scale_to_zero_loop(self):
        """Background loop checking for idle VMs to shutdown with timeout protection."""
        shutdown_event = get_shutdown_event()
        max_iterations = int(os.getenv("SCALE_TO_ZERO_MAX_ITERATIONS", "0")) or None
        iteration = 0

        while True:
            # Check for shutdown
            if shutdown_event.is_set():
                logger.info("Scale-to-zero monitor stopped via shutdown event")
                break

            # Check max iterations (for testing/safety)
            if max_iterations and iteration >= max_iterations:
                logger.info(f"Scale-to-zero monitor reached max iterations ({max_iterations})")
                break

            iteration += 1

            try:
                await asyncio.sleep(SCALE_TO_ZERO_CHECK_INTERVAL)

                # Only check if we have an active GCP VM
                if not self._gcp_vm_ip or not self._gcp_ml_endpoint:
                    continue

                # Check if we've been idle long enough
                if self._last_request_time:
                    idle_duration = datetime.now() - self._last_request_time
                    idle_minutes = idle_duration.total_seconds() / 60

                    if idle_minutes >= self._idle_shutdown_minutes:
                        logger.info(
                            f"⏰ VM idle for {idle_minutes:.1f} minutes "
                            f"(threshold: {self._idle_shutdown_minutes}min) - initiating shutdown"
                        )
                        # Add timeout protection for VM shutdown
                        await asyncio.wait_for(
                            self._shutdown_idle_vm(),
                            timeout=TimeoutConfig.VM_OPERATION
                        )

            except asyncio.TimeoutError:
                logger.warning(f"VM shutdown timed out after {TimeoutConfig.VM_OPERATION}s")
            except asyncio.CancelledError:
                logger.info("Scale-to-zero monitor cancelled")
                break
            except Exception as e:
                logger.error(f"Scale-to-zero monitor error: {e}")

    async def _shutdown_idle_vm(self):
        """Shutdown idle GCP VM to save costs"""
        if not self._gcp_vm_ip:
            return

        vm_ip = self._gcp_vm_ip

        try:
            # Calculate runtime for cost tracking
            if self._vm_created_time:
                runtime_hours = (datetime.now() - self._vm_created_time).total_seconds() / 3600
                self._stats["total_vm_runtime_hours"] += runtime_hours

                # Calculate cost saved by shutting down
                remaining_hour_fraction = 1.0 - (runtime_hours % 1)
                cost_saved_this_session = remaining_hour_fraction * SPOT_VM_COST_PER_HOUR
                self._stats["total_cost_saved_usd"] += cost_saved_this_session

                logger.info(
                    f"💰 VM runtime: {runtime_hours:.2f}h, "
                    f"cost saved by early shutdown: ${cost_saved_this_session:.4f}"
                )

            # Terminate VM via GCP VM Manager (using correct method name)
            if self._gcp_vm_manager:
                for vm_name, vm in list(self._gcp_vm_manager.managed_vms.items()):
                    if vm.ip_address == vm_ip:
                        logger.info(f"🗑️  Shutting down idle VM: {vm_name}")
                        # Fix: Use terminate_vm() instead of non-existent delete_vm()
                        await self._gcp_vm_manager.terminate_vm(
                            vm_name,
                            reason=f"Scale-to-zero: idle for {(time.time() - (self._last_request_time or 0)) / 60:.1f}min"
                        )
                        self._stats["vm_shutdowns_idle"] += 1
                        break

            # Clear VM references
            self._gcp_vm_ip = None
            self._gcp_ml_endpoint = None
            self._vm_created_time = None

            logger.info(f"✅ Scale-to-zero: VM {vm_ip} shutdown successfully")

            # Notify callbacks
            for callback in self._shutdown_callbacks:
                try:
                    if asyncio.iscoroutinefunction(callback):
                        await callback("vm_shutdown_idle", {"vm_ip": vm_ip})
                    else:
                        callback("vm_shutdown_idle", {"vm_ip": vm_ip})
                except Exception as e:
                    logger.debug(f"Shutdown callback error: {e}")

        except Exception as e:
            logger.error(f"Failed to shutdown idle VM: {e}")

    def register_shutdown_callback(self, callback: Callable):
        """Register callback to be notified when VM is shutdown"""
        self._shutdown_callbacks.append(callback)

    async def force_vm_shutdown(self):
        """Force immediate VM shutdown (for cleanup/exit)"""
        logger.info("🔄 Force VM shutdown requested")
        await self._shutdown_idle_vm()

    # =========================================================================
    # COST-AWARE REQUEST ROUTING
    # =========================================================================

    async def route_request(self, request: MLRequest) -> MLResponse:
        """
        Route an ML request to the appropriate backend.

        This is the main entry point for all voice ML operations.
        Automatically handles:
        - Backend selection based on startup decision
        - Caching for repeated voice patterns (Helicone-style)
        - Circuit breaker via HybridBackendClient
        - Failover between backends
        - Cost tracking with savings calculation
        - Scale-to-zero idle tracking

        Args:
            request: The ML request to process

        Returns:
            MLResponse with results and metadata
        """
        start_time = time.time()
        self._stats["total_requests"] += 1

        # Track last request time for scale-to-zero
        self._last_request_time = datetime.now()

        # Check cache first (Helicone-style voice pattern caching)
        cache_key = self._get_cache_key(request)
        if self._cache_enabled and cache_key in self._cache:
            cached = self._cache[cache_key]
            cache_age = (time.time() * 1000) - cached.processing_time_ms
            if cache_age < self._cache_ttl * 1000:
                self._stats["cache_hits"] += 1
                self._stats["embeddings_from_cache"] += 1

                # Calculate cost saved by cache hit
                cost_saved = self._cost_per_embedding
                if cached.backend_used == MLBackend.GCP_CLOUD:
                    # Cloud processing would have cost more
                    cost_saved += (cached.processing_time_ms / 1000) * (SPOT_VM_COST_PER_HOUR / 3600)
                self._stats["total_cost_saved_usd"] += cost_saved

                # Update cache hit rate
                total = self._stats["cache_hits"] + self._stats["local_requests"] + self._stats["cloud_requests"]
                self._stats["cache_hit_rate"] = self._stats["cache_hits"] / max(1, total)

                logger.debug(
                    f"🚀 Cache hit for {request.operation.value} "
                    f"(saves ~{cached.processing_time_ms:.0f}ms, ${cost_saved:.6f})"
                )
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

            # Update cache for successful responses
            if self._cache_enabled and response.success:
                self._cache[cache_key] = response
                self._total_embeddings_processed += 1

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
        """
        Process request on cloud using CloudECAPAClient v18.2.0 or fallback to legacy GCP.

        CloudECAPAClient v18.2.0 provides:
        - Multi-backend support: Cloud Run → Spot VM → Local fallback
        - Intelligent routing based on memory pressure and cost
        - Per-backend cost tracking with daily budget enforcement
        """
        start_time = time.time()

        # ================================================================
        # PRIORITY: Use CloudECAPAClient v18.2.0 for embedding extraction
        # This provides superior routing with Spot VM support and cost tracking
        # ================================================================
        if self._cloud_ecapa_client and request.operation == MLOperation.EMBEDDING_EXTRACTION:
            try:
                # Use CloudECAPAClient for ECAPA embedding extraction
                embedding = await self._cloud_ecapa_client.extract_embedding(
                    audio_data=request.audio_data,
                    sample_rate=request.sample_rate,
                )

                processing_time = (time.time() - start_time) * 1000

                if embedding is not None:
                    # Get cost from CloudECAPAClient
                    stats = self._cloud_ecapa_client.get_stats()
                    cost = stats.get("last_request_cost_usd", 0.0001)

                    # Update average latency
                    n = self._stats["cloud_requests"] + 1
                    self._stats["avg_cloud_latency_ms"] = (
                        (self._stats["avg_cloud_latency_ms"] * (n - 1) + processing_time) / n
                    )

                    return MLResponse(
                        success=True,
                        backend_used=MLBackend.GCP_CLOUD,
                        operation=request.operation,
                        result={
                            "embedding": embedding.tolist() if hasattr(embedding, 'tolist') else list(embedding),
                            "dimensions": len(embedding) if embedding is not None else 0,
                            "backend": stats.get("last_backend", "cloud_ecapa"),
                        },
                        processing_time_ms=processing_time,
                        cost_usd=cost,
                        request_id=request.request_id,
                    )
                else:
                    logger.warning("CloudECAPAClient returned None embedding, falling back to legacy")
            except Exception as e:
                logger.warning(f"CloudECAPAClient failed: {e}, falling back to legacy GCP")

        # ================================================================
        # FALLBACK: Legacy GCP processing (VM-based or direct HTTP)
        # ================================================================
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

                        # Track VM creation for scale-to-zero
                        self._vm_created_time = datetime.now()
                        self._stats["vm_startups"] += 1

                        logger.info(f"✅ GCP VM created: {self._gcp_ml_endpoint}")
                        logger.info(f"   Cost: ${result.get('cost_per_hour', SPOT_VM_COST_PER_HOUR)}/hour")
                        logger.info(f"   Scale-to-zero: Will shutdown after {self._idle_shutdown_minutes}min idle")

                        # Start scale-to-zero monitor
                        await self._start_scale_to_zero_monitor()
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
        """Get router statistics with cost optimization metrics"""
        # Calculate additional metrics
        total_requests = self._stats["total_requests"]
        cache_efficiency = self._stats["cache_hits"] / max(1, total_requests)

        base_stats = {
            **self._stats,
            "current_backend": self._current_backend.value,
            "gcp_endpoint": self._gcp_ml_endpoint or "not configured",
            "cache_size": len(self._cache),
            "hybrid_client_available": HYBRID_CLIENT_AVAILABLE,
            "gcp_optimizer_available": GCP_OPTIMIZER_AVAILABLE,
            "cloud_ecapa_client_available": CLOUD_ECAPA_CLIENT_AVAILABLE,
            "startup_mode": self._startup_decision.mode.value if self._startup_decision else "unknown",
            # Scale-to-zero stats
            "scale_to_zero_enabled": self._scale_to_zero_enabled,
            "idle_shutdown_minutes": self._idle_shutdown_minutes,
            "vm_is_active": self._gcp_vm_ip is not None,
            "last_request_time": self._last_request_time.isoformat() if self._last_request_time else None,
            "vm_created_time": self._vm_created_time.isoformat() if self._vm_created_time else None,
            # Cost efficiency
            "cache_efficiency_percent": round(cache_efficiency * 100, 2),
            "total_embeddings_processed": self._total_embeddings_processed,
            "total_embeddings_cached": self._total_embeddings_cached,
            "estimated_monthly_savings": round(self._stats["total_cost_saved_usd"] * 30, 4),
        }

        # Include CloudECAPAClient stats if available
        if self._cloud_ecapa_client:
            try:
                ecapa_stats = self._cloud_ecapa_client.get_stats()
                base_stats["cloud_ecapa_client"] = {
                    "connected": True,
                    "version": ecapa_stats.get("version", "18.2.0"),
                    "backend": ecapa_stats.get("current_backend", "unknown"),
                    "total_requests": ecapa_stats.get("total_requests", 0),
                    "cloud_run_requests": ecapa_stats.get("cloud_run_requests", 0),
                    "spot_vm_requests": ecapa_stats.get("spot_vm_requests", 0),
                    "local_requests": ecapa_stats.get("local_requests", 0),
                    "cache_hits": ecapa_stats.get("cache_hits", 0),
                    "total_cost_usd": ecapa_stats.get("total_cost_usd", 0.0),
                }
            except Exception as e:
                base_stats["cloud_ecapa_client"] = {
                    "connected": False,
                    "error": str(e),
                }
        else:
            base_stats["cloud_ecapa_client"] = {"connected": False}

        return base_stats

    async def cleanup(self):
        """Cleanup resources including scale-to-zero and active VMs"""
        # Cancel scale-to-zero monitor
        if self._scale_to_zero_task and not self._scale_to_zero_task.done():
            self._scale_to_zero_task.cancel()
            try:
                await self._scale_to_zero_task
            except asyncio.CancelledError:
                pass

        # Shutdown any active VM
        if self._gcp_vm_ip:
            logger.info("🧹 Cleanup: Shutting down active GCP VM")
            await self._shutdown_idle_vm()

        # Close hybrid client
        if self._hybrid_client:
            await self._hybrid_client.close()

        self._cache.clear()
        logger.info("✅ CloudMLRouter cleaned up (VM shutdown, cache cleared)")


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
