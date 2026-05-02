"""Move 4 Slice 1 — InvariantDriftAuditor primitive (PRD §27.4.4).

The load-bearing safety property for the Reverse Russian Doll Order 2
trajectory: as the organism adapts (Pass C surface miners propose
tightenings, Move 3 advisory router proposes deferrals, operators
approve patches), the *architectural invariants* the system was born
with must not silently regress.

Companion to ``observability/trajectory_auditor.py`` — that module
tracks **physical** codebase trajectory (LOC, complexity, public-API
count). This module tracks **semantic** invariant drift (shipped-code
pins, flag defaults, exploration floors, posture). Orthogonal
concerns; both feed into the same operator situational-awareness
loop.

Move 3 (auto_action_router) closed the verification → action loop
operationally. Move 4 closes it *temporally* — the organism takes a
frozen snapshot of its own architectural promises at boot and can
re-validate later snapshots against the baseline to detect
*semantic drift*: a safety property that quietly weakened between
two points in time.

Slice 1 ships the **primitive only**:

  * Frozen ``InvariantSnapshot`` dataclass — captures the four
    architectural promise surfaces this codebase already exposes:
      1. ``shipped_code_invariants`` — the registered AST/byte pins
         (e.g., "BG never cascades to Claude unless is_read_only").
      2. ``flag_registry`` — every typed env flag's default value
         (a default flip is a behavioral change worth detecting).
      3. ``exploration_engine.ExplorationFloors`` — per-complexity
         thresholds for the Iron Gate exploration ledger
         (a lowered floor IS a safety regression).
      4. ``posture_store`` current-reading — informational drift
         (postures legitimately move; this is a watchword surface).

  * ``InvariantDriftRecord`` — one diff between two snapshots, with
    severity classification (CRITICAL / WARNING / INFO).

  * Pure ``compare_snapshots(a, b)`` engine — deterministic, total,
    NEVER raises. Caller can run this at any cadence (boot vs now,
    sliding window, etc.) — Slice 1 has zero opinions on cadence.

  * ``capture_snapshot()`` — defensive live capture that reads from
    the four surfaces and degrades gracefully when any is unavailable.

Slices 2-5 (NOT in this commit) wire boot capture, periodic
re-validation, the auto_action_router signal bridge, and operator
surfaces (REPL + GET + SSE).

Authority invariants (AST-pinned by companion tests):

  * Imports stdlib + four read-only governance modules ONLY:
    ``shipped_code_invariants``, ``flag_registry``,
    ``exploration_engine`` (for ``ExplorationFloors`` only), and
    ``posture_observer`` (for ``get_default_store`` only).
  * NO orchestrator / phase_runners / candidate_generator /
    iron_gate / change_engine / policy / semantic_guardian /
    semantic_firewall / providers / doubleword_provider /
    urgency_router / auto_action_router imports — pure comparison
    primitive over already-public read-only surfaces.
  * NEVER raises out of any public function — defensive everywhere
    (mirrors ``shipped_code_invariants.validate_all`` discipline).
  * Read-only — never writes a ledger, never publishes SSE, never
    mutates ctx. Slice 1 is a primitive; Slice 4 wires producer
    surfaces.
"""
from __future__ import annotations

import enum
import hashlib
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass
from typing import (
    Any,
    Dict,
    List,
    Mapping,
    Optional,
    Tuple,
)

logger = logging.getLogger(__name__)


INVARIANT_DRIFT_AUDITOR_SCHEMA_VERSION: str = "invariant_drift_auditor.1"


# ---------------------------------------------------------------------------
# Master flag (Slice 1: present but only consulted by capture/compare to
# allow operators to disable the primitive at boot. Default-false until
# Slice 5 graduation.)
# ---------------------------------------------------------------------------


def invariant_drift_auditor_enabled() -> bool:
    """``JARVIS_INVARIANT_DRIFT_AUDITOR_ENABLED`` (**graduated 2026-04-30
    Slice 5 — default ``true``**).

    Asymmetric semantics mirror the ``shipped_code_invariants``
    pattern: empty/whitespace = unset = *current* default
    (post-graduation = ``true``); explicit ``0`` / ``false`` /
    ``no`` / ``off`` hot-reverts. Re-read on every public-API
    entry so flips take effect without restart.
    """
    raw = os.environ.get(
        "JARVIS_INVARIANT_DRIFT_AUDITOR_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return True  # graduated default — Slice 5
    return raw in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Severity + drift kind enums (J.A.R.M.A.T.R.I.X. — explicit state space)
# ---------------------------------------------------------------------------


class DriftSeverity(str, enum.Enum):
    """How much a single drift record matters.

    ``CRITICAL`` — *safety property weakened*. An invariant was
    removed, a violation was introduced, an exploration floor was
    lowered, or a required category was dropped. Operator-actionable.

    ``WARNING`` — *meaningful but possibly-benign change*. Flag
    registry hash drifted (could be a legitimate addition or a
    default-flip — caller decides), or violation signature changed
    at the same count.

    ``INFO`` — *known-OK informational drift*. Posture moved;
    posture transitions are normal organism behavior, surfaced
    here only for operator situational awareness.
    """

    CRITICAL = "critical"
    WARNING = "warning"
    INFO = "info"


class DriftKind(str, enum.Enum):
    """Closed taxonomy of detectable drifts. Every drift maps to
    exactly one of these — never None, never an unclassified bucket.

    Mirrors the ``AdvisoryActionType`` 5-value-explicit discipline
    from Move 3."""

    SHIPPED_INVARIANT_REMOVED = "shipped_invariant_removed"
    SHIPPED_VIOLATION_INTRODUCED = "shipped_violation_introduced"
    SHIPPED_VIOLATION_SIGNATURE_CHANGED = (
        "shipped_violation_signature_changed"
    )
    FLAG_REGISTRY_HASH_CHANGED = "flag_registry_hash_changed"
    FLAG_REGISTRY_COUNT_DECREASED = "flag_registry_count_decreased"
    EXPLORATION_FLOOR_LOWERED = "exploration_floor_lowered"
    EXPLORATION_REQUIRED_CATEGORY_DROPPED = (
        "exploration_required_category_dropped"
    )
    EXPLORATION_BUCKET_REMOVED = "exploration_bucket_removed"
    POSTURE_DRIFT = "posture_drift"
    # Cascading state vector fix (2026-05-01): long-horizon semantic
    # gradient drift. Individual per-op changes may each pass all
    # discrete checks (SemanticGuardian + Iron Gate + Quorum), but
    # cumulative small shifts between snapshot points can regress an
    # invariant surface by 100% with zero alarms. This kind is
    # emitted by the observer's gradient tracker, NOT by the
    # compare_snapshots() primitive (which remains a pure binary
    # comparison between two snapshots).
    GRADIENT_DRIFT_DETECTED = "gradient_drift_detected"


# ---------------------------------------------------------------------------
# Frozen dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExplorationFloorPin:
    """Per-complexity exploration-floor snapshot. Floor values are
    captured as-of-snapshot — the comparison engine treats *any
    decrease* in ``min_score`` or ``min_categories`` as CRITICAL,
    and any drop from ``required_categories`` as CRITICAL."""

    complexity: str
    min_score: float
    min_categories: int
    required_categories: Tuple[str, ...]  # sorted, frozen-tuple

    def to_dict(self) -> Dict[str, Any]:
        return {
            "complexity": self.complexity,
            "min_score": self.min_score,
            "min_categories": self.min_categories,
            "required_categories": list(self.required_categories),
        }

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, Any],
    ) -> Optional["ExplorationFloorPin"]:
        """Reconstruct from a ``to_dict`` payload. Returns ``None`` on
        any malformed shape — defensive contract for cross-session
        load paths. NEVER raises."""
        try:
            return cls(
                complexity=str(payload["complexity"]),
                min_score=float(payload["min_score"]),
                min_categories=int(payload["min_categories"]),
                required_categories=tuple(
                    str(c) for c in (
                        payload.get("required_categories") or ()
                    )
                ),
            )
        except (KeyError, TypeError, ValueError):
            return None


@dataclass(frozen=True)
class InvariantSnapshot:
    """One frozen capture of architectural invariants at a point in
    time. Comparison-friendly: every nested collection is a tuple
    (frozen-hashable) and all primitives are stable across processes."""

    snapshot_id: str
    captured_at_utc: float

    # 1. Shipped-code invariants
    shipped_invariant_names: Tuple[str, ...]  # sorted
    shipped_violation_signature: str  # sha256 hex
    shipped_violation_count: int

    # 2. Flag registry — hash of every flag's (name, default) pair.
    flag_registry_hash: str  # sha256 hex
    flag_count: int

    # 3. Exploration floors — per-complexity pin tuple
    exploration_floor_pins: Tuple[ExplorationFloorPin, ...]

    # 4. Posture (informational)
    posture_value: Optional[str]
    posture_confidence: Optional[float]

    schema_version: str = INVARIANT_DRIFT_AUDITOR_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "captured_at_utc": self.captured_at_utc,
            "shipped_invariant_names": list(self.shipped_invariant_names),
            "shipped_violation_signature": self.shipped_violation_signature,
            "shipped_violation_count": self.shipped_violation_count,
            "flag_registry_hash": self.flag_registry_hash,
            "flag_count": self.flag_count,
            "exploration_floor_pins": [
                p.to_dict() for p in self.exploration_floor_pins
            ],
            "posture_value": self.posture_value,
            "posture_confidence": self.posture_confidence,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, Any],
    ) -> Optional["InvariantSnapshot"]:
        """Reconstruct from a ``to_dict`` payload. Returns ``None`` on
        schema mismatch OR any malformed field — caller treats as
        "no baseline" and re-captures. Mirrors PostureStore's
        ``reading_from_json`` discipline. NEVER raises."""
        try:
            schema = payload.get("schema_version")
            if schema != INVARIANT_DRIFT_AUDITOR_SCHEMA_VERSION:
                logger.warning(
                    "[InvariantDriftAuditor] schema mismatch: got "
                    "%r, want %r; treating as no-baseline",
                    schema, INVARIANT_DRIFT_AUDITOR_SCHEMA_VERSION,
                )
                return None
            pins_raw = payload.get("exploration_floor_pins") or ()
            pins: List[ExplorationFloorPin] = []
            for raw in pins_raw:
                if not isinstance(raw, Mapping):
                    return None
                pin = ExplorationFloorPin.from_dict(raw)
                if pin is None:
                    return None
                pins.append(pin)
            posture_conf_raw = payload.get("posture_confidence")
            posture_conf = (
                float(posture_conf_raw)
                if posture_conf_raw is not None else None
            )
            posture_value_raw = payload.get("posture_value")
            posture_value = (
                str(posture_value_raw)
                if posture_value_raw is not None else None
            )
            return cls(
                snapshot_id=str(payload["snapshot_id"]),
                captured_at_utc=float(payload["captured_at_utc"]),
                shipped_invariant_names=tuple(
                    str(n) for n in (
                        payload.get("shipped_invariant_names") or ()
                    )
                ),
                shipped_violation_signature=str(
                    payload.get("shipped_violation_signature", "")
                ),
                shipped_violation_count=int(
                    payload.get("shipped_violation_count", 0)
                ),
                flag_registry_hash=str(
                    payload.get("flag_registry_hash", "")
                ),
                flag_count=int(payload.get("flag_count", 0)),
                exploration_floor_pins=tuple(pins),
                posture_value=posture_value,
                posture_confidence=posture_conf,
            )
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning(
                "[InvariantDriftAuditor] malformed snapshot "
                "payload: %s", exc,
            )
            return None


@dataclass(frozen=True)
class InvariantDriftRecord:
    """One drift between two snapshots. Frozen so it can be safely
    propagated through the auto_action_router signal bridge (Slice 4)
    without aliasing concerns."""

    drift_kind: DriftKind
    severity: DriftSeverity
    detail: str
    # Optional structured fields — caller surfaces (REPL, SSE) can
    # render either ``detail`` or these fields. Frozen tuples to keep
    # the dataclass hashable.
    affected_keys: Tuple[str, ...] = ()
    schema_version: str = INVARIANT_DRIFT_AUDITOR_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "drift_kind": self.drift_kind.value,
            "severity": self.severity.value,
            "detail": self.detail,
            "affected_keys": list(self.affected_keys),
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, Any],
    ) -> Optional["InvariantDriftRecord"]:
        """Reconstruct from a ``to_dict`` payload. Returns ``None`` on
        unknown enum value OR malformed shape. NEVER raises."""
        try:
            kind = DriftKind(payload["drift_kind"])
            severity = DriftSeverity(payload["severity"])
            return cls(
                drift_kind=kind,
                severity=severity,
                detail=str(payload.get("detail", "")),
                affected_keys=tuple(
                    str(k) for k in (
                        payload.get("affected_keys") or ()
                    )
                ),
            )
        except (KeyError, TypeError, ValueError):
            return None


# ---------------------------------------------------------------------------
# Capture — read live process state defensively
# ---------------------------------------------------------------------------


def _hash_pairs(pairs: List[Tuple[str, str]]) -> str:
    """Deterministic sha256 over a sorted list of ``(key, value)``
    string pairs. Uses JSON encoding for value-shape stability across
    Python versions (``repr`` could change). NEVER raises."""
    try:
        canonical = json.dumps(
            sorted(pairs), sort_keys=True, ensure_ascii=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(
            canonical.encode("utf-8"),
        ).hexdigest()
    except Exception:  # noqa: BLE001 — defensive
        return ""


def _capture_shipped_invariants() -> Tuple[
    Tuple[str, ...], str, int,
]:
    """Returns ``(sorted_names, violation_signature_hex, violation_count)``.

    ``validate_all`` is called once; violations are hashed by
    ``(invariant_name, target_file, detail)`` so signature change
    detects "different violations at same count" too.

    NEVER raises — degrades to ``((), "", 0)``."""
    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            list_shipped_code_invariants,
            validate_all,
        )
    except Exception:  # noqa: BLE001 — defensive (module optional)
        return ((), "", 0)
    try:
        invariants = list_shipped_code_invariants()
        names: Tuple[str, ...] = tuple(
            sorted(inv.invariant_name for inv in invariants)
        )
    except Exception:  # noqa: BLE001 — defensive
        names = ()
    try:
        violations = validate_all()
        pairs: List[Tuple[str, str]] = []
        for v in violations:
            name = getattr(v, "invariant_name", "")
            target = getattr(v, "target_file", "")
            detail = getattr(v, "detail", "")
            pairs.append((str(name) + ":" + str(target), str(detail)))
        return (names, _hash_pairs(pairs), len(violations))
    except Exception:  # noqa: BLE001 — defensive
        return (names, "", 0)


def _capture_flag_registry() -> Tuple[str, int]:
    """Returns ``(registry_hash_hex, flag_count)``. Hash covers every
    flag's ``(name, str(default))`` pair so a default-flip is detected.

    Type stays implicit: a flag-type change without a name change is
    a code refactor that the FlagRegistry author will see; for drift
    detection we want the *behavioral* baseline (the default value).

    NEVER raises — degrades to ``("", 0)``."""
    try:
        from backend.core.ouroboros.governance.flag_registry import (
            get_default_registry,
        )
    except Exception:  # noqa: BLE001 — defensive
        return ("", 0)
    try:
        registry = get_default_registry()
        specs = registry.list_all()
    except Exception:  # noqa: BLE001 — defensive
        return ("", 0)
    pairs: List[Tuple[str, str]] = []
    for spec in specs:
        try:
            pairs.append(
                (str(getattr(spec, "name", "")),
                 repr(getattr(spec, "default", None))),
            )
        except Exception:  # noqa: BLE001 — defensive per-spec
            continue
    return (_hash_pairs(pairs), len(specs))


def _exploration_complexity_buckets() -> Tuple[str, ...]:
    """Discover complexity bucket keys *at runtime* from
    ``exploration_engine._DEFAULT_FLOORS`` rather than hardcoding
    them. If the bucket map is unavailable, returns ``()`` and
    capture falls through gracefully.

    NEVER raises."""
    try:
        from backend.core.ouroboros.governance.exploration_engine import (  # noqa: E501
            _DEFAULT_FLOORS,
        )
    except Exception:  # noqa: BLE001 — defensive
        return ()
    try:
        if not isinstance(_DEFAULT_FLOORS, Mapping):
            return ()
        return tuple(sorted(str(k) for k in _DEFAULT_FLOORS.keys()))
    except Exception:  # noqa: BLE001 — defensive
        return ()


def _capture_exploration_floors() -> Tuple[ExplorationFloorPin, ...]:
    """Snapshot ``ExplorationFloors`` for every known complexity
    bucket. Reads via ``from_env_with_adapted`` so adapted floors
    (Pass C tightenings) are visible to the auditor — that's the
    behavior an operator wants to pin against drift.

    NEVER raises — per-bucket failures are swallowed; the resulting
    tuple may be shorter than the bucket list."""
    buckets = _exploration_complexity_buckets()
    if not buckets:
        return ()
    try:
        from backend.core.ouroboros.governance.exploration_engine import (
            ExplorationFloors,
        )
    except Exception:  # noqa: BLE001 — defensive
        return ()
    pins: List[ExplorationFloorPin] = []
    for bucket in buckets:
        try:
            floors = ExplorationFloors.from_env_with_adapted(bucket)
        except Exception:  # noqa: BLE001 — defensive per-bucket
            continue
        try:
            req: Tuple[str, ...] = tuple(
                sorted(
                    getattr(c, "value", str(c))
                    for c in (floors.required_categories or ())
                )
            )
            pins.append(
                ExplorationFloorPin(
                    complexity=str(floors.complexity),
                    min_score=float(floors.min_score),
                    min_categories=int(floors.min_categories),
                    required_categories=req,
                ),
            )
        except Exception:  # noqa: BLE001 — defensive per-pin
            continue
    return tuple(pins)


def _capture_posture() -> Tuple[Optional[str], Optional[float]]:
    """Read the current ``PostureReading`` via the default
    ``PostureStore``. Returns ``(posture_value, confidence)`` or
    ``(None, None)`` when no current reading exists.

    NEVER raises."""
    try:
        from backend.core.ouroboros.governance.posture_observer import (
            get_default_store,
        )
    except Exception:  # noqa: BLE001 — defensive
        return (None, None)
    try:
        store = get_default_store()
        reading = store.load_current()
    except Exception:  # noqa: BLE001 — defensive
        return (None, None)
    if reading is None:
        return (None, None)
    try:
        posture_attr = getattr(reading, "posture", None)
        value = (
            getattr(posture_attr, "value", None)
            if posture_attr is not None else None
        )
        conf = getattr(reading, "confidence", None)
        return (
            str(value) if value is not None else None,
            float(conf) if conf is not None else None,
        )
    except Exception:  # noqa: BLE001 — defensive
        return (None, None)


def capture_snapshot(
    *,
    snapshot_id: Optional[str] = None,
    now: Optional[float] = None,
) -> InvariantSnapshot:
    """Build a fresh ``InvariantSnapshot`` from live process state.

    Defensive everywhere — every individual surface failure is
    swallowed and replaced with a sentinel (empty tuple / 0 / "" /
    None). NEVER raises.

    ``snapshot_id`` defaults to a fresh uuid4; ``now`` defaults to
    ``time.time()``. Both injectable for deterministic tests."""
    sid = snapshot_id if snapshot_id else str(uuid.uuid4())
    captured_at = float(now) if now is not None else time.time()

    names, vio_sig, vio_count = _capture_shipped_invariants()
    flag_hash, flag_count = _capture_flag_registry()
    floor_pins = _capture_exploration_floors()
    posture_value, posture_conf = _capture_posture()

    return InvariantSnapshot(
        snapshot_id=sid,
        captured_at_utc=captured_at,
        shipped_invariant_names=names,
        shipped_violation_signature=vio_sig,
        shipped_violation_count=vio_count,
        flag_registry_hash=flag_hash,
        flag_count=flag_count,
        exploration_floor_pins=floor_pins,
        posture_value=posture_value,
        posture_confidence=posture_conf,
    )


# ---------------------------------------------------------------------------
# Comparison engine — pure, deterministic, total
# ---------------------------------------------------------------------------


def _compare_shipped_invariants(
    a: InvariantSnapshot, b: InvariantSnapshot,
) -> List[InvariantDriftRecord]:
    out: List[InvariantDriftRecord] = []
    a_names = set(a.shipped_invariant_names)
    b_names = set(b.shipped_invariant_names)
    removed = sorted(a_names - b_names)
    if removed:
        out.append(
            InvariantDriftRecord(
                drift_kind=DriftKind.SHIPPED_INVARIANT_REMOVED,
                severity=DriftSeverity.CRITICAL,
                detail=(
                    f"shipped invariants removed since baseline "
                    f"({len(removed)}): "
                    f"{', '.join(removed)}"
                ),
                affected_keys=tuple(removed),
            ),
        )
    if a.shipped_violation_count == 0 and b.shipped_violation_count > 0:
        out.append(
            InvariantDriftRecord(
                drift_kind=DriftKind.SHIPPED_VIOLATION_INTRODUCED,
                severity=DriftSeverity.CRITICAL,
                detail=(
                    f"shipped invariant violations introduced: "
                    f"{a.shipped_violation_count} -> "
                    f"{b.shipped_violation_count}"
                ),
            ),
        )
    elif (
        a.shipped_violation_count > 0
        and b.shipped_violation_count > 0
        and a.shipped_violation_signature
        and b.shipped_violation_signature
        and a.shipped_violation_signature
        != b.shipped_violation_signature
    ):
        out.append(
            InvariantDriftRecord(
                drift_kind=(
                    DriftKind.SHIPPED_VIOLATION_SIGNATURE_CHANGED
                ),
                severity=DriftSeverity.WARNING,
                detail=(
                    f"shipped invariant violation signature changed "
                    f"(count {a.shipped_violation_count} -> "
                    f"{b.shipped_violation_count}); different "
                    f"violations may now be present"
                ),
            ),
        )
    return out


def _compare_flag_registry(
    a: InvariantSnapshot, b: InvariantSnapshot,
) -> List[InvariantDriftRecord]:
    out: List[InvariantDriftRecord] = []
    if (
        a.flag_registry_hash
        and b.flag_registry_hash
        and a.flag_registry_hash != b.flag_registry_hash
    ):
        out.append(
            InvariantDriftRecord(
                drift_kind=DriftKind.FLAG_REGISTRY_HASH_CHANGED,
                severity=DriftSeverity.WARNING,
                detail=(
                    f"flag registry hash changed; flag count "
                    f"{a.flag_count} -> {b.flag_count} (a default "
                    f"may have flipped or a flag added/renamed)"
                ),
            ),
        )
    if b.flag_count < a.flag_count:
        out.append(
            InvariantDriftRecord(
                drift_kind=DriftKind.FLAG_REGISTRY_COUNT_DECREASED,
                severity=DriftSeverity.CRITICAL,
                detail=(
                    f"flag registry count decreased: "
                    f"{a.flag_count} -> {b.flag_count} (a flag was "
                    f"removed — operator-actionable)"
                ),
            ),
        )
    return out


def _compare_exploration_floors(
    a: InvariantSnapshot, b: InvariantSnapshot,
) -> List[InvariantDriftRecord]:
    out: List[InvariantDriftRecord] = []
    a_by_complexity: Dict[str, ExplorationFloorPin] = {
        p.complexity: p for p in a.exploration_floor_pins
    }
    b_by_complexity: Dict[str, ExplorationFloorPin] = {
        p.complexity: p for p in b.exploration_floor_pins
    }
    removed_buckets = sorted(
        set(a_by_complexity) - set(b_by_complexity)
    )
    if removed_buckets:
        out.append(
            InvariantDriftRecord(
                drift_kind=DriftKind.EXPLORATION_BUCKET_REMOVED,
                severity=DriftSeverity.CRITICAL,
                detail=(
                    f"exploration complexity bucket(s) removed: "
                    f"{', '.join(removed_buckets)}"
                ),
                affected_keys=tuple(removed_buckets),
            ),
        )
    for complexity in sorted(a_by_complexity.keys() & b_by_complexity.keys()):
        pin_a = a_by_complexity[complexity]
        pin_b = b_by_complexity[complexity]
        if pin_b.min_score < pin_a.min_score:
            out.append(
                InvariantDriftRecord(
                    drift_kind=DriftKind.EXPLORATION_FLOOR_LOWERED,
                    severity=DriftSeverity.CRITICAL,
                    detail=(
                        f"exploration min_score lowered for "
                        f"{complexity!r}: {pin_a.min_score:.2f} -> "
                        f"{pin_b.min_score:.2f}"
                    ),
                    affected_keys=(complexity,),
                ),
            )
        if pin_b.min_categories < pin_a.min_categories:
            out.append(
                InvariantDriftRecord(
                    drift_kind=DriftKind.EXPLORATION_FLOOR_LOWERED,
                    severity=DriftSeverity.CRITICAL,
                    detail=(
                        f"exploration min_categories lowered for "
                        f"{complexity!r}: {pin_a.min_categories} -> "
                        f"{pin_b.min_categories}"
                    ),
                    affected_keys=(complexity,),
                ),
            )
        a_req = set(pin_a.required_categories)
        b_req = set(pin_b.required_categories)
        dropped = sorted(a_req - b_req)
        if dropped:
            out.append(
                InvariantDriftRecord(
                    drift_kind=(
                        DriftKind.EXPLORATION_REQUIRED_CATEGORY_DROPPED
                    ),
                    severity=DriftSeverity.CRITICAL,
                    detail=(
                        f"exploration required_categories dropped "
                        f"for {complexity!r}: {', '.join(dropped)}"
                    ),
                    affected_keys=tuple([complexity, *dropped]),
                ),
            )
    return out


def _compare_posture(
    a: InvariantSnapshot, b: InvariantSnapshot,
) -> List[InvariantDriftRecord]:
    if a.posture_value is None or b.posture_value is None:
        return []
    if a.posture_value == b.posture_value:
        return []
    return [
        InvariantDriftRecord(
            drift_kind=DriftKind.POSTURE_DRIFT,
            severity=DriftSeverity.INFO,
            detail=(
                f"posture moved {a.posture_value} -> "
                f"{b.posture_value} (informational; postures "
                f"legitimately drift)"
            ),
            affected_keys=(a.posture_value, b.posture_value),
        ),
    ]


def compare_snapshots(
    baseline: InvariantSnapshot,
    current: InvariantSnapshot,
) -> Tuple[InvariantDriftRecord, ...]:
    """Pure, deterministic, total comparison. Returns drift records
    found going from ``baseline`` -> ``current``. Empty tuple == no
    drift detected.

    NEVER raises — defensive even on malformed snapshots (the public
    constructors guarantee shape, but operator code may construct
    snapshots from on-disk JSON whose schema drifted).

    Ordering of returned records:
      1. shipped-invariant drift first (highest-trust signal)
      2. flag-registry drift
      3. exploration-floor drift
      4. posture drift last (informational)
    Within each group, deterministic order (sorted keys)."""
    if not isinstance(baseline, InvariantSnapshot) or not isinstance(
        current, InvariantSnapshot,
    ):
        return ()
    out: List[InvariantDriftRecord] = []
    try:
        out.extend(_compare_shipped_invariants(baseline, current))
    except Exception:  # noqa: BLE001 — defensive
        logger.debug(
            "[InvariantDriftAuditor] shipped-invariant compare raised",
            exc_info=True,
        )
    try:
        out.extend(_compare_flag_registry(baseline, current))
    except Exception:  # noqa: BLE001 — defensive
        logger.debug(
            "[InvariantDriftAuditor] flag-registry compare raised",
            exc_info=True,
        )
    try:
        out.extend(_compare_exploration_floors(baseline, current))
    except Exception:  # noqa: BLE001 — defensive
        logger.debug(
            "[InvariantDriftAuditor] exploration-floor compare raised",
            exc_info=True,
        )
    try:
        out.extend(_compare_posture(baseline, current))
    except Exception:  # noqa: BLE001 — defensive
        logger.debug(
            "[InvariantDriftAuditor] posture compare raised",
            exc_info=True,
        )
    return tuple(out)


# ---------------------------------------------------------------------------
# Filtering helpers — kept intentionally lightweight; richer query
# surfaces are Slice 5 territory (REPL / GET /observability/trajectory).
# ---------------------------------------------------------------------------


def filter_by_severity(
    records: Tuple[InvariantDriftRecord, ...],
    *,
    minimum: DriftSeverity = DriftSeverity.WARNING,
) -> Tuple[InvariantDriftRecord, ...]:
    """Return only records whose severity is at-or-above ``minimum``.
    CRITICAL > WARNING > INFO. NEVER raises."""
    order = {
        DriftSeverity.CRITICAL: 2,
        DriftSeverity.WARNING: 1,
        DriftSeverity.INFO: 0,
    }
    threshold = order.get(minimum, 1)
    return tuple(
        r for r in records
        if order.get(r.severity, 0) >= threshold
    )


def has_critical_drift(
    records: Tuple[InvariantDriftRecord, ...],
) -> bool:
    """True iff any record is CRITICAL. NEVER raises."""
    return any(r.severity is DriftSeverity.CRITICAL for r in records)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = [
    "DriftKind",
    "DriftSeverity",
    "ExplorationFloorPin",
    "INVARIANT_DRIFT_AUDITOR_SCHEMA_VERSION",
    "InvariantDriftRecord",
    "InvariantSnapshot",
    "capture_snapshot",
    "compare_snapshots",
    "filter_by_severity",
    "has_critical_drift",
    "invariant_drift_auditor_enabled",
]
