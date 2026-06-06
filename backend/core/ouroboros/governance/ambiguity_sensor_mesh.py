"""Asynchronous Ambiguity Sensor Mesh (Slice 99).

The LIVE nervous system that feeds the Slice-98 Dynamic Risk-State
Convergence Engine. Slice 98 built the brain (``record_ambiguity`` +
``convergence_score`` + the strictest-wins floor composition); this is
the afferent pathway that delivers REAL signals to it.

Design (non-negotiable)
=======================
* **NEVER break the hot path.** Every producer hook is BEST-EFFORT,
  wrapped in try/except, and a sensor failure can NEVER break
  generation / emit / the orchestrator. A hook is a tiny O(1)
  increment into a bounded in-process accumulator — nothing more.

* **PULL model, async, non-blocking.** Hooks only APPEND timestamped
  events to thread-safe bounded accumulators (no network, no LLM, no
  convergence-engine call on the hot path). A separate async sampler
  daemon READS those accumulators on a cadence and decides whether to
  fire ``record_ambiguity``. The sampler uses ``asyncio.sleep`` — no
  blocking I/O, no event-loop starvation.

* **Fire-and-forget honored.** The cross-repo mesh is "predictions, not
  requests" (Slice 97). JARVIS-side handshake health is observed ONLY
  from EMIT-SIDE failures (local ``emit_ripple`` failures) — there is
  NO outbound ping that expects a reply. Adding a request/response
  heartbeat would undo Slice 97's safety model, so we do not.

* **§33.1 default-FALSE master** ``JARVIS_AMBIGUITY_SENSOR_MESH_ENABLED``
  — hooks + sampler are ALL inert when off. Recovery in the convergence
  engine stays automatic (pure function of the window); the per-signal
  decay we pass through ``record_ambiguity(decay_s=...)`` preserves that
  (per-signal age-vs-decay, no latch).

Authority asymmetry (AST-pinned)
--------------------------------
Imports only stdlib + (lazily) ``dynamic_risk_convergence`` and the
observability/flag-registry seam. NEVER imports orchestrator /
iron_gate / policy / change_engine / candidate_generator /
auto_committer.
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

logger = logging.getLogger("Ouroboros.AmbiguitySensorMesh")


SENSOR_MESH_SCHEMA_VERSION: str = "ambiguity_sensor_mesh.1"


# ===========================================================================
# Env knobs
# ===========================================================================


_ENV_MASTER = "JARVIS_AMBIGUITY_SENSOR_MESH_ENABLED"
_ENV_WINDOW_S = "JARVIS_SENSOR_MESH_WINDOW_S"
_ENV_INTERVAL_S = "JARVIS_SENSOR_MESH_INTERVAL_S"
_ENV_MAX_EVENTS = "JARVIS_SENSOR_MESH_MAX_EVENTS"

# Per-class threshold / severity-weight / decay knobs. The "structured
# tensor": source = signal class, severity = weight, decay = decay_s.
_ENV_THRESH_HANDSHAKE = "JARVIS_SENSOR_MESH_HANDSHAKE_THRESHOLD"
_ENV_THRESH_CONTRADICTORY = "JARVIS_SENSOR_MESH_CONTRADICTORY_THRESHOLD"
_ENV_THRESH_MALFORMED = "JARVIS_SENSOR_MESH_MALFORMED_THRESHOLD"

_ENV_WEIGHT_HANDSHAKE = "JARVIS_SENSOR_MESH_HANDSHAKE_WEIGHT"
_ENV_WEIGHT_CONTRADICTORY = "JARVIS_SENSOR_MESH_CONTRADICTORY_WEIGHT"
_ENV_WEIGHT_MALFORMED = "JARVIS_SENSOR_MESH_MALFORMED_WEIGHT"

_ENV_DECAY_HANDSHAKE = "JARVIS_SENSOR_MESH_HANDSHAKE_DECAY_S"
_ENV_DECAY_CONTRADICTORY = "JARVIS_SENSOR_MESH_CONTRADICTORY_DECAY_S"
_ENV_DECAY_MALFORMED = "JARVIS_SENSOR_MESH_MALFORMED_DECAY_S"

# Slice 100 — FSM Sentinel. The orchestrator GENERATE_RETRY loop calls
# observe_generate_retry(...) with the current attempt number. The sentinel
# fires note_llm_contradiction ONLY when the attempt count reaches this
# threshold — i.e. the model is on at least its 2nd attempt = repeatedly
# fighting the validator. A single first-attempt retry is NOT ambiguity.
_ENV_SENTINEL_ATTEMPT_THRESHOLD = "JARVIS_SENTINEL_CONTRADICTION_ATTEMPT_THRESHOLD"
_DEFAULT_SENTINEL_ATTEMPT_THRESHOLD = 2


def _sentinel_attempt_threshold() -> int:
    return max(1, _env_int(
        _ENV_SENTINEL_ATTEMPT_THRESHOLD, _DEFAULT_SENTINEL_ATTEMPT_THRESHOLD,
    ))

_DEFAULT_WINDOW_S = 60.0
_DEFAULT_INTERVAL_S = 5.0
_DEFAULT_MAX_EVENTS = 500

# Default per-class thresholds (events within window → fire). Chosen so
# a single failure isn't ambiguity, but a sustained burst is.
_DEFAULT_THRESH_HANDSHAKE = 3
_DEFAULT_THRESH_CONTRADICTORY = 3
_DEFAULT_THRESH_MALFORMED = 3

# Default severity weights — each fire contributes this much weight to
# the convergence score. Sized so two simultaneous sources reach the
# paranoia threshold (6.0) → approval_required floor.
_DEFAULT_WEIGHT_HANDSHAKE = 3.0
_DEFAULT_WEIGHT_CONTRADICTORY = 3.0
_DEFAULT_WEIGHT_MALFORMED = 3.0

# Default per-class decay horizons handed to record_ambiguity(decay_s=)
# so the convergence engine relaxes per-signal automatically.
_DEFAULT_DECAY_HANDSHAKE = 60.0
_DEFAULT_DECAY_CONTRADICTORY = 60.0
_DEFAULT_DECAY_MALFORMED = 60.0

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSY = frozenset({"0", "false", "no", "off"})


def _flag(name: str, *, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    if raw in _FALSY:
        return False
    return raw in _TRUTHY or default


def master_enabled() -> bool:
    """§33.1 master — default-**FALSE**. Re-read at call time so
    monkeypatch + live operator flips work. When off, hooks are no-ops
    and the sampler is inert."""
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


def _interval_s() -> float:
    return max(0.0, _env_float(_ENV_INTERVAL_S, _DEFAULT_INTERVAL_S))


def _max_events() -> int:
    return max(1, _env_int(_ENV_MAX_EVENTS, _DEFAULT_MAX_EVENTS))


# ===========================================================================
# Closed taxonomy — mirrors the convergence engine's AmbiguitySignal
# ===========================================================================


class SignalClass(str, enum.Enum):
    """Closed 3-value producer-hook taxonomy. Each maps 1:1 onto a
    ``dynamic_risk_convergence.AmbiguitySignal``. Bytes-pinned via AST."""

    CROSS_REPO_HANDSHAKE = "cross_repo_handshake"
    CONTRADICTORY_OUTPUT = "contradictory_output"
    MALFORMED_INTENT = "malformed_intent"


# Per-class (threshold_env, default_threshold, weight_env,
# default_weight, decay_env, default_decay) tensor descriptor.
_CLASS_CONFIG: Dict[SignalClass, Tuple[str, int, str, float, str, float]] = {
    SignalClass.CROSS_REPO_HANDSHAKE: (
        _ENV_THRESH_HANDSHAKE, _DEFAULT_THRESH_HANDSHAKE,
        _ENV_WEIGHT_HANDSHAKE, _DEFAULT_WEIGHT_HANDSHAKE,
        _ENV_DECAY_HANDSHAKE, _DEFAULT_DECAY_HANDSHAKE,
    ),
    SignalClass.CONTRADICTORY_OUTPUT: (
        _ENV_THRESH_CONTRADICTORY, _DEFAULT_THRESH_CONTRADICTORY,
        _ENV_WEIGHT_CONTRADICTORY, _DEFAULT_WEIGHT_CONTRADICTORY,
        _ENV_DECAY_CONTRADICTORY, _DEFAULT_DECAY_CONTRADICTORY,
    ),
    SignalClass.MALFORMED_INTENT: (
        _ENV_THRESH_MALFORMED, _DEFAULT_THRESH_MALFORMED,
        _ENV_WEIGHT_MALFORMED, _DEFAULT_WEIGHT_MALFORMED,
        _ENV_DECAY_MALFORMED, _DEFAULT_DECAY_MALFORMED,
    ),
}


def _threshold_for(cls: SignalClass) -> int:
    env, default, *_ = _CLASS_CONFIG[cls]
    return max(1, _env_int(env, default))


def _weight_for(cls: SignalClass) -> float:
    _, _, env, default, _, _ = _CLASS_CONFIG[cls]
    return _env_float(env, default)


def _decay_for(cls: SignalClass) -> float:
    *_, env, default = _CLASS_CONFIG[cls]
    return _env_float(env, default)


# ===========================================================================
# Thread-safe in-process accumulators (per signal class)
# ===========================================================================


# One bounded deque of event timestamps per signal class. The ONLY
# mutable state. Drop-oldest on overflow (deque maxlen). Never grows
# unbounded.
_ACCUMULATORS: Dict[SignalClass, Deque[float]] = {
    cls: deque(maxlen=_DEFAULT_MAX_EVENTS) for cls in SignalClass
}
_LOCK = threading.Lock()


def _ensure_capacity() -> None:
    """Re-size accumulators if the env cap changed since module load.
    Defensive — never raises."""
    cap = _max_events()
    for cls, buf in list(_ACCUMULATORS.items()):
        if buf.maxlen != cap:
            _ACCUMULATORS[cls] = deque(buf, maxlen=cap)


def _append(cls: SignalClass, now_unix: Optional[float]) -> None:
    """Append a timestamped event into a class accumulator. O(1).
    Inert when master off. NEVER raises (hot-path safety)."""
    try:
        if not master_enabled():
            return
        try:
            ts = float(now_unix) if now_unix is not None else time.time()
        except (TypeError, ValueError):
            ts = time.time()
        with _LOCK:
            _ensure_capacity()
            _ACCUMULATORS[cls].append(ts)
    except Exception:  # noqa: BLE001 — producer hooks NEVER raise
        return


def _window_count(cls: SignalClass, now_unix: float) -> int:
    """In-window event count for a class. PURE w.r.t. the accumulator +
    now. NEVER raises."""
    try:
        window = _window_s()
        with _LOCK:
            snapshot = list(_ACCUMULATORS.get(cls, ()))
        count = 0
        for ts in snapshot:
            try:
                age = now_unix - float(ts)
            except (TypeError, ValueError):
                continue
            if age < 0:
                age = 0.0
            if age < window:
                count += 1
        return count
    except Exception:  # noqa: BLE001
        return 0


def reset_accumulators() -> None:
    """Test helper — clear all class accumulators. Production NEVER needs
    this (the sliding window self-decays). NEVER raises."""
    with _LOCK:
        for buf in _ACCUMULATORS.values():
            buf.clear()


# ===========================================================================
# Producer hooks — the LIVE nervous system. Tiny, best-effort, never raise.
# ===========================================================================


def note_cross_repo_emit_failure(
    detail: str = "", *, now_unix: Optional[float] = None,
) -> None:
    """Record an EMIT-SIDE cross-repo handshake failure.

    Call site: ``ripple_emitter.emit_ripple`` failure paths (sign error /
    write failure). Fire-and-forget honored — this is observed from
    LOCAL emit failures only, never a request/response heartbeat.
    O(1), best-effort, inert when master off. NEVER raises."""
    _append(SignalClass.CROSS_REPO_HANDSHAKE, now_unix)


def note_llm_contradiction(
    detail: str = "", *, now_unix: Optional[float] = None,
) -> None:
    """Record an LLM-fighting-the-validator signal — repeated GENERATE
    retries / Iron-Gate rejections / contradictory generative output.

    Call site (when wired): the orchestrator's GENERATE_RETRY /
    exploration-gate rejection point, ONLY past a small retry count.
    O(1), best-effort, inert when master off. NEVER raises."""
    _append(SignalClass.CONTRADICTORY_OUTPUT, now_unix)


def note_malformed_intent(
    detail: str = "", *, now_unix: Optional[float] = None,
) -> None:
    """Record a malformed-intent signal (intent-parse failure).

    O(1), best-effort, inert when master off. NEVER raises."""
    _append(SignalClass.MALFORMED_INTENT, now_unix)


# ===========================================================================
# Slice 100 — FSM Sentinel: the localized guard wired into the orchestrator
# GENERATE_RETRY loop. Self-contained, pull-model, NEVER raises.
# ===========================================================================


def observe_generate_retry(
    op_id: str,
    attempt_num: int,
    *,
    detail: str = "",
    now_unix: Optional[float] = None,
) -> bool:
    """FSM Sentinel — observe one GENERATE retry-advance from the
    orchestrator and, if the model is repeatedly fighting the validator,
    feed a contradiction signal to the sensor mesh.

    Fires ``note_llm_contradiction`` (a PULL-model accumulator append —
    NOT ``record_ambiguity``; the async sampler decides that later) ONLY
    when ``attempt_num >= JARVIS_SENTINEL_CONTRADICTION_ATTEMPT_THRESHOLD``
    (default 2). A single first-attempt retry is NOT ambiguity; a
    sustained run of validation/Iron-Gate failures across retries is.

    Returns ``True`` iff a contradiction was recorded. ENTIRELY wrapped in
    try/except — NEVER raises (this is wired into a ~6k-line hot path and
    a telemetry failure can NEVER crash or alter the FSM). Inert when the
    sensor-mesh master flag is off (``note_llm_contradiction`` already
    no-ops). This is the sentinel: a localized guard, not a whole-FSM
    decorator."""
    try:
        try:
            attempt = int(attempt_num)
        except (TypeError, ValueError):
            return False
        if attempt < _sentinel_attempt_threshold():
            return False
        # PULL model — append to the accumulator only. The async sampler
        # (run_sensor_mesh_once) reads it and decides whether to fire
        # record_ambiguity. The sentinel does NOT call record_ambiguity.
        note_llm_contradiction(
            f"op={op_id} attempt={attempt} {detail}".strip(),
            now_unix=now_unix,
        )
        return True
    except Exception:  # noqa: BLE001 — sentinel NEVER raises (hot path)
        return False


# ===========================================================================
# §33.5 frozen versioned report artifact
# ===========================================================================


# Map each SignalClass → the convergence engine's AmbiguitySignal value
# string (resolved lazily to AmbiguitySignal at fire time — keeps the
# import lazy for authority-asymmetry).
_CLASS_TO_CONVERGENCE_SIGNAL = {
    SignalClass.CROSS_REPO_HANDSHAKE: "CROSS_REPO_HANDSHAKE_FAILURE",
    SignalClass.CONTRADICTORY_OUTPUT: "CONTRADICTORY_OUTPUT",
    SignalClass.MALFORMED_INTENT: "MALFORMED_INTENT",
}


@dataclass(frozen=True)
class SensorMeshReport:
    """Frozen one-pass evaluation snapshot — §33.5 versioned artifact.

    * ``window_counts`` — per-class in-window event count.
    * ``fired`` — per-class bool: did this pass fire ``record_ambiguity``.
    * ``weights`` / ``decays`` — per-class severity weight + decay used.
    * ``thresholds`` — per-class threshold evaluated against.
    * ``evaluated_at_unix`` — when the pass ran.
    """

    schema_version: str
    window_counts: Dict[str, int]
    fired: Dict[str, bool]
    weights: Dict[str, float]
    decays: Dict[str, float]
    thresholds: Dict[str, int]
    evaluated_at_unix: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "window_counts": dict(self.window_counts),
            "fired": dict(self.fired),
            "weights": dict(self.weights),
            "decays": dict(self.decays),
            "thresholds": dict(self.thresholds),
            "evaluated_at_unix": float(self.evaluated_at_unix),
        }

    @property
    def any_fired(self) -> bool:
        return any(self.fired.values())


# ===========================================================================
# Async sampler — PULL model. Reads accumulators, fires record_ambiguity.
# ===========================================================================


def _fire_record_ambiguity(
    cls: SignalClass,
    *,
    now_unix: float,
    weight: float,
    decay_s: float,
) -> bool:
    """Lazy-import the convergence engine and fire one structured
    ``record_ambiguity`` for this class. Best-effort; NEVER raises.
    Returns True iff the call was made."""
    try:
        from backend.core.ouroboros.governance import (
            dynamic_risk_convergence as drc,
        )

        signal = getattr(
            drc.AmbiguitySignal,
            _CLASS_TO_CONVERGENCE_SIGNAL[cls],
            None,
        )
        if signal is None:
            return False
        drc.record_ambiguity(
            signal,
            now_unix=now_unix,
            weight=weight,
            decay_s=decay_s,
        )
        return True
    except Exception:  # noqa: BLE001 — sampler NEVER raises
        logger.debug(
            "[SensorMesh] record_ambiguity fire failed for %s",
            getattr(cls, "value", cls),
            exc_info=True,
        )
        return False


async def run_sensor_mesh_once(
    now_unix: Optional[float] = None,
) -> SensorMeshReport:
    """One evaluation pass. For each signal class, compute the in-window
    event count; if it breaches the class threshold, fire a structured
    ``record_ambiguity(signal, weight=severity, decay_s=per-class)``.

    Master OFF → an all-zero / nothing-fired report (inert). Async +
    bounded; does NOT sleep. NEVER raises."""
    try:
        now = float(now_unix) if now_unix is not None else time.time()
    except (TypeError, ValueError):
        now = time.time()

    window_counts: Dict[str, int] = {}
    fired: Dict[str, bool] = {}
    weights: Dict[str, float] = {}
    decays: Dict[str, float] = {}
    thresholds: Dict[str, int] = {}

    enabled = False
    try:
        enabled = master_enabled()
    except Exception:  # noqa: BLE001
        enabled = False

    for cls in SignalClass:
        key = cls.value
        weight = _weight_for(cls)
        decay = _decay_for(cls)
        threshold = _threshold_for(cls)
        weights[key] = weight
        decays[key] = decay
        thresholds[key] = threshold
        if not enabled:
            window_counts[key] = 0
            fired[key] = False
            continue
        count = _window_count(cls, now)
        window_counts[key] = count
        did_fire = False
        if count >= threshold:
            did_fire = _fire_record_ambiguity(
                cls, now_unix=now, weight=weight, decay_s=decay,
            )
        fired[key] = did_fire

    return SensorMeshReport(
        schema_version=SENSOR_MESH_SCHEMA_VERSION,
        window_counts=window_counts,
        fired=fired,
        weights=weights,
        decays=decays,
        thresholds=thresholds,
        evaluated_at_unix=now,
    )


async def run_sensor_mesh_loop(
    *,
    interval_s: Optional[float] = None,
    iterations: Optional[int] = None,
) -> int:
    """Async cadence loop. Runs ``run_sensor_mesh_once`` every
    ``interval_s`` seconds (env ``JARVIS_SENSOR_MESH_INTERVAL_S`` when
    unset). ``iterations`` bounds the loop for tests so it terminates;
    when ``None`` the loop runs until cancelled.

    PULL model — non-blocking ``asyncio.sleep`` between passes, no
    event-loop starvation. NEVER raises (a pass failure is swallowed and
    the loop continues). Returns the number of passes executed."""
    import asyncio

    sleep_s = _interval_s() if interval_s is None else max(0.0, float(
        interval_s if interval_s is not None else _interval_s()
    ))

    passes = 0
    try:
        while True:
            try:
                await run_sensor_mesh_once()
            except Exception:  # noqa: BLE001 — sampler NEVER raises
                logger.debug("[SensorMesh] pass failed", exc_info=True)
            passes += 1
            if iterations is not None and passes >= int(iterations):
                break
            if sleep_s > 0:
                try:
                    await asyncio.sleep(sleep_s)
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001
                    pass
    except asyncio.CancelledError:
        # Cooperative cancellation — clean exit, not an error.
        return passes
    except Exception:  # noqa: BLE001 — loop NEVER raises out
        logger.debug("[SensorMesh] loop aborted", exc_info=True)
    return passes


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
        "backend/core/ouroboros/governance/ambiguity_sensor_mesh.py"
    )

    _EXPECTED_CLASSES = {
        "cross_repo_handshake",
        "contradictory_output",
        "malformed_intent",
    }

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

    def _validate_signal_class_taxonomy(
        tree: ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        found = _enum_members(tree, "SignalClass")
        if not found:
            return ("SignalClass class not found",)
        missing = _EXPECTED_CLASSES - found
        extra = found - _EXPECTED_CLASSES
        if missing:
            return (f"SignalClass missing: {sorted(missing)}",)
        if extra:
            return (f"SignalClass drift: {sorted(extra)}",)
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
        """§33.1 — master_enabled MUST default to False (hooks + sampler
        inert by default)."""
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
                    "default=False (§33.1 default-FALSE)",
                )
        return ("master_enabled() not found",)

    def _validate_hooks_never_push_directly(
        tree: ast.AST, source: str,
    ) -> tuple:
        """PULL model — the producer hooks must NOT call
        record_ambiguity directly (that would push from the hot path).
        Only the sampler (_fire_record_ambiguity) may. We assert the
        three note_* hook bodies contain no 'record_ambiguity' call."""
        hook_names = {
            "note_cross_repo_emit_failure",
            "note_llm_contradiction",
            "note_malformed_intent",
        }
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.FunctionDef)
                and node.name in hook_names
            ):
                for sub in ast.walk(node):
                    if (
                        isinstance(sub, ast.Attribute)
                        and sub.attr == "record_ambiguity"
                    ):
                        return (
                            f"{node.name} calls record_ambiguity directly "
                            "— violates PULL model (hooks only append)",
                        )
        return ()

    return [
        ShippedCodeInvariant(
            invariant_name="sensor_mesh_class_taxonomy_closed",
            target_file=target,
            description=(
                "SignalClass 3-value taxonomy bytes-pinned, 1:1 with "
                "the convergence engine's AmbiguitySignal."
            ),
            validate=_validate_signal_class_taxonomy,
        ),
        ShippedCodeInvariant(
            invariant_name="sensor_mesh_authority_asymmetry",
            target_file=target,
            description=(
                "Sensor mesh imports only stdlib + (lazy) "
                "dynamic_risk_convergence / observability. MUST NOT "
                "import orchestrator / iron_gate / policy / "
                "change_engine / candidate_generator / auto_committer."
            ),
            validate=_validate_authority_asymmetry,
        ),
        ShippedCodeInvariant(
            invariant_name="sensor_mesh_master_default_false",
            target_file=target,
            description=(
                "§33.1 — master default-FALSE so all hooks + the "
                "sampler are inert by default."
            ),
            validate=_validate_master_default_false,
        ),
        ShippedCodeInvariant(
            invariant_name="sensor_mesh_hooks_pull_model",
            target_file=target,
            description=(
                "PULL model — producer hooks (note_*) only append to "
                "accumulators; they NEVER call record_ambiguity from "
                "the hot path. Only the async sampler fires it."
            ),
            validate=_validate_hooks_never_push_directly,
        ),
    ]


# ===========================================================================
# FlagRegistry seeds (auto-discovered via §33.3 naming-cage)
# ===========================================================================


def register_flags(registry: Any) -> int:
    """Register this mesh's env knobs. Auto-discovered. NEVER raises
    fatally (fail-open per seed)."""
    try:
        from backend.core.ouroboros.governance.flag_registry import (
            Category,
            FlagSpec,
            FlagType,
        )
    except Exception:  # noqa: BLE001
        return 0

    src = "backend/core/ouroboros/governance/ambiguity_sensor_mesh.py"
    seeds = [
        FlagSpec(
            name=_ENV_MASTER,
            type=FlagType.BOOL,
            default=False,
            description=(
                "Asynchronous Ambiguity Sensor Mesh master switch. "
                "§33.1 default-FALSE — when off, all producer hooks + "
                "the async sampler are inert (no signals reach the "
                "convergence engine)."
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
                "Sliding window (seconds) over which the sampler counts "
                "accumulated producer-hook events per signal class."
            ),
            category=Category.TIMING,
            source_file=src,
            example=f"{_ENV_WINDOW_S}=60.0",
        ),
        FlagSpec(
            name=_ENV_INTERVAL_S,
            type=FlagType.FLOAT,
            default=_DEFAULT_INTERVAL_S,
            description=(
                "Async sampler cadence (seconds) between evaluation "
                "passes."
            ),
            category=Category.TIMING,
            source_file=src,
            example=f"{_ENV_INTERVAL_S}=5.0",
        ),
        FlagSpec(
            name=_ENV_MAX_EVENTS,
            type=FlagType.INT,
            default=_DEFAULT_MAX_EVENTS,
            description=(
                "Bounded per-class accumulator capacity (oldest "
                "dropped on overflow)."
            ),
            category=Category.CAPACITY,
            source_file=src,
            example=f"{_ENV_MAX_EVENTS}=500",
        ),
        FlagSpec(
            name=_ENV_THRESH_HANDSHAKE,
            type=FlagType.INT,
            default=_DEFAULT_THRESH_HANDSHAKE,
            description=(
                "Cross-repo handshake-failure events within the window "
                "→ fire CROSS_REPO_HANDSHAKE_FAILURE ambiguity."
            ),
            category=Category.TUNING,
            source_file=src,
            example=f"{_ENV_THRESH_HANDSHAKE}=3",
        ),
        FlagSpec(
            name=_ENV_THRESH_CONTRADICTORY,
            type=FlagType.INT,
            default=_DEFAULT_THRESH_CONTRADICTORY,
            description=(
                "LLM-contradiction events within the window → fire "
                "CONTRADICTORY_OUTPUT ambiguity."
            ),
            category=Category.TUNING,
            source_file=src,
            example=f"{_ENV_THRESH_CONTRADICTORY}=3",
        ),
        FlagSpec(
            name=_ENV_THRESH_MALFORMED,
            type=FlagType.INT,
            default=_DEFAULT_THRESH_MALFORMED,
            description=(
                "Malformed-intent events within the window → fire "
                "MALFORMED_INTENT ambiguity."
            ),
            category=Category.TUNING,
            source_file=src,
            example=f"{_ENV_THRESH_MALFORMED}=3",
        ),
        FlagSpec(
            name=_ENV_WEIGHT_HANDSHAKE,
            type=FlagType.FLOAT,
            default=_DEFAULT_WEIGHT_HANDSHAKE,
            description=(
                "Severity weight contributed to the convergence score "
                "when the handshake class fires."
            ),
            category=Category.TUNING,
            source_file=src,
            example=f"{_ENV_WEIGHT_HANDSHAKE}=3.0",
        ),
        FlagSpec(
            name=_ENV_WEIGHT_CONTRADICTORY,
            type=FlagType.FLOAT,
            default=_DEFAULT_WEIGHT_CONTRADICTORY,
            description=(
                "Severity weight contributed when the contradictory "
                "class fires."
            ),
            category=Category.TUNING,
            source_file=src,
            example=f"{_ENV_WEIGHT_CONTRADICTORY}=3.0",
        ),
        FlagSpec(
            name=_ENV_WEIGHT_MALFORMED,
            type=FlagType.FLOAT,
            default=_DEFAULT_WEIGHT_MALFORMED,
            description=(
                "Severity weight contributed when the malformed-intent "
                "class fires."
            ),
            category=Category.TUNING,
            source_file=src,
            example=f"{_ENV_WEIGHT_MALFORMED}=3.0",
        ),
        FlagSpec(
            name=_ENV_DECAY_HANDSHAKE,
            type=FlagType.FLOAT,
            default=_DEFAULT_DECAY_HANDSHAKE,
            description=(
                "Per-signal decay horizon (seconds) handed to "
                "record_ambiguity(decay_s=) for handshake fires — the "
                "convergence engine relaxes per-signal automatically."
            ),
            category=Category.TIMING,
            source_file=src,
            example=f"{_ENV_DECAY_HANDSHAKE}=60.0",
        ),
        FlagSpec(
            name=_ENV_DECAY_CONTRADICTORY,
            type=FlagType.FLOAT,
            default=_DEFAULT_DECAY_CONTRADICTORY,
            description=(
                "Per-signal decay horizon (seconds) for contradictory "
                "fires."
            ),
            category=Category.TIMING,
            source_file=src,
            example=f"{_ENV_DECAY_CONTRADICTORY}=60.0",
        ),
        FlagSpec(
            name=_ENV_DECAY_MALFORMED,
            type=FlagType.FLOAT,
            default=_DEFAULT_DECAY_MALFORMED,
            description=(
                "Per-signal decay horizon (seconds) for malformed-intent "
                "fires."
            ),
            category=Category.TIMING,
            source_file=src,
            example=f"{_ENV_DECAY_MALFORMED}=60.0",
        ),
        FlagSpec(
            name=_ENV_SENTINEL_ATTEMPT_THRESHOLD,
            type=FlagType.INT,
            default=_DEFAULT_SENTINEL_ATTEMPT_THRESHOLD,
            description=(
                "Slice 100 FSM Sentinel — minimum GENERATE attempt number "
                "at which a retry-advance records an LLM-contradiction "
                "signal (the model is repeatedly fighting the validator). "
                "Default 2: a single first-attempt retry is not ambiguity."
            ),
            category=Category.TUNING,
            source_file=src,
            example=f"{_ENV_SENTINEL_ATTEMPT_THRESHOLD}=2",
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
    "SENSOR_MESH_SCHEMA_VERSION",
    "SignalClass",
    "SensorMeshReport",
    "master_enabled",
    "note_cross_repo_emit_failure",
    "note_llm_contradiction",
    "note_malformed_intent",
    "observe_generate_retry",
    "run_sensor_mesh_once",
    "run_sensor_mesh_loop",
    "reset_accumulators",
    "register_shipped_invariants",
    "register_flags",
]
