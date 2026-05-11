"""
Compositional Curiosity
========================

Closes §40 Wave 1 #16 — the last non-experimental Wave 1 arc.
Per the operator binding:

  "Compositional curiosity: identify under-explored capability
   gaps in the substrate composition graph. Surface pairs of
   mature substrates from different domains that have never
   been jointly developed → curious compositions worth trying."

This substrate is a **pure-function compositional novelty
detector**. It reads:

* :func:`flag_registry.list_all` — full substrate inventory
  (each FlagSpec carries ``source_file`` + ``category``).
* :func:`second_order_doll_metric.aggregate_doll_completion`
  (Wave 1 #15) — per-Category stage (maturity proxy).
* ``ast.parse`` over each substrate source file — extracts the
  import graph to detect cross-category co-composition.

The substrate then walks the 8×8 Category pair matrix, computes
a deterministic ``novelty_score`` per pair, and emits the top N
:class:`CompositionPair` candidates as
:class:`CompositionalCuriosityReport`. **Operator-paced** — the
substrate only proposes; consumer-side curiosity intent
generation stays out of scope.

Novelty score formula (deterministic, operator-tunable weights):

* ``maturity_a × maturity_b`` — both substrates must be
  GRADUATED-ish for the pair to be actionable; immature
  substrates score 0.
* ``/ (1 + co_occurrence)`` — if Category A already imports
  Category B (or vice-versa), the pair is no longer NOVEL.
* Clamped to [0.0, 1.0].

Composition contract — pure-function over canonical surfaces:

* :func:`flag_registry.get_default_registry` (read-only via
  caller-injectable provider — default lazy import).
* :func:`second_order_doll_metric.aggregate_doll_completion`
  (read-only via caller-injectable provider — default lazy
  import).
* :func:`governance_boundary_gate.is_boundary_crossed` (Wave 2
  #5) — flagged when either substrate file in the pair is
  cage-touching.
* :func:`cross_process_jsonl.flock_append_line` — optional
  §33.4 audit at ``.jarvis/compositional_curiosity_ledger.jsonl``.

NEVER raises. Empty FlagRegistry / missing doll snapshot /
unparseable source file all degrade to NO_CANDIDATES or
DISABLED verdict, not exception.

Closed 4-value :class:`CuriosityVerdict`:

  NO_CANDIDATES   ✓ no pair above novelty threshold
  EMERGING        ⚠ ≥1 pair at NOVEL level
  ACTIONABLE      🔭 ≥1 pair at FRONTIER level
  DISABLED        ◌ master flag off

Closed 4-value :class:`NoveltyLevel`:

  STALE      pair co-occurrence ≥ 2 (both directions imported
             — already composed)
  MUNDANE    co-occurrence == 1 OR low maturity product
  NOVEL      novelty_score ≥ novel_threshold (default 0.20)
  FRONTIER   novelty_score ≥ frontier_threshold (default 0.50)

§33.1 cognitive substrate
``JARVIS_COMPOSITIONAL_CURIOSITY_ENABLED`` default-**FALSE**.
Sub-flag ``JARVIS_COMPOSITIONAL_CURIOSITY_PERSIST_ENABLED``
gates §33.4 writes (default TRUE).

Authority asymmetry (AST-pinned): imports stdlib only at
module-load. ``flag_registry`` / ``second_order_doll_metric`` /
``governance_boundary_gate`` / ``cross_process_jsonl`` are all
lazy-imported behind composer helpers. Does NOT import
orchestrator / iron_gate / policy / providers /
candidate_generator / urgency_router / change_engine /
semantic_guardian / auto_committer / risk_tier_floor /
curiosity_scheduler (curiosity_scheduler consumes this substrate,
not the other way around).
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


COMPOSITIONAL_CURIOSITY_SCHEMA_VERSION: str = "compositional_curiosity.1"


# ===========================================================================
# Env knobs
# ===========================================================================


_ENV_MASTER = "JARVIS_COMPOSITIONAL_CURIOSITY_ENABLED"
_ENV_PERSIST = "JARVIS_COMPOSITIONAL_CURIOSITY_PERSIST_ENABLED"
_ENV_NOVEL_THRESHOLD = "JARVIS_COMPOSITIONAL_CURIOSITY_NOVEL_THRESHOLD"
_ENV_FRONTIER_THRESHOLD = (
    "JARVIS_COMPOSITIONAL_CURIOSITY_FRONTIER_THRESHOLD"
)
_ENV_MAX_PAIRS = "JARVIS_COMPOSITIONAL_CURIOSITY_MAX_PAIRS"
_ENV_MAX_FILES_PER_CATEGORY = (
    "JARVIS_COMPOSITIONAL_CURIOSITY_MAX_FILES_PER_CATEGORY"
)
_ENV_LEDGER_PATH = "JARVIS_COMPOSITIONAL_CURIOSITY_LEDGER_PATH"

_DEFAULT_NOVEL_THRESHOLD = 0.20
_DEFAULT_FRONTIER_THRESHOLD = 0.50
_DEFAULT_MAX_PAIRS = 10
_DEFAULT_MAX_FILES_PER_CATEGORY = 15

_DEFAULT_LEDGER_REL = ".jarvis/compositional_curiosity_ledger.jsonl"

_TRUTHY: FrozenSet[str] = frozenset({"1", "true", "yes", "on"})


def _flag(name: str, *, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in _TRUTHY


def master_enabled() -> bool:
    """§33.1 cognitive substrate — default-FALSE.

    Operator-paced opt-in. Returns NO_CANDIDATES /
    DISABLED-equivalent stubs when off.
    """
    return _flag(_ENV_MASTER, default=False)


def persistence_enabled() -> bool:
    """Sub-flag — gate §33.4 JSONL writes. Default TRUE."""
    return _flag(_ENV_PERSIST, default=True)


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


def novel_threshold() -> float:
    """Score threshold to label a pair NOVEL. Default 0.20.
    Clamped to [0.0, 1.0]."""
    return _read_clamped_float(
        _ENV_NOVEL_THRESHOLD, _DEFAULT_NOVEL_THRESHOLD,
        0.0, 1.0,
    )


def frontier_threshold() -> float:
    """Score threshold to label a pair FRONTIER. Default 0.50.
    Auto-clamped ≥ novel_threshold."""
    raw = _read_clamped_float(
        _ENV_FRONTIER_THRESHOLD, _DEFAULT_FRONTIER_THRESHOLD,
        0.0, 1.0,
    )
    return max(raw, novel_threshold())


def max_pairs() -> int:
    """Cap on returned candidate count. Defaults to 10.
    Clamped to [1, 1000]."""
    return _read_clamped_int(
        _ENV_MAX_PAIRS, _DEFAULT_MAX_PAIRS, 1, 1000,
    )


def max_files_per_category() -> int:
    """Cap on substrate files parsed per Category. Bounds the
    O(C × F × ast.parse) cost. Defaults to 15. Clamped to
    [1, 500]."""
    return _read_clamped_int(
        _ENV_MAX_FILES_PER_CATEGORY,
        _DEFAULT_MAX_FILES_PER_CATEGORY,
        1, 500,
    )


def ledger_path() -> Path:
    """Audit-ledger path. Defaults to
    ``.jarvis/compositional_curiosity_ledger.jsonl``."""
    raw = os.environ.get(_ENV_LEDGER_PATH, "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path(_DEFAULT_LEDGER_REL)


# ===========================================================================
# Closed taxonomies
# ===========================================================================


class CuriosityVerdict(str, enum.Enum):
    """Closed 4-value verdict — bytes-pinned via AST."""

    NO_CANDIDATES = "no_candidates"
    EMERGING = "emerging"
    ACTIONABLE = "actionable"
    DISABLED = "disabled"


class NoveltyLevel(str, enum.Enum):
    """Closed 4-value novelty taxonomy — bytes-pinned via AST."""

    STALE = "stale"
    MUNDANE = "mundane"
    NOVEL = "novel"
    FRONTIER = "frontier"


_VERDICT_GLYPH: Dict[str, str] = {
    CuriosityVerdict.NO_CANDIDATES.value: "✓",
    CuriosityVerdict.EMERGING.value: "⚠",
    CuriosityVerdict.ACTIONABLE.value: "🔭",
    CuriosityVerdict.DISABLED.value: "◌",
}


_NOVELTY_GLYPH: Dict[str, str] = {
    NoveltyLevel.STALE.value: "·",
    NoveltyLevel.MUNDANE.value: "○",
    NoveltyLevel.NOVEL.value: "◇",
    NoveltyLevel.FRONTIER.value: "🔭",
}


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


def novelty_glyph(level: object) -> str:
    """Public glyph accessor. NEVER raises."""
    try:
        if hasattr(level, "value"):
            return _NOVELTY_GLYPH.get(str(level.value), "?")
        return _NOVELTY_GLYPH.get(
            str(level or "").strip().lower(), "?",
        )
    except Exception:  # noqa: BLE001
        return "?"


# ===========================================================================
# §33.5 frozen versioned artifacts
# ===========================================================================


@dataclass(frozen=True)
class CompositionPair:
    """One (Category A × Category B) pair — frozen audit record."""

    category_a: str
    category_b: str
    maturity_a: float
    maturity_b: float
    co_occurrence: int
    novelty_score: float
    novelty_level: NoveltyLevel
    sample_files_a: Tuple[str, ...]
    sample_files_b: Tuple[str, ...]
    boundary_crossed: bool
    schema_version: str = COMPOSITIONAL_CURIOSITY_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "category_a": self.category_a[:64],
            "category_b": self.category_b[:64],
            "maturity_a": float(self.maturity_a),
            "maturity_b": float(self.maturity_b),
            "co_occurrence": int(self.co_occurrence),
            "novelty_score": float(self.novelty_score),
            "novelty_level": self.novelty_level.value,
            "sample_files_a": list(self.sample_files_a),
            "sample_files_b": list(self.sample_files_b),
            "boundary_crossed": bool(self.boundary_crossed),
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class CompositionalCuriosityReport:
    """Aggregate report — frozen §33.5 artifact."""

    evaluated_at_unix: float
    master_enabled: bool
    verdict: CuriosityVerdict
    pairs_examined: int
    candidate_pairs: Tuple[CompositionPair, ...]
    diagnostic: str
    elapsed_s: float
    schema_version: str = COMPOSITIONAL_CURIOSITY_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "evaluated_at_unix": self.evaluated_at_unix,
            "master_enabled": self.master_enabled,
            "verdict": self.verdict.value,
            "pairs_examined": int(self.pairs_examined),
            "candidate_pairs": [
                p.to_dict() for p in self.candidate_pairs
            ],
            "diagnostic": self.diagnostic[:512],
            "elapsed_s": float(self.elapsed_s),
            "schema_version": self.schema_version,
        }


# ===========================================================================
# Composers — canonical surfaces (lazy-imported)
# ===========================================================================


def _list_flag_specs() -> Tuple[Any, ...]:
    """Compose FlagRegistry. NEVER raises."""
    try:
        from backend.core.ouroboros.governance.flag_registry import (  # noqa: E501
            get_default_registry,
        )
    except ImportError:
        return ()
    try:
        reg = get_default_registry()
        # Seed the registry so the inventory is non-empty in
        # processes where seed_default_registry hasn't yet
        # been called.
        try:
            from backend.core.ouroboros.governance.flag_registry_seed import (  # noqa: E501
                seed_default_registry,
            )
            seed_default_registry(reg)
        except Exception:  # noqa: BLE001
            pass
        return tuple(reg.list_all())
    except Exception:  # noqa: BLE001
        return ()


def _load_doll_snapshot() -> Optional[Any]:
    """Compose Wave 1 #15 doll snapshot. Returns None when
    master is off. NEVER raises."""
    try:
        from backend.core.ouroboros.governance.second_order_doll_metric import (  # noqa: E501
            aggregate_doll_completion,
        )
    except ImportError:
        return None
    try:
        snap = aggregate_doll_completion()
        if not getattr(snap, "master_enabled", False):
            return None
        return snap
    except Exception:  # noqa: BLE001
        return None


def _is_boundary_crossed(files: Sequence[str]) -> bool:
    """Compose Wave 2 #5. NEVER raises."""
    if not files:
        return False
    try:
        from backend.core.ouroboros.governance.governance_boundary_gate import (  # noqa: E501
            is_boundary_crossed,
        )
        return bool(is_boundary_crossed(files))
    except Exception:  # noqa: BLE001
        return False


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


# ===========================================================================
# Pure pipeline
# ===========================================================================


def _index_by_category(
    specs: Sequence[Any],
) -> Dict[str, List[str]]:
    """Group source_files by Category. Each file appears once
    per category (de-duplicated). NEVER raises."""
    grouped: Dict[str, Set[str]] = {}
    for spec in specs:
        try:
            cat = str(
                getattr(getattr(spec, "category", None), "value", "")
                or "",
            ).strip().lower()
            src = str(getattr(spec, "source_file", "") or "").strip()
            if not cat or not src:
                continue
            grouped.setdefault(cat, set()).add(src)
        except Exception:  # noqa: BLE001
            continue
    bounded = max_files_per_category()
    out: Dict[str, List[str]] = {}
    for cat, files in grouped.items():
        sorted_files = sorted(files)[:bounded]
        out[cat] = sorted_files
    return out


def _parse_imports_for_file(
    file_path: str,
    *,
    repo_root: Optional[Path] = None,
) -> FrozenSet[str]:
    """ast.parse the source file → extract every dotted module
    name from ``from X import Y`` statements. NEVER raises.

    Returns frozenset of module dotted-names (e.g.
    ``backend.core.ouroboros.governance.belief_revision_ledger``).
    """
    try:
        root = repo_root or Path.cwd()
        target = root / file_path
        if not target.exists():
            return frozenset()
        src = target.read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001
        return frozenset()
    try:
        tree = ast.parse(src)
    except (SyntaxError, ValueError):
        return frozenset()
    modules: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod:
                modules.add(mod)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name:
                    modules.add(alias.name)
    return frozenset(modules)


def _file_to_category(
    grouped: Mapping[str, Sequence[str]],
) -> Dict[str, str]:
    """Invert the grouping — map each source_file → category.
    Files that appear in multiple categories pick the
    alphabetically-first category (deterministic). NEVER raises.
    """
    out: Dict[str, str] = {}
    for cat in sorted(grouped.keys()):
        for f in grouped[cat]:
            out.setdefault(f, cat)
    return out


def _build_category_import_graph(
    grouped: Mapping[str, Sequence[str]],
    *,
    repo_root: Optional[Path] = None,
) -> Dict[str, FrozenSet[str]]:
    """For each Category, return the set of OTHER categories
    its substrates collectively import from. NEVER raises.

    Algorithm:
      1. For each source_file, parse imports.
      2. For each import, resolve to a source_file (substring
         match on dotted-module → file-path).
      3. Map resolved source_file → category via
         ``_file_to_category``.
      4. Aggregate per source-category.
    """
    file_to_cat = _file_to_category(grouped)
    out: Dict[str, Set[str]] = {}
    # Build a quick suffix lookup so dotted-module name maps
    # to source_file via the trailing segment (the substrate
    # filename without ``.py``).
    file_basenames: Dict[str, str] = {}
    for f in file_to_cat.keys():
        try:
            base = Path(f).stem  # "belief_revision_ledger"
            file_basenames[base] = f
        except Exception:  # noqa: BLE001
            continue
    for cat, files in grouped.items():
        for f in files:
            imports = _parse_imports_for_file(
                f, repo_root=repo_root,
            )
            for mod in imports:
                # Resolve the final dotted segment to a known
                # substrate basename. If found, look up its
                # category and record the directed edge.
                try:
                    final = mod.rsplit(".", 1)[-1]
                except Exception:  # noqa: BLE001
                    continue
                imported_file = file_basenames.get(final)
                if imported_file is None:
                    continue
                imported_cat = file_to_cat.get(imported_file, "")
                if not imported_cat or imported_cat == cat:
                    continue
                out.setdefault(cat, set()).add(imported_cat)
    return {k: frozenset(v) for k, v in out.items()}


def _maturity_for_category(
    snapshot: Optional[Any], category: str,
) -> float:
    """Look up the canonical _STAGE_WEIGHT for a Category from
    Wave 1 #15. Returns 0.0 when snapshot unavailable. NEVER
    raises."""
    if snapshot is None:
        return 0.0
    try:
        from backend.core.ouroboros.governance.second_order_doll_metric import (  # noqa: E501
            _STAGE_WEIGHT,
        )
    except ImportError:
        return 0.0
    try:
        target = category.strip().lower()
        for axis in getattr(snapshot, "axes", ()):
            cat = str(
                getattr(axis, "category", "") or "",
            ).strip().lower()
            if cat == target:
                stage = getattr(axis, "stage", None)
                stage_value = (
                    getattr(stage, "value", "") if stage else ""
                )
                return float(_STAGE_WEIGHT.get(stage_value, 0.0))
        return 0.0
    except Exception:  # noqa: BLE001
        return 0.0


def _co_occurrence_for(
    cat_a: str, cat_b: str,
    import_graph: Mapping[str, FrozenSet[str]],
) -> int:
    """0/1/2 directed co-import count. NEVER raises."""
    a_to_b = 1 if cat_b in import_graph.get(cat_a, frozenset()) else 0
    b_to_a = 1 if cat_a in import_graph.get(cat_b, frozenset()) else 0
    return a_to_b + b_to_a


def _novelty_level_for(
    score: float,
    co_occurrence: int,
    novel_t: float,
    frontier_t: float,
) -> NoveltyLevel:
    """Pure classifier. NEVER raises."""
    if co_occurrence >= 2:
        return NoveltyLevel.STALE
    if score >= frontier_t:
        return NoveltyLevel.FRONTIER
    if score >= novel_t:
        return NoveltyLevel.NOVEL
    return NoveltyLevel.MUNDANE


def _build_pair(
    cat_a: str, cat_b: str,
    grouped: Mapping[str, Sequence[str]],
    maturity_a: float, maturity_b: float,
    co_occurrence: int,
    novel_t: float, frontier_t: float,
) -> CompositionPair:
    """Pure constructor. NEVER raises."""
    # Score: maturity_product divided by (1 + co_occurrence) so
    # historically-composed pairs decay; clamp to [0, 1].
    raw = (maturity_a * maturity_b) / float(1 + co_occurrence)
    score = max(0.0, min(1.0, raw))
    level = _novelty_level_for(
        score, co_occurrence, novel_t, frontier_t,
    )
    samples_a = tuple(grouped.get(cat_a, ())[:5])
    samples_b = tuple(grouped.get(cat_b, ())[:5])
    boundary = _is_boundary_crossed(
        list(samples_a) + list(samples_b),
    )
    return CompositionPair(
        category_a=cat_a,
        category_b=cat_b,
        maturity_a=maturity_a,
        maturity_b=maturity_b,
        co_occurrence=co_occurrence,
        novelty_score=score,
        novelty_level=level,
        sample_files_a=samples_a,
        sample_files_b=samples_b,
        boundary_crossed=boundary,
    )


# ===========================================================================
# Top-level evaluator
# ===========================================================================


def identify_curious_pairs(
    *,
    flag_specs: Optional[Sequence[Any]] = None,
    doll_snapshot: Optional[Any] = None,
    repo_root: Optional[Path] = None,
    now_unix: Optional[float] = None,
) -> CompositionalCuriosityReport:
    """Top-level evaluator. NEVER raises.

    Parameters
    ----------
    flag_specs:
        Caller-injectable inventory (testing seam). Defaults
        to FlagRegistry.list_all().
    doll_snapshot:
        Caller-injectable snapshot (testing seam). Defaults to
        Wave 1 #15 aggregate_doll_completion.
    repo_root:
        Caller-injectable repo root for import-graph parsing
        (testing seam). Defaults to CWD.
    """
    started = time.time() if now_unix is None else float(now_unix)

    if not master_enabled():
        return CompositionalCuriosityReport(
            evaluated_at_unix=started,
            master_enabled=False,
            verdict=CuriosityVerdict.DISABLED,
            pairs_examined=0,
            candidate_pairs=(),
            diagnostic=(
                f"gate disabled via {_ENV_MASTER}=false"
            ),
            elapsed_s=0.0,
        )

    specs = (
        flag_specs
        if flag_specs is not None
        else _list_flag_specs()
    )
    snapshot = (
        doll_snapshot
        if doll_snapshot is not None
        else _load_doll_snapshot()
    )

    grouped = _index_by_category(specs)
    if not grouped:
        return CompositionalCuriosityReport(
            evaluated_at_unix=started,
            master_enabled=True,
            verdict=CuriosityVerdict.NO_CANDIDATES,
            pairs_examined=0,
            candidate_pairs=(),
            diagnostic=(
                "empty FlagRegistry inventory — no substrates "
                "to evaluate"
            ),
            elapsed_s=max(0.0, time.time() - started),
        )

    import_graph = _build_category_import_graph(
        grouped, repo_root=repo_root,
    )

    novel_t = novel_threshold()
    frontier_t = frontier_threshold()

    categories = sorted(grouped.keys())
    pairs: List[CompositionPair] = []
    examined = 0
    for i, cat_a in enumerate(categories):
        for cat_b in categories[i + 1:]:
            examined += 1
            mat_a = _maturity_for_category(snapshot, cat_a)
            mat_b = _maturity_for_category(snapshot, cat_b)
            co = _co_occurrence_for(cat_a, cat_b, import_graph)
            pair = _build_pair(
                cat_a, cat_b,
                grouped,
                mat_a, mat_b, co,
                novel_t, frontier_t,
            )
            # Filter STALE — operator only wants actionable signal.
            if pair.novelty_level is NoveltyLevel.STALE:
                continue
            pairs.append(pair)

    # Rank by score desc, then category alphabetical for stable
    # output. Cap at max_pairs.
    pairs.sort(
        key=lambda p: (-p.novelty_score, p.category_a, p.category_b),
    )
    cap = max_pairs()
    candidates = tuple(pairs[:cap])

    if any(p.novelty_level is NoveltyLevel.FRONTIER for p in candidates):
        verdict = CuriosityVerdict.ACTIONABLE
    elif any(p.novelty_level is NoveltyLevel.NOVEL for p in candidates):
        verdict = CuriosityVerdict.EMERGING
    else:
        verdict = CuriosityVerdict.NO_CANDIDATES

    diagnostic = (
        f"examined {examined} pair(s); {len(candidates)} "
        f"candidate(s) surfaced (novel_t={novel_t:.2f} "
        f"frontier_t={frontier_t:.2f})"
    )

    report = CompositionalCuriosityReport(
        evaluated_at_unix=started,
        master_enabled=True,
        verdict=verdict,
        pairs_examined=examined,
        candidate_pairs=candidates,
        diagnostic=diagnostic,
        elapsed_s=max(0.0, time.time() - started),
    )
    _persist_report(report)
    _publish_curiosity_event(report)
    return report


# ===========================================================================
# §33.4 persistence
# ===========================================================================


def _persist_report(report: CompositionalCuriosityReport) -> None:
    """Best-effort §33.4 write. NEVER raises. Skips when no
    candidates surfaced (silence on idle)."""
    if not report.candidate_pairs:
        return
    _flock_append({"kind": "summary", "payload": report.to_dict()})
    for pair in report.candidate_pairs:
        if pair.novelty_level in (
            NoveltyLevel.NOVEL, NoveltyLevel.FRONTIER,
        ):
            _flock_append(
                {"kind": "candidate", "payload": pair.to_dict()},
            )


# ===========================================================================
# SSE publisher
# ===========================================================================


def _publish_curiosity_event(
    report: CompositionalCuriosityReport,
) -> None:
    """Best-effort SSE publish. NEVER raises. Fires only on
    EMERGING / ACTIONABLE verdict."""
    if not master_enabled():
        return
    if report.verdict not in (
        CuriosityVerdict.EMERGING, CuriosityVerdict.ACTIONABLE,
    ):
        return
    try:
        from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
            EVENT_TYPE_COMPOSITIONAL_CURIOSITY_EVALUATED,
            publish_task_event,
        )
        publish_task_event(
            EVENT_TYPE_COMPOSITIONAL_CURIOSITY_EVALUATED,
            (
                f"system::compositional_curiosity::"
                f"{report.schema_version}"
            ),
            {
                "verdict": report.verdict.value,
                "pairs_examined": report.pairs_examined,
                "candidate_count": len(report.candidate_pairs),
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


def format_curiosity_panel(
    report: Optional[CompositionalCuriosityReport] = None,
) -> str:
    """Operator-facing panel. NEVER raises."""
    if report is None:
        if not master_enabled():
            return (
                f"compositional curiosity: disabled "
                f"({_ENV_MASTER}=false)"
            )
        return "compositional curiosity: no report"
    if not report.master_enabled:
        return (
            f"compositional curiosity: disabled "
            f"({_ENV_MASTER}=false)"
        )
    glyph = verdict_glyph(report.verdict)
    lines = [
        f"🔭 Compositional Curiosity  {glyph} "
        f"{report.verdict.value}",
        f"  pairs_examined : {report.pairs_examined}",
        f"  candidates     : {len(report.candidate_pairs)}",
    ]
    if report.candidate_pairs:
        lines.append("  top pairs:")
        for p in report.candidate_pairs[:5]:
            ng = novelty_glyph(p.novelty_level)
            lines.append(
                f"    {ng} {p.category_a:<14} × {p.category_b:<14} "
                f"score={p.novelty_score:.2f} "
                f"co={p.co_occurrence} "
                f"({p.novelty_level.value})"
            )
        if len(report.candidate_pairs) > 5:
            lines.append(
                f"    ... (+{len(report.candidate_pairs) - 5} more)"
            )
    lines.append(f"  diagnostic     : {report.diagnostic}")
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
        "compositional_curiosity.py"
    )

    _EXPECTED_VERDICTS = {
        "no_candidates", "emerging", "actionable", "disabled",
    }
    _EXPECTED_NOVELTY = {
        "stale", "mundane", "novel", "frontier",
    }

    def _validate_verdict_taxonomy(
        tree: ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ClassDef)
                and node.name == "CuriosityVerdict"
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
                        f"CuriosityVerdict missing: "
                        f"{sorted(missing)}",
                    )
                if extra:
                    return (
                        f"CuriosityVerdict drift: "
                        f"{sorted(extra)}",
                    )
                return ()
        return ("CuriosityVerdict class not found",)

    def _validate_novelty_taxonomy(
        tree: ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ClassDef)
                and node.name == "NoveltyLevel"
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
                missing = _EXPECTED_NOVELTY - found
                extra = found - _EXPECTED_NOVELTY
                if missing:
                    return (
                        f"NoveltyLevel missing: "
                        f"{sorted(missing)}",
                    )
                if extra:
                    return (
                        f"NoveltyLevel drift: {sorted(extra)}",
                    )
                return ()
        return ("NoveltyLevel class not found",)

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
            "backend.core.ouroboros.governance.curiosity_scheduler",
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
        if "flag_registry" not in source:
            violations.append(
                "must compose canonical flag_registry "
                "(inventory source)",
            )
        if "second_order_doll_metric" not in source:
            violations.append(
                "must compose Wave 1 #15 "
                "second_order_doll_metric (maturity source)",
            )
        if "governance_boundary_gate" not in source:
            violations.append(
                "must compose Wave 2 #5 "
                "governance_boundary_gate (cage flag)",
            )
        if "cross_process_jsonl" not in source:
            violations.append(
                "must compose canonical cross_process_jsonl "
                "(§33.4 ledger)",
            )
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name=(
                "compositional_curiosity_verdict_taxonomy_closed"
            ),
            target_file=target,
            description=(
                "CuriosityVerdict 4-value taxonomy bytes-pinned."
            ),
            validate=_validate_verdict_taxonomy,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "compositional_curiosity_novelty_taxonomy_closed"
            ),
            target_file=target,
            description=(
                "NoveltyLevel 4-value taxonomy bytes-pinned."
            ),
            validate=_validate_novelty_taxonomy,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "compositional_curiosity_authority_asymmetry"
            ),
            target_file=target,
            description=(
                "Substrate purity — pure novelty detector. "
                "MUST NOT import orchestrator / iron_gate / "
                "policy / providers / candidate_generator / "
                "urgency_router / change_engine / "
                "semantic_guardian / auto_committer / "
                "risk_tier_floor / curiosity_scheduler "
                "(curiosity_scheduler consumes this substrate, "
                "not the other way around)."
            ),
            validate=_validate_authority_asymmetry,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "compositional_curiosity_master_default_false"
            ),
            target_file=target,
            description=(
                "§33.1 cognitive substrate default-FALSE."
            ),
            validate=_validate_master_default_false,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "compositional_curiosity_composes_canonical"
            ),
            target_file=target,
            description=(
                "Substrate composes flag_registry + Wave 1 "
                "#15 second_order_doll_metric + Wave 2 #5 "
                "governance_boundary_gate + canonical "
                "cross_process_jsonl — no parallel "
                "inventory / maturity / cage / ledger."
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
        "backend/core/ouroboros/governance/"
        "compositional_curiosity.py"
    )

    seeds = [
        FlagSpec(
            name=_ENV_MASTER,
            type=FlagType.BOOL,
            default=False,
            description=(
                "Compositional curiosity master switch. §33.1 "
                "cognitive substrate default-FALSE. When on, "
                "the substrate scans FlagRegistry inventory + "
                "Wave 1 #15 doll snapshot + per-substrate "
                "import graph (via ast.parse) and surfaces "
                "Category-pair compositions that are mature "
                "but uncomposed. Closes §40 Wave 1 #16 (PRD "
                "v2.99+) — final non-experimental §40 arc."
            ),
            category=Category.EXPERIMENTAL,
            source_file=src,
            example=f"{_ENV_MASTER}=true",
        ),
        FlagSpec(
            name=_ENV_PERSIST,
            type=FlagType.BOOL,
            default=True,
            description=(
                "Sub-flag — gate §33.4 JSONL audit writes."
            ),
            category=Category.SAFETY,
            source_file=src,
            example=f"{_ENV_PERSIST}=false",
        ),
        FlagSpec(
            name=_ENV_NOVEL_THRESHOLD,
            type=FlagType.FLOAT,
            default=_DEFAULT_NOVEL_THRESHOLD,
            description=(
                "Score threshold to label a pair NOVEL. "
                "Defaults to 0.20. Clamped to [0.0, 1.0]."
            ),
            category=Category.TUNING,
            source_file=src,
            example=f"{_ENV_NOVEL_THRESHOLD}=0.30",
        ),
        FlagSpec(
            name=_ENV_FRONTIER_THRESHOLD,
            type=FlagType.FLOAT,
            default=_DEFAULT_FRONTIER_THRESHOLD,
            description=(
                "Score threshold to label a pair FRONTIER. "
                "Defaults to 0.50. Auto-clamped ≥ "
                "novel_threshold."
            ),
            category=Category.TUNING,
            source_file=src,
            example=f"{_ENV_FRONTIER_THRESHOLD}=0.70",
        ),
        FlagSpec(
            name=_ENV_MAX_PAIRS,
            type=FlagType.INT,
            default=_DEFAULT_MAX_PAIRS,
            description=(
                "Cap on returned candidate pairs. Defaults to "
                "10. Clamped to [1, 1000]."
            ),
            category=Category.CAPACITY,
            source_file=src,
            example=f"{_ENV_MAX_PAIRS}=20",
        ),
        FlagSpec(
            name=_ENV_MAX_FILES_PER_CATEGORY,
            type=FlagType.INT,
            default=_DEFAULT_MAX_FILES_PER_CATEGORY,
            description=(
                "Cap on substrate files parsed per Category. "
                "Bounds the O(C × F × ast.parse) cost. "
                "Defaults to 15. Clamped to [1, 500]."
            ),
            category=Category.CAPACITY,
            source_file=src,
            example=f"{_ENV_MAX_FILES_PER_CATEGORY}=30",
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
    "COMPOSITIONAL_CURIOSITY_SCHEMA_VERSION",
    "CuriosityVerdict",
    "NoveltyLevel",
    "CompositionPair",
    "CompositionalCuriosityReport",
    "master_enabled",
    "persistence_enabled",
    "novel_threshold",
    "frontier_threshold",
    "max_pairs",
    "max_files_per_category",
    "ledger_path",
    "verdict_glyph",
    "novelty_glyph",
    "identify_curious_pairs",
    "format_curiosity_panel",
    "register_shipped_invariants",
    "register_flags",
]
