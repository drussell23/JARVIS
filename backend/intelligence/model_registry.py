"""
Model Registry - Comprehensive Model Capability & Resource Tracking
=================================================================

Maintains a dynamic registry of all available models with:
- Capabilities (what tasks they can perform)
- Resource requirements (RAM, disk, GPU)
- Performance profiles (latency, quality, cost)
- Deployment constraints (local, GCP, API)

Zero hardcoding - all configuration driven from hybrid_config.yaml
"""

import logging
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Set

import yaml

logger = logging.getLogger(__name__)


class ModelDeployment(Enum):
    """Where a model can be deployed"""

    LOCAL_ONLY = "local_only"  # macOS 16GB RAM only
    GCP_ONLY = "gcp_only"  # GCP 32GB RAM only
    API = "api"  # External API (no RAM)
    BOTH = "both"  # Can run on either


class ModelState(Enum):
    """Current state of a model in the lifecycle"""

    ARCHIVED = "archived"  # In Cloud Storage (cheapest, slowest)
    CACHED = "cached"  # On disk (cheap, fast load)
    LOADING = "loading"  # Currently loading to RAM
    LOADED = "loaded"  # In RAM, ready to use
    ACTIVE = "active"  # Currently processing requests
    UNLOADING = "unloading"  # Currently unloading from RAM
    ERROR = "error"  # Failed to load


@dataclass
class ResourceProfile:
    """Resource requirements and constraints for a model"""

    ram_gb: float  # RAM required when loaded
    disk_gb: float  # Disk space for cached model
    vram_gb: float = 0.0  # GPU VRAM if needed
    min_ram_backend_gb: int = 16  # Minimum backend RAM to deploy

    # Cost per query (API models only)
    cost_per_query_usd: float = 0.0

    # Storage costs (for disk/cloud caching)
    disk_storage_cost_per_month: float = 0.0  # Auto-calculated

    def __post_init__(self):
        """Calculate storage costs"""
        if self.disk_gb > 0:
            # GCP disk: $0.020/GB/month
            self.disk_storage_cost_per_month = self.disk_gb * 0.020


@dataclass
class PerformanceProfile:
    """Performance characteristics of a model"""

    latency_ms: float  # Typical inference latency
    quality_score: float  # 0.0-1.0 quality rating
    throughput_qps: float = 1.0  # Queries per second

    # Load times for different states
    load_from_cache_seconds: float = 20.0  # Disk → RAM
    load_from_archive_seconds: float = 90.0  # Cloud → Disk → RAM
    unload_seconds: float = 5.0  # RAM → Disk


@dataclass
class ModelDefinition:
    """Complete definition of a model"""

    name: str
    display_name: str
    model_type: str  # llm, vision, embedding, etc.
    capabilities: Set[str]  # What tasks it can perform
    deployment: ModelDeployment

    # Resource and performance profiles
    resources: ResourceProfile
    performance: PerformanceProfile

    # Configuration
    backend_preference: str = "gcp"  # local, gcp, api
    lazy_load: bool = True  # Load on first use?
    keep_loaded: bool = False  # Never unload?
    priority: int = 50  # Higher = keep loaded longer

    # State tracking (runtime)
    current_state: ModelState = field(default=ModelState.CACHED)
    last_used_timestamp: float = 0.0
    total_queries: int = 0
    total_cost_usd: float = 0.0

    def can_deploy_on(self, backend: str, backend_ram_gb: int) -> bool:
        """Check if model can be deployed on a specific backend"""
        if self.deployment == ModelDeployment.API:
            return backend == "api"
        elif self.deployment == ModelDeployment.LOCAL_ONLY:
            return backend == "local" and backend_ram_gb >= self.resources.min_ram_backend_gb
        elif self.deployment == ModelDeployment.GCP_ONLY:
            return backend == "gcp" and backend_ram_gb >= self.resources.min_ram_backend_gb
        elif self.deployment == ModelDeployment.BOTH:
            return backend_ram_gb >= self.resources.min_ram_backend_gb
        return False

    def supports_capability(self, capability: str) -> bool:
        """Check if model supports a specific capability"""
        return capability in self.capabilities

    def get_load_time_estimate(self) -> float:
        """Estimate time to load model based on current state"""
        if self.current_state in [ModelState.LOADED, ModelState.ACTIVE]:
            return 0.0
        elif self.current_state == ModelState.CACHED:
            return self.performance.load_from_cache_seconds
        elif self.current_state == ModelState.ARCHIVED:
            return self.performance.load_from_archive_seconds
        elif self.current_state == ModelState.LOADING:
            return 10.0  # Partial load, estimate remaining
        return 0.0

    def __eq__(self, other: object) -> bool:
        """Two ModelDefinitions are equal if they have the same name"""
        if not isinstance(other, ModelDefinition):
            return NotImplemented
        return self.name == other.name

    def __hash__(self) -> int:
        """Hash by name so ModelDefinition can be used in sets and as dict keys"""
        return hash(self.name)


class ModelRegistry:
    """
    Dynamic model registry - tracks all available models and their states

    Zero hardcoding design:
    - All models defined in hybrid_config.yaml
    - Automatic capability indexing
    - Runtime state tracking
    - Performance monitoring
    """

    def __init__(self, config_path: Optional[str] = None):
        if config_path is None:
            config_path = Path(__file__).parent.parent / "core" / "hybrid_config.yaml"

        self.config_path = Path(config_path)
        self.models: Dict[str, ModelDefinition] = {}
        self.capability_index: Dict[str, Set[str]] = {}  # capability → model names
        self.backend_constraints = {
            "local": 16,  # GB RAM
            "gcp": 32,  # GB RAM
        }

        self._load_models_from_config()
        self._build_capability_index()

        logger.info(f"✅ Model Registry initialized with {len(self.models)} models")
        logger.info(f"   Indexed {len(self.capability_index)} capabilities")

    def _load_models_from_config(self):
        """Load model definitions from hybrid_config.yaml"""
        try:
            with open(self.config_path) as f:
                config = yaml.safe_load(f)

            hybrid_config = config.get("hybrid", {})

            # Load local models
            self._load_local_models(hybrid_config.get("backends", {}).get("local", {}))

            # Load GCP models
            self._load_gcp_models(hybrid_config.get("backends", {}).get("gcp", {}))

            # Load API models (Claude, etc.)
            self._load_api_models(hybrid_config)

            # Load model-specific configs
            self._load_llm_config(hybrid_config.get("local_llm", {}))
            self._load_component_ram_estimates(hybrid_config.get("component_ram_estimates", {}))

        except Exception as e:
            logger.error(f"Failed to load models from config: {e}")
            self._load_default_models()

    def _load_local_models(self, local_config: dict):
        """Load models that run on local macOS"""
        capabilities = local_config.get("capabilities", [])

        # Vision capture (local only)
        if "vision_capture" in capabilities:
            self.models["vision_capture"] = ModelDefinition(
                name="vision_capture",
                display_name="Vision Capture",
                model_type="vision",
                capabilities={"vision_capture", "screenshot", "monitor_detection"},
                deployment=ModelDeployment.LOCAL_ONLY,
                resources=ResourceProfile(ram_gb=0.5, disk_gb=0),
                performance=PerformanceProfile(
                    latency_ms=100, quality_score=0.95, throughput_qps=10
                ),
                backend_preference="local",
                keep_loaded=True,  # Protected CORE component
                priority=100,
            )

        # Voice activation (local only)
        if "voice_activation" in capabilities:
            self.models["voice_activation"] = ModelDefinition(
                name="voice_activation",
                display_name="Voice Activation",
                model_type="voice",
                capabilities={"voice_activation", "wake_word_detection"},
                deployment=ModelDeployment.LOCAL_ONLY,
                resources=ResourceProfile(ram_gb=0.3, disk_gb=0),
                performance=PerformanceProfile(latency_ms=50, quality_score=0.9, throughput_qps=20),
                backend_preference="local",
                keep_loaded=True,
                priority=100,
            )

    def _load_gcp_models(self, gcp_config: dict):
        """Load models that run on GCP"""
        capabilities = gcp_config.get("capabilities", [])

        # YOLOv8m - Medium (recommended)
        if "llm_inference" in capabilities or "ml_processing" in capabilities:
            self.models["yolov8m"] = ModelDefinition(
                name="yolov8m",
                display_name="YOLOv8 Medium",
                model_type="vision",
                capabilities={
                    "object_detection",
                    "ui_detection",
                    "icon_recognition",
                    "vision_analyze_heavy",
                },
                deployment=ModelDeployment.GCP_ONLY,
                resources=ResourceProfile(
                    ram_gb=3.0, disk_gb=6.0, min_ram_backend_gb=32  # Model files
                ),
                performance=PerformanceProfile(
                    latency_ms=33,  # 30 FPS
                    quality_score=0.85,
                    throughput_qps=30,
                    load_from_cache_seconds=15.0,
                    load_from_archive_seconds=60.0,
                ),
                backend_preference="gcp",
                lazy_load=True,
                priority=70,
            )

            # YOLOv8x - Extra-large (optional, higher quality)
            self.models["yolov8x"] = ModelDefinition(
                name="yolov8x",
                display_name="YOLOv8 Extra-Large",
                model_type="vision",
                capabilities={
                    "object_detection",
                    "ui_detection",
                    "icon_recognition",
                    "vision_analyze_heavy",
                    "high_quality_vision",
                },
                deployment=ModelDeployment.GCP_ONLY,
                resources=ResourceProfile(ram_gb=6.0, disk_gb=12.0, min_ram_backend_gb=32),
                performance=PerformanceProfile(
                    latency_ms=50,  # 20 FPS
                    quality_score=0.95,
                    throughput_qps=20,
                    load_from_cache_seconds=20.0,
                    load_from_archive_seconds=80.0,
                ),
                backend_preference="gcp",
                lazy_load=True,
                priority=60,  # Lower priority than YOLOv8m
            )

            # Semantic Search
            self.models["semantic_search"] = ModelDefinition(
                name="semantic_search",
                display_name="Semantic Search",
                model_type="embedding",
                capabilities={
                    "embedding",
                    "similarity_search",
                    "temporal_queries",
                    "semantic_search",
                },
                deployment=ModelDeployment.GCP_ONLY,
                resources=ResourceProfile(ram_gb=2.0, disk_gb=4.0, min_ram_backend_gb=16),
                performance=PerformanceProfile(
                    latency_ms=100,
                    quality_score=0.85,
                    throughput_qps=10,
                    load_from_cache_seconds=10.0,
                ),
                backend_preference="gcp",
                lazy_load=True,
                keep_loaded=True,  # Small enough to keep loaded
                priority=80,
            )

    def _load_llm_config(self, llm_config: dict):
        """Load LLaMA 70B configuration"""
        if not llm_config.get("enabled"):
            return

        model_config = llm_config.get("model", {})
        resources_config = llm_config.get("resources", {})
        generation_config = llm_config.get("generation", {})

        capabilities = set(llm_config.get("use_cases", []))
        capabilities.update(
            [
                "nlp_analysis",
                "chatbot_inference",
                "goal_inference",
                "intent_classification",
                "query_expansion",
                "response_generation",
                "conversational_ai",
                "code_explanation",
                "text_summarization",
                "question_answering",
            ]
        )

        self.models["llama_70b"] = ModelDefinition(
            name="llama_70b",
            display_name=model_config.get("name", "LLaMA 3.1 70B"),
            model_type="llm",
            capabilities=capabilities,
            deployment=ModelDeployment.GCP_ONLY,
            resources=ResourceProfile(
                ram_gb=resources_config.get("ram_required_gb", 24),
                disk_gb=40.0,  # Model weights
                min_ram_backend_gb=32,
            ),
            performance=PerformanceProfile(
                latency_ms=generation_config.get("max_new_tokens", 512),  # Rough estimate
                quality_score=0.92,
                throughput_qps=1.0,
                load_from_cache_seconds=25.0,
                load_from_archive_seconds=90.0,
            ),
            backend_preference="gcp",
            lazy_load=llm_config.get("loading", {}).get("lazy_load", True),
            keep_loaded=False,  # Can unload if idle
            priority=90,  # High priority, keep loaded when possible
        )

    def _load_api_models(self, config: dict):
        """Load external API models (Claude, etc.)"""
        # Claude API
        self.models["claude_api"] = ModelDefinition(
            name="claude_api",
            display_name="Claude API (Sonnet 3.5)",
            model_type="multi_modal",
            capabilities={
                "nlp_analysis",
                "vision",
                "multi_modal",
                "code_explanation",
                "reasoning",
                "chatbot_inference",
                "response_generation",
                "text_summarization",
                "question_answering",
                "vision_analyze_heavy",
            },
            deployment=ModelDeployment.API,
            resources=ResourceProfile(
                ram_gb=0.0,  # API call, no local RAM
                disk_gb=0.0,
                cost_per_query_usd=0.045,  # Estimate: 500in + 500out tokens
            ),
            performance=PerformanceProfile(
                latency_ms=1500,  # 1-3s typical
                quality_score=0.98,  # Highest quality
                throughput_qps=0.5,
            ),
            backend_preference="api",
            lazy_load=False,  # Always available
            keep_loaded=False,
            priority=50,
        )

    def _load_component_ram_estimates(self, estimates: dict):
        """Load RAM estimates for existing components"""
        # These are tracked for monitoring but not managed by lifecycle manager
        # (they're part of the core JARVIS system)

    def _load_default_models(self):
        """Fallback: load minimal default models if config fails"""
        logger.warning("Using default model definitions")

        # Just Claude API as fallback
        self.models["claude_api"] = ModelDefinition(
            name="claude_api",
            display_name="Claude API",
            model_type="multi_modal",
            capabilities={"nlp_analysis", "vision", "multi_modal"},
            deployment=ModelDeployment.API,
            resources=ResourceProfile(ram_gb=0, disk_gb=0, cost_per_query_usd=0.045),
            performance=PerformanceProfile(latency_ms=1500, quality_score=0.98),
            backend_preference="api",
        )

    def _build_capability_index(self):
        """Build reverse index: capability → models"""
        self.capability_index.clear()

        for model_name, model_def in self.models.items():
            for capability in model_def.capabilities:
                if capability not in self.capability_index:
                    self.capability_index[capability] = set()
                self.capability_index[capability].add(model_name)

        logger.debug(f"Built capability index: {len(self.capability_index)} capabilities")

    # ============== Query Methods ==============

    def get_model(self, name: str) -> Optional[ModelDefinition]:
        """Get model definition by name"""
        return self.models.get(name)

    def get_models_for_capability(self, capability: str) -> List[ModelDefinition]:
        """Get all models that support a specific capability"""
        model_names = self.capability_index.get(capability, set())
        return [self.models[name] for name in model_names if name in self.models]

    def get_models_for_backend(self, backend: str) -> List[ModelDefinition]:
        """Get all models deployable on a specific backend"""
        backend_ram = self.backend_constraints.get(backend, 16)
        return [
            model for model in self.models.values() if model.can_deploy_on(backend, backend_ram)
        ]

    def get_loaded_models(self) -> List[ModelDefinition]:
        """Get all models currently loaded in RAM"""
        return [
            model
            for model in self.models.values()
            if model.current_state in [ModelState.LOADED, ModelState.ACTIVE]
        ]

    def get_total_loaded_ram(self) -> float:
        """Calculate total RAM used by loaded models"""
        return sum(model.resources.ram_gb for model in self.get_loaded_models())

    def update_model_state(self, model_name: str, new_state: ModelState):
        """Update a model's current state"""
        if model_name in self.models:
            self.models[model_name].current_state = new_state
            logger.debug(f"Model {model_name} → {new_state.value}")

    def record_query(self, model_name: str, cost_usd: float = 0.0):
        """Record a query execution for tracking"""
        if model_name in self.models:
            import time

            self.models[model_name].last_used_timestamp = time.time()
            self.models[model_name].total_queries += 1
            self.models[model_name].total_cost_usd += cost_usd

    def get_status(self) -> dict:
        """Get comprehensive registry status"""
        loaded_models = self.get_loaded_models()
        total_ram = self.get_total_loaded_ram()

        return {
            "total_models": len(self.models),
            "loaded_models": len(loaded_models),
            "total_ram_gb": round(total_ram, 2),
            "models": {
                name: {
                    "state": model.current_state.value,
                    "ram_gb": model.resources.ram_gb,
                    "queries": model.total_queries,
                    "cost_usd": round(model.total_cost_usd, 2),
                }
                for name, model in self.models.items()
            },
        }


# Global registry instance
_registry: Optional[ModelRegistry] = None


def get_model_registry() -> ModelRegistry:
    """Get or create global model registry instance"""
    global _registry
    if _registry is None:
        _registry = ModelRegistry()
    return _registry
