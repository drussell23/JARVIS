"""Priority #1 Slice 1 — Long-Horizon Semantic Coherence Auditor primitive.

The single highest-impact RSI-safety upgrade post-Move-6. Closes the
**gestalt-rotation blind spot**: Move 4's ``InvariantDriftAuditor``
detects *structural* drift (a shipped invariant changed since boot
snapshot); this auditor detects *behavioral* drift — the system's
behavioral mass moving in a direction it shouldn't, integrated over
a rolling window.

Without temporal drift detection, RSI loops have no halting condition
rooted in their own behavior. Anti-Venom's mathematical safety
guarantees become per-op only — the *integral* over many ops can
still drift unbounded. This primitive is the measurement instrument.

Slice 1 ships the **primitive layer only** — pure data + pure
compute. No I/O, no async, no governance imports. Slice 2 adds the
cross-process flock'd window store; Slice 3 the posture-aware async
observer; Slice 4 the auto_action_router bridge with monotonic-
tightening contract; Slice 5 the graduation + operator surfaces.

Direct-solve principles (per the operator directive):

  * **Asynchronous-ready** — frozen dataclasses propagate cleanly
    across async boundaries (Slice 3's observer task and Slice 4's
    bridge will round-trip ``BehavioralSignature`` /
    ``BehavioralDriftVerdict`` through ``asyncio.to_thread`` and
    SSE serialization).

  * **Dynamic** — every numeric threshold is env-tunable with floor
    + ceiling clamps. NO hardcoded magic constants in behavior
    logic. Per-kind drift budgets are independently adjustable.

  * **Adaptive** — degraded inputs (single signature window, all-
    empty distributions, missing apply event manifest) all map to
    explicit ``CoherenceOutcome`` values rather than raises.
    INSUFFICIENT_DATA is a first-class outcome — Slice 3 observer
    wakes up earlier than the window's first natural comparison
    and gracefully suppresses verdicts.

  * **Intelligent** — distribution comparison via total-variation
    distance (a true distance metric, not a ratio); recency-
    weighted aggregation via the same halflife-decay formula
    SemanticIndex uses for centroid weighting (formula parity
    pinned by companion test rather than transitive import — keeps
    this module pure-stdlib).

  * **Robust** — every public function is total: every input maps
    to exactly one ``BehavioralDriftVerdict``. Garbage input,
    schema mismatch, type errors — all collapse to FAILED outcome
    or empty signature, never raises.

  * **No hardcoding** — 6-value ``BehavioralDriftKind`` is a closed
    taxonomy enum (J.A.R.M.A.T.R.I.X. — every drift maps to exactly
    one). 5-value ``CoherenceOutcome`` is closed. 4-value
    ``DriftSeverity`` is closed. Per-kind budgets are caller-
    supplied via ``DriftBudgets`` dataclass with env-knob defaults.

Authority invariants (AST-pinned by Slice 5 graduation):

  * Imports stdlib ONLY. NEVER imports any governance module —
    not even Move 6 Slice 2's ``ast_canonical`` (the observer
    in Slice 3 uses ast_canonical to compute fingerprints; Slice 1
    receives the precomputed fingerprint strings as input).
    Strongest possible authority invariant: pure-stdlib means
    zero attack surface from upstream module changes.
  * NEVER imports orchestrator / phase_runners / iron_gate /
    change_engine / policy / candidate_generator / providers /
    doubleword_provider / urgency_router / auto_action_router /
    subagent_scheduler / tool_executor / semantic_guardian /
    semantic_firewall.
  * No async functions in Slice 1 (Slice 3 introduces async).
  * Read-only — never writes a file, never executes code,
    never imports SemanticIndex (heavy dep). The recency-decay
    formula is reimplemented inline; equivalence pinned by test.
  * No mutation tools referenced anywhere.
  * No exec/eval/compile (mirrors Move 6 Slice 2's critical
    safety pin — auditor never executes shipped code; only
    compares fingerprints).

Master flag default-false until Slice 5 graduation:
``JARVIS_COHERENCE_AUDITOR_ENABLED``. Asymmetric env semantics —
empty/whitespace = unset = current default; explicit truthy/falsy
overrides at call time.

The 6-value BehavioralDriftKind taxonomy:

  * ``BEHAVIORAL_ROUTE_DRIFT`` — route distribution (immediate /
    standard / complex / background / speculative) rotated by
    > total-variation budget. Cost profile silently shifting.
  * ``POSTURE_LOCKED`` — StrategicPosture stuck in same value
    > threshold consecutive hours. Wrong posture biases every
    downstream decision.
  * ``SYMBOL_FLUX_DRIFT`` — tracked module's AST signature
    changed AND no APPLY event in the window touched that
    module's path. Off-graduation behavioral evolution.
  * ``POLICY_DEFAULT_DRIFT`` — env-flag default observed at
    runtime differs from FlagRegistry-registered default.
    Silent policy shift.
  * ``RECURRENCE_DRIFT`` — same `failure_class` postmortem
    appeared > threshold times in window. Forward-compat
    signal Priority #2 PostmortemRecall consumes.
  * ``CONFIDENCE_DRIFT`` — p99 confidence-drop count rising
    window-over-window > rise budget. Latent-space stability
    eroding.
"""
from __future__ import annotations

import enum
import hashlib
import logging
import os
from dataclasses import dataclass, field
from typing import (
    Any,
    Dict,
    Mapping,
    Optional,
    Tuple,
)

logger = logging.getLogger(__name__)


COHERENCE_AUDITOR_SCHEMA_VERSION: str = "coherence_auditor.1"


# ---------------------------------------------------------------------------
# Master flag — asymmetric env semantics
# ---------------------------------------------------------------------------


def coherence_auditor_enabled() -> bool:
    """``JARVIS_COHERENCE_AUDITOR_ENABLED`` (default ``false``
    until Slice 5 graduation).

    Asymmetric env semantics — empty/whitespace = unset = current
    default; explicit ``0``/``false``/``no``/``off`` evaluates
    false; explicit truthy values evaluate true. Re-read on every
    call so flips hot-revert without restart."""
    raw = os.environ.get(
        "JARVIS_COHERENCE_AUDITOR_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return False  # default-false until Slice 5 graduation
    return raw in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Env-knob helpers — every numeric is floor+ceiling clamped
# ---------------------------------------------------------------------------


def _env_float_clamped(
    name: str, default: float, *, floor: float, ceiling: float,
) -> float:
    """Read an env var as float; clamp to [floor, ceiling]; fall
    back to default on missing / garbage. NEVER raises."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        v = float(raw)
        return min(ceiling, max(floor, v))
    except (TypeError, ValueError):
        return default


def _env_int_clamped(
    name: str, default: int, *, floor: int, ceiling: int,
) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        v = int(raw)
        return min(ceiling, max(floor, v))
    except (TypeError, ValueError):
        return default


# Per-kind drift budgets — defaults from scope §"Knobs" table.
# Cap structure: ``min(ceiling, max(floor, value))`` enforces
# structural safety. Operators cannot loosen below floor.


def budget_route_drift_pct() -> float:
    """``JARVIS_COHERENCE_BUDGET_ROUTE_DRIFT_PCT`` (default 25.0,
    floor 5.0, ceiling 100.0).

    Total-variation distance threshold for route distribution
    drift, expressed as percentage (0–100). 25.0 means a drift is
    flagged when ``0.5 * sum(|prev[r] - curr[r]|) > 0.25``."""
    return _env_float_clamped(
        "JARVIS_COHERENCE_BUDGET_ROUTE_DRIFT_PCT",
        25.0, floor=5.0, ceiling=100.0,
    )


def budget_posture_locked_hours() -> float:
    """``JARVIS_COHERENCE_BUDGET_POSTURE_LOCKED_HOURS`` (default
    48.0, floor 24.0, ceiling 168.0)."""
    return _env_float_clamped(
        "JARVIS_COHERENCE_BUDGET_POSTURE_LOCKED_HOURS",
        48.0, floor=24.0, ceiling=168.0,
    )


def budget_recurrence_count() -> int:
    """``JARVIS_COHERENCE_BUDGET_RECURRENCE_COUNT`` (default 3,
    floor 2, ceiling 50)."""
    return _env_int_clamped(
        "JARVIS_COHERENCE_BUDGET_RECURRENCE_COUNT",
        3, floor=2, ceiling=50,
    )


def budget_confidence_rise_pct() -> float:
    """``JARVIS_COHERENCE_BUDGET_CONFIDENCE_RISE_PCT`` (default
    50.0, floor 10.0, ceiling 500.0).

    Window-over-window p99 confidence-drop count rise threshold,
    expressed as percentage. 50.0 means flag when curr_p99 >=
    1.5 × prev_p99."""
    return _env_float_clamped(
        "JARVIS_COHERENCE_BUDGET_CONFIDENCE_RISE_PCT",
        50.0, floor=10.0, ceiling=500.0,
    )


def halflife_days() -> float:
    """``JARVIS_COHERENCE_HALFLIFE_DAYS`` (default 14.0, floor
    0.5, ceiling 90.0).

    Mirrors SemanticIndex's 14-day default for non-conversation
    decay. Within a coherence window, older signatures and ops
    decay at this halflife. Formula parity with
    ``semantic_index._recency_weight`` is pinned by companion
    test (literal byte-equivalence)."""
    return _env_float_clamped(
        "JARVIS_COHERENCE_HALFLIFE_DAYS",
        14.0, floor=0.5, ceiling=90.0,
    )


# ---------------------------------------------------------------------------
# Closed 6-value taxonomy of behavioral drift kinds (J.A.R.M.A.T.R.I.X.)
# ---------------------------------------------------------------------------


class BehavioralDriftKind(str, enum.Enum):
    """6-value closed taxonomy. DISTINCT from Move 4's 9-value
    structural ``DriftKind`` — different vocabulary, different
    schema, different semantics. Behavioral drift is the
    *integral* of system behavior; structural drift is the
    *discrete moment* an invariant violates."""

    BEHAVIORAL_ROUTE_DRIFT = "behavioral_route_drift"
    POSTURE_LOCKED = "posture_locked"
    SYMBOL_FLUX_DRIFT = "symbol_flux_drift"
    POLICY_DEFAULT_DRIFT = "policy_default_drift"
    RECURRENCE_DRIFT = "recurrence_drift"
    CONFIDENCE_DRIFT = "confidence_drift"


# ---------------------------------------------------------------------------
# Closed 5-value taxonomy of coherence outcomes
# ---------------------------------------------------------------------------


class CoherenceOutcome(str, enum.Enum):
    """5-value closed enum (J.A.R.M.A.T.R.I.X.). Every input
    maps to exactly one — never None, never implicit fall-through.
    Mirrors Move 4 ``DriftAuditOutcome`` / Move 5 ``ProbeOutcome``
    / Move 6 ``ConsensusOutcome`` discipline.

    ``COHERENT``           — Within budget on every kind.
    ``DRIFT_DETECTED``     — At least one finding crossed budget.
    ``INSUFFICIENT_DATA``  — Window too short for comparison
                             (first signature, only one signature
                             in window).
    ``DISABLED``           — Master flag off.
    ``FAILED``             — Defensive sentinel."""

    COHERENT = "coherent"
    DRIFT_DETECTED = "drift_detected"
    INSUFFICIENT_DATA = "insufficient_data"
    DISABLED = "disabled"
    FAILED = "failed"


# ---------------------------------------------------------------------------
# Closed 4-value severity taxonomy
# ---------------------------------------------------------------------------


class DriftSeverity(str, enum.Enum):
    """4-value closed taxonomy. Severity is delta_metric / budget:
      * ``NONE``    — no drift
      * ``LOW``     — 1.0 ≤ ratio < 1.5
      * ``MEDIUM``  — 1.5 ≤ ratio < 3.0
      * ``HIGH``    — ratio ≥ 3.0"""

    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


_SEVERITY_RANK: Dict[DriftSeverity, int] = {
    DriftSeverity.NONE: 0,
    DriftSeverity.LOW: 1,
    DriftSeverity.MEDIUM: 2,
    DriftSeverity.HIGH: 3,
}


def _severity_for_ratio(ratio: float) -> DriftSeverity:
    """Map a delta/budget ratio to severity. NEVER raises.

    NaN ratio → NONE (signal-less, conservative). Negative ratio
    → NONE. Otherwise: <1.0 NONE, <1.5 LOW, <3.0 MEDIUM, ≥3.0
    HIGH."""
    try:
        # NaN check — all comparisons with NaN return False so a
        # plain fall-through-to-HIGH would be incorrect. NaN is
        # treated as "no usable signal" → NONE.
        if ratio != ratio:  # NaN check (NaN != NaN)
            return DriftSeverity.NONE
        if ratio < 1.0:
            return DriftSeverity.NONE
        if ratio < 1.5:
            return DriftSeverity.LOW
        if ratio < 3.0:
            return DriftSeverity.MEDIUM
        return DriftSeverity.HIGH
    except Exception:  # noqa: BLE001 — defensive
        return DriftSeverity.NONE


# ---------------------------------------------------------------------------
# Frozen dataclasses — propagation-safe across async + lock boundaries
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OpRecord:
    """One observed op event within a coherence window. Slice 3
    populates from ``phase_capture`` artifacts."""

    op_id: str
    route: str  # immediate / standard / complex / background / speculative
    ts: float


@dataclass(frozen=True)
class PostureRecord:
    """One posture observation. Slice 3 populates from
    ``posture_history.jsonl`` (Wave 1 #1)."""

    posture: str  # explore / consolidate / harden / maintain
    ts: float


@dataclass(frozen=True)
class WindowData:
    """Pre-collected window data. Slice 3's observer populates
    this from ``phase_capture`` + ``posture_history.jsonl`` +
    latest ``summary.json`` + AST-fingerprinted modules.

    Slice 1 is opaque to the source — it accepts pre-aggregated
    structured input. This decouples the primitive from the
    collection mechanism."""

    window_start_ts: float
    window_end_ts: float
    op_records: Tuple[OpRecord, ...] = field(default_factory=tuple)
    posture_records: Tuple[PostureRecord, ...] = field(
        default_factory=tuple,
    )
    module_fingerprints: Mapping[str, str] = field(
        default_factory=dict,
    )
    apply_event_paths: frozenset = field(
        default_factory=frozenset,
    )  # paths touched by APPLY in the window
    p99_confidence_drop_count: int = 0
    recurrence_records: Mapping[str, int] = field(
        default_factory=dict,
    )
    ops_summary: Mapping[str, int] = field(default_factory=dict)
    # POLICY_DEFAULT_DRIFT input: {flag_name: (registered_default,
    # observed_runtime_value)} — Slice 3 collects from FlagRegistry
    # snapshot vs os.environ.
    policy_observations: Mapping[str, Tuple[Any, Any]] = field(
        default_factory=dict,
    )

    def window_seconds(self) -> float:
        return max(0.0, self.window_end_ts - self.window_start_ts)


@dataclass(frozen=True)
class BehavioralSignature:
    """Aggregate behavioral signature over a window. Frozen so
    propagation through Slice 3's observer task and Slice 4's
    bridge is safe.

    Recency-weighted distributions: route_distribution and
    posture_distribution are normalized to sum to 1.0 (within
    floating-point tolerance). Older records weigh less per
    SemanticIndex's halflife formula."""

    window_start_ts: float
    window_end_ts: float
    route_distribution: Mapping[str, float]
    posture_distribution: Mapping[str, float]
    module_fingerprints: Mapping[str, str]
    p99_confidence_drop_count: int
    recurrence_index: Mapping[str, int]
    ops_summary: Mapping[str, int]
    posture_max_consecutive_hours: float = 0.0
    schema_version: str = COHERENCE_AUDITOR_SCHEMA_VERSION

    def signature_id(self) -> str:
        """Deterministic sha256 over the signature's structural
        contents (timestamps + distributions + fingerprints).
        Used by Slice 2 for window indexing and Slice 4 for
        dedup."""
        try:
            payload_parts = [
                f"start={self.window_start_ts:.6f}",
                f"end={self.window_end_ts:.6f}",
                "routes=" + ",".join(
                    f"{k}:{v:.6f}"
                    for k, v in sorted(
                        self.route_distribution.items(),
                    )
                ),
                "postures=" + ",".join(
                    f"{k}:{v:.6f}"
                    for k, v in sorted(
                        self.posture_distribution.items(),
                    )
                ),
                "modules=" + ",".join(
                    f"{k}:{v}"
                    for k, v in sorted(
                        self.module_fingerprints.items(),
                    )
                ),
                f"p99={self.p99_confidence_drop_count}",
                "recur=" + ",".join(
                    f"{k}:{v}"
                    for k, v in sorted(
                        self.recurrence_index.items(),
                    )
                ),
            ]
            payload = "|".join(payload_parts)
            return hashlib.sha256(payload.encode("utf-8")).hexdigest()
        except Exception:  # noqa: BLE001 — defensive
            return ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "window_start_ts": self.window_start_ts,
            "window_end_ts": self.window_end_ts,
            "route_distribution": dict(self.route_distribution),
            "posture_distribution": dict(self.posture_distribution),
            "module_fingerprints": dict(self.module_fingerprints),
            "p99_confidence_drop_count": (
                self.p99_confidence_drop_count
            ),
            "recurrence_index": dict(self.recurrence_index),
            "ops_summary": dict(self.ops_summary),
            "posture_max_consecutive_hours": (
                self.posture_max_consecutive_hours
            ),
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, Any],
    ) -> Optional["BehavioralSignature"]:
        """Schema-tolerant reconstruction. Returns ``None`` on
        schema mismatch OR malformed shape. NEVER raises."""
        try:
            schema = payload.get("schema_version")
            if schema != COHERENCE_AUDITOR_SCHEMA_VERSION:
                return None
            return cls(
                window_start_ts=float(payload["window_start_ts"]),
                window_end_ts=float(payload["window_end_ts"]),
                route_distribution=dict(
                    payload.get("route_distribution", {}),
                ),
                posture_distribution=dict(
                    payload.get("posture_distribution", {}),
                ),
                module_fingerprints=dict(
                    payload.get("module_fingerprints", {}),
                ),
                p99_confidence_drop_count=int(
                    payload.get("p99_confidence_drop_count", 0),
                ),
                recurrence_index=dict(
                    payload.get("recurrence_index", {}),
                ),
                ops_summary=dict(payload.get("ops_summary", {})),
                posture_max_consecutive_hours=float(
                    payload.get(
                        "posture_max_consecutive_hours", 0.0,
                    ),
                ),
            )
        except (KeyError, TypeError, ValueError):
            return None


@dataclass(frozen=True)
class DriftBudgets:
    """Per-kind drift budgets. Defaults read from env knobs.
    Frozen so a Slice 3 observer can pass the same budget across
    its periodic loop without worrying about mutation."""

    route_drift_pct: float = 25.0
    posture_locked_hours: float = 48.0
    recurrence_count: int = 3
    confidence_rise_pct: float = 50.0
    schema_version: str = COHERENCE_AUDITOR_SCHEMA_VERSION

    @classmethod
    def from_env(cls) -> "DriftBudgets":
        """Construct a DriftBudgets with all env-knob defaults
        applied. NEVER raises (each helper is defensive)."""
        return cls(
            route_drift_pct=budget_route_drift_pct(),
            posture_locked_hours=budget_posture_locked_hours(),
            recurrence_count=budget_recurrence_count(),
            confidence_rise_pct=budget_confidence_rise_pct(),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "route_drift_pct": self.route_drift_pct,
            "posture_locked_hours": self.posture_locked_hours,
            "recurrence_count": self.recurrence_count,
            "confidence_rise_pct": self.confidence_rise_pct,
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class BehavioralDriftFinding:
    """One drift finding within a verdict. Frozen for safe
    propagation."""

    kind: BehavioralDriftKind
    severity: DriftSeverity
    detail: str
    delta_metric: float
    budget_metric: float
    prev_signature_id: Optional[str] = None
    curr_signature_id: Optional[str] = None
    schema_version: str = COHERENCE_AUDITOR_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind.value,
            "severity": self.severity.value,
            "detail": self.detail,
            "delta_metric": self.delta_metric,
            "budget_metric": self.budget_metric,
            "prev_signature_id": self.prev_signature_id,
            "curr_signature_id": self.curr_signature_id,
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class BehavioralDriftVerdict:
    """Aggregate verdict produced by ``compute_behavioral_drift``.
    Frozen for safe propagation across Slice 3's observer + Slice
    4's bridge + Slice 5's SSE serialization."""

    outcome: CoherenceOutcome
    findings: Tuple[BehavioralDriftFinding, ...] = field(
        default_factory=tuple,
    )
    largest_severity: DriftSeverity = DriftSeverity.NONE
    drift_signature: str = ""
    detail: str = ""
    schema_version: str = COHERENCE_AUDITOR_SCHEMA_VERSION

    def has_drift(self) -> bool:
        return self.outcome is CoherenceOutcome.DRIFT_DETECTED

    def is_actionable(self) -> bool:
        """True iff drift detected AND severity ≥ MEDIUM. Slice 4
        uses this to decide whether to propose a tightening
        advisory."""
        return self.has_drift() and (
            _SEVERITY_RANK[self.largest_severity]
            >= _SEVERITY_RANK[DriftSeverity.MEDIUM]
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "findings": [f.to_dict() for f in self.findings],
            "largest_severity": self.largest_severity.value,
            "drift_signature": self.drift_signature,
            "detail": self.detail,
            "schema_version": self.schema_version,
        }


# ---------------------------------------------------------------------------
# Internal: recency-decay weighting (parity with SemanticIndex)
# ---------------------------------------------------------------------------


def _recency_weight(age_s: float, halflife_d: float) -> float:
    """``0.5 ** (age_days / halflife_days)``. Clamped to [0, 1].
    NEVER raises.

    Literal formula parity with
    ``semantic_index._recency_weight`` — pinned by companion test
    ``test_recency_weight_parity_with_semantic_index``. Re-
    implemented here so this module stays pure-stdlib (zero
    governance imports) — strongest possible authority invariant."""
    try:
        if halflife_d <= 0 or age_s < 0:
            return 1.0
        age_days = age_s / 86400.0
        return 0.5 ** (age_days / halflife_d)
    except Exception:  # noqa: BLE001 — defensive
        return 1.0


def _normalize_distribution(
    counts: Mapping[str, float],
) -> Dict[str, float]:
    """Normalize a counts mapping to a probability distribution
    summing to 1.0. Empty input → empty output (NOT a uniform
    distribution — empty signal is its own truth). NEVER raises."""
    try:
        total = float(sum(counts.values()))
        if total <= 0:
            return {}
        return {k: float(v) / total for k, v in counts.items()}
    except Exception:  # noqa: BLE001 — defensive
        return {}


def _total_variation_distance(
    p: Mapping[str, float], q: Mapping[str, float],
) -> float:
    """``0.5 * sum(|p[i] - q[i]|)`` over the union of keys.
    Standard total-variation distance. Range [0, 1]. Empty
    distributions yield 0 (no signal). NEVER raises."""
    try:
        keys = set(p.keys()) | set(q.keys())
        if not keys:
            return 0.0
        s = 0.0
        for k in keys:
            s += abs(float(p.get(k, 0.0)) - float(q.get(k, 0.0)))
        return 0.5 * s
    except Exception:  # noqa: BLE001 — defensive
        return 0.0


def _max_consecutive_hours(
    posture_records: Tuple[PostureRecord, ...],
    window_end_ts: float,
) -> float:
    """Compute the max consecutive run, in hours, of any single
    posture value within the records. Empty / single-record
    returns 0.0. NEVER raises.

    Records are sorted by ts ascending. A run extends while the
    posture value is unchanged; the run's duration is from its
    first ts to the next-different ts (or window_end_ts if it's
    the last run)."""
    try:
        if not posture_records:
            return 0.0
        sorted_records = sorted(posture_records, key=lambda r: r.ts)
        max_hours = 0.0
        run_start_ts = sorted_records[0].ts
        run_posture = sorted_records[0].posture
        for record in sorted_records[1:]:
            if record.posture != run_posture:
                run_seconds = max(0.0, record.ts - run_start_ts)
                max_hours = max(max_hours, run_seconds / 3600.0)
                run_start_ts = record.ts
                run_posture = record.posture
        # Tail run: from last transition to window_end
        tail_seconds = max(0.0, window_end_ts - run_start_ts)
        max_hours = max(max_hours, tail_seconds / 3600.0)
        return max_hours
    except Exception:  # noqa: BLE001 — defensive
        return 0.0


def _recency_weighted_counts(
    items: Tuple[Any, ...],
    *,
    key_fn,
    ts_fn,
    reference_ts: float,
    halflife_d: float,
) -> Dict[str, float]:
    """Aggregate ``items`` into a per-key weight sum where each
    item's weight = ``_recency_weight(reference_ts - ts_fn(item),
    halflife_d)``. Returns raw weighted counts (NOT normalized).
    NEVER raises."""
    try:
        out: Dict[str, float] = {}
        for item in items:
            try:
                key = str(key_fn(item))
                ts = float(ts_fn(item))
                w = _recency_weight(reference_ts - ts, halflife_d)
                out[key] = out.get(key, 0.0) + w
            except Exception:  # noqa: BLE001 — defensive per-item
                continue
        return out
    except Exception:  # noqa: BLE001 — defensive
        return {}


def _drift_signature_hash(
    findings: Tuple[BehavioralDriftFinding, ...],
) -> str:
    """Stable sha256 over sorted (kind, detail) tuples for
    Slice 3's dedup. Excludes timestamps + signature_ids so the
    same drift detected at different audit times produces the
    same dedup key. NEVER raises."""
    try:
        if not findings:
            return ""
        # Sort by kind value + detail to make order-stable.
        parts = sorted(
            f"{f.kind.value}:{f.detail}" for f in findings
        )
        payload = "|".join(parts)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()
    except Exception:  # noqa: BLE001 — defensive
        return ""


# ---------------------------------------------------------------------------
# Public: compute_behavioral_signature
# ---------------------------------------------------------------------------


def compute_behavioral_signature(
    data: WindowData,
) -> BehavioralSignature:
    """Aggregate window data into a single ``BehavioralSignature``.
    Pure aggregator. NEVER raises.

    Recency-weighted distributions:
      * ``route_distribution`` — weighted by ``_recency_weight``
        with halflife from ``halflife_days()``. Recent ops weigh
        more than older ops.
      * ``posture_distribution`` — same weighting.
      * ``module_fingerprints`` — passed through verbatim
        (fingerprint strings are state, not weight-able).
      * ``recurrence_index``, ``ops_summary`` — passed through
        verbatim (already aggregated counts).
      * ``posture_max_consecutive_hours`` — derived via
        ``_max_consecutive_hours``."""
    try:
        if not isinstance(data, WindowData):
            return BehavioralSignature(
                window_start_ts=0.0,
                window_end_ts=0.0,
                route_distribution={},
                posture_distribution={},
                module_fingerprints={},
                p99_confidence_drop_count=0,
                recurrence_index={},
                ops_summary={},
            )
        hl = halflife_days()
        ref_ts = data.window_end_ts

        route_counts = _recency_weighted_counts(
            data.op_records,
            key_fn=lambda o: o.route,
            ts_fn=lambda o: o.ts,
            reference_ts=ref_ts, halflife_d=hl,
        )
        posture_counts = _recency_weighted_counts(
            data.posture_records,
            key_fn=lambda p: p.posture,
            ts_fn=lambda p: p.ts,
            reference_ts=ref_ts, halflife_d=hl,
        )

        return BehavioralSignature(
            window_start_ts=float(data.window_start_ts),
            window_end_ts=float(data.window_end_ts),
            route_distribution=_normalize_distribution(
                route_counts,
            ),
            posture_distribution=_normalize_distribution(
                posture_counts,
            ),
            module_fingerprints=dict(data.module_fingerprints),
            p99_confidence_drop_count=int(
                data.p99_confidence_drop_count,
            ),
            recurrence_index=dict(data.recurrence_records),
            ops_summary=dict(data.ops_summary),
            posture_max_consecutive_hours=(
                _max_consecutive_hours(
                    data.posture_records, data.window_end_ts,
                )
            ),
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[CoherenceAuditor] compute_behavioral_signature "
            "raised: %s", exc,
        )
        return BehavioralSignature(
            window_start_ts=0.0, window_end_ts=0.0,
            route_distribution={}, posture_distribution={},
            module_fingerprints={},
            p99_confidence_drop_count=0,
            recurrence_index={}, ops_summary={},
        )


# ---------------------------------------------------------------------------
# Public: compute_behavioral_drift
# ---------------------------------------------------------------------------


def compute_behavioral_drift(
    prev: Optional[BehavioralSignature],
    curr: Optional[BehavioralSignature],
    *,
    budgets: Optional[DriftBudgets] = None,
    apply_event_paths: Optional[frozenset] = None,
    enabled_override: Optional[bool] = None,
    policy_observations: Optional[
        Mapping[str, Tuple[Any, Any]]
    ] = None,
) -> BehavioralDriftVerdict:
    """Pure decision over (prev, curr) signature pair. NEVER
    raises. Returns exactly one ``BehavioralDriftVerdict``.

    Decision tree (every input maps to exactly one outcome):

      1. Master flag off (``enabled_override`` or
         ``coherence_auditor_enabled()``) → ``DISABLED``.
      2. ``curr`` is None / not a BehavioralSignature → ``FAILED``.
      3. ``prev`` is None → ``INSUFFICIENT_DATA`` (first window).
      4. Otherwise: compute per-kind findings via budgets;
         findings empty → ``COHERENT``; any finding → ``DRIFT_
         DETECTED``.

    Per-kind detection:
      * ``BEHAVIORAL_ROUTE_DRIFT``: total-variation distance of
        route distributions × 100 vs ``budgets.route_drift_pct``.
      * ``POSTURE_LOCKED``: ``curr.posture_max_consecutive_hours``
        vs ``budgets.posture_locked_hours``.
      * ``SYMBOL_FLUX_DRIFT``: any module whose fingerprint
        differs prev→curr AND whose path is NOT in
        ``apply_event_paths``.
      * ``POLICY_DEFAULT_DRIFT``: any
        ``policy_observations[flag] = (registered, observed)``
        where ``registered != observed``.
      * ``RECURRENCE_DRIFT``: any recurrence_index entry whose
        count exceeds ``budgets.recurrence_count``.
      * ``CONFIDENCE_DRIFT``: ``curr.p99 / max(1, prev.p99) - 1``
        × 100 vs ``budgets.confidence_rise_pct``."""
    try:
        # Step 1: master flag
        is_enabled = (
            enabled_override if enabled_override is not None
            else coherence_auditor_enabled()
        )
        if not is_enabled:
            return BehavioralDriftVerdict(
                outcome=CoherenceOutcome.DISABLED,
                detail=(
                    "JARVIS_COHERENCE_AUDITOR_ENABLED is false "
                    "(or override) — no drift comparison"
                ),
            )

        # Step 2: curr validity
        if not isinstance(curr, BehavioralSignature):
            return BehavioralDriftVerdict(
                outcome=CoherenceOutcome.FAILED,
                detail="curr is not a BehavioralSignature",
            )

        # Step 3: prev presence
        if prev is None:
            return BehavioralDriftVerdict(
                outcome=CoherenceOutcome.INSUFFICIENT_DATA,
                detail=(
                    "prev signature is None — first window in "
                    "audit history; defer drift verdict"
                ),
            )
        if not isinstance(prev, BehavioralSignature):
            return BehavioralDriftVerdict(
                outcome=CoherenceOutcome.FAILED,
                detail="prev is not a BehavioralSignature",
            )

        # Step 4: per-kind findings
        b = budgets if budgets is not None else DriftBudgets.from_env()
        findings: list = []
        prev_id = prev.signature_id() or None
        curr_id = curr.signature_id() or None
        applied_paths = (
            apply_event_paths if apply_event_paths is not None
            else frozenset()
        )

        # 4a. BEHAVIORAL_ROUTE_DRIFT
        try:
            tvd = _total_variation_distance(
                prev.route_distribution, curr.route_distribution,
            )
            tvd_pct = tvd * 100.0
            if tvd_pct > b.route_drift_pct:
                ratio = tvd_pct / max(1e-9, b.route_drift_pct)
                findings.append(BehavioralDriftFinding(
                    kind=BehavioralDriftKind.BEHAVIORAL_ROUTE_DRIFT,
                    severity=_severity_for_ratio(ratio),
                    detail=(
                        f"route distribution rotated by "
                        f"{tvd_pct:.2f}% > budget "
                        f"{b.route_drift_pct:.2f}%"
                    ),
                    delta_metric=tvd_pct,
                    budget_metric=b.route_drift_pct,
                    prev_signature_id=prev_id,
                    curr_signature_id=curr_id,
                ))
        except Exception:  # noqa: BLE001 — defensive per-kind
            pass

        # 4b. POSTURE_LOCKED
        try:
            locked_hours = curr.posture_max_consecutive_hours
            if locked_hours > b.posture_locked_hours:
                ratio = locked_hours / max(
                    1e-9, b.posture_locked_hours,
                )
                findings.append(BehavioralDriftFinding(
                    kind=BehavioralDriftKind.POSTURE_LOCKED,
                    severity=_severity_for_ratio(ratio),
                    detail=(
                        f"posture locked for {locked_hours:.2f}h "
                        f"> budget {b.posture_locked_hours:.2f}h"
                    ),
                    delta_metric=locked_hours,
                    budget_metric=b.posture_locked_hours,
                    prev_signature_id=prev_id,
                    curr_signature_id=curr_id,
                ))
        except Exception:  # noqa: BLE001 — defensive
            pass

        # 4c. SYMBOL_FLUX_DRIFT
        try:
            for path, curr_fp in curr.module_fingerprints.items():
                prev_fp = prev.module_fingerprints.get(path)
                if prev_fp is None:
                    # New module — not flux; observer's responsibility
                    # to register it. Skip.
                    continue
                if prev_fp == curr_fp:
                    continue
                if path in applied_paths:
                    # Legitimate APPLY recorded the change. Skip.
                    continue
                # Off-graduation flux — fingerprint changed without
                # an APPLY event referencing this path.
                findings.append(BehavioralDriftFinding(
                    kind=BehavioralDriftKind.SYMBOL_FLUX_DRIFT,
                    # Severity LOW for symbol_flux because the
                    # fact-of-change is binary (no graceful
                    # delta); operators escalate via Slice 4.
                    severity=DriftSeverity.MEDIUM,
                    detail=(
                        f"module {path!r} fingerprint changed "
                        f"without an APPLY event in window: "
                        f"{prev_fp[:8]}…→{curr_fp[:8]}…"
                    ),
                    delta_metric=1.0,
                    budget_metric=0.0,
                    prev_signature_id=prev_id,
                    curr_signature_id=curr_id,
                ))
        except Exception:  # noqa: BLE001 — defensive
            pass

        # 4d. POLICY_DEFAULT_DRIFT
        try:
            obs = (
                policy_observations
                if policy_observations is not None else {}
            )
            for flag, pair in obs.items():
                try:
                    registered, observed = pair
                except Exception:  # noqa: BLE001 — defensive per-flag
                    continue
                if registered == observed:
                    continue
                findings.append(BehavioralDriftFinding(
                    kind=(
                        BehavioralDriftKind.POLICY_DEFAULT_DRIFT
                    ),
                    severity=DriftSeverity.MEDIUM,
                    detail=(
                        f"flag {flag!r} runtime "
                        f"{observed!r} != registered "
                        f"default {registered!r}"
                    ),
                    delta_metric=1.0,
                    budget_metric=0.0,
                    prev_signature_id=prev_id,
                    curr_signature_id=curr_id,
                ))
        except Exception:  # noqa: BLE001 — defensive
            pass

        # 4e. RECURRENCE_DRIFT
        try:
            for failure_class, count in (
                curr.recurrence_index.items()
            ):
                if int(count) > b.recurrence_count:
                    ratio = int(count) / max(
                        1.0, float(b.recurrence_count),
                    )
                    findings.append(BehavioralDriftFinding(
                        kind=(
                            BehavioralDriftKind.RECURRENCE_DRIFT
                        ),
                        severity=_severity_for_ratio(ratio),
                        detail=(
                            f"failure_class {failure_class!r} "
                            f"appeared {count} times > budget "
                            f"{b.recurrence_count}"
                        ),
                        delta_metric=float(count),
                        budget_metric=float(
                            b.recurrence_count,
                        ),
                        prev_signature_id=prev_id,
                        curr_signature_id=curr_id,
                    ))
        except Exception:  # noqa: BLE001 — defensive
            pass

        # 4f. CONFIDENCE_DRIFT
        try:
            prev_p99 = max(1, int(prev.p99_confidence_drop_count))
            curr_p99 = int(curr.p99_confidence_drop_count)
            rise_pct = (curr_p99 / prev_p99 - 1.0) * 100.0
            if rise_pct > b.confidence_rise_pct:
                ratio = rise_pct / max(
                    1e-9, b.confidence_rise_pct,
                )
                findings.append(BehavioralDriftFinding(
                    kind=BehavioralDriftKind.CONFIDENCE_DRIFT,
                    severity=_severity_for_ratio(ratio),
                    detail=(
                        f"p99 confidence drops rose {rise_pct:.2f}% "
                        f"({prev_p99}→{curr_p99}) > budget "
                        f"{b.confidence_rise_pct:.2f}%"
                    ),
                    delta_metric=rise_pct,
                    budget_metric=b.confidence_rise_pct,
                    prev_signature_id=prev_id,
                    curr_signature_id=curr_id,
                ))
        except Exception:  # noqa: BLE001 — defensive
            pass

        # Aggregate
        if not findings:
            return BehavioralDriftVerdict(
                outcome=CoherenceOutcome.COHERENT,
                detail=(
                    "all 6 drift kinds within budget for this "
                    "window pair"
                ),
            )
        finding_tuple = tuple(findings)
        largest = max(
            (f.severity for f in finding_tuple),
            key=lambda s: _SEVERITY_RANK[s],
        )
        return BehavioralDriftVerdict(
            outcome=CoherenceOutcome.DRIFT_DETECTED,
            findings=finding_tuple,
            largest_severity=largest,
            drift_signature=_drift_signature_hash(finding_tuple),
            detail=(
                f"{len(finding_tuple)} drift finding(s); "
                f"largest_severity={largest.value}"
            ),
        )
    except Exception as exc:  # noqa: BLE001 — last-resort defensive
        logger.debug(
            "[CoherenceAuditor] compute_behavioral_drift "
            "raised: %s", exc,
        )
        return BehavioralDriftVerdict(
            outcome=CoherenceOutcome.FAILED,
            detail=f"compute_behavioral_drift raised: {exc!r}",
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = [
    "COHERENCE_AUDITOR_SCHEMA_VERSION",
    "BehavioralDriftFinding",
    "BehavioralDriftKind",
    "BehavioralDriftVerdict",
    "BehavioralSignature",
    "CoherenceOutcome",
    "DriftBudgets",
    "DriftSeverity",
    "OpRecord",
    "PostureRecord",
    "WindowData",
    "budget_confidence_rise_pct",
    "budget_posture_locked_hours",
    "budget_recurrence_count",
    "budget_route_drift_pct",
    "coherence_auditor_enabled",
    "compute_behavioral_drift",
    "compute_behavioral_signature",
    "halflife_days",
]
