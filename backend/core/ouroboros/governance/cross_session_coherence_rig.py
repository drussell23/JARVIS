"""
Cross-Session Coherence Empirical Rig
======================================

Closes §40 Wave 3 #6 — the **empirical closure-verdict rig** that
operator binding (§40.5) calls for:

  "Move 7 + Move 4 + M9 measure drift mathematically. The system
   hasn't run long enough (>7 days continuous, >50 ops) to validate
   repeated runs converge (signatures stabilize, lessons persist).
   Build empirical closure-verdict rig that reads final session
   summaries + plots coherence curves. Feeds PhD §6 dissertation
   eval."

The :mod:`cross_session_harness` substrate (shipped v2.80 on
2026-05-09) provides the **mathematical primitives**:
:class:`CoherenceAxis` (4-value), :class:`DriftLevel` (4-value),
:class:`AxisDigest`, :class:`AxisDrift`, :func:`compute_drift`,
:func:`aggregate_digest`. Per the v2.80 spec, ``aggregate_digest``
snapshots the **current** state of the four canonical cross-
session memory substrates (UserPreferenceStore /
AdaptationLedger / SemanticIndex / LastSessionSummary).

This module is the **empirical rig that walks the live archive**:
for each archived session, it derives a per-session AxisDigest
from the session's own ``summary.json`` (NOT a snapshot of
present state). Adjacent sessions are then diffed via canonical
:func:`compute_drift` to produce a time-ordered coherence curve
suitable for PhD-side plotting + dissertation evidence.

Composition contract — thin pure-function wrapper, zero parallel
state, zero new math primitives:

* :func:`session_archive.find_sessions` — canonical chronological
  walker over ``.jarvis/session_archive.db``. Returns frozen
  SessionRecord tuples sorted newest-first.
* :func:`last_session_summary.get_default_summary().load` — canonical
  parser over ``.ouroboros/sessions/<id>/summary.json``. Returns
  frozen SessionRecord tuples (note: different class shape from
  session_archive's SessionRecord — both reused via duck-typed
  field access).
* :data:`cross_session_harness.CoherenceAxis` — canonical 4-value
  enum. AST-pinned no parallel taxonomy.
* :data:`cross_session_harness.DriftLevel` — canonical 4-value
  enum. AST-pinned no parallel taxonomy.
* :class:`cross_session_harness.AxisDigest` — canonical per-axis
  fingerprint shape. The rig builds these from summary.json
  fields rather than from live substrate state — that's the
  "empirical" framing: we measure what each shipped session
  observed, not what the current process observes.
* :func:`cross_session_harness.compute_drift` — canonical
  per-axis drift classifier. Reused verbatim.

§33.1 cognitive substrate ``JARVIS_CROSS_SESSION_COHERENCE_RIG_ENABLED``
default-**FALSE**. Operator opts in to run the rig; PhD-side
notebook composes the public :func:`walk_session_arc` accessor
and consumes :meth:`ArcDriftReport.to_dict` for plotting via
matplotlib externally.

Authority asymmetry (AST-pinned): imports stdlib +
cross_session_harness + session_archive + last_session_summary
ONLY. Does NOT import orchestrator / iron_gate / policy /
providers / candidate_generator / urgency_router / change_engine
/ semantic_guardian / auto_committer / risk_tier_floor. Pure-
function rig is read-only; produces machine-readable JSON.
"""
from __future__ import annotations

import ast
import enum
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass
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


CROSS_SESSION_COHERENCE_RIG_SCHEMA_VERSION: str = (
    "cross_session_coherence_rig.1"
)


# ===========================================================================
# Env knobs
# ===========================================================================


_ENV_MASTER = "JARVIS_CROSS_SESSION_COHERENCE_RIG_ENABLED"
_ENV_MAX_SESSIONS = (
    "JARVIS_CROSS_SESSION_COHERENCE_RIG_MAX_SESSIONS"
)

_DEFAULT_MAX_SESSIONS = 50
_MIN_MAX_SESSIONS = 2
_MAX_MAX_SESSIONS = 10_000


_TRUTHY: FrozenSet[str] = frozenset({"1", "true", "yes", "on"})


def _flag(name: str, *, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in _TRUTHY


def master_enabled() -> bool:
    """§33.1 cognitive substrate — default-FALSE.

    Operator-paced opt-in. The rig is a heavy I/O surface
    (walks session_archive + parses N summary.json files) so
    we keep it dormant by default; opt-in via env flip when
    running the empirical evaluation."""
    return _flag(_ENV_MASTER, default=False)


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


def max_sessions() -> int:
    """Defensive ceiling on the session arc walked per call.
    Clamped to [2, 10_000]; default 50."""
    return _read_clamped_int(
        _ENV_MAX_SESSIONS,
        _DEFAULT_MAX_SESSIONS,
        _MIN_MAX_SESSIONS,
        _MAX_MAX_SESSIONS,
    )


# ===========================================================================
# Canonical accessor composition (no parallel state, no parallel taxonomy)
# ===========================================================================


def _canonical_coherence_axis():  # noqa: ANN202
    """Lazy-import canonical CoherenceAxis enum. NEVER raises —
    falls back to a 4-value tuple of string identifiers if
    substrate unavailable (defensive)."""
    try:
        from backend.core.ouroboros.governance.cross_session_harness import (  # noqa: E501
            CoherenceAxis,
        )
        return CoherenceAxis
    except Exception:  # noqa: BLE001
        return None


def _canonical_drift_level():  # noqa: ANN202
    """Lazy-import canonical DriftLevel enum. NEVER raises."""
    try:
        from backend.core.ouroboros.governance.cross_session_harness import (  # noqa: E501
            DriftLevel,
        )
        return DriftLevel
    except Exception:  # noqa: BLE001
        return None


def _canonical_axis_digest():  # noqa: ANN202
    """Lazy-import canonical AxisDigest dataclass. NEVER raises."""
    try:
        from backend.core.ouroboros.governance.cross_session_harness import (  # noqa: E501
            AxisDigest,
        )
        return AxisDigest
    except Exception:  # noqa: BLE001
        return None


def _canonical_compute_drift():  # noqa: ANN202
    """Lazy-import canonical compute_drift function. NEVER raises."""
    try:
        from backend.core.ouroboros.governance.cross_session_harness import (  # noqa: E501
            compute_drift,
        )
        return compute_drift
    except Exception:  # noqa: BLE001
        return None


# ===========================================================================
# Per-session axis-fingerprint derivation
# ===========================================================================


def _stable_sha16(payload: str) -> str:
    """Canonical 16-char hex SHA-256 prefix — matches AxisDigest's
    ``content_hash`` shape."""
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _fingerprint_user_prefs(summary_record: Any) -> Dict[str, Any]:
    """Derive a USER_PREFS-axis fingerprint from one session.

    Proxy: hash of last_apply_files + last_apply_mode. When the
    session landed user-visible changes, this fingerprint
    reflects them. Two sessions with identical applied-file
    sets produce identical fingerprints — that's the empirical
    "preferences stable" signal.

    Returns ``{"hash": str, "count": int, "diagnostic": str}``.
    NEVER raises.
    """
    try:
        files_raw = getattr(summary_record, "last_apply_files", "")
        mode_raw = getattr(summary_record, "last_apply_mode", "")
        files = sorted(
            (files_raw or "").split(",") if files_raw else []
        )
        canonical = json.dumps(
            {"files": files, "mode": str(mode_raw or "")},
            sort_keys=True,
        )
        return {
            "hash": _stable_sha16(canonical),
            "count": len(files),
            "diagnostic": "",
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "hash": "",
            "count": 0,
            "diagnostic": f"user_prefs_extract_failed:{type(exc).__name__}",
        }


def _fingerprint_adaptations(
    summary_record: Any,
) -> Dict[str, Any]:
    """Derive ADAPTATIONS-axis fingerprint. Proxy: hash of
    M9 drift_status + drift_ratio. Two sessions with the same
    drift profile produce identical fingerprints.
    NEVER raises."""
    try:
        status = str(
            getattr(summary_record, "drift_status", "") or "",
        )
        ratio = getattr(summary_record, "drift_ratio", 0.0) or 0.0
        canonical = json.dumps(
            {"status": status, "ratio": round(float(ratio), 6)},
            sort_keys=True,
        )
        return {
            "hash": _stable_sha16(canonical),
            "count": 1 if status else 0,
            "diagnostic": "",
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "hash": "",
            "count": 0,
            "diagnostic": (
                f"adaptations_extract_failed:{type(exc).__name__}"
            ),
        }


def _fingerprint_semantic_centroid(
    summary_record: Any,
) -> Dict[str, Any]:
    """Derive SEMANTIC_CENTROID-axis fingerprint. Proxy: hash
    of convergence_state. The full semantic centroid (1024-dim
    vector) isn't in summary.json; convergence_state is the
    canonical operator-visible proxy for "did the system's
    semantic understanding stabilize?". NEVER raises."""
    try:
        state = str(
            getattr(summary_record, "convergence_state", "") or "",
        )
        canonical = json.dumps(
            {"convergence_state": state}, sort_keys=True,
        )
        return {
            "hash": _stable_sha16(canonical),
            "count": 1 if state else 0,
            "diagnostic": "",
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "hash": "",
            "count": 0,
            "diagnostic": (
                f"semantic_centroid_extract_failed:"
                f"{type(exc).__name__}"
            ),
        }


def _fingerprint_session_history(
    summary_record: Any,
) -> Dict[str, Any]:
    """Derive SESSION_HISTORY-axis fingerprint. Proxy: hash of
    the session's own shape (stop_reason + ops_count +
    stats_attempted + cost_total). Identical session shapes →
    identical fingerprints. The 'session history is stable'
    signal. NEVER raises."""
    try:
        canonical = json.dumps(
            {
                "stop_reason": str(getattr(
                    summary_record, "stop_reason", "",
                ) or ""),
                "ops_count": int(getattr(
                    summary_record, "stats_attempted", 0,
                ) or 0),
                "completed": int(getattr(
                    summary_record, "stats_completed", 0,
                ) or 0),
                "cost_total_rounded": round(
                    float(getattr(
                        summary_record, "cost_total", 0.0,
                    ) or 0.0),
                    4,
                ),
            },
            sort_keys=True,
        )
        ops_count = int(getattr(
            summary_record, "stats_attempted", 0,
        ) or 0)
        return {
            "hash": _stable_sha16(canonical),
            "count": ops_count,
            "diagnostic": "",
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "hash": "",
            "count": 0,
            "diagnostic": (
                f"session_history_extract_failed:"
                f"{type(exc).__name__}"
            ),
        }


# Per-axis fingerprint dispatch — bytes-pinned via AST.
_FINGERPRINTERS: Dict[str, Any] = {
    "user_prefs": _fingerprint_user_prefs,
    "adaptations": _fingerprint_adaptations,
    "semantic_centroid": _fingerprint_semantic_centroid,
    "session_history": _fingerprint_session_history,
}


def build_axis_digests_for_session(
    summary_record: Any,
) -> "Mapping[str, Any]":
    """Build the 4 canonical AxisDigest objects for one session.

    Composes canonical CoherenceAxis enum + AxisDigest dataclass
    so the result drops directly into :func:`compute_drift`.
    NEVER raises — substrate-unavailable axes produce
    diagnostic-tagged digests.

    Returns ``Mapping[axis_value_str, AxisDigest]``.
    """
    AxisDigest = _canonical_axis_digest()
    CoherenceAxis = _canonical_coherence_axis()
    if AxisDigest is None or CoherenceAxis is None:
        return {}
    out: Dict[str, Any] = {}
    for axis_value, fingerprinter in _FINGERPRINTERS.items():
        try:
            axis_enum = CoherenceAxis.coerce(axis_value)
            fp = fingerprinter(summary_record)
            out[axis_value] = AxisDigest(
                axis=axis_enum,
                record_count=int(fp.get("count", 0)),
                content_hash=str(fp.get("hash", "")),
                sample_size_bytes=0,
                diagnostic=str(fp.get("diagnostic", "")),
            )
        except Exception:  # noqa: BLE001 — defensive per-axis
            continue
    return out


# ===========================================================================
# Closed taxonomy — arc-level verdict
# ===========================================================================


class ArcVerdict(str, enum.Enum):
    """Closed 4-value arc-level verdict — bytes-pinned via AST.

    Aggregates the per-axis-per-boundary DriftLevel results into
    a single operator-visible signal.

    * ``COHERENT`` — every axis at every boundary is STABLE.
      System has converged (PhD claim of long-horizon coherence
      empirically supported on this arc).
    * ``MOSTLY_COHERENT`` — coherence_ratio ≥ 0.75. Most
      boundaries stable; some drift acceptable.
    * ``DRIFTING`` — coherence_ratio < 0.75 but > 0. System
      hasn't converged on this arc.
    * ``INSUFFICIENT_DATA`` — fewer than 2 sessions in arc
      OR substrate unavailable OR master disabled.
    """

    COHERENT = "coherent"
    MOSTLY_COHERENT = "mostly_coherent"
    DRIFTING = "drifting"
    INSUFFICIENT_DATA = "insufficient_data"


# Coherence-ratio threshold for COHERENT vs MOSTLY_COHERENT
# (defensive — operator-tunable via env if PhD eval needs
# stricter / looser criterion).
_COHERENT_THRESHOLD = 1.0
_MOSTLY_COHERENT_THRESHOLD = 0.75


# ===========================================================================
# §33.5 frozen versioned artifacts
# ===========================================================================


@dataclass(frozen=True)
class BoundaryDrift:
    """One (axis × boundary) drift record. Frozen."""

    boundary_index: int
    axis: str
    level: str
    record_count_delta: int
    hash_changed: bool
    diagnostic: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "boundary_index": int(self.boundary_index),
            "axis": self.axis,
            "level": self.level,
            "record_count_delta": int(self.record_count_delta),
            "hash_changed": bool(self.hash_changed),
            "diagnostic": self.diagnostic[:256],
        }


@dataclass(frozen=True)
class ArcDriftReport:
    """Aggregate arc-level report — frozen §33.5 artifact.

    JSON-serializable via :meth:`to_dict`. PhD-side notebook
    consumes this directly for plotting coherence curves.
    """

    walked_at_unix: float
    master_enabled: bool
    verdict: ArcVerdict
    session_count: int
    session_arc: Tuple[str, ...]
    """Chronologically-ordered session IDs in this arc."""
    boundary_count: int
    """N-1 for an N-session arc."""
    axis_count: int
    """4 — one per canonical CoherenceAxis."""
    drifts: Tuple[BoundaryDrift, ...]
    """Per-axis-per-boundary records. Total = axis_count *
    boundary_count. Bounded at 10_000 entries."""
    stable_count: int
    drifting_count: int
    diverged_count: int
    corrupted_count: int
    coherence_ratio: float
    """stable_count / (stable_count + drifting_count +
    diverged_count). Excludes CORRUPTED — those represent
    measurement failure, not coherence signal."""
    per_axis_timeseries: Mapping[str, Tuple[Tuple[int, str], ...]]
    """Per-axis time series — axis_value →
    ((boundary_idx, level), ...). The plotting payload."""
    elapsed_s: float
    diagnostic: str
    schema_version: str = (
        CROSS_SESSION_COHERENCE_RIG_SCHEMA_VERSION
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "walked_at_unix": float(self.walked_at_unix),
            "master_enabled": bool(self.master_enabled),
            "verdict": self.verdict.value,
            "session_count": int(self.session_count),
            "session_arc": list(self.session_arc),
            "boundary_count": int(self.boundary_count),
            "axis_count": int(self.axis_count),
            "drifts": [d.to_dict() for d in self.drifts],
            "stable_count": int(self.stable_count),
            "drifting_count": int(self.drifting_count),
            "diverged_count": int(self.diverged_count),
            "corrupted_count": int(self.corrupted_count),
            "coherence_ratio": float(self.coherence_ratio),
            "per_axis_timeseries": {
                k: [list(t) for t in v]
                for k, v in self.per_axis_timeseries.items()
            },
            "elapsed_s": float(self.elapsed_s),
            "diagnostic": self.diagnostic[:512],
            "schema_version": self.schema_version,
        }


_DRIFTS_BOUND = 10_000


# ===========================================================================
# Session archive walker (composes canonical session_archive)
# ===========================================================================


def _load_session_summaries(
    since_epoch: Optional[float],
    until_epoch: Optional[float],
    limit: int,
) -> Tuple[Any, ...]:
    """Compose canonical last_session_summary.load() — returns
    chronological tuple of SessionRecord objects (oldest first).
    NEVER raises — empty tuple on any failure.

    Why last_session_summary instead of session_archive directly:
    LastSessionSummary parses the actual summary.json fields
    (drift_status / convergence_state / last_apply_files / etc.)
    that the fingerprinters need. session_archive.SessionRecord
    only carries the index metadata (started_at / outcome /
    cost_usd) and would require a second parse.
    """
    try:
        from backend.core.ouroboros.governance.last_session_summary import (  # noqa: E501
            get_default_summary,
        )
    except Exception:  # noqa: BLE001
        return ()
    try:
        lss = get_default_summary()
        records = lss.load(n_sessions=int(limit))
    except Exception:  # noqa: BLE001
        return ()
    if not records:
        return ()
    # last_session_summary returns newest-first; reverse for
    # chronological order.
    chronological = list(reversed(records))
    # Apply since/until bounds if supplied — defensive filter.
    if since_epoch is not None or until_epoch is not None:
        filtered: List[Any] = []
        for r in chronological:
            ts = getattr(r, "started_at_epoch", None)
            if ts is None:
                ts = getattr(r, "ended_at_epoch", None)
            try:
                ts = float(ts) if ts is not None else None
            except (TypeError, ValueError):
                ts = None
            if since_epoch is not None and ts is not None and (
                ts < float(since_epoch)
            ):
                continue
            if until_epoch is not None and ts is not None and (
                ts > float(until_epoch)
            ):
                continue
            filtered.append(r)
        chronological = filtered
    return tuple(chronological)


# ===========================================================================
# Top-level rig — empirical closure-verdict producer
# ===========================================================================


def walk_session_arc(
    *,
    since_epoch: Optional[float] = None,
    until_epoch: Optional[float] = None,
    limit: Optional[int] = None,
    session_records_override: Optional[Sequence[Any]] = None,
) -> ArcDriftReport:
    """Empirical closure-verdict rig — top-level public API.

    Walks N chronologically-ordered session summaries; builds
    per-axis fingerprints from each; diffs adjacent pairs via
    the canonical :func:`compute_drift`; aggregates into a
    JSON-serializable :class:`ArcDriftReport` suitable for
    PhD-side plotting.

    Parameters
    ----------
    since_epoch / until_epoch:
        Wall-clock bounds on the session arc. Both inclusive;
        ``None`` means unbounded.
    limit:
        Maximum sessions walked. Defaults to env-tunable
        :func:`max_sessions` (default 50). Clamped to
        [2, 10_000].
    session_records_override:
        Caller-injectable session records for hermetic testing.
        Bypasses the canonical loader entirely.

    Returns
    -------
    ArcDriftReport
        Frozen §33.5 versioned artifact. NEVER raises.

    Operator-binding semantics:
      * Master flag off → INSUFFICIENT_DATA verdict, no I/O.
      * <2 sessions → INSUFFICIENT_DATA (no boundary to diff).
      * compute_drift unavailable → INSUFFICIENT_DATA + diagnostic.
      * Otherwise → COHERENT / MOSTLY_COHERENT / DRIFTING based
        on coherence_ratio.
    """
    started = time.time()

    if not master_enabled():
        return ArcDriftReport(
            walked_at_unix=started,
            master_enabled=False,
            verdict=ArcVerdict.INSUFFICIENT_DATA,
            session_count=0,
            session_arc=(),
            boundary_count=0,
            axis_count=len(_FINGERPRINTERS),
            drifts=(),
            stable_count=0,
            drifting_count=0,
            diverged_count=0,
            corrupted_count=0,
            coherence_ratio=0.0,
            per_axis_timeseries={},
            elapsed_s=0.0,
            diagnostic=(
                f"master flag {_ENV_MASTER}=false — operator "
                "opt-in workflow; no I/O performed"
            ),
        )

    effective_limit = (
        int(limit) if limit is not None else max_sessions()
    )
    effective_limit = max(
        _MIN_MAX_SESSIONS, min(_MAX_MAX_SESSIONS, effective_limit),
    )

    if session_records_override is not None:
        records = tuple(session_records_override)
    else:
        records = _load_session_summaries(
            since_epoch, until_epoch, effective_limit,
        )

    if len(records) < 2:
        return ArcDriftReport(
            walked_at_unix=started,
            master_enabled=True,
            verdict=ArcVerdict.INSUFFICIENT_DATA,
            session_count=len(records),
            session_arc=tuple(
                str(getattr(r, "session_id", "")) for r in records
            ),
            boundary_count=0,
            axis_count=len(_FINGERPRINTERS),
            drifts=(),
            stable_count=0,
            drifting_count=0,
            diverged_count=0,
            corrupted_count=0,
            coherence_ratio=0.0,
            per_axis_timeseries={},
            elapsed_s=time.time() - started,
            diagnostic=(
                f"only {len(records)} session(s) in arc — "
                "minimum 2 required to compute drift"
            ),
        )

    compute_drift = _canonical_compute_drift()
    if compute_drift is None:
        return ArcDriftReport(
            walked_at_unix=started,
            master_enabled=True,
            verdict=ArcVerdict.INSUFFICIENT_DATA,
            session_count=len(records),
            session_arc=tuple(
                str(getattr(r, "session_id", "")) for r in records
            ),
            boundary_count=0,
            axis_count=len(_FINGERPRINTERS),
            drifts=(),
            stable_count=0,
            drifting_count=0,
            diverged_count=0,
            corrupted_count=0,
            coherence_ratio=0.0,
            per_axis_timeseries={},
            elapsed_s=time.time() - started,
            diagnostic=(
                "canonical cross_session_harness.compute_drift "
                "unavailable"
            ),
        )

    # Build per-session digests.
    session_digests: List[Mapping[str, Any]] = []
    session_ids: List[str] = []
    for record in records:
        digests = build_axis_digests_for_session(record)
        if digests:
            session_digests.append(digests)
            session_ids.append(
                str(getattr(record, "session_id", "")),
            )

    if len(session_digests) < 2:
        return ArcDriftReport(
            walked_at_unix=started,
            master_enabled=True,
            verdict=ArcVerdict.INSUFFICIENT_DATA,
            session_count=len(session_digests),
            session_arc=tuple(session_ids),
            boundary_count=0,
            axis_count=len(_FINGERPRINTERS),
            drifts=(),
            stable_count=0,
            drifting_count=0,
            diverged_count=0,
            corrupted_count=0,
            coherence_ratio=0.0,
            per_axis_timeseries={},
            elapsed_s=time.time() - started,
            diagnostic=(
                "fewer than 2 sessions yielded valid digests"
            ),
        )

    # Walk adjacent pairs.
    drifts: List[BoundaryDrift] = []
    per_axis_series: Dict[str, List[Tuple[int, str]]] = {
        axis: [] for axis in _FINGERPRINTERS
    }
    stable = drifting = diverged = corrupted = 0
    for boundary_idx in range(len(session_digests) - 1):
        before = session_digests[boundary_idx]
        after = session_digests[boundary_idx + 1]
        for axis in _FINGERPRINTERS:
            try:
                b = before.get(axis)
                a = after.get(axis)
                if b is None or a is None:
                    continue
                axis_drift = compute_drift(before=b, after=a)
                level_val = ""
                lvl = getattr(axis_drift, "level", None)
                if hasattr(lvl, "value"):
                    level_val = str(lvl.value)
                else:
                    level_val = str(lvl or "")
                if level_val == "stable":
                    stable += 1
                elif level_val == "drifting":
                    drifting += 1
                elif level_val == "diverged":
                    diverged += 1
                elif level_val == "corrupted":
                    corrupted += 1
                if len(drifts) < _DRIFTS_BOUND:
                    drifts.append(BoundaryDrift(
                        boundary_index=boundary_idx,
                        axis=axis,
                        level=level_val,
                        record_count_delta=int(
                            getattr(
                                axis_drift,
                                "record_count_delta",
                                0,
                            ) or 0,
                        ),
                        hash_changed=bool(
                            getattr(
                                axis_drift, "hash_changed", False,
                            ),
                        ),
                        diagnostic=str(
                            getattr(
                                axis_drift, "diagnostic", "",
                            ) or "",
                        ),
                    ))
                per_axis_series[axis].append(
                    (boundary_idx, level_val),
                )
            except Exception:  # noqa: BLE001 — defensive
                continue

    meaningful = stable + drifting + diverged
    coherence_ratio = (
        float(stable) / float(meaningful)
        if meaningful > 0 else 0.0
    )

    if coherence_ratio >= _COHERENT_THRESHOLD:
        verdict = ArcVerdict.COHERENT
    elif coherence_ratio >= _MOSTLY_COHERENT_THRESHOLD:
        verdict = ArcVerdict.MOSTLY_COHERENT
    else:
        verdict = ArcVerdict.DRIFTING

    diagnostic = (
        f"walked {len(session_digests)} sessions across "
        f"{len(session_digests) - 1} boundaries; "
        f"stable={stable} drifting={drifting} "
        f"diverged={diverged} corrupted={corrupted}; "
        f"coherence_ratio={coherence_ratio:.3f}"
    )

    return ArcDriftReport(
        walked_at_unix=started,
        master_enabled=True,
        verdict=verdict,
        session_count=len(session_digests),
        session_arc=tuple(session_ids),
        boundary_count=len(session_digests) - 1,
        axis_count=len(_FINGERPRINTERS),
        drifts=tuple(drifts),
        stable_count=stable,
        drifting_count=drifting,
        diverged_count=diverged,
        corrupted_count=corrupted,
        coherence_ratio=coherence_ratio,
        per_axis_timeseries={
            k: tuple(v) for k, v in per_axis_series.items()
        },
        elapsed_s=time.time() - started,
        diagnostic=diagnostic,
    )


# ===========================================================================
# Renderer
# ===========================================================================


def format_arc_panel(
    report: Optional[ArcDriftReport] = None,
) -> str:
    """Operator-facing summary panel. NEVER raises."""
    if report is None:
        if not master_enabled():
            return (
                f"cross-session coherence rig: disabled "
                f"({_ENV_MASTER}=false)"
            )
        report = walk_session_arc()
    if not report.master_enabled:
        return (
            f"cross-session coherence rig: disabled "
            f"({_ENV_MASTER}=false)"
        )
    lines = [
        f"📐 Cross-Session Coherence Arc  "
        f"({report.verdict.value})",
        f"  session_count       : {report.session_count}",
        f"  boundary_count      : {report.boundary_count}",
        f"  stable / drifting   : {report.stable_count} / "
        f"{report.drifting_count}",
        f"  diverged / corrupted: {report.diverged_count} / "
        f"{report.corrupted_count}",
        f"  coherence_ratio     : {report.coherence_ratio:.3f}",
    ]
    if report.per_axis_timeseries:
        lines.append("  per-axis time series:")
        for axis, series in sorted(
            report.per_axis_timeseries.items(),
        ):
            level_counts: Dict[str, int] = {}
            for (_idx, lvl) in series:
                level_counts[lvl] = level_counts.get(lvl, 0) + 1
            summary = " ".join(
                f"{lvl}={n}"
                for lvl, n in sorted(level_counts.items())
            )
            lines.append(f"    {axis:<20} : {summary}")
    lines.append(f"  diagnostic          : {report.diagnostic}")
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
        "backend/core/ouroboros/governance/"
        "cross_session_coherence_rig.py"
    )

    _EXPECTED_VERDICTS = {
        "coherent", "mostly_coherent",
        "drifting", "insufficient_data",
    }

    def _validate_verdict_taxonomy(
        tree: ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ClassDef)
                and node.name == "ArcVerdict"
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
                        f"ArcVerdict missing: "
                        f"{sorted(missing)}",
                    )
                if extra:
                    return (
                        f"ArcVerdict drift: {sorted(extra)}",
                    )
                return ()
        return ("ArcVerdict class not found",)

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

    def _validate_composes_canonical_harness(
        tree: ast.AST, source: str,
    ) -> tuple:
        violations: List[str] = []
        if "cross_session_harness" not in source:
            violations.append(
                "must compose canonical "
                "cross_session_harness (no parallel "
                "drift math)",
            )
        if "compute_drift" not in source:
            violations.append(
                "must compose canonical compute_drift "
                "function — no parallel per-axis drift "
                "classifier",
            )
        if "AxisDigest" not in source:
            violations.append(
                "must compose canonical AxisDigest dataclass "
                "— no parallel digest type",
            )
        if "CoherenceAxis" not in source:
            violations.append(
                "must compose canonical CoherenceAxis enum "
                "— no parallel axis taxonomy",
            )
        return tuple(violations)

    def _validate_fingerprinter_coverage(
        tree: ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        """The _FINGERPRINTERS dict MUST cover all 4 canonical
        CoherenceAxis values verbatim. Drift would silently
        drop an axis from the empirical arc.

        Walks both ``ast.Assign`` (plain) and ``ast.AnnAssign``
        (type-annotated) nodes — the canonical source uses
        ``_FINGERPRINTERS: Dict[str, Any] = {...}`` which is
        an AnnAssign.
        """
        required_keys = {
            "user_prefs",
            "adaptations",
            "semantic_centroid",
            "session_history",
        }
        for node in ast.walk(tree):
            # Plain assignment: _FINGERPRINTERS = {...}
            if (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == "_FINGERPRINTERS"
                and isinstance(node.value, ast.Dict)
            ):
                value_node = node.value
            # Annotated assignment: _FINGERPRINTERS: T = {...}
            elif (
                isinstance(node, ast.AnnAssign)
                and isinstance(node.target, ast.Name)
                and node.target.id == "_FINGERPRINTERS"
                and isinstance(node.value, ast.Dict)
            ):
                value_node = node.value
            else:
                continue
            found = set()
            for k in value_node.keys:
                if (
                    isinstance(k, ast.Constant)
                    and isinstance(k.value, str)
                ):
                    found.add(k.value)
            missing = required_keys - found
            if missing:
                return (
                    f"_FINGERPRINTERS missing axes: "
                    f"{sorted(missing)}",
                )
            extra = found - required_keys
            if extra:
                return (
                    f"_FINGERPRINTERS has unexpected "
                    f"axes: {sorted(extra)}",
                )
            return ()
        return ("_FINGERPRINTERS dict not found",)

    return [
        ShippedCodeInvariant(
            invariant_name=(
                "cross_session_coherence_rig_"
                "verdict_taxonomy_closed"
            ),
            target_file=target,
            description=(
                "ArcVerdict 4-value taxonomy bytes-pinned."
            ),
            validate=_validate_verdict_taxonomy,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "cross_session_coherence_rig_"
                "authority_asymmetry"
            ),
            target_file=target,
            description=(
                "Substrate purity — empirical rig is read-only. "
                "MUST NOT import orchestrator / iron_gate / "
                "policy / providers / candidate_generator / "
                "urgency_router / change_engine / "
                "semantic_guardian / auto_committer / "
                "risk_tier_floor."
            ),
            validate=_validate_authority_asymmetry,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "cross_session_coherence_rig_"
                "master_default_false"
            ),
            target_file=target,
            description=(
                "§33.1 cognitive substrate default-FALSE."
            ),
            validate=_validate_master_default_false,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "cross_session_coherence_rig_"
                "composes_canonical_harness"
            ),
            target_file=target,
            description=(
                "Composes canonical cross_session_harness "
                "primitives (CoherenceAxis / AxisDigest / "
                "compute_drift) — no parallel taxonomy, no "
                "parallel digest type, no parallel drift "
                "classifier."
            ),
            validate=_validate_composes_canonical_harness,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "cross_session_coherence_rig_"
                "fingerprinter_coverage"
            ),
            target_file=target,
            description=(
                "_FINGERPRINTERS dict MUST cover all 4 "
                "canonical CoherenceAxis values verbatim. "
                "Drift = silently dropped axis from empirical "
                "arc."
            ),
            validate=_validate_fingerprinter_coverage,
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
        "backend/core/ouroboros/governance/"
        "cross_session_coherence_rig.py"
    )

    seeds = [
        FlagSpec(
            name=_ENV_MASTER,
            type=FlagType.BOOL,
            default=False,
            description=(
                "Cross-session coherence rig master switch. "
                "§33.1 cognitive substrate default-FALSE. "
                "When on, walk_session_arc() walks the live "
                "session archive + computes per-axis drift "
                "across adjacent sessions. PhD-side notebook "
                "consumes ArcDriftReport.to_dict for plotting."
            ),
            category=Category.OBSERVABILITY,
            source_file=src,
            example=f"{_ENV_MASTER}=true",
        ),
        FlagSpec(
            name=_ENV_MAX_SESSIONS,
            type=FlagType.INT,
            default=_DEFAULT_MAX_SESSIONS,
            description=(
                "Maximum sessions walked per arc. Clamped "
                "to [2, 10_000]. Defaults to 50 — sufficient "
                "for >7-day PhD eval window at typical "
                "operator-paced cadence."
            ),
            category=Category.CAPACITY,
            source_file=src,
            example=f"{_ENV_MAX_SESSIONS}=200",
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
    "CROSS_SESSION_COHERENCE_RIG_SCHEMA_VERSION",
    "ArcVerdict",
    "BoundaryDrift",
    "ArcDriftReport",
    "master_enabled",
    "max_sessions",
    "build_axis_digests_for_session",
    "walk_session_arc",
    "format_arc_panel",
    "register_shipped_invariants",
    "register_flags",
]
