"""
Goal Metric Dashboard — Autonomous Throughput Aggregator
========================================================

Read-only observability substrate that measures the ONE thing the O+V
manifesto's stated objective is actually about: is the codebase engineering
its own evolution, and how fast / how reliably / at what cost?

It answers three questions from GROUND TRUTH, never from a value judgment:

  1. **Net-positive landed changes per unattended day** — how many genuinely
     mutated, VERIFY-validated, autonomously-committed diffs the organism
     landed and *kept*, normalized by honest unattended runtime.
  2. **Regression rate** — of what landed, how much was later reverted
     (the objective "it didn't hold up" signal).
  3. **Cost** — the amortized dollars spent per landed change, including the
     failed attempts (the honest economic efficiency of autonomy).

Composition contract (mandate: DRY — zero parallel state, zero new ledger,
zero duplicated logic). Two canonical sources only:

  * ``auto_committer.ov_coauthor_line()`` — the canonical, ASCII, drift-stable
    trailer that PROVES a commit was authored by the AutoCommitter. Because
    the AutoCommitter stamps it ONLY after APPLY+VERIFY passes (the sole code
    path that writes it), the trailer's presence is a structural proof of
    "genuinely mutated + validated + committed" — mandate 4, for free.
  * The battle-test session ``summary.json`` set (``.ouroboros/sessions/bt-*``)
    — the reconciled per-session record of ``duration_s`` (unattended
    runtime), ``cost_total``, and the per-op ``operations[]`` array carrying
    ``terminal_reason_code`` (``verify_regression`` = pipeline-caught
    regression). Same artifact for runtime + cost + pipeline-regression →
    one read, self-consistent window.

Bulletproof invariants:
  * A "landed change" is NEVER counted without a git-provable, trailer-bearing,
    NON-EMPTY (files-changed) commit reachable from the target branch. No
    trailer / no files → not counted.
  * Orange-tier review commits (``chore(ouroboros-review):`` / body
    ``DO NOT AUTO-MERGE``) are human-gated, carry NO trailer, and are excluded
    (defense-in-depth even though the trailer filter already excludes them).
  * Every ratio is division-guarded → ``None`` on a zero denominator; the
    aggregator NEVER raises (every path defensive). Zero-output initial runs
    return a fully-formed snapshot of zeros/Nones, not an exception.

Authority posture: this module is a read-only projector. It imports NO
orchestrator / policy / gate module. It only runs ``git log`` (read-only
subprocess) and reads session-summary JSON.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

AUTONOMY_METRICS_SCHEMA_VERSION = "autonomy.1"

# Unique record/field sentinels for the git-log projection — private to this
# module (each metric owns its own narrow git projection; only the trailer
# marker is shared, per the canonical-source composition contract).
_REC_SEP = "__OV_AUTONOMY_REC__"
_GIT_FORMAT = _REC_SEP + "%n%H%n%ct%n%B%n__OV_AUTONOMY_ENDHDR__"

# Reverted-commit reference, e.g. "This reverts commit 1a2b3c4d...".
_REVERT_RE = re.compile(r"This reverts commit ([0-9a-fA-F]{7,40})")
_RISK_RE = re.compile(r"^Risk:\s*([A-Za-z_]+)", re.MULTILINE)
_ORANGE_SUBJECT_PREFIX = "chore(ouroboros-review):"
_ORANGE_BODY_MARKER = "DO NOT AUTO-MERGE"

_CANONICAL_TIERS = frozenset(
    {"safe_auto", "notify_apply", "approval_required", "blocked"}
)


# ---------------------------------------------------------------------------
# Env knobs — additive, clamped, never authoritative
# ---------------------------------------------------------------------------


def master_enabled() -> bool:
    """``JARVIS_AUTONOMY_METRICS_ENABLED`` (default true). Read-only surface;
    a kill switch only silences the endpoint, it changes no behavior."""
    raw = os.environ.get("JARVIS_AUTONOMY_METRICS_ENABLED", "true")
    return raw.strip().lower() not in ("0", "false", "no", "off")


def _read_clamped_int(env_name: str, default: int, lo: int, hi: int) -> int:
    try:
        v = int(os.environ.get(env_name, "").strip() or default)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


def window_days() -> int:
    """``JARVIS_AUTONOMY_METRICS_WINDOW_DAYS`` — lookback window for BOTH the
    git scan and the session set (single aligned window). Default 30."""
    return _read_clamped_int("JARVIS_AUTONOMY_METRICS_WINDOW_DAYS", 30, 1, 3650)


def target_branch() -> str:
    """``JARVIS_AUTONOMY_METRICS_BRANCH`` — the branch on which a change must
    be reachable to count as landed (excludes Orange-PR + un-promoted
    workspace branches). Default ``main``."""
    raw = os.environ.get("JARVIS_AUTONOMY_METRICS_BRANCH", "").strip()
    return raw or "main"


def commit_scan_max() -> int:
    """``JARVIS_AUTONOMY_METRICS_COMMIT_SCAN_MAX`` — hard cap on commits
    walked (belt-and-braces beyond the ``--since`` window). Default 5000."""
    return _read_clamped_int(
        "JARVIS_AUTONOMY_METRICS_COMMIT_SCAN_MAX", 5000, 100, 100000
    )


def session_scan_max() -> int:
    """``JARVIS_AUTONOMY_METRICS_SESSION_SCAN_MAX`` — cap on session dirs read
    per aggregation (newest-first). Default 500."""
    return _read_clamped_int(
        "JARVIS_AUTONOMY_METRICS_SESSION_SCAN_MAX", 500, 10, 10000
    )


def _ov_trailer() -> str:
    """Canonical O+V commit trailer, composed from the single source of truth
    (``auto_committer.ov_coauthor_line``). ASCII + drift-stable. Empty string
    on any failure → the scan then counts ZERO landed changes (fail-closed:
    an unresolvable marker can never over-count)."""
    try:
        from backend.core.ouroboros.governance.auto_committer import (
            ov_coauthor_line,
        )
        return ov_coauthor_line()
    except Exception:  # noqa: BLE001 — fail-closed to empty marker
        return ""


# ---------------------------------------------------------------------------
# git-log projection — read-only subprocess, NEVER raises
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Commit:
    commit_hash: str
    commit_time_unix: int
    body: str
    files: Tuple[str, ...]


def _run_git_log(
    repo_path: Path,
    branch: str,
    since_epoch: float,
    max_commits: int,
    *,
    runner: Optional[Any] = None,
) -> str:
    """Invoke ``git log <branch> --since`` with a non-shell argv. Returns the
    raw formatted output; ``""`` on any failure (git missing, bad branch,
    non-zero rc, timeout). ``runner`` is an injectable subprocess.run seam."""
    effective_runner = runner if runner is not None else subprocess.run
    git_exe = shutil.which("git")
    if git_exe is None:
        return ""
    try:
        result = effective_runner(
            [
                git_exe, "-C", str(repo_path), "log", branch,
                f"--since={max(0.0, float(since_epoch)):.0f}",
                f"--max-count={max(1, int(max_commits))}",
                f"--format={_GIT_FORMAT}",
                "--name-only", "--no-color",
            ],
            capture_output=True, text=True, timeout=30, check=False,
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


def _parse_commits(raw: str) -> Tuple[_Commit, ...]:
    """Pure parser; NEVER raises — malformed records are skipped."""
    if not raw:
        return ()
    parsed: List[_Commit] = []
    for chunk in raw.split(_REC_SEP + "\n"):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            header, _, after = chunk.partition("__OV_AUTONOMY_ENDHDR__")
            lines = header.strip().split("\n")
            if len(lines) < 3:
                continue
            commit_hash = lines[0].strip()
            if not commit_hash:
                continue
            try:
                ctime = int(lines[1].strip())
            except (TypeError, ValueError):
                continue
            body = "\n".join(lines[2:])
            files = tuple(
                ln.strip() for ln in after.strip().split("\n") if ln.strip()
            )
            parsed.append(_Commit(commit_hash, ctime, body, files))
        except Exception:  # noqa: BLE001
            continue
    return tuple(parsed)


def _is_ov_commit(body: str, trailer: str) -> bool:
    return bool(trailer) and trailer in body


def _is_orange(body: str) -> bool:
    """Human-gated Orange-tier review commit — excluded defense-in-depth."""
    if not body:
        return False
    first = body.strip().split("\n", 1)[0]
    return (
        first.startswith(_ORANGE_SUBJECT_PREFIX)
        or _ORANGE_BODY_MARKER in body
    )


def _risk_tier(body: str) -> str:
    """Extract the ``Risk: <TIER>`` token as a canonical lowercase name, or
    ``"unknown"``."""
    if not body:
        return "unknown"
    m = _RISK_RE.search(body)
    if not m:
        return "unknown"
    token = m.group(1).strip().lower()
    return token if token in _CANONICAL_TIERS else "unknown"


# ---------------------------------------------------------------------------
# Session-summary projection — reconciled runtime + cost + pipeline-regression
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _SessionFacts:
    duration_s: float
    cost_total: float
    cost_breakdown: Dict[str, float]
    total_ops: int
    verify_regressions: int
    suspension_likely: bool


def _sessions_root(repo_path: Path) -> Path:
    return repo_path / ".ouroboros" / "sessions"


def _session_dir_epoch(name: str) -> Optional[float]:
    """Best-effort start epoch from the session dir name.

    ``bt-YYYY-MM-DD-HHMMSS`` (lexicographic == chronological) or
    ``bt-iso-<epoch>``. Returns None if unparseable (caller falls back to the
    summary file mtime)."""
    try:
        if name.startswith("bt-iso-"):
            return float(name[len("bt-iso-"):])
        # bt-YYYY-MM-DD-HHMMSS
        parts = name.split("-")
        if len(parts) >= 5 and parts[0] == "bt":
            y, mo, d, hms = parts[1], parts[2], parts[3], parts[4]
            import calendar
            tm = (
                int(y), int(mo), int(d),
                int(hms[0:2]), int(hms[2:4]), int(hms[4:6]),
                0, 0, -1,
            )
            return float(calendar.timegm(tm))
    except Exception:  # noqa: BLE001
        return None
    return None


def _read_session_summary(path: Path) -> Optional[_SessionFacts]:
    """Parse one ``summary.json`` into the narrow facts we need. NEVER raises;
    returns None if the file is unreadable/unparseable."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(raw, dict):
        return None

    def _f(key: str, default: float = 0.0) -> float:
        try:
            v = raw.get(key)
            return float(v) if v is not None else default
        except (TypeError, ValueError):
            return default

    breakdown: Dict[str, float] = {}
    try:
        cb = raw.get("cost_breakdown")
        if isinstance(cb, dict):
            for k, v in cb.items():
                try:
                    breakdown[str(k)] = float(v)
                except (TypeError, ValueError):
                    continue
    except Exception:  # noqa: BLE001
        breakdown = {}

    total_ops = 0
    verify_regressions = 0
    try:
        ops = raw.get("operations")
        if isinstance(ops, list):
            total_ops = len(ops)
            for op in ops:
                if not isinstance(op, dict):
                    continue
                if (
                    str(op.get("status", "")).lower() == "failed"
                    and str(op.get("terminal_reason_code", ""))
                    == "verify_regression"
                ):
                    verify_regressions += 1
    except Exception:  # noqa: BLE001
        pass

    return _SessionFacts(
        duration_s=_f("duration_s"),
        cost_total=_f("cost_total"),
        cost_breakdown=breakdown,
        total_ops=total_ops,
        verify_regressions=verify_regressions,
        suspension_likely=bool(raw.get("suspension_likely", False)),
    )


def _read_sessions(
    repo_path: Path, since_epoch: float, scan_max: int,
) -> Tuple[List[_SessionFacts], int]:
    """Read in-window session summaries (newest-first, capped). Returns
    (facts, suspension_excluded_count). NEVER raises."""
    root = _sessions_root(repo_path)
    facts: List[_SessionFacts] = []
    suspension_excluded = 0
    try:
        if not root.is_dir():
            return facts, 0
        dirs = sorted(
            (p for p in root.iterdir() if p.is_dir() and p.name.startswith("bt-")),
            key=lambda p: p.name, reverse=True,
        )[:scan_max]
    except Exception:  # noqa: BLE001
        return facts, 0
    for d in dirs:
        summary = d / "summary.json"
        if not summary.is_file():
            continue
        # Window filter: dir-name epoch, else summary mtime.
        epoch = _session_dir_epoch(d.name)
        if epoch is None:
            try:
                epoch = summary.stat().st_mtime
            except Exception:  # noqa: BLE001
                epoch = None
        if epoch is not None and epoch < since_epoch:
            continue
        f = _read_session_summary(summary)
        if f is None:
            continue
        # Suspension-likely sessions are NOT honest unattended time — exclude
        # from the runtime denominator (mandate 2: objective runtime reality).
        if f.suspension_likely:
            suspension_excluded += 1
            continue
        facts.append(f)
    return facts, suspension_excluded


# ---------------------------------------------------------------------------
# Aggregator
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AutonomySnapshot:
    schema_version: str = AUTONOMY_METRICS_SCHEMA_VERSION
    enabled: bool = True
    reason_code: str = "ok"
    generated_at_unix: float = 0.0
    window_days: int = 30
    branch: str = "main"
    # landed
    landed_count: int = 0
    net_positive_landed: int = 0
    reverted_landed: int = 0
    landed_by_risk_tier: Dict[str, int] = field(default_factory=dict)
    # throughput
    unattended_days: float = 0.0
    landed_per_unattended_day: Optional[float] = None
    sessions_counted: int = 0
    sessions_suspension_excluded: int = 0
    # regression
    post_landing_regression_rate: Optional[float] = None
    pipeline_caught_verify_regressions: int = 0
    total_ops_in_window: int = 0
    # cost
    total_cost_usd: float = 0.0
    cost_per_landed_change_usd: Optional[float] = None
    cost_by_provider: Dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "enabled": self.enabled,
            "reason_code": self.reason_code,
            "generated_at_unix": self.generated_at_unix,
            "window_days": self.window_days,
            "landed": {
                "branch": self.branch,
                "count": self.landed_count,
                "net_positive": self.net_positive_landed,
                "reverted": self.reverted_landed,
                "by_risk_tier": dict(self.landed_by_risk_tier),
            },
            "throughput": {
                "unattended_days": round(self.unattended_days, 4),
                "landed_per_unattended_day": self.landed_per_unattended_day,
                "net_positive_per_unattended_day": (
                    round(self.net_positive_landed / self.unattended_days, 4)
                    if self.unattended_days > 0 else None
                ),
                "sessions_counted": self.sessions_counted,
                "sessions_suspension_excluded": self.sessions_suspension_excluded,
            },
            "regression": {
                "post_landing_rate": self.post_landing_regression_rate,
                "reverted_landed": self.reverted_landed,
                "pipeline_caught_verify_regressions": (
                    self.pipeline_caught_verify_regressions
                ),
                "total_ops_in_window": self.total_ops_in_window,
            },
            "cost": {
                "total_usd": round(self.total_cost_usd, 6),
                "per_landed_change_usd": self.cost_per_landed_change_usd,
                "by_provider": {
                    k: round(v, 6) for k, v in self.cost_by_provider.items()
                },
            },
            "sources": {
                "landed": "git log <branch> filtered by O+V co-author trailer (non-empty diff)",
                "runtime_and_cost": "session summary.json (duration_s / cost_total, suspension-gated)",
                "regression": "git reverts of trailered commits + operations[].verify_regression",
            },
        }


def _resolve_repo_root(explicit: Optional[Path] = None) -> Path:
    if explicit is not None:
        return explicit
    # Walk up from this module to the repo root (contains .git or .ouroboros).
    here = Path(__file__).resolve()
    for parent in [here, *here.parents]:
        if (parent / ".git").exists() or (parent / ".ouroboros").is_dir():
            return parent
    return Path.cwd()


def _safe_ratio(num: float, den: float) -> Optional[float]:
    """Division-guarded ratio → None on a zero (or non-positive) denominator.
    The single place zero-output states are made exception-proof."""
    if den <= 0:
        return None
    try:
        return round(num / den, 6)
    except Exception:  # noqa: BLE001
        return None


def aggregate_autonomy_metrics(
    *,
    repo_root: Optional[Path] = None,
    now: Optional[float] = None,
    git_runner: Optional[Any] = None,
) -> AutonomySnapshot:
    """Compose the goal-metric snapshot from git + session summaries.

    Pure of side effects (read-only). NEVER raises — any fault degrades to a
    partial snapshot with a diagnostic ``reason_code``. Zero-output initial
    runs return an all-zero/None snapshot, not an error.
    """
    try:
        _now = float(now) if now is not None else time.time()
        _win_days = window_days()
        _branch = target_branch()
        _since = _now - (_win_days * 86400.0)
        repo = _resolve_repo_root(repo_root)

        # --- git: landed changes + reverts ---
        trailer = _ov_trailer()
        landed_count = 0
        by_tier: Dict[str, int] = {}
        landed_short_shas: set = set()
        reason = "ok"
        if not trailer:
            reason = "no_ov_marker"
        raw = _run_git_log(
            repo, _branch, _since, commit_scan_max(), runner=git_runner,
        )
        if not raw and reason == "ok":
            reason = "no_git_history"
        commits = _parse_commits(raw)
        reverted_refs: set = set()
        for c in commits:
            body = c.body
            # Collect revert references from ANY commit (human reverts of
            # autonomous work are the post-landing regression signal).
            for m in _REVERT_RE.finditer(body):
                reverted_refs.add(m.group(1).lower())
            if not _is_ov_commit(body, trailer):
                continue
            if _is_orange(body):
                continue  # human-gated, defense-in-depth
            if not c.files:
                continue  # NOT genuinely mutated → never counted (mandate 4)
            landed_count += 1
            tier = _risk_tier(body)
            by_tier[tier] = by_tier.get(tier, 0) + 1
            landed_short_shas.add(c.commit_hash.lower())
            landed_short_shas.add(c.commit_hash.lower()[:12])

        # Reverted-landed: a revert whose target sha matches an in-window
        # landed autonomous commit. Prefix-tolerant (git reverts may cite a
        # short sha).
        reverted_landed = 0
        for ref in reverted_refs:
            r = ref.lower()
            if r in landed_short_shas or any(
                sha.startswith(r) or r.startswith(sha)
                for sha in landed_short_shas
            ):
                reverted_landed += 1
        reverted_landed = min(reverted_landed, landed_count)
        net_positive = landed_count - reverted_landed

        # --- sessions: runtime + cost + pipeline regression ---
        facts, suspension_excluded = _read_sessions(
            repo, _since, session_scan_max()
        )
        total_runtime_s = sum(f.duration_s for f in facts)
        total_cost = sum(f.cost_total for f in facts)
        total_ops = sum(f.total_ops for f in facts)
        verify_regressions = sum(f.verify_regressions for f in facts)
        cost_by_provider: Dict[str, float] = {}
        for f in facts:
            for prov, usd in f.cost_breakdown.items():
                cost_by_provider[prov] = cost_by_provider.get(prov, 0.0) + usd
        unattended_days = total_runtime_s / 86400.0

        return AutonomySnapshot(
            enabled=True,
            reason_code=reason,
            generated_at_unix=_now,
            window_days=_win_days,
            branch=_branch,
            landed_count=landed_count,
            net_positive_landed=net_positive,
            reverted_landed=reverted_landed,
            landed_by_risk_tier=by_tier,
            unattended_days=unattended_days,
            landed_per_unattended_day=_safe_ratio(landed_count, unattended_days),
            sessions_counted=len(facts),
            sessions_suspension_excluded=suspension_excluded,
            post_landing_regression_rate=_safe_ratio(reverted_landed, landed_count),
            pipeline_caught_verify_regressions=verify_regressions,
            total_ops_in_window=total_ops,
            total_cost_usd=total_cost,
            cost_per_landed_change_usd=_safe_ratio(total_cost, landed_count),
            cost_by_provider=cost_by_provider,
        )
    except Exception:  # noqa: BLE001 — aggregator MUST never raise
        return AutonomySnapshot(
            enabled=True, reason_code="aggregation_error",
            generated_at_unix=(now if now is not None else 0.0) or 0.0,
        )


# ---------------------------------------------------------------------------
# Cached snapshot — avoid hammering git/fs on rapid renders (SSE/REPL/poll)
# ---------------------------------------------------------------------------

_SNAPSHOT_LOCK = threading.RLock()
_LAST_SNAPSHOT: Optional[AutonomySnapshot] = None
_LAST_SNAPSHOT_TS: float = 0.0


def _snapshot_ttl_s() -> float:
    try:
        return max(1.0, float(
            os.environ.get("JARVIS_AUTONOMY_METRICS_TTL_S", "").strip() or 30.0
        ))
    except (TypeError, ValueError):
        return 30.0


def snapshot(
    *, repo_root: Optional[Path] = None, force: bool = False,
) -> Dict[str, Any]:
    """Public entry point for the observability GET — returns a serializable
    dict. TTL-cached. When the master flag is off, returns a minimal
    ``enabled: false`` dict (the endpoint layer decides 403 vs body). NEVER
    raises."""
    if not master_enabled():
        return {
            "schema_version": AUTONOMY_METRICS_SCHEMA_VERSION,
            "enabled": False,
            "reason_code": "disabled",
        }
    global _LAST_SNAPSHOT, _LAST_SNAPSHOT_TS
    now = time.time()
    with _SNAPSHOT_LOCK:
        if (
            not force
            and _LAST_SNAPSHOT is not None
            and (now - _LAST_SNAPSHOT_TS) <= _snapshot_ttl_s()
        ):
            return _LAST_SNAPSHOT.to_dict()
    snap = aggregate_autonomy_metrics(repo_root=repo_root, now=now)
    with _SNAPSHOT_LOCK:
        _LAST_SNAPSHOT = snap
        _LAST_SNAPSHOT_TS = now
    return snap.to_dict()


def reset_cache_for_tests() -> None:
    global _LAST_SNAPSHOT, _LAST_SNAPSHOT_TS
    with _SNAPSHOT_LOCK:
        _LAST_SNAPSHOT = None
        _LAST_SNAPSHOT_TS = 0.0
