"""stagnation_detector -- Semantic Stagnation Detector (Phase 1c, G3).

The intelligent early break for the Epistemic Deadlock Breaker. Tracks the
clarification turns for a **worker PAIR** (one request/response exchange) and
computes a fast Jaccard / token-overlap similarity between consecutive turns.
When the pair starts repeating the same logic (high similarity for a window of
turns), it is looping -- signal the breaker to shatter EARLY, before the integer
``max_turn_budget`` is reached.

**Keyed by worker-PAIR, not correlation_id (red-team CRITICAL #2).** A caller
that mints a fresh ``correlation_id`` on every turn would otherwise reset the
similarity window each turn and loop forever. By bucketing on a stable
pair-key (``frozenset({worker_a, worker_b})``), cross-correlation turns between
the SAME pair feed the SAME stagnation bucket -- rotating the correlation_id can
no longer reset the count. The bucket key is supplied by the caller (the
breaker passes its pair-key); a bare correlation_id is accepted for backward
compatibility and used verbatim as the bucket key.

Pure stdlib (no heavy dep): normalize -> lowercase token set -> Jaccard
``|A intersect B| / |A union B|``. Optionally a normalized-intent hash for
exact-repeat detection.

Fail-CLOSED: an unparseable turn / detector error -> treat as a stagnation
signal (break), never as "keep talking".

**Gated under the swarm master (no standalone env flag needed).** Thresholds +
the per-pair bucket cap are env-tunable; the detector itself is a pure analyzer
with no side effects. The pair-bucket map is a bounded LRU (anti-OOM under a
unique-pair / unique-corr flood).
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Dict, FrozenSet, List, Optional, Union

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


def _pairs_capacity() -> int:
    """Bounded-LRU cap on the per-pair bucket map (anti-OOM)."""
    return _env_int("JARVIS_SWARM_STAGNATION_PAIRS_CAPACITY", 1024)


# A bucket key is either a stable worker-pair (preferred) or a bare
# correlation_id string (backward-compatible). Both hash + compare cleanly.
BucketKey = Union[str, "FrozenSet[str]"]


def pair_key(worker_a: str, worker_b: str) -> "FrozenSet[str]":
    """Stable, order-insensitive bucket key for a worker PAIR.

    ``frozenset({a, b})`` is identical regardless of which worker is "a" vs "b"
    and is invariant to correlation_id rotation -- the structural fix for the
    corr-rotation deadlock evasion (red-team CRITICAL #2).
    """
    return frozenset({str(worker_a or ""), str(worker_b or "")})


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
    """Per-**worker-pair** looping detection via Jaccard similarity.

    Feed each new turn's text via :meth:`observe`, keyed by a stable bucket key
    (a :func:`pair_key` frozenset, preferred) so rotating the correlation_id
    cannot reset the window. Returns True the moment the pair has produced
    ``window`` consecutive turn-pairs whose similarity is at or above the
    threshold (or exact-repeat intent hashes) -- i.e. the exchange is looping.
    Fail-CLOSED: any internal error -> True (stagnant).

    The bucket map is a bounded LRU (``_pairs_capacity``) -- a unique-pair /
    unique-corr flood evicts oldest, never OOMs.
    """

    def __init__(
        self,
        *,
        threshold: Optional[float] = None,
        window: Optional[int] = None,
    ) -> None:
        self._threshold = threshold if threshold is not None else stagnation_threshold()
        self._window = window if window is not None else stagnation_window()
        # Bounded LRU keyed by bucket key (pair frozenset or bare corr).
        self._pairs: "OrderedDict[BucketKey, _PairState]" = OrderedDict()
        self._pairs_capacity = _pairs_capacity()

    def _bucket(self, key: BucketKey) -> _PairState:
        """Fetch-or-create the bucket state for ``key`` with LRU discipline."""
        state = self._pairs.get(key)
        if state is None:
            state = _PairState()
            self._pairs[key] = state
        self._pairs.move_to_end(key)
        while len(self._pairs) > self._pairs_capacity:
            self._pairs.popitem(last=False)
        return state

    def observe(self, correlation_id: BucketKey, turn_text: str) -> bool:
        """Record a turn for a bucket key and return True iff the pair is now
        semantically stagnant.

        ``correlation_id`` is the bucket key: pass a :func:`pair_key` frozenset
        to bucket by worker-PAIR (corr-rotation-immune), or a bare string for
        backward-compatible per-id bucketing.

        Fail-CLOSED: a None/garbage turn or any error -> treat as a stagnation
        signal so the breaker shatters the loop rather than letting it spin.
        """
        try:
            key: BucketKey = correlation_id if isinstance(
                correlation_id, frozenset
            ) else str(correlation_id or "")
            state = self._bucket(key)
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
                    "[StagnationDetector] bucket=%s SEMANTIC STAGNATION "
                    "(consecutive=%d window=%d threshold=%.2f)",
                    key,
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

    def turn_count(self, correlation_id: BucketKey) -> int:
        """Number of turns observed for a bucket key (fail-soft -> 0)."""
        try:
            key: BucketKey = correlation_id if isinstance(
                correlation_id, frozenset
            ) else str(correlation_id or "")
            state = self._pairs.get(key)
            return len(state.turns) if state is not None else 0
        except Exception:  # noqa: BLE001
            return 0

    def reset(self, correlation_id: BucketKey) -> None:
        """Forget a bucket key (e.g. after the breaker resolves it)."""
        key: BucketKey = correlation_id if isinstance(
            correlation_id, frozenset
        ) else str(correlation_id or "")
        self._pairs.pop(key, None)
