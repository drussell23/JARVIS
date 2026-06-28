"""Slice 48 — Semantic Target Stratification scoring substrate.

Shared, policy-driven helpers for biasing autonomous target selection toward
small / test-covered files and away from massive zero-coverage core modules —
WITHOUT a hardcoded filename denylist (Manifesto §5: intelligence-driven
routing, no hardcoded tables).

Two pure functions, no class state:

  * ``file_has_test_coverage`` — the canonical "does this source file have a
    specific test" signal.  Uses the SAME multi-strategy global AST-aware
    resolver as ``TestRunner.resolve_affected_tests`` (Strategies 1–3, no
    broad-repo fallback) so the Advisor gate and sensor stratification can
    never drift.  OperationAdvisor delegates its per-file coverage check here.

    Strategy 2 (suffix-aware recursive across all test roots) catches names
    like ``test_repl_input_polish_slice4.py`` that the old single-path
    ``tests/test_{stem}.py`` existence check missed — fixing the spurious
    Advisor BLOCK on files that DO have tests.

  * ``stratification_penalty_multiplier`` — the soft down-rank weight. A file's
    baseline priority is multiplied by this in (1 - alpha, 1.0]. Covered files
    are never penalized; uncovered files are penalized proportional to their
    line-count (saturating at ``max_lines``). The ``suppress`` flag is the
    self-improvement escape hatch: when an operation's intent IS adding test
    coverage, the penalty is bypassed so the organism can still target — and
    heal — its own large uncovered modules over time.

Both ``alpha`` and ``max_lines`` are env-tunable (no hardcoding):
  * ``JARVIS_STRATIFICATION_PENALTY_ALPHA`` (default 0.75) — max down-rank.
  * ``JARVIS_STRATIFICATION_MAX_LINES``    (default 2000) — saturation point.
  * ``JARVIS_TEST_DIR_NAMES``              (default "tests,test") — test roots.
  * ``JARVIS_STRATIFICATION_AST_IMPORT_ENABLED`` (default "true") — enable
    Strategy 3 AST-import scan (cached per repo_root per process).
"""
from __future__ import annotations

import ast as _ast
import os
from pathlib import Path
from typing import Dict, FrozenSet, Iterable, List, Union

# Defaults are module constants so tests + callers share one source of truth.
DEFAULT_PENALTY_ALPHA: float = 0.75
DEFAULT_PENALTY_MAX_LINES: int = 2000
# Slice 49 — ingest-priority penalty scale/cap. The worst target file's soft
# penalty (0..alpha) is projected onto 0..SCALE integer priority points and
# capped, so it deprioritizes large uncovered ops without ever swamping the
# base priority scale (sources map to 1..99).
DEFAULT_INGEST_PENALTY_SCALE: int = 5


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# AST-aware coverage resolver helpers
# ---------------------------------------------------------------------------

def _strat_test_dir_names() -> FrozenSet[str]:
    """Return the configured test-root directory names (read from env at call time)."""
    return frozenset(os.environ.get("JARVIS_TEST_DIR_NAMES", "tests,test").split(","))


def _strat_ast_import_enabled() -> bool:
    return os.environ.get(
        "JARVIS_STRATIFICATION_AST_IMPORT_ENABLED", "true",
    ).strip().lower() not in ("0", "false", "no")


# Lazy AST import map per repo_root — built once, cached per process.
# Keyed by resolved repo_root (Path); value is {dotted_module: [test_files]}.
# Thread-safe for reads; idempotent racy writes are benign.
_strat_ast_cache: Dict[Path, Dict[str, List[Path]]] = {}


def _strat_build_ast_map(
    repo_root: Path,
    dir_names: FrozenSet[str],
) -> Dict[str, List[Path]]:
    """Synchronous AST import-map builder (mirrors test_runner._build_test_import_map).

    Scans every ``test_*.py`` under the configured test roots and maps
    ``dotted_module_path → [test_files that import it]``.
    """
    import_map: Dict[str, List[Path]] = {}
    for tdn in sorted(dir_names):
        top = repo_root / tdn
        if not top.is_dir():
            continue
        for test_file in sorted(top.rglob("test_*.py")):
            if not test_file.is_file():
                continue
            try:
                source = test_file.read_text(encoding="utf-8", errors="replace")
                tree = _ast.parse(source, filename=str(test_file))
            except (SyntaxError, OSError, UnicodeDecodeError):
                continue
            for node in _ast.walk(tree):
                if isinstance(node, _ast.Import):
                    for alias in node.names:
                        lst = import_map.setdefault(alias.name, [])
                        if test_file not in lst:
                            lst.append(test_file)
                elif isinstance(node, _ast.ImportFrom):
                    module = node.module or ""
                    if module:
                        lst = import_map.setdefault(module, [])
                        if test_file not in lst:
                            lst.append(test_file)
                    for alias in node.names:
                        full = f"{module}.{alias.name}" if module else alias.name
                        lst = import_map.setdefault(full, [])
                        if test_file not in lst:
                            lst.append(test_file)
    return import_map


def _strat_path_to_module(source_file: Path, repo_root: Path) -> str | None:
    """Convert a repo-relative source path to a dotted module string.

    Mirrors ``test_runner._path_to_module``.  Returns ``None`` when
    ``source_file`` is outside ``repo_root``.
    """
    try:
        rel = source_file.resolve().relative_to(repo_root.resolve())
    except ValueError:
        return None
    parts = list(rel.parts)
    if not parts:
        return None
    if parts[-1].endswith(".py"):
        parts[-1] = parts[-1][:-3]
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) if parts else None


def file_has_test_coverage(
    file_path: Union[str, Path],
    repo_root: Path,
) -> bool:
    """Return True if ``file_path`` has ≥1 specific test via global AST-aware resolution.

    Canonical definition of the codebase's test-existence signal.  Uses the
    SAME multi-strategy resolver logic as ``TestRunner.resolve_affected_tests``
    (Strategies 1–3), so the OperationAdvisor gate and sensor stratification
    bias can never drift from the test runner's own discovery.

    Strategy 1 — **Suffix-aware recursive** (subsumes the old exact-match):
        Search all configured test roots for ``test_<stem>.py`` *and*
        ``test_<stem>_*.py`` (catches ``_slice4``-style suffix variants).

    Strategy 2 — **AST-import** (cached per ``repo_root`` per process):
        A test file whose AST directly imports this module counts as
        coverage.  Gated by ``JARVIS_STRATIFICATION_AST_IMPORT_ENABLED``
        (default ``true``).

    Deliberately excludes the broad repo-level ``tests/`` fallback
    (Strategy 4 in the TestRunner): that path signals *no specific test
    found* and MUST NOT be mistaken for coverage.

    Non-``.py`` and ``test_*`` inputs are treated as covered (no penalty).

    ``repo_root`` is automatically translated via
    :func:`execution_context.authoritative_repo_root` so that a
    ``.worktrees/<name>/`` path (an L3 isolation worktree that may be empty
    or partially cleaned) is silently redirected to the parent repo root
    where test files actually live. READS stay authoritative; WRITES still
    target the worktree.
    """
    # Defense-in-depth: translate .worktrees/<name> paths to the real repo
    # root so coverage detection never returns 0 due to an empty worktree.
    try:
        from backend.core.ouroboros.governance.execution_context import (
            authoritative_repo_root as _auth_root,
        )
        _scan_root = _auth_root(Path(repo_root))
    except Exception:  # noqa: BLE001 — fail-soft, never breaks coverage
        _scan_root = Path(repo_root)

    name = Path(file_path).name
    if not name.endswith(".py") or "test_" in name:
        return True
    stem = Path(file_path).stem
    dir_names = _strat_test_dir_names()
    exact_name = f"test_{stem}.py"
    suffix_prefix = f"test_{stem}_"

    # Strategy 1: suffix-aware recursive search across all test roots.
    # Finds test_<stem>.py (exact) AND test_<stem>_*.py (suffix variants).
    # This subsumes the old single-path tests/test_{stem}.py existence check.
    # Uses _scan_root (translated from .worktrees/<name> to the real repo root
    # via authoritative_repo_root) so coverage detection never returns 0 due
    # to an empty isolation worktree.
    for tdn in sorted(dir_names):
        top = _scan_root / tdn
        if not top.is_dir():
            continue
        for match in sorted(top.rglob("test_*.py")):
            if not match.is_file():
                continue
            mname = match.name
            if mname == exact_name or (
                mname.startswith(suffix_prefix) and mname.endswith(".py")
            ):
                return True

    # Strategy 2: AST-import scan (lazy cached per _scan_root, env-opt-out).
    # Uses _scan_root (authoritative repo root) so the import map is built
    # from real test files, never from an empty isolation worktree.
    if _strat_ast_import_enabled():
        try:
            fp = Path(file_path)
            if not fp.is_absolute():
                fp = _scan_root / fp
            module_path = _strat_path_to_module(fp, _scan_root)
            if module_path:
                resolved_root = _scan_root.resolve()
                if resolved_root not in _strat_ast_cache:
                    _strat_ast_cache[resolved_root] = _strat_build_ast_map(
                        resolved_root, dir_names
                    )
                if _strat_ast_cache[resolved_root].get(module_path):
                    return True
        except Exception:  # noqa: BLE001 — fail-soft, never raises
            pass


    return False


def stratification_penalty_multiplier(
    total_lines: int,
    has_test_coverage: bool,
    *,
    alpha: float | None = None,
    max_lines: int | None = None,
    suppress: bool = False,
) -> float:
    """Soft down-rank weight in ``(1 - alpha, 1.0]``.

    ``multiplier = 1 - alpha * min(1, total_lines / max_lines) * (1 - covered)``

    Returns exactly ``1.0`` when the file is covered or ``suppress`` is set
    (the test-generation escape hatch). Otherwise scales the penalty by the
    file's normalized line-count so huge uncovered modules are pushed down
    while small leaf utilities are barely touched.
    """
    if suppress or has_test_coverage:
        return 1.0
    a = DEFAULT_PENALTY_ALPHA if alpha is None else alpha
    mx = DEFAULT_PENALTY_MAX_LINES if max_lines is None else max_lines
    if alpha is None:
        a = _env_float("JARVIS_STRATIFICATION_PENALTY_ALPHA", a)
    if max_lines is None:
        mx = _env_int("JARVIS_STRATIFICATION_MAX_LINES", mx)
    if mx <= 0:
        return 1.0
    size_norm = min(1.0, max(0, total_lines) / float(mx))
    multiplier = 1.0 - a * size_norm
    # Clamp into (1 - alpha, 1.0] defensively (alpha could be mis-set > 1).
    floor = 1.0 - a
    return max(floor, min(1.0, multiplier))


def _count_lines(path: Path) -> int:
    """Cheap line count; 0 on any error (fail-soft, never raises)."""
    try:
        with open(path, "rb") as f:
            return sum(1 for _ in f)
    except OSError:
        return 0


def ingest_priority_penalty(
    target_files: Iterable[Union[str, Path]],
    repo_root: Path,
    *,
    suppress: bool = False,
    alpha: float | None = None,
    max_lines: int | None = None,
    scale: int | None = None,
) -> int:
    """Integer priority penalty (>= 0) for the central ingest funnel (Slice 49).

    Deprioritizes operations targeting large, uncovered files fleet-wide
    (added to the priority int, where lower = higher priority). The worst
    target file dominates; the result is projected onto 0..scale and capped,
    so it can never swamp the base priority scale. Covered files and the
    ``suppress`` escape hatch (test-generation intent) yield 0. Fail-soft:
    any error on a file contributes 0, never raises.

    Stays SOFT by construction — this only reorders the queue. The hard
    blast-radius gate remains OperationAdvisor.advise().
    """
    if suppress:
        return 0
    a = DEFAULT_PENALTY_ALPHA if alpha is None else alpha
    if alpha is None:
        a = _env_float("JARVIS_STRATIFICATION_PENALTY_ALPHA", a)
    sc = DEFAULT_INGEST_PENALTY_SCALE if scale is None else scale
    if scale is None:
        sc = _env_int("JARVIS_INGEST_STRATIFICATION_SCALE", sc)
    if sc <= 0:
        return 0

    worst = 0.0
    for f in target_files or ():
        name = Path(f).name
        if not name.endswith(".py") or "test_" in name:
            continue
        if file_has_test_coverage(f, repo_root):
            continue
        lines = _count_lines(repo_root / f)
        if lines <= 0:
            continue
        mult = stratification_penalty_multiplier(
            lines, has_test_coverage=False, alpha=a, max_lines=max_lines,
        )
        worst = max(worst, 1.0 - mult)  # 0..alpha

    return min(sc, round(worst * sc))
