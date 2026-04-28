"""Slice 3.2 — ConvergenceGovernor: formal halting layer.

Per ``OUROBOROS_VENOM_PRD.md`` §24.10.3 (Priority 3):

  > Formal termination is the difference between "could improve
  > forever" (good) and "will hang forever on the wrong input"
  > (catastrophic).

The ConvergenceGovernor sits between ``CuriosityScheduler`` and
``CuriosityEngine``. It tracks per-hypothesis ``BeliefState`` across
cycles and enforces four halting conditions — at least one MUST fire
for every hypothesis.

## Halting conditions (all mathematically derived)

  1. **Convergence**: H(posterior) < ε where ε derives from prior
  2. **Budget**: cost_spent ≥ budget_per_hypothesis
  3. **Max probes**: observations ≥ O(log₂(1/ε))
  4. **Diminishing returns**: |entropy_delta| < threshold for N
     consecutive observations

## Integration contract

  CuriosityScheduler.tick()
    → ConvergenceGovernor.should_explore(hypothesis_id)
    → CuriosityEngine.run_cycle(filtered)
      → HypothesisProbe.test()
      → ConvergenceGovernor.record_observation(hypothesis_id, verdict, cost)
        → Bayesian update → ConvergenceProof if halted

## Cooling schedule

  ``global_cooling_factor()`` returns the mean cooling factor across
  all active (non-converged) hypotheses. CuriosityScheduler reads
  this to modulate its fire rate. As hypotheses converge →
  cooling_factor → 0 → scheduler self-quiets.

## Cage rules (load-bearing)

  * Imports only: ``exploration_calculus``, ``_file_lock``.
  * **NEVER raises** into the caller.
  * **Master flag**: ``JARVIS_CONVERGENCE_GOVERNOR_ENABLED``
    (default false).
  * JSONL persistence at ``.jarvis/convergence_state.jsonl``.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from backend.core.ouroboros.governance.adaptation.exploration_calculus import (
    BeliefState,
    ConvergenceProof,
    STATE_CONVERGED,
    cooling_factor,
    epsilon_from_prior,
    initial_belief,
    make_convergence_proof,
    max_probes_for_epsilon,
    parse_belief_state,
    update_belief,
)

logger = logging.getLogger(__name__)

_TRUTHY = ("1", "true", "yes", "on")

# ---------------------------------------------------------------------------
# Hard caps
# ---------------------------------------------------------------------------

MAX_TRACKED_HYPOTHESES: int = 200
MAX_STATE_FILE_BYTES: int = 4 * 1024 * 1024
MAX_PROOFS_RETAINED: int = 500

# ---------------------------------------------------------------------------
# Master flag + configuration
# ---------------------------------------------------------------------------


def is_governor_enabled() -> bool:
    """Master flag — ``JARVIS_CONVERGENCE_GOVERNOR_ENABLED``."""
    return os.environ.get(
        "JARVIS_CONVERGENCE_GOVERNOR_ENABLED", "",
    ).strip().lower() in _TRUTHY


def _state_path() -> Path:
    raw = os.environ.get("JARVIS_CONVERGENCE_GOVERNOR_PATH")
    if raw:
        return Path(raw)
    return Path(".jarvis") / "convergence_state.jsonl"


def _proofs_path() -> Path:
    raw = os.environ.get("JARVIS_CONVERGENCE_PROOFS_PATH")
    if raw:
        return Path(raw)
    return Path(".jarvis") / "convergence_proofs.jsonl"


# ---------------------------------------------------------------------------
# ConvergenceGovernor
# ---------------------------------------------------------------------------


class ConvergenceGovernor:
    """Per-hypothesis convergence tracker with formal halting proofs.

    Maintains a ``BeliefState`` for each tracked hypothesis. Exposes
    advisory signals (``should_explore``, ``global_cooling_factor``)
    that CuriosityScheduler consults. Emits ``ConvergenceProof``
    records when hypotheses halt.

    Thread-safety: not thread-safe. Designed for tick-driven
    orchestrator loop (single-threaded heartbeat), consistent with
    Phase 2 modules.
    """

    def __init__(
        self,
        state_path: Optional[Path] = None,
        proofs_path: Optional[Path] = None,
    ) -> None:
        self._state_path = state_path or _state_path()
        self._proofs_path = proofs_path or _proofs_path()
        self._beliefs: Dict[str, BeliefState] = {}
        self._proofs: List[ConvergenceProof] = []
        self._loaded = False

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        self._load_beliefs()
        self._load_proofs()

    def _load_beliefs(self) -> None:
        if not self._state_path.exists():
            return
        try:
            size = self._state_path.stat().st_size
            if size > MAX_STATE_FILE_BYTES:
                logger.warning(
                    "[ConvergenceGovernor] state file too large (%d bytes) "
                    "— starting empty", size,
                )
                return
            text = self._state_path.read_text(encoding="utf-8")
        except OSError:
            return
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            bs = parse_belief_state(obj)
            if bs and bs.hypothesis_id:
                self._beliefs[bs.hypothesis_id] = bs
                if len(self._beliefs) >= MAX_TRACKED_HYPOTHESES:
                    break

    def _load_proofs(self) -> None:
        if not self._proofs_path.exists():
            return
        try:
            text = self._proofs_path.read_text(encoding="utf-8")
        except OSError:
            return
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            try:
                proof = ConvergenceProof(
                    hypothesis_id=str(obj.get("hypothesis_id", "")),
                    halted=bool(obj.get("halted", False)),
                    halt_reason=str(obj.get("halt_reason", "")),
                    probes_used=int(obj.get("probes_used", 0)),
                    theoretical_max_probes=int(
                        obj.get("theoretical_max_probes", 0)),
                    cost_spent=float(obj.get("cost_spent", 0.0)),
                    final_belief=float(obj.get("final_belief", 0.0)),
                    final_entropy=float(obj.get("final_entropy", 0.0)),
                    epsilon=float(obj.get("epsilon", 0.0)),
                    ts_unix=float(obj.get("ts_unix", 0.0)),
                )
                self._proofs.append(proof)
            except (TypeError, ValueError):
                continue
            if len(self._proofs) >= MAX_PROOFS_RETAINED:
                break

    def _persist_beliefs(self) -> Tuple[bool, str]:
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            with self._state_path.open("w", encoding="utf-8") as f:
                for bs in self._beliefs.values():
                    line = json.dumps(bs.to_dict(), separators=(",", ":"))
                    f.write(line + "\n")
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass
        except OSError as exc:
            return (False, f"persist_beliefs_failed:{exc}")
        return (True, "ok")

    def _persist_proof(self, proof: ConvergenceProof) -> Tuple[bool, str]:
        try:
            self._proofs_path.parent.mkdir(parents=True, exist_ok=True)
            with self._proofs_path.open("a", encoding="utf-8") as f:
                line = json.dumps(proof.to_dict(), separators=(",", ":"))
                f.write(line + "\n")
                f.flush()
        except OSError as exc:
            return (False, f"persist_proof_failed:{exc}")
        return (True, "ok")

    # ------------------------------------------------------------------
    # Core API
    # ------------------------------------------------------------------

    def track_hypothesis(
        self,
        hypothesis_id: str,
        prior: float = 0.5,
        now_unix: Optional[float] = None,
    ) -> BeliefState:
        """Begin tracking a new hypothesis. NEVER raises."""
        self._ensure_loaded()
        ts = now_unix or time.time()
        if hypothesis_id in self._beliefs:
            return self._beliefs[hypothesis_id]
        if len(self._beliefs) >= MAX_TRACKED_HYPOTHESES:
            # Evict the oldest converged hypothesis.
            self._evict_oldest_converged()
        bs = initial_belief(hypothesis_id, prior=prior, now_unix=ts)
        self._beliefs[hypothesis_id] = bs
        self._persist_beliefs()
        return bs

    def _evict_oldest_converged(self) -> None:
        """Evict the oldest converged hypothesis to make room."""
        converged = [
            (hid, bs) for hid, bs in self._beliefs.items()
            if bs.convergence_state == STATE_CONVERGED
        ]
        if converged:
            converged.sort(key=lambda x: x[1].ts_unix)
            del self._beliefs[converged[0][0]]
        elif self._beliefs:
            # No converged hypotheses — evict oldest by timestamp.
            oldest = min(self._beliefs.items(), key=lambda x: x[1].ts_unix)
            del self._beliefs[oldest[0]]

    def should_explore(self, hypothesis_id: str) -> bool:
        """True iff this hypothesis should receive another probe.

        Returns False if:
          - hypothesis not tracked
          - any halt condition is met
          - governor is disabled

        NEVER raises.
        """
        if not is_governor_enabled():
            return False
        self._ensure_loaded()
        bs = self._beliefs.get(hypothesis_id)
        if bs is None:
            return False
        eps = epsilon_from_prior(bs.prior)
        return not bs.is_halted(eps)

    def record_observation(
        self,
        hypothesis_id: str,
        verdict: str,
        cost_usd: float = 0.0,
        now_unix: Optional[float] = None,
    ) -> Tuple[BeliefState, Optional[ConvergenceProof]]:
        """Record a probe observation, update belief, check halting.

        Returns (updated BeliefState, ConvergenceProof if halted).
        NEVER raises.
        """
        self._ensure_loaded()
        ts = now_unix or time.time()
        bs = self._beliefs.get(hypothesis_id)
        if bs is None:
            # Auto-track with default prior.
            bs = initial_belief(hypothesis_id, now_unix=ts)

        new_bs = update_belief(
            bs, verdict=verdict, cost_usd=cost_usd, now_unix=ts,
        )
        self._beliefs[hypothesis_id] = new_bs

        eps = epsilon_from_prior(new_bs.prior)
        proof: Optional[ConvergenceProof] = None
        if new_bs.is_halted(eps):
            proof = make_convergence_proof(new_bs, now_unix=ts)
            self._proofs.append(proof)
            if len(self._proofs) > MAX_PROOFS_RETAINED:
                self._proofs = self._proofs[-MAX_PROOFS_RETAINED:]
            self._persist_proof(proof)

        self._persist_beliefs()
        return (new_bs, proof)

    # ------------------------------------------------------------------
    # Cooling schedule
    # ------------------------------------------------------------------

    def global_cooling_factor(self) -> float:
        """Mean cooling factor across all active (non-converged)
        hypotheses.

        Returns 1.0 if no hypotheses are tracked (full curiosity).
        Returns 0.0 if all hypotheses are converged (no curiosity).
        Returns the mean entropy-based cooling factor otherwise.

        Used by CuriosityScheduler to modulate fire rate.
        NEVER raises.
        """
        self._ensure_loaded()
        if not self._beliefs:
            return 1.0
        active = [
            bs for bs in self._beliefs.values()
            if bs.convergence_state != STATE_CONVERGED
        ]
        if not active:
            return 0.0
        total = sum(cooling_factor(bs.entropy) for bs in active)
        return total / len(active)

    # ------------------------------------------------------------------
    # Query API
    # ------------------------------------------------------------------

    def get_belief(self, hypothesis_id: str) -> Optional[BeliefState]:
        """Return the current BeliefState for a hypothesis."""
        self._ensure_loaded()
        return self._beliefs.get(hypothesis_id)

    def active_hypotheses(self) -> List[str]:
        """Hypothesis IDs still open for exploration."""
        self._ensure_loaded()
        result = []
        for hid, bs in self._beliefs.items():
            eps = epsilon_from_prior(bs.prior)
            if not bs.is_halted(eps):
                result.append(hid)
        return sorted(result)

    def converged_hypotheses(self) -> List[str]:
        """Hypothesis IDs that have converged."""
        self._ensure_loaded()
        return sorted(
            hid for hid, bs in self._beliefs.items()
            if bs.convergence_state == STATE_CONVERGED
        )

    def all_proofs(self) -> Tuple[ConvergenceProof, ...]:
        """All convergence proofs emitted so far."""
        self._ensure_loaded()
        return tuple(self._proofs)

    def stats(self) -> Dict[str, Any]:
        """Summary statistics for diagnostics."""
        self._ensure_loaded()
        active = self.active_hypotheses()
        converged = self.converged_hypotheses()
        return {
            "total_tracked": len(self._beliefs),
            "active": len(active),
            "converged": len(converged),
            "proofs_emitted": len(self._proofs),
            "global_cooling_factor": round(self.global_cooling_factor(), 4),
        }

    def reset(self) -> None:
        """Clear all state. Test-only."""
        self._beliefs.clear()
        self._proofs.clear()
        self._loaded = False


# ---------------------------------------------------------------------------
# Default singleton
# ---------------------------------------------------------------------------

_DEFAULT_GOVERNOR: Optional[ConvergenceGovernor] = None


def get_default_governor(
    state_path: Optional[Path] = None,
    proofs_path: Optional[Path] = None,
) -> ConvergenceGovernor:
    global _DEFAULT_GOVERNOR
    if _DEFAULT_GOVERNOR is None:
        _DEFAULT_GOVERNOR = ConvergenceGovernor(
            state_path=state_path,
            proofs_path=proofs_path,
        )
    return _DEFAULT_GOVERNOR


def reset_default_governor() -> None:
    global _DEFAULT_GOVERNOR
    _DEFAULT_GOVERNOR = None


__all__ = [
    "ConvergenceGovernor",
    "MAX_PROOFS_RETAINED",
    "MAX_STATE_FILE_BYTES",
    "MAX_TRACKED_HYPOTHESES",
    "get_default_governor",
    "is_governor_enabled",
    "reset_default_governor",
]
