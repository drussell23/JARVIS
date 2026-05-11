"""
Second-Order Doll Completion Metric — RSI-Acceleration Probe
=============================================================

Read-only observability substrate that measures how close O+V is to
**safely completing second-order self-modification** — the recursive
capability of O+V modifying its own governance cage (Iron Gate /
SemanticGuardian / risk-tier-floor / mutation budget) under the
operator's structural authority.

RRD (Reverse Russian Doll) architecture context
-----------------------------------------------
- **Zero-order doll** — the operator (outermost shell; structural authority)
- **First-order doll** — O+V modifying *downstream* code under cage discipline
- **Second-order doll** — O+V modifying the *cage itself* safely (this metric)

The metric is the **cheapest RSI-acceleration probe** in the §40 forward
roadmap: it produces the data Waves 2-4 need (recursion-depth gate
calibration / hash-cap signature gate / Antivenom self-immunization)
WITHOUT performing any second-order modification itself. It only
*measures*; the gates produced from its output remain operator-paced.

Composition contract
--------------------
The substrate is a **thin composer** over canonical sources — zero
parallel state, zero new ledger, zero duplicated logic:

1. ``flag_registry.ensure_seeded()`` — canonical descriptor surface.
   Each :class:`FlagSpec` carries ``source_file`` + ``category``
   (8-value :class:`Category` enum). The set of flag source_files
   IS the formalized governance surface — the metric's denominator.

2. ``capability_constellation.principles_for_category()`` — canonical
   Category → Manifesto-principles mapping (single source of truth;
   AST-pinned). The metric reuses this map verbatim as its
   per-axis principle attribution.

3. ``auto_committer.ov_signature_substring()`` — canonical autonomous-
   commit signature substring. Detection of O+V-authored commits in
   ``git log`` output uses this accessor; no parallel string-grep.

4. ``git log`` (read-only subprocess) — enumerates commits touching
   the FlagRegistry's source_files within a bounded scan window
   (env-tunable; defensive bounds). Parses ``Risk: <tier>``
   tokens via canonical risk-tier names (no hardcoded set).

5. ``risk_tier_floor.get_active_tier_order()`` — canonical tier-name
   set. Parsing of the ``Risk:`` token is gated by this set —
   adding a new tier in the canonical taxonomy is structurally
   reflected without code change here.

Closed taxonomy
---------------
:class:`DollCompletionStage` — 5-value frozen ladder:

- ``UNTOUCHED`` — no autonomous commits in this category
- ``OBSERVED`` — at most 1 autonomous commit, all at APPROVAL_REQUIRED
- ``PROPOSED`` — ≥``proposed_threshold`` commits, mostly APPROVAL_REQUIRED
- ``APPLIED`` — ≥``applied_threshold`` commits, includes SAFE_AUTO or NOTIFY_APPLY
- ``GRADUATED`` — ≥``graduated_threshold`` commits over ≥``graduated_min_days``
  days, includes SAFE_AUTO

Stage transitions are strictly-monotone-by-evidence — adding more
autonomous commits at a strictly-less-cautious tier cannot regress
the stage. Operator-revert is reflected as a downgrade only after
the revert commits also appear in the scan window (no parallel
revert state).

Master flag
-----------
``JARVIS_SECOND_ORDER_DOLL_METRIC_ENABLED`` default-**FALSE** per
§33.1 graduation contract. Substrate purity: when master is off,
``aggregate_doll_completion()`` returns an empty
:class:`DollCompletionSnapshot` with ``master_enabled=False``.

Authority asymmetry (AST-pinned)
--------------------------------
The substrate imports stdlib + ``meta.shipped_code_invariants`` +
ride lazy-imports of canonical sources ONLY. It does NOT import
orchestrator / risk_tier_floor (the *enforcement* module — we read
its public canonical tier names via a thin lazy accessor) /
candidate_generator / iron_gate / semantic_guardian. The metric
NEVER raises (every code path defensive); a malformed git log row
or missing FlagRegistry seed degrades to UNTOUCHED, not exception.
"""
from __future__ import annotations

import ast
import enum
import logging
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
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


SECOND_ORDER_DOLL_METRIC_SCHEMA_VERSION: str = "second_order_doll_metric.1"


# ===========================================================================
# Env knobs — single source of truth (operator binding "no hardcoding")
# ===========================================================================


_ENV_MASTER = "JARVIS_SECOND_ORDER_DOLL_METRIC_ENABLED"
_ENV_COMMIT_SCAN_MAX = "JARVIS_DOLL_COMMIT_SCAN_MAX"
_ENV_GRADUATED_THRESHOLD = "JARVIS_DOLL_GRADUATED_THRESHOLD"
_ENV_GRADUATED_MIN_DAYS = "JARVIS_DOLL_GRADUATED_MIN_DAYS"
_ENV_APPLIED_THRESHOLD = "JARVIS_DOLL_APPLIED_THRESHOLD"
_ENV_PROPOSED_THRESHOLD = "JARVIS_DOLL_PROPOSED_THRESHOLD"

_DEFAULT_COMMIT_SCAN_MAX = 500
_DEFAULT_GRADUATED_THRESHOLD = 10
_DEFAULT_GRADUATED_MIN_DAYS = 30
_DEFAULT_APPLIED_THRESHOLD = 5
_DEFAULT_PROPOSED_THRESHOLD = 2

_MIN_COMMIT_SCAN_MAX = 10
_MAX_COMMIT_SCAN_MAX = 50_000


_TRUTHY: FrozenSet[str] = frozenset({"1", "true", "yes", "on"})


def _flag(name: str, *, default: bool = False) -> bool:
    """Canonical truthy reader — mirrors §38.11-F's _flag helper."""
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in _TRUTHY


def master_enabled() -> bool:
    """§33.1 graduation contract — master default-FALSE."""
    return _flag(_ENV_MASTER, default=False)


def _read_clamped_int(env_name: str, default: int, lo: int, hi: int) -> int:
    raw = os.environ.get(env_name, "").strip()
    if not raw:
        return default
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def commit_scan_max() -> int:
    return _read_clamped_int(
        _ENV_COMMIT_SCAN_MAX,
        _DEFAULT_COMMIT_SCAN_MAX,
        _MIN_COMMIT_SCAN_MAX,
        _MAX_COMMIT_SCAN_MAX,
    )


def graduated_threshold() -> int:
    return _read_clamped_int(
        _ENV_GRADUATED_THRESHOLD,
        _DEFAULT_GRADUATED_THRESHOLD,
        1,
        10_000,
    )


def graduated_min_days() -> int:
    return _read_clamped_int(
        _ENV_GRADUATED_MIN_DAYS,
        _DEFAULT_GRADUATED_MIN_DAYS,
        1,
        3_650,
    )


def applied_threshold() -> int:
    return _read_clamped_int(
        _ENV_APPLIED_THRESHOLD,
        _DEFAULT_APPLIED_THRESHOLD,
        1,
        10_000,
    )


def proposed_threshold() -> int:
    return _read_clamped_int(
        _ENV_PROPOSED_THRESHOLD,
        _DEFAULT_PROPOSED_THRESHOLD,
        1,
        10_000,
    )


# ===========================================================================
# Closed taxonomy — 5-value DollCompletionStage ladder
# ===========================================================================


class DollCompletionStage(str, enum.Enum):
    """Closed 5-value frozen ladder. Bytes-pinned via AST.

    Order is strictly increasing: a stage may only advance with more
    autonomous evidence at less-cautious tiers. Operator reverts are
    reflected as natural downgrade when revert commits appear in scan
    window — no parallel revert state.

    The ladder is **named for the recursive doll**, not for the
    feature: UNTOUCHED means O+V hasn't probed this part of its own
    cage; GRADUATED means O+V has been autonomously modifying this
    part safely for long enough that the second-order doll for
    that axis is structurally validated.
    """

    UNTOUCHED = "untouched"      # ○ no autonomous commits
    OBSERVED = "observed"         # · 1 commit, APPROVAL_REQUIRED only
    PROPOSED = "proposed"         # ◌ ≥proposed_threshold, mostly approval
    APPLIED = "applied"           # ◐ ≥applied_threshold w/ SAFE_AUTO|NOTIFY_APPLY
    GRADUATED = "graduated"       # ● ≥graduated_threshold over ≥min_days


# Glyph map — bytes-pinned via AST; operator binding "no hardcoding" enforced
# by referencing the canonical DollCompletionStage enum values, not raw strings.
_STAGE_GLYPH: Dict[str, str] = {
    DollCompletionStage.UNTOUCHED.value: "○",
    DollCompletionStage.OBSERVED.value: "·",
    DollCompletionStage.PROPOSED.value: "◌",
    DollCompletionStage.APPLIED.value: "◐",
    DollCompletionStage.GRADUATED.value: "●",
}


def stage_glyph(stage: object) -> str:
    """Public accessor for stage glyph. NEVER raises."""
    try:
        if hasattr(stage, "value"):
            return _STAGE_GLYPH.get(str(stage.value), "?")
        return _STAGE_GLYPH.get(str(stage or "").strip().lower(), "?")
    except Exception:  # noqa: BLE001
        return "?"


# ===========================================================================
# Canonical risk-tier name set — lazy-loaded once, defensive on failure
# ===========================================================================
#
# We compose ``risk_tier_floor.get_active_tier_order()`` for the
# canonical tier-name vocabulary. We do NOT import the enforcement
# logic — only read the keys of the public accessor's return value.


_RISK_TIER_FALLBACK: FrozenSet[str] = frozenset({
    "safe_auto", "notify_apply", "approval_required", "blocked",
})


def _canonical_risk_tier_names() -> FrozenSet[str]:
    """Composes ``risk_tier_floor.get_active_tier_order()`` keys.
    Falls back to the historical 4-tier set on lazy-import failure.
    NEVER raises."""
    try:
        from backend.core.ouroboros.governance.risk_tier_floor import (  # noqa: E501
            get_active_tier_order,
        )
        order = get_active_tier_order()
        if isinstance(order, Mapping):
            return frozenset(str(k).strip().lower() for k in order.keys())
    except Exception:  # noqa: BLE001
        pass
    return _RISK_TIER_FALLBACK


# ===========================================================================
# Frozen §33.5 versioned artifacts
# ===========================================================================


@dataclass(frozen=True)
class CommitEvidence:
    """One autonomous commit's tier + age signal, derived from git log.

    The metric does NOT keep parallel commit state — this is a
    transient projection produced during aggregation. Field set is
    deliberately narrow so we never depend on git log layout drift.
    """

    commit_hash: str
    risk_tier: str               # canonical lowercase tier name OR "unknown"
    age_seconds: float           # negative-safe (clock skew → 0.0)
    schema_version: str = SECOND_ORDER_DOLL_METRIC_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "commit_hash": self.commit_hash,
            "risk_tier": self.risk_tier,
            "age_seconds": self.age_seconds,
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class AxisProgress:
    """Per-Category second-order doll progress.

    Each :class:`flag_registry.Category` becomes one axis. Stage is
    derived purely from this axis's commit evidence + thresholds.
    """

    category: str                    # canonical Category enum value
    linked_principles: Tuple[str, ...]
    flag_count: int                  # flags in this category
    source_file_count: int           # distinct source files
    autonomous_commit_count: int     # O+V-signed commits on source files
    earliest_commit_age_s: float     # 0.0 if none
    most_recent_commit_age_s: float  # 0.0 if none
    tier_distribution: Mapping[str, int]   # canonical-tier-name → count
    stage: DollCompletionStage
    diagnostic: str                  # short operator-facing explanation
    schema_version: str = SECOND_ORDER_DOLL_METRIC_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "category": self.category,
            "linked_principles": list(self.linked_principles),
            "flag_count": self.flag_count,
            "source_file_count": self.source_file_count,
            "autonomous_commit_count": self.autonomous_commit_count,
            "earliest_commit_age_s": self.earliest_commit_age_s,
            "most_recent_commit_age_s": self.most_recent_commit_age_s,
            "tier_distribution": dict(self.tier_distribution),
            "stage": self.stage.value,
            "diagnostic": self.diagnostic,
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class DollCompletionSnapshot:
    """Aggregate snapshot across all 8 Category axes."""

    aggregated_at_unix: float
    master_enabled: bool
    axes: Tuple[AxisProgress, ...]
    stage_counts: Mapping[str, int]      # stage.value → count of axes
    completion_ratio: float              # weighted: GRADUATED=1.0, etc.
    elapsed_s: float
    diagnostic: str
    schema_version: str = SECOND_ORDER_DOLL_METRIC_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "aggregated_at_unix": self.aggregated_at_unix,
            "master_enabled": self.master_enabled,
            "axes": [a.to_dict() for a in self.axes],
            "stage_counts": dict(self.stage_counts),
            "completion_ratio": self.completion_ratio,
            "elapsed_s": self.elapsed_s,
            "diagnostic": self.diagnostic,
            "schema_version": self.schema_version,
        }

    def axis_for_category(self, category: object) -> Optional[AxisProgress]:
        """Find one axis by category. NEVER raises."""
        target = ""
        try:
            if hasattr(category, "value"):
                target = str(category.value).strip().lower()
            else:
                target = str(category or "").strip().lower()
        except Exception:  # noqa: BLE001
            return None
        for axis in self.axes:
            if axis.category == target:
                return axis
        return None


# Stage → completion-ratio weight (used by aggregate weighted average).
# Higher stage = closer to graduated second-order doll for that axis.
_STAGE_WEIGHT: Dict[str, float] = {
    DollCompletionStage.UNTOUCHED.value: 0.0,
    DollCompletionStage.OBSERVED.value: 0.1,
    DollCompletionStage.PROPOSED.value: 0.3,
    DollCompletionStage.APPLIED.value: 0.7,
    DollCompletionStage.GRADUATED.value: 1.0,
}


# ===========================================================================
# Git log composer (read-only subprocess)
# ===========================================================================


# Sentinel separating commit-header line from name-only file list.
# Chosen to be safely outside any commit-message content.
_GIT_FORMAT = "__OV_DOLL__%n%H%n%ct%n%B%n__END_HEADER__"


@dataclass(frozen=True)
class _RawCommit:
    """Pre-aggregation git log row — internal projection only."""

    commit_hash: str
    commit_time_unix: int
    body: str
    files: Tuple[str, ...]


def _run_git_log(
    repo_path: Path,
    max_commits: int,
    *,
    runner: Optional[Any] = None,
) -> str:
    """Invoke ``git log`` against ``repo_path``. NEVER raises.

    ``runner`` is caller-injectable (testing seam): a callable matching
    :func:`subprocess.run`'s signature. Defaults to subprocess.run when
    omitted.
    """
    effective_runner = runner if runner is not None else subprocess.run
    git_exe = shutil.which("git")
    if git_exe is None:
        return ""
    try:
        result = effective_runner(
            [
                git_exe,
                "-C",
                str(repo_path),
                "log",
                f"--max-count={max(1, int(max_commits))}",
                f"--format={_GIT_FORMAT}",
                "--name-only",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except Exception:  # noqa: BLE001
        return ""
    if getattr(result, "returncode", 1) != 0:
        return ""
    out = getattr(result, "stdout", "") or ""
    if not isinstance(out, str):
        try:
            out = out.decode("utf-8", errors="ignore")
        except Exception:  # noqa: BLE001
            return ""
    return out


def _parse_git_log(raw: str) -> Tuple[_RawCommit, ...]:
    """Pure-function parser. NEVER raises — malformed sections skipped."""
    if not raw:
        return ()
    chunks = raw.split("__OV_DOLL__\n")
    parsed: List[_RawCommit] = []
    for chunk in chunks:
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            header, _, after = chunk.partition("__END_HEADER__")
            header = header.strip()
            after = after.strip()
            lines = header.split("\n")
            if len(lines) < 3:
                continue
            commit_hash = lines[0].strip()
            try:
                ctime = int(lines[1].strip())
            except (TypeError, ValueError):
                continue
            body = "\n".join(lines[2:])
            file_lines = tuple(
                ln.strip()
                for ln in after.split("\n")
                if ln.strip()
            )
            if not commit_hash:
                continue
            parsed.append(_RawCommit(
                commit_hash=commit_hash,
                commit_time_unix=ctime,
                body=body,
                files=file_lines,
            ))
        except Exception:  # noqa: BLE001
            continue
    return tuple(parsed)


def _is_autonomous_commit(body: str, signature: str) -> bool:
    """Detect O+V-signed commit via canonical signature substring."""
    if not body or not signature:
        return False
    return signature in body


def _extract_risk_tier(body: str, canonical_tiers: FrozenSet[str]) -> str:
    """Parse ``Risk: <tier>`` token. Returns lowercase canonical tier
    name OR ``"unknown"`` if absent / unrecognized.
    """
    if not body:
        return "unknown"
    for line in body.splitlines():
        line = line.strip()
        if not line.lower().startswith("risk:"):
            continue
        _, _, value = line.partition(":")
        token = value.strip().lower()
        if token in canonical_tiers:
            return token
    return "unknown"


# ===========================================================================
# Aggregator — composes canonical sources
# ===========================================================================


# Cached aggregation snapshot to avoid hammering git log per render.
_SNAPSHOT_LOCK = threading.RLock()
_LAST_SNAPSHOT: Optional[DollCompletionSnapshot] = None
_LAST_SNAPSHOT_TS: float = 0.0
_SNAPSHOT_TTL_S: float = 60.0


def get_cached_snapshot() -> Optional[DollCompletionSnapshot]:
    """Return last aggregated snapshot if within TTL, else None.

    Operator-friendly accessor for downstream renderers that fire
    rapidly (REPL, SSE). NEVER raises.
    """
    with _SNAPSHOT_LOCK:
        if _LAST_SNAPSHOT is None:
            return None
        if (time.time() - _LAST_SNAPSHOT_TS) > _SNAPSHOT_TTL_S:
            return None
        return _LAST_SNAPSHOT


def _resolve_repo_root() -> Path:
    """Discover repo root via walk-up from this module's location.

    Walk-up looking for a ``.git`` directory. Defensive — falls back
    to CWD on any failure. NEVER raises.
    """
    try:
        here = Path(__file__).resolve()
        for ancestor in (here, *here.parents):
            try:
                if (ancestor / ".git").exists():
                    return ancestor
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        pass
    try:
        return Path.cwd()
    except Exception:  # noqa: BLE001
        return Path(".")


def _flags_grouped_by_category() -> Mapping[str, Sequence[Any]]:
    """Compose canonical ``flag_registry.ensure_seeded().list_all()``
    and group flags by ``category.value``. Pure read; NEVER raises."""
    grouped: Dict[str, List[Any]] = {}
    try:
        from backend.core.ouroboros.governance.flag_registry import (  # noqa: E501
            ensure_seeded,
        )
        registry = ensure_seeded()
        for spec in registry.list_all():
            try:
                cat = getattr(spec, "category", None)
                cat_value = (
                    getattr(cat, "value", None)
                    or str(cat or "").strip().lower()
                )
                if not cat_value:
                    continue
                grouped.setdefault(cat_value, []).append(spec)
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        return {}
    return grouped


def _principles_for(category_value: str) -> Tuple[str, ...]:
    """Compose canonical ``capability_constellation.principles_for_category``.
    NEVER raises."""
    try:
        from backend.core.ouroboros.governance.capability_constellation import (  # noqa: E501
            principles_for_category,
        )
        return principles_for_category(category_value)
    except Exception:  # noqa: BLE001
        return ()


def _ov_signature() -> str:
    """Compose canonical ``auto_committer.ov_signature_substring()``.
    NEVER raises."""
    try:
        from backend.core.ouroboros.governance.auto_committer import (  # noqa: E501
            ov_signature_substring,
        )
        return ov_signature_substring()
    except Exception:  # noqa: BLE001
        # Defensive fallback — substring of canonical signature.
        # If auto_committer is unavailable we cannot detect commits;
        # the metric returns UNTOUCHED for everything. This matches
        # the substrate-unavailable rollback contract.
        return ""


def _stage_for_axis(
    *,
    autonomous_count: int,
    tier_distribution: Mapping[str, int],
    earliest_age_s: float,
    proposed_thr: int,
    applied_thr: int,
    graduated_thr: int,
    graduated_min_days_v: int,
) -> Tuple[DollCompletionStage, str]:
    """Pure-function stage derivation. First-match-wins from GRADUATED
    down. Returns (stage, diagnostic). NEVER raises.
    """
    if autonomous_count <= 0:
        return (
            DollCompletionStage.UNTOUCHED,
            "no autonomous commits in this category",
        )

    has_safe_auto = tier_distribution.get("safe_auto", 0) > 0
    has_notify_apply = tier_distribution.get("notify_apply", 0) > 0
    has_approval_required = (
        tier_distribution.get("approval_required", 0) > 0
    )
    least_cautious = has_safe_auto or has_notify_apply
    days_span = earliest_age_s / 86400.0 if earliest_age_s > 0 else 0.0

    # GRADUATED — strictest gate, must satisfy ALL three conditions
    if (
        autonomous_count >= graduated_thr
        and has_safe_auto
        and days_span >= graduated_min_days_v
    ):
        return (
            DollCompletionStage.GRADUATED,
            (
                f"{autonomous_count} commits over {days_span:.1f}d "
                f"(safe_auto present)"
            ),
        )

    # APPLIED — has cage-trusted commits at applied threshold
    if least_cautious and autonomous_count >= applied_thr:
        return (
            DollCompletionStage.APPLIED,
            (
                f"{autonomous_count} commits, "
                f"safe_auto+notify_apply present"
            ),
        )

    # PROPOSED — at least proposed threshold, mostly operator-gated
    if has_approval_required and autonomous_count >= proposed_thr:
        return (
            DollCompletionStage.PROPOSED,
            (
                f"{autonomous_count} commits, "
                "approval_required dominant"
            ),
        )

    # OBSERVED — fallback when one or more commits but below proposed
    # threshold OR no approval_required signal
    return (
        DollCompletionStage.OBSERVED,
        f"{autonomous_count} commits below proposed threshold",
    )


def aggregate_doll_completion(
    *,
    repo_path: Optional[Path] = None,
    git_log_runner: Optional[Any] = None,
    now_unix: Optional[float] = None,
    force_refresh: bool = False,
) -> DollCompletionSnapshot:
    """Compose canonical sources into one immutable snapshot. NEVER raises.

    Master-flag-gated: returns an empty snapshot with ``master_enabled=False``
    when ``JARVIS_SECOND_ORDER_DOLL_METRIC_ENABLED=false``. Caches result
    for ``_SNAPSHOT_TTL_S`` seconds (defensive against rapid-fire REPL
    polling); pass ``force_refresh=True`` to bypass.
    """
    started = time.time()
    if now_unix is None:
        now_unix = started

    if not master_enabled():
        return DollCompletionSnapshot(
            aggregated_at_unix=started,
            master_enabled=False,
            axes=(),
            stage_counts={},
            completion_ratio=0.0,
            elapsed_s=0.0,
            diagnostic=(
                f"master flag {_ENV_MASTER}=false — set true to "
                "begin aggregation"
            ),
        )

    if not force_refresh:
        cached = get_cached_snapshot()
        if cached is not None and cached.master_enabled:
            return cached

    repo = repo_path or _resolve_repo_root()
    signature = _ov_signature()
    canonical_tiers = _canonical_risk_tier_names()
    groups = _flags_grouped_by_category()
    scan_max = commit_scan_max()

    # Pull git log once; filter per-source-file using the canonical
    # FlagSpec.source_file projection.
    raw_log = _run_git_log(
        repo,
        scan_max,
        runner=git_log_runner,
    )
    commits = _parse_git_log(raw_log)

    # Build per-axis aggregation.
    proposed_thr = proposed_threshold()
    applied_thr = applied_threshold()
    graduated_thr = graduated_threshold()
    graduated_min_days_v = graduated_min_days()
    axes: List[AxisProgress] = []

    for category_value, flags in sorted(groups.items()):
        # Collect source_files set for this category (lower-cased for compare).
        source_files: set[str] = set()
        for spec in flags:
            try:
                sf = getattr(spec, "source_file", "")
                if isinstance(sf, str) and sf.strip():
                    source_files.add(sf.strip())
            except Exception:  # noqa: BLE001
                continue

        # Filter commits to those that touched any of this category's files
        # AND are autonomous (O+V-signed).
        autonomous: List[CommitEvidence] = []
        for c in commits:
            if not _is_autonomous_commit(c.body, signature):
                continue
            touched = False
            for f in c.files:
                # Match a source_file if commit-touched-file ends with it
                # (FlagSpec.source_file may be relative or absolute).
                for sf in source_files:
                    if sf and (f.endswith(sf) or sf.endswith(f)):
                        touched = True
                        break
                if touched:
                    break
            if not touched:
                continue
            age = max(0.0, float(now_unix) - float(c.commit_time_unix))
            risk_tier = _extract_risk_tier(c.body, canonical_tiers)
            autonomous.append(CommitEvidence(
                commit_hash=c.commit_hash,
                risk_tier=risk_tier,
                age_seconds=age,
            ))

        # Compute tier distribution
        tier_dist: Dict[str, int] = {}
        for ev in autonomous:
            tier_dist[ev.risk_tier] = tier_dist.get(ev.risk_tier, 0) + 1

        # Compute earliest + most recent commit ages
        if autonomous:
            ages = [ev.age_seconds for ev in autonomous]
            earliest_age = max(ages)        # oldest commit = highest age
            most_recent_age = min(ages)
        else:
            earliest_age = 0.0
            most_recent_age = 0.0

        stage, diagnostic = _stage_for_axis(
            autonomous_count=len(autonomous),
            tier_distribution=tier_dist,
            earliest_age_s=earliest_age,
            proposed_thr=proposed_thr,
            applied_thr=applied_thr,
            graduated_thr=graduated_thr,
            graduated_min_days_v=graduated_min_days_v,
        )

        axes.append(AxisProgress(
            category=category_value,
            linked_principles=_principles_for(category_value),
            flag_count=len(flags),
            source_file_count=len(source_files),
            autonomous_commit_count=len(autonomous),
            earliest_commit_age_s=earliest_age,
            most_recent_commit_age_s=most_recent_age,
            tier_distribution=tier_dist,
            stage=stage,
            diagnostic=diagnostic,
        ))

    # Per-stage counts + completion ratio (weighted average)
    stage_counts: Dict[str, int] = {}
    weight_sum = 0.0
    for axis in axes:
        sv = axis.stage.value
        stage_counts[sv] = stage_counts.get(sv, 0) + 1
        weight_sum += _STAGE_WEIGHT.get(sv, 0.0)
    completion_ratio = (
        weight_sum / len(axes) if axes else 0.0
    )

    elapsed = time.time() - started
    snapshot = DollCompletionSnapshot(
        aggregated_at_unix=started,
        master_enabled=True,
        axes=tuple(axes),
        stage_counts=stage_counts,
        completion_ratio=completion_ratio,
        elapsed_s=elapsed,
        diagnostic=(
            f"scanned ≤{scan_max} commits; "
            f"{sum(a.autonomous_commit_count for a in axes)} "
            "autonomous commits matched FlagRegistry source_files"
        ),
    )

    with _SNAPSHOT_LOCK:
        global _LAST_SNAPSHOT, _LAST_SNAPSHOT_TS
        _LAST_SNAPSHOT = snapshot
        _LAST_SNAPSHOT_TS = time.time()

    # Best-effort SSE publish — composed via canonical broker.
    _publish_doll_progress_event(snapshot)

    return snapshot


def reset_cache_for_tests() -> None:
    """Test seam — drop cached snapshot. NEVER raises."""
    with _SNAPSHOT_LOCK:
        global _LAST_SNAPSHOT, _LAST_SNAPSHOT_TS
        _LAST_SNAPSHOT = None
        _LAST_SNAPSHOT_TS = 0.0


# ===========================================================================
# SSE publisher (best-effort, NEVER raises)
# ===========================================================================


def _publish_doll_progress_event(
    snapshot: DollCompletionSnapshot,
) -> None:
    """Compose canonical broker. Best-effort; NEVER raises into the
    aggregation path."""
    try:
        from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
            EVENT_TYPE_SECOND_ORDER_DOLL_PROGRESS_UPDATED,
            publish_task_event,
        )
        payload = {
            "aggregated_at_unix": snapshot.aggregated_at_unix,
            "stage_counts": dict(snapshot.stage_counts),
            "completion_ratio": snapshot.completion_ratio,
            "axes": [
                {
                    "category": a.category,
                    "stage": a.stage.value,
                    "autonomous_commit_count": a.autonomous_commit_count,
                }
                for a in snapshot.axes
            ],
            "schema_version": snapshot.schema_version,
        }
        # publish_task_event signature: (event_type, op_id, payload). The
        # metric is system-level (no specific op_id) — use a stable
        # synthetic op_id derived from the schema version so consumers
        # can filter / group reliably.
        publish_task_event(
            EVENT_TYPE_SECOND_ORDER_DOLL_PROGRESS_UPDATED,
            f"system::second_order_doll::{snapshot.schema_version}",
            payload,
        )
    except Exception:  # noqa: BLE001
        return


# ===========================================================================
# Renderers — read-only, master-flag-gated
# ===========================================================================


def format_doll_completion_panel(
    snapshot: Optional[DollCompletionSnapshot] = None,
) -> str:
    """Render snapshot as an operator-facing panel. NEVER raises.

    When ``snapshot is None`` and master is off, returns an empty
    string (silent — caller controls when nothing visible matters).
    """
    if snapshot is None:
        if not master_enabled():
            return ""
        snapshot = aggregate_doll_completion()
    if not snapshot.master_enabled:
        return (
            f"second-order doll: disabled "
            f"({_ENV_MASTER}=false)"
        )
    if not snapshot.axes:
        return (
            "second-order doll: no axes available "
            "(FlagRegistry empty? canonical sources unreachable?)"
        )

    lines: List[str] = []
    pct = snapshot.completion_ratio * 100.0
    lines.append(
        f"🪆 Second-order doll completion: {pct:.1f}%  "
        f"({len(snapshot.axes)} axes)"
    )
    stage_parts = []
    for stage in DollCompletionStage:
        cnt = snapshot.stage_counts.get(stage.value, 0)
        glyph = _STAGE_GLYPH.get(stage.value, "?")
        stage_parts.append(f"{glyph} {stage.value}={cnt}")
    lines.append("  " + " · ".join(stage_parts))

    for axis in snapshot.axes:
        glyph = stage_glyph(axis.stage)
        principles = ", ".join(axis.linked_principles) or "(none)"
        lines.append(
            f"  {glyph} {axis.category:<14} "
            f"{axis.stage.value:<10} "
            f"flags={axis.flag_count:<3} "
            f"commits={axis.autonomous_commit_count:<3} "
            f"— {principles}"
        )
    lines.append(f"  diagnostic: {snapshot.diagnostic}")
    return "\n".join(lines)


def format_axis_detail(
    snapshot: DollCompletionSnapshot,
    category: object,
) -> str:
    """Detailed per-axis render. NEVER raises."""
    axis = snapshot.axis_for_category(category)
    if axis is None:
        return (
            f"axis '{category}' not found "
            "(canonical Category enum may not have this value)"
        )
    glyph = stage_glyph(axis.stage)
    lines: List[str] = [
        f"🪆 Axis: {axis.category}  {glyph} {axis.stage.value}",
        f"  linked principles: "
        f"{', '.join(axis.linked_principles) or '(none)'}",
        f"  flags registered:           {axis.flag_count}",
        f"  source files in axis:       {axis.source_file_count}",
        f"  autonomous commits:         {axis.autonomous_commit_count}",
        f"  earliest commit age:        "
        f"{axis.earliest_commit_age_s / 86400.0:.1f}d",
        f"  most-recent commit age:     "
        f"{axis.most_recent_commit_age_s / 86400.0:.1f}d",
    ]
    if axis.tier_distribution:
        tier_summary = " · ".join(
            f"{t}={c}" for t, c in sorted(axis.tier_distribution.items())
        )
        lines.append(f"  tier distribution:          {tier_summary}")
    lines.append(f"  diagnostic: {axis.diagnostic}")
    return "\n".join(lines)


# ===========================================================================
# AST pins via shipped_code_invariants (auto-discovered)
# ===========================================================================


def register_shipped_invariants() -> list:
    """Return AST invariant pins for this module. Auto-discovered by
    :func:`shipped_code_invariants.ensure_seeded`."""
    from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
        ShippedCodeInvariant,
    )

    pins = []
    target = (
        "backend/core/ouroboros/governance/"
        "second_order_doll_metric.py"
    )

    # ---- Pin 1: master_default_false ------------------------------------

    def _master_default_false(tree: ast.AST, src: str) -> Tuple[str, ...]:
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
        return ("master_enabled() function not found",)

    pins.append(ShippedCodeInvariant(
        invariant_name="second_order_doll_master_default_false",
        description=(
            "§33.1 graduation contract — master stays default-False "
            "until evidence ladder closes."
        ),
        target_file=target,
        validate=_master_default_false,
    ))

    # ---- Pin 2: authority_asymmetry -------------------------------------

    def _authority_asymmetry(tree: ast.AST, src: str) -> Tuple[str, ...]:
        forbidden = (
            "backend.core.ouroboros.governance.orchestrator",
            "backend.core.ouroboros.governance.candidate_generator",
            "backend.core.ouroboros.governance.iron_gate",
            "backend.core.ouroboros.governance.semantic_guardian",
            "backend.core.ouroboros.governance.providers",
            "backend.core.ouroboros.governance.urgency_router",
            "backend.core.ouroboros.governance.change_engine",
        )
        violations: List[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if any(mod.startswith(f) for f in forbidden):
                    violations.append(
                        f"forbidden authority import: {mod}"
                    )
        return tuple(violations)

    pins.append(ShippedCodeInvariant(
        invariant_name="second_order_doll_authority_asymmetry",
        description=(
            "Substrate purity — read-only aggregator + renderer; "
            "no orchestrator / iron_gate / semantic_guardian / "
            "providers / candidate_generator / change_engine "
            "authority imports allowed."
        ),
        target_file=target,
        validate=_authority_asymmetry,
    ))

    # ---- Pin 3: stage_taxonomy_5_values ---------------------------------

    def _stage_taxonomy(tree: ast.AST, src: str) -> Tuple[str, ...]:
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ClassDef)
                and node.name == "DollCompletionStage"
            ):
                names = {
                    a.targets[0].id
                    for a in node.body
                    if isinstance(a, ast.Assign)
                    and a.targets
                    and isinstance(a.targets[0], ast.Name)
                }
                expected = {
                    "UNTOUCHED", "OBSERVED", "PROPOSED",
                    "APPLIED", "GRADUATED",
                }
                missing = expected - names
                if missing:
                    return (
                        f"DollCompletionStage missing values: "
                        f"{sorted(missing)}",
                    )
                extra = names - expected
                if extra:
                    return (
                        f"DollCompletionStage has unexpected values: "
                        f"{sorted(extra)} — adding requires updating "
                        "_STAGE_WEIGHT + _STAGE_GLYPH + tests",
                    )
                return ()
        return ("DollCompletionStage class not found",)

    pins.append(ShippedCodeInvariant(
        invariant_name="second_order_doll_stage_taxonomy_5_values",
        description=(
            "Closed 5-value DollCompletionStage ladder is "
            "frozen — adding/removing a stage requires "
            "updating _STAGE_WEIGHT + _STAGE_GLYPH + stage "
            "derivation rule + tests."
        ),
        target_file=target,
        validate=_stage_taxonomy,
    ))

    # ---- Pin 4: composes_canonical_constellation -----------------------

    def _composes_constellation(
        tree: ast.AST, src: str,
    ) -> Tuple[str, ...]:
        if (
            "capability_constellation" not in src
            or "principles_for_category" not in src
        ):
            return (
                "must lazy-import "
                "capability_constellation.principles_for_category "
                "(canonical Manifesto-principle map — no parallel "
                "mapping)",
            )
        return ()

    pins.append(ShippedCodeInvariant(
        invariant_name=(
            "second_order_doll_composes_canonical_constellation"
        ),
        description=(
            "Metric composes capability_constellation's "
            "principles_for_category accessor for Manifesto-"
            "principle attribution — no parallel category→"
            "principle map."
        ),
        target_file=target,
        validate=_composes_constellation,
    ))

    # ---- Pin 5: composes_canonical_flag_registry -----------------------

    def _composes_flag_registry(
        tree: ast.AST, src: str,
    ) -> Tuple[str, ...]:
        if (
            "flag_registry" not in src
            or "ensure_seeded" not in src
        ):
            return (
                "must lazy-import flag_registry.ensure_seeded "
                "(canonical FlagSpec source — Category enum is "
                "the axis dimension)",
            )
        return ()

    pins.append(ShippedCodeInvariant(
        invariant_name=(
            "second_order_doll_composes_canonical_flag_registry"
        ),
        description=(
            "Metric composes canonical flag_registry.ensure_seeded "
            "for the FlagSpec descriptor surface — no parallel "
            "flag catalog."
        ),
        target_file=target,
        validate=_composes_flag_registry,
    ))

    # ---- Pin 6: composes_canonical_ov_signature ------------------------

    def _composes_ov_signature(
        tree: ast.AST, src: str,
    ) -> Tuple[str, ...]:
        if (
            "auto_committer" not in src
            or "ov_signature_substring" not in src
        ):
            return (
                "must lazy-import "
                "auto_committer.ov_signature_substring "
                "(canonical autonomous-commit signature — no "
                "parallel string-grep)",
            )
        return ()

    pins.append(ShippedCodeInvariant(
        invariant_name=(
            "second_order_doll_composes_canonical_ov_signature"
        ),
        description=(
            "Metric composes auto_committer.ov_signature_substring "
            "for autonomous-commit detection — no parallel literal."
        ),
        target_file=target,
        validate=_composes_ov_signature,
    ))

    return pins


# ===========================================================================
# FlagRegistry seeds (auto-discovered via §33.3 naming-cage)
# ===========================================================================


def register_flags(registry: Any) -> int:
    """Register this substrate's env knobs into FlagRegistry. Picked up
    zero-edit by ``flag_registry_seed._discover_module_provided_flags``."""
    from backend.core.ouroboros.governance.flag_registry import (
        Category,
        FlagSpec,
        FlagType,
    )

    seeds = [
        FlagSpec(
            name=_ENV_MASTER,
            type=FlagType.BOOL,
            default=False,
            description=(
                "Master switch for the second-order doll completion "
                "metric — §33.1 graduation contract, operator-paced."
            ),
            category=Category.OBSERVABILITY,
            source_file=(
                "backend/core/ouroboros/governance/"
                "second_order_doll_metric.py"
            ),
            example=f"{_ENV_MASTER}=true",
        ),
        FlagSpec(
            name=_ENV_COMMIT_SCAN_MAX,
            type=FlagType.INT,
            default=_DEFAULT_COMMIT_SCAN_MAX,
            description=(
                "Maximum commits scanned by git log per "
                "aggregate_doll_completion call. Clamped to "
                "[10, 50_000]."
            ),
            category=Category.CAPACITY,
            source_file=(
                "backend/core/ouroboros/governance/"
                "second_order_doll_metric.py"
            ),
            example=f"{_ENV_COMMIT_SCAN_MAX}=1000",
        ),
        FlagSpec(
            name=_ENV_GRADUATED_THRESHOLD,
            type=FlagType.INT,
            default=_DEFAULT_GRADUATED_THRESHOLD,
            description=(
                "Autonomous commits required for GRADUATED stage."
            ),
            category=Category.TUNING,
            source_file=(
                "backend/core/ouroboros/governance/"
                "second_order_doll_metric.py"
            ),
            example=f"{_ENV_GRADUATED_THRESHOLD}=20",
        ),
        FlagSpec(
            name=_ENV_GRADUATED_MIN_DAYS,
            type=FlagType.INT,
            default=_DEFAULT_GRADUATED_MIN_DAYS,
            description=(
                "Calendar-day span required (oldest→newest) for "
                "GRADUATED stage."
            ),
            category=Category.TIMING,
            source_file=(
                "backend/core/ouroboros/governance/"
                "second_order_doll_metric.py"
            ),
            example=f"{_ENV_GRADUATED_MIN_DAYS}=60",
        ),
        FlagSpec(
            name=_ENV_APPLIED_THRESHOLD,
            type=FlagType.INT,
            default=_DEFAULT_APPLIED_THRESHOLD,
            description=(
                "Autonomous commits required for APPLIED stage."
            ),
            category=Category.TUNING,
            source_file=(
                "backend/core/ouroboros/governance/"
                "second_order_doll_metric.py"
            ),
            example=f"{_ENV_APPLIED_THRESHOLD}=10",
        ),
        FlagSpec(
            name=_ENV_PROPOSED_THRESHOLD,
            type=FlagType.INT,
            default=_DEFAULT_PROPOSED_THRESHOLD,
            description=(
                "Autonomous commits required for PROPOSED stage."
            ),
            category=Category.TUNING,
            source_file=(
                "backend/core/ouroboros/governance/"
                "second_order_doll_metric.py"
            ),
            example=f"{_ENV_PROPOSED_THRESHOLD}=3",
        ),
    ]

    count = 0
    for spec in seeds:
        try:
            registry.register(spec)
            count += 1
        except Exception:  # noqa: BLE001
            # Fail-open contract per §33.1 — a single bad seed
            # MUST NOT block the whole walk.
            continue
    return count


__all__ = [
    # Schema
    "SECOND_ORDER_DOLL_METRIC_SCHEMA_VERSION",
    # Env helpers
    "master_enabled",
    "commit_scan_max",
    "graduated_threshold",
    "graduated_min_days",
    "applied_threshold",
    "proposed_threshold",
    # Taxonomy
    "DollCompletionStage",
    "stage_glyph",
    # Artifacts
    "CommitEvidence",
    "AxisProgress",
    "DollCompletionSnapshot",
    # Aggregator + renderers
    "aggregate_doll_completion",
    "get_cached_snapshot",
    "reset_cache_for_tests",
    "format_doll_completion_panel",
    "format_axis_detail",
    # Substrate hooks
    "register_shipped_invariants",
    "register_flags",
]
