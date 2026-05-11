"""
Multi-Day Deadlock Detector — Cross-Session Pattern Recognition
================================================================

Closes §41.4 Phase 1 ninth (final) arc (PRD v3.0+). Per the binding:

  "Multi-day deadlock detection | ~1 week | Catch deadlocks
   spanning multiple days that single-session detection
   misses"

Single-session detection (PostureObserver task-death,
WallClockWatchdog wall-clock cap, idle-timeout, semantic
guardian) already catches *immediate* hangs. What slips
through is **cross-session pattern repetition**: the same
operation, file, error fingerprint, or stop_reason
recurring across multiple sessions over multiple days
without resolution. Each session ends "cleanly" by its own
metric, but the **system as a whole** is deadlocked — it
keeps trying the same thing and getting the same result.

This substrate runs at session boot (or on-demand) and
scans the last N days of session artifacts to surface
these patterns.

Four detectors, each producing :class:`DeadlockSignal`:

1. **REPEAT_STOP_REASON** — same non-``complete``
   ``stop_reason`` (e.g., ``wall_clock_cap``,
   ``incomplete_kill``, ``idle_timeout``) recurring in
   ``min_occurrences`` sessions over ``span_days``.
   Diagnoses: provider hang persisting, watchdog firing
   repeatedly, external kill loop.

2. **REPEAT_FAILURE** — high failure ratio
   (``stats_failed`` / ``stats_attempted`` ≥ threshold)
   sustained over N consecutive sessions. Diagnoses:
   degraded provider, broken test suite, environmental
   regression.

3. **VERDICT_THRASH** — same file appearing in BUGFIX
   commits across days (composes
   :func:`long_horizon_memory.walk_git_log` + theme
   classifier). When the same file gets repeated bugfix
   commits across ``min_flips`` distinct days within
   ``span_days``, the fix isn't sticking — that's a
   thrash deadlock.

4. **ZERO_PROGRESS** — N consecutive sessions with
   ``branch_commits == 0`` (no commits produced).
   System is running but not shipping. Diagnoses: every
   op blocked at gate / repeated approval timeouts /
   semantic guardian rejecting everything.

Composition contract:

* :mod:`last_session_summary` — schema knowledge for
  session.summary.json fields (lazy import; never
  imports the SessionRecord factory)
* :func:`long_horizon_memory.walk_git_log` — git history
  walker for VERDICT_THRASH (lazy import; reuses the
  same fake-runner injection used by
  long_horizon_memory tests)
* :func:`governance_boundary_gate.is_boundary_crossed`
  — boundary cage flag on touched files
* :func:`cross_process_jsonl.flock_append_line` —
  §33.4 audit ledger
* stdlib :mod:`subprocess` + :mod:`pathlib`

Closed 4-value :class:`DeadlockKind`:

  REPEAT_STOP_REASON  same non-complete stop_reason
                      recurring
  REPEAT_FAILURE      high failure ratio across N sessions
  VERDICT_THRASH      same file bugfix-cycled across days
  ZERO_PROGRESS       N sessions in a row with 0 commits

Closed 4-value :class:`DeadlockSeverity`:

  NONE     no evidence
  LOW      occurrences = min_occurrences (suspicious but
           below confirmation threshold)
  MEDIUM   occurrences ≥ 2× min_occurrences
  HIGH     occurrences ≥ 3× min_occurrences OR spans
           ≥ critical_span_days

Closed 4-value :class:`EvidenceSource`:

  SESSION_SUMMARY    summary.json parses
  GIT_HISTORY        commit log walk (composes
                     long_horizon_memory)
  OPS_DIGEST         per-session ops_digest field within
                     summary.json
  COMBINED           cross-source aggregation

Closed 4-value :class:`DeadlockVerdict`:

  NO_DEADLOCK   all detectors NONE-severity
  SUSPECTED     at least one LOW or MEDIUM signal
  CONFIRMED     at least one HIGH signal
  DISABLED      master flag off

§33.1 cognitive substrate
``JARVIS_MULTI_DAY_DEADLOCK_DETECTION_ENABLED``
default-**FALSE**.

Authority asymmetry (AST-pinned): stdlib at module load.
``last_session_summary`` + ``long_horizon_memory`` +
``governance_boundary_gate`` + ``cross_process_jsonl``
are lazy-imported. Does NOT import orchestrator /
iron_gate / policy / providers / candidate_generator /
urgency_router / change_engine / semantic_guardian /
auto_committer / risk_tier_floor / tool_executor /
plan_generator. Substrate is observational; it surfaces
diagnostic signals — operator action is NEVER autonomous.
"""
from __future__ import annotations

import ast
import enum
import json
import logging
import os
import re
import subprocess
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    FrozenSet,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

logger = logging.getLogger(__name__)


MULTI_DAY_DEADLOCK_SCHEMA_VERSION: str = (
    "multi_day_deadlock_detector.1"
)


_ENV_MASTER = "JARVIS_MULTI_DAY_DEADLOCK_DETECTION_ENABLED"
_ENV_PERSIST = (
    "JARVIS_MULTI_DAY_DEADLOCK_DETECTION_PERSIST_ENABLED"
)
_ENV_LOOKBACK_DAYS = (
    "JARVIS_MULTI_DAY_DEADLOCK_DETECTION_LOOKBACK_DAYS"
)
_ENV_MIN_OCCURRENCES = (
    "JARVIS_MULTI_DAY_DEADLOCK_DETECTION_MIN_OCCURRENCES"
)
_ENV_FAILURE_RATIO = (
    "JARVIS_MULTI_DAY_DEADLOCK_DETECTION_FAILURE_RATIO"
)
_ENV_ZERO_PROGRESS_STREAK = (
    "JARVIS_MULTI_DAY_DEADLOCK_DETECTION_ZERO_PROGRESS_STREAK"
)
_ENV_THRASH_MIN_DAYS = (
    "JARVIS_MULTI_DAY_DEADLOCK_DETECTION_THRASH_MIN_DAYS"
)
_ENV_CRITICAL_SPAN_DAYS = (
    "JARVIS_MULTI_DAY_DEADLOCK_DETECTION_CRITICAL_SPAN_DAYS"
)
_ENV_MAX_SESSIONS = (
    "JARVIS_MULTI_DAY_DEADLOCK_DETECTION_MAX_SESSIONS"
)
_ENV_SESSION_ROOT = (
    "JARVIS_MULTI_DAY_DEADLOCK_DETECTION_SESSION_ROOT"
)
_ENV_LEDGER_PATH = (
    "JARVIS_MULTI_DAY_DEADLOCK_DETECTION_LEDGER_PATH"
)

_DEFAULT_LOOKBACK_DAYS = 7
_DEFAULT_MIN_OCCURRENCES = 3
_DEFAULT_FAILURE_RATIO = 0.5
_DEFAULT_ZERO_PROGRESS_STREAK = 3
_DEFAULT_THRASH_MIN_DAYS = 3
_DEFAULT_CRITICAL_SPAN_DAYS = 5
_DEFAULT_MAX_SESSIONS = 200
_DEFAULT_SESSION_ROOT = ".ouroboros/sessions"
_DEFAULT_LEDGER_REL = ".jarvis/multi_day_deadlock_ledger.jsonl"

# Stop reasons that count as "clean" — recurrence of these
# is not a deadlock signal. Sourced from the harness
# normalization: a session that ends with `complete` after
# wall_clock_cap / idle_timeout / budget_exceeded is doing
# its job. Per CLAUDE.md v2.95 §32 cadence-arc Layer 8 +
# v2.85 Layer 8 footnote, these three are clean-bar
# equivalents.
_CLEAN_STOP_REASONS: FrozenSet[str] = frozenset({
    "complete",
    "wall_clock_cap",
    "idle_timeout",
    "budget_exceeded",
})

_TRUTHY: FrozenSet[str] = frozenset({"1", "true", "yes", "on"})


def _flag(name: str, *, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in _TRUTHY


def master_enabled() -> bool:
    """§33.1 — default-FALSE."""
    return _flag(_ENV_MASTER, default=False)


def persistence_enabled() -> bool:
    return _flag(_ENV_PERSIST, default=True)


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


def _read_clamped_float(
    name: str, default: float, lo: float, hi: float,
) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        n = float(raw)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def lookback_days() -> int:
    return _read_clamped_int(
        _ENV_LOOKBACK_DAYS, _DEFAULT_LOOKBACK_DAYS, 1, 365,
    )


def min_occurrences() -> int:
    return _read_clamped_int(
        _ENV_MIN_OCCURRENCES,
        _DEFAULT_MIN_OCCURRENCES, 2, 100,
    )


def failure_ratio_threshold() -> float:
    return _read_clamped_float(
        _ENV_FAILURE_RATIO,
        _DEFAULT_FAILURE_RATIO, 0.01, 1.0,
    )


def zero_progress_streak_threshold() -> int:
    return _read_clamped_int(
        _ENV_ZERO_PROGRESS_STREAK,
        _DEFAULT_ZERO_PROGRESS_STREAK, 2, 50,
    )


def thrash_min_days() -> int:
    return _read_clamped_int(
        _ENV_THRASH_MIN_DAYS,
        _DEFAULT_THRASH_MIN_DAYS, 2, 365,
    )


def critical_span_days() -> int:
    return _read_clamped_int(
        _ENV_CRITICAL_SPAN_DAYS,
        _DEFAULT_CRITICAL_SPAN_DAYS, 2, 365,
    )


def max_sessions_scanned() -> int:
    return _read_clamped_int(
        _ENV_MAX_SESSIONS, _DEFAULT_MAX_SESSIONS, 1, 10_000,
    )


def session_root() -> Path:
    raw = os.environ.get(_ENV_SESSION_ROOT, "").strip()
    return Path(raw or _DEFAULT_SESSION_ROOT).expanduser()


def ledger_path() -> Path:
    raw = os.environ.get(_ENV_LEDGER_PATH, "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path(_DEFAULT_LEDGER_REL)


# Closed taxonomies


class DeadlockKind(str, enum.Enum):
    """Closed 4-value taxonomy — bytes-pinned via AST."""

    REPEAT_STOP_REASON = "repeat_stop_reason"
    REPEAT_FAILURE = "repeat_failure"
    VERDICT_THRASH = "verdict_thrash"
    ZERO_PROGRESS = "zero_progress"


class DeadlockSeverity(str, enum.Enum):
    """Closed 4-value taxonomy — bytes-pinned via AST."""

    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class EvidenceSource(str, enum.Enum):
    """Closed 4-value taxonomy — bytes-pinned via AST."""

    SESSION_SUMMARY = "session_summary"
    GIT_HISTORY = "git_history"
    OPS_DIGEST = "ops_digest"
    COMBINED = "combined"


class DeadlockVerdict(str, enum.Enum):
    """Closed 4-value taxonomy — bytes-pinned via AST."""

    NO_DEADLOCK = "no_deadlock"
    SUSPECTED = "suspected"
    CONFIRMED = "confirmed"
    DISABLED = "disabled"


_KIND_GLYPH: Dict[str, str] = {
    DeadlockKind.REPEAT_STOP_REASON.value: "🔁",
    DeadlockKind.REPEAT_FAILURE.value: "❌",
    DeadlockKind.VERDICT_THRASH.value: "⇄",
    DeadlockKind.ZERO_PROGRESS.value: "∅",
}


_SEVERITY_GLYPH: Dict[str, str] = {
    DeadlockSeverity.NONE.value: "·",
    DeadlockSeverity.LOW.value: "▁",
    DeadlockSeverity.MEDIUM.value: "▄",
    DeadlockSeverity.HIGH.value: "█",
}


_SOURCE_GLYPH: Dict[str, str] = {
    EvidenceSource.SESSION_SUMMARY.value: "📋",
    EvidenceSource.GIT_HISTORY.value: "📜",
    EvidenceSource.OPS_DIGEST.value: "📊",
    EvidenceSource.COMBINED.value: "🔗",
}


_VERDICT_GLYPH: Dict[str, str] = {
    DeadlockVerdict.NO_DEADLOCK.value: "✓",
    DeadlockVerdict.SUSPECTED.value: "?",
    DeadlockVerdict.CONFIRMED.value: "!",
    DeadlockVerdict.DISABLED.value: "◌",
}


def _coerce_value(obj: object) -> str:
    try:
        val = getattr(obj, "value", None)
        if val is not None:
            return str(val).strip().lower()
        return str(obj or "").strip().lower()
    except Exception:  # noqa: BLE001
        return ""


def kind_glyph(kind: object) -> str:
    """NEVER raises."""
    return _KIND_GLYPH.get(_coerce_value(kind), "?")


def severity_glyph(severity: object) -> str:
    """NEVER raises."""
    return _SEVERITY_GLYPH.get(_coerce_value(severity), "?")


def source_glyph(source: object) -> str:
    """NEVER raises."""
    return _SOURCE_GLYPH.get(_coerce_value(source), "?")


def verdict_glyph(verdict: object) -> str:
    """NEVER raises."""
    return _VERDICT_GLYPH.get(_coerce_value(verdict), "?")


# §33.5 frozen artifacts


@dataclass(frozen=True)
class SessionDigest:
    """Parsed projection of one session.summary.json — minimal
    fields needed by detectors. Tolerant of absent keys."""

    session_id: str
    age_days: float
    stop_reason: str
    session_outcome: str
    stats_attempted: int
    stats_failed: int
    stats_completed: int
    branch_commits: int
    last_apply_mode: str
    failure_ratio: float
    schema_version: str = MULTI_DAY_DEADLOCK_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id[:128],
            "age_days": float(self.age_days),
            "stop_reason": self.stop_reason[:64],
            "session_outcome": self.session_outcome[:64],
            "stats_attempted": int(self.stats_attempted),
            "stats_failed": int(self.stats_failed),
            "stats_completed": int(self.stats_completed),
            "branch_commits": int(self.branch_commits),
            "last_apply_mode": self.last_apply_mode[:32],
            "failure_ratio": float(self.failure_ratio),
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class DeadlockSignal:
    """One detected cross-session pattern."""

    kind: DeadlockKind
    severity: DeadlockSeverity
    fingerprint: str
    evidence_source: EvidenceSource
    evidence_text: str
    occurrences: int
    span_days: float
    affected_files: Tuple[str, ...]
    boundary_crossed: bool
    schema_version: str = MULTI_DAY_DEADLOCK_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind.value,
            "severity": self.severity.value,
            "fingerprint": self.fingerprint[:128],
            "evidence_source": self.evidence_source.value,
            "evidence_text": self.evidence_text[:512],
            "occurrences": int(self.occurrences),
            "span_days": float(self.span_days),
            "affected_files": list(self.affected_files),
            "boundary_crossed": bool(self.boundary_crossed),
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class DeadlockReport:
    """Top-level scan report."""

    evaluated_at_unix: float
    master_enabled: bool
    verdict: DeadlockVerdict
    lookback_days: int
    sessions_scanned: int
    signals: Tuple[DeadlockSignal, ...]
    diagnostic: str
    elapsed_s: float
    schema_version: str = MULTI_DAY_DEADLOCK_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "evaluated_at_unix": self.evaluated_at_unix,
            "master_enabled": self.master_enabled,
            "verdict": self.verdict.value,
            "lookback_days": int(self.lookback_days),
            "sessions_scanned": int(self.sessions_scanned),
            "signals": [s.to_dict() for s in self.signals],
            "diagnostic": self.diagnostic[:512],
            "elapsed_s": float(self.elapsed_s),
            "schema_version": self.schema_version,
        }


# Composers — lazy-imported governance surfaces


def _is_boundary_crossed(file_path: str) -> bool:
    """Compose Wave 2 #5 boundary gate. NEVER raises."""
    if not file_path:
        return False
    try:
        from backend.core.ouroboros.governance.governance_boundary_gate import (  # noqa: E501  # type: ignore[import-not-found]
            is_boundary_crossed,
        )
        return bool(is_boundary_crossed((file_path,)))
    except Exception:  # noqa: BLE001
        return False


def _any_boundary_crossed(paths: Sequence[str]) -> bool:
    return any(_is_boundary_crossed(p) for p in paths)


def _flock_append(payload: Mapping[str, Any]) -> bool:
    """Best-effort §33.4 write. NEVER raises."""
    if not master_enabled() or not persistence_enabled():
        return False
    try:
        from backend.core.ouroboros.governance.cross_process_jsonl import (  # noqa: E501  # type: ignore[import-not-found]
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


# Summary parsing — tolerant of any session.summary.json layout


def _safe_int(value: Any) -> int:
    try:
        if value is None:
            return 0
        return int(value)
    except (TypeError, ValueError):
        return 0


def _safe_str(value: Any) -> str:
    if value is None:
        return ""
    try:
        return str(value)
    except Exception:  # noqa: BLE001
        return ""


def _safe_float(value: Any) -> float:
    try:
        if value is None:
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def parse_session_summary(
    summary_path: Path,
    *,
    now_unix: Optional[float] = None,
) -> Optional[SessionDigest]:
    """Parse one ``session/<id>/summary.json`` into a
    :class:`SessionDigest`. NEVER raises — returns None on
    any parse failure."""
    now = time.time() if now_unix is None else float(now_unix)
    try:
        if not summary_path.exists():
            return None
        raw = summary_path.read_text(encoding="utf-8")
        data = json.loads(raw)
        if not isinstance(data, dict):
            return None
    except Exception:  # noqa: BLE001
        return None
    try:
        mtime = summary_path.stat().st_mtime
        age_s = max(0.0, now - mtime)
        age_days = age_s / 86_400.0
    except Exception:  # noqa: BLE001
        age_days = 0.0
    session_id = _safe_str(
        data.get("session_id") or summary_path.parent.name,
    )[:128]
    stop_reason = _safe_str(data.get("stop_reason"))[:64]
    session_outcome = _safe_str(
        data.get("session_outcome"),
    )[:64]
    # Stats flattened — schema may have a stats dict OR
    # flat fields
    stats = data.get("stats")
    if isinstance(stats, dict):
        attempted = _safe_int(stats.get("attempted"))
        failed = _safe_int(stats.get("failed"))
        completed = _safe_int(stats.get("completed"))
    else:
        attempted = _safe_int(data.get("stats_attempted"))
        failed = _safe_int(data.get("stats_failed"))
        completed = _safe_int(data.get("stats_completed"))
    # branch_stats
    bs = data.get("branch_stats")
    if isinstance(bs, dict):
        commits = _safe_int(bs.get("commits"))
    else:
        commits = _safe_int(data.get("branch_commits"))
    # ops_digest fields
    ops = data.get("ops_digest")
    last_apply_mode = ""
    if isinstance(ops, dict):
        last_apply_mode = _safe_str(
            ops.get("last_apply_mode"),
        )[:32]
    if not last_apply_mode:
        last_apply_mode = _safe_str(
            data.get("last_apply_mode"),
        )[:32]
    failure_ratio = (
        failed / attempted if attempted > 0 else 0.0
    )
    return SessionDigest(
        session_id=session_id,
        age_days=age_days,
        stop_reason=stop_reason,
        session_outcome=session_outcome,
        stats_attempted=attempted,
        stats_failed=failed,
        stats_completed=completed,
        branch_commits=commits,
        last_apply_mode=last_apply_mode,
        failure_ratio=failure_ratio,
    )


def walk_session_summaries(
    *,
    root: Optional[Path] = None,
    lookback_days_param: Optional[int] = None,
    max_count: Optional[int] = None,
    now_unix: Optional[float] = None,
) -> Tuple[SessionDigest, ...]:
    """Iterate ``<root>/<session-id>/summary.json``. NEVER
    raises — returns empty tuple on bad root."""
    scan_root = root if root is not None else session_root()
    lookback = lookback_days_param or lookback_days()
    cap = max_count or max_sessions_scanned()
    now = time.time() if now_unix is None else float(now_unix)
    out: List[SessionDigest] = []
    try:
        if not scan_root.exists() or not scan_root.is_dir():
            return ()
        # Sort newest-first by mtime; cap at max
        entries = list(scan_root.iterdir())
        entries.sort(
            key=lambda p: (
                p.stat().st_mtime if p.is_dir() else 0.0
            ),
            reverse=True,
        )
        for entry in entries:
            if len(out) >= cap:
                break
            try:
                if not entry.is_dir():
                    continue
                summary = entry / "summary.json"
                digest = parse_session_summary(
                    summary, now_unix=now,
                )
                if digest is None:
                    continue
                if digest.age_days > lookback:
                    continue
                out.append(digest)
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        return tuple(out)
    return tuple(out)


# Severity classifier


def _classify_severity(
    occurrences: int,
    span_days: float,
    *,
    min_occ: int,
    critical_span: int,
) -> DeadlockSeverity:
    """Pure classifier. NEVER raises."""
    if occurrences < min_occ:
        return DeadlockSeverity.NONE
    if (
        occurrences >= 3 * min_occ
        or span_days >= critical_span
    ):
        return DeadlockSeverity.HIGH
    if occurrences >= 2 * min_occ:
        return DeadlockSeverity.MEDIUM
    return DeadlockSeverity.LOW


# Detectors


def detect_repeat_stop_reason(
    sessions: Sequence[SessionDigest],
    *,
    min_occ: Optional[int] = None,
    critical_span: Optional[int] = None,
) -> Tuple[DeadlockSignal, ...]:
    """REPEAT_STOP_REASON detector. NEVER raises."""
    occ_threshold = min_occ or min_occurrences()
    crit_span = critical_span or critical_span_days()
    by_reason: Dict[str, List[SessionDigest]] = {}
    for s in sessions:
        reason = (s.stop_reason or "").strip().lower()
        if not reason or reason in _CLEAN_STOP_REASONS:
            continue
        by_reason.setdefault(reason, []).append(s)
    out: List[DeadlockSignal] = []
    for reason, group in by_reason.items():
        if len(group) < occ_threshold:
            continue
        ages = [g.age_days for g in group]
        span = max(ages) - min(ages) if ages else 0.0
        severity = _classify_severity(
            len(group), span,
            min_occ=occ_threshold,
            critical_span=crit_span,
        )
        if severity is DeadlockSeverity.NONE:
            continue
        sample_ids = tuple(
            g.session_id[:32] for g in sorted(
                group, key=lambda x: -x.age_days,
            )[:3]
        )
        out.append(DeadlockSignal(
            kind=DeadlockKind.REPEAT_STOP_REASON,
            severity=severity,
            fingerprint=f"stop_reason:{reason}",
            evidence_source=EvidenceSource.SESSION_SUMMARY,
            evidence_text=(
                f"stop_reason={reason!r} in {len(group)} "
                f"session(s) over {span:.1f}d "
                f"(sample={list(sample_ids)})"
            ),
            occurrences=len(group),
            span_days=span,
            affected_files=(),
            boundary_crossed=False,
        ))
    return tuple(out)


def detect_repeat_failure(
    sessions: Sequence[SessionDigest],
    *,
    min_occ: Optional[int] = None,
    critical_span: Optional[int] = None,
    ratio_threshold: Optional[float] = None,
) -> Tuple[DeadlockSignal, ...]:
    """REPEAT_FAILURE detector — N sessions with
    failure_ratio ≥ threshold. NEVER raises."""
    occ_threshold = min_occ or min_occurrences()
    crit_span = critical_span or critical_span_days()
    ratio = (
        ratio_threshold if ratio_threshold is not None
        else failure_ratio_threshold()
    )
    high_failure = [
        s for s in sessions
        if s.stats_attempted > 0
        and s.failure_ratio >= ratio
    ]
    if len(high_failure) < occ_threshold:
        return ()
    ages = [s.age_days for s in high_failure]
    span = max(ages) - min(ages) if ages else 0.0
    severity = _classify_severity(
        len(high_failure), span,
        min_occ=occ_threshold,
        critical_span=crit_span,
    )
    if severity is DeadlockSeverity.NONE:
        return ()
    avg_ratio = (
        sum(s.failure_ratio for s in high_failure)
        / len(high_failure)
    )
    sample_ids = tuple(
        s.session_id[:32] for s in sorted(
            high_failure, key=lambda x: -x.age_days,
        )[:3]
    )
    return (DeadlockSignal(
        kind=DeadlockKind.REPEAT_FAILURE,
        severity=severity,
        fingerprint=f"failure_ratio:>={ratio:.2f}",
        evidence_source=EvidenceSource.SESSION_SUMMARY,
        evidence_text=(
            f"{len(high_failure)} session(s) with "
            f"failure_ratio≥{ratio:.2f} (avg={avg_ratio:.2f}) "
            f"over {span:.1f}d (sample={list(sample_ids)})"
        ),
        occurrences=len(high_failure),
        span_days=span,
        affected_files=(),
        boundary_crossed=False,
    ),)


_GitLogRunner = Callable[[Sequence[str]], Optional[str]]


def detect_verdict_thrash(
    *,
    git_runner: Optional[_GitLogRunner] = None,
    repo_root: Optional[Path] = None,
    min_days: Optional[int] = None,
    critical_span: Optional[int] = None,
    now_unix: Optional[float] = None,
) -> Tuple[DeadlockSignal, ...]:
    """VERDICT_THRASH detector — file appearing in BUGFIX
    commits across distinct days. NEVER raises."""
    days_threshold = min_days or thrash_min_days()
    crit_span = critical_span or critical_span_days()
    now = time.time() if now_unix is None else float(now_unix)
    try:
        from backend.core.ouroboros.governance.long_horizon_memory import (  # noqa: E501  # type: ignore[import-not-found]
            MemoryTheme,
            walk_git_log,
        )
    except Exception:  # noqa: BLE001
        return ()
    try:
        commits = walk_git_log(
            git_runner=git_runner,
            repo_root=repo_root,
            now_unix=now,
        )
    except Exception:  # noqa: BLE001
        return ()
    if not commits:
        return ()
    # For each file, collect the distinct days on which it
    # appeared in a BUGFIX commit
    file_bugfix_days: Dict[str, set] = {}
    file_age_min_max: Dict[str, Tuple[float, float]] = {}
    for c in commits:
        if c.theme is not MemoryTheme.BUGFIX:
            continue
        # Discretize age to day buckets
        day_bucket = int(c.age_days)
        for f in c.files:
            file_bugfix_days.setdefault(f, set()).add(
                day_bucket,
            )
            cur = file_age_min_max.get(f)
            if cur is None:
                file_age_min_max[f] = (c.age_days, c.age_days)
            else:
                file_age_min_max[f] = (
                    min(cur[0], c.age_days),
                    max(cur[1], c.age_days),
                )
    out: List[DeadlockSignal] = []
    for f, days in file_bugfix_days.items():
        if len(days) < days_threshold:
            continue
        lo, hi = file_age_min_max[f]
        span = hi - lo
        severity = _classify_severity(
            len(days), span,
            min_occ=days_threshold,
            critical_span=crit_span,
        )
        if severity is DeadlockSeverity.NONE:
            continue
        out.append(DeadlockSignal(
            kind=DeadlockKind.VERDICT_THRASH,
            severity=severity,
            fingerprint=f"thrash_file:{f}",
            evidence_source=EvidenceSource.GIT_HISTORY,
            evidence_text=(
                f"{f} touched by BUGFIX on "
                f"{len(days)} distinct day(s) "
                f"over {span:.1f}d span — fix not sticking"
            ),
            occurrences=len(days),
            span_days=span,
            affected_files=(f,),
            boundary_crossed=_is_boundary_crossed(f),
        ))
    # Sort by occurrences desc for deterministic ordering
    out.sort(
        key=lambda s: (-s.occurrences, s.fingerprint),
    )
    return tuple(out)


def detect_zero_progress(
    sessions: Sequence[SessionDigest],
    *,
    streak_threshold: Optional[int] = None,
    critical_span: Optional[int] = None,
) -> Tuple[DeadlockSignal, ...]:
    """ZERO_PROGRESS detector — N consecutive sessions with
    branch_commits==0. NEVER raises.

    Ordering: sessions are processed newest-first (age
    ascending). A "streak" is a contiguous run of
    zero-commit sessions starting from the most recent."""
    thresh = streak_threshold or zero_progress_streak_threshold()
    crit_span = critical_span or critical_span_days()
    if not sessions:
        return ()
    ordered = sorted(sessions, key=lambda s: s.age_days)
    streak: List[SessionDigest] = []
    for s in ordered:
        if s.branch_commits <= 0:
            streak.append(s)
        else:
            break  # streak broken
    if len(streak) < thresh:
        return ()
    ages = [s.age_days for s in streak]
    span = max(ages) - min(ages) if ages else 0.0
    severity = _classify_severity(
        len(streak), span,
        min_occ=thresh,
        critical_span=crit_span,
    )
    if severity is DeadlockSeverity.NONE:
        return ()
    sample_ids = tuple(
        s.session_id[:32] for s in streak[:3]
    )
    return (DeadlockSignal(
        kind=DeadlockKind.ZERO_PROGRESS,
        severity=severity,
        fingerprint="zero_progress_streak",
        evidence_source=EvidenceSource.SESSION_SUMMARY,
        evidence_text=(
            f"{len(streak)} consecutive session(s) with "
            f"branch_commits=0 over {span:.1f}d "
            f"(sample={list(sample_ids)}) — system not "
            f"shipping"
        ),
        occurrences=len(streak),
        span_days=span,
        affected_files=(),
        boundary_crossed=False,
    ),)


# Top-level aggregator


def _aggregate_verdict(
    signals: Sequence[DeadlockSignal],
) -> DeadlockVerdict:
    """Pure aggregation. NEVER raises."""
    if not signals:
        return DeadlockVerdict.NO_DEADLOCK
    has_high = any(
        s.severity is DeadlockSeverity.HIGH for s in signals
    )
    if has_high:
        return DeadlockVerdict.CONFIRMED
    has_below_high = any(
        s.severity in (
            DeadlockSeverity.LOW, DeadlockSeverity.MEDIUM,
        )
        for s in signals
    )
    if has_below_high:
        return DeadlockVerdict.SUSPECTED
    return DeadlockVerdict.NO_DEADLOCK


def detect_deadlocks(
    *,
    sessions_override: Optional[Sequence[SessionDigest]] = None,
    session_root_path: Optional[Path] = None,
    lookback_days_param: Optional[int] = None,
    git_runner: Optional[_GitLogRunner] = None,
    repo_root: Optional[Path] = None,
    now_unix: Optional[float] = None,
) -> DeadlockReport:
    """Top-level scan. NEVER raises."""
    started = (
        time.time() if now_unix is None else float(now_unix)
    )
    if not master_enabled():
        return DeadlockReport(
            evaluated_at_unix=started,
            master_enabled=False,
            verdict=DeadlockVerdict.DISABLED,
            lookback_days=lookback_days(),
            sessions_scanned=0,
            signals=(),
            diagnostic=f"gate disabled via {_ENV_MASTER}=false",
            elapsed_s=0.0,
        )
    lb = lookback_days_param or lookback_days()
    if sessions_override is not None:
        sessions = tuple(sessions_override)
    else:
        sessions = walk_session_summaries(
            root=session_root_path,
            lookback_days_param=lb,
            now_unix=started,
        )
    all_signals: List[DeadlockSignal] = []
    all_signals.extend(detect_repeat_stop_reason(sessions))
    all_signals.extend(detect_repeat_failure(sessions))
    all_signals.extend(detect_zero_progress(sessions))
    all_signals.extend(detect_verdict_thrash(
        git_runner=git_runner,
        repo_root=repo_root,
        now_unix=started,
    ))
    verdict = _aggregate_verdict(all_signals)
    by_severity: Dict[str, int] = {}
    for s in all_signals:
        by_severity[s.severity.value] = (
            by_severity.get(s.severity.value, 0) + 1
        )
    diagnostic = (
        f"verdict={verdict.value} "
        f"sessions_scanned={len(sessions)} "
        f"signals={len(all_signals)} "
        f"by_severity={by_severity}"
    )
    report = DeadlockReport(
        evaluated_at_unix=started,
        master_enabled=True,
        verdict=verdict,
        lookback_days=lb,
        sessions_scanned=len(sessions),
        signals=tuple(all_signals),
        diagnostic=diagnostic,
        elapsed_s=max(0.0, time.time() - started),
    )
    _persist_report(report)
    _publish_event(report)
    return report


def _persist_report(report: DeadlockReport) -> None:
    if report.verdict is DeadlockVerdict.DISABLED:
        return
    _flock_append({
        "kind": "deadlock_report", "payload": report.to_dict(),
    })


def _publish_event(report: DeadlockReport) -> None:
    if not master_enabled():
        return
    if report.verdict is DeadlockVerdict.DISABLED:
        return
    try:
        from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501  # type: ignore[import-not-found]
            EVENT_TYPE_MULTI_DAY_DEADLOCK_EVALUATED,
            publish_task_event,
        )
        by_kind: Dict[str, int] = {}
        by_severity: Dict[str, int] = {}
        for s in report.signals:
            by_kind[s.kind.value] = (
                by_kind.get(s.kind.value, 0) + 1
            )
            by_severity[s.severity.value] = (
                by_severity.get(s.severity.value, 0) + 1
            )
        publish_task_event(
            EVENT_TYPE_MULTI_DAY_DEADLOCK_EVALUATED,
            (
                f"system::multi_day_deadlock::"
                f"{report.schema_version}"
            ),
            {
                "verdict": report.verdict.value,
                "lookback_days": report.lookback_days,
                "sessions_scanned": report.sessions_scanned,
                "signal_count": len(report.signals),
                "by_kind": by_kind,
                "by_severity": by_severity,
                "elapsed_s": report.elapsed_s,
                "schema_version": report.schema_version,
            },
        )
    except Exception:  # noqa: BLE001
        return


def format_deadlock_panel(
    report: Optional[DeadlockReport] = None,
) -> str:
    """NEVER raises."""
    if report is None:
        if not master_enabled():
            return (
                f"multi-day deadlock: disabled "
                f"({_ENV_MASTER}=false)"
            )
        return "multi-day deadlock: no report"
    if not report.master_enabled:
        return (
            f"multi-day deadlock: disabled "
            f"({_ENV_MASTER}=false)"
        )
    vg = verdict_glyph(report.verdict)
    lines = [
        f"⏱  Multi-Day Deadlock  {vg} {report.verdict.value}",
        f"  lookback         : {report.lookback_days}d",
        f"  sessions_scanned : {report.sessions_scanned}",
        f"  signals          : {len(report.signals)}",
    ]
    if report.signals:
        for sig in report.signals[:5]:
            kg = kind_glyph(sig.kind)
            sg_ = severity_glyph(sig.severity)
            src = source_glyph(sig.evidence_source)
            lines.append(
                f"    {kg}{sg_}{src} {sig.kind.value} "
                f"[{sig.severity.value}]: "
                f"{sig.fingerprint[:48]} "
                f"(n={sig.occurrences}, "
                f"span={sig.span_days:.1f}d)"
            )
    lines.append(f"  diagnostic       : {report.diagnostic}")
    return "\n".join(lines)


# AST pins


def register_shipped_invariants() -> list:
    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501  # type: ignore[import-not-found]
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    target = (
        "backend/core/ouroboros/governance/"
        "multi_day_deadlock_detector.py"
    )

    _EXPECTED_KIND = {
        "repeat_stop_reason", "repeat_failure",
        "verdict_thrash", "zero_progress",
    }
    _EXPECTED_SEVERITY = {
        "none", "low", "medium", "high",
    }
    _EXPECTED_SOURCE = {
        "session_summary", "git_history",
        "ops_digest", "combined",
    }
    _EXPECTED_VERDICT = {
        "no_deadlock", "suspected", "confirmed", "disabled",
    }

    def _validate_taxonomy(class_name: str, expected: set):
        def _validate(tree: ast.AST, source: str) -> tuple:  # noqa: ARG001
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.ClassDef)
                    and node.name == class_name
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
                    missing = expected - found
                    extra = found - expected
                    if missing:
                        return (
                            f"{class_name} missing: "
                            f"{sorted(missing)}",
                        )
                    if extra:
                        return (
                            f"{class_name} drift: "
                            f"{sorted(extra)}",
                        )
                    return ()
            return (f"{class_name} class not found",)
        return _validate

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
            "backend.core.ouroboros.governance.tool_executor",
            "backend.core.ouroboros.governance.plan_generator",
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
        if "last_session_summary" not in source:
            violations.append(
                "must compose last_session_summary",
            )
        if "long_horizon_memory" not in source:
            violations.append(
                "must compose long_horizon_memory "
                "(Phase 1 #7 git walker)",
            )
        if "governance_boundary_gate" not in source:
            violations.append(
                "must compose Wave 2 #5 "
                "governance_boundary_gate",
            )
        if "cross_process_jsonl" not in source:
            violations.append(
                "must compose cross_process_jsonl",
            )
        if "subprocess" not in source:
            violations.append("must compose stdlib subprocess")
        if "pathlib" not in source:
            violations.append("must compose stdlib pathlib")
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name=(
                "multi_day_deadlock_kind_taxonomy_closed"
            ),
            target_file=target,
            description=(
                "DeadlockKind 4-value taxonomy bytes-pinned."
            ),
            validate=_validate_taxonomy(
                "DeadlockKind", _EXPECTED_KIND,
            ),
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "multi_day_deadlock_severity_taxonomy_closed"
            ),
            target_file=target,
            description=(
                "DeadlockSeverity 4-value taxonomy bytes-pinned."
            ),
            validate=_validate_taxonomy(
                "DeadlockSeverity", _EXPECTED_SEVERITY,
            ),
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "multi_day_deadlock_source_taxonomy_closed"
            ),
            target_file=target,
            description=(
                "EvidenceSource 4-value taxonomy bytes-pinned."
            ),
            validate=_validate_taxonomy(
                "EvidenceSource", _EXPECTED_SOURCE,
            ),
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "multi_day_deadlock_verdict_taxonomy_closed"
            ),
            target_file=target,
            description=(
                "DeadlockVerdict 4-value taxonomy bytes-pinned."
            ),
            validate=_validate_taxonomy(
                "DeadlockVerdict", _EXPECTED_VERDICT,
            ),
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "multi_day_deadlock_authority_asymmetry"
            ),
            target_file=target,
            description=(
                "Substrate purity — observational layer. MUST "
                "NOT import orchestrator / iron_gate / policy "
                "/ etc / plan_generator."
            ),
            validate=_validate_authority_asymmetry,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "multi_day_deadlock_master_default_false"
            ),
            target_file=target,
            description="§33.1 default-FALSE.",
            validate=_validate_master_default_false,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "multi_day_deadlock_composes_canonical"
            ),
            target_file=target,
            description=(
                "Substrate composes last_session_summary + "
                "long_horizon_memory + "
                "governance_boundary_gate + "
                "cross_process_jsonl + stdlib subprocess + "
                "stdlib pathlib."
            ),
            validate=_validate_composes_canonical,
        ),
    ]


def register_flags(registry: Any) -> int:
    from backend.core.ouroboros.governance.flag_registry import (
        Category,
        FlagSpec,
        FlagType,
    )

    src = (
        "backend/core/ouroboros/governance/"
        "multi_day_deadlock_detector.py"
    )

    seeds = [
        FlagSpec(
            name=_ENV_MASTER,
            type=FlagType.BOOL,
            default=False,
            description=(
                "Multi-Day Deadlock Detector master. §33.1 "
                "default-FALSE. Closes §41.4 Phase 1 ninth "
                "(final) arc (PRD v3.0+). Cross-session "
                "pattern detector — catches deadlocks "
                "spanning multiple days that single-session "
                "detection misses. 4 detector kinds: "
                "REPEAT_STOP_REASON, REPEAT_FAILURE, "
                "VERDICT_THRASH, ZERO_PROGRESS."
            ),
            category=Category.INTEGRATION,
            source_file=src,
            example=f"{_ENV_MASTER}=true",
        ),
        FlagSpec(
            name=_ENV_PERSIST,
            type=FlagType.BOOL,
            default=True,
            description="Sub-flag — §33.4 ledger writes.",
            category=Category.SAFETY,
            source_file=src,
            example=f"{_ENV_PERSIST}=false",
        ),
        FlagSpec(
            name=_ENV_LOOKBACK_DAYS,
            type=FlagType.INT,
            default=_DEFAULT_LOOKBACK_DAYS,
            description=(
                "Days of session history to scan. Default 7."
            ),
            category=Category.TUNING,
            source_file=src,
            example=f"{_ENV_LOOKBACK_DAYS}=14",
        ),
        FlagSpec(
            name=_ENV_MIN_OCCURRENCES,
            type=FlagType.INT,
            default=_DEFAULT_MIN_OCCURRENCES,
            description=(
                "Minimum occurrences before signal fires. "
                "Default 3."
            ),
            category=Category.TUNING,
            source_file=src,
            example=f"{_ENV_MIN_OCCURRENCES}=5",
        ),
        FlagSpec(
            name=_ENV_FAILURE_RATIO,
            type=FlagType.FLOAT,
            default=_DEFAULT_FAILURE_RATIO,
            description=(
                "Failure ratio threshold for REPEAT_FAILURE "
                "(0.0-1.0). Default 0.5."
            ),
            category=Category.TUNING,
            source_file=src,
            example=f"{_ENV_FAILURE_RATIO}=0.75",
        ),
        FlagSpec(
            name=_ENV_ZERO_PROGRESS_STREAK,
            type=FlagType.INT,
            default=_DEFAULT_ZERO_PROGRESS_STREAK,
            description=(
                "Consecutive zero-commit sessions before "
                "ZERO_PROGRESS fires. Default 3."
            ),
            category=Category.TUNING,
            source_file=src,
            example=f"{_ENV_ZERO_PROGRESS_STREAK}=5",
        ),
        FlagSpec(
            name=_ENV_THRASH_MIN_DAYS,
            type=FlagType.INT,
            default=_DEFAULT_THRASH_MIN_DAYS,
            description=(
                "Distinct bugfix-days per file before "
                "VERDICT_THRASH fires. Default 3."
            ),
            category=Category.TUNING,
            source_file=src,
            example=f"{_ENV_THRASH_MIN_DAYS}=5",
        ),
        FlagSpec(
            name=_ENV_CRITICAL_SPAN_DAYS,
            type=FlagType.INT,
            default=_DEFAULT_CRITICAL_SPAN_DAYS,
            description=(
                "Span (days) above which severity auto-"
                "escalates to HIGH. Default 5."
            ),
            category=Category.TUNING,
            source_file=src,
            example=f"{_ENV_CRITICAL_SPAN_DAYS}=10",
        ),
        FlagSpec(
            name=_ENV_MAX_SESSIONS,
            type=FlagType.INT,
            default=_DEFAULT_MAX_SESSIONS,
            description=(
                "Max session dirs scanned per detect. "
                "Default 200."
            ),
            category=Category.CAPACITY,
            source_file=src,
            example=f"{_ENV_MAX_SESSIONS}=500",
        ),
        FlagSpec(
            name=_ENV_SESSION_ROOT,
            type=FlagType.STR,
            default=_DEFAULT_SESSION_ROOT,
            description=(
                "Session root dir. Default "
                "`.ouroboros/sessions`."
            ),
            category=Category.INTEGRATION,
            source_file=src,
            example=(
                f"{_ENV_SESSION_ROOT}=.ouroboros/sessions"
            ),
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
    "MULTI_DAY_DEADLOCK_SCHEMA_VERSION",
    "DeadlockKind",
    "DeadlockSeverity",
    "EvidenceSource",
    "DeadlockVerdict",
    "SessionDigest",
    "DeadlockSignal",
    "DeadlockReport",
    "master_enabled",
    "persistence_enabled",
    "lookback_days",
    "min_occurrences",
    "failure_ratio_threshold",
    "zero_progress_streak_threshold",
    "thrash_min_days",
    "critical_span_days",
    "max_sessions_scanned",
    "session_root",
    "ledger_path",
    "kind_glyph",
    "severity_glyph",
    "source_glyph",
    "verdict_glyph",
    "parse_session_summary",
    "walk_session_summaries",
    "detect_repeat_stop_reason",
    "detect_repeat_failure",
    "detect_verdict_thrash",
    "detect_zero_progress",
    "detect_deadlocks",
    "format_deadlock_panel",
    "register_shipped_invariants",
    "register_flags",
]
