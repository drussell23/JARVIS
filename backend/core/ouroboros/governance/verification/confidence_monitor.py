"""Priority 1 Slice 2 — Rolling-window confidence monitor + circuit-breaker.

The active layer for Priority 1 (Confidence-Aware Execution, PRD
§26.5.1). Slice 1 (`confidence_capture.py`) acquires the signal;
this module evaluates it against a posture-relevant floor and
produces a structural verdict that the GENERATE retry path consumes.

Architecture
------------

  * ``ConfidenceVerdict`` — three-valued enum:
    ``OK`` / ``APPROACHING_FLOOR`` / ``BELOW_FLOOR``.
  * ``ConfidenceMonitor`` — bounded-deque rolling window over
    top-1/top-2 margins. Pure data: ``observe(margin)`` accumulates;
    ``evaluate(posture: Optional[str])`` returns the verdict.
  * ``ConfidenceCollapseError`` — typed RuntimeError subclass for
    the GENERATE-retry path. Mirrors ``ExplorationInsufficientError``
    so existing ``except RuntimeError`` retry handlers catch it
    without modification. Carries the verdict + window snapshot for
    Slice 3's HypothesisProbe consumer.

Master flag + sub-flag (shadow → enforce pattern)
-------------------------------------------------

``JARVIS_CONFIDENCE_MONITOR_ENABLED`` (default ``false``) — gates
observation + evaluation. When off, ``evaluate()`` returns ``OK``
unconditionally and ``observe()`` is a pure no-op.

``JARVIS_CONFIDENCE_MONITOR_ENFORCE`` (default ``false``) — gates
the abort/raise path. Slice 2 ships SHADOW only: the monitor
observes + tags ctx artifacts, but does NOT raise. Slice 5
graduation flips both flags simultaneously after 3 clean soaks.
This mirrors the Slice 5 Arc B shadow→enforce pattern (memory:
`project_slice5_arc_b.md`).

Knobs (FlagRegistry-typed; defensively bounded)
-----------------------------------------------

  * ``JARVIS_CONFIDENCE_FLOOR`` (default ``0.05``) — minimum
    acceptable rolling-mean top-1/top-2 margin. Posture-relevant
    via ``evaluate(posture=...)``: HARDEN tightens to 0.10,
    EXPLORE loosens to 0.02, MAINTAIN stays at default,
    CONSOLIDATE 0.07. Caller passes posture; the monitor itself
    is posture-agnostic (no DirectionInferrer import — keeps
    authority invariants clean).
  * ``JARVIS_CONFIDENCE_WINDOW_K`` (default ``16``) — rolling
    deque maxlen. Floored at 1.
  * ``JARVIS_CONFIDENCE_APPROACHING_FACTOR`` (default ``1.5``) —
    ``APPROACHING_FLOOR`` triggers at ``floor × factor``. Allows
    early SSE warning before the abort condition.

Semantic precision (root-cause precision, not shortcut)
-------------------------------------------------------

  * Top-1/top-2 margin is the canonical confidence signal — large
    margin = confident, small margin = uncertain. Mean over a
    rolling window smooths per-token noise.
  * ``BELOW_FLOOR`` requires the rolling-mean margin to fall below
    ``floor``. Early-stream tokens (window not yet full) DON'T
    trigger BELOW_FLOOR — at least ``min_observations`` (default
    K/2, floored at 2) must have landed. This prevents false
    positives on short generations.
  * ``APPROACHING_FLOOR`` triggers when ``floor < margin ≤
    floor × factor`` AND window is sufficiently warm. Slice 4
    will surface this as an SSE event; Slice 2 just exposes the
    verdict.

Authority invariants (AST-pinned by tests)
------------------------------------------

  * No imports of orchestrator / phase_runners / candidate_generator /
    iron_gate / change_engine / policy / semantic_guardian /
    semantic_firewall / providers / direction_inferrer.
  * Pure stdlib (``collections``, ``logging``, ``math``, ``os``,
    ``threading``) + typing only.
  * NEVER raises out of any public method — defensive everywhere.
  * Read-only on inputs — never modifies the captured trace.
  * No control-flow influence in master-off OR shadow modes —
    Slice 5 graduation flips ENFORCE on; until then,
    ``ConfidenceCollapseError`` is constructed but never raised
    by this module's public surface. The provider wiring (which
    has access to ENFORCE flag) decides whether to raise.
"""
from __future__ import annotations

import logging
import math
import os
import threading
from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Deque, Optional

logger = logging.getLogger(__name__)


CONFIDENCE_MONITOR_SCHEMA_VERSION: str = "confidence_monitor.1"


# ---------------------------------------------------------------------------
# Master flag + enforce sub-flag — Slice 2 ships both default false
# ---------------------------------------------------------------------------


def confidence_monitor_enabled() -> bool:
    """``JARVIS_CONFIDENCE_MONITOR_ENABLED`` (default ``true`` —
    graduated in Priority 1 Slice 5).

    Asymmetric env semantics — empty/whitespace = unset = graduated
    default-true; explicit truthy enables; explicit falsy disables.
    Re-read at call time so monkeypatch + live toggle work."""
    raw = os.environ.get(
        "JARVIS_CONFIDENCE_MONITOR_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return True  # graduated default (Slice 5 — was false in Slice 2)
    return raw in ("1", "true", "yes", "on")


def confidence_monitor_enforce() -> bool:
    """``JARVIS_CONFIDENCE_MONITOR_ENFORCE`` (default ``true`` —
    graduated in Priority 1 Slice 5; was shadow-only in Slice 2).

    Sub-flag governing the raise path. When on (graduated),
    BELOW_FLOOR mid-stream triggers ``ConfidenceCollapseError`` so
    the GENERATE retry path engages. When off (hot-revert), the
    monitor observes + tags ctx but does NOT raise.

    Precedence: env explicit > adapted YAML > hardcoded default.
    The adapted YAML can only set this to ``True`` (the loader
    drops a ``False`` since baseline is False — see
    ``adapted_confidence_loader._filter_enforce``)."""
    raw = os.environ.get(
        "JARVIS_CONFIDENCE_MONITOR_ENFORCE", "",
    ).strip().lower()
    if raw == "":
        adapted = _adapted_enforce_or_none()
        if adapted is not None:
            return adapted
        return True  # graduated default (Slice 5 — was shadow in Slice 2)
    return raw in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Knobs (FlagRegistry-typed; values clamped defensively)
# ---------------------------------------------------------------------------


_DEFAULT_FLOOR: float = 0.05
_DEFAULT_WINDOW_K: int = 16
_DEFAULT_APPROACHING_FACTOR: float = 1.5
_MIN_WINDOW_K: int = 1
_MIN_APPROACHING_FACTOR: float = 1.0  # factor < 1.0 would mean
                                       # "approaching is below floor"
                                       # (degenerate); clamp.

# Posture-relevant floor multipliers. Caller passes posture string;
# monitor multiplies the configured floor by the posture's factor
# to derive the effective floor. Posture-agnostic mode → factor 1.0.
_POSTURE_FLOOR_MULTIPLIERS: dict = {
    "HARDEN":      2.0,   # tighter floor — more sensitive to drops
    "CONSOLIDATE": 1.4,   # moderately tighter
    "MAINTAIN":    1.0,   # default
    "EXPLORE":     0.4,   # looser — accept more uncertainty
}


# ---------------------------------------------------------------------------
# Adapted-thresholds loader bridge (Gap #2 Slice 3)
# ---------------------------------------------------------------------------
#
# When the env knob is unset, accessors consult
# ``adapted_confidence_loader`` for an operator-approved tightening
# materialized into ``.jarvis/adapted_confidence_thresholds.yaml``.
# Precedence (load-bearing):
#
#     env explicit  >  adapted YAML  >  hardcoded default
#
# The loader is default-off (``JARVIS_CONFIDENCE_LOAD_ADAPTED``);
# when off OR YAML missing OR malformed, the per-knob accessor
# returns ``None`` and the monitor falls through to its hardcoded
# default — pre-Slice-3 behavior is byte-identical.
#
# Helpers below wrap the loader so a runtime import error or other
# defensive failure never breaks confidence_monitor's contract of
# "every accessor NEVER raises". Lazy import inside the helper
# keeps the module-level import surface unchanged when the loader
# is disabled.


def _adapted_floor_or_none() -> Optional[float]:
    try:
        from backend.core.ouroboros.governance.adaptation.adapted_confidence_loader import (  # noqa: E501
            adapted_floor,
        )
        return adapted_floor()
    except Exception:  # noqa: BLE001 — loader is best-effort
        return None


def _adapted_window_k_or_none() -> Optional[int]:
    try:
        from backend.core.ouroboros.governance.adaptation.adapted_confidence_loader import (  # noqa: E501
            adapted_window_k,
        )
        return adapted_window_k()
    except Exception:  # noqa: BLE001 — loader is best-effort
        return None


def _adapted_approaching_factor_or_none() -> Optional[float]:
    try:
        from backend.core.ouroboros.governance.adaptation.adapted_confidence_loader import (  # noqa: E501
            adapted_approaching_factor,
        )
        return adapted_approaching_factor()
    except Exception:  # noqa: BLE001 — loader is best-effort
        return None


def _adapted_enforce_or_none() -> Optional[bool]:
    try:
        from backend.core.ouroboros.governance.adaptation.adapted_confidence_loader import (  # noqa: E501
            adapted_enforce,
        )
        return adapted_enforce()
    except Exception:  # noqa: BLE001 — loader is best-effort
        return None


def confidence_floor() -> float:
    """``JARVIS_CONFIDENCE_FLOOR`` (default ``0.05``). Floor is the
    minimum acceptable rolling-mean top-1/top-2 margin. Floored at
    0.0 (negative would be a logical error).

    Precedence: env explicit > adapted YAML > hardcoded default.
    NEVER raises."""
    raw = os.environ.get("JARVIS_CONFIDENCE_FLOOR", "").strip()
    if not raw:
        adapted = _adapted_floor_or_none()
        if adapted is not None:
            return adapted
        return _DEFAULT_FLOOR
    try:
        val = float(raw)
        if not math.isfinite(val) or val < 0.0:
            return _DEFAULT_FLOOR
        return val
    except (TypeError, ValueError):
        return _DEFAULT_FLOOR


def confidence_window_k() -> int:
    """``JARVIS_CONFIDENCE_WINDOW_K`` (default ``16``). Floored at 1.

    Precedence: env explicit > adapted YAML > hardcoded default.
    NEVER raises."""
    raw = os.environ.get("JARVIS_CONFIDENCE_WINDOW_K", "").strip()
    if not raw:
        adapted = _adapted_window_k_or_none()
        if adapted is not None:
            return max(_MIN_WINDOW_K, adapted)
        return _DEFAULT_WINDOW_K
    try:
        val = int(raw)
        return max(_MIN_WINDOW_K, val)
    except (TypeError, ValueError):
        return _DEFAULT_WINDOW_K


def confidence_approaching_factor() -> float:
    """``JARVIS_CONFIDENCE_APPROACHING_FACTOR`` (default ``1.5``).
    Floored at 1.0 (factor < 1.0 would invert APPROACHING vs BELOW
    semantics).

    Precedence: env explicit > adapted YAML > hardcoded default.
    NEVER raises."""
    raw = os.environ.get(
        "JARVIS_CONFIDENCE_APPROACHING_FACTOR", "",
    ).strip()
    if not raw:
        adapted = _adapted_approaching_factor_or_none()
        if adapted is not None:
            return max(_MIN_APPROACHING_FACTOR, adapted)
        return _DEFAULT_APPROACHING_FACTOR
    try:
        val = float(raw)
        if not math.isfinite(val):
            return _DEFAULT_APPROACHING_FACTOR
        return max(_MIN_APPROACHING_FACTOR, val)
    except (TypeError, ValueError):
        return _DEFAULT_APPROACHING_FACTOR


def _posture_multiplier(posture: Optional[str]) -> float:
    """Return the floor multiplier for a posture string. Unknown /
    None posture → 1.0 (the configured floor). NEVER raises."""
    try:
        if posture is None:
            return 1.0
        key = str(posture).strip().upper()
        return _POSTURE_FLOOR_MULTIPLIERS.get(key, 1.0)
    except Exception:  # noqa: BLE001 — defensive
        return 1.0


# ---------------------------------------------------------------------------
# Verdict enum
# ---------------------------------------------------------------------------


class ConfidenceVerdict(str, Enum):
    """Three-valued evaluation outcome.

    String-valued so it serializes cleanly into JSON-friendly
    artifacts on ctx and ledger records. Enum membership is the
    runtime contract; the string value is the persistence shape."""

    OK = "ok"
    APPROACHING_FLOOR = "approaching_floor"
    BELOW_FLOOR = "below_floor"


# ---------------------------------------------------------------------------
# ConfidenceCollapseError — for the GENERATE retry path (Slice 3 consumer)
# ---------------------------------------------------------------------------


class ConfidenceCollapseError(RuntimeError):
    """Raised by the provider wiring when the monitor observes
    BELOW_FLOOR AND ``JARVIS_CONFIDENCE_MONITOR_ENFORCE=true``.

    Mirrors ``ExplorationInsufficientError`` (exploration_engine.py)
    so existing ``except RuntimeError`` / ``except Exception``
    retry handlers in candidate_generator + orchestrator catch it
    without code change. The string message starts with
    ``"confidence_collapse:"`` so the error-classification regex
    branches (similar to ``"exploration_insufficient:"``) can route
    it to the appropriate retry path.

    Slice 3 will consume this via HypothesisProbe to decide
    RETRY_WITH_FEEDBACK vs ESCALATE_TO_OPERATOR. Slice 2 ships
    the error class shape; the provider wiring constructs but
    does NOT raise it until ENFORCE flips."""

    def __init__(
        self,
        *,
        verdict: ConfidenceVerdict,
        rolling_margin: Optional[float],
        floor: float,
        effective_floor: float,
        window_size: int,
        observations_count: int,
        posture: Optional[str] = None,
        provider: str = "",
        model_id: str = "",
        op_id: str = "",
    ) -> None:
        self.verdict = verdict
        self.rolling_margin = rolling_margin
        self.floor = floor
        self.effective_floor = effective_floor
        self.window_size = window_size
        self.observations_count = observations_count
        self.posture = posture
        self.provider = provider
        self.model_id = model_id
        self.op_id = op_id
        margin_repr = (
            f"{rolling_margin:.4f}" if rolling_margin is not None else "n/a"
        )
        msg = (
            f"confidence_collapse:verdict={verdict.value} "
            f"margin={margin_repr} floor={floor:.4f} "
            f"effective_floor={effective_floor:.4f} "
            f"posture={posture or 'none'} "
            f"obs={observations_count}/{window_size} "
            f"provider={provider!r} model={model_id!r} op={op_id!r}"
        )
        super().__init__(msg)


# ---------------------------------------------------------------------------
# ConfidenceMonitor — bounded rolling-window evaluator
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MonitorSnapshot:
    """Frozen, hashable snapshot of monitor state at a point in
    time. Useful for ledger persistence + Slice 4 SSE payloads.

    Empty-window state: ``observations_count=0``, ``rolling_margin=None``."""

    observations_count: int = 0
    window_size: int = 0
    rolling_margin: Optional[float] = None
    min_margin: Optional[float] = None
    max_margin: Optional[float] = None
    schema_version: str = CONFIDENCE_MONITOR_SCHEMA_VERSION


class ConfidenceMonitor:
    """Bounded-deque rolling-window monitor over top-1/top-2 margins.

    NOT a singleton. One instance per GENERATE round; lives on
    ``ctx.artifacts["confidence_monitor"]`` alongside the Slice 1
    capturer.

    Thread-safe (RLock). Master-flag-gated: when off, every method
    is a pure no-op (observe → False; evaluate → OK; snapshot →
    empty).

    Construction is cheap. Tests + production both rely on default-
    constructed instances; explicit window_size override is for
    test isolation."""

    __slots__ = (
        "_window",
        "_window_size",
        "_observations",
        "_lock",
        "_provider",
        "_model_id",
        "_op_id",
    )

    def __init__(
        self,
        *,
        window_size: Optional[int] = None,
        provider: str = "",
        model_id: str = "",
        op_id: str = "",
    ) -> None:
        size = (
            window_size
            if isinstance(window_size, int) and window_size >= _MIN_WINDOW_K
            else confidence_window_k()
        )
        self._window: Deque[float] = deque(maxlen=size)
        self._window_size: int = size
        self._observations: int = 0
        self._lock = threading.RLock()
        self._provider: str = provider
        self._model_id: str = model_id
        self._op_id: str = op_id

    @property
    def window_size(self) -> int:
        return self._window_size

    @property
    def observations_count(self) -> int:
        with self._lock:
            return self._observations

    @property
    def provider(self) -> str:
        return self._provider

    @property
    def model_id(self) -> str:
        return self._model_id

    def observe(self, margin: object) -> bool:
        """Append a single margin observation to the rolling window.

        Returns True if observed, False if dropped (master-off OR
        non-finite margin — non-finite margins are SILENTLY DROPPED
        to keep the rolling-mean meaningful; alternative would be
        coercing to 0.0 which would falsely depress confidence).

        NEVER raises."""
        if not confidence_monitor_enabled():
            return False
        try:
            val = float(margin)  # type: ignore[arg-type]
            if not math.isfinite(val):
                return False
            with self._lock:
                self._window.append(val)
                self._observations += 1
                return True
        except (TypeError, ValueError):
            return False
        except Exception:  # noqa: BLE001 — defensive
            return False

    def current_margin(self) -> Optional[float]:
        """Return the rolling-mean margin over the current window,
        or ``None`` if empty. NEVER raises."""
        try:
            with self._lock:
                if not self._window:
                    return None
                return sum(self._window) / len(self._window)
        except Exception:  # noqa: BLE001 — defensive
            return None

    def reset_window(self) -> int:
        """Clear the rolling-margin window. Returns the number of
        observations dropped. NEVER raises.

        **Move 5 Slice 4** entry point: when a confidence-aware
        probe loop CONVERGES on an autonomous answer
        (``ProbeOutcome.CONVERGED``), the caller invokes this to
        clear the rolling window so the next ``evaluate()`` call
        returns ``OK`` regardless of the prior low-confidence
        signal. This is the structural mechanism that makes
        ``PROBE_ENVIRONMENT`` → ``RETRY_WITH_FEEDBACK`` produce a
        clean retry without re-firing the collapse on the next
        observation.

        Master-flag respected: if ``confidence_monitor_enabled()``
        is off, this is a no-op (returns 0)."""
        if not confidence_monitor_enabled():
            return 0
        try:
            with self._lock:
                dropped = len(self._window)
                self._window.clear()
                return dropped
        except Exception:  # noqa: BLE001 — defensive
            return 0

    def snapshot(self) -> MonitorSnapshot:
        """Frozen point-in-time view of monitor state. Useful for
        ledger persistence, SSE payloads (Slice 4), heartbeat
        annotations. NEVER raises."""
        try:
            with self._lock:
                if not self._window:
                    return MonitorSnapshot(
                        window_size=self._window_size,
                    )
                vals = tuple(self._window)
            mean_val = sum(vals) / len(vals)
            return MonitorSnapshot(
                observations_count=self._observations,
                window_size=self._window_size,
                rolling_margin=mean_val,
                min_margin=min(vals),
                max_margin=max(vals),
            )
        except Exception:  # noqa: BLE001 — defensive
            return MonitorSnapshot(window_size=self._window_size)

    def evaluate(
        self,
        *,
        posture: Optional[str] = None,
        floor: Optional[float] = None,
        approaching_factor: Optional[float] = None,
    ) -> ConfidenceVerdict:
        """Return the verdict for the current rolling window.

        Master-flag-gated: when off, returns ``OK`` unconditionally
        — provider wiring relies on this to short-circuit cleanly.

        Insufficient-observations behavior: when fewer than
        ``ceil(window_size/2)`` (floored at 2) observations have
        landed, returns ``OK`` regardless of margin. Prevents
        false-positive aborts on short generations where one
        unusually-confused token would otherwise dominate.

        Posture-relevance: passes ``posture`` through
        ``_posture_multiplier`` to derive the effective floor.
        Caller is responsible for fetching current posture (e.g.,
        from DirectionInferrer). The monitor itself imports zero
        posture infrastructure — keeps authority invariants clean.

        Override knobs ``floor`` / ``approaching_factor`` are for
        tests + per-op tuning; production callers pass None and
        let the env knobs govern.

        NEVER raises."""
        if not confidence_monitor_enabled():
            return ConfidenceVerdict.OK
        try:
            with self._lock:
                obs = self._observations
                cur_window = tuple(self._window)
            if not cur_window:
                return ConfidenceVerdict.OK

            min_obs_required = max(2, (self._window_size + 1) // 2)
            if obs < min_obs_required:
                return ConfidenceVerdict.OK

            mean_val = sum(cur_window) / len(cur_window)

            base_floor = (
                float(floor) if floor is not None else confidence_floor()
            )
            base_floor = max(0.0, base_floor)
            posture_mult = _posture_multiplier(posture)
            effective_floor = base_floor * posture_mult

            factor = (
                float(approaching_factor)
                if approaching_factor is not None
                else confidence_approaching_factor()
            )
            factor = max(_MIN_APPROACHING_FACTOR, factor)

            if mean_val < effective_floor:
                return ConfidenceVerdict.BELOW_FLOOR
            if mean_val < effective_floor * factor:
                return ConfidenceVerdict.APPROACHING_FLOOR
            return ConfidenceVerdict.OK
        except Exception:  # noqa: BLE001 — defensive
            return ConfidenceVerdict.OK

    def effective_floor(
        self, *, posture: Optional[str] = None,
        floor: Optional[float] = None,
    ) -> float:
        """Pure helper — what is the effective floor under this
        posture? Useful for diagnostic + Slice 4 SSE payloads.
        NEVER raises."""
        try:
            base = float(floor) if floor is not None else confidence_floor()
            base = max(0.0, base)
            return base * _posture_multiplier(posture)
        except Exception:  # noqa: BLE001 — defensive
            return _DEFAULT_FLOOR

    def reset(self) -> None:
        """Drop window state. Useful between tool-loop rounds.
        NEVER raises."""
        try:
            with self._lock:
                self._window.clear()
                self._observations = 0
        except Exception:  # noqa: BLE001
            pass

    def to_collapse_error(
        self,
        *,
        verdict: ConfidenceVerdict,
        posture: Optional[str] = None,
        floor: Optional[float] = None,
    ) -> ConfidenceCollapseError:
        """Construct a ``ConfidenceCollapseError`` from current
        monitor state. NOT raised by this module — provider wiring
        decides whether to raise (gated on ENFORCE sub-flag).
        NEVER raises."""
        snap = self.snapshot()
        base = float(floor) if floor is not None else confidence_floor()
        base = max(0.0, base)
        eff = base * _posture_multiplier(posture)
        return ConfidenceCollapseError(
            verdict=verdict,
            rolling_margin=snap.rolling_margin,
            floor=base,
            effective_floor=eff,
            window_size=snap.window_size,
            observations_count=snap.observations_count,
            posture=posture,
            provider=self._provider,
            model_id=self._model_id,
            op_id=self._op_id,
        )


# ---------------------------------------------------------------------------
# Convenience: bridge from Slice 1 ConfidenceTrace → margin sequence
# ---------------------------------------------------------------------------


def feed_trace_into_monitor(
    monitor: ConfidenceMonitor,
    trace: object,
) -> int:
    """Push every margin from a ``ConfidenceTrace`` (Slice 1) into
    ``monitor.observe()``. Returns the count of accepted observations.

    Useful for post-hoc evaluation — the DW stream wiring observes
    inline; this helper supports test paths + Slice 3+ consumers
    that build a monitor from a captured trace.

    NEVER raises. Skips tokens whose ``margin_top1_top2()`` is None."""
    if monitor is None:
        return 0
    accepted = 0
    try:
        tokens = getattr(trace, "tokens", None)
        if not tokens:
            return 0
        for tok in tokens:
            try:
                m = tok.margin_top1_top2()
                if m is None:
                    continue
                if monitor.observe(m):
                    accepted += 1
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001 — defensive
        return accepted
    return accepted


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = [
    "CONFIDENCE_MONITOR_SCHEMA_VERSION",
    "ConfidenceCollapseError",
    "ConfidenceMonitor",
    "ConfidenceVerdict",
    "MonitorSnapshot",
    "confidence_approaching_factor",
    "confidence_floor",
    "confidence_monitor_enabled",
    "confidence_monitor_enforce",
    "confidence_window_k",
    "feed_trace_into_monitor",
]
