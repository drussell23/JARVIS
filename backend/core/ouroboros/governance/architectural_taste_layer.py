"""
Architectural Taste Layer — Design-Quality Advisor
===================================================

Closes §41.4 Phase 1 third arc (PRD v3.0+). Closes the gap
between mechanical structural validation (SemanticGuardian's
10 AST patterns, M10's parser-shape check) and *design taste*
— consistency with established code patterns, cohesion of
changes, composition vs duplication, simplicity proportional
to value.

The substrate is **advisory** — it emits a 4-value
:class:`TasteVerdict` plus per-dimension scores; it does NOT
gate APPLY. Consumer-side wiring (raising risk_tier when
verdict is POOR, surfacing in operator panel) stays out of
scope.

Approach (deterministic + pluggable LLM enrichment):

1. **Taste profile construction** — read recent commits via
   ``git log`` (stdlib subprocess; bounded by env). For each
   commit's touched files, parse AST and extract:
   - Identifier naming patterns (snake_case ratio, prefix
     conventions, length distribution)
   - File cohesion (function count, class count, top-level
     statement count)
   - Composition (count of ImportFrom for sibling files vs
     fully-qualified external imports)
   - Simplicity (lines per function, AST node count)
2. **Per-file taste assessment** — given a proposed change
   (target_file + new content), parse its AST + compare each
   dimension against the profile. Score 0.0–1.0 per dimension.
3. **Verdict synthesis** — average per-dimension scores into
   one of 4 verdicts; classify a 4-value signal capturing
   *why*.
4. **Optional LLM enrichment** — caller may inject a
   ``llm_evaluator`` callable that takes the deterministic
   :class:`TasteAssessment` + the file content and returns a
   refined verdict + diagnostic. Substrate works without it;
   wires cleanly when operator provides one.

The substrate is **deterministic** — same git history + same
proposed change → same baseline verdict. LLM enrichment is
optional (deterministic baseline always provided first).

Composition contract:

* :func:`subprocess.run` (stdlib) — git log walker. Bounded
  by ``JARVIS_ARCHITECTURAL_TASTE_MAX_COMMITS`` (default 50).
  Substrate does NOT compose second_order_doll_metric's
  private ``_run_git_log`` because that's per-Category;
  taste layer needs whole-repo profile.
* :mod:`ast` (stdlib) — AST parsing for both profile + new
  content; rejects malformed input gracefully.
* :func:`governance_boundary_gate.is_boundary_crossed` (Wave
  2 #5) — cage-touch flag.
* :func:`cross_process_jsonl.flock_append_line` — §33.4
  audit at ``.jarvis/architectural_taste_ledger.jsonl``.

NEVER raises. Empty git history / unparseable AST / missing
target file all degrade to ``NO_SIGNAL`` / ``QUESTIONABLE``
verdict, not exception.

Closed 4-value :class:`TasteVerdict`:

  EXCELLENT      all 4 dimensions ≥ excellent_threshold
                 (default 0.75)
  GOOD           average score ≥ good_threshold (default 0.6)
  QUESTIONABLE   average in [0.4, good_threshold)
  POOR           average < poor_threshold (default 0.4)

Closed 4-value :class:`TasteSignal`:

  CONSISTENT     score matches profile within tolerance
  NOVEL          score deviates positively (above profile)
  DRIFTING       score deviates negatively (below profile)
  NO_SIGNAL      insufficient profile data (empty git log
                 OR < min_profile_commits)

Closed 4-value :class:`TasteDimension`:

  NAMING         identifier naming consistency
  COHESION       single-file vs scattered change
  COMPOSITION    imports-from-existing vs duplication
  SIMPLICITY     AST node count + function length

§33.1 cognitive substrate
``JARVIS_ARCHITECTURAL_TASTE_ENABLED`` default-**FALSE**.

Authority asymmetry (AST-pinned): stdlib only at module load.
``governance_boundary_gate`` + ``cross_process_jsonl`` are
lazy-imported. Does NOT import orchestrator / iron_gate /
policy / providers / candidate_generator / urgency_router /
change_engine / semantic_guardian / auto_committer /
risk_tier_floor / tool_executor / plan_generator (the
substrate is advisory — it does not gate any phase).
"""
from __future__ import annotations

import ast
import enum
import json
import logging
import os
import re
import statistics
import subprocess
import time
from dataclasses import dataclass, field
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
    Set,
    Tuple,
)

logger = logging.getLogger(__name__)


ARCHITECTURAL_TASTE_SCHEMA_VERSION: str = "architectural_taste.1"


_ENV_MASTER = "JARVIS_ARCHITECTURAL_TASTE_ENABLED"
_ENV_PERSIST = "JARVIS_ARCHITECTURAL_TASTE_PERSIST_ENABLED"
_ENV_MAX_COMMITS = "JARVIS_ARCHITECTURAL_TASTE_MAX_COMMITS"
_ENV_MIN_PROFILE_COMMITS = (
    "JARVIS_ARCHITECTURAL_TASTE_MIN_PROFILE_COMMITS"
)
_ENV_EXCELLENT_THRESHOLD = (
    "JARVIS_ARCHITECTURAL_TASTE_EXCELLENT_THRESHOLD"
)
_ENV_GOOD_THRESHOLD = (
    "JARVIS_ARCHITECTURAL_TASTE_GOOD_THRESHOLD"
)
_ENV_POOR_THRESHOLD = (
    "JARVIS_ARCHITECTURAL_TASTE_POOR_THRESHOLD"
)
_ENV_SIGNAL_TOLERANCE = (
    "JARVIS_ARCHITECTURAL_TASTE_SIGNAL_TOLERANCE"
)
_ENV_GIT_TIMEOUT_S = "JARVIS_ARCHITECTURAL_TASTE_GIT_TIMEOUT_S"
_ENV_MAX_FILES_PER_COMMIT = (
    "JARVIS_ARCHITECTURAL_TASTE_MAX_FILES_PER_COMMIT"
)
_ENV_LEDGER_PATH = "JARVIS_ARCHITECTURAL_TASTE_LEDGER_PATH"

_DEFAULT_MAX_COMMITS = 50
_DEFAULT_MIN_PROFILE_COMMITS = 3
_DEFAULT_EXCELLENT_THRESHOLD = 0.75
_DEFAULT_GOOD_THRESHOLD = 0.6
_DEFAULT_POOR_THRESHOLD = 0.4
_DEFAULT_SIGNAL_TOLERANCE = 0.15
_DEFAULT_GIT_TIMEOUT_S = 15
_DEFAULT_MAX_FILES_PER_COMMIT = 30

_DEFAULT_LEDGER_REL = ".jarvis/architectural_taste_ledger.jsonl"

_TRUTHY: FrozenSet[str] = frozenset({"1", "true", "yes", "on"})


def _flag(name: str, *, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in _TRUTHY


def master_enabled() -> bool:
    """§33.1 default-FALSE, upgraded by Slice 198 to three-state: an explicit
    ``JARVIS_ARCHITECTURAL_TASTE_ENABLED`` value wins (``=0`` is the supreme
    kill switch); when UNSET the gate ARMS itself once the organism has
    autonomously graduated AND a synthetic responsiveness assertion passes
    (``taste_layer_armed``). Fail-soft: arming module unavailable → legacy
    default-FALSE."""
    raw = os.environ.get(_ENV_MASTER, "").strip().lower()
    if raw == "":
        try:
            from backend.core.ouroboros.governance.m10_autonomous_graduation import (  # noqa: E501
                taste_layer_armed,
            )
            return bool(taste_layer_armed())
        except Exception:  # noqa: BLE001
            return False
    return raw in ("1", "true", "yes", "on")


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
        v = float(raw)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


def max_commits_to_scan() -> int:
    return _read_clamped_int(
        _ENV_MAX_COMMITS, _DEFAULT_MAX_COMMITS, 1, 1000,
    )


def min_profile_commits() -> int:
    return _read_clamped_int(
        _ENV_MIN_PROFILE_COMMITS,
        _DEFAULT_MIN_PROFILE_COMMITS, 1, 100,
    )


def excellent_threshold() -> float:
    return _read_clamped_float(
        _ENV_EXCELLENT_THRESHOLD,
        _DEFAULT_EXCELLENT_THRESHOLD, 0.0, 1.0,
    )


def good_threshold() -> float:
    raw = _read_clamped_float(
        _ENV_GOOD_THRESHOLD, _DEFAULT_GOOD_THRESHOLD,
        0.0, 1.0,
    )
    # Auto-clamp: good must be < excellent.
    return min(raw, excellent_threshold())


def poor_threshold() -> float:
    raw = _read_clamped_float(
        _ENV_POOR_THRESHOLD, _DEFAULT_POOR_THRESHOLD,
        0.0, 1.0,
    )
    return min(raw, good_threshold())


def signal_tolerance() -> float:
    return _read_clamped_float(
        _ENV_SIGNAL_TOLERANCE,
        _DEFAULT_SIGNAL_TOLERANCE, 0.0, 1.0,
    )


def git_timeout_s() -> int:
    return _read_clamped_int(
        _ENV_GIT_TIMEOUT_S, _DEFAULT_GIT_TIMEOUT_S, 1, 300,
    )


def max_files_per_commit() -> int:
    return _read_clamped_int(
        _ENV_MAX_FILES_PER_COMMIT,
        _DEFAULT_MAX_FILES_PER_COMMIT, 1, 1000,
    )


def ledger_path() -> Path:
    raw = os.environ.get(_ENV_LEDGER_PATH, "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path(_DEFAULT_LEDGER_REL)


# Closed taxonomies


class TasteVerdict(str, enum.Enum):
    """Closed 4-value verdict — bytes-pinned via AST."""

    EXCELLENT = "excellent"
    GOOD = "good"
    QUESTIONABLE = "questionable"
    POOR = "poor"


class TasteSignal(str, enum.Enum):
    """Closed 4-value signal — bytes-pinned via AST."""

    CONSISTENT = "consistent"
    NOVEL = "novel"
    DRIFTING = "drifting"
    NO_SIGNAL = "no_signal"


class TasteDimension(str, enum.Enum):
    """Closed 4-value design dimension — bytes-pinned via AST."""

    NAMING = "naming"
    COHESION = "cohesion"
    COMPOSITION = "composition"
    SIMPLICITY = "simplicity"


_VERDICT_GLYPH: Dict[str, str] = {
    TasteVerdict.EXCELLENT.value: "★",
    TasteVerdict.GOOD.value: "✓",
    TasteVerdict.QUESTIONABLE.value: "⚠",
    TasteVerdict.POOR.value: "✗",
}


_SIGNAL_GLYPH: Dict[str, str] = {
    TasteSignal.CONSISTENT.value: "=",
    TasteSignal.NOVEL.value: "↗",
    TasteSignal.DRIFTING.value: "↘",
    TasteSignal.NO_SIGNAL.value: "·",
}


_DIMENSION_GLYPH: Dict[str, str] = {
    TasteDimension.NAMING.value: "🔤",
    TasteDimension.COHESION.value: "⊙",
    TasteDimension.COMPOSITION.value: "⊕",
    TasteDimension.SIMPLICITY.value: "▽",
}


def verdict_glyph(verdict: object) -> str:
    """NEVER raises."""
    try:
        if hasattr(verdict, "value"):
            return _VERDICT_GLYPH.get(str(verdict.value), "?")
        return _VERDICT_GLYPH.get(
            str(verdict or "").strip().lower(), "?",
        )
    except Exception:  # noqa: BLE001
        return "?"


def signal_glyph(signal: object) -> str:
    """NEVER raises."""
    try:
        if hasattr(signal, "value"):
            return _SIGNAL_GLYPH.get(str(signal.value), "?")
        return _SIGNAL_GLYPH.get(
            str(signal or "").strip().lower(), "?",
        )
    except Exception:  # noqa: BLE001
        return "?"


def dimension_glyph(dim: object) -> str:
    """NEVER raises."""
    try:
        if hasattr(dim, "value"):
            return _DIMENSION_GLYPH.get(str(dim.value), "?")
        return _DIMENSION_GLYPH.get(
            str(dim or "").strip().lower(), "?",
        )
    except Exception:  # noqa: BLE001
        return "?"


# §33.5 frozen artifacts


@dataclass(frozen=True)
class TasteProfile:
    """Aggregate taste profile derived from recent commits."""

    commit_count: int
    file_count: int
    snake_case_ratio: float           # 0..1
    avg_function_length: float        # lines
    avg_imports_per_file: float
    avg_sibling_import_ratio: float   # imports from same dir
    avg_ast_nodes_per_file: float
    diagnostic: str
    schema_version: str = ARCHITECTURAL_TASTE_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "commit_count": int(self.commit_count),
            "file_count": int(self.file_count),
            "snake_case_ratio": float(self.snake_case_ratio),
            "avg_function_length": float(self.avg_function_length),
            "avg_imports_per_file": float(self.avg_imports_per_file),
            "avg_sibling_import_ratio": float(
                self.avg_sibling_import_ratio,
            ),
            "avg_ast_nodes_per_file": float(
                self.avg_ast_nodes_per_file,
            ),
            "diagnostic": self.diagnostic[:512],
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class DimensionScore:
    """One dimension's score (0..1) for one proposed change."""

    dimension: TasteDimension
    score: float
    raw_metric: float            # the underlying number
    profile_metric: float        # what the profile baseline was
    signal: TasteSignal
    diagnostic: str
    schema_version: str = ARCHITECTURAL_TASTE_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "dimension": self.dimension.value,
            "score": float(self.score),
            "raw_metric": float(self.raw_metric),
            "profile_metric": float(self.profile_metric),
            "signal": self.signal.value,
            "diagnostic": self.diagnostic[:256],
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class TasteAssessment:
    """Per-file design-quality assessment."""

    file_path: str
    verdict: TasteVerdict
    overall_signal: TasteSignal
    dimension_scores: Tuple[DimensionScore, ...]
    average_score: float
    boundary_crossed: bool
    llm_enriched: bool
    diagnostic: str
    schema_version: str = ARCHITECTURAL_TASTE_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "file_path": self.file_path[:256],
            "verdict": self.verdict.value,
            "overall_signal": self.overall_signal.value,
            "dimension_scores": [
                s.to_dict() for s in self.dimension_scores
            ],
            "average_score": float(self.average_score),
            "boundary_crossed": bool(self.boundary_crossed),
            "llm_enriched": bool(self.llm_enriched),
            "diagnostic": self.diagnostic[:512],
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class TasteReport:
    """Top-level taste report — frozen §33.5 artifact."""

    evaluated_at_unix: float
    master_enabled: bool
    overall_verdict: TasteVerdict
    profile: Optional[TasteProfile]
    assessments: Tuple[TasteAssessment, ...]
    diagnostic: str
    elapsed_s: float
    schema_version: str = ARCHITECTURAL_TASTE_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "evaluated_at_unix": self.evaluated_at_unix,
            "master_enabled": self.master_enabled,
            "overall_verdict": self.overall_verdict.value,
            "profile": (
                self.profile.to_dict() if self.profile else None
            ),
            "assessments": [
                a.to_dict() for a in self.assessments
            ],
            "diagnostic": self.diagnostic[:512],
            "elapsed_s": float(self.elapsed_s),
            "schema_version": self.schema_version,
        }


# Composers


def _is_boundary_crossed(file_path: str) -> bool:
    """Compose Wave 2 #5 boundary gate. NEVER raises."""
    if not file_path:
        return False
    try:
        from backend.core.ouroboros.governance.governance_boundary_gate import (  # noqa: E501
            is_boundary_crossed,
        )
        return bool(is_boundary_crossed((file_path,)))
    except Exception:  # noqa: BLE001
        return False


def _flock_append(payload: Mapping[str, Any]) -> bool:
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


# Git log walker


def _walk_git_log(
    *,
    repo_root: Optional[Path] = None,
) -> Tuple[Tuple[str, ...], ...]:
    """Walk recent commits via subprocess git log. Returns tuple
    of (commit_files_tuple,) per commit. NEVER raises."""
    try:
        root = repo_root or Path.cwd()
        cap = max_commits_to_scan()
        result = subprocess.run(
            [
                "git", "log",
                f"-n{cap}",
                "--name-only",
                "--pretty=format:%H",
            ],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=float(git_timeout_s()),
            check=False,
        )
        if result.returncode != 0:
            return ()
        lines = (result.stdout or "").splitlines()
        commits: List[Tuple[str, ...]] = []
        files: List[str] = []
        first_line = True
        files_cap = max_files_per_commit()
        for line in lines:
            stripped = line.strip()
            if not stripped:
                if files or first_line:
                    if files:
                        commits.append(tuple(files[:files_cap]))
                    files = []
                continue
            if first_line:
                first_line = False
                # SHA line — skip; next blank closes the commit
                continue
            # Heuristic: SHA is 40 hex chars
            if re.match(r"^[0-9a-f]{40}$", stripped):
                if files:
                    commits.append(tuple(files[:files_cap]))
                files = []
                first_line = True
                continue
            files.append(stripped)
        if files:
            commits.append(tuple(files[:files_cap]))
        return tuple(commits)
    except Exception:  # noqa: BLE001
        return ()


# AST analysis primitives


_SNAKE_CASE_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def _classify_identifier(name: str) -> bool:
    """True if identifier follows snake_case. NEVER raises."""
    try:
        return bool(_SNAKE_CASE_RE.match(str(name or "")))
    except Exception:  # noqa: BLE001
        return False


def _analyze_file_ast(
    *,
    file_path: str,
    repo_root: Optional[Path] = None,
    source_override: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Parse one file → metrics dict. NEVER raises. Returns
    None on read/parse failure."""
    if source_override is not None:
        src = source_override
    else:
        try:
            root = repo_root or Path.cwd()
            target = root / file_path
            if not target.exists() or not target.is_file():
                return None
            src = target.read_text(encoding="utf-8")
        except Exception:  # noqa: BLE001
            return None
    try:
        tree = ast.parse(src)
    except (SyntaxError, ValueError):
        return None
    metrics: Dict[str, Any] = {
        "snake_case_identifiers": 0,
        "total_identifiers": 0,
        "function_lengths": [],
        "import_count": 0,
        "sibling_import_count": 0,
        "ast_node_count": 0,
    }
    parent_dir = ""
    try:
        parent_dir = (
            str(Path(file_path).parent).strip("/")
        )
    except Exception:  # noqa: BLE001
        parent_dir = ""
    for node in ast.walk(tree):
        metrics["ast_node_count"] += 1
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            metrics["total_identifiers"] += 1
            if _classify_identifier(node.name):
                metrics["snake_case_identifiers"] += 1
            # Function length: last lineno - first lineno
            try:
                fn_lines = (
                    (node.end_lineno or node.lineno) - node.lineno
                    + 1
                )
                metrics["function_lengths"].append(int(fn_lines))
            except Exception:  # noqa: BLE001
                pass
        elif isinstance(node, ast.ClassDef):
            # Classes typically use PascalCase — don't count them
            # against snake_case ratio (would skew profile).
            pass
        elif isinstance(node, ast.ImportFrom):
            metrics["import_count"] += 1
            mod = (node.module or "")
            if parent_dir and parent_dir.replace("/", ".") in mod:
                metrics["sibling_import_count"] += 1
        elif isinstance(node, ast.Import):
            metrics["import_count"] += 1
    return metrics


# Profile construction


def build_taste_profile(
    *,
    repo_root: Optional[Path] = None,
    commits_override: Optional[Sequence[Sequence[str]]] = None,
) -> TasteProfile:
    """Build aggregate taste profile from recent commits.
    NEVER raises.

    When ``commits_override`` is provided (testing seam),
    skips git log walk."""
    commits = (
        tuple(tuple(c) for c in commits_override)
        if commits_override is not None
        else _walk_git_log(repo_root=repo_root)
    )
    snake_total = 0
    snake_match = 0
    fn_lengths: List[int] = []
    imports_per_file: List[int] = []
    sibling_ratios: List[float] = []
    ast_node_counts: List[int] = []
    files_seen: Set[str] = set()
    for commit_files in commits:
        for f in commit_files:
            if f in files_seen:
                continue
            if not f.endswith(".py"):
                continue
            metrics = _analyze_file_ast(
                file_path=f, repo_root=repo_root,
            )
            if metrics is None:
                continue
            # Only track files we actually analyzed — non-Python
            # and unparseable files are excluded from file_count
            # so the metric reflects substrate-visible coverage.
            files_seen.add(f)
            snake_match += int(
                metrics.get("snake_case_identifiers", 0),
            )
            snake_total += int(metrics.get("total_identifiers", 0))
            fn_lengths.extend(metrics.get("function_lengths") or [])
            ic = int(metrics.get("import_count", 0) or 0)
            sc = int(metrics.get("sibling_import_count", 0) or 0)
            imports_per_file.append(ic)
            sibling_ratios.append(
                (sc / ic) if ic > 0 else 0.0,
            )
            ast_node_counts.append(
                int(metrics.get("ast_node_count", 0) or 0),
            )

    snake_ratio = (
        (snake_match / snake_total) if snake_total > 0 else 0.0
    )
    avg_fn = (
        statistics.mean(fn_lengths) if fn_lengths else 0.0
    )
    avg_imp = (
        statistics.mean(imports_per_file)
        if imports_per_file else 0.0
    )
    avg_sib = (
        statistics.mean(sibling_ratios)
        if sibling_ratios else 0.0
    )
    avg_ast = (
        statistics.mean(ast_node_counts)
        if ast_node_counts else 0.0
    )

    return TasteProfile(
        commit_count=len(commits),
        file_count=len(files_seen),
        snake_case_ratio=snake_ratio,
        avg_function_length=avg_fn,
        avg_imports_per_file=avg_imp,
        avg_sibling_import_ratio=avg_sib,
        avg_ast_nodes_per_file=avg_ast,
        diagnostic=(
            f"profile: {len(commits)} commit(s), "
            f"{len(files_seen)} unique file(s), "
            f"snake={snake_ratio:.2f}, "
            f"avg_fn_len={avg_fn:.1f}, "
            f"avg_imp={avg_imp:.1f}"
        ),
    )


# Per-file assessment


def _score_dimension(
    dimension: TasteDimension,
    raw: float,
    profile_metric: float,
    *,
    tolerance: float,
) -> Tuple[float, TasteSignal, str]:
    """Pure scorer. Returns (score, signal, diagnostic).
    NEVER raises."""
    # Score is similarity to profile (1.0 = perfect match,
    # 0.0 = maximum distance). Different dimensions have
    # different scales — normalize to [0,1] heuristically.
    if dimension is TasteDimension.NAMING:
        # raw + profile both in [0,1] already (ratios).
        diff = abs(raw - profile_metric)
        score = max(0.0, 1.0 - diff)
    elif dimension is TasteDimension.COHESION:
        # raw is "files touched"; profile is avg. Single file
        # always perfect cohesion. Scale: score = 1 if raw <=
        # profile + 1, else fades.
        if raw <= max(1.0, profile_metric + 1.0):
            score = 1.0
        else:
            score = max(0.0, 1.0 - (raw - profile_metric) / 10.0)
    elif dimension is TasteDimension.COMPOSITION:
        # raw + profile both in [0,1] (sibling-import ratio).
        # Higher raw than profile = better composition.
        if raw >= profile_metric:
            score = 1.0
        else:
            score = max(0.0, 1.0 - (profile_metric - raw))
    else:  # SIMPLICITY
        # raw is "ast nodes" / "avg function length"; lower
        # is simpler. Score = 1 if raw <= profile, fades up.
        if profile_metric <= 0:
            score = 0.5
        elif raw <= profile_metric:
            score = 1.0
        else:
            ratio = raw / profile_metric
            score = max(0.0, 1.0 - (ratio - 1.0) * 0.5)
    # Determine signal
    diff = raw - profile_metric
    if profile_metric == 0 and raw == 0:
        signal = TasteSignal.NO_SIGNAL
    elif abs(diff) <= tolerance * max(1.0, profile_metric):
        signal = TasteSignal.CONSISTENT
    elif dimension in (
        TasteDimension.NAMING,
        TasteDimension.COMPOSITION,
    ):
        signal = TasteSignal.NOVEL if diff > 0 else TasteSignal.DRIFTING
    else:
        # cohesion/simplicity: lower is better, so positive
        # diff is DRIFTING
        signal = TasteSignal.DRIFTING if diff > 0 else TasteSignal.NOVEL
    diagnostic = (
        f"{dimension.value}: raw={raw:.2f} "
        f"profile={profile_metric:.2f} → score={score:.2f}"
    )
    return score, signal, diagnostic


def assess_file(
    file_path: str,
    *,
    source_override: Optional[str] = None,
    repo_root: Optional[Path] = None,
    profile: Optional[TasteProfile] = None,
    siblings_count: int = 1,
    llm_evaluator: Optional[Callable[
        [TasteAssessment, str], Optional[TasteAssessment],
    ]] = None,
) -> Optional[TasteAssessment]:
    """Per-file taste assessment. NEVER raises. Returns None
    when file is unreadable / not Python.

    ``siblings_count`` is the count of OTHER files in the
    same proposed change (used for COHESION scoring).

    ``llm_evaluator`` is the optional operator-injectable
    enricher. When provided, called with the deterministic
    assessment + source content; if it returns a non-None
    TasteAssessment, that's used in place of the baseline.
    All exceptions in the enricher are caught (baseline wins).
    """
    if not file_path:
        return None
    metrics = _analyze_file_ast(
        file_path=file_path,
        repo_root=repo_root,
        source_override=source_override,
    )
    if metrics is None:
        return None
    boundary = _is_boundary_crossed(file_path)
    prof = profile if profile is not None else build_taste_profile(
        repo_root=repo_root,
    )
    tol = signal_tolerance()

    # Compute raw metrics for this file.
    snake_match = int(metrics.get("snake_case_identifiers", 0))
    snake_total = int(metrics.get("total_identifiers", 0))
    raw_naming = (
        (snake_match / snake_total) if snake_total > 0 else 0.0
    )
    raw_cohesion = float(siblings_count)
    ic = int(metrics.get("import_count", 0) or 0)
    sc = int(metrics.get("sibling_import_count", 0) or 0)
    raw_composition = (sc / ic) if ic > 0 else 0.0
    raw_simplicity = float(
        statistics.mean(metrics.get("function_lengths") or [0])
        if metrics.get("function_lengths") else 0
    )

    dim_scores: List[DimensionScore] = []
    for dim, raw, prof_metric in (
        (TasteDimension.NAMING, raw_naming, prof.snake_case_ratio),
        (TasteDimension.COHESION, raw_cohesion,
         max(1.0, prof.avg_imports_per_file / 5.0)),
        (TasteDimension.COMPOSITION, raw_composition,
         prof.avg_sibling_import_ratio),
        (TasteDimension.SIMPLICITY, raw_simplicity,
         prof.avg_function_length),
    ):
        score, signal, diag = _score_dimension(
            dim, raw, prof_metric, tolerance=tol,
        )
        dim_scores.append(DimensionScore(
            dimension=dim,
            score=score,
            raw_metric=raw,
            profile_metric=prof_metric,
            signal=signal,
            diagnostic=diag,
        ))

    avg_score = (
        statistics.mean(s.score for s in dim_scores)
        if dim_scores else 0.0
    )
    if prof.commit_count < min_profile_commits():
        verdict = TasteVerdict.QUESTIONABLE
        overall_signal = TasteSignal.NO_SIGNAL
        diagnostic_extra = (
            f" (profile under-sampled: "
            f"{prof.commit_count} < {min_profile_commits()})"
        )
    else:
        verdict = _verdict_for_average(avg_score)
        overall_signal = _overall_signal_for_dimensions(dim_scores)
        diagnostic_extra = ""

    assessment = TasteAssessment(
        file_path=file_path,
        verdict=verdict,
        overall_signal=overall_signal,
        dimension_scores=tuple(dim_scores),
        average_score=avg_score,
        boundary_crossed=boundary,
        llm_enriched=False,
        diagnostic=(
            f"avg_score={avg_score:.2f} verdict={verdict.value} "
            f"signal={overall_signal.value}"
            + diagnostic_extra
        ),
    )

    if llm_evaluator is None:
        return assessment
    try:
        src_for_llm = source_override
        if src_for_llm is None:
            try:
                target = (repo_root or Path.cwd()) / file_path
                src_for_llm = target.read_text(encoding="utf-8")
            except Exception:  # noqa: BLE001
                src_for_llm = ""
        enriched = llm_evaluator(assessment, src_for_llm or "")
        if enriched is None:
            return assessment
        # Mark the enriched assessment so consumers know.
        return TasteAssessment(
            file_path=enriched.file_path,
            verdict=enriched.verdict,
            overall_signal=enriched.overall_signal,
            dimension_scores=enriched.dimension_scores,
            average_score=enriched.average_score,
            boundary_crossed=enriched.boundary_crossed,
            llm_enriched=True,
            diagnostic=enriched.diagnostic,
        )
    except Exception:  # noqa: BLE001
        return assessment


def _verdict_for_average(avg: float) -> TasteVerdict:
    exc_t = excellent_threshold()
    good_t = good_threshold()
    poor_t = poor_threshold()
    if avg >= exc_t:
        return TasteVerdict.EXCELLENT
    if avg >= good_t:
        return TasteVerdict.GOOD
    if avg >= poor_t:
        return TasteVerdict.QUESTIONABLE
    return TasteVerdict.POOR


def _overall_signal_for_dimensions(
    scores: Sequence[DimensionScore],
) -> TasteSignal:
    if not scores:
        return TasteSignal.NO_SIGNAL
    counts: Dict[str, int] = {}
    for s in scores:
        counts[s.signal.value] = counts.get(s.signal.value, 0) + 1
    # Plurality of signals wins; ties prefer NO_SIGNAL.
    if not counts:
        return TasteSignal.NO_SIGNAL
    max_count = max(counts.values())
    candidates = [k for k, v in counts.items() if v == max_count]
    if len(candidates) > 1:
        return TasteSignal.NO_SIGNAL
    for s in TasteSignal:
        if s.value == candidates[0]:
            return s
    return TasteSignal.NO_SIGNAL


# Top-level


def evaluate_change(
    target_files: Sequence[str],
    *,
    sources_override: Optional[Mapping[str, str]] = None,
    repo_root: Optional[Path] = None,
    profile_override: Optional[TasteProfile] = None,
    commits_override: Optional[Sequence[Sequence[str]]] = None,
    llm_evaluator: Optional[Callable[
        [TasteAssessment, str], Optional[TasteAssessment],
    ]] = None,
    now_unix: Optional[float] = None,
) -> TasteReport:
    """Top-level: build profile + assess each target file.
    NEVER raises.

    ``target_files`` — the proposed-change file paths.
    ``sources_override`` — operator-supplied
    ``{file_path: source_text}`` for testing or for proposed
    content that isn't yet on disk.
    ``commits_override`` — testing seam for git log.
    ``llm_evaluator`` — optional per-file enricher (see
    :func:`assess_file`)."""
    started = time.time() if now_unix is None else float(now_unix)
    if not master_enabled():
        return TasteReport(
            evaluated_at_unix=started,
            master_enabled=False,
            overall_verdict=TasteVerdict.QUESTIONABLE,
            profile=None,
            assessments=(),
            diagnostic=(
                f"gate disabled via {_ENV_MASTER}=false"
            ),
            elapsed_s=0.0,
        )
    files = tuple(
        str(f).strip() for f in target_files
        if str(f or "").strip()
    )
    if not files:
        return TasteReport(
            evaluated_at_unix=started,
            master_enabled=True,
            overall_verdict=TasteVerdict.QUESTIONABLE,
            profile=None,
            assessments=(),
            diagnostic="no target_files supplied",
            elapsed_s=max(0.0, time.time() - started),
        )

    profile = (
        profile_override
        if profile_override is not None
        else build_taste_profile(
            repo_root=repo_root,
            commits_override=commits_override,
        )
    )

    sources = sources_override or {}
    assessments: List[TasteAssessment] = []
    siblings_count = len(files)
    for f in files:
        source_text = sources.get(f)
        a = assess_file(
            f,
            source_override=source_text,
            repo_root=repo_root,
            profile=profile,
            siblings_count=siblings_count,
            llm_evaluator=llm_evaluator,
        )
        if a is not None:
            assessments.append(a)

    if not assessments:
        verdict = TasteVerdict.QUESTIONABLE
        diagnostic = "no Python files analyzed (none parseable)"
    elif profile.commit_count < min_profile_commits():
        # Under-sampled profile — overall verdict ALWAYS
        # QUESTIONABLE regardless of per-file scores. The
        # baseline is insufficient evidence to conclude
        # design-quality consistency.
        verdict = TasteVerdict.QUESTIONABLE
        diagnostic = (
            f"profile under-sampled "
            f"({profile.commit_count} < "
            f"{min_profile_commits()} commits); "
            f"{len(assessments)} file(s) assessed but "
            "verdict gated to QUESTIONABLE"
        )
    else:
        avg = statistics.mean(a.average_score for a in assessments)
        verdict = _verdict_for_average(avg)
        diagnostic = (
            f"{len(assessments)} file(s) assessed; "
            f"avg score {avg:.2f} → {verdict.value}"
        )

    report = TasteReport(
        evaluated_at_unix=started,
        master_enabled=True,
        overall_verdict=verdict,
        profile=profile,
        assessments=tuple(assessments),
        diagnostic=diagnostic,
        elapsed_s=max(0.0, time.time() - started),
    )
    _persist_report(report)
    _publish_event(report)
    return report


def _persist_report(report: TasteReport) -> None:
    """§33.4 audit. NEVER raises. Skips QUESTIONABLE-when-empty
    (no signal to record)."""
    if not report.assessments:
        return
    _flock_append({
        "kind": "taste_report", "payload": report.to_dict(),
    })


def _publish_event(report: TasteReport) -> None:
    """Best-effort SSE. NEVER raises."""
    if not master_enabled():
        return
    if not report.assessments:
        return
    try:
        from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
            EVENT_TYPE_ARCHITECTURAL_TASTE_EVALUATED,
            publish_task_event,
        )
        publish_task_event(
            EVENT_TYPE_ARCHITECTURAL_TASTE_EVALUATED,
            (
                f"system::architectural_taste::"
                f"{report.schema_version}"
            ),
            {
                "overall_verdict": report.overall_verdict.value,
                "assessment_count": len(report.assessments),
                "llm_enriched": any(
                    a.llm_enriched for a in report.assessments
                ),
                "elapsed_s": report.elapsed_s,
                "schema_version": report.schema_version,
            },
        )
    except Exception:  # noqa: BLE001
        return


def format_taste_panel(
    report: Optional[TasteReport] = None,
) -> str:
    """NEVER raises."""
    if report is None:
        if not master_enabled():
            return (
                f"architectural taste: disabled "
                f"({_ENV_MASTER}=false)"
            )
        return "architectural taste: no report"
    if not report.master_enabled:
        return (
            f"architectural taste: disabled "
            f"({_ENV_MASTER}=false)"
        )
    vg = verdict_glyph(report.overall_verdict)
    lines = [
        f"🎨 Architectural Taste  {vg} "
        f"{report.overall_verdict.value}",
    ]
    if report.profile is not None:
        p = report.profile
        lines.extend([
            f"  profile commits  : {p.commit_count}",
            f"  profile files    : {p.file_count}",
            f"  snake_case_ratio : {p.snake_case_ratio:.2f}",
            f"  avg_function_len : {p.avg_function_length:.1f}",
        ])
    if report.assessments:
        lines.append("  assessments:")
        for a in report.assessments[:5]:
            ag = verdict_glyph(a.verdict)
            sg = signal_glyph(a.overall_signal)
            llm_tag = " 🧠" if a.llm_enriched else ""
            lines.append(
                f"    {ag} {sg} {a.file_path[:40]:<40} "
                f"avg={a.average_score:.2f}{llm_tag}"
            )
        if len(report.assessments) > 5:
            lines.append(
                f"    ... (+{len(report.assessments) - 5} more)"
            )
    lines.append(f"  diagnostic       : {report.diagnostic}")
    return "\n".join(lines)


# AST pins


def register_shipped_invariants() -> list:
    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    target = (
        "backend/core/ouroboros/governance/"
        "architectural_taste_layer.py"
    )

    _EXPECTED_VERDICTS = {
        "excellent", "good", "questionable", "poor",
    }
    _EXPECTED_SIGNALS = {
        "consistent", "novel", "drifting", "no_signal",
    }
    _EXPECTED_DIMENSIONS = {
        "naming", "cohesion", "composition", "simplicity",
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
        tree: ast.AST, source: str,
    ) -> tuple:
        # §33.1 + Slice 198 — the gate must never DEFAULT-ON. Under the
        # Sovereign Ignition Protocol master_enabled() is three-state: an
        # explicit env value wins, and the UNSET path delegates to the
        # graduation-gated arming predicate (taste_layer_armed) — never an
        # unconditional True. The default-FALSE guarantee is preserved: with
        # the env unset AND the organism not autonomously graduated, the gate
        # is off. This invariant pins that the unset path routes through the
        # graduation gate rather than defaulting on.
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.FunctionDef)
                and node.name == "master_enabled"
            ):
                body_src = ast.get_source_segment(source, node) or ""
                if "taste_layer_armed" not in body_src:
                    return (
                        "master_enabled() unset path must consult "
                        "taste_layer_armed (graduation-gated) — no "
                        "unconditional default-on per §33.1 + Slice 198",
                    )
                return ()
        return ("master_enabled() not found",)

    def _validate_composes_canonical(
        tree: ast.AST, source: str,
    ) -> tuple:
        violations: List[str] = []
        if "governance_boundary_gate" not in source:
            violations.append(
                "must compose Wave 2 #5 "
                "governance_boundary_gate (cage detection)",
            )
        if "cross_process_jsonl" not in source:
            violations.append(
                "must compose cross_process_jsonl "
                "(§33.4 ledger)",
            )
        if "import ast" not in source:
            violations.append(
                "must compose stdlib ast module "
                "(structural code analysis)",
            )
        if "subprocess" not in source:
            violations.append(
                "must compose stdlib subprocess "
                "(git log walker)",
            )
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name=(
                "architectural_taste_verdict_taxonomy_closed"
            ),
            target_file=target,
            description=(
                "TasteVerdict 4-value taxonomy bytes-pinned."
            ),
            validate=_validate_taxonomy(
                "TasteVerdict", _EXPECTED_VERDICTS,
            ),
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "architectural_taste_signal_taxonomy_closed"
            ),
            target_file=target,
            description=(
                "TasteSignal 4-value taxonomy bytes-pinned."
            ),
            validate=_validate_taxonomy(
                "TasteSignal", _EXPECTED_SIGNALS,
            ),
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "architectural_taste_dimension_taxonomy_closed"
            ),
            target_file=target,
            description=(
                "TasteDimension 4-value taxonomy bytes-pinned."
            ),
            validate=_validate_taxonomy(
                "TasteDimension", _EXPECTED_DIMENSIONS,
            ),
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "architectural_taste_authority_asymmetry"
            ),
            target_file=target,
            description=(
                "Substrate purity — advisory only. MUST NOT "
                "import orchestrator / iron_gate / policy / "
                "etc / plan_generator. Substrate does not gate "
                "any phase; consumer-side wiring is operator-"
                "paced."
            ),
            validate=_validate_authority_asymmetry,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "architectural_taste_master_default_false"
            ),
            target_file=target,
            description="§33.1 default-FALSE.",
            validate=_validate_master_default_false,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "architectural_taste_composes_canonical"
            ),
            target_file=target,
            description=(
                "Substrate composes Wave 2 #5 "
                "governance_boundary_gate + cross_process_jsonl "
                "+ stdlib ast + stdlib subprocess for git log."
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
        "architectural_taste_layer.py"
    )

    seeds = [
        FlagSpec(
            name=_ENV_MASTER,
            type=FlagType.BOOL,
            default=False,
            description=(
                "Architectural Taste Layer master. §33.1 "
                "default-FALSE. Closes §41.4 Phase 1 third "
                "arc (PRD v3.0+). Advisory design-quality "
                "verdict from git-log-derived profile + AST "
                "analysis of proposed changes + optional LLM "
                "enricher. Substrate does NOT gate APPLY; "
                "consumer-side wiring is operator-paced."
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
            name=_ENV_MAX_COMMITS,
            type=FlagType.INT,
            default=_DEFAULT_MAX_COMMITS,
            description=(
                "Cap on git log commits scanned for profile. "
                "Default 50."
            ),
            category=Category.CAPACITY,
            source_file=src,
            example=f"{_ENV_MAX_COMMITS}=100",
        ),
        FlagSpec(
            name=_ENV_MIN_PROFILE_COMMITS,
            type=FlagType.INT,
            default=_DEFAULT_MIN_PROFILE_COMMITS,
            description=(
                "Min commits needed before profile is actionable. "
                "Below threshold → QUESTIONABLE verdict. Default 3."
            ),
            category=Category.TUNING,
            source_file=src,
            example=f"{_ENV_MIN_PROFILE_COMMITS}=10",
        ),
        FlagSpec(
            name=_ENV_EXCELLENT_THRESHOLD,
            type=FlagType.FLOAT,
            default=_DEFAULT_EXCELLENT_THRESHOLD,
            description=(
                "Avg score threshold for EXCELLENT verdict. "
                "Default 0.75."
            ),
            category=Category.TUNING,
            source_file=src,
            example=f"{_ENV_EXCELLENT_THRESHOLD}=0.80",
        ),
        FlagSpec(
            name=_ENV_GOOD_THRESHOLD,
            type=FlagType.FLOAT,
            default=_DEFAULT_GOOD_THRESHOLD,
            description=(
                "Avg score threshold for GOOD verdict. "
                "Default 0.6. Auto-clamped < excellent."
            ),
            category=Category.TUNING,
            source_file=src,
            example=f"{_ENV_GOOD_THRESHOLD}=0.65",
        ),
        FlagSpec(
            name=_ENV_POOR_THRESHOLD,
            type=FlagType.FLOAT,
            default=_DEFAULT_POOR_THRESHOLD,
            description=(
                "Avg score threshold for POOR verdict "
                "(below = POOR). Default 0.4. Auto-clamped < "
                "good."
            ),
            category=Category.TUNING,
            source_file=src,
            example=f"{_ENV_POOR_THRESHOLD}=0.35",
        ),
        FlagSpec(
            name=_ENV_SIGNAL_TOLERANCE,
            type=FlagType.FLOAT,
            default=_DEFAULT_SIGNAL_TOLERANCE,
            description=(
                "Tolerance for CONSISTENT signal "
                "classification (|raw - profile| ≤ tolerance "
                "→ CONSISTENT). Default 0.15."
            ),
            category=Category.TUNING,
            source_file=src,
            example=f"{_ENV_SIGNAL_TOLERANCE}=0.20",
        ),
        FlagSpec(
            name=_ENV_GIT_TIMEOUT_S,
            type=FlagType.INT,
            default=_DEFAULT_GIT_TIMEOUT_S,
            description=(
                "Timeout for git subprocess (s). Default 15."
            ),
            category=Category.TIMING,
            source_file=src,
            example=f"{_ENV_GIT_TIMEOUT_S}=30",
        ),
        FlagSpec(
            name=_ENV_MAX_FILES_PER_COMMIT,
            type=FlagType.INT,
            default=_DEFAULT_MAX_FILES_PER_COMMIT,
            description=(
                "Cap on files inspected per commit. Default 30."
            ),
            category=Category.CAPACITY,
            source_file=src,
            example=f"{_ENV_MAX_FILES_PER_COMMIT}=100",
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
    "ARCHITECTURAL_TASTE_SCHEMA_VERSION",
    "TasteVerdict",
    "TasteSignal",
    "TasteDimension",
    "TasteProfile",
    "DimensionScore",
    "TasteAssessment",
    "TasteReport",
    "master_enabled",
    "persistence_enabled",
    "max_commits_to_scan",
    "min_profile_commits",
    "excellent_threshold",
    "good_threshold",
    "poor_threshold",
    "signal_tolerance",
    "git_timeout_s",
    "max_files_per_commit",
    "ledger_path",
    "verdict_glyph",
    "signal_glyph",
    "dimension_glyph",
    "build_taste_profile",
    "assess_file",
    "evaluate_change",
    "format_taste_panel",
    "register_shipped_invariants",
    "register_flags",
]
