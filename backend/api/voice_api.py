from fastapi import (
    APIRouter,
    UploadFile,
    File,
    WebSocket,
    WebSocketDisconnect,
    HTTPException,
)
from fastapi.responses import StreamingResponse, Response
from pydantic import BaseModel
from typing import Optional, Dict, List
import asyncio
import json
import base64
import io
import logging
import os
import sys
from datetime import datetime

logger = logging.getLogger(__name__)

# Add graceful handler import
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    from graceful_http_handler import graceful_endpoint
except ImportError:
    # Fallback if graceful handler is not available
    def graceful_endpoint(func):
        return func

from engines.voice_engine import (
    VoiceAssistant,
    VoiceConfig,
    TTSEngine,
    AudioFormat,
    TranscriptionResult,
    TTSResult,
)
from chatbots._archived_local_models.intelligent_chatbot import IntelligentChatbot

# Import base Chatbot type
from typing import Protocol

class Chatbot(Protocol):
    """Protocol for chatbot interface"""
    async def chat(self, user_input: str) -> str:
        ...

class TTSRequest(BaseModel):
    text: str
    engine: Optional[str] = "edge_tts"
    voice: Optional[str] = None
    speech_rate: Optional[float] = 1.0
    pitch: Optional[float] = 1.0
    volume: Optional[float] = 1.0
    format: Optional[str] = "mp3"

class STTResponse(BaseModel):
    text: str
    language: str
    confidence: float
    duration: Optional[float] = None
    segments: Optional[List[Dict]] = None

class VoiceCommandRequest(BaseModel):
    audio_data: str  # Base64 encoded audio
    format: Optional[str] = "wav"
    language: Optional[str] = "en"

class VoiceConfigUpdate(BaseModel):
    wake_word: Optional[str] = None
    tts_engine: Optional[str] = None
    voice_name: Optional[str] = None
    speech_rate: Optional[float] = None
    language: Optional[str] = None

class VoiceAPI:
    """API for voice interaction capabilities"""

    def __init__(self, chatbot: Chatbot):
        """Initialize Voice API with chatbot integration"""
        self.chatbot = chatbot
        self.voice_config = VoiceConfig()
        self.voice_assistant = VoiceAssistant(self.voice_config)
        self.router = APIRouter()
        self.websocket_clients = set()

        # Set command callback
        self.voice_assistant.set_command_callback(self._process_voice_command)

        # Register routes
        self._register_routes()

    def _register_routes(self):
        """Register API routes"""
        # Text-to-Speech
        self.router.add_api_route("/tts", self.synthesize_speech, methods=["POST"])
        self.router.add_api_route("/tts/voices", self.list_voices, methods=["GET"])

        # Speech-to-Text
        self.router.add_api_route("/stt", self.transcribe_audio, methods=["POST"])
        self.router.add_api_route("/stt/file", self.transcribe_file, methods=["POST"])

        # Voice Commands
        self.router.add_api_route(
            "/voice/command", self.process_voice_command, methods=["POST"]
        )
        self.router.add_api_route(
            "/voice/config", self.update_voice_config, methods=["POST"]
        )
        self.router.add_api_route(
            "/voice/config", self.get_voice_config, methods=["GET"]
        )

        # Wake Word
        self.router.add_api_route(
            "/voice/wake/start", self.start_wake_word, methods=["POST"]
        )
        self.router.add_api_route(
            "/voice/wake/stop", self.stop_wake_word, methods=["POST"]
        )
        self.router.add_api_route(
            "/voice/wake/status", self.wake_word_status, methods=["GET"]
        )

        # WebSocket for real-time voice
        self.router.add_api_websocket_route("/voice/stream", self.voice_stream_endpoint)

        # Audio processing endpoints
        self.router.add_api_route(
            "/voice/audio/calibrate", self.calibrate_noise, methods=["POST"]
        )
        self.router.add_api_route(
            "/voice/audio/metrics", self.get_audio_metrics, methods=["GET"]
        )
        self.router.add_api_route(
            "/voice/audio/feedback", self.test_feedback, methods=["POST"]
        )

    @graceful_endpoint
    async def synthesize_speech(self, request: TTSRequest) -> Response:
        """Synthesize speech from text"""
        try:
            # Map engine string to enum
            engine = (
                TTSEngine(request.engine)
                if request.engine
                else self.voice_config.tts_engine
            )

            # Update config temporarily
            original_config = self.voice_config
            temp_config = VoiceConfig(
                tts_engine=engine,
                voice_name=request.voice,
                speech_rate=(
                    request.speech_rate
                    if request.speech_rate is not None
                    else self.voice_config.speech_rate
                ),
                pitch=(
                    request.pitch
                    if request.pitch is not None
                    else self.voice_config.pitch
                ),
                volume=(
                    request.volume
                    if request.volume is not None
                    else self.voice_config.volume
                ),
            )

            # Create TTS with temp config
            tts = self.voice_assistant.tts
            tts.config = temp_config

            # Synthesize
            result = await tts.synthesize(request.text, engine)

            # Restore original config
            tts.config = original_config

            # Return audio data
            media_type = {
                AudioFormat.MP3: "audio/mpeg",
                AudioFormat.WAV: "audio/wav",
                AudioFormat.OGG: "audio/ogg",
            }.get(result.format, "audio/mpeg")

            return Response(
                content=result.audio_data,
                media_type=media_type,
                headers={
                    "Content-Disposition": f"inline; filename=speech.{result.format.value}",
                    "X-Voice-Used": result.voice_used,
                    "X-Duration": str(result.duration),
                },
            )

        except Exception as e:
            raise  # Graceful handler will catch this

    @graceful_endpoint
    async def list_voices(self) -> Dict:
        """List available TTS voices"""
        try:
            # Get Edge TTS voices
            import edge_tts

            voices = await edge_tts.list_voices()

            # Organize by language
            voices_by_language = {}
            for voice in voices:
                lang = voice["Locale"]
                if lang not in voices_by_language:
                    voices_by_language[lang] = []
                voices_by_language[lang].append(
                    {
                        "name": voice["ShortName"],
                        "display_name": voice["FriendlyName"],
                        "gender": voice["Gender"],
                        "locale": voice["Locale"],
                    }
                )

            return {
                "engines": {
                    "edge_tts": {"voices": voices_by_language, "total": len(voices)},
                    "gtts": {
                        "languages": [
                            "en",
                            "es",
                            "fr",
                            "de",
                            "it",
                            "pt",
                            "ru",
                            "ja",
                            "ko",
                            "zh",
                        ],
                        "info": "gTTS supports many languages but has limited voice options",
                    },
                    "pyttsx3": {"info": "System voices vary by operating system"},
                },
                "current_engine": self.voice_config.tts_engine.value,
            }

        except Exception as e:
            raise  # Graceful handler will catch this

    @graceful_endpoint
    async def transcribe_audio(self, request: VoiceCommandRequest) -> STTResponse:
        """Transcribe audio from base64 data"""
        try:
            # Decode base64 audio
            audio_data = base64.b64decode(request.audio_data)

            # Transcribe
            result = self.voice_assistant.stt.transcribe(
                audio_data, language=request.language
            )

            return STTResponse(
                text=result.text,
                language=result.language,
                confidence=result.confidence,
                duration=result.duration,
                segments=result.segments,
            )

        except Exception as e:
            raise  # Graceful handler will catch this

    async def transcribe_file(self, file: UploadFile = File(...)) -> STTResponse:
        """Transcribe audio from uploaded file"""
        try:
            # Read file content
            audio_data = await file.read()

            # Save to temporary file for processing
            import tempfile

            with tempfile.NamedTemporaryFile(
                delete=False, suffix=f".{file.filename.split('.')[-1]}"
            ) as tmp:
                tmp.write(audio_data)
                tmp_path = tmp.name

            # Transcribe
            result = self.voice_assistant.stt.transcribe(tmp_path)

            # Clean up
            os.unlink(tmp_path)

            return STTResponse(
                text=result.text,
                language=result.language,
                confidence=result.confidence,
                duration=result.duration,
                segments=result.segments,
            )

        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    @graceful_endpoint
    async def process_voice_command(self, request: VoiceCommandRequest) -> Dict:
        """Process a voice command"""
        try:
            # Decode audio
            audio_data = base64.b64decode(request.audio_data)

            # Transcribe
            transcription = self.voice_assistant.stt.transcribe(
                audio_data, language=request.language
            )

            # Process with chatbot
            response_data = self.chatbot.generate_response_with_context(
                transcription.text
            )

            # Synthesize response
            tts_result = await self.voice_assistant.tts.synthesize(
                response_data["response"]
            )

            return {
                "transcription": {
                    "text": transcription.text,
                    "language": transcription.language,
                    "confidence": transcription.confidence,
                },
                "response": response_data,
                "audio_response": {
                    "data": base64.b64encode(tts_result.audio_data).decode(),
                    "format": tts_result.format.value,
                    "duration": tts_result.duration,
                },
            }

        except Exception as e:
            raise  # Graceful handler will catch this

    async def _process_voice_command(self, text: str) -> str:
        """Internal callback for voice assistant"""
        response_data = self.chatbot.generate_response_with_context(text)
        return response_data["response"]

    async def update_voice_config(self, config: VoiceConfigUpdate) -> Dict:
        """Update voice configuration"""
        updates = []

        if config.wake_word:
            self.voice_config.wake_word = config.wake_word
            self.voice_assistant.wake_word_detector.wake_word = config.wake_word.lower()
            updates.append(f"Wake word updated to '{config.wake_word}'")

        if config.tts_engine:
            try:
                self.voice_config.tts_engine = TTSEngine(config.tts_engine)
                updates.append(f"TTS engine updated to {config.tts_engine}")
            except ValueError:
                raise HTTPException(
                    status_code=400, detail=f"Invalid TTS engine: {config.tts_engine}"
                )

        if config.voice_name:
            self.voice_config.voice_name = config.voice_name
            updates.append(f"Voice updated to {config.voice_name}")

        if config.speech_rate is not None:
            self.voice_config.speech_rate = config.speech_rate
            updates.append(f"Speech rate updated to {config.speech_rate}")

        if config.language:
            self.voice_config.language = config.language
            updates.append(f"Language updated to {config.language}")

        return {
            "message": "Voice configuration updated",
            "updates": updates,
            "current_config": {
                "wake_word": self.voice_config.wake_word,
                "tts_engine": self.voice_config.tts_engine.value,
                "voice_name": self.voice_config.voice_name,
                "speech_rate": self.voice_config.speech_rate,
                "language": self.voice_config.language,
            },
        }

    async def get_voice_config(self) -> Dict:
        """Get current voice configuration"""
        return {
            "wake_word": self.voice_config.wake_word,
            "tts_engine": self.voice_config.tts_engine.value,
            "voice_name": self.voice_config.voice_name,
            "speech_rate": self.voice_config.speech_rate,
            "pitch": self.voice_config.pitch,
            "volume": self.voice_config.volume,
            "language": self.voice_config.language,
            "sample_rate": self.voice_config.sample_rate,
            "noise_threshold": self.voice_config.noise_threshold,
        }

    async def start_wake_word(self) -> Dict:
        """Start wake word detection"""
        if not self.voice_assistant.is_active:
            self.voice_assistant.start()
            return {"status": "started", "wake_word": self.voice_config.wake_word}
        else:
            return {
                "status": "already_running",
                "wake_word": self.voice_config.wake_word,
            }

    async def stop_wake_word(self) -> Dict:
        """Stop wake word detection"""
        if self.voice_assistant.is_active:
            self.voice_assistant.stop()
            return {"status": "stopped"}
        else:
            return {"status": "not_running"}

    async def wake_word_status(self) -> Dict:
        """Get wake word detection status"""
        return {
            "is_active": self.voice_assistant.is_active,
            "wake_word": self.voice_config.wake_word,
            "is_listening": self.voice_assistant.wake_word_detector.is_listening,
        }

    async def voice_stream_endpoint(self, websocket: WebSocket):
        """WebSocket endpoint for real-time voice streaming"""
        await websocket.accept()
        self.websocket_clients.add(websocket)

        # WebSocket idle timeout protection
        idle_timeout = float(os.getenv("TIMEOUT_WEBSOCKET_IDLE", "300.0"))  # 5 min default

        try:
            while True:
                # Receive audio data with timeout
                try:
                    data = await asyncio.wait_for(
                        websocket.receive_json(),
                        timeout=idle_timeout
                    )
                except asyncio.TimeoutError:
                    logger.info("Voice stream WebSocket idle timeout, closing connection")
                    break

                if data.get("type") == "audio":
                    # Process audio chunk
                    audio_data = base64.b64decode(data["data"])

                    # Add to processing queue (simplified for example)
                    # In production, you'd accumulate chunks and process when complete

                    # Send acknowledgment
                    await websocket.send_json(
                        {"type": "ack", "timestamp": datetime.now().isoformat()}
                    )

                elif data.get("type") == "command":
                    # Process complete command
                    command_text = data.get("text", "")

                    # Generate response
                    response_data = self.chatbot.generate_response_with_context(
                        command_text
                    )

                    # Send response
                    await websocket.send_json(
                        {
                            "type": "response",
                            "text": response_data["response"],
                            "nlp_analysis": response_data.get("nlp_analysis", {}),
                            "timestamp": datetime.now().isoformat(),
                        }
                    )

                    # Synthesize and send audio
                    tts_result = await self.voice_assistant.tts.synthesize(
                        response_data["response"]
                    )
                    await websocket.send_json(
                        {
                            "type": "audio_response",
                            "data": base64.b64encode(tts_result.audio_data).decode(),
                            "format": tts_result.format.value,
                            "duration": tts_result.duration,
                        }
                    )

        except WebSocketDisconnect:
            self.websocket_clients.remove(websocket)
        except Exception as e:
            await websocket.send_json({"type": "error", "message": str(e)})
            self.websocket_clients.remove(websocket)

    @graceful_endpoint
    async def calibrate_noise(self) -> Dict:
        """Calibrate noise profile for better speech detection"""
        try:
            # Calibrate noise
            self.voice_assistant.calibrate_noise(duration=1.5)

            return {
                "status": "success",
                "message": "Noise calibration complete",
                "noise_threshold": self.voice_config.noise_threshold,
            }

        except Exception as e:
            raise  # Graceful handler will catch this

    @graceful_endpoint
    async def get_audio_metrics(self) -> Dict:
        """Get current audio processing metrics"""
        try:
            # Get latest metrics from audio processor
            metrics = {
                "noise_reduction_enabled": self.voice_assistant.audio_processor.config.noise_reduction,
                "vad_enabled": self.voice_assistant.audio_processor.config.vad_enabled,
                "vad_threshold": self.voice_assistant.audio_processor.config.vad_threshold,
                "sample_rate": self.voice_config.sample_rate,
                "is_active": self.voice_assistant.is_active,
            }

            # Add calibration status
            if self.voice_assistant.audio_processor.noise_profile:
                metrics["noise_calibrated"] = True
                metrics["noise_profile_rms"] = (
                    self.voice_assistant.audio_processor.noise_profile.get("rms", 0)
                )
            else:
                metrics["noise_calibrated"] = False

            return metrics

        except Exception as e:
            raise  # Graceful handler will catch this

    @graceful_endpoint
    async def test_feedback(self) -> Dict:
        """Test audio feedback sounds"""
        try:
            # Play all feedback sounds
            feedback_types = [
                "beep",
                "double_beep",
                "success",
                "error",
                "listening",
                "processing",
            ]

            for feedback_type in feedback_types:
                self.voice_assistant._play_feedback(feedback_type)
                await asyncio.sleep(0.5)

            return {
                "status": "success",
                "message": "Played all feedback sounds",
                "feedback_types": feedback_types,
            }

        except Exception as e:
            raise  # Graceful handler will catch this
