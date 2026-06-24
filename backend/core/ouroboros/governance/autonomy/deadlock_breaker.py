"""deadlock_breaker -- the Epistemic Deadlock Breaker (Phase 1c, G3).

When two workers locked in a clarification round-trip either (a) trip the
``SemanticStagnationDetector`` (intelligent early break) OR (b) reach
``max_turn_budget + 1`` turns without a verified artifact (the dumb backstop),
the breaker SHATTERS the deadlock.

**Keyed by worker-PAIR, not correlation_id (red-team CRITICAL #2).** Both the
integer turn budget AND the stagnation window are bucketed on the stable
``frozenset({worker_a, worker_b})`` pair-key. A caller that mints a fresh
``correlation_id`` on every turn (the default ``request()`` path) can NO LONGER
reset the turn count or the similarity window -- cross-correlation turns
between the same pair feed the SAME budget + the SAME stagnation bucket. The
breaker SHATTERS the deadlock:

    1. kill both worker processes (cancel their units via the existing
       hard-kill / unit-cancel wrapper),
    2. dissolve the sub-graph (cancel the pair's units + dependents),
    3. bubble ``[SOVEREIGN YIELD: EPISTEMIC DEADLOCK]`` with the bounded,
       sanitized transcript of the deadlocked exchange.

Terminal + op-never-lost: the deadlock is SEALED (transcript captured +
SovereignYield emitted) and BUBBLED via :class:`DeadlockInterruptedException`,
never silently dropped.

Fail-CLOSED: a turn-count read error / ambiguous "is there a verified
artifact?" -> treat as a deadlock (interrupt). The breaker never errs on the
side of "keep talking".

REUSE: ``ide_observability_stream.publish_sovereign_yield`` for the yield;
``secure_logging.sanitize_for_log`` + ``conversation_bridge.redact_secrets``
for the bounded transcript.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional, Sequence

from backend.core.ouroboros.governance.autonomy.stagnation_detector import (
    SemanticStagnationDetector,
    pair_key,
)
from backend.core.ouroboros.governance.conversation_bridge import redact_secrets
from backend.core.secure_logging import sanitize_for_log

logger = logging.getLogger(__name__)

_YIELD_REASON = "epistemic_deadlock"


def _env_int(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def max_turn_budget() -> int:
    """Integer turn ceiling for a clarification pair (the dumb backstop)."""
    return _env_int("JARVIS_SWARM_CLARIFICATION_MAX_TURNS", 3)


def _max_transcript_turns() -> int:
    return _env_int("JARVIS_SWARM_DEADLOCK_TRANSCRIPT_TURNS", 12)


def _max_transcript_chars() -> int:
    return _env_int("JARVIS_SWARM_DEADLOCK_TRANSCRIPT_CHARS", 2000)


class DeadlockInterruptedException(Exception):
    """Raised when an epistemic deadlock is shattered.

    Carries the bounded, sanitized transcript + the worker ids whose units
    were dissolved. Terminal -- the orchestrator treats this as op-yielded, not
    a silent drop.
    """

    def __init__(
        self,
        *,
        correlation_id: str,
        worker_a: str,
        worker_b: str,
        trigger: str,
        transcript: str,
        dissolved_units: Sequence[str] = (),
    ) -> None:
        self.correlation_id = correlation_id
        self.worker_a = worker_a
        self.worker_b = worker_b
        self.trigger = trigger  # "semantic_stagnation" | "max_turn_budget"
        self.transcript = transcript
        self.dissolved_units = tuple(dissolved_units)
        super().__init__(
            f"[SOVEREIGN YIELD: EPISTEMIC DEADLOCK] corr={correlation_id} "
            f"trigger={trigger} workers=({worker_a},{worker_b}) "
            f"dissolved={len(self.dissolved_units)}"
        )


def _sanitize_transcript(turns: Sequence[str]) -> str:
    """Bounded + Tier -1 sanitized transcript of the deadlocked exchange.

    Caps the number of turns AND the total chars; strips control chars and
    redacts secret shapes so a deadlock yield never leaks raw worker chatter or
    credentials. NEVER raises.
    """
    try:
        recent = list(turns)[-_max_transcript_turns():]
        lines: List[str] = []
        for idx, turn in enumerate(recent):
            cleaned = sanitize_for_log(str(turn), max_len=_max_transcript_chars())
            redacted, _ = redact_secrets(cleaned)
            lines.append(f"[{idx}] {redacted}")
        joined = "\n".join(lines)
        return joined[: _max_transcript_chars()]
    except Exception:  # noqa: BLE001 -- fail-soft transcript.
        return "[transcript unavailable]"


def _emit_yield(op_id: str, reason: str) -> None:
    try:
        from backend.core.ouroboros.governance.ide_observability_stream import (
            publish_sovereign_yield,
        )

        publish_sovereign_yield(op_id, reason)
    except Exception:  # noqa: BLE001 -- fail-soft
        logger.debug("[DeadlockBreaker] publish_sovereign_yield failed", exc_info=True)


@dataclass
class EpistemicDeadlockBreaker:
    """Watches a clarification pair and shatters an epistemic deadlock.

    Parameters
    ----------
    correlation_id:
        The clarification pair under watch.
    worker_a, worker_b:
        The two worker ids in the round-trip.
    op_id:
        The op id for the SovereignYield.
    detector:
        A :class:`SemanticStagnationDetector` (the intelligent early break).
        Defaults to a fresh detector.
    kill_unit:
        Callback to hard-kill / cancel a unit by id (reuse the existing
        unit-cancel / hard-kill wrapper). Optional; missing -> dissolve still
        records the unit ids and yields.
    """

    correlation_id: str
    worker_a: str
    worker_b: str
    op_id: str = ""
    detector: SemanticStagnationDetector = field(default_factory=SemanticStagnationDetector)
    kill_unit: Optional[Callable[[str], Any]] = None
    _transcript: List[str] = field(default_factory=list)
    _turns: int = 0
    _pair_key: Any = None

    def __post_init__(self) -> None:
        if not self.op_id:
            self.op_id = self.correlation_id
        # Stable, corr-rotation-immune bucket key for BOTH the turn budget
        # (this instance's _turns) and the stagnation window (the detector
        # bucket). Rotating the correlation_id cannot reset either.
        self._pair_key = pair_key(self.worker_a, self.worker_b)

    def observe_turn(
        self,
        turn_text: str,
        *,
        verified_artifact: bool = False,
        correlation_id: Optional[str] = None,  # accepted but NOT used as a key
    ) -> None:
        """Record a turn from the pair. Raises :class:`DeadlockInterruptedException`
        on EITHER trigger.

        ``correlation_id`` may rotate per turn (the default ``request()``
        behavior) -- it is accepted for transcript/telemetry context but is
        DELIBERATELY NOT used as the budget/stagnation key. Both the turn budget
        and the stagnation window are keyed by the stable worker-PAIR, so corr
        rotation cannot reset the count or the similarity window.

        Fail-CLOSED: a turn-count / artifact-ambiguity error -> interrupt.
        ``verified_artifact=True`` means the exchange produced a real artifact
        on this turn -> the pair is NOT deadlocked (no interrupt), the turn is
        still recorded.
        """
        try:
            text = turn_text if isinstance(turn_text, str) else str(turn_text)
            self._transcript.append(text)
            self._turns += 1

            # A verified artifact resolves the exchange -- never a deadlock.
            if verified_artifact:
                self.detector.reset(self._pair_key)
                return

            # (a) intelligent early break -- semantic stagnation, bucketed by
            #     the PAIR key (corr-rotation-immune).
            stagnant = self.detector.observe(self._pair_key, text)
            if stagnant:
                self._shatter("semantic_stagnation")
                return  # unreachable -- _shatter raises

            # (b) dumb backstop -- max_turn_budget + 1 without an artifact. The
            #     count is per-pair (this instance), not per-correlation_id.
            if self._turns >= (max_turn_budget() + 1):
                self._shatter("max_turn_budget")
                return  # unreachable
        except DeadlockInterruptedException:
            raise
        except Exception:  # noqa: BLE001 -- fail-CLOSED -> interrupt.
            logger.debug(
                "[DeadlockBreaker] observe_turn raised -> interrupting (fail-closed)",
                exc_info=True,
            )
            self._shatter("fail_closed")

    def _shatter(self, trigger: str) -> None:
        """Kill both workers, dissolve the sub-graph, bubble the yield."""
        dissolved: List[str] = []
        # 1. + 2. kill both worker processes / dissolve their units.
        for wid in (self.worker_a, self.worker_b):
            if not wid:
                continue
            dissolved.append(wid)
            if self.kill_unit is not None:
                try:
                    self.kill_unit(wid)
                except Exception:  # noqa: BLE001 -- kill is best-effort; never blocks the dissolve.
                    logger.debug(
                        "[DeadlockBreaker] kill_unit(%s) raised (non-fatal)", wid,
                        exc_info=True,
                    )

        transcript = _sanitize_transcript(self._transcript)

        # 3. bubble [SOVEREIGN YIELD: EPISTEMIC DEADLOCK] (seal-before-raise).
        logger.warning(
            "[SOVEREIGN YIELD: EPISTEMIC DEADLOCK] op=%s corr=%s trigger=%s "
            "workers=(%s,%s) dissolved=%d",
            self.op_id,
            self.correlation_id,
            trigger,
            self.worker_a,
            self.worker_b,
            len(dissolved),
        )
        _emit_yield(self.op_id, _YIELD_REASON)

        raise DeadlockInterruptedException(
            correlation_id=self.correlation_id,
            worker_a=self.worker_a,
            worker_b=self.worker_b,
            trigger=trigger,
            transcript=transcript,
            dissolved_units=dissolved,
        )
