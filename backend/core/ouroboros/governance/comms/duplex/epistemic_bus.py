"""Unified Epistemic Memory Bus — the split-brain cure.

Operator authorization 2026-07-19. Daniel and Karen previously held
persona-local visual caches (Contextual Fragmentation): Daniel sees an
error, the operator says "Karen, write a patch for that", and Karen's
prompt had no idea what "that" was — or worse, re-triggered Quartz.

This bus is the ONE centralized memory pointer at the orchestrator
root (mandate 1 — no disk serialization, no giant string relays
between persona prompts): the dhash footprint + the VLM semantic
state live HERE; every persona FSM holds a reference, never a copy.

Volatility is structural (mandate 2 — a stale gaze is a hallucination
vector):

  * **Temporal**: entries older than ``JARVIS_EPISTEMIC_VISUAL_TTL_S``
    (60s) are flushed at read time — an expired pointer is never
    served.
  * **Spatial**: a catastrophic dhash delta on the next capture
    (Space swap, IDE closed) flushes instantly via
    :meth:`spatial_invalidate`.

DRY (mandate 3): every deposit ALSO records a bounded turn into the
EXISTING ConversationBridge under ``### Visual context`` — the
ContextCompactor summarizes it into the LLM window exactly as it does
every other dialogue lane. One memory, every consumer.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger("Ouroboros.EpistemicBus")


def _visual_ttl_s() -> float:
    try:
        return max(5.0, min(600.0, float(os.environ.get(
            "JARVIS_EPISTEMIC_VISUAL_TTL_S", "60",
        ))))
    except (TypeError, ValueError):
        return 60.0


class EpistemicMemoryBus:
    """The shared short-term epistemic state. Thread-safe (personas
    live on different tasks); NEVER raises anywhere."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._visual: Optional[Dict[str, Any]] = None
        self.stats: Dict[str, int] = {
            "deposits": 0, "inherits": 0, "ttl_flushes": 0,
            "spatial_flushes": 0, "manual_flushes": 0,
        }

    # ---- the visual lane ----

    def deposit_visual(
        self,
        semantic_state: str,
        frame_hash: str,
        *,
        persona: str = "daniel",
    ) -> None:
        """One gaze result in — becomes THE pointer every persona
        inherits. Also bridges into ConversationBridge (DRY) so the
        compactor carries it into the LLM window. NEVER raises."""
        try:
            with self._lock:
                self._visual = {
                    "semantic_state": str(semantic_state or ""),
                    "frame_hash": str(frame_hash or ""),
                    "persona": str(persona),
                    "deposited_at": self._clock(),
                }
                self.stats["deposits"] += 1
            try:
                from backend.core.ouroboros.governance.conversation_bridge import (  # noqa: E501,PLC0415
                    record_visual_state,
                )
                record_visual_state(semantic_state, persona=persona)
            except Exception:  # noqa: BLE001
                pass
        except Exception:  # noqa: BLE001
            pass

    def inherit_visual(self) -> Optional[Dict[str, Any]]:
        """The cross-persona read: the LIVE pointer, or None. TTL is
        enforced AT READ — an expired state is flushed, never served
        (hallucination guard). NEVER raises."""
        try:
            with self._lock:
                v = self._visual
                if v is None:
                    return None
                if (self._clock() - v["deposited_at"]) > _visual_ttl_s():
                    self._visual = None
                    self.stats["ttl_flushes"] += 1
                    logger.info("[EpistemicBus] visual TTL expired — flushed")
                    return None
                self.stats["inherits"] += 1
                return dict(v)
        except Exception:  # noqa: BLE001
            return None

    def spatial_invalidate(self, new_frame_hash: str) -> bool:
        """Catastrophic-delta hook: the operator swapped Spaces /
        closed the IDE — the remembered screen no longer exists.
        True when a flush happened. NEVER raises."""
        try:
            with self._lock:
                v = self._visual
                if v is None:
                    return False
                if str(new_frame_hash) != v["frame_hash"]:
                    self._visual = None
                    self.stats["spatial_flushes"] += 1
                    logger.info(
                        "[EpistemicBus] catastrophic dhash delta — "
                        "visual state flushed",
                    )
                    return True
                return False
        except Exception:  # noqa: BLE001
            return False

    def flush(self) -> None:
        try:
            with self._lock:
                if self._visual is not None:
                    self.stats["manual_flushes"] += 1
                self._visual = None
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# Process-wide root pointer (the orchestrator-root singleton)
# ---------------------------------------------------------------------------

_DEFAULT: Optional[EpistemicMemoryBus] = None
_DEFAULT_LOCK = threading.Lock()


def get_default_bus() -> EpistemicMemoryBus:
    global _DEFAULT
    with _DEFAULT_LOCK:
        if _DEFAULT is None:
            _DEFAULT = EpistemicMemoryBus()
        return _DEFAULT


def reset_default_bus() -> None:
    global _DEFAULT
    with _DEFAULT_LOCK:
        _DEFAULT = None


__all__ = [
    "EpistemicMemoryBus",
    "get_default_bus",
    "reset_default_bus",
]
