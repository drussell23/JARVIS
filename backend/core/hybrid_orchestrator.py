"""
JARVIS Hybrid Orchestrator - UAE/SAI/CAI Integrated with LangGraph Chain-of-Thought
Main entry point for hybrid local/cloud architecture
Coordinates between local Mac and GCP backends with intelligent routing

Integrated Intelligence Systems:
- UAE (Unified Awareness Engine): Real-time context aggregation
- SAI (Self-Aware Intelligence): Self-healing and optimization
- CAI (Context Awareness Intelligence): Intent prediction
- learning_database: Persistent memory and pattern learning
- LangGraph Chain-of-Thought: Multi-step reasoning with explicit thought chains
- UnifiedIntelligenceOrchestrator: Coordinated intelligence fusion

This module provides the main orchestration layer for JARVIS, handling:
- Intelligent request routing between local and cloud backends
- Integration with multiple AI systems (UAE, SAI, CAI)
- Chain-of-thought reasoning for transparent decision-making
- Model lifecycle management and selection
- Cost optimization and resource management
- Health monitoring and automatic failover
"""

import asyncio
import hashlib
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional
from enum import Enum

from core.hybrid_backend_client import HybridBackendClient
from core.hybrid_router import HybridRouter, RouteDecision, RoutingContext

logger = logging.getLogger(__name__)

# =============================================================================
# TRINITY UNIFIED LOOP MANAGER INTEGRATION (v3.0)
# =============================================================================
# Import safe async primitives that never fail due to missing event loops
try:
    _cross_repo_path = Path.home() / ".jarvis" / "cross_repo"
    if str(_cross_repo_path) not in sys.path:
        sys.path.insert(0, str(_cross_repo_path))

    from unified_loop_manager import (
        safe_to_thread,
        safe_create_task,
        safe_get_running_loop,
        get_trinity_manager,
    )
    TRINITY_AVAILABLE = True
except ImportError:
    TRINITY_AVAILABLE = False

    # Fallback implementations
    async def safe_to_thread(func, *args, **kwargs):
        """Fallback safe_to_thread with loop creation."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        return await loop.run_in_executor(None, lambda: func(*args, **kwargs))

    def safe_create_task(coro, *, name=None):
        return asyncio.create_task(coro, name=name)

    def safe_get_running_loop():
        try:
            return asyncio.get_running_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            return loop

    def get_trinity_manager():
        return None

    logger.debug("Trinity not available, using fallback async primitives")


class IntelligenceMode(Enum):
    """Intelligence system operation modes."""
    STANDARD = "standard"  # Original UAE/SAI/CAI without chain-of-thought
    ENHANCED = "enhanced"  # Enhanced with LangGraph chain-of-thought reasoning
    UNIFIED = "unified"    # Full UnifiedIntelligenceOrchestrator coordination
    REASONING_GRAPH = "reasoning_graph"  # Multi-branch reasoning with failure recovery
    VOICE_AUTH = "voice_auth"  # Voice biometric authentication with intelligent reasoning


# UAE/SAI/CAI Integration (lazy loaded)
_uae_engine = None
_sai_system = None
_cai_system = None
_learning_db = None
_sai_load_error: Optional[str] = None
_cai_load_error: Optional[str] = None

# Enhanced Intelligence with LangGraph (lazy loaded)
_enhanced_uae = None
_enhanced_sai = None
_enhanced_cai = None
_unified_orchestrator = None
_reasoning_graph_engine = None
_tts_handler = None
_intelligence_mode = IntelligenceMode.REASONING_GRAPH  # Multi-branch reasoning by default

# Phase 3.1: Local LLM Integration (lazy loaded)
_llm_inference = None

# Phase 3.1+: Intelligent Model Management (lazy loaded)
_model_registry = None
_lifecycle_manager = None
_model_selector = None

# Voice Auth Intelligence (lazy loaded)
_voice_auth_orchestrator = None
_voice_auth_reasoning_graph = None


def _get_uae():
    """Lazy load UAE (Unified Awareness Engine).
    
    Returns:
        UnifiedAwarenessEngine or None: UAE instance if available, None otherwise
    """
    global _uae_engine
    if _uae_engine is None:
        try:
            from intelligence.unified_awareness_engine import UnifiedAwarenessEngine

            _uae_engine = UnifiedAwarenessEngine()
            logger.info("✅ UAE loaded")
        except Exception as e:
            logger.warning(f"UAE not available: {e}")
    return _uae_engine


_sai_attempted = False


def _get_sai():
    """Lazy load SAI (Self-Aware Intelligence).

    Returns:
        SelfAwareIntelligence or None: SAI instance if available, None otherwise
    """
    global _sai_system, _sai_attempted, _sai_load_error
    if _sai_system is None and not _sai_attempted:
        _sai_attempted = True
        try:
            from intelligence.self_aware_intelligence import SelfAwareIntelligence

            _sai_system = SelfAwareIntelligence()
            _sai_load_error = None
            logger.info("✅ SAI loaded (%s)", type(_sai_system).__name__)
        except Exception as e:
            _sai_load_error = f"{type(e).__name__}: {e}"
            logger.exception("SAI initialization failed")
    return _sai_system


_cai_attempted = False


def _get_cai():
    """Lazy load CAI (Context Awareness Intelligence).

    Returns:
        ContextAwarenessIntelligence or None: CAI instance if available, None otherwise
    """
    global _cai_system, _cai_attempted, _cai_load_error
    if _cai_system is None and not _cai_attempted:
        _cai_attempted = True
        try:
            from intelligence.context_awareness_intelligence import ContextAwarenessIntelligence

            _cai_system = ContextAwarenessIntelligence()
            _cai_load_error = None
            logger.info("✅ CAI loaded (%s)", type(_cai_system).__name__)
        except Exception as e:
            _cai_load_error = f"{type(e).__name__}: {e}"
            logger.exception("CAI initialization failed")
    return _cai_system


def get_sai_loader_status() -> Dict[str, Any]:
    """Get SAI lazy-loader status for deterministic health reporting."""
    return {
        "attempted": _sai_attempted,
        "available": _sai_system is not None,
        "error": _sai_load_error,
        "implementation": type(_sai_system).__name__ if _sai_system is not None else None,
    }


def get_cai_loader_status() -> Dict[str, Any]:
    """Get CAI lazy-loader status for deterministic health reporting."""
    return {
        "attempted": _cai_attempted,
        "available": _cai_system is not None,
        "error": _cai_load_error,
        "implementation": type(_cai_system).__name__ if _cai_system is not None else None,
    }


async def _get_learning_db():
    """Lazy load learning database for persistent memory.
    
    Returns:
        LearningDatabase or None: Learning database instance if available, None otherwise
    """
    global _learning_db
    if _learning_db is None:
        try:
            from intelligence.learning_database import get_learning_database

            _learning_db = await get_learning_database()
            logger.info("✅ learning_database loaded")
        except Exception as e:
            logger.warning(f"learning_database not available: {e}")
    return _learning_db


def _get_llm():
    """Lazy load Local LLM (Phase 3.1).
    
    Returns:
        LocalLLMInference or None: LLM inference instance if available, None otherwise
    """
    global _llm_inference
    if _llm_inference is None:
        try:
            from intelligence.local_llm_inference import get_llm_inference

            _llm_inference = get_llm_inference()
            logger.info("✅ Local LLM (LLaMA 3.1 70B) ready for lazy loading")
        except Exception as e:
            logger.warning(f"Local LLM not available: {e}")
    return _llm_inference


def _get_model_registry():
    """Lazy load Model Registry (Phase 3.1+).
    
    Returns:
        ModelRegistry or None: Model registry instance if available, None otherwise
    """
    global _model_registry
    if _model_registry is None:
        try:
            from intelligence.model_registry import get_model_registry

            _model_registry = get_model_registry()
            logger.info("✅ Model Registry initialized")
        except Exception as e:
            logger.warning(f"Model Registry not available: {e}")
    return _model_registry


def _get_lifecycle_manager():
    """Lazy load Model Lifecycle Manager (Phase 3.1+).
    
    Returns:
        ModelLifecycleManager or None: Lifecycle manager instance if available, None otherwise
    """
    global _lifecycle_manager
    if _lifecycle_manager is None:
        try:
            from intelligence.model_lifecycle_manager import get_lifecycle_manager

            _lifecycle_manager = get_lifecycle_manager()
            logger.info("✅ Model Lifecycle Manager initialized")
        except Exception as e:
            logger.warning(f"Model Lifecycle Manager not available: {e}")
    return _lifecycle_manager


def _get_model_selector():
    """Lazy load Intelligent Model Selector (Phase 3.1+).

    Returns:
        IntelligentModelSelector or None: Model selector instance if available, None otherwise
    """
    global _model_selector
    if _model_selector is None:
        try:
            from intelligence.model_selector import get_model_selector

            _model_selector = get_model_selector()
            logger.info("✅ Intelligent Model Selector initialized")
        except Exception as e:
            logger.warning(f"Model Selector not available: {e}")
    return _model_selector


# ============== LANGGRAPH CHAIN-OF-THOUGHT INTELLIGENCE ==============

def _get_enhanced_uae():
    """Lazy load Enhanced UAE with LangGraph chain-of-thought reasoning.

    Returns:
        EnhancedUAE or None: Enhanced UAE instance if available, None otherwise
    """
    global _enhanced_uae
    if _enhanced_uae is None:
        try:
            from intelligence.uae_langgraph import create_enhanced_uae

            _enhanced_uae = create_enhanced_uae()
            logger.info("✅ EnhancedUAE with chain-of-thought loaded")
        except Exception as e:
            logger.warning(f"EnhancedUAE not available: {e}")
    return _enhanced_uae


def _get_enhanced_sai():
    """Lazy load Enhanced SAI with LangGraph chain-of-thought reasoning.

    Returns:
        EnhancedSAI or None: Enhanced SAI instance if available, None otherwise
    """
    global _enhanced_sai
    if _enhanced_sai is None:
        try:
            from intelligence.intelligence_langgraph import create_enhanced_sai

            _enhanced_sai = create_enhanced_sai()
            logger.info("✅ EnhancedSAI with chain-of-thought loaded")
        except Exception as e:
            logger.warning(f"EnhancedSAI not available: {e}")
    return _enhanced_sai


def _get_enhanced_cai():
    """Lazy load Enhanced CAI with LangGraph chain-of-thought reasoning.

    Returns:
        EnhancedCAI or None: Enhanced CAI instance if available, None otherwise
    """
    global _enhanced_cai
    if _enhanced_cai is None:
        try:
            from intelligence.intelligence_langgraph import create_enhanced_cai

            _enhanced_cai = create_enhanced_cai()
            logger.info("✅ EnhancedCAI with chain-of-thought loaded")
        except Exception as e:
            logger.warning(f"EnhancedCAI not available: {e}")
    return _enhanced_cai


async def _get_unified_orchestrator():
    """Lazy load UnifiedIntelligenceOrchestrator for coordinated intelligence.

    The UnifiedIntelligenceOrchestrator coordinates all enhanced intelligence
    systems (UAE, SAI, CAI) with chain-of-thought reasoning for sophisticated
    multi-system decision making.

    Returns:
        UnifiedIntelligenceOrchestrator or None: Orchestrator if available
    """
    global _unified_orchestrator
    if _unified_orchestrator is None:
        try:
            from intelligence.intelligence_langgraph import create_unified_orchestrator

            _unified_orchestrator = await create_unified_orchestrator()
            logger.info("✅ UnifiedIntelligenceOrchestrator loaded")
        except Exception as e:
            logger.warning(f"UnifiedIntelligenceOrchestrator not available: {e}")
    return _unified_orchestrator


async def _get_reasoning_graph_engine():
    """Lazy load ReasoningGraphEngine for multi-branch reasoning.

    The ReasoningGraphEngine provides advanced multi-branch reasoning with:
    - Multiple solution branches explored in parallel
    - Automatic failure recovery and alternative generation
    - Real-time voice narration of reasoning process
    - Learning from outcomes for continuous improvement

    Returns:
        ReasoningGraphEngine or None: Engine if available
    """
    global _reasoning_graph_engine
    if _reasoning_graph_engine is None:
        try:
            from intelligence.reasoning_graph_engine import create_reasoning_graph_engine
            from autonomy.tool_orchestrator import get_orchestrator as get_tool_orchestrator

            # Get TTS callback for narration
            tts_callback = await _get_tts_callback()

            # Get tool orchestrator for execution
            tool_orchestrator = get_tool_orchestrator()

            _reasoning_graph_engine = create_reasoning_graph_engine(
                tool_orchestrator=tool_orchestrator,
                tts_callback=tts_callback,
                max_parallel_branches=3,
                max_total_branches=10
            )
            logger.info("✅ ReasoningGraphEngine with multi-branch reasoning loaded")
        except Exception as e:
            logger.warning(f"ReasoningGraphEngine not available: {e}")
    return _reasoning_graph_engine


async def _get_tts_callback():
    """Get TTS callback for voice narration.

    Returns:
        Async callback function for TTS or None
    """
    global _tts_handler
    if _tts_handler is None:
        try:
            from api.async_tts_handler import get_tts_handler, speak_async

            _tts_handler = speak_async
            logger.info("✅ TTS handler loaded for voice narration")
        except Exception as e:
            logger.debug(f"TTS handler not available (optional): {e}")
            _tts_handler = None
    return _tts_handler


# ============== VOICE AUTH INTELLIGENCE ==============

async def _get_voice_auth_orchestrator():
    """Lazy load Voice Auth Orchestrator for intelligent voice authentication.

    The VoiceAuthOrchestrator provides multi-factor authentication with:
    - Primary voice biometric verification (85% threshold)
    - Behavioral fusion fallback (80% threshold)
    - Challenge question fallback
    - Apple Watch proximity fallback
    - Password final fallback

    Returns:
        VoiceAuthOrchestrator or None: Orchestrator if available
    """
    global _voice_auth_orchestrator
    if _voice_auth_orchestrator is None:
        try:
            from voice_unlock.orchestration import get_voice_auth_orchestrator

            _voice_auth_orchestrator = await get_voice_auth_orchestrator()
            logger.info("✅ VoiceAuthOrchestrator with fallback chain loaded")
        except Exception as e:
            logger.warning(f"VoiceAuthOrchestrator not available: {e}")
    return _voice_auth_orchestrator


async def _get_voice_auth_reasoning_graph():
    """Lazy load Voice Auth Reasoning Graph for intelligent authentication reasoning.

    The VoiceAuthenticationReasoningGraph provides LangGraph-based reasoning:
    - Multi-phase verification pipeline (PERCEIVING -> DECIDING)
    - Hypothesis-driven reasoning for borderline cases
    - Early exit optimization for high-confidence cases
    - Comprehensive error recovery
    - Real-time metrics and observability

    Returns:
        VoiceAuthenticationReasoningGraph or None: Graph if available
    """
    global _voice_auth_reasoning_graph
    if _voice_auth_reasoning_graph is None:
        try:
            from voice_unlock.reasoning import get_voice_auth_reasoning_graph

            _voice_auth_reasoning_graph = await get_voice_auth_reasoning_graph()
            logger.info("✅ VoiceAuthenticationReasoningGraph loaded")
        except Exception as e:
            logger.warning(f"VoiceAuthenticationReasoningGraph not available: {e}")
    return _voice_auth_reasoning_graph


def set_intelligence_mode(mode: IntelligenceMode) -> None:
    """Set the intelligence system operation mode.

    Args:
        mode: Intelligence mode to use
            - STANDARD: Original UAE/SAI/CAI without chain-of-thought
            - ENHANCED: Enhanced systems with LangGraph chain-of-thought
            - UNIFIED: Full UnifiedIntelligenceOrchestrator coordination
            - REASONING_GRAPH: Multi-branch reasoning with failure recovery
            - VOICE_AUTH: Voice biometric authentication with intelligent reasoning
    """
    global _intelligence_mode
    _intelligence_mode = mode
    logger.info(f"🧠 Intelligence mode set to: {mode.value}")


def get_intelligence_mode() -> IntelligenceMode:
    """Get the current intelligence system operation mode.

    Returns:
        Current IntelligenceMode
    """
    return _intelligence_mode

# =====================================================================


class HybridOrchestrator:
    """Main orchestrator for JARVIS hybrid architecture.
    
    This class coordinates between local Mac and GCP backends, providing:
    - Intelligent request routing based on context and capabilities
    - Integration with UAE/SAI/CAI intelligence systems
    - Automatic failover and load balancing
    - Health monitoring and performance analytics
    - Cost optimization through idle time tracking
    - Model lifecycle management and selection
    
    Attributes:
        config_path: Path to hybrid configuration file
        client: HybridBackendClient for backend communication
        router: HybridRouter for intelligent routing decisions
        is_running: Whether the orchestrator is currently running
        request_count: Total number of requests processed
    """

    def __init__(self, config_path: Optional[str] = None):
        """Initialize the HybridOrchestrator.
        
        Args:
            config_path: Path to configuration file. Defaults to backend/core/hybrid_config.yaml
        """
        if config_path:
            self.config_path = config_path
        else:
            # Resolve relative to this file (backend/core/hybrid_orchestrator.py)
            # to get backend/core/hybrid_config.yaml regardless of cwd
            self.config_path = str(Path(__file__).resolve().parent / "hybrid_config.yaml")

        # Initialize components
        self.client = HybridBackendClient(self.config_path)
        self.router = HybridRouter(self.client.config)

        # State
        self.is_running = False
        self.request_count = 0

        logger.info("🎭 HybridOrchestrator initialized")

    async def start(self) -> None:
        """Start the orchestrator and all its components.
        
        Raises:
            RuntimeError: If orchestrator is already running
        """
        if self.is_running:
            logger.warning("Orchestrator already running")
            return

        logger.info("🚀 Starting HybridOrchestrator...")
        await self.client.start()

        # Start Model Lifecycle Manager (Phase 3.1+)
        lifecycle_manager = _get_lifecycle_manager()
        if lifecycle_manager:
            await lifecycle_manager.start()
            logger.info("✅ Model Lifecycle Manager started")

        # Register backend capabilities from discovered services
        self._register_backend_capabilities()

        self.is_running = True
        logger.info("✅ HybridOrchestrator started")

    def _register_backend_capabilities(self) -> None:
        """Register capabilities from discovered backend services.

        v109.2: Added missing method that was causing startup errors.
        This method registers the capabilities of local and GCP backends
        so the router can make intelligent routing decisions.
        """
        try:
            # Get capabilities from the client's discovered backends
            backend_configs = getattr(self.client, 'backend_configs', {})

            for backend_name, config in backend_configs.items():
                capabilities = config.get('capabilities', [])
                priority = config.get('priority', 50)

                # Register with the router
                if hasattr(self.router, 'register_backend'):
                    self.router.register_backend(backend_name, capabilities, priority)
                    logger.debug(f"[HybridOrchestrator] Registered {backend_name} capabilities: {capabilities}")

        except Exception as e:
            # Non-critical - orchestrator can work without explicit capability registration
            logger.debug(f"[HybridOrchestrator] Capability registration skipped: {e}")

    async def stop(self) -> None:
        """Stop the orchestrator and clean up resources.

        v255.0: Always closes resources even when not ``is_running``.
        The backend client creates an async HTTP client at construction time,
        so skipping stop() leaks sessions during early-shutdown paths.
        """
        logger.info("🛑 Stopping HybridOrchestrator...")

        # Stop lifecycle manager if it was initialized and is running.
        # Do not call _get_lifecycle_manager() here to avoid creating
        # a new singleton during shutdown.
        global _lifecycle_manager
        if _lifecycle_manager is not None:
            try:
                if getattr(_lifecycle_manager, "is_running", False):
                    await _lifecycle_manager.stop()
                    logger.info("✅ Model Lifecycle Manager stopped")
            except RuntimeError as e:
                if "Event loop is closed" in str(e):
                    logger.debug("Skipping Model Lifecycle Manager stop: event loop closed")
                else:
                    logger.warning(f"Model Lifecycle Manager stop error: {e}")
            except Exception as e:
                logger.warning(f"Model Lifecycle Manager stop error: {e}")

        # Close backend client regardless of orchestrator running state.
        try:
            await self.client.stop()
        except RuntimeError as e:
            if "Event loop is closed" in str(e):
                logger.debug("Skipping HybridBackendClient stop: event loop closed")
            else:
                logger.warning(f"HybridBackendClient stop error: {e}")
        except Exception as e:
            logger.warning(f"HybridBackendClient stop error: {e}")

        self.is_running = False
        logger.info("✅ HybridOrchestrator stopped")

    async def execute_command(
        self,
        command: str,
        command_type: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Execute command with intelligent routing and UAE/SAI/CAI integration.

        Args:
            command: The command to execute
            command_type: Type of command (query, action, etc.)
            metadata: Additional metadata for routing

        Returns:
            Dict containing:
                - success: Whether execution succeeded
                - result: Command execution result
                - routing: Routing decision metadata
                - intelligence: Context from UAE/SAI/CAI systems
                - error: Error message if execution failed

        Raises:
            Exception: If command execution fails and self-healing is unsuccessful
        """
        if not self.is_running:
            await self.start()

        self.request_count += 1

        # Create routing context
        context = RoutingContext(command=command, command_type=command_type, metadata=metadata)

        # Route the request with defensive metadata handling
        decision, backend_name, route_metadata = self.router.route(context)

        # Defensive access for route_metadata - ensure required keys exist
        # This fixes the KeyError: 'routing' issue during parallel initialization
        if route_metadata is None:
            route_metadata = {}
        rule_name = route_metadata.get("rule", "default")
        confidence = route_metadata.get("confidence", 0.0)

        rule = self._get_rule(rule_name)

        logger.info(
            f"📨 Request #{self.request_count}: '{command[:50]}...' "
            f"→ {decision.value} (rule: {rule_name}, "
            f"confidence: {confidence:.2f})"
        )

        # Enrich with intelligence systems
        intelligence_context = await self._gather_intelligence_context(command, rule)

        # Merge with existing metadata
        enhanced_metadata = {**(metadata or {}), **intelligence_context}

        # Determine capability based on decision
        capability = self._get_capability_from_decision(decision, command)

        # Execute request
        try:
            result = await self.client.execute(
                path="/api/command",
                method="POST",
                data={
                    "command": command,
                    "command_type": command_type,
                    "metadata": enhanced_metadata,
                    "intelligence_context": intelligence_context,
                },
                capability=capability,
            )

            # Add routing and intelligence metadata to response
            result["routing"] = {
                "decision": decision.value,
                "backend": backend_name,
                **route_metadata,
            }
            result["intelligence"] = intelligence_context

            # ============== PHASE 2.5: Record Backend Activity ==============
            # Record activity for GCP idle time tracking
            if backend_name:
                self.router.record_backend_activity(backend_name)
                logger.debug(f"📝 Recorded activity for '{backend_name}'")
            # ================================================================

            # Learn from execution (SAI)
            if rule and rule.get("use_sai"):
                await self._sai_learn_from_execution(command, result)

            # Store in learning database
            if rule and rule.get("use_learning_db"):
                await self._store_in_learning_db(command, result)

            return result

        except Exception as e:
            logger.error(f"Command execution failed: {e}")

            # SAI self-healing attempt
            if self._should_attempt_self_heal():
                logger.info("🔧 SAI attempting self-heal...")
                heal_result = await self._sai_self_heal(e, command)
                if heal_result.get("success"):
                    return heal_result

            return {
                "success": False,
                "error": str(e),
                "routing": {"decision": decision.value, "backend": backend_name, **route_metadata},
                "intelligence": intelligence_context,
            }

    def _get_rule(self, rule_name: str) -> Optional[Dict]:
        """Get routing rule by name.

        Args:
            rule_name: Name of the routing rule to retrieve

        Returns:
            Dict containing rule configuration or None if not found

        Note:
            This method uses defensive access to handle missing config keys
            gracefully, which can occur during parallel initialization before
            the full config is loaded.
        """
        try:
            # Defensive nested access - handle missing keys at any level
            config = getattr(self.client, 'config', {}) or {}
            hybrid_config = config.get("hybrid", {}) or {}
            routing_config = hybrid_config.get("routing", {}) or {}
            rules = routing_config.get("rules", []) or []

            for rule in rules:
                if rule.get("name") == rule_name:
                    return rule
            return None
        except (AttributeError, TypeError, KeyError) as e:
            logger.debug(f"Could not get rule '{rule_name}': {e}")
            return None

    async def _gather_intelligence_context(
        self, command: str, rule: Optional[Dict]
    ) -> Dict[str, Any]:
        """Gather context from UAE/SAI/CAI/learning_database systems.

        Supports three modes:
        - STANDARD: Original UAE/SAI/CAI without chain-of-thought
        - ENHANCED: Enhanced systems with LangGraph chain-of-thought reasoning
        - UNIFIED: Full UnifiedIntelligenceOrchestrator coordination

        Args:
            command: The command being executed
            rule: Routing rule configuration

        Returns:
            Dict containing enriched context from intelligence systems:
                - uae: Unified Awareness Engine context
                - cai: Context Awareness Intelligence predictions
                - learning_db: Historical patterns and preferences
                - llm: Local LLM availability status
                - reasoning_chain: (ENHANCED/UNIFIED) Chain-of-thought trace
                - confidence_calibration: (ENHANCED/UNIFIED) Self-assessed confidence
        """
        context = {}

        if not rule:
            return context

        # Check intelligence mode for chain-of-thought reasoning
        mode = get_intelligence_mode()

        # ============== REASONING_GRAPH MODE: Multi-Branch with Failure Recovery ==============
        if mode == IntelligenceMode.REASONING_GRAPH:
            reasoning_engine = await _get_reasoning_graph_engine()
            if reasoning_engine:
                try:
                    # Use reasoning graph engine for multi-branch decision making
                    reasoning_result = await reasoning_engine.reason(
                        query=command,
                        context={"rule": rule, "command": command, "metadata": context},
                        narrate=True  # Enable voice narration
                    )
                    context["reasoning_graph"] = {
                        "session_id": reasoning_result.get("session_id"),
                        "result": reasoning_result.get("result"),
                        "confidence": reasoning_result.get("confidence", 0.0),
                        "total_attempts": reasoning_result.get("total_attempts", 0),
                        "successful_branches": reasoning_result.get("successful_branches", 0),
                        "failed_branches": reasoning_result.get("failed_branches", 0),
                        "learning_insights": reasoning_result.get("learning_insights", []),
                        "narration_log": reasoning_result.get("narration_log", []),
                        "branch_stats": reasoning_result.get("branch_stats", {}),
                        "needs_intervention": reasoning_result.get("needs_intervention", False),
                        "intervention_reason": reasoning_result.get("intervention_reason"),
                    }
                    logger.debug(
                        f"🧠 ReasoningGraph: {reasoning_result.get('successful_branches', 0)} successful, "
                        f"{reasoning_result.get('failed_branches', 0)} failed branches, "
                        f"confidence={reasoning_result.get('confidence', 0):.2f}"
                    )
                    return context  # Reasoning graph mode handles everything
                except Exception as e:
                    logger.warning(f"ReasoningGraphEngine failed, falling back to unified: {e}")
                    mode = IntelligenceMode.UNIFIED  # Fallback

        # ============== VOICE_AUTH MODE: Voice Biometric Authentication ==============
        if mode == IntelligenceMode.VOICE_AUTH:
            # First try the reasoning graph for voice authentication
            reasoning_graph = await _get_voice_auth_reasoning_graph()
            if reasoning_graph:
                try:
                    # Get audio data from context if available
                    audio_data = context.get("audio_data")
                    user_id = context.get("user_id", "owner")

                    if audio_data:
                        # Run voice authentication reasoning graph
                        voice_result = await reasoning_graph.authenticate(
                            audio_data=audio_data,
                            user_id=user_id,
                            context={"rule": rule, "command": command},
                        )
                        context["voice_auth_reasoning"] = {
                            "decision": voice_result.get("decision"),
                            "confidence": voice_result.get("confidence", 0.0),
                            "verified": voice_result.get("verified", False),
                            "speaker_name": voice_result.get("speaker_name"),
                            "level": voice_result.get("level"),
                            "hypothesis": voice_result.get("hypothesis"),
                            "reasoning_steps": voice_result.get("reasoning_steps", []),
                            "processing_time_ms": voice_result.get("processing_time_ms", 0),
                        }
                        logger.debug(
                            f"🎤 VoiceAuthReasoning: verified={voice_result.get('verified')}, "
                            f"confidence={voice_result.get('confidence', 0):.2f}, "
                            f"level={voice_result.get('level')}"
                        )
                        return context  # Voice auth reasoning handles everything
                except Exception as e:
                    logger.warning(f"VoiceAuthReasoningGraph failed, trying orchestrator: {e}")

            # Fallback to orchestrator if reasoning graph unavailable or failed
            orchestrator = await _get_voice_auth_orchestrator()
            if orchestrator:
                try:
                    audio_data = context.get("audio_data")
                    user_id = context.get("user_id", "owner")

                    if audio_data:
                        auth_result = await orchestrator.authenticate(
                            audio_data=audio_data,
                            user_id=user_id,
                            context={"rule": rule, "command": command},
                        )
                        context["voice_auth_orchestrator"] = {
                            "decision": auth_result.decision.value if hasattr(auth_result.decision, 'value') else str(auth_result.decision),
                            "final_confidence": auth_result.final_confidence,
                            "authenticated_user": auth_result.authenticated_user,
                            "final_level": auth_result.final_level.display_name if hasattr(auth_result.final_level, 'display_name') else str(auth_result.final_level),
                            "levels_attempted": auth_result.levels_attempted,
                            "total_duration_ms": auth_result.total_duration_ms,
                            "response_text": auth_result.response_text,
                            "spoofing_suspected": auth_result.spoofing_suspected,
                        }
                        logger.debug(
                            f"🎤 VoiceAuthOrchestrator: decision={auth_result.decision}, "
                            f"confidence={auth_result.final_confidence:.2f}, "
                            f"levels_attempted={auth_result.levels_attempted}"
                        )
                        return context  # Voice auth orchestrator handles everything
                except Exception as e:
                    logger.warning(f"VoiceAuthOrchestrator failed, falling back to unified: {e}")
                    mode = IntelligenceMode.UNIFIED  # Fallback

        # ============== UNIFIED MODE: Full Orchestrated Intelligence ==============
        if mode == IntelligenceMode.UNIFIED and rule.get("use_uae"):
            orchestrator = await _get_unified_orchestrator()
            if orchestrator:
                try:
                    # Use unified orchestrator for coordinated intelligence
                    unified_result = await orchestrator.process_with_reasoning(
                        query=command,
                        context={"rule": rule, "command": command}
                    )
                    context["unified_intelligence"] = {
                        "decision": unified_result.get("decision"),
                        "confidence": unified_result.get("confidence", 0.0),
                        "reasoning_chain": unified_result.get("reasoning_chain", []),
                        "uae_contribution": unified_result.get("uae_analysis"),
                        "sai_contribution": unified_result.get("sai_analysis"),
                        "cai_contribution": unified_result.get("cai_analysis"),
                        "fusion_reasoning": unified_result.get("fusion_reasoning"),
                        "self_reflection": unified_result.get("reflection"),
                    }
                    logger.debug(f"🧠 Unified Intelligence gathered with {len(unified_result.get('reasoning_chain', []))} reasoning steps")
                    return context  # Unified mode handles everything
                except Exception as e:
                    logger.warning(f"Unified Intelligence failed, falling back to enhanced: {e}")
                    mode = IntelligenceMode.ENHANCED  # Fallback

        # ============== ENHANCED MODE: Chain-of-Thought Reasoning ==============
        if mode == IntelligenceMode.ENHANCED:
            # Enhanced UAE with chain-of-thought
            if rule.get("use_uae"):
                enhanced_uae = _get_enhanced_uae()
                if enhanced_uae:
                    try:
                        uae_result = await enhanced_uae.make_reasoned_decision(
                            element_id=command,
                            context={"command": command}
                        )
                        context["uae"] = {
                            "decision": uae_result.chosen_position if hasattr(uae_result, 'chosen_position') else None,
                            "confidence": uae_result.confidence if hasattr(uae_result, 'confidence') else 0.0,
                            "reasoning_chain": uae_result.reasoning_chain if hasattr(uae_result, 'reasoning_chain') else [],
                            "thought_process": uae_result.thought_process if hasattr(uae_result, 'thought_process') else [],
                            "self_reflection": uae_result.self_reflection if hasattr(uae_result, 'self_reflection') else None,
                        }
                        logger.debug(f"🧠 Enhanced UAE context with chain-of-thought gathered")
                    except Exception as e:
                        logger.warning(f"Enhanced UAE failed: {e}")

            # Enhanced CAI with chain-of-thought
            if rule.get("use_cai"):
                enhanced_cai = _get_enhanced_cai()
                if enhanced_cai:
                    try:
                        cai_result = await enhanced_cai.analyze_user_state_with_reasoning(
                            workspace_state={"command": command},
                            activity_data={"text": command, "command": command},
                        )
                        context["cai"] = {
                            "emotional_state": cai_result.get("emotional_state"),
                            "cognitive_state": cai_result.get("cognitive_load"),
                            "confidence": cai_result.get("confidence", 0.0),
                            "reasoning_chain": cai_result.get("reasoning_trace", ""),
                            "personality_adaptation": cai_result.get("personality_adjustments"),
                            "predicted_intent": None,
                        }
                        logger.debug(f"🎯 Enhanced CAI with chain-of-thought gathered")
                    except Exception as e:
                        logger.warning(f"Enhanced CAI failed: {e}")

            # Enhanced SAI with chain-of-thought
            if rule.get("use_sai"):
                enhanced_sai = _get_enhanced_sai()
                if enhanced_sai:
                    try:
                        sai_result = await enhanced_sai.analyze_environment_with_reasoning(
                            current_snapshot={"command": command},
                            previous_snapshot=None,
                            detected_changes=[],
                        )
                        context["sai"] = {
                            "environment_state": {"command": command},
                            "detected_changes": sai_result.get("affected_elements", []),
                            "impact_assessment": {
                                "stability_score": sai_result.get("stability_score", 0.0),
                                "change_significance": sai_result.get("change_significance", 0.0),
                                "recommended_actions": sai_result.get("recommended_actions", []),
                            },
                            "confidence": sai_result.get("confidence", 0.0),
                            "reasoning_chain": sai_result.get("reasoning_trace", ""),
                            "predictions": sai_result.get("predictions", []),
                        }
                        logger.debug(f"🔍 Enhanced SAI with chain-of-thought gathered")
                    except Exception as e:
                        logger.warning(f"Enhanced SAI failed: {e}")

        # ============== STANDARD MODE: Original Systems ==============
        else:
            # UAE: Unified Awareness Engine
            if rule.get("use_uae"):
                uae = _get_uae()
                if uae:
                    try:
                        uae_context = await safe_to_thread(uae.get_current_context)
                        context["uae"] = {
                            "screen_state": uae_context.get("screen_locked", False),
                            "active_apps": uae_context.get("active_apps", []),
                            "current_space": uae_context.get("current_space"),
                            "network_status": uae_context.get("network_connected", True),
                        }
                        logger.debug(f"🧠 UAE context gathered")
                    except Exception as e:
                        logger.warning(f"UAE context failed: {e}")

            # CAI: Context Awareness Intelligence
            if rule.get("use_cai"):
                cai = _get_cai()
                if cai:
                    try:
                        intent = await safe_to_thread(cai.predict_intent, command)
                        context["cai"] = {
                            "predicted_intent": intent.get("intent"),
                            "confidence": intent.get("confidence", 0.0),
                            "suggested_action": intent.get("suggestion"),
                        }
                        logger.debug(f"🎯 CAI intent: {intent.get('intent')}")
                    except Exception as e:
                        logger.warning(f"CAI prediction failed: {e}")

        # learning_database: Historical patterns
        if rule.get("use_learning_db"):
            learning_db = await _get_learning_db()
            if learning_db:
                try:
                    similar_patterns = await learning_db.find_similar_patterns(command)
                    context["learning_db"] = {
                        "similar_commands": [p.get("command") for p in similar_patterns[:3]],
                        "success_rate": (
                            sum(p.get("success", 0) for p in similar_patterns)
                            / len(similar_patterns)
                            if similar_patterns
                            else 0.0
                        ),
                        "learned_preferences": (
                            similar_patterns[0].get("metadata") if similar_patterns else {}
                        ),
                    }
                    logger.debug(f"📚 learning_db: Found {len(similar_patterns)} similar patterns")
                except Exception as e:
                    logger.warning(f"learning_db query failed: {e}")

        # ============== PHASE 3.1: Local LLM Integration ==============
        # LLM: Local Language Model (LLaMA 3.1 70B on GCP)
        if rule.get("use_llm"):
            llm = _get_llm()
            if llm:
                try:
                    # Check if LLM is available and started
                    if not llm.is_running:
                        await llm.start()

                    # Get LLM status for routing decisions
                    llm_status = llm.get_status()
                    context["llm"] = {
                        "available": llm_status["model_state"] == "loaded",
                        "model_name": llm_status.get("model_name"),
                        "health": llm_status.get("health", {}),
                        "avg_inference_time": llm_status["health"].get("avg_inference_time", 0),
                    }
                    logger.debug(
                        f"🤖 LLM context: {llm_status['model_state']} "
                        f"({llm_status['health'].get('success_rate', 0):.1%} success rate)"
                    )
                except Exception as e:
                    logger.warning(f"LLM context failed: {e}")
        # ===============================================================

        return context

    async def _sai_learn_from_execution(self, command: str, result: Dict) -> None:
        """Allow SAI to learn from command execution results.
        
        Args:
            command: The executed command
            result: Execution result containing success status and metadata
        """
        sai = _get_sai()
        if sai:
            try:
                await safe_to_thread(
                    sai.learn_from_execution,
                    command=command,
                    success=result.get("success", False),
                    response_time=result.get("response_time", 0),
                    metadata=result.get("routing", {}),
                )
                logger.debug("🤖 SAI learned from execution")
            except Exception as e:
                logger.warning(f"SAI learning failed: {e}")

    async def _store_in_learning_db(self, command: str, result: Dict) -> None:
        """Store execution results in learning database for future reference.
        
        Args:
            command: The executed command
            result: Execution result to store
        """
        learning_db = await _get_learning_db()
        if learning_db:
            try:
                await learning_db.store_interaction(
                    command=command, result=result, timestamp=asyncio.get_event_loop().time()
                )
                logger.debug("💾 Stored in learning_database")
            except Exception as e:
                logger.warning(f"learning_db storage failed: {e}")

    def _should_attempt_self_heal(self) -> bool:
        """Check if SAI should attempt self-healing based on configuration.
        
        Returns:
            bool: True if self-healing should be attempted
        """
        config = self.client.config.get("hybrid", {}).get("intelligence", {}).get("sai", {})
        return config.get("enabled", False) and config.get("self_healing", False)

    async def _sai_self_heal(self, error: Exception, command: str) -> Dict[str, Any]:
        """Attempt SAI self-healing from execution error.
        
        Args:
            error: The exception that occurred
            command: The command that failed
            
        Returns:
            Dict containing heal result and potentially retried command result
        """
        sai = _get_sai()
        if sai:
            try:
                heal_result = await safe_to_thread(
                    sai.attempt_self_heal, error=str(error), context={"command": command}
                )
                if heal_result.get("healed"):
                    logger.info(f"✅ SAI self-heal successful: {heal_result.get('action')}")
                    # Retry command after heal
                    return await self.execute_command(command)
                else:
                    logger.warning(f"⚠️  SAI self-heal unsuccessful")
            except Exception as e:
                logger.error(f"SAI self-heal failed: {e}")

        return {"success": False, "error": str(error)}

    # ============== REASONING GRAPH: Multi-Branch Reasoning ==============

    async def execute_with_multi_branch_reasoning(
        self,
        command: str,
        context: Optional[Dict[str, Any]] = None,
        narrate: bool = True,
        max_attempts: int = 20
    ) -> Dict[str, Any]:
        """Execute command using multi-branch reasoning with failure recovery.

        This method provides JARVIS's most advanced reasoning capability:
        - Generates multiple solution branches simultaneously
        - Automatically tries alternatives when approaches fail
        - Learns from failures to generate better solutions
        - Narrates the thinking process in real-time

        Example flow:
            User: "JARVIS, the build is failing"

            JARVIS: "I'm analyzing three potential causes:
                     1. Type mismatch on line 47
                     2. Missing dependency import
                     3. Environment variable not set

                     Let me work through these systematically...

                     [2 seconds later]
                     Testing solution 1... that didn't fully resolve it.

                     [3 seconds later]
                     Solution 2 revealed the root cause - combining approaches...

                     [4 seconds later]
                     Build is now passing! The issue was a combination of the type error
                     AND a missing import. I've fixed both."

        Args:
            command: Command/problem to solve
            context: Additional context information
            narrate: Whether to enable voice narration
            max_attempts: Maximum number of solution attempts

        Returns:
            Dict containing:
                - success: Whether a solution was found
                - result: The solution result
                - confidence: Confidence in the solution
                - reasoning_trace: Log of all reasoning steps
                - learning_insights: What was learned from this process
                - branches_tried: Number of approaches attempted
                - narration_log: Voice narration history
        """
        reasoning_engine = await _get_reasoning_graph_engine()

        if not reasoning_engine:
            # Fallback to standard execution
            logger.warning("ReasoningGraphEngine not available, using standard execution")
            return await self.execute_command(command, metadata=context)

        try:
            # Execute with multi-branch reasoning
            result = await reasoning_engine.reason(
                query=command,
                context=context or {},
                constraints={"max_attempts": max_attempts},
                narrate=narrate
            )

            # Format response
            return {
                "success": result.get("result", {}).get("success", False),
                "result": result.get("result"),
                "confidence": result.get("confidence", 0.0),
                "reasoning_trace": {
                    "total_attempts": result.get("total_attempts", 0),
                    "successful_branches": result.get("successful_branches", 0),
                    "failed_branches": result.get("failed_branches", 0),
                    "branch_stats": result.get("branch_stats", {}),
                },
                "learning_insights": result.get("learning_insights", []),
                "branches_tried": result.get("total_attempts", 0),
                "narration_log": result.get("narration_log", []),
                "needs_intervention": result.get("needs_intervention", False),
                "intervention_reason": result.get("intervention_reason"),
            }

        except Exception as e:
            logger.error(f"Multi-branch reasoning failed: {e}")
            return {
                "success": False,
                "error": str(e),
                "result": None,
                "confidence": 0.0,
            }

    async def get_reasoning_narration_history(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Get the history of voice narrations from reasoning.

        Args:
            limit: Maximum number of entries to return

        Returns:
            List of narration entries with timestamps and events
        """
        reasoning_engine = await _get_reasoning_graph_engine()
        if reasoning_engine:
            return reasoning_engine.get_narration_history(limit)
        return []

    def set_narration_style(self, style: str) -> None:
        """Set the voice narration style.

        Args:
            style: One of "concise", "detailed", "technical", "casual"
        """
        import asyncio
        from intelligence.reasoning_graph_engine import NarrationStyle

        style_map = {
            "concise": NarrationStyle.CONCISE,
            "detailed": NarrationStyle.DETAILED,
            "technical": NarrationStyle.TECHNICAL,
            "casual": NarrationStyle.CASUAL,
        }

        if style not in style_map:
            logger.warning(f"Unknown narration style: {style}")
            return

        async def _set_style():
            engine = await _get_reasoning_graph_engine()
            if engine:
                engine.set_narration_style(style_map[style])
                logger.info(f"🎤 Narration style set to: {style}")

        # Run async function — avoid nested event loop crash.
        try:
            loop = asyncio.get_running_loop()
            # Loop is running: schedule as a task (safe).
            loop.create_task(_set_style())
        except RuntimeError:
            # No running loop: safe to create one.
            asyncio.run(_set_style())

    # ============== PHASE 3.1: LLM Helper Methods ==============

    async def execute_with_intelligent_model_selection(
        self,
        query: str,
        intent: Optional[str] = None,
        required_capabilities: Optional[set] = None,
        context: Optional[Dict] = None,
        **kwargs,
    ) -> Dict[str, Any]:
        """Execute a query with intelligent model selection.

        The model selector will:
        1. Analyze the query (CAI integration)
        2. Consider context and focus level (UAE integration)
        3. Check RAM availability (SAI integration)
        4. Score all capable models
        5. Select the best option
        6. Load model if needed (lifecycle manager)
        7. Execute the query

        Args:
            query: User's query text
            intent: Pre-classified intent (optional)
            required_capabilities: Required capabilities (optional)
            context: Additional context from UAE/SAI/CAI
            **kwargs: Additional parameters for model

        Returns:
            Dict with result and metadata:
                - success: Whether execution succeeded
                - text: Generated response text
                - model_used: Name of the model that was used
                - fallback_used: Whether a fallback model was used
                - error: Error message if execution failed
        """
        model_selector = _get_model_selector()
        if not model_selector:
            logger.warning("Model selector not available, falling back to direct LLM")
            return await self.execute_llm_inference(query, **kwargs)

        lifecycle_manager = _get_lifecycle_manager()
        if not lifecycle_manager:
            logger.warning("Lifecycle manager not available")
            return await self.execute_llm_inference(query, **kwargs)

        try:
            # Select best model with fallback chain
            primary_model, fallbacks = await model_selector.select_with_fallback(
                query=query,
                intent=intent,
                required_capabilities=required_capabilities,
                context=context,
            )

            if not primary_model:
                logger.error("No suitable model found for query")
                return {"success": False, "error": "No suitable model found", "query": query}

            # Try primary model
            try:
                result = await self._execute_with_model(
                    primary_model, query, lifecycle_manager, **kwargs
                )
                if result["success"]:
                    result["model_used"] = primary_model.name
                    return result
            except Exception as e:
                logger.warning(f"Primary model {primary_model.name} failed: {e}")

            # Try fallbacks
            for fallback_model in fallbacks:
                try:
                    logger.info(f"Trying fallback model: {fallback_model.name}")
                    result = await self._execute_with_model(
                        fallback_model, query, lifecycle_manager, **kwargs
                    )
                    if result["success"]:
                        result["model_used"] = fallback_model.name
                        result["fallback_used"] = True
                        return result
                except Exception as e:
                    logger.warning(f"Fallback model {fallback_model.name} failed: {e}")
                    continue

            return {"success": False, "error": "All models failed", "query": query}

        except Exception as e:
            logger.error(f"Error in intelligent model selection: {e}")
            return {"success": False, "error": str(e), "query": query}

    async def _execute_with_model(
        self, model_def, query: str, lifecycle_manager, **kwargs
    ) -> Dict[str, Any]:
        """Execute query with a specific model and unified RAG context injection.
        
        PHASE 1 UPDATE: Now injects RAG context from UnifiedRAGContextManager
        before executing any LLM, connecting all models to Trinity web knowledge
        and local knowledge base.
        
        Args:
            model_def: Model definition containing name, type, and capabilities
            query: Query to execute
            lifecycle_manager: Model lifecycle manager instance
            **kwargs: Additional parameters for model execution
            
        Returns:
            Dict containing execution result with RAG metadata
        """
        # Load model if needed
        model_instance = await lifecycle_manager.get_model(
            model_def.name, required_by="orchestrator"
        )

        if not model_instance:
            return {"success": False, "error": f"Failed to load {model_def.name}"}

        # =================================================================
        # PHASE 1 + 2: Inject RAG Context with Resilience Protection
        # =================================================================
        rag_context = None
        enhanced_query = query
        was_degraded = False
        
        if model_def.model_type == "llm":
            try:
                from engines.rag_engine import (
                    get_unified_rag_context,
                    CorrelationContext,
                    execute_with_resilience,
                )
                
                # Create correlation context for distributed tracing
                correlation = CorrelationContext.create()
                correlation.baggage["model"] = model_def.name
                correlation.baggage["query_id"] = kwargs.get("query_id", correlation.span_id)
                
                # Define RAG retrieval function
                async def retrieve_rag():
                    return await get_unified_rag_context(
                        query=query,
                        correlation_context=correlation,
                        include_web_sources=kwargs.get("include_web_sources", True),
                        conversation_history=kwargs.get("conversation_history"),
                    )
                
                # Define fallback for graceful degradation
                async def fallback_empty():
                    logger.warning("[RAG] Using fallback: empty context")
                    return {"context_text": "", "sources": [], "web_sources": []}
                
                # Execute with PHASE 2 resilience protection
                try:
                    rag_context, was_degraded = await execute_with_resilience(
                        source="rag_trinity",
                        func=retrieve_rag,
                        priority=2,  # High priority for RAG
                        fallback=fallback_empty,
                        dedupe_key=f"rag:{hashlib.md5(query.encode()).hexdigest()[:16]}",
                    )
                except Exception as resilience_err:
                    logger.warning(f"[RAG] Resilience layer error: {resilience_err}")
                    # Fallback to direct call
                    rag_context = await retrieve_rag()
                
                # Build enhanced prompt with RAG context
                if rag_context.get("context_text"):
                    enhanced_query = self._build_rag_enhanced_prompt(query, rag_context)
                    logger.debug(
                        f"[RAG] Injected {rag_context.get('source_count', 0)} sources "
                        f"for {model_def.name} (correlation={correlation.short_id})"
                    )
                    
            except ImportError as e:
                logger.warning(f"[RAG] UnifiedRAGContextManager not available: {e}")
            except Exception as e:
                logger.warning(f"[RAG] Context retrieval failed, proceeding without RAG: {e}")

        # Execute based on model type
        if model_def.model_type == "llm":
            # LLM inference with RAG-enhanced query
            if model_def.name == "llama_70b":
                result = await self.execute_llm_inference(enhanced_query, **kwargs)
            elif model_def.name == "claude_api":
                # TODO: Add Claude API execution
                result = {
                    "success": True,
                    "text": f"[Claude API would process: {query}]",
                    "model": "claude_api",
                }
            else:
                result = {"success": False, "error": f"Unknown LLM: {model_def.name}"}

        elif model_def.model_type == "vision":
            # Vision model execution
            # Check if this is a YOLO model
            if model_def.name.startswith("yolov8"):
                try:
                    from vision.yolo_vision_detector import get_yolo_detector

                    yolo_detector = get_yolo_detector()

                    # Extract image from query if it's multimodal content
                    image_data = kwargs.get("image_data")
                    if not image_data and isinstance(query, list):
                        # Extract image from multimodal content
                        for content in query:
                            if isinstance(content, dict) and content.get("type") == "image":
                                image_data = content.get("source", {}).get("data")
                                break

                    if not image_data:
                        return {
                            "success": False,
                            "error": "No image data provided for YOLO detection",
                        }

                    # Decode base64 image if needed
                    if isinstance(image_data, str):
                        import base64
                        from io import BytesIO

                        from PIL import Image

                        image_bytes = base64.b64decode(image_data)
                        image = Image.open(BytesIO(image_bytes))
                    else:
                        image = image_data

                    # Perform detection
                    detection_result = await yolo_detector.detect_ui_elements(image)

                    # Format result
                    detections_list = [
                        {
                            "class": det.class_name,
                            "confidence": det.confidence,
                            "bbox": {
                                "x": det.bbox.x,
                                "y": det.bbox.y,
                                "width": det.bbox.width,
                                "height": det.bbox.height,
                            },
                        }
                        for det in detection_result.detections
                    ]

                    result = {
                        "success": True,
                        "text": f"Detected {len(detections_list)} objects",
                        "detections": detections_list,
                        "model": model_def.name,
                    }

                except Exception as e:
                    logger.error(f"YOLO detection failed: {e}")
                    result = {"success": False, "error": f"YOLO detection failed: {e}"}
            else:
                # Other vision models (Claude Vision)
                result = {
                    "success": True,
                    "text": f"[Vision model {model_def.name} would process the query]",
                    "model": model_def.name,
                }

        elif model_def.model_type == "embedding":
            # Semantic search
            result = {
                "success": True,
                "text": f"[Semantic search would process: {query}]",
                "model": model_def.name,
            }

        else:
            result = {"success": False, "error": f"Unknown model type: {model_def.model_type}"}

        # =================================================================
        # PHASE 1: Add RAG metadata to result for source citation
        # =================================================================
        if rag_context and result.get("success"):
            result["rag_sources"] = rag_context.get("sources", [])
            result["web_sources"] = rag_context.get("web_sources", [])
            result["rag_correlation_id"] = rag_context.get("correlation_id")
            result["rag_cache_hit"] = rag_context.get("cache_hit", False)

        return result
    
    def _build_rag_enhanced_prompt(
        self,
        query: str,
        rag_context: Dict[str, Any],
    ) -> str:
        """
        Build an enhanced prompt with RAG context for LLM.
        
        Injects relevant context from Trinity web scrape and local KB
        into the prompt before sending to the LLM.
        
        Format:
            [CONTEXT START]
            === Recent Web Knowledge ===
            [Web Source: url] content
            
            === Local Knowledge Base ===
            content
            [CONTEXT END]
            
            User Query: {query}
        
        Args:
            query: Original user query
            rag_context: Context from UnifiedRAGContextManager
            
        Returns:
            Enhanced prompt string with injected context
        """
        context_text = rag_context.get("context_text", "").strip()
        
        if not context_text:
            return query
        
        # Build enhanced prompt with context
        enhanced_prompt = f"""[CONTEXT START]
Use the following context to inform your response. Cite sources when relevant.

{context_text}
[CONTEXT END]

User Query: {query}"""
        
        return enhanced_prompt

    async def execute_llm_inference(
        self,
        prompt: str,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        **kwargs,
    ) -> Dict[str, Any]:
        """Execute LLM inference with LLaMA 3.1 70B.

        Args:
            prompt: Text prompt for generation
            max_tokens: Max tokens to generate
            temperature: Sampling temperature (0.0-1.0)
            **kwargs: Additional generation parameters

        Returns:
            Dict containing:
                - success: Whether generation succeeded
                - text: Generated text response
                - model: Model name used
                - backend: Backend that processed the request
                - error: Error message if generation failed
        """
        llm = _get_llm()
        if not llm:
            return {
                "success": False,
                "error": "Local LLM not available",
                "text": "",
            }

        try:
            # Start LLM if not running
            if not llm.is_running:
                await llm.start()

            # Generate text
            generated_text = await llm.generate(
                prompt, max_tokens=max_tokens, temperature=temperature, **kwargs
            )

            return {
                "success": True,
                "text": generated_text,
                "model": "llama-3.1-70b",
                "backend": "gcp",
            }

        except Exception as e:
            logger.error(f"LLM inference failed: {e}")
            return {
                "success": False,
                "error": str(e),
                "text": "",
            }

    async def classify_intent_with_llm(self, command: str) -> Dict[str, Any]:
        """Use LLM to classify user intent.
        
        Args:
            command: User command to classify
            
        Returns:
            Dict containing:
                - success: Whether classification succeeded
                - intent: Classified intent
                - confidence: Confidence score (0.0-1.0)
                - entities: Extracted entities
                - action: Suggested action
                - error: Error message if classification failed
        """
        prompt = f"""Classify the intent of this command. Respond with JSON format:
{{"intent": "...", "confidence": 0.0-1.0, "entities": [...], "action": "..."}}

Command: "{command}"

Classification:"""

        result = await self.execute_llm_inference(prompt, max_tokens=100, temperature=0.3)

        if result["success"]:
            try:
                import json

                # Parse JSON from response
                text = result["text"].strip()
                # Extract JSON if wrapped in other text
                if "{" in text:
                    json_start = text.index("{")
                    json_end = text.rindex("}") + 1
                    text = text[json_start:json_end]

                classification = json.loads(text)
                return {
                    "success": True,
                    "intent": classification.get("intent"),
                    "confidence": classification.get("confidence", 0.8),
                    "entities": classification.get("entities", []),
                    "action": classification.get("action"),
                }
            except Exception as e:
                logger.warning(f"Failed to parse LLM classification: {e}")
                return {"success": False, "error": str(e)}

        return result


# ============================================================================
# GLOBAL ORCHESTRATOR INSTANCE
# ============================================================================

_global_orchestrator: Optional[HybridOrchestrator] = None


def get_orchestrator() -> HybridOrchestrator:
    """Get or create the global Hybrid Orchestrator instance.
    
    This function provides a singleton pattern for accessing the hybrid
    orchestrator throughout the application. The orchestrator coordinates
    between local and cloud backends with intelligent routing.
    
    Returns:
        HybridOrchestrator: The global orchestrator instance
        
    Example:
        >>> from backend.core.hybrid_orchestrator import get_orchestrator
        >>> orchestrator = get_orchestrator()
        >>> result = await orchestrator.execute_command("open safari")
    """
    global _global_orchestrator
    if _global_orchestrator is None:
        _global_orchestrator = HybridOrchestrator()
        logger.info("✅ Global HybridOrchestrator instance created")
    return _global_orchestrator


async def get_orchestrator_async() -> HybridOrchestrator:
    """Get or create and start the global Hybrid Orchestrator instance.

    Similar to get_orchestrator() but ensures the orchestrator is started
    before returning. Useful for async contexts where you want to guarantee
    the orchestrator is ready to use.

    Returns:
        HybridOrchestrator: The global orchestrator instance (started)

    Example:
        >>> orchestrator = await get_orchestrator_async()
        >>> result = await orchestrator.execute_command("search for AI news")
    """
    orchestrator = get_orchestrator()
    if not orchestrator.is_running:
        await orchestrator.start()
    return orchestrator


async def stop_orchestrator() -> None:
    """Stop and clear the global Hybrid Orchestrator singleton."""
    global _global_orchestrator

    if _global_orchestrator is not None:
        await _global_orchestrator.stop()
        _global_orchestrator = None


# ============================================================================
# VOICE AUTHENTICATION CONVENIENCE FUNCTIONS
# ============================================================================

async def authenticate_voice(
    audio_data: bytes,
    user_id: str = "owner",
    use_reasoning: bool = True,
    context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Convenience function for voice biometric authentication.

    Provides easy access to JARVIS's voice authentication system with:
    - LangGraph reasoning for borderline cases
    - Multi-factor fallback chain
    - ChromaDB pattern memory
    - Langfuse audit trails

    Args:
        audio_data: Raw audio bytes (16kHz mono)
        user_id: User ID to authenticate against
        use_reasoning: Whether to use LangGraph reasoning for borderline cases
        context: Additional context (location, device, etc.)

    Returns:
        Dict containing:
            - verified: Whether authentication succeeded
            - confidence: Confidence score (0-1)
            - speaker_name: Identified speaker name
            - decision: Authentication decision
            - response_text: Response message
            - method: Authentication method used
            - processing_time_ms: Processing time

    Example:
        >>> result = await authenticate_voice(audio_bytes, user_id="derek")
        >>> if result["verified"]:
        ...     print(f"Welcome back, {result['speaker_name']}!")
    """
    result = {
        "verified": False,
        "confidence": 0.0,
        "speaker_name": None,
        "decision": "error",
        "response_text": "Authentication not available",
        "method": "none",
        "processing_time_ms": 0.0,
    }

    import time
    start_time = time.time()

    try:
        # Try reasoning graph first if enabled
        if use_reasoning:
            reasoning_graph = await _get_voice_auth_reasoning_graph()
            if reasoning_graph:
                try:
                    voice_result = await reasoning_graph.authenticate(
                        audio_data=audio_data,
                        user_id=user_id,
                        context=context or {},
                    )
                    result["verified"] = voice_result.get("verified", False)
                    result["confidence"] = voice_result.get("confidence", 0.0)
                    result["speaker_name"] = voice_result.get("speaker_name")
                    result["decision"] = voice_result.get("decision", "unknown")
                    result["response_text"] = voice_result.get("announcement", "")
                    result["method"] = "reasoning_graph"
                    result["reasoning_steps"] = voice_result.get("reasoning_steps", [])
                    result["processing_time_ms"] = (time.time() - start_time) * 1000
                    return result
                except Exception as e:
                    logger.warning(f"Reasoning graph failed: {e}")

        # Fall back to orchestrator
        orchestrator = await _get_voice_auth_orchestrator()
        if orchestrator:
            auth_result = await orchestrator.authenticate(
                audio_data=audio_data,
                user_id=user_id,
                context=context or {},
            )
            result["verified"] = auth_result.decision.value == "authenticated" if hasattr(auth_result.decision, 'value') else str(auth_result.decision) == "authenticated"
            result["confidence"] = auth_result.final_confidence
            result["speaker_name"] = auth_result.authenticated_user
            result["decision"] = auth_result.decision.value if hasattr(auth_result.decision, 'value') else str(auth_result.decision)
            result["response_text"] = auth_result.response_text
            result["method"] = auth_result.final_level.display_name if hasattr(auth_result.final_level, 'display_name') else str(auth_result.final_level)
            result["levels_attempted"] = auth_result.levels_attempted

    except Exception as e:
        logger.error(f"Voice authentication error: {e}")
        result["response_text"] = f"Authentication error: {str(e)}"

    result["processing_time_ms"] = (time.time() - start_time) * 1000
    return result


async def get_voice_auth_status() -> Dict[str, Any]:
    """Get voice authentication system status.

    Returns:
        Dict containing availability and configuration of voice auth components
    """
    status = {
        "reasoning_graph_available": False,
        "orchestrator_available": False,
        "vbi_available": False,
    }

    try:
        reasoning_graph = await _get_voice_auth_reasoning_graph()
        status["reasoning_graph_available"] = reasoning_graph is not None
    except Exception:
        pass

    try:
        orchestrator = await _get_voice_auth_orchestrator()
        status["orchestrator_available"] = orchestrator is not None
    except Exception:
        pass

    try:
        from backend.voice_unlock.voice_biometric_intelligence import (
            get_voice_biometric_intelligence,
        )
        vbi = await get_voice_biometric_intelligence()
        status["vbi_available"] = vbi is not None and vbi._initialized
        if vbi:
            status["vbi_stats"] = vbi.get_stats().get("enhanced_modules", {})
    except Exception:
        pass

    return status
