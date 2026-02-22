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
import inspect
import io
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

# Configuration
_MAX_CONTEXT_TURNS = int(os.getenv("JARVIS_CONV_MAX_TURNS", "20"))
_SESSION_TIMEOUT_S = float(os.getenv("JARVIS_CONV_SESSION_TIMEOUT", "300"))
_SENTENCE_DELIMITERS = re.compile(r'(?<=[.!?])\s+')
_EXECUTE_INTENT_CONFIDENCE = float(
    os.getenv("JARVIS_CONV_EXECUTE_INTENT_CONFIDENCE", "0.62")
)
_AUTHENTICATE_PATTERN = re.compile(
    r"\b(?:authenticate|biometric|voice\s+unlock|unlock\s+(?:my\s+)?screen)\b",
    re.IGNORECASE,
)
_EXECUTE_IMPERATIVE_PATTERN = re.compile(
    r"^\s*(?:please\s+)?(?:"
    r"open|close|launch|run|execute|send|set|switch|turn|enable|disable|"
    r"connect|disconnect|start|stop|restart|shutdown|lock|unlock|schedule|"
    r"create|delete|kill"
    r")\b",
    re.IGNORECASE,
)
_COMPLEXITY_HINT_PATTERN = re.compile(
    r"\b(?:analyze|compare|architecture|strategy|multi[-\s]?step|derive|proof)\b",
    re.IGNORECASE,
)
_MATH_HINT_PATTERN = re.compile(
    r"\b(?:solve|equation|integral|derivative|calculus|algebra|matrix|theorem)\b",
    re.IGNORECASE,
)
_CODE_HINT_PATTERN = re.compile(
    r"\b(?:python|javascript|typescript|java|c\+\+|rust|function|class|debug|bug|stacktrace)\b",
    re.IGNORECASE,
)
# v258.3: Anchored exit pattern — requires the exit phrase to be the
# ENTIRE utterance (with optional "jarvis" prefix and punctuation).
# "stop" exits, but "stop the server" does NOT.
_EXIT_PATTERN = re.compile(
    r"^(?:(?:hey\s+)?jarvis[,\s]*)?(?:"
    r"goodbye|good\s*bye|bye(?:\s+bye)?|stop|exit|quit|"
    r"end\s+(?:the\s+)?conversation|that'?s\s+all|i'?m\s+done|"
    r"stop\s+(?:talking|listening|the\s+conversation)|"
    r"jarvis\s+(?:stop|quit)"
    r")[\s.!?]*$",
    re.IGNORECASE,
)
_LOCK_SCREEN_PATTERN = re.compile(
    r"\block\s+(?:my\s+)?(?:screen|computer|mac)\b",
    re.IGNORECASE,
)
_UNLOCK_SCREEN_PATTERN = re.compile(
    r"\bunlock\s+(?:my\s+)?(?:screen|computer|mac)\b",
    re.IGNORECASE,
)


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
        intent_classifier=None,
        command_processor=None,
        query_complexity_manager=None,
        mode_dispatcher=None,
    ):
        self._audio_bus = audio_bus
        self._streaming_stt = streaming_stt
        self._turn_detector = turn_detector
        self._barge_in = barge_in
        self._tts_engine = tts_engine
        self._llm_client = llm_client
        self._intent_classifier = intent_classifier
        self._command_processor = command_processor
        self._query_complexity_manager = query_complexity_manager
        self._mode_dispatcher = mode_dispatcher
        self._intent_classifier_init_failed = False
        self._command_processor_init_failed = False
        self._query_complexity_lookup_attempted = False

        self._session: Optional[ConversationSession] = None
        self._sentence_splitter = SentenceSplitter()
        self._running = False
        self._resume_event = asyncio.Event()
        self._resume_event.set()  # Start unpaused
        self._run_task: Optional[asyncio.Task] = None

        # System prompt for conversation mode
        self._system_prompt = os.getenv(
            "JARVIS_CONV_SYSTEM_PROMPT",
            "You are JARVIS, a helpful AI assistant engaged in a voice "
            "conversation. Keep responses concise and natural for speech. "
            "Use short sentences. Avoid markdown, code blocks, or lists "
            "unless specifically asked."
        )

    def set_mode_dispatcher(self, dispatcher) -> None:
        """Set the ModeDispatcher reference (for biometric auth delegation)."""
        self._mode_dispatcher = dispatcher

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
        self._resume_event.set()  # Unblock if paused, loop will see _running=False
        if self._run_task is not None:
            self._run_task.cancel()
            try:
                await self._run_task
            except asyncio.CancelledError:
                pass
            self._run_task = None

    async def pause(self) -> None:
        """
        Pause the conversation loop without ending the session.

        The loop stops listening/responding but the session transcript
        is preserved. Call resume() to continue.
        """
        if not self._running:
            return
        self._resume_event.clear()
        logger.info("[ConvPipeline] Paused")

    async def resume(self) -> None:
        """
        Resume a paused conversation loop.

        The loop continues from where it left off with the full
        session transcript intact.
        """
        if not self.is_paused:
            return
        self._resume_event.set()
        logger.info("[ConvPipeline] Resumed")

    @property
    def is_paused(self) -> bool:
        """Whether the conversation loop is currently paused."""
        return self._running and not self._resume_event.is_set()

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
            # v258.3: WARNING not debug — speech state inconsistency can
            # cause post-speech cooldown to stay active during conversation,
            # rejecting legitimate user speech.
            logger.warning("[ConvPipeline] Speech state mode toggle failed: %s", e)

    async def _conversation_loop(self) -> None:
        """Core conversation loop: listen → understand → respond → repeat."""
        while self._running and self._session is not None:
            # Wait if paused (biometric auth in progress)
            if not self._resume_event.is_set():
                logger.debug("[ConvPipeline] Waiting for resume...")
                await self._resume_event.wait()
                if not self._running:
                    break

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

                # 4. Classify turn intent and route deterministically
                intent_decision = await self._classify_turn_intent(user_text)
                route = intent_decision.get("route", "discuss")
                logger.info(
                    "[ConvPipeline] Intent route=%s intent=%s confidence=%.2f",
                    route,
                    intent_decision.get("intent", "unknown"),
                    float(intent_decision.get("confidence", 0.0) or 0.0),
                )

                # 5. Check for exit commands — AFTER intent classification.
                # v258.3: Only exit when the route is "discuss". This prevents
                # "stop the server" from killing the conversation when the
                # intent router correctly classified it as a command.
                if route == "discuss" and self._is_exit_command(user_text):
                    logger.info("[ConvPipeline] Exit command detected")
                    break

                if route == "authenticate":
                    # v258.3: Biometric auth has a dedicated flow that
                    # pauses conversation, delegates to ModeDispatcher VBIA,
                    # and resumes after authentication completes.
                    handled = await self._handle_authenticate_turn(user_text)
                    if handled:
                        continue
                    logger.info(
                        "[ConvPipeline] Auth route unavailable, falling back to discuss"
                    )
                elif route == "execute":
                    handled = await self._execute_command_turn(
                        user_text=user_text,
                        intent_decision=intent_decision,
                    )
                    if handled:
                        continue
                    logger.warning(
                        "[ConvPipeline] Command route unavailable, falling back to discuss"
                    )

                # 6. Discuss route → generate and speak LLM response
                await self._generate_and_speak_response(
                    user_text=user_text,
                    intent_decision=intent_decision,
                )

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

    async def _generate_and_speak_response(
        self,
        user_text: Optional[str] = None,
        intent_decision: Optional[Dict[str, Any]] = None,
    ) -> None:
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
                token_stream = self._get_llm_stream(
                    messages,
                    user_text=user_text,
                    intent_decision=intent_decision,
                )

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
        self,
        messages: List[Dict[str, str]],
        user_text: Optional[str] = None,
        intent_decision: Optional[Dict[str, Any]] = None,
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
            from backend.intelligence.unified_model_serving import (
                ModelRequest,
                TaskType,
            )

            # Separate system prompt from conversation messages
            system_prompt = None
            conversation_messages = []
            for m in messages:
                if m["role"] == "system":
                    system_prompt = m["content"]
                else:
                    conversation_messages.append(m)

            task_type_hint, complexity_level = await self._infer_task_route(
                user_text or self._latest_user_turn_text()
            )
            request_task_type = self._map_task_type_hint_to_model_task(
                task_type_hint, TaskType
            )
            request_context: Dict[str, Any] = {
                "task_type": task_type_hint,
                "complexity_level": complexity_level,
                "conversation_mode": True,
            }
            if intent_decision:
                request_context["conversation_intent"] = intent_decision.get(
                    "intent"
                )
                request_context["intent_confidence"] = float(
                    intent_decision.get("confidence", 0.0) or 0.0
                )

            request = ModelRequest(
                messages=conversation_messages,
                system_prompt=system_prompt,
                task_type=request_task_type,
                stream=True,
                temperature=float(os.getenv("JARVIS_CONV_TEMPERATURE", "0.7")),
                max_tokens=int(os.getenv("JARVIS_CONV_MAX_TOKENS", "512")),
                context=request_context,
            )

            async for chunk in self._llm_client.generate_stream(request):
                yield chunk

        except Exception as e:
            logger.error(f"[ConvPipeline] LLM stream error: {e}")
            yield "I'm sorry, I had trouble generating a response."

    def _latest_user_turn_text(self) -> str:
        """Return latest user utterance from session context, if any."""
        if self._session is None:
            return ""
        for turn in reversed(self._session.turns):
            if turn.role == "user":
                return turn.text
        return ""

    async def _classify_turn_intent(self, user_text: str) -> Dict[str, Any]:
        """Classify intent using lightweight heuristics. No ML classifier.

        v242.1: Removed CAI dependency. J-Prime handles deep classification
        via UCP for the 'execute' route. ConversationPipeline only needs to
        distinguish commands from conversation.

        Routes:
            - discuss: normal conversational LLM response
            - execute: route through UnifiedCommandProcessor
            - authenticate: security-sensitive execution flow
        """
        lowered = (user_text or "").strip().lower()

        # Protected local op: voice biometric authentication
        if _AUTHENTICATE_PATTERN.search(lowered):
            return {
                "route": "authenticate",
                "intent": "authenticate",
                "confidence": 1.0,
                "source": "heuristic",
            }

        # Imperative verb heuristic — fast, no ML overhead
        if _EXECUTE_IMPERATIVE_PATTERN.search(lowered):
            return {
                "route": "execute",
                "intent": "system_command",
                "confidence": 0.8,
                "source": "heuristic",
            }

        # v242.1 CAI rollback gate: if explicitly enabled, use ML classification
        if os.environ.get("JARVIS_CONV_PIPELINE_USE_CAI", "").lower() in (
            "true",
            "1",
            "yes",
        ):
            prediction = await self._predict_intent(user_text)
            intent = str(prediction.get("intent", "conversation")).lower()
            confidence = float(prediction.get("confidence", 0.0) or 0.0)
            execute_intents = {
                "display_control",
                "system_command",
                "automation_request",
                "preference_setting",
            }
            if intent in execute_intents and confidence >= _EXECUTE_INTENT_CONFIDENCE:
                return {
                    "route": "execute",
                    "intent": intent,
                    "confidence": confidence,
                    "source": prediction.get("source", "cai"),
                }
            return {
                "route": "discuss",
                "intent": intent,
                "confidence": confidence,
                "source": prediction.get("source", "fallback"),
            }

        # Default: conversational (stay in discuss path for fast local LLM)
        return {
            "route": "discuss",
            "intent": "conversation",
            "confidence": 0.8,
            "source": "heuristic",
        }

    async def _predict_intent(self, user_text: str) -> Dict[str, Any]:
        """Predict intent using CAI when available; fall back safely when unavailable.

        v242.1: Gated behind JARVIS_CONV_PIPELINE_USE_CAI=true (default: false).
        CAI classification is redundant with J-Prime's Phi classifier.
        """
        if not os.environ.get("JARVIS_CONV_PIPELINE_USE_CAI", "").lower() in (
            "true",
            "1",
            "yes",
        ):
            return {"intent": "conversation", "confidence": 0.0, "source": "cai_disabled"}

        if self._intent_classifier is None and not self._intent_classifier_init_failed:
            try:
                from backend.intelligence.context_awareness_intelligence import (
                    ContextAwarenessIntelligence,
                )

                self._intent_classifier = ContextAwarenessIntelligence()
            except Exception as e:
                self._intent_classifier_init_failed = True
                logger.debug("[ConvPipeline] CAI unavailable: %s", e)

        if self._intent_classifier is None:
            return {"intent": "conversation", "confidence": 0.0, "source": "fallback"}

        try:
            prediction = await asyncio.to_thread(
                self._intent_classifier.predict_intent, user_text
            )
            if isinstance(prediction, dict):
                prediction.setdefault("source", "cai")
                return prediction
        except Exception as e:
            # v258.3: WARNING not debug — if CAI is consistently crashing,
            # all conversation turns fall back to discuss-only (no command
            # execution from conversation mode). Needs production visibility.
            logger.warning("[ConvPipeline] CAI predict_intent failed: %s", e)

        return {"intent": "conversation", "confidence": 0.0, "source": "fallback"}

    async def _get_command_processor(self):
        """Lazily initialize unified command processor for execute/auth routes."""
        if self._command_processor is not None:
            return self._command_processor
        if self._command_processor_init_failed:
            return None

        try:
            from backend.api.unified_command_processor import get_unified_processor

            self._command_processor = get_unified_processor()
        except Exception as e:
            self._command_processor_init_failed = True
            logger.warning("[ConvPipeline] Command processor unavailable: %s", e)
            return None

        return self._command_processor

    async def _execute_command_turn(
        self,
        user_text: str,
        intent_decision: Optional[Dict[str, Any]] = None,
        *,
        allow_auth_redirect: bool = True,
    ) -> bool:
        """Execute a user turn as an actionable command and speak structured outcome."""
        if self._session is None:
            return False

        lowered = (user_text or "").strip().lower()
        if allow_auth_redirect and _UNLOCK_SCREEN_PATTERN.search(lowered):
            logger.info(
                "[ConvPipeline] Routing unlock command through biometric flow"
            )
            return await self._handle_authenticate_turn(user_text)

        await self._interrupt_output_for_user_command()

        command_processor = await self._get_command_processor()
        if command_processor is None:
            return False

        pre_ack_text: Optional[str] = None
        if _LOCK_SCREEN_PATTERN.search(lowered):
            # Locking can silence output immediately; acknowledge before executing.
            pre_ack_text = "Locking your screen now."
            await self._speak_sentence(pre_ack_text, asyncio.Event())

        result: Any = None
        try:
            process_kwargs: Dict[str, Any] = {}
            try:
                params = inspect.signature(command_processor.process_command).parameters
                accepts_var_kwargs = any(
                    p.kind == inspect.Parameter.VAR_KEYWORD
                    for p in params.values()
                )
                if "audio_data" in params:
                    process_kwargs["audio_data"] = None
                if "speaker_name" in params:
                    process_kwargs["speaker_name"] = None
                if "source_context" in params or accepts_var_kwargs:
                    process_kwargs["source_context"] = {
                        "source": "conversation_pipeline",
                        "allow_during_tts_interrupt": True,
                        "intent_route": (
                            intent_decision.get("route")
                            if isinstance(intent_decision, dict)
                            else "execute"
                        ),
                    }
            except (TypeError, ValueError):
                pass

            result = await command_processor.process_command(user_text, **process_kwargs)
        except Exception as e:
            logger.error("[ConvPipeline] Command execution error: %s", e)
            response_text = "I could not execute that command right now."
        else:
            response_text = self._extract_command_response(result)
            success = True
            if isinstance(result, dict):
                success = bool(result.get("success", True))
            if not response_text:
                response_text = (
                    "I could not complete that command."
                    if not success
                    else "I ran that command."
                )
            if pre_ack_text and success:
                # Already acknowledged before lock execution; don't wait on post-lock TTS.
                response_text = pre_ack_text

        if self._barge_in is not None:
            self._barge_in.reset()
        cancel_event = (
            self._barge_in.get_cancel_event()
            if self._barge_in is not None
            else asyncio.Event()
        )

        should_speak_response = not (pre_ack_text and response_text == pre_ack_text)
        if should_speak_response and response_text:
            await self._speak_sentence(response_text, cancel_event)
        self._session.add_turn("assistant", response_text)
        logger.info(
            "[ConvPipeline] Command response (%s): %s",
            intent_decision.get("intent", "unknown") if intent_decision else "unknown",
            response_text[:120],
        )
        return True

    async def _handle_authenticate_turn(self, user_text: str) -> bool:
        """
        Handle biometric authentication requests from within conversation.

        v258.3: Delegates to ModeDispatcher's BIOMETRIC flow which:
        1. Pauses the conversation pipeline
        2. Captures fresh mic audio for speaker verification
        3. Runs VBIA authentication (challenge/response, voiceprint match)
        4. Resumes conversation after auth completes

        Falls back to command processor if ModeDispatcher is unavailable
        (but without audio_data, voice biometric handlers will be limited).

        Returns True if the auth attempt was handled, False to fall through.
        """
        if self._session is None:
            return False

        await self._interrupt_output_for_user_command()

        # --- Preferred path: ModeDispatcher biometric flow ---
        dispatcher = self._mode_dispatcher
        if dispatcher is None:
            # Lazy-load from supervisor if not injected at construction
            try:
                from backend.audio.mode_dispatcher import (
                    get_mode_dispatcher,  # type: ignore[attr-defined]
                )
                dispatcher = get_mode_dispatcher()
            except Exception:
                pass

        if dispatcher is not None:
            try:
                from backend.audio.mode_dispatcher import VoiceMode

                # Speak acknowledgment before entering biometric mode
                cancel = asyncio.Event()
                await self._speak_sentence(
                    "Starting voice authentication. Please speak clearly.",
                    cancel,
                )

                # Delegate to ModeDispatcher — this pauses conversation,
                # runs biometric auth, and resumes when done.
                await dispatcher.switch_mode(VoiceMode.BIOMETRIC)

                # Wait for the biometric task to finish (it runs as a
                # background task in the dispatcher). Poll with timeout.
                auth_timeout = float(
                    os.getenv("JARVIS_BIOMETRIC_AUTH_TIMEOUT", "25")
                )
                biometric_task = getattr(dispatcher, "_biometric_task", None)
                if biometric_task is not None:
                    try:
                        await asyncio.wait_for(
                            asyncio.shield(biometric_task),
                            timeout=auth_timeout,
                        )
                    except asyncio.TimeoutError:
                        logger.warning(
                            "[ConvPipeline] Biometric auth timed out after %.0fs",
                            auth_timeout,
                        )
                        try:
                            if not biometric_task.done():
                                biometric_task.cancel()
                                try:
                                    await biometric_task
                                except asyncio.CancelledError:
                                    pass
                            if getattr(dispatcher, "current_mode", None) == VoiceMode.BIOMETRIC:
                                await dispatcher.return_from_biometric()
                            dispatcher._last_biometric_result = {
                                "success": False,
                                "verified": False,
                                "unlocked": False,
                                "reason": "authentication_timed_out",
                                "message": "Voice authentication timed out. Please try again.",
                            }
                        except Exception as timeout_recovery_error:
                            logger.warning(
                                "[ConvPipeline] Timeout recovery failed: %s",
                                timeout_recovery_error,
                            )
                    except asyncio.CancelledError:
                        pass
                    except Exception as e:
                        logger.warning(
                            "[ConvPipeline] Biometric auth error: %s", e
                        )

                # ModeDispatcher's _on_biometric_done callback will
                # switch back to previous mode and resume conversation.
                # Record the authentication attempt in session transcript.
                biometric_result = getattr(dispatcher, "_last_biometric_result", None)
                status_text = (
                    biometric_result.get("message")
                    if isinstance(biometric_result, dict)
                    and isinstance(biometric_result.get("message"), str)
                    and biometric_result.get("message")
                    else "Voice authentication completed."
                )
                self._session.add_turn(
                    "assistant",
                    status_text,
                )
                logger.info("[ConvPipeline] Biometric auth flow completed")
                return True

            except Exception as e:
                logger.warning(
                    "[ConvPipeline] ModeDispatcher biometric delegation failed: %s", e
                )

        # --- Fallback: route through command processor (no audio_data) ---
        return await self._execute_command_turn(
            user_text=user_text,
            intent_decision={"intent": "authenticate", "route": "authenticate"},
            allow_auth_redirect=False,
        )

    async def _interrupt_output_for_user_command(self) -> None:
        """Force-stop current assistant output before executing user command/auth."""
        if self._barge_in is not None:
            try:
                self._barge_in.get_cancel_event().set()
            except Exception:
                pass

        if self._audio_bus is not None:
            try:
                self._audio_bus.flush_playback()
            except Exception:
                pass

        try:
            from backend.core.unified_speech_state import get_speech_state_manager

            manager = await get_speech_state_manager()
            await manager.stop_speaking()
        except Exception:
            pass

    def _extract_command_response(self, result: Any) -> str:
        """Normalize UnifiedCommandProcessor output into a speakable sentence."""
        if result is None:
            return ""
        if isinstance(result, str):
            return result.strip()
        if not isinstance(result, dict):
            return str(result).strip()

        for key in ("response", "formatted_response", "message", "error"):
            value = result.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    async def _infer_task_route(self, user_text: str) -> Tuple[str, str]:
        """
        Infer fine-grained task routing metadata for J-Prime model specialization.

        Returns:
            (task_type, complexity_level)
        """
        text = (user_text or "").strip()
        if not text:
            return "general_chat", "MODERATE"

        complexity_level = await self._infer_complexity_level(text)

        try:
            from backend.api.query_handler import _infer_task_type

            task_type = await asyncio.to_thread(
                _infer_task_type, text, complexity_level
            )
            if isinstance(task_type, str) and task_type:
                return task_type, complexity_level
        except Exception as e:
            logger.debug("[ConvPipeline] Task type inference fallback: %s", e)

        return self._heuristic_task_type(text, complexity_level), complexity_level

    async def _infer_complexity_level(self, text: str) -> str:
        """Infer complexity level using QueryComplexityManager when available."""
        manager = await self._get_query_complexity_manager()
        if manager is not None and hasattr(manager, "process_query"):
            try:
                classified = await manager.process_query(text)
                level = getattr(getattr(classified, "complexity", None), "level", None)
                level_name = getattr(level, "name", None)
                if isinstance(level_name, str) and level_name:
                    return level_name.upper()
            except Exception as e:
                logger.debug("[ConvPipeline] Complexity manager inference failed: %s", e)

        # Deterministic fallback when complexity manager is absent/unavailable.
        word_count = len(text.split())
        if word_count <= 6:
            return "SIMPLE"
        if word_count >= 20 or _COMPLEXITY_HINT_PATTERN.search(text):
            return "COMPLEX"
        return "MODERATE"

    async def _get_query_complexity_manager(self):
        """Return global QueryComplexityManager if initialized."""
        if self._query_complexity_manager is not None:
            return self._query_complexity_manager
        if self._query_complexity_lookup_attempted:
            return None

        self._query_complexity_lookup_attempted = True
        try:
            from backend.context_intelligence.handlers.query_complexity_manager import (
                get_query_complexity_manager,
            )

            self._query_complexity_manager = get_query_complexity_manager()
        except Exception as e:
            logger.debug("[ConvPipeline] QueryComplexityManager unavailable: %s", e)
            self._query_complexity_manager = None

        return self._query_complexity_manager

    def _heuristic_task_type(self, text: str, complexity_level: str) -> str:
        """Fallback task type inference when query_handler import is unavailable."""
        lowered = text.lower()
        if _MATH_HINT_PATTERN.search(lowered):
            return (
                "math_complex"
                if complexity_level in {"COMPLEX", "ADVANCED", "EXPERT"}
                else "math_simple"
            )
        if _CODE_HINT_PATTERN.search(lowered):
            return (
                "code_complex"
                if complexity_level in {"COMPLEX", "ADVANCED", "EXPERT"}
                else "code_simple"
            )
        if complexity_level in {"ADVANCED", "EXPERT"}:
            return "reason_complex"
        if complexity_level == "SIMPLE":
            return "simple_chat"
        return "general_chat"

    def _map_task_type_hint_to_model_task(self, task_hint: str, task_type_enum):
        """Map fine-grained task hints to UnifiedModelServing TaskType enum."""
        hint = (task_hint or "").lower()
        if hint.startswith("code_"):
            return task_type_enum.CODE
        if hint.startswith("math_") or hint.startswith("reason_"):
            return task_type_enum.REASONING
        return task_type_enum.CHAT

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
        """
        Check if the user wants to exit conversation mode.

        v258.3: Uses anchored regex (_EXIT_PATTERN) so the exit phrase
        must be the ENTIRE utterance. "stop" exits, but "stop the server"
        does NOT — it will be routed through the command processor instead.
        """
        return bool(_EXIT_PATTERN.match((text or "").strip()))

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
            "paused": self.is_paused,
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
