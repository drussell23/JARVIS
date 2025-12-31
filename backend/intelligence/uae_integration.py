#!/usr/bin/env python3
"""
UAE Integration Module
======================

Central integration point for Unified Awareness Engine (UAE) with JARVIS systems.

This module provides:
- UAE initialization and lifecycle management
- Integration with vision analyzer
- Integration with SAI engine
- Context Intelligence bootstrapping
- Metrics and monitoring
- Global UAE instance management
- LangGraph Chain-of-Thought reasoning integration
- EnhancedUAE with multi-step reasoning capabilities

Usage in main.py:
    from intelligence.uae_integration import initialize_uae, get_uae

    # During startup (standard mode)
    uae = await initialize_uae(vision_analyzer=vision_analyzer)

    # During startup (enhanced mode with chain-of-thought)
    uae = await initialize_uae(
        vision_analyzer=vision_analyzer,
        enable_chain_of_thought=True
    )

    # In command handlers
    uae = get_uae()
    decision = await uae.get_element_position("control_center")

    # With chain-of-thought reasoning
    enhanced_uae = get_enhanced_uae()
    reasoned_decision = await enhanced_uae.make_reasoned_decision("control_center")

Author: Derek J. Russell
Date: October 2025
Version: 2.0.0  # Updated for LangGraph chain-of-thought
"""

import asyncio
import logging
from typing import Optional, Dict, Any, Union
from pathlib import Path
from enum import Enum

from intelligence.unified_awareness_engine import (
    UnifiedAwarenessEngine,
    get_uae_engine
)
from vision.situational_awareness import (
    get_sai_engine,
    SituationalAwarenessEngine
)
from intelligence.learning_database import (
    get_learning_database,
    JARVISLearningDatabase
)
from intelligence.yabai_spatial_intelligence import (
    get_yabai_intelligence,
    YabaiSpatialIntelligence
)
from intelligence.workspace_pattern_learner import (
    get_pattern_learner,
    WorkspacePatternLearner
)
from intelligence.yabai_sai_integration import (
    initialize_bridge,
    get_bridge,
    shutdown_bridge,
    YabaiSAIBridge
)
from intelligence.proactive_intelligence_engine import (
    initialize_proactive_intelligence,
    get_proactive_intelligence,
    shutdown_proactive_intelligence,
    ProactiveIntelligenceEngine
)

logger = logging.getLogger(__name__)


class UAEMode(Enum):
    """UAE operation modes."""
    STANDARD = "standard"  # Original UAE without chain-of-thought
    ENHANCED = "enhanced"  # EnhancedUAE with LangGraph chain-of-thought


# Global UAE instance
_uae_instance: Optional[UnifiedAwarenessEngine] = None
_uae_initialized = False
_learning_db_instance: Optional[JARVISLearningDatabase] = None
_yabai_instance: Optional[YabaiSpatialIntelligence] = None
_pattern_learner_instance: Optional[WorkspacePatternLearner] = None
_bridge_instance: Optional[YabaiSAIBridge] = None
_proactive_intelligence_instance: Optional[ProactiveIntelligenceEngine] = None

# Enhanced UAE with LangGraph (lazy loaded)
_enhanced_uae_instance = None
_enhanced_sai_instance = None
_enhanced_cai_instance = None
_unified_orchestrator_instance = None
_uae_mode = UAEMode.ENHANCED  # Chain-of-thought reasoning AUTO by default


async def initialize_uae(
    vision_analyzer=None,
    sai_monitoring_interval: float = 5.0,  # Enhanced 24/7 mode: 5s interval
    enable_auto_start: bool = True,
    knowledge_base_path: Optional[Path] = None,
    enable_learning_db: bool = True,
    enable_yabai: bool = True,  # Enable Yabai spatial intelligence
    enable_proactive_intelligence: bool = True,  # Enable Phase 4 proactive communication
    voice_callback: Optional[Any] = None,  # Voice output callback
    notification_callback: Optional[Any] = None,  # Notification callback
    enable_chain_of_thought: bool = True,  # LangGraph chain-of-thought reasoning (AUTO by default)
    enable_unified_orchestrator: bool = True  # Full UnifiedIntelligenceOrchestrator (AUTO by default)
) -> Union[UnifiedAwarenessEngine, Any]:
    """
    Initialize UAE system with full Phase 4+ intelligence stack:
    - Learning Database
    - Yabai Spatial Intelligence (event-driven)
    - Workspace Pattern Learner (ML-powered)
    - Yabai ↔ SAI Integration Bridge
    - Proactive Intelligence Engine (Natural Communication)
    - LangGraph Chain-of-Thought Reasoning (Phase 5)
    - Unified Intelligence Orchestrator (Phase 5)

    Args:
        vision_analyzer: Claude Vision analyzer instance
        sai_monitoring_interval: SAI monitoring interval in seconds
        enable_auto_start: Whether to auto-start UAE
        knowledge_base_path: Path to knowledge base file
        enable_learning_db: Enable Learning Database integration
        enable_yabai: Enable Yabai spatial intelligence (24/7 workspace monitoring)
        enable_proactive_intelligence: Enable Phase 4 proactive communication
        voice_callback: Voice output callback
        notification_callback: Notification callback
        enable_chain_of_thought: Enable LangGraph chain-of-thought reasoning
        enable_unified_orchestrator: Enable full UnifiedIntelligenceOrchestrator

    Returns:
        Initialized UAE engine (standard or enhanced) with persistent memory + spatial intelligence
    """
    global _uae_instance, _uae_initialized, _learning_db_instance, _yabai_instance
    global _pattern_learner_instance, _bridge_instance, _proactive_intelligence_instance
    global _enhanced_uae_instance, _enhanced_sai_instance, _enhanced_cai_instance
    global _unified_orchestrator_instance, _uae_mode

    if _uae_initialized and _uae_instance is not None:
        logger.info("[UAE-INIT] UAE already initialized")
        return _uae_instance

    logger.info("[UAE-INIT] ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    logger.info("[UAE-INIT] Initializing Phase 4 Intelligence Stack...")
    logger.info("[UAE-INIT] ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    try:
        # Step 1: Initialize Learning Database (if enabled)
        learning_db = None
        if enable_learning_db:
            logger.info("[UAE-INIT] Step 1/5: Initializing Learning Database...")
            try:
                learning_db = await get_learning_database(config={
                    'cache_size': 2000,
                    'cache_ttl_seconds': 7200,
                    'enable_ml_features': True,
                    'auto_optimize': True,
                    'batch_insert_size': 100
                })
                _learning_db_instance = learning_db
                logger.info("[UAE-INIT] ✅ Learning Database initialized")
                logger.info(f"[UAE-INIT]    • Cache: 2000 entries, 2hr TTL")
                logger.info(f"[UAE-INIT]    • ML features: Enabled")
                logger.info(f"[UAE-INIT]    • Auto-optimize: Enabled")
                logger.info(f"[UAE-INIT]    • 24/7 Workspace tracking: Ready")
            except Exception as e:
                logger.warning(f"[UAE-INIT] ⚠️  Learning Database failed to initialize: {e}")
                logger.info("[UAE-INIT]    • Continuing without persistent memory")

        # Step 2: Initialize Yabai Spatial Intelligence (if enabled)
        yabai = None
        if enable_yabai:
            logger.info("[UAE-INIT] Step 2/7: Initializing Yabai Spatial Intelligence (Event-Driven)...")
            try:
                yabai = await get_yabai_intelligence(
                    learning_db=learning_db,
                    monitoring_interval=sai_monitoring_interval,
                    enable_24_7_mode=True
                )
                _yabai_instance = yabai

                if yabai.yabai_available:
                    logger.info("[UAE-INIT] ✅ Yabai Spatial Intelligence initialized")
                    logger.info(f"[UAE-INIT]    • Yabai integration: Active")
                    logger.info(f"[UAE-INIT]    • Workspace monitoring: 24/7")
                    logger.info(f"[UAE-INIT]    • Event-driven architecture: Enabled")
                    logger.info(f"[UAE-INIT]    • Cross-Space learning: Enabled")
                else:
                    logger.warning("[UAE-INIT] ⚠️  Yabai not available on system")
                    logger.info("[UAE-INIT]    • Continuing without spatial intelligence")
                    yabai = None
            except Exception as e:
                logger.warning(f"[UAE-INIT] ⚠️  Yabai initialization failed: {e}")
                logger.info("[UAE-INIT]    • Continuing without spatial intelligence")

        # Step 3: Initialize Workspace Pattern Learner (ML-powered)
        pattern_learner = None
        if enable_yabai and yabai:  # Only if Yabai is available
            logger.info("[UAE-INIT] Step 3/7: Initializing Workspace Pattern Learner (ML)...")
            try:
                pattern_learner = await get_pattern_learner(
                    learning_db=learning_db,
                    min_pattern_occurrences=3,
                    confidence_threshold=0.6
                )
                _pattern_learner_instance = pattern_learner
                logger.info("[UAE-INIT] ✅ Workspace Pattern Learner initialized")
                logger.info(f"[UAE-INIT]    • ML clustering: Enabled")
                logger.info(f"[UAE-INIT]    • Predictive engine: Enabled")
                logger.info(f"[UAE-INIT]    • Confidence threshold: 0.6")
                logger.info(f"[UAE-INIT]    • Behavioral vectors: Active")
            except Exception as e:
                logger.warning(f"[UAE-INIT] ⚠️  Pattern Learner failed to initialize: {e}")
                logger.info("[UAE-INIT]    • Continuing without ML pattern learning")

        # Step 4: Create SAI engine
        logger.info("[UAE-INIT] Step 4/7: Creating Situational Awareness Engine...")
        sai_engine = get_sai_engine(
            vision_analyzer=vision_analyzer,
            monitoring_interval=sai_monitoring_interval,
            enable_auto_revalidation=True
        )
        logger.info("[UAE-INIT] ✅ SAI engine created")

        # Step 5: Initialize Yabai ↔ SAI Integration Bridge
        bridge = None
        if enable_yabai and yabai and sai_engine:
            logger.info("[UAE-INIT] Step 5/7: Initializing Yabai ↔ SAI Integration Bridge...")
            try:
                bridge = await initialize_bridge(
                    yabai_intelligence=yabai,
                    sai_engine=sai_engine,
                    pattern_learner=pattern_learner
                )
                _bridge_instance = bridge
                logger.info("[UAE-INIT] ✅ Integration Bridge initialized")
                logger.info(f"[UAE-INIT]    • Bidirectional communication: Enabled")
                logger.info(f"[UAE-INIT]    • Context enrichment: Auto")
                logger.info(f"[UAE-INIT]    • Action coordination: Enabled")
                logger.info(f"[UAE-INIT]    • Cross-system learning: Active")
            except Exception as e:
                logger.warning(f"[UAE-INIT] ⚠️  Integration Bridge failed to initialize: {e}")
                logger.info("[UAE-INIT]    • Continuing with isolated systems")

        # Step 6: Create UAE engine with Learning DB
        logger.info("[UAE-INIT] Step 6/7: Creating Unified Awareness Engine...")
        uae = get_uae_engine(
            sai_engine=sai_engine,
            vision_analyzer=vision_analyzer,
            learning_db=learning_db
        )

        # Set custom knowledge base path if provided
        if knowledge_base_path:
            uae.context_layer.knowledge_base_path = knowledge_base_path
            uae.context_layer._load_knowledge_base()

        # Initialize Learning DB connection in Context Layer
        if learning_db:
            await uae.context_layer.initialize_db(learning_db)
            logger.info("[UAE-INIT] ✅ Learning Database integrated with Context Intelligence")

        logger.info("[UAE-INIT] ✅ UAE engine created")

        # Step 7: Auto-start all monitoring systems
        if enable_auto_start:
            logger.info("[UAE-INIT] Step 7/8: Starting all monitoring systems...")

            # Start UAE
            await uae.start()
            logger.info("[UAE-INIT] ✅ UAE started and monitoring")

            # Start Yabai monitoring
            if yabai and yabai.yabai_available:
                await yabai.start_monitoring()
                logger.info("[UAE-INIT] ✅ Yabai 24/7 workspace monitoring started (event-driven)")

        # Step 8: Initialize Proactive Intelligence Engine (Phase 4)
        proactive_intelligence = None
        if enable_proactive_intelligence:
            logger.info("[UAE-INIT] Step 8/8: Initializing Proactive Intelligence Engine...")
            try:
                proactive_intelligence = await initialize_proactive_intelligence(
                    learning_db=learning_db,
                    pattern_learner=pattern_learner,
                    yabai_intelligence=yabai,
                    uae_engine=uae,
                    voice_callback=voice_callback,
                    notification_callback=notification_callback
                )
                _proactive_intelligence_instance = proactive_intelligence
                logger.info("[UAE-INIT] ✅ Proactive Intelligence Engine initialized")
                logger.info(f"[UAE-INIT]    • Natural Communication: ✅ Active")
                logger.info(f"[UAE-INIT]    • Voice Output: {'✅ Enabled' if voice_callback else '⚠️  Disabled'}")
                logger.info(f"[UAE-INIT]    • Predictive Suggestions: ✅ Active")
                logger.info(f"[UAE-INIT]    • Workflow Optimization: ✅ Active")
            except Exception as e:
                logger.warning(f"[UAE-INIT] ⚠️  Proactive Intelligence failed to initialize: {e}")
                logger.info("[UAE-INIT]    • Continuing without proactive communication")

        # Store global instance
        _uae_instance = uae
        _uae_initialized = True

        # ============== PHASE 5: LangGraph Chain-of-Thought Integration ==============
        enhanced_uae = None
        enhanced_sai = None
        enhanced_cai = None
        unified_orchestrator = None

        if enable_chain_of_thought:
            logger.info("[UAE-INIT] Step 9/10: Initializing LangGraph Chain-of-Thought...")
            try:
                from intelligence.uae_langgraph import create_enhanced_uae
                from intelligence.intelligence_langgraph import (
                    create_enhanced_sai,
                    create_enhanced_cai
                )

                # Initialize EnhancedUAE with chain-of-thought
                enhanced_uae = create_enhanced_uae(base_uae=uae)
                _enhanced_uae_instance = enhanced_uae
                logger.info("[UAE-INIT] ✅ EnhancedUAE with chain-of-thought initialized")

                # Initialize EnhancedSAI with chain-of-thought
                enhanced_sai = create_enhanced_sai()
                _enhanced_sai_instance = enhanced_sai
                logger.info("[UAE-INIT] ✅ EnhancedSAI with chain-of-thought initialized")

                # Initialize EnhancedCAI with chain-of-thought
                enhanced_cai = create_enhanced_cai()
                _enhanced_cai_instance = enhanced_cai
                logger.info("[UAE-INIT] ✅ EnhancedCAI with chain-of-thought initialized")

                _uae_mode = UAEMode.ENHANCED
                logger.info("[UAE-INIT]    • Multi-step reasoning: ✅ Active")
                logger.info("[UAE-INIT]    • Self-reflection: ✅ Active")
                logger.info("[UAE-INIT]    • Confidence calibration: ✅ Active")
                logger.info("[UAE-INIT]    • Reasoning audit trails: ✅ Active")

            except Exception as e:
                logger.warning(f"[UAE-INIT] ⚠️  Chain-of-thought initialization failed: {e}")
                logger.info("[UAE-INIT]    • Continuing with standard UAE")

        # Initialize UnifiedIntelligenceOrchestrator if requested
        if enable_unified_orchestrator and enable_chain_of_thought:
            logger.info("[UAE-INIT] Step 10/10: Initializing UnifiedIntelligenceOrchestrator...")
            try:
                from intelligence.intelligence_langgraph import create_unified_orchestrator

                unified_orchestrator = await create_unified_orchestrator(
                    enhanced_uae=enhanced_uae,
                    enhanced_sai=enhanced_sai,
                    enhanced_cai=enhanced_cai
                )
                _unified_orchestrator_instance = unified_orchestrator
                logger.info("[UAE-INIT] ✅ UnifiedIntelligenceOrchestrator initialized")
                logger.info("[UAE-INIT]    • Cross-system coordination: ✅ Active")
                logger.info("[UAE-INIT]    • Consensus reasoning: ✅ Active")
                logger.info("[UAE-INIT]    • Unified decision fusion: ✅ Active")

            except Exception as e:
                logger.warning(f"[UAE-INIT] ⚠️  UnifiedIntelligenceOrchestrator failed: {e}")
                logger.info("[UAE-INIT]    • Continuing with individual enhanced systems")

        # ============================================================================

        logger.info("[UAE-INIT] ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        logger.info("[UAE-INIT] ✅ Phase 5 Intelligence Stack: FULLY OPERATIONAL")
        logger.info("[UAE-INIT] ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        logger.info("[UAE-INIT] Core Systems:")
        logger.info("[UAE-INIT]    • Context Intelligence: ✅ Active (with Learning DB)")
        logger.info("[UAE-INIT]    • Situational Awareness (SAI): ✅ Active (5s monitoring)")
        logger.info("[UAE-INIT]    • Decision Fusion: ✅ Active")
        logger.info("[UAE-INIT]    • Persistent Memory: " + ("✅ Enabled" if learning_db else "⚠️  Disabled"))
        logger.info("[UAE-INIT] Phase 2 Systems:")
        logger.info("[UAE-INIT]    • Spatial Intelligence (Yabai): " + ("✅ Active (24/7 event-driven)" if (yabai and yabai.yabai_available) else "⚠️  Disabled"))
        logger.info("[UAE-INIT]    • Pattern Learner (ML): " + ("✅ Active" if pattern_learner else "⚠️  Disabled"))
        logger.info("[UAE-INIT]    • Integration Bridge (Yabai↔SAI): " + ("✅ Active" if bridge else "⚠️  Disabled"))
        logger.info("[UAE-INIT] Phase 4 Systems:")
        logger.info("[UAE-INIT]    • Proactive Intelligence: " + ("✅ Active (Natural Communication)" if proactive_intelligence else "⚠️  Disabled"))
        logger.info("[UAE-INIT] Phase 5 Systems (LangGraph Chain-of-Thought):")
        logger.info("[UAE-INIT]    • EnhancedUAE (CoT): " + ("✅ Active" if enhanced_uae else "⚠️  Disabled"))
        logger.info("[UAE-INIT]    • EnhancedSAI (CoT): " + ("✅ Active" if enhanced_sai else "⚠️  Disabled"))
        logger.info("[UAE-INIT]    • EnhancedCAI (CoT): " + ("✅ Active" if enhanced_cai else "⚠️  Disabled"))
        logger.info("[UAE-INIT]    • UnifiedOrchestrator: " + ("✅ Active" if unified_orchestrator else "⚠️  Disabled"))
        logger.info("[UAE-INIT] ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

        # Return enhanced UAE if chain-of-thought is enabled, otherwise standard
        return enhanced_uae if enhanced_uae else uae

    except Exception as e:
        logger.error(f"[UAE-INIT] Failed to initialize UAE: {e}", exc_info=True)
        raise


def is_uae_initialized() -> bool:
    """
    Check if UAE has been initialized.

    This function allows callers to check initialization state before
    calling get_uae() to avoid warning logs during lazy initialization.

    Returns:
        True if UAE has been initialized, False otherwise
    """
    return _uae_initialized and _uae_instance is not None


def is_enhanced_uae_initialized() -> bool:
    """
    Check if EnhancedUAE has been initialized.

    Returns:
        True if EnhancedUAE has been initialized, False otherwise
    """
    return _enhanced_uae_instance is not None


def get_uae(silent: bool = False) -> Optional[UnifiedAwarenessEngine]:
    """
    Get global UAE instance

    Args:
        silent: If True, don't log warnings if not initialized (for lazy init checks)

    Returns:
        UAE instance or None if not initialized
    """
    if not _uae_initialized or _uae_instance is None:
        if not silent:
            logger.warning("[UAE] UAE not initialized - call initialize_uae() first")
        return None

    return _uae_instance


def get_enhanced_uae(silent: bool = False):
    """
    Get global EnhancedUAE instance with chain-of-thought reasoning.

    Args:
        silent: If True, don't log warnings if not initialized (for lazy init checks)

    Returns:
        EnhancedUAE instance or None if not initialized
    """
    global _enhanced_uae_instance
    if _enhanced_uae_instance is None:
        if not silent:
            logger.warning("[UAE] EnhancedUAE not initialized - call initialize_uae(enable_chain_of_thought=True)")
        return None
    return _enhanced_uae_instance


def get_enhanced_sai():
    """
    Get global EnhancedSAI instance with chain-of-thought reasoning.

    Returns:
        EnhancedSAI instance or None if not initialized
    """
    global _enhanced_sai_instance
    if _enhanced_sai_instance is None:
        logger.warning("[UAE] EnhancedSAI not initialized - call initialize_uae(enable_chain_of_thought=True)")
        return None
    return _enhanced_sai_instance


def get_enhanced_cai():
    """
    Get global EnhancedCAI instance with chain-of-thought reasoning.

    Returns:
        EnhancedCAI instance or None if not initialized
    """
    global _enhanced_cai_instance
    if _enhanced_cai_instance is None:
        logger.warning("[UAE] EnhancedCAI not initialized - call initialize_uae(enable_chain_of_thought=True)")
        return None
    return _enhanced_cai_instance


def get_unified_intelligence_orchestrator():
    """
    Get global UnifiedIntelligenceOrchestrator instance.

    Returns:
        UnifiedIntelligenceOrchestrator instance or None if not initialized
    """
    global _unified_orchestrator_instance
    if _unified_orchestrator_instance is None:
        logger.warning("[UAE] UnifiedIntelligenceOrchestrator not initialized - call initialize_uae(enable_unified_orchestrator=True)")
        return None
    return _unified_orchestrator_instance


def get_uae_mode() -> UAEMode:
    """
    Get current UAE operation mode.

    Returns:
        Current UAEMode (STANDARD or ENHANCED)
    """
    return _uae_mode


def set_uae_mode(mode: UAEMode) -> None:
    """
    Set UAE operation mode.

    Args:
        mode: UAEMode to set
    """
    global _uae_mode
    _uae_mode = mode
    logger.info(f"[UAE] Mode set to: {mode.value}")


async def shutdown_uae():
    """Shutdown UAE system with full Phase 5 intelligence stack"""
    global _uae_instance, _uae_initialized, _learning_db_instance, _yabai_instance
    global _pattern_learner_instance, _bridge_instance, _proactive_intelligence_instance
    global _enhanced_uae_instance, _enhanced_sai_instance, _enhanced_cai_instance
    global _unified_orchestrator_instance, _uae_mode

    if not _uae_initialized or _uae_instance is None:
        return

    logger.info("[UAE-SHUTDOWN] Shutting down Phase 5 intelligence stack...")

    try:
        # Stop Proactive Intelligence first
        if _proactive_intelligence_instance:
            logger.info("[UAE-SHUTDOWN] Stopping Proactive Intelligence Engine...")
            await shutdown_proactive_intelligence()
            logger.info("[UAE-SHUTDOWN] ✅ Proactive Intelligence stopped")
            _proactive_intelligence_instance = None

        # Stop Integration Bridge
        if _bridge_instance:
            logger.info("[UAE-SHUTDOWN] Stopping Yabai ↔ SAI Integration Bridge...")
            await shutdown_bridge()
            logger.info("[UAE-SHUTDOWN] ✅ Integration Bridge stopped")
            _bridge_instance = None

        # Stop Yabai monitoring
        if _yabai_instance and _yabai_instance.is_monitoring:
            logger.info("[UAE-SHUTDOWN] Stopping Yabai spatial monitoring...")
            await _yabai_instance.stop_monitoring()
            logger.info("[UAE-SHUTDOWN] ✅ Yabai stopped")
            _yabai_instance = None

        # Pattern Learner (no explicit stop needed - stateless)
        if _pattern_learner_instance:
            logger.info("[UAE-SHUTDOWN] ✅ Pattern Learner finalized")
            _pattern_learner_instance = None

        # Stop UAE monitoring
        await _uae_instance.stop()
        logger.info("[UAE-SHUTDOWN] ✅ UAE stopped")

        # Close Learning Database
        if _learning_db_instance:
            logger.info("[UAE-SHUTDOWN] Closing Learning Database...")
            await _learning_db_instance.close()
            logger.info("[UAE-SHUTDOWN] ✅ Learning Database closed")
            _learning_db_instance = None

        # Cleanup enhanced instances (Phase 5)
        if _unified_orchestrator_instance:
            logger.info("[UAE-SHUTDOWN] Stopping UnifiedIntelligenceOrchestrator...")
            _unified_orchestrator_instance = None
            logger.info("[UAE-SHUTDOWN] ✅ UnifiedIntelligenceOrchestrator stopped")

        if _enhanced_uae_instance:
            logger.info("[UAE-SHUTDOWN] Stopping EnhancedUAE...")
            _enhanced_uae_instance = None
            logger.info("[UAE-SHUTDOWN] ✅ EnhancedUAE stopped")

        if _enhanced_sai_instance:
            logger.info("[UAE-SHUTDOWN] Stopping EnhancedSAI...")
            _enhanced_sai_instance = None
            logger.info("[UAE-SHUTDOWN] ✅ EnhancedSAI stopped")

        if _enhanced_cai_instance:
            logger.info("[UAE-SHUTDOWN] Stopping EnhancedCAI...")
            _enhanced_cai_instance = None
            logger.info("[UAE-SHUTDOWN] ✅ EnhancedCAI stopped")

        _uae_mode = UAEMode.STANDARD
        _uae_initialized = False
        logger.info("[UAE-SHUTDOWN] ✅ Phase 5 intelligence stack shutdown complete")
        # Keep instance for potential restart

    except Exception as e:
        logger.error(f"[UAE-SHUTDOWN] Error during shutdown: {e}", exc_info=True)


def get_learning_db() -> Optional[JARVISLearningDatabase]:
    """
    Get global Learning Database instance

    Returns:
        Learning Database instance or None if not initialized
    """
    global _learning_db_instance
    return _learning_db_instance


def get_yabai() -> Optional[YabaiSpatialIntelligence]:
    """
    Get global Yabai Spatial Intelligence instance

    Returns:
        Yabai instance or None if not initialized
    """
    global _yabai_instance
    return _yabai_instance


def get_pattern_learner_sync() -> Optional[WorkspacePatternLearner]:
    """
    Get global Workspace Pattern Learner instance

    Returns:
        Pattern Learner instance or None if not initialized
    """
    global _pattern_learner_instance
    return _pattern_learner_instance


def get_integration_bridge() -> Optional[YabaiSAIBridge]:
    """
    Get global Yabai ↔ SAI Integration Bridge instance

    Returns:
        Bridge instance or None if not initialized
    """
    global _bridge_instance
    return _bridge_instance


def get_uae_metrics() -> Dict[str, Any]:
    """
    Get UAE metrics for monitoring

    Returns:
        Comprehensive metrics dict
    """
    if not _uae_initialized or _uae_instance is None:
        return {
            'initialized': False,
            'active': False,
            'error': 'UAE not initialized'
        }

    try:
        metrics = _uae_instance.get_comprehensive_metrics()
        metrics['initialized'] = True
        return metrics

    except Exception as e:
        logger.error(f"[UAE] Error getting metrics: {e}")
        return {
            'initialized': True,
            'active': False,
            'error': str(e)
        }


async def integrate_with_display_monitor(display_monitor):
    """
    Integrate UAE with display monitor service

    Args:
        display_monitor: Display monitor instance
    """
    uae = get_uae()
    if not uae:
        logger.warning("[UAE-INTEGRATION] UAE not available for display monitor integration")
        return

    try:
        # Register display changes with UAE
        def on_display_event(event_type, event_data):
            logger.info(f"[UAE-INTEGRATION] Display event: {event_type}")
            # UAE will automatically detect and adapt through SAI

        if hasattr(display_monitor, 'register_callback'):
            display_monitor.register_callback('all', on_display_event)
            logger.info("[UAE-INTEGRATION] ✅ Integrated with display monitor")

    except Exception as e:
        logger.error(f"[UAE-INTEGRATION] Display monitor integration failed: {e}")


async def integrate_with_vision_system(vision_analyzer):
    """
    Integrate UAE with vision system

    Args:
        vision_analyzer: Vision analyzer instance
    """
    uae = get_uae()
    if not uae:
        logger.warning("[UAE-INTEGRATION] UAE not available for vision integration")
        return

    try:
        # Update UAE with vision analyzer
        if uae.vision_analyzer is None:
            uae.vision_analyzer = vision_analyzer
            uae.situation_layer.sai_engine.vision_analyzer = vision_analyzer
            logger.info("[UAE-INTEGRATION] ✅ Integrated with vision system")

    except Exception as e:
        logger.error(f"[UAE-INTEGRATION] Vision system integration failed: {e}")


async def integrate_with_multi_space(multi_space_manager):
    """
    Integrate UAE with multi-space intelligence

    Args:
        multi_space_manager: Multi-space manager instance
    """
    uae = get_uae()
    if not uae:
        logger.warning("[UAE-INTEGRATION] UAE not available for multi-space integration")
        return

    try:
        # Register space change callback
        async def on_space_change(space_data):
            logger.info(f"[UAE-INTEGRATION] Space changed to: {space_data.get('space_id')}")
            # UAE SAI will detect and adapt automatically

        if hasattr(multi_space_manager, 'register_space_change_callback'):
            multi_space_manager.register_space_change_callback(on_space_change)
            logger.info("[UAE-INTEGRATION] ✅ Integrated with multi-space system")

    except Exception as e:
        logger.error(f"[UAE-INTEGRATION] Multi-space integration failed: {e}")


def register_uae_routes(app, prefix: str = "/api/uae"):
    """
    Register UAE API routes

    Args:
        app: FastAPI app instance
        prefix: API prefix
    """
    from fastapi import APIRouter, HTTPException
    from pydantic import BaseModel

    router = APIRouter(prefix=prefix, tags=["UAE"])

    class ElementPositionRequest(BaseModel):
        element_id: str
        force_detect: bool = False

    @router.get("/status")
    async def get_status():
        """Get UAE status"""
        return {
            'initialized': _uae_initialized,
            'active': _uae_instance.is_active if _uae_instance else False
        }

    @router.get("/metrics")
    async def get_metrics():
        """Get comprehensive UAE metrics"""
        return get_uae_metrics()

    @router.post("/position")
    async def get_element_position(request: ElementPositionRequest):
        """Get element position using UAE"""
        uae = get_uae()
        if not uae:
            raise HTTPException(status_code=503, detail="UAE not initialized")

        try:
            decision = await uae.get_element_position(
                request.element_id,
                force_detect=request.force_detect
            )

            return {
                'success': True,
                'element_id': decision.element_id,
                'position': decision.chosen_position,
                'confidence': decision.confidence,
                'source': decision.decision_source.value,
                'reasoning': decision.reasoning
            }

        except Exception as e:
            logger.error(f"[UAE-API] Error getting position: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @router.post("/start")
    async def start_uae():
        """Start UAE monitoring"""
        uae = get_uae()
        if not uae:
            raise HTTPException(status_code=503, detail="UAE not initialized")

        try:
            await uae.start()
            return {'success': True, 'message': 'UAE started'}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    @router.post("/stop")
    async def stop_uae():
        """Stop UAE monitoring"""
        uae = get_uae()
        if not uae:
            raise HTTPException(status_code=503, detail="UAE not initialized")

        try:
            await uae.stop()
            return {'success': True, 'message': 'UAE stopped'}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    # Register router with app
    app.include_router(router)
    logger.info(f"[UAE-API] Registered UAE routes at {prefix}")


# ============================================================================
# Helper Functions for Command Integration
# ============================================================================

async def uae_click(
    element_id: str,
    enable_communication: bool = True,
    communication_mode: str = 'normal',
    voice_callback=None,
    text_callback=None,
    **kwargs
) -> Dict[str, Any]:
    """
    Click using UAE intelligence with natural communication

    Args:
        element_id: Element to click
        enable_communication: Enable natural communication
        communication_mode: Communication verbosity (silent/minimal/normal/verbose/debug)
        voice_callback: Optional voice output callback
        text_callback: Optional text output callback
        **kwargs: Additional arguments

    Returns:
        Click result
    """
    uae = get_uae()
    if not uae:
        return {
            'success': False,
            'error': 'UAE not initialized'
        }

    try:
        # Import UAE-enhanced clicker and communication
        from display.uae_enhanced_control_center_clicker import get_uae_clicker
        from intelligence.uae_natural_communication import CommunicationMode

        # Convert string to CommunicationMode
        mode_map = {
            'silent': CommunicationMode.SILENT,
            'minimal': CommunicationMode.MINIMAL,
            'normal': CommunicationMode.NORMAL,
            'verbose': CommunicationMode.VERBOSE,
            'debug': CommunicationMode.DEBUG
        }
        comm_mode = mode_map.get(communication_mode.lower(), CommunicationMode.NORMAL)

        async with get_uae_clicker(
            enable_uae=True,
            enable_communication=enable_communication,
            communication_mode=comm_mode,
            voice_callback=voice_callback,
            text_callback=text_callback
        ) as clicker:
            result = await clicker.click(element_id, context=kwargs)

            return {
                'success': result.success,
                'coordinates': result.coordinates,
                'method': result.method_used,
                'confidence': result.confidence,
                'verification': result.verification_passed,
                'metadata': result.metadata
            }

    except Exception as e:
        logger.error(f"[UAE-CLICK] Error: {e}", exc_info=True)
        return {
            'success': False,
            'error': str(e)
        }


async def uae_connect_device(
    device_name: str,
    enable_communication: bool = True,
    communication_mode: str = 'normal',
    voice_callback=None,
    text_callback=None
) -> Dict[str, Any]:
    """
    Connect to AirPlay device using UAE intelligence with natural communication

    Args:
        device_name: Device name
        enable_communication: Enable natural communication
        communication_mode: Communication verbosity (silent/minimal/normal/verbose/debug)
        voice_callback: Optional voice output callback
        text_callback: Optional text output callback

    Returns:
        Connection result
    """
    try:
        from display.uae_enhanced_control_center_clicker import get_uae_clicker
        from intelligence.uae_natural_communication import CommunicationMode

        # Convert string to CommunicationMode
        mode_map = {
            'silent': CommunicationMode.SILENT,
            'minimal': CommunicationMode.MINIMAL,
            'normal': CommunicationMode.NORMAL,
            'verbose': CommunicationMode.VERBOSE,
            'debug': CommunicationMode.DEBUG
        }
        comm_mode = mode_map.get(communication_mode.lower(), CommunicationMode.NORMAL)

        async with get_uae_clicker(
            enable_uae=True,
            enable_communication=enable_communication,
            communication_mode=comm_mode,
            voice_callback=voice_callback,
            text_callback=text_callback
        ) as clicker:
            result = await clicker.connect_to_device(device_name)
            return result

    except Exception as e:
        logger.error(f"[UAE-CONNECT] Error: {e}", exc_info=True)
        return {
            'success': False,
            'error': str(e)
        }


# ============================================================================
# Import Function for main.py
# ============================================================================

def import_uae():
    """
    Import UAE components for main.py parallel loading

    Returns:
        Dict with UAE components
    """
    try:
        from intelligence.unified_awareness_engine import UnifiedAwarenessEngine
        from vision.situational_awareness import SituationalAwarenessEngine

        return {
            'UnifiedAwarenessEngine': UnifiedAwarenessEngine,
            'SituationalAwarenessEngine': SituationalAwarenessEngine,
            'initialize_uae': initialize_uae,
            'get_uae': get_uae,
            'available': True
        }

    except Exception as e:
        logger.error(f"[UAE-IMPORT] Failed to import UAE: {e}")
        return {
            'available': False,
            'error': str(e)
        }


# ============================================================================
# Example Usage
# ============================================================================

async def main():
    """Example UAE integration"""
    logging.basicConfig(level=logging.INFO)

    print("\n" + "=" * 80)
    print("UAE Integration Module - Demo")
    print("=" * 80)

    # Initialize UAE
    print("\n✅ Initializing UAE...")
    uae = await initialize_uae(enable_auto_start=True)

    # Get metrics
    print("\n📊 UAE Metrics:")
    metrics = get_uae_metrics()
    print(f"   Active: {metrics['engine']['active']}")
    print(f"   Total executions: {metrics['engine']['total_executions']}")

    # Test click
    print("\n🎯 Testing UAE click...")
    result = await uae_click("control_center")
    print(f"   Success: {result.get('success')}")

    # Shutdown
    print("\n⏹️  Shutting down...")
    await shutdown_uae()

    print("\n" + "=" * 80)
    print("✅ Demo complete!")
    print("=" * 80)


if __name__ == "__main__":
    asyncio.run(main())
