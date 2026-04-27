"""Phase 8.2 — Latent-confidence ring buffer.

Per `OUROBOROS_VENOM_PRD.md` §3.6.4:

  > Bounded ledger of every classifier confidence + threshold +
  > outcome.

This module ships an in-memory ring buffer (NOT JSONL — confidence
events are noisy + ephemeral; we don't want them on disk forever).
Operators query via `recent(N)` or `recent_for_classifier(name, N)`
to inspect "is the model getting LESS confident over a session?"

## Why a ring buffer (not JSONL)

Confidence events fire on every classifier invocation: intent
classification, urgency routing, semantic-firewall verdict, plan
approval, etc. At ~10 events/op × 100 ops/day → 1000 rows/day.
JSONL persistence would dominate disk; ring buffer keeps the most-
recent N in-memory + drops oldest.

## Event shape

```python
ConfidenceEvent(
    classifier_name="intent_classifier",
    confidence=0.87,
    threshold=0.5,
    outcome="CONVERSATIONAL",
    extra={"prompt_chars": 240},
    ts_epoch=1714128000.0,
)
```

Outcome can be the chosen branch, the action taken, or a verdict
string — operator-defined.

## Default-off

`JARVIS_LATENT_CONFIDENCE_RING_ENABLED` (default false). When off,
``record()`` is a no-op; ``recent()`` returns empty.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


_TRUTHY = ("1", "true", "yes", "on")


# Ring buffer capacity. 4096 events × ~200 bytes/event = ~800 KiB
# memory at full capacity. Sufficient for ~6 hours of typical
# session traffic.
DEFAULT_RING_CAPACITY: int = 4096

# Per-event size caps.
MAX_CLASSIFIER_NAME_CHARS: int = 120
MAX_OUTCOME_CHARS: int = 240
MAX_EXTRA_KEYS: int = 16


def is_ring_enabled() -> bool:
    """Master flag — ``JARVIS_LATENT_CONFIDENCE_RING_ENABLED``
    (default false)."""
    return os.environ.get(
        "JARVIS_LATENT_CONFIDENCE_RING_ENABLED", "",
    ).strip().lower() in _TRUTHY


def get_ring_capacity() -> int:
    raw = os.environ.get("JARVIS_LATENT_CONFIDENCE_RING_CAPACITY")
    if raw is None:
        return DEFAULT_RING_CAPACITY
    try:
        v = int(raw)
        return v if v >= 1 else DEFAULT_RING_CAPACITY
    except ValueError:
        return DEFAULT_RING_CAPACITY


@dataclass(frozen=True)
class ConfidenceEvent:
    """One classifier-invocation observation."""

    classifier_name: str
    confidence: float
    threshold: float
    outcome: str
    extra: Dict[str, Any]
    ts_epoch: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "classifier_name": self.classifier_name,
            "confidence": self.confidence,
            "threshold": self.threshold,
            "outcome": self.outcome,
            "extra": dict(self.extra),
            "ts_epoch": self.ts_epoch,
        }

    @property
    def below_threshold(self) -> bool:
        """True iff confidence is below threshold (sub-confident)."""
        return self.confidence < self.threshold


def _truncate_dict(
    d: Optional[Dict[str, Any]], max_keys: int,
) -> Dict[str, Any]:
    if not isinstance(d, dict):
        return {}
    if len(d) <= max_keys:
        return dict(d)
    keys = list(d.keys())[:max_keys]
    return {k: d[k] for k in keys}


class LatentConfidenceRing:
    """Bounded in-memory ring buffer of ConfidenceEvent rows.

    Drop-oldest semantics: when the ring is full, the oldest event
    is evicted. Thread-safe (RLock around the deque).
    """

    def __init__(self, capacity: Optional[int] = None) -> None:
        self._capacity = (
            capacity if capacity is not None else get_ring_capacity()
        )
        self._buf: Deque[ConfidenceEvent] = deque(maxlen=self._capacity)
        self._lock = threading.RLock()

    @property
    def capacity(self) -> int:
        return self._capacity

    def __len__(self) -> int:
        with self._lock:
            return len(self._buf)

    def record(
        self,
        *,
        classifier_name: str,
        confidence: float,
        threshold: float,
        outcome: str,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Tuple[bool, str]:
        """Record one observation. NEVER raises."""
        if not is_ring_enabled():
            return (False, "master_off")
        cn = (classifier_name or "").strip()[:MAX_CLASSIFIER_NAME_CHARS]
        if not cn:
            return (False, "empty_classifier_name")
        try:
            conf = float(confidence)
            thr = float(threshold)
        except (TypeError, ValueError):
            return (False, "non_numeric_confidence_or_threshold")
        out = (outcome or "")[:MAX_OUTCOME_CHARS]
        event = ConfidenceEvent(
            classifier_name=cn,
            confidence=conf,
            threshold=thr,
            outcome=out,
            extra=_truncate_dict(extra, MAX_EXTRA_KEYS),
            ts_epoch=time.time(),
        )
        with self._lock:
            self._buf.append(event)
        return (True, "ok")

    def recent(self, n: int = 100) -> List[ConfidenceEvent]:
        """Return up to N most-recent events (newest last)."""
        if not is_ring_enabled():
            return []
        with self._lock:
            if n >= len(self._buf):
                return list(self._buf)
            return list(self._buf)[-n:]

    def recent_for_classifier(
        self, classifier_name: str, n: int = 100,
    ) -> List[ConfidenceEvent]:
        """Return up to N most-recent events for one classifier."""
        if not is_ring_enabled():
            return []
        with self._lock:
            matching = [
                e for e in self._buf
                if e.classifier_name == classifier_name
            ]
        if n >= len(matching):
            return matching
        return matching[-n:]

    def confidence_drop_indicators(
        self,
        classifier_name: str,
        window: int = 20,
        drop_threshold_pct: float = 20.0,
    ) -> Dict[str, Any]:
        """Detect a confidence drop: compare mean confidence in the
        most-recent window to the prior window of equal size.

        Returns ``{"window_size", "recent_mean", "prior_mean",
        "drop_pct", "drop_detected"}``. ``drop_detected`` is True
        iff `recent_mean` < `prior_mean × (1 - drop_threshold_pct/100)`.

        Used by Phase 8 SerpentFlow to emit `latent_confidence_dropped`
        SSE events when significant degradation is detected.
        """
        if not is_ring_enabled():
            return {
                "window_size": 0, "recent_mean": 0.0,
                "prior_mean": 0.0, "drop_pct": 0.0,
                "drop_detected": False, "reason": "master_off",
            }
        with self._lock:
            matching = [
                e.confidence for e in self._buf
                if e.classifier_name == classifier_name
            ]
        if len(matching) < 2 * window:
            return {
                "window_size": window, "recent_mean": 0.0,
                "prior_mean": 0.0, "drop_pct": 0.0,
                "drop_detected": False,
                "reason": f"insufficient_data:{len(matching)}<{2*window}",
            }
        recent = matching[-window:]
        prior = matching[-2*window:-window]
        recent_mean = sum(recent) / window
        prior_mean = sum(prior) / window
        if prior_mean == 0:
            drop_pct = 0.0
            detected = False
        else:
            drop_pct = (prior_mean - recent_mean) / prior_mean * 100.0
            detected = drop_pct >= drop_threshold_pct
        return {
            "window_size": window,
            "recent_mean": recent_mean,
            "prior_mean": prior_mean,
            "drop_pct": drop_pct,
            "drop_detected": detected,
            "reason": "ok",
        }


_DEFAULT_RING: Optional[LatentConfidenceRing] = None


def get_default_ring() -> LatentConfidenceRing:
    global _DEFAULT_RING
    if _DEFAULT_RING is None:
        _DEFAULT_RING = LatentConfidenceRing()
    return _DEFAULT_RING


def reset_default_ring() -> None:
    global _DEFAULT_RING
    _DEFAULT_RING = None


__all__ = [
    "ConfidenceEvent",
    "DEFAULT_RING_CAPACITY",
    "LatentConfidenceRing",
    "MAX_CLASSIFIER_NAME_CHARS",
    "MAX_EXTRA_KEYS",
    "MAX_OUTCOME_CHARS",
    "get_default_ring",
    "get_ring_capacity",
    "is_ring_enabled",
    "reset_default_ring",
]
