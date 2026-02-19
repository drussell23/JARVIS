"""
Conversation Pipeline Orchestrator (Layer 5)
=============================================

Wires all audio layers into a complete voice conversation loop:

    Mic → AEC → VAD → TurnDetector → StreamingSTT → LLM → SentenceSplitter
                                                           → StreamingTTS → AudioBus

Supports barge-in (user interrupts JARVIS) and maintains a sliding-window
conversation transcript for LLM context.

Architecture:
    ┌───────────────────────────────────────────────────────────────────┐
    │                     ConversationPipeline                         │
    │                                                                  │
    │  AudioBus ──▶ StreamingSTT ──▶ TurnDetector ──▶ [user text]     │
    │      ▲                                              │            │
    │      │                                              ▼            │
    │  TTS stream ◀── SentenceSplitter ◀── LLM stream ◀── context    │
    │      │                                                           │
    │  BargeInController (cancels TTS on user speech)                  │
    └───────────────────────────────────────────────────────────────────┘
"""

import asyncio
import io
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import AsyncIterator, Dict, List, Optional

import numpy as np

logger = logging.getLogger(__name__)

# Configuration
_MAX_CONTEXT_TURNS = int(os.getenv("JARVIS_CONV_MAX_TURNS", "20"))
_SESSION_TIMEOUT_S = float(os.getenv("JARVIS_CONV_SESSION_TIMEOUT", "300"))
_SENTENCE_DELIMITERS = re.compile(r'(?<=[.!?])\s+')


# ============================================================================
# Conversation Data Model
# ============================================================================

@dataclass
class ConversationTurn:
    """A single turn in the conversation."""
    role: str          # "user" or "assistant"
    text: str
    timestamp: float
    audio_duration_ms: Optional[float] = None


@dataclass
class ConversationSession:
    """Ordered turn transcript with sliding window for LLM context."""
    session_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    turns: List[ConversationTurn] = field(default_factory=list)
    max_context_turns: int = _MAX_CONTEXT_TURNS
    created_at: float = field(default_factory=time.time)
    last_activity: float = field(default_factory=time.time)

    def add_turn(
        self,
        role: str,
        text: str,
        audio_duration_ms: Optional[float] = None
    ) -> None:
        """Add a turn and maintain sliding window."""
        turn = ConversationTurn(
            role=role,
            text=text,
            timestamp=time.time(),
            audio_duration_ms=audio_duration_ms,
        )
        self.turns.append(turn)
        self.last_activity = time.time()

        # Trim to max context
        if len(self.turns) > self.max_context_turns:
            self.turns = self.turns[-self.max_context_turns:]

    def get_context_for_llm(self) -> List[Dict[str, str]]:
        """Get conversation history as messages array for LLM."""
        messages = []
        for turn in self.turns:
            messages.append({
                "role": turn.role,
                "content": turn.text,
            })
        return messages

    @property
    def is_expired(self) -> bool:
        """Check if session has timed out."""
        return (time.time() - self.last_activity) > _SESSION_TIMEOUT_S

    @property
    def turn_count(self) -> int:
        return len(self.turns)

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "turn_count": self.turn_count,
            "created_at": self.created_at,
            "last_activity": self.last_activity,
            "is_expired": self.is_expired,
        }


# ============================================================================
# Sentence Splitter
# ============================================================================

class SentenceSplitter:
    """
    Accumulates LLM tokens until a sentence boundary, then yields complete
    sentences for TTS. This lets the user hear the first word at ~300-500ms
    instead of waiting for the full LLM response.
    """

    def __init__(self, min_sentence_len: int = 10):
        self._min_len = min_sentence_len

    async def split(
        self, token_stream: AsyncIterator[str]
    ) -> AsyncIterator[str]:
        """
        Consume an async stream of tokens and yield complete sentences.
        Handles cases where sentence-ending punctuation arrives without
        trailing whitespace (common with token-by-token LLM streaming).
        """
        buffer = ""

        async for token in token_stream:
            buffer += token

            # Check for sentence boundaries
            while True:
                match = _SENTENCE_DELIMITERS.search(buffer)
                if match and match.start() >= self._min_len:
                    sentence = buffer[:match.end()].strip()
                    buffer = buffer[match.end():]
                    if sentence:
                        yield sentence
                else:
                    # Check for punctuation at end of buffer followed by
                    # trailing whitespace (the regex doesn't match because
                    # there's no subsequent text yet).
                    stripped = buffer.rstrip()
                    if (
                        len(stripped) >= self._min_len
                        and stripped
                        and stripped[-1] in '.!?'
                        and len(buffer) != len(stripped)
                    ):
                        buffer = ""
                        yield stripped
                    break

        # Flush remaining buffer
        buffer = buffer.strip()
        if buffer:
            yield buffer


# ============================================================================
# Conversation Pipeline
# ============================================================================

class ConversationPipeline:
    """
    Full conversation orchestrator.

    Coordinates:
        - StreamingSTT (mic → text)
        - TurnDetector (detect end of user speech)
        - BargeInController (cancel TTS on interrupt)
        - LLM (generate response via UnifiedModelServing)
        - SentenceSplitter (stream sentences to TTS)
        - UnifiedTTSEngine → AudioBus (speak response)
    """

    def __init__(
        self,
        audio_bus=None,
        streaming_stt=None,
        turn_detector=None,
        barge_in=None,
        tts_engine=None,
        llm_client=None,
    ):
        self._audio_bus = audio_bus
        self._streaming_stt = streaming_stt
        self._turn_detector = turn_detector
        self._barge_in = barge_in
        self._tts_engine = tts_engine
        self._llm_client = llm_client

        self._session: Optional[ConversationSession] = None
        self._sentence_splitter = SentenceSplitter()
        self._running = False
        self._run_task: Optional[asyncio.Task] = None

        # System prompt for conversation mode
        self._system_prompt = os.getenv(
            "JARVIS_CONV_SYSTEM_PROMPT",
            "You are JARVIS, a helpful AI assistant engaged in a voice "
            "conversation. Keep responses concise and natural for speech. "
            "Use short sentences. Avoid markdown, code blocks, or lists "
            "unless specifically asked."
        )

    async def start_session(self) -> str:
        """Start a new conversation session. Returns session_id."""
        self._session = ConversationSession()
        logger.info(
            f"[ConvPipeline] Started session {self._session.session_id}"
        )
        return self._session.session_id

    async def end_session(self) -> None:
        """End the current conversation session."""
        if self._session is not None:
            logger.info(
                f"[ConvPipeline] Ended session {self._session.session_id} "
                f"({self._session.turn_count} turns)"
            )
            self._session = None

        self._running = False
        if self._run_task is not None:
            self._run_task.cancel()
            try:
                await self._run_task
            except asyncio.CancelledError:
                pass
            self._run_task = None

    async def run(self) -> None:
        """
        Main conversation loop.

        Runs until end_session() is called or session expires.
        Enables conversation mode on the speech state manager (disables
        post-speech cooldown when AEC handles echo suppression).
        """
        if self._session is None:
            await self.start_session()

        self._running = True

        # Enable conversation mode — AEC handles echo suppression, so
        # the speech state manager can skip its post-speech cooldown.
        await self._set_conversation_mode(True)

        try:
            await self._conversation_loop()
        except asyncio.CancelledError:
            logger.info("[ConvPipeline] Conversation loop cancelled")
        except Exception as e:
            logger.error(f"[ConvPipeline] Error in conversation loop: {e}")
        finally:
            self._running = False
            await self._set_conversation_mode(False)

    async def _set_conversation_mode(self, enabled: bool) -> None:
        """Toggle conversation mode on the speech state manager."""
        try:
            from backend.core.unified_speech_state import get_speech_state_manager
            manager = await get_speech_state_manager()
            manager.set_conversation_mode(enabled)
            logger.info(
                f"[ConvPipeline] Conversation mode {'enabled' if enabled else 'disabled'}"
            )
        except Exception as e:
            logger.debug(f"[ConvPipeline] Speech state mode toggle failed: {e}")

    async def _conversation_loop(self) -> None:
        """Core conversation loop: listen → understand → respond → repeat."""
        while self._running and self._session is not None:
            if self._session.is_expired:
                logger.info("[ConvPipeline] Session expired")
                break

            try:
                # 1. Wait for user to finish speaking (get transcript)
                user_text = await self._listen_for_turn()
                if user_text is None:
                    continue

                if not user_text.strip():
                    continue

                # 2. Self-voice echo filter — last line of defense.
                # If AEC didn't fully cancel JARVIS's voice, Whisper may
                # transcribe fragments of what JARVIS just said. Check
                # against recent speech via the unified speech state manager.
                if await self._is_self_voice_echo(user_text):
                    logger.info(
                        f"[ConvPipeline] Rejected self-voice echo: "
                        f"{user_text[:60]!r}"
                    )
                    continue

                # 3. Add user turn to session
                self._session.add_turn("user", user_text)
                logger.info(
                    f"[ConvPipeline] User: {user_text[:80]}"
                    f"{'...' if len(user_text) > 80 else ''}"
                )

                # 4. Check for exit commands
                if self._is_exit_command(user_text):
                    logger.info("[ConvPipeline] Exit command detected")
                    break

                # 5. Generate and speak response
                await self._generate_and_speak_response()

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"[ConvPipeline] Turn error: {e}")
                await asyncio.sleep(0.5)

    async def _listen_for_turn(self) -> Optional[str]:
        """
        Listen for a complete user turn using StreamingSTT + TurnDetector.

        The TurnDetector uses adaptive silence thresholds based on question
        context (300ms for yes/no, 600ms default, 900ms for open-ended).
        Multiple final transcripts within one turn are accumulated — the user
        may speak in short bursts that the STT segments individually but which
        form a single conversational turn.

        Returns the accumulated transcript text, or None on timeout/silence.
        """
        if self._streaming_stt is None:
            # Fallback: wait for input (testing mode)
            await asyncio.sleep(1.0)
            return None

        # Reset barge-in controller
        if self._barge_in is not None:
            self._barge_in.reset()

        # Reset turn detector for the new listening phase
        if self._turn_detector is not None:
            self._turn_detector.reset()

        accumulated_text_parts: list = []
        turn_ended = False

        try:
            async for event in self._streaming_stt.get_transcripts():
                if not self._running:
                    break

                if event.is_partial:
                    # Feed VAD-derived speech signal to TurnDetector.
                    # Partial events mean speech is active.
                    if self._turn_detector is not None:
                        self._turn_detector.on_vad_result(
                            is_speech=True, timestamp_ms=event.timestamp_ms
                        )
                else:
                    # Final transcript — STT's VAD detected end of utterance.
                    if event.text.strip():
                        accumulated_text_parts.append(event.text.strip())

                    # Feed silence signal to TurnDetector (STT emits final
                    # when it detects sustained silence).
                    if self._turn_detector is not None:
                        result = self._turn_detector.on_vad_result(
                            is_speech=False, timestamp_ms=event.timestamp_ms
                        )
                        if result == "turn_end":
                            turn_ended = True
                            break
                    else:
                        # No TurnDetector — fall back to single-final behavior
                        turn_ended = True
                        break

        except asyncio.TimeoutError:
            pass

        if not accumulated_text_parts:
            return None

        # Join all accumulated segments into one turn
        return " ".join(accumulated_text_parts)

    async def _generate_and_speak_response(self) -> None:
        """Generate LLM response and stream it through TTS."""
        if self._session is None:
            return

        # Build messages for LLM
        messages = [
            {"role": "system", "content": self._system_prompt},
            *self._session.get_context_for_llm(),
        ]

        # Reset barge-in for the response
        if self._barge_in is not None:
            self._barge_in.reset()
        cancel_event = (
            self._barge_in.get_cancel_event()
            if self._barge_in is not None
            else asyncio.Event()
        )

        full_response = ""

        try:
            if self._llm_client is not None:
                # Use UnifiedModelServing for streaming response
                token_stream = self._get_llm_stream(messages)

                async for sentence in self._sentence_splitter.split(token_stream):
                    if cancel_event.is_set():
                        logger.info("[ConvPipeline] Barge-in — stopping response")
                        break

                    full_response += sentence + " "

                    # Speak sentence through TTS
                    await self._speak_sentence(sentence, cancel_event)

            else:
                # No LLM client — echo mode for testing
                full_response = f"I heard you say: {self._session.turns[-1].text}"
                await self._speak_sentence(full_response, cancel_event)

        except Exception as e:
            logger.error(f"[ConvPipeline] Response generation error: {e}")
            full_response = full_response or "(response failed)"

        # Add assistant turn to session
        full_response = full_response.strip()
        if full_response:
            self._session.add_turn("assistant", full_response)
            logger.info(
                f"[ConvPipeline] JARVIS: {full_response[:80]}"
                f"{'...' if len(full_response) > 80 else ''}"
            )

    async def _get_llm_stream(
        self, messages: List[Dict[str, str]]
    ) -> AsyncIterator[str]:
        """
        Get a streaming token response from the LLM.

        Uses UnifiedModelServing's generate_stream() which supports
        the PRIME_API -> PRIME_LOCAL -> CLAUDE tier chain.

        generate_stream() expects a ModelRequest object with a messages
        list — NOT a flat prompt string.
        """
        if self._llm_client is None:
            return

        try:
            from backend.intelligence.unified_model_serving import ModelRequest

            # Separate system prompt from conversation messages
            system_prompt = None
            conversation_messages = []
            for m in messages:
                if m["role"] == "system":
                    system_prompt = m["content"]
                else:
                    conversation_messages.append(m)

            request = ModelRequest(
                messages=conversation_messages,
                system_prompt=system_prompt,
                stream=True,
                temperature=float(os.getenv("JARVIS_CONV_TEMPERATURE", "0.7")),
                max_tokens=int(os.getenv("JARVIS_CONV_MAX_TOKENS", "512")),
            )

            async for chunk in self._llm_client.generate_stream(request):
                yield chunk

        except Exception as e:
            logger.error(f"[ConvPipeline] LLM stream error: {e}")
            yield "I'm sorry, I had trouble generating a response."

    async def _speak_sentence(
        self, sentence: str, cancel_event: asyncio.Event
    ) -> None:
        """Speak a single sentence through TTS, routing through AudioBus for AEC."""
        if cancel_event.is_set():
            return

        if self._tts_engine is None:
            return

        try:
            # Preferred path: synthesize → decode WAV → stream through AudioBus
            # This gives AEC a reference signal and supports barge-in cancel.
            if (
                hasattr(self._tts_engine, 'synthesize')
                and self._audio_bus is not None
            ):
                tts_result = await self._tts_engine.synthesize(sentence)
                if tts_result is not None:
                    audio_bytes = getattr(tts_result, 'audio_data', None)
                    sample_rate = getattr(tts_result, 'sample_rate', 22050)

                    if audio_bytes is not None and not cancel_event.is_set():
                        # Decode WAV container to float32 PCM
                        try:
                            import soundfile as sf
                            audio_np, file_sr = sf.read(
                                io.BytesIO(audio_bytes), dtype='float32',
                            )
                            sample_rate = file_sr
                        except Exception:
                            # Raw int16 PCM fallback (no WAV header)
                            audio_np = np.frombuffer(
                                audio_bytes, dtype=np.int16,
                            ).astype(np.float32) / 32767.0

                        if not cancel_event.is_set():
                            # Stream through AudioBus with barge-in cancel
                            _chunk_size = int(sample_rate * 0.1)  # 100ms chunks

                            async def _audio_chunks():
                                for i in range(0, len(audio_np), _chunk_size):
                                    yield audio_np[i:i + _chunk_size]

                            await self._audio_bus.play_stream(
                                _audio_chunks(), sample_rate,
                                cancel=cancel_event,
                            )
                            return

            # Fallback: direct TTS playback (legacy path, no AEC reference)
            if hasattr(self._tts_engine, 'speak_stream'):
                await self._tts_engine.speak_stream(
                    sentence, play_audio=True,
                    cancel_event=cancel_event,
                    source="conversation_pipeline",
                )
            else:
                await self._tts_engine.speak(
                    sentence, play_audio=True,
                    source="conversation_pipeline",
                )
        except Exception as e:
            logger.debug(f"[ConvPipeline] TTS error: {e}")

    async def _is_self_voice_echo(self, text: str) -> bool:
        """
        Check if transcribed text is an echo of JARVIS's own recent speech.

        Uses the UnifiedSpeechStateManager's similarity check as a safety net
        against imperfect AEC. In conversation mode, cooldown is already
        disabled (AEC handles echo at the signal level), so only the
        semantic similarity check fires — detecting partial transcriptions
        of JARVIS's own words that leaked through AEC.

        Also checks against the most recent assistant turn in the session
        for a direct text match (catches cases where the speech state
        manager's 10-second window has expired but the echo is still
        from the immediately preceding response).
        """
        try:
            from backend.core.unified_speech_state import get_speech_state_manager
            manager = await get_speech_state_manager()
            result = manager.should_reject_audio(transcribed_text=text)
            if result.reject and result.reason == "echo_detected":
                return True
        except Exception as e:
            logger.debug(f"[ConvPipeline] Echo check failed: {e}")

        # Secondary check: compare against last assistant turn in session
        if self._session and self._session.turns:
            for turn in reversed(self._session.turns):
                if turn.role != "assistant":
                    break
                # If the user's text is a substring of what JARVIS just said
                # (or vice versa), it's likely a partial echo
                text_lower = text.lower().strip()
                turn_lower = turn.text.lower().strip()
                if len(text_lower) > 5:
                    if text_lower in turn_lower or turn_lower in text_lower:
                        return True
                    # Check word overlap ratio
                    user_words = set(text_lower.split())
                    jarvis_words = set(turn_lower.split())
                    if user_words and jarvis_words:
                        overlap = len(user_words & jarvis_words)
                        ratio = overlap / max(len(user_words), 1)
                        if ratio > float(os.getenv(
                            "JARVIS_ECHO_WORD_OVERLAP_THRESHOLD", "0.7"
                        )):
                            return True

        return False

    def _is_exit_command(self, text: str) -> bool:
        """Check if the user wants to exit conversation mode."""
        text_lower = text.lower().strip()
        exit_phrases = [
            "goodbye", "good bye", "bye", "stop", "exit",
            "end conversation", "that's all", "i'm done",
            "jarvis stop", "jarvis quit",
        ]
        return any(phrase in text_lower for phrase in exit_phrases)

    # ---- Properties ----

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def session(self) -> Optional[ConversationSession]:
        return self._session

    def get_status(self) -> dict:
        """Get pipeline status."""
        return {
            "running": self._running,
            "session": self._session.to_dict() if self._session else None,
            "stt_running": (
                self._streaming_stt.is_running
                if self._streaming_stt else False
            ),
            "barge_in": (
                self._barge_in.get_status()
                if self._barge_in else None
            ),
        }
