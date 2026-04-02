"""
Persona Engine — LLM-powered reasoning for Trinity personas.

Constructs layered prompts (Role Prefix + Manifesto Slice + Thread Context
+ Intent Instruction) and dispatches them to Doubleword for inference.
Parses structured JSON responses with plaintext fallback and failure
resilience.

Each persona maps to a Trinity role:
    jarvis   -> body
    j_prime  -> mind
    reactor  -> immune_system
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from backend.hive.manifesto_slices import ROLE_PREFIXES, get_manifesto_slice
from backend.hive.model_router import HiveModelRouter
from backend.hive.thread_models import (
    AgentLogMessage,
    CognitiveState,
    HiveMessage,
    HiveThread,
    PersonaIntent,
    PersonaReasoningMessage,
)

logger = logging.getLogger(__name__)


# ============================================================================
# CONSTANTS
# ============================================================================

# Maps Trinity persona names to their functional roles.
PERSONA_ROLE_MAP: Dict[str, str] = {
    "jarvis": "body",
    "j_prime": "mind",
    "reactor": "immune_system",
}

# Intent-specific instruction text injected as Layer C of the prompt.
# Each instruction tells the model what kind of reasoning to produce.
_INTENT_INSTRUCTIONS: Dict[PersonaIntent, str] = {
    PersonaIntent.OBSERVE: (
        "Synthesize the telemetry and specialist logs in this thread. "
        "Identify patterns, anomalies, and environmental state. "
        "Report only what the data shows — do not propose solutions."
    ),
    PersonaIntent.PROPOSE: (
        "Propose a concrete solution to the issue described in this thread. "
        "Include scope, expected outcome, rollback strategy, and the "
        "minimal diff required. Cite specific code paths where applicable."
    ),
    PersonaIntent.CHALLENGE: (
        "Challenge the current proposal by citing specific evidence: "
        "conflicting telemetry, unaddressed edge cases, historical "
        "regressions, or violated invariants. State what additional "
        "evidence would resolve your concern."
    ),
    PersonaIntent.SUPPORT: (
        "Support the current proposal by confirming alignment. State "
        "which evidence convinced you, which Manifesto principles the "
        "proposal upholds, and any minor refinements to strengthen it."
    ),
    PersonaIntent.VALIDATE: (
        "Assess the safety and correctness of the proposed change. "
        "Evaluate blast radius, reversibility, test coverage, and "
        "whether unaddressed challenges remain. Give an approve or "
        "reject verdict with clear justification."
    ),
}

# Maximum characters per serialized thread message in the prompt.
_MSG_TRUNCATION_LIMIT = 500


# ============================================================================
# PERSONA ENGINE
# ============================================================================


class PersonaEngine:
    """Generate persona-grounded reasoning via layered LLM prompts.

    The engine constructs a four-layer prompt:

    1. **Layer A** — Role prefix from :data:`ROLE_PREFIXES`.
    2. **Layer B** — Manifesto slice from :func:`get_manifesto_slice`.
    3. **Thread context** — Serialized thread messages (truncated).
    4. **Intent instruction** — Action-specific directive + response format.

    Then dispatches it to Doubleword for inference, parses the response,
    and returns a :class:`PersonaReasoningMessage`.

    Parameters
    ----------
    doubleword:
        Doubleword client instance (must expose ``prompt_only`` async method).
    model_router:
        :class:`HiveModelRouter` for cognitive-state-aware model selection.
    """

    def __init__(self, doubleword: Any, model_router: HiveModelRouter) -> None:
        self._dw = doubleword
        self._model_router = model_router

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def generate_reasoning(
        self,
        persona: str,
        intent: PersonaIntent,
        thread: HiveThread,
    ) -> PersonaReasoningMessage:
        """Generate a reasoning message for *persona* with *intent* in *thread*.

        Parameters
        ----------
        persona:
            Trinity persona name (``"jarvis"``, ``"j_prime"``, ``"reactor"``).
        intent:
            The reasoning intent for this invocation.
        thread:
            The :class:`HiveThread` providing debate context.

        Returns
        -------
        PersonaReasoningMessage
            Always returns a message — on failure, ``confidence`` is 0.0 and
            ``reasoning`` describes the error.
        """
        role = PERSONA_ROLE_MAP.get(persona, "body")
        model = self._model_router.get_model(thread.cognitive_state)
        config = self._model_router.get_config(thread.cognitive_state)
        caller_id = f"hive_{persona}_{intent.value}"

        try:
            prompt = self._build_prompt(persona, intent, thread)
            raw = await self._dw.prompt_only(
                prompt,
                model=model,
                caller_id=caller_id,
                max_tokens=config.get("max_tokens", 4000),
            )
            if not raw or not raw.strip():
                return self._failure_message(
                    persona=persona,
                    role=role,
                    intent=intent,
                    thread_id=thread.thread_id,
                    model=model,
                    error="empty response from model",
                )
            return self._parse_response(
                raw=raw,
                persona=persona,
                role=role,
                intent=intent,
                thread_id=thread.thread_id,
                model=model,
            )
        except Exception as exc:
            logger.warning(
                "PersonaEngine inference failed for %s/%s: %s",
                persona,
                intent.value,
                exc,
                exc_info=True,
            )
            return self._failure_message(
                persona=persona,
                role=role,
                intent=intent,
                thread_id=thread.thread_id,
                model=model,
                error=str(exc),
            )

    # ------------------------------------------------------------------
    # Prompt construction
    # ------------------------------------------------------------------

    def _build_prompt(
        self,
        persona: str,
        intent: PersonaIntent,
        thread: HiveThread,
    ) -> str:
        """Assemble the four-layer prompt.

        Layer A: Role prefix (persona identity + boundaries).
        Layer B: Manifesto slice (intent-aligned principles).
        Layer C: Thread context (serialized messages, truncated).
        Layer D: Intent instruction + response format hint.
        """
        parts: List[str] = []

        # Layer A — Role prefix
        role_prefix = ROLE_PREFIXES.get(persona, "")
        if role_prefix:
            parts.append(f"[ROLE]\n{role_prefix}")

        # Layer B — Manifesto slice
        try:
            manifesto = get_manifesto_slice(intent)
            parts.append(f"[MANIFESTO CONTEXT]\n{manifesto}")
        except KeyError:
            logger.debug("No manifesto slice for intent %s", intent)

        # Layer C — Thread context
        thread_context = self._serialize_thread_context(thread)
        if thread_context:
            parts.append(f"[THREAD CONTEXT]\n{thread_context}")

        # Layer D — Intent instruction + response format
        instruction = _INTENT_INSTRUCTIONS.get(intent, "Provide your analysis.")
        parts.append(f"[INSTRUCTION]\n{instruction}")

        # Response format hint
        if intent == PersonaIntent.VALIDATE:
            format_hint = (
                "Respond in JSON with keys: reasoning (string), confidence "
                "(float 0-1), manifesto_principle (string), "
                "validate_verdict (string: 'approve' or 'reject')."
            )
        else:
            format_hint = (
                "Respond in JSON with keys: reasoning (string), confidence "
                "(float 0-1), manifesto_principle (string)."
            )
        parts.append(f"[RESPONSE FORMAT]\n{format_hint}")

        return "\n\n".join(parts)

    def _serialize_thread_context(self, thread: HiveThread) -> str:
        """Serialize thread messages into a compact text block.

        Each message is truncated to :data:`_MSG_TRUNCATION_LIMIT` characters.
        """
        lines: List[str] = []
        for msg in thread.messages:
            line = self._serialize_message(msg)
            if len(line) > _MSG_TRUNCATION_LIMIT:
                line = line[:_MSG_TRUNCATION_LIMIT] + "..."
            lines.append(line)
        return "\n".join(lines)

    @staticmethod
    def _serialize_message(msg: HiveMessage) -> str:
        """Produce a single-line text summary of a message."""
        if isinstance(msg, AgentLogMessage):
            return (
                f"[{msg.severity.upper()}] {msg.agent_name} "
                f"({msg.trinity_parent}/{msg.category}): "
                f"{json.dumps(msg.payload, default=str)}"
            )
        if isinstance(msg, PersonaReasoningMessage):
            return (
                f"[{msg.intent.value.upper()}] {msg.persona} "
                f"(confidence={msg.confidence:.2f}): "
                f"{msg.reasoning}"
            )
        # Defensive fallback for unknown message types.
        return str(msg)

    # ------------------------------------------------------------------
    # Response parsing
    # ------------------------------------------------------------------

    def _parse_response(
        self,
        raw: str,
        persona: str,
        role: str,
        intent: PersonaIntent,
        thread_id: str,
        model: Optional[str],
    ) -> PersonaReasoningMessage:
        """Parse model output into a :class:`PersonaReasoningMessage`.

        Tries JSON first; falls back to plaintext with confidence=0.5.
        """
        token_cost = len(raw) // 4

        # Attempt JSON extraction
        parsed = self._try_parse_json(raw)
        if parsed is not None:
            reasoning = parsed.get("reasoning", raw)
            confidence = self._clamp_confidence(parsed.get("confidence", 0.5))
            manifesto_principle = parsed.get("manifesto_principle")
            validate_verdict = parsed.get("validate_verdict")

            return PersonaReasoningMessage(
                thread_id=thread_id,
                persona=persona,
                role=role,
                intent=intent,
                references=[],
                reasoning=reasoning,
                confidence=confidence,
                model_used=model or "unknown",
                token_cost=token_cost,
                manifesto_principle=manifesto_principle,
                validate_verdict=validate_verdict,
            )

        # Plaintext fallback
        return PersonaReasoningMessage(
            thread_id=thread_id,
            persona=persona,
            role=role,
            intent=intent,
            references=[],
            reasoning=raw.strip(),
            confidence=0.5,
            model_used=model or "unknown",
            token_cost=token_cost,
        )

    @staticmethod
    def _try_parse_json(raw: str) -> Optional[Dict[str, Any]]:
        """Attempt to parse JSON from the raw response.

        Handles both clean JSON and JSON embedded in markdown code blocks.
        """
        text = raw.strip()

        # Strip markdown code fences if present.
        if text.startswith("```"):
            lines = text.split("\n")
            # Remove first line (```json or ```) and last line (```)
            inner_lines = []
            started = False
            for line in lines:
                if not started:
                    if line.strip().startswith("```"):
                        started = True
                        continue
                elif line.strip() == "```":
                    break
                else:
                    inner_lines.append(line)
            text = "\n".join(inner_lines).strip()

        try:
            obj = json.loads(text)
            if isinstance(obj, dict):
                return obj
        except (json.JSONDecodeError, TypeError):
            pass
        return None

    @staticmethod
    def _clamp_confidence(value: Any) -> float:
        """Clamp a confidence value to [0.0, 1.0]."""
        try:
            f = float(value)
        except (TypeError, ValueError):
            return 0.5
        return max(0.0, min(1.0, f))

    # ------------------------------------------------------------------
    # Failure handling
    # ------------------------------------------------------------------

    @staticmethod
    def _failure_message(
        persona: str,
        role: str,
        intent: PersonaIntent,
        thread_id: str,
        model: Optional[str],
        error: str,
    ) -> PersonaReasoningMessage:
        """Build a sentinel message for inference failures."""
        return PersonaReasoningMessage(
            thread_id=thread_id,
            persona=persona,
            role=role,
            intent=intent,
            references=[],
            reasoning=f"[inference failed: {error}]",
            confidence=0.0,
            model_used=model or "unknown",
            token_cost=0,
        )
