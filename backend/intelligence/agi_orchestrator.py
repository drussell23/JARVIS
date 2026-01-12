"""
AGI Orchestrator v100.0 - Unified Artificial General Intelligence Coordinator
==============================================================================

The master orchestrator that unifies all AGI components into a cohesive system:
1. MetaCognitiveEngine - Self-aware reasoning and introspection
2. MultiModalPerceptionFusion - Vision + voice + text integration
3. ContinuousImprovementEngine - Self-improving learning loop
4. EmotionalIntelligenceModule - Empathetic response system
5. LongTermMemory - Persistent episodic/semantic memory
6. Trinity Integration - Cross-repo coordination

This is the "brain stem" that coordinates all higher cognitive functions.

Architecture:
    ┌─────────────────────────────────────────────────────────────────────────┐
    │                         AGI Orchestrator                                 │
    │  ┌─────────────────────────────────────────────────────────────────────┐│
    │  │                     Perception Pipeline                              ││
    │  │  ┌──────────┐    ┌──────────┐    ┌──────────┐    ┌──────────┐      ││
    │  │  │  Vision  │───►│  Audio   │───►│   Text   │───►│  Fusion  │      ││
    │  │  └──────────┘    └──────────┘    └──────────┘    └──────────┘      ││
    │  └─────────────────────────────────────────────────────────────────────┘│
    │                                    │                                     │
    │                                    ▼                                     │
    │  ┌─────────────────────────────────────────────────────────────────────┐│
    │  │                      Cognition Core                                  ││
    │  │  ┌──────────────┐   ┌───────────────┐   ┌────────────────┐         ││
    │  │  │ Meta-Cognition│   │   Reasoning   │   │    Memory      │         ││
    │  │  │   Engine     │◄─►│    Engine     │◄─►│   Manager      │         ││
    │  │  └──────────────┘   └───────────────┘   └────────────────┘         ││
    │  └─────────────────────────────────────────────────────────────────────┘│
    │                                    │                                     │
    │                                    ▼                                     │
    │  ┌─────────────────────────────────────────────────────────────────────┐│
    │  │                     Action & Response                                ││
    │  │  ┌──────────────┐   ┌───────────────┐   ┌────────────────┐         ││
    │  │  │  Emotional   │   │    Action     │   │    Voice       │         ││
    │  │  │ Intelligence │◄─►│   Planner     │◄─►│   Output       │         ││
    │  │  └──────────────┘   └───────────────┘   └────────────────┘         ││
    │  └─────────────────────────────────────────────────────────────────────┘│
    │                                    │                                     │
    │                                    ▼                                     │
    │  ┌─────────────────────────────────────────────────────────────────────┐│
    │  │                    Learning & Improvement                            ││
    │  │  ┌──────────────┐   ┌───────────────┐   ┌────────────────┐         ││
    │  │  │ Continuous   │   │    Trinity    │   │   Monitoring   │         ││
    │  │  │ Improvement  │◄─►│  Integration  │◄─►│   & Metrics    │         ││
    │  │  └──────────────┘   └───────────────┘   └────────────────┘         ││
    │  └─────────────────────────────────────────────────────────────────────┘│
    └─────────────────────────────────────────────────────────────────────────┘

Author: JARVIS System
Version: 100.0.0
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from pathlib import Path
from typing import (
    Any,
    Callable,
    Coroutine,
    Dict,
    List,
    Optional,
    Set,
    Tuple,
    Union,
)

from backend.core.async_safety import LazyAsyncLock

# Environment-driven configuration
AGI_DATA_DIR = Path(os.getenv(
    "AGI_DATA_DIR",
    str(Path.home() / ".jarvis" / "agi_orchestrator")
))
AGI_PROCESSING_TIMEOUT_SECONDS = float(os.getenv("AGI_PROCESSING_TIMEOUT", "30.0"))
AGI_PARALLEL_ENABLED = os.getenv("AGI_PARALLEL_ENABLED", "true").lower() == "true"
AGI_INTROSPECTION_ENABLED = os.getenv("AGI_INTROSPECTION_ENABLED", "true").lower() == "true"
AGI_EMOTIONAL_ENABLED = os.getenv("AGI_EMOTIONAL_ENABLED", "true").lower() == "true"
AGI_IMPROVEMENT_ENABLED = os.getenv("AGI_IMPROVEMENT_ENABLED", "true").lower() == "true"
AGI_TRINITY_ENABLED = os.getenv("AGI_TRINITY_ENABLED", "true").lower() == "true"


class AGIPhase(Enum):
    """Phases of AGI cognitive processing."""
    PERCEPTION = "perception"
    COGNITION = "cognition"
    DECISION = "decision"
    ACTION = "action"
    REFLECTION = "reflection"
    LEARNING = "learning"


class ProcessingMode(Enum):
    """Processing modes."""
    REACTIVE = "reactive"  # Fast, instinctive response
    DELIBERATIVE = "deliberative"  # Careful, reasoned response
    CREATIVE = "creative"  # Novel, exploratory response
    REFLECTIVE = "reflective"  # Self-aware, introspective


class ComponentHealth(Enum):
    """Health status of components."""
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    FAILED = "failed"
    UNKNOWN = "unknown"


@dataclass
class CognitiveInput:
    """Input to the AGI system from any modality."""
    input_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: float = field(default_factory=time.time)

    # Content
    modality: str = "text"  # "text", "voice", "vision", "multimodal"
    content: Any = None
    raw_data: Optional[Any] = None

    # Context
    context: Dict[str, Any] = field(default_factory=dict)
    source: str = "user"
    priority: int = 5  # 1-10, higher = more urgent

    # Processing hints
    requires_response: bool = True
    response_deadline_ms: Optional[float] = None


@dataclass
class CognitiveOutput:
    """Output from the AGI system."""
    output_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: float = field(default_factory=time.time)

    # Response
    response_text: Optional[str] = None
    response_voice: Optional[Dict[str, Any]] = None
    actions_to_take: List[Dict[str, Any]] = field(default_factory=list)

    # Confidence
    confidence: float = 0.5
    reasoning_chain_id: Optional[str] = None

    # Emotional context
    emotional_tone: str = "neutral"
    empathy_applied: bool = False

    # Meta-cognitive feedback
    self_assessment: Dict[str, float] = field(default_factory=dict)
    improvement_suggestions: List[str] = field(default_factory=list)

    # Processing metrics
    processing_time_ms: float = 0.0
    components_used: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "output_id": self.output_id,
            "timestamp": self.timestamp,
            "response_text": self.response_text,
            "actions": self.actions_to_take,
            "confidence": self.confidence,
            "emotional_tone": self.emotional_tone,
            "processing_time_ms": self.processing_time_ms,
            "components_used": self.components_used,
        }


@dataclass
class ComponentStatus:
    """Status of a cognitive component."""
    component_name: str
    health: ComponentHealth = ComponentHealth.UNKNOWN
    last_heartbeat: float = field(default_factory=time.time)
    error_count: int = 0
    last_error: Optional[str] = None
    metrics: Dict[str, float] = field(default_factory=dict)


@dataclass
class AGIState:
    """Current state of the AGI system."""
    state_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: float = field(default_factory=time.time)

    # Processing state
    current_phase: AGIPhase = AGIPhase.PERCEPTION
    processing_mode: ProcessingMode = ProcessingMode.REACTIVE
    active_inputs: int = 0

    # Component states
    component_statuses: Dict[str, ComponentStatus] = field(default_factory=dict)

    # Metrics
    total_inputs_processed: int = 0
    total_outputs_generated: int = 0
    avg_processing_time_ms: float = 0.0

    # Learning metrics
    improvement_velocity: float = 0.0
    knowledge_growth_rate: float = 0.0


class AGIOrchestrator:
    """
    Main AGI Orchestrator - Coordinates all cognitive components.

    This is the central coordinator that ties together:
    - MetaCognitiveEngine: Self-aware reasoning
    - MultiModalPerceptionFusion: Sensory integration
    - ContinuousImprovementEngine: Self-improvement
    - EmotionalIntelligenceModule: Empathy and emotion
    - LongTermMemory: Persistent memory
    - Trinity integration: Cross-repo coordination
    """

    def __init__(self):
        self.logger = logging.getLogger("AGIOrchestrator")

        # Component references (lazy-loaded)
        self._meta_cognitive = None
        self._perception_fusion = None
        self._improvement_engine = None
        self._emotional_intelligence = None
        self._long_term_memory = None
        self._trinity_event_bus = None
        self._trinity_monitoring = None

        # State
        self._running = False
        self._state = AGIState()
        self._lock = asyncio.Lock()

        # Processing queue
        self._input_queue: asyncio.Queue = asyncio.Queue()
        self._output_queue: asyncio.Queue = asyncio.Queue()

        # History
        self._processing_history: deque = deque(maxlen=1000)

        # Background tasks
        self._processing_task: Optional[asyncio.Task] = None
        self._health_monitor_task: Optional[asyncio.Task] = None
        self._introspection_task: Optional[asyncio.Task] = None

        # Callbacks
        self._output_callbacks: List[Callable[[CognitiveOutput], Coroutine]] = []

        # Ensure data directory
        AGI_DATA_DIR.mkdir(parents=True, exist_ok=True)

    async def start(self) -> None:
        """Start the AGI orchestrator and all components."""
        if self._running:
            return

        self._running = True
        self.logger.info("=" * 60)
        self.logger.info("AGI ORCHESTRATOR v100.0 STARTING")
        self.logger.info("=" * 60)

        start_time = time.time()

        # Initialize components in parallel where possible
        init_tasks = []

        # Meta-Cognitive Engine
        if AGI_INTROSPECTION_ENABLED:
            init_tasks.append(("MetaCognitive", self._init_meta_cognitive()))

        # Perception Fusion
        init_tasks.append(("PerceptionFusion", self._init_perception_fusion()))

        # Continuous Improvement
        if AGI_IMPROVEMENT_ENABLED:
            init_tasks.append(("ContinuousImprovement", self._init_improvement_engine()))

        # Emotional Intelligence
        if AGI_EMOTIONAL_ENABLED:
            init_tasks.append(("EmotionalIntelligence", self._init_emotional_intelligence()))

        # Long-Term Memory
        init_tasks.append(("LongTermMemory", self._init_long_term_memory()))

        # Trinity Integration
        if AGI_TRINITY_ENABLED:
            init_tasks.append(("TrinityIntegration", self._init_trinity_integration()))

        # Run initialization in parallel
        results = await asyncio.gather(
            *[task for _, task in init_tasks],
            return_exceptions=True
        )

        # Log results
        for (name, _), result in zip(init_tasks, results):
            if isinstance(result, Exception):
                self.logger.warning(f"  ⚠️ {name}: Failed - {result}")
                self._state.component_statuses[name] = ComponentStatus(
                    component_name=name,
                    health=ComponentHealth.FAILED,
                    last_error=str(result),
                )
            else:
                self.logger.info(f"  ✓ {name}: Initialized")
                self._state.component_statuses[name] = ComponentStatus(
                    component_name=name,
                    health=ComponentHealth.HEALTHY,
                )

        # Start background tasks
        self._processing_task = asyncio.create_task(self._processing_loop())
        self._health_monitor_task = asyncio.create_task(self._health_monitor_loop())

        if AGI_INTROSPECTION_ENABLED:
            self._introspection_task = asyncio.create_task(self._introspection_loop())

        duration = time.time() - start_time
        self.logger.info("=" * 60)
        self.logger.info(f"AGI ORCHESTRATOR READY ({duration:.2f}s)")
        self.logger.info("=" * 60)

    async def stop(self) -> None:
        """Stop the AGI orchestrator."""
        self._running = False
        self.logger.info("AGI Orchestrator stopping...")

        # Cancel background tasks
        for task in [self._processing_task, self._health_monitor_task, self._introspection_task]:
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        # Stop components
        stop_tasks = []
        if self._meta_cognitive:
            stop_tasks.append(self._meta_cognitive.stop())
        if self._perception_fusion:
            stop_tasks.append(self._perception_fusion.stop())
        if self._improvement_engine:
            stop_tasks.append(self._improvement_engine.stop())
        if self._emotional_intelligence:
            stop_tasks.append(self._emotional_intelligence.stop())

        await asyncio.gather(*stop_tasks, return_exceptions=True)

        # Save state
        await self._save_state()

        self.logger.info("AGI Orchestrator stopped")

    async def process(self, input_data: CognitiveInput) -> CognitiveOutput:
        """Process a cognitive input and generate output."""
        start_time = time.time()
        output = CognitiveOutput()

        try:
            async with self._lock:
                self._state.active_inputs += 1
                self._state.current_phase = AGIPhase.PERCEPTION

            # Phase 1: Perception - Fuse multi-modal inputs
            perception_result = await self._perception_phase(input_data)
            output.components_used.append("perception_fusion")

            # Phase 2: Emotional Analysis
            self._state.current_phase = AGIPhase.COGNITION
            emotional_state = None
            if self._emotional_intelligence:
                emotional_state = await self._emotional_phase(input_data)
                output.components_used.append("emotional_intelligence")
                output.emotional_tone = emotional_state.primary_emotion.value if emotional_state else "neutral"

            # Phase 3: Memory Retrieval
            memory_context = await self._memory_phase(input_data, perception_result)
            if memory_context:
                output.components_used.append("long_term_memory")

            # Phase 4: Cognition - Generate response
            self._state.current_phase = AGIPhase.DECISION
            response = await self._cognition_phase(
                input_data, perception_result, emotional_state, memory_context
            )
            output.response_text = response.get("text")
            output.actions_to_take = response.get("actions", [])
            output.confidence = response.get("confidence", 0.5)

            # Phase 5: Meta-Cognitive Review
            if self._meta_cognitive:
                self._state.current_phase = AGIPhase.REFLECTION
                meta_review = await self._meta_cognitive_phase(output, input_data)
                output.self_assessment = meta_review.get("assessment", {})
                output.improvement_suggestions = meta_review.get("suggestions", [])
                output.components_used.append("meta_cognitive")

            # Phase 6: Apply Empathy
            if self._emotional_intelligence and emotional_state:
                empathetic_response = await self._emotional_intelligence.get_empathetic_response()
                if empathetic_response.should_acknowledge_emotion:
                    output.empathy_applied = True
                    # Prepend acknowledgment if appropriate
                    if empathetic_response.acknowledgment_phrase and output.response_text:
                        output.response_text = f"{empathetic_response.acknowledgment_phrase} {output.response_text}"

            # Phase 7: Learning
            self._state.current_phase = AGIPhase.LEARNING
            if self._improvement_engine:
                await self._learning_phase(input_data, output)
                output.components_used.append("continuous_improvement")

            # Record metrics
            output.processing_time_ms = (time.time() - start_time) * 1000

            # Store in memory
            if self._long_term_memory:
                await self._store_experience(input_data, output)

            # Update state
            async with self._lock:
                self._state.active_inputs -= 1
                self._state.total_inputs_processed += 1
                self._state.total_outputs_generated += 1
                self._state.avg_processing_time_ms = (
                    self._state.avg_processing_time_ms * 0.9 + output.processing_time_ms * 0.1
                )

            # Notify callbacks
            for callback in self._output_callbacks:
                try:
                    await callback(output)
                except Exception as e:
                    self.logger.warning(f"Output callback error: {e}")

            # Store in history
            self._processing_history.append({
                "input_id": input_data.input_id,
                "output_id": output.output_id,
                "timestamp": output.timestamp,
                "processing_time_ms": output.processing_time_ms,
            })

            return output

        except Exception as e:
            self.logger.error(f"AGI processing error: {e}")
            output.response_text = "I encountered an issue processing that. Let me try again."
            output.confidence = 0.3
            output.processing_time_ms = (time.time() - start_time) * 1000
            return output

    async def _perception_phase(self, input_data: CognitiveInput) -> Dict[str, Any]:
        """Phase 1: Perception - Process and fuse sensory inputs."""
        result = {"raw": input_data.content}

        if self._perception_fusion and input_data.modality in ("multimodal", "voice", "vision"):
            try:
                from .multi_modal_perception_fusion import ModalityInput, Modality, PerceptionType

                # Create modality input
                modality = {
                    "text": Modality.TEXT,
                    "voice": Modality.AUDIO,
                    "vision": Modality.VISION,
                }.get(input_data.modality, Modality.TEXT)

                modal_input = ModalityInput(
                    modality=modality,
                    raw_data=input_data.content,
                    features=input_data.context.get("features", {}),
                )

                await self._perception_fusion.add_input(modal_input)
                fused = await self._perception_fusion.fuse_recent()

                result["fused"] = {
                    "confidence": fused.overall_confidence,
                    "modalities": [m.value for m in fused.modalities_used],
                    "features": fused.unified_features,
                    "consistency": fused.consistency_score,
                }
            except Exception as e:
                self.logger.warning(f"Perception fusion error: {e}")

        return result

    async def _emotional_phase(self, input_data: CognitiveInput) -> Optional[Any]:
        """Phase 2: Emotional Analysis."""
        if not self._emotional_intelligence:
            return None

        try:
            if input_data.modality == "text":
                return await self._emotional_intelligence.process_text(
                    str(input_data.content or "")
                )
            elif input_data.modality == "voice":
                voice_features = input_data.context.get("voice_features", {})
                return await self._emotional_intelligence.process_voice(
                    pitch_mean=voice_features.get("pitch_mean", 0),
                    pitch_std=voice_features.get("pitch_std", 0),
                    speech_rate=voice_features.get("speech_rate", 1.0),
                    energy=voice_features.get("energy", 0.5),
                )
            else:
                return await self._emotional_intelligence.get_current_state()
        except Exception as e:
            self.logger.warning(f"Emotional analysis error: {e}")
            return None

    async def _memory_phase(
        self,
        input_data: CognitiveInput,
        perception_result: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """Phase 3: Memory Retrieval."""
        if not self._long_term_memory:
            return None

        try:
            # Search for relevant memories
            query = str(input_data.content or "")
            if query:
                # Use semantic search if available
                relevant_memories = await self._long_term_memory.search_semantic(
                    query=query,
                    limit=5,
                )
                return {"relevant_memories": relevant_memories}
        except Exception as e:
            self.logger.warning(f"Memory retrieval error: {e}")

        return None

    async def _cognition_phase(
        self,
        input_data: CognitiveInput,
        perception_result: Dict[str, Any],
        emotional_state: Optional[Any],
        memory_context: Optional[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Phase 4: Cognition - Generate response."""
        # Build context for response generation
        context = {
            "input": input_data.content,
            "modality": input_data.modality,
            "perception": perception_result,
        }

        if emotional_state:
            context["emotional_state"] = {
                "primary_emotion": emotional_state.primary_emotion.value,
                "stress_level": emotional_state.stress_level,
            }

        if memory_context:
            context["memories"] = memory_context

        # Generate response (in production, this would use LLM)
        response = {
            "text": f"I processed your {input_data.modality} input.",
            "actions": [],
            "confidence": 0.7,
        }

        # Adjust based on emotional context
        if emotional_state and emotional_state.stress_level > 0.6:
            response["confidence"] *= 0.9  # Slightly less confident under stress

        return response

    async def _meta_cognitive_phase(
        self,
        output: CognitiveOutput,
        input_data: CognitiveInput
    ) -> Dict[str, Any]:
        """Phase 5: Meta-Cognitive Review."""
        if not self._meta_cognitive:
            return {}

        try:
            from .meta_cognitive_engine import ReasoningChain, ReasoningStep, ReasoningOutcome

            # Create reasoning chain for analysis
            chain = ReasoningChain(
                goal=f"Process {input_data.modality} input",
                context={"input_id": input_data.input_id},
                final_decision=output.response_text,
                final_confidence=output.confidence,
            )

            # Add a step representing the processing
            step = ReasoningStep(
                thought=f"Generated response with confidence {output.confidence}",
                confidence=output.confidence,
            )
            chain.add_step(step)

            # Analyze for biases and blind spots
            biases, blind_spots, correction = await self._meta_cognitive.analyze_reasoning(chain)

            # Record the chain
            await self._meta_cognitive.record_reasoning_chain(chain)

            return {
                "assessment": {
                    "bias_count": len(biases),
                    "blind_spot_count": len(blind_spots),
                    "correction_applied": correction is not None and correction.success,
                },
                "suggestions": [
                    b.mitigation_suggestion for b in biases if b.mitigation_suggestion
                ] + [
                    bs.description for bs in blind_spots
                ],
            }
        except Exception as e:
            self.logger.warning(f"Meta-cognitive analysis error: {e}")
            return {}

    async def _learning_phase(
        self,
        input_data: CognitiveInput,
        output: CognitiveOutput
    ) -> None:
        """Phase 6: Learning from the interaction."""
        if not self._improvement_engine:
            return

        try:
            from .continuous_improvement_engine import MetricType

            # Record processing metrics
            await self._improvement_engine.record_metric(
                MetricType.LATENCY,
                output.processing_time_ms,
                domain="agi_processing",
            )

            await self._improvement_engine.record_metric(
                MetricType.CONFIDENCE,
                output.confidence,
                domain="response_generation",
            )

            # Record component usage
            for component in output.components_used:
                await self._improvement_engine.record_metric(
                    MetricType.THROUGHPUT,
                    1.0,
                    domain=component,
                )

        except Exception as e:
            self.logger.warning(f"Learning phase error: {e}")

    async def _store_experience(
        self,
        input_data: CognitiveInput,
        output: CognitiveOutput
    ) -> None:
        """Store the interaction as an experience in long-term memory."""
        if not self._long_term_memory:
            return

        try:
            await self._long_term_memory.store_episodic(
                event_type="agi_interaction",
                description=f"Processed {input_data.modality} input",
                context={
                    "input_id": input_data.input_id,
                    "output_id": output.output_id,
                    "confidence": output.confidence,
                    "processing_time_ms": output.processing_time_ms,
                    "components_used": output.components_used,
                    "emotional_tone": output.emotional_tone,
                },
            )
        except Exception as e:
            self.logger.warning(f"Experience storage error: {e}")

    async def _processing_loop(self) -> None:
        """Background loop to process queued inputs."""
        while self._running:
            try:
                # Get input with timeout
                try:
                    input_data = await asyncio.wait_for(
                        self._input_queue.get(),
                        timeout=1.0
                    )
                except asyncio.TimeoutError:
                    continue

                # Process
                output = await self.process(input_data)

                # Put to output queue
                await self._output_queue.put(output)

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Processing loop error: {e}")

    async def _health_monitor_loop(self) -> None:
        """Monitor component health."""
        while self._running:
            try:
                await asyncio.sleep(30)

                # Check each component
                for name, status in self._state.component_statuses.items():
                    component = self._get_component(name)
                    if component:
                        try:
                            stats = component.get_stats()
                            status.health = ComponentHealth.HEALTHY
                            status.metrics = stats
                            status.last_heartbeat = time.time()
                        except Exception as e:
                            status.health = ComponentHealth.DEGRADED
                            status.error_count += 1
                            status.last_error = str(e)

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Health monitor error: {e}")

    async def _introspection_loop(self) -> None:
        """Periodic self-introspection."""
        while self._running:
            try:
                await asyncio.sleep(300)  # Every 5 minutes

                if self._meta_cognitive:
                    report = await self._meta_cognitive.generate_introspection_report(
                        time_period_days=1
                    )

                    if report.reasoning_health_score < 0.5:
                        self.logger.warning(
                            f"AGI introspection: Low reasoning health ({report.reasoning_health_score:.1%})"
                        )
                        for rec in report.recommendations[:3]:
                            self.logger.warning(f"  Recommendation: {rec}")
                    else:
                        self.logger.info(
                            f"AGI introspection: Health score {report.reasoning_health_score:.1%}"
                        )

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Introspection loop error: {e}")

    def _get_component(self, name: str) -> Optional[Any]:
        """Get component by name."""
        mapping = {
            "MetaCognitive": self._meta_cognitive,
            "PerceptionFusion": self._perception_fusion,
            "ContinuousImprovement": self._improvement_engine,
            "EmotionalIntelligence": self._emotional_intelligence,
            "LongTermMemory": self._long_term_memory,
        }
        return mapping.get(name)

    # Component initialization methods
    async def _init_meta_cognitive(self) -> None:
        """Initialize meta-cognitive engine."""
        try:
            from .meta_cognitive_engine import get_meta_cognitive_engine
            self._meta_cognitive = await get_meta_cognitive_engine()
        except ImportError as e:
            raise RuntimeError(f"MetaCognitiveEngine not available: {e}")

    async def _init_perception_fusion(self) -> None:
        """Initialize perception fusion."""
        try:
            from .multi_modal_perception_fusion import get_perception_fusion
            self._perception_fusion = await get_perception_fusion()
        except ImportError as e:
            raise RuntimeError(f"MultiModalPerceptionFusion not available: {e}")

    async def _init_improvement_engine(self) -> None:
        """Initialize continuous improvement engine."""
        try:
            from .continuous_improvement_engine import get_improvement_engine
            self._improvement_engine = await get_improvement_engine()
        except ImportError as e:
            raise RuntimeError(f"ContinuousImprovementEngine not available: {e}")

    async def _init_emotional_intelligence(self) -> None:
        """Initialize emotional intelligence module."""
        try:
            from .emotional_intelligence_module import get_emotional_intelligence
            self._emotional_intelligence = await get_emotional_intelligence()
        except ImportError as e:
            raise RuntimeError(f"EmotionalIntelligenceModule not available: {e}")

    async def _init_long_term_memory(self) -> None:
        """Initialize long-term memory."""
        try:
            from .long_term_memory import get_long_term_memory
            self._long_term_memory = await get_long_term_memory()
        except ImportError as e:
            self.logger.warning(f"LongTermMemory not available: {e}")

    async def _init_trinity_integration(self) -> None:
        """Initialize Trinity cross-repo integration."""
        try:
            from backend.core.trinity_event_bus import get_trinity_event_bus
            from backend.core.trinity_monitoring import get_trinity_monitoring

            self._trinity_event_bus = get_trinity_event_bus()
            self._trinity_monitoring = get_trinity_monitoring()

            # Subscribe to relevant events
            if self._trinity_event_bus:
                await self._trinity_event_bus.subscribe(
                    "agi.#",
                    self._handle_trinity_event
                )
        except ImportError as e:
            self.logger.warning(f"Trinity integration not available: {e}")

    async def _handle_trinity_event(self, event: Any) -> None:
        """Handle events from Trinity event bus."""
        try:
            self.logger.debug(f"Received Trinity event: {event.topic}")
            # Process cross-repo events
        except Exception as e:
            self.logger.warning(f"Trinity event handling error: {e}")

    async def _save_state(self) -> None:
        """Save orchestrator state."""
        state_file = AGI_DATA_DIR / "orchestrator_state.json"
        try:
            data = {
                "timestamp": time.time(),
                "total_inputs": self._state.total_inputs_processed,
                "total_outputs": self._state.total_outputs_generated,
                "avg_processing_time_ms": self._state.avg_processing_time_ms,
            }
            with open(state_file, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            self.logger.error(f"Failed to save state: {e}")

    def register_output_callback(
        self,
        callback: Callable[[CognitiveOutput], Coroutine]
    ) -> None:
        """Register a callback for output events."""
        self._output_callbacks.append(callback)

    async def queue_input(self, input_data: CognitiveInput) -> None:
        """Queue an input for processing."""
        await self._input_queue.put(input_data)

    async def get_next_output(self) -> CognitiveOutput:
        """Get the next processed output."""
        return await self._output_queue.get()

    def get_state(self) -> AGIState:
        """Get current AGI state."""
        return self._state

    def get_stats(self) -> Dict[str, Any]:
        """Get current statistics."""
        component_stats = {}
        for name, status in self._state.component_statuses.items():
            component_stats[name] = {
                "health": status.health.value,
                "error_count": status.error_count,
            }

        return {
            "running": self._running,
            "current_phase": self._state.current_phase.value,
            "active_inputs": self._state.active_inputs,
            "total_processed": self._state.total_inputs_processed,
            "avg_processing_time_ms": self._state.avg_processing_time_ms,
            "components": component_stats,
        }


# Global instance
_agi_orchestrator: Optional[AGIOrchestrator] = None
_lock = LazyAsyncLock()  # v100.1: Lazy initialization to avoid "no running event loop" error


async def get_agi_orchestrator() -> AGIOrchestrator:
    """Get the global AGIOrchestrator instance."""
    global _agi_orchestrator

    async with _lock:
        if _agi_orchestrator is None:
            _agi_orchestrator = AGIOrchestrator()
            await _agi_orchestrator.start()

        return _agi_orchestrator
