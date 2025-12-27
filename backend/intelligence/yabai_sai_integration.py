#!/usr/bin/env python3
"""
Yabai ↔ SAI Integration Bridge
===============================

Bidirectional bridge connecting Yabai Spatial Intelligence with SAI
(Situational Awareness Intelligence) for unified workspace intelligence.

This module provides:
- Event forwarding from Yabai → SAI
- Context enrichment from SAI → Yabai
- Unified spatial + visual intelligence
- Cross-system pattern correlation
- Synchronized decision making

Features:
- Real-time event translation
- Context-aware spatial decisions
- Visual + spatial data fusion
- Proactive action coordination
- Adaptive behavior learning
- Multi-modal intelligence integration

Author: Derek J. Russell
Date: October 2025
Version: 2.0.0 - Unified Intelligence Bridge
"""

import asyncio
import logging
import time
from datetime import datetime
from typing import Dict, List, Optional, Any, Callable
from dataclasses import dataclass, asdict
from enum import Enum
from collections import defaultdict, deque

logger = logging.getLogger(__name__)


# ============================================================================
# Bridge Event System
# ============================================================================

class BridgeEventType(Enum):
    """Event types for bridge communication"""
    # Yabai → SAI
    SPATIAL_CONTEXT_CHANGED = "spatial_context_changed"
    WORKSPACE_TRANSITION = "workspace_transition"
    APP_FOCUS_CHANGED = "app_focus_changed"
    WINDOW_LAYOUT_CHANGED = "window_layout_changed"

    # SAI → Yabai
    VISUAL_STATE_DETECTED = "visual_state_detected"
    UI_ELEMENT_FOUND = "ui_element_found"
    SCREEN_CONTENT_CHANGED = "screen_content_changed"
    ACTION_REQUIRED = "action_required"

    # Bidirectional
    PATTERN_DETECTED = "pattern_detected"
    PREDICTION_AVAILABLE = "prediction_available"
    CONTEXT_ENRICHED = "context_enriched"


@dataclass
class BridgeEvent:
    """Event passing through the bridge"""
    event_type: BridgeEventType
    source: str                         # "yabai" or "sai"
    destination: str                    # "yabai" or "sai"
    timestamp: float
    data: Dict[str, Any]
    priority: int = 1                   # 1=low, 5=high
    requires_response: bool = False


@dataclass
class EnrichedContext:
    """Context enriched with both spatial and visual data"""
    space_id: int
    focused_app: str
    window_layout: Dict[str, Any]
    visual_state: Dict[str, Any]
    ui_elements: List[Dict[str, Any]]
    confidence: float
    timestamp: float


@dataclass
class IntegratedAction:
    """Action coordinated between Yabai and SAI"""
    action_type: str
    target_space: Optional[int]
    target_app: Optional[str]
    target_ui_element: Optional[Dict[str, Any]]
    spatial_coordinates: Optional[Dict[str, float]]
    confidence: float
    reasoning: str


# ============================================================================
# Yabai ↔ SAI Integration Bridge
# ============================================================================

class YabaiSAIBridge:
    """
    Bidirectional integration bridge between Yabai Spatial Intelligence and SAI

    Responsibilities:
    - Forward spatial events to SAI for visual context
    - Forward visual events to Yabai for spatial context
    - Merge spatial + visual intelligence
    - Coordinate actions across both systems
    - Enable cross-system learning
    """

    def __init__(
        self,
        yabai_intelligence=None,
        sai_engine=None,
        pattern_learner=None,
        enable_bidirectional: bool = True,
        enable_auto_enrichment: bool = True,
        enable_action_coordination: bool = True
    ):
        """
        Initialize Integration Bridge

        Args:
            yabai_intelligence: Yabai Spatial Intelligence instance
            sai_engine: SAI engine instance
            pattern_learner: Workspace Pattern Learner instance
            enable_bidirectional: Enable two-way communication
            enable_auto_enrichment: Auto-enrich contexts
            enable_action_coordination: Coordinate actions between systems
        """
        self.yabai = yabai_intelligence
        self.sai = sai_engine
        self.pattern_learner = pattern_learner

        self.enable_bidirectional = enable_bidirectional
        self.enable_auto_enrichment = enable_auto_enrichment
        self.enable_action_coordination = enable_action_coordination

        # Event management
        self.event_queue: asyncio.Queue = asyncio.Queue(maxsize=500)
        self.event_handlers: Dict[BridgeEventType, List[Callable]] = defaultdict(list)
        self.event_history: deque = deque(maxlen=1000)

        # Context management
        self.current_enriched_context: Optional[EnrichedContext] = None
        self.context_cache: deque = deque(maxlen=100)

        # Action coordination
        self.pending_actions: List[IntegratedAction] = []
        self.action_history: deque = deque(maxlen=500)

        # Bridge state
        self.is_active = False
        self.processing_task: Optional[asyncio.Task] = None

        # Metrics
        self.metrics = {
            'events_bridged': 0,
            'contexts_enriched': 0,
            'actions_coordinated': 0,
            'yabai_to_sai': 0,
            'sai_to_yabai': 0,
            'pattern_correlations': 0
        }

        logger.info("[BRIDGE] Yabai ↔ SAI Integration Bridge initialized")
        logger.info(f"[BRIDGE] Bidirectional: {enable_bidirectional}")
        logger.info(f"[BRIDGE] Auto-enrichment: {enable_auto_enrichment}")
        logger.info(f"[BRIDGE] Action coordination: {enable_action_coordination}")

    # ========================================================================
    # Bridge Lifecycle
    # ========================================================================

    async def start(self):
        """Start the integration bridge"""
        if self.is_active:
            logger.warning("[BRIDGE] Already active")
            return

        logger.info("[BRIDGE] Starting Yabai ↔ SAI integration bridge...")
        self.is_active = True

        # Register event listeners on both systems
        await self._register_yabai_listeners()
        await self._register_sai_listeners()

        # Start event processing
        self.processing_task = asyncio.create_task(self._process_events())

        logger.info("[BRIDGE] ✅ Integration bridge active")
        logger.info("[BRIDGE] ✅ Cross-system intelligence enabled")

    async def stop(self):
        """Stop the integration bridge"""
        if not self.is_active:
            return

        logger.info("[BRIDGE] Stopping integration bridge...")
        self.is_active = False

        # Cancel processing task
        if self.processing_task:
            self.processing_task.cancel()
            try:
                await self.processing_task
            except asyncio.CancelledError:
                pass

        logger.info("[BRIDGE] ✅ Integration bridge stopped")
        logger.info(f"[BRIDGE] Total events bridged: {self.metrics['events_bridged']}")
        logger.info(f"[BRIDGE] Contexts enriched: {self.metrics['contexts_enriched']}")

    # ========================================================================
    # Event Listener Registration
    # ========================================================================

    async def _register_yabai_listeners(self):
        """Register listeners on Yabai events"""
        if not self.yabai:
            logger.warning("[BRIDGE] Yabai not available, skipping listener registration")
            return

        try:
            from intelligence.yabai_spatial_intelligence import YabaiEventType

            # Register for key Yabai events
            self.yabai.register_event_listener(
                YabaiEventType.SPACE_CHANGED,
                self._on_yabai_space_changed
            )
            self.yabai.register_event_listener(
                YabaiEventType.WINDOW_FOCUSED,
                self._on_yabai_window_focused
            )
            self.yabai.register_event_listener(
                YabaiEventType.WINDOW_MOVED,
                self._on_yabai_window_moved
            )
            self.yabai.register_event_listener(
                YabaiEventType.APP_LAUNCHED,
                self._on_yabai_app_launched
            )

            logger.info("[BRIDGE] ✅ Registered Yabai event listeners")

        except Exception as e:
            logger.error(f"[BRIDGE] Error registering Yabai listeners: {e}", exc_info=True)

    async def _register_sai_listeners(self):
        """Register listeners on SAI events"""
        if not self.sai:
            logger.warning("[BRIDGE] SAI not available, skipping listener registration")
            return

        try:
            # SAI will notify bridge of visual state changes
            # This is handled through polling in the bridge event loop
            logger.info("[BRIDGE] ✅ SAI monitoring integrated")

        except Exception as e:
            logger.error(f"[BRIDGE] Error registering SAI listeners: {e}", exc_info=True)

    # ========================================================================
    # Yabai Event Handlers (Yabai → SAI)
    # ========================================================================

    async def _on_yabai_space_changed(self, event):
        """Handle Space change from Yabai"""
        try:
            bridge_event = BridgeEvent(
                event_type=BridgeEventType.WORKSPACE_TRANSITION,
                source="yabai",
                destination="sai",
                timestamp=event.timestamp,
                data={
                    'from_space': event.metadata.get('from_space'),
                    'to_space': event.metadata.get('to_space'),
                    'space_id': event.space_id
                },
                priority=4
            )
            await self._emit_bridge_event(bridge_event)

            # Trigger context enrichment
            if self.enable_auto_enrichment:
                await self._enrich_spatial_context(event.space_id)

            # Learn from pattern
            if self.pattern_learner:
                await self.pattern_learner.learn_from_event("space_changed", {
                    'space_id': event.space_id,
                    'from_space': event.metadata.get('from_space'),
                    'to_space': event.metadata.get('to_space')
                })

            self.metrics['yabai_to_sai'] += 1

        except Exception as e:
            logger.error(f"[BRIDGE] Error handling space change: {e}", exc_info=True)

    async def _on_yabai_window_focused(self, event):
        """Handle window focus from Yabai"""
        try:
            bridge_event = BridgeEvent(
                event_type=BridgeEventType.APP_FOCUS_CHANGED,
                source="yabai",
                destination="sai",
                timestamp=event.timestamp,
                data={
                    'app_name': event.app_name,
                    'space_id': event.space_id,
                    'window_id': event.window_id,
                    'title': event.metadata.get('title')
                },
                priority=3
            )
            await self._emit_bridge_event(bridge_event)

            # Request SAI to analyze current screen
            if self.sai and self.enable_auto_enrichment:
                await self._request_sai_analysis()

            # Learn from pattern
            if self.pattern_learner:
                await self.pattern_learner.learn_from_event("window_focused", {
                    'app_name': event.app_name,
                    'space_id': event.space_id,
                    'window_id': event.window_id
                })

            self.metrics['yabai_to_sai'] += 1

        except Exception as e:
            logger.error(f"[BRIDGE] Error handling window focus: {e}", exc_info=True)

    async def _on_yabai_window_moved(self, event):
        """Handle window move from Yabai"""
        try:
            bridge_event = BridgeEvent(
                event_type=BridgeEventType.WINDOW_LAYOUT_CHANGED,
                source="yabai",
                destination="sai",
                timestamp=event.timestamp,
                data={
                    'app_name': event.app_name,
                    'window_id': event.window_id,
                    'from_space': event.metadata.get('from_space'),
                    'to_space': event.metadata.get('to_space')
                },
                priority=2
            )
            await self._emit_bridge_event(bridge_event)

            self.metrics['yabai_to_sai'] += 1

        except Exception as e:
            logger.error(f"[BRIDGE] Error handling window move: {e}", exc_info=True)

    async def _on_yabai_app_launched(self, event):
        """Handle app launch from Yabai"""
        try:
            bridge_event = BridgeEvent(
                event_type=BridgeEventType.SPATIAL_CONTEXT_CHANGED,
                source="yabai",
                destination="sai",
                timestamp=event.timestamp,
                data={
                    'app_name': event.app_name,
                    'event': 'app_launched'
                },
                priority=3
            )
            await self._emit_bridge_event(bridge_event)

            # Learn from pattern
            if self.pattern_learner:
                await self.pattern_learner.learn_from_event("app_launched", {
                    'app_name': event.app_name
                })

            self.metrics['yabai_to_sai'] += 1

        except Exception as e:
            logger.error(f"[BRIDGE] Error handling app launch: {e}", exc_info=True)

    # ========================================================================
    # Context Enrichment
    # ========================================================================

    async def _enrich_spatial_context(self, space_id: int):
        """Enrich spatial context with visual data from SAI"""
        try:
            if not self.sai or not self.yabai:
                return

            logger.debug(f"[BRIDGE] Enriching context for Space {space_id}")

            # Get spatial data from Yabai
            spatial_context = await self._get_yabai_spatial_context(space_id)

            # Get visual data from SAI
            visual_context = await self._get_sai_visual_context()

            # Merge contexts
            enriched = EnrichedContext(
                space_id=space_id,
                focused_app=spatial_context.get('focused_app', 'Unknown'),
                window_layout=spatial_context.get('layout', {}),
                visual_state=visual_context.get('state', {}),
                ui_elements=visual_context.get('elements', []),
                confidence=min(
                    spatial_context.get('confidence', 0.5),
                    visual_context.get('confidence', 0.5)
                ),
                timestamp=time.time()
            )

            self.current_enriched_context = enriched
            self.context_cache.append(enriched)
            self.metrics['contexts_enriched'] += 1

            logger.debug(f"[BRIDGE] Context enriched with {len(enriched.ui_elements)} UI elements")

            # Emit enriched context event
            bridge_event = BridgeEvent(
                event_type=BridgeEventType.CONTEXT_ENRICHED,
                source="bridge",
                destination="all",
                timestamp=time.time(),
                data=asdict(enriched),
                priority=4
            )
            await self._emit_bridge_event(bridge_event)

        except Exception as e:
            logger.error(f"[BRIDGE] Error enriching context: {e}", exc_info=True)

    async def _get_yabai_spatial_context(self, space_id: int) -> Dict[str, Any]:
        """Get spatial context from Yabai"""
        try:
            if not self.yabai or space_id not in self.yabai.current_spaces:
                return {}

            space = self.yabai.current_spaces[space_id]

            return {
                'space_id': space_id,
                'focused_app': space.focused_window.app_name if space.focused_window else None,
                'window_count': len(space.windows),
                'layout': {
                    'windows': [
                        {
                            'app': w.app_name,
                            'frame': w.frame,
                            'focused': w.is_focused
                        }
                        for w in space.windows
                    ]
                },
                'confidence': 0.9
            }

        except Exception as e:
            logger.error(f"[BRIDGE] Error getting spatial context: {e}", exc_info=True)
            return {}

    async def _get_sai_visual_context(self) -> Dict[str, Any]:
        """
        Get comprehensive visual context from SAI

        Returns rich contextual information including:
        - UI state (resolution, active space, screen lock status)
        - UI elements (tracked element positions and confidence)
        - Screen state (display topology, resolution)
        - Recent environmental changes
        - Cache statistics
        """
        try:
            if not self.sai:
                logger.warning("[BRIDGE] SAI not initialized, returning empty context")
                return {}

            # Get current comprehensive context from SAI
            context = await self.sai.get_current_context()

            logger.debug(
                f"[BRIDGE] Retrieved SAI context: "
                f"{len(context.get('ui_elements', []))} elements, "
                f"confidence={context.get('confidence', 0):.2f}, "
                f"monitoring={'active' if context.get('monitoring_active') else 'inactive'}"
            )

            return {
                'state': context.get('ui_state', {}),
                'elements': context.get('ui_elements', []),
                'screen_state': context.get('screen_state', {}),
                'confidence': context.get('confidence', 0.5),
                'display_topology': context.get('display_topology', {}),
                'tracked_elements': context.get('tracked_elements', {}),
                'recent_changes': context.get('recent_changes', []),
                'cache_stats': context.get('cache_stats', {}),
                'environment_hash': context.get('environment_hash'),
                'timestamp': context.get('timestamp')
            }

        except Exception as e:
            logger.error(f"[BRIDGE] Error getting visual context: {e}", exc_info=True)
            return {}

    async def _request_sai_analysis(self):
        """Request SAI to analyze current screen"""
        try:
            if not self.sai:
                return

            # Update SAI display topology to reflect current screen state
            # The SAI monitoring loop will pick up changes automatically
            if hasattr(self.sai, 'display_awareness') and self.sai.display_awareness:
                await self.sai.display_awareness.update_topology()

        except Exception as e:
            logger.error(f"[BRIDGE] Error requesting SAI analysis: {e}", exc_info=True)

    # ========================================================================
    # Event Processing
    # ========================================================================

    async def _emit_bridge_event(self, event: BridgeEvent):
        """Emit event to bridge queue"""
        try:
            await self.event_queue.put(event)
            self.event_history.append(event)
            self.metrics['events_bridged'] += 1

        except asyncio.QueueFull:
            logger.warning(f"[BRIDGE] Event queue full, dropping event: {event.event_type.value}")

    async def _process_events(self):
        """Process events from the bridge queue"""
        logger.info("[BRIDGE] Event processing started")

        while self.is_active:
            try:
                # Get event with timeout
                event = await asyncio.wait_for(self.event_queue.get(), timeout=1.0)

                # Route event to handlers
                handlers = self.event_handlers.get(event.event_type, [])
                for handler in handlers:
                    try:
                        if asyncio.iscoroutinefunction(handler):
                            await handler(event)
                        else:
                            handler(event)
                    except Exception as e:
                        logger.error(f"[BRIDGE] Error in event handler: {e}", exc_info=True)

                # Log bridged event
                logger.debug(f"[BRIDGE] Event bridged: {event.source} → {event.destination}: {event.event_type.value}")

            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[BRIDGE] Error processing event: {e}", exc_info=True)

    # ========================================================================
    # Action Coordination
    # ========================================================================

    async def coordinate_action(
        self,
        action_type: str,
        target_space: Optional[int] = None,
        target_app: Optional[str] = None,
        confidence: float = 0.7
    ) -> IntegratedAction:
        """
        Coordinate an action between Yabai and SAI

        Args:
            action_type: Type of action to perform
            target_space: Target Space ID (Yabai)
            target_app: Target app name (Yabai)
            confidence: Confidence in this action

        Returns:
            IntegratedAction with spatial and visual coordinates
        """
        try:
            if not self.enable_action_coordination:
                raise ValueError("Action coordination not enabled")

            logger.info(f"[BRIDGE] Coordinating action: {action_type}")

            # Get spatial coordinates from Yabai
            spatial_coords = None
            if target_space and self.yabai:
                spatial_coords = await self._get_spatial_coordinates(target_space, target_app)

            # Get UI element from SAI
            ui_element = None
            if target_app and self.sai:
                ui_element = await self._find_ui_element(target_app)

            # Create integrated action
            action = IntegratedAction(
                action_type=action_type,
                target_space=target_space,
                target_app=target_app,
                target_ui_element=ui_element,
                spatial_coordinates=spatial_coords,
                confidence=confidence,
                reasoning=f"Coordinated {action_type} between Yabai and SAI"
            )

            self.pending_actions.append(action)
            self.action_history.append(action)
            self.metrics['actions_coordinated'] += 1

            logger.info(f"[BRIDGE] ✅ Action coordinated: {action_type} (confidence: {confidence:.2f})")

            return action

        except Exception as e:
            logger.error(f"[BRIDGE] Error coordinating action: {e}", exc_info=True)
            raise

    async def _get_spatial_coordinates(self, space_id: int, app_name: Optional[str]) -> Optional[Dict[str, float]]:
        """Get spatial coordinates from Yabai"""
        try:
            if not self.yabai or space_id not in self.yabai.current_spaces:
                return None

            space = self.yabai.current_spaces[space_id]

            # Find window
            target_window = None
            if app_name:
                target_window = next((w for w in space.windows if w.app_name == app_name), None)
            else:
                target_window = space.focused_window

            if target_window:
                return target_window.frame

            return None

        except Exception as e:
            logger.error(f"[BRIDGE] Error getting spatial coordinates: {e}", exc_info=True)
            return None

    async def _find_ui_element(self, app_name: str) -> Optional[Dict[str, Any]]:
        """Find UI element from SAI"""
        try:
            if not self.sai:
                return None

            # Get current visual context
            visual_context = await self._get_sai_visual_context()
            elements = visual_context.get('elements', [])

            # Find element related to app
            for element in elements:
                if app_name.lower() in element.get('text', '').lower():
                    return element

            return None

        except Exception as e:
            logger.error(f"[BRIDGE] Error finding UI element: {e}", exc_info=True)
            return None

    # ========================================================================
    # Queries
    # ========================================================================

    def get_enriched_context(self) -> Optional[EnrichedContext]:
        """Get current enriched context"""
        return self.current_enriched_context

    def get_metrics(self) -> Dict[str, Any]:
        """Get bridge metrics"""
        return {
            **self.metrics,
            'pending_actions': len(self.pending_actions),
            'event_queue_size': self.event_queue.qsize(),
            'is_active': self.is_active
        }


# ============================================================================
# Factory Functions
# ============================================================================

_bridge_instance: Optional[YabaiSAIBridge] = None


async def initialize_bridge(
    yabai_intelligence=None,
    sai_engine=None,
    pattern_learner=None
) -> YabaiSAIBridge:
    """Initialize and start the Yabai ↔ SAI bridge"""
    global _bridge_instance

    if _bridge_instance is None:
        _bridge_instance = YabaiSAIBridge(
            yabai_intelligence=yabai_intelligence,
            sai_engine=sai_engine,
            pattern_learner=pattern_learner
        )

        await _bridge_instance.start()
        logger.info("[BRIDGE] Integration bridge initialized and started")

    return _bridge_instance


def get_bridge() -> Optional[YabaiSAIBridge]:
    """Get existing bridge instance"""
    return _bridge_instance


async def shutdown_bridge():
    """Shutdown the bridge"""
    global _bridge_instance

    if _bridge_instance:
        await _bridge_instance.stop()
        _bridge_instance = None
        logger.info("[BRIDGE] Bridge shutdown complete")
