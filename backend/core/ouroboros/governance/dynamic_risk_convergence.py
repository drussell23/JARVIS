"""
Dynamic Risk-State Convergence Engine (Slice 98 Phase 2)
========================================================

Under measurable AMBIGUITY — cross-repo HMAC handshake failures from
the Slice-97 mesh, a high contradictory-output rate, a high
malformed-intent rate — the system must RECOVERABLY bias toward the
safe default: raise the global risk-tier floor toward observation-only
/ paranoia, then **autonomously relax back to baseline the instant the
ambiguity clears, with zero manual reset.**

THE recovery invariant (non-negotiable)
---------------------------------------
The recommended floor is a **PURE FUNCTION of the CURRENT ambiguity
window** — NOT a latched state machine. Signals are stamped with a
timestamp; :func:`convergence_score` only counts signals whose age is
``< window``. When signals age out of the rolling window, the computed
score drops and the floor relaxes automatically. There is:

* NO irreversible sever.
* NO latch that needs an operator reset.
* NO write-access cut / cage fail-closed-on-unmappable.

This mirrors the CLAUDE.md watchdog-isolation principle: the engine
shares no latched state with the system it guards. The only mutable
module state is the bounded signal deque (which only ever *decays* in
relevance as time advances) and an SSE chatter-suppression band tracker
(observability-only — never authoritative).

Composition discipline
----------------------
This engine does NOT build a parallel actuator. The canonical risk-floor
actuator is :mod:`risk_tier_floor`; its
:func:`risk_tier_floor.recommended_floor` composes multiple floor
sources via strictest-wins over the tier ladder. This engine contributes
ONE more candidate to that strictest-wins (see the lazy-import
composition wired into ``recommended_floor``). The returned floor strings
(``"notify_apply"`` / ``"approval_required"``) are exactly the tier-name
strings :mod:`risk_tier_floor` understands.

Authority asymmetry (AST-pinned)
--------------------------------
Imports only stdlib + (lazily) :mod:`risk_tier_floor` and
:mod:`ide_observability_stream`. NEVER imports orchestrator / iron_gate
/ policy / change_engine / candidate_generator / auto_committer.

§33.1 master flag
-----------------
``JARVIS_DYNAMIC_RISK_CONVERGENCE_ENABLED`` default-**FALSE**. Master-off
→ :func:`recommended_convergence_floor` returns ``None`` (inert), so the
composition into :func:`risk_tier_floor.recommended_floor` is byte-
identical to the engine not existing.
"""
from __future__ import annotations

import ast
import enum
import logging
import os
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Deque, Dict, List, Optional, Tuple

logger = logging.getLogger("Ouroboros.DynamicRiskConvergence")


CONVERGENCE_SCHEMA_VERSION: str = "dynamic_risk_convergence.1"


# ===========================================================================
# Env knobs
# ===========================================================================


_ENV_MASTER = "JARVIS_DYNAMIC_RISK_CONVERGENCE_ENABLED"
_ENV_MAX_SIGNALS = "JARVIS_CONVERGENCE_MAX_SIGNALS"
_ENV_WINDOW_S = "JARVIS_CONVERGENCE_WINDOW_S"
_ENV_ELEVATED_THRESHOLD = "JARVIS_CONVERGENCE_ELEVATED_THRESHOLD"
_ENV_PARANOIA_THRESHOLD = "JARVIS_CONVERGENCE_PARANOIA_THRESHOLD"
_ENV_WEIGHT_HANDSHAKE = "JARVIS_CONVERGENCE_WEIGHT_HANDSHAKE"
_ENV_WEIGHT_CONTRADICTORY = "JARVIS_CONVERGENCE_WEIGHT_CONTRADICTORY"
_ENV_WEIGHT_MALFORMED = "JARVIS_CONVERGENCE_WEIGHT_MALFORMED"

_DEFAULT_MAX_SIGNALS = 1000
_DEFAULT_WINDOW_S = 60.0
_DEFAULT_ELEVATED_THRESHOLD = 3.0
_DEFAULT_PARANOIA_THRESHOLD = 6.0

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSY = frozenset({"0", "false", "no", "off"})

# Floor tier-name strings — MUST match risk_tier_floor's vocabulary.
_FLOOR_ELEVATED = "notify_apply"
_FLOOR_PARANOIA = "approval_required"


def _flag(name: str, *, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    if raw in _FALSY:
        return False
    return raw in _TRUTHY or default


def master_enabled() -> bool:
    """§33.1 cognitive-substrate variant — default-**FALSE**.

    The convergence engine is a new cognitive surface awaiting
    empirical validation, so it ships default-FALSE (inert) per the
    canonical §33.1 pattern. Master-off → no floor contribution.
    """
    return _flag(_ENV_MASTER, default=False)


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _window_s() -> float:
    return _env_float(_ENV_WINDOW_S, _DEFAULT_WINDOW_S)


def _elevated_threshold() -> float:
    return _env_float(_ENV_ELEVATED_THRESHOLD, _DEFAULT_ELEVATED_THRESHOLD)


def _paranoia_threshold() -> float:
    return _env_float(_ENV_PARANOIA_THRESHOLD, _DEFAULT_PARANOIA_THRESHOLD)


# ===========================================================================
# Closed taxonomies
# ===========================================================================


class AmbiguitySignal(str, enum.Enum):
    """Closed 3-value ambiguity-signal taxonomy. Bytes-pinned via AST."""

    CROSS_REPO_HANDSHAKE_FAILURE = "cross_repo_handshake_failure"
    CONTRADICTORY_OUTPUT = "contradictory_output"
    MALFORMED_INTENT = "malformed_intent"


class ConvergenceBand(str, enum.Enum):
    """Closed 3-value convergence-band taxonomy. Bytes-pinned via AST.

    * ``NORMAL`` — baseline; no convergence floor contributed.
    * ``ELEVATED`` — ambiguity above the elevated threshold; floor
      raised to ``notify_apply``.
    * ``PARANOIA`` — ambiguity above the paranoia threshold; floor
      raised to ``approval_required`` (observation-only bias).
    """

    NORMAL = "normal"
    ELEVATED = "elevated"
    PARANOIA = "paranoia"


def _weight_for(signal: Any) -> float:
    """Per-signal weight multiplier (env-tunable). Unknown / garbage
    signal inputs default to 1.0 — never raises."""
    if signal is AmbiguitySignal.CROSS_REPO_HANDSHAKE_FAILURE:
        return _env_float(_ENV_WEIGHT_HANDSHAKE, 1.0)
    if signal is AmbiguitySignal.CONTRADICTORY_OUTPUT:
        return _env_float(_ENV_WEIGHT_CONTRADICTORY, 1.0)
    if signal is AmbiguitySignal.MALFORMED_INTENT:
        return _env_float(_ENV_WEIGHT_MALFORMED, 1.0)
    return 1.0


# ===========================================================================
# Bounded, time-windowed signal recorder
# ===========================================================================


# Module-level bounded deque of (signal, timestamp_unix, weight). The
# ONLY mutable authoritative state — and it only ever decays in relevance
# as time advances (pure-function-of-window recovery).
_SIGNALS: Deque[Tuple[Any, float, float]] = deque(
    maxlen=_DEFAULT_MAX_SIGNALS,
)
_LOCK = threading.Lock()

# Observability-only chatter-suppression — tracks the last band we
# emitted an SSE for so we publish only on transitions. NEVER
# authoritative (the floor is a pure function of the window).
_LAST_EMITTED_BAND: Optional[ConvergenceBand] = None


def _ensure_capacity() -> None:
    """Re-size the deque if the env cap changed since module load.

    Defensive: keeps the maxlen aligned with
    ``JARVIS_CONVERGENCE_MAX_SIGNALS`` without losing recent signals.
    """
    global _SIGNALS
    cap = _env_int(_ENV_MAX_SIGNALS, _DEFAULT_MAX_SIGNALS)
    if cap < 1:
        cap = 1
    if _SIGNALS.maxlen != cap:
        _SIGNALS = deque(_SIGNALS, maxlen=cap)


def record_ambiguity(
    signal: AmbiguitySignal,
    *,
    now_unix: Optional[float] = None,
    weight: float = 1.0,
) -> None:
    """Append an ambiguity signal stamped with the current time.

    Bounded (deque maxlen). NEVER raises — garbage inputs are stored
    as-is and simply contribute the default weight at scoring time
    (or are skipped if they can't be timestamped).
    """
    try:
        ts = float(now_unix) if now_unix is not None else time.time()
    except (TypeError, ValueError):
        ts = time.time()
    try:
        w = float(weight)
    except (TypeError, ValueError):
        w = 1.0
    with _LOCK:
        _ensure_capacity()
        _SIGNALS.append((signal, ts, w))


def reset_signals() -> None:
    """Test helper — clear the signal buffer + the SSE band tracker.

    NOTE: production NEVER needs this for recovery — recovery is purely
    a function of the time window (signals age out on their own). This
    exists only for deterministic test isolation. NEVER raises.
    """
    global _LAST_EMITTED_BAND
    with _LOCK:
        _SIGNALS.clear()
        _LAST_EMITTED_BAND = None


# ===========================================================================
# Pure scoring — the recovery invariant lives here
# ===========================================================================


def convergence_score(now_unix: Optional[float] = None) -> float:
    """Sum the weights of signals whose age ``< window``.

    PURE with respect to the current buffer + ``now``. Older signals
    simply don't count — that IS the auto-recovery: as ``now`` advances
    past ``signal_ts + window``, the signal drops out of the sum without
    any reset. NEVER raises.
    """
    try:
        now = float(now_unix) if now_unix is not None else time.time()
    except (TypeError, ValueError):
        now = time.time()
    window = _window_s()
    total = 0.0
    with _LOCK:
        snapshot = list(_SIGNALS)
    for signal, ts, stored_weight in snapshot:
        try:
            age = now - float(ts)
        except (TypeError, ValueError):
            continue
        if age < 0:
            # Future-stamped signal — count it (clock skew tolerance).
            age = 0.0
        if age < window:
            try:
                total += _weight_for(signal) * float(stored_weight)
            except (TypeError, ValueError):
                total += _weight_for(signal)
    return total


def _signal_counts(now_unix: Optional[float] = None) -> Dict[str, int]:
    """Count in-window signals per AmbiguitySignal name. NEVER raises."""
    try:
        now = float(now_unix) if now_unix is not None else time.time()
    except (TypeError, ValueError):
        now = time.time()
    window = _window_s()
    counts: Dict[str, int] = {s.value: 0 for s in AmbiguitySignal}
    with _LOCK:
        snapshot = list(_SIGNALS)
    for signal, ts, _w in snapshot:
        try:
            age = now - float(ts)
        except (TypeError, ValueError):
            continue
        if age < 0:
            age = 0.0
        if age < window and isinstance(signal, AmbiguitySignal):
            counts[signal.value] = counts.get(signal.value, 0) + 1
    return counts


def _band_for_score(score: float) -> ConvergenceBand:
    """Map a score to a band via the two thresholds. PURE."""
    if score < _elevated_threshold():
        return ConvergenceBand.NORMAL
    if score < _paranoia_threshold():
        return ConvergenceBand.ELEVATED
    return ConvergenceBand.PARANOIA


def _floor_for_band(band: ConvergenceBand) -> Optional[str]:
    if band is ConvergenceBand.ELEVATED:
        return _FLOOR_ELEVATED
    if band is ConvergenceBand.PARANOIA:
        return _FLOOR_PARANOIA
    return None


def recommended_convergence_floor(
    now_unix: Optional[float] = None,
) -> Optional[str]:
    """Return the convergence-derived risk-tier floor, or ``None``.

    Master OFF → ``None`` (inert). Else map the current in-window score
    to a floor:

      * ``score < elevated_threshold`` (default 3.0) → ``None`` (NORMAL).
      * ``score < paranoia_threshold`` (default 6.0) → ``"notify_apply"``.
      * ``score >= paranoia_threshold`` → ``"approval_required"``.

    PURE function of the window → relaxes to ``None`` automatically when
    the window empties. NEVER raises.
    """
    if not master_enabled():
        return None
    try:
        score = convergence_score(now_unix=now_unix)
        return _floor_for_band(_band_for_score(score))
    except Exception:  # noqa: BLE001 — defensive; floor must stay robust
        logger.debug(
            "[Convergence] recommended_convergence_floor failed",
            exc_info=True,
        )
        return None


# ===========================================================================
# §33.5 frozen versioned verdict artifact
# ===========================================================================


@dataclass(frozen=True)
class ConvergenceVerdict:
    """Frozen evaluation snapshot — §33.5 versioned artifact."""

    schema_version: str
    band: ConvergenceBand
    score: float
    recommended_floor: Optional[str]
    signal_counts: Dict[str, int]
    evaluated_at_unix: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "band": self.band.value,
            "score": float(self.score),
            "recommended_floor": self.recommended_floor,
            "signal_counts": dict(self.signal_counts),
            "evaluated_at_unix": float(self.evaluated_at_unix),
        }


def convergence_verdict(
    now_unix: Optional[float] = None,
) -> ConvergenceVerdict:
    """Compute the current verdict + emit an SSE on a band TRANSITION.

    Master OFF → ``NORMAL`` band, ``None`` floor, score 0.0
    (DISABLED-ish). NEVER raises.
    """
    try:
        now = float(now_unix) if now_unix is not None else time.time()
    except (TypeError, ValueError):
        now = time.time()

    if not master_enabled():
        return ConvergenceVerdict(
            schema_version=CONVERGENCE_SCHEMA_VERSION,
            band=ConvergenceBand.NORMAL,
            score=0.0,
            recommended_floor=None,
            signal_counts={s.value: 0 for s in AmbiguitySignal},
            evaluated_at_unix=now,
        )

    score = convergence_score(now_unix=now)
    band = _band_for_score(score)
    verdict = ConvergenceVerdict(
        schema_version=CONVERGENCE_SCHEMA_VERSION,
        band=band,
        score=score,
        recommended_floor=_floor_for_band(band),
        signal_counts=_signal_counts(now_unix=now),
        evaluated_at_unix=now,
    )
    _maybe_emit_transition(verdict)
    return verdict


# ===========================================================================
# Best-effort SSE — transition-only, NEVER raises
# ===========================================================================


def _maybe_emit_transition(verdict: ConvergenceVerdict) -> None:
    """Emit ``schelling_convergence_changed`` ONLY on a band transition.

    Tracks the last-emitted band in a module var (chatter suppression).
    Lazy-imports the SSE broker; swallows ALL exceptions — observability
    must never break the floor path. NEVER raises.
    """
    global _LAST_EMITTED_BAND
    try:
        with _LOCK:
            if verdict.band is _LAST_EMITTED_BAND:
                return
            # Initial state: an untracked (None) tracker landing on the
            # NORMAL baseline is not a transition worth announcing — only
            # a *departure* from baseline (or any later band change) is.
            if (
                _LAST_EMITTED_BAND is None
                and verdict.band is ConvergenceBand.NORMAL
            ):
                _LAST_EMITTED_BAND = ConvergenceBand.NORMAL
                return
            _LAST_EMITTED_BAND = verdict.band
    except Exception:  # noqa: BLE001
        return
    try:
        from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
            EVENT_TYPE_SCHELLING_CONVERGENCE_CHANGED,
            get_default_broker,
            stream_enabled,
        )

        if not stream_enabled():
            return
        get_default_broker().publish(
            EVENT_TYPE_SCHELLING_CONVERGENCE_CHANGED,
            "risk_convergence",
            verdict.to_dict(),
        )
    except Exception:  # noqa: BLE001 — observability best-effort
        logger.debug(
            "[Convergence] SSE transition publish failed", exc_info=True,
        )


# ===========================================================================
# AST pins via shipped_code_invariants
# ===========================================================================


def register_shipped_invariants() -> list:
    """Return AST invariant pins. Auto-discovered via §33.3. NEVER
    raises."""
    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    target = (
        "backend/core/ouroboros/governance/dynamic_risk_convergence.py"
    )

    _EXPECTED_SIGNALS = {
        "cross_repo_handshake_failure",
        "contradictory_output",
        "malformed_intent",
    }
    _EXPECTED_BANDS = {"normal", "elevated", "paranoia"}

    def _enum_members(tree: ast.AST, class_name: str) -> set:
        found: set = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == class_name:
                for sub in node.body:
                    if (
                        isinstance(sub, ast.Assign)
                        and len(sub.targets) == 1
                        and isinstance(sub.targets[0], ast.Name)
                        and isinstance(sub.value, ast.Constant)
                        and isinstance(sub.value.value, str)
                    ):
                        found.add(sub.value.value)
        return found

    def _validate_signal_taxonomy(
        tree: ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        found = _enum_members(tree, "AmbiguitySignal")
        if not found:
            return ("AmbiguitySignal class not found",)
        missing = _EXPECTED_SIGNALS - found
        extra = found - _EXPECTED_SIGNALS
        if missing:
            return (f"AmbiguitySignal missing: {sorted(missing)}",)
        if extra:
            return (f"AmbiguitySignal drift: {sorted(extra)}",)
        return ()

    def _validate_band_taxonomy(
        tree: ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        found = _enum_members(tree, "ConvergenceBand")
        if not found:
            return ("ConvergenceBand class not found",)
        missing = _EXPECTED_BANDS - found
        extra = found - _EXPECTED_BANDS
        if missing:
            return (f"ConvergenceBand missing: {sorted(missing)}",)
        if extra:
            return (f"ConvergenceBand drift: {sorted(extra)}",)
        return ()

    def _validate_authority_asymmetry(
        tree: ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        forbidden = (
            "backend.core.ouroboros.governance.orchestrator",
            "backend.core.ouroboros.governance.iron_gate",
            "backend.core.ouroboros.governance.policy",
            "backend.core.ouroboros.governance.change_engine",
            "backend.core.ouroboros.governance.candidate_generator",
            "backend.core.ouroboros.governance.auto_committer",
        )
        violations: List[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if any(mod == f for f in forbidden):
                    violations.append(f"forbidden authority import: {mod}")
        return tuple(violations)

    def _validate_master_default_false(
        tree: ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        """§33.1 cognitive-substrate shape — master_enabled MUST default
        to False (inert by default → byte-identical risk_tier_floor)."""
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.FunctionDef)
                and node.name == "master_enabled"
            ):
                for sub in ast.walk(node):
                    if (
                        isinstance(sub, ast.Call)
                        and isinstance(sub.func, ast.Name)
                        and sub.func.id == "_flag"
                    ):
                        for kw in sub.keywords:
                            if (
                                kw.arg == "default"
                                and isinstance(kw.value, ast.Constant)
                                and kw.value.value is False
                            ):
                                return ()
                return (
                    "master_enabled() must call _flag(...) with "
                    "default=False (§33.1 cognitive-substrate shape)",
                )
        return ("master_enabled() not found",)

    def _validate_composes_canonical(
        tree: ast.AST, source: str,
    ) -> tuple:
        """The engine MUST reference risk_tier_floor's tier-name
        vocabulary — it composes the canonical actuator, not a parallel
        one. The floor strings notify_apply / approval_required are the
        contract."""
        if "notify_apply" not in source or "approval_required" not in source:
            return (
                "convergence floor must emit risk_tier_floor tier-name "
                "strings (notify_apply / approval_required) — composes "
                "the canonical actuator, no parallel ladder",
            )
        return ()

    return [
        ShippedCodeInvariant(
            invariant_name="convergence_signal_taxonomy_closed",
            target_file=target,
            description=(
                "AmbiguitySignal 3-value taxonomy bytes-pinned. "
                "Adding/removing a signal requires updating the weight "
                "map + tests."
            ),
            validate=_validate_signal_taxonomy,
        ),
        ShippedCodeInvariant(
            invariant_name="convergence_band_taxonomy_closed",
            target_file=target,
            description=(
                "ConvergenceBand 3-value taxonomy bytes-pinned "
                "(NORMAL/ELEVATED/PARANOIA). Drift would desync the "
                "score→floor mapping."
            ),
            validate=_validate_band_taxonomy,
        ),
        ShippedCodeInvariant(
            invariant_name="convergence_authority_asymmetry",
            target_file=target,
            description=(
                "Substrate purity — convergence engine imports only "
                "stdlib + (lazy) risk_tier_floor / "
                "ide_observability_stream. MUST NOT import orchestrator "
                "/ iron_gate / policy / change_engine / "
                "candidate_generator / auto_committer."
            ),
            validate=_validate_authority_asymmetry,
        ),
        ShippedCodeInvariant(
            invariant_name="convergence_master_default_false",
            target_file=target,
            description=(
                "§33.1 cognitive-substrate shape — master default-FALSE "
                "so the composition into risk_tier_floor is inert / "
                "byte-identical by default."
            ),
            validate=_validate_master_default_false,
        ),
        ShippedCodeInvariant(
            invariant_name="convergence_composes_canonical",
            target_file=target,
            description=(
                "Composes the canonical risk_tier_floor actuator — "
                "emits its tier-name vocabulary (notify_apply / "
                "approval_required), no parallel ladder."
            ),
            validate=_validate_composes_canonical,
        ),
    ]


# ===========================================================================
# FlagRegistry seeds (auto-discovered via §33.3 naming-cage)
# ===========================================================================


def register_flags(registry: Any) -> int:
    """Register this engine's env knobs. Auto-discovered. NEVER raises
    fatally (fail-open per seed)."""
    try:
        from backend.core.ouroboros.governance.flag_registry import (
            Category,
            FlagSpec,
            FlagType,
        )
    except Exception:  # noqa: BLE001
        return 0

    src = "backend/core/ouroboros/governance/dynamic_risk_convergence.py"
    seeds = [
        FlagSpec(
            name=_ENV_MASTER,
            type=FlagType.BOOL,
            default=False,
            description=(
                "Dynamic Risk-State Convergence Engine master switch. "
                "§33.1 default-FALSE (inert) — when off, contributes no "
                "floor and risk_tier_floor is byte-identical."
            ),
            category=Category.SAFETY,
            source_file=src,
            example=f"{_ENV_MASTER}=true",
        ),
        FlagSpec(
            name=_ENV_WINDOW_S,
            type=FlagType.FLOAT,
            default=_DEFAULT_WINDOW_S,
            description=(
                "Rolling ambiguity window (seconds). Signals older than "
                "this don't count — the pure-function recovery horizon."
            ),
            category=Category.TIMING,
            source_file=src,
            example=f"{_ENV_WINDOW_S}=60.0",
        ),
        FlagSpec(
            name=_ENV_MAX_SIGNALS,
            type=FlagType.INT,
            default=_DEFAULT_MAX_SIGNALS,
            description=(
                "Bounded signal-deque capacity (oldest dropped on "
                "overflow)."
            ),
            category=Category.CAPACITY,
            source_file=src,
            example=f"{_ENV_MAX_SIGNALS}=1000",
        ),
        FlagSpec(
            name=_ENV_ELEVATED_THRESHOLD,
            type=FlagType.FLOAT,
            default=_DEFAULT_ELEVATED_THRESHOLD,
            description=(
                "Score >= this → ELEVATED band (notify_apply floor)."
            ),
            category=Category.TUNING,
            source_file=src,
            example=f"{_ENV_ELEVATED_THRESHOLD}=3.0",
        ),
        FlagSpec(
            name=_ENV_PARANOIA_THRESHOLD,
            type=FlagType.FLOAT,
            default=_DEFAULT_PARANOIA_THRESHOLD,
            description=(
                "Score >= this → PARANOIA band (approval_required "
                "floor, observation-only bias)."
            ),
            category=Category.TUNING,
            source_file=src,
            example=f"{_ENV_PARANOIA_THRESHOLD}=6.0",
        ),
        FlagSpec(
            name=_ENV_WEIGHT_HANDSHAKE,
            type=FlagType.FLOAT,
            default=1.0,
            description=(
                "Weight multiplier for CROSS_REPO_HANDSHAKE_FAILURE "
                "signals."
            ),
            category=Category.TUNING,
            source_file=src,
            example=f"{_ENV_WEIGHT_HANDSHAKE}=1.0",
        ),
        FlagSpec(
            name=_ENV_WEIGHT_CONTRADICTORY,
            type=FlagType.FLOAT,
            default=1.0,
            description=(
                "Weight multiplier for CONTRADICTORY_OUTPUT signals."
            ),
            category=Category.TUNING,
            source_file=src,
            example=f"{_ENV_WEIGHT_CONTRADICTORY}=1.0",
        ),
        FlagSpec(
            name=_ENV_WEIGHT_MALFORMED,
            type=FlagType.FLOAT,
            default=1.0,
            description=(
                "Weight multiplier for MALFORMED_INTENT signals."
            ),
            category=Category.TUNING,
            source_file=src,
            example=f"{_ENV_WEIGHT_MALFORMED}=1.0",
        ),
    ]

    count = 0
    for spec in seeds:
        try:
            registry.register(spec)
            count += 1
        except Exception:  # noqa: BLE001 — fail-open per §33.1
            continue
    return count


__all__ = [
    "CONVERGENCE_SCHEMA_VERSION",
    "AmbiguitySignal",
    "ConvergenceBand",
    "ConvergenceVerdict",
    "master_enabled",
    "record_ambiguity",
    "reset_signals",
    "convergence_score",
    "recommended_convergence_floor",
    "convergence_verdict",
    "register_shipped_invariants",
    "register_flags",
]
