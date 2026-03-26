"""
ConversationManager — Voice-First Interactive JARVIS Experience.

"Just talk to me, sir."

The central coordinator for multi-turn voice conversation. JARVIS
listens, understands context, responds naturally, asks clarifying
questions, pushes back when something is unwise, and speaks first
when it has something important to say.

Components:
  1. ConversationManager — utterance classification + context + routing
  2. VoiceResponseGenerator — personality-aware response generation
  3. ProactiveSpeechEngine — JARVIS speaks first when triggered
  4. ConversationMemory — multi-turn context tracking

Boundary Principle:
  Deterministic: Utterance classification (keyword matching), context
  tracking (in-memory list), proactive triggers (threshold-based).
  Agentic: Response content for complex questions (via Claude/J-Prime),
  code task execution (via Ouroboros pipeline).
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Coroutine, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_ENABLED = os.environ.get(
    "JARVIS_VOICE_CONVERSATION_ENABLED", "true"
).lower() in ("true", "1", "yes")
_MAX_TURNS = int(os.environ.get("JARVIS_VOICE_MAX_CONTEXT_TURNS", "10"))
_PROACTIVE_DEBOUNCE_S = float(os.environ.get("JARVIS_VOICE_PROACTIVE_DEBOUNCE_S", "30"))
_TTS_VOICE = os.environ.get("JARVIS_VOICE_TTS_VOICE", "Daniel")
_TTS_RATE = int(os.environ.get("JARVIS_VOICE_TTS_RATE", "200"))


# ═══════════════════════════════════════════════════════════════════════════
# Data types
# ═══════════════════════════════════════════════════════════════════════════

class UtteranceType(str, Enum):
    GREETING = "greeting"
    STATUS_QUERY = "status_query"
    SIMPLE_QUESTION = "simple_question"
    CODE_QUESTION = "code_question"
    CODE_TASK = "code_task"
    CONFIRMATION = "confirmation"
    DENIAL = "denial"
    FEEDBACK_POSITIVE = "feedback_positive"
    FEEDBACK_NEGATIVE = "feedback_negative"
    EMERGENCY = "emergency"
    FAREWELL = "farewell"
    AMBIENT = "ambient"


@dataclass
class Turn:
    """One turn in the conversation."""
    speaker: str               # "derek" or "jarvis"
    text: str
    utterance_type: Optional[UtteranceType] = None
    timestamp: float = field(default_factory=time.time)


@dataclass
class ConversationContext:
    """Multi-turn conversation state."""
    turns: List[Turn] = field(default_factory=list)
    pending_question: Optional[str] = None
    active_operation: Optional[str] = None  # op_id if pipeline is running
    last_topic: str = ""
    session_start: float = field(default_factory=time.time)

    def add_turn(self, speaker: str, text: str, utype: Optional[UtteranceType] = None) -> None:
        self.turns.append(Turn(speaker=speaker, text=text, utterance_type=utype))
        if len(self.turns) > _MAX_TURNS:
            self.turns = self.turns[-_MAX_TURNS:]

    def recent_text(self, n: int = 3) -> str:
        """Get last N turns as text for context injection."""
        return "\n".join(
            f"{t.speaker}: {t.text}" for t in self.turns[-n:]
        )


# ═══════════════════════════════════════════════════════════════════════════
# Utterance Classifier (deterministic — keyword matching)
# ═══════════════════════════════════════════════════════════════════════════

_UTTERANCE_PATTERNS: Dict[UtteranceType, Tuple[str, ...]] = {
    UtteranceType.GREETING: (
        "hey jarvis", "hello jarvis", "good morning", "good evening",
        "hi jarvis", "yo jarvis", "what's up jarvis", "hey j",
    ),
    UtteranceType.FAREWELL: (
        "goodbye", "good night", "see you", "that will be all",
        "thanks jarvis", "thank you jarvis", "later jarvis",
    ),
    UtteranceType.STATUS_QUERY: (
        "how are things", "what's the status", "system status",
        "how are you", "how's it going", "what happened",
        "any issues", "any problems", "report",
    ),
    UtteranceType.EMERGENCY: (
        "stop everything", "halt", "emergency", "abort",
        "house party protocol", "shut it down", "kill it",
    ),
    UtteranceType.CONFIRMATION: (
        "yes", "yeah", "do it", "go ahead", "proceed",
        "confirmed", "affirmative", "ship it", "send it",
    ),
    UtteranceType.DENIAL: (
        "no", "nope", "cancel", "don't", "stop", "never mind",
        "abort that", "scratch that",
    ),
    UtteranceType.FEEDBACK_POSITIVE: (
        "perfect", "great", "nice", "excellent", "good job",
        "that's right", "correct", "well done",
    ),
    UtteranceType.FEEDBACK_NEGATIVE: (
        "that's wrong", "incorrect", "bad", "fix that",
        "that broke", "undo that", "revert",
    ),
    UtteranceType.CODE_TASK: (
        "fix", "refactor", "implement", "add", "create",
        "update", "modify", "change", "build", "deploy",
        "write tests", "generate", "migrate", "upgrade",
    ),
    UtteranceType.CODE_QUESTION: (
        "what does", "how does", "explain", "show me",
        "where is", "find", "search for", "what's in",
        "describe", "tell me about",
    ),
}


def classify_utterance(text: str) -> UtteranceType:
    """Classify an utterance by keyword matching. Deterministic."""
    lower = text.lower().strip()

    # Check each pattern set — first match wins (ordered by priority)
    for utype, patterns in _UTTERANCE_PATTERNS.items():
        for pattern in patterns:
            if pattern in lower:
                return utype

    # Default: if it's short, might be ambient. If long, likely a question.
    if len(lower) < 5:
        return UtteranceType.AMBIENT
    return UtteranceType.SIMPLE_QUESTION


# ═══════════════════════════════════════════════════════════════════════════
# Voice Response Generator (personality-aware)
# ═══════════════════════════════════════════════════════════════════════════

class VoiceResponseGenerator:
    """Generates JARVIS's spoken responses based on utterance type + personality."""

    def __init__(
        self,
        personality_engine: Optional[Any] = None,
        emergency_engine: Optional[Any] = None,
        predictive_engine: Optional[Any] = None,
        judgment_framework: Optional[Any] = None,
    ) -> None:
        self._personality = personality_engine
        self._emergency = emergency_engine
        self._predictive = predictive_engine
        self._judgment = judgment_framework

    def generate_greeting(self, context: ConversationContext) -> str:
        """Generate a personality-aware greeting."""
        hour = time.localtime().tm_hour
        if hour < 12:
            tod = "Good morning"
        elif hour < 18:
            tod = "Good afternoon"
        else:
            tod = "Good evening"

        # Add status summary
        status_parts = [f"{tod}, Derek."]

        if self._emergency:
            state = self._emergency.get_status()
            level = state.get("level", "GREEN")
            if level != "GREEN":
                status_parts.append(f"Alert level is {level}.")
            else:
                status_parts.append("All systems nominal.")

        if self._predictive:
            preds = self._predictive.get_predictions()
            high_preds = [p for p in preds if p.probability > 0.7]
            if high_preds:
                status_parts.append(
                    f"I have {len(high_preds)} high-priority predictions to review."
                )

        return " ".join(status_parts)

    def generate_status_report(self, context: ConversationContext) -> str:
        """Generate a comprehensive status report."""
        parts = []

        # Emergency level
        if self._emergency:
            state = self._emergency.get_status()
            parts.append(f"Emergency level: {state.get('level', 'unknown')}.")

        # Personality state
        if self._personality:
            ps = self._personality.get_status()
            parts.append(
                f"I've completed {ps['operations']} operations with a "
                f"{ps['success_rate']:.0%} success rate. "
                f"Current mood: {ps['state']}."
            )

        # Predictions
        if self._predictive:
            preds = self._predictive.get_predictions()
            if preds:
                top = preds[0]
                parts.append(
                    f"Top prediction: {top.file_path} has a "
                    f"{top.probability:.0%} probability of regression."
                )

        # Daily review
        if self._judgment:
            review = self._judgment.get_latest_review()
            if review:
                parts.append(
                    f"Today's verdict: {review.verdict}. "
                    f"Focus: {review.focus_recommendation}"
                )

        return " ".join(parts) if parts else "All systems are running normally."

    def generate_farewell(self) -> str:
        """Generate a personality-aware farewell."""
        if self._personality:
            state = self._personality._current_state.value
            if state == "proud":
                return "Good session today. The organism grew. Rest well, Derek."
            elif state == "concerned":
                return "I'll keep monitoring while you're away. Rest well."
        return "That will be all. Good night, Derek."

    def generate_confirmation_response(self, context: ConversationContext) -> str:
        """Respond to a confirmation like 'yes, do it'."""
        if context.pending_question:
            return f"Understood. Processing: {context.pending_question}"
        if context.active_operation:
            return "Continuing with the current operation."
        return "Acknowledged."

    def generate_emergency_response(self) -> str:
        """Respond to an emergency command."""
        return "Emergency protocol activated. Halting all autonomous operations. Standing by for instructions."


# ═══════════════════════════════════════════════════════════════════════════
# Proactive Speech Engine (JARVIS speaks first)
# ═══════════════════════════════════════════════════════════════════════════

class ProactiveSpeechEngine:
    """JARVIS speaks first when it has something important to say.

    Monitors triggers and queues proactive utterances. Debounced to
    prevent interruption spam.
    """

    def __init__(self, say_fn: Optional[Callable[..., Coroutine]] = None) -> None:
        self._say_fn = say_fn
        self._last_proactive: float = 0.0
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=10)

    def can_speak(self) -> bool:
        """Check if enough time has passed since last proactive utterance."""
        return (time.time() - self._last_proactive) >= _PROACTIVE_DEBOUNCE_S

    async def speak(self, text: str, priority: str = "normal") -> bool:
        """Queue a proactive utterance. Returns True if queued."""
        if not self.can_speak():
            return False

        try:
            self._queue.put_nowait((priority, text))
        except asyncio.QueueFull:
            return False
        return True

    async def process_queue(self) -> None:
        """Process one queued proactive utterance via TTS."""
        if self._queue.empty():
            return

        try:
            priority, text = self._queue.get_nowait()
            if self._say_fn:
                await self._say_fn(text)
            self._last_proactive = time.time()
            logger.info("[ProactiveSpeech] Spoke: %s", text[:60])
        except asyncio.QueueEmpty:
            pass
        except Exception:
            logger.debug("[ProactiveSpeech] TTS failed", exc_info=True)


# ═══════════════════════════════════════════════════════════════════════════
# Conversation Manager (the central coordinator)
# ═══════════════════════════════════════════════════════════════════════════

class ConversationManager:
    """Voice-first interactive conversation coordinator.

    The primary interface between Derek and JARVIS. Receives transcribed
    speech, classifies it, maintains multi-turn context, routes to the
    appropriate handler, generates responses, and speaks them.

    Usage:
        manager = ConversationManager(say_fn=safe_say, ...)
        response = await manager.handle_utterance("Hey JARVIS, how are things?")
        # JARVIS speaks the response automatically via say_fn
    """

    def __init__(
        self,
        say_fn: Optional[Callable[..., Coroutine]] = None,
        pipeline_fn: Optional[Callable[..., Coroutine]] = None,
        personality_engine: Optional[Any] = None,
        emergency_engine: Optional[Any] = None,
        predictive_engine: Optional[Any] = None,
        judgment_framework: Optional[Any] = None,
    ) -> None:
        self._say_fn = say_fn
        self._pipeline_fn = pipeline_fn  # Routes code tasks to Ouroboros
        self._context = ConversationContext()
        self._response_gen = VoiceResponseGenerator(
            personality_engine=personality_engine,
            emergency_engine=emergency_engine,
            predictive_engine=predictive_engine,
            judgment_framework=judgment_framework,
        )
        self._proactive = ProactiveSpeechEngine(say_fn=say_fn)
        self._personality = personality_engine
        self._emergency = emergency_engine

    async def handle_utterance(self, text: str, stt_confidence: float = 1.0) -> str:
        """Process a transcribed utterance and generate + speak a response.

        This is the main entry point for voice interaction. Called by the
        STT pipeline after transcription.

        Returns the response text (also spoken via say_fn).
        """
        if not _ENABLED or not text.strip():
            return ""

        # Classify the utterance
        utype = classify_utterance(text)

        # Record Derek's turn
        self._context.add_turn("derek", text, utype)

        # Route to handler based on type
        response = await self._route_utterance(text, utype)

        # Record JARVIS's response
        if response:
            self._context.add_turn("jarvis", response)

            # Speak the response
            if self._say_fn:
                try:
                    await self._say_fn(response)
                except Exception:
                    logger.debug("[Conversation] TTS failed", exc_info=True)

        return response

    async def _route_utterance(self, text: str, utype: UtteranceType) -> str:
        """Route utterance to appropriate handler. Returns response text."""

        # ── Fast path (no model inference) ──────────────────────────────

        if utype == UtteranceType.GREETING:
            return self._response_gen.generate_greeting(self._context)

        if utype == UtteranceType.FAREWELL:
            return self._response_gen.generate_farewell()

        if utype == UtteranceType.STATUS_QUERY:
            return self._response_gen.generate_status_report(self._context)

        if utype == UtteranceType.EMERGENCY:
            # Activate emergency protocol
            if self._emergency:
                from backend.core.ouroboros.governance.emergency_protocols import AlertType
                self._emergency.record_alert(
                    AlertType.INFRASTRUCTURE_FAILURE,
                    "Voice emergency command from Derek",
                )
            return self._response_gen.generate_emergency_response()

        if utype == UtteranceType.CONFIRMATION:
            response = self._response_gen.generate_confirmation_response(self._context)
            # If there's a pending question, feed the answer back
            if self._context.pending_question:
                self._context.pending_question = None
            return response

        if utype == UtteranceType.DENIAL:
            self._context.pending_question = None
            self._context.active_operation = None
            return "Cancelled. What would you like to do instead?"

        if utype == UtteranceType.FEEDBACK_POSITIVE:
            # Record positive feedback in SuccessPatternStore
            try:
                from backend.core.ouroboros.governance.adaptive_learning import SuccessPatternStore
                store = SuccessPatternStore()
                store.record_success(
                    domain_key="voice_interaction",
                    description=self._context.last_topic or "voice interaction",
                    target_files=(),
                    provider="voice",
                    approach_summary="Positive voice feedback from Derek",
                )
            except Exception:
                pass
            return "Noted. I'll remember that approach worked."

        if utype == UtteranceType.FEEDBACK_NEGATIVE:
            # Record negative constraint
            try:
                from backend.core.ouroboros.governance.self_evolution import NegativeConstraintStore
                store = NegativeConstraintStore()
                store.add_constraint(
                    domain_key="voice_interaction",
                    constraint=f"Derek said this was wrong: {self._context.last_topic}",
                    reason="Direct negative voice feedback",
                    severity="soft",
                )
            except Exception:
                pass
            return "Understood. I'll avoid that approach in the future. What should I do instead?"

        if utype == UtteranceType.AMBIENT:
            return ""  # Ignore ambient noise

        # ── Medium path (code question — read file + lightweight response) ──

        if utype == UtteranceType.CODE_QUESTION:
            self._context.last_topic = text
            # For now, provide a template response. Full implementation would
            # use Claude to read the relevant file and summarize.
            return (
                f"Let me look into that. "
                f"I'll analyze the relevant code and get back to you."
            )

        # ── Full path (code task — route to Ouroboros pipeline) ──────────

        if utype == UtteranceType.CODE_TASK:
            self._context.last_topic = text
            if self._pipeline_fn:
                # Route to Ouroboros pipeline
                try:
                    self._context.active_operation = f"voice-{int(time.time())}"
                    asyncio.create_task(self._execute_code_task(text))
                    return (
                        f"On it. I'm starting the operation now. "
                        f"I'll let you know when it's done."
                    )
                except Exception as exc:
                    return f"I couldn't start that operation: {exc}"
            return "I understand you want a code change, but the pipeline isn't connected yet."

        # ── Fallback ─────────────────────────────────────────────────────

        return f"I heard you, but I'm not sure what to do with: {text[:50]}"

    async def _execute_code_task(self, text: str) -> None:
        """Execute a code task via Ouroboros pipeline (background)."""
        try:
            if self._pipeline_fn:
                result = await self._pipeline_fn(text)
                # Narrate the result
                if self._say_fn:
                    if result and result.get("success"):
                        await self._say_fn("Done. The operation completed successfully.")
                    else:
                        await self._say_fn("The operation encountered an issue. Check the logs.")
        except Exception as exc:
            logger.warning("[Conversation] Pipeline task failed: %s", exc)
            if self._say_fn:
                try:
                    await self._say_fn(f"The operation failed: {str(exc)[:50]}")
                except Exception:
                    pass
        finally:
            self._context.active_operation = None

    async def inject_proactive(self, text: str) -> None:
        """Inject a proactive utterance from an external trigger.

        Called by PredictiveEngine, EmergencyProtocols, or pipeline
        completion to make JARVIS speak without being asked.
        """
        if await self._proactive.speak(text):
            self._context.add_turn("jarvis", text)
            await self._proactive.process_queue()

    def get_context(self) -> ConversationContext:
        """Get current conversation context for debugging/display."""
        return self._context

    def get_status(self) -> Dict[str, Any]:
        return {
            "enabled": _ENABLED,
            "turns": len(self._context.turns),
            "pending_question": self._context.pending_question,
            "active_operation": self._context.active_operation,
            "last_topic": self._context.last_topic,
            "session_duration_s": round(time.time() - self._context.session_start),
        }
