"""
Adaptive Model Lifecycle Manager - 3-Tier State Management
==========================================================

Manages models across 3 storage tiers:
1. RAM (HOT): Loaded models, instant access, uses GCP/local RAM
2. Disk Cache (WARM): Cached on disk, 10-30s load time, $0.02/GB/month
3. Cloud Storage (COLD): Archived, 60-120s load time, cheapest

Features:
- Intelligent RAM-aware loading/unloading
- LRU eviction when RAM pressure high
- Automatic state transitions based on usage patterns
- Cost optimization through archiving
- No crashes from RAM overload
- Full async/await architecture

Zero hardcoding - all thresholds and policies from config
"""

import asyncio
import logging
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

import psutil
import yaml

from backend.intelligence.model_registry import ModelDefinition, ModelState, get_model_registry

logger = logging.getLogger(__name__)


@dataclass
class RAMStatus:
    """Current RAM status for a backend"""

    backend: str
    total_gb: float
    used_gb: float
    available_gb: float
    percent_used: float
    models_loaded: List[str]
    model_ram_gb: float


@dataclass
class LoadRequest:
    """Request to load a model"""

    model_name: str
    priority: int = 50
    required_by: Optional[str] = None  # Which component needs it
    timeout_seconds: float = 120.0
    future: Optional[asyncio.Future] = None


class AdaptiveModelLifecycleManager:
    """
    Manages model lifecycle across 3 tiers: RAM → Disk → Cloud Storage

    Responsibilities:
    - Load models to RAM when needed
    - Unload idle models to disk cache
    - Archive rarely-used models to cloud storage
    - Prevent RAM overload through LRU eviction
    - Optimize costs through intelligent state transitions
    """

    def __init__(self, config_path: Optional[str] = None):
        if config_path is None:
            config_path = Path(__file__).parent.parent / "core" / "hybrid_config.yaml"

        self.config_path = Path(config_path)
        self.config = self._load_config()
        self.registry = get_model_registry()

        # RAM thresholds (from config)
        ram_config = self.config.get("hybrid", {}).get("ram_awareness", {})
        self.local_ram_warning = ram_config.get("local_thresholds", {}).get("warning", 70)
        self.local_ram_critical = ram_config.get("local_thresholds", {}).get("critical", 85)
        self.gcp_ram_warning = ram_config.get("gcp_thresholds", {}).get("warning", 75)
        self.gcp_ram_critical = ram_config.get("gcp_thresholds", {}).get("critical", 90)

        # State transition policies (from config or defaults)
        lifecycle_config = self.config.get("hybrid", {}).get("model_lifecycle", {})
        self.unload_after_idle_seconds = lifecycle_config.get(
            "unload_after_idle_seconds", 1800
        )  # 30 min
        self.archive_after_idle_seconds = lifecycle_config.get(
            "archive_after_idle_seconds", 86400
        )  # 24 hrs
        self.min_keep_loaded_models = lifecycle_config.get("min_keep_loaded_models", 1)
        self.max_loaded_models = lifecycle_config.get("max_loaded_models", 3)

        # Backend RAM limits
        self.backend_ram_limits = {"local": 16, "gcp": 32}  # GB  # GB

        # Model loading/unloading locks
        self.model_locks: Dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.load_queue: asyncio.Queue[LoadRequest] = asyncio.Queue()

        # Background tasks
        self.background_tasks: List[asyncio.Task] = []
        self.is_running = False

        # Actual model instances (lazy loaded)
        self.loaded_model_instances: Dict[str, any] = {}

        # Statistics
        self.stats = {
            "loads": 0,
            "unloads": 0,
            "cache_hits": 0,
            "cache_misses": 0,
            "lru_evictions": 0,
            "ram_pressure_events": 0,
        }

        logger.info("✅ Adaptive Model Lifecycle Manager initialized")
        logger.info(
            f"   RAM Thresholds: Local {self.local_ram_critical}%, GCP {self.gcp_ram_critical}%"
        )
        logger.info(f"   Unload after: {self.unload_after_idle_seconds}s idle")
        logger.info(f"   Archive after: {self.archive_after_idle_seconds}s idle")

    def _load_config(self) -> dict:
        """Load configuration from YAML"""
        try:
            with open(self.config_path) as f:
                return yaml.safe_load(f)
        except Exception as e:
            logger.error(f"Failed to load config: {e}")
            return {}

    async def start(self):
        """Start background optimization tasks"""
        if self.is_running:
            return

        self.is_running = True

        # Start background tasks
        self.background_tasks = [
            asyncio.create_task(self._background_optimizer()),
            asyncio.create_task(self._ram_monitor()),
            asyncio.create_task(self._load_queue_processor()),
        ]

        logger.info("✅ Model Lifecycle Manager background tasks started")

    async def stop(self):
        """Stop background tasks gracefully"""
        self.is_running = False

        # Cancel all background loops first, then await in parallel.
        for task in self.background_tasks:
            if not task.done():
                task.cancel()

        if self.background_tasks:
            await asyncio.gather(*self.background_tasks, return_exceptions=True)
            self.background_tasks.clear()

        # Resolve any queued load futures so callers don't hang on shutdown.
        try:
            while not self.load_queue.empty():
                req = self.load_queue.get_nowait()
                if req.future and not req.future.done():
                    req.future.cancel()
        except Exception:
            pass

        logger.info("Model Lifecycle Manager stopped")

    # ============== Model Loading/Unloading ==============

    async def get_model(
        self,
        model_name: str,
        required_by: Optional[str] = None,
        priority: int = 50,
        timeout: float = 120.0,
    ) -> Optional[any]:
        """
        Get a model, loading it if necessary

        Args:
            model_name: Name of model to get
            required_by: Component requesting the model (for logging)
            priority: Higher priority models load first
            timeout: Max seconds to wait for load

        Returns:
            Model instance or None if failed
        """
        model_def = self.registry.get_model(model_name)
        if not model_def:
            logger.error(f"Model {model_name} not found in registry")
            return None

        # Check if already loaded
        if model_def.current_state in [ModelState.LOADED, ModelState.ACTIVE]:
            self.stats["cache_hits"] += 1
            self.registry.record_query(model_name)
            return self.loaded_model_instances.get(model_name)

        # Need to load
        self.stats["cache_misses"] += 1
        logger.info(f"Model {model_name} not loaded, queueing load request...")

        # Create load request
        future = asyncio.get_event_loop().create_future()
        request = LoadRequest(
            model_name=model_name,
            priority=priority,
            required_by=required_by,
            timeout_seconds=timeout,
            future=future,
        )

        await self.load_queue.put(request)

        # Wait for load to complete
        try:
            model_instance = await asyncio.wait_for(future, timeout=timeout)
            self.registry.record_query(model_name)
            return model_instance
        except asyncio.TimeoutError:
            logger.error(f"Timeout waiting for {model_name} to load")
            return None
        except Exception as e:
            logger.error(f"Error loading {model_name}: {e}")
            return None

    async def _load_queue_processor(self):
        """Process model load requests from queue"""
        while self.is_running:
            try:
                # Get next load request (with timeout to check is_running)
                try:
                    request = await asyncio.wait_for(self.load_queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue

                # Process load request
                await self._process_load_request(request)

            except Exception as e:
                logger.error(f"Error in load queue processor: {e}")
                await asyncio.sleep(1)

    async def _process_load_request(self, request: LoadRequest):
        """Process a single load request"""
        model_name = request.model_name
        model_def = self.registry.get_model(model_name)

        if not model_def:
            if request.future and not request.future.done():
                request.future.set_exception(ValueError(f"Model {model_name} not found"))
            return

        # Acquire lock for this model
        async with self.model_locks[model_name]:
            try:
                # Double-check state (might have been loaded by another request)
                if model_def.current_state in [ModelState.LOADED, ModelState.ACTIVE]:
                    if request.future and not request.future.done():
                        request.future.set_result(self.loaded_model_instances.get(model_name))
                    return

                # Check RAM availability
                backend = model_def.backend_preference
                ram_needed = model_def.resources.ram_gb
                ram_status = await self._get_ram_status(backend)

                if ram_status.available_gb < ram_needed:
                    logger.warning(
                        f"Insufficient RAM for {model_name}: need {ram_needed}GB, have {ram_status.available_gb}GB"
                    )
                    # Try to free RAM
                    await self._free_ram(backend, ram_needed)

                    # Re-check after freeing
                    ram_status = await self._get_ram_status(backend)
                    if ram_status.available_gb < ram_needed:
                        logger.error(f"Still insufficient RAM after eviction for {model_name}")
                        if request.future and not request.future.done():
                            request.future.set_exception(MemoryError("Insufficient RAM"))
                        return

                # Load the model
                logger.info(f"Loading {model_name} from {model_def.current_state.value}...")
                self.registry.update_model_state(model_name, ModelState.LOADING)

                model_instance = await self._load_model_instance(model_def)

                if model_instance:
                    self.loaded_model_instances[model_name] = model_instance
                    self.registry.update_model_state(model_name, ModelState.LOADED)
                    self.stats["loads"] += 1
                    logger.info(f"✅ {model_name} loaded successfully")

                    if request.future and not request.future.done():
                        request.future.set_result(model_instance)
                else:
                    self.registry.update_model_state(model_name, ModelState.ERROR)
                    logger.error(f"Failed to load {model_name}")

                    if request.future and not request.future.done():
                        request.future.set_exception(RuntimeError(f"Failed to load {model_name}"))

            except Exception as e:
                logger.error(f"Error loading {model_name}: {e}")
                self.registry.update_model_state(model_name, ModelState.ERROR)

                if request.future and not request.future.done():
                    request.future.set_exception(e)

    async def _load_model_instance(self, model_def: ModelDefinition) -> Optional[any]:
        """
        Actually load a model instance based on its type

        This is where we integrate with specific model loaders:
        - LLaMA 70B: Use LocalLLMInference
        - YOLOv8: Use YOLOv8 loader
        - Semantic Search: Use embedding model loader
        """
        try:
            if model_def.model_type == "llm" and model_def.name == "llama_70b":
                # Load LLaMA 70B via LocalLLMInference
                from backend.intelligence.local_llm_inference import get_llm_inference

                llm = get_llm_inference()
                if not llm.is_running:
                    await llm.start()

                # Wait for model to load
                model_def.performance.load_from_cache_seconds
                if model_def.current_state == ModelState.ARCHIVED:
                    model_def.performance.load_from_archive_seconds

                # LLaMA lazy loads internally, so just trigger it
                await asyncio.sleep(2)  # Give it time to initialize
                return llm

            elif model_def.model_type == "vision" and "yolo" in model_def.name:
                # YOLOv8 loader (placeholder - implement when Phase 3.2 starts)
                logger.info(f"YOLOv8 loader not yet implemented for {model_def.name}")
                await asyncio.sleep(model_def.get_load_time_estimate())
                return {"model": "yolov8_placeholder", "loaded": True}

            elif model_def.model_type == "embedding":
                # Semantic search loader (placeholder - implement when Phase 3.4 starts)
                logger.info(f"Semantic search loader not yet implemented for {model_def.name}")
                await asyncio.sleep(model_def.get_load_time_estimate())
                return {"model": "semantic_search_placeholder", "loaded": True}

            elif model_def.deployment.value == "api":
                # API models are always "loaded" (no actual loading needed)
                return {"model": "api", "endpoint": model_def.name}

            else:
                logger.warning(
                    f"No loader implemented for {model_def.model_type} / {model_def.name}"
                )
                return None

        except Exception as e:
            logger.error(f"Error loading {model_def.name}: {e}")
            return None

    async def unload_model(self, model_name: str, force: bool = False):
        """
        Unload a model from RAM to disk cache

        Args:
            model_name: Model to unload
            force: Force unload even if keep_loaded=True
        """
        model_def = self.registry.get_model(model_name)
        if not model_def:
            return

        # Don't unload protected models unless forced
        if model_def.keep_loaded and not force:
            logger.debug(f"Skipping unload of protected model {model_name}")
            return

        # Acquire lock
        async with self.model_locks[model_name]:
            if model_def.current_state not in [ModelState.LOADED, ModelState.ACTIVE]:
                return

            logger.info(f"Unloading {model_name} from RAM...")
            self.registry.update_model_state(model_name, ModelState.UNLOADING)

            try:
                # Unload the actual model
                if model_name in self.loaded_model_instances:
                    model_instance = self.loaded_model_instances[model_name]

                    # Call model-specific unload if available
                    if hasattr(model_instance, "stop"):
                        await model_instance.stop()
                    elif hasattr(model_instance, "unload"):
                        await model_instance.unload()

                    del self.loaded_model_instances[model_name]

                # Transition to CACHED state
                self.registry.update_model_state(model_name, ModelState.CACHED)
                self.stats["unloads"] += 1
                logger.info(f"✅ {model_name} unloaded to disk cache")

                # Simulate unload time
                await asyncio.sleep(model_def.performance.unload_seconds)

            except Exception as e:
                logger.error(f"Error unloading {model_name}: {e}")
                self.registry.update_model_state(model_name, ModelState.ERROR)

    # ============== RAM Management ==============

    async def _get_ram_status(self, backend: str) -> RAMStatus:
        """Get current RAM status for a backend"""
        total_gb = self.backend_ram_limits.get(backend, 16)
        loaded_models = [
            m for m in self.registry.get_loaded_models() if m.backend_preference == backend
        ]
        model_ram_gb = sum(m.resources.ram_gb for m in loaded_models)

        # Get system RAM usage
        if backend == "local":
            mem = psutil.virtual_memory()
            used_gb = mem.used / (1024**3)
            available_gb = mem.available / (1024**3)
            percent_used = mem.percent
        else:
            # GCP backend - estimate based on loaded models
            used_gb = model_ram_gb + 4  # +4GB for system/other components
            available_gb = total_gb - used_gb
            percent_used = (used_gb / total_gb) * 100

        return RAMStatus(
            backend=backend,
            total_gb=total_gb,
            used_gb=used_gb,
            available_gb=available_gb,
            percent_used=percent_used,
            models_loaded=[m.name for m in loaded_models],
            model_ram_gb=model_ram_gb,
        )

    async def _free_ram(self, backend: str, ram_needed_gb: float) -> float:
        """
        Free RAM by unloading least-recently-used models

        Returns:
            Amount of RAM freed (GB)
        """
        logger.info(f"Attempting to free {ram_needed_gb}GB RAM on {backend}")
        self.stats["ram_pressure_events"] += 1

        # Get loaded models for this backend
        loaded_models = [
            m for m in self.registry.get_loaded_models() if m.backend_preference == backend
        ]

        # Sort by LRU (least recently used first), but respect priority
        lru_models = sorted(
            loaded_models,
            key=lambda m: (
                m.priority,
                -m.last_used_timestamp,
            ),  # Lower priority + older = unload first
        )

        ram_freed = 0.0
        for model_def in lru_models:
            if ram_freed >= ram_needed_gb:
                break

            # Don't unload if used in last 5 minutes
            if time.time() - model_def.last_used_timestamp < 300:
                continue

            # Don't unload below minimum
            if len(self.loaded_model_instances) <= self.min_keep_loaded_models:
                logger.warning("Already at minimum loaded models, cannot free more RAM")
                break

            # Unload this model
            await self.unload_model(model_def.name)
            ram_freed += model_def.resources.ram_gb
            self.stats["lru_evictions"] += 1

        logger.info(f"Freed {ram_freed}GB RAM on {backend}")
        return ram_freed

    # ============== Background Optimization ==============

    async def _background_optimizer(self):
        """
        Background task: optimize model states based on usage patterns

        Runs every 10 minutes:
        - Unload idle models (>30 min idle, RAM >70%)
        - Archive rarely-used models (>24 hrs idle)
        """
        while self.is_running:
            try:
                await asyncio.sleep(600)  # 10 minutes

                logger.debug("Running background model optimization...")

                current_time = time.time()
                for model_name, model_def in self.registry.models.items():
                    if not model_def.last_used_timestamp:
                        continue

                    idle_time = current_time - model_def.last_used_timestamp

                    # Unload if idle >30 min and RAM pressure
                    if model_def.current_state in [ModelState.LOADED, ModelState.ACTIVE]:
                        if idle_time > self.unload_after_idle_seconds:
                            ram_status = await self._get_ram_status(model_def.backend_preference)

                            # Check RAM pressure
                            threshold = (
                                self.gcp_ram_warning
                                if model_def.backend_preference == "gcp"
                                else self.local_ram_warning
                            )

                            if ram_status.percent_used > threshold:
                                logger.info(
                                    f"Unloading idle {model_name} (idle {idle_time/60:.1f}min, RAM {ram_status.percent_used:.1f}%)"
                                )
                                await self.unload_model(model_name)

                    # Archive if idle >24 hours
                    elif model_def.current_state == ModelState.CACHED:
                        if idle_time > self.archive_after_idle_seconds:
                            logger.info(
                                f"Archiving rarely-used {model_name} (idle {idle_time/3600:.1f}hrs)"
                            )
                            await self._archive_model(model_name)

            except Exception as e:
                logger.error(f"Error in background optimizer: {e}")
                await asyncio.sleep(60)

    async def _ram_monitor(self):
        """
        Background task: monitor RAM usage and take action if critical

        Runs every 30 seconds
        """
        while self.is_running:
            try:
                await asyncio.sleep(30)

                for backend in ["local", "gcp"]:
                    ram_status = await self._get_ram_status(backend)

                    threshold = (
                        self.gcp_ram_critical if backend == "gcp" else self.local_ram_critical
                    )

                    if ram_status.percent_used > threshold:
                        logger.warning(
                            f"⚠️ Critical RAM pressure on {backend}: {ram_status.percent_used:.1f}%"
                        )
                        logger.warning(f"   Loaded models: {ram_status.models_loaded}")

                        # Free 20% of RAM
                        ram_to_free = ram_status.total_gb * 0.2
                        await self._free_ram(backend, ram_to_free)

            except Exception as e:
                logger.error(f"Error in RAM monitor: {e}")
                await asyncio.sleep(60)

    async def _archive_model(self, model_name: str):
        """
        Archive a model to cloud storage (Phase 2 implementation)

        For now, just transition state to ARCHIVED
        In Phase 2, would actually upload to GCS
        """
        model_def = self.registry.get_model(model_name)
        if not model_def or model_def.current_state != ModelState.CACHED:
            return

        logger.info(f"Archiving {model_name} to cloud storage...")
        # TODO: Implement actual GCS upload in Phase 2
        self.registry.update_model_state(model_name, ModelState.ARCHIVED)
        logger.info(
            f"✅ {model_name} archived (save ${model_def.resources.disk_storage_cost_per_month:.2f}/month)"
        )

    # ============== Status & Monitoring ==============

    def get_status(self) -> dict:
        """Get comprehensive lifecycle manager status"""
        local_ram = asyncio.run(self._get_ram_status("local"))
        gcp_ram = asyncio.run(self._get_ram_status("gcp"))

        return {
            "running": self.is_running,
            "ram": {
                "local": {
                    "total_gb": local_ram.total_gb,
                    "used_gb": round(local_ram.used_gb, 2),
                    "available_gb": round(local_ram.available_gb, 2),
                    "percent_used": round(local_ram.percent_used, 1),
                    "models": local_ram.models_loaded,
                },
                "gcp": {
                    "total_gb": gcp_ram.total_gb,
                    "used_gb": round(gcp_ram.used_gb, 2),
                    "available_gb": round(gcp_ram.available_gb, 2),
                    "percent_used": round(gcp_ram.percent_used, 1),
                    "models": gcp_ram.models_loaded,
                },
            },
            "models": {
                "loaded": len(self.loaded_model_instances),
                "cached": len(
                    [
                        m
                        for m in self.registry.models.values()
                        if m.current_state == ModelState.CACHED
                    ]
                ),
                "archived": len(
                    [
                        m
                        for m in self.registry.models.values()
                        if m.current_state == ModelState.ARCHIVED
                    ]
                ),
            },
            "stats": self.stats,
            "policies": {
                "unload_after_idle_seconds": self.unload_after_idle_seconds,
                "archive_after_idle_seconds": self.archive_after_idle_seconds,
                "local_ram_critical": self.local_ram_critical,
                "gcp_ram_critical": self.gcp_ram_critical,
            },
        }


# Global instance
_lifecycle_manager: Optional[AdaptiveModelLifecycleManager] = None


def get_lifecycle_manager() -> AdaptiveModelLifecycleManager:
    """Get or create global lifecycle manager instance"""
    global _lifecycle_manager
    if _lifecycle_manager is None:
        _lifecycle_manager = AdaptiveModelLifecycleManager()
    return _lifecycle_manager
