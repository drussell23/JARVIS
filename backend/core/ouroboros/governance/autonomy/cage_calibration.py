"""The cage learns what the work actually needs — and only ever tightens.

`worker_synthesizer` derives a worker's shape from static AST inspection of
its sub-goal. That derivation is a PRIOR, and until now there was no
posterior: a worker granted ``mutation_budget=3`` that never used more than
one, or one that failed because it was denied a tool, taught the synthesizer
nothing. The same shape was re-derived, identically wrong, forever.

`ScopedToolBackend` has been recording the evidence the whole time —
``mutations_count`` against ``max_mutations``, and ``call_records`` stamped
``authorized`` / ``type_denied`` / ``count_denied``. Nobody consumed it for
learning. This module closes that loop.

Tighten autonomously; NEVER widen
----------------------------------
This is the load-bearing decision, and it is deliberately asymmetric.

Observed under-use is safe to act on alone: a shape class whose workers
consistently use one mutation of three does not need three, and dropping to
what the evidence shows is strictly less privilege than the prior already
granted. Nothing can go wrong that was not already possible.

Observed DENIAL is not. A worker repeatedly denied ``bash`` is evidence that
something wants ``bash`` — and a system that grants it on that basis has
built a privilege-escalation ramp out of persistence. Any worker, including a
prompt-injected one, could widen its own cage by asking enough times.

So denials never widen anything. They become a FINDING: the synthesizer's
inspection rule for that class of work is wrong, and the rule is what should
be fixed. That is the root fix; a learned per-class exception would be the
workaround, and it would erode the cage one class at a time.

Note the direction is the opposite of `UserMemory.matches_path`, where the
safe move was to WIDEN a guard and never narrow. The invariant is not
"always widen" or "always tighten" — it is *move only in the direction that
cannot grant something new*. For a deny-list that means widening; for an
allow-list it means tightening.

The learning key is the synthesizer's own derivation
-----------------------------------------------------
Not ``unit_id`` — those never repeat. The key is ``role`` + ``read_only``,
both already produced by `worker_synthesizer`. The synthesizer clusters work
into descriptive roles (``"python-source mutator"``,
``"test-suite analyzer"``) as a side effect of deriving a shape, so the
clustering needed for learning already exists and costs nothing. No new
taxonomy, no hardcoded categories.

Reuses the memory arc's math, not a copy of it
-----------------------------------------------
Two `memory_utility.UtilityStore` instances, pointed at their own files. That
class is already an opaque-key store of decayed, confidence-weighted
observations with a corpus-mean baseline — exactly the statistics needed
here, already written and tested. A second implementation would be a second
definition of "how fast does evidence age".

* the OUTCOME store: weight = did the unit succeed (is this shape working?)
* the HEADROOM store: weight = mutations_used / mutations_granted (how much
  does this class of work actually need?)

Cold start changes nothing
---------------------------
No observations → the prior is returned unchanged, byte-identical to the
synthesizer's output. Same invariant `Drift.UNKNOWN` and `memory_utility`
keep: absence of evidence is not evidence, and a calibration that acted on
nothing would be a rewrite of the cage disguised as learning.

Python 3.9+, ``from __future__ import annotations``.
"""
from __future__ import annotations

import dataclasses
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger("Ouroboros.CageCalibration")

CAGE_CALIBRATION_SCHEMA_VERSION: str = "cage_calibration.1"

__all__ = [
    "CAGE_CALIBRATION_SCHEMA_VERSION",
    "CageObservation",
    "CageFinding",
    "calibrate_shape",
    "calibration_enabled",
    "findings",
    "observe_unit",
    "reset_for_tests",
    "shape_signature",
]


def _flag(name: str, default: str = "1") -> bool:
    try:
        return os.environ.get(name, default).strip().lower() not in (
            "0", "false", "no", "off", "")
    except Exception:  # noqa: BLE001
        return True


def _num(name: str, default: float, lo: float, hi: float) -> float:
    try:
        return min(hi, max(lo, float(os.environ.get(name, "").strip() or default)))
    except Exception:  # noqa: BLE001
        return default


def calibration_enabled() -> bool:
    """``JARVIS_CAGE_CALIBRATION_ENABLED`` (default **false**).

    Default-OFF because it narrows a live security boundary from observed
    data. Tightening is the safe direction, but "safe direction" is not
    "unsupervised by default" — it earns its default-on by soak, like every
    other autonomous behaviour here.
    """
    return _flag("JARVIS_CAGE_CALIBRATION_ENABLED", "0")


def _min_observations() -> int:
    """Observations of a shape class before its cage may be tightened.

    ``JARVIS_CAGE_CALIBRATION_MIN_OBS``. One worker that happened to need
    only one mutation is not evidence that the class needs one.
    """
    return int(_num("JARVIS_CAGE_CALIBRATION_MIN_OBS", 8, 2, 1000))


def _headroom_margin() -> float:
    """Safety margin above observed peak usage. ``JARVIS_CAGE_HEADROOM_MARGIN``.

    Tightening to the exact observed maximum would make the next slightly
    harder instance of the same work fail on ``count_denied``. The margin
    buys that instance room while still shedding unused privilege.
    """
    return _num("JARVIS_CAGE_HEADROOM_MARGIN", 1.5, 1.0, 4.0)


def _denial_finding_ratio() -> float:
    """Denial rate at which a shape class becomes a FINDING.

    ``JARVIS_CAGE_DENIAL_FINDING_RATIO``. Not a widening threshold — nothing
    widens. It is the point at which the synthesizer's inspection rule for
    this class is worth reporting as wrong.
    """
    return _num("JARVIS_CAGE_DENIAL_FINDING_RATIO", 0.25, 0.0, 1.0)


# ---------------------------------------------------------------------------
# The learning key
# ---------------------------------------------------------------------------

_SIG_CLEAN = re.compile(r"[^a-z0-9]+")


def shape_signature(shape: Any) -> str:
    """The class this shape belongs to. Pure. NEVER raises.

    ``role`` + ``read_only``, both already derived by the synthesizer. Using
    its own clustering means no second taxonomy exists to drift from the
    first, and a new role invented by inspection tomorrow automatically
    becomes a new learning class with no code change.
    """
    try:
        role = _SIG_CLEAN.sub(
            "-", str(getattr(shape, "role", "") or "unknown").lower()).strip("-")
        mode = "ro" if bool(getattr(shape, "read_only", False)) else "rw"
        return f"{role or 'unknown'}:{mode}"
    except Exception:  # noqa: BLE001
        return "unknown:rw"


# ---------------------------------------------------------------------------
# Stores — two instances of the memory arc's UtilityStore
# ---------------------------------------------------------------------------

_stores: Dict[str, Any] = {}
_root_override: Optional[Path] = None


def _store(kind: str) -> Any:
    """A `UtilityStore` for *kind*, created on first use. NEVER raises."""
    global _stores  # noqa: PLW0603
    existing = _stores.get(kind)
    if existing is not None:
        return existing
    try:
        from backend.core.ouroboros.governance.memory_utility import UtilityStore

        root = _root_override
        if root is None:
            root = Path(__file__).resolve().parents[5]
        store = UtilityStore(root / ".jarvis" / f"cage_{kind}.jsonl")
        _stores[kind] = store
        return store
    except Exception:  # noqa: BLE001
        logger.debug("[CageCalibration] store unavailable", exc_info=True)
        return None


def reset_for_tests(root: Optional[Path] = None) -> None:
    """Drop both stores, optionally rebinding their root. Test-only."""
    global _stores, _root_override  # noqa: PLW0603
    _root_override = root
    _stores = {}


# ---------------------------------------------------------------------------
# Observation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CageObservation:
    """What one caged worker actually did with what it was given."""

    signature: str
    granted_mutations: int
    used_mutations: int
    denied_tools: Tuple[str, ...]
    count_denied: int
    succeeded: bool

    @property
    def headroom(self) -> float:
        """``used / granted`` in [0, 1]. 1.0 when nothing was granted.

        A read-only worker granted zero mutations has, by definition, used
        all of its (zero) budget — reporting 0.0 would drag the class mean
        toward "needs nothing" using a worker that was never allowed
        anything.
        """
        if self.granted_mutations <= 0:
            return 1.0
        return min(1.0, max(0.0, self.used_mutations / self.granted_mutations))


def _extract(backend: Any) -> Tuple[int, int, Tuple[str, ...], int]:
    """``(granted, used, denied_tools, count_denied)`` from a cage. NEVER raises."""
    try:
        granted = int(getattr(backend, "max_mutations", 0) or 0)
        used = int(getattr(backend, "mutations_count", 0) or 0)
        denied: List[str] = []
        count_denied = 0
        for record in (getattr(backend, "call_records", ()) or ()):
            # (name, call_id, status, t) — the cage's own audit trail.
            if len(record) < 3:
                continue
            name, status = str(record[0]), str(record[2])
            if status == "type_denied":
                denied.append(name)
            elif status == "count_denied":
                count_denied += 1
        return granted, used, tuple(denied), count_denied
    except Exception:  # noqa: BLE001
        logger.debug("[CageCalibration] cage extraction degraded", exc_info=True)
        return 0, 0, (), 0


def observe_unit(shape: Any, backend: Any, result: Any) -> Optional[CageObservation]:
    """Record what this worker did with its cage. NEVER raises.

    Returns None when calibration is off or the cage yielded nothing to learn
    from. Called at the ONE seam where a caged unit finishes, so a worker
    cannot complete without leaving evidence.
    """
    if not calibration_enabled():
        return None
    try:
        import time as _t

        signature = shape_signature(shape)
        if signature.startswith("unknown:"):
            # A shape with no derivable role cannot be attributed to a class.
            # Recording it would credit a real class's statistics with an
            # observation that belongs to no class — the same reason an
            # unattributable op earns no memory topic any credit.
            logger.debug("[CageCalibration] unattributable shape — not recorded")
            return None
        granted, used, denied, count_denied = _extract(backend)
        status = str(getattr(getattr(result, "status", None), "value",
                             getattr(result, "status", ""))).lower()
        succeeded = status in ("completed", "complete", "succeeded")

        obs = CageObservation(
            signature=signature, granted_mutations=granted, used_mutations=used,
            denied_tools=denied, count_denied=count_denied, succeeded=succeeded,
        )

        from backend.core.ouroboros.governance.memory_utility import Observation

        now = _t.time()
        outcome = _store("outcome")
        if outcome is not None:
            outcome.add([Observation(signature, 1.0 if succeeded else 0.0, now)])
        # Headroom is only meaningful when a budget was actually granted AND
        # the unit succeeded. A FAILED worker may have stopped early for
        # reasons unrelated to budget, and counting its low usage as "this
        # class needs less" would tighten the cage on the strength of a crash.
        headroom = _store("headroom")
        if headroom is not None and granted > 0 and succeeded:
            headroom.add([Observation(signature, obs.headroom, now)])
        if denied or count_denied:
            # Feed the aggregating sensor from the SAME seam, so no second
            # instrumentation point exists to fall out of date. Fail-soft:
            # the sensor must never be able to fail a unit's telemetry.
            try:
                from backend.core.ouroboros.governance.intake.sensors.cage_hygiene_sensor import (
                    note_denials,
                )
                note_denials(signature, denied, count_denied)
            except Exception:  # noqa: BLE001
                logger.debug("[CageCalibration] denial aggregation skipped",
                             exc_info=True)
            denial = _store("denial")
            if denial is not None:
                denial.add([Observation(signature, 1.0, now,
                                        op_id=",".join(sorted(set(denied))[:5]))])
        logger.debug(
            "[CageCalibration] %s granted=%d used=%d denied=%s cd=%d ok=%s",
            signature, granted, used, denied, count_denied, succeeded,
        )
        return obs
    except Exception:  # noqa: BLE001
        logger.debug("[CageCalibration] observation degraded", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Calibration — tighten only
# ---------------------------------------------------------------------------


def calibrate_shape(shape: Any) -> Any:
    """The prior, tightened by evidence. NEVER raises, NEVER widens.

    Returns *shape* unchanged when calibration is off, evidence is thin, or
    anything at all goes wrong. Every failure path returns the synthesizer's
    original output, so a broken calibrator costs exactly nothing.

    The post-condition is asserted, not assumed: the returned shape's tool
    set must be a SUBSET of the prior's and its budget must be ``<=`` the
    prior's. If the computation ever violated that, the prior is returned
    instead — a calibrator is not permitted to be the thing that grants
    privilege.
    """
    if not calibration_enabled():
        return shape
    try:
        signature = shape_signature(shape)
        store = _store("headroom")
        if store is None:
            return shape
        reading = store.reading(signature)
        if reading.cold or reading.observations < _min_observations():
            return shape  # thin evidence -> the prior stands
        if reading.polarity is None:
            return shape

        granted = int(getattr(shape, "mutation_budget", 0) or 0)
        if granted <= 0:
            return shape

        # Decayed mean headroom -> the budget this class actually consumes,
        # plus a margin so the next slightly-harder instance is not starved.
        target = int(round(granted * float(reading.polarity) * _headroom_margin()))
        tightened = max(1, min(granted, target))
        if tightened >= granted:
            return shape  # nothing to shed

        candidate = dataclasses.replace(shape, mutation_budget=tightened)

        # Post-condition. Cheap, and the only thing standing between a
        # subtle arithmetic slip and a widened cage.
        if not _is_tightening(shape, candidate):
            logger.warning(
                "[CageCalibration] %s calibration would not tighten — "
                "returning the prior", signature)
            return shape

        logger.info(
            "[CageCalibration] %s mutation_budget %d -> %d "
            "(headroom %.2f over n=%d)",
            signature, granted, tightened, reading.polarity,
            reading.observations,
        )
        return candidate
    except Exception:  # noqa: BLE001
        logger.debug("[CageCalibration] calibration degraded", exc_info=True)
        return shape


def _is_tightening(prior: Any, candidate: Any) -> bool:
    """Whether *candidate* grants nothing the *prior* did not. Pure.

    The structural invariant, checked rather than trusted.
    """
    try:
        prior_tools = set(getattr(prior, "allowed_tools", ()) or ())
        cand_tools = set(getattr(candidate, "allowed_tools", ()) or ())
        if not cand_tools.issubset(prior_tools):
            return False
        if int(getattr(candidate, "mutation_budget", 0) or 0) > int(
                getattr(prior, "mutation_budget", 0) or 0):
            return False
        if bool(getattr(prior, "read_only", False)) and not bool(
                getattr(candidate, "read_only", False)):
            return False
        return True
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# Proactive surface — denials are findings, never widenings
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CageFinding:
    """A shape class whose synthesized cage is chronically wrong."""

    signature: str
    kind: str
    detail: str
    observations: int


def findings() -> List[CageFinding]:
    """Shape classes the evidence says are mis-synthesized. NEVER raises.

    This is the proactive half, and the reason denials do not widen: the
    finding names the SYNTHESIZER'S RULE as the thing to fix, which is the
    root cause. A learned per-class exception would paper over it and erode
    the cage one class at a time.
    """
    out: List[CageFinding] = []
    if not calibration_enabled():
        return out
    try:
        denial = _store("denial")
        outcome = _store("outcome")
        if denial is None or outcome is None:
            return out
        floor = _min_observations()
        ratio_floor = _denial_finding_ratio()
        for signature in denial.hashes():
            d = denial.reading(signature)
            o = outcome.reading(signature)
            if o.cold or o.observations < floor:
                continue
            ratio = d.observations / max(1, o.observations)
            if ratio < ratio_floor:
                continue
            out.append(CageFinding(
                signature=signature, kind="chronic_denial",
                detail=(f"workers of class {signature!r} were denied a tool or "
                        f"exhausted their budget in {d.observations} of "
                        f"{o.observations} runs ({ratio:.0%}). The synthesizer's "
                        f"inspection rule for this class is under-granting — fix "
                        f"the RULE in worker_synthesizer; the cage is not widened "
                        f"from observed demand by design."),
                observations=o.observations,
            ))
    except Exception:  # noqa: BLE001
        logger.debug("[CageCalibration] findings degraded", exc_info=True)
    return out


def render_calibration_lines(limit: int = 8) -> List[str]:
    """Markup lines for an operator surface. NEVER raises."""
    if not calibration_enabled():
        return ["  [dim]cage calibration disabled "
                "(JARVIS_CAGE_CALIBRATION_ENABLED=0)[/dim]"]
    try:
        headroom = _store("headroom")
        outcome = _store("outcome")
        if headroom is None or outcome is None:
            return ["  [dim]cage calibration unavailable[/dim]"]
        sigs = sorted(set(headroom.hashes()) | set(outcome.hashes()))
        if not sigs:
            return ["  [bold]swarm · cage calibration[/bold]",
                    "    [dim]no caged workers observed yet[/dim]"]
        out = [f"  [bold]swarm · cage calibration[/bold]  "
               f"[dim]{len(sigs)} shape class(es)[/dim]"]
        for sig in sigs[:limit]:
            h = headroom.reading(sig)
            o = outcome.reading(sig)
            hp = f"{h.polarity:.2f}" if h.polarity is not None else "—"
            op = f"{o.polarity:.0%}" if o.polarity is not None else "—"
            out.append(f"    {sig}  headroom {hp}  success {op}  n={o.observations}")
        for finding in findings()[:limit]:
            out.append(f"    [yellow]⚠ {finding.signature}[/yellow] "
                       f"{finding.kind}")
        return out
    except Exception as exc:  # noqa: BLE001
        return [f"  [dim]cage calibration degraded: "
                f"{type(exc).__name__}: {exc}[/dim]"]
