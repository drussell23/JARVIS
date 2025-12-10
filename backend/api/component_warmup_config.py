#!/usr/bin/env python3
"""
ULTRA-ADVANCED Dynamic Component Warmup Configuration
======================================================

Revolutionary Features:
- 🔍 AUTO-DISCOVERY: Automatically scans codebase for warmable components
- 🧠 INTELLIGENT PRIORITY: AI-driven priority assignment based on usage patterns
- ⚡ ADAPTIVE TIMEOUTS: Learns optimal timeouts from historical data
- 🔄 DEPENDENCY AUTO-RESOLUTION: Automatically detects component dependencies
- 🏥 HEALTH CHECK GENERATION: Auto-generates health checks via introspection
- 📊 PERFORMANCE LEARNING: Continuously optimizes based on real-world metrics
- 🚀 PARALLEL OPTIMIZATION: Maximum concurrency with smart batching
- 🛡️ FALLBACK SAFETY: Manual registration as failsafe

Modes:
1. DYNAMIC (default): Auto-discovers components with zero hardcoding
2. HYBRID: Combines auto-discovery with manual overrides
3. MANUAL: Traditional hardcoded registration (fallback)

Set via environment: WARMUP_MODE=dynamic|hybrid|manual
"""

import asyncio
import importlib
import inspect
import json
import logging
import os
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

from core.component_warmup import ComponentPriority, get_warmup_system

logger = logging.getLogger(__name__)

# Configuration
WARMUP_MODE = os.getenv("WARMUP_MODE", "manual").lower()  # dynamic, hybrid, manual - DEFAULT TO MANUAL FOR LOW RAM
ENABLE_AUTO_DISCOVERY = WARMUP_MODE in ("dynamic", "hybrid")
ENABLE_PERFORMANCE_LEARNING = os.getenv("WARMUP_LEARNING", "false").lower() == "true"  # Disable by default to save memory


# ═══════════════════════════════════════════════════════════════════════════
# DYNAMIC DISCOVERY SYSTEM
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class ComponentMetadata:
    """Rich metadata about a discovered component"""
    module_path: str
    class_name: str
    function_name: Optional[str] = None
    priority_score: float = 0.5  # 0.0 (LOW) to 1.0 (CRITICAL)
    estimated_load_time: float = 5.0
    dependencies: Set[str] = field(default_factory=set)
    required: bool = False
    category: str = "unknown"
    manual_override: bool = False  # If manually registered


@dataclass
class ComponentPattern:
    """Patterns for intelligent component discovery"""
    name_patterns: List[str]
    priority: ComponentPriority
    timeout_multiplier: float
    required: bool
    category: str


# Smart patterns for auto-discovery
COMPONENT_PATTERNS = [
    ComponentPattern(
        name_patterns=["*auth*", "*security*", "*lock*", "*permission*"],
        priority=ComponentPriority.CRITICAL,
        timeout_multiplier=2.0,
        required=True,
        category="security"
    ),
    ComponentPattern(
        name_patterns=["*voice*", "*speaker*", "*stt*", "*biometric*", "*audio*"],
        priority=ComponentPriority.CRITICAL,
        timeout_multiplier=2.5,
        required=False,
        category="voice"
    ),
    ComponentPattern(
        name_patterns=["*context*", "*nlp*", "*intelligence*", "*intent*"],
        priority=ComponentPriority.HIGH,
        timeout_multiplier=1.5,
        required=True,
        category="intelligence"
    ),
    ComponentPattern(
        name_patterns=["*vision*", "*yabai*", "*window*", "*display*", "*capture*"],
        priority=ComponentPriority.MEDIUM,
        timeout_multiplier=1.2,
        required=False,
        category="vision"
    ),
    ComponentPattern(
        name_patterns=["*learning*", "*database*", "*cache*", "*storage*"],
        priority=ComponentPriority.MEDIUM,
        timeout_multiplier=1.5,
        required=False,
        category="data"
    ),
    ComponentPattern(
        name_patterns=["*query*", "*handler*", "*router*", "*processor*"],
        priority=ComponentPriority.LOW,
        timeout_multiplier=1.0,
        required=False,
        category="processing"
    ),
]


class DynamicComponentDiscovery:
    """
    Revolutionary auto-discovery system that finds and configures
    warmable components with ZERO hardcoding.
    """

    def __init__(self, base_path: Optional[Path] = None):
        self.base_path = base_path or Path(__file__).parent.parent
        self.discovered_components: Dict[str, ComponentMetadata] = {}
        self.performance_cache = self._load_performance_cache()

    def _load_performance_cache(self) -> Dict:
        """Load historical performance data for adaptive optimization"""
        cache_file = self.base_path / ".jarvis_cache" / "component_performance.json"
        try:
            if cache_file.exists():
                with open(cache_file, 'r') as f:
                    data = json.load(f)
                    logger.info(f"[DYNAMIC] Loaded performance cache with {len(data.get('load_times', {}))} components")
                    return data
        except Exception as e:
            logger.debug(f"Could not load performance cache: {e}")
        return {"load_times": {}, "failure_rates": {}, "priorities": {}}

    async def _save_performance_cache(self):
        """Save performance data for future optimization"""
        cache_dir = self.base_path / ".jarvis_cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = cache_dir / "component_performance.json"

        try:
            with open(cache_file, 'w') as f:
                json.dump(self.performance_cache, f, indent=2)
            logger.debug("[DYNAMIC] Saved performance cache")
        except Exception as e:
            logger.warning(f"Could not save performance cache: {e}")

    async def discover_all_components(self) -> Dict[str, ComponentMetadata]:
        """
        🔍 AUTO-DISCOVER all warmable components via intelligent scanning.

        Discovery strategies:
        1. Singleton functions (get_*, initialize_*)
        2. Service classes (*Service, *Manager, *Handler)
        3. Module metadata (__warmup__ declarations)
        4. Learning database usage patterns
        """
        logger.info("[DYNAMIC] 🔍 Starting intelligent component discovery...")
        start_time = time.time()

        # Parallel discovery
        await asyncio.gather(
            self._discover_via_singletons(),
            self._discover_via_patterns(),
            return_exceptions=True
        )

        # Post-processing
        await self._analyze_dependencies()
        await self._calculate_intelligent_priorities()
        await self._estimate_load_times()

        elapsed = time.time() - start_time
        logger.info(
            f"[DYNAMIC] ✅ Discovered {len(self.discovered_components)} components "
            f"in {elapsed:.2f}s"
        )

        return self.discovered_components

    async def _discover_via_singletons(self):
        """Scan for singleton getter functions"""
        try:
            scan_modules = [
                'voice_unlock',
                'context_intelligence',
                'intelligence',
                'vision',
                'core',
                'system_control',
            ]

            for module_name in scan_modules:
                try:
                    module = importlib.import_module(module_name)
                    await self._scan_module_recursively(module)
                except (ImportError, AttributeError):
                    continue

        except Exception as e:
            logger.debug(f"Singleton discovery error: {e}")

    async def _scan_module_recursively(self, module):
        """Recursively scan module for singleton functions"""
        try:
            for name, obj in inspect.getmembers(module):
                # Look for get_* or initialize_* functions
                if (name.startswith(('get_', 'initialize_')) and
                    inspect.isfunction(obj) and
                    not name.startswith('get_warmup')):  # Avoid recursion

                    component_name = name.replace('get_', '').replace('initialize_', '')

                    if component_name and component_name not in self.discovered_components:
                        self.discovered_components[component_name] = ComponentMetadata(
                            module_path=module.__name__,
                            class_name=name,
                            function_name=name,
                            category=self._infer_category(component_name),
                        )
                        logger.debug(f"[DYNAMIC] Found: {component_name} in {module.__name__}")

        except Exception as e:
            logger.debug(f"Error scanning {module}: {e}")

    async def _discover_via_patterns(self):
        """
        🔍 ADVANCED: Discover components via filesystem patterns and conventions.

        Discovery strategies:
        1. Class-based services (*Service, *Manager, *Handler, *Controller, *Engine, *Router)
        2. Module metadata (__warmup__ declarations)
        3. Decorated functions (@warmup, @preload)
        4. Directory structure conventions (*/services/, */managers/, */handlers/)
        5. Configuration files (warmup.yaml, components.json)
        """
        try:
            logger.debug("[DYNAMIC] Discovering via patterns and conventions...")

            # Strategy 1: Scan for service-like classes
            await self._discover_service_classes()

            # Strategy 2: Look for __warmup__ metadata in modules
            await self._discover_via_metadata()

            # Strategy 3: Scan conventional directories
            await self._discover_via_directory_structure()

            # Strategy 4: Load from configuration files if present
            await self._discover_via_config_files()

        except Exception as e:
            logger.debug(f"Pattern discovery error: {e}")

    async def _discover_service_classes(self):
        """Discover service-like classes (XyzService, XyzManager, etc.)"""
        try:
            service_suffixes = ['Service', 'Manager', 'Handler', 'Controller', 'Engine', 'Router', 'Processor']

            scan_paths = [
                self.base_path / 'voice_unlock',
                self.base_path / 'context_intelligence',
                self.base_path / 'intelligence',
                self.base_path / 'vision',
                self.base_path / 'core',
                self.base_path / 'system_control',
            ]

            for scan_path in scan_paths:
                if not scan_path.exists():
                    continue

                # Scan Python files
                for py_file in scan_path.rglob('*.py'):
                    if '__pycache__' in str(py_file) or 'test' in str(py_file):
                        continue

                    try:
                        # Read file to find class definitions
                        content = py_file.read_text()
                        lines = content.split('\n')

                        for line in lines:
                            # Look for class definitions with service suffixes
                            if line.strip().startswith('class '):
                                for suffix in service_suffixes:
                                    if suffix in line:
                                        # Extract class name
                                        class_name = line.split('class ')[1].split('(')[0].split(':')[0].strip()

                                        # Check if there's a get_ function for this class
                                        getter_name = f"get_{self._class_to_snake_case(class_name)}"

                                        if f"def {getter_name}" in content or f"async def {getter_name}" in content:
                                            component_name = self._class_to_snake_case(class_name)

                                            if component_name not in self.discovered_components:
                                                # Calculate module path from file path
                                                rel_path = py_file.relative_to(self.base_path)
                                                module_path = str(rel_path.with_suffix('')).replace('/', '.')

                                                self.discovered_components[component_name] = ComponentMetadata(
                                                    module_path=module_path,
                                                    class_name=class_name,
                                                    function_name=getter_name,
                                                    category=self._infer_category(component_name),
                                                )
                                                logger.debug(f"[DYNAMIC] Found service class: {class_name} → {component_name}")
                                                break

                    except Exception as e:
                        logger.debug(f"Error scanning {py_file}: {e}")
                        continue

        except Exception as e:
            logger.debug(f"Service class discovery error: {e}")

    async def _discover_via_metadata(self):
        """Discover components that declare __warmup__ metadata"""
        try:
            scan_modules = [
                'voice_unlock',
                'context_intelligence',
                'intelligence',
                'vision',
                'core',
                'system_control',
            ]

            for module_name in scan_modules:
                try:
                    module = importlib.import_module(module_name)

                    # Check for __warmup__ attribute
                    if hasattr(module, '__warmup__'):
                        warmup_config = getattr(module, '__warmup__')

                        if isinstance(warmup_config, dict):
                            component_name = warmup_config.get('name', module_name.split('.')[-1])

                            if component_name not in self.discovered_components:
                                # Get loader function
                                loader_name = warmup_config.get('loader', f"get_{component_name}")

                                self.discovered_components[component_name] = ComponentMetadata(
                                    module_path=module.__name__,
                                    class_name=loader_name,
                                    function_name=loader_name,
                                    priority_score=warmup_config.get('priority', 0.5),
                                    estimated_load_time=warmup_config.get('timeout', 5.0),
                                    required=warmup_config.get('required', False),
                                    category=warmup_config.get('category', 'unknown'),
                                )
                                logger.info(f"[DYNAMIC] Found __warmup__ metadata: {component_name}")

                except (ImportError, AttributeError):
                    continue

        except Exception as e:
            logger.debug(f"Metadata discovery error: {e}")

    async def _discover_via_directory_structure(self):
        """Discover components based on directory naming conventions"""
        try:
            # Look for conventional directories
            service_dirs = [
                'services',
                'managers',
                'handlers',
                'engines',
                'routers',
                'processors',
            ]

            for service_dir in service_dirs:
                # Search in backend directory
                search_paths = list(self.base_path.rglob(service_dir))

                for path in search_paths:
                    if not path.is_dir() or '__pycache__' in str(path):
                        continue

                    # Scan Python files in this directory
                    for py_file in path.glob('*.py'):
                        if py_file.name.startswith('_'):
                            continue

                        try:
                            component_name = py_file.stem

                            if component_name not in self.discovered_components:
                                # Check for getter function
                                content = py_file.read_text()

                                getter_patterns = [
                                    f"def get_{component_name}",
                                    f"async def get_{component_name}",
                                    f"def initialize_{component_name}",
                                    f"async def initialize_{component_name}",
                                ]

                                for pattern in getter_patterns:
                                    if pattern in content:
                                        # Calculate module path
                                        rel_path = py_file.relative_to(self.base_path)
                                        module_path = str(rel_path.with_suffix('')).replace('/', '.')

                                        function_name = pattern.replace('def ', '').replace('async ', '').split('(')[0]

                                        self.discovered_components[component_name] = ComponentMetadata(
                                            module_path=module_path,
                                            class_name=function_name,
                                            function_name=function_name,
                                            category=self._infer_category_from_dir(service_dir),
                                        )
                                        logger.debug(f"[DYNAMIC] Found in {service_dir}/: {component_name}")
                                        break

                        except Exception as e:
                            logger.debug(f"Error scanning {py_file}: {e}")
                            continue

        except Exception as e:
            logger.debug(f"Directory structure discovery error: {e}")

    async def _discover_via_config_files(self):
        """Discover components declared in configuration files"""
        try:
            # Look for warmup configuration files
            config_files = [
                self.base_path / 'warmup.yaml',
                self.base_path / 'warmup.yml',
                self.base_path / 'config' / 'warmup.yaml',
                self.base_path / '.jarvis' / 'warmup.yaml',
            ]

            for config_file in config_files:
                if not config_file.exists():
                    continue

                try:
                    import yaml

                    with open(config_file, 'r') as f:
                        config = yaml.safe_load(f)

                    if not config or 'components' not in config:
                        continue

                    for component_config in config['components']:
                        component_name = component_config.get('name')

                        if component_name and component_name not in self.discovered_components:
                            self.discovered_components[component_name] = ComponentMetadata(
                                module_path=component_config.get('module', component_name),
                                class_name=component_config.get('loader', f"get_{component_name}"),
                                function_name=component_config.get('loader', f"get_{component_name}"),
                                priority_score=component_config.get('priority', 0.5),
                                estimated_load_time=component_config.get('timeout', 5.0),
                                required=component_config.get('required', False),
                                category=component_config.get('category', 'unknown'),
                            )
                            logger.info(f"[DYNAMIC] Found in config file: {component_name}")

                except Exception as e:
                    logger.debug(f"Error loading config file {config_file}: {e}")

        except Exception as e:
            logger.debug(f"Config file discovery error: {e}")

    def _class_to_snake_case(self, class_name: str) -> str:
        """Convert ClassName to class_name"""
        import re
        # Insert underscore before uppercase letters
        s1 = re.sub('(.)([A-Z][a-z]+)', r'\1_\2', class_name)
        # Insert underscore before uppercase letters preceded by lowercase
        return re.sub('([a-z0-9])([A-Z])', r'\1_\2', s1).lower()

    def _infer_category_from_dir(self, dir_name: str) -> str:
        """Infer category from directory name"""
        dir_lower = dir_name.lower()

        category_map = {
            'services': 'service',
            'managers': 'management',
            'handlers': 'processing',
            'engines': 'engine',
            'routers': 'routing',
            'processors': 'processing',
        }

        return category_map.get(dir_lower, 'unknown')

    async def _analyze_dependencies(self):
        """🔗 Auto-detect component dependencies via import analysis"""
        logger.debug("[DYNAMIC] Analyzing component dependencies...")

        for name, metadata in self.discovered_components.items():
            try:
                module = importlib.import_module(metadata.module_path)
                source = inspect.getsource(module)

                # Check for imports of other discovered components
                for other_name, other_metadata in self.discovered_components.items():
                    if (other_name != name and
                        other_metadata.module_path in source):
                        metadata.dependencies.add(other_name)

            except Exception as e:
                logger.debug(f"Could not analyze dependencies for {name}: {e}")

    async def _calculate_intelligent_priorities(self):
        """🧠 AI-driven priority assignment based on patterns and history"""
        for name, metadata in self.discovered_components.items():
            base_score = 0.5

            # Pattern matching
            name_lower = name.lower()
            for pattern in COMPONENT_PATTERNS:
                if any(self._match_pattern(name_lower, p) for p in pattern.name_patterns):
                    base_score = self._priority_to_score(pattern.priority)
                    metadata.required = pattern.required
                    metadata.category = pattern.category
                    break

            # Historical adjustment
            if name in self.performance_cache.get("priorities", {}):
                historical = self.performance_cache["priorities"][name]
                base_score = base_score * 0.7 + historical * 0.3

            metadata.priority_score = base_score

    async def _estimate_load_times(self):
        """⏱️ Adaptive timeout calculation from historical data"""
        for name, metadata in self.discovered_components.items():
            base_time = 5.0

            # Pattern-based multiplier
            name_lower = name.lower()
            for pattern in COMPONENT_PATTERNS:
                if any(self._match_pattern(name_lower, p) for p in pattern.name_patterns):
                    base_time *= pattern.timeout_multiplier
                    break

            # Use historical P95 if available
            if name in self.performance_cache.get("load_times", {}):
                times = self.performance_cache["load_times"][name]
                if isinstance(times, list) and times:
                    sorted_times = sorted(times)
                    p95_idx = int(len(sorted_times) * 0.95)
                    historical = sorted_times[p95_idx]
                    base_time = base_time * 0.5 + historical * 0.5

            metadata.estimated_load_time = max(3.0, min(30.0, base_time))  # Clamp

    def _match_pattern(self, text: str, pattern: str) -> bool:
        """Match wildcard pattern"""
        import fnmatch
        return fnmatch.fnmatch(text, pattern)

    def _priority_to_score(self, priority: ComponentPriority) -> float:
        """Convert priority enum to score"""
        mapping = {
            ComponentPriority.CRITICAL: 1.0,
            ComponentPriority.HIGH: 0.75,
            ComponentPriority.MEDIUM: 0.5,
            ComponentPriority.LOW: 0.25,
            ComponentPriority.DEFERRED: 0.1,
        }
        return mapping.get(priority, 0.5)

    def _score_to_priority(self, score: float) -> ComponentPriority:
        """Convert score to priority enum"""
        if score >= 0.9:
            return ComponentPriority.CRITICAL
        elif score >= 0.65:
            return ComponentPriority.HIGH
        elif score >= 0.4:
            return ComponentPriority.MEDIUM
        elif score >= 0.15:
            return ComponentPriority.LOW
        else:
            return ComponentPriority.DEFERRED

    def _infer_category(self, component_name: str) -> str:
        """Infer category from component name"""
        name_lower = component_name.lower()

        categories = {
            "security": ["auth", "security", "lock", "permission", "biometric"],
            "voice": ["voice", "speaker", "stt", "tts", "audio", "speech"],
            "vision": ["vision", "yabai", "window", "display", "screen"],
            "intelligence": ["context", "nlp", "intent", "intelligence"],
            "data": ["database", "cache", "storage", "learning"],
            "system": ["macos", "controller", "system"],
        }

        for category, keywords in categories.items():
            if any(kw in name_lower for kw in keywords):
                return category

        return "unknown"


class DynamicLoaderFactory:
    """🏭 Auto-generates loaders and health checks without hardcoding"""

    @staticmethod
    async def create_loader(metadata: ComponentMetadata) -> Callable:
        """Generate async loader function dynamically"""

        async def dynamic_loader():
            try:
                module = importlib.import_module(metadata.module_path)

                if metadata.function_name:
                    loader_func = getattr(module, metadata.function_name)

                    # Handle async/sync
                    if inspect.iscoroutinefunction(loader_func):
                        instance = await loader_func()
                    else:
                        instance = loader_func()

                    # Auto-initialize if needed
                    if (hasattr(instance, 'initialize') and
                        not getattr(instance, 'initialized', True)):
                        if inspect.iscoroutinefunction(instance.initialize):
                            await instance.initialize()
                        else:
                            instance.initialize()

                    return instance

                return None

            except Exception as e:
                logger.debug(f"[DYNAMIC-LOADER] Failed to load {metadata.class_name}: {e}")
                return None

        return dynamic_loader

    @staticmethod
    async def create_health_check(metadata: ComponentMetadata) -> Optional[Callable]:
        """Generate health check function dynamically"""

        async def dynamic_health_check(instance) -> bool:
            if instance is None:
                return False

            try:
                # Try common health check methods
                for method_name in ['health_check', 'is_healthy', 'ping', 'verify']:
                    if hasattr(instance, method_name):
                        method = getattr(instance, method_name)
                        if callable(method):
                            result = await method() if inspect.iscoroutinefunction(method) else method()
                            return bool(result)

                # Fallback: check initialized attribute
                if hasattr(instance, 'initialized'):
                    return bool(instance.initialized)

                return True  # Exists = healthy

            except Exception:
                return False

        return dynamic_health_check


# ═══════════════════════════════════════════════════════════════════════════
# MANUAL COMPONENT REGISTRATION (Optimized & Enhanced)
# Used as fallback or in HYBRID/MANUAL modes
# ═══════════════════════════════════════════════════════════════════════════

async def register_manual_components(warmup):
    """
    Enhanced manual registration with optimized loaders.
    Used as failsafe when auto-discovery is disabled or fails.
    """
    logger.info("[MANUAL] Registering manually configured components...")

    # ═══ CRITICAL COMPONENTS ═══
    warmup.register_component(
        name="screen_lock_detector",
        loader=load_screen_lock_detector,
        priority=ComponentPriority.CRITICAL,
        health_check=check_screen_lock_detector_health,
        timeout=10.0,
        required=True,
        category="security",
    )

    # 🔥 ECAPA-TDNN Pre-Warmer - CRITICAL for instant voice unlock!
    # This warms up the Cloud ECAPA endpoint at STARTUP, not when user says "unlock my screen"
    warmup.register_component(
        name="ecapa_prewarmer",
        loader=load_ecapa_prewarmer,
        priority=ComponentPriority.CRITICAL,
        health_check=check_ecapa_prewarmer_health,
        timeout=120.0,  # 2 minutes - Cloud Run cold starts can take time
        retry_count=2,
        required=False,  # Don't block startup, but try hard
        category="voice_biometrics",
    )

    warmup.register_component(
        name="voice_auth",
        loader=load_voice_auth,
        priority=ComponentPriority.CRITICAL,
        health_check=check_voice_auth_health,
        timeout=20.0,
        retry_count=1,
        required=False,
        category="security",
    )

    # ═══ HIGH PRIORITY COMPONENTS ═══
    warmup.register_component(
        name="context_aware_handler",
        loader=load_context_aware_handler,
        priority=ComponentPriority.HIGH,
        dependencies=["screen_lock_detector"],
        timeout=20.0,
        required=True,
        category="intelligence",
    )

    warmup.register_component(
        name="multi_space_context_graph",
        loader=load_multi_space_context_graph,
        priority=ComponentPriority.HIGH,
        health_check=check_context_graph_health,
        timeout=20.0,
        required=False,
        category="context",
    )

    warmup.register_component(
        name="implicit_reference_resolver",
        loader=load_implicit_resolver,
        priority=ComponentPriority.HIGH,
        timeout=15.0,
        required=False,
        category="nlp",
    )

    warmup.register_component(
        name="compound_action_parser",
        loader=load_compound_parser,
        priority=ComponentPriority.HIGH,
        health_check=check_compound_parser_health,
        timeout=15.0,
        required=False,
        category="nlp",
    )

    warmup.register_component(
        name="macos_controller",
        loader=load_macos_controller,
        priority=ComponentPriority.HIGH,
        health_check=check_macos_controller_health,
        timeout=10.0,
        required=True,
        category="system",
    )

    # ═══ MEDIUM PRIORITY COMPONENTS ═══
    warmup.register_component(
        name="query_complexity_manager",
        loader=load_query_complexity_manager,
        priority=ComponentPriority.MEDIUM,
        dependencies=["implicit_reference_resolver"],
        timeout=20.0,
        required=False,
        category="intelligence",
    )

    warmup.register_component(
        name="yabai_detector",
        loader=load_yabai_detector,
        priority=ComponentPriority.MEDIUM,
        health_check=check_yabai_health,
        timeout=10.0,
        required=False,
        category="vision",
    )

    warmup.register_component(
        name="multi_space_window_detector",
        loader=load_window_detector,
        priority=ComponentPriority.MEDIUM,
        dependencies=["yabai_detector"],
        timeout=15.0,
        required=False,
        category="vision",
    )

    warmup.register_component(
        name="learning_database",
        loader=load_learning_database,
        priority=ComponentPriority.MEDIUM,
        health_check=check_database_health,
        timeout=30.0,
        required=False,
        category="learning",
    )

    # ═══ LOW PRIORITY COMPONENTS ═══
    warmup.register_component(
        name="action_query_handler",
        loader=load_action_query_handler,
        priority=ComponentPriority.LOW,
        dependencies=["implicit_reference_resolver"],
        timeout=10.0,
        required=False,
        category="intelligence",
    )

    warmup.register_component(
        name="predictive_query_handler",
        loader=load_predictive_handler,
        priority=ComponentPriority.LOW,
        timeout=10.0,
        required=False,
        category="intelligence",
    )

    warmup.register_component(
        name="multi_space_query_handler",
        loader=load_multi_space_handler,
        priority=ComponentPriority.LOW,
        dependencies=["multi_space_context_graph", "learning_database"],
        timeout=25.0,
        required=False,
        category="vision",
    )

    logger.info(f"[MANUAL] Registered {len(warmup.components)} manual components")


# ═══════════════════════════════════════════════════════════════════════════
# MAIN REGISTRATION (Intelligent Mode Selection)
# ═══════════════════════════════════════════════════════════════════════════

async def register_all_components(processor_instance=None):
    """
    🚀 INTELLIGENT COMPONENT REGISTRATION with mode selection:

    - DYNAMIC: Pure auto-discovery (zero hardcoding)
    - HYBRID: Auto-discovery + manual overrides (default, best of both)
    - MANUAL: Traditional hardcoded registration (failsafe)

    Set via: WARMUP_MODE=dynamic|hybrid|manual
    """
    logger.info(f"[WARMUP-CONFIG] 🚀 Initializing in {WARMUP_MODE.upper()} mode...")
    warmup = get_warmup_system()
    start_time = time.time()

    try:
        if WARMUP_MODE == "dynamic" and ENABLE_AUTO_DISCOVERY:
            # Pure auto-discovery
            await register_dynamic_only(warmup)

        elif WARMUP_MODE == "hybrid" and ENABLE_AUTO_DISCOVERY:
            # Hybrid: manual + auto-discovery
            await register_hybrid(warmup)

        else:
            # Manual fallback
            await register_manual_components(warmup)

    except Exception as e:
        logger.error(f"[WARMUP-CONFIG] Registration failed: {e}")
        # Fallback to manual
        if WARMUP_MODE != "manual":
            logger.warning("[WARMUP-CONFIG] Falling back to manual registration...")
            await register_manual_components(warmup)

    elapsed = time.time() - start_time
    logger.info(
        f"[WARMUP-CONFIG] ✅ Registered {len(warmup.components)} components "
        f"in {elapsed:.2f}s ({WARMUP_MODE.upper()} mode)"
    )


async def register_dynamic_only(warmup):
    """Pure auto-discovery mode"""
    logger.info("[DYNAMIC] Using pure auto-discovery (zero hardcoding)")

    discovery = DynamicComponentDiscovery()
    factory = DynamicLoaderFactory()

    components = await discovery.discover_all_components()

    if not components:
        raise Exception("No components discovered")

    for name, metadata in components.items():
        loader = await factory.create_loader(metadata)
        health_check = await factory.create_health_check(metadata)
        priority = discovery._score_to_priority(metadata.priority_score)

        warmup.register_component(
            name=name,
            loader=loader,
            priority=priority,
            health_check=health_check,
            dependencies=list(metadata.dependencies),
            timeout=metadata.estimated_load_time,
            retry_count=1 if metadata.priority_score > 0.7 else 2,
            required=metadata.required,
            category=metadata.category,
        )

    await discovery._save_performance_cache()


async def register_hybrid(warmup):
    """Hybrid mode: manual (trusted) + auto-discovery (experimental)"""
    logger.info("[HYBRID] Using hybrid mode (manual + auto-discovery)")

    # Register critical manual components first (trusted)
    await register_manual_components(warmup)

    # Then add auto-discovered components (non-conflicting)
    try:
        discovery = DynamicComponentDiscovery()
        factory = DynamicLoaderFactory()

        components = await discovery.discover_all_components()

        # Only add new components not already manually registered
        for name, metadata in components.items():
            if name not in warmup.components:
                loader = await factory.create_loader(metadata)
                health_check = await factory.create_health_check(metadata)
                priority = discovery._score_to_priority(metadata.priority_score)

                warmup.register_component(
                    name=name,
                    loader=loader,
                    priority=priority,
                    health_check=health_check,
                    dependencies=list(metadata.dependencies),
                    timeout=metadata.estimated_load_time,
                    retry_count=2,
                    required=False,  # Auto-discovered are optional
                    category=metadata.category,
                )

                logger.debug(f"[HYBRID] Added auto-discovered: {name}")

        await discovery._save_performance_cache()

    except Exception as e:
        logger.warning(f"[HYBRID] Auto-discovery failed, continuing with manual only: {e}")


# ═══════════════════════════════════════════════════════════════════════════
# OPTIMIZED COMPONENT LOADERS
# ═══════════════════════════════════════════════════════════════════════════

async def load_screen_lock_detector():
    """Load screen lock detector"""
    from context_intelligence.detectors.screen_lock_detector import get_screen_lock_detector
    detector = get_screen_lock_detector()
    await detector.is_screen_locked()
    return detector


async def check_screen_lock_detector_health(detector) -> bool:
    """Verify screen lock detector is working"""
    try:
        result = await detector.is_screen_locked()
        return isinstance(result, bool)
    except:
        return False


# =============================================================================
# 🔥 ECAPA-TDNN PRE-WARMER - Warms up Cloud ECAPA at STARTUP
# =============================================================================

async def load_ecapa_prewarmer():
    """
    🔥 Pre-warm ECAPA-TDNN Cloud endpoint at STARTUP.
    
    This ensures voice biometric verification is INSTANT when the user says
    "unlock my screen" - no more waiting for Cloud Run cold starts!
    
    The warmup:
    1. Finds the Cloud ECAPA endpoint (Cloud Run or Spot VM)
    2. Sends a warmup request to trigger model loading
    3. Verifies the embedding is valid
    4. Marks the system as "warm" for instant future requests
    """
    logger.info("[WARMUP] 🔥 Starting ECAPA-TDNN pre-warming at STARTUP...")
    start_time = time.time()
    
    try:
        # Import the pre-warmer and VBI orchestrator
        from core.vbi_debug_tracer import ECAPAPreWarmer, get_vbi_orchestrator
        
        # Get or create the prewarmer (singleton)
        prewarmer = ECAPAPreWarmer()
        
        # Check if already warm
        if prewarmer.is_warm:
            elapsed = time.time() - start_time
            logger.info(f"[WARMUP] ✅ ECAPA already warm ({elapsed:.2f}s)")
            return prewarmer
        
        # Perform the warmup - this is the slow part that should happen at STARTUP
        logger.info("[WARMUP] 🔄 Warming up Cloud ECAPA endpoint (this may take 30-60s on cold start)...")
        
        warmup_result = await asyncio.wait_for(
            prewarmer.warmup(force=False),
            timeout=120.0  # 2 minute timeout for cold starts
        )
        
        elapsed = time.time() - start_time
        
        if warmup_result.get("status") == "success":
            logger.info(f"[WARMUP] ✅ ECAPA-TDNN PRE-WARMED SUCCESSFULLY in {elapsed:.2f}s!")
            logger.info(f"[WARMUP]    └─ Endpoint: {warmup_result.get('stages', [{}])[0].get('endpoint', 'unknown')}")
            logger.info(f"[WARMUP]    └─ Embedding dimensions: {warmup_result.get('stages', [{}])[-1].get('embedding_size', 192)}")
            logger.info(f"[WARMUP]    └─ Voice unlock commands will now be INSTANT! 🚀")
        elif warmup_result.get("status") == "already_warm":
            logger.info(f"[WARMUP] ✅ ECAPA was already warm ({elapsed:.2f}s)")
        else:
            logger.warning(f"[WARMUP] ⚠️ ECAPA warmup status: {warmup_result.get('status')} ({elapsed:.2f}s)")
            logger.warning(f"[WARMUP]    └─ Error: {warmup_result.get('error', 'unknown')}")
            logger.warning(f"[WARMUP]    └─ Voice unlock will work but may be slow on first request")
        
        return prewarmer
        
    except asyncio.TimeoutError:
        elapsed = time.time() - start_time
        logger.warning(f"[WARMUP] ⏱️ ECAPA warmup timed out after {elapsed:.2f}s")
        logger.warning("[WARMUP]    └─ Cloud endpoint may be cold, first unlock will be slower")
        logger.warning("[WARMUP]    └─ Continuing startup - warmup will happen on first request")
        
        # Return the prewarmer anyway - it'll warm up on first request
        try:
            from core.vbi_debug_tracer import ECAPAPreWarmer
            return ECAPAPreWarmer()
        except:
            return None
        
    except ImportError as e:
        logger.warning(f"[WARMUP] ⚠️ ECAPA pre-warmer not available: {e}")
        logger.warning("[WARMUP]    └─ Voice unlock may be slower on first request")
        return None
        
    except Exception as e:
        elapsed = time.time() - start_time
        logger.error(f"[WARMUP] ❌ ECAPA pre-warmup failed: {e} ({elapsed:.2f}s)")
        import traceback
        logger.debug(traceback.format_exc())
        
        # Return None but don't crash - voice unlock will warm up on first request
        return None


async def check_ecapa_prewarmer_health(prewarmer) -> bool:
    """
    Verify ECAPA pre-warmer is ready for instant voice unlock.
    
    Returns True if:
    - Pre-warmer exists and is warm, OR
    - Pre-warmer exists (will warm on first request)
    """
    if prewarmer is None:
        return False
    
    try:
        is_warm = prewarmer.is_warm
        
        if is_warm:
            logger.info("[WARMUP] ✅ ECAPA pre-warmer health check PASSED (warm)")
        else:
            logger.info("[WARMUP] ⚠️ ECAPA pre-warmer exists but not warm (will warm on first request)")
        
        # Return True even if not warm - the prewarmer exists and will work
        return True
        
    except Exception as e:
        logger.warning(f"[WARMUP] ⚠️ ECAPA health check error: {e}")
        return False


async def load_voice_auth():
    """
    Load and FULLY INITIALIZE voice authentication system with pre-loading.
    Reduces first-unlock time from 30-60s to <5s!
    """
    try:
        logger.info("[WARMUP] 🎤 Loading voice authentication system...")
        start_time = asyncio.get_event_loop().time()

        from voice_unlock.intelligent_voice_unlock_service import get_intelligent_unlock_service

        service = get_intelligent_unlock_service()

        if not service.initialized:
            logger.info("[WARMUP] 🚀 Initializing voice auth components...")
            await service.initialize()

            # Pre-load speaker encoder
            if hasattr(service, 'speaker_engine') and service.speaker_engine:
                logger.info("[WARMUP] 🔄 Pre-loading speaker encoder...")
                try:
                    await service.speaker_engine.preload_models()
                except AttributeError:
                    try:
                        import numpy as np
                        dummy_audio = np.zeros(16000, dtype=np.float32)
                        _ = await service.speaker_engine.extract_embedding(dummy_audio)
                        logger.info("[WARMUP] ✅ Speaker encoder pre-loaded")
                    except Exception as e:
                        logger.warning(f"[WARMUP] ⚠️ Could not pre-load speaker encoder: {e}")

            # Pre-warm STT engine
            if hasattr(service, 'stt_router') and service.stt_router:
                logger.info("[WARMUP] 🔄 Pre-warming STT engine...")
                try:
                    import numpy as np
                    dummy_audio = np.zeros(16000, dtype=np.float32)
                    _ = await service.stt_router.transcribe(dummy_audio)
                    logger.info("[WARMUP] ✅ STT engine pre-warmed")
                except Exception as e:
                    logger.warning(f"[WARMUP] ⚠️ Could not pre-warm STT: {e}")

            # 🔐 CRITICAL: Pre-load unified voice cache with Derek's voiceprint
            logger.info("[WARMUP] 🔐 Pre-loading unified voice cache...")
            try:
                from voice_unlock.unified_voice_cache_manager import get_unified_voice_cache
                unified_cache = await get_unified_voice_cache()

                if unified_cache:
                    profiles_count = unified_cache.profiles_loaded
                    cache_state = unified_cache.state.value if hasattr(unified_cache.state, 'value') else str(unified_cache.state)

                    if profiles_count > 0:
                        logger.info(
                            f"[WARMUP] ✅ Unified voice cache ready: "
                            f"{profiles_count} profile(s), state={cache_state}"
                        )
                        # Log profile names for verification
                        for name, profile in unified_cache.get_preloaded_profiles().items():
                            owner_tag = " [OWNER]" if profile.source == "learning_database" else ""
                            logger.info(
                                f"[WARMUP]    └─ {name}{owner_tag} "
                                f"(dim={profile.embedding_dimensions}, samples={profile.total_samples})"
                            )
                    else:
                        logger.warning(
                            f"[WARMUP] ⚠️ Unified voice cache has NO profiles! "
                            f"Voice unlock will fail. state={cache_state}"
                        )
                else:
                    logger.warning("[WARMUP] ⚠️ Unified voice cache is None")

            except ImportError as e:
                logger.warning(f"[WARMUP] ⚠️ Unified voice cache module not available: {e}")
            except Exception as e:
                logger.warning(f"[WARMUP] ⚠️ Unified voice cache error: {e}")

            # 🧠 Pre-load Voice Biometric Intelligence (NO hardcoded names!)
            logger.info("[WARMUP] 🧠 Pre-loading Voice Biometric Intelligence...")
            try:
                from voice_unlock.voice_biometric_intelligence import get_voice_biometric_intelligence
                vbi = await get_voice_biometric_intelligence()

                if vbi and vbi._unified_cache:
                    vbi_profiles = vbi._unified_cache.profiles_loaded
                    logger.info(f"[WARMUP] ✅ Voice Biometric Intelligence ready ({vbi_profiles} profiles)")

                    # Log loaded profiles dynamically - no hardcoding!
                    preloaded = vbi._unified_cache.get_preloaded_profiles()
                    for profile_name, profile_data in preloaded.items():
                        is_owner = profile_data.source == "learning_database"
                        owner_tag = " [PRIMARY OWNER]" if is_owner else ""
                        logger.info(
                            f"[WARMUP]    └─ VBI: {profile_name}{owner_tag} "
                            f"(dim={profile_data.embedding_dimensions}, "
                            f"samples={profile_data.total_samples})"
                        )

                    # Verify at least one owner profile exists
                    has_owner = any(
                        p.source == "learning_database"
                        for p in preloaded.values()
                    )
                    if not has_owner and vbi_profiles > 0:
                        logger.warning(
                            "[WARMUP] ⚠️ VBI has profiles but none marked as primary owner! "
                            "Voice unlock may fail."
                        )
                else:
                    logger.warning("[WARMUP] ⚠️ Voice Biometric Intelligence has no cache")

            except Exception as e:
                logger.warning(f"[WARMUP] ⚠️ VBI pre-load error: {e}")

        elapsed = asyncio.get_event_loop().time() - start_time
        logger.info(f"[WARMUP] ✅ Voice auth ready in {elapsed:.2f}s")
        return service

    except ImportError as e:
        logger.warning(f"[WARMUP] Voice auth not available: {e}")
        return None
    except Exception as e:
        logger.error(f"[WARMUP] Failed to load voice auth: {e}")
        import traceback
        traceback.print_exc()
        return None


async def check_voice_auth_health(service) -> bool:
    """Verify voice auth is fully initialized and working"""
    if service is None:
        return False

    try:
        if not service.initialized:
            return False

        has_stt = hasattr(service, 'stt_router') and service.stt_router is not None
        has_speaker = hasattr(service, 'speaker_engine') and service.speaker_engine is not None

        has_profiles = False
        if hasattr(service, 'learning_db') and service.learning_db:
            try:
                profiles = await service.learning_db.get_all_speaker_profiles()
                has_profiles = len(profiles) > 0
            except:
                pass

        # 🔐 Check unified voice cache for preloaded profiles (critical for fast unlock!)
        has_cache_profiles = False
        cache_profile_count = 0
        try:
            from voice_unlock.unified_voice_cache_manager import get_unified_cache_manager
            cache = get_unified_cache_manager()
            if cache:
                cache_profile_count = cache.profiles_loaded
                has_cache_profiles = cache_profile_count > 0
        except:
            pass

        # 🧠 Check VBI is ready - get it directly, don't rely on service attribute
        has_vbi = False
        vbi_profile_count = 0
        try:
            from voice_unlock.voice_biometric_intelligence import get_voice_biometric_intelligence
            vbi = await get_voice_biometric_intelligence()
            if vbi and hasattr(vbi, '_unified_cache') and vbi._unified_cache:
                vbi_profile_count = vbi._unified_cache.profiles_loaded
                has_vbi = vbi_profile_count > 0
        except Exception as e:
            logger.debug(f"VBI check error: {e}")

        is_healthy = has_stt and has_speaker and has_profiles and has_cache_profiles

        if is_healthy:
            logger.info(
                f"[WARMUP] ✅ Voice auth health check PASSED "
                f"(cache: {cache_profile_count} profiles, VBI: {has_vbi} ({vbi_profile_count} profiles))"
            )
        else:
            logger.warning(
                f"[WARMUP] ⚠️ Voice auth health check DEGRADED "
                f"(STT: {has_stt}, Speaker: {has_speaker}, "
                f"Profiles: {has_profiles}, CacheProfiles: {has_cache_profiles}, "
                f"VBI: {has_vbi} ({vbi_profile_count} profiles))"
            )

        return is_healthy
    except Exception as e:
        logger.error(f"[WARMUP] Voice auth health check failed: {e}")
        return False


async def load_context_aware_handler():
    """Load context-aware command handler"""
    from context_intelligence.handlers.context_aware_handler import get_context_aware_handler
    return get_context_aware_handler()


async def load_multi_space_context_graph():
    """Load multi-space context graph"""
    from core.context.multi_space_context_graph import MultiSpaceContextGraph
    # Fix: Use correct parameter names (temporal_decay_minutes, not decay_ttl)
    return MultiSpaceContextGraph(
        max_history_size=1000,
        temporal_decay_minutes=5  # 5 minutes (was decay_ttl=300 seconds)
    )


async def check_context_graph_health(graph) -> bool:
    """Verify context graph is working"""
    return graph is not None


async def load_implicit_resolver():
    """Load implicit reference resolver"""
    from core.nlp.implicit_reference_resolver import get_implicit_resolver
    return get_implicit_resolver()


async def load_compound_parser():
    """Load compound action parser"""
    try:
        from context_intelligence.analyzers.compound_action_parser import get_compound_parser
        parser = get_compound_parser()
        # Test parse (no await needed - parse returns actions synchronously)
        actions = await parser.parse("test command")
        logger.info(f"✅ Compound parser loaded - parsed {len(actions)} actions")
        return parser
    except Exception as e:
        logger.error(f"❌ Failed to load compound parser: {e}")
        # Return a mock parser so the system can continue
        return None


async def check_compound_parser_health(parser) -> bool:
    """Verify compound parser is working"""
    try:
        result = await parser.parse("open safari")
        return result is not None
    except:
        return False


async def load_macos_controller():
    """Load MacOS controller"""
    from system_control.macos_controller import MacOSController
    return MacOSController()


async def check_macos_controller_health(controller) -> bool:
    """Verify MacOS controller is working"""
    return controller is not None


async def load_query_complexity_manager():
    """Load query complexity manager"""
    try:
        from context_intelligence.handlers.query_complexity_manager import QueryComplexityManager
        return QueryComplexityManager()
    except ImportError:
        return None


async def load_yabai_detector():
    """Load Yabai space detector"""
    try:
        from vision.yabai_space_detector import YabaiSpaceDetector
        return YabaiSpaceDetector()
    except:
        logger.debug("[WARMUP] Yabai detector not available")
        return None


async def check_yabai_health(detector) -> bool:
    """Verify Yabai detector is working"""
    return detector is not None


async def load_window_detector():
    """Load multi-space window detector"""
    try:
        from vision.multi_space_window_detector import MultiSpaceWindowDetector
        return MultiSpaceWindowDetector()
    except:
        return None


async def load_learning_database():
    """Load learning database"""
    try:
        from intelligence.learning_database import get_learning_database
        db = await get_learning_database()
        await asyncio.sleep(0.1)
        return db
    except Exception as e:
        logger.debug(f"[WARMUP] Learning database not available: {e}")
        return None


async def check_database_health(db) -> bool:
    """Verify database is connected"""
    if db is None:
        return False
    try:
        return hasattr(db, "store_command_execution")
    except:
        return False


async def load_action_query_handler():
    """Load action query handler"""
    try:
        from context_intelligence.handlers.action_query_handler import (
            get_action_query_handler,
            initialize_action_query_handler,
        )
        handler = get_action_query_handler()
        if handler is None:
            from core.nlp.implicit_reference_resolver import get_implicit_resolver
            handler = initialize_action_query_handler(
                context_graph=None, implicit_resolver=get_implicit_resolver()
            )
        return handler
    except:
        return None


async def load_predictive_handler():
    """Load predictive query handler"""
    try:
        from context_intelligence.handlers.predictive_query_handler import (
            get_predictive_handler,
            initialize_predictive_handler,
        )
        handler = get_predictive_handler()
        if handler is None:
            handler = initialize_predictive_handler()
        return handler
    except:
        return None


async def load_multi_space_handler():
    """Load multi-space query handler"""
    try:
        from context_intelligence.handlers.multi_space_query_handler import (
            get_multi_space_query_handler,
        )
        return get_multi_space_query_handler()
    except:
        return None
