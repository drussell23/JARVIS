"""Slice 2.2 — TrajectoryAuditor: codebase trajectory tracker.

Per ``OUROBOROS_VENOM_PRD.md`` §24.10.2:

  > Per-trajectory Antivenom — baseline system growth (LOC, coverage,
  > complexity) and flag anomalies.

Maintains a rolling baseline of codebase health metrics. After each
commit, computes current metrics and compares against the baseline.
Flags sudden deviations as trajectory drift signals.

## Cage rules (load-bearing)

  * **Stdlib-only** — ``ast``, ``os``, ``pathlib``, ``json``,
    ``hashlib``. No ``radon``, no ``coverage.py``.
  * **NEVER raises into the caller.**
  * **Default-off** — ``JARVIS_TRAJECTORY_AUDITOR_ENABLED``.
"""
from __future__ import annotations

import ast
import hashlib
import json
import logging
import math
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

_TRUTHY = ("1", "true", "yes", "on")

# ---------------------------------------------------------------------------
# Hard caps
# ---------------------------------------------------------------------------
MAX_SNAPSHOTS: int = 100
MAX_SNAPSHOT_FILE_BYTES: int = 8 * 1024 * 1024
MAX_DRIFT_SIGNALS: int = 50
MAX_MODULE_DEPTH: int = 4

# ---------------------------------------------------------------------------
# Master flag + configuration
# ---------------------------------------------------------------------------

def is_trajectory_enabled() -> bool:
    return os.environ.get(
        "JARVIS_TRAJECTORY_AUDITOR_ENABLED", "",
    ).strip().lower() in _TRUTHY

def _snapshots_path() -> Path:
    raw = os.environ.get("JARVIS_TRAJECTORY_AUDITOR_PATH")
    if raw:
        return Path(raw)
    return Path(".jarvis") / "trajectory_snapshots.jsonl"

def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default

def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return default

def _loc_growth_warn_pct() -> float:
    return _env_float("JARVIS_TRAJECTORY_LOC_GROWTH_WARN_PCT", 50.0)

def _complexity_warn_pct() -> float:
    return _env_float("JARVIS_TRAJECTORY_COMPLEXITY_WARN_PCT", 30.0)

def _api_change_warn_pct() -> float:
    return _env_float("JARVIS_TRAJECTORY_API_CHANGE_WARN_PCT", 25.0)

def _baseline_window() -> int:
    return _env_int("JARVIS_TRAJECTORY_BASELINE_WINDOW", 10)

# Scan filter — only Python files, skip hidden dirs and __pycache__.
_SKIP_DIRS = frozenset({
    "__pycache__", ".git", ".tox", ".mypy_cache", ".pytest_cache",
    "node_modules", ".venv", "venv", ".eggs", "dist", "build",
})

# ---------------------------------------------------------------------------
# TrajectorySnapshot
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TrajectorySnapshot:
    ts_unix: float
    total_loc: int
    loc_by_module: Dict[str, int]
    test_file_count: int
    avg_function_complexity: float
    public_api_count: int
    governance_file_count: int
    snapshot_hash: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ts_unix": self.ts_unix,
            "total_loc": self.total_loc,
            "loc_by_module": self.loc_by_module,
            "test_file_count": self.test_file_count,
            "avg_function_complexity": round(self.avg_function_complexity, 4),
            "public_api_count": self.public_api_count,
            "governance_file_count": self.governance_file_count,
            "snapshot_hash": self.snapshot_hash,
        }

# ---------------------------------------------------------------------------
# DriftSignal
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DriftSignal:
    metric: str
    baseline_value: float
    current_value: float
    change_pct: float
    severity: str  # "info" | "warning" | "critical"
    detail: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "metric": self.metric,
            "baseline_value": round(self.baseline_value, 4),
            "current_value": round(self.current_value, 4),
            "change_pct": round(self.change_pct, 2),
            "severity": self.severity,
            "detail": self.detail,
        }

# ---------------------------------------------------------------------------
# TrajectoryReport
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TrajectoryReport:
    current: TrajectorySnapshot
    baseline: Optional[TrajectorySnapshot]
    drift_signals: Tuple[DriftSignal, ...]
    verdict: str  # "stable" | "growing" | "drifting" | "alarming"
    ts_unix: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "current": self.current.to_dict(),
            "baseline": self.baseline.to_dict() if self.baseline else None,
            "drift_signals": [s.to_dict() for s in self.drift_signals],
            "verdict": self.verdict,
            "ts_unix": self.ts_unix,
        }

# ---------------------------------------------------------------------------
# Code metrics (stdlib-only)
# ---------------------------------------------------------------------------

def count_loc(filepath: Path) -> int:
    """Count non-blank, non-comment lines in a Python file. NEVER raises."""
    try:
        text = filepath.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    count = 0
    in_docstring = False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith('"""') or stripped.startswith("'''"):
            q = stripped[:3]
            if stripped.count(q) >= 2 and len(stripped) > 3:
                continue  # single-line docstring
            in_docstring = not in_docstring
            continue
        if in_docstring:
            continue
        if stripped.startswith("#"):
            continue
        count += 1
    return count

def compute_complexity(filepath: Path) -> float:
    """Compute mean cyclomatic complexity of functions in a Python file.

    Uses ``ast`` to count branching nodes per function. Returns 0.0
    on parse failure. NEVER raises.
    """
    try:
        source = filepath.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=str(filepath))
    except (OSError, SyntaxError):
        return 0.0

    _BRANCH_TYPES = (
        ast.If, ast.For, ast.While, ast.ExceptHandler,
        ast.With, ast.Assert, ast.BoolOp,
    )
    # Python 3.10+ has ast.TryStar; use getattr for compatibility.
    try:
        _try_star = ast.TryStar  # type: ignore[attr-defined]
    except AttributeError:
        _try_star = None

    functions = [
        n for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    if not functions:
        return 0.0

    complexities: List[float] = []
    for func in functions:
        cc = 1  # base complexity
        for node in ast.walk(func):
            if isinstance(node, _BRANCH_TYPES):
                cc += 1
            if _try_star and isinstance(node, _try_star):
                cc += 1
        complexities.append(float(cc))

    return sum(complexities) / len(complexities) if complexities else 0.0

def count_public_api(filepath: Path) -> int:
    """Count entries in ``__all__`` if present. NEVER raises."""
    try:
        source = filepath.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=str(filepath))
    except (OSError, SyntaxError):
        return 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "__all__":
                    if isinstance(node.value, (ast.List, ast.Tuple)):
                        return len(node.value.elts)
    return 0

def _module_key(filepath: Path, root: Path) -> str:
    """Derive a module key from a filepath relative to root."""
    try:
        rel = filepath.relative_to(root)
    except ValueError:
        return str(filepath.name)
    parts = list(rel.parts[:MAX_MODULE_DEPTH])
    if parts and parts[-1].endswith(".py"):
        parts[-1] = parts[-1][:-3]
    return "/".join(parts)

# ---------------------------------------------------------------------------
# TrajectoryAuditor
# ---------------------------------------------------------------------------

class TrajectoryAuditor:
    """Codebase trajectory tracker with rolling-baseline drift detection."""

    def __init__(
        self,
        project_root: Path,
        snapshots_path: Optional[Path] = None,
        scan_dirs: Optional[Sequence[str]] = None,
    ) -> None:
        self._root = project_root.resolve()
        self._snapshots_path = snapshots_path or _snapshots_path()
        self._scan_dirs = list(scan_dirs or ["backend"])
        self._history: Optional[List[TrajectorySnapshot]] = None

    def _ensure_history(self) -> List[TrajectorySnapshot]:
        if self._history is not None:
            return self._history
        self._history = self._load_history()
        return self._history

    def _load_history(self) -> List[TrajectorySnapshot]:
        if not self._snapshots_path.exists():
            return []
        try:
            size = self._snapshots_path.stat().st_size
            if size > MAX_SNAPSHOT_FILE_BYTES:
                return []
            text = self._snapshots_path.read_text(encoding="utf-8")
        except OSError:
            return []
        snapshots: List[TrajectorySnapshot] = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            snap = _parse_snapshot(obj)
            if snap:
                snapshots.append(snap)
            if len(snapshots) >= MAX_SNAPSHOTS:
                break
        snapshots.sort(key=lambda s: s.ts_unix)
        return snapshots

    # ------------------------------------------------------------------
    # Snapshot
    # ------------------------------------------------------------------

    def snapshot(self, now_unix: Optional[float] = None) -> TrajectorySnapshot:
        """Compute current codebase metrics. NEVER raises."""
        now = now_unix or time.time()
        total_loc = 0
        loc_by_module: Dict[str, int] = {}
        test_count = 0
        complexities: List[float] = []
        api_count = 0
        gov_count = 0

        for scan_dir in self._scan_dirs:
            base = self._root / scan_dir
            if not base.is_dir():
                continue
            for dirpath, dirnames, filenames in os.walk(str(base)):
                dirnames[:] = [
                    d for d in dirnames if d not in _SKIP_DIRS
                ]
                dp = Path(dirpath)
                for fname in filenames:
                    if not fname.endswith(".py"):
                        continue
                    fpath = dp / fname
                    loc = count_loc(fpath)
                    total_loc += loc
                    mk = _module_key(fpath, self._root)
                    loc_by_module[mk] = loc_by_module.get(mk, 0) + loc
                    if fname.startswith("test_") or fname.endswith("_test.py"):
                        test_count += 1
                    cc = compute_complexity(fpath)
                    if cc > 0:
                        complexities.append(cc)
                    api_count += count_public_api(fpath)
                    if "governance" in str(fpath):
                        gov_count += 1

        avg_cc = (
            sum(complexities) / len(complexities)
            if complexities else 0.0
        )

        # Compute hash.
        try:
            from backend.core.ouroboros.governance.observability.determinism_substrate import (
                canonical_hash,
            )
            hash_input = {
                "total_loc": total_loc,
                "test_file_count": test_count,
                "avg_cc": round(avg_cc, 4),
                "api_count": api_count,
                "gov_count": gov_count,
            }
            snap_hash = canonical_hash(hash_input)
        except ImportError:
            snap_hash = hashlib.sha256(
                f"{total_loc}:{test_count}:{avg_cc}".encode()
            ).hexdigest()[:16]

        return TrajectorySnapshot(
            ts_unix=now,
            total_loc=total_loc,
            loc_by_module=loc_by_module,
            test_file_count=test_count,
            avg_function_complexity=round(avg_cc, 4),
            public_api_count=api_count,
            governance_file_count=gov_count,
            snapshot_hash=snap_hash,
        )

    # ------------------------------------------------------------------
    # Baseline (rolling average)
    # ------------------------------------------------------------------

    def baseline(self) -> Optional[TrajectorySnapshot]:
        """Compute the rolling-average baseline from recent history."""
        history = self._ensure_history()
        window = _baseline_window()
        recent = history[-window:] if history else []
        if not recent:
            return None

        n = len(recent)
        avg_loc = sum(s.total_loc for s in recent) // n
        avg_tests = sum(s.test_file_count for s in recent) // n
        avg_cc = sum(s.avg_function_complexity for s in recent) / n
        avg_api = sum(s.public_api_count for s in recent) // n
        avg_gov = sum(s.governance_file_count for s in recent) // n

        return TrajectorySnapshot(
            ts_unix=recent[-1].ts_unix,
            total_loc=avg_loc,
            loc_by_module={},
            test_file_count=avg_tests,
            avg_function_complexity=round(avg_cc, 4),
            public_api_count=avg_api,
            governance_file_count=avg_gov,
            snapshot_hash="baseline",
        )

    # ------------------------------------------------------------------
    # Audit (compare current vs baseline)
    # ------------------------------------------------------------------

    def audit(self, now_unix: Optional[float] = None) -> TrajectoryReport:
        """Compute current snapshot, compare to baseline, flag drift."""
        now = now_unix or time.time()
        current = self.snapshot(now_unix=now)
        bl = self.baseline()

        if bl is None:
            return TrajectoryReport(
                current=current,
                baseline=None,
                drift_signals=(),
                verdict="stable",
                ts_unix=now,
            )

        signals: List[DriftSignal] = []

        def _check(
            metric: str, baseline_val: float, current_val: float,
            warn_pct: float, critical_mult: float = 2.0,
            decrease_is_critical: bool = False,
        ) -> None:
            if baseline_val == 0:
                if current_val > 0:
                    signals.append(DriftSignal(
                        metric=metric, baseline_value=0,
                        current_value=current_val, change_pct=100.0,
                        severity="info",
                        detail=f"{metric} appeared (was 0, now {current_val})",
                    ))
                return
            pct = ((current_val - baseline_val) / abs(baseline_val)) * 100
            if decrease_is_critical and pct < -warn_pct:
                sev = "critical"
            elif abs(pct) > warn_pct * critical_mult:
                sev = "critical"
            elif abs(pct) > warn_pct:
                sev = "warning"
            else:
                return
            signals.append(DriftSignal(
                metric=metric,
                baseline_value=baseline_val,
                current_value=current_val,
                change_pct=round(pct, 2),
                severity=sev,
                detail=f"{metric}: {baseline_val:.0f} → {current_val:.0f} ({pct:+.1f}%)",
            ))

        _check("total_loc", float(bl.total_loc), float(current.total_loc),
               _loc_growth_warn_pct())
        _check("avg_complexity", bl.avg_function_complexity,
               current.avg_function_complexity, _complexity_warn_pct())
        _check("public_api_count", float(bl.public_api_count),
               float(current.public_api_count), _api_change_warn_pct())
        _check("test_file_count", float(bl.test_file_count),
               float(current.test_file_count), 20.0,
               decrease_is_critical=True)

        signals = signals[:MAX_DRIFT_SIGNALS]
        verdict = _classify_trajectory(signals)

        return TrajectoryReport(
            current=current,
            baseline=bl,
            drift_signals=tuple(signals),
            verdict=verdict,
            ts_unix=now,
        )

    # ------------------------------------------------------------------
    # Record + persist
    # ------------------------------------------------------------------

    def record_snapshot(
        self, snap: TrajectorySnapshot,
    ) -> Tuple[bool, str]:
        """Persist a snapshot to history. NEVER raises."""
        history = self._ensure_history()
        history.append(snap)
        if len(history) > MAX_SNAPSHOTS:
            history[:] = history[-MAX_SNAPSHOTS:]
        return self._persist_history(history)

    def _persist_history(
        self, history: List[TrajectorySnapshot],
    ) -> Tuple[bool, str]:
        try:
            self._snapshots_path.parent.mkdir(parents=True, exist_ok=True)
            with self._snapshots_path.open("w", encoding="utf-8") as f:
                for snap in history:
                    line = json.dumps(snap.to_dict(), separators=(",", ":"))
                    f.write(line + "\n")
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass
        except OSError as exc:
            return (False, f"persist_failed:{exc}")
        return (True, "ok")

    def reset(self) -> None:
        self._history = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _classify_trajectory(signals: Sequence[DriftSignal]) -> str:
    if not signals:
        return "stable"
    severities = [s.severity for s in signals]
    if severities.count("critical") >= 2:
        return "alarming"
    if "critical" in severities:
        return "drifting"
    if "warning" in severities:
        return "growing"
    return "stable"

def _parse_snapshot(obj: Dict[str, Any]) -> Optional[TrajectorySnapshot]:
    try:
        return TrajectorySnapshot(
            ts_unix=float(obj.get("ts_unix", 0)),
            total_loc=int(obj.get("total_loc", 0)),
            loc_by_module=obj.get("loc_by_module", {}),
            test_file_count=int(obj.get("test_file_count", 0)),
            avg_function_complexity=float(obj.get("avg_function_complexity", 0)),
            public_api_count=int(obj.get("public_api_count", 0)),
            governance_file_count=int(obj.get("governance_file_count", 0)),
            snapshot_hash=str(obj.get("snapshot_hash", "")),
        )
    except (TypeError, ValueError):
        return None

# ---------------------------------------------------------------------------
# Default singleton
# ---------------------------------------------------------------------------

_DEFAULT_AUDITOR: Optional[TrajectoryAuditor] = None

def get_default_trajectory_auditor(
    project_root: Optional[Path] = None,
) -> TrajectoryAuditor:
    global _DEFAULT_AUDITOR
    if _DEFAULT_AUDITOR is None:
        _DEFAULT_AUDITOR = TrajectoryAuditor(
            project_root=project_root or Path("."),
        )
    return _DEFAULT_AUDITOR

def reset_default_trajectory_auditor() -> None:
    global _DEFAULT_AUDITOR
    _DEFAULT_AUDITOR = None

__all__ = [
    "DriftSignal",
    "MAX_DRIFT_SIGNALS",
    "MAX_MODULE_DEPTH",
    "MAX_SNAPSHOT_FILE_BYTES",
    "MAX_SNAPSHOTS",
    "TrajectoryAuditor",
    "TrajectoryReport",
    "TrajectorySnapshot",
    "compute_complexity",
    "count_loc",
    "count_public_api",
    "get_default_trajectory_auditor",
    "is_trajectory_enabled",
    "reset_default_trajectory_auditor",
]
