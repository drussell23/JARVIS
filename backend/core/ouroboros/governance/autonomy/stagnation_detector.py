"""stagnation_detector -- Semantic Stagnation Detector (Phase 1c, G3).

The intelligent early break for the Epistemic Deadlock Breaker. Tracks the
clarification turns for a ``correlation_id`` (one request/response pair) and
computes a fast Jaccard / token-overlap similarity between consecutive turns.
When the pair starts repeating the same logic (high similarity for a window of
turns), it is looping -- signal the breaker to shatter EARLY, before the integer
``max_turn_budget`` is reached.

Pure stdlib (no heavy dep): normalize -> lowercase token set -> Jaccard
``|A intersect B| / |A union B|``. Optionally a normalized-intent hash for
exact-repeat detection.

Fail-CLOSED: an unparseable turn / detector error -> treat as a stagnation
signal (break), never as "keep talking".

**Gated under the swarm master (no standalone env flag needed).** Thresholds
are env-tunable; the detector itself is a pure analyzer with no side effects.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _env_float(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default
    return value


def _env_int(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def stagnation_threshold() -> float:
    """Jaccard similarity at/above which a turn pair is considered stagnant."""
    val = _env_float("JARVIS_SWARM_STAGNATION_THRESHOLD", 0.85)
    # Clamp to a sane [0, 1] band; out-of-band -> default.
    if not (0.0 <= val <= 1.0):
        return 0.85
    return val


def stagnation_window() -> int:
    """Consecutive stagnant turn-pairs required to declare SEMANTIC STAGNATION."""
    return _env_int("JARVIS_SWARM_STAGNATION_WINDOW", 2)


def _normalize_tokens(text: str) -> frozenset:
    """Lowercase token set. Pure; empty set on empty/garbage."""
    if not isinstance(text, str) or not text:
        return frozenset()
    return frozenset(_TOKEN_RE.findall(text.lower()))


def jaccard_similarity(a: str, b: str) -> float:
    """Token-overlap Jaccard ``|A intersect B| / |A union B|``. Pure stdlib.

    Two empty turns are treated as fully similar (1.0) -- repeating silence is a
    stagnation, not novelty. One-empty/one-nonempty -> 0.0.
    """
    sa = _normalize_tokens(a)
    sb = _normalize_tokens(b)
    if not sa and not sb:
        return 1.0
    union = sa | sb
    if not union:
        return 1.0
    return len(sa & sb) / len(union)


def _intent_hash(text: str) -> str:
    """Normalized-intent hash for exact-repeat detection (order-insensitive)."""
    toks = sorted(_normalize_tokens(text))
    return hashlib.sha256((" ".join(toks)).encode("utf-8")).hexdigest()


@dataclass
class _PairState:
    turns: List[str] = field(default_factory=list)
    intent_hashes: List[str] = field(default_factory=list)
    consecutive_stagnant: int = 0


class SemanticStagnationDetector:
    """Per-``correlation_id`` looping detection via Jaccard similarity.

    Feed each new turn's text via :meth:`observe`. Returns True the moment the
    pair has produced ``window`` consecutive turn-pairs whose similarity is at
    or above the threshold (or exact-repeat intent hashes) -- i.e. the exchange
    is looping. Fail-CLOSED: any internal error -> True (stagnant).
    """

    def __init__(
        self,
        *,
        threshold: Optional[float] = None,
        window: Optional[int] = None,
    ) -> None:
        self._threshold = threshold if threshold is not None else stagnation_threshold()
        self._window = window if window is not None else stagnation_window()
        self._pairs: Dict[str, _PairState] = defaultdict(_PairState)

    def observe(self, correlation_id: str, turn_text: str) -> bool:
        """Record a turn for ``correlation_id`` and return True iff the pair is
        now semantically stagnant.

        Fail-CLOSED: a None/garbage turn or any error -> treat as a stagnation
        signal so the breaker shatters the loop rather than letting it spin.
        """
        try:
            corr = str(correlation_id or "")
            state = self._pairs[corr]
            text = turn_text if isinstance(turn_text, str) else str(turn_text)

            if state.turns:
                prev = state.turns[-1]
                sim = jaccard_similarity(prev, text)
                exact_repeat = (
                    bool(state.intent_hashes)
                    and _intent_hash(text) == state.intent_hashes[-1]
                )
                if sim >= self._threshold or exact_repeat:
                    state.consecutive_stagnant += 1
                else:
                    state.consecutive_stagnant = 0

            state.turns.append(text)
            state.intent_hashes.append(_intent_hash(text))
            # Bound memory: keep only the last few turns (window + slack).
            cap = max(4, self._window + 2)
            if len(state.turns) > cap:
                state.turns = state.turns[-cap:]
                state.intent_hashes = state.intent_hashes[-cap:]

            stagnant = state.consecutive_stagnant >= self._window
            if stagnant:
                logger.warning(
                    "[StagnationDetector] corr=%s SEMANTIC STAGNATION "
                    "(consecutive=%d window=%d threshold=%.2f)",
                    corr,
                    state.consecutive_stagnant,
                    self._window,
                    self._threshold,
                )
            return stagnant
        except Exception:  # noqa: BLE001 -- fail-CLOSED -> break.
            logger.debug(
                "[StagnationDetector] observe raised -> treating as STAGNATION",
                exc_info=True,
            )
            return True

    def turn_count(self, correlation_id: str) -> int:
        """Number of turns observed for a correlation id (fail-soft -> 0)."""
        try:
            return len(self._pairs[str(correlation_id or "")].turns)
        except Exception:  # noqa: BLE001
            return 0

    def reset(self, correlation_id: str) -> None:
        """Forget a correlation id (e.g. after the breaker resolves it)."""
        self._pairs.pop(str(correlation_id or ""), None)
