"""Proactive Proposal Queue — cross-space insights, flow-protected.

Operator authorization 2026-07-19. The final proactive layer:
CrossSpaceGaze insights become PROPOSALS, held until the operator is
idle, evicted when stale, and NEVER executed without an explicit
``[Y/n]`` (mandate 2 — proposals are suggestions, not mutations).

  * **Active Flow Protection (mandate 1+2):** native macOS idle via
    ``CGEventSourceSecondsSinceLastEventType`` — NO polling. A
    proposal renders only when the operator has been idle ≥
    ``JARVIS_PROPOSAL_IDLE_GATE_S`` (15s); keyboard/mouse within the
    window HOLDS it in memory (flow is sacred).
  * **Eviction (mandate 2):** a proposal older than
    ``JARVIS_PROPOSAL_TTL_S`` (5 min) OR invalidated by a
    catastrophic desktop dhash delta (window closed / fixed) is
    silently flushed — a stale suggestion is worse than none.
  * **Strict execution gating (mandate 2):** ``present_if_idle``
    returns the proposal for the caller to route through the EXISTING
    SerpentFlow ``[Y/n]`` Iron Gate (mandate 3 — no separate alert
    system); this module NEVER touches the filesystem.

Everything injectable; NEVER raises on any path.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("Ouroboros.ProposalQueue")


def _idle_gate_s() -> float:
    try:
        return max(2.0, min(300.0, float(os.environ.get(
            "JARVIS_PROPOSAL_IDLE_GATE_S", "15",
        ))))
    except (TypeError, ValueError):
        return 15.0


def _proposal_ttl_s() -> float:
    try:
        return max(30.0, min(3600.0, float(os.environ.get(
            "JARVIS_PROPOSAL_TTL_S", "300",     # 5 min
        ))))
    except (TypeError, ValueError):
        return 300.0


def native_idle_seconds() -> float:
    """macOS global input idle via CGEventSourceSecondsSinceLastEvent-
    Type (mandate 1 — native, zero polling). Returns 0.0 (assume
    ACTIVE — fail-safe toward NOT interrupting) when unreadable."""
    try:
        import Quartz  # noqa: PLC0415
        # kCGEventSourceStateHIDSystemState = 1;
        # kCGAnyInputEventType = 0xFFFFFFFF (all input types).
        return float(Quartz.CGEventSourceSecondsSinceLastEventType(
            1, 0xFFFFFFFF,
        ))
    except Exception:  # noqa: BLE001
        return 0.0


class Proposal:
    __slots__ = ("insight", "spaces", "dhash", "created_at", "id")

    def __init__(self, insight: Any, spaces: List[int], dhash: str,
                 created_at: float, pid: str) -> None:
        self.insight = insight
        self.spaces = spaces
        self.dhash = dhash
        self.created_at = created_at
        self.id = pid

    def summary(self) -> str:
        try:
            if isinstance(self.insight, dict):
                return str(self.insight.get("description", "cross-space insight"))
            return str(getattr(self.insight, "description", "cross-space insight"))
        except Exception:  # noqa: BLE001
            return "cross-space insight"


class ProposalQueue:
    """Holds flow-protected proposals. ``idle_source`` and ``clock``
    injected for tests; production defaults are native. NEVER raises."""

    def __init__(
        self,
        *,
        idle_source: Optional[Callable[[], float]] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._idle = idle_source or native_idle_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._q: List[Proposal] = []
        self._seq = 0
        self.stats: Dict[str, int] = {
            "submitted": 0, "held_flow": 0, "presented": 0,
            "ttl_evicted": 0, "spatial_evicted": 0, "deduped": 0,
            "slot_evicted": 0,
        }

    def submit(self, insight: Any, spaces: List[int], dhash: str) -> bool:
        """Enqueue a proposal. Semantic dedup on summary+spaces.

        Amortized Presentation Slot (mandate 2 — the Alert Avalanche
        Guard): at most ONE proposal is ever held. A FRESHER insight
        arriving while the slot is occupied SILENTLY EVICTS the older
        one and takes its place — a returning operator faces exactly
        one current prompt, never a stacked backlog of stale [Y/n]s.
        NEVER raises."""
        try:
            summary = self._summ(insight)
            with self._lock:
                for p in self._q:
                    if p.summary() == summary and p.spaces == spaces:
                        self.stats["deduped"] += 1
                        return False
                # Single-slot amortization: newest wins.
                if self._q:
                    self._q.clear()
                    self.stats["slot_evicted"] += 1
                self._seq += 1
                self._q.append(Proposal(
                    insight, list(spaces), str(dhash),
                    self._clock(), f"prop-{self._seq}",
                ))
                self.stats["submitted"] += 1
            return True
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _summ(insight: Any) -> str:
        try:
            if isinstance(insight, dict):
                return str(insight.get("description", ""))
            return str(getattr(insight, "description", ""))
        except Exception:  # noqa: BLE001
            return ""

    def evict_stale(self, *, current_dhash: Optional[str] = None) -> int:
        """TTL + spatial eviction. ``current_dhash`` (the latest
        desktop topology) flushes any proposal whose remembered dhash
        no longer matches (window closed / fixed). Returns count
        evicted. NEVER raises."""
        try:
            now = self._clock()
            ttl = _proposal_ttl_s()
            with self._lock:
                keep: List[Proposal] = []
                for p in self._q:
                    if (now - p.created_at) > ttl:
                        self.stats["ttl_evicted"] += 1
                        continue
                    if current_dhash is not None and p.dhash != current_dhash:
                        self.stats["spatial_evicted"] += 1
                        continue
                    keep.append(p)
                evicted = len(self._q) - len(keep)
                self._q = keep
            return evicted
        except Exception:  # noqa: BLE001
            return 0

    def present_if_idle(self) -> Optional[Proposal]:
        """The Active Flow gate: return the OLDEST live proposal ONLY
        when the operator is idle ≥ the gate; otherwise hold (flow is
        sacred). The caller routes the returned proposal through the
        EXISTING [Y/n] Iron Gate — this method never mutates anything.
        NEVER raises."""
        try:
            with self._lock:
                if not self._q:
                    return None
            idle = self._idle()
            if idle < _idle_gate_s():
                self.stats["held_flow"] += 1
                return None                     # operator is in flow
            with self._lock:
                if not self._q:
                    return None
                p = self._q.pop(0)
            self.stats["presented"] += 1
            return p
        except Exception:  # noqa: BLE001
            return None

    @property
    def depth(self) -> int:
        with self._lock:
            return len(self._q)


__all__ = ["Proposal", "ProposalQueue", "native_idle_seconds"]
