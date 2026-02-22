"""
Unified Command Processor - Dynamic command interpretation with zero hardcoding
Learns from the system and adapts to any environment

v88.0: Ultra Protection Integration
- Adaptive circuit breaker with ML-based prediction
- Backpressure handling with AIMD rate limiting
- W3C distributed tracing
- Timeout enforcement
"""

import asyncio
import json
import logging
import os
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# =============================================================================
# v88.0: ULTRA COORDINATOR INTEGRATION
# =============================================================================

# v88.0: Module-level ultra coordinator for protection
_ultra_coordinator: Optional[Any] = None
_ultra_coord_lock: Optional[asyncio.Lock] = None


async def _get_ultra_coordinator() -> Optional[Any]:
    """v88.0: Get ultra coordinator with lazy initialization."""
    global _ultra_coordinator, _ultra_coord_lock

    # Skip if disabled
    if os.getenv("JARVIS_ENABLE_ULTRA_COORD", "true").lower() not in ("true", "1", "yes"):
        return None

    if _ultra_coordinator is not None:
        return _ultra_coordinator

    # Lazy init lock
    if _ultra_coord_lock is None:
        _ultra_coord_lock = asyncio.Lock()

    async with _ultra_coord_lock:
        if _ultra_coordinator is not None:
            return _ultra_coordinator

        try:
            from backend.core.trinity_integrator import get_ultra_coordinator
            _ultra_coordinator = await get_ultra_coordinator()
            logger.info("[UnifiedProcessor] v88.0 Ultra coordinator initialized")
            return _ultra_coordinator
        except Exception as e:
            logger.debug(f"[UnifiedProcessor] v88.0 Ultra coordinator not available: {e}")
            return None

# Import manual unlock handler
try:
    from api.manual_unlock_handler import handle_manual_unlock
except ImportError:
    handle_manual_unlock = None
    logger.warning("Manual unlock handler not available")

# Import Intelligent Vision Router
try:
    from vision.intelligent_vision_router import IntelligentVisionRouter

    INTELLIGENT_ROUTER_AVAILABLE = True
    logger.info("[UNIFIED] ✅ Intelligent Vision Router available")
except ImportError as e:
    INTELLIGENT_ROUTER_AVAILABLE = False
    logger.warning(f"[UNIFIED] Intelligent Vision Router not available: {e}")


class DynamicPatternLearner:
    """Learns command patterns from usage and system analysis"""

    def __init__(self):
        self.learned_patterns = defaultdict(list)
        self.app_verbs = set()
        self.system_verbs = set()
        self.query_indicators = set()
        self.learned_apps = set()
        self.pattern_confidence = defaultdict(float)
        self._initialize_base_patterns()
        self._learn_from_system()

    def _initialize_base_patterns(self):
        """Initialize with minimal base patterns that will be expanded"""
        # These are just seeds - the system will learn more
        self.app_verbs = {"open", "close", "launch", "quit", "start", "kill"}
        self.system_verbs = {"set", "adjust", "toggle", "take", "enable", "disable", "search", "find", "google"}
        self.query_indicators = {
            "what",
            "who",
            "where",
            "when",
            "why",
            "how",
            "is",
            "are",
            "can",
        }

    def _learn_from_system(self):
        """Learn available applications and commands from the system"""
        try:
            # Dynamically discover installed applications
            from system_control.dynamic_app_controller import get_dynamic_app_controller

            controller = get_dynamic_app_controller()

            # Learn all installed apps
            if hasattr(controller, "installed_apps_cache"):
                for app_key, app_info in controller.installed_apps_cache.items():
                    self.learned_apps.add(app_info["name"].lower())
                    # Also learn variations
                    self.learned_apps.add(app_key.lower())

            logger.info(f"Learned {len(self.learned_apps)} applications from system")

        except Exception as e:
            logger.debug(f"Could not learn from system controller: {e}")

    def learn_pattern(self, command: str, command_type: str, success: bool):
        """Learn from command execution results"""
        words = command.lower().split()
        if success and len(words) > 0:
            # Learn verb patterns
            first_word = words[0]
            if command_type == "system" and first_word not in self.system_verbs:
                self.system_verbs.add(first_word)
                self.pattern_confidence[f"verb_{first_word}"] += 0.1

            # Learn app names from successful commands
            if command_type == "system" and any(verb in words for verb in self.app_verbs):
                # Extract potential app names
                for i, word in enumerate(words):
                    if word in self.app_verbs and i + 1 < len(words):
                        potential_app = words[i + 1]
                        if (
                            potential_app not in self.app_verbs
                            and potential_app not in self.system_verbs
                        ):
                            self.learned_apps.add(potential_app)

    def is_learned_app(self, word: str) -> bool:
        """Check if a word is a learned app name"""
        return word.lower() in self.learned_apps

    def get_command_patterns(self, command_type: str) -> List[str]:
        """Get learned patterns for a command type"""
        return self.learned_patterns.get(command_type, [])


class CommandType(Enum):
    """Types of commands JARVIS can handle"""

    VISION = "vision"
    SYSTEM = "system"
    WEATHER = "weather"
    COMMUNICATION = "communication"
    AUTONOMY = "autonomy"
    QUERY = "query"
    COMPOUND = "compound"
    META = "meta"
    VOICE_UNLOCK = "voice_unlock"  # Unlock screen (requires voice verification)
    SCREEN_LOCK = "screen_lock"    # Lock screen (no verification needed, but owner recognition)
    DOCUMENT = "document"
    DISPLAY = "display"
    UNKNOWN = "unknown"


@dataclass
class UnifiedContext:
    """Single context shared across all command processing"""

    conversation_history: List[Dict[str, Any]]
    current_visual: Optional[Dict[str, Any]] = None
    last_entity: Optional[Dict[str, Any]] = None  # For "it/that" resolution
    active_monitoring: bool = False
    user_preferences: Optional[Dict[str, Any]] = None
    system_state: Optional[Dict[str, Any]] = None

    def __post_init__(self):
        if self.user_preferences is None:
            self.user_preferences = {}
        if self.system_state is None:
            self.system_state = {}

    def resolve_reference(self, text: str) -> Tuple[Optional[str], float]:
        """Resolve 'it', 'that', 'this' to actual entities"""
        reference_words = ["it", "that", "this", "them"]

        for word in reference_words:
            if word in text.lower():
                if self.last_entity:
                    # Check how recent the entity is
                    if "timestamp" in self.last_entity:
                        age = (datetime.now() - self.last_entity["timestamp"]).seconds
                        confidence = 0.9 if age < 30 else 0.7 if age < 60 else 0.5
                    else:
                        confidence = 0.8
                    return self.last_entity.get("value", ""), confidence

                # Check visual context
                if self.current_visual:
                    return self.current_visual.get("focused_element", ""), 0.7

        return None, 0.0

    def update_from_command(self, command_type: CommandType, result: Dict[str, Any]):
        """Update context based on command execution"""
        self.conversation_history.append(
            {"type": command_type.value, "result": result, "timestamp": datetime.now()}
        )

        # Extract entities for future reference
        if command_type == CommandType.VISION and "elements" in result:
            if result["elements"]:
                self.last_entity = {
                    "value": result["elements"][0],
                    "timestamp": datetime.now(),
                    "type": "visual_element",
                }

        # Update visual context
        if command_type == CommandType.VISION:
            self.current_visual = result.get("visual_context", {})


class UnifiedCommandProcessor:
    """Dynamic command processor that learns and adapts"""

    def __init__(self, claude_api_key: Optional[str] = None, app=None):
        self.context = UnifiedContext(conversation_history=[])
        self.handlers = {}
        self.pattern_learner = DynamicPatternLearner()
        self.command_stats = defaultdict(int)
        self.success_patterns = defaultdict(list)
        self._initialize_handlers()
        self.claude_api_key = claude_api_key
        self._app = app  # Store app reference for accessing app.state
        self._load_learned_data()

        # Initialize multi-space context graph for advanced context tracking
        self.context_graph = None

        # Initialize resolver systems
        self.contextual_resolver = None  # Space/monitor resolution
        self.implicit_resolver = None  # Entity/intent resolution
        self.multi_space_handler = None  # Multi-space query handler
        self.temporal_handler = (
            None  # Temporal query handler (change detection, error tracking, timeline)
        )
        self.query_complexity_manager = None  # Query complexity classification and routing
        self.medium_complexity_handler = None  # Medium complexity (Level 2) query execution
        self.display_reference_handler = None  # Display voice command resolution
        self.goal_autonomous_integration = None  # Goal inference + autonomous decision engine
        self.response_strategy_manager = None  # Response strategy optimization
        self.context_aware_manager = None  # Context-aware response management
        self.proactive_suggestion_manager = None  # Proactive suggestions
        self.confidence_manager = None  # Confidence scoring
        self.multi_monitor_manager = None  # Multi-monitor management
        self.multi_monitor_query_handler = None  # Multi-monitor query handling
        self.change_detection_manager = None  # Change detection management
        self.proactive_monitoring_manager = None  # Proactive monitoring
        self._resolvers_initialized = False

        # Initialize Intelligent Vision Router (YOLO + LLaMA + Claude intelligent routing)
        self.vision_router = None
        self._vision_router_initialized = False

        # Initialize Speaker Verification Service (voice biometric authentication)
        self.speaker_verification = None
        self.message_generator = None
        self._speaker_verification_initialized = False

        # v242.1: Reflex manifest cache with content-hash invalidation
        self._reflex_manifest_cache: Optional[dict] = None
        self._reflex_manifest_hash: str = ""
        self._reflex_inhibition_cache: Optional[dict] = None
        self._reflex_inhibition_hash: str = ""
        self._reflex_cache_checked_at: float = 0.0
        self._REFLEX_CACHE_CHECK_INTERVAL: float = float(
            os.getenv("JARVIS_REFLEX_CACHE_INTERVAL", "5.0")
        )

        # v242.1: Observability counters for routing diagnostics
        self._v242_metrics = {
            "reflex_hits": 0,
            "reflex_cache_hits": 0,
            "jprime_calls": 0,
            "jprime_timeouts": 0,
            "jprime_503_retries": 0,
            "brain_vacuum_activations": 0,
            "vision_requests": 0,
            "workspace_requests": 0,
            "classifications": {},  # domain -> count
        }

    async def _initialize_resolvers(self):
        """
        Initialize resolver systems using robust parallel tiered initialization.

        Architecture:
        - Tier 1: Independent components (no dependencies) - run in parallel
        - Tier 2: Components depending on Tier 1 - run in parallel after Tier 1
        - Tier 3: Components depending on Tier 2 - run in parallel after Tier 2
        - Tier 4: Complex handlers depending on Tier 3 - run in parallel after Tier 3
        - Tier 5: Vision router and speaker verification - background optional

        Features:
        - Per-component 3-5 second timeouts (not one global timeout)
        - asyncio.to_thread for blocking imports
        - asyncio.gather with return_exceptions=True for fault tolerance
        - Graceful degradation - failures don't stop other components
        - Smart dependency resolution
        """
        import time
        start_time = time.time()

        logger.info("[UNIFIED] 🚀 Starting PARALLEL resolver initialization v2.0")

        # Track initialization results
        init_results = {}

        # =======================================================================
        # TIER 1: Independent Components (no dependencies) - PARALLEL
        # Uses asyncio.to_thread() for blocking imports to achieve TRUE parallelism
        # =======================================================================
        def _sync_init_context_graph():
            """Synchronous init for MultiSpaceContextGraph"""
            from core.context.multi_space_context_graph import MultiSpaceContextGraph
            return MultiSpaceContextGraph()

        async def init_context_graph():
            """Initialize MultiSpaceContextGraph"""
            try:
                self.context_graph = await asyncio.to_thread(_sync_init_context_graph)
                return ("context_graph", True, "MultiSpaceContextGraph")
            except Exception as e:
                logger.warning(f"[UNIFIED] MultiSpaceContextGraph not available: {e}")
                return ("context_graph", False, str(e))

        def _sync_init_capture_strategy_manager():
            """Synchronous init for CaptureStrategyManager"""
            from context_intelligence.managers import (
                initialize_capture_strategy_manager,
                get_capture_strategy_manager,
            )
            if get_capture_strategy_manager() is None:
                initialize_capture_strategy_manager(
                    cache_ttl=60.0,
                    max_cache_entries=100,
                    enable_error_matrix=True,
                )
            return True

        async def init_capture_strategy_manager():
            """Initialize CaptureStrategyManager"""
            try:
                await asyncio.to_thread(_sync_init_capture_strategy_manager)
                return ("capture_strategy_manager", True, "CaptureStrategyManager")
            except Exception as e:
                logger.warning(f"[UNIFIED] CaptureStrategyManager not available: {e}")
                return ("capture_strategy_manager", False, str(e))

        def _sync_init_ocr_strategy_manager():
            """Synchronous init for OCRStrategyManager"""
            import os
            from context_intelligence.managers import (
                initialize_ocr_strategy_manager,
                get_ocr_strategy_manager,
            )
            if get_ocr_strategy_manager() is None:
                anthropic_api_key = os.getenv("ANTHROPIC_API_KEY")
                initialize_ocr_strategy_manager(
                    cache_ttl=300.0,
                    max_cache_entries=200,
                    enable_error_matrix=True,
                    anthropic_api_key=anthropic_api_key,
                )
            return True

        async def init_ocr_strategy_manager():
            """Initialize OCRStrategyManager"""
            try:
                await asyncio.to_thread(_sync_init_ocr_strategy_manager)
                return ("ocr_strategy_manager", True, "OCRStrategyManager")
            except Exception as e:
                logger.warning(f"[UNIFIED] OCRStrategyManager not available: {e}")
                return ("ocr_strategy_manager", False, str(e))

        def _sync_init_contextual_resolver():
            """Synchronous init for ContextualQueryResolver"""
            from context_intelligence.resolvers import get_contextual_resolver
            return get_contextual_resolver()

        async def init_contextual_resolver():
            """Initialize ContextualQueryResolver"""
            try:
                self.contextual_resolver = await asyncio.to_thread(_sync_init_contextual_resolver)
                return ("contextual_resolver", True, "ContextualQueryResolver")
            except Exception as e:
                logger.warning(f"[UNIFIED] ContextualQueryResolver not available: {e}")
                return ("contextual_resolver", False, str(e))

        def _sync_init_response_strategy_manager():
            """Synchronous init for ResponseStrategyManager"""
            from context_intelligence.managers import (
                ResponseQuality,
                initialize_response_strategy_manager,
            )
            return initialize_response_strategy_manager(
                vision_client=None,
                min_quality=ResponseQuality.SPECIFIC,
            )

        async def init_response_strategy_manager():
            """Initialize ResponseStrategyManager"""
            try:
                self.response_strategy_manager = await asyncio.to_thread(_sync_init_response_strategy_manager)
                return ("response_strategy_manager", True, "ResponseStrategyManager")
            except Exception as e:
                logger.warning(f"[UNIFIED] ResponseStrategyManager not available: {e}")
                return ("response_strategy_manager", False, str(e))

        def _sync_init_confidence_manager():
            """Synchronous init for ConfidenceManager"""
            from context_intelligence.managers import initialize_confidence_manager
            return initialize_confidence_manager(
                include_visual_indicators=True,
                include_reasoning=True,
                min_confidence_for_high=0.8,
                min_confidence_for_medium=0.5,
            )

        async def init_confidence_manager():
            """Initialize ConfidenceManager"""
            try:
                self.confidence_manager = await asyncio.to_thread(_sync_init_confidence_manager)
                return ("confidence_manager", True, "ConfidenceManager")
            except Exception as e:
                logger.warning(f"[UNIFIED] ConfidenceManager not available: {e}")
                return ("confidence_manager", False, str(e))

        def _sync_init_yabai_detector():
            """Synchronous init for YabaiSpaceDetector"""
            from vision.yabai_space_detector import YabaiSpaceDetector
            return YabaiSpaceDetector()

        async def init_yabai_detector():
            """Initialize YabaiSpaceDetector"""
            try:
                self._yabai_detector = await asyncio.to_thread(_sync_init_yabai_detector)
                return ("yabai_detector", True, "YabaiSpaceDetector")
            except Exception as e:
                logger.warning(f"[UNIFIED] YabaiSpaceDetector not available: {e}")
                self._yabai_detector = None
                return ("yabai_detector", False, str(e))

        def _sync_init_cg_window_detector():
            """Synchronous init for MultiSpaceWindowDetector"""
            from vision.multi_space_window_detector import MultiSpaceWindowDetector
            return MultiSpaceWindowDetector()

        async def init_cg_window_detector():
            """Initialize MultiSpaceWindowDetector"""
            try:
                self._cg_window_detector = await asyncio.to_thread(_sync_init_cg_window_detector)
                return ("cg_window_detector", True, "MultiSpaceWindowDetector")
            except Exception as e:
                logger.warning(f"[UNIFIED] MultiSpaceWindowDetector not available: {e}")
                self._cg_window_detector = None
                return ("cg_window_detector", False, str(e))

        async def init_learning_database():
            """Initialize Learning Database"""
            try:
                from intelligence.learning_database import get_learning_database
                self._learning_db = await asyncio.wait_for(
                    get_learning_database(),
                    timeout=5.0
                )
                return ("learning_database", True, "LearningDatabase")
            except asyncio.TimeoutError:
                logger.warning("[UNIFIED] LearningDatabase timed out (5s)")
                self._learning_db = None
                return ("learning_database", False, "timeout")
            except Exception as e:
                logger.warning(f"[UNIFIED] LearningDatabase not available: {e}")
                self._learning_db = None
                return ("learning_database", False, str(e))

        # Execute Tier 1 in parallel with per-component timeouts
        logger.info("[UNIFIED] 📦 Tier 1: Initializing independent components...")
        tier1_tasks = [
            asyncio.wait_for(init_context_graph(), timeout=3.0),
            asyncio.wait_for(init_capture_strategy_manager(), timeout=3.0),
            asyncio.wait_for(init_ocr_strategy_manager(), timeout=3.0),
            asyncio.wait_for(init_contextual_resolver(), timeout=3.0),
            asyncio.wait_for(init_response_strategy_manager(), timeout=3.0),
            asyncio.wait_for(init_confidence_manager(), timeout=3.0),
            asyncio.wait_for(init_yabai_detector(), timeout=3.0),
            asyncio.wait_for(init_cg_window_detector(), timeout=3.0),
            init_learning_database(),  # Has its own timeout
        ]

        tier1_results = await asyncio.gather(*tier1_tasks, return_exceptions=True)

        # Process Tier 1 results
        tier1_success = 0
        for result in tier1_results:
            if isinstance(result, Exception):
                logger.warning(f"[UNIFIED] Tier 1 component failed: {result}")
            elif isinstance(result, tuple) and len(result) == 3:
                name, success, desc = result
                init_results[name] = success
                if success:
                    tier1_success += 1
                    logger.info(f"[UNIFIED] ✅ {desc}")

        tier1_time = time.time() - start_time
        logger.info(f"[UNIFIED] Tier 1 complete: {tier1_success}/{len(tier1_tasks)} in {tier1_time:.2f}s")

        # =======================================================================
        # TIER 2: Components depending on Tier 1 - PARALLEL
        # =======================================================================
        async def init_implicit_resolver():
            """Initialize ImplicitReferenceResolver (requires context_graph).

            v259.1: Fixed — was importing non-existent `initialize_implicit_resolver()`.
            The correct API is `get_implicit_resolver()` which is a lazy singleton
            that internally obtains context_graph from `get_multi_space_context_graph()`.
            """
            if not self.context_graph:
                return ("implicit_resolver", False, "no context_graph")
            try:
                from core.nlp.implicit_reference_resolver import get_implicit_resolver
                self.implicit_resolver = await asyncio.to_thread(get_implicit_resolver)
                return ("implicit_resolver", True, "ImplicitReferenceResolver")
            except Exception as e:
                logger.warning(f"[UNIFIED] ImplicitReferenceResolver not available: {e}")
                return ("implicit_resolver", False, str(e))

        async def init_query_complexity_manager():
            """Initialize QueryComplexityManager"""
            try:
                from context_intelligence.handlers import initialize_query_complexity_manager
                self.query_complexity_manager = initialize_query_complexity_manager(
                    implicit_resolver=self.implicit_resolver  # May be None, that's OK
                )
                return ("query_complexity_manager", True, "QueryComplexityManager")
            except Exception as e:
                logger.warning(f"[UNIFIED] QueryComplexityManager not available: {e}")
                return ("query_complexity_manager", False, str(e))

        async def init_goal_autonomous_integration():
            """Initialize Goal Inference + Autonomous Decision Integration"""
            try:
                from backend.intelligence.goal_autonomous_uae_integration import get_integration
                self.goal_autonomous_integration = get_integration()
                return ("goal_autonomous_integration", True, "GoalAutonomousIntegration")
            except Exception as e:
                logger.warning(f"[UNIFIED] GoalAutonomousIntegration not available: {e}")
                return ("goal_autonomous_integration", False, str(e))

        # Execute Tier 2 in parallel
        logger.info("[UNIFIED] 📦 Tier 2: Initializing dependent components...")
        tier2_start = time.time()
        tier2_tasks = [
            asyncio.wait_for(init_implicit_resolver(), timeout=3.0),
            asyncio.wait_for(init_query_complexity_manager(), timeout=3.0),
            asyncio.wait_for(init_goal_autonomous_integration(), timeout=3.0),
        ]

        tier2_results = await asyncio.gather(*tier2_tasks, return_exceptions=True)

        tier2_success = 0
        for result in tier2_results:
            if isinstance(result, Exception):
                logger.warning(f"[UNIFIED] Tier 2 component failed: {result}")
            elif isinstance(result, tuple) and len(result) == 3:
                name, success, desc = result
                init_results[name] = success
                if success:
                    tier2_success += 1
                    logger.info(f"[UNIFIED] ✅ {desc}")

        tier2_time = time.time() - tier2_start
        logger.info(f"[UNIFIED] Tier 2 complete: {tier2_success}/{len(tier2_tasks)} in {tier2_time:.2f}s")

        # =======================================================================
        # TIER 3: Managers depending on implicit_resolver - PARALLEL
        # =======================================================================
        async def init_context_aware_manager():
            """Initialize ContextAwareResponseManager"""
            try:
                from context_intelligence.managers import initialize_context_aware_response_manager
                self.context_aware_manager = initialize_context_aware_response_manager(
                    implicit_resolver=self.implicit_resolver,
                    max_history=10,
                    context_ttl=300.0,
                )
                return ("context_aware_manager", True, "ContextAwareResponseManager")
            except Exception as e:
                logger.warning(f"[UNIFIED] ContextAwareResponseManager not available: {e}")
                return ("context_aware_manager", False, str(e))

        async def init_multi_monitor_manager():
            """Initialize MultiMonitorManager"""
            try:
                from context_intelligence.managers import initialize_multi_monitor_manager
                conversation_tracker = None
                if self.context_aware_manager:
                    conversation_tracker = self.context_aware_manager.conversation_tracker
                self.multi_monitor_manager = initialize_multi_monitor_manager(
                    implicit_resolver=self.implicit_resolver,
                    conversation_tracker=conversation_tracker,
                    auto_refresh_interval=30.0,
                )
                return ("multi_monitor_manager", True, "MultiMonitorManager")
            except Exception as e:
                logger.warning(f"[UNIFIED] MultiMonitorManager not available: {e}")
                return ("multi_monitor_manager", False, str(e))

        async def init_change_detection_manager():
            """Initialize ChangeDetectionManager"""
            try:
                from pathlib import Path
                from context_intelligence.managers import initialize_change_detection_manager
                conversation_tracker = None
                if self.context_aware_manager:
                    conversation_tracker = self.context_aware_manager.conversation_tracker
                self.change_detection_manager = initialize_change_detection_manager(
                    cache_dir=Path.home() / ".jarvis" / "change_cache",
                    cache_ttl=3600.0,
                    max_cache_size=100,
                    implicit_resolver=self.implicit_resolver,
                    conversation_tracker=conversation_tracker,
                )
                return ("change_detection_manager", True, "ChangeDetectionManager")
            except Exception as e:
                logger.warning(f"[UNIFIED] ChangeDetectionManager not available: {e}")
                return ("change_detection_manager", False, str(e))

        async def init_proactive_suggestion_manager():
            """Initialize ProactiveSuggestionManager"""
            try:
                from context_intelligence.managers import initialize_proactive_suggestion_manager
                conversation_tracker = None
                if self.context_aware_manager:
                    conversation_tracker = self.context_aware_manager.conversation_tracker
                self.proactive_suggestion_manager = initialize_proactive_suggestion_manager(
                    conversation_tracker=conversation_tracker,
                    implicit_resolver=self.implicit_resolver,
                    max_suggestions=2,
                    confidence_threshold=0.5,
                )
                return ("proactive_suggestion_manager", True, "ProactiveSuggestionManager")
            except Exception as e:
                logger.warning(f"[UNIFIED] ProactiveSuggestionManager not available: {e}")
                return ("proactive_suggestion_manager", False, str(e))

        async def init_multi_space_handler():
            """Initialize MultiSpaceQueryHandler"""
            if not self.context_graph:
                self.multi_space_handler = None
                return ("multi_space_handler", False, "no context_graph")
            try:
                from context_intelligence.handlers import initialize_multi_space_handler
                self.multi_space_handler = initialize_multi_space_handler(
                    context_graph=self.context_graph,
                    implicit_resolver=self.implicit_resolver,
                    contextual_resolver=self.contextual_resolver,
                    learning_db=getattr(self, '_learning_db', None),
                    yabai_detector=getattr(self, '_yabai_detector', None),
                    cg_window_detector=getattr(self, '_cg_window_detector', None),
                )
                return ("multi_space_handler", True, "MultiSpaceQueryHandler")
            except Exception as e:
                logger.warning(f"[UNIFIED] MultiSpaceQueryHandler not available: {e}")
                return ("multi_space_handler", False, str(e))

        # Execute Tier 3 in parallel
        logger.info("[UNIFIED] 📦 Tier 3: Initializing manager components...")
        tier3_start = time.time()

        # First init context_aware_manager as others depend on conversation_tracker
        await asyncio.wait_for(init_context_aware_manager(), timeout=3.0)

        # Now init the rest in parallel
        tier3_tasks = [
            asyncio.wait_for(init_multi_monitor_manager(), timeout=3.0),
            asyncio.wait_for(init_change_detection_manager(), timeout=3.0),
            asyncio.wait_for(init_proactive_suggestion_manager(), timeout=3.0),
            asyncio.wait_for(init_multi_space_handler(), timeout=3.0),
        ]

        tier3_results = await asyncio.gather(*tier3_tasks, return_exceptions=True)

        tier3_success = 1 if self.context_aware_manager else 0  # Count context_aware_manager
        for result in tier3_results:
            if isinstance(result, Exception):
                logger.warning(f"[UNIFIED] Tier 3 component failed: {result}")
            elif isinstance(result, tuple) and len(result) == 3:
                name, success, desc = result
                init_results[name] = success
                if success:
                    tier3_success += 1
                    logger.info(f"[UNIFIED] ✅ {desc}")

        tier3_time = time.time() - tier3_start
        logger.info(f"[UNIFIED] Tier 3 complete: {tier3_success}/5 in {tier3_time:.2f}s")

        # =======================================================================
        # TIER 4: Handlers depending on Tier 3 - PARALLEL
        # =======================================================================
        async def init_multi_monitor_query_handler():
            """Initialize MultiMonitorQueryHandler"""
            try:
                from context_intelligence.handlers import initialize_multi_monitor_query_handler
                from context_intelligence.managers import (
                    get_capture_strategy_manager,
                    get_ocr_strategy_manager,
                )
                self.multi_monitor_query_handler = initialize_multi_monitor_query_handler(
                    multi_monitor_manager=self.multi_monitor_manager,
                    capture_manager=get_capture_strategy_manager(),
                    ocr_manager=get_ocr_strategy_manager(),
                    implicit_resolver=self.implicit_resolver,
                )
                return ("multi_monitor_query_handler", True, "MultiMonitorQueryHandler")
            except Exception as e:
                logger.warning(f"[UNIFIED] MultiMonitorQueryHandler not available: {e}")
                return ("multi_monitor_query_handler", False, str(e))

        async def init_proactive_monitoring_manager():
            """Initialize ProactiveMonitoringManager"""
            try:
                from context_intelligence.managers import (
                    get_capture_strategy_manager,
                    get_ocr_strategy_manager,
                    initialize_proactive_monitoring_manager,
                )
                conversation_tracker = None
                if self.context_aware_manager:
                    conversation_tracker = self.context_aware_manager.conversation_tracker

                def alert_callback(alert):
                    logger.info(f"[ALERT] {alert.message}")

                self.proactive_monitoring_manager = initialize_proactive_monitoring_manager(
                    change_detection_manager=self.change_detection_manager,
                    capture_manager=get_capture_strategy_manager(),
                    ocr_manager=get_ocr_strategy_manager(),
                    implicit_resolver=self.implicit_resolver,
                    conversation_tracker=conversation_tracker,
                    default_interval=10.0,
                    alert_callback=alert_callback,
                )
                return ("proactive_monitoring_manager", True, "ProactiveMonitoringManager")
            except Exception as e:
                logger.warning(f"[UNIFIED] ProactiveMonitoringManager not available: {e}")
                return ("proactive_monitoring_manager", False, str(e))

        async def init_temporal_handler():
            """Initialize TemporalQueryHandler"""
            try:
                from context_intelligence.handlers import initialize_temporal_query_handler
                conversation_tracker = None
                if self.context_aware_manager:
                    conversation_tracker = self.context_aware_manager.conversation_tracker
                self.temporal_handler = initialize_temporal_query_handler(
                    proactive_monitoring_manager=self.proactive_monitoring_manager,
                    change_detection_manager=self.change_detection_manager,
                    implicit_resolver=self.implicit_resolver,
                    conversation_tracker=conversation_tracker,
                )
                return ("temporal_handler", True, "TemporalQueryHandler")
            except Exception as e:
                logger.warning(f"[UNIFIED] TemporalQueryHandler not available: {e}")
                return ("temporal_handler", False, str(e))

        async def init_display_reference_handler():
            """Initialize DisplayReferenceHandler"""
            try:
                from context_intelligence.handlers.display_reference_handler import (
                    initialize_display_reference_handler,
                )
                self.display_reference_handler = initialize_display_reference_handler(
                    implicit_resolver=self.implicit_resolver,
                    display_monitor=None,
                )
                return ("display_reference_handler", True, "DisplayReferenceHandler")
            except Exception as e:
                logger.warning(f"[UNIFIED] DisplayReferenceHandler not available: {e}")
                return ("display_reference_handler", False, str(e))

        # Execute Tier 4 in parallel
        logger.info("[UNIFIED] 📦 Tier 4: Initializing handler components...")
        tier4_start = time.time()
        tier4_tasks = [
            asyncio.wait_for(init_multi_monitor_query_handler(), timeout=3.0),
            asyncio.wait_for(init_proactive_monitoring_manager(), timeout=3.0),
            asyncio.wait_for(init_temporal_handler(), timeout=3.0),
            asyncio.wait_for(init_display_reference_handler(), timeout=3.0),
        ]

        tier4_results = await asyncio.gather(*tier4_tasks, return_exceptions=True)

        tier4_success = 0
        for result in tier4_results:
            if isinstance(result, Exception):
                logger.warning(f"[UNIFIED] Tier 4 component failed: {result}")
            elif isinstance(result, tuple) and len(result) == 3:
                name, success, desc = result
                init_results[name] = success
                if success:
                    tier4_success += 1
                    logger.info(f"[UNIFIED] ✅ {desc}")

        tier4_time = time.time() - tier4_start
        logger.info(f"[UNIFIED] Tier 4 complete: {tier4_success}/{len(tier4_tasks)} in {tier4_time:.2f}s")

        # =======================================================================
        # TIER 5: Complex handlers - PARALLEL
        # =======================================================================
        async def init_medium_complexity_handler():
            """Initialize MediumComplexityHandler"""
            try:
                from context_intelligence.handlers import initialize_medium_complexity_handler
                from context_intelligence.managers import (
                    get_capture_strategy_manager,
                    get_ocr_strategy_manager,
                )
                self.medium_complexity_handler = initialize_medium_complexity_handler(
                    capture_manager=get_capture_strategy_manager(),
                    ocr_manager=get_ocr_strategy_manager(),
                    response_manager=self.response_strategy_manager,
                    context_aware_manager=self.context_aware_manager,
                    proactive_suggestion_manager=self.proactive_suggestion_manager,
                    confidence_manager=self.confidence_manager,
                    multi_monitor_manager=self.multi_monitor_manager,
                    multi_monitor_query_handler=self.multi_monitor_query_handler,
                    implicit_resolver=self.implicit_resolver,
                )
                return ("medium_complexity_handler", True, "MediumComplexityHandler")
            except Exception as e:
                logger.warning(f"[UNIFIED] MediumComplexityHandler not available: {e}")
                return ("medium_complexity_handler", False, str(e))

        async def init_complex_complexity_handler():
            """Initialize ComplexComplexityHandler"""
            try:
                from context_intelligence.handlers import (
                    get_predictive_handler,
                    initialize_complex_complexity_handler,
                )
                from context_intelligence.managers import (
                    get_capture_strategy_manager,
                    get_ocr_strategy_manager,
                )
                self.complex_complexity_handler = initialize_complex_complexity_handler(
                    temporal_handler=self.temporal_handler,
                    multi_space_handler=self.multi_space_handler,
                    predictive_handler=get_predictive_handler(),
                    capture_manager=get_capture_strategy_manager(),
                    ocr_manager=get_ocr_strategy_manager(),
                    multi_monitor_manager=self.multi_monitor_manager,
                    implicit_resolver=self.implicit_resolver,
                    cache_ttl=60.0,
                    max_concurrent_captures=5,
                )
                return ("complex_complexity_handler", True, "ComplexComplexityHandler")
            except Exception as e:
                logger.warning(f"[UNIFIED] ComplexComplexityHandler not available: {e}")
                return ("complex_complexity_handler", False, str(e))

        # Execute Tier 5 in parallel
        logger.info("[UNIFIED] 📦 Tier 5: Initializing complex handlers...")
        tier5_start = time.time()
        tier5_tasks = [
            asyncio.wait_for(init_medium_complexity_handler(), timeout=3.0),
            asyncio.wait_for(init_complex_complexity_handler(), timeout=3.0),
        ]

        tier5_results = await asyncio.gather(*tier5_tasks, return_exceptions=True)

        tier5_success = 0
        for result in tier5_results:
            if isinstance(result, Exception):
                logger.warning(f"[UNIFIED] Tier 5 component failed: {result}")
            elif isinstance(result, tuple) and len(result) == 3:
                name, success, desc = result
                init_results[name] = success
                if success:
                    tier5_success += 1
                    logger.info(f"[UNIFIED] ✅ {desc}")

        tier5_time = time.time() - tier5_start
        logger.info(f"[UNIFIED] Tier 5 complete: {tier5_success}/{len(tier5_tasks)} in {tier5_time:.2f}s")

        # =======================================================================
        # TIER 6: Vision Router and Speaker Verification - BACKGROUND (optional)
        # =======================================================================
        async def init_vision_router():
            """Initialize Intelligent Vision Router (YOLO + LLaMA + Claude routing)"""
            if not INTELLIGENT_ROUTER_AVAILABLE or self._vision_router_initialized:
                return ("vision_router", False, "not available or already initialized")
            try:
                yolo_detector = None
                try:
                    from vision.yolo_vision_detector import get_yolo_detector
                    yolo_detector = get_yolo_detector()
                except Exception:
                    pass

                llama_executor = None
                try:
                    from core.hybrid_orchestrator import get_hybrid_orchestrator
                    orchestrator = get_hybrid_orchestrator()
                    if orchestrator and hasattr(orchestrator, "model_manager"):
                        llama_executor = orchestrator.model_manager
                except Exception:
                    pass

                claude_vision_analyzer = None
                try:
                    import os
                    from vision.optimized_claude_vision import OptimizedClaudeVisionAnalyzer
                    api_key = os.getenv("ANTHROPIC_API_KEY")
                    if api_key:
                        claude_vision_analyzer = OptimizedClaudeVisionAnalyzer(
                            api_key=api_key, use_intelligent_selection=True, use_yolo_hybrid=True
                        )
                except Exception:
                    pass

                self.vision_router = IntelligentVisionRouter(
                    yolo_detector=yolo_detector,
                    llama_executor=llama_executor,
                    claude_vision_analyzer=claude_vision_analyzer,
                    yabai_detector=getattr(self, '_yabai_detector', None),
                    max_cost_per_query=0.05,
                    target_latency_ms=2000,
                    prefer_local=True,
                )
                self._vision_router_initialized = True
                return ("vision_router", True, "IntelligentVisionRouter")
            except Exception as e:
                logger.warning(f"[UNIFIED] IntelligentVisionRouter not available: {e}")
                return ("vision_router", False, str(e))

        async def init_speaker_verification():
            """Initialize Speaker Verification Service"""
            if self._speaker_verification_initialized:
                return ("speaker_verification", False, "already initialized")
            try:
                from voice.contextual_message_generator import get_message_generator
                from voice.speaker_verification_service import get_speaker_verification_service

                self.speaker_verification = await asyncio.wait_for(
                    get_speaker_verification_service(),
                    timeout=8.0
                )
                self.message_generator = get_message_generator()
                await asyncio.wait_for(
                    self.message_generator.initialize(),
                    timeout=3.0
                )
                self._speaker_verification_initialized = True
                return ("speaker_verification", True, "SpeakerVerificationService")
            except asyncio.TimeoutError:
                logger.warning("[UNIFIED] Speaker verification timed out")
                return ("speaker_verification", False, "timeout")
            except Exception as e:
                logger.warning(f"[UNIFIED] Speaker verification not available: {e}")
                return ("speaker_verification", False, str(e))

        # Execute Tier 6 in background (don't block on these)
        logger.info("[UNIFIED] 📦 Tier 6: Starting optional components in background...")
        tier6_start = time.time()

        # Run vision router and speaker verification with generous timeouts
        tier6_tasks = [
            asyncio.wait_for(init_vision_router(), timeout=5.0),
            asyncio.wait_for(init_speaker_verification(), timeout=12.0),
        ]

        # Use gather but don't fail if these timeout - they're optional
        tier6_results = await asyncio.gather(*tier6_tasks, return_exceptions=True)

        tier6_success = 0
        for result in tier6_results:
            if isinstance(result, Exception):
                logger.warning(f"[UNIFIED] Tier 6 component failed (optional): {result}")
            elif isinstance(result, tuple) and len(result) == 3:
                name, success, desc = result
                init_results[name] = success
                if success:
                    tier6_success += 1
                    logger.info(f"[UNIFIED] ✅ {desc}")

        tier6_time = time.time() - tier6_start
        logger.info(f"[UNIFIED] Tier 6 complete: {tier6_success}/{len(tier6_tasks)} in {tier6_time:.2f}s")

        # =======================================================================
        # SUMMARY
        # =======================================================================
        total_time = time.time() - start_time
        total_success = sum(1 for v in init_results.values() if v)
        total_components = len(init_results)

        # Log active resolvers
        resolvers_active = []
        if self.context_graph:
            resolvers_active.append("ContextGraph")
        if self.implicit_resolver:
            resolvers_active.append("ImplicitResolver")
        if self.contextual_resolver:
            resolvers_active.append("ContextualResolver")
        if self.multi_space_handler:
            resolvers_active.append("MultiSpaceHandler")
        if self.temporal_handler:
            resolvers_active.append("TemporalHandler")
        if self.query_complexity_manager:
            resolvers_active.append("QueryComplexityManager")
        if self.response_strategy_manager:
            resolvers_active.append("ResponseStrategyManager")
        if self.context_aware_manager:
            resolvers_active.append("ContextAwareManager")
        if self.proactive_suggestion_manager:
            resolvers_active.append("ProactiveSuggestionManager")
        if self.confidence_manager:
            resolvers_active.append("ConfidenceManager")
        if self.multi_monitor_manager:
            resolvers_active.append("MultiMonitorManager")
        if self.multi_monitor_query_handler:
            resolvers_active.append("MultiMonitorQueryHandler")
        if self.change_detection_manager:
            resolvers_active.append("ChangeDetectionManager")
        if self.proactive_monitoring_manager:
            resolvers_active.append("ProactiveMonitoringManager")
        if self.medium_complexity_handler:
            resolvers_active.append("MediumComplexityHandler")
        if getattr(self, 'complex_complexity_handler', None):
            resolvers_active.append("ComplexComplexityHandler")

        if resolvers_active:
            logger.info(f"[UNIFIED] 🎯 Active resolvers ({len(resolvers_active)}): {', '.join(resolvers_active)}")
        else:
            logger.warning("[UNIFIED] ⚠️ No resolvers available - queries will use basic processing")

        logger.info(f"[UNIFIED] ✅ PARALLEL initialization complete: {total_success}/{total_components} components in {total_time:.2f}s")

        self._resolvers_initialized = True

    # NOTE: Legacy sequential initialization removed in v2.0
    # The new _initialize_resolvers() uses 6-tier parallel initialization
    # with per-component timeouts, asyncio.gather, and graceful degradation

    async def _initialize_resolvers_legacy_removed(self):
        """
        DEPRECATED: This method has been replaced by the new parallel initialization.
        The old sequential approach took 15+ seconds and caused timeouts.
        The new approach completes in 3-5 seconds using tiered parallel initialization.
        """
        raise NotImplementedError(
            "Legacy sequential initialization has been removed. "
            "Use _initialize_resolvers() which uses parallel initialization."
        )

    async def warmup_components(self):
        """
        Pre-initialize all components using advanced warmup system.

        This eliminates first-command latency by loading components
        at startup with priority-based, async, health-checked initialization.
        """
        try:
            from api.component_warmup_config import register_all_components
            from core.component_warmup import get_warmup_system

            logger.info("[UNIFIED] 🚀 Starting component warmup...")

            # Register all components
            await register_all_components(self)

            # Execute warmup
            warmup = get_warmup_system()
            report = await warmup.warmup_all()

            # Store component instances in processor
            self.context_graph = warmup.get_component("multi_space_context_graph")
            self.implicit_resolver = warmup.get_component("implicit_reference_resolver")
            self.contextual_resolver = warmup.get_component(
                "implicit_reference_resolver"
            )  # fallback
            self.yabai_detector = warmup.get_component("yabai_detector")
            self.window_detector = warmup.get_component("multi_space_window_detector")
            self.query_complexity_manager = warmup.get_component("query_complexity_manager")
            self.action_handler = warmup.get_component("action_query_handler")
            self.predictive_handler = warmup.get_component("predictive_query_handler")
            self.multi_space_handler = warmup.get_component("multi_space_query_handler")

            # Mark as initialized
            self._resolvers_initialized = True

            # v259.1: Late-bind dependencies to all managers/handlers that were
            # initialized before their upstream singletons completed. During
            # tier-based parallel init, dependencies like implicit_resolver and
            # ocr_manager may be None because their singletons initialize in
            # earlier tiers via asyncio.to_thread. Now that all tiers are
            # complete, propagate the live instances to all components that
            # hold stale None snapshots.
            _late_bind_targets = [
                self.query_complexity_manager,
                self.context_aware_manager,
                self.multi_monitor_manager,
                self.change_detection_manager,
                self.proactive_suggestion_manager,
                self.multi_space_handler,
                self.multi_monitor_query_handler,
                self.proactive_monitoring_manager,
                self.temporal_handler,
                self.display_reference_handler,
                getattr(self, 'medium_complexity_handler', None),
                getattr(self, 'complex_complexity_handler', None),
            ]

            # Late-bind implicit_resolver
            if self.implicit_resolver:
                _rewired = 0
                for _target in _late_bind_targets:
                    if _target is None:
                        continue
                    if hasattr(_target, 'set_implicit_resolver'):
                        _target.set_implicit_resolver(self.implicit_resolver)
                        _rewired += 1
                    elif hasattr(_target, 'implicit_resolver'):
                        if not _target.implicit_resolver:
                            _target.implicit_resolver = self.implicit_resolver
                            _rewired += 1
                if _rewired > 0:
                    logger.info(
                        f"[UNIFIED] v259.1: Late-bound implicit_resolver "
                        f"to {_rewired} component(s)"
                    )

            # v259.1: Late-bind ocr_manager — same pattern. The OCR singleton
            # initializes in Tier 1 via asyncio.to_thread, but Tier 4 handlers
            # may call get_ocr_strategy_manager() before the thread completes.
            try:
                from context_intelligence.managers import get_ocr_strategy_manager
                _ocr = get_ocr_strategy_manager()
            except Exception:
                _ocr = None
            if _ocr is not None:
                _ocr_rewired = 0
                for _target in _late_bind_targets:
                    if _target is None:
                        continue
                    if hasattr(_target, 'set_ocr_manager'):
                        _target.set_ocr_manager(_ocr)
                        _ocr_rewired += 1
                    elif hasattr(_target, 'ocr_manager'):
                        if not _target.ocr_manager:
                            _target.ocr_manager = _ocr
                            _ocr_rewired += 1
                if _ocr_rewired > 0:
                    logger.info(
                        f"[UNIFIED] v259.1: Late-bound ocr_manager "
                        f"to {_ocr_rewired} component(s)"
                    )

            # v259.1: Late-bind capture_manager — same pattern
            try:
                from context_intelligence.managers import get_capture_strategy_manager
                _capture = get_capture_strategy_manager()
            except Exception:
                _capture = None
            if _capture is not None:
                _cap_rewired = 0
                for _target in _late_bind_targets:
                    if _target is None:
                        continue
                    if hasattr(_target, 'capture_manager'):
                        if not _target.capture_manager:
                            _target.capture_manager = _capture
                            _cap_rewired += 1
                if _cap_rewired > 0:
                    logger.info(
                        f"[UNIFIED] v259.1: Late-bound capture_manager "
                        f"to {_cap_rewired} component(s)"
                    )

            logger.info(
                f"[UNIFIED] ✅ Component warmup complete! "
                f"{report['ready_count']}/{report['total_count']} components ready "
                f"in {report['total_load_time']:.2f}s"
            )

            return report

        except Exception as e:
            logger.error(f"[UNIFIED] ❌ Component warmup failed: {e}", exc_info=True)
            # Fall back to lazy initialization
            logger.warning("[UNIFIED] Falling back to lazy initialization")
            return None

    def _load_learned_data(self):
        """Load previously learned patterns and statistics"""
        try:
            data_dir = Path.home() / ".jarvis" / "learning"
            data_dir.mkdir(parents=True, exist_ok=True)

            stats_file = data_dir / "command_stats.json"
            if stats_file.exists():
                with open(stats_file, "r") as f:
                    self.command_stats = defaultdict(int, json.load(f))

            patterns_file = data_dir / "success_patterns.json"
            if patterns_file.exists():
                with open(patterns_file, "r") as f:
                    self.success_patterns = defaultdict(list, json.load(f))

        except Exception as e:
            logger.debug(f"Could not load learned data: {e}")

    def _save_learned_data(self):
        """Save learned patterns and statistics"""
        try:
            data_dir = Path.home() / ".jarvis" / "learning"
            data_dir.mkdir(parents=True, exist_ok=True)

            with open(data_dir / "command_stats.json", "w") as f:
                json.dump(dict(self.command_stats), f)

            with open(data_dir / "success_patterns.json", "w") as f:
                json.dump(dict(self.success_patterns), f)

        except Exception as e:
            logger.debug(f"Could not save learned data: {e}")

    def _initialize_handlers(self):
        """Initialize command handlers lazily"""
        # We'll import handlers only when needed to avoid circular imports
        self.handler_modules = {
            CommandType.VISION: "api.vision_command_handler",
            CommandType.SYSTEM: "system_control.macos_controller",
            CommandType.WEATHER: "system_control.weather_system_config",
            CommandType.AUTONOMY: "api.autonomy_handler",
            CommandType.VOICE_UNLOCK: "api.voice_unlock_handler",
            CommandType.SCREEN_LOCK: "api.simple_unlock_handler",  # Lock screen handler
            CommandType.QUERY: "api.query_handler",  # Add basic query handler
        }

    async def process_command(
        self, command_text: str, websocket=None, audio_data: bytes = None,
        speaker_name: str = None, deadline: Optional[float] = None,
        source_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Process any command through unified pipeline with FULL context awareness including voice authentication"""
        logger.info(f"[UNIFIED] Processing with context awareness: '{command_text}'")

        source_context = source_context or {}
        allow_during_tts_interrupt = bool(
            source_context.get("allow_during_tts_interrupt", False)
        )

        # =========================================================================
        # 🔇 SELF-VOICE SUPPRESSION - Prevent JARVIS echo/hallucinations
        # =========================================================================
        # If JARVIS is currently speaking, this command is likely JARVIS's own
        # voice being picked up by the microphone. Silently reject it to prevent
        # feedback loops where JARVIS processes its own speech as commands.
        # =========================================================================
        try:
            from agi_os.realtime_voice_communicator import get_voice_communicator
            voice_comm = await asyncio.wait_for(get_voice_communicator(), timeout=0.5)
            if voice_comm and voice_comm.is_speaking and not allow_during_tts_interrupt:
                logger.warning(f"🔇 [SELF-VOICE-SUPPRESSION] Rejecting command while JARVIS is speaking: '{command_text[:50]}...'")
                return {
                    "success": False,
                    "response": None,  # Silent - don't speak or it creates more echo
                    "type": "self_voice_suppressed",
                    "message": "Command rejected - JARVIS is currently speaking",
                    "original_command": command_text
                }
            if voice_comm and voice_comm.is_speaking and allow_during_tts_interrupt:
                logger.debug(
                    "[UNIFIED] Bypassing self-voice suppression for confirmed barge-in command"
                )
        except Exception as e:
            logger.debug(f"[UNIFIED] Self-voice check skipped: {e}")

        # Store audio data and speaker for voice authentication (used by context-aware handlers)
        self.current_audio_data = audio_data
        self.current_speaker_name = speaker_name

        # Debug: Log audio data availability
        if audio_data:
            logger.info(f"[UNIFIED] Audio data available: {len(audio_data)} bytes")
        else:
            logger.info("[UNIFIED] No audio data provided for this command")

        # =========================================================================
        # v242 SPINAL REFLEX ARC: Reflex check -> J-Prime call -> Action executor
        # =========================================================================
        # Replaces 5,000 lines of keyword classification with ~200 lines.
        # 1. Check reflex manifest (local, sub-ms)
        # 2. Call J-Prime for classification + generation
        # 3. Execute action based on J-Prime's routing metadata
        # 4. Brain vacuum fallback if J-Prime is unreachable
        # =========================================================================

        # Track command frequency
        self.command_stats[command_text.lower()] += 1

        # === Step 1: Reflex manifest check (sub-ms, file-based) ===
        reflex = await self._check_reflex_manifest(command_text)
        if reflex:
            self._v242_metrics["reflex_hits"] += 1
            logger.info(f"[v242] Reflex matched: {reflex.get('reflex_id')} for '{command_text[:60]}'")
            result = await self._execute_reflex(reflex, command_text)
            # Fire-and-forget telemetry notification
            asyncio.create_task(self._notify_reflex_executed(command_text, reflex))
            return result

        # === Step 2: J-Prime call (the brain) ===
        logger.info(f"[v242] Sending to J-Prime: '{command_text[:80]}'")

        # v242.1: Build lightweight context for J-Prime classification
        _jprime_ctx: Dict[str, Any] = {}
        if source_context and isinstance(source_context, dict):
            _jprime_ctx["source"] = source_context.get("source", "unknown")
        if audio_data is not None:
            _jprime_ctx["has_audio"] = True
        if speaker_name:
            _jprime_ctx["speaker"] = speaker_name
        # Lightweight screen context (active app name, no screenshot)
        try:
            import subprocess
            _active_app = subprocess.check_output(
                ["osascript", "-e",
                 'tell application "System Events" to get name of first application process whose frontmost is true'],
                timeout=1, stderr=subprocess.DEVNULL,
            ).decode().strip()
            if _active_app:
                _jprime_ctx["active_app"] = _active_app
        except Exception:
            pass
        # Last 5 conversation turns for continuity
        if hasattr(self, 'context') and hasattr(self.context, 'conversation_history'):
            history = self.context.conversation_history
            if history:
                _jprime_ctx["recent_history"] = [
                    {"role": h.get("role", "user"), "content": str(h.get("content", ""))[:200]}
                    for h in (history[-5:] if isinstance(history, list) else [])
                ]
        response = await self._call_jprime(
            command_text, deadline=deadline, source_context=_jprime_ctx or None,
        )
        if response:
            logger.info(
                f"[v242] J-Prime classified: intent={response.intent}, "
                f"domain={response.domain}, confidence={response.confidence:.2f}, "
                f"source={response.source}"
            )
            # v242.1: Track classification domain distribution
            _cls_domain = response.domain or "unknown"
            self._v242_metrics["classifications"][_cls_domain] = (
                self._v242_metrics["classifications"].get(_cls_domain, 0) + 1
            )
            result = await self._execute_action(
                response, command_text, websocket, audio_data, speaker_name
            )

            # === Telemetry: Emit interaction to Reactor-Core for training ===
            try:
                from core.telemetry_emitter import get_telemetry_emitter

                emitter = await get_telemetry_emitter()
                _resp_meta = result.get("metadata", {})
                _model_id = _resp_meta.get("model_id") if isinstance(_resp_meta, dict) else None

                _telemetry_task = asyncio.create_task(
                    emitter.emit_interaction(
                        user_input=command_text,
                        response=result.get("response", ""),
                        success=result.get("success", False),
                        confidence=response.confidence,
                        latency_ms=result.get("latency_ms", 0.0),
                        source=result.get("source", response.source),
                        metadata={
                            "command_type": result.get("command_type", "UNKNOWN"),
                            "intent": response.intent,
                            "domain": response.domain,
                        },
                        model_id=_model_id,
                        task_type=result.get("task_type"),
                    ),
                    name="telemetry_emit_interaction",
                )
                _telemetry_task.add_done_callback(
                    lambda t: logger.warning(f"[UNIFIED] Telemetry emission failed: {t.exception()}")
                    if not t.cancelled() and t.exception() else None
                )
            except ImportError:
                pass  # Telemetry not available
            except Exception as e:
                logger.debug(f"[UNIFIED] Telemetry emission setup failed: {e}")

            return result

        # === Step 3: Brain vacuum fallback ===
        logger.warning("[v242] J-Prime unreachable and no reflex match. Brain vacuum fallback.")
        return {
            "success": False,
            "response": "I'm having trouble connecting to my brain. Please try again in a moment.",
            "command_type": "UNKNOWN",
            "source": "brain_vacuum",
        }


    @staticmethod
    def _summarize_workspace_result(result: dict, intent: str) -> str:
        """Generate a human-readable response from structured workspace data."""
        if intent in ("check_email", "fetch_unread_emails"):
            count = result.get("count", 0)
            total = result.get("total_unread", count)
            if count == 0:
                return "No unread emails found."
            emails = result.get("emails", [])
            lines = [f"You have {total} unread emails. Here are the latest {count}:"]
            for em in emails[:5]:
                subj = em.get("subject", "(no subject)")
                sender = em.get("from", "unknown")
                lines.append(f"  - {subj} (from {sender})")
            if count > 5:
                lines.append(f"  ...and {count - 5} more")
            return "\n".join(lines)
        elif intent in ("check_calendar", "check_calendar_events"):
            events = result.get("events", [])
            if not events:
                return "No events on your calendar for this time period."
            lines = [f"You have {len(events)} event(s):"]
            for ev in events[:5]:
                summary = ev.get("summary", ev.get("title", "(untitled)"))
                start = ev.get("start", "")
                lines.append(f"  - {summary} ({start})")
            return "\n".join(lines)
        elif intent == "workspace_summary":
            # Prefer the agent's "brief" field, then "summary", then fallback
            return result.get("brief") or result.get("summary", "Workspace summary completed.")
        else:
            return "Workspace command completed successfully."

    # =========================================================================
    # v242 SPINAL REFLEX ARC — New methods for reflex + J-Prime routing
    # =========================================================================

    def get_routing_metrics(self) -> Dict[str, Any]:
        """Return v242 routing metrics for observability."""
        return dict(self._v242_metrics)

    async def _check_reflex_manifest(self, command_text: str) -> Optional[Dict[str, Any]]:
        """Check if command matches a reflex in the J-Prime-published manifest.

        v242.1: Cached with content-hash invalidation. Disk reads rate-limited
        to every JARVIS_REFLEX_CACHE_INTERVAL seconds (default 5s).
        Returns reflex dict if matched, None otherwise.
        Checks inhibition signals before executing.
        """
        import hashlib
        import time as _time

        manifest_path = Path.home() / ".jarvis" / "trinity" / "reflex_manifest.json"
        inhibition_path = Path.home() / ".jarvis" / "trinity" / "reflex_inhibition.json"

        # Rate-limit disk stat/read to every N seconds
        now = _time.monotonic()
        if now - self._reflex_cache_checked_at > self._REFLEX_CACHE_CHECK_INTERVAL:
            self._reflex_cache_checked_at = now
            # Refresh manifest cache if content changed
            try:
                if manifest_path.exists():
                    raw = manifest_path.read_bytes()
                    h = hashlib.md5(raw).hexdigest()
                    if h != self._reflex_manifest_hash:
                        self._reflex_manifest_cache = json.loads(raw)
                        self._reflex_manifest_hash = h
                else:
                    self._reflex_manifest_cache = None
                    self._reflex_manifest_hash = ""
            except (OSError, json.JSONDecodeError):
                pass  # Keep stale cache on read error

            # Refresh inhibition cache if content changed
            try:
                if inhibition_path.exists():
                    raw = inhibition_path.read_bytes()
                    h = hashlib.md5(raw).hexdigest()
                    if h != self._reflex_inhibition_hash:
                        self._reflex_inhibition_cache = json.loads(raw)
                        self._reflex_inhibition_hash = h
                else:
                    self._reflex_inhibition_cache = None
                    self._reflex_inhibition_hash = ""
            except (OSError, json.JSONDecodeError):
                pass  # Keep stale cache on read error
        else:
            # v242.1: Cache was fresh — no disk read needed
            self._v242_metrics["reflex_cache_hits"] += 1

        manifest = self._reflex_manifest_cache
        if not manifest:
            return None

        # Check each reflex for pattern match
        normalized = command_text.lower().strip()
        for reflex_id, reflex in manifest.get("reflexes", {}).items():
            patterns = reflex.get("patterns", [])
            if any(normalized == p.lower() for p in patterns):
                # Check inhibition before executing
                inhibition = self._reflex_inhibition_cache
                if inhibition:
                    inhibited = inhibition.get("inhibit_reflexes", [])
                    if reflex_id in inhibited:
                        published = inhibition.get("published_at", "")
                        ttl = inhibition.get("ttl_seconds", 0)
                        try:
                            from datetime import timezone
                            pub_time = datetime.fromisoformat(published)
                            if (datetime.now(timezone.utc) - pub_time).total_seconds() < ttl:
                                logger.info(
                                    f"[v242] Reflex '{reflex_id}' inhibited: "
                                    f"{inhibition.get('reason')}"
                                )
                                return None  # Inhibited -- send to J-Prime instead
                        except (ValueError, TypeError):
                            pass  # Inhibition check failed -- execute reflex anyway

                return {"reflex_id": reflex_id, **reflex}

        return None

    async def _call_jprime(
        self, command_text: str, deadline: Optional[float] = None,
        source_context: Optional[Dict[str, Any]] = None,
    ) -> Optional[Any]:
        """Send query to J-Prime for classification and response.

        Returns StructuredResponse or None on failure.
        v242.1: On J-Prime failure, attempts brain vacuum fallback via client
        before returning None.
        v242.1: Retries once after 2s on 503 model-swap (source='model_swapping').
        """
        self._v242_metrics["jprime_calls"] += 1
        client = None
        try:
            from core.jarvis_prime_client import get_jarvis_prime_client

            client = get_jarvis_prime_client()
            if client is None:
                return None

            import time as _time
            timeout = 30.0
            if deadline:
                remaining = deadline - _time.monotonic()
                timeout = max(2.0, remaining - 1.0)

            response = await asyncio.wait_for(
                client.classify_and_complete(
                    query=command_text,
                    max_tokens=512,
                    context_metadata=source_context,
                ),
                timeout=timeout,
            )

            # v242.1: Detect model-swap 503 and retry once after 2s
            if response and getattr(response, 'source', '') == 'model_swapping':
                self._v242_metrics["jprime_503_retries"] += 1
                # Only retry if we have enough time left
                retry_timeout = timeout
                if deadline:
                    remaining = deadline - _time.monotonic()
                    retry_timeout = max(2.0, remaining - 1.0)
                    if remaining < 4.0:
                        # Not enough time for 2s sleep + retry
                        logger.info(
                            "[v242] J-Prime mid-model-swap but insufficient "
                            f"time for retry ({remaining:.1f}s left)"
                        )
                        # Fall through to brain vacuum below
                        response = None
                    else:
                        logger.info("[v242] J-Prime mid-model-swap, retrying in 2s")
                        await asyncio.sleep(2.0)
                        # Recalculate timeout for retry
                        remaining = deadline - _time.monotonic()
                        retry_timeout = max(2.0, remaining - 1.0)
                        response = await asyncio.wait_for(
                            client.classify_and_complete(
                                query=command_text,
                                max_tokens=512,
                                context_metadata=source_context,
                            ),
                            timeout=retry_timeout,
                        )
                else:
                    logger.info("[v242] J-Prime mid-model-swap, retrying in 2s")
                    await asyncio.sleep(2.0)
                    response = await asyncio.wait_for(
                        client.classify_and_complete(
                            query=command_text,
                            max_tokens=512,
                            context_metadata=source_context,
                        ),
                        timeout=retry_timeout,
                    )

            # If retry also returned model_swapping, treat as failure
            if response and getattr(response, 'source', '') == 'model_swapping':
                logger.warning("[v242] J-Prime still swapping after retry, falling back")
                response = None

            if response is not None:
                return response

        except asyncio.TimeoutError:
            self._v242_metrics["jprime_timeouts"] += 1
            logger.warning("[v242] J-Prime call timed out")
        except Exception as e:
            logger.warning(f"[v242] J-Prime call failed: {e}")

        # v242.1: J-Prime failed — try brain vacuum fallback via existing client
        if client is not None:
            try:
                self._v242_metrics["brain_vacuum_activations"] += 1
                logger.info("[v242] Attempting brain vacuum fallback via client")
                return await client._brain_vacuum_fallback(
                    query=command_text, system_prompt=None, max_tokens=512,
                )
            except Exception as fallback_err:
                logger.error(f"[v242] Brain vacuum fallback also failed: {fallback_err}")

        return None

    async def _execute_action(
        self, response: Any, command_text: str,
        websocket=None, audio_data: bytes = None, speaker_name: str = None,
    ) -> Dict[str, Any]:
        """Execute the action determined by J-Prime's classification.

        Maps intent + domain to existing execution handlers.
        Returns the standard process_command() response dict.
        """
        intent = response.intent
        domain = response.domain

        # Intent: answer / conversation -- just return the text
        if intent in ("answer", "conversation"):
            return {
                "success": True,
                "response": response.content,
                "command_type": "QUERY",
                "source": response.source,
                "x_jarvis_routing": {
                    "intent": intent, "domain": domain,
                    "confidence": response.confidence,
                },
            }

        # Intent: action -- execute a system command
        if intent == "action":
            if domain == "surveillance":
                return await self._handle_surveillance_action(command_text, websocket)
            elif domain == "system":
                return await self._handle_system_action_via_jprime(
                    command_text, response.suggested_actions
                )
            elif domain == "screen_lock":
                return await self._handle_screen_lock_action(command_text)
            elif domain == "voice_unlock":
                return await self._handle_voice_unlock_action(
                    command_text, websocket, audio_data, speaker_name
                )
            elif domain == "workspace":
                return await self._handle_workspace_action(command_text, response)
            else:
                return {
                    "success": True,
                    "response": response.content,
                    "command_type": "SYSTEM",
                    "source": response.source,
                }

        # Intent: vision_needed -- use existing vision handler
        if intent == "vision_needed":
            return await self._handle_vision_action(command_text, websocket)

        # Intent: multi_step_action -- execute step by step
        if intent == "multi_step_action":
            return await self._handle_compound_command(
                command_text, context={"suggested_actions": response.suggested_actions}
            )

        # Intent: clarify -- ask user to clarify
        if intent == "clarify":
            return {
                "success": True,
                "response": response.content or "Could you clarify what you'd like me to do?",
                "command_type": "QUERY",
                "source": response.source,
            }

        # Fallback: treat as answer
        return {
            "success": True,
            "response": response.content,
            "command_type": "QUERY",
            "source": response.source,
        }

    async def _handle_surveillance_action(
        self, command_text: str, websocket=None,
    ) -> Dict[str, Any]:
        """Route surveillance commands to IntelligentCommandHandler."""
        import os
        import sys

        try:
            _current_file = os.path.abspath(__file__)
            _api_dir = os.path.dirname(_current_file)
            _backend_root = os.path.dirname(_api_dir)
            _project_root = os.path.dirname(_backend_root)

            for _inject_path in [_backend_root, _project_root]:
                if _inject_path not in sys.path:
                    sys.path.insert(0, _inject_path)

            from voice.intelligent_command_handler import IntelligentCommandHandler

            intelligent_handler = IntelligentCommandHandler()
            surveillance_timeout = float(
                os.getenv("JARVIS_SURVEILLANCE_HANDLER_TIMEOUT", "80")
            )

            result = await asyncio.wait_for(
                intelligent_handler.handle_command(command_text),
                timeout=surveillance_timeout,
            )

            # Unpack result (handles tuple, dict, or string)
            if isinstance(result, tuple) and len(result) == 2:
                response_text, handler_used = result
            elif isinstance(result, dict):
                response_text = result.get("response", result.get("text", "Monitoring initiated"))
                handler_used = result.get("handler", "surveillance")
            elif isinstance(result, str):
                response_text = result
                handler_used = "surveillance"
            else:
                response_text = str(result) if result else "Monitoring initiated"
                handler_used = "unknown"

            return {
                "success": True,
                "response": response_text,
                "command_type": "surveillance",
                "handler_used": handler_used,
            }

        except asyncio.TimeoutError:
            return {
                "success": False,
                "response": "Surveillance setup timed out. Please try again.",
                "command_type": "surveillance",
                "error": "handler_timeout",
            }
        except Exception as e:
            logger.error(f"[v242] Surveillance action failed: {e}", exc_info=True)
            return {
                "success": False,
                "response": f"I encountered an error setting up monitoring: {str(e)}",
                "command_type": "surveillance",
                "error": str(e),
            }

    async def _handle_system_action_via_jprime(
        self, command_text: str, suggested_actions: list,
    ) -> Dict[str, Any]:
        """Execute system commands using existing _execute_system_command."""
        try:
            result = await asyncio.wait_for(
                self._execute_system_command(command_text),
                timeout=20.0,
            )
            return {
                "success": result.get("success", False),
                "response": result.get("response", ""),
                "command_type": "SYSTEM",
                **result,
            }
        except asyncio.TimeoutError:
            return {
                "success": False,
                "response": "The command timed out. Please try again.",
                "command_type": "SYSTEM",
                "error": "timeout",
            }
        except Exception as e:
            logger.error(f"[v242] System action failed: {e}", exc_info=True)
            return {
                "success": False,
                "response": f"Sorry, I had trouble executing that command: {str(e)}",
                "command_type": "SYSTEM",
                "error": str(e),
            }

    async def _handle_screen_lock_action(self, command_text: str) -> Dict[str, Any]:
        """Execute screen lock via transport handler."""
        try:
            from api.simple_unlock_handler import _get_owner_name
            try:
                owner_name = await asyncio.wait_for(_get_owner_name(), timeout=1.0)
            except Exception:
                owner_name = "there"

            from api.simple_unlock_handler import handle_unlock_command
            result = await asyncio.wait_for(
                handle_unlock_command(command_text),
                timeout=5.0,
            )
            return {
                "success": result.get("success", True),
                "response": result.get(
                    "response",
                    f"Locking your screen now, {owner_name}. See you soon!",
                ),
                "command_type": "screen_lock",
                "fast_path": True,
                **result,
            }
        except Exception as e:
            logger.error(f"[v242] Screen lock failed: {e}")
            return {
                "success": False,
                "response": f"Failed to lock screen: {str(e)}",
                "command_type": "screen_lock",
                "error": str(e),
            }

    async def _handle_voice_unlock_action(
        self, command_text: str, websocket=None,
        audio_data: bytes = None, speaker_name: str = None,
    ) -> Dict[str, Any]:
        """Execute voice unlock via voice_unlock_handler."""
        try:
            handler = await self._get_handler(CommandType.VOICE_UNLOCK)
            if not handler:
                return {
                    "success": False,
                    "response": "Voice unlock handler not available.",
                    "command_type": "voice_unlock",
                }

            jarvis_instance = type(
                "obj", (object,),
                {
                    "last_audio_data": audio_data or self.current_audio_data,
                    "last_speaker_name": speaker_name or self.current_speaker_name,
                },
            )()

            result = await handler.handle_command(command_text, websocket, jarvis_instance)
            return {
                "success": result.get("success", result.get("type") == "voice_unlock"),
                "response": result.get("message", result.get("response", "")),
                "command_type": "voice_unlock",
                **result,
            }
        except Exception as e:
            logger.error(f"[v242] Voice unlock failed: {e}", exc_info=True)
            return {
                "success": False,
                "response": f"Voice unlock failed: {str(e)}",
                "command_type": "voice_unlock",
                "error": str(e),
            }

    async def _handle_workspace_action(
        self, command_text: str, response: Any,
    ) -> Dict[str, Any]:
        """Route workspace commands to the registered GoogleWorkspaceAgent singleton.

        v242.1: Uses Neural Mesh agent registry singleton if available,
        falls back to lazy singleton to avoid expensive re-initialization.
        """
        self._v242_metrics["workspace_requests"] += 1
        try:
            # Prefer existing agent instance from Neural Mesh coordinator
            agent = None
            if hasattr(self, '_neural_mesh_coordinator') and self._neural_mesh_coordinator:
                agent = self._neural_mesh_coordinator.get_agent("google_workspace_agent")

            if agent is None:
                # Lazy singleton fallback (avoids re-constructing expensive OAuth clients)
                if not hasattr(self, '_workspace_agent_singleton'):
                    try:
                        from neural_mesh.agents.google_workspace_agent import GoogleWorkspaceAgent
                        self._workspace_agent_singleton = GoogleWorkspaceAgent()
                    except ImportError:
                        logger.warning("[v242] GoogleWorkspaceAgent not available")
                        self._workspace_agent_singleton = None
                agent = self._workspace_agent_singleton

            if agent is None:
                # No workspace agent available — return J-Prime's text response
                return {
                    "success": True,
                    "response": response.content or "Workspace features are not currently available.",
                    "command_type": "WORKSPACE",
                    "source": response.source,
                }

            result = await agent.execute_task({
                "action": "handle_workspace_query",
                "query": command_text,
            })
            summary = self._summarize_workspace_result(
                result if isinstance(result, dict) else {"response": str(result)},
                getattr(response, 'intent', 'workspace'),
            )
            return {
                "success": True,
                "response": summary,
                "command_type": "WORKSPACE",
                "source": response.source,
            }
        except Exception as e:
            logger.error(f"[v242] Workspace action failed: {e}", exc_info=True)
            # Fallback: return J-Prime's text response
            return {
                "success": True,
                "response": response.content or "I couldn't complete that workspace action.",
                "command_type": "WORKSPACE",
                "source": response.source,
            }

    async def _handle_vision_action(
        self, command_text: str, websocket=None,
    ) -> Dict[str, Any]:
        """Two-phase vision: Phase 1 (J-Prime classification) already done, now Phase 2 (capture + analyze).

        v242.1: The two-phase vision architecture:
          Phase 1: J-Prime classifies text-only -> intent=vision_needed (already completed by caller)
          Phase 2: This method captures screenshot -> sends to vision router for analysis

        This ensures no unnecessary screenshots are taken for non-vision intents.
        """
        self._v242_metrics["vision_requests"] += 1
        try:
            # Phase 2: Capture screenshot and analyze
            if self.vision_router and self._vision_router_initialized:
                import pyautogui
                loop = asyncio.get_running_loop()
                screenshot = await loop.run_in_executor(None, pyautogui.screenshot)

                # Build context from conversation history for continuity
                _vision_context: Dict[str, Any] = {
                    "original_query": command_text,
                }
                if hasattr(self, "context") and hasattr(self.context, "conversation_history"):
                    _vision_context["conversation_history"] = (
                        self.context.conversation_history[-5:]
                        if isinstance(self.context.conversation_history, list)
                        else []
                    )

                # Use vision router for analysis (existing, proven path)
                result = await self.vision_router.execute_query(
                    query=command_text,
                    screenshot=screenshot,
                    context=_vision_context,
                )
                return {
                    "success": result.get("success", result.get("handled", False)),
                    "response": result.get("response", ""),
                    "command_type": "VISION",
                    **result,
                }

            # Fallback: no vision router, ask J-Prime for text-only answer
            logger.warning("[v242] Vision router unavailable, text-only fallback")
            sub = await self._call_jprime(command_text)
            if sub:
                return {
                    "success": True,
                    "response": sub.content,
                    "command_type": "VISION",
                    "source": sub.source,
                }
            return {
                "success": False,
                "response": "Vision capabilities are currently unavailable.",
                "command_type": "VISION",
            }
        except Exception as e:
            logger.error(f"[v242] Vision action failed: {e}", exc_info=True)
            return {
                "success": False,
                "response": f"Error analyzing screen: {e}",
                "command_type": "VISION",
                "error": str(e),
            }

    async def _execute_reflex(
        self, reflex: dict, command_text: str,
    ) -> Dict[str, Any]:
        """Execute a matched reflex immediately."""
        import random

        reflex_id = reflex.get("reflex_id", "unknown")
        action = reflex.get("action", "")

        if action == "canned_response":
            pool = reflex.get("response_pool", ["Done."])
            return {
                "success": True,
                "response": random.choice(pool),
                "command_type": "REFLEX",
                "reflex_id": reflex_id,
            }

        if action == "system_command":
            executor_path = reflex.get("executor", "")
            try:
                result = await self._execute_system_reflex(executor_path)
                return {
                    "success": True,
                    "response": result or f"Done: {reflex_id}",
                    "command_type": "REFLEX",
                    "reflex_id": reflex_id,
                }
            except Exception as e:
                logger.error(f"[v242] Reflex execution failed: {e}")
                return {
                    "success": False,
                    "response": str(e),
                    "command_type": "REFLEX",
                }

        return {
            "success": False,
            "response": "Unknown reflex action",
            "command_type": "REFLEX",
        }

    async def _execute_system_reflex(self, executor_path: str) -> Optional[str]:
        """Execute a system reflex by its executor path (e.g., 'macos_controller.lock_screen')."""
        parts = executor_path.split(".")
        if len(parts) != 2:
            raise ValueError(f"Invalid executor path: {executor_path}")

        controller_name, method_name = parts

        # Map controller names to actual instances
        if controller_name == "macos_controller":
            from system_control.macos_controller import MacOSController

            controller = MacOSController()
            if hasattr(controller, method_name):
                method = getattr(controller, method_name)
                if asyncio.iscoroutinefunction(method):
                    return await method()
                else:
                    return method()

        raise ValueError(f"Unknown controller: {controller_name}")

    async def _notify_reflex_executed(
        self, command_text: str, reflex: dict,
    ) -> None:
        """Fire-and-forget: notify J-Prime that a reflex was executed (for learning)."""
        try:
            logger.debug(
                f"[v242] Reflex executed: {reflex.get('reflex_id')} "
                f"for '{command_text}'"
            )
        except Exception:
            pass  # Non-critical

    # =========================================================================
    # END v242 SPINAL REFLEX ARC
    # =========================================================================

    def _is_single_concept(self, text: str, connector: str) -> bool:
        """Check if connector is part of a single concept rather than joining commands"""
        # Common phrases that shouldn't be split
        single_concepts = {
            "and press enter",
            "and enter",
            "and return",
            "black and white",
            "up and down",
            "back and forth",
            "pros and cons",
            "dos and don'ts",
        }

        for concept in single_concepts:
            if concept in text:
                return True

        # Check if it's part of a search query or typed text
        before_connector = text.split(connector)[0]
        if any(pattern in before_connector for pattern in ["search for", "type", "write", "enter"]):
            return True

        return False

    def _detect_system_indicators(self, words: List[str]) -> int:
        """Count system-related indicators in words"""
        indicators = 0

        # System settings
        settings_words = {
            "volume",
            "brightness",
            "wifi",
            "bluetooth",
            "display",
            "sound",
            "network",
        }
        indicators += sum(1 for word in words if word in settings_words)

        # System actions
        action_words = {"screenshot", "restart", "shutdown", "sleep", "lock", "unlock", "search", "find", "google"}
        indicators += sum(1 for word in words if word in action_words)

        # File operations
        file_words = {
            "file",
            "folder",
            "directory",
            "document",
            "save",
            "open",
            "create",
        }
        indicators += sum(1 for word in words if word in file_words)

        return indicators

    def _calculate_display_score(self, words: List[str], command_lower: str) -> float:
        """
        Calculate likelihood of display/screen mirroring command

        Detects commands like:
        - "screen mirror my Mac to the Living Room TV"
        - "connect to Living Room TV"
        - "extend display to Sony TV"
        - "airplay to Living Room TV"
        - "stop living room tv"
        - "disconnect from living room tv"
        - "stop screen mirroring"
        """
        score = 0.0

        # Clean words by removing punctuation
        import re

        clean_words = [re.sub(r"[^\w\s]", "", word) for word in words]

        # Primary display/mirroring keywords (STRONG indicators)
        primary_keywords = {
            "mirror": 0.8,
            "airplay": 0.9,
            "extend": 0.7,
        }

        for keyword, weight in primary_keywords.items():
            if keyword in clean_words:
                score += weight

        # Secondary display keywords (combined with display action)
        secondary_keywords = {"display", "screen", "tv", "television"}
        has_secondary = any(kw in clean_words for kw in secondary_keywords)

        # Display action verbs (both connection and disconnection)
        action_verbs = {
            "connect",
            "cast",
            "project",
            "stream",
            "share",
            "stop",
            "disconnect",
            "turn",
            "disable",
        }
        has_action = any(verb in clean_words for verb in action_verbs)

        # Disconnection indicators (boost score for disconnect commands)
        disconnect_indicators = {"stop", "disconnect", "turn", "disable", "off"}
        has_disconnect = any(indicator in clean_words for indicator in disconnect_indicators)
        if has_disconnect and has_secondary:
            score += 0.7

        # Boost if we have action verb + display keyword
        if has_action and has_secondary:
            score += 0.6

        # Boost for prepositions indicating target ("to", "on")
        if ("to" in clean_words or "on" in clean_words) and has_secondary:
            score += 0.2

        # Check for TV/display names (Living Room, Sony, etc.)
        # If "room" or "tv" or brand names are mentioned with action
        tv_indicators = {"room", "tv", "television", "sony", "lg", "samsung"}
        has_tv_indicator = any(indicator in clean_words for indicator in tv_indicators)

        if has_tv_indicator and (has_action or score > 0):
            score += 0.3

        # Specific display name patterns (HIGH confidence even without action verb)
        # These patterns strongly indicate user wants to connect to a display
        display_name_patterns = [
            r"living\s*room\s*tv",  # "living room tv"
            r"bedroom\s*tv",  # "bedroom tv"
            r"kitchen\s*tv",  # "kitchen tv"
            r"office\s*tv",  # "office tv"
            r"\w+\s*room\s*tv",  # "any room tv"
            r"(sony|lg|samsung)\s*tv",  # "sony tv", "lg tv", etc.
        ]

        for pattern in display_name_patterns:
            if re.search(pattern, command_lower):
                # Known display name mentioned - very likely a connection request
                score = max(score, 0.85)
                break

        # Specific phrase matching (highest confidence)
        if "screen mirror" in command_lower or "screen mirroring" in command_lower:
            score = max(score, 0.95)

        if "airplay" in command_lower and "to" in command_lower:
            score = max(score, 0.95)

        # Disconnection phrases (high confidence)
        disconnect_phrases = [
            "stop screen mirror",
            "stop mirroring",
            "disconnect display",
            "turn off screen mirror",
            "stop airplay",
        ]
        for phrase in disconnect_phrases:
            if phrase in command_lower:
                score = max(score, 0.95)
                break

        # Mode change phrases (high confidence)
        mode_change_phrases = [
            "change to extended",
            "change to entire",
            "change to window",
            "switch to extended",
            "switch to entire",
            "switch to window",
            "set to extended",
            "set to entire",
            "extended display",
            "entire screen",
            "window or app",
        ]
        for phrase in mode_change_phrases:
            if phrase in command_lower:
                score = max(score, 0.95)
                break

        return min(score, 1.0)  # Cap at 1.0

    def _calculate_vision_score(self, words: List[str], command_lower: str) -> float:
        """Calculate likelihood of vision command"""
        score = 0.0

        # Clean words by removing punctuation for better matching
        import re

        clean_words = [re.sub(r"[^\w\s]", "", word) for word in words]

        # EXCLUDE lock/unlock commands - they're system commands, not vision
        if "lock" in clean_words or "unlock" in clean_words:
            return 0.0

        # Vision verbs
        vision_verbs = {
            "see",
            "look",
            "watch",
            "monitor",
            "analyze",
            "describe",
            "show",
            "read",
            "check",
            "examine",
            "find",
            "locate",
            "detect",
            "identify",
        }
        verb_count = sum(1 for word in clean_words if word in vision_verbs)
        score += verb_count * 0.2

        # Common vision question patterns - "can you see", "what do you see", etc.
        vision_question_patterns = [
            "can you see",
            "do you see",
            "what do you see",
            "can you look",
            "can you watch",
            "are you watching",
            "are you looking",
            "is visible",
            "is hidden",
            "is showing",
            "is displayed",
            "visible",
            "icon visible",
            "button visible",
        ]
        for pattern in vision_question_patterns:
            if pattern in command_lower:
                score += 0.6  # Strong boost for explicit vision questions
                break

        # "monitor" or "analyze" with "screen" is definitely vision
        if ("monitor" in clean_words or "analyze" in clean_words) and "screen" in clean_words:
            score += 0.5  # Extra boost for monitor/analyze screen

        # Vision nouns (but be careful with 'screen' - it could be system related)
        vision_nouns = {
            "display",
            "window",
            "image",
            "visual",
            "picture",
            "desktop",
            "space",
            "workspace",
            "screen",
            "icon",
            "button",
            "element",
            "control",
            "ui",
        }
        score += sum(0.15 for word in clean_words if word in vision_nouns)

        # Vision adjectives/state words
        vision_state_words = {
            "visible",
            "hidden",
            "showing",
            "displayed",
            "appearing",
            "present",
        }
        state_count = sum(1 for word in clean_words if word in vision_state_words)
        score += state_count * 0.25  # Strong indicator for vision queries

        # Multi-space indicators (very strong vision signal)
        multi_space_indicators = {
            "desktop",
            "space",
            "spaces",  # Added plural
            "workspace",
            "workspaces",  # Added plural
            "across",
            "multiple",
            "different",
            "other",
            "all",
        }
        multi_space_count = sum(1 for word in clean_words if word in multi_space_indicators)
        if multi_space_count > 0:
            score += 0.4 * multi_space_count  # Strong boost for multi-space queries

        # Extra boost for "desktop spaces" or "workspace" combinations
        if ("desktop" in clean_words and ("space" in clean_words or "spaces" in clean_words)) or (
            "workspace" in clean_words or "workspaces" in clean_words
        ):
            score += 0.3  # Extra boost for these specific combinations

        # 'screen' only counts as vision if paired with vision verbs or multi-space indicators
        if "screen" in clean_words:
            if any(word in vision_verbs for word in clean_words) or multi_space_count > 0:
                score += 0.15
            # Questions about screen are very likely vision
            elif clean_words[0] in {"what", "whats", "show", "display"}:
                score += 0.6  # Strong boost for screen questions

        # Questioning about visual or workspace
        if clean_words and clean_words[0] in {"what", "whats"}:
            visual_indicators = {
                "screen",
                "see",
                "display",
                "desktop",
                "space",
                "workspace",
                "happening",
                "going",
                "doing",
            }
            if any(word in clean_words for word in visual_indicators):
                score += 0.3

        # Phrases that strongly indicate workspace/multi-space vision queries
        workspace_phrases = [
            "desktop space",
            "across my desktop",
            "multiple desktop",
            "different space",
            "what am i working",
            "what is happening",
            "what's happening",
            "what is going on",
            "happening across",
            "across my desktop spaces",
        ]
        for phrase in workspace_phrases:
            if phrase in command_lower:
                score += 0.5

        return min(score, 0.95)

    def _detect_voice_unlock_patterns(self, text: str) -> int:
        """Detect voice unlock related patterns"""
        patterns = 0

        voice_words = {"voice", "vocal", "speech", "voiceprint"}
        unlock_words = {
            "unlock",
            "lock",
            "authenticate",
            "verify",
            "enroll",
            "enrollment",
        }

        # Check for voice + unlock combinations
        has_voice = any(word in text for word in voice_words)
        has_unlock = any(word in text for word in unlock_words)

        if has_voice and has_unlock:
            patterns += 2
        elif has_voice or has_unlock:
            patterns += 1

        # Log for debugging
        if has_voice or has_unlock:
            logger.debug(
                f"Voice unlock pattern detection: text='{text}', has_voice={has_voice}, has_unlock={has_unlock}, patterns={patterns}"
            )

        # Direct phrases - these are definitely voice unlock commands
        voice_unlock_phrases = [
            "voice unlock",
            "unlock with voice",
            "enable voice unlock",
            "disable voice unlock",
            "enroll my voice",
            "enroll voice",
            "voice enrollment",
        ]

        if any(phrase in text for phrase in voice_unlock_phrases):
            patterns += 3  # Strong match

        return patterns

    def _calculate_autonomy_score(self, words: List[str]) -> float:
        """Calculate autonomy command likelihood"""
        score = 0.0

        autonomy_words = {"autonomy", "autonomous", "auto", "automatic", "self"}
        control_words = {"control", "take", "activate", "enable", "mode"}

        score += sum(0.3 for word in words if word in autonomy_words)
        score += sum(0.2 for word in words if word in control_words)

        # Boost for specific phrases
        text = " ".join(words)
        if "take over" in text or "full control" in text:
            score += 0.4

        return min(score, 0.95)

    def _is_question_pattern(self, words: List[str]) -> bool:
        """Detect if command is a question"""
        if not words:
            return False

        # Question starters
        question_starts = {
            "what",
            "who",
            "where",
            "when",
            "why",
            "how",
            "is",
            "are",
            "can",
            "could",
            "would",
            "should",
            "will",
            "do",
            "does",
        }

        # Check first word
        if words[0] in question_starts:
            return True

        # Check for question marks (though unlikely in voice)
        if any("?" in word for word in words):
            return True

        return False

    def _contains_url_pattern(self, text: str) -> bool:
        """Check if text contains URL patterns"""
        # URL indicators
        url_patterns = [
            r"https?://",
            r"www\.",
            r"\.(com|org|net|edu|gov|io|co|uk)",
            r"://",
        ]

        for pattern in url_patterns:
            if re.search(pattern, text):
                return True

        # Common websites without full URLs
        websites = {
            "google",
            "facebook",
            "twitter",
            "youtube",
            "github",
            "amazon",
            "reddit",
        }
        words = text.split()

        # Check if website is mentioned with navigation verb
        nav_verbs = {"go", "visit", "open", "navigate", "browse"}
        for i, word in enumerate(words):
            if word in websites and i > 0 and words[i - 1] in nav_verbs:
                return True

        return False

    async def _resolve_vision_query(self, query: str) -> Dict[str, Any]:
        """
        Two-stage resolution for comprehensive query understanding

        Stage 1 (Implicit Resolver): Entity & Intent Resolution
        - "What does it say?" -> "it" = error in Terminal
        - Intent: DESCRIBE
        - Entity type: error
        - May include space_id from visual attention

        Stage 2 (Contextual Resolver): Space & Monitor Resolution
        - If Stage 1 didn't find space, resolve it now
        - "What's happening?" -> Space 2 (active space)
        - "Compare them" -> Spaces [3, 5] (last queried)

        Returns:
            Dict with comprehensive resolution including:
            - intent: QueryIntent (from implicit resolver)
            - entity: Resolved entity (error, file, etc.)
            - spaces: List[int] (resolved space IDs)
            - confidence: Combined confidence score
        """
        resolution = {
            "original_query": query,
            "resolved": False,
            "query": query,
            "intent": None,
            "entity_resolution": None,
            "space_resolution": None,
            "spaces": None,
            "confidence": 0.0,
        }

        # ============================================================
        # STAGE 1: Implicit Reference Resolution (Entity & Intent)
        # ============================================================
        if self.implicit_resolver:
            try:
                logger.debug(f"[UNIFIED] Stage 1: Implicit resolution for '{query}'")
                implicit_result = await self.implicit_resolver.resolve_query(query)

                # Extract intent
                resolution["intent"] = implicit_result.get("intent")

                # Extract entity referent
                referent = implicit_result.get("referent", {})
                if referent and referent.get("source") != "none":
                    resolution["entity_resolution"] = {
                        "source": referent.get("source"),
                        "type": referent.get("type"),
                        "entity": referent.get("entity"),
                        "confidence": referent.get("relevance", 0.0),
                    }

                    logger.info(
                        f"[UNIFIED] Stage 1 ✅: Intent={resolution['intent']}, "
                        f"Entity={referent.get('type')} from {referent.get('source')}"
                    )

                    # If implicit resolver found a specific space, use it (high confidence!)
                    if referent.get("space_id"):
                        resolution["spaces"] = [referent["space_id"]]
                        resolution["space_resolution"] = {
                            "strategy": "implicit_reference",
                            "confidence": 1.0,
                            "source": "visual_attention",
                        }
                        resolution["resolved"] = True
                        resolution["confidence"] = implicit_result.get("confidence", 0.9)

                        # Enhance query with entity and space info
                        entity_desc = referent.get("entity", "")[:50]
                        resolution["query"] = (
                            f"{query} [entity: {entity_desc}, space: {referent['space_id']}]"
                        )

                        logger.info(
                            f"[UNIFIED] Stage 1 complete: Space {referent['space_id']} from implicit resolver"
                        )
                        return resolution

            except Exception as e:
                logger.warning(f"[UNIFIED] Stage 1 error: {e}", exc_info=True)

        # ============================================================
        # STAGE 2: Contextual Space Resolution (if needed)
        # ============================================================
        if self.contextual_resolver:
            try:
                logger.debug(f"[UNIFIED] Stage 2: Contextual space resolution for '{query}'")
                space_result = await self.contextual_resolver.resolve_query(query)

                if space_result.requires_clarification:
                    # Query is too ambiguous
                    resolution["clarification_needed"] = True
                    resolution["clarification_message"] = space_result.clarification_message
                    logger.info(f"[UNIFIED] Stage 2: Clarification needed")
                    return resolution

                if space_result.success and space_result.resolved_spaces:
                    # Successfully resolved spaces
                    spaces = space_result.resolved_spaces
                    strategy = space_result.strategy_used.value

                    resolution["spaces"] = spaces
                    resolution["space_resolution"] = {
                        "strategy": strategy,
                        "confidence": space_result.confidence,
                        "monitors": space_result.resolved_monitors,
                    }
                    resolution["resolved"] = True

                    # Calculate combined confidence
                    if resolution["entity_resolution"]:
                        # Both stages succeeded
                        entity_conf = resolution["entity_resolution"]["confidence"]
                        space_conf = space_result.confidence
                        resolution["confidence"] = (entity_conf + space_conf) / 2
                    else:
                        # Only space resolution
                        resolution["confidence"] = space_result.confidence

                    # Enhance query with space info (and entity if available)
                    enhanced_query = query
                    if resolution["entity_resolution"]:
                        entity = resolution["entity_resolution"]["entity"][:50]
                        enhanced_query = f"{query} [entity: {entity}]"

                    if len(spaces) == 1:
                        enhanced_query = f"{enhanced_query} [space {spaces[0]}]"
                    elif len(spaces) > 1:
                        enhanced_query = f"{enhanced_query} [spaces {', '.join(map(str, spaces))}]"

                    resolution["query"] = enhanced_query

                    logger.info(
                        f"[UNIFIED] Stage 2 ✅: Resolved to spaces {spaces} "
                        f"using {strategy} (confidence: {space_result.confidence})"
                    )

            except Exception as e:
                logger.warning(f"[UNIFIED] Stage 2 error: {e}", exc_info=True)

        # ============================================================
        # FALLBACK: No resolution
        # ============================================================
        if not resolution["resolved"]:
            logger.debug(f"[UNIFIED] No resolution available for '{query}' - using original query")
            resolution["query"] = query
            resolution["confidence"] = 0.0

        return resolution

    def record_visual_attention(
        self,
        space_id: int,
        app_name: str,
        ocr_text: str,
        content_type: str = "unknown",
        significance: str = "normal",
    ):
        """
        Record visual attention for implicit reference resolution

        This creates a feedback loop where vision analysis feeds into the
        implicit resolver's visual attention tracker.

        Args:
            space_id: The space where content was seen
            app_name: The application displaying the content
            ocr_text: OCR text from the screen
            content_type: Type of content (error, code, documentation, terminal_output)
            significance: Importance level (critical, high, normal, low)
        """
        if not self.implicit_resolver:
            return

        try:
            self.implicit_resolver.record_visual_attention(
                space_id=space_id,
                app_name=app_name,
                ocr_text=ocr_text,
                content_type=content_type,
                significance=significance,
            )
            logger.debug(
                f"[UNIFIED] Recorded visual attention: {content_type} in {app_name} "
                f"(Space {space_id}, significance={significance})"
            )
        except Exception as e:
            logger.warning(f"[UNIFIED] Failed to record visual attention: {e}")

    def _is_multi_space_query(
        self, query: str
    ) -> bool:  # check if the query is about multiple spaces
        """
        Detect if a query is asking about multiple spaces.

        Examples:
        - "Compare space 3 and space 5"
        - "Which space has the error?"
        - "Find the terminal across all spaces"
        - "What's different between space 1 and space 2?"
        """
        query_lower = query.lower()  # convert the query to lowercase

        # Keywords that indicate multi-space queries
        multi_space_keywords = [
            "compare",
            "difference",
            "different",
            "find",
            "which space",
            "across",
            "all spaces",
            "search",
            "locate",
        ]

        # Check for keywords
        if any(
            keyword in query_lower for keyword in multi_space_keywords
        ):  # if any of the keywords are in the query, it's a multi-space query
            return True  # return True if it's a multi-space query

        # Check for multiple space mentions
        import re

        space_matches = re.findall(
            r"space\s+\d+", query_lower
        )  # find all space mentions in the query
        if (
            len(space_matches) >= 2
        ):  # if there are at least two space mentions, it's a multi-space query
            return True  # return True if it's a multi-space query

        return False  # return False if it's not a multi-space query

    # Function to handle multi-space queries
    async def _handle_multi_space_query(self, query: str) -> Dict[str, Any]:
        """
        Handle multi-space queries using the MultiSpaceQueryHandler.

        Args:
            query: User's multi-space query

        Returns:
            Dict with comprehensive multi-space analysis
        """
        if not self.multi_space_handler:  # if the multi-space handler is not available
            # Fallback: treat as regular vision query
            logger.warning("[UNIFIED] Multi-space query detected but handler not available")
            return {
                "success": False,  # indicate failure
                "response": "Multi-space analysis not available. Please specify a single space.",  # add error message
                "multi_space": False,  # indicate that it's not a multi-space query
            }

        try:
            logger.info(f"[UNIFIED] Handling multi-space query: '{query}'")

            # Use the multi-space handler
            result = await self.multi_space_handler.handle_query(
                query
            )  # handle the multi-space query

            # Build response with the results of the multi-space query
            response = {
                "success": True,  # indicate success
                "response": result.synthesis,  # add the synthesis to the response
                "multi_space": True,  # indicate that it's a multi-space query
                "query_type": result.query_type.value,  # add the query type to the response
                "spaces_analyzed": result.spaces_analyzed,  # add the spaces analyzed to the response
                "results": [
                    {
                        "space_id": r.space_id,  # add the space id to the response
                        "success": r.success,  # add the success to the response
                        "app": r.app_name,  # add the app name to the response
                        "content_type": r.content_type,  # add the content type to the response
                        "summary": r.content_summary,  # add the content summary to the response
                        "errors": r.errors,  # add the errors to the response
                        "significance": r.significance,  # add the significance to the response
                    }
                    for r in result.results  # loop through the results
                ],
                "confidence": result.confidence,  # add the confidence to the response
                "analysis_time": result.total_time,  # add the analysis time to the response
            }

            # Add comparison if available
            if result.comparison:  # if there is a comparison, add it to the response
                response["comparison"] = result.comparison  # add the comparison to the response

            # Add differences if available
            if result.differences:  # if there is a difference, add it to the response
                response["differences"] = result.differences  # add the difference to the response

            # Add search matches if available
            if result.search_matches:  # if there is a search match, add it to the response
                response["search_matches"] = (
                    result.search_matches
                )  # add the search match to the response

            logger.info(
                f"[UNIFIED] Multi-space query completed: "
                f"{len(result.spaces_analyzed)} spaces analyzed in {result.total_time:.2f}s"
            )

            return response

        except Exception as e:
            logger.error(f"[UNIFIED] Multi-space query failed: {e}", exc_info=True)
            return {
                "success": False,
                "response": f"Multi-space analysis failed: {str(e)}",
                "multi_space": True,
                "error": str(e),
            }

    def _is_temporal_query(self, query: str) -> bool:
        """
        Detect if a query is temporal (time-based, change detection, error tracking).

        Examples:
        - "What changed in space 3?"
        - "Has the error been fixed?"
        - "What's new in the last 5 minutes?"
        - "When did this error first appear?"
        """
        query_lower = query.lower()

        # Keywords that indicate temporal queries
        temporal_keywords = [
            "changed",
            "change",
            "different",
            "fixed",
            "error",
            "bug",
            "issue",
            "new",
            "recently",
            "last",
            "when",
            "history",
            "timeline",
            "appeared",
            "first",
            "started",
            "ago",
            "since",
            "before",
            "after",
            "latest",
            "recent",
            "past",
        ]

        # Check for keywords
        if any(keyword in query_lower for keyword in temporal_keywords):
            return True

        # Check for time expressions
        import re

        time_patterns = [
            r"\d+\s+(minute|hour|day|second)s?\s+ago",
            r"last\s+\d+\s+(minute|hour|day|second)s?",
            r"in\s+the\s+last",
            r"(today|yesterday|recently|just now)",
        ]

        for pattern in time_patterns:
            if re.search(pattern, query_lower):
                return True

        return False

    async def _handle_temporal_query(self, query: str) -> Dict[str, Any]:
        """
        Handle temporal queries using the TemporalQueryHandler.

        Args:
            query: User's temporal query

        Returns:
            Dict with temporal analysis results
        """
        if not self.temporal_handler:
            # Fallback: treat as regular query
            logger.warning("[UNIFIED] Temporal query detected but handler not available")
            return {
                "success": False,
                "response": "Temporal analysis not available. Cannot track changes over time.",
                "temporal": False,
            }

        try:
            logger.info(f"[UNIFIED] Handling temporal query: '{query}'")

            # Get current space (or from query)
            space_id = None
            import re

            space_match = re.search(r"space\s+(\d+)", query.lower())
            if space_match:
                space_id = int(space_match.group(1))

            # Use the temporal handler
            result = await self.temporal_handler.handle_query(query, space_id)

            # Build response
            response = {
                "success": True,
                "response": result.summary,
                "temporal": True,
                "query_type": result.query_type.name,
                "time_range": {
                    "start": result.time_range.start.isoformat(),
                    "end": result.time_range.end.isoformat(),
                    "duration_seconds": result.time_range.duration_seconds,
                },
                "changes": [
                    {
                        "type": change.change_type.value,
                        "description": change.description,
                        "confidence": change.confidence,
                        "timestamp": change.timestamp.isoformat(),
                        "space_id": change.space_id,
                    }
                    for change in result.changes
                ],
                "timeline": result.timeline,
                "screenshot_count": len(result.screenshots),
            }

            # Add metadata if available
            if result.metadata:
                response["metadata"] = result.metadata

            logger.info(
                f"[UNIFIED] Temporal query completed: "
                f"{len(result.changes)} changes detected over {result.time_range.duration_seconds:.0f}s"
            )

            return response

        except Exception as e:
            logger.error(f"[UNIFIED] Temporal query failed: {e}", exc_info=True)
            return {
                "success": False,
                "response": f"Temporal analysis failed: {str(e)}",
                "temporal": True,
                "error": str(e),
            }

    async def _get_full_system_context(self) -> Dict[str, Any]:
        """Get comprehensive system context for intelligent command processing"""
        try:
            from context_intelligence.detectors.screen_lock_detector import get_screen_lock_detector

            screen_detector = get_screen_lock_detector()
            is_locked = await screen_detector.is_screen_locked()

            # Get active applications (you can expand this)
            active_apps = []
            try:
                import subprocess

                result = subprocess.run(
                    [
                        "osascript",
                        "-e",
                        'tell application "System Events" to get name of (processes where background only is false)',
                    ],
                    capture_output=True,
                    text=True,
                    timeout=2,
                )
                if result.returncode == 0:
                    active_apps = result.stdout.strip().split(", ")
            except Exception:
                pass

            return {
                "screen_locked": is_locked,
                "active_apps": active_apps,
                "network_connected": True,  # You can expand this check
                "timestamp": datetime.now().isoformat(),
                "user_preferences": self.context.user_preferences,
                "conversation_history": len(self.context.conversation_history),
            }
        except Exception as e:
            logger.warning(f"Could not get full system context: {e}")
            return {
                "screen_locked": False,
                "active_apps": [],
                "network_connected": True,
                "timestamp": datetime.now().isoformat(),
            }

    async def _get_fast_system_context(self) -> Dict[str, Any]:
        """
        Get lightweight system context for fast command routing.
        Avoids heavy AppleScript calls or window server queries.
        """
        try:
            # Check screen lock using non-blocking methods
            from core.transport_handlers import _is_locked_now
            
            is_locked = _is_locked_now()
            
            # If detection failed/unavailable, assume unlocked to avoid blocking operations
            if is_locked is None:
                is_locked = False
                
            return {
                "screen_locked": is_locked,
                "active_apps": [], # Skip app list for speed
                "timestamp": datetime.now().isoformat(),
                "fast_mode": True
            }
        except Exception as e:
            logger.warning(f"Fast system context failed: {e}")
            return {"screen_locked": False, "active_apps": [], "fast_mode": True}

    async def _execute_command(
        self,
        command_type: CommandType,
        command_text: str,
        websocket=None,
        context: Dict[str, Any] = None,
        deadline: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        v88.0: Execute command with ultra protection stack.

        Protection includes:
        - Adaptive circuit breaker with ML-based prediction
        - Backpressure handling with AIMD rate limiting
        - W3C distributed tracing
        - Timeout enforcement
        """
        # v88.0: Use ultra coordinator protection if available
        ultra_coord = await _get_ultra_coordinator()
        if ultra_coord:
            # v241.0: Cap ultra coordinator timeout to remaining deadline budget
            from core.prime_router import compute_remaining
            timeout = compute_remaining(deadline, float(os.getenv("JARVIS_COMMAND_EXECUTION_TIMEOUT", "120.0")))
            success, result, metadata = await ultra_coord.execute_with_protection(
                component=f"command_{command_type.value}",
                operation=lambda: self._execute_command_internal(
                    command_type, command_text, websocket, context, deadline=deadline
                ),
                timeout=timeout,
            )
            if success and result is not None:
                # Inject v88.0 trace info into result
                if isinstance(result, dict):
                    result["v88_protected"] = True
                    if "trace_id" in metadata:
                        result["v88_trace_id"] = metadata["trace_id"]
                return result
            elif not success:
                error_msg = metadata.get("error", "Unknown protection error")
                logger.warning(f"[UnifiedProcessor] v88.0 Protection failed: {error_msg}")
                return {
                    "success": False,
                    "response": "I'm experiencing some difficulties. Please try again.",
                    "command_type": command_type.value,
                    "v88_error": error_msg,
                    "circuit_open": metadata.get("circuit_open", False),
                }

        # Fallback: direct execution without protection
        return await self._execute_command_internal(
            command_type, command_text, websocket, context
        )

    async def _execute_command_internal(
        self,
        command_type: CommandType,
        command_text: str,
        websocket=None,
        context: Dict[str, Any] = None,
        deadline: Optional[float] = None,
    ) -> Dict[str, Any]:
        """v88.0: Internal command execution (called by protection wrapper)"""

        # =========================================================================
        # 🛡️ SOVEREIGN SURVEILLANCE ROUTING v1.0.0
        # =========================================================================
        # Goal: Zero-Leak Routing. Surveillance commands NEVER hit the Cloud API.
        # Strategy: Dynamic Grammar + Semantic Triangulation + Hard Circuit Breaker.
        # =========================================================================

        command_lower = command_text.lower().strip()

        # --- LAYER 1: DYNAMIC GRAMMAR ENGINE ---
        # Matches: "all [ANY APP] windows", "every [ANY APP] tab", "each instance"
        god_mode_pattern = r"\b(all|every|each)\s+.*?\s*(windows?|tabs?|instances?|spaces?)\b"
        has_multi_target = bool(re.search(god_mode_pattern, command_lower, re.IGNORECASE))

        # --- LAYER 2: SEMANTIC TRIANGULATION ---
        watch_keywords = ['watch', 'monitor', 'track', 'alert when', 'notify when', 'detect', 'scan']
        triggers = ['for', 'when', 'until', 'if', 'whenever']

        has_watch = any(k in command_lower for k in watch_keywords)
        has_trigger = any(t in command_lower for t in triggers)

        # High-Confidence Classification
        is_surveillance_intent = has_multi_target or (has_watch and has_trigger)

        if has_watch or has_multi_target:
            logger.info(
                f"[SOVEREIGN] Intent Analysis: '{command_text}' | "
                f"Grammar={has_multi_target}, Keywords={has_watch}, Structure={has_trigger} | "
                f"DECISION={'🔒 LOCAL SURVEILLANCE' if is_surveillance_intent else '☁️ VISION'}"
            )

        # --- LAYER 3: SOVEREIGNTY CIRCUIT BREAKER ---
        if is_surveillance_intent:
            logger.info(f"[SOVEREIGN] 🔒 Enforcing Local-Only Routing for: '{command_text}'")

            try:
                # 3a. RESILIENT PATH INJECTION
                _current_file = os.path.abspath(__file__)
                _api_dir = os.path.dirname(_current_file)
                _backend_root = os.path.dirname(_api_dir)
                _project_root = os.path.dirname(_backend_root)

                for _path in [_backend_root, _project_root]:
                    if _path not in sys.path:
                        sys.path.insert(0, _path)

                # 3b. LOCAL MODULE LOADING
                from voice.intelligent_command_handler import IntelligentCommandHandler
                intelligent_handler = IntelligentCommandHandler()
                logger.info("[SOVEREIGN] ✅ Local surveillance handler loaded")

                # 3c. ASYNC EXECUTION WITH DYNAMIC TIMEOUT (v31.0)
                # Surveillance needs more time for multi-window initialization
                surveillance_timeout = float(os.getenv("JARVIS_SURVEILLANCE_HANDLER_TIMEOUT", "60"))
                logger.info(f"[SOVEREIGN] Using {surveillance_timeout}s timeout for surveillance")
                result = await asyncio.wait_for(
                    intelligent_handler.handle_command(command_text),
                    timeout=surveillance_timeout
                )

                # 3d. NORMALIZE RESPONSE
                if isinstance(result, tuple) and len(result) == 2:
                    response_text, handler_used = result
                elif isinstance(result, dict):
                    response_text = result.get("response", "Monitoring initiated")
                    handler_used = result.get("handler", "surveillance")
                else:
                    response_text = str(result) if result else "Monitoring initiated"
                    handler_used = "surveillance"

                logger.info(f"[SOVEREIGN] ✅ Local execution success: {response_text[:80]}...")

                return {
                    "success": True,
                    "response": response_text,
                    "command_type": "surveillance",
                    "handler_used": handler_used,
                    "routing": "sovereign_local_only",
                    "cloud_blocked": True,
                }

            except asyncio.TimeoutError:
                logger.error(f"[SOVEREIGN] ⏱️ Local handler timeout after {handler_timeout}s")
                return {
                    "success": False,
                    "response": "My local surveillance system timed out. Cloud fallback blocked for privacy.",
                    "command_type": "surveillance",
                    "error": "local_timeout",
                    "circuit_breaker_tripped": True,
                }

            except Exception as e:
                # 3e. HARD CIRCUIT BREAKER - NEVER fall through to cloud
                logger.critical(f"[SOVEREIGN] ❌ Local surveillance failure: {e}", exc_info=True)
                return {
                    "success": False,
                    "response": f"My local surveillance core failed. Cloud fallback blocked for privacy. (Error: {str(e)[:100]})",
                    "command_type": "surveillance",
                    "error": "local_module_crash",
                    "circuit_breaker_tripped": True,
                }

        # =========================================================================
        # END SOVEREIGN ROUTING - Non-surveillance commands continue below
        # =========================================================================

        # Get or initialize handler
        if command_type not in self.handlers:
            handler = await self._get_handler(command_type)
            if handler:
                self.handlers[command_type] = handler

        handler = self.handlers.get(command_type)

        if not handler and command_type not in [
            CommandType.SYSTEM,
            CommandType.META,
            CommandType.DOCUMENT,
            CommandType.DISPLAY,  # Display has dedicated handler below
        ]:
            return {
                "success": False,
                "response": f"I don't have a handler for {command_type.value} commands yet.",
                "command_type": command_type.value,
            }

        # Execute with unified context
        try:
            # Different handlers have different interfaces, normalize them
            if command_type == CommandType.VISION:
                # =====================================================================
                # ROOT CAUSE FIX: Intent Disambiguation v2.0.0
                # =====================================================================
                # PROBLEM: "Watch all Chrome windows for bouncing ball" was being
                # routed to VisionCommandHandler (returns "Application window active")
                # instead of VisualMonitorAgent (background surveillance).
                #
                # SOLUTION: Detect Surveillance Intent before simple vision analysis
                # =====================================================================

                # Load configurable surveillance patterns (no hardcoding!)
                import os
                monitoring_keywords = os.getenv(
                    "JARVIS_MONITORING_KEYWORDS",
                    "watch,monitor,track,alert when,notify when,detect when,look for,scan for"
                ).split(",")
                monitoring_keywords = [k.strip() for k in monitoring_keywords]

                # Load surveillance structure patterns (no hardcoding!)
                surveillance_patterns = os.getenv(
                    "JARVIS_SURVEILLANCE_PATTERNS",
                    "for,when,until,if,whenever"
                ).split(",")
                surveillance_patterns = [p.strip() for p in surveillance_patterns]

                command_lower = command_text.lower()

                # =====================================================================
                # ROOT CAUSE FIX: Grammar-Based Intent Routing v3.0.0
                # =====================================================================
                # PROBLEM: Hardcoded phrase lists ("all chrome", "all safari") are brittle
                # - Fails for new apps (Arc, VSCode, Discord, etc.)
                # - Requires manual updates for every new application
                #
                # SOLUTION: Grammar-Based Routing using Regex
                # - Matches sentence STRUCTURE, not specific words
                # - Works for ANY application dynamically
                # - Zero hardcoding of app names
                #
                # CLINICAL-GRADE PATTERN: Grammar-Based Slot Filling
                # - Inspired by production voice assistants (Siri, Alexa)
                # - Uses regex to extract grammatical structure
                # - Supports ANY app without pre-configuration
                # =====================================================================
                # Note: 're' module already imported at module level (line 9)

                # Dynamic Grammar Pattern (Environment-Configurable)
                # Pattern: \b(QUANTIFIER)\s+(?:[APP_NAME]\s+)?(TARGET_TYPE)\b
                #
                # Matches: "all [APP] windows", "every [APP] tab", "each [APP] instance"
                #
                # ✅ EXAMPLES THAT NOW WORK UNIVERSALLY (Zero Hardcoding):
                #
                #   Browser Apps:
                #   - "watch all Chrome windows for bouncing ball" ✓
                #   - "monitor every Arc tab for error message" ✓
                #   - "track each Safari window for download complete" ✓
                #   - "watch all Firefox tabs for login success" ✓
                #   - "scan all Brave windows for notification" ✓
                #
                #   Developer Apps:
                #   - "watch all VSCode windows for build complete" ✓
                #   - "monitor every Terminal instance for deployment done" ✓
                #   - "track all IntelliJ windows for test passed" ✓
                #   - "watch each PyCharm tab for debug breakpoint" ✓
                #   - "monitor all Xcode windows for compile success" ✓
                #
                #   Communication Apps:
                #   - "watch all Slack windows for Derek mentioned" ✓
                #   - "monitor every Discord tab for new message" ✓
                #   - "track all Teams windows for meeting started" ✓
                #   - "watch each Zoom window for participant joined" ✓
                #
                #   Creative Apps:
                #   - "watch all Figma tabs for comment added" ✓
                #   - "monitor every Photoshop window for export complete" ✓
                #   - "track all Canva windows for download ready" ✓
                #
                #   ANY Other App:
                #   - "watch all Spotify windows for song title" ✓
                #   - "monitor every Notes tab for save complete" ✓
                #   - "track each Calendar window for event reminder" ✓
                #
                # =====================================================================
                # ROOT CAUSE FIX: Robust Pattern Matching v8.0.0
                # =====================================================================
                # PROBLEM: Strict regex `(?:[\w\s]+\s+)?` fails on some app names
                # - "watch all chrome windows" sometimes doesn't match
                # - Falls back to VisionHandler → "Application window active"
                #
                # SOLUTION: More aggressive pattern + fallback simple patterns
                # - Use `.*?` (non-greedy wildcard) instead of `(?:[\w\s]+\s+)?`
                # - Add simple fallback patterns for edge cases
                # - Explicit logging to prove detection works
                # =====================================================================

                # Grammar Pattern Components:
                # - (all|every|each) = Quantifier (God Mode trigger)
                # - .*? = Non-greedy wildcard (matches ANY app name)
                # - (windows?|tabs?|instances?|spaces?) = Target type
                #
                # AGGRESSIVE PATTERN: Matches "all [ANYTHING] windows"
                god_mode_pattern = os.getenv(
                    "JARVIS_GOD_MODE_GRAMMAR_PATTERN",
                    r"\b(all|every|each)\s+.*?\s*(windows?|tabs?|instances?|spaces?)\b"
                )

                # Fallback simple patterns (if main pattern fails)
                simple_patterns = [
                    r"\ball\s+.*?\s+windows?\b",      # "all ... window(s)"
                    r"\bevery\s+.*?\s+windows?\b",    # "every ... window(s)"
                    r"\beach\s+.*?\s+windows?\b",     # "each ... window(s)"
                    r"\ball\s+.*?\s+tabs?\b",         # "all ... tab(s)"
                    r"\bevery\s+.*?\s+tabs?\b",       # "every ... tab(s)"
                ]

                # Intelligent pattern matching:
                # - Must have monitoring keyword AND surveillance structure
                # - Examples: "watch Chrome FOR ball", "monitor windows WHEN error"
                has_monitoring_keyword = any(k in command_lower for k in monitoring_keywords)
                has_surveillance_structure = any(p in command_lower for p in surveillance_patterns)

                # v242.0 Gap J: Negative patterns prevent misclassification
                _non_surv_kw2 = os.getenv(
                    "JARVIS_NON_SURVEILLANCE_KEYWORDS",
                    "youtube,video,movie,show,episode,series,stream,play,recipe,flight,"
                    "hotel,restaurant,information,tutorial,guide,how to,what is,explain"
                ).split(",")
                _non_surv_kw2 = [k.strip() for k in _non_surv_kw2 if k.strip()]
                _has_non_surv2 = any(k in command_lower for k in _non_surv_kw2)

                is_surveillance_command = (
                    has_monitoring_keyword and has_surveillance_structure and not _has_non_surv2
                )

                # Grammar-Based Multi-Target Detection (AGGRESSIVE!)
                # No hardcoded app names - matches grammatical structure
                has_multi_target = bool(re.search(god_mode_pattern, command_lower, re.IGNORECASE))

                # Fallback: Try simple patterns if main pattern didn't match
                if not has_multi_target:
                    for simple_pattern in simple_patterns:
                        if re.search(simple_pattern, command_lower, re.IGNORECASE):
                            has_multi_target = True
                            logger.debug(
                                f"[INTENT] Fallback pattern matched: '{simple_pattern}' in '{command_text}'"
                            )
                            break

                # If monitoring keyword + grammar pattern detected = surveillance (strong signal)
                if has_monitoring_keyword and has_multi_target:
                    is_surveillance_command = True

                # Extract grammar match details for logging
                grammar_match = re.search(god_mode_pattern, command_lower, re.IGNORECASE)
                grammar_matched_text = grammar_match.group(0) if grammar_match else None

                # =====================================================================
                # EXPLICIT LOGGING: Prove detection works
                # =====================================================================
                logger.info(
                    f"[INTENT] Surveillance Check v8.0.0: '{command_text}' -> "
                    f"IsSurveillance={is_surveillance_command} "
                    f"(monitoring={has_monitoring_keyword}, "
                    f"structure={has_surveillance_structure}, "
                    f"multi_target={has_multi_target}, "
                    f"grammar='{grammar_matched_text}')"
                )

                logger.debug(
                    f"[INTENT] Grammar-Based Disambiguation v8.0.0: "
                    f"monitoring_keyword={has_monitoring_keyword}, "
                    f"surveillance_structure={has_surveillance_structure}, "
                    f"multi_target={has_multi_target}, "
                    f"grammar_match='{grammar_matched_text}', "
                    f"is_surveillance={is_surveillance_command}"
                )

                if is_surveillance_command:
                    # =====================================================================
                    # SURVEILLANCE INTENT: Route to IntelligentCommandHandler
                    # =====================================================================
                    if grammar_matched_text:
                        logger.info(
                            f"[UNIFIED] 👁️ Surveillance Intent Detected (Grammar: '{grammar_matched_text}'): "
                            f"'{command_text}' -> Routing to Neural Mesh (VisualMonitorAgent)"
                        )
                    else:
                        logger.info(
                            f"[UNIFIED] 👁️ Surveillance Intent Detected: '{command_text}' "
                            f"-> Routing to Neural Mesh (VisualMonitorAgent)"
                        )

                    try:
                        # =====================================================================
                        # ROOT CAUSE FIX: Robust Type Handling & Async Safety v4.0.0
                        # =====================================================================
                        # PROBLEM: IntelligentCommandHandler.handle_command() returns Tuple[str, str]
                        # - Router expected dict/string
                        # - Type mismatch caused silent crash
                        # - Fell back to legacy VisionCommandHandler ("Application window active")
                        #
                        # SOLUTION: Robust unpacking for ALL return types (tuple, dict, string)
                        # =====================================================================

                        # =====================================================================
                        # ROOT CAUSE FIX v10.0.0: Dynamic Path Injection for Local Brain
                        # =====================================================================
                        # PROBLEM: Import fails because Python can't find 'voice' package
                        # - Causes fallback to expensive Claude API (401 error or cost)
                        # - User sees "Application window active" instead of surveillance
                        #
                        # SOLUTION: Dynamically inject backend path BEFORE import
                        # - Calculate paths relative to THIS file's location
                        # - No hardcoding - works regardless of installation location
                        # - Prevents cloud fallback by loading local handler
                        # =====================================================================
                        import sys

                        # Calculate paths dynamically from THIS file's location
                        _current_file = os.path.abspath(__file__)
                        _api_dir = os.path.dirname(_current_file)           # backend/api/
                        _backend_root = os.path.dirname(_api_dir)           # backend/
                        _project_root = os.path.dirname(_backend_root)      # project root

                        # Inject paths if not present (order matters: backend first)
                        for _inject_path in [_backend_root, _project_root]:
                            if _inject_path not in sys.path:
                                sys.path.insert(0, _inject_path)
                                logger.debug(f"[UNIFIED] Injected path: {_inject_path}")

                        # Now import the local handler (should find 'voice' package)
                        from voice.intelligent_command_handler import IntelligentCommandHandler

                        logger.info("[UNIFIED] ✅ Local Handler Loaded Successfully")

                        # Initialize handler dynamically
                        intelligent_handler = IntelligentCommandHandler()

                        # =====================================================================
                        # v31.0: Dynamic Timeout Based on Command Type
                        # =====================================================================
                        # Surveillance commands need more time for multi-window setup
                        # Regular commands use standard timeout
                        if command_type == CommandType.SURVEILLANCE:
                            handler_timeout = float(os.getenv("JARVIS_SURVEILLANCE_HANDLER_TIMEOUT", "60"))
                            logger.info(f"[UNIFIED] Using extended {handler_timeout}s timeout for surveillance")
                        else:
                            handler_timeout = float(os.getenv("JARVIS_HANDLER_TIMEOUT", "30"))

                        try:
                            result = await asyncio.wait_for(
                                intelligent_handler.handle_command(command_text),
                                timeout=handler_timeout
                            )
                        except asyncio.TimeoutError:
                            logger.error(
                                f"[UNIFIED] IntelligentCommandHandler timed out after {handler_timeout}s"
                            )
                            timeout_msg = (
                                f"Surveillance setup timed out after {handler_timeout:.0f} seconds. "
                                "The system may be initializing multiple windows. Please try again."
                                if command_type == CommandType.SURVEILLANCE
                                else f"I'm having trouble processing that command, {self.user_name}. The system is taking longer than expected."
                            )
                            return {
                                "success": False,
                                "response": timeout_msg,
                                "command_type": command_type.value,
                                "error": "handler_timeout",
                            }

                        # =====================================================================
                        # Robust Type Handling: Unpack tuple/dict/string
                        # =====================================================================
                        response_text = None
                        handler_used = None

                        # TYPE 1: Tuple (response_text, handler_type)
                        if isinstance(result, tuple) and len(result) == 2:
                            response_text, handler_used = result
                            logger.debug(
                                f"[UNIFIED] Unpacked tuple: response='{response_text[:50]}...', "
                                f"handler='{handler_used}'"
                            )

                        # TYPE 2: Dictionary
                        elif isinstance(result, dict):
                            response_text = result.get("response", result.get("text", "Monitoring initiated"))
                            handler_used = result.get("handler", result.get("type", "surveillance"))
                            logger.debug(
                                f"[UNIFIED] Unpacked dict: response='{response_text[:50] if response_text else 'None'}...', "
                                f"handler='{handler_used}'"
                            )

                        # TYPE 3: String (direct response)
                        elif isinstance(result, str):
                            response_text = result
                            handler_used = "surveillance"
                            logger.debug(
                                f"[UNIFIED] Got string response: '{response_text[:50]}...'"
                            )

                        # TYPE 4: Unknown (shouldn't happen, but handle gracefully)
                        else:
                            logger.warning(
                                f"[UNIFIED] Unexpected result type from IntelligentCommandHandler: {type(result)}"
                            )
                            response_text = str(result) if result else "Monitoring initiated"
                            handler_used = "unknown"

                        # Ensure we have a valid response
                        if not response_text:
                            response_text = "Monitoring initiated"

                        # =====================================================================
                        # Build normalized response dictionary
                        # =====================================================================
                        normalized_result = {
                            "handled": True,
                            "success": True,
                            "response": response_text,
                            "command_type": "surveillance",
                            "handler_used": handler_used,
                            # Add intent metadata with grammar-based routing details
                            "intent_disambiguation": {
                                "detected_intent": "surveillance",
                                "routed_to": "IntelligentCommandHandler->VisualMonitorAgent",
                                "routing_method": "grammar-based_v8.0.0",  # Updated to v8.0.0
                                "keywords_matched": [k for k in monitoring_keywords if k in command_lower],
                                "patterns_matched": [p for p in surveillance_patterns if p in command_lower],
                                "grammar_match": grammar_matched_text,
                                "god_mode_detected": has_multi_target,
                            }
                        }

                        logger.info(
                            f"[UNIFIED] ✅ Surveillance command handled successfully: "
                            f"handler='{handler_used}', response_length={len(response_text)}"
                        )

                        return {
                            "success": True,
                            "response": response_text,
                            "command_type": command_type.value,
                            **normalized_result,
                        }

                    except ImportError as e:
                        # =====================================================================
                        # ROOT CAUSE FIX: Prevent Fallback to Wrong Handler v8.0.0
                        # =====================================================================
                        # PROBLEM: Import error causes silent fallback to VisionHandler
                        # - User gets "Application window active" instead of error
                        #
                        # SOLUTION: Return error immediately, don't fall through
                        # =====================================================================
                        logger.error(
                            f"[UNIFIED] ❌ Failed to load IntelligentCommandHandler: {e}. "
                            f"Surveillance routing BLOCKED - returning error instead of fallback."
                        )
                        return {
                            "success": False,
                            "response": f"I couldn't load my surveillance system, {self.user_name}. "
                                       f"The IntelligentCommandHandler is unavailable. "
                                       f"Error: {str(e)}",
                            "command_type": command_type.value,
                            "error": "import_error",
                            "error_details": str(e),
                        }

                    except Exception as e:
                        # =====================================================================
                        # ROOT CAUSE FIX: Prevent Fallback to Wrong Handler v8.0.0
                        # =====================================================================
                        # PROBLEM: Generic exception causes silent fallback to VisionHandler
                        # - User gets "Application window active" instead of error
                        #
                        # SOLUTION: Return error immediately, don't fall through
                        # =====================================================================
                        logger.error(
                            f"[UNIFIED] ❌ Surveillance command failed: {e}. "
                            f"Returning error instead of fallback.",
                            exc_info=True
                        )
                        return {
                            "success": False,
                            "response": f"I encountered an error setting up monitoring, {self.user_name}: {str(e)}",
                            "command_type": command_type.value,
                            "error": "execution_error",
                            "error_details": str(e),
                        }

                # =====================================================================
                # CIRCUIT BREAKER v1.0.0: Block Cloud Fallback for Surveillance
                # =====================================================================
                # PROBLEM: If is_surveillance_command was True but IntelligentCommandHandler
                # failed/crashed, the code falls through to generic vision handling which
                # calls expensive Claude API → 401 Invalid API Key error or $$ charges.
                #
                # SOLUTION: Re-check surveillance intent here. If this was a surveillance
                # command, BLOCK cloud fallback and return clean error instead.
                #
                # WHY HERE: This is the last line of defense before vision_router.execute_query()
                # or handler.analyze_screen() which both hit Claude API.
                # =====================================================================

                # Re-detect surveillance intent (defensive - in case we fell through somehow)
                _cb_watch_keywords = ['watch', 'monitor', 'track', 'alert when', 'notify when',
                                      'detect when', 'look for', 'scan for', 'observe']
                _cb_surveillance_triggers = ['for', 'when', 'until', 'if', 'whenever', 'while']
                _cb_god_mode_pattern = r'\b(all|every|each)\s+.*?\s*(windows?|tabs?|instances?|spaces?)\b'

                _cb_command_lower = command_text.lower()
                _cb_is_watch = any(k in _cb_command_lower for k in _cb_watch_keywords)
                _cb_is_trigger = any(t in _cb_command_lower for t in _cb_surveillance_triggers)
                _cb_is_multi_target = bool(re.search(_cb_god_mode_pattern, _cb_command_lower, re.IGNORECASE))

                # Circuit breaker trips if: watch keyword + (trigger pattern OR multi-target)
                _cb_should_trip = _cb_is_watch and (_cb_is_trigger or _cb_is_multi_target)

                if _cb_should_trip:
                    logger.error(
                        f"🛑 [CIRCUIT BREAKER] Local Surveillance Failed. "
                        f"Blocking Cloud Fallback for: '{command_text}' | "
                        f"watch={_cb_is_watch}, trigger={_cb_is_trigger}, multi_target={_cb_is_multi_target}"
                    )
                    return {
                        "success": False,
                        "response": (
                            f"My local surveillance system is offline, {self.user_name}. "
                            f"I cannot execute this monitoring request via the cloud API. "
                            f"Please check the logs or restart the system."
                        ),
                        "command_type": command_type.value,
                        "error": "circuit_breaker_trip",
                        "circuit_breaker_active": True,
                        "blocked_cloud_fallback": True,
                        "surveillance_intent": {
                            "watch_keyword_detected": _cb_is_watch,
                            "trigger_pattern_detected": _cb_is_trigger,
                            "multi_target_detected": _cb_is_multi_target,
                        }
                    }

                # =====================================================================
                # LEGACY MONITORING CHECK (kept for backward compatibility)
                # =====================================================================
                # For simple "start monitor" / "stop monitor" commands
                if any(word in command_text.lower() for word in ["start", "stop", "monitor"]):
                    result = await handler.handle_command(command_text)
                else:
                    # Check if this is a temporal query first (change detection, error tracking, timeline)
                    if self._is_temporal_query(command_text):
                        logger.info(f"[UNIFIED] Detected temporal query: '{command_text}'")
                        return await self._handle_temporal_query(command_text)

                    # Check if this is a multi-space query
                    if self._is_multi_space_query(command_text):
                        logger.info(f"[UNIFIED] Detected multi-space query: '{command_text}'")
                        return await self._handle_multi_space_query(command_text)

                    # It's a single-space vision query - use two-stage resolution
                    resolved_query = await self._resolve_vision_query(command_text)

                    # Check if clarification is needed
                    if resolved_query.get("clarification_needed"):
                        return {
                            "success": False,
                            "response": resolved_query.get("clarification_message"),
                            "command_type": command_type.value,
                            "clarification_needed": True,
                        }

                    # Analyze the screen with the enhanced query
                    try:
                        # Use Intelligent Vision Router if available
                        if self.vision_router and self._vision_router_initialized:
                            logger.info(
                                "[UNIFIED] 🧠 Using Intelligent Vision Router for optimal model selection"
                            )

                            # Capture screenshot if needed
                            screenshot = None
                            try:
                                import pyautogui
                                loop = asyncio.get_running_loop()
                                screenshot = await loop.run_in_executor(None, pyautogui.screenshot)
                            except Exception as e:
                                logger.warning(f"[UNIFIED] Failed to capture screenshot: {e}")

                            # Execute via router (automatically selects YOLO/LLaMA/Claude/Yabai)
                            result = await self.vision_router.execute_query(
                                query=resolved_query.get("query", command_text),
                                screenshot=screenshot,
                                context={
                                    "conversation_history": self.context.conversation_history[-5:],
                                    "original_query": command_text,
                                    "resolved_query": resolved_query,
                                },
                            )

                            # Add routing metadata to result
                            if result.get("routing_metadata"):
                                logger.info(
                                    f"[UNIFIED] Router used {result['routing_metadata']['model_used']} "
                                    f"(latency: {result['routing_metadata']['actual_latency_ms']:.0f}ms, "
                                    f"cost: ${result['routing_metadata']['actual_cost_usd']:.4f})"
                                )

                            # Ensure result has proper format
                            if not isinstance(result, dict):
                                logger.error(f"[VISION] Router returned non-dict: {type(result)}")
                                result = {"handled": True, "success": True, "response": str(result)}

                            # Map "success" to "handled" for compatibility
                            if "handled" not in result and "success" in result:
                                result["handled"] = result["success"]

                        else:
                            # Fallback to legacy vision handler
                            logger.info(
                                "[UNIFIED] Using legacy vision handler (router not available)"
                            )
                            result = await handler.analyze_screen(
                                resolved_query.get("query", command_text)
                            )

                            # Ensure result has proper format
                            if not isinstance(result, dict):
                                logger.error(
                                    f"[VISION] analyze_screen returned non-dict: {type(result)}"
                                )
                                result = {"handled": True, "response": str(result)}

                            # Ensure handled key exists
                            if "handled" not in result:
                                logger.warning(
                                    "[VISION] analyze_screen missing 'handled' key, adding it"
                                )
                                result["handled"] = True

                    except Exception as e:
                        logger.error(f"[VISION] Vision analysis failed: {e}", exc_info=True)
                        result = {
                            "handled": True,
                            "success": False,
                            "response": f"I encountered an error analyzing your screen: {str(e)}",
                            "error": True,
                        }

                    # Add comprehensive resolution context to result
                    if resolved_query.get("resolved"):
                        result["query_resolution"] = {
                            "original_query": command_text,
                            "intent": resolved_query.get("intent"),
                            "entity_resolution": resolved_query.get("entity_resolution"),
                            "space_resolution": resolved_query.get("space_resolution"),
                            "resolved_spaces": resolved_query.get("spaces"),
                            "confidence": resolved_query.get("confidence"),
                            "two_stage": True,  # Indicates both resolvers were used
                        }

                        # Log the comprehensive resolution
                        logger.info(
                            f"[UNIFIED] Vision query resolved - "
                            f"Intent: {resolved_query.get('intent')}, "
                            f"Spaces: {resolved_query.get('spaces')}, "
                            f"Confidence: {resolved_query.get('confidence')}"
                        )

                vision_response = {
                    "success": result.get("handled", False),
                    "response": result.get("response", ""),
                    "command_type": command_type.value,
                    **result,
                }
                logger.info(
                    f"[VISION] Returning response - success={vision_response['success']}, response_len={len(vision_response.get('response', ''))}"
                )
                return vision_response
            elif command_type == CommandType.WEATHER:
                result = await handler.get_weather(command_text)
                return {
                    "success": result.get("success", False),
                    "response": result.get("formatted_response", result.get("message", "")),
                    "command_type": command_type.value,
                    **result,
                }
            elif command_type == CommandType.SYSTEM:
                # Handle system commands (app control, system settings, etc.)
                result = await self._execute_system_command(command_text)
                return {
                    "success": result.get("success", False),
                    "response": result.get("response", ""),
                    "command_type": command_type.value,
                    **result,
                }
            elif command_type == CommandType.META:
                # Handle meta commands (wake words, cancellations, performance reports)
                command_lower = command_text.lower().strip()

                if command_lower in [
                    "activate",
                    "wake",
                    "wake up",
                    "hello",
                    "hey",
                ]:
                    # Silent acknowledgment for wake words
                    return {
                        "success": True,
                        "response": "",
                        "command_type": "meta",
                        "silent": True,
                    }
                elif (
                    "performance" in command_lower
                    or "vision performance" in command_lower
                    or "show stats" in command_lower
                ):
                    # Get performance report from vision router
                    if self.vision_router and self._vision_router_initialized:
                        report = self.vision_router.get_performance_report()

                        # Format response
                        response_lines = [
                            "Vision Performance Report:",
                            f"Total queries processed: {report['total_queries']}",
                            f"Total cost: {report['total_cost_usd']}",
                            "\nModel Performance:",
                        ]

                        for model_name, stats in report.get("models", {}).items():
                            response_lines.append(
                                f"  • {model_name}: {stats['total_queries']} queries, "
                                f"{stats['success_rate']} success, "
                                f"avg {stats['avg_latency_ms']}, "
                                f"cost {stats['total_cost_usd']}"
                            )

                        return {
                            "success": True,
                            "response": "\n".join(response_lines),
                            "command_type": "meta",
                            "performance_report": report,
                        }
                    else:
                        return {
                            "success": False,
                            "response": "Vision router not initialized - no performance data available",
                            "command_type": "meta",
                        }
                else:
                    return {
                        "success": True,
                        "response": "Understood",
                        "command_type": "meta",
                    }
            elif command_type == CommandType.DISPLAY:
                # Handle display/screen mirroring commands with Goal Inference optimization

                # Check if Goal Inference has pre-loaded resources
                prediction_boost = False
                if self.goal_autonomous_integration:
                    try:
                        # Check if we predicted this command
                        integration_context = {
                            "command": command_text,
                            "active_applications": system_context.get("active_apps", []),
                        }

                        # If we have high confidence from Goal Inference, use optimized path
                        display_decision = (
                            await self.goal_autonomous_integration.predict_display_connection(
                                integration_context
                            )
                        )
                        if display_decision and display_decision.integrated_confidence > 0.85:
                            logger.info(
                                f"[GOAL-INFERENCE] Using optimized display connection path (confidence: {display_decision.integrated_confidence:.0%})"
                            )
                            prediction_boost = True
                    except Exception as e:
                        logger.debug(f"[GOAL-INFERENCE] No prediction boost: {e}")

                # Execute display command (possibly with optimized resources)
                start_time = datetime.now()
                result = await self._execute_display_command(command_text)
                execution_time = (datetime.now() - start_time).total_seconds()

                # Add Goal Inference metadata to response
                if prediction_boost:
                    result["goal_inference_active"] = True
                    result["execution_time"] = f"{execution_time:.2f}s (optimized)"
                    if execution_time < 0.5:
                        result["response"] = (
                            result.get("response", "")
                            + " I anticipated your request and pre-loaded resources for faster connection."
                        )
                else:
                    result["execution_time"] = f"{execution_time:.2f}s"

                return {
                    "success": result.get("success", False),
                    "response": result.get("response", ""),
                    "command_type": command_type.value,
                    **result,
                }
            elif command_type == CommandType.DOCUMENT:
                # Handle document creation commands WITH CONTEXT AWARENESS
                logger.info(
                    f"[DOCUMENT] Routing to context-aware document handler: '{command_text}'"
                )
                try:
                    from context_intelligence.executors import (
                        get_document_writer,
                        parse_document_request,
                    )
                    from context_intelligence.handlers.context_aware_handler import (
                        get_context_aware_handler,
                    )

                    # Get the context-aware handler
                    context_handler = get_context_aware_handler()

                    # Define the document creation callback
                    async def create_document_callback(
                        command: str, context: Dict[str, Any] = None
                    ):
                        logger.info(f"[DOCUMENT] Creating document within context-aware flow")

                        # Parse the document request
                        doc_request = parse_document_request(command, {})

                        # Get document writer
                        writer = get_document_writer()

                        # Start document creation as a background task (non-blocking)
                        # This allows us to return immediately with feedback
                        logger.info(f"[DOCUMENT] Starting background document creation task")
                        asyncio.create_task(
                            writer.create_document(request=doc_request, websocket=websocket)
                        )

                        # Return immediate feedback to user
                        return {
                            "success": True,
                            "task_started": True,
                            "topic": doc_request.topic,
                            "message": f"I'm creating an essay about {doc_request.topic} for you, Sir.",
                        }

                    # Use context-aware handler to check screen lock FIRST with voice authentication
                    logger.info(
                        f"[DOCUMENT] Checking context (including screen lock) before document creation..."
                    )
                    result = await context_handler.handle_command_with_context(
                        command_text,
                        execute_callback=create_document_callback,
                        audio_data=self.current_audio_data,
                        speaker_name=self.current_speaker_name,
                    )

                    # The context handler will handle all messaging including screen lock notifications
                    if result.get("success"):
                        return {
                            "success": True,
                            "response": result.get(
                                "summary",
                                result.get("messages", ["Document created"])[0],
                            ),
                            "command_type": command_type.value,
                            "speak": False,  # Context handler already spoke if needed
                            **result,
                        }
                    else:
                        return {
                            "success": False,
                            "response": result.get(
                                "summary",
                                result.get("messages", ["Failed to create document"])[0],
                            ),
                            "command_type": command_type.value,
                            **result,
                        }

                except Exception as e:
                    logger.error(
                        f"[DOCUMENT] Error in context-aware document creation: {e}",
                        exc_info=True,
                    )
                    return {
                        "success": False,
                        "response": f"I encountered an error creating the document: {str(e)}",
                        "command_type": command_type.value,
                        "error": str(e),
                    }
            elif command_type == CommandType.VOICE_UNLOCK:
                # Handle voice unlock commands with quick response
                command_lower = command_text.lower()

                # Check for initial enrollment request
                if (
                    "enroll" in command_lower
                    and "voice" in command_lower
                    and "start" not in command_lower
                ):
                    # Quick response for enrollment instructions
                    return {
                        "success": True,
                        "response": 'To enroll your voice, Sir, I need you to speak clearly for about 10 seconds. Say "Start voice enrollment now" when you are ready in a quiet environment.',
                        "command_type": command_type.value,
                        "type": "voice_unlock",
                        "action": "enrollment_instructions",
                        "next_command": "start voice enrollment now",
                    }
                # For actual enrollment start, let the handler process it
                elif any(
                    phrase in command_lower
                    for phrase in [
                        "start voice enrollment now",
                        "begin voice enrollment",
                        "start enrollment now",
                    ]
                ):
                    # Let the actual handler process enrollment
                    result = await handler.handle_command(command_text, websocket)
                    return {
                        "success": result.get("success", result.get("type") == "voice_unlock"),
                        "response": result.get("message", result.get("response", "")),
                        "command_type": command_type.value,
                        **result,
                    }
                else:
                    # Other voice unlock commands - use the handler with audio data
                    # Create jarvis_instance with audio data for voice verification
                    jarvis_instance = type(
                        "obj",
                        (object,),
                        {
                            "last_audio_data": self.current_audio_data,
                            "last_speaker_name": self.current_speaker_name,
                        },
                    )()

                    # Debug: Log audio passthrough
                    if self.current_audio_data:
                        logger.info(
                            f"[UNIFIED] Passing audio to voice unlock handler: {len(self.current_audio_data)} bytes"
                        )
                    else:
                        logger.warning("[UNIFIED] No audio data to pass to voice unlock handler!")

                    result = await handler.handle_command(command_text, websocket, jarvis_instance)
                    return {
                        "success": result.get("success", result.get("type") == "voice_unlock"),
                        "response": result.get("message", result.get("response", "")),
                        "command_type": command_type.value,
                        **result,
                    }
            elif command_type == CommandType.SCREEN_LOCK:
                # =========================================================================
                # FAST LOCK PATH v2.0 - No VBI, No ECAPA, No Blocking
                # =========================================================================
                # Lock commands use FAST speaker identification:
                # 1. Use cached speaker name from recent transcription (fastest)
                # 2. Fall back to owner name from database (cached)
                # 3. NEVER trigger VBI/ECAPA (causes event loop blocking)
                # =========================================================================
                logger.info(f"[SCREEN_LOCK] 🔒 FAST PATH: Processing lock with lightweight speaker ID")
                
                try:
                    # =========================================================
                    # FAST SPEAKER IDENTIFICATION (No VBI)
                    # =========================================================
                    speaker_name = None
                    
                    # Priority 1: Use speaker name from recent transcription (already identified)
                    if self.current_speaker_name:
                        speaker_name = self.current_speaker_name
                        logger.info(f"[SCREEN_LOCK] Speaker from transcription: {speaker_name}")
                    elif speaker_name:
                        speaker_name = speaker_name
                        logger.info(f"[SCREEN_LOCK] Speaker from parameter: {speaker_name}")
                    
                    # Priority 2: Get owner name from database (cached, fast)
                    if not speaker_name:
                        try:
                            from api.simple_unlock_handler import _get_owner_name
                            speaker_name = await asyncio.wait_for(_get_owner_name(), timeout=1.0)
                            logger.info(f"[SCREEN_LOCK] Speaker from database: {speaker_name}")
                        except asyncio.TimeoutError:
                            speaker_name = "there"
                            logger.warning("[SCREEN_LOCK] Owner name lookup timed out, using fallback")
                        except Exception as e:
                            speaker_name = "there"
                            logger.warning(f"[SCREEN_LOCK] Owner name lookup failed: {e}")
                    
                    # =========================================================
                    # EXECUTE LOCK - Direct, Fast, Non-Blocking
                    # =========================================================
                    from core.transport_handlers import applescript_handler
                    
                    # Execute lock directly via transport handler (bypasses all VBI)
                    lock_result = await applescript_handler("lock_screen", {})
                    
                    if lock_result.get("success"):
                        response = f"Of course, {speaker_name}. Locking your screen now. See you soon!"
                        logger.info(f"[SCREEN_LOCK] ✅ Screen locked for {speaker_name}")
                        
                        return {
                            "success": True,
                            "response": response,
                            "command_type": "screen_lock",
                            "type": "screen_lock",
                            "speaker_identified": speaker_name,
                            "action": "lock_screen",
                            "method": lock_result.get("method", "direct"),
                        }
                    else:
                        response = f"I couldn't lock the screen, {speaker_name}. Try pressing Control+Command+Q."
                        logger.warning(f"[SCREEN_LOCK] ❌ Lock failed: {lock_result.get('error')}")
                        
                        return {
                            "success": False,
                            "response": response,
                            "command_type": "screen_lock",
                            "type": "screen_lock",
                            "error": lock_result.get("error", "lock_failed"),
                        }
                        
                except Exception as e:
                    logger.error(f"[SCREEN_LOCK] Error: {e}", exc_info=True)
                    return {
                        "success": False,
                        "response": f"I encountered an error trying to lock your screen: {str(e)}",
                        "command_type": "screen_lock",
                        "error": str(e),
                    }
            elif command_type == CommandType.QUERY:
                # ==========================================================================
                # QUERY COMMAND HANDLER - Routes to J-Prime/Cloud via PrimeRouter
                # v84.0: Trinity Integration with intelligent LLM routing
                # ==========================================================================
                try:
                    from api.query_handler import handle_query

                    logger.info(f"[UNIFIED] 🧠 Processing query via PrimeRouter: '{command_text[:50]}...'")

                    # Build context for query.
                    # Skip screen context on fast query path — simple queries don't need it,
                    # and _get_screen_context may depend on uninitialized resolvers.
                    _fast_query = getattr(self, '_fast_query_eligible', False)
                    query_context = {
                        "screen_context": (
                            await self._get_screen_context()
                            if not _fast_query and hasattr(self, '_get_screen_context')
                            else {}
                        ),
                        "history": self.context_history if hasattr(self, 'context_history') else [],
                    }

                    # v236.0: Pass classified_query for adaptive prompt generation.
                    # classified_query is set on self in process_command() (line ~1731)
                    # and read here in _execute_command_internal() (different method scope).
                    _classified_query = getattr(self, '_classified_query', None)
                    result = await handle_query(
                        command_text,
                        query_context,
                        classified_query=_classified_query,
                        deadline=deadline,  # v241.0
                    )

                    logger.info(f"[UNIFIED] ✅ Query response from {result.get('source', 'unknown')}")

                    return {
                        "success": result.get("success", True),
                        "response": result.get("response", ""),
                        "command_type": command_type.value,
                        "source": result.get("source", "unknown"),
                        "model": result.get("model", "unknown"),
                        "latency_ms": result.get("latency_ms", 0),
                        **{k: v for k, v in result.items() if k not in ["success", "response"]},
                    }
                except ImportError as e:
                    logger.error(f"[UNIFIED] Query handler not available: {e}")
                    return {
                        "success": False,
                        "response": "Query processing is currently unavailable.",
                        "command_type": command_type.value,
                        "error": str(e),
                    }
                except Exception as e:
                    logger.error(f"[UNIFIED] Query processing error: {e}", exc_info=True)
                    return {
                        "success": False,
                        "response": f"Error processing query: {str(e)}",
                        "command_type": command_type.value,
                        "error": str(e),
                    }
            else:
                # Generic handler interface - fallback for unhandled command types
                logger.warning(f"[UNIFIED] ⚠️ No specific handler for {command_type.value}, using generic response")
                return {
                    "success": True,
                    "response": f"Executing {command_type.value} command",
                    "command_type": command_type.value,
                }

        except Exception as e:
            logger.error(f"Error executing {command_type.value} command: {e}", exc_info=True)
            return {
                "success": False,
                "response": f"I encountered an error with that {command_type.value} command.",
                "command_type": command_type.value,
                "error": str(e),
            }

    async def _get_handler(self, command_type: CommandType):
        """Dynamically import and get handler for command type"""
        # System commands are handled directly in _execute_command
        if command_type == CommandType.SYSTEM:
            return True  # Return True to indicate system handler is available

        module_name = self.handler_modules.get(command_type)
        if not module_name:
            return None

        try:
            if command_type == CommandType.VISION:
                from api.vision_command_handler import vision_command_handler

                return vision_command_handler
            elif command_type == CommandType.WEATHER:
                from system_control.weather_system_config import get_weather_system

                return get_weather_system()
            elif command_type == CommandType.AUTONOMY:
                from api.autonomy_handler import get_autonomy_handler

                return get_autonomy_handler()
            elif command_type == CommandType.VOICE_UNLOCK:
                from api.voice_unlock_handler import get_voice_unlock_handler

                return get_voice_unlock_handler()
            elif command_type == CommandType.QUERY:
                from api.query_handler import handle_query
                # NOTE: This is only used for handler availability checking (line 3512).
                # Actual QUERY execution is at line ~4462 which passes classified_query.
                return handle_query
            # Add other handlers as needed

        except ImportError as e:
            logger.error(f"Failed to import handler for {command_type.value}: {e}")
            return None

    async def _handle_compound_command(
        self, command_text: str, context: Dict[str, Any] = None
    ) -> Dict[str, Any]:
        """Handle commands with multiple parts and maintain context between them"""
        logger.info(f"[COMPOUND] Handling compound command with context: {context is not None}")

        # IMPORTANT: Check screen lock state FIRST using CAI
        # This ensures compound commands like "open safari and search dogs" unlock the screen first
        try:
            from context_intelligence.detectors.screen_lock_detector import get_screen_lock_detector

            screen_detector = get_screen_lock_detector()
            is_locked = await screen_detector.is_screen_locked()

            if is_locked:
                logger.warning(
                    f"[COMPOUND] Screen is LOCKED - checking if unlock needed for: {command_text}"
                )

                # Check if compound command requires screen access
                screen_context = await screen_detector.check_screen_context(
                    command_text, speaker_name=getattr(self, "current_speaker_name", None)
                )

                if screen_context["requires_unlock"]:
                    logger.warning(
                        f"[COMPOUND] Unlock required! Message: {screen_context['unlock_message']}"
                    )

                    # Get audio data if available (stored from process_command)
                    audio_data = getattr(self, "current_audio_data", None)
                    speaker_name = getattr(self, "current_speaker_name", None)

                    # Perform unlock with voice authentication
                    unlock_success, unlock_msg = await screen_detector.handle_screen_lock_context(
                        command_text, audio_data=audio_data, speaker_name=speaker_name
                    )

                    if not unlock_success:
                        logger.error(f"[COMPOUND] Screen unlock failed: {unlock_msg}")
                        return {
                            "success": False,
                            "response": unlock_msg
                            or "Failed to unlock screen. Cannot execute command.",
                            "command_type": "compound",
                        }
                    else:
                        logger.info(
                            f"[COMPOUND] ✅ Screen unlocked successfully - proceeding with compound command"
                        )
        except Exception as e:
            logger.error(f"[COMPOUND] Error checking screen lock: {e}")
            # Continue anyway - don't block the command

        # Parse compound commands more intelligently
        parts = self._parse_compound_parts(command_text)

        results = []
        all_success = True
        responses = []

        # Track context for dependent commands
        active_app = None
        previous_result = None

        # Use provided context if available
        if context:
            logger.info(f"[COMPOUND] Using provided system context")

        # Check if all parts are similar operations that can be parallelized
        can_parallelize = self._can_parallelize_commands(parts)

        if can_parallelize:
            # Process similar operations in parallel (e.g., closing multiple apps)
            logger.info(f"[COMPOUND] Processing {len(parts)} similar commands in parallel")

            # Create tasks for parallel execution
            tasks = []
            for part in parts:
                part = part.strip()
                if not part:
                    continue

                # Process each part as an independent command via J-Prime
                async def process_part(p):
                    sub_response = await self._call_jprime(p)
                    if sub_response:
                        return await self._execute_action(sub_response, p)
                    return {"success": False, "response": f"Failed to process: {p}"}

                tasks.append(process_part(part))

            # Execute all tasks in parallel
            results = await asyncio.gather(*tasks)

            # Collect responses
            for result in results:
                if result.get("success", False):
                    responses.append(result.get("response", ""))
                else:
                    all_success = False
                    responses.append(f"Failed: {result.get('response', 'Unknown error')}")
        else:
            # Sequential processing for dependent commands
            for i, part in enumerate(parts):
                part = part.strip()
                if not part:
                    continue

                # Provide user feedback for multi-step commands
                if len(parts) > 1:
                    logger.info(f"[COMPOUND] Step {i+1}/{len(parts)}: {part}")

                # Check if this is a dependent command that needs context
                enhanced_command = self._enhance_with_context(part, active_app, previous_result)
                logger.info(
                    f"[COMPOUND] Enhanced command: '{part}' -> '{enhanced_command}' (active_app: {active_app})"
                )

                # Process individual part via J-Prime (v242: no local classification)
                sub_response = await self._call_jprime(enhanced_command)
                if sub_response:
                    result = await self._execute_action(sub_response, enhanced_command)
                else:
                    result = {"success": False, "response": f"Failed to process: {enhanced_command}"}
                results.append(result)

                # Update context for next command
                if result.get("success", False):
                    # Track opened apps for subsequent commands
                    if any(word in part.lower() for word in ["open", "launch", "start"]):
                        # Find which app was opened dynamically
                        words = enhanced_command.lower().split()
                        for word in words:
                            if self.pattern_learner.is_learned_app(word):
                                active_app = word
                                # Even if app is already open, we want to track it for context
                                logger.info(f"[COMPOUND] Tracking active app: {active_app}")
                                break

                    # Skip "already open" messages in compound commands
                    response = result.get("response", "")
                    if "already open" not in response.lower() or len(parts) == 1:
                        responses.append(response)
                else:
                    all_success = False
                    responses.append(f"Failed: {result.get('response', 'Unknown error')}")
                    # Don't continue if a step fails
                    break

                previous_result = result

                # Add small delay between commands for reliability
                if i < len(parts) - 1:
                    await asyncio.sleep(0.5)

        # Create conversational response
        if len(responses) > 1:
            # Clean up individual responses first
            cleaned_responses = []
            for i, resp in enumerate(responses):
                # Remove trailing "Sir" from all but the last response
                if resp.endswith(", Sir") and i < len(responses) - 1:
                    resp = resp[:-5]
                cleaned_responses.append(resp)

            # Combine into natural response
            if len(cleaned_responses) == 2:
                # For 2 steps: "Opening Safari and searching for dogs"
                response = f"{cleaned_responses[0]} and {cleaned_responses[1]}"
            else:
                # For 3+ steps: "Opening Safari, navigating to Google, and taking a screenshot"
                response = ", ".join(cleaned_responses[:-1]) + f" and {cleaned_responses[-1]}"

            # Add "Sir" at the end if it's not already there
            if not response.endswith(", Sir"):
                response += ", Sir"
        else:
            response = responses[0] if responses else "I'll help you with that"

        return {
            "success": all_success,
            "response": response,
            "command_type": CommandType.COMPOUND.value,
            "sub_results": results,
            "steps_completed": len([r for r in results if r.get("success", False)]),
            "total_steps": len(parts),
            "context": context or {},
            "steps_taken": [f"Step {i+1}: {part}" for i, part in enumerate(parts)],
        }

    def _parse_compound_parts(self, command_text: str) -> List[str]:
        """Dynamically parse compound command into logical parts"""
        # Check for multi-operation patterns
        multi_op_patterns = [
            "separate tabs",
            "different tabs",
            "multiple tabs",
            "each tab",
            "respectively",
        ]
        if any(pattern in command_text.lower() for pattern in multi_op_patterns):
            # This is a single complex command with multiple targets
            return [command_text]

        # First check for implicit compound (no connector)
        words = command_text.lower().split()
        if len(words) >= 3:
            # Look for app names to find split points
            for i, word in enumerate(words):
                if self.pattern_learner.is_learned_app(word) and i > 0 and i < len(words) - 1:
                    # Check if there's a verb before and action after
                    if words[i - 1] in self.pattern_learner.app_verbs:
                        # Check if there's an action after the app
                        remaining_words = words[i + 1 :]
                        if any(
                            w
                            in self.pattern_learner.app_verbs | {"search", "navigate", "go", "type"}
                            for w in remaining_words
                        ):
                            # Split at the app name
                            part1 = " ".join(words[: i + 1])
                            part2 = " ".join(words[i + 1 :])
                            logger.info(f"[PARSE] Split implicit compound: '{part1}' | '{part2}'")
                            return [part1, part2]

        # Dynamic connector detection
        connectors = [
            " and ",
            " then ",
            ", and ",
            ", then ",
            " && ",
            " ; ",
            " plus ",
            " also ",
        ]

        # Smart parsing - analyze command structure
        parts = []
        remaining = command_text

        # Find all connector positions
        connector_positions = []
        for connector in connectors:
            pos = 0
            while connector in remaining[pos:]:
                index = remaining.find(connector, pos)
                if index != -1:
                    connector_positions.append((index, connector))
                    pos = index + 1
                else:
                    break

        # Sort by position
        connector_positions.sort(key=lambda x: x[0])

        if not connector_positions:
            return [command_text]

        # Analyze each potential split point
        last_pos = 0
        for pos, connector in connector_positions:
            before = remaining[last_pos:pos].strip()
            after = remaining[pos + len(connector) :].strip()

            # Use intelligent splitting logic
            should_split = self._should_split_at_connector(before, after, connector)

            if should_split:
                if before:
                    parts.append(before)
                last_pos = pos + len(connector)

        # Add remaining part
        final_part = remaining[last_pos:].strip()
        if final_part:
            parts.append(final_part)

        # If no valid splits, return original
        return parts if parts else [command_text]

    def _should_split_at_connector(self, before: str, after: str, connector: str) -> bool:
        """Determine if we should split at this connector"""
        if not before or not after:
            return False

        # Get word analysis
        before_words = before.lower().split()
        after_words = after.lower().split()

        # Check if both sides have verbs (indicating separate commands)
        before_has_verb = any(
            word in self.pattern_learner.app_verbs | self.pattern_learner.system_verbs
            for word in before_words
        )
        after_has_verb = any(
            word in self.pattern_learner.app_verbs | self.pattern_learner.system_verbs
            for word in after_words[:3]
        )  # Check first 3 words of after

        if before_has_verb and after_has_verb:
            return True

        # Check if both sides mention apps
        before_has_app = any(self.pattern_learner.is_learned_app(word) for word in before_words)
        after_has_app = any(self.pattern_learner.is_learned_app(word) for word in after_words)

        if before_has_app and after_has_app and before_has_verb:
            return True

        # Don't split if it's part of a single concept
        single_concepts = {
            connector + "press enter",
            connector + "enter",
            connector + "return",
            "type",
            "write",
            "and then",
        }

        # Check if the connector is truly part of a single phrase
        full_text = (before + connector + after).lower()
        for concept in single_concepts:
            if concept in full_text:
                return False

        # Special handling for search/navigation commands
        # "search for X and Y" should not split, but "open X and search for Y" should
        if connector == " and " and "search for" in after.lower():
            # If before has an app operation, this should split
            if any(verb in before.lower() for verb in ["open", "launch", "start", "close"]):
                return True

        # Don't split URLs or domains
        if self._contains_url_pattern(before + connector + after):
            url_start = before.rfind("http")
            if url_start == -1:
                url_start = before.rfind("www.")
            if url_start != -1:
                return False

        return True

    def _can_parallelize_commands(self, parts: List[str]) -> bool:
        """Dynamically determine if commands can be run in parallel"""
        if len(parts) < 2:
            return False

        # Analyze command dependencies
        command_analyses = []

        for i, part in enumerate(parts):
            words = part.lower().split()

            analysis = {
                "has_verb": any(
                    word in self.pattern_learner.app_verbs | self.pattern_learner.system_verbs
                    for word in words
                ),
                "has_app": any(self.pattern_learner.is_learned_app(word) for word in words),
                "has_dependency": False,
                "operation_type": None,
                "affects_state": False,
            }

            # Detect operation type
            for word in words:
                if word in self.pattern_learner.app_verbs:
                    if word in {"open", "launch", "start"}:
                        analysis["operation_type"] = "open"
                    elif word in {"close", "quit", "kill"}:
                        analysis["operation_type"] = "close"
                    break

            # Check for dependencies on previous commands
            dependency_indicators = {"then", "after", "next", "followed", "using"}
            if any(indicator in words for indicator in dependency_indicators):
                analysis["has_dependency"] = True

            # Check if command affects state that next command might depend on
            state_affecting = {"search", "type", "navigate", "click", "select", "focus"}
            if any(action in words for action in state_affecting):
                analysis["affects_state"] = True

            # Check for explicit references to previous results
            if i > 0:
                reference_words = {"it", "that", "there", "result"}
                if any(ref in words for ref in reference_words):
                    analysis["has_dependency"] = True

            command_analyses.append(analysis)

        # Determine if parallelizable
        # Commands can be parallel if:
        # 1. No command has dependencies
        # 2. No command affects state that others might use
        # 3. All are similar operation types

        has_dependencies = any(a["has_dependency"] for a in command_analyses)
        if has_dependencies:
            return False

        affects_state = any(a["affects_state"] for a in command_analyses)
        if affects_state:
            return False

        # Check operation types
        operation_types = [a["operation_type"] for a in command_analyses if a["operation_type"]]
        if operation_types:
            # All same type = parallelizable
            return len(set(operation_types)) == 1

        # Default: if simple and independent, allow parallel
        all_simple = all(len(part.split()) <= 4 for part in parts)
        all_have_verbs = all(a["has_verb"] for a in command_analyses)

        return all_simple and all_have_verbs

    def _enhance_with_context(
        self, command: str, active_app: Optional[str], previous_result: Optional[Dict]
    ) -> str:
        """Enhance command with context from previous commands"""
        command_lower = command.lower()
        words = command_lower.split()

        # Dynamic pattern detection for navigation and search
        nav_indicators = {"go", "navigate", "browse", "visit", "open"}
        search_indicators = {"search", "find", "look", "google", "query"}

        # Check if this needs browser context
        has_nav = any(word in words for word in nav_indicators)
        has_search = any(word in words for word in search_indicators)

        if (has_nav or has_search) and active_app:
            # Check if active app is likely a browser (learned from system)
            browser_indicators = {"browser", "web", "internet"}
            is_browser = active_app.lower() in {"safari", "chrome", "firefox"} or any(
                indicator in active_app.lower() for indicator in browser_indicators
            )

            if is_browser:
                # Check if browser not already specified
                if not self.pattern_learner.is_learned_app(active_app):
                    # Learn this as a browser app
                    self.pattern_learner.learned_apps.add(active_app.lower())

                # Enhance command if browser not mentioned
                app_mentioned = any(self.pattern_learner.is_learned_app(word) for word in words)
                if not app_mentioned:
                    # Add browser context
                    if has_search:
                        # Clean up the command - remove "and", "search", "search for" to get just the query
                        cleaned = command
                        # Remove leading "and" if present
                        if cleaned.lower().startswith("and "):
                            cleaned = cleaned[4:]
                        # Remove search-related words
                        cleaned = cleaned.replace("search for", "").replace("search", "").strip()
                        command = f"search in {active_app} for {cleaned}"
                    elif "go to" in command_lower:
                        command = command.replace("go to", f"tell {active_app} to go to")
                    else:
                        command = f"in {active_app} {command}"

        # Use previous result context if available
        if previous_result and previous_result.get("success"):
            # Could enhance with information from previous successful command
            pass

        return command

    def _parse_system_command(self, command_text: str) -> Tuple[str, Optional[str], Dict[str, Any]]:
        """Dynamically parse system command to extract type, target, and parameters"""
        words = command_text.lower().split()
        command_type = None
        target = None
        params = {}

        # Detect command type based on verb patterns
        first_word = words[0] if words else ""

        # Tab/browser operations
        tab_indicators = {"tab", "tabs"}
        if any(indicator in words for indicator in tab_indicators):
            command_type = "tab_control"
            # Find browser if mentioned
            for word in words:
                if self.pattern_learner.is_learned_app(word):
                    # Check if it's likely a browser
                    if any(
                        browser_hint in word
                        for browser_hint in ["safari", "chrome", "firefox", "browser"]
                    ):
                        target = word
                        break
            # Extract URL if present
            url_patterns = ["go to", "navigate to", "and open", "open"]
            for pattern in url_patterns:
                if pattern in command_text.lower():
                    idx = command_text.lower().find(pattern)
                    url_part = command_text[idx + len(pattern) :].strip()
                    if url_part:
                        params["url"] = self._normalize_url(url_part)
                    break

        # App control operations
        elif first_word in self.pattern_learner.app_verbs:
            if first_word in {"open", "launch", "start"}:
                command_type = "app_open"
            elif first_word in {"close", "quit", "kill"}:
                command_type = "app_close"

            # Find target app
            for i, word in enumerate(words[1:], 1):
                if self.pattern_learner.is_learned_app(word):
                    target = word
                    break
                # Also check multi-word apps
                if i < len(words) - 1:
                    two_word = f"{word} {words[i+1]}"
                    if self.pattern_learner.is_learned_app(two_word):
                        target = two_word
                        break

        # System settings operations
        elif any(
            setting in words
            for setting in {"volume", "brightness", "wifi", "bluetooth", "screenshot"}
        ):
            command_type = "system_setting"
            # Determine which setting
            if "volume" in words:
                target = "volume"
                if "mute" in words:
                    params["action"] = "mute"
                elif "unmute" in words:
                    params["action"] = "unmute"
                else:
                    # Extract level
                    for word in words:
                        if word.isdigit():
                            params["level"] = int(word)
                            break
            elif "brightness" in words:
                target = "brightness"
                for word in words:
                    if word.isdigit():
                        params["level"] = int(word)
                        break
            elif "wifi" in words or "wi-fi" in words:
                target = "wifi"
                params["enable"] = "on" in words or "enable" in words
            elif "screenshot" in words:
                target = "screenshot"

        # Web operations
        elif any(
            web_verb in words for web_verb in {"search", "google", "browse", "navigate", "visit"}
        ):
            command_type = "web_action"
            # Determine specific action
            if "search" in words or "google" in words:
                params["action"] = "search"
                # Extract search query
                # Handle "search in X for Y" pattern first
                if "search in" in command_text.lower() and " for " in command_text.lower():
                    # Extract query after "for"
                    for_idx = command_text.lower().find(" for ")
                    if for_idx != -1:
                        query = command_text[for_idx + 5 :].strip()
                        if query:
                            params["query"] = query
                            logger.info(
                                f"[PARSE] Extracted query from 'search in X for Y' pattern: '{query}'"
                            )
                else:
                    # Standard search patterns
                    search_patterns = ["search for", "google", "look up", "find"]
                    for pattern in search_patterns:
                        if pattern in command_text.lower():
                            idx = command_text.lower().find(pattern)
                            query = command_text[idx + len(pattern) :].strip()
                            if query:
                                params["query"] = query
                                logger.info(
                                    f"[PARSE] Extracted query from '{pattern}' pattern: '{query}'"
                                )
                                break
            else:
                params["action"] = "navigate"
                # Extract URL
                nav_patterns = ["go to", "navigate to", "visit", "browse to"]
                for pattern in nav_patterns:
                    if pattern in command_text.lower():
                        idx = command_text.lower().find(pattern)
                        url = command_text[idx + len(pattern) :].strip()
                        if url:
                            params["url"] = self._normalize_url(url)
                            break

            # Find browser if specified
            # Check both original words and full command text (in case of "search in X for Y")
            if "in " in command_text.lower():
                # Extract browser from "in [browser]" pattern
                in_match = re.search(r"\bin\s+(\w+)\s+", command_text.lower())
                if in_match:
                    potential_browser = in_match.group(1)
                    if self.pattern_learner.is_learned_app(potential_browser):
                        target = potential_browser

            # If not found, check individual words
            if not target:
                for word in words:
                    if self.pattern_learner.is_learned_app(word):
                        if any(hint in word for hint in ["safari", "chrome", "firefox", "browser"]):
                            target = word
                            break

        # Multi-tab searches
        if "separate tabs" in command_text.lower() or "different tabs" in command_text.lower():
            command_type = "multi_tab_search"
            params["multi_tab"] = True

        # Typing operations
        elif "type" in words:
            command_type = "type_text"
            # Extract text to type
            idx = command_text.lower().find("type")
            text = command_text[idx + 4 :].strip()
            # Remove trailing instructions
            text = text.replace(" and press enter", "").replace(" and enter", "")
            params["text"] = text
            params["press_enter"] = "enter" in command_text.lower()

        return command_type or "unknown", target, params

    def _normalize_url(self, url: str) -> str:
        """Normalize URL input"""
        url = url.strip()

        # Handle common shortcuts
        if url.lower() in {"google", "google.com"}:
            return "https://google.com"

        # Add protocol if missing
        if not url.startswith(("http://", "https://")):
            if "." in url:
                return f"https://{url}"
            else:
                # Assume .com for single words
                return f"https://{url}.com"

        return url

    def _detect_default_browser(self) -> str:
        """Detect the default browser dynamically"""
        # Try to get default browser from system
        try:
            import subprocess

            result = subprocess.run(
                [
                    "defaults",
                    "read",
                    "com.apple.LaunchServices/com.apple.launchservices.secure",
                    "LSHandlers",
                ],
                capture_output=True,
                text=True,
            )
            if "safari" in result.stdout.lower():
                return "safari"
            elif "chrome" in result.stdout.lower():
                return "chrome"
            elif "firefox" in result.stdout.lower():
                return "firefox"
        except Exception:
            pass

        # Default fallback
        return "safari"

    async def _execute_display_action(self, display_ref, original_command: str) -> Dict[str, Any]:
        """
        Execute display action based on resolved DisplayReference

        This is the new direct routing system that uses display_ref.action
        instead of pattern matching.

        Args:
            display_ref: DisplayReference from display_reference_handler
            original_command: Original user command (for context)

        Returns:
            Dict with success status and response
        """
        from context_intelligence.handlers.display_reference_handler import ActionType

        logger.info(
            f"[DISPLAY-ACTION] Executing: action={display_ref.action.value}, "
            f"display={display_ref.display_name}, mode={display_ref.mode.value if display_ref.mode else 'auto'}"
        )

        try:
            # Get display monitor instance
            monitor = None
            if hasattr(self, "_app") and self._app:
                if hasattr(self._app.state, "display_monitor"):
                    monitor = self._app.state.display_monitor
                    logger.info(f"[DISPLAY-ACTION] Got monitor from app.state: {monitor}")

            if monitor is None:
                from display import get_display_monitor

                monitor = get_display_monitor()
                logger.info(f"[DISPLAY-ACTION] Got monitor from get_display_monitor(): {monitor}")

            if monitor is None:
                logger.error(
                    "[DISPLAY-ACTION] ❌ CRITICAL: Display monitor is None! Cannot execute connection."
                )
                return {
                    "success": False,
                    "response": "Display monitor not initialized. Please restart JARVIS.",
                }

            # Route based on action type
            if display_ref.action == ActionType.CONNECT:
                return await self._action_connect_display(monitor, display_ref, original_command)

            elif display_ref.action == ActionType.DISCONNECT:
                return await self._action_disconnect_display(monitor, display_ref, original_command)

            elif display_ref.action == ActionType.CHANGE_MODE:
                return await self._action_change_mode(monitor, display_ref, original_command)

            elif display_ref.action == ActionType.QUERY_STATUS:
                return await self._action_query_status(monitor, display_ref, original_command)

            elif display_ref.action == ActionType.LIST_DISPLAYS:
                return await self._action_list_displays(monitor, display_ref, original_command)

            else:
                logger.warning(f"[DISPLAY-ACTION] Unknown action: {display_ref.action.value}")
                return {
                    "success": False,
                    "response": f"I don't know how to perform action: {display_ref.action.value}",
                }

        except Exception as e:
            logger.error(f"[DISPLAY-ACTION] Error: {e}", exc_info=True)
            return {
                "success": False,
                "response": f"Error executing display action: {str(e)}",
            }

    async def _action_connect_display(
        self, monitor, display_ref, original_command: str
    ) -> Dict[str, Any]:
        """Execute CONNECT action"""
        from context_intelligence.handlers.display_reference_handler import ModeType

        display_name = display_ref.display_name
        # Normalize display_id to use underscores to match config format (e.g., living_room_tv)
        display_id = display_ref.display_id or display_name.lower().replace(" ", "_")
        mode = display_ref.mode

        logger.info(f"[DISPLAY-ACTION] Connecting to '{display_name}' (id={display_id})")

        # Determine mode string for monitor.connect_display
        mode_str = (
            "extended"
            if mode == ModeType.EXTENDED
            else (
                "entire"
                if mode == ModeType.ENTIRE_SCREEN
                else "window" if mode == ModeType.WINDOW else "mirror"
            )
        )  # Default

        try:
            # NEW: Try Claude Computer Use integration first (vision-based, dynamic)
            try:
                from display.jarvis_computer_use_integration import (
                    JARVISComputerUse,
                    ExecutionMode
                )
                import os

                # Check if API key is available
                if os.environ.get("ANTHROPIC_API_KEY"):
                    logger.info(f"[DISPLAY-ACTION] Trying Computer Use for '{display_name}'")
                    jarvis_cu = JARVISComputerUse(execution_mode=ExecutionMode.FULL_VOICE)
                    await jarvis_cu.initialize()

                    cu_result = await jarvis_cu.connect_to_display(display_name, narrate=True)

                    if cu_result.success:
                        logger.info(f"[DISPLAY-ACTION] Computer Use succeeded for '{display_name}'")
                        from datetime import datetime
                        hour = datetime.now().hour
                        greeting = (
                            "Good morning" if 5 <= hour < 12
                            else "Good afternoon" if 12 <= hour < 17
                            else "Good evening" if 17 <= hour < 21
                            else "Good night"
                        )
                        return {
                            "success": True,
                            "response": f"{greeting}! Connected to {display_name}, sir.",
                            "display_name": display_name,
                            "display_id": display_id,
                            "mode": mode_str,
                            "action": "connect",
                            "method": "computer_use",
                            "resolution_strategy": display_ref.resolution_strategy.value,
                            "confidence": cu_result.confidence,
                        }
                    else:
                        logger.warning(f"[DISPLAY-ACTION] Computer Use failed, trying fallback: {cu_result.message}")
                else:
                    logger.info("[DISPLAY-ACTION] No ANTHROPIC_API_KEY, skipping Computer Use")
            except Exception as cu_error:
                logger.warning(f"[DISPLAY-ACTION] Computer Use error, falling back: {cu_error}")

            # FALLBACK: Connect using display monitor (original method)
            result = await monitor.connect_display(display_id)

            if result.get("success"):
                # Generate time-aware response
                from datetime import datetime

                hour = datetime.now().hour
                greeting = (
                    "Good morning"
                    if 5 <= hour < 12
                    else (
                        "Good afternoon"
                        if 12 <= hour < 17
                        else "Good evening" if 17 <= hour < 21 else "Good night"
                    )
                )

                response = f"{greeting}! Connected to {display_name}, sir."

                # Add mode info if specified
                if mode:
                    response += f" Display mode: {mode.value}."

                return {
                    "success": True,
                    "response": response,
                    "display_name": display_name,
                    "display_id": display_id,
                    "mode": mode_str,
                    "action": "connect",
                    "resolution_strategy": display_ref.resolution_strategy.value,
                    "confidence": display_ref.confidence,
                }
            else:
                return {
                    "success": False,
                    "response": result.get("message", f"Unable to connect to {display_name}."),
                    "display_name": display_name,
                }

        except Exception as e:
            logger.error(f"[DISPLAY-ACTION] Connect error: {e}", exc_info=True)
            return {
                "success": False,
                "response": f"Error connecting to {display_name}: {str(e)}",
            }

    async def _action_disconnect_display(
        self, monitor, display_ref, original_command: str
    ) -> Dict[str, Any]:
        """Execute DISCONNECT action"""
        display_name = display_ref.display_name
        # Normalize display_id to use underscores to match config format (e.g., living_room_tv)
        display_id = display_ref.display_id or display_name.lower().replace(" ", "_")

        logger.info(f"[DISPLAY-ACTION] Disconnecting from '{display_name}' (id={display_id})")

        try:
            result = await monitor.disconnect_display(display_id)

            if result.get("success"):
                return {
                    "success": True,
                    "response": f"Disconnected from {display_name}, sir.",
                    "display_name": display_name,
                    "display_id": display_id,
                    "action": "disconnect",
                }
            else:
                return {
                    "success": False,
                    "response": result.get("message", f"Unable to disconnect from {display_name}."),
                    "display_name": display_name,
                }

        except Exception as e:
            logger.error(f"[DISPLAY-ACTION] Disconnect error: {e}", exc_info=True)
            return {
                "success": False,
                "response": f"Error disconnecting from {display_name}: {str(e)}",
            }

    async def _action_change_mode(
        self, monitor, display_ref, original_command: str
    ) -> Dict[str, Any]:
        """Execute CHANGE_MODE action"""
        from context_intelligence.handlers.display_reference_handler import ModeType

        display_name = display_ref.display_name
        display_id = display_ref.display_id or display_name.lower().replace(" ", "-")
        mode = display_ref.mode

        if not mode:
            return {
                "success": False,
                "response": "Please specify which mode you'd like: entire screen, window, or extended display.",
            }

        logger.info(f"[DISPLAY-ACTION] Changing '{display_name}' to {mode.value} mode")

        # Map ModeType to mode string
        mode_str = (
            "entire"
            if mode == ModeType.ENTIRE_SCREEN
            else (
                "window"
                if mode == ModeType.WINDOW
                else "extended" if mode == ModeType.EXTENDED else "mirror"
            )
        )

        try:
            result = await monitor.change_display_mode(display_id, mode_str)

            if result.get("success"):
                return {
                    "success": True,
                    "response": f"Changed {display_name} to {mode.value} mode, sir.",
                    "display_name": display_name,
                    "mode": mode_str,
                    "action": "change_mode",
                }
            else:
                return {
                    "success": False,
                    "response": result.get(
                        "message", f"Unable to change {display_name} to {mode.value} mode."
                    ),
                }

        except Exception as e:
            logger.error(f"[DISPLAY-ACTION] Change mode error: {e}", exc_info=True)
            return {
                "success": False,
                "response": f"Error changing mode: {str(e)}",
            }

    async def _action_query_status(
        self, monitor, display_ref, original_command: str
    ) -> Dict[str, Any]:
        """Execute QUERY_STATUS action"""
        logger.info(f"[DISPLAY-ACTION] Querying display status")

        try:
            status = monitor.get_status()
            connected = status.get("connected_displays", [])
            available = monitor.get_available_display_details()

            if connected:
                display_names = [d.get("display_name", d) for d in connected]
                response = (
                    f"You have {len(connected)} display(s) connected: {', '.join(display_names)}."
                )
            else:
                response = "No displays are currently connected."

            if available:
                avail_names = [d["display_name"] for d in available]
                response += f" Available displays: {', '.join(avail_names)}."

            return {
                "success": True,
                "response": response,
                "connected_displays": connected,
                "available_displays": available,
                "action": "query_status",
            }

        except Exception as e:
            logger.error(f"[DISPLAY-ACTION] Query status error: {e}", exc_info=True)
            return {
                "success": False,
                "response": f"Error querying display status: {str(e)}",
            }

    async def _action_list_displays(
        self, monitor, display_ref, original_command: str
    ) -> Dict[str, Any]:
        """Execute LIST_DISPLAYS action"""
        logger.info(f"[DISPLAY-ACTION] Listing available displays")

        try:
            available = monitor.get_available_display_details()

            if available:
                names = [d["display_name"] for d in available]
                response = f"Available displays: {', '.join(names)}."
            else:
                response = "No displays are currently available."

            return {
                "success": True,
                "response": response,
                "available_displays": available,
                "action": "list_displays",
            }

        except Exception as e:
            logger.error(f"[DISPLAY-ACTION] List displays error: {e}", exc_info=True)
            return {
                "success": False,
                "response": f"Error listing displays: {str(e)}",
            }

    async def _execute_display_command(self, command_text: str) -> Dict[str, Any]:
        """
        Execute display/screen mirroring commands

        Handles commands like:
        - "Living Room TV" (implicit: connect to Living Room TV)
        - "screen mirror my Mac to the Living Room TV"
        - "connect to Living Room TV"
        - "connect to the TV" (uses context to resolve "the TV")
        - "extend display to Sony TV"
        - "airplay to Living Room TV"
        - "stop living room tv"
        - "disconnect from living room tv"
        - "disconnect from that display" (uses context)
        - "stop screen mirroring"
        - "change to extended display"
        - "switch to entire screen"
        - "set to window mode"

        Flow:
        1. TV is in standby mode (AirPlay chip active, broadcasts availability)
        2. macOS Control Center sees "Living Room TV"
        3. JARVIS detects "Living Room TV" via DNS-SD
        4. User command triggers AirPlay connection request
        5. Sony TV receives wake signal → Powers ON automatically
        6. Mac screen appears on Sony TV
        """
        command_lower = command_text.lower()
        logger.info(f"[DISPLAY] Processing display command: '{command_text}'")

        # DEBUG: Log to file
        with open("/tmp/jarvis_display_command.log", "a") as f:  # nosec B108 # Debug logging
            f.write(f"\n{'='*80}\n")
            f.write(f"[DISPLAY] _execute_display_command called\n")
            f.write(f"  Command: '{command_text}'\n")
            import datetime

            f.write(f"  Time: {datetime.datetime.now()}\n")

        # NEW: Try display reference handler first for intelligent voice command resolution
        display_ref = None
        logger.info(
            f"[DISPLAY] display_reference_handler exists: {self.display_reference_handler is not None}"
        )
        if self.display_reference_handler:
            logger.info("[DISPLAY] Using display_reference_handler to resolve command")
            try:
                display_ref = await self.display_reference_handler.handle_voice_command(
                    command_text
                )
                logger.info(f"[DISPLAY] display_ref returned: {display_ref}")

                if display_ref:
                    logger.info(
                        f"[DISPLAY] Display reference resolved: {display_ref.display_name} "
                        f"(action={display_ref.action.value}, mode={display_ref.mode.value if display_ref.mode else None}, "
                        f"confidence={display_ref.confidence:.2f}, strategy={display_ref.resolution_strategy.value})"
                    )

                    # NEW: Direct action routing based on display_ref.action
                    # This bypasses the old pattern matching logic and goes straight to execution
                    try:
                        result = await self._execute_display_action(display_ref, command_text)

                        # Learn from success/failure
                        if result.get("success"):
                            self.display_reference_handler.learn_from_success(
                                command_text, display_ref
                            )
                            logger.info(
                                f"[DISPLAY] ✅ Action completed successfully - learned from: '{command_text}'"
                            )
                        else:
                            self.display_reference_handler.learn_from_failure(
                                command_text, display_ref
                            )
                            logger.warning(
                                f"[DISPLAY] ❌ Action failed - learned from: '{command_text}'"
                            )

                        return result

                    except Exception as e:
                        logger.error(
                            f"[DISPLAY] Error executing display action: {e}", exc_info=True
                        )
                        # Learn from failure
                        self.display_reference_handler.learn_from_failure(command_text, display_ref)
                        # Fall through to legacy logic as fallback
                        logger.warning("[DISPLAY] Falling back to legacy display command logic")

            except Exception as e:
                logger.warning(
                    f"[DISPLAY] Display reference handler error (continuing with fallback): {e}"
                )
                # Continue with existing logic if handler fails

        try:
            # Try to get the running display monitor instance
            monitor = None

            # Check if we have app reference
            if hasattr(self, "_app") and self._app:
                if hasattr(self._app.state, "display_monitor"):
                    monitor = self._app.state.display_monitor
                    logger.info("[DISPLAY] Using running display monitor from app.state")

            # Fallback: get singleton instance
            if monitor is None:
                from display import get_display_monitor

                monitor = get_display_monitor()
                logger.info("[DISPLAY] Using display monitor singleton")

            # Check if this is a mode change command
            mode_keywords = ["change", "switch", "set"]
            mode_types = {
                "entire": ["entire", "entire screen", "full screen"],
                "window": ["window", "window or app", "app"],
                "extended": ["extended", "extend", "extended display"],
            }

            is_mode_change = any(keyword in command_lower for keyword in mode_keywords)
            detected_mode = None

            if is_mode_change:
                # Detect which mode the user wants
                for mode_key, mode_phrases in mode_types.items():
                    if any(phrase in command_lower for phrase in mode_phrases):
                        detected_mode = mode_key
                        break

            if is_mode_change and detected_mode:
                # Handle mode change
                logger.info(f"[DISPLAY] Detected mode change command to '{detected_mode}'")

                # Check config for connected displays
                status = monitor.get_status()
                connected_displays = list(status.get("connected_displays", []))

                logger.debug(f"[DISPLAY] Connected displays: {connected_displays}")

                # If only one display is connected, change its mode
                if len(connected_displays) == 1:
                    display_id = connected_displays[0]
                    logger.info(f"[DISPLAY] Changing '{display_id}' to {detected_mode} mode...")

                    result = await monitor.change_display_mode(display_id, detected_mode)

                    if result.get("success"):
                        mode_name = result.get("mode", detected_mode)
                        return {
                            "success": True,
                            "response": f"Changed to {mode_name} mode, sir.",
                            "mode": mode_name,
                        }
                    else:
                        return {
                            "success": False,
                            "response": result.get(
                                "message", f"Unable to change to {detected_mode} mode."
                            ),
                        }
                elif len(connected_displays) > 1:
                    # Multiple displays connected, need to specify which one
                    return {
                        "success": False,
                        "response": f"Multiple displays are connected. Please specify which display to change: {', '.join(connected_displays)}",
                        "connected_displays": connected_displays,
                    }
                else:
                    return {
                        "success": False,
                        "response": "No displays are currently connected.",
                    }

            # Check if this is a disconnection command
            disconnect_keywords = ["stop", "disconnect", "turn off", "disable"]
            is_disconnect = any(keyword in command_lower for keyword in disconnect_keywords)

            # Make sure it's not a mode change command being misdetected
            if is_disconnect and not is_mode_change:
                # Handle disconnection
                logger.info(f"[DISPLAY] Detected disconnection command")

                # Check config for monitored displays
                status = monitor.get_status()
                connected_displays = list(status.get("connected_displays", []))

                logger.debug(f"[DISPLAY] Connected displays: {connected_displays}")

                # If only one display is connected, disconnect it
                if len(connected_displays) == 1:
                    display_id = connected_displays[0]
                    logger.info(f"[DISPLAY] Disconnecting from '{display_id}'...")

                    result = await monitor.disconnect_display(display_id)

                    if result.get("success"):
                        return {
                            "success": True,
                            "response": "Display disconnected, sir.",
                        }
                    else:
                        return {
                            "success": False,
                            "response": result.get("message", "Unable to disconnect display."),
                        }
                elif len(connected_displays) > 1:
                    # Multiple displays connected, need to specify which one
                    return {
                        "success": False,
                        "response": f"Multiple displays are connected. Please specify which one to disconnect: {', '.join(connected_displays)}",
                        "connected_displays": connected_displays,
                    }
                else:
                    return {
                        "success": False,
                        "response": "No displays are currently connected.",
                    }

            # Extract display name from command (for connection)
            # Look for TV names, room names, or brand names
            display_name = None

            # Check config for monitored displays
            status = monitor.get_status()
            available_displays = monitor.get_available_display_details()

            logger.debug(
                f"[DISPLAY] Available displays: {[d['display_name'] for d in available_displays]}"
            )

            # Match display name in command text
            display_id = None
            for display_info in available_displays:
                name = display_info["display_name"]
                # Check if display name appears in command (case insensitive)
                if name.lower() in command_lower:
                    display_name = name
                    display_id = display_info["display_id"]
                    break

            if not display_name:
                # Try to extract room name or TV reference
                import re

                # Match patterns like "living room", "bedroom", "sony", "lg", etc.
                patterns = [
                    r"(living\s*room|bedroom|kitchen|office)\s*tv",
                    r"(sony|lg|samsung)\s*tv",
                    r"to\s+([a-z\s]+tv)",
                ]
                for pattern in patterns:
                    match = re.search(pattern, command_lower)
                    if match:
                        extracted = match.group(0).replace("to ", "").strip()
                        # Try to match with available displays
                        for display_info in available_displays:
                            if extracted.lower() in display_info["display_name"].lower():
                                display_name = display_info["display_name"]
                                display_id = display_info["display_id"]
                                break
                        if display_name:
                            break

            # Determine mode (mirror vs extend)
            mode = "mirror" if "mirror" in command_lower else "extend"

            if not display_id:
                # No specific display found, show available options
                if available_displays:
                    names = [d["display_name"] for d in available_displays]
                    return {
                        "success": False,
                        "response": f"I couldn't determine which display to connect to. Available displays: {', '.join(names)}. Please specify one.",
                        "available_displays": names,
                    }
                else:
                    return {
                        "success": False,
                        "response": "No displays are currently available. Please ensure your TV or display is powered on and connected to the network.",
                    }

            logger.info(
                f"[DISPLAY] Connecting to '{display_name}' (id: {display_id}) in {mode} mode..."
            )

            # DEBUG: Log to file
            with open("/tmp/jarvis_display_command.log", "a") as f:  # nosec B108 # Debug logging
                f.write(f"\n{'='*60}\n")
                f.write(f"[DISPLAY] About to call monitor.connect_display('{display_id}')\n")
                f.write(f"  Display name: {display_name}\n")
                f.write(f"  Mode: {mode}\n")

            # Connect to display using display_id
            result = await monitor.connect_display(display_id)

            # DEBUG: Log result
            with open("/tmp/jarvis_display_command.log", "a") as f:  # nosec B108 # Debug logging
                f.write(f"[DISPLAY] Result: {result.get('success')}\n")
                f.write(f"  Message: {result.get('message', 'none')}\n")

            if result.get("success"):
                # Check if already connected (cached) or connection in progress
                if result.get("cached"):
                    response = f"Your screen is already being {mode}ed to {display_name}, sir."
                elif result.get("in_progress"):
                    # This should not happen anymore with the fix, but keep as fallback
                    response = f"Connecting to {display_name} now, sir."
                else:
                    response = f"Connected to {display_name}, sir."

                return {
                    "success": True,
                    "response": response,
                    "display_name": display_name,
                    "mode": mode,
                    "already_connected": result.get("cached", False),
                }
            else:
                return {
                    "success": False,
                    "response": result.get("message", f"Unable to connect to {display_name}."),
                    "display_name": display_name,
                }

        except Exception as e:
            logger.error(f"[DISPLAY] Error executing display command: {e}", exc_info=True)
            return {
                "success": False,
                "response": f"I encountered an error while trying to connect to the display: {str(e)}",
            }

    async def _execute_system_command(self, command_text: str) -> Dict[str, Any]:
        """Dynamically execute system commands without hardcoding"""

        command_lower = command_text.lower()
        
        # =========================================================================
        # ULTRA-FAST PATH: Web search via BrowsingAgent → fallback to open
        # =========================================================================
        # v6.4: BrowsingAgent returns structured results via WebSearchExtractor
        # (DuckDuckGo/Brave/Bing/Google/SearXNG — API-based, <500ms).
        # Falls back to `open` subprocess (opens Google in browser) if unavailable.
        # This still bypasses MacOSController/DynamicAppController/screen lock.
        # =========================================================================
        import re
        search_patterns = [
            r"^search\s+(?:for\s+)?(.+)$",
            r"^google\s+(.+)$",
            r"^look\s+up\s+(.+)$",
            r"^find\s+(.+)$",
        ]

        for pattern in search_patterns:
            match = re.match(pattern, command_lower.strip())
            if match:
                query = match.group(1).strip()
                if query:
                    return await self._execute_web_search(query)

        # Check if this is actually a voice unlock command misclassified as system
        if ("voice" in command_lower and "unlock" in command_lower) or (
            "enable" in command_lower and "voice unlock" in command_lower
        ):
            # Redirect to voice unlock handler
            handler = await self._get_handler(CommandType.VOICE_UNLOCK)
            if handler:
                result = await handler.handle_command(command_text)
                return {
                    "success": result.get("success", result.get("type") == "voice_unlock"),
                    "response": result.get("message", result.get("response", "")),
                    "command_type": "voice_unlock",
                    **result,
                }

        try:
            from system_control.dynamic_app_controller import get_dynamic_app_controller
            from system_control.macos_controller import MacOSController

            macos_controller = MacOSController()
            dynamic_controller = get_dynamic_app_controller()

            # Check for lock/unlock screen commands first
            # Use the existing voice unlock integration for proper daemon support
            if ("lock" in command_lower or "unlock" in command_lower) and "screen" in command_lower:
                logger.info(
                    f"[SYSTEM] Screen lock/unlock command detected, using voice unlock handler"
                )
                try:
                    from api.simple_unlock_handler import handle_unlock_command

                    # Create a jarvis_instance-like object to pass audio data for voice verification
                    class AudioContainer:
                        def __init__(self, audio_data, speaker_name):
                            self.last_audio_data = audio_data
                            self.last_speaker_name = speaker_name

                    # Pass audio data through jarvis_instance for voice biometric verification
                    jarvis_instance = AudioContainer(
                        audio_data=self.current_audio_data,
                        speaker_name=self.current_speaker_name
                    ) if self.current_audio_data else None

                    if jarvis_instance:
                        logger.info(f"🎤 [VOICE-UNLOCK] Passing audio data to unlock handler for verification ({len(self.current_audio_data) if self.current_audio_data else 0} bytes)")
                    else:
                        logger.warning(f"⚠️ [VOICE-UNLOCK] No audio data available - voice verification will be bypassed")

                    # Pass the command to the existing unlock handler which integrates with the daemon
                    result = await handle_unlock_command(command_text, jarvis_instance=jarvis_instance)

                    # Ensure we return a properly formatted result
                    if isinstance(result, dict):
                        # Add command_type if not present
                        if "command_type" not in result:
                            import re

                            tokens = set(re.findall(r"[a-z']+", command_lower))
                            if "unlock" in tokens:
                                result["command_type"] = "screen_unlock"
                            elif "lock" in tokens:
                                result["command_type"] = "screen_lock"
                            else:
                                result["command_type"] = "screen_control"
                        return result
                    else:
                        # Fallback to macos_controller if the unlock handler returns unexpected format
                        logger.warning(
                            f"[SYSTEM] Unexpected result from unlock handler, falling back"
                        )
                        result = await macos_controller.handle_command(command_text)
                        return result

                except ImportError:
                    logger.warning(
                        f"[SYSTEM] Simple unlock handler not available, using macos_controller"
                    )
                    result = await macos_controller.handle_command(command_text)
                    return result
                except Exception as e:
                    logger.error(f"[SYSTEM] Error with unlock handler: {e}, falling back")
                    result = await macos_controller.handle_command(command_text)
                    return result

            # Parse command dynamically
            command_type, target, params = self._parse_system_command(command_text)

            logger.info(f"[SYSTEM] Parsing '{command_text}'")
            logger.info(f"[SYSTEM] Parsed: type={command_type}, target={target}, params={params}")

            # Execute based on parsed command type
            if command_type == "tab_control":
                # Handle new tab operations
                browser = target or self._detect_default_browser()
                url = params.get("url")
                success, message = macos_controller.open_new_tab(browser, url)
                return {"success": success, "response": message}

            elif command_type == "app_open":
                # Open application
                if target:
                    success, message = await dynamic_controller.open_app_intelligently(target)
                    return {"success": success, "response": message}
                else:
                    return {
                        "success": False,
                        "response": "Please specify which app to open",
                    }

            elif command_type == "app_close":
                # Close application
                if target:
                    success, message = await dynamic_controller.close_app_intelligently(target)
                    return {"success": success, "response": message}
                else:
                    return {
                        "success": False,
                        "response": "Please specify which app to close",
                    }

            elif command_type == "system_setting":
                # Handle system settings
                if target == "volume":
                    action = params.get("action")
                    if action == "mute":
                        success, message = macos_controller.mute_volume(True)
                    elif action == "unmute":
                        success, message = macos_controller.mute_volume(False)
                    elif "level" in params:
                        success, message = macos_controller.set_volume(params["level"])
                    else:
                        return {
                            "success": False,
                            "response": "Please specify volume level or mute/unmute",
                        }
                    return {"success": success, "response": message}

                elif target == "brightness":
                    if "level" in params:
                        level = params["level"] / 100.0  # Convert to 0.0-1.0
                        success, message = macos_controller.adjust_brightness(level)
                    else:
                        return {
                            "success": False,
                            "response": "Please specify brightness level (0-100)",
                        }
                    return {"success": success, "response": message}

                elif target == "wifi":
                    enable = params.get("enable", True)
                    success, message = macos_controller.toggle_wifi(enable)
                    return {"success": success, "response": message}

                elif target == "screenshot":
                    success, message = macos_controller.take_screenshot()
                    return {"success": success, "response": message}

                else:
                    return {
                        "success": False,
                        "response": f"Unknown system setting: {target}",
                    }

            elif command_type == "web_action":
                # Handle web navigation and searches
                # v6.4: BrowsingAgent-first for search, Playwright for navigation
                action = params.get("action")
                browser = target

                if action == "search" and "query" in params:
                    # Use unified search (same as ultra-fast path)
                    return await self._execute_web_search(params["query"])

                elif action == "navigate" and "url" in params:
                    # Try BrowsingAgent for navigation (gets page title + content)
                    browsing_result = await self._try_browsing_navigate(params["url"])
                    if browsing_result:
                        return browsing_result
                    # Fallback: AppleScript
                    success, message = await macos_controller.open_url(params["url"], browser)
                    return {"success": success, "response": message}

                else:
                    return {
                        "success": False,
                        "response": "Please specify what to search for or where to navigate",
                    }

            elif command_type == "multi_tab_search":
                # Handle multi-tab searches dynamically
                return await self._handle_multi_tab_search(command_text, target)

            elif command_type == "type_text":
                # Handle typing
                text = params.get("text", "")
                press_enter = params.get("press_enter", False)
                browser = target

                if text:
                    success, message = macos_controller.type_in_browser(text, browser, press_enter)
                    return {"success": success, "response": message}
                else:
                    return {"success": False, "response": "Please specify what to type"}

            else:
                # Unknown command type - try to be helpful
                return {
                    "success": False,
                    "response": f"I'm not sure how to handle that command. I parsed it as '{command_type}' but couldn't execute it. Try rephrasing or being more specific.",
                }

        except Exception as e:
            logger.error(f"Error executing system command: {e}", exc_info=True)
            return {
                "success": False,
                "response": f"Failed to execute system command: {str(e)}",
            }

    # =========================================================================
    # v6.4: BrowsingAgent integration methods
    # =========================================================================

    async def _execute_web_search(self, query: str) -> Dict[str, Any]:
        """Unified web search: BrowsingAgent API → open subprocess fallback.

        Three-tier fallback:
        1. WebSearchExtractor via BrowsingAgent (structured results, <500ms)
        2. open subprocess (opens browser, no content returned)
        3. Error response

        v6.4: Replaces the old ultra-fast path that only opened Google.
        """
        logger.info(f"[SEARCH] Web search for '{query}'")

        # Tier 1: Try BrowsingAgent (structured API search)
        # Single timeout budget covers init + search (no stacking)
        import time as _time
        search_budget = float(os.environ.get("BROWSE_SEARCH_BUDGET", "10.0"))
        try:
            from browsing.browsing_agent import get_browsing_agent

            budget_start = _time.monotonic()
            agent = await asyncio.wait_for(get_browsing_agent(), timeout=search_budget)
            if agent:
                remaining = max(1.0, search_budget - (_time.monotonic() - budget_start))
                result = await asyncio.wait_for(
                    agent.execute_task({
                        "action": "search",
                        "query": query,
                        "max_results": 5,
                    }),
                    timeout=remaining,
                )
                if result.get("success") and result.get("results"):
                    formatted = self._format_search_results(result["results"])
                    logger.info(
                        f"[SEARCH] BrowsingAgent returned {result.get('count', len(result['results']))} "
                        f"results via {result.get('provider', 'unknown')}"
                    )
                    return {
                        "success": True,
                        "response": formatted,
                        "structured_results": result["results"],
                        "source": "browsing_agent",
                        "provider": result.get("provider", "unknown"),
                        "command_type": "web_search",
                    }
        except asyncio.TimeoutError:
            logger.warning(f"[SEARCH] BrowsingAgent timed out for '{query}'")
        except ImportError:
            logger.debug("[SEARCH] BrowsingAgent not available (browsing module not found)")
        except Exception as e:
            logger.debug(f"[SEARCH] BrowsingAgent unavailable: {e}")

        # Tier 2: Fallback — open in browser (original ultra-fast path behavior)
        try:
            from urllib.parse import quote

            url = f"https://www.google.com/search?q={quote(query)}"
            process = await asyncio.create_subprocess_exec(
                "open", url,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(process.wait(), timeout=5.0)

            if process.returncode == 0:
                logger.info(f"[SEARCH] Opened browser for '{query}'")
                return {
                    "success": True,
                    "response": f"Searching for {query}, Sir",
                    "command_type": "web_search",
                    "fast_path": "browser_open",
                }
            else:
                logger.warning(f"[SEARCH] open returned code {process.returncode}")
        except asyncio.TimeoutError:
            logger.warning(f"[SEARCH] Browser open timed out for '{query}'")
        except Exception as e:
            logger.warning(f"[SEARCH] Browser open failed: {e}")

        return {"success": False, "response": f"Unable to search for '{query}'"}

    def _format_search_results(self, results: List[Dict[str, Any]]) -> str:
        """Format structured search results for voice/text response.

        Voice-friendly: titles + snippets only (no URLs — terrible for TTS).
        URLs are still available in the structured_results dict for display.
        """
        if not results:
            return "No results found."

        lines = [f"Here are the top {min(len(results), 5)} results:"]
        for i, r in enumerate(results[:5], 1):
            title = r.get("title", "Untitled")
            snippet = r.get("snippet", "")
            lines.append(f"{i}. {title}")
            if snippet:
                lines.append(f"   {snippet[:150]}")
        return "\n".join(lines)

    async def _try_browsing_navigate(self, url: str) -> Optional[Dict[str, Any]]:
        """Try to navigate via BrowsingAgent (gets page title). Returns None for fallback."""
        try:
            from browsing.browsing_agent import get_browsing_agent

            agent = await asyncio.wait_for(get_browsing_agent(), timeout=5.0)
            if agent and agent._playwright_available:
                result = await asyncio.wait_for(
                    agent.execute_task({"action": "navigate", "url": url}),
                    timeout=float(os.environ.get("BROWSE_NAV_TIMEOUT", "15.0")),
                )
                if result.get("success"):
                    title = result.get("title", url)
                    return {
                        "success": True,
                        "response": f"Navigated to {title}",
                        "page_title": title,
                        "url": result.get("url", url),
                        "source": "browsing_agent",
                    }
        except asyncio.TimeoutError:
            logger.debug(f"[BROWSE] Navigation timed out for {url}")
        except ImportError:
            pass
        except Exception as e:
            logger.debug(f"[BROWSE] Navigation failed for {url}: {e}")

        return None  # Signal: use fallback

    async def _handle_multi_tab_search(
        self, command_text: str, browser: Optional[str]
    ) -> Dict[str, Any]:
        """Handle searches across multiple tabs dynamically"""
        try:
            from system_control.dynamic_app_controller import get_dynamic_app_controller
            from system_control.macos_controller import MacOSController

            macos_controller = MacOSController()
            dynamic_controller = get_dynamic_app_controller()

            # Extract search terms dynamically
            search_terms = self._extract_multi_search_terms(command_text)

            if not search_terms:
                return {
                    "success": False,
                    "response": "Couldn't identify what to search for",
                }

            # Detect browser if not specified
            if not browser:
                browser = self._detect_browser_from_context(command_text)

            # Open browser if needed
            if "open" in command_text.lower():
                success, _ = await dynamic_controller.open_app_intelligently(browser)
                if success:
                    await asyncio.sleep(1.5)

            # Open tabs for each search term
            results = []
            for i, term in enumerate(search_terms):
                if i == 0:
                    # First search in current tab
                    success, msg = macos_controller.web_search(term, browser=browser)
                else:
                    # Subsequent searches in new tabs
                    await asyncio.sleep(0.5)
                    search_url = f"https://google.com/search?q={term.replace(' ', '+')}"
                    success, msg = macos_controller.open_new_tab(browser, search_url)
                results.append(success)

            if all(results):
                terms_str = " and ".join(f"'{term}'" for term in search_terms)
                return {
                    "success": True,
                    "response": f"Searching for {terms_str} in separate tabs, Sir",
                }
            else:
                return {"success": False, "response": "Had trouble opening some tabs"}

        except Exception as e:
            logger.error(f"Error in multi-tab search: {e}", exc_info=True)
            return {
                "success": False,
                "response": f"Failed to perform multi-tab search: {str(e)}",
            }

    def _extract_multi_search_terms(self, command_text: str) -> List[str]:
        """Extract multiple search terms from command"""
        # Look for pattern like "search for X and Y and Z"
        patterns = ["search for", "google", "look up", "find"]

        for pattern in patterns:
            if pattern in command_text.lower():
                idx = command_text.lower().find(pattern)
                after_pattern = command_text[idx + len(pattern) :].strip()

                # Remove trailing instructions
                for instruction in [
                    "on separate tabs",
                    "in different tabs",
                    "on multiple tabs",
                ]:
                    if instruction in after_pattern.lower():
                        after_pattern = after_pattern[
                            : after_pattern.lower().find(instruction)
                        ].strip()

                # Split by 'and' to get individual terms
                terms = []
                parts = after_pattern.split(" and ")
                for part in parts:
                    part = part.strip()
                    if part and not any(skip in part.lower() for skip in ["open", "close", "quit"]):
                        terms.append(part)

                return terms

        return []

    def _detect_browser_from_context(self, command_text: str) -> str:
        """Detect which browser is mentioned in command"""
        words = command_text.lower().split()

        # Check for any learned app that might be a browser
        for word in words:
            if self.pattern_learner.is_learned_app(word):
                # Check if it's likely a browser
                browser_hints = ["safari", "chrome", "firefox", "browser", "web"]
                if any(hint in word for hint in browser_hints):
                    return word

        # Default to detected default browser
        return self._detect_default_browser()


# Singleton instance
_unified_processor = None


def get_unified_processor(api_key: Optional[str] = None, app=None) -> UnifiedCommandProcessor:
    """Get or create the unified command processor"""
    global _unified_processor
    if _unified_processor is None:
        _unified_processor = UnifiedCommandProcessor(api_key, app=app)
    elif app is not None and not hasattr(_unified_processor, "_app"):
        # Update existing processor with app reference
        _unified_processor._app = app
    return _unified_processor
