import numpy as np
from typing import Optional, Dict, List, Tuple, Union, BinaryIO
import sounddevice as sd
import soundfile as sf
from pydub import AudioSegment
from pydub.playback import play
import io
import tempfile
import os
from datetime import datetime
import asyncio
from dataclasses import dataclass
from enum import Enum
import json
import wave
import pyaudio
from threading import Thread, Event
import queue
from gtts import gTTS
import pyttsx3
import edge_tts

# Fix TensorFlow before importing transformers
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'
os.environ['USE_TORCH'] = '1'
os.environ['USE_TF'] = '0'

# Import centralized model manager
from utils.centralized_model_manager import model_manager

# Try to import whisper and torch
try:
    import whisper
    WHISPER_AVAILABLE = True
except ImportError:
    WHISPER_AVAILABLE = False
    
try:
    import torch
    TORCH_AVAILABLE = True
except ImportError:
    TORCH_AVAILABLE = False

# Try to import transformers with TF fix
try:
    # Fix TensorFlow if needed
    if not hasattr(tf, 'data'):
        class MockData:
            class Dataset:
                @staticmethod
                def from_tensor_slices(*args, **kwargs):
                    return None
        tf.data = MockData()
except Exception:
    pass

try:
    from transformers import pipeline
    TRANSFORMERS_AVAILABLE = True
except ImportError:
    TRANSFORMERS_AVAILABLE = False
from utils.audio_processor import (
    AudioStreamProcessor, VoiceActivityDetector, AudioFeedbackGenerator,
    AudioConfig, AudioMetrics, StreamingAudioProcessor
)

class AudioFormat(Enum):
    """Supported audio formats"""
    WAV = "wav"
    MP3 = "mp3"
    OGG = "ogg"
    WEBM = "webm"
    M4A = "m4a"

class TTSEngine(Enum):
    """Available TTS engines"""
    GTTS = "gtts"  # Google Text-to-Speech
    PYTTSX3 = "pyttsx3"  # Offline TTS
    EDGE_TTS = "edge_tts"  # Microsoft Edge TTS (high quality)

@dataclass
class VoiceConfig:
    """Voice configuration settings"""
    language: str = "en"
    tts_engine: TTSEngine = TTSEngine.EDGE_TTS
    voice_name: Optional[str] = None  # Engine-specific voice
    speech_rate: float = 1.0
    pitch: float = 1.0
    volume: float = 1.0
    wake_word: str = "hey jarvis"
    wake_word_sensitivity: float = 0.5
    noise_threshold: int = 500
    sample_rate: int = 16000
    chunk_size: int = 1024

@dataclass
class TranscriptionResult:
    """Result from speech-to-text transcription"""
    text: str
    language: str
    confidence: float
    segments: Optional[List[Dict]] = None
    duration: Optional[float] = None

@dataclass
class TTSResult:
    """Result from text-to-speech synthesis"""
    audio_data: bytes
    format: AudioFormat
    duration: float
    voice_used: str

class WhisperSTT:
    """Speech-to-Text using OpenAI Whisper"""
    
    def __init__(self, model_size: str = "base"):
        """
        Initialize Whisper STT
        
        Args:
            model_size: Size of Whisper model (tiny, base, small, medium, large)
        """
        self.model_size = model_size
        self.model = None
        self._model_loaded = False
        self.sample_rate = 16000
        
    def _ensure_model_loaded(self):
        """Lazy load the Whisper model"""
        if not self._model_loaded and WHISPER_AVAILABLE:
            try:
                # Use centralized model manager to prevent duplicate loading
                self.model = model_manager.get_whisper_model(self.model_size)
                self._model_loaded = True
            except Exception as e:
                print(f"Failed to load Whisper model: {e}")
                self.model = None
                self._model_loaded = True  # Don't retry
        
    def transcribe(self, audio_data: Union[np.ndarray, bytes, str], language: Optional[str] = None) -> TranscriptionResult:
        """
        Transcribe audio to text
        
        Args:
            audio_data: Audio data as numpy array, bytes, or file path
            language: Optional language code for faster processing
            
        Returns:
            TranscriptionResult with transcribed text and metadata
        """
        # Lazy load model
        self._ensure_model_loaded()
        
        if not self.model:
            return TranscriptionResult(
                text="[Whisper model not available]",
                language="en",
                confidence=0.0,
                segments=[],
                processing_time=0.0
            )
        # Convert input to proper format
        if isinstance(audio_data, bytes):
            # Convert bytes to numpy array
            audio_array = self._bytes_to_array(audio_data)
        elif isinstance(audio_data, str):
            # Load from file
            audio_array, _ = sf.read(audio_data)
        else:
            audio_array = audio_data
            
        # Ensure float32 format
        if audio_array.dtype != np.float32:
            audio_array = audio_array.astype(np.float32)
            
        # Transcribe
        result = self.model.transcribe(
            audio_array,
            language=language,
            task="transcribe"
        )
        
        return TranscriptionResult(
            text=result["text"].strip(),
            language=result["language"],
            confidence=1.0,  # Whisper doesn't provide confidence scores
            segments=result.get("segments", []),
            duration=len(audio_array) / self.sample_rate
        )
    
    def transcribe_stream(self, audio_stream) -> TranscriptionResult:
        """Transcribe audio from a stream"""
        # Collect audio chunks
        audio_chunks = []
        for chunk in audio_stream:
            audio_chunks.append(chunk)
            
        # Combine chunks
        audio_data = np.concatenate(audio_chunks)
        
        return self.transcribe(audio_data)
    
    def _bytes_to_array(self, audio_bytes: bytes) -> np.ndarray:
        """Convert audio bytes to numpy array"""
        # Try to decode with soundfile
        try:
            audio_array, _ = sf.read(io.BytesIO(audio_bytes))
            return audio_array
        except Exception:
            # Fallback to raw PCM
            return np.frombuffer(audio_bytes, dtype=np.float32)

class NaturalTTS:
    """Text-to-Speech with multiple engine support"""
    
    def __init__(self, config: VoiceConfig):
        """Initialize TTS with specified configuration"""
        self.config = config
        self.engines = self._initialize_engines()
        
    def _initialize_engines(self) -> Dict[TTSEngine, any]:
        """Initialize available TTS engines"""
        engines = {}
        
        # Initialize pyttsx3 (offline)
        try:
            engines[TTSEngine.PYTTSX3] = pyttsx3.init()
            # Configure pyttsx3
            engine = engines[TTSEngine.PYTTSX3]
            engine.setProperty('rate', int(200 * self.config.speech_rate))
            engine.setProperty('volume', self.config.volume)
        except Exception:
            print("Warning: pyttsx3 not available")
            
        # gTTS is initialized on-demand
        engines[TTSEngine.GTTS] = None
        
        # edge-tts is async and initialized on-demand
        engines[TTSEngine.EDGE_TTS] = None
        
        return engines
    
    async def synthesize(self, text: str, engine: Optional[TTSEngine] = None) -> TTSResult:
        """
        Synthesize speech from text
        
        Args:
            text: Text to synthesize
            engine: Optional specific engine to use
            
        Returns:
            TTSResult with audio data
        """
        engine = engine or self.config.tts_engine
        
        if engine == TTSEngine.EDGE_TTS:
            return await self._synthesize_edge_tts(text)
        elif engine == TTSEngine.GTTS:
            return self._synthesize_gtts(text)
        elif engine == TTSEngine.PYTTSX3:
            return self._synthesize_pyttsx3(text)
        else:
            raise ValueError(f"Unknown TTS engine: {engine}")
    
    async def _synthesize_edge_tts(self, text: str) -> TTSResult:
        """Synthesize using Microsoft Edge TTS (high quality)"""
        # Get available voices
        voices = await edge_tts.list_voices()
        
        # Select voice based on config
        if self.config.voice_name:
            voice = self.config.voice_name
        else:
            # Default to a natural English voice
            english_voices = [v for v in voices if v["Locale"].startswith("en-")]
            voice = "en-US-JennyNeural" if english_voices else voices[0]["ShortName"]
        
        # Create communication object
        communicate = edge_tts.Communicate(
            text,
            voice,
            rate=f"{int((self.config.speech_rate - 1) * 100):+d}%",
            volume=f"{int((self.config.volume - 1) * 100):+d}%",
            pitch=f"{int((self.config.pitch - 1) * 50):+d}Hz"
        )
        
        # Synthesize to temporary file
        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as tmp_file:
            await communicate.save(tmp_file.name)
            
            # Read audio data
            with open(tmp_file.name, 'rb') as f:
                audio_data = f.read()
                
            # Get duration
            audio = AudioSegment.from_mp3(tmp_file.name)
            duration = len(audio) / 1000.0
            
            # Clean up
            os.unlink(tmp_file.name)
            
        return TTSResult(
            audio_data=audio_data,
            format=AudioFormat.MP3,
            duration=duration,
            voice_used=voice
        )
    
    def _synthesize_gtts(self, text: str) -> TTSResult:
        """Synthesize using Google TTS"""
        # Create gTTS object
        tts = gTTS(
            text=text,
            lang=self.config.language,
            slow=self.config.speech_rate < 0.9
        )
        
        # Save to bytes
        audio_buffer = io.BytesIO()
        tts.write_to_fp(audio_buffer)
        audio_data = audio_buffer.getvalue()
        
        # Get duration (approximate)
        duration = len(text.split()) * 0.5 / self.config.speech_rate
        
        return TTSResult(
            audio_data=audio_data,
            format=AudioFormat.MP3,
            duration=duration,
            voice_used=f"gtts-{self.config.language}"
        )
    
    def _synthesize_pyttsx3(self, text: str) -> TTSResult:
        """Synthesize using pyttsx3 (offline)"""
        engine = self.engines[TTSEngine.PYTTSX3]
        
        # Save to temporary file
        with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as tmp_file:
            engine.save_to_file(text, tmp_file.name)
            engine.runAndWait()
            
            # Read audio data
            with open(tmp_file.name, 'rb') as f:
                audio_data = f.read()
                
            # Get duration
            with wave.open(tmp_file.name, 'rb') as wav_file:
                frames = wav_file.getnframes()
                rate = wav_file.getframerate()
                duration = frames / float(rate)
                
            # Clean up
            os.unlink(tmp_file.name)
            
        # Get voice info
        voices = engine.getProperty('voices')
        current_voice = engine.getProperty('voice')
        voice_name = next((v.name for v in voices if v.id == current_voice), "default")
        
        return TTSResult(
            audio_data=audio_data,
            format=AudioFormat.WAV,
            duration=duration,
            voice_used=voice_name
        )
    
    def play_audio(self, audio_result: TTSResult):
        """Play synthesized audio"""
        # Convert to AudioSegment for playback
        if audio_result.format == AudioFormat.MP3:
            audio = AudioSegment.from_mp3(io.BytesIO(audio_result.audio_data))
        elif audio_result.format == AudioFormat.WAV:
            audio = AudioSegment.from_wav(io.BytesIO(audio_result.audio_data))
        else:
            raise ValueError(f"Unsupported format for playback: {audio_result.format}")
            
        # Play audio
        play(audio)

class WakeWordDetector:
    """Wake word detection for hands-free activation"""
    
    def __init__(self, config: VoiceConfig):
        """Initialize wake word detector"""
        self.config = config
        self.wake_word = config.wake_word.lower()
        self.is_listening = False
        self.detection_callback = None
        self.audio_queue = queue.Queue()
        self.whisper_model = None
        self._model_loaded = False
        
    def _ensure_model_loaded(self):
        """Lazy load the Whisper model for wake word detection"""
        if not self._model_loaded and WHISPER_AVAILABLE:
            try:
                # Use centralized model manager - shares the same tiny model instance
                self.whisper_model = model_manager.get_whisper_tiny()
                self._model_loaded = True
            except Exception as e:
                print(f"Failed to load wake word Whisper model: {e}")
                self._model_loaded = True  # Don't retry
        
    def start_listening(self, callback):
        """Start listening for wake word"""
        self.detection_callback = callback
        self.is_listening = True
        
        # Start audio capture thread
        self.capture_thread = Thread(target=self._capture_audio, daemon=True)
        self.capture_thread.start()

        # Start detection thread
        self.detection_thread = Thread(target=self._detect_wake_word, daemon=True)
        self.detection_thread.start()
        
    def stop_listening(self):
        """Stop listening for wake word"""
        self.is_listening = False
        if hasattr(self, 'capture_thread'):
            self.capture_thread.join()
        if hasattr(self, 'detection_thread'):
            self.detection_thread.join()
            
    def _capture_audio(self):
        """Capture audio continuously"""
        p = pyaudio.PyAudio()
        
        stream = p.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=self.config.sample_rate,
            input=True,
            frames_per_buffer=self.config.chunk_size
        )
        
        print(f"Listening for wake word: '{self.wake_word}'")
        
        while self.is_listening:
            try:
                data = stream.read(self.config.chunk_size, exception_on_overflow=False)
                self.audio_queue.put(data)
            except Exception as e:
                print(f"Audio capture error: {e}")
                
        stream.stop_stream()
        stream.close()
        p.terminate()
        
    def _detect_wake_word(self):
        """Detect wake word in audio stream"""
        audio_buffer = []
        buffer_duration = 2  # seconds
        buffer_size = int(self.config.sample_rate * buffer_duration)
        
        while self.is_listening:
            try:
                # Get audio chunk
                if not self.audio_queue.empty():
                    chunk = self.audio_queue.get()
                    audio_buffer.append(chunk)
                    
                    # Maintain buffer size
                    total_samples = sum(len(chunk) // 2 for chunk in audio_buffer)
                    if total_samples > buffer_size:
                        # Remove old chunks
                        while total_samples > buffer_size and audio_buffer:
                            removed = audio_buffer.pop(0)
                            total_samples -= len(removed) // 2
                            
                    # Check for wake word every 0.5 seconds
                    if total_samples >= self.config.sample_rate * 0.5:
                        # Convert to numpy array
                        audio_data = b''.join(audio_buffer)
                        audio_array = np.frombuffer(audio_data, dtype=np.int16).astype(np.float32) / 32768.0
                        
                        # Lazy load model
                        self._ensure_model_loaded()
                        
                        if not self.whisper_model:
                            continue
                            
                        # Transcribe
                        result = self.whisper_model.transcribe(
                            audio_array,
                            language="en",
                            task="transcribe"
                        )
                        
                        transcribed_text = result["text"].lower().strip()
                        
                        # Check for wake word
                        if self.wake_word in transcribed_text:
                            print(f"Wake word detected: {transcribed_text}")
                            if self.detection_callback:
                                self.detection_callback()
                            # Clear buffer after detection
                            audio_buffer = []
                            
                else:
                    # Small delay to prevent busy waiting
                    asyncio.sleep(0.01)
                    
            except Exception as e:
                print(f"Wake word detection error: {e}")

class VoiceCommandProcessor:
    """Process voice commands with context awareness"""
    
    def __init__(self, stt: WhisperSTT, tts: NaturalTTS, config: VoiceConfig):
        """Initialize voice command processor"""
        self.stt = stt
        self.tts = tts
        self.config = config
        self.is_recording = False
        self.audio_queue = queue.Queue()
        
    def start_recording(self) -> Thread:
        """Start recording audio for command"""
        self.is_recording = True
        self.audio_queue = queue.Queue()  # Clear queue
        
        # Start recording thread
        record_thread = Thread(target=self._record_audio, daemon=True)
        record_thread.start()
        
        return record_thread
        
    def stop_recording(self) -> Optional[TranscriptionResult]:
        """Stop recording and transcribe"""
        self.is_recording = False
        
        # Collect all audio chunks
        audio_chunks = []
        while not self.audio_queue.empty():
            audio_chunks.append(self.audio_queue.get())
            
        if not audio_chunks:
            return None
            
        # Combine chunks
        audio_data = b''.join(audio_chunks)
        audio_array = np.frombuffer(audio_data, dtype=np.int16).astype(np.float32) / 32768.0
        
        # Transcribe
        return self.stt.transcribe(audio_array, language=self.config.language)
        
    def _record_audio(self):
        """Record audio until stopped"""
        p = pyaudio.PyAudio()
        
        stream = p.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=self.config.sample_rate,
            input=True,
            frames_per_buffer=self.config.chunk_size
        )
        
        print("Recording... (speak now)")
        silence_chunks = 0
        max_silence_chunks = int(2 * self.config.sample_rate / self.config.chunk_size)  # 2 seconds
        
        while self.is_recording:
            try:
                data = stream.read(self.config.chunk_size, exception_on_overflow=False)
                self.audio_queue.put(data)
                
                # Check for silence (simple volume-based)
                audio_chunk = np.frombuffer(data, dtype=np.int16)
                volume = np.abs(audio_chunk).mean()
                
                if volume < self.config.noise_threshold:
                    silence_chunks += 1
                    if silence_chunks > max_silence_chunks:
                        print("Silence detected, stopping recording")
                        self.is_recording = False
                else:
                    silence_chunks = 0
                    
            except Exception as e:
                print(f"Recording error: {e}")
                
        stream.stop_stream()
        stream.close()
        p.terminate()
        
    async def process_voice_command(self, callback) -> Dict:
        """
        Record and process a voice command
        
        Args:
            callback: Async function to process the transcribed text
            
        Returns:
            Dict with transcription and response
        """
        # Start recording
        record_thread = self.start_recording()
        
        # Wait for recording to complete
        record_thread.join(timeout=10)  # Max 10 seconds recording
        
        # Stop and transcribe
        transcription = self.stop_recording()
        
        if not transcription:
            return {"error": "No audio captured"}
            
        print(f"Transcribed: {transcription.text}")
        
        # Process command
        response = await callback(transcription.text)
        
        # Synthesize response
        tts_result = await self.tts.synthesize(response)
        
        # Play response
        self.tts.play_audio(tts_result)
        
        return {
            "transcription": transcription.text,
            "response": response,
            "audio_duration": transcription.duration,
            "response_duration": tts_result.duration
        }

class VoiceAssistant:
    """Complete voice assistant integrating all components"""
    
    def __init__(self, config: Optional[VoiceConfig] = None):
        """Initialize voice assistant"""
        self.config = config or VoiceConfig()
        
        # Initialize components
        self.stt = WhisperSTT(model_size="base")
        self.tts = NaturalTTS(self.config)
        self.wake_word_detector = WakeWordDetector(self.config)
        self.command_processor = VoiceCommandProcessor(self.stt, self.tts, self.config)
        
        # Initialize audio processing
        audio_config = AudioConfig(
            sample_rate=self.config.sample_rate,
            chunk_size=self.config.chunk_size,
            noise_reduction=True,
            vad_enabled=True,
            vad_threshold=self.config.noise_threshold / 32768.0  # Convert to float32 range
        )
        self.audio_processor = AudioStreamProcessor(audio_config)
        self.vad = VoiceActivityDetector(audio_config)
        self.audio_feedback = AudioFeedbackGenerator(self.config.sample_rate)
        
        # State
        self.is_active = False
        self.command_callback = None
        
        # Set up VAD callbacks
        self.vad.on_speech_start = self._on_speech_detected
        self.vad.on_speech_end = self._on_speech_end
        
    def set_command_callback(self, callback):
        """Set callback for processing commands"""
        self.command_callback = callback
        
    def start(self):
        """Start the voice assistant"""
        self.is_active = True
        
        # Start audio processor
        self.audio_processor.start()
        
        # Calibrate noise
        print("Calibrating noise level...")
        self._play_feedback("beep")
        self.audio_processor.calibrate_noise(duration=1.0)
        self._play_feedback("success")
        
        # Start wake word detection
        self.wake_word_detector.start_listening(self._on_wake_word_detected)
        
        print(f"Voice assistant started. Say '{self.config.wake_word}' to activate.")
        
    def stop(self):
        """Stop the voice assistant"""
        self.is_active = False
        self.audio_processor.stop()
        self.wake_word_detector.stop_listening()
        print("Voice assistant stopped.")
        
    def _on_wake_word_detected(self):
        """Handle wake word detection"""
        if not self.is_active:
            return
            
        print("Wake word detected! Listening for command...")
        
        # Process voice command
        asyncio.create_task(self._process_command())
        
    async def _process_command(self):
        """Process a voice command after wake word"""
        if not self.command_callback:
            print("No command callback set")
            return
            
        # Give audio feedback
        await self._play_activation_sound()
        
        # Process command
        result = await self.command_processor.process_voice_command(self.command_callback)
        
        print(f"Command processed: {result}")
        
    async def _play_activation_sound(self):
        """Play a sound to indicate activation"""
        # Simple beep using TTS
        activation_phrase = "Yes?"
        tts_result = await self.tts.synthesize(activation_phrase)
        self.tts.play_audio(tts_result)
        
    async def speak(self, text: str):
        """Make the assistant speak"""
        tts_result = await self.tts.synthesize(text)
        self.tts.play_audio(tts_result)
        
    def transcribe_audio_file(self, file_path: str) -> TranscriptionResult:
        """Transcribe an audio file"""
        return self.stt.transcribe(file_path)
    
    def _on_speech_detected(self):
        """Called when speech is detected"""
        print("Speech detected...")
        self._play_feedback("listening")
        
    def _on_speech_end(self, speech_audio: np.ndarray):
        """Called when speech ends"""
        print("Processing speech...")
        self._play_feedback("processing")
        
        # Process the speech
        if self.command_callback:
            # Transcribe
            result = self.stt.transcribe(speech_audio)
            
            # Process command
            asyncio.create_task(self._process_speech_command(result.text))
            
    async def _process_speech_command(self, text: str):
        """Process transcribed speech command"""
        if self.command_callback:
            response = await self.command_callback(text)
            await self.speak(response)
            self._play_feedback("success")
            
    def _play_feedback(self, feedback_type: str):
        """Play audio feedback"""
        feedback_audio = self.audio_feedback.get_feedback(feedback_type)
        if feedback_audio is not None:
            # Convert to int16 for playback
            audio_int16 = (feedback_audio * 32767).astype(np.int16)
            
            # Play using PyAudio
            p = pyaudio.PyAudio()
            stream = p.open(
                format=pyaudio.paInt16,
                channels=1,
                rate=self.config.sample_rate,
                output=True
            )
            
            stream.write(audio_int16.tobytes())
            stream.stop_stream()
            stream.close()
            p.terminate()
            
    def calibrate_noise(self, duration: float = 1.0):
        """Calibrate noise profile"""
        self._play_feedback("beep")
        print(f"Calibrating noise for {duration} seconds. Please remain quiet...")
        self.audio_processor.calibrate_noise(duration)
        self._play_feedback("double_beep")
        print("Noise calibration complete.")

# Example usage and testing
if __name__ == "__main__":
    import asyncio
    
    async def test_voice_assistant():
        # Create voice assistant
        config = VoiceConfig(
            tts_engine=TTSEngine.EDGE_TTS,
            wake_word="hey jarvis",
            language="en"
        )
        assistant = VoiceAssistant(config)
        
        # Test TTS
        print("Testing text-to-speech...")
        await assistant.speak("Hello! I am your AI assistant. How can I help you today?")
        
        # Test STT with a sample
        print("\nTesting speech-to-text...")
        # This would normally use a real audio file
        # result = assistant.transcribe_audio_file("sample.wav")
        # print(f"Transcribed: {result.text}")
        
        # Set up command processing
        async def process_command(text):
            return f"You said: {text}. I'm processing your request."
        
        assistant.set_command_callback(process_command)
        
        # Start assistant (commented out for testing)
        # assistant.start()
        # await asyncio.sleep(30)  # Run for 30 seconds
        # assistant.stop()
        
    # Run test
    asyncio.run(test_voice_assistant())