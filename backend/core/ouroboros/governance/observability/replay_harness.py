"""Slice 1.4 — Replay-as-pure-function harness.

Per ``OUROBOROS_VENOM_PRD.md`` §24.5 / §24.10.1:

  > replay-as-pure-function: ``replay(log, state₀) → state_T``
  > where ``state_T`` is byte-identical across runs. This is the
  > highest-impact unit test the codebase is missing.

This module ships:

  1. ``replay(log, state_0)`` — pure function that walks a
     ``DecisionRow`` log and reduces it into a final state dict.
     No side effects, no I/O, no randomness.

  2. ``assert_byte_identical_trace(trace_1, trace_2)`` — the
     determinism assertion. Proves that two independent runs of
     the same operation produce byte-identical decision traces.

  3. ``time_travel(log, t)`` — ``replay(log[:t], state_0)`` shortcut
     for inspecting the system at any point T in its history.

## Cage rules (load-bearing)

  * **Pure function** — ``replay()`` has NO side effects, NO I/O,
    NO randomness, NO datetime.now(). It consumes only its arguments
    and produces only its return value.
  * **Stdlib + determinism_substrate import surface only.**
  * **Canonical JSON for comparison** — ``assert_byte_identical_trace``
    uses ``canonical_serialize`` from Slice 1.2 so the comparison
    is architecture-stable.
  * **NEVER raises into the caller** — ``replay()`` catches errors
    and annotates them into the state dict. ``assert_byte_identical``
    returns ``(bool, diff_detail)`` rather than raising.

## Default-off

  Not flag-gated (test harness only — never runs in production hot
  path). The harness is consumed by unit tests and the replay CLI.
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# State shape
# ---------------------------------------------------------------------------


@dataclass
class ReplayState:
    """Mutable state accumulator for the replay reducer.

    This is the "state" that ``replay()`` reduces over the log.
    Each ``DecisionRow`` advances the state one step.

    Fields are chosen to capture every observable side effect of a
    decision trace:
      * ``decisions`` — ordered list of (phase, decision) pairs
      * ``factors_seen`` — set of factor keys encountered
      * ``phase_sequence`` — ordered list of phases traversed
      * ``payload_hashes`` — ordered list of content-addressed hashes
      * ``predecessor_graph`` — {row_hash: [predecessor_hashes]}
      * ``errors`` — any errors during replay (state corruption)
      * ``step_count`` — number of rows processed
    """

    decisions: List[Tuple[str, str]] = field(default_factory=list)
    factors_seen: List[str] = field(default_factory=list)
    phase_sequence: List[str] = field(default_factory=list)
    payload_hashes: List[str] = field(default_factory=list)
    predecessor_graph: Dict[str, List[str]] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)
    step_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        """Canonical dict representation for comparison."""
        return {
            "decisions": self.decisions,
            "factors_seen": self.factors_seen,
            "phase_sequence": self.phase_sequence,
            "payload_hashes": self.payload_hashes,
            "predecessor_graph": self.predecessor_graph,
            "errors": self.errors,
            "step_count": self.step_count,
        }


# ---------------------------------------------------------------------------
# Replay reducer (PURE FUNCTION — no side effects)
# ---------------------------------------------------------------------------


def _apply_row(state: ReplayState, row: Any) -> None:
    """Apply one DecisionRow to the state. Mutates ``state`` in place.

    Defensive — if a row has unexpected shape, the error is recorded
    in ``state.errors`` rather than raising.
    """
    try:
        phase = str(getattr(row, "phase", "") or "")
        decision = str(getattr(row, "decision", "") or "")
        state.decisions.append((phase, decision))
        state.phase_sequence.append(phase)
        state.step_count += 1

        # Track factors seen (unique keys across all rows).
        factors = getattr(row, "factors", None)
        if isinstance(factors, dict):
            for k in sorted(factors.keys()):
                if k not in state.factors_seen:
                    state.factors_seen.append(k)

        # Track payload hash (Merkle DAG content addressing).
        payload_hash = str(getattr(row, "payload_hash", "") or "")
        if payload_hash:
            state.payload_hashes.append(payload_hash)

        # Track predecessor graph edges.
        predecessor_ids = getattr(row, "predecessor_ids", ()) or ()
        if predecessor_ids and payload_hash:
            state.predecessor_graph[payload_hash] = [
                str(p) for p in predecessor_ids
            ]

    except Exception as exc:  # noqa: BLE001 — defensive
        state.errors.append(
            f"replay_error_at_step_{state.step_count}: "
            f"{type(exc).__name__}: {exc}"
        )


def replay(
    log: Sequence[Any],
    state_0: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Replay a decision trace log into a final state.

    This is a **PURE FUNCTION**: no side effects, no I/O, no
    randomness. The same ``(log, state_0)`` always produces the
    same return value.

    Parameters
    ----------
    log:
        Sequence of ``DecisionRow`` objects (or anything with
        ``.phase``, ``.decision``, ``.factors``, ``.payload_hash``,
        ``.predecessor_ids`` attributes).
    state_0:
        Optional initial state dict. If provided, its fields are
        merged into the initial ``ReplayState``. Default: empty state.

    Returns
    -------
    Dict[str, Any]
        The final state after all rows have been applied.
    """
    state = ReplayState()

    # Merge initial state if provided.
    if state_0:
        for key in ("decisions", "factors_seen", "phase_sequence",
                     "payload_hashes", "errors"):
            if key in state_0 and isinstance(state_0[key], list):
                setattr(state, key, list(state_0[key]))
        if "predecessor_graph" in state_0 and isinstance(
            state_0["predecessor_graph"], dict,
        ):
            state.predecessor_graph = dict(state_0["predecessor_graph"])
        if "step_count" in state_0:
            state.step_count = int(state_0.get("step_count", 0))

    for row in log:
        _apply_row(state, row)

    return state.to_dict()


def time_travel(
    log: Sequence[Any],
    t: int,
    state_0: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Replay the first ``t`` rows of the log.

    ``time_travel(log, t)`` ≡ ``replay(log[:t], state_0)``

    Enables "time-travel debugging": inspect the system state at
    any point T in its history.
    """
    return replay(log[:t], state_0)


# ---------------------------------------------------------------------------
# Byte-identical trace assertion
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TraceComparison:
    """Result of comparing two decision traces.

    ``identical`` is True iff the canonical JSON representations
    are byte-identical. ``diff_detail`` describes the first
    divergence found (if any).
    """

    identical: bool
    trace_1_hash: str
    trace_2_hash: str
    diff_detail: str = ""
    divergence_step: int = -1


def assert_byte_identical_trace(
    trace_1: Sequence[Any],
    trace_2: Sequence[Any],
) -> TraceComparison:
    """Assert that two decision traces produce byte-identical states.

    This is §24.10.1's "highest-impact unit test the codebase is
    missing". Two independent runs of the same operation with the
    same inputs MUST produce the same decision trace.

    Both traces are replayed through ``replay()`` and the resulting
    states are compared via ``canonical_serialize()`` for
    architecture-stable byte comparison.

    Returns a ``TraceComparison`` — NEVER raises.
    """
    try:
        from backend.core.ouroboros.governance.observability.determinism_substrate import (  # noqa: E501
            canonical_serialize,
        )
    except ImportError:
        # Fallback to json.dumps with sort_keys if substrate not available.
        def canonical_serialize(obj: Any) -> str:
            return json.dumps(obj, sort_keys=True, default=str)

    state_1 = replay(trace_1)
    state_2 = replay(trace_2)

    try:
        canonical_1 = canonical_serialize(state_1)
        canonical_2 = canonical_serialize(state_2)
    except Exception as exc:  # noqa: BLE001
        return TraceComparison(
            identical=False,
            trace_1_hash="error",
            trace_2_hash="error",
            diff_detail=f"serialization_error: {exc}",
        )

    hash_1 = hashlib.sha256(canonical_1.encode("utf-8")).hexdigest()
    hash_2 = hashlib.sha256(canonical_2.encode("utf-8")).hexdigest()

    if hash_1 == hash_2:
        return TraceComparison(
            identical=True,
            trace_1_hash=hash_1,
            trace_2_hash=hash_2,
        )

    # Find the first divergence point.
    diff_detail = ""
    divergence_step = -1
    for key in ("decisions", "phase_sequence", "payload_hashes",
                "factors_seen", "predecessor_graph", "step_count"):
        v1 = state_1.get(key)
        v2 = state_2.get(key)
        if v1 != v2:
            # For lists, find the first differing index.
            if isinstance(v1, list) and isinstance(v2, list):
                for i, (a, b) in enumerate(zip(v1, v2)):
                    if a != b:
                        diff_detail = (
                            f"divergence at {key}[{i}]: "
                            f"{repr(a)[:100]} vs {repr(b)[:100]}"
                        )
                        divergence_step = i
                        break
                else:
                    # Different lengths.
                    diff_detail = (
                        f"divergence at {key}: length "
                        f"{len(v1)} vs {len(v2)}"
                    )
            else:
                diff_detail = (
                    f"divergence at {key}: "
                    f"{repr(v1)[:100]} vs {repr(v2)[:100]}"
                )
            break

    return TraceComparison(
        identical=False,
        trace_1_hash=hash_1,
        trace_2_hash=hash_2,
        diff_detail=diff_detail,
        divergence_step=divergence_step,
    )


__all__ = [
    "ReplayState",
    "TraceComparison",
    "assert_byte_identical_trace",
    "replay",
    "time_travel",
]
