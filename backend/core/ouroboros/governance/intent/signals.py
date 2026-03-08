"""
Intent Signal Data Model
========================

Foundational data types for JARVIS's Intent Engine (Layer 1 of autonomous
self-development).  Every detected anomaly -- test failure, stack trace,
git analysis -- is captured as an :class:`IntentSignal` and de-duplicated
via :class:`DedupTracker` before entering the governance pipeline.

Key design decisions:

* **Frozen dataclass** -- signals are immutable once created, ensuring
  thread-safety and auditability.
* **Deterministic dedup_key** -- SHA-256 hash of (repo + sorted files +
  evidence signature), so identical root causes collapse regardless of
  description wording, confidence, or source channel.
* **Monotonic cooldown** -- :class:`DedupTracker` uses ``time.monotonic()``
  to avoid wall-clock skew issues.
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Tuple

from backend.core.ouroboros.governance.operation_id import generate_operation_id


# ---------------------------------------------------------------------------
# IntentSignal
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IntentSignal:
    """Immutable record of a detected anomaly that may require autonomous action.

    Parameters
    ----------
    source:
        Channel that produced the signal.  One of ``"intent:test_failure"``,
        ``"intent:stack_trace"``, ``"intent:git_analysis"``, etc.
    target_files:
        Tuple of file paths implicated by the signal.
    repo:
        Repository origin (``"jarvis"``, ``"prime"``, ``"reactor-core"``).
    description:
        Human-readable summary of what was detected.
    evidence:
        Arbitrary evidence dict.  **Must** contain a ``"signature"`` key used
        for deduplication (e.g. ``"ValueError:module:42"``).
    confidence:
        Model or heuristic confidence in the signal, 0.0 -- 1.0.
    stable:
        ``True`` when the signal has met stability criteria (e.g. reproduced
        across multiple runs).
    signal_id:
        Auto-generated unique identifier via :func:`generate_operation_id`.
    timestamp:
        Auto-generated UTC creation time.
    """

    source: str
    target_files: Tuple[str, ...]
    repo: str
    description: str
    evidence: Dict[str, Any]
    confidence: float
    stable: bool

    # Auto-generated fields
    signal_id: str = field(default_factory=lambda: generate_operation_id("sig"))
    timestamp: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc),
    )

    # ------------------------------------------------------------------
    # Deduplication
    # ------------------------------------------------------------------

    @property
    def dedup_key(self) -> str:
        """Deterministic dedup key: SHA-256 of (repo + sorted files + signature).

        Truncated to 16 hex characters.  Two signals with the same repo,
        target files, and evidence signature will always produce the same key,
        regardless of description, confidence, source channel, or timestamps.
        """
        sorted_files = tuple(sorted(self.target_files))
        signature = self.evidence.get("signature", "")
        raw = f"{self.repo}|{'|'.join(sorted_files)}|{signature}"
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        return digest[:16]


# ---------------------------------------------------------------------------
# DedupTracker
# ---------------------------------------------------------------------------


class DedupTracker:
    """Tracks recently seen signal dedup keys to suppress duplicates.

    Uses ``time.monotonic()`` internally so cooldown is immune to
    wall-clock adjustments.

    Parameters
    ----------
    cooldown_s:
        Minimum seconds between accepting two signals with the same
        ``dedup_key``.  Default 300 s (5 minutes).
    """

    def __init__(self, cooldown_s: float = 300.0) -> None:
        self._cooldown_s = cooldown_s
        self._seen: Dict[str, float] = {}  # dedup_key -> monotonic timestamp

    def is_new(self, signal: IntentSignal) -> bool:
        """Return ``True`` if *signal* has not been seen within the cooldown.

        If ``True`` is returned the signal's dedup key is recorded with the
        current monotonic time (i.e. calling ``is_new`` both checks **and**
        registers the signal).
        """
        key = signal.dedup_key
        now = time.monotonic()
        last_seen = self._seen.get(key)

        if last_seen is not None and (now - last_seen) < self._cooldown_s:
            return False

        self._seen[key] = now
        return True

    def clear(self) -> None:
        """Reset all tracked dedup keys."""
        self._seen.clear()
