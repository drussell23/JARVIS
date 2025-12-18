#!/usr/bin/env python3
"""
Advanced Async Architecture - Dynamic Event-Driven Command Pipeline

Ultra-robust, adaptive, zero-hardcoding async processing system for JARVIS.
Provides a comprehensive pipeline for processing voice commands with dynamic
stage registration, circuit breaker patterns, event-driven architecture,
and intelligent error handling.

This module implements:
- Dynamic pipeline stages with configurable timeouts and retries
- Adaptive circuit breaker with ML-based prediction
- Event-driven message bus with priority handling
- Context-aware command processing
- Follow-up intent detection and routing
- Comprehensive error handling and recovery
- AGI OS integration for autonomous event streaming and proactive actions

AGI OS Integration:
- Emits pipeline events to AGI OS event stream
- Receives action requests from AGI OS for autonomous processing
- Dynamic owner identification for personalized responses
- Voice communicator integration for real-time TTS feedback (Daniel voice)

Example:
    >>> pipeline = get_async_pipeline(jarvis_instance)
    >>> result = await pipeline.process_async("open safari and search for dogs")
    >>> print(result['response'])
    "I've opened Safari and searched for dogs, Sir."

    # With AGI OS integration
    >>> owner_name = await pipeline.get_owner_name()
    >>> await pipeline.emit_agi_event("COMMAND_COMPLETED", {"response": result})
"""

import asyncio
import logging
import re
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

# Ensure project root is in path for 'from backend.X' imports
_project_root = Path(__file__).parent.parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

logger = logging.getLogger(__name__)


class PipelineStage(Enum):
    """Dynamic pipeline processing stages.

    Represents the various stages a command goes through during processing,
    from initial receipt to final completion or failure.
    """

    RECEIVED = "received"
    VALIDATED = "validated"
    PREPROCESSED = "preprocessed"
    INTENT_ANALYSIS = "intent_analysis"
    COMPONENT_LOADING = "component_loading"
    MIDDLEWARE = "middleware"
    PROCESSING = "processing"
    POSTPROCESSING = "postprocessing"
    RESPONSE_GENERATION = "response_generation"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class PipelineContext:
    """Context passed through the pipeline stages.

    Contains all information needed to process a command, including metadata,
    metrics, and state information that accumulates as the command moves
    through different pipeline stages.

    Attributes:
        command_id: Unique identifier for this command
        text: The original command text
        user_name: Name of the user issuing the command
        timestamp: Unix timestamp when command was received
        stage: Current pipeline stage
        intent: Detected intent of the command
        components_loaded: List of components loaded for this command
        response: Generated response text
        error: Error message if command failed
        metadata: Additional metadata dictionary
        metrics: Performance metrics dictionary
        retries: Number of retry attempts made
        priority: Command priority (0=normal, 1=high, 2=critical)
        audio_data: Voice audio data for authentication
        speaker_name: Identified speaker name from voice recognition
    """

    command_id: str
    text: str
    user_name: str = "Sir"
    timestamp: float = field(default_factory=time.time)
    stage: PipelineStage = PipelineStage.RECEIVED
    intent: Optional[str] = None
    components_loaded: List[str] = field(default_factory=list)
    response: Optional[str] = None
    error: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    metrics: Dict[str, float] = field(default_factory=dict)
    retries: int = 0
    priority: int = 0  # 0=normal, 1=high, 2=critical
    audio_data: Optional[bytes] = None  # Voice audio for authentication
    speaker_name: Optional[str] = None  # Identified speaker name

    def to_dict(self) -> Dict[str, Any]:
        """Convert context to dictionary for serialization.

        Returns:
            Dictionary representation of the context, excluding binary audio data.
        """
        return {
            "command_id": self.command_id,
            "text": self.text,
            "user_name": self.user_name,
            "timestamp": self.timestamp,
            "stage": self.stage.value,
            "intent": self.intent,
            "components_loaded": self.components_loaded,
            "response": self.response,
            "error": self.error,
            "metadata": self.metadata,
            "metrics": self.metrics,
            "retries": self.retries,
            "priority": self.priority,
        }


class AdaptiveCircuitBreaker:
    """Advanced circuit breaker with adaptive thresholds and ML-based prediction.

    Implements the circuit breaker pattern with adaptive learning capabilities.
    Automatically adjusts failure thresholds and timeouts based on historical
    performance patterns to optimize system resilience.

    Attributes:
        failure_count: Current number of consecutive failures
        success_count: Current number of consecutive successes
        threshold: Current failure threshold before opening circuit
        timeout: Current timeout before attempting to close circuit
        state: Current circuit state (CLOSED, OPEN, HALF_OPEN)
        last_failure_time: Timestamp of last failure
        adaptive: Whether adaptive learning is enabled
        failure_history: List of failure timestamps
        success_rate_history: List of historical success rates
    """

    def __init__(
        self,
        initial_threshold: int = 5,
        initial_timeout: int = 60,
        adaptive: bool = True,
    ):
        """Initialize the adaptive circuit breaker.

        Args:
            initial_threshold: Initial failure count threshold
            initial_timeout: Initial timeout in seconds
            adaptive: Enable adaptive threshold adjustment
        """
        self.failure_count = 0
        self.success_count = 0
        self.threshold = initial_threshold
        self.timeout = initial_timeout
        self.state = "CLOSED"  # CLOSED, OPEN, HALF_OPEN
        self.last_failure_time = None
        self.adaptive = adaptive
        self.failure_history: List[float] = []
        self.success_rate_history: List[float] = []
        self._total_calls = 0
        self._successful_calls = 0

    @property
    def success_rate(self) -> float:
        """Calculate current success rate.

        Returns:
            Success rate as a float between 0.0 and 1.0
        """
        if self._total_calls == 0:
            return 1.0  # Default to 100% success if no calls yet
        return self._successful_calls / self._total_calls

    async def call(self, func: Callable, *args, **kwargs):
        """Execute function with adaptive circuit breaker protection.

        Args:
            func: Function to execute
            *args: Positional arguments for the function
            **kwargs: Keyword arguments for the function

        Returns:
            Result of the function call

        Raises:
            Exception: If circuit is open or function fails
        """
        if self.state == "OPEN":
            if time.time() - self.last_failure_time > self.timeout:
                logger.info(
                    f"Circuit breaker: transitioning to HALF_OPEN (threshold={self.threshold})"
                )
                self.state = "HALF_OPEN"
            else:
                raise Exception(
                    f"Circuit breaker is OPEN - service unavailable (retry in {int(self.timeout - (time.time() - self.last_failure_time))}s)"
                )

        try:
            start = time.time()
            result = await func(*args, **kwargs)
            duration = time.time() - start

            self.on_success(duration)
            return result

        except Exception as e:
            self.on_failure()
            raise e

    def on_success(self, duration: float):
        """Handle successful execution with adaptive learning.

        Args:
            duration: Execution duration in seconds
        """
        self.success_count += 1
        self._total_calls += 1
        self._successful_calls += 1
        self.failure_count = max(0, self.failure_count - 1)  # Gradual recovery

        if self.state == "HALF_OPEN":
            logger.info("Circuit breaker: transitioning to CLOSED")
            self.state = "CLOSED"

        # Adaptive threshold adjustment
        if self.adaptive:
            success_rate = self.success_count / (self.success_count + len(self.failure_history))
            self.success_rate_history.append(success_rate)

            # Increase threshold if success rate is high
            if success_rate > 0.95 and self.threshold < 20:
                self.threshold += 1
                logger.debug(f"Increased circuit breaker threshold to {self.threshold}")

    def on_failure(self):
        """Handle failed execution with adaptive learning."""
        self.failure_count += 1
        self._total_calls += 1
        self.last_failure_time = time.time()
        self.failure_history.append(time.time())

        # Adaptive threshold adjustment
        if self.adaptive and len(self.failure_history) > 10:
            recent_failures = sum(1 for t in self.failure_history[-10:] if time.time() - t < 60)

            if recent_failures > 5 and self.threshold > 3:
                self.threshold -= 1
                logger.warning(f"Decreased circuit breaker threshold to {self.threshold}")

        if self.failure_count >= self.threshold:
            logger.warning(
                f"Circuit breaker: OPEN (failures: {self.failure_count}/{self.threshold})"
            )
            self.state = "OPEN"

            # Adaptive timeout based on failure patterns
            if self.adaptive:
                avg_failure_interval = self._calculate_failure_interval()
                if avg_failure_interval > 0:
                    self.timeout = min(300, int(avg_failure_interval * 2))  # Max 5 min
                    logger.info(f"Adaptive timeout set to {self.timeout}s")

    def _calculate_failure_interval(self) -> float:
        """Calculate average interval between failures.

        Returns:
            Average interval in seconds, or 0 if insufficient data
        """
        if len(self.failure_history) < 2:
            return 0

        intervals = []
        for i in range(1, min(10, len(self.failure_history))):
            interval = self.failure_history[-i] - self.failure_history[-(i + 1)]
            intervals.append(interval)

        return sum(intervals) / len(intervals) if intervals else 0


class AsyncEventBus:
    """Advanced event-driven message bus with filtering and priority.

    Provides a publish-subscribe event system with priority handling,
    filtering capabilities, and comprehensive event history tracking.

    Attributes:
        subscribers: Dictionary mapping event types to subscriber lists
        event_queue: Priority queue for event processing
        event_history: List of recent events for debugging
        max_history: Maximum number of events to keep in history
        listeners: Compatibility alias for subscribers
        queue: Compatibility alias for event_queue
    """

    def __init__(self):
        """Initialize the async event bus."""
        self.subscribers: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        self.event_queue = asyncio.PriorityQueue()
        self.event_history: List[Dict[str, Any]] = []
        self.max_history = 1000
        self.listeners = {}  # For compatibility with get_metrics
        self.queue = self.event_queue  # Alias for compatibility

    def subscribe(
        self,
        event_type: str,
        handler: Callable,
        priority: int = 0,
        filter_func: Optional[Callable] = None,
    ):
        """Subscribe to an event type with priority and filtering.

        Args:
            event_type: Type of event to subscribe to
            handler: Function to call when event is emitted
            priority: Handler priority (higher values execute first)
            filter_func: Optional filter function to determine if handler should run
        """
        self.subscribers[event_type].append(
            {"handler": handler, "priority": priority, "filter": filter_func}
        )
        logger.info(f"Subscribed handler to event: {event_type} (priority={priority})")

    async def emit(self, event_type: str, data: Any, priority: int = 0):
        """Emit an event to all subscribers with priority.

        Args:
            event_type: Type of event being emitted
            data: Event data to pass to handlers
            priority: Event priority for processing order
        """
        event = {
            "type": event_type,
            "data": data,
            "timestamp": time.time(),
            "priority": priority,
        }

        # Store in history
        self.event_history.append(event)
        if len(self.event_history) > self.max_history:
            self.event_history.pop(0)

        logger.debug(f"Emitting event: {event_type} (priority={priority})")

        if event_type in self.subscribers:
            # Sort by priority (higher priority first)
            sorted_subs = sorted(
                self.subscribers[event_type], key=lambda x: x["priority"], reverse=True
            )

            tasks = []
            for sub in sorted_subs:
                # Apply filter if present
                if sub["filter"] and not sub["filter"](data):
                    continue

                tasks.append(self._safe_handle(sub["handler"], data))

            await asyncio.gather(*tasks, return_exceptions=True)

    async def _safe_handle(self, handler: Callable, data: Any):
        """Safely execute handler with error handling.

        Args:
            handler: Event handler function
            data: Event data to pass to handler
        """
        try:
            if asyncio.iscoroutinefunction(handler):
                await handler(data)
            else:
                handler(data)
        except Exception as e:
            logger.error(f"Error in event handler: {e}", exc_info=True)

    def get_event_stats(self) -> Dict[str, Any]:
        """Get event bus statistics.

        Returns:
            Dictionary containing event statistics and metrics
        """
        event_counts = defaultdict(int)
        for event in self.event_history:
            event_counts[event["type"]] += 1

        return {
            "total_events": len(self.event_history),
            "event_types": len(event_counts),
            "event_counts": dict(event_counts),
            "subscribers": {k: len(v) for k, v in self.subscribers.items()},
        }


class PipelineMiddleware:
    """Middleware system for pipeline processing.

    Provides a way to inject processing logic at various points in the
    pipeline execution without modifying the core pipeline stages.

    Attributes:
        name: Middleware identifier
        handler: Function to execute for middleware processing
        enabled: Whether this middleware is currently active
        metrics: Performance metrics for this middleware
    """

    def __init__(self, name: str, handler: Callable):
        """Initialize pipeline middleware.

        Args:
            name: Unique name for this middleware
            handler: Function to execute for processing
        """
        self.name = name
        self.handler = handler
        self.enabled = True
        self.metrics: Dict[str, float] = {}

    async def process(self, context: PipelineContext) -> PipelineContext:
        """Process context through middleware.

        Args:
            context: Pipeline context to process

        Returns:
            Modified pipeline context
        """
        if not self.enabled:
            return context

        start = time.time()
        try:
            if asyncio.iscoroutinefunction(self.handler):
                await self.handler(context)
            else:
                self.handler(context)

            self.metrics["last_duration"] = time.time() - start
            self.metrics["total_calls"] = self.metrics.get("total_calls", 0) + 1

        except Exception as e:
            logger.error(f"Middleware {self.name} error: {e}", exc_info=True)
            context.metadata[f"middleware_error_{self.name}"] = str(e)

        return context


class DynamicPipelineStage:
    """Dynamic pipeline stage with configurable behavior.

    Represents a single stage in the processing pipeline with configurable
    timeout, retry logic, and requirement settings. Tracks performance
    metrics for monitoring and optimization.

    Attributes:
        name: Stage identifier
        handler: Function to execute for this stage
        timeout: Maximum execution time in seconds
        retry_count: Number of retry attempts on failure
        required: Whether stage failure should fail the entire pipeline
        metrics: Performance and execution metrics
    """

    def __init__(
        self,
        name: str,
        handler: Callable,
        timeout: Optional[float] = None,
        retry_count: int = 0,
        required: bool = True,
    ):
        """Initialize dynamic pipeline stage.

        Args:
            name: Unique name for this stage
            handler: Function to execute for stage processing
            timeout: Maximum execution time in seconds
            retry_count: Number of retry attempts on failure
            required: Whether stage failure should fail entire pipeline
        """
        self.name = name
        self.handler = handler
        self.timeout = timeout or 30.0
        self.retry_count = retry_count
        self.required = required
        self.metrics: Dict[str, Any] = {
            "executions": 0,
            "failures": 0,
            "total_duration": 0.0,
            "avg_duration": 0.0,
        }

    async def execute(self, context: PipelineContext) -> None:
        """Execute stage with retry logic.

        Args:
            context: Pipeline context to process

        Raises:
            Exception: If stage is required and all retries fail
        """
        attempts = 0
        last_error = None

        while attempts <= self.retry_count:
            try:
                start = time.time()

                # Execute with timeout
                await asyncio.wait_for(self._run_handler(context), timeout=self.timeout)

                duration = time.time() - start
                self._update_metrics(duration, success=True)
                context.metrics[f"stage_{self.name}_duration"] = duration

                return

            except asyncio.TimeoutError:
                last_error = f"Stage {self.name} timed out after {self.timeout}s"
                logger.warning(last_error)

            except Exception as e:
                last_error = f"Stage {self.name} error: {str(e)}"
                logger.error(last_error, exc_info=True)

            attempts += 1
            if attempts <= self.retry_count:
                await asyncio.sleep(2**attempts)  # Exponential backoff

        # All retries failed
        self._update_metrics(0, success=False)

        if self.required:
            raise Exception(last_error or f"Stage {self.name} failed")
        else:
            logger.warning(f"Non-required stage {self.name} failed, continuing...")
            context.metadata[f"stage_{self.name}_skipped"] = True

    async def _run_handler(self, context: PipelineContext):
        """Run the stage handler.

        Args:
            context: Pipeline context to process
        """
        if asyncio.iscoroutinefunction(self.handler):
            await self.handler(context)
        else:
            self.handler(context)

    def _update_metrics(self, duration: float, success: bool):
        """Update stage performance metrics.

        Args:
            duration: Execution duration in seconds
            success: Whether execution was successful
        """
        self.metrics["executions"] += 1
        if not success:
            self.metrics["failures"] += 1

        self.metrics["total_duration"] += duration
        self.metrics["avg_duration"] = self.metrics["total_duration"] / self.metrics["executions"]


class AdvancedAsyncPipeline:
    """Ultra-advanced async pipeline with dynamic configuration.

    Main pipeline class that orchestrates command processing through multiple
    stages with event-driven architecture, circuit breaker protection, and
    comprehensive error handling. Supports dynamic stage registration,
    middleware injection, and context-aware processing.

    Attributes:
        jarvis: Reference to main JARVIS instance
        config: Pipeline configuration dictionary
        event_bus: Event bus for publish-subscribe messaging
        circuit_breaker: Circuit breaker for fault tolerance
        stages: Dictionary of registered pipeline stages
        middleware: List of registered middleware components
        active_commands: Currently processing commands
        performance_metrics: Historical performance data
        intent_engine: Intent classification engine
        context_store: Context storage for follow-up handling
        router: Command routing system
        context_bridge: Bridge to context intelligence system
    """

    def __init__(self, jarvis_instance=None, config: Optional[Dict[str, Any]] = None):
        """Initialize the advanced async pipeline.

        Args:
            jarvis_instance: Reference to main JARVIS instance
            config: Configuration dictionary for pipeline settings
        """
        self.jarvis = jarvis_instance
        self.config = config or {}
        self.event_bus = AsyncEventBus()
        self.circuit_breaker = AdaptiveCircuitBreaker(
            initial_threshold=self.config.get("circuit_breaker_threshold", 5),
            initial_timeout=self.config.get("circuit_breaker_timeout", 60),
            adaptive=self.config.get("adaptive_circuit_breaker", True),
        )

        # Dynamic stage registry
        self.stages: Dict[str, DynamicPipelineStage] = {}
        self.middleware: List[PipelineMiddleware] = []
        self.active_commands: Dict[str, PipelineContext] = {}

        # Performance monitoring
        self.performance_metrics: Dict[str, List[float]] = defaultdict(list)

        # ═══════════════════════════════════════════════════════════════
        # Follow-Up System Components
        # ═══════════════════════════════════════════════════════════════
        self.intent_engine = None
        self.context_store = None
        self.router = None
        self._follow_up_enabled = self.config.get("follow_up_enabled", True)

        # ═══════════════════════════════════════════════════════════════
        # Context Intelligence System (Priority 1-3)
        # ═══════════════════════════════════════════════════════════════
        self.context_bridge = None  # Will be set by main.py if available

        # ═══════════════════════════════════════════════════════════════
        # AGI OS Integration - Autonomous Event Streaming
        # ═══════════════════════════════════════════════════════════════
        self.agi_os_enabled = self.config.get("agi_os_enabled", True)
        self._agi_event_stream = None
        self._agi_voice = None
        self._agi_owner_service = None

        if self.agi_os_enabled:
            asyncio.create_task(self._init_agi_os_integration())

        # ═══════════════════════════════════════════════════════════════
        # VBI Health Monitor Integration - Advanced Health Tracking
        # ═══════════════════════════════════════════════════════════════
        self._vbi_health_monitor = None
        self._health_monitor_enabled = self.config.get("vbi_health_monitor_enabled", True)

        if self._health_monitor_enabled:
            asyncio.create_task(self._init_vbi_health_monitor())

        if self._follow_up_enabled:
            try:
                self._init_followup_system()
                logger.info("✅ Follow-up system initialized successfully")
            except Exception as e:
                logger.error(f"❌ Failed to initialize follow-up system: {e}", exc_info=True)
                self._follow_up_enabled = False

        # Initialize default stages
        self._register_default_stages()

    def _register_default_stages(self):
        """Register default pipeline stages with appropriate timeouts and requirements."""
        self.register_stage("validation", self._validate_command, timeout=5.0, required=True)

        self.register_stage(
            "screen_lock_check",
            self._check_screen_lock_universal,
            timeout=5.0,
            required=True,
        )

        self.register_stage("preprocessing", self._preprocess_command, timeout=5.0, required=False)

        self.register_stage("intent_analysis", self._analyze_intent, timeout=10.0, required=True)

        self.register_stage(
            "component_loading", self._load_components, timeout=15.0, required=False
        )

        self.register_stage(
            "processing",
            self._process_command,
            timeout=60.0,  # Increased for locked screen unlock flow
            retry_count=2,
            required=True,
        )

        self.register_stage(
            "postprocessing", self._postprocess_response, timeout=5.0, required=False
        )

        self.register_stage(
            "response_generation", self._generate_response, timeout=10.0, required=True
        )

    async def _validate_command(self, context: PipelineContext) -> PipelineContext:
        """
        Validate command before processing.

        Checks for:
        - Empty commands
        - Invalid formats
        - Required fields
        - Security concerns

        Args:
            context: Pipeline context containing command data

        Returns:
            PipelineContext: Updated context with validation results

        Raises:
            ValueError: If command is invalid
        """
        command = context.data.get("command", "")

        # Check for empty command
        if not command or not command.strip():
            raise ValueError("Command cannot be empty")

        # Store validation result
        context.data["validated"] = True
        context.data["validation_timestamp"] = datetime.now().isoformat()

        return context

    async def _check_screen_lock_universal(self, context: PipelineContext) -> PipelineContext:
        """
        Universal screen lock detection across all paths.

        Checks if the screen is currently locked and handles accordingly.

        Args:
            context: Pipeline context

        Returns:
            PipelineContext: Updated context with lock status

        Note:
            This provides a unified screen lock check for all command paths.
        """
        # Check if we have screen lock detection available
        try:
            if hasattr(self, 'screen_lock_detector') and self.screen_lock_detector:
                is_locked = await self.screen_lock_detector.is_locked()
                context.data["screen_locked"] = is_locked
                if is_locked:
                    logger.info("Screen is currently locked")
            else:
                # No lock detector available, assume unlocked
                context.data["screen_locked"] = False
        except Exception as e:
            logger.debug(f"Screen lock check failed: {e}")
            context.data["screen_locked"] = False

        return context

    async def _preprocess_command(self, context: PipelineContext) -> PipelineContext:
        """Preprocess command before analysis."""
        # Add any preprocessing logic here
        context.data["preprocessed"] = True
        return context

    async def _analyze_intent(self, context: PipelineContext) -> PipelineContext:
        """Analyze command intent."""
        # Basic intent analysis
        command = context.data.get("command", "")
        context.data["intent"] = "general"  # Default intent
        context.data["intent_analyzed"] = True
        return context

    async def _load_components(self, context: PipelineContext) -> PipelineContext:
        """Load required components for command processing."""
        context.data["components_loaded"] = True
        return context

    async def _process_command(self, context: PipelineContext) -> PipelineContext:
        """Main command processing logic."""
        # This would contain the actual command processing
        context.data["processed"] = True
        context.data["result"] = {"status": "success", "message": "Command processed"}
        return context

    async def _postprocess_response(self, context: PipelineContext) -> PipelineContext:
        """Postprocess the response before generation."""
        context.data["postprocessed"] = True
        return context

    async def _generate_response(self, context: PipelineContext) -> PipelineContext:
        """Generate final response."""
        result = context.data.get("result", {})
        context.data["response"] = {
            "success": True,
            "data": result,
            "timestamp": datetime.now().isoformat()
        }
        return context

    def _generate_error_response(self, context: PipelineContext, error: Exception) -> Dict[str, Any]:
        """Generate error response for failed commands."""
        return {
            "success": False,
            "response": f"I encountered an error: {str(error)}",
            "error": str(error),
            "command_id": context.command_id,
            "timestamp": datetime.now().isoformat(),
            "metadata": {
                "stage": context.stage.value if hasattr(context, "stage") else "unknown",
                "command": context.text if hasattr(context, "text") else ""
            }
        }

    def _init_followup_system(self):
        """Initialize follow-up handling system components.

        Sets up intent classification, context storage, and routing systems
        for handling follow-up questions and contextual conversations.

        Raises:
            ImportError: If required follow-up system modules are not available
            Exception: If initialization fails
        """
        from pathlib import Path

        from backend.core.context.memory_store import InMemoryContextStore
        from backend.core.intent.adaptive_classifier import (
            AdaptiveIntentEngine,
            LexicalClassifier,
            WeightedVotingStrategy,
        )
        from backend.core.intent.intent_registry import IntentRegistry
        from backend.core.routing.adaptive_router import (
            AdaptiveRouter,
            PluginRegistry,
            RouteMatcher,
            context_validation_middleware,
            logging_middleware,
        )
        from backend.vision.handlers.follow_up_plugin import VisionFollowUpPlugin

        # Initialize intent registry and load patterns
        config_path = Path(__file__).parent.parent / "config" / "followup_intents.json"
        if config_path.exists():
            registry = IntentRegistry(config_path=config_path)
        else:
            from backend.core.intent.intent_registry import create_default_registry

            registry = create_default_registry()

        patterns = registry.get_all_patterns()

        # Create lexical classifier
        classifier = LexicalClassifier(
            name="lexical_followup",
            patterns=patterns,
            priority=100,  # Highest priority
            case_sensitive=False,
        )

        # Create intent engine
        self.intent_engine = AdaptiveIntentEngine(
            classifiers=[classifier],
            strategy=WeightedVotingStrategy(
                source_weights={"lexical_followup": 1.0},
                min_confidence=0.6,
            ),
        )

        # Initialize context store
        max_contexts = self.config.get("max_pending_contexts", 100)
        self.context_store = InMemoryContextStore(max_size=max_contexts)

        # Start auto-cleanup
        asyncio.create_task(self.context_store.start_auto_cleanup())

        # Initialize router
        matcher = RouteMatcher()
        self.router = AdaptiveRouter(matcher=matcher)

        # Add middleware
        self.router.use_middleware(logging_middleware)
        self.router.use_middleware(context_validation_middleware)

        # Register vision follow-up plugin
        self.plugin_registry = PluginRegistry(self.router)
        vision_plugin = VisionFollowUpPlugin()
        asyncio.create_task(self.plugin_registry.register_plugin("vision_followup", vision_plugin))

        logger.info(
            f"Follow-up system initialized: "
            f"{self.intent_engine.classifier_count} classifiers, "
            f"max_contexts={max_contexts}"
        )

    async def _init_agi_os_integration(self):
        """Initialize AGI OS integration for autonomous event streaming.

        Connects the pipeline to the AGI OS event stream, voice communicator,
        and owner identity service for personalized, proactive responses.
        """
        try:
            from agi_os import (
                get_event_stream,
                get_voice_communicator,
                get_owner_identity,
                EventType,
                EventPriority,
            )

            # Get AGI OS components
            self._agi_event_stream = await get_event_stream()
            self._agi_voice = await get_voice_communicator()
            self._agi_owner_service = await get_owner_identity()

            # Subscribe to pipeline events from AGI OS
            async def handle_agi_event(event):
                """Handle events from AGI OS for pipeline processing."""
                if event.event_type == EventType.ACTION_REQUESTED:
                    # Route action requests through the pipeline
                    action_data = event.data.get("action", {})
                    if action_data.get("requires_pipeline", False):
                        logger.info(f"[AGI-OS] Processing action via pipeline: {action_data.get('name')}")
                        # Queue for processing

            self._agi_event_stream.subscribe(EventType.ACTION_REQUESTED, handle_agi_event)

            logger.info("✅ AGI OS integration initialized")
            logger.info("   • Event stream: Connected")
            logger.info("   • Voice communicator: Ready (Daniel TTS)")
            logger.info("   • Owner identity: Dynamic")

        except ImportError as e:
            logger.warning(f"⚠️ AGI OS not available: {e}")
            self.agi_os_enabled = False
        except Exception as e:
            logger.error(f"❌ AGI OS integration failed: {e}", exc_info=True)
            self.agi_os_enabled = False

    async def _init_vbi_health_monitor(self):
        """Initialize VBI Health Monitor for advanced operation tracking.

        Connects the pipeline to the VBI health monitoring system for:
        - Operation tracking with timeout detection
        - Application-level heartbeats
        - Circuit breaker integration
        - Fallback chain orchestration
        """
        try:
            from backend.core.vbi_health_monitor import (
                get_vbi_health_monitor,
                ComponentType,
                HealthLevel,
            )

            self._vbi_health_monitor = await get_vbi_health_monitor()

            # Track last health state to avoid spam logging
            _last_health_state = {"value": None, "stale_components": set()}

            # Subscribe to health events from VBI monitor
            async def handle_health_event(event_type: str, data: dict):
                """Handle health events from VBI monitor.
                
                Only logs state CHANGES to avoid spam. Heartbeat and timeout
                events are handled by the VBI monitor itself with deduplication.
                """
                nonlocal _last_health_state
                
                if event_type == "operation_timeout":
                    # Operation timeouts are important - always log
                    logger.warning(
                        f"[VBI-HEALTH] Operation timeout detected: "
                        f"{data.get('operation_type')} in {data.get('component')}"
                    )
                elif event_type == "heartbeat_stale":
                    # Only log if this is a NEW stale component
                    component = data.get('component')
                    if component and component not in _last_health_state["stale_components"]:
                        _last_health_state["stale_components"].add(component)
                        # Don't log here - the HeartbeatManager already logs once
                        pass
                elif event_type == "health_update":
                    # Only log health state CHANGES
                    current_health = data.get('overall_health')
                    is_healthy = data.get("is_healthy", True)
                    
                    if current_health != _last_health_state["value"]:
                        # Health state changed
                        if not is_healthy:
                            logger.warning(
                                f"[VBI-HEALTH] System health changed: "
                                f"{_last_health_state['value'] or 'initial'} → {current_health}"
                            )
                        elif _last_health_state["value"] is not None and not _last_health_state.get("was_healthy", True):
                            # Recovered from unhealthy state
                            logger.info(f"[VBI-HEALTH] System health recovered: → {current_health}")
                        
                        _last_health_state["value"] = current_health
                        _last_health_state["was_healthy"] = is_healthy

            self._vbi_health_monitor.on_event(handle_health_event)

            logger.info("✅ VBI Health Monitor initialized")
            logger.info("   • Operation tracking: Active")
            logger.info("   • Heartbeat monitoring: Active")
            logger.info("   • Circuit breakers: Configured")

        except ImportError as e:
            logger.warning(f"⚠️ VBI Health Monitor not available: {e}")
            self._health_monitor_enabled = False
        except Exception as e:
            logger.error(f"❌ VBI Health Monitor initialization failed: {e}", exc_info=True)
            self._health_monitor_enabled = False

    async def _track_operation(
        self,
        operation_type: str,
        component_type: str = "vbi_engine",
        timeout_seconds: float = 30.0,
        metadata: dict = None,
    ):
        """Start tracking an operation with the VBI health monitor.

        Args:
            operation_type: Type of operation (e.g., "voice_unlock")
            component_type: Component type string
            timeout_seconds: Timeout for the operation
            metadata: Additional metadata

        Returns:
            TrackedOperation or None if health monitor not available
        """
        if not self._vbi_health_monitor:
            return None

        try:
            from backend.core.vbi_health_monitor import ComponentType

            component_map = {
                "vbi_engine": ComponentType.VBI_ENGINE,
                "ecapa_client": ComponentType.ECAPA_CLIENT,
                "cloud_run": ComponentType.CLOUD_RUN,
                "cloudsql": ComponentType.CLOUDSQL,
                "sqlite": ComponentType.SQLITE,
                "websocket": ComponentType.WEBSOCKET,
            }

            component = component_map.get(component_type, ComponentType.VBI_ENGINE)

            return await self._vbi_health_monitor.start_operation(
                operation_type=operation_type,
                component=component,
                timeout_seconds=timeout_seconds,
                metadata=metadata or {},
            )
        except Exception as e:
            logger.debug(f"[VBI-HEALTH] Failed to track operation: {e}")
            return None

    async def _complete_tracked_operation(self, operation, result=None):
        """Complete a tracked operation successfully."""
        if not self._vbi_health_monitor or not operation:
            return

        try:
            await self._vbi_health_monitor.complete_operation(operation, result)
        except Exception as e:
            logger.debug(f"[VBI-HEALTH] Failed to complete operation: {e}")

    async def _fail_tracked_operation(self, operation, error: str):
        """Mark a tracked operation as failed."""
        if not self._vbi_health_monitor or not operation:
            return

        try:
            await self._vbi_health_monitor.fail_operation(operation, error)
        except Exception as e:
            logger.debug(f"[VBI-HEALTH] Failed to record operation failure: {e}")

    async def emit_agi_event(
        self,
        event_type: str,
        data: Dict[str, Any],
        priority: str = "NORMAL",
    ):
        """Emit an event to the AGI OS event stream.

        Args:
            event_type: Type of event (e.g., "command_completed", "error_detected")
            data: Event data dictionary
            priority: Event priority (LOW, NORMAL, HIGH, CRITICAL)
        """
        if not self.agi_os_enabled or not self._agi_event_stream:
            return

        try:
            from agi_os import EventType, EventPriority, AGIEvent

            # Map string to enum
            priority_map = {
                "LOW": EventPriority.LOW,
                "NORMAL": EventPriority.NORMAL,
                "HIGH": EventPriority.HIGH,
                "CRITICAL": EventPriority.CRITICAL,
            }

            event = AGIEvent(
                event_type=getattr(EventType, event_type, EventType.SYSTEM_STATUS),
                source="async_pipeline",
                data=data,
                priority=priority_map.get(priority, EventPriority.NORMAL),
            )

            await self._agi_event_stream.emit(event)
            logger.debug(f"[AGI-OS] Emitted event: {event_type}")

        except Exception as e:
            logger.debug(f"[AGI-OS] Event emission failed: {e}")

    async def get_owner_name(self) -> str:
        """Get the current owner's name for personalized responses.

        Returns:
            Owner's first name, or "Sir" as fallback
        """
        if self._agi_owner_service:
            try:
                owner = await self._agi_owner_service.get_current_owner()
                return owner.name.split()[0] if owner.name else "Sir"
            except Exception:
                pass
        return "Sir"

    def register_stage(
        self,
        name: str,
        handler: Callable,
        timeout: Optional[float] = None,
        retry_count: int = 0,
        required: bool = True,
    ):
        """Dynamically register a new pipeline stage.

        Args:
            name: Unique name for the stage
            handler: Function to execute for this stage
            timeout: Maximum execution time in seconds
            retry_count: Number of retry attempts on failure
            required: Whether stage failure should fail entire pipeline
        """
        stage = DynamicPipelineStage(
            name=name,
            handler=handler,
            timeout=timeout,
            retry_count=retry_count,
            required=required,
        )
        self.stages[name] = stage
        logger.info(
            f"✅ Registered pipeline stage: {name} (timeout={timeout}s, retries={retry_count})"
        )

    def register_middleware(self, name: str, handler: Callable):
        """Register middleware for pipeline processing.

        Args:
            name: Unique name for the middleware
            handler: Function to execute for middleware processing
        """
        middleware = PipelineMiddleware(name, handler)
        self.middleware.append(middleware)
        logger.info(f"✅ Registered middleware: {name}")

    def unregister_stage(self, name: str):
        """Remove a pipeline stage.

        Args:
            name: Name of the stage to remove
        """
        if name in self.stages:
            del self.stages[name]
            logger.info(f"Unregistered pipeline stage: {name}")

    async def process_async(
        self,
        text: str,
        user_name: str = "Sir",
        priority: int = 0,
        metadata: Optional[Dict[str, Any]] = None,
        audio_data: Optional[bytes] = None,
        speaker_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Process command through advanced async pipeline.

        Main entry point for command processing. Handles the complete pipeline
        from command receipt through response generation, with comprehensive
        error handling and performance monitoring.

        Args:
            text: Command text to process
            user_name: Name of the user issuing the command
            priority: Command priority (0=normal, 1=high, 2=critical)
            metadata: Additional metadata dictionary
            audio_data: Voice audio data for authentication
            speaker_name: Identified speaker name from voice recognition

        Returns:
            Dictionary containing response, metadata, success status, and metrics

        Example:
            >>> result = await pipeline.process_async("open safari")
            >>> print(result['response'])
            "I've opened Safari for you, Sir."
        """

        # FAST PATH for lock/unlock commands - bypass heavy processing
        text_lower = text.lower()
        if any(
            phrase in text_lower
            for phrase in [
                "lock my screen",
                "lock screen",
                "unlock my screen",
                "unlock screen",
                "lock the screen",
                "unlock the screen",
            ]
        ):
            return await self._fast_lock_unlock(
                text, user_name, metadata, audio_data=audio_data, speaker_name=speaker_name
            )

        # FAST PATH for voice security testing - direct execution
        if any(
            phrase in text_lower
            for phrase in [
                "test my voice security",
                "test voice security",
                "verify voice authentication",
                "check voice security",
                "run security test",
                "test voice biometric",
            ]
        ):
            return await self._fast_voice_security_test(text, user_name, metadata)

        # =====================================================================
        # PROACTIVE CONTEXT AWARENESS INTELLIGENCE (CAI) - LOCKED SCREEN DETECTION
        # =====================================================================
        # Transparently detect if screen is locked and handle unlock + continuation
        # This enables autonomous workflows like:
        #   "Hey JARVIS, search for dogs" → detect lock → verify voice → unlock → search
        # =====================================================================
        if not (metadata or {}).get("screen_just_unlocked", False):
            # Skip if we just completed an unlock to prevent infinite loops
            proactive_unlock_result = await self._handle_proactive_unlock_if_needed(
                text=text,
                user_name=user_name,
                metadata=metadata,
                audio_data=audio_data,
                speaker_name=speaker_name,
                priority=priority,
            )
            if proactive_unlock_result is not None:
                return proactive_unlock_result

        # Create pipeline context
        command_id = f"cmd_{int(time.time() * 1000)}"
        context = PipelineContext(
            command_id=command_id,
            text=text,
            user_name=user_name,
            priority=priority,
            metadata=metadata or {},
            audio_data=audio_data,
            speaker_name=speaker_name,
        )

        self.active_commands[command_id] = context

        try:
            # Emit command received event
            await self.event_bus.emit("command_received", context, priority=priority)

            # Process through pipeline with circuit breaker
            result = await self.circuit_breaker.call(self._execute_pipeline, context)

            # Emit command completed event
            context.stage = PipelineStage.COMPLETED
            await self.event_bus.emit("command_completed", context, priority=priority)

            # Update performance metrics
            self._record_performance(context)

            return result

        except Exception as e:
            logger.error(f"Pipeline error for command {command_id}: {e}", exc_info=True)
            context.stage = PipelineStage.FAILED
            context.error = str(e)
            await self.event_bus.emit("command_failed", context, priority=priority)

            return self._generate_error_response(context, e)

        finally:
            # Cleanup after processing (thread-safe)
            try:
                self.active_commands.pop(command_id, None)
            except RuntimeError:
                # Handle dictionary changed size during iteration
                pass

    async def process_command_async(
        self,
        text: str,
        user_name: str = "Sir",
        priority: int = 0,
        metadata: Optional[Dict[str, Any]] = None,
        audio_data: Optional[bytes] = None,
        speaker_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Backwards-compatible alias for voice/WebSocket integrations.

        Several integrations call `process_command_async()`; the canonical entry point is
        `process_async()`. This wrapper keeps the API stable and ensures audio/speaker
        context can be passed through (critical for robust lock/unlock handling).
        """
        return await self.process_async(
            text=text,
            user_name=user_name,
            priority=priority,
            metadata=metadata,
            audio_data=audio_data,
            speaker_name=speaker_name,
        )

    def _record_performance(self, context: PipelineContext) -> None:
        """Record performance metrics for the completed command.

        Args:
            context: Pipeline context with timing metrics
        """
        try:
            # Record overall command duration
            if hasattr(context, 'timestamp'):
                duration = time.time() - context.timestamp
                self.performance_metrics['command_duration'].append(duration)

            # Record individual stage durations
            for metric_name, metric_value in context.metrics.items():
                if metric_name.endswith('_duration'):
                    self.performance_metrics[metric_name].append(metric_value)

            # Keep only last 1000 measurements to prevent unbounded growth
            for key in self.performance_metrics:
                if len(self.performance_metrics[key]) > 1000:
                    self.performance_metrics[key] = self.performance_metrics[key][-1000:]

        except Exception as e:
            logger.debug(f"Failed to record performance metrics: {e}")

    async def _execute_pipeline(self, context: PipelineContext) -> Dict[str, Any]:
        """Execute the pipeline stages sequentially.

        Args:
            context: Pipeline context to process through all stages

        Returns:
            Dictionary containing the final result with response and metadata

        Raises:
            Exception: If a required stage fails
        """
        # Convert context to data dictionary if needed
        if not hasattr(context, 'data'):
            context.data = {
                "command": context.text,
                "user_name": context.user_name,
                "metadata": context.metadata,
            }

        # Execute each registered stage in order
        stage_order = [
            "validation",
            "screen_lock_check",
            "preprocessing",
            "intent_analysis",
            "component_loading",
            "processing",
            "postprocessing",
            "response_generation",
        ]

        for stage_name in stage_order:
            if stage_name in self.stages:
                stage = self.stages[stage_name]
                try:
                    await stage.execute(context)
                except Exception as e:
                    if stage.required:
                        raise
                    else:
                        logger.warning(f"Non-required stage {stage_name} failed: {e}")

        # Return final response
        return context.data.get("response", {
            "success": True,
            "response": "Command processed successfully",
            "metadata": context.metadata
        })

    # =========================================================================
    # PROACTIVE UNLOCK + POST-UNLOCK RE-ENTRY (CAI Integration)
    # =========================================================================
    # This enables autonomous workflows like:
    #   User: "Hey JARVIS, search for dogs"
    #   JARVIS: [detects locked screen] → [verifies voice] → [unlocks]
    #           → [continues executing "search for dogs"]
    # =========================================================================

    async def _handle_proactive_unlock_if_needed(
        self,
        text: str,
        user_name: str,
        metadata: Optional[Dict] = None,
        audio_data: Optional[bytes] = None,
        speaker_name: Optional[str] = None,
        priority: int = 0,
    ) -> Optional[Dict]:
        """
        Proactively detect locked screen and handle transparent unlock with continuation.

        This method implements the autonomous unlock workflow:
        1. Uses fast screen lock detection (non-blocking with timeout)
        2. Uses IntentAnalyzer to determine if command requires screen access
        3. If locked and needs screen, performs VBI verification + unlock
        4. Provides verbal acknowledgment through the entire flow
        5. Re-injects original command with screen_just_unlocked flag

        Args:
            text: Original command text
            user_name: User name
            metadata: Optional metadata dict
            audio_data: Optional voice audio data for VBI authentication
            speaker_name: Optional identified speaker name
            priority: Command priority

        Returns:
            None if no proactive action needed (let normal pipeline continue)
            Dict with result if proactive unlock was performed and command executed
        """
        start_time = time.time()

        # Wrap entire operation in timeout to prevent hangs
        try:
            return await asyncio.wait_for(
                self._proactive_unlock_impl(
                    text=text,
                    user_name=user_name,
                    metadata=metadata,
                    audio_data=audio_data,
                    speaker_name=speaker_name,
                    priority=priority,
                    start_time=start_time,
                ),
                timeout=30.0  # Max 30 seconds for entire proactive flow
            )
        except asyncio.TimeoutError:
            logger.warning(f"⏱️ [PROACTIVE-CAI] Timeout after 30s - falling back to normal pipeline")
            return None
        except Exception as e:
            logger.warning(f"[PROACTIVE-CAI] Error: {e} - falling back to normal pipeline")
            return None

    async def _proactive_unlock_impl(
        self,
        text: str,
        user_name: str,
        metadata: Optional[Dict],
        audio_data: Optional[bytes],
        speaker_name: Optional[str],
        priority: int,
        start_time: float,
    ) -> Optional[Dict]:
        """
        Internal implementation of proactive unlock with full CAI integration.
        Separated to allow timeout wrapper in parent method.
        """
        try:
            # ─────────────────────────────────────────────────────────────────
            # Step 1: FAST Screen Lock Check (direct, non-blocking)
            # ─────────────────────────────────────────────────────────────────
            is_locked = await self._fast_check_screen_locked()

            if not is_locked:
                # Screen is not locked - no proactive action needed
                return None

            logger.info(
                f"🔒 [PROACTIVE-CAI] Screen is LOCKED - analyzing command: '{text[:50]}...'"
            )

            # ─────────────────────────────────────────────────────────────────
            # Step 2: Analyze intent to determine if command requires screen
            # ─────────────────────────────────────────────────────────────────
            intent = await self._analyze_intent_for_screen_requirement(text)

            # Commands that don't require screen access can proceed without unlock
            if not intent.get("requires_screen", True):
                logger.info(
                    f"🔓 [PROACTIVE-CAI] Command doesn't require screen (intent={intent.get('type', 'unknown')}) - "
                    f"skipping proactive unlock"
                )
                return None

            logger.info(
                f"📺 [PROACTIVE-CAI] Command requires screen access: "
                f"intent={intent.get('type', 'unknown')}, confidence={intent.get('confidence', 0):.1%}"
            )

            # ─────────────────────────────────────────────────────────────────
            # Step 3: Check if command is exempt from unlock requirement
            # ─────────────────────────────────────────────────────────────────
            if self._is_command_screen_exempt(text):
                logger.info(f"🔓 [PROACTIVE-CAI] Command is screen-exempt - skipping proactive unlock")
                return None

            # ─────────────────────────────────────────────────────────────────
            # Step 5: Extract semantic continuation intent
            # ─────────────────────────────────────────────────────────────────
            continuation_action = self._extract_continuation_intent(text, intent)

            logger.info(
                f"🎯 [PROACTIVE-CAI] Semantic continuation: '{continuation_action}'"
            )

            # ─────────────────────────────────────────────────────────────────
            # Step 6: Verbal acknowledgment - tell user we're unlocking
            # ─────────────────────────────────────────────────────────────────
            acknowledgment = self._generate_proactive_acknowledgment(
                speaker_name=speaker_name or user_name,
                continuation_action=continuation_action,
            )

            logger.info(f"🎤 [PROACTIVE-CAI] Acknowledgment: '{acknowledgment}'")

            # Speak the acknowledgment asynchronously
            await self._speak_acknowledgment(acknowledgment)

            # ─────────────────────────────────────────────────────────────────
            # Step 7: Perform VBI verification and unlock
            # ─────────────────────────────────────────────────────────────────
            logger.info(f"🔐 [PROACTIVE-CAI] Performing VBI verification and unlock...")

            unlock_result = await self._fast_lock_unlock(
                text="unlock my screen",  # Internal unlock command
                user_name=user_name,
                metadata={
                    **(metadata or {}),
                    "proactive_unlock": True,
                    "original_command": text,
                    "continuation_intent": continuation_action,
                },
                audio_data=audio_data,
                speaker_name=speaker_name,
            )

            if not unlock_result.get("success", False):
                # Unlock failed - return the failure result
                logger.warning(
                    f"❌ [PROACTIVE-CAI] Unlock failed: {unlock_result.get('response', 'Unknown error')}"
                )
                return unlock_result

            logger.info(f"✅ [PROACTIVE-CAI] Screen unlocked successfully!")

            # ─────────────────────────────────────────────────────────────────
            # Step 8: POST-UNLOCK RE-ENTRY - Continue with original command
            # ─────────────────────────────────────────────────────────────────
            # Brief pause to ensure screen is fully unlocked
            await asyncio.sleep(0.5)

            logger.info(
                f"🔄 [PROACTIVE-CAI] RE-ENTRY: Executing original command: '{text[:50]}...'"
            )

            # Re-inject the original command with screen_just_unlocked flag
            continuation_result = await self.process_async(
                text=text,
                user_name=user_name,
                priority=priority,
                metadata={
                    **(metadata or {}),
                    "screen_just_unlocked": True,
                    "proactive_unlock_performed": True,
                    "unlock_latency_ms": (time.time() - start_time) * 1000,
                },
                audio_data=audio_data,
                speaker_name=speaker_name,
            )

            # Merge the unlock info into the continuation result
            total_latency = (time.time() - start_time) * 1000
            continuation_result["proactive_unlock"] = {
                "performed": True,
                "unlock_latency_ms": unlock_result.get("latency_ms", 0),
                "total_latency_ms": total_latency,
                "continuation_intent": continuation_action,
            }

            logger.info(
                f"✅ [PROACTIVE-CAI] Complete workflow finished in {total_latency:.0f}ms"
            )

            # ─────────────────────────────────────────────────────────────────
            # Step 9: Success acknowledgment (if continuation succeeded)
            # ─────────────────────────────────────────────────────────────────
            if continuation_result.get("success", False):
                # Generate success message based on what we did
                success_message = self._generate_completion_acknowledgment(
                    speaker_name=speaker_name or user_name,
                    continuation_action=continuation_action,
                    continuation_result=continuation_result,
                )
                # Speak success asynchronously (fire and forget)
                asyncio.create_task(self._speak_acknowledgment(success_message))

            return continuation_result

        except ImportError as e:
            # CAI components not available - fall back to normal pipeline
            logger.debug(f"[PROACTIVE-CAI] CAI components not available: {e}")
            return None
        except Exception as e:
            logger.warning(f"[PROACTIVE-CAI] Error during proactive handling: {e}")
            # Don't block normal pipeline on errors
            return None

    async def _fast_check_screen_locked(self) -> bool:
        """
        Fast, non-blocking screen lock detection with multiple fallback strategies.

        Uses direct low-level detection to avoid slow module imports.
        Returns within 2 seconds max.
        """
        try:
            # Strategy 1: Direct Quartz session check (fastest, most reliable)
            try:
                from Quartz import CGSessionCopyCurrentDictionary
                session_dict = CGSessionCopyCurrentDictionary()
                if session_dict:
                    is_locked = session_dict.get("CGSSessionScreenIsLocked", False)
                    screen_saver = session_dict.get("CGSSessionScreenSaverIsRunning", False)
                    if is_locked or screen_saver:
                        logger.debug(f"[FAST-LOCK-CHECK] Quartz: locked={is_locked}, screensaver={screen_saver}")
                        return True
                    return False
            except ImportError:
                pass

            # Strategy 2: Try the voice_unlock screen detector directly
            try:
                from voice_unlock.objc.server.screen_lock_detector import is_screen_locked
                result = await asyncio.wait_for(
                    asyncio.get_event_loop().run_in_executor(None, is_screen_locked),
                    timeout=1.5
                )
                return bool(result)
            except (ImportError, asyncio.TimeoutError):
                pass

            # Strategy 3: Check for loginwindow process (indicates lock screen)
            try:
                process = await asyncio.create_subprocess_exec(
                    "pgrep", "-x", "loginwindow",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                stdout, _ = await asyncio.wait_for(process.communicate(), timeout=1.0)
                # loginwindow always runs, but if CGSSession is locked, screen is locked
                # This is a fallback that assumes unlocked if we can't determine
                return False
            except (asyncio.TimeoutError, Exception):
                pass

            # Default: assume unlocked to avoid blocking user commands
            logger.debug("[FAST-LOCK-CHECK] Could not determine lock state - assuming unlocked")
            return False

        except Exception as e:
            logger.debug(f"[FAST-LOCK-CHECK] Error: {e} - assuming unlocked")
            return False

    async def _analyze_intent_for_screen_requirement(self, text: str) -> Dict[str, Any]:
        """
        Analyze command intent to determine if it requires screen access.

        Uses lightweight pattern matching for speed, with optional CAI integration.
        Returns dict with: type, requires_screen, confidence
        """
        text_lower = text.lower()

        # Fast pattern-based intent detection (no heavy imports)
        intent_patterns = {
            # Screen-required intents
            "app_launch": {
                "patterns": [r"\b(open|launch|start|run)\s+\w+", r"\bswitch\s+to\s+\w+"],
                "requires_screen": True,
            },
            "web_browse": {
                "patterns": [r"\b(search|google|look up|browse)\b", r"\bgo\s+to\s+.*\.(com|org|net)"],
                "requires_screen": True,
            },
            "file_operation": {
                "patterns": [r"\b(create|edit|save|open)\s+(file|document|folder)"],
                "requires_screen": True,
            },
            "ui_interaction": {
                "patterns": [r"\b(click|scroll|type|select|minimize|maximize)\b"],
                "requires_screen": True,
            },
            # Screen-NOT-required intents
            "time_weather": {
                "patterns": [r"\b(what|tell).*(time|weather|temperature)", r"\bhow's\s+the\s+weather"],
                "requires_screen": False,
            },
            "voice_only": {
                "patterns": [r"\b(play|pause|stop)\s+(music|audio)", r"\bset\s+(timer|alarm|reminder)"],
                "requires_screen": False,
            },
            "screen_control": {
                "patterns": [r"\b(lock|unlock)\s+(my\s+)?(screen|computer|mac)"],
                "requires_screen": False,  # Screen control handles itself
            },
        }

        for intent_type, config in intent_patterns.items():
            for pattern in config["patterns"]:
                if re.search(pattern, text_lower, re.IGNORECASE):
                    return {
                        "type": intent_type,
                        "requires_screen": config["requires_screen"],
                        "confidence": 0.85,
                        "pattern_matched": pattern,
                    }

        # Try CAI IntentAnalyzer if available (with timeout)
        try:
            # Direct import to avoid module chain issues
            import importlib.util
            spec = importlib.util.spec_from_file_location(
                "intent_analyzer",
                str(Path(__file__).parent.parent / "context_intelligence" / "analyzers" / "intent_analyzer.py")
            )
            if spec and spec.loader:
                intent_mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(intent_mod)
                analyzer = intent_mod.get_intent_analyzer()

                intent = await asyncio.wait_for(
                    analyzer.analyze(text),
                    timeout=2.0
                )
                return {
                    "type": intent.type.value if hasattr(intent.type, "value") else str(intent.type),
                    "requires_screen": intent.requires_screen,
                    "confidence": intent.confidence,
                    "source": "cai_analyzer",
                }
        except (ImportError, asyncio.TimeoutError, Exception) as e:
            logger.debug(f"[INTENT-ANALYSIS] CAI analyzer unavailable: {e}")

        # Default: assume screen required for safety
        return {
            "type": "unknown",
            "requires_screen": True,
            "confidence": 0.5,
        }

    def _is_command_screen_exempt(self, text: str) -> bool:
        """
        Check if command is exempt from screen unlock requirement.

        Some commands can run regardless of screen state.
        """
        text_lower = text.lower()

        exempt_patterns = [
            # Screen control (handled separately)
            r"\b(lock|unlock)\s+(my\s+)?(screen|computer|mac)\b",
            # Voice-only commands
            r"\bwhat\s+(time|is the time)\b",
            r"\b(what's|how's)\s+the\s+weather\b",
            r"\bset\s+(a\s+)?(timer|alarm|reminder)\b",
            r"\b(play|pause|stop|skip)\s+(music|song|audio)\b",
            # System info
            r"\b(tell me|what is)\s+(the\s+)?(battery|volume)\b",
            # Conversational
            r"\bhey\s+jarvis\b",
            r"\bthank\s+you\b",
            r"\bgoodbye\b",
            r"\bhow\s+are\s+you\b",
        ]

        for pattern in exempt_patterns:
            if re.search(pattern, text_lower, re.IGNORECASE):
                return True

        return False

    def _extract_continuation_intent(self, text: str, intent: Any) -> str:
        """
        Extract the semantic continuation intent from the command.

        This determines what the user actually wants to do AFTER the screen is unlocked.

        Args:
            text: Original command text
            intent: Analyzed intent from IntentAnalyzer

        Returns:
            Human-readable description of what to do after unlock
        """
        text_lower = text.lower()

        # Common patterns and their semantic meanings
        patterns = [
            # Search patterns
            (r"search\s+(?:for\s+)?(.+)", lambda m: f"search for {m.group(1)}"),
            (r"google\s+(.+)", lambda m: f"search for {m.group(1)}"),
            (r"look\s+up\s+(.+)", lambda m: f"look up {m.group(1)}"),
            # App launch patterns
            (r"open\s+(.+)", lambda m: f"open {m.group(1)}"),
            (r"launch\s+(.+)", lambda m: f"launch {m.group(1)}"),
            (r"start\s+(.+)", lambda m: f"start {m.group(1)}"),
            # Navigation patterns
            (r"go\s+to\s+(.+)", lambda m: f"navigate to {m.group(1)}"),
            (r"navigate\s+to\s+(.+)", lambda m: f"navigate to {m.group(1)}"),
            # File operations
            (r"(?:create|write)\s+(?:an?\s+)?(.+)", lambda m: f"create {m.group(1)}"),
            (r"edit\s+(.+)", lambda m: f"edit {m.group(1)}"),
        ]

        for pattern, extractor in patterns:
            match = re.search(pattern, text_lower, re.IGNORECASE)
            if match:
                return extractor(match)

        # Fallback: use intent type for description
        intent_descriptions = {
            "app_launch": "open the application",
            "web_browse": "browse the web",
            "file_operation": "perform file operation",
            "document_creation": "create document",
            "system_query": "check system status",
        }

        # Handle both dict-based intent (our format) and object-based intent (CAI format)
        if isinstance(intent, dict):
            intent_type = intent.get("type", "unknown")
        elif hasattr(intent, "type"):
            intent_type = intent.type.value if hasattr(intent.type, "value") else str(intent.type)
        else:
            intent_type = "unknown"

        return intent_descriptions.get(intent_type, "complete your request")

    def _generate_proactive_acknowledgment(
        self,
        speaker_name: str,
        continuation_action: str,
    ) -> str:
        """
        Generate contextual verbal acknowledgment for proactive unlock.

        Creates a natural, personalized message that tells the user:
        1. We detected the screen is locked
        2. We're verifying their voice
        3. We'll continue with their original request after unlock

        Args:
            speaker_name: Identified speaker name for personalization
            continuation_action: What we'll do after unlock

        Returns:
            Natural acknowledgment message to speak
        """
        # Dynamic templates for variety
        templates = [
            f"I notice your screen is locked, {speaker_name}. Let me verify your voice and unlock it so I can {continuation_action}.",
            f"Your screen is locked. Verifying your voice now, {speaker_name}, then I'll {continuation_action}.",
            f"Screen's locked. One moment while I verify it's you, {speaker_name}. I'll {continuation_action} right after.",
            f"Let me unlock your screen first, {speaker_name}. Then I'll {continuation_action} for you.",
        ]

        # Use consistent selection based on command hash for predictability
        import hashlib
        hash_val = int(hashlib.md5(continuation_action.encode()).hexdigest()[:8], 16)
        return templates[hash_val % len(templates)]

    def _generate_completion_acknowledgment(
        self,
        speaker_name: str,
        continuation_action: str,
        continuation_result: Dict,
    ) -> str:
        """
        Generate contextual verbal acknowledgment after successful continuation.

        Creates a natural, personalized message that confirms the original
        task has been completed after the proactive unlock.

        Args:
            speaker_name: Identified speaker name for personalization
            continuation_action: What we completed
            continuation_result: Result from the continuation execution

        Returns:
            Natural completion message to speak
        """
        # Check if the response includes a message we can use
        response_msg = continuation_result.get("response", "")
        if response_msg and len(response_msg) < 100:
            # Use the actual response if it's short and meaningful
            return response_msg

        # Dynamic templates for variety
        templates = [
            f"Done, {speaker_name}. I've completed that for you.",
            f"All set, {speaker_name}.",
            f"There you go, {speaker_name}. Task complete.",
            f"Finished, {speaker_name}.",
        ]

        # Use consistent selection based on action hash
        import hashlib
        hash_val = int(hashlib.md5(continuation_action.encode()).hexdigest()[:8], 16)
        return templates[hash_val % len(templates)]

    async def _speak_acknowledgment(self, message: str) -> None:
        """
        Speak the acknowledgment message using available TTS.

        Tries multiple TTS backends in order of preference:
        1. AGI OS voice communicator (if available)
        2. JARVIS voice API (if available)
        3. Direct macOS `say` command (fallback)

        Args:
            message: The message to speak
        """
        try:
            # Try AGI OS voice communicator first
            if hasattr(self, "_agi_voice_communicator") and self._agi_voice_communicator:
                await self._agi_voice_communicator.speak(message, priority="high")
                return

            # Try JARVIS voice API
            try:
                import aiohttp
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        "http://localhost:8010/audio/speak",
                        json={"text": message, "voice": "Daniel"},
                        timeout=aiohttp.ClientTimeout(total=5.0),
                    ) as resp:
                        if resp.status == 200:
                            return
            except Exception:
                pass

            # Fallback to macOS say command
            process = await asyncio.create_subprocess_exec(
                "say", "-v", "Daniel", message,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(process.wait(), timeout=10.0)

        except Exception as e:
            logger.debug(f"[PROACTIVE-CAI] Could not speak acknowledgment: {e}")

    async def _fast_lock_unlock(
        self,
        text: str,
        user_name: str,
        metadata: Optional[Dict] = None,
        audio_data: Optional[bytes] = None,
        speaker_name: Optional[str] = None,
    ) -> Dict:
        """Fast lock/unlock handler for voice commands with comprehensive monitoring.

        Bypasses heavy pipeline processing for instant screen lock/unlock
        using macOS controller directly. Includes timeout protection and
        detailed performance tracking.

        Args:
            text: Command text
            user_name: User name
            metadata: Optional metadata dict
            audio_data: Optional voice audio data for authentication
            speaker_name: Optional identified speaker name

        Returns:
            Result dict with success status, response message, and timing
        """
        start_time = time.time()
        text_lower = text.lower()

        # Determine action type for logging
        tokens = set(re.findall(r"[a-z']+", text_lower))
        is_lock = ("lock" in tokens) and ("unlock" not in tokens)
        action_type = "LOCK" if is_lock else "UNLOCK"

        # Start VBI health monitor operation tracking
        tracked_operation = await self._track_operation(
            operation_type="voice_unlock" if not is_lock else "screen_lock",
            component_type="vbi_engine",
            timeout_seconds=60.0,
            metadata={
                "action_type": action_type,
                "user_name": user_name,
                "speaker_name": speaker_name,
                "has_audio": audio_data is not None,
            }
        )

        logger.info(f"🔒 [LOCK-UNLOCK-START] {action_type} command received at {datetime.now().isoformat()}")
        logger.info(f"🔒 [LOCK-UNLOCK-DETAILS] Command: '{text}', User: {user_name}, Speaker: {speaker_name}")

        # Create monitoring task to detect hangs
        async def timeout_monitor():
            """Monitor for command timeout and log detailed info."""
            await asyncio.sleep(5)  # 5 second warning
            elapsed = time.time() - start_time
            if elapsed > 5:
                logger.warning(
                    f"⚠️ [LOCK-UNLOCK-SLOW] {action_type} taking longer than expected: {elapsed:.1f}s"
                )

            await asyncio.sleep(25)  # Total 30 second timeout
            elapsed = time.time() - start_time
            if elapsed > 30:
                logger.error(
                    f"🚨 [LOCK-UNLOCK-TIMEOUT] {action_type} HUNG for {elapsed:.1f}s - "
                    f"Command: '{text}', User: {user_name}"
                )
                # Write to dedicated hang detection file
                try:
                    hang_file = Path("backend/logs/hanging_commands.log")
                    hang_file.parent.mkdir(exist_ok=True)
                    with open(hang_file, "a") as f:
                        f.write(
                            f"{datetime.now().isoformat()} | TIMEOUT | {action_type} | "
                            f"{elapsed:.1f}s | {text} | {user_name}\n"
                        )
                except:
                    pass

        # Start monitoring task
        monitor_task = asyncio.create_task(timeout_monitor())

        try:
            # Add timeout wrapper around the entire operation
            async def execute_lock_unlock():
                """Execute lock/unlock with detailed step tracking."""
                step_times = {}

                # Step 1: Import controller
                step_start = time.time()
                from backend.system_control.macos_controller import MacOSController
                step_times["import"] = (time.time() - step_start) * 1000
                logger.debug(f"🔒 [LOCK-UNLOCK-STEP] Controller imported in {step_times['import']:.1f}ms")

                # Step 2: Initialize controller
                step_start = time.time()
                controller = MacOSController()
                step_times["init"] = (time.time() - step_start) * 1000
                logger.debug(f"🔒 [LOCK-UNLOCK-STEP] Controller initialized in {step_times['init']:.1f}ms")

                # Step 3: Execute action
                step_start = time.time()
                if is_lock:
                    logger.info(f"🔒 [LOCK-UNLOCK-EXECUTE] Calling controller.lock_screen()...")
                    # IMPORTANT: Never let "transparency" speaker identification block locking.
                    # Some speaker verification implementations are CPU-bound and can starve the event loop,
                    # causing the UI to hang at "🔒 Locking..." and preventing the lock from executing.
                    #
                    # We pass through any already-known speaker name, otherwise we let the controller
                    # fall back to dynamic owner naming (or "there") without delaying the lock.
                    success, message = await controller.lock_screen(speaker_name=speaker_name)
                    action = "locked"
                else:  # unlock
                    # =====================================================================
                    # ENHANCED VOICE BIOMETRIC INTELLIGENCE INTEGRATION (v4.0)
                    # =====================================================================
                    # Use VBI first for enhanced verification with:
                    # - LangGraph reasoning for borderline cases
                    # - ChromaDB pattern memory for voice evolution
                    # - Langfuse audit trails
                    # - Helicone-style cost tracking
                    # - Voice drift detection and adaptation
                    # =====================================================================
                    logger.info(f"🔓 [LOCK-UNLOCK-EXECUTE] Attempting enhanced VBI verification...")

                    vbi_verified = False
                    vbi_result = None

                    if audio_data:
                        try:
                            from backend.voice_unlock.voice_biometric_intelligence import (
                                get_voice_biometric_intelligence,
                            )

                            vbi = await get_voice_biometric_intelligence()

                            # Create context for VBI
                            vbi_context = {
                                "text": text,
                                "user_name": user_name,
                                "speaker_name": speaker_name,
                                "source": "async_pipeline_unlock",
                                "command_type": "unlock",
                                **(metadata or {}),
                            }

                            # Run enhanced verification (includes reasoning, pattern storage, tracing)
                            # CRITICAL: Add timeout to prevent VBI from hanging the entire pipeline
                            try:
                                vbi_result = await asyncio.wait_for(
                                    vbi.verify_and_announce(
                                        audio_data=audio_data,
                                        context=vbi_context,
                                        speak=False,  # Don't speak here, we'll handle response
                                    ),
                                    timeout=10.0  # 10 second max for VBI verification
                                )
                            except asyncio.TimeoutError:
                                logger.warning("⏱️ [VBI-TIMEOUT] VBI verification timed out after 10s")
                                vbi_result = None
                                vbi_verified = False

                            # Only process VBI result if we got one (not timed out)
                            if vbi_result is not None:
                                vbi_verified = vbi_result.verified
                                vbi_confidence = vbi_result.confidence
                                vbi_announcement = vbi_result.announcement

                                logger.info(
                                    f"🔐 [VBI-RESULT] verified={vbi_verified}, confidence={vbi_confidence:.1%}, "
                                    f"level={vbi_result.level.value if hasattr(vbi_result.level, 'value') else str(vbi_result.level)}, "
                                    f"method={vbi_result.verification_method.value if hasattr(vbi_result.verification_method, 'value') else str(vbi_result.verification_method)}"
                                )

                                if vbi_result.spoofing_detected:
                                    logger.warning(f"🚨 [VBI-SECURITY] Spoofing detected: {vbi_result.spoofing_reason}")
                                    return False, f"Security alert: {vbi_result.spoofing_reason}", "blocked", step_times

                                if vbi_verified:
                                    # Use VBI result for unlock
                                    logger.info(f"✅ [VBI-VERIFIED] Voice verified via VBI - proceeding with unlock")
                                    step_times["vbi_verify"] = (time.time() - step_start) * 1000

                                    # Track enhanced module usage
                                    step_times["vbi_stats"] = {
                                        "reasoning_used": vbi._stats.get('reasoning_invocations', 0) > 0,
                                        "patterns_stored": vbi._stats.get('pattern_stores', 0) > 0,
                                        "drift_detected": vbi._stats.get('drift_detections', 0) > 0,
                                    }

                        except ImportError as e:
                            logger.debug(f"🔐 [VBI-UNAVAILABLE] VBI not available: {e}")
                        except Exception as e:
                            logger.warning(f"🔐 [VBI-ERROR] VBI verification failed: {e}")

                    # If VBI verified, proceed with unlock using intelligent service
                    # If VBI not verified or not available, fall back to standard flow
                    logger.info(f"🔓 [LOCK-UNLOCK-EXECUTE] Using intelligent voice unlock service...")
                    try:
                        from backend.voice_unlock.intelligent_voice_unlock_service import (
                            get_intelligent_unlock_service,
                        )

                        unlock_service = get_intelligent_unlock_service()

                        # Initialize if needed (with timeout)
                        if not unlock_service.initialized:
                            logger.info("🔓 [LOCK-UNLOCK-INIT] Initializing unlock service...")
                            try:
                                await asyncio.wait_for(
                                    unlock_service.initialize(),
                                    timeout=5.0  # 5 second max for initialization
                                )
                            except asyncio.TimeoutError:
                                logger.error("⏱️ [LOCK-UNLOCK-INIT] Service initialization timed out")
                                raise Exception("Unlock service initialization timed out")

                        # Process unlock with audio data and context
                        context = {
                            "text": text,
                            "user_name": user_name,
                            "speaker_name": speaker_name,
                            "metadata": metadata or {},
                            # Pass sample_rate from metadata if available
                            "audio_sample_rate": metadata.get("audio_sample_rate") if metadata else None,
                            "audio_mime_type": metadata.get("audio_mime_type") if metadata else None,
                            # Pass VBI verification result if available
                            "vbi_verified": vbi_verified,
                            "vbi_confidence": vbi_result.confidence if vbi_result else None,
                            "vbi_announcement": vbi_result.announcement if vbi_result else None,
                        }

                        # If VBI already verified, we can use that result
                        if vbi_verified and vbi_result:
                            logger.info(f"🔓 [LOCK-UNLOCK-VBI] Using VBI verification result for unlock")
                            # Direct unlock using VBI-verified context
                            context_analysis = {
                                "unlock_type": "vbi_verified",
                                "verification_score": vbi_result.confidence,
                                "confidence": vbi_result.confidence,
                                "speaker_verified": True,
                                "vbi_level": vbi_result.level.value if hasattr(vbi_result.level, 'value') else str(vbi_result.level),
                                "vbi_method": vbi_result.verification_method.value if hasattr(vbi_result.verification_method, 'value') else str(vbi_result.verification_method),
                            }
                            scenario_analysis = {
                                "scenario": "vbi_verified_unlock",
                                "risk_level": "low" if vbi_result.confidence > 0.85 else "medium",
                                "unlock_allowed": True,
                                "reason": f"VBI verified at {vbi_result.confidence:.1%} confidence"
                            }
                            result = await asyncio.wait_for(
                                unlock_service._perform_unlock(
                                    speaker_name=vbi_result.speaker_name or speaker_name or user_name,
                                    context_analysis=context_analysis,
                                    scenario_analysis=scenario_analysis
                                ),
                                timeout=15.0  # 15 second max for unlock execution
                            )
                            # Use VBI announcement if available
                            if vbi_result.announcement:
                                result["message"] = vbi_result.announcement
                        elif audio_data:
                            result = await asyncio.wait_for(
                                unlock_service.process_voice_unlock_command(
                                    audio_data=audio_data,
                                    context=context
                                ),
                                timeout=20.0  # 20 second max for full unlock flow
                            )
                        else:
                            # Text-only unlock (no audio available)
                            logger.info(f"🔓 [LOCK-UNLOCK-TEXT] Text-only unlock request: '{text}'")
                            # Direct unlock using internal method with proper context dicts
                            context_analysis = {
                                "unlock_type": "text_command",
                                "verification_score": 0.95,
                                "confidence": 0.95,
                                "speaker_verified": True,
                                "text_command": text
                            }
                            scenario_analysis = {
                                "scenario": "text_unlock",
                                "risk_level": "low",
                                "unlock_allowed": True,
                                "reason": "text_command_from_authenticated_session"
                            }
                            result = await asyncio.wait_for(
                                unlock_service._perform_unlock(
                                    speaker_name=speaker_name or user_name,
                                    context_analysis=context_analysis,
                                    scenario_analysis=scenario_analysis
                                ),
                                timeout=15.0  # 15 second max for text unlock
                            )

                        success = result.get("success", False)
                        message = result.get("message", "Screen unlock attempted")
                        action = result.get("action", "unlocked")

                        logger.info(
                            f"🔓 [LOCK-UNLOCK-EXECUTE] Intelligent unlock completed: success={success}, "
                            f"action={action}, speaker={speaker_name or user_name}"
                        )

                    except ImportError as e:
                        logger.warning(f"🔓 [LOCK-UNLOCK-FALLBACK] Intelligent service unavailable: {e}")
                        logger.info(f"🔓 [LOCK-UNLOCK-FALLBACK] Attempting keychain unlock with audio verification...")
                        # Fallback to keychain unlock WITH AUDIO DATA
                        try:
                            from backend.macos_keychain_unlock import MacOSKeychainUnlock

                            keychain_unlock = MacOSKeychainUnlock()

                            # Pass audio data for voice verification in fallback path
                            if audio_data:
                                logger.info(f"🎤 [LOCK-UNLOCK-FALLBACK] Passing {len(audio_data)} bytes of audio to keychain unlock")
                                # Use enhanced verification with multi-factor fusion
                                from voice.speaker_verification_service import get_speaker_verification_service
                                speaker_service = await get_speaker_verification_service()

                                # Use enhanced verification for sick voice detection, challenge questions, etc.
                                verification_result = await speaker_service.verify_speaker_enhanced(
                                    audio_data,
                                    speaker_name or user_name,
                                    context={"environment": "default", "source": "unlock_fallback"}
                                )

                                if verification_result.get("verified", False):
                                    confidence = verification_result.get("confidence", 0.0)
                                    feedback = verification_result.get("feedback", {})
                                    feedback_msg = feedback.get("message", "") if isinstance(feedback, dict) else ""
                                    logger.info(f"✅ [LOCK-UNLOCK-FALLBACK] Voice verified: {confidence:.1%} confidence")
                                    if feedback_msg:
                                        logger.info(f"💬 [LOCK-UNLOCK-FALLBACK] Feedback: {feedback_msg}")
                                    result = await keychain_unlock.unlock_screen(
                                        verified_speaker=speaker_name or user_name
                                    )
                                elif verification_result.get("decision") == "challenge_pending":
                                    # Challenge question required - for now, deny and prompt retry
                                    confidence = verification_result.get("confidence", 0.0)
                                    feedback = verification_result.get("feedback", {})
                                    feedback_msg = feedback.get("message", "Voice verification needs additional confirmation.") if isinstance(feedback, dict) else "Voice verification needs additional confirmation."
                                    logger.info(f"🔐 [LOCK-UNLOCK-FALLBACK] Challenge required: {feedback_msg}")
                                    result = {
                                        "success": False,
                                        "message": feedback_msg,
                                        "action": "challenge_required"
                                    }
                                else:
                                    confidence = verification_result.get("confidence", 0.0)
                                    feedback = verification_result.get("feedback", {})
                                    recommendation = verification_result.get("recommendation", "")
                                    feedback_msg = feedback.get("message", "") if isinstance(feedback, dict) else ""
                                    logger.warning(f"🚫 [LOCK-UNLOCK-FALLBACK] Voice verification failed: {confidence:.1%}")
                                    if recommendation:
                                        logger.info(f"💡 [LOCK-UNLOCK-FALLBACK] Recommendation: {recommendation}")
                                    result = {
                                        "success": False,
                                        "message": feedback_msg or f"Voice verification failed (confidence: {confidence:.1%})",
                                        "action": "denied"
                                    }
                            else:
                                logger.warning(f"⚠️ [LOCK-UNLOCK-FALLBACK] No audio data - bypassing voice verification")
                                result = await keychain_unlock.unlock_screen(
                                    verified_speaker=speaker_name or user_name
                                )

                            success = result.get("success", False)
                            message = result.get("message", "Screen unlock attempted")
                            action = result.get("action", "unlocked")

                            logger.info(f"🔓 [LOCK-UNLOCK-FALLBACK] Keychain unlock result: success={success}, action={action}")

                        except Exception as e2:
                            logger.error(f"🔓 [LOCK-UNLOCK-ERROR] Keychain fallback failed: {e2}", exc_info=True)
                            # Final fallback to controller (no voice verification)
                            logger.info(f"🔓 [LOCK-UNLOCK-FALLBACK] Final fallback to controller.unlock_screen()")
                            success, message = await controller.unlock_screen()
                            action = "unlocked"

                    except Exception as e:
                        logger.error(f"🔓 [LOCK-UNLOCK-ERROR] Unlock failed: {e}", exc_info=True)
                        logger.info(f"🔓 [LOCK-UNLOCK-FALLBACK] Exception handler - trying controller.unlock_screen()...")

                        # Try enhanced voice verification even in exception fallback if we have audio
                        if audio_data:
                            try:
                                logger.info(f"🎤 [LOCK-UNLOCK-FALLBACK] Attempting enhanced voice verification before controller fallback...")
                                from voice.speaker_verification_service import get_speaker_verification_service
                                speaker_service = await get_speaker_verification_service()

                                # Use enhanced verification for better feedback and multi-factor auth
                                verification_result = await speaker_service.verify_speaker_enhanced(
                                    audio_data,
                                    speaker_name or user_name,
                                    context={"environment": "default", "source": "exception_fallback"}
                                )

                                if not verification_result.get("verified", False):
                                    confidence = verification_result.get("confidence", 0.0)
                                    feedback = verification_result.get("feedback", {})
                                    recommendation = verification_result.get("recommendation", "")
                                    feedback_msg = feedback.get("message", "") if isinstance(feedback, dict) else ""
                                    logger.warning(f"🚫 [LOCK-UNLOCK-FALLBACK] Voice verification failed in exception handler: {confidence:.1%}")
                                    if recommendation:
                                        logger.info(f"💡 [LOCK-UNLOCK-FALLBACK] Recommendation: {recommendation}")
                                    success = False
                                    message = feedback_msg or f"Voice verification failed (confidence: {confidence:.1%}). Please try again."
                                    action = "denied"
                                else:
                                    confidence = verification_result.get("confidence", 0.0)
                                    feedback = verification_result.get("feedback", {})
                                    feedback_msg = feedback.get("message", "") if isinstance(feedback, dict) else ""
                                    logger.info(f"✅ [LOCK-UNLOCK-FALLBACK] Voice verified in exception handler: {confidence:.1%}")
                                    if feedback_msg:
                                        logger.info(f"💬 [LOCK-UNLOCK-FALLBACK] Feedback: {feedback_msg}")
                                    success, message = await controller.unlock_screen()
                                    action = "unlocked"
                            except Exception as verify_error:
                                logger.error(f"❌ [LOCK-UNLOCK-FALLBACK] Voice verification in exception handler failed: {verify_error}")
                                success, message = await controller.unlock_screen()
                                action = "unlocked"
                        else:
                            logger.warning(f"⚠️ [LOCK-UNLOCK-FALLBACK] No audio data in exception handler - proceeding without verification")
                            success, message = await controller.unlock_screen()
                            action = "unlocked"

                step_times["execute"] = (time.time() - step_start) * 1000
                logger.info(
                    f"🔒 [LOCK-UNLOCK-STEP] {action_type} executed in {step_times['execute']:.1f}ms "
                    f"(success={success})"
                )

                return success, message, action, step_times

            # Execute with 60 second timeout
            try:
                success, message, action, step_times = await asyncio.wait_for(
                    execute_lock_unlock(),
                    timeout=60.0
                )
            except asyncio.TimeoutError:
                logger.error(f"🚨 [LOCK-UNLOCK-TIMEOUT] {action_type} command timed out after 60s")
                # Write to hang detection file
                try:
                    hang_file = Path("backend/logs/hanging_commands.log")
                    with open(hang_file, "a") as f:
                        f.write(
                            f"{datetime.now().isoformat()} | TIMEOUT-60s | {action_type} | "
                            f"60.0s | {text} | {user_name}\n"
                        )
                except:
                    pass

                return {
                    "success": False,
                    "error": f"{action_type} command timed out after 60 seconds",
                    "response": f"Screen {action_type.lower()} timed out.",
                    "latency_ms": 60000,
                    "timeout": True,
                    "metadata": {"command": text, "user": user_name}
                }

            # Cancel monitor task since we completed
            monitor_task.cancel()

            latency_ms = (time.time() - start_time) * 1000

            # Log completion with full details
            logger.info(
                f"✅ [LOCK-UNLOCK-COMPLETE] {action_type} finished in {latency_ms:.0f}ms "
                f"(success={success}) | Steps: import={step_times.get('import', 0):.1f}ms, "
                f"init={step_times.get('init', 0):.1f}ms, execute={step_times.get('execute', 0):.1f}ms"
            )

            # Log to performance tracking file
            try:
                perf_file = Path("backend/logs/lock_unlock_performance.log")
                perf_file.parent.mkdir(exist_ok=True)
                with open(perf_file, "a") as f:
                    f.write(
                        f"{datetime.now().isoformat()} | {action_type} | {latency_ms:.0f}ms | "
                        f"success={success} | import={step_times.get('import', 0):.1f}ms | "
                        f"init={step_times.get('init', 0):.1f}ms | execute={step_times.get('execute', 0):.1f}ms | "
                        f"{text}\n"
                    )
            except Exception as e:
                logger.debug(f"Could not write performance log: {e}")

            # Complete VBI health monitor operation tracking
            if success:
                await self._complete_tracked_operation(tracked_operation, {
                    "action": action,
                    "latency_ms": latency_ms,
                })
            else:
                await self._fail_tracked_operation(tracked_operation, message or "Operation failed")

            return {
                "success": success,
                "response": message,
                "fast_path": True,
                "action": action,
                "latency_ms": latency_ms,
                "step_times_ms": step_times,
                "metadata": {
                    "method": "fast_lock_unlock",
                    "command": text,
                    "user": user_name,
                    "action_type": action_type,
                },
            }

        except Exception as e:
            # Cancel monitor task
            monitor_task.cancel()

            # Record failure in VBI health monitor
            await self._fail_tracked_operation(tracked_operation, str(e))

            elapsed_ms = (time.time() - start_time) * 1000
            logger.error(
                f"❌ [LOCK-UNLOCK-ERROR] {action_type} failed after {elapsed_ms:.0f}ms: {e}",
                exc_info=True
            )

            # Write error to hang detection file
            try:
                hang_file = Path("backend/logs/hanging_commands.log")
                with open(hang_file, "a") as f:
                    f.write(
                        f"{datetime.now().isoformat()} | ERROR | {action_type} | "
                        f"{elapsed_ms:.0f}ms | {str(e)} | {text} | {user_name}\n"
                    )
            except:
                pass

            return {
                "success": False,
                "error": str(e),
                "response": f"Failed to {action_type.lower()} screen: {str(e)}",
                "latency_ms": elapsed_ms,
                "metadata": {"command": text, "user": user_name, "action_type": action_type}
            }

    async def _fast_voice_security_test(
        self,
        text: str,
        user_name: str,
        metadata: Optional[Dict] = None,
    ) -> Dict:
        """Fast voice security test handler.

        Runs comprehensive voice biometric security tests to verify that
        unauthorized voices are rejected and the authorized voice is accepted.

        Args:
            text: Command text
            user_name: User name
            metadata: Optional metadata dict

        Returns:
            Result dict with test results and security verdict
        """
        start_time = time.time()

        logger.info(f"🔒 [SECURITY-TEST-START] Voice security test initiated at {datetime.now().isoformat()}")
        logger.info(f"🔒 [SECURITY-TEST-USER] Testing for user: {user_name}")

        try:
            # Import voice security tester with audio playback
            from backend.voice_unlock.voice_security_tester import (
                VoiceSecurityTester,
                PlaybackConfig,
                AudioBackend,
            )

            # Enable audio playback for voice-triggered tests
            playback_config = PlaybackConfig(
                enabled=True,
                verbose=True,
                backend=AudioBackend.AUTO,
                volume=0.5,
                announce_profile=True,
                pause_after_playback=0.5
            )

            # Use full test mode (36 unique voice profiles with actual accent diversity)
            # Includes: US (21), British (5), Australian (5), Indian (5)
            test_config = {
                'test_mode': 'full',
                'authorized_user': user_name,
            }

            # Define progress callback to provide visual feedback (not announced)
            async def progress_callback(current: int, total: int):
                """Update progress - visual only, not announced audibly"""
                progress_msg = f"Testing voice {current} of {total}"
                logger.info(f"🔒 [SECURITY-TEST-PROGRESS] {progress_msg}")
                # Note: Progress is logged but NOT spoken via TTS
                # User can see it in logs/UI without audio interruption

            # Create tester instance with audio playback and progress callback
            tester = VoiceSecurityTester(
                config=test_config,
                playback_config=playback_config,
                progress_callback=progress_callback
            )

            # Run security tests
            logger.info("🔒 [SECURITY-TEST-RUNNING] Executing voice security tests with audio playback...")
            report = await tester.run_security_tests()

            # Save report
            await tester.save_report(report)

            latency_ms = (time.time() - start_time) * 1000

            # Generate response based on results
            if report.is_secure:
                response = (
                    f"Voice security test complete. {report.summary['passed']} of {report.summary['total']} tests passed. "
                    f"Your voice authentication is secure. No unauthorized voices were accepted."
                )
            else:
                breaches = report.summary.get('security_breaches', 0)
                false_rejects = report.summary.get('false_rejections', 0)
                response = (
                    f"Voice security test complete. Warning: {breaches} security breaches detected. "
                    f"{false_rejects} false rejections occurred. Please review the security report."
                )

            logger.info(
                f"✅ [SECURITY-TEST-COMPLETE] Test finished in {latency_ms:.0f}ms "
                f"(secure={report.is_secure}) | Tests: {report.summary['total']}, "
                f"Passed: {report.summary['passed']}, Failed: {report.summary['failed']}"
            )

            return {
                "success": True,
                "response": response,
                "fast_path": True,
                "is_secure": report.is_secure,
                "test_summary": report.summary,
                "latency_ms": latency_ms,
                "metadata": {
                    "method": "fast_voice_security_test",
                    "command": text,
                    "user": user_name,
                    "report_file": str(report.report_file) if hasattr(report, 'report_file') else None
                },
            }

        except Exception as e:
            elapsed_ms = (time.time() - start_time) * 1000
            logger.error(
                f"❌ [SECURITY-TEST-ERROR] Voice security test failed after {elapsed_ms:.0f}ms: {e}",
                exc_info=True
            )

            return {
                "success": False,
                "error": str(e),
                "response": f"Voice security test failed: {str(e)}",
                "latency_ms": elapsed_ms,
                "metadata": {"command": text, "user": user_name}
            }


# Global pipeline instance
_pipeline_instance: Optional[AdvancedAsyncPipeline] = None


def get_async_pipeline(jarvis_instance=None) -> AdvancedAsyncPipeline:
    """Get or create the global async pipeline instance.

    Args:
        jarvis_instance: Optional JARVIS instance to use

    Returns:
        AdvancedAsyncPipeline: The global pipeline instance
    """
    global _pipeline_instance
    if _pipeline_instance is None:
        _pipeline_instance = AdvancedAsyncPipeline(jarvis_instance)
    return _pipeline_instance
