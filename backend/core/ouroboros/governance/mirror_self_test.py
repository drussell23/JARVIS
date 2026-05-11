"""
Mirror-Self Test
================

Closes §40 Wave 4 #14 — the fifth Wave 4 (Tier 3 calibration
learning) arc. Per the operator binding:

  "System prompted to predict its own next action given current
   state; compare actual vs predicted; track calibration. Closes
   the metacognition gap (system knowing what it's about to do)."

This substrate is a **pure-function metacognition ledger** that
records what the system PREDICTED it would do, then records
what ACTUALLY happened, and computes per-dimension calibration
scores. Operators see a 4-axis accuracy vector across
``NEXT_PHASE`` / ``TARGET_FILE`` / ``RISK_TIER`` / ``OUTCOME``
predictions.

When a prediction × actual pair shows FALSIFIED (predicted ≠
actual) the substrate optionally bridges the discrepancy into
:class:`belief_revision_ledger` (Wave 4 #9) so the broader
calibration loop sees metacognition misses as falsifying
evidence — no duplicate falsification tracking.

The substrate is **deterministic** — same prediction/actual
corpus → same calibration. Zero LLM. The calibration vector is
*surfaced*; consumer-side action (e.g., raising risk_tier when
RISK_TIER calibration is POOR) stays operator-paced.

Composition contract — thin pure-function ledger over canonical
substrates:

* :func:`cross_process_jsonl.flock_append_line` — §33.4 audit
  ledger at ``.jarvis/mirror_self_ledger.jsonl``. One row per
  ``record_prediction`` + one row per ``record_actual``.
* :func:`belief_revision_ledger.record_evidence` (Wave 4 #9)
  via optional sub-flag — when on, falsified predictions
  emit a falsifying-evidence row against a stable domain
  claim ``mirror_self_calibration:{dimension}``.
* :func:`governance_boundary_gate.is_boundary_crossed` (Wave
  2 #5) — applied when the predicted/actual TARGET_FILE
  touches the cage; surfaces ``cage_touch`` flag on the
  calibration report.

NEVER raises. Empty ledger / belief ledger unavailable /
malformed rows all degrade to ``UNCALIBRATED`` verdict, not
exception.

Closed 4-value :class:`PredictionDimension`:

  NEXT_PHASE     orchestrator state — what phase will fire
                 next (CLASSIFY / GENERATE / VALIDATE / APPLY
                 / VERIFY etc.)
  TARGET_FILE    exploration heuristic — what file will the
                 op touch
  RISK_TIER      policy calibration — what risk tier will
                 the op land in (safe_auto / notify_apply /
                 approval_required / blocked)
  OUTCOME        end-to-end overconfidence — success / failure
                 / partial

Closed 4-value :class:`CalibrationVerdict`:

  UNCALIBRATED   sample_count < min_sample (cold start)
  POOR           accuracy < 0.40
  FAIR           0.40 ≤ accuracy < 0.75
  GOOD           accuracy ≥ 0.75

§33.1 cognitive substrate ``JARVIS_MIRROR_SELF_TEST_ENABLED``
default-**FALSE** — operator-paced opt-in. Sub-flags:
``JARVIS_MIRROR_SELF_PERSIST_ENABLED`` (gate §33.4 writes,
default TRUE), ``JARVIS_MIRROR_SELF_BELIEF_BRIDGE_ENABLED``
(gate Wave 4 #9 falsification bridge, default TRUE).

Authority asymmetry (AST-pinned): imports stdlib only at
module-load. ``cross_process_jsonl`` /
``belief_revision_ledger`` / ``governance_boundary_gate`` are
all lazy-imported behind composer helpers. Does NOT import
orchestrator / iron_gate / policy / providers /
candidate_generator / urgency_router / change_engine /
semantic_guardian / auto_committer / risk_tier_floor.
"""
from __future__ import annotations

import ast
import enum
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    Any,
    Dict,
    FrozenSet,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

logger = logging.getLogger(__name__)


MIRROR_SELF_SCHEMA_VERSION: str = "mirror_self.1"


# ===========================================================================
# Env knobs
# ===========================================================================


_ENV_MASTER = "JARVIS_MIRROR_SELF_TEST_ENABLED"
_ENV_PERSIST = "JARVIS_MIRROR_SELF_PERSIST_ENABLED"
_ENV_BELIEF_BRIDGE = "JARVIS_MIRROR_SELF_BELIEF_BRIDGE_ENABLED"
_ENV_MIN_SAMPLE = "JARVIS_MIRROR_SELF_MIN_SAMPLE"
_ENV_WINDOW_S = "JARVIS_MIRROR_SELF_WINDOW_S"
_ENV_MAX_RECORDS = "JARVIS_MIRROR_SELF_MAX_RECORDS"
_ENV_LEDGER_PATH = "JARVIS_MIRROR_SELF_LEDGER_PATH"

_DEFAULT_MIN_SAMPLE = 5
_DEFAULT_WINDOW_S = 86_400  # 24h default window
_DEFAULT_MAX_RECORDS = 1_000
_MIN_SAMPLE_LO = 1
_MIN_SAMPLE_HI = 10_000
_MIN_WINDOW = 60
_MAX_WINDOW = 30 * 86_400  # 30 days
_MIN_MAX_RECORDS = 1
_MAX_MAX_RECORDS = 1_000_000

_DEFAULT_LEDGER_REL = ".jarvis/mirror_self_ledger.jsonl"

_TRUTHY: FrozenSet[str] = frozenset({"1", "true", "yes", "on"})


def _flag(name: str, *, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in _TRUTHY


def master_enabled() -> bool:
    """§33.1 cognitive substrate — default-FALSE.

    Operator-paced opt-in. Records + evaluation return
    UNCALIBRATED-equivalent stubs when off. Flip
    ``JARVIS_MIRROR_SELF_TEST_ENABLED=true`` to begin
    accumulating prediction/actual pairs.
    """
    return _flag(_ENV_MASTER, default=False)


def persistence_enabled() -> bool:
    """Sub-flag — gate §33.4 JSONL writes. Default TRUE."""
    return _flag(_ENV_PERSIST, default=True)


def belief_bridge_enabled() -> bool:
    """Sub-flag — gate the optional belief_revision_ledger
    bridge. When on, falsified predictions also emit a
    falsifying-evidence row in Wave 4 #9's ledger. Default
    TRUE so the broader calibration loop sees metacognition
    misses without operator wiring."""
    return _flag(_ENV_BELIEF_BRIDGE, default=True)


def _read_clamped_int(
    name: str, default: int, lo: int, hi: int,
) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def min_sample_size() -> int:
    """Min predictions per dimension before a calibration
    verdict moves from UNCALIBRATED to POOR/FAIR/GOOD. Defaults
    to 5. Clamped to [1, 10_000]."""
    return _read_clamped_int(
        _ENV_MIN_SAMPLE,
        _DEFAULT_MIN_SAMPLE,
        _MIN_SAMPLE_LO,
        _MIN_SAMPLE_HI,
    )


def window_s() -> int:
    """Time window (seconds) for "recent" prediction filtering.
    Defaults to 86_400 (24h). Clamped to [60, 30 days]."""
    return _read_clamped_int(
        _ENV_WINDOW_S,
        _DEFAULT_WINDOW_S,
        _MIN_WINDOW,
        _MAX_WINDOW,
    )


def max_records() -> int:
    """Cap on ledger rows read per evaluation. Clamped to
    [1, 1_000_000]."""
    return _read_clamped_int(
        _ENV_MAX_RECORDS,
        _DEFAULT_MAX_RECORDS,
        _MIN_MAX_RECORDS,
        _MAX_MAX_RECORDS,
    )


def ledger_path() -> Path:
    """Audit-ledger path. Defaults to
    ``.jarvis/mirror_self_ledger.jsonl``. Operator may override
    via ``JARVIS_MIRROR_SELF_LEDGER_PATH``."""
    raw = os.environ.get(_ENV_LEDGER_PATH, "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path(_DEFAULT_LEDGER_REL)


# ===========================================================================
# Closed taxonomies
# ===========================================================================


class PredictionDimension(str, enum.Enum):
    """Closed 4-value prediction axis — bytes-pinned via AST."""

    NEXT_PHASE = "next_phase"
    TARGET_FILE = "target_file"
    RISK_TIER = "risk_tier"
    OUTCOME = "outcome"


class CalibrationVerdict(str, enum.Enum):
    """Closed 4-value calibration verdict — bytes-pinned via AST."""

    UNCALIBRATED = "uncalibrated"
    POOR = "poor"
    FAIR = "fair"
    GOOD = "good"


_DIMENSION_GLYPH: Dict[str, str] = {
    PredictionDimension.NEXT_PHASE.value: "→",
    PredictionDimension.TARGET_FILE.value: "📄",
    PredictionDimension.RISK_TIER.value: "🛡",
    PredictionDimension.OUTCOME.value: "✦",
}


_VERDICT_GLYPH: Dict[str, str] = {
    CalibrationVerdict.UNCALIBRATED.value: "○",
    CalibrationVerdict.POOR.value: "▽",
    CalibrationVerdict.FAIR.value: "◊",
    CalibrationVerdict.GOOD.value: "▲",
}


def dimension_glyph(dim: object) -> str:
    """Public glyph accessor. NEVER raises."""
    try:
        if hasattr(dim, "value"):
            return _DIMENSION_GLYPH.get(str(dim.value), "?")
        return _DIMENSION_GLYPH.get(
            str(dim or "").strip().lower(), "?",
        )
    except Exception:  # noqa: BLE001
        return "?"


def verdict_glyph(verdict: object) -> str:
    """Public glyph accessor. NEVER raises."""
    try:
        if hasattr(verdict, "value"):
            return _VERDICT_GLYPH.get(str(verdict.value), "?")
        return _VERDICT_GLYPH.get(
            str(verdict or "").strip().lower(), "?",
        )
    except Exception:  # noqa: BLE001
        return "?"


def _coerce_dimension(raw: Any) -> Optional[PredictionDimension]:
    """Best-effort coercion. NEVER raises. Unknown → None
    (caller filters)."""
    if isinstance(raw, PredictionDimension):
        return raw
    try:
        s = str(getattr(raw, "value", raw) or "").strip().lower()
    except Exception:  # noqa: BLE001
        return None
    for d in PredictionDimension:
        if d.value == s:
            return d
    return None


def _normalize(value: Any) -> str:
    """Canonical value normalization for prediction matching.
    NEVER raises."""
    try:
        return str(value or "").strip().lower()
    except Exception:  # noqa: BLE001
        return ""


def _calibration_verdict_for(
    accuracy: float, sample_count: int,
) -> CalibrationVerdict:
    if sample_count < min_sample_size():
        return CalibrationVerdict.UNCALIBRATED
    if accuracy < 0.40:
        return CalibrationVerdict.POOR
    if accuracy < 0.75:
        return CalibrationVerdict.FAIR
    return CalibrationVerdict.GOOD


# ===========================================================================
# §33.5 frozen versioned artifacts
# ===========================================================================


@dataclass(frozen=True)
class PredictionRow:
    """One prediction recorded at op-start."""

    op_id: str
    dimension: PredictionDimension
    predicted_value: str
    predicted_at_unix: float
    schema_version: str = MIRROR_SELF_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": "prediction",
            "op_id": self.op_id[:128],
            "dimension": self.dimension.value,
            "predicted_value": self.predicted_value[:256],
            "predicted_at_unix": float(self.predicted_at_unix),
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class ActualRow:
    """One actual outcome recorded at op-completion."""

    op_id: str
    dimension: PredictionDimension
    actual_value: str
    actual_at_unix: float
    was_correct: bool
    schema_version: str = MIRROR_SELF_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": "actual",
            "op_id": self.op_id[:128],
            "dimension": self.dimension.value,
            "actual_value": self.actual_value[:256],
            "actual_at_unix": float(self.actual_at_unix),
            "was_correct": bool(self.was_correct),
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class CalibrationReport:
    """Per-dimension calibration aggregation."""

    dimension: PredictionDimension
    sample_count: int
    correct_count: int
    accuracy: float
    verdict: CalibrationVerdict
    window_s: int
    diagnostic: str
    schema_version: str = MIRROR_SELF_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "dimension": self.dimension.value,
            "sample_count": int(self.sample_count),
            "correct_count": int(self.correct_count),
            "accuracy": float(self.accuracy),
            "verdict": self.verdict.value,
            "window_s": int(self.window_s),
            "diagnostic": self.diagnostic[:512],
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class MirrorSelfReport:
    """Aggregate across all 4 dimensions."""

    evaluated_at_unix: float
    master_enabled: bool
    per_dimension: Tuple[CalibrationReport, ...]
    diagnostic: str
    elapsed_s: float
    schema_version: str = MIRROR_SELF_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "evaluated_at_unix": self.evaluated_at_unix,
            "master_enabled": self.master_enabled,
            "per_dimension": [
                r.to_dict() for r in self.per_dimension
            ],
            "diagnostic": self.diagnostic[:512],
            "elapsed_s": float(self.elapsed_s),
            "schema_version": self.schema_version,
        }


# ===========================================================================
# Composers — canonical surfaces (all lazy-imported)
# ===========================================================================


def _flock_append(payload: Mapping[str, Any]) -> bool:
    """Best-effort §33.4 write. NEVER raises."""
    if not master_enabled() or not persistence_enabled():
        return False
    try:
        from backend.core.ouroboros.governance.cross_process_jsonl import (  # noqa: E501
            flock_append_line,
        )
    except ImportError:
        return False
    try:
        target = ledger_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        flock_append_line(target, json.dumps(dict(payload)))
        return True
    except Exception:  # noqa: BLE001
        return False


def _load_ledger_rows(
    *,
    max_total: Optional[int] = None,
    path_override: Optional[Path] = None,
) -> Tuple[Dict[str, Any], ...]:
    """Plain stdlib read-back. Corrupted lines skipped.
    NEVER raises."""
    cap = max_records() if max_total is None else int(max_total)
    target = path_override or ledger_path()
    rows: List[Dict[str, Any]] = []
    try:
        if not target.exists():
            return ()
        with target.open("r", encoding="utf-8") as fp:
            for raw in fp:
                line = raw.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if not isinstance(obj, dict):
                    continue
                rows.append(obj)
                if len(rows) >= cap:
                    break
    except Exception:  # noqa: BLE001
        return tuple(rows)
    return tuple(rows)


def _bridge_to_belief_ledger(
    *,
    op_id: str,
    dimension: PredictionDimension,
    predicted: str,
    actual: str,
    was_correct: bool,
    now_unix: float,
) -> str:
    """Optional Wave 4 #9 bridge. When sub-flag is on and the
    prediction was wrong, emit a falsifying-evidence row in
    belief_revision_ledger against a stable per-dimension
    claim. Returns the claim_id emitted (or ""). NEVER raises.
    """
    if not belief_bridge_enabled():
        return ""
    if was_correct:
        return ""
    try:
        from backend.core.ouroboros.governance.belief_revision_ledger import (  # noqa: E501
            EvidenceKind,
            record_claim,
            record_evidence,
        )
    except ImportError:
        return ""
    try:
        # The stable per-dimension domain claim — same text +
        # domain → same claim_id (deterministic in Wave 4 #9).
        # Without a stable timestamp the claim_id would drift,
        # so we use a deterministic anchor (1.0) for the
        # bridge-claim's claimed_at_unix.
        claim_text = (
            f"mirror_self prediction calibration: dimension="
            f"{dimension.value}"
        )
        claim_domain = (
            f"mirror_self_calibration:{dimension.value}"
        )
        claim = record_claim(
            text=claim_text,
            domain=claim_domain,
            confidence=0.5,
            now_unix=1.0,  # stable anchor
        )
        if claim is None:
            return ""
        record_evidence(
            claim.claim_id,
            EvidenceKind.FALSIFYING,
            source_op_id=op_id,
            note=(
                f"predicted={predicted[:120]} actual="
                f"{actual[:120]}"
            ),
            now_unix=now_unix,
        )
        return claim.claim_id
    except Exception:  # noqa: BLE001
        return ""


def _maybe_check_boundary(
    predicted: str, actual: str,
) -> bool:
    """For TARGET_FILE dimension — flag when either predicted
    or actual touches the cage. NEVER raises."""
    try:
        from backend.core.ouroboros.governance.governance_boundary_gate import (  # noqa: E501
            is_boundary_crossed,
        )
    except Exception:  # noqa: BLE001
        return False
    try:
        for v in (predicted, actual):
            if v and is_boundary_crossed((v,)):
                return True
        return False
    except Exception:  # noqa: BLE001
        return False


# ===========================================================================
# Producer-bridge — record_prediction / record_actual
# ===========================================================================


def record_prediction(
    op_id: str,
    dimension: Any,
    predicted_value: str,
    *,
    now_unix: Optional[float] = None,
) -> Optional[PredictionRow]:
    """Producer-bridge — record a prediction at op-start.
    NEVER raises. Returns the frozen artifact even master-off
    so the caller's audit trail isn't silently lost."""
    try:
        oid = str(op_id or "").strip()
    except Exception:  # noqa: BLE001
        return None
    if not oid:
        return None
    dim = _coerce_dimension(dimension)
    if dim is None:
        return None
    try:
        pred = str(predicted_value or "")[:256]
    except Exception:  # noqa: BLE001
        return None
    if not pred:
        return None
    now = time.time() if now_unix is None else float(now_unix)
    row = PredictionRow(
        op_id=oid,
        dimension=dim,
        predicted_value=pred,
        predicted_at_unix=now,
    )
    _flock_append(row.to_dict())
    return row


def record_actual(
    op_id: str,
    dimension: Any,
    actual_value: str,
    *,
    now_unix: Optional[float] = None,
    rows: Optional[Sequence[Mapping[str, Any]]] = None,
) -> Optional[ActualRow]:
    """Producer-bridge — record actual outcome at op-completion.

    Resolves the matching prediction (op_id + dimension) from
    the ledger, computes ``was_correct``, persists the actual
    row, and optionally bridges falsified predictions into the
    Wave 4 #9 belief ledger.

    NEVER raises.
    """
    try:
        oid = str(op_id or "").strip()
    except Exception:  # noqa: BLE001
        return None
    if not oid:
        return None
    dim = _coerce_dimension(dimension)
    if dim is None:
        return None
    try:
        act = str(actual_value or "")[:256]
    except Exception:  # noqa: BLE001
        return None
    if not act:
        return None
    now = time.time() if now_unix is None else float(now_unix)

    # Look up the matching prediction.
    ledger_rows = rows if rows is not None else _load_ledger_rows()
    predicted = ""
    for r in ledger_rows:
        if r.get("kind") != "prediction":
            continue
        if r.get("op_id") != oid:
            continue
        if r.get("dimension") != dim.value:
            continue
        predicted = str(r.get("predicted_value", ""))
        break

    was_correct = (
        bool(predicted) and _normalize(predicted) == _normalize(act)
    )
    row = ActualRow(
        op_id=oid,
        dimension=dim,
        actual_value=act,
        actual_at_unix=now,
        was_correct=was_correct,
    )
    _flock_append(row.to_dict())

    # Optional Wave 4 #9 bridge.
    if not was_correct and predicted and master_enabled():
        _bridge_to_belief_ledger(
            op_id=oid,
            dimension=dim,
            predicted=predicted,
            actual=act,
            was_correct=False,
            now_unix=now,
        )

    return row


# ===========================================================================
# Pure calibration aggregation
# ===========================================================================


def _filter_window(
    rows: Sequence[Mapping[str, Any]],
    *,
    window_seconds: int,
    now_unix: float,
) -> Tuple[Mapping[str, Any], ...]:
    """Filter to rows within [now - window, now]. NEVER raises."""
    if window_seconds <= 0:
        return tuple(rows)
    cutoff = now_unix - window_seconds
    out: List[Mapping[str, Any]] = []
    for r in rows:
        try:
            t = float(
                r.get("predicted_at_unix")
                or r.get("actual_at_unix")
                or 0.0,
            )
        except Exception:  # noqa: BLE001
            continue
        if t >= cutoff:
            out.append(r)
    return tuple(out)


def _aggregate_for_dimension(
    rows: Sequence[Mapping[str, Any]],
    dimension: PredictionDimension,
    *,
    window_seconds: int,
) -> CalibrationReport:
    """Pure aggregation. NEVER raises."""
    matching_actuals = [
        r for r in rows
        if r.get("kind") == "actual"
        and r.get("dimension") == dimension.value
    ]
    sample = len(matching_actuals)
    correct = sum(
        1 for r in matching_actuals if bool(r.get("was_correct"))
    )
    accuracy = (correct / sample) if sample > 0 else 0.0
    verdict = _calibration_verdict_for(accuracy, sample)
    diagnostic = (
        f"{correct}/{sample} correct (window={window_seconds}s); "
        f"accuracy={accuracy:.2f} → {verdict.value}"
    )
    return CalibrationReport(
        dimension=dimension,
        sample_count=sample,
        correct_count=correct,
        accuracy=accuracy,
        verdict=verdict,
        window_s=window_seconds,
        diagnostic=diagnostic,
    )


def compute_calibration(
    dimension: Any,
    *,
    window_seconds: Optional[int] = None,
    rows: Optional[Sequence[Mapping[str, Any]]] = None,
    now_unix: Optional[float] = None,
) -> CalibrationReport:
    """Per-dimension calibration computation. NEVER raises.

    Returns an UNCALIBRATED report when master is off OR no
    actuals have been recorded yet for the dimension.
    """
    dim = _coerce_dimension(dimension)
    if dim is None:
        # Fall back to the first dimension — caller-error path.
        dim = PredictionDimension.OUTCOME
    win = window_s() if window_seconds is None else int(window_seconds)
    now = time.time() if now_unix is None else float(now_unix)
    if not master_enabled():
        return CalibrationReport(
            dimension=dim,
            sample_count=0,
            correct_count=0,
            accuracy=0.0,
            verdict=CalibrationVerdict.UNCALIBRATED,
            window_s=win,
            diagnostic=(
                f"gate disabled via {_ENV_MASTER}=false"
            ),
        )
    all_rows = rows if rows is not None else _load_ledger_rows()
    filtered = _filter_window(
        all_rows, window_seconds=win, now_unix=now,
    )
    return _aggregate_for_dimension(
        filtered, dim, window_seconds=win,
    )


def compute_all_calibrations(
    *,
    window_seconds: Optional[int] = None,
    rows: Optional[Sequence[Mapping[str, Any]]] = None,
    now_unix: Optional[float] = None,
) -> MirrorSelfReport:
    """Compute calibration across all 4 dimensions. NEVER
    raises. Always returns 4 per-dimension reports (one each
    for NEXT_PHASE / TARGET_FILE / RISK_TIER / OUTCOME)."""
    started = time.time() if now_unix is None else float(now_unix)
    win = window_s() if window_seconds is None else int(window_seconds)
    if not master_enabled():
        per_dim = tuple(
            CalibrationReport(
                dimension=d,
                sample_count=0,
                correct_count=0,
                accuracy=0.0,
                verdict=CalibrationVerdict.UNCALIBRATED,
                window_s=win,
                diagnostic=(
                    f"gate disabled via {_ENV_MASTER}=false"
                ),
            )
            for d in PredictionDimension
        )
        return MirrorSelfReport(
            evaluated_at_unix=started,
            master_enabled=False,
            per_dimension=per_dim,
            diagnostic=f"gate disabled via {_ENV_MASTER}=false",
            elapsed_s=0.0,
        )

    all_rows = rows if rows is not None else _load_ledger_rows()
    filtered = _filter_window(
        all_rows, window_seconds=win, now_unix=started,
    )
    per_dim = tuple(
        _aggregate_for_dimension(filtered, d, window_seconds=win)
        for d in PredictionDimension
    )
    # Diagnostic — count per verdict
    counts: Dict[str, int] = {}
    for r in per_dim:
        counts[r.verdict.value] = counts.get(r.verdict.value, 0) + 1
    diagnostic = (
        f"per-verdict: "
        f"good={counts.get('good', 0)} "
        f"fair={counts.get('fair', 0)} "
        f"poor={counts.get('poor', 0)} "
        f"uncalibrated={counts.get('uncalibrated', 0)}"
    )
    report = MirrorSelfReport(
        evaluated_at_unix=started,
        master_enabled=True,
        per_dimension=per_dim,
        diagnostic=diagnostic,
        elapsed_s=max(0.0, time.time() - started),
    )
    _publish_calibration_event(report)
    return report


# ===========================================================================
# SSE publisher
# ===========================================================================


def _publish_calibration_event(report: MirrorSelfReport) -> None:
    """Best-effort SSE publish. NEVER raises. Fires only when
    at least one dimension is actionable (not UNCALIBRATED)."""
    if not master_enabled():
        return
    actionable = any(
        r.verdict is not CalibrationVerdict.UNCALIBRATED
        for r in report.per_dimension
    )
    if not actionable:
        return
    try:
        from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
            EVENT_TYPE_MIRROR_SELF_CALIBRATED,
            publish_task_event,
        )
        per_dim_summary = {
            r.dimension.value: {
                "verdict": r.verdict.value,
                "accuracy": r.accuracy,
                "sample_count": r.sample_count,
            }
            for r in report.per_dimension
        }
        publish_task_event(
            EVENT_TYPE_MIRROR_SELF_CALIBRATED,
            (
                f"system::mirror_self::"
                f"{report.schema_version}"
            ),
            {
                "per_dimension": per_dim_summary,
                "evaluated_at_unix": report.evaluated_at_unix,
                "elapsed_s": report.elapsed_s,
                "schema_version": report.schema_version,
            },
        )
    except Exception:  # noqa: BLE001
        return


# ===========================================================================
# Renderer
# ===========================================================================


def format_mirror_self_panel(
    report: Optional[MirrorSelfReport] = None,
) -> str:
    """Operator-facing 4-axis calibration vector. NEVER raises."""
    if report is None:
        if not master_enabled():
            return (
                f"mirror-self: disabled "
                f"({_ENV_MASTER}=false)"
            )
        return "mirror-self: no report"
    if not report.master_enabled:
        return (
            f"mirror-self: disabled ({_ENV_MASTER}=false)"
        )
    lines = ["🪞 Mirror-Self Calibration Vector"]
    for r in report.per_dimension:
        dg = dimension_glyph(r.dimension)
        vg = verdict_glyph(r.verdict)
        lines.append(
            f"  {dg} {r.dimension.value:<14} "
            f"{vg} {r.verdict.value:<13} "
            f"{r.correct_count:>3}/{r.sample_count:<3} "
            f"= {r.accuracy:.2f}"
        )
    lines.append(f"  diagnostic: {report.diagnostic}")
    return "\n".join(lines)


# ===========================================================================
# AST pins
# ===========================================================================


def register_shipped_invariants() -> list:
    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    target = (
        "backend/core/ouroboros/governance/mirror_self_test.py"
    )

    _EXPECTED_DIMS = {
        "next_phase", "target_file", "risk_tier", "outcome",
    }
    _EXPECTED_VERDICTS = {
        "uncalibrated", "poor", "fair", "good",
    }

    def _validate_dimension_taxonomy(
        tree: ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ClassDef)
                and node.name == "PredictionDimension"
            ):
                found = set()
                for sub in node.body:
                    if (
                        isinstance(sub, ast.Assign)
                        and len(sub.targets) == 1
                        and isinstance(sub.targets[0], ast.Name)
                        and isinstance(sub.value, ast.Constant)
                        and isinstance(sub.value.value, str)
                    ):
                        found.add(sub.value.value)
                missing = _EXPECTED_DIMS - found
                extra = found - _EXPECTED_DIMS
                if missing:
                    return (
                        f"PredictionDimension missing: "
                        f"{sorted(missing)}",
                    )
                if extra:
                    return (
                        f"PredictionDimension drift: "
                        f"{sorted(extra)}",
                    )
                return ()
        return ("PredictionDimension class not found",)

    def _validate_verdict_taxonomy(
        tree: ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ClassDef)
                and node.name == "CalibrationVerdict"
            ):
                found = set()
                for sub in node.body:
                    if (
                        isinstance(sub, ast.Assign)
                        and len(sub.targets) == 1
                        and isinstance(sub.targets[0], ast.Name)
                        and isinstance(sub.value, ast.Constant)
                        and isinstance(sub.value.value, str)
                    ):
                        found.add(sub.value.value)
                missing = _EXPECTED_VERDICTS - found
                extra = found - _EXPECTED_VERDICTS
                if missing:
                    return (
                        f"CalibrationVerdict missing: "
                        f"{sorted(missing)}",
                    )
                if extra:
                    return (
                        f"CalibrationVerdict drift: "
                        f"{sorted(extra)}",
                    )
                return ()
        return ("CalibrationVerdict class not found",)

    def _validate_authority_asymmetry(
        tree: ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        forbidden = (
            "backend.core.ouroboros.governance.orchestrator",
            "backend.core.ouroboros.governance.iron_gate",
            "backend.core.ouroboros.governance.policy",
            "backend.core.ouroboros.governance.providers",
            "backend.core.ouroboros.governance.candidate_generator",
            "backend.core.ouroboros.governance.urgency_router",
            "backend.core.ouroboros.governance.change_engine",
            "backend.core.ouroboros.governance.semantic_guardian",
            "backend.core.ouroboros.governance.auto_committer",
            "backend.core.ouroboros.governance.risk_tier_floor",
        )
        violations: List[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if any(mod == f for f in forbidden):
                    violations.append(
                        f"forbidden authority import: {mod}",
                    )
        return tuple(violations)

    def _validate_master_default_false(
        tree: ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
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
                    "master_enabled() must call _flag(...) "
                    "with default=False per §33.1",
                )
        return ("master_enabled() not found",)

    def _validate_composes_canonical(
        tree: ast.AST, source: str,
    ) -> tuple:
        violations: List[str] = []
        if "cross_process_jsonl" not in source:
            violations.append(
                "must compose canonical cross_process_jsonl "
                "(no parallel JSONL writer)",
            )
        if "belief_revision_ledger" not in source:
            violations.append(
                "must compose Wave 4 #9 belief_revision_ledger "
                "(falsified predictions bridge into broader "
                "calibration loop)",
            )
        if "governance_boundary_gate" not in source:
            violations.append(
                "must compose Wave 2 #5 "
                "governance_boundary_gate (cage-touch flag "
                "for TARGET_FILE dimension)",
            )
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name=(
                "mirror_self_dimension_taxonomy_closed"
            ),
            target_file=target,
            description=(
                "PredictionDimension 4-value taxonomy "
                "bytes-pinned."
            ),
            validate=_validate_dimension_taxonomy,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "mirror_self_verdict_taxonomy_closed"
            ),
            target_file=target,
            description=(
                "CalibrationVerdict 4-value taxonomy "
                "bytes-pinned."
            ),
            validate=_validate_verdict_taxonomy,
        ),
        ShippedCodeInvariant(
            invariant_name="mirror_self_authority_asymmetry",
            target_file=target,
            description=(
                "Substrate purity — pure ledger + calibration "
                "computer. MUST NOT import orchestrator / "
                "iron_gate / policy / providers / "
                "candidate_generator / urgency_router / "
                "change_engine / semantic_guardian / "
                "auto_committer / risk_tier_floor."
            ),
            validate=_validate_authority_asymmetry,
        ),
        ShippedCodeInvariant(
            invariant_name="mirror_self_master_default_false",
            target_file=target,
            description=(
                "§33.1 cognitive substrate default-FALSE."
            ),
            validate=_validate_master_default_false,
        ),
        ShippedCodeInvariant(
            invariant_name="mirror_self_composes_canonical",
            target_file=target,
            description=(
                "Substrate composes canonical "
                "cross_process_jsonl + Wave 4 #9 "
                "belief_revision_ledger + Wave 2 #5 "
                "governance_boundary_gate — no parallel "
                "implementations."
            ),
            validate=_validate_composes_canonical,
        ),
    ]


# ===========================================================================
# FlagRegistry seeds
# ===========================================================================


def register_flags(registry: Any) -> int:
    from backend.core.ouroboros.governance.flag_registry import (
        Category,
        FlagSpec,
        FlagType,
    )

    src = (
        "backend/core/ouroboros/governance/mirror_self_test.py"
    )

    seeds = [
        FlagSpec(
            name=_ENV_MASTER,
            type=FlagType.BOOL,
            default=False,
            description=(
                "Mirror-Self test master switch. §33.1 "
                "cognitive substrate default-FALSE. When on, "
                "the substrate records record_prediction at "
                "op-start + record_actual at op-end across 4 "
                "dimensions (NEXT_PHASE / TARGET_FILE / "
                "RISK_TIER / OUTCOME) and computes per-"
                "dimension calibration (UNCALIBRATED / POOR "
                "/ FAIR / GOOD). Falsified predictions "
                "optionally bridge into Wave 4 #9 belief "
                "ledger as falsifying-evidence rows. Closes "
                "§40 Wave 4 #14 (PRD v2.99+)."
            ),
            category=Category.SAFETY,
            source_file=src,
            example=f"{_ENV_MASTER}=true",
        ),
        FlagSpec(
            name=_ENV_PERSIST,
            type=FlagType.BOOL,
            default=True,
            description=(
                "Sub-flag — gate §33.4 JSONL audit writes. "
                "Default True when master on."
            ),
            category=Category.SAFETY,
            source_file=src,
            example=f"{_ENV_PERSIST}=false",
        ),
        FlagSpec(
            name=_ENV_BELIEF_BRIDGE,
            type=FlagType.BOOL,
            default=True,
            description=(
                "Sub-flag — gate the belief_revision_ledger "
                "(Wave 4 #9) falsifying-evidence bridge for "
                "wrong predictions. Default True so the "
                "broader calibration loop sees metacognition "
                "misses without operator wiring."
            ),
            category=Category.SAFETY,
            source_file=src,
            example=f"{_ENV_BELIEF_BRIDGE}=false",
        ),
        FlagSpec(
            name=_ENV_MIN_SAMPLE,
            type=FlagType.INT,
            default=_DEFAULT_MIN_SAMPLE,
            description=(
                "Minimum predictions per dimension before a "
                "calibration verdict moves from UNCALIBRATED "
                "to POOR/FAIR/GOOD. Defaults to 5. Clamped to "
                "[1, 10_000]."
            ),
            category=Category.TUNING,
            source_file=src,
            example=f"{_ENV_MIN_SAMPLE}=10",
        ),
        FlagSpec(
            name=_ENV_WINDOW_S,
            type=FlagType.INT,
            default=_DEFAULT_WINDOW_S,
            description=(
                "Time window (seconds) for recent prediction "
                "filtering. Defaults to 86400 (24h). Clamped "
                "to [60, 30 days]."
            ),
            category=Category.TUNING,
            source_file=src,
            example=f"{_ENV_WINDOW_S}=3600",
        ),
        FlagSpec(
            name=_ENV_MAX_RECORDS,
            type=FlagType.INT,
            default=_DEFAULT_MAX_RECORDS,
            description=(
                "Cap on ledger rows read per evaluation. "
                "Clamped to [1, 1_000_000]."
            ),
            category=Category.CAPACITY,
            source_file=src,
            example=f"{_ENV_MAX_RECORDS}=5000",
        ),
    ]

    count = 0
    for spec in seeds:
        try:
            registry.register(spec)
            count += 1
        except Exception:  # noqa: BLE001
            continue
    return count


__all__ = [
    "MIRROR_SELF_SCHEMA_VERSION",
    "PredictionDimension",
    "CalibrationVerdict",
    "PredictionRow",
    "ActualRow",
    "CalibrationReport",
    "MirrorSelfReport",
    "master_enabled",
    "persistence_enabled",
    "belief_bridge_enabled",
    "min_sample_size",
    "window_s",
    "max_records",
    "ledger_path",
    "dimension_glyph",
    "verdict_glyph",
    "record_prediction",
    "record_actual",
    "compute_calibration",
    "compute_all_calibrations",
    "format_mirror_self_panel",
    "register_shipped_invariants",
    "register_flags",
]
