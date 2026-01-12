"""
Progressive Model Loader for JARVIS
Dynamic, robust model loading with automatic discovery and intelligent parallelization
"""

import asyncio
import logging
import time
import inspect
import importlib
import pkgutil
from typing import Dict, List, Optional, Any, Callable, Set, Tuple
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor
from collections import defaultdict

# Import daemon executor for clean shutdown
try:
    from core.thread_manager import get_daemon_executor
    _USE_DAEMON_EXECUTOR = True
except ImportError:
    _USE_DAEMON_EXECUTOR = False
import json
import os
from pathlib import Path
import threading
from queue import Queue
import multiprocessing
import yaml

logger = logging.getLogger(__name__)

class ModelLoaderConfig:
    """Configuration loader for model loading settings"""

    def __init__(self, config_path: Optional[str] = None):
        self.config_path = config_path or os.path.join(
            os.path.dirname(__file__), "model_loader_config.yaml"
        )
        self.config = self._load_config()

    def _load_config(self) -> Dict[str, Any]:
        """Load configuration from YAML file"""
        try:
            with open(self.config_path, "r") as f:
                return yaml.safe_load(f)
        except Exception as e:
            logger.warning(f"Could not load config from {self.config_path}: {e}")
            return self._get_default_config()

    def _get_default_config(self) -> Dict[str, Any]:
        """Return default configuration if file not found"""
        return {
            "resources": {
                "max_workers": "auto",
                "max_memory_percent": 80,
                "gpu_enabled": True,
                "cache_enabled": True,
                "cache_dir": "model_cache",
            },
            "discovery": {
                "enabled": True,
                "scan_paths": ["vision", "voice", "autonomy", "api"],
                "exclude_patterns": ["*_test.py", "*_demo.py"],
            },
            "loading": {
                "parallel_threshold": 4,
                "timeout_multiplier": 3,
                "retry_attempts": 3,
                "retry_delay": 2,
            },
        }

    def get(self, path: str, default: Any = None) -> Any:
        """Get configuration value by dot-separated path"""
        keys = path.split(".")
        value = self.config

        for key in keys:
            if isinstance(value, dict) and key in value:
                value = value[key]
            else:
                return default

        return value

@dataclass
class ModelInfo:
    """Enhanced model information with auto-discovery capabilities"""

    name: str
    module_path: str
    class_name: str
    category: str
    priority: int = 2  # 1=critical, 2=essential, 3=enhancement
    lazy: bool = True
    fallback: Optional[str] = None
    dependencies: List[str] = field(default_factory=list)
    estimated_load_time: float = 1.0
    memory_requirement: int = 100  # MB
    gpu_required: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        """Auto-detect model characteristics if not provided"""
        if not self.metadata.get("auto_discovered"):
            self._auto_detect_characteristics()

    def _auto_detect_characteristics(self):
        """Automatically detect model characteristics from module/class"""
        try:
            # Check if it's a transformer model
            if (
                "transformer" in self.module_path.lower()
                or "bert" in self.class_name.lower()
            ):
                self.gpu_required = True
                self.memory_requirement = 500
                self.estimated_load_time = 3.0

            # Check if it's a vision model
            elif "vision" in self.module_path.lower() or "cv2" in self.module_path:
                self.memory_requirement = 300
                self.estimated_load_time = 2.0

            # Check if it's a voice/audio model
            elif "voice" in self.module_path.lower() or "audio" in self.module_path:
                self.memory_requirement = 400
                self.estimated_load_time = 2.5

        except Exception:
            pass  # Use defaults if detection fails

class DynamicModelDiscovery:
    """Automatically discover models in the codebase"""

    def __init__(
        self,
        base_paths: Optional[List[str]] = None,
        config: Optional[ModelLoaderConfig] = None,
    ):
        self.base_paths = base_paths or ["vision", "voice", "autonomy", "api"]
        self.discovered_models: Dict[str, ModelInfo] = {}
        self.config = config or ModelLoaderConfig()

    def discover_models(self) -> Dict[str, ModelInfo]:
        """Scan codebase for model classes"""
        logger.info("🔍 Starting dynamic model discovery...")

        for base_path in self.base_paths:
            self._scan_directory(base_path)

        logger.info(f"📦 Discovered {len(self.discovered_models)} models automatically")
        return self.discovered_models

    def _scan_directory(self, path: str):
        """Recursively scan directory for model classes"""
        try:
            module = importlib.import_module(path)

            # Walk through all submodules
            for importer, modname, ispkg in pkgutil.walk_packages(
                module.__path__, prefix=f"{path}."
            ):
                # Skip if module matches exclusion patterns
                module_filename = modname.split(".")[-1] + ".py"
                exclude_patterns = self.config.get(
                    "discovery.exclude_patterns", ["*_test.py", "*_demo.py"]
                )

                should_skip = False
                for pattern in exclude_patterns:
                    import fnmatch

                    if fnmatch.fnmatch(module_filename, pattern):
                        logger.debug(f"Skipping excluded module: {modname}")
                        should_skip = True
                        break

                if should_skip:
                    continue

                try:
                    submodule = importlib.import_module(modname)
                    self._extract_models_from_module(submodule, modname)
                except Exception as e:
                    logger.debug(f"Skipping module {modname}: {e}")

        except Exception as e:
            logger.debug(f"Could not scan {path}: {e}")

    def _extract_models_from_module(self, module, module_path: str):
        """Extract model classes from a module"""
        for name, obj in inspect.getmembers(module):
            if self._is_model_class(obj, name):
                model_key = f"{module_path.split('.')[-1]}_{name}".lower()

                # Determine category and priority
                category = module_path.split(".")[0]
                priority = self._determine_priority(name, module_path)

                self.discovered_models[model_key] = ModelInfo(
                    name=name,
                    module_path=module_path,
                    class_name=name,
                    category=category,
                    priority=priority,
                    lazy=(priority > 1),
                    metadata={"auto_discovered": True},
                )

    def _is_model_class(self, obj, name: str) -> bool:
        """Determine if an object is likely a model class"""
        if not inspect.isclass(obj):
            return False

        # Skip base classes and abstract classes
        excluded_classes = [
            "BaseModel",  # Pydantic base
            "ABC",  # Abstract base class
            "Enum",  # Enum base
            "Exception",  # Exception classes
            "Type",  # Type hints
            "Generic",  # Generic base
            "BaseSettings",  # Pydantic settings base
            "BaseConfig",  # Config base
        ]

        if name in excluded_classes:
            return False
            
        # Skip if it's a Pydantic model base class without concrete fields
        try:
            if hasattr(obj, "__bases__"):
                from pydantic import BaseModel
                if BaseModel in obj.__bases__ and name == "BaseModel":
                    return False
                # Check if it's a concrete Pydantic model (has fields)
                if BaseModel in obj.__mro__:
                    # Must have at least one field defined
                    if hasattr(obj, "__fields__") and not obj.__fields__:
                        return False
        except Exception:
            pass

        # Skip if it's from external libraries
        module = inspect.getmodule(obj)
        if module and module.__name__.startswith(("pydantic", "typing", "abc", "enum", "collections")):
            return False

        # Check for common model patterns
        model_indicators = [
            "Model",
            "System",
            "Manager",
            "Engine",
            "Analyzer",
            "Router",
            "Handler",
            "Processor",
            "Generator",
            "Framework",
            "Vision",
            "Voice",
            "Core",
            "Agent",
        ]

        # Must have at least one indicator AND not be a simple base class
        has_indicator = any(indicator in name for indicator in model_indicators)

        # Additional check: must have init method or be instantiable
        try:
            has_init = hasattr(obj, "__init__") and obj.__init__ is not object.__init__
            # Also check it's not an abstract class
            is_abstract = hasattr(obj, "__abstractmethods__") and obj.__abstractmethods__
            if is_abstract:
                return False
        except Exception:
            has_init = False

        return has_indicator and has_init and not name.startswith("_")

    def _determine_priority(self, class_name: str, module_path: str) -> int:
        """Intelligently determine model priority"""
        # Critical patterns (Priority 1) - only the most essential
        critical_classes = [
            "VisionSystemV2",
            "JARVISAgentVoice",
            "ClaudeAICore",
            "SimpleChatbot",
        ]
        if class_name in critical_classes:
            return 1

        # Module-based critical patterns
        critical_modules = ["main", "core"]
        if (
            any(m in module_path.lower() for m in critical_modules)
            and "Core" in class_name
        ):
            return 1

        # Essential patterns (Priority 2) - needed for core functionality
        essential_patterns = ["Router", "Handler", "API"]
        essential_modules = ["api"]
        if any(p in class_name for p in essential_patterns) and any(
            m in module_path for m in essential_modules
        ):
            return 2

        # Other important but not essential
        if any(pattern in class_name for pattern in ["Manager", "System", "Engine"]):
            return 2

        # Everything else is enhancement (Priority 3)
        return 3

class DependencyResolver:
    """Intelligent dependency resolution for parallel loading"""

    def __init__(self):
        self.dependency_graph: Dict[str, Set[str]] = defaultdict(set)
        self.reverse_dependencies: Dict[str, Set[str]] = defaultdict(set)

    def analyze_dependencies(self, models: Dict[str, ModelInfo]) -> Dict[str, Set[str]]:
        """Analyze and build dependency graph"""
        logger.info("🔗 Analyzing model dependencies...")

        for model_name, model_info in models.items():
            # Explicit dependencies
            for dep in model_info.dependencies:
                self.dependency_graph[model_name].add(dep)
                self.reverse_dependencies[dep].add(model_name)

            # Implicit dependencies based on imports
            self._detect_implicit_dependencies(model_name, model_info)

        # Detect circular dependencies
        self._detect_circular_dependencies()

        return self.dependency_graph

    def _detect_implicit_dependencies(self, model_name: str, model_info: ModelInfo):
        """Detect dependencies based on module imports"""
        try:
            module = importlib.import_module(model_info.module_path)

            # Check module's imports
            if hasattr(module, "__file__") and module.__file__ is not None:
                with open(module.__file__, "r") as f:
                    content = f.read()

                # Look for import patterns
                import_patterns = [
                    "from (\\w+) import",
                    "import (\\w+)",
                ]

                # Add detected dependencies
                # (Implementation simplified for brevity)

        except Exception:
            pass  # Skip if we can't analyze

    def _detect_circular_dependencies(self):
        """Detect and log circular dependencies"""
        visited = set()
        rec_stack = set()

        def has_cycle(node, path=[]):
            visited.add(node)
            rec_stack.add(node)
            path.append(node)

            for neighbor in self.dependency_graph.get(node, []):
                if neighbor not in visited:
                    if has_cycle(neighbor, path.copy()):
                        return True
                elif neighbor in rec_stack:
                    cycle_start = path.index(neighbor)
                    cycle = path[cycle_start:] + [neighbor]
                    logger.warning(
                        f"⚠️  Circular dependency detected: {' -> '.join(cycle)}"
                    )
                    return True

            rec_stack.remove(node)
            return False

        for node in self.dependency_graph:
            if node not in visited:
                has_cycle(node)

    def get_load_order(self, models: Dict[str, ModelInfo]) -> List[List[str]]:
        """Get optimal parallel loading order respecting dependencies"""
        # Topological sort with level grouping for parallel loading
        in_degree = defaultdict(int)

        # Calculate in-degrees
        for model in models:
            for dep in self.dependency_graph.get(model, []):
                in_degree[dep] += 1

        # Find models with no dependencies (can load immediately)
        queue = [model for model in models if in_degree[model] == 0]
        load_levels = []

        while queue:
            # All models in current queue can be loaded in parallel
            current_level = queue.copy()
            load_levels.append(current_level)
            queue.clear()

            # Process current level
            for model in current_level:
                # Reduce in-degree for dependent models
                for dependent in self.reverse_dependencies.get(model, []):
                    in_degree[dependent] -= 1
                    if in_degree[dependent] == 0:
                        queue.append(dependent)

        return load_levels

class AdaptiveLoadBalancer:
    """Dynamically balance load across CPU cores and memory"""

    def __init__(self):
        self.cpu_count = multiprocessing.cpu_count()
        self.available_memory = self._get_available_memory()
        self.load_history: List[Dict[str, float]] = []
        self.optimal_workers = self._calculate_optimal_workers()

    def _get_available_memory(self) -> int:
        """Get available system memory in MB"""
        try:
            import psutil

            return psutil.virtual_memory().available // (1024 * 1024)
        except ImportError:
            return 4096  # Default 4GB

    def _calculate_optimal_workers(self) -> int:
        """Calculate optimal number of workers based on system resources"""
        # Use 75% of CPU cores for model loading
        workers = max(1, int(self.cpu_count * 0.75))

        # Adjust based on available memory
        if self.available_memory < 2048:  # Less than 2GB
            workers = min(workers, 2)
        elif self.available_memory < 4096:  # Less than 4GB
            workers = min(workers, 4)

        return workers

    def should_load_parallel(self, models: List[ModelInfo]) -> bool:
        """Determine if models should be loaded in parallel"""
        total_memory = sum(m.memory_requirement for m in models)

        # Check if we have enough memory for parallel loading
        return total_memory < self.available_memory * 0.8

    def get_worker_pool_size(self, models: List[ModelInfo]) -> int:
        """Get optimal worker pool size for current load"""
        avg_load_time = sum(m.estimated_load_time for m in models) / len(models)

        if avg_load_time < 1.0:  # Fast loading models
            return min(len(models), self.optimal_workers)
        elif avg_load_time < 3.0:  # Medium loading models
            return min(len(models), max(2, self.optimal_workers // 2))
        else:  # Slow loading models
            return min(2, self.optimal_workers)

class ProgressiveModelLoader:
    """Enhanced progressive model loader with dynamic discovery and intelligent parallelization"""

    def __init__(
        self,
        config_path: Optional[str] = None,
        auto_discover: Optional[bool] = None,
        cache_dir: Optional[str] = None,
        max_workers: Optional[int] = None,
    ):

        # Load configuration
        self.config = ModelLoaderConfig(config_path)

        # Initialize components with config
        self.discovery = DynamicModelDiscovery(
            base_paths=self.config.get(
                "discovery.scan_paths", ["vision", "voice", "autonomy", "api"]
            ),
            config=self.config,
        )
        self.resolver = DependencyResolver()
        self.balancer = AdaptiveLoadBalancer()

        # Configuration with fallbacks to config file
        self.auto_discover = (
            auto_discover
            if auto_discover is not None
            else self.config.get("discovery.enabled", True)
        )
        self.cache_dir = (
            Path(cache_dir)
            if cache_dir
            else Path(self.config.get("resources.cache_dir", "model_cache"))
        )

        # Determine max workers
        config_workers = self.config.get("resources.max_workers", "auto")
        if max_workers:
            self.max_workers = max_workers
        elif config_workers == "auto":
            self.max_workers = self.balancer.optimal_workers
        else:
            self.max_workers = int(config_workers)

        # Execution pools (use daemon executor for clean shutdown)
        if _USE_DAEMON_EXECUTOR:
            self.thread_executor = get_daemon_executor(max_workers=self.max_workers, name='model-loader')
        else:
            self.thread_executor = ThreadPoolExecutor(max_workers=self.max_workers)
        self.process_executor = ProcessPoolExecutor(
            max_workers=max(1, self.max_workers // 2)
        )

        # State management
        self.loaded_models: Dict[str, Any] = {}
        self.loading_status: Dict[str, str] = {}
        self.load_times: Dict[str, float] = {}
        self.model_registry: Dict[str, ModelInfo] = {}
        self._background_tasks: List[asyncio.Task] = []

        # Performance tracking
        self.performance_metrics = {
            "total_load_time": 0.0,
            "parallel_efficiency": 0.0,
            "memory_peak": 0,
            "cache_hits": 0,
            "cache_misses": 0,
        }

        # Initialize
        if self.auto_discover:
            self._initialize_registry()

    def _initialize_registry(self):
        """Initialize model registry with auto-discovery"""
        # Start with discovered models
        if self.auto_discover:
            self.model_registry.update(self.discovery.discover_models())

        # Add any manually defined models
        self.model_registry.update(self._get_manual_models())

        # Analyze dependencies
        self.resolver.analyze_dependencies(self.model_registry)

        logger.info(
            f"📊 Model registry initialized with {len(self.model_registry)} models"
        )

    def _get_manual_models(self) -> Dict[str, ModelInfo]:
        """Get manually defined critical models"""
        return {
            "vision_core": ModelInfo(
                name="Vision System Core",
                module_path="vision.vision_system_v2",
                class_name="VisionSystemV2",
                category="vision",
                priority=1,
                lazy=False,
                estimated_load_time=2.0,
                memory_requirement=300,
            ),
            "voice_core": ModelInfo(
                name="Voice System Core",
                module_path="voice.jarvis_agent_voice",
                class_name="JARVISAgentVoice",
                category="voice",
                priority=1,
                lazy=False,
                estimated_load_time=1.5,
                memory_requirement=200,
            ),
            "claude_core": ModelInfo(
                name="Claude Vision Core",
                module_path="vision.workspace_analyzer",
                class_name="WorkspaceAnalyzer",
                category="claude",
                priority=1,
                lazy=False,
                estimated_load_time=1.0,
                memory_requirement=150,
            ),
        }

    async def initialize_all_models(self) -> Dict[str, Any]:
        """Initialize all models with intelligent parallelization"""
        start_time = time.time()
        logger.info("🚀 Starting intelligent parallel model initialization...")

        # Get optimal loading order
        load_levels = self.resolver.get_load_order(self.model_registry)

        total_models = len(self.model_registry)
        loaded_count = 0

        # Load models level by level
        for level_num, level_models in enumerate(load_levels):
            level_model_infos = [
                self.model_registry[model]
                for model in level_models
                if model in self.model_registry
            ]

            # Check if we should load this level in parallel
            if self.balancer.should_load_parallel(level_model_infos):
                logger.info(
                    f"⚡ Loading level {level_num + 1} models in parallel ({len(level_models)} models)"
                )

                # Adjust worker pool size dynamically
                pool_size = self.balancer.get_worker_pool_size(level_model_infos)

                # Load models in parallel
                tasks = []
                for model_name in level_models:
                    if model_name in self.model_registry:
                        model_info = self.model_registry[model_name]
                        task = asyncio.create_task(
                            self._load_model_async(model_name, model_info)
                        )
                        tasks.append(task)

                # Wait for level to complete
                results = await asyncio.gather(*tasks, return_exceptions=True)

                # Process results
                for i, result in enumerate(results):
                    model_name = level_models[i]
                    if isinstance(result, Exception):
                        logger.error(f"❌ Failed to load {model_name}: {result}")
                        await self._handle_load_failure(model_name, result)
                    else:
                        loaded_count += 1
                        progress = (loaded_count / total_models) * 100
                        logger.info(
                            f"✅ {model_name} loaded ({progress:.1f}% complete)"
                        )

            else:
                # Load sequentially if memory constrained
                logger.info(
                    f"📦 Loading level {level_num + 1} models sequentially (memory constrained)"
                )
                for model_name in level_models:
                    if model_name in self.model_registry:
                        try:
                            model_info = self.model_registry[model_name]
                            await self._load_model_async(model_name, model_info)
                            loaded_count += 1
                            progress = (loaded_count / total_models) * 100
                            logger.info(
                                f"✅ {model_name} loaded ({progress:.1f}% complete)"
                            )
                        except Exception as e:
                            logger.error(f"❌ Failed to load {model_name}: {e}")
                            await self._handle_load_failure(model_name, e)

        # Calculate performance metrics
        total_time = time.time() - start_time
        self.performance_metrics["total_load_time"] = total_time
        self.performance_metrics["parallel_efficiency"] = self._calculate_efficiency()

        logger.info(
            f"""
🎯 Model Loading Complete!
━━━━━━━━━━━━━━━━━━━━━━━━
📦 Total models: {total_models}
✅ Successfully loaded: {len(self.loaded_models)}
❌ Failed: {total_models - len(self.loaded_models)}
⏱️  Total time: {total_time:.2f}s
⚡ Parallel efficiency: {self.performance_metrics['parallel_efficiency']:.1f}%
💾 Cache hits: {self.performance_metrics['cache_hits']}
━━━━━━━━━━━━━━━━━━━━━━━━
        """
        )

        return self.loaded_models

    async def initialize_critical_models(self) -> Dict[str, Any]:
        """Load only critical models for fast startup"""
        # Get phase limit from config
        max_critical = self.config.get("loading.max_models_per_phase.critical", 10)

        # Filter and limit critical models
        all_critical = {
            name: info
            for name, info in self.model_registry.items()
            if info.priority == 1 and not info.lazy
        }

        # Sort by importance (specific critical classes first)
        critical_order = [
            "SimpleChatbot",
            "ClaudeAICore",
            "VisionSystemV2",
            "JARVISAgentVoice",
        ]
        sorted_models = sorted(
            all_critical.items(),
            key=lambda x: (
                critical_order.index(x[1].class_name)
                if x[1].class_name in critical_order
                else len(critical_order)
            ),
        )

        # Take only the most important ones up to the limit
        critical_models = dict(sorted_models[:max_critical])

        logger.info(
            f"🚨 Loading {len(critical_models)} critical models (of {len(all_critical)} found) for immediate startup..."
        )

        # Load critical models with highest priority
        tasks = []
        for model_name, model_info in critical_models.items():
            task = asyncio.create_task(self._load_model_async(model_name, model_info))
            tasks.append(task)

        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Ensure all critical models loaded successfully
        loaded = {}
        for i, (model_name, model_info) in enumerate(critical_models.items()):
            if isinstance(results[i], Exception):
                logger.error(f"❌ Critical model {model_name} failed: {results[i]}")
                raise RuntimeError(f"Critical model {model_name} failed to load")
            else:
                loaded[model_name] = results[i]
                logger.info(f"✅ Critical model {model_name} ready")

        return loaded

    async def load_essential_models_background(self):
        """Load essential models in the background"""
        # Get phase limit from config
        max_essential = self.config.get("loading.max_models_per_phase.essential", 20)

        # Filter essential models
        all_essential = {
            name: info
            for name, info in self.model_registry.items()
            if info.priority == 2 and not info.lazy
        }

        # Limit to configured max
        essential_models = dict(list(all_essential.items())[:max_essential])

        logger.info(
            f"⚡ Loading {len(essential_models)} essential models (of {len(all_essential)} found) in background..."
        )

        # Load in background with lower priority
        tasks = []
        for model_name, model_info in essential_models.items():
            task = asyncio.create_task(self._load_model_async(model_name, model_info))
            tasks.append(task)

        # Don't wait for completion - this runs in background
        self._background_tasks.extend(tasks)

    async def load_enhancement_models_lazy(self):
        """Setup lazy loading for enhancement models"""
        # Get phase limit
        max_enhancement = self.config.get(
            "loading.max_models_per_phase.enhancement", 50
        )

        # Filter enhancement models
        all_enhancement = {
            name: info
            for name, info in self.model_registry.items()
            if info.priority == 3
        }

        # Mark them for lazy loading (up to limit)
        enhancement_count = 0
        for name, info in all_enhancement.items():
            if enhancement_count >= max_enhancement:
                break
            info.lazy = True
            enhancement_count += 1

        logger.info(
            f"🔮 {enhancement_count} enhancement models (of {len(all_enhancement)} found) set for lazy loading"
        )

    async def _load_model_async(self, model_name: str, model_info: ModelInfo) -> Any:
        """Load a single model with caching and error handling"""
        start_time = time.time()
        self.loading_status[model_name] = "loading"

        try:
            # Check cache first
            cached_model = await self._load_from_cache(model_name)
            if cached_model:
                self.performance_metrics["cache_hits"] += 1
                self.loaded_models[model_name] = cached_model
                self.loading_status[model_name] = "loaded"
                logger.info(f"💾 {model_name} loaded from cache")
                return cached_model

            self.performance_metrics["cache_misses"] += 1

            # Choose executor based on model requirements
            if model_info.gpu_required or model_info.memory_requirement > 500:
                executor = self.process_executor
            else:
                executor = self.thread_executor

            # Import and instantiate model
            loop = asyncio.get_event_loop()

            # Import module
            module = await loop.run_in_executor(
                executor, importlib.import_module, model_info.module_path
            )

            # Get class
            model_class = getattr(module, model_info.class_name)

            # Create instance with timeout
            try:
                # Check if this is a singleton/already instantiated
                if hasattr(model_class, "_instance"):
                    instance = model_class._instance
                elif hasattr(model_class, "get_instance"):
                    instance = await asyncio.wait_for(
                        loop.run_in_executor(executor, model_class.get_instance),
                        timeout=model_info.estimated_load_time * 3,
                    )
                else:
                    # Try to instantiate with common patterns
                    try:
                        # Try no-args constructor first
                        instance = await asyncio.wait_for(
                            loop.run_in_executor(executor, model_class),
                            timeout=model_info.estimated_load_time * 3,
                        )
                    except Exception as e:
                        # If that fails, try with config parameter
                        if "config" in str(e) or "missing" in str(e):
                            instance = await asyncio.wait_for(
                                loop.run_in_executor(
                                    executor, lambda: model_class(config={})
                                ),
                                timeout=model_info.estimated_load_time * 3,
                            )
                        else:
                            # Check if it's a request/response model (shouldn't be instantiated)
                            if "Request" in model_info.class_name or "Response" in model_info.class_name:
                                logger.debug(f"Skipping {model_name} - appears to be a data model")
                                raise Exception("Data model, not a service")
                            # For classes that shouldn't be instantiated, just return the class
                            logger.warning(
                                f"Cannot instantiate {model_name}, using class object"
                            )
                            instance = model_class
            except asyncio.TimeoutError:
                raise TimeoutError(
                    f"Model loading exceeded timeout ({model_info.estimated_load_time * 3}s)"
                )

            # Cache the loaded model
            await self._save_to_cache(model_name, instance)

            # Update state
            load_time = time.time() - start_time
            self.loaded_models[model_name] = instance
            self.loading_status[model_name] = "loaded"
            self.load_times[model_name] = load_time

            logger.info(f"✅ {model_info.name} loaded in {load_time:.2f}s")

            return instance

        except Exception as e:
            self.loading_status[model_name] = "failed"
            logger.error(f"❌ Failed to load {model_info.name}: {e}")
            raise

    async def _handle_load_failure(self, model_name: str, error: Exception):
        """Handle model loading failure with fallback strategies"""
        model_info = self.model_registry.get(model_name)

        if model_info and model_info.fallback:
            logger.info(f"🔄 Attempting fallback to {model_info.fallback}")

            if model_info.fallback in self.loaded_models:
                self.loaded_models[model_name] = self.loaded_models[model_info.fallback]
                self.loading_status[model_name] = "fallback"
                logger.info(f"✅ Fallback successful for {model_name}")
            else:
                logger.error(f"❌ Fallback {model_info.fallback} not available")

        # Record failure for analysis
        self._record_failure(model_name, error)

    async def _load_from_cache(self, model_name: str) -> Optional[Any]:
        """Load model from cache if available"""
        cache_path = self.cache_dir / f"{model_name}.cache"

        if cache_path.exists():
            try:
                # Implement actual cache loading logic here
                # This is a placeholder
                return None
            except Exception:
                return None

        return None

    async def _save_to_cache(self, model_name: str, model: Any):
        """Save model to cache for faster loading"""
        try:
            # Implement actual cache saving logic here
            # This is a placeholder
            pass
        except Exception:
            pass  # Caching is optional

    def _calculate_efficiency(self) -> float:
        """Calculate parallel loading efficiency"""
        if not self.load_times:
            return 0.0

        sequential_time = sum(self.load_times.values())
        parallel_time = self.performance_metrics["total_load_time"]

        if parallel_time > 0:
            efficiency = (sequential_time / parallel_time - 1) * 100
            return max(0.0, min(100.0, efficiency))

        return 0.0

    def _record_failure(self, model_name: str, error: Exception):
        """Record failure for analysis and learning"""
        failure_log = self.cache_dir / "failures.json"

        try:
            failures = {}
            if failure_log.exists():
                with open(failure_log, "r") as f:
                    failures = json.load(f)

            failures[model_name] = {
                "error": str(error),
                "timestamp": time.time(),
                "type": type(error).__name__,
            }

            with open(failure_log, "w") as f:
                json.dump(failures, f, indent=2)

        except Exception:
            pass  # Logging failures is optional

    def get_model(self, model_name: str) -> Optional[Any]:
        """Get a loaded model, with lazy loading support"""
        if model_name in self.loaded_models:
            return self.loaded_models[model_name]

        # Check if model should be lazy loaded
        if model_name in self.model_registry:
            model_info = self.model_registry[model_name]

            if model_info.lazy and self.loading_status.get(model_name) != "loading":
                logger.info(f"🔄 Lazy loading {model_name}...")

                # Create async task for lazy loading
                asyncio.create_task(self._load_model_async(model_name, model_info))

        return None

    def get_status(self) -> Dict[str, Any]:
        """Get comprehensive loading status"""
        return {
            "summary": {
                "total": len(self.model_registry),
                "loaded": len(
                    [s for s in self.loading_status.values() if s == "loaded"]
                ),
                "loading": len(
                    [s for s in self.loading_status.values() if s == "loading"]
                ),
                "failed": len(
                    [s for s in self.loading_status.values() if s == "failed"]
                ),
                "fallback": len(
                    [s for s in self.loading_status.values() if s == "fallback"]
                ),
                "pending": len(self.model_registry) - len(self.loading_status),
            },
            "models": self.loading_status,
            "performance": self.performance_metrics,
            "system": {
                "workers": self.max_workers,
                "cpu_count": self.balancer.cpu_count,
                "memory_available": self.balancer.available_memory,
            },
        }

    async def shutdown(self):
        """Gracefully shutdown the loader"""
        logger.info("🛑 Shutting down model loader...")

        # Wait for any ongoing loads
        loading_models = [
            model
            for model, status in self.loading_status.items()
            if status == "loading"
        ]

        if loading_models:
            logger.info(
                f"⏳ Waiting for {len(loading_models)} models to finish loading..."
            )
            # Add timeout to prevent hanging
            await asyncio.sleep(5)

        # Shutdown executors
        self.thread_executor.shutdown(wait=False)
        self.process_executor.shutdown(wait=False)

        logger.info("✅ Model loader shutdown complete")

# Global instance with auto-discovery disabled for faster startup
model_loader = ProgressiveModelLoader(auto_discover=False)

# Enhanced lazy proxy with automatic loading
class SmartLazyProxy:
    """Smart proxy that handles lazy loading with fallbacks"""

    def __init__(self, model_name: str, timeout: float = 10.0):
        self.model_name = model_name
        self.timeout = timeout
        self._model = None
        self._loading = False
        self._load_event = asyncio.Event()

    async def _ensure_loaded(self):
        """Ensure model is loaded before use"""
        if self._model is None and not self._loading:
            self._loading = True

            try:
                # Wait for model to be available
                start_time = time.time()
                while time.time() - start_time < self.timeout:
                    self._model = model_loader.get_model(self.model_name)
                    if self._model:
                        break
                    await asyncio.sleep(0.1)

                if self._model is None:
                    raise RuntimeError(
                        f"Model {self.model_name} failed to load within {self.timeout}s"
                    )

            finally:
                self._loading = False
                self._load_event.set()

        elif self._loading:
            # Wait for ongoing load
            await self._load_event.wait()

    def __getattr__(self, name):
        """Synchronous attribute access with blocking load"""
        if self._model is None:
            # Run async load in sync context
            loop = asyncio.new_event_loop()
            loop.run_until_complete(self._ensure_loaded())
            loop.close()

        return getattr(self._model, name)

    async def __agetattr__(self, name):
        """Async attribute access"""
        await self._ensure_loaded()
        return getattr(self._model, name)

# Export smart proxies for commonly used models
vision_system = SmartLazyProxy("vision_core")
voice_system = SmartLazyProxy("voice_core")
claude_vision = SmartLazyProxy("claude_core")
neural_router = SmartLazyProxy("neural_router")
ml_voice_system = SmartLazyProxy("ml_voice")
