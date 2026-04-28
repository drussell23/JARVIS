"""Phase 1 — Determinism Substrate (PRD §24.10 Critical Path #1).

Architectural foundation for replayable RSI. Without deterministic
entropy + clock, no decision can be replayed; bug reproduction is
best-effort; counterfactual analysis is impossible; Wang's RSI
convergence proof has no foundation.

Slice 1.1 ships the two foundational primitives only. Subsequent
slices wire these into the decision capture ledger (1.2), phase
runner replay hooks (1.3), and the replay harness (1.4-1.5).

Public surface:
  * SessionEntropy / DeterministicEntropy / entropy_for / entropy_enabled
  * RealClock / FrozenClock / clock_for_session / clock_enabled

Authority invariants:
  * NEVER imports orchestrator / phase_runner / candidate_generator —
    determinism is a substrate primitive, NOT a cognitive consumer.
  * NEVER raises out of any public method — defensive everywhere.
  * Pure stdlib (random, hashlib, secrets, os, time, asyncio, json,
    threading, tempfile). No third-party deps.
  * Atomic disk I/O reuses the temp+rename pattern from
    posture_store / dw_promotion_ledger / dw_ttft_observer.
  * All thresholds + defaults are env-tunable (no hardcoding).
"""
from __future__ import annotations

from backend.core.ouroboros.governance.determinism.clock import (
    FrozenClock,
    RealClock,
    clock_enabled,
    clock_for_session,
)
from backend.core.ouroboros.governance.determinism.entropy import (
    DeterministicEntropy,
    SessionEntropy,
    entropy_enabled,
    entropy_for,
)

__all__ = [
    "DeterministicEntropy",
    "FrozenClock",
    "RealClock",
    "SessionEntropy",
    "clock_enabled",
    "clock_for_session",
    "entropy_enabled",
    "entropy_for",
]
