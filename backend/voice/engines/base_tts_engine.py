"""
Base TTS Engine Interface

This module defines the common interface and data structures for all Text-to-Speech engines
in the voice processing system. It provides abstract base classes and configuration objects
that ensure consistent behavior across different TTS implementations.

The module includes:
- TTSEngine enum for supported engine types
- TTSConfig dataclass for engine configuration
- TTSResult dataclass for synthesis results
- BaseTTSEngine abstract base class for implementation

Example:
    >>> from backend.voice.engines.base_tts_engine import TTSEngine, TTSConfig
    >>> config = TTSConfig(name="my_tts", engine=TTSEngine.GTTS, language="en")
    >>> # Use config with concrete TTS engine implementation
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import AsyncIterator, Dict, List, Optional

logger = logging.getLogger(__name__)


class TTSEngine(Enum):
    """Enumeration of supported Text-to-Speech engines.
    
    This enum defines all the TTS engines that can be used in the system,
    each with different characteristics and use cases.
    
    Attributes:
        GTTS: Google Text-to-Speech (free, online, requires internet)
        COQUI: Coqui TTS (local, neural networks, high quality)
        PYTTSX3: System TTS (local, fast, uses OS speech synthesis)
        ELEVENLABS: ElevenLabs (premium, online, very high quality)
        MACOS: macOS native speech synthesis (local, fast, macOS only)
    """

    GTTS = "gtts"  # Google Text-to-Speech (free, online)
    COQUI = "coqui"  # Coqui TTS (local, neural)
    PYTTSX3 = "pyttsx3"  # System TTS (local, fast)
    ELEVENLABS = "elevenlabs"  # ElevenLabs (premium, online)
    MACOS = "macos"  # macOS native (local, fast)
    PIPER = "piper"  # Piper TTS (local, neural, streaming)
    VOICEAI = "voiceai"  # Voice.ai WebSocket streaming (R&D spike)


@dataclass
class TTSConfig:
    """Configuration parameters for TTS engine initialization.
    
    This dataclass contains all the necessary configuration parameters
    to initialize and customize a TTS engine's behavior.
    
    Attributes:
        name: Human-readable name for this TTS configuration
        engine: The TTS engine type to use (from TTSEngine enum)
        language: Language code for speech synthesis (default: "en")
        voice: Specific voice name/ID to use (engine-dependent)
        speed: Speech rate multiplier, 1.0 = normal speed
        pitch: Pitch multiplier, 1.0 = normal pitch
        volume: Volume multiplier, 1.0 = normal volume
        sample_rate: Audio sample rate in Hz for output
        model_path: Path to local model files (for local engines)
        api_key: API key for cloud-based engines
    
    Example:
        >>> config = TTSConfig(
        ...     name="fast_english",
        ...     engine=TTSEngine.PYTTSX3,
        ...     language="en",
        ...     speed=1.2,
        ...     volume=0.8
        ... )
    """

    name: str
    engine: TTSEngine
    language: str = "en"
    voice: Optional[str] = None
    speed: float = 1.0
    pitch: float = 1.0
    volume: float = 1.0
    sample_rate: int = 22050
    model_path: Optional[str] = None
    api_key: Optional[str] = None


@dataclass
class TTSResult:
    """Result object containing synthesized speech and metadata.
    
    This dataclass encapsulates the output of a TTS synthesis operation,
    including the audio data and various metadata about the synthesis process.
    
    Attributes:
        audio_data: Raw audio bytes in the specified format
        sample_rate: Sample rate of the audio data in Hz
        duration_ms: Duration of the synthesized audio in milliseconds
        latency_ms: Time taken to synthesize the audio in milliseconds
        engine: The TTS engine that generated this result
        voice: Name/ID of the voice used for synthesis
        metadata: Additional engine-specific metadata
    
    Example:
        >>> result = TTSResult(
        ...     audio_data=b'audio_bytes_here',
        ...     sample_rate=22050,
        ...     duration_ms=2500.0,
        ...     latency_ms=150.0,
        ...     engine=TTSEngine.GTTS,
        ...     voice="en-US-Standard-A",
        ...     metadata={"format": "wav", "bitrate": 128}
        ... )
    """

    audio_data: bytes
    sample_rate: int
    duration_ms: float
    latency_ms: float
    engine: TTSEngine
    voice: str
    metadata: Dict


@dataclass
class TTSChunk:
    """A chunk of streaming TTS audio output.

    Used by synthesize_stream() to yield audio incrementally as it's generated.
    Enables low-latency playback by starting output before full synthesis completes.
    """

    audio_data: bytes
    chunk_index: int
    is_final: bool
    sample_rate: int
    duration_ms: Optional[float] = None
    metadata: Dict = field(default_factory=dict)


class BaseTTSEngine(ABC):
    """Abstract base class for all TTS engine implementations.
    
    This class defines the common interface that all TTS engines must implement.
    It provides a consistent API for text-to-speech synthesis across different
    underlying technologies and services.
    
    All concrete TTS engine implementations should inherit from this class
    and implement all abstract methods.
    
    Attributes:
        config: The TTSConfig object used to initialize this engine
        initialized: Boolean flag indicating if the engine is ready for use
    
    Example:
        >>> class MyTTSEngine(BaseTTSEngine):
        ...     async def initialize(self):
        ...         # Implementation here
        ...         self.initialized = True
        ...     
        ...     async def synthesize(self, text: str) -> TTSResult:
        ...         # Implementation here
        ...         pass
    """

    def __init__(self, config: TTSConfig) -> None:
        """Initialize the base TTS engine with configuration.
        
        Args:
            config: TTSConfig object containing engine parameters
        """
        self.config = config
        self.initialized = False

    @abstractmethod
    async def initialize(self) -> None:
        """Initialize the TTS engine and prepare it for synthesis.
        
        This method should perform any necessary setup operations such as:
        - Loading models or connecting to services
        - Validating configuration parameters
        - Setting up audio processing pipelines
        - Authenticating with external APIs
        
        After successful initialization, the engine should be ready to
        synthesize speech from text.
        
        Raises:
            TTSEngineError: If initialization fails due to configuration
                or resource issues
            ConnectionError: If unable to connect to external services
            FileNotFoundError: If required model files are missing
        """

    @abstractmethod
    async def synthesize(self, text: str) -> TTSResult:
        """Synthesize speech from input text.
        
        This is the core method that converts text into speech audio.
        The implementation should handle text preprocessing, synthesis,
        and audio post-processing according to the engine's capabilities.
        
        Args:
            text: The text string to convert to speech. Should be
                preprocessed and ready for synthesis.
        
        Returns:
            TTSResult object containing the synthesized audio data
            and associated metadata.
        
        Raises:
            TTSEngineError: If synthesis fails due to engine issues
            ValueError: If the input text is invalid or empty
            ConnectionError: If unable to reach external TTS service
            RuntimeError: If the engine is not properly initialized
        
        Example:
            >>> result = await engine.synthesize("Hello, world!")
            >>> print(f"Generated {len(result.audio_data)} bytes of audio")
            >>> print(f"Duration: {result.duration_ms}ms")
        """

    @abstractmethod
    async def get_available_voices(self) -> List[str]:
        """Retrieve list of available voices for this engine.
        
        Returns a list of voice names/IDs that can be used with this
        engine. The format and naming convention of voice identifiers
        is engine-specific.
        
        Returns:
            List of voice identifiers available for this engine.
            Empty list if no voices are available or if the engine
            doesn't support voice selection.
        
        Raises:
            TTSEngineError: If unable to retrieve voice list
            ConnectionError: If unable to reach external service
            RuntimeError: If the engine is not properly initialized
        
        Example:
            >>> voices = await engine.get_available_voices()
            >>> print(f"Available voices: {voices}")
            ['en-US-Standard-A', 'en-US-Standard-B', 'en-GB-Standard-A']
        """

    @abstractmethod
    async def cleanup(self) -> None:
        """Clean up engine resources and perform shutdown operations.
        
        This method should release any resources held by the engine such as:
        - Closing network connections
        - Freeing memory allocated for models
        - Stopping background threads or processes
        - Saving any necessary state
        
        After cleanup, the engine should not be used for synthesis
        until re-initialized.
        
        Raises:
            TTSEngineError: If cleanup operations fail
        """

    async def synthesize_stream(self, text: str) -> AsyncIterator[TTSChunk]:
        """Stream synthesized audio in chunks for low-latency playback.

        Default implementation wraps synthesize() and yields a single chunk.
        Override in subclasses for true streaming (e.g., Piper).

        Args:
            text: Text to synthesize.

        Yields:
            TTSChunk objects with audio data.
        """
        result = await self.synthesize(text)
        yield TTSChunk(
            audio_data=result.audio_data,
            chunk_index=0,
            is_final=True,
            sample_rate=result.sample_rate,
            duration_ms=result.duration_ms,
        )

    def is_initialized(self) -> bool:
        """Check if the engine has been properly initialized.

        Returns:
            True if the engine is initialized and ready for synthesis,
            False otherwise.

        Example:
            >>> if engine.is_initialized():
            ...     result = await engine.synthesize("Hello")
            ... else:
            ...     await engine.initialize()
        """
        return self.initialized