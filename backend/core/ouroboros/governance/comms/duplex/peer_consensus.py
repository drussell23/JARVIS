"""Peer Consensus RPC — Daniel and Karen converse as reasoning agents.

The second closing gap (operator authorization 2026-07-19). Until now
the personas shared memory and split voices but never TALKED. This is
the inter-agent handshake: JARVIS (Daniel) yields a hard coding query
to O+V (Karen), awaits her diagnostic, and answers the user with it —
over the EXISTING TrinityEventBus transport + the Claude/DW provider
connections.

**Depth-Bounded Consensus Lock (mandate 2 — the anti-loop guard):** a
single user interaction may trigger AT MOST ONE round-trip. The lock
is armed on the first yield; a SECOND yield within the same
interaction is INTERCEPTED — no provider call, no token burn — and
Daniel gracefully falls back to reporting the ambiguity to the
operator. This makes an infinite Daniel↔Karen token-burn loop
structurally impossible, not merely discouraged.

``karen_diagnose`` is the injected reasoning seam (production: Karen's
answer engine → rt_gate → Claude/DW). Every path is bounded + fail-
soft; NEVER raises.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Awaitable, Callable, Dict, Optional

logger = logging.getLogger("Ouroboros.PeerConsensus")

OUTCOME_RESOLVED = "resolved"
OUTCOME_LOCK_INTERCEPTED = "lock_intercepted_fallback"
OUTCOME_TIMEOUT = "timeout_fallback"
OUTCOME_ERROR = "error_fallback"

_FALLBACK_MESSAGE = (
    "I looked into that with the engineering side but couldn't fully "
    "resolve it in one pass — here's what I have, and I'd flag the "
    "ambiguity rather than guess further."
)


def _consensus_timeout_s() -> float:
    try:
        return max(2.0, min(120.0, float(os.environ.get(
            "JARVIS_CONSENSUS_TIMEOUT_S", "30",
        )))
        )
    except (TypeError, ValueError):
        return 30.0


class ConsensusSession:
    """One user interaction's consensus budget. The lock is per-
    session so a NEW user interaction gets a fresh single round-trip
    — the guard bounds a loop WITHIN one interaction, not across a
    conversation."""

    def __init__(self) -> None:
        self._used = False

    @property
    def spent(self) -> bool:
        return self._used

    def try_claim(self) -> bool:
        """Claim the single round-trip. Returns True the FIRST time,
        False forever after (the second-yield interception point)."""
        if self._used:
            return False
        self._used = True
        return True


class PeerConsensus:
    """The inter-agent RPC. ``karen_diagnose(query, context) -> str``
    is Karen's reasoning seam (routes to Claude/DW in production)."""

    def __init__(
        self,
        *,
        karen_diagnose: Optional[Callable[..., Awaitable[str]]] = None,
    ) -> None:
        self._diagnose = karen_diagnose or self._default_diagnose
        self.stats: Dict[str, int] = {
            "yields": 0, "resolved": 0, "lock_intercepted": 0,
            "timeouts": 0, "errors": 0,
        }

    @staticmethod
    async def _default_diagnose(query: str, context: Any) -> str:
        # Production: Karen's answer engine → rt_gate → Claude/DW.
        from backend.core.ouroboros.governance.karen_answer_engine import (  # noqa: E501,PLC0415
            KarenQueryProvider,
        )
        return await KarenQueryProvider().query(query)

    def new_session(self) -> ConsensusSession:
        """A fresh single-round-trip budget for one user interaction."""
        return ConsensusSession()

    async def daniel_yields_to_karen(
        self,
        session: ConsensusSession,
        query: str,
        *,
        context: Any = None,
    ) -> Dict[str, Any]:
        """Daniel yields ``query`` to Karen and awaits her diagnostic.

        Depth-Bounded Lock: the FIRST yield in this session runs; a
        SECOND is intercepted (no provider call) → graceful fallback.
        Returns ``{"outcome", "payload"}``. NEVER raises."""
        try:
            self.stats["yields"] += 1
            if not session.try_claim():
                # SECOND yield in the same interaction — the anti-loop
                # guard fires BEFORE any API call.
                self.stats["lock_intercepted"] += 1
                logger.warning(
                    "[PeerConsensus] depth-bound lock intercepted a "
                    "second yield — no provider call; graceful fallback",
                )
                return {
                    "outcome": OUTCOME_LOCK_INTERCEPTED,
                    "payload": _FALLBACK_MESSAGE,
                }
            try:
                payload = await asyncio.wait_for(
                    self._diagnose(query, context),
                    timeout=_consensus_timeout_s(),
                )
            except asyncio.TimeoutError:
                self.stats["timeouts"] += 1
                return {"outcome": OUTCOME_TIMEOUT, "payload": _FALLBACK_MESSAGE}
            except Exception:  # noqa: BLE001
                self.stats["errors"] += 1
                return {"outcome": OUTCOME_ERROR, "payload": _FALLBACK_MESSAGE}
            self.stats["resolved"] += 1
            return {"outcome": OUTCOME_RESOLVED, "payload": str(payload or "")}
        except Exception:  # noqa: BLE001
            self.stats["errors"] += 1
            return {"outcome": OUTCOME_ERROR, "payload": _FALLBACK_MESSAGE}


def peer_consensus_enabled() -> bool:
    """Master gate — default OFF (§33.1: inter-agent LLM dialogue that
    spends tokens graduates). NEVER raises."""
    return os.environ.get(
        "JARVIS_PEER_CONSENSUS_ENABLED", "",
    ).strip().lower() in ("1", "true", "yes", "on")


def should_yield_to_karen(command: str) -> bool:
    """Daniel's yield decision: a query whose semantic class is
    engineering/codebase AND carries technical depth is Karen's to
    diagnose. Reuses the EXISTING persona classifier (DRY) — no new
    routing table. NEVER raises."""
    try:
        from .ambient import classify_persona, PERSONA_KAREN  # noqa: PLC0415
        low = str(command or "").lower()
        # Depth markers — a shallow "what's the weather" never yields.
        technical = any(w in low for w in (
            "code", "bug", "error", "stack", "trace", "deadlock",
            "refactor", "test", "exception", "why does", "debug",
            "regression", "fix the", "function", "class", "import",
        ))
        # Persona routing: the command's semantic class → Karen's plane.
        engineering = classify_persona(
            "engineering" if technical else "system",
        ) == PERSONA_KAREN
        return bool(technical and engineering)
    except Exception:  # noqa: BLE001
        return False


def build_live_peer_consensus(
    *,
    karen_diagnose: Optional[Callable[..., Awaitable[str]]] = None,
) -> Optional["PeerConsensus"]:
    """Mount a PeerConsensus for a live surface, or ``None`` when the
    master gate is down. NEVER raises."""
    try:
        if not peer_consensus_enabled():
            return None
        return PeerConsensus(karen_diagnose=karen_diagnose)
    except Exception:  # noqa: BLE001
        return None


__all__ = [
    "OUTCOME_ERROR",
    "OUTCOME_LOCK_INTERCEPTED",
    "OUTCOME_RESOLVED",
    "OUTCOME_TIMEOUT",
    "ConsensusSession",
    "PeerConsensus",
    "build_live_peer_consensus",
    "peer_consensus_enabled",
    "should_yield_to_karen",
]
