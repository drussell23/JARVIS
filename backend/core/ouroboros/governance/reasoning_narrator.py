"""
ReasoningNarrator — Explains WHY decisions were made, not just WHAT happened.

Closes the "explaining code" gap. VoiceNarrator narrates events (WHAT).
ReasoningNarrator explains the reasoning chain (WHY) by pulling from:
- Entropy scores (why this domain triggered/didn't)
- Provider selection reasoning (why Doubleword vs J-Prime vs Claude)
- Critique summaries (why validation failed)
- Domain rules (what historical patterns informed this decision)
- Success patterns (what worked before for similar tasks)

Boundary Principle:
  Deterministic: Data collection from entropy, provider stats, rules.
  Agentic: The narration TEXT is assembled from deterministic signals,
  then voiced via safe_say(). No model inference for narration content.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_ENABLED = os.environ.get(
    "JARVIS_REASONING_NARRATOR_ENABLED", "true"
).lower() in ("true", "1", "yes")


@dataclass
class ReasoningTrace:
    """Captures the WHY behind an operation's decisions."""
    op_id: str
    phases: List[Dict[str, str]]  # [{phase, reasoning}]

    def add_phase(self, phase: str, reasoning: str) -> None:
        self.phases.append({"phase": phase, "reasoning": reasoning})

    def format_for_voice(self) -> str:
        """Format the reasoning trace for voice narration."""
        if not self.phases:
            return ""
        parts = []
        for p in self.phases[-3:]:  # Last 3 phases for brevity
            parts.append(f"{p['phase']}: {p['reasoning']}")
        return ". ".join(parts)

    def format_for_log(self) -> str:
        """Format the full trace for ledger/log recording."""
        lines = [f"Reasoning Trace for {self.op_id}:"]
        for p in self.phases:
            lines.append(f"  [{p['phase']}] {p['reasoning']}")
        return "\n".join(lines)


class ReasoningNarrator:
    """Assembles and narrates the WHY behind governance decisions.

    Called at key decision points in the orchestrator to build a
    ReasoningTrace. At COMPLETE/POSTMORTEM, the trace is narrated
    via voice and recorded in the ledger.

    Narration examples:
      "I chose Doubleword because this domain has high complexity and
       historically succeeds 80% with the 397B model. Validation passed
       on the first attempt. No entropy concerns."

      "I used Claude as fallback because J-Prime was unhealthy. The
       domain 'voice_unlock::.py' has a 60% chronic failure rate —
       I applied extra sandbox validation per the FALSE_CONFIDENCE protocol."
    """

    def __init__(self, say_fn: Optional[Any] = None) -> None:
        self._say_fn = say_fn
        self._enabled = _ENABLED
        self._active_traces: Dict[str, ReasoningTrace] = {}

    def start_trace(self, op_id: str) -> ReasoningTrace:
        """Start a new reasoning trace for an operation."""
        trace = ReasoningTrace(op_id=op_id, phases=[])
        self._active_traces[op_id] = trace
        return trace

    def record_classify(self, op_id: str, risk_tier: str, reason: str) -> None:
        trace = self._active_traces.get(op_id)
        if trace:
            trace.add_phase("CLASSIFY", f"Risk={risk_tier} because {reason}")

    def record_route(self, op_id: str, provider: str, reason: str) -> None:
        trace = self._active_traces.get(op_id)
        if trace:
            trace.add_phase("ROUTE", f"Selected {provider} because {reason}")

    def record_generate(
        self, op_id: str, provider: str, candidates: int, duration_s: float
    ) -> None:
        trace = self._active_traces.get(op_id)
        if trace:
            trace.add_phase(
                "GENERATE",
                f"{provider} produced {candidates} candidates in {duration_s:.1f}s",
            )

    def record_validate(
        self, op_id: str, passed: bool, failure_class: Optional[str] = None
    ) -> None:
        trace = self._active_traces.get(op_id)
        if trace:
            if passed:
                trace.add_phase("VALIDATE", "Passed on first attempt")
            else:
                trace.add_phase(
                    "VALIDATE",
                    f"Failed ({failure_class or 'unknown'}), entering repair",
                )

    def record_entropy(
        self, op_id: str, systemic: float, quadrant: str
    ) -> None:
        trace = self._active_traces.get(op_id)
        if trace:
            trace.add_phase(
                "ENTROPY",
                f"Systemic={systemic:.3f}, quadrant={quadrant}",
            )

    def record_outcome(self, op_id: str, success: bool, reason: str = "") -> None:
        trace = self._active_traces.get(op_id)
        if trace:
            if success:
                trace.add_phase("COMPLETE", reason or "Operation succeeded")
            else:
                trace.add_phase("POSTMORTEM", reason or "Operation failed")

    async def narrate_completion(self, op_id: str) -> Optional[str]:
        """Narrate the reasoning trace at operation completion.

        Returns the narration text (for ledger recording).
        """
        trace = self._active_traces.pop(op_id, None)
        if trace is None or not self._enabled:
            return None

        narration = trace.format_for_voice()
        if not narration:
            return None

        # Log full trace
        logger.info("[ReasoningNarrator] %s", trace.format_for_log())

        # Voice narration (fire-and-forget)
        if self._say_fn is not None:
            try:
                await self._say_fn(narration)
            except Exception:
                logger.debug("[ReasoningNarrator] Voice failed", exc_info=True)

        return narration
