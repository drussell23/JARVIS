"""
JARVIS Audio Infrastructure
=============================

Layered audio subsystem for real-time voice conversation.

Layers:
    -1: FullDuplexDevice    — Single sounddevice.Stream for synchronized I/O
     0: AudioBus + AEC      — Central routing singleton, echo cancellation
     1: (TTS engines)       — Streaming TTS via Piper, routed through AudioBus
     2: (StreamingSTT)      — Incremental transcription via faster-whisper
     3: TurnDetector        — Adaptive silence-based turn detection
     4: BargeInController   — Interrupt TTS when user speaks
     5: ConversationPipeline — Full conversation orchestrator
     6: ModeDispatcher      — Route between command/conversation/biometric

Usage:
    from backend.audio import get_audio_bus, AudioBus

    bus = get_audio_bus()
    await bus.start()
"""

from backend.audio.audio_bus import (
    AudioBus,
    AudioSink,
    LocalSpeakerSink,
    WebSocketSink,
    get_audio_bus,
    get_audio_bus_safe,
)
from backend.audio.full_duplex_device import DeviceConfig, FullDuplexDevice
from backend.audio.playback_ring_buffer import PlaybackRingBuffer
from backend.audio.turn_detector import TurnDetector
from backend.audio.barge_in_controller import BargeInController
from backend.audio.conversation_pipeline import (
    ConversationPipeline,
    ConversationSession,
    ConversationTurn,
)
from backend.audio.mode_dispatcher import ModeDispatcher, VoiceMode

try:
    from backend.audio.audio_pipeline_bootstrap import (
        start_audio_bus as bootstrap_start_audio_bus,
        wire_conversation_pipeline,
        shutdown as bootstrap_shutdown,
        PipelineHandle,
    )
except ImportError:
    pass

__all__ = [
    "AudioBus",
    "AudioSink",
    "BargeInController",
    "ConversationPipeline",
    "ConversationSession",
    "ConversationTurn",
    "DeviceConfig",
    "FullDuplexDevice",
    "LocalSpeakerSink",
    "ModeDispatcher",
    "PlaybackRingBuffer",
    "TurnDetector",
    "VoiceMode",
    "WebSocketSink",
    "get_audio_bus",
    "get_audio_bus_safe",
    "PipelineHandle",
    "bootstrap_shutdown",
    "bootstrap_start_audio_bus",
    "wire_conversation_pipeline",
]
