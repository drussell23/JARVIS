"""Forward-progress detector for the GENERATE retry loop.

Motivation
----------
A common failure mode: the model produces a candidate that fails validation.
The retry loop re-invokes generation with failure feedback. The model makes
a cosmetic tweak but reproduces structurally the same (broken) content. The
loop burns its retry budget, potentially cascades to L2 repair, and still
makes no actual progress.

This module detects that loop by hashing the content of each candidate (the
authoritative identity) and failing fast when the same hash is observed
``max_repeats`` times consecutively for the same op.

Design principles
-----------------
1.  **Content-hash identity.** The candidate's ``full_content`` (or the
    concatenation of ``files[].full_content`` for multi-file candidates) is
    hashed with SHA-256. If the upstream provider already stamped a
    ``candidate_hash`` we trust that, otherwise we compute our own.

2.  **Consecutive-only.** A candidate that repeats twice in a row is stuck.
    A candidate that repeats once, then a different one, then the first
    again is *not* considered stuck — the model is exploring.

3.  **No hardcoding.** ``max_repeats`` and the kill switch come from env
    vars with safe defaults, matching the CostGovernor pattern.

4.  **Safe by default.** Missing content → hash=""  → ``observe`` is a
    no-op.  ``finish()`` is optional; entries pruned by TTL on the next
    ``observe`` call to prevent leaks.

5.  **Phase-aware abort.** Like ``CostGovernor``, the detector only flags
    the condition; the orchestrator uses ``_l2_escape_terminal`` to pick
    the right terminal phase.

Compliance
----------
* Manifesto §6 — Threshold-triggered neuroplasticity: stuck ops trip a
  threshold and escape the retry loop instead of hammering the provider.
* Manifesto §7 — Absolute observability: every repeat is logged at DEBUG,
  every trip at WARNING with the full history.
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional

logger = logging.getLogger(__name__)


# -----------------------------------------------------------------------------
# Env helper (duplicated from cost_governor so this module has zero internal
# deps — makes it trivially importable anywhere in the governance tree).
# -----------------------------------------------------------------------------

def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return default
    return val if val > 0 else default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return default
    return val if val > 0 else default


# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------

@dataclass(frozen=True)
class ForwardProgressConfig:
    """Immutable config for the detector."""

    max_repeats: int = field(
        default_factory=lambda: _env_int("JARVIS_FORWARD_PROGRESS_MAX_REPEATS", 2)
    )
    ttl_s: float = field(
        default_factory=lambda: _env_float("JARVIS_FORWARD_PROGRESS_TTL_S", 3600.0)
    )
    enabled: bool = field(
        default_factory=lambda: os.environ.get(
            "JARVIS_FORWARD_PROGRESS_ENABLED", "true"
        ).lower() == "true"
    )


# -----------------------------------------------------------------------------
# Per-op state
# -----------------------------------------------------------------------------

@dataclass
class _OpProgressEntry:
    op_id: str
    last_hash: str = ""
    repeat_count: int = 0
    total_observations: int = 0
    created_at: float = 0.0
    tripped: bool = False


# -----------------------------------------------------------------------------
# Hash helpers
# -----------------------------------------------------------------------------

def candidate_content_hash(candidate: Any) -> str:
    """Return a deterministic SHA-256 hash of a candidate's content.

    Handles:
      * Upstream-stamped ``candidate_hash`` (trust if non-empty).
      * Single-file ``full_content`` / ``raw_content``.
      * Multi-file ``files: [{file_path, full_content}, ...]``.
      * Arbitrary mapping-like objects with string content.

    Returns an empty string if no content is extractable — the caller must
    treat that as a no-op.
    """
    if candidate is None:
        return ""

    if isinstance(candidate, Mapping):
        # 1) Trust an upstream-stamped hash if present.
        upstream = candidate.get("candidate_hash", "") or ""
        if isinstance(upstream, str) and upstream:
            return upstream

        # 2) Multi-file shape: hash each (path, content) pair deterministically.
        files = candidate.get("files")
        if isinstance(files, list) and files:
            hasher = hashlib.sha256()
            for entry in files:
                if not isinstance(entry, Mapping):
                    continue
                path = str(entry.get("file_path", "") or "")
                content = str(entry.get("full_content", "") or "")
                hasher.update(path.encode("utf-8", errors="ignore"))
                hasher.update(b"\x00")
                hasher.update(content.encode("utf-8", errors="ignore"))
                hasher.update(b"\x00")
            return hasher.hexdigest()

        # 3) Single-file shape.
        content = (
            candidate.get("full_content", "")
            or candidate.get("raw_content", "")
            or ""
        )
        if isinstance(content, str) and content:
            return hashlib.sha256(content.encode("utf-8", errors="ignore")).hexdigest()

    # Duck-typed object with raw_content / full_content attributes.
    content = (
        getattr(candidate, "full_content", "")
        or getattr(candidate, "raw_content", "")
        or ""
    )
    if isinstance(content, str) and content:
        return hashlib.sha256(content.encode("utf-8", errors="ignore")).hexdigest()

    return ""


# -----------------------------------------------------------------------------
# Detector
# -----------------------------------------------------------------------------

class ForwardProgressDetector:
    """Tracks consecutive identical candidate hashes per op.

    Usage
    -----
        detector = ForwardProgressDetector()
        hash_ = candidate_content_hash(candidate)
        if detector.observe(op_id, hash_):
            # Stuck — abort with phase-aware terminal
            ...
        # ... normal flow ...
        detector.finish(op_id)  # optional; TTL-pruned otherwise
    """

    def __init__(self, config: Optional[ForwardProgressConfig] = None) -> None:
        self._config = config or ForwardProgressConfig()
        self._entries: Dict[str, _OpProgressEntry] = {}

    # --------------------------------------------------------------
    # Public API
    # --------------------------------------------------------------

    def observe(self, op_id: str, candidate_hash: str) -> bool:
        """Record a candidate hash for ``op_id``. Returns True if stuck.

        An empty hash is a no-op and does not reset state.
        A new non-empty hash that differs from the previous resets the
        repeat counter. A new hash that matches the previous increments
        it; when it reaches ``max_repeats`` the detector trips (returns
        True) and remains tripped until ``finish(op_id)``.
        """
        if not self._config.enabled:
            return False
        if not candidate_hash:
            return False

        self._prune_stale()

        entry = self._entries.get(op_id)
        if entry is None:
            entry = _OpProgressEntry(
                op_id=op_id,
                created_at=time.monotonic(),
            )
            self._entries[op_id] = entry

        entry.total_observations += 1

        if entry.tripped:
            # Already tripped — stay tripped until finish().
            return True

        if entry.last_hash == candidate_hash:
            entry.repeat_count += 1
            logger.debug(
                "[ForwardProgress] op=%s hash=%s repeat=%d/%d",
                op_id[:12], candidate_hash[:12],
                entry.repeat_count, self._config.max_repeats,
            )
        else:
            entry.last_hash = candidate_hash
            entry.repeat_count = 1
            logger.debug(
                "[ForwardProgress] op=%s new hash=%s",
                op_id[:12], candidate_hash[:12],
            )

        if entry.repeat_count >= self._config.max_repeats:
            entry.tripped = True
            logger.warning(
                "[ForwardProgress] op=%s STUCK: hash %s observed %d times "
                "in a row (limit %d)",
                op_id, candidate_hash[:12],
                entry.repeat_count, self._config.max_repeats,
            )
            return True
        return False

    def is_tripped(self, op_id: str) -> bool:
        """Return True if ``op_id`` has been marked stuck."""
        if not self._config.enabled:
            return False
        entry = self._entries.get(op_id)
        return bool(entry and entry.tripped)

    def finish(self, op_id: str) -> Optional[Mapping[str, Any]]:
        """Finalize and remove the op entry. Returns summary or None."""
        entry = self._entries.pop(op_id, None)
        if entry is None:
            return None
        return {
            "op_id": entry.op_id,
            "last_hash": entry.last_hash,
            "repeat_count": entry.repeat_count,
            "total_observations": entry.total_observations,
            "tripped": entry.tripped,
        }

    def summary(self, op_id: str) -> Optional[Mapping[str, Any]]:
        """Return current state for ``op_id`` without removal."""
        entry = self._entries.get(op_id)
        if entry is None:
            return None
        return {
            "op_id": entry.op_id,
            "last_hash": entry.last_hash,
            "repeat_count": entry.repeat_count,
            "total_observations": entry.total_observations,
            "tripped": entry.tripped,
        }

    # --------------------------------------------------------------
    # Internal
    # --------------------------------------------------------------

    def _prune_stale(self) -> int:
        if not self._entries:
            return 0
        now = time.monotonic()
        ttl = self._config.ttl_s
        stale = [
            op_id for op_id, entry in self._entries.items()
            if now - entry.created_at > ttl
        ]
        for op_id in stale:
            self._entries.pop(op_id, None)
        return len(stale)

    def active_op_count(self) -> int:
        return len(self._entries)
