"""
JARVIS Neural Mesh - Voice System Adapter

Adapts the JARVIS Voice components (VoiceMemoryAgent, SpeakerVerificationService,
VoiceUnlockIntegration, StreamingProcessor) for Neural Mesh integration.

This adapter enables:
- Distributed voice authentication across agents
- Shared speaker profiles via knowledge graph
- Voice-triggered multi-agent workflows
- Continuous voice learning coordination

Usage:
    from agents.voice_memory_agent import VoiceMemoryAgent

    voice_agent = VoiceMemoryAgent()
    await voice_agent.initialize()

    adapted = VoiceSystemAdapter(
        voice_component=voice_agent,
        component_type="memory",
    )

    await coordinator.register_agent(adapted)
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Optional,
    Set,
    Union,
)
from uuid import uuid4

from ..base.base_neural_mesh_agent import BaseNeuralMeshAgent
from ..data_models import (
    AgentMessage,
    KnowledgeEntry,
    KnowledgeType,
    MessagePriority,
    MessageType,
)

logger = logging.getLogger(__name__)


class VoiceComponentType(str, Enum):
    """Types of voice components in JARVIS."""
    MEMORY = "memory"  # VoiceMemoryAgent
    VERIFICATION = "verification"  # SpeakerVerificationService
    UNLOCK = "unlock"  # VoiceUnlockIntegration
    STREAMING = "streaming"  # StreamingProcessor
    TTS = "tts"  # Text-to-Speech
    STT = "stt"  # Speech-to-Text


@dataclass
class VoiceCapabilities:
    """Capabilities matrix for voice components."""
    speaker_verification: bool = False
    voice_memory: bool = False
    voice_unlock: bool = False
    streaming_audio: bool = False
    text_to_speech: bool = False
    speech_to_text: bool = False
    enrollment: bool = False
    profile_management: bool = False
    continuous_learning: bool = False
    anti_spoofing: bool = False

    def to_set(self) -> Set[str]:
        """Convert capabilities to set of strings."""
        caps = set()
        if self.speaker_verification:
            caps.add("speaker_verification")
        if self.voice_memory:
            caps.add("voice_memory")
        if self.voice_unlock:
            caps.add("voice_unlock")
        if self.streaming_audio:
            caps.add("streaming_audio")
        if self.text_to_speech:
            caps.add("text_to_speech")
        if self.speech_to_text:
            caps.add("speech_to_text")
        if self.enrollment:
            caps.add("enrollment")
        if self.profile_management:
            caps.add("profile_management")
        if self.continuous_learning:
            caps.add("continuous_learning")
        if self.anti_spoofing:
            caps.add("anti_spoofing")
        return caps


# Capability mappings for each component type
COMPONENT_CAPABILITIES: Dict[VoiceComponentType, VoiceCapabilities] = {
    VoiceComponentType.MEMORY: VoiceCapabilities(
        voice_memory=True,
        profile_management=True,
        continuous_learning=True,
    ),
    VoiceComponentType.VERIFICATION: VoiceCapabilities(
        speaker_verification=True,
        enrollment=True,
        profile_management=True,
        anti_spoofing=True,
    ),
    VoiceComponentType.UNLOCK: VoiceCapabilities(
        voice_unlock=True,
        speaker_verification=True,
        anti_spoofing=True,
    ),
    VoiceComponentType.STREAMING: VoiceCapabilities(
        streaming_audio=True,
        speech_to_text=True,
    ),
    VoiceComponentType.TTS: VoiceCapabilities(
        text_to_speech=True,
    ),
    VoiceComponentType.STT: VoiceCapabilities(
        speech_to_text=True,
        streaming_audio=True,
    ),
}


class VoiceSystemAdapter(BaseNeuralMeshAgent):
    """
    Adapter for JARVIS Voice components to work with Neural Mesh.

    This adapter wraps VoiceMemoryAgent, SpeakerVerificationService,
    VoiceUnlockIntegration, and other voice components.

    Key Features:
    - Voice authentication shared across all agents
    - Distributed speaker profile management
    - Real-time voice event broadcasting
    - Continuous learning coordination

    Example - Wrapping VoiceMemoryAgent:
        from agents.voice_memory_agent import VoiceMemoryAgent

        agent = VoiceMemoryAgent()
        await agent.initialize()

        adapter = VoiceSystemAdapter(
            voice_component=agent,
            component_type=VoiceComponentType.MEMORY,
        )
        await coordinator.register_agent(adapter)

    Example - Voice verification:
        result = await adapter.execute_task({
            "action": "verify_speaker",
            "input": {"audio_data": audio_bytes, "expected_speaker": "Derek"}
        })
    """

    def __init__(
        self,
        voice_component: Any,
        component_type: Union[VoiceComponentType, str],
        agent_name: Optional[str] = None,
        additional_capabilities: Optional[Set[str]] = None,
        version: str = "1.0.0",
    ) -> None:
        """Initialize the voice system adapter.

        Args:
            voice_component: The voice component to wrap
            component_type: Type of component
            agent_name: Optional custom name
            additional_capabilities: Extra capabilities
            version: Adapter version
        """
        # Normalize component type
        if isinstance(component_type, str):
            component_type = VoiceComponentType(component_type.lower())

        self._component_type = component_type
        self._component = voice_component

        # Get capabilities
        caps = COMPONENT_CAPABILITIES.get(
            component_type,
            VoiceCapabilities()
        )
        capabilities = caps.to_set()

        if additional_capabilities:
            capabilities.update(additional_capabilities)

        name = agent_name or f"voice_{component_type.value}"

        super().__init__(
            agent_name=name,
            agent_type="voice",
            capabilities=capabilities,
            backend="local",
            version=version,
        )

        # Task handlers
        self._task_handlers: Dict[str, Callable] = {}
        self._setup_handlers()

        # Voice state
        self._verified_speakers: Dict[str, datetime] = {}
        self._verification_cache_ttl = 60.0  # seconds
        self._enrollment_sessions: Dict[str, Dict[str, Any]] = {}

        # Event tracking
        self._voice_events: List[Dict[str, Any]] = []
        self._max_events = 100

    def _setup_handlers(self) -> None:
        """Setup action handlers based on component type."""
        # Common handlers
        self._task_handlers["get_status"] = self._handle_get_status
        self._task_handlers["get_profiles"] = self._handle_get_profiles

        # Memory handlers
        if self._component_type == VoiceComponentType.MEMORY:
            self._task_handlers["check_freshness"] = self._handle_check_freshness
            self._task_handlers["get_voice_memory"] = self._handle_get_memory
            self._task_handlers["update_memory"] = self._handle_update_memory
            self._task_handlers["run_diagnostics"] = self._handle_diagnostics
            self._task_handlers["self_heal"] = self._handle_self_heal

        # Verification handlers
        if self._component_type in (
            VoiceComponentType.VERIFICATION,
            VoiceComponentType.UNLOCK,
        ):
            self._task_handlers["verify_speaker"] = self._handle_verify_speaker
            self._task_handlers["enroll_speaker"] = self._handle_enroll_speaker
            self._task_handlers["update_profile"] = self._handle_update_profile
            self._task_handlers["delete_profile"] = self._handle_delete_profile

        # Unlock handlers
        if self._component_type == VoiceComponentType.UNLOCK:
            self._task_handlers["unlock"] = self._handle_unlock
            self._task_handlers["get_unlock_status"] = self._handle_unlock_status
            self._task_handlers["configure_unlock"] = self._handle_configure_unlock

        # Streaming handlers
        if self._component_type in (
            VoiceComponentType.STREAMING,
            VoiceComponentType.STT,
        ):
            self._task_handlers["start_stream"] = self._handle_start_stream
            self._task_handlers["stop_stream"] = self._handle_stop_stream
            self._task_handlers["transcribe"] = self._handle_transcribe

        # TTS handlers
        if self._component_type == VoiceComponentType.TTS:
            self._task_handlers["synthesize"] = self._handle_synthesize
            self._task_handlers["speak"] = self._handle_speak

    async def on_initialize(self) -> None:
        """Initialize the adapter and underlying component."""
        logger.info(
            "Initializing VoiceSystemAdapter for %s",
            self._component_type.value,
        )

        # Initialize component
        if hasattr(self._component, "initialize"):
            result = self._component.initialize()
            if asyncio.iscoroutine(result):
                await result

        # Subscribe to messages
        await self.subscribe(
            MessageType.CUSTOM,
            self._handle_custom_message,
        )
        await self.subscribe(
            MessageType.KNOWLEDGE_SHARED,
            self._handle_knowledge_shared,
        )

        # Load speaker profiles into knowledge graph
        if self.knowledge_graph and self._component_type in (
            VoiceComponentType.MEMORY,
            VoiceComponentType.VERIFICATION,
        ):
            await self._sync_profiles_to_knowledge_graph()

        logger.info(
            "VoiceSystemAdapter initialized: %s with capabilities %s",
            self.agent_name,
            self.capabilities,
        )

    async def on_start(self) -> None:
        """Called when agent starts."""
        logger.info("%s voice adapter started", self._component_type.value)

        # Start monitoring if available
        if hasattr(self._component, "start_monitoring"):
            result = self._component.start_monitoring()
            if asyncio.iscoroutine(result):
                await result

    async def on_stop(self) -> None:
        """Cleanup when agent stops."""
        logger.info("%s voice adapter stopping", self._component_type.value)

        # Stop any streams
        if self._component_type in (
            VoiceComponentType.STREAMING,
            VoiceComponentType.STT,
        ):
            if hasattr(self._component, "stop_stream"):
                result = self._component.stop_stream()
                if asyncio.iscoroutine(result):
                    await result

        # Cleanup
        for method in ("cleanup", "close", "shutdown", "stop"):
            if hasattr(self._component, method):
                result = getattr(self._component, method)()
                if asyncio.iscoroutine(result):
                    await result
                break

    async def execute_task(self, payload: Dict[str, Any]) -> Any:
        """Execute a voice task.

        Args:
            payload: Task payload with 'action' and 'input'

        Returns:
            Task result
        """
        action = payload.get("action", "")
        input_data = payload.get("input", {})

        logger.debug(
            "Executing voice task: %s on %s",
            action,
            self._component_type.value,
        )

        handler = self._task_handlers.get(action)
        if not handler:
            if hasattr(self._component, action):
                handler = self._create_component_handler(action)
            else:
                raise ValueError(
                    f"Unknown action '{action}' for {self._component_type.value}"
                )

        try:
            if asyncio.iscoroutinefunction(handler):
                result = await handler(input_data)
            else:
                loop = asyncio.get_event_loop()
                result = await loop.run_in_executor(None, handler, input_data)

            # Record event
            self._record_event(action, input_data, result, True)

            return result

        except Exception as e:
            self._record_event(action, input_data, None, False, str(e))
            raise

    def _create_component_handler(self, method_name: str) -> Callable:
        """Create handler delegating to component method."""
        method = getattr(self._component, method_name)

        async def handler(input_data: Dict[str, Any]) -> Any:
            if asyncio.iscoroutinefunction(method):
                return await method(**input_data)
            else:
                loop = asyncio.get_event_loop()
                return await loop.run_in_executor(
                    None,
                    lambda: method(**input_data)
                )

        return handler

    def _record_event(
        self,
        action: str,
        input_data: Dict[str, Any],
        result: Any,
        success: bool,
        error: Optional[str] = None,
    ) -> None:
        """Record voice event."""
        event = {
            "action": action,
            "success": success,
            "error": error,
            "timestamp": datetime.utcnow().isoformat(),
        }

        self._voice_events.append(event)
        if len(self._voice_events) > self._max_events:
            self._voice_events.pop(0)

    async def _sync_profiles_to_knowledge_graph(self) -> None:
        """Sync speaker profiles to knowledge graph."""
        profiles = []

        if hasattr(self._component, "get_all_profiles"):
            profiles = self._component.get_all_profiles()
            if asyncio.iscoroutine(profiles):
                profiles = await profiles
        elif hasattr(self._component, "voice_memory"):
            profiles = list(self._component.voice_memory.values())

        for profile in profiles:
            speaker_name = profile.get("speaker_name", profile.get("name", "unknown"))
            await self.add_knowledge(
                knowledge_type=KnowledgeType.OBSERVATION,
                data={
                    "type": "speaker_profile",
                    "speaker_name": speaker_name,
                    "profile_summary": {
                        "sample_count": profile.get("sample_count", 0),
                        "freshness": profile.get("freshness", 0),
                        "last_seen": profile.get("last_seen"),
                    },
                },
                tags={"voice", "speaker", speaker_name},
                ttl_seconds=3600,  # 1 hour
            )

    # =========================================================================
    # Common Handlers
    # =========================================================================

    async def _handle_get_status(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """Get component status."""
        status = {
            "component_type": self._component_type.value,
            "agent_name": self.agent_name,
            "capabilities": list(self.capabilities),
            "running": self._running,
            "verified_speakers": len(self._verified_speakers),
            "events": len(self._voice_events),
        }

        if hasattr(self._component, "get_status"):
            component_status = self._component.get_status()
            if asyncio.iscoroutine(component_status):
                component_status = await component_status
            status["component_status"] = component_status

        return status

    async def _handle_get_profiles(
        self,
        input_data: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Get all speaker profiles."""
        profiles = []

        if hasattr(self._component, "get_all_profiles"):
            profiles = self._component.get_all_profiles()
            if asyncio.iscoroutine(profiles):
                profiles = await profiles
        elif hasattr(self._component, "voice_memory"):
            profiles = list(self._component.voice_memory.values())

        return {"profiles": profiles, "count": len(profiles)}

    # =========================================================================
    # Memory Handlers
    # =========================================================================

    async def _handle_check_freshness(
        self,
        input_data: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Check voice sample freshness."""
        speaker = input_data.get("speaker")

        if hasattr(self._component, "check_voice_freshness"):
            # Try with speaker arg first, fall back to no args
            try:
                if speaker:
                    result = self._component.check_voice_freshness(speaker=speaker)
                else:
                    result = self._component.check_voice_freshness()
            except TypeError:
                # Method doesn't accept kwargs, try without
                result = self._component.check_voice_freshness()
            if asyncio.iscoroutine(result):
                result = await result
            return result

        if hasattr(self._component, "startup_freshness_check"):
            result = self._component.startup_freshness_check()
            if asyncio.iscoroutine(result):
                result = await result
            return result

        return {"freshness": None}

    async def _handle_get_memory(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """Get voice memory state."""
        speaker = input_data.get("speaker")

        if speaker and hasattr(self._component, "voice_memory"):
            memory = self._component.voice_memory.get(speaker, {})
            return {"memory": memory}

        if hasattr(self._component, "voice_memory"):
            return {"memory": self._component.voice_memory}

        return {"memory": {}}

    async def _handle_update_memory(
        self,
        input_data: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Update voice memory."""
        speaker = input_data.get("speaker", "")
        audio_data = input_data.get("audio_data")

        if hasattr(self._component, "update_voice_memory"):
            result = self._component.update_voice_memory(speaker, audio_data)
            if asyncio.iscoroutine(result):
                result = await result

            # Share update with other agents
            await self.broadcast(
                message_type=MessageType.KNOWLEDGE_SHARED,
                payload={
                    "source": self.agent_name,
                    "knowledge_type": "voice_memory_update",
                    "speaker": speaker,
                },
            )

            return {"success": True, "result": result}

        return {"success": False, "error": "No update method available"}

    async def _handle_diagnostics(
        self,
        input_data: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Run voice diagnostics."""
        speaker = input_data.get("speaker")

        if hasattr(self._component, "run_comprehensive_diagnostics"):
            result = self._component.run_comprehensive_diagnostics(speaker)
            if asyncio.iscoroutine(result):
                result = await result
            return result

        if hasattr(self._component, "diagnose"):
            result = self._component.diagnose()
            if asyncio.iscoroutine(result):
                result = await result
            return {"diagnostics": result}

        return {"diagnostics": None}

    async def _handle_self_heal(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """Run self-healing on voice profiles."""
        if hasattr(self._component, "autonomous_self_heal"):
            result = self._component.autonomous_self_heal()
            if asyncio.iscoroutine(result):
                result = await result
            return result

        if hasattr(self._component, "self_heal"):
            result = self._component.self_heal()
            if asyncio.iscoroutine(result):
                result = await result
            return {"healed": result}

        return {"healed": False, "error": "No self-heal available"}

    # =========================================================================
    # Verification Handlers
    # =========================================================================

    async def _handle_verify_speaker(
        self,
        input_data: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Verify a speaker's identity."""
        audio_data = input_data.get("audio_data")
        expected_speaker = input_data.get("expected_speaker")
        threshold = input_data.get("threshold", 0.85)

        # Check cache
        if expected_speaker and expected_speaker in self._verified_speakers:
            cached_time = self._verified_speakers[expected_speaker]
            if (datetime.utcnow() - cached_time).total_seconds() < self._verification_cache_ttl:
                return {
                    "verified": True,
                    "speaker": expected_speaker,
                    "cached": True,
                }

        # Perform verification
        result = None

        if hasattr(self._component, "verify_speaker"):
            result = self._component.verify_speaker(
                audio_data,
                expected_speaker=expected_speaker,
                threshold=threshold,
            )
            if asyncio.iscoroutine(result):
                result = await result

        elif hasattr(self._component, "verify"):
            result = self._component.verify(audio_data, expected_speaker)
            if asyncio.iscoroutine(result):
                result = await result

        if result and result.get("verified"):
            speaker = result.get("speaker", expected_speaker)
            self._verified_speakers[speaker] = datetime.utcnow()

            # Broadcast verification success
            await self.broadcast(
                message_type=MessageType.CUSTOM,
                payload={
                    "event": "speaker_verified",
                    "speaker": speaker,
                    "confidence": result.get("confidence", 0),
                },
                priority=MessagePriority.HIGH,
            )

        return result or {"verified": False}

    async def _handle_enroll_speaker(
        self,
        input_data: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Enroll a new speaker."""
        speaker_name = input_data.get("speaker_name", "")
        audio_samples = input_data.get("audio_samples", [])

        if hasattr(self._component, "enroll_speaker"):
            result = self._component.enroll_speaker(speaker_name, audio_samples)
            if asyncio.iscoroutine(result):
                result = await result

            # Store in knowledge graph
            if result and result.get("success") and self.knowledge_graph:
                await self.add_knowledge(
                    knowledge_type=KnowledgeType.OBSERVATION,
                    data={
                        "type": "speaker_enrolled",
                        "speaker_name": speaker_name,
                        "sample_count": len(audio_samples),
                        "enrolled_at": datetime.utcnow().isoformat(),
                    },
                    tags={"voice", "enrollment", speaker_name},
                )

            return result

        if hasattr(self._component, "enroll"):
            result = self._component.enroll(speaker_name, audio_samples)
            if asyncio.iscoroutine(result):
                result = await result
            return {"success": True, "result": result}

        return {"success": False, "error": "No enrollment method available"}

    async def _handle_update_profile(
        self,
        input_data: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Update speaker profile."""
        speaker_name = input_data.get("speaker_name", "")
        audio_data = input_data.get("audio_data")

        if hasattr(self._component, "update_profile"):
            result = self._component.update_profile(speaker_name, audio_data)
            if asyncio.iscoroutine(result):
                result = await result
            return {"success": True, "result": result}

        if hasattr(self._component, "add_sample"):
            result = self._component.add_sample(speaker_name, audio_data)
            if asyncio.iscoroutine(result):
                result = await result
            return {"success": True, "result": result}

        return {"success": False}

    async def _handle_delete_profile(
        self,
        input_data: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Delete speaker profile."""
        speaker_name = input_data.get("speaker_name", "")

        if hasattr(self._component, "delete_profile"):
            result = self._component.delete_profile(speaker_name)
            if asyncio.iscoroutine(result):
                result = await result
            return {"success": True, "result": result}

        return {"success": False, "error": "No delete method available"}

    # =========================================================================
    # Unlock Handlers
    # =========================================================================

    async def _handle_unlock(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """Attempt voice unlock."""
        audio_data = input_data.get("audio_data")

        if hasattr(self._component, "attempt_unlock"):
            result = self._component.attempt_unlock(audio_data)
            if asyncio.iscoroutine(result):
                result = await result

            # Broadcast unlock result
            if result and result.get("unlocked"):
                await self.broadcast(
                    message_type=MessageType.CUSTOM,
                    payload={
                        "event": "screen_unlocked",
                        "speaker": result.get("speaker"),
                        "method": "voice",
                    },
                    priority=MessagePriority.CRITICAL,
                )

            return result

        if hasattr(self._component, "unlock"):
            result = self._component.unlock(audio_data)
            if asyncio.iscoroutine(result):
                result = await result
            return {"unlocked": result}

        return {"unlocked": False, "error": "No unlock method available"}

    async def _handle_unlock_status(
        self,
        input_data: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Get unlock status."""
        if hasattr(self._component, "get_unlock_status"):
            status = self._component.get_unlock_status()
            if asyncio.iscoroutine(status):
                status = await status
            return status

        return {"status": "unknown"}

    async def _handle_configure_unlock(
        self,
        input_data: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Configure unlock settings."""
        config = input_data.get("config", {})

        if hasattr(self._component, "configure"):
            result = self._component.configure(config)
            if asyncio.iscoroutine(result):
                result = await result
            return {"success": True, "result": result}

        return {"success": False}

    # =========================================================================
    # Streaming Handlers
    # =========================================================================

    async def _handle_start_stream(
        self,
        input_data: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Start audio stream."""
        if hasattr(self._component, "start_stream"):
            result = self._component.start_stream()
            if asyncio.iscoroutine(result):
                result = await result
            return {"started": True, "result": result}

        if hasattr(self._component, "start"):
            result = self._component.start()
            if asyncio.iscoroutine(result):
                result = await result
            return {"started": True}

        return {"started": False}

    async def _handle_stop_stream(
        self,
        input_data: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Stop audio stream."""
        if hasattr(self._component, "stop_stream"):
            result = self._component.stop_stream()
            if asyncio.iscoroutine(result):
                result = await result
            return {"stopped": True}

        if hasattr(self._component, "stop"):
            result = self._component.stop()
            if asyncio.iscoroutine(result):
                result = await result
            return {"stopped": True}

        return {"stopped": False}

    async def _handle_transcribe(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """Transcribe audio."""
        audio_data = input_data.get("audio_data")

        if hasattr(self._component, "transcribe"):
            result = self._component.transcribe(audio_data)
            if asyncio.iscoroutine(result):
                result = await result
            return {"transcription": result}

        return {"transcription": None}

    # =========================================================================
    # TTS Handlers
    # =========================================================================

    async def _handle_synthesize(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """Synthesize text to speech."""
        text = input_data.get("text", "")
        voice = input_data.get("voice", "default")

        if hasattr(self._component, "synthesize"):
            result = self._component.synthesize(text, voice=voice)
            if asyncio.iscoroutine(result):
                result = await result
            return {"audio": result}

        return {"audio": None}

    async def _handle_speak(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """Speak text out loud."""
        text = input_data.get("text", "")
        voice = input_data.get("voice", "default")

        if hasattr(self._component, "speak"):
            result = self._component.speak(text, voice=voice)
            if asyncio.iscoroutine(result):
                result = await result
            return {"spoken": True}

        if hasattr(self._component, "say"):
            result = self._component.say(text)
            if asyncio.iscoroutine(result):
                result = await result
            return {"spoken": True}

        return {"spoken": False}

    # =========================================================================
    # Message Handlers
    # =========================================================================

    async def _handle_custom_message(self, message: AgentMessage) -> None:
        """Handle custom messages."""
        event = message.payload.get("event", "")

        if event == "request_verification":
            audio = message.payload.get("audio_data")
            speaker = message.payload.get("expected_speaker")

            result = await self._handle_verify_speaker({
                "audio_data": audio,
                "expected_speaker": speaker,
            })

            if self.message_bus:
                await self.message_bus.respond(
                    message,
                    payload=result,
                    from_agent=self.agent_name,
                )

        elif event == "speaker_verified":
            # Another agent verified a speaker - update cache
            speaker = message.payload.get("speaker")
            if speaker:
                self._verified_speakers[speaker] = datetime.utcnow()

    async def _handle_knowledge_shared(self, message: AgentMessage) -> None:
        """Handle knowledge shared by other agents."""
        source = message.payload.get("source", "")
        if source == self.agent_name:
            return

        knowledge_type = message.payload.get("knowledge_type", "")

        if knowledge_type == "voice_memory_update":
            # Another voice agent updated memory - refresh local cache
            if hasattr(self._component, "refresh_cache"):
                result = self._component.refresh_cache()
                if asyncio.iscoroutine(result):
                    await result

    @property
    def component(self) -> Any:
        """Access wrapped component."""
        return self._component

    @property
    def component_type(self) -> VoiceComponentType:
        """Get component type."""
        return self._component_type


# =============================================================================
# Factory Functions
# =============================================================================

async def create_voice_memory_adapter(
    agent: Optional[Any] = None,
    agent_name: str = "voice_memory",
) -> VoiceSystemAdapter:
    """Create adapter for VoiceMemoryAgent.

    Args:
        agent: Existing agent (creates new if None)
        agent_name: Name for the adapter

    Returns:
        Configured VoiceSystemAdapter
    """
    if agent is None:
        try:
            from agents.voice_memory_agent import VoiceMemoryAgent
            agent = VoiceMemoryAgent()
        except ImportError:
            logger.warning("Could not import VoiceMemoryAgent")
            raise

    return VoiceSystemAdapter(
        voice_component=agent,
        component_type=VoiceComponentType.MEMORY,
        agent_name=agent_name,
    )


async def create_speaker_verification_adapter(
    service: Optional[Any] = None,
    agent_name: str = "speaker_verification",
) -> VoiceSystemAdapter:
    """Create adapter for SpeakerVerificationService.

    Args:
        service: Existing service (creates new if None)
        agent_name: Name for the adapter

    Returns:
        Configured VoiceSystemAdapter
    """
    if service is None:
        try:
            from voice.speaker_verification_service import (
                get_speaker_verification_service,
            )
            service = await get_speaker_verification_service()
        except ImportError:
            logger.warning("Could not import SpeakerVerificationService")
            raise

    return VoiceSystemAdapter(
        voice_component=service,
        component_type=VoiceComponentType.VERIFICATION,
        agent_name=agent_name,
    )


async def create_voice_unlock_adapter(
    integration: Optional[Any] = None,
    agent_name: str = "voice_unlock",
) -> VoiceSystemAdapter:
    """Create adapter for VoiceUnlockIntegration.

    Args:
        integration: Existing integration (creates new if None)
        agent_name: Name for the adapter

    Returns:
        Configured VoiceSystemAdapter
    """
    if integration is None:
        # v93.0: Try multiple import paths with better error handling
        VoiceUnlockIntegration = None
        import_errors = []

        # Try absolute import first (most reliable)
        try:
            from backend.voice.voice_unlock_integration import VoiceUnlockIntegration as VUI
            VoiceUnlockIntegration = VUI
        except ImportError as e:
            import_errors.append(f"backend.voice.voice_unlock_integration: {e}")

        # Try relative import from voice directory
        if VoiceUnlockIntegration is None:
            try:
                from ...voice.voice_unlock_integration import VoiceUnlockIntegration as VUI
                VoiceUnlockIntegration = VUI
            except ImportError as e:
                import_errors.append(f"...voice.voice_unlock_integration: {e}")

        # Try relative import from voice_unlock directory
        if VoiceUnlockIntegration is None:
            try:
                from ...voice_unlock.voice_unlock_integration import VoiceUnlockIntegration as VUI
                VoiceUnlockIntegration = VUI
            except ImportError as e:
                import_errors.append(f"...voice_unlock.voice_unlock_integration: {e}")

        # Try direct module import as last resort
        if VoiceUnlockIntegration is None:
            try:
                import importlib
                mod = importlib.import_module("voice.voice_unlock_integration")
                VoiceUnlockIntegration = mod.VoiceUnlockIntegration
            except (ImportError, AttributeError) as e:
                import_errors.append(f"voice.voice_unlock_integration (direct): {e}")

        if VoiceUnlockIntegration is None:
            logger.warning(
                f"Could not import VoiceUnlockIntegration from any path. "
                f"Errors: {'; '.join(import_errors)}. "
                f"Voice unlock adapter will not be available."
            )
            raise ImportError("VoiceUnlockIntegration not found in any import path")

        integration = VoiceUnlockIntegration()

    return VoiceSystemAdapter(
        voice_component=integration,
        component_type=VoiceComponentType.UNLOCK,
        agent_name=agent_name,
    )
