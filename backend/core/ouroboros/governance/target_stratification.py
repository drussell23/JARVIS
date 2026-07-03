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
import bisect
import logging
import os
import threading
import time
from pathlib import Path
from typing import (
    Dict,
    FrozenSet,
    Iterable,
    List,
    NamedTuple,
    Optional,
    Set,
    Tuple,
    Union,
)

logger = logging.getLogger(__name__)

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


# ---------------------------------------------------------------------------
# Tier-4 loop-starvation fix — off-loop coverage index
# ---------------------------------------------------------------------------
#
# Traced mechanism (session bt-iso-1783102490): ``file_has_test_coverage``
# was invoked synchronously ON the asyncio loop from the intake hot path
# (UnifiedIntakeRouter._ingest_impl → _compute_priority →
# ingest_priority_penalty). Strategy 1 materializes
# ``sorted(top.rglob("test_*.py"))`` — a full recursive walk + stat of the
# entire tests/ tree (~2,839 files here) on EVERY call — and Strategy 2
# cold-builds the AST import map by reading + ``ast.parse``-ing ALL test
# files (~963K lines) on the FIRST cache miss: the observed 41,713 ms
# single on-loop block, with 1–16 s recurring blocks from the per-call
# rglob afterwards.
#
# The fix: a per-repo in-memory **coverage index** — the sorted tuple of
# test-file basenames (Strategy 1 becomes two bisect/set lookups) plus the
# existing AST import map (Strategy 2 becomes a dict lookup) — built in a
# SINGLE tree traversal OFF the event loop via the unified
# ``cooperative_fs_io.offload`` substrate.
#
#   * Single-flight: concurrent triggers coalesce on ``_COVERAGE_IDX_LOCK``
#     + the ``building`` set — at most one build per repo_root at a time.
#   * Atomic swap: the index is assembled entirely in the worker, then
#     swapped into ``_coverage_index`` under the lock — readers observe
#     the old index or the new one, never a partial.
#   * Degraded-proceed: while the index is cold/building, async callers
#     (intake) proceed WITHOUT the stratification prior (advisory-only,
#     priority ordering — never authority), mirroring the SemanticIndex
#     Tier-2b convention. Sync callers keep the legacy scan, byte-identical.
#   * Fail-soft: a failed build logs at DEBUG, arms a retry cooldown, and
#     leaves callers degraded — never raises into intake.
#
# Env knobs (no hardcoding):
#   * ``JARVIS_STRATIFICATION_INDEX_ENABLED``  (default "true") — master.
#   * ``JARVIS_STRATIFICATION_INDEX_TTL_S``    (default 600) — refresh age;
#     a stale index is STILL served while the refresh rebuilds off-loop
#     (eventually consistent; the signal is advisory). ``0`` = build once.
#   * ``JARVIS_STRATIFICATION_INDEX_RETRY_S``  (default 600) — failure
#     cooldown before another build attempt.


class _CoverageIndex(NamedTuple):
    """Immutable per-repo test-coverage index (atomic-swap unit)."""

    names_set: FrozenSet[str]      # every test_*.py basename (exact match)
    names_sorted: Tuple[str, ...]  # same names, sorted (prefix bisect)
    ast_map: Dict[str, List[Path]]
    built_at: float


_coverage_index: Dict[Path, _CoverageIndex] = {}
_coverage_index_building: Set[Path] = set()
_coverage_index_failed_at: Dict[Path, float] = {}
_coverage_index_tasks: Set[object] = set()  # strong refs to build tasks
_COVERAGE_IDX_LOCK = threading.Lock()


def _coverage_index_enabled() -> bool:
    return os.environ.get(
        "JARVIS_STRATIFICATION_INDEX_ENABLED", "true",
    ).strip().lower() not in ("0", "false", "no", "off")


def _coverage_index_ttl_s() -> float:
    return max(0.0, _env_float("JARVIS_STRATIFICATION_INDEX_TTL_S", 600.0))


def _coverage_index_retry_s() -> float:
    return max(0.0, _env_float("JARVIS_STRATIFICATION_INDEX_RETRY_S", 600.0))


def _resolve_scan_root(repo_root: Union[str, Path]) -> Path:
    """Canonical scan-root resolution shared by every coverage entrypoint.

    Applies the same ``.worktrees/<name>`` → parent-repo translation as
    ``file_has_test_coverage`` (READS stay authoritative) and resolves the
    path so the index cache key is stable across spellings.
    """
    try:
        from backend.core.ouroboros.governance.execution_context import (
            authoritative_repo_root as _auth_root,
        )
        root = _auth_root(Path(repo_root))
    except Exception:  # noqa: BLE001 — fail-soft, never breaks coverage
        root = Path(repo_root)
    try:
        return root.resolve()
    except OSError:  # circular symlinks — keep syntactic form
        return root


def _build_coverage_index_sync(
    repo_root: Path,
    dir_names: FrozenSet[str],
) -> _CoverageIndex:
    """ONE traversal of the test tree → name index + AST import map.

    Heavy by design (this is the 41.7 s cold cost) — MUST only run off the
    event loop (offload substrate / worker thread). Pure function of the
    filesystem; assembles the full index before the caller swaps it in.
    """
    files = list(_iter_test_files(repo_root, dir_names))
    names = frozenset(f.name for f in files)
    ast_map = _strat_build_ast_map(repo_root, dir_names, files=files)
    return _CoverageIndex(
        names_set=names,
        names_sorted=tuple(sorted(names)),
        ast_map=ast_map,
        built_at=time.time(),
    )


def _coverage_index_lookup(scan_root: Path) -> Optional[_CoverageIndex]:
    """Return the warm index for an already-resolved scan root (or None)."""
    if not _coverage_index_enabled():
        return None
    with _COVERAGE_IDX_LOCK:
        return _coverage_index.get(scan_root)


def coverage_index_ready(repo_root: Union[str, Path]) -> bool:
    """True when a warm in-memory coverage index exists for ``repo_root``.

    Async hot paths branch on this: ready → compute the stratification
    penalty via cheap index lookups (offloaded); not ready → proceed
    DEGRADED (no penalty) and let :func:`trigger_coverage_index_build`
    warm the index in the background.
    """
    return _coverage_index_lookup(_resolve_scan_root(repo_root)) is not None


def reset_coverage_index() -> None:
    """Test hook — drop all coverage-index state (mirrors reset_default_index)."""
    with _COVERAGE_IDX_LOCK:
        _coverage_index.clear()
        _coverage_index_building.clear()
        _coverage_index_failed_at.clear()
        _coverage_index_tasks.clear()


async def _coverage_index_build_task(scan_root: Path) -> None:
    """Detached build task — offloads the heavy traversal, swaps atomically.

    NEVER raises out (fire-and-forget). Failure arms the retry cooldown and
    logs at DEBUG; success also warms the legacy ``_strat_ast_cache`` so
    synchronous callers skip their own cold AST build.
    """
    idx: Optional[_CoverageIndex] = None
    try:
        dir_names = _strat_test_dir_names()
        try:
            from backend.core.ouroboros.governance.cooperative_fs_io import (
                is_offload_error,
                offload,
            )
        except Exception:  # noqa: BLE001 — substrate import fault
            # Still keep the multi-second build off the loop (bare thread).
            import asyncio as _aio
            try:
                idx = await _aio.to_thread(
                    _build_coverage_index_sync, scan_root, dir_names,
                )
            except Exception:  # noqa: BLE001
                idx = None
        else:
            result = await offload(
                _build_coverage_index_sync, scan_root, dir_names,
                cpu_bound=False,
            )
            idx = None if is_offload_error(result) else result
    except Exception:  # noqa: BLE001 — belt-and-suspenders
        idx = None
        logger.debug(
            "[Stratification] coverage index build raised root=%s",
            scan_root, exc_info=True,
        )
    finally:
        with _COVERAGE_IDX_LOCK:
            if idx is not None:
                _coverage_index[scan_root] = idx  # atomic swap
                _coverage_index_failed_at.pop(scan_root, None)
            else:
                _coverage_index_failed_at[scan_root] = time.time()
            _coverage_index_building.discard(scan_root)
        if idx is not None:
            # Warm the legacy per-process AST cache too — sync callers
            # (miner scan_once, Advisor coverage) skip their cold build.
            _strat_ast_cache[scan_root] = idx.ast_map
            logger.info(
                "[Stratification] coverage index built root=%s "
                "test_files=%d ast_modules=%d",
                scan_root, len(idx.names_set), len(idx.ast_map),
            )
        else:
            logger.debug(
                "[Stratification] coverage index build failed root=%s "
                "(degraded — no stratification prior until retry window)",
                scan_root,
            )


def trigger_coverage_index_build(repo_root: Union[str, Path]) -> str:
    """Non-blocking, single-flight build trigger (mirrors build_async).

    Returns a string sentinel for logs/tests:
      * ``"started"``                — a detached build task was scheduled
      * ``"skipped_running"``        — a build is already in flight
      * ``"skipped_fresh"``          — index exists and is within TTL
      * ``"skipped_failed_cooldown"``— last build failed; retry window open
      * ``"skipped_no_loop"``        — no running event loop (sync caller)
      * ``"skipped_disabled"``       — master switch off
    """
    if not _coverage_index_enabled():
        return "skipped_disabled"
    scan_root = _resolve_scan_root(repo_root)
    now = time.time()
    with _COVERAGE_IDX_LOCK:
        if scan_root in _coverage_index_building:
            return "skipped_running"
        idx = _coverage_index.get(scan_root)
        if idx is not None:
            ttl = _coverage_index_ttl_s()
            if ttl <= 0 or (now - idx.built_at) < ttl:
                return "skipped_fresh"
        failed_at = _coverage_index_failed_at.get(scan_root)
        if failed_at is not None and (now - failed_at) < _coverage_index_retry_s():
            return "skipped_failed_cooldown"
        _coverage_index_building.add(scan_root)
    import asyncio as _aio
    try:
        loop = _aio.get_running_loop()
    except RuntimeError:
        with _COVERAGE_IDX_LOCK:
            _coverage_index_building.discard(scan_root)
        return "skipped_no_loop"
    task = loop.create_task(_coverage_index_build_task(scan_root))
    _coverage_index_tasks.add(task)
    task.add_done_callback(_coverage_index_tasks.discard)
    return "started"


def _iter_test_files(
    repo_root: Path,
    dir_names: FrozenSet[str],
) -> "Iterable[Path]":
    """Yield every ``test_*.py`` regular file under the configured test roots.

    Single source of truth for the test-tree traversal so the AST-map
    builder and the coverage-index builder can never drift (and so the
    expensive walk happens exactly ONCE when both are built together).
    """
    for tdn in sorted(dir_names):
        top = repo_root / tdn
        if not top.is_dir():
            continue
        for test_file in sorted(top.rglob("test_*.py")):
            if test_file.is_file():
                yield test_file


def _strat_build_ast_map(
    repo_root: Path,
    dir_names: FrozenSet[str],
    *,
    files: "Optional[List[Path]]" = None,
) -> Dict[str, List[Path]]:
    """Synchronous AST import-map builder (mirrors test_runner._build_test_import_map).

    Scans every ``test_*.py`` under the configured test roots and maps
    ``dotted_module_path → [test_files that import it]``. ``files`` lets a
    caller that already walked the tree (the coverage-index builder) reuse
    its file list instead of paying a second full traversal.
    """
    import_map: Dict[str, List[Path]] = {}
    for test_file in (
        files if files is not None
        else _iter_test_files(repo_root, dir_names)
    ):
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

    # Tier-4 fast path — when the off-loop coverage index is warm, answer
    # from memory: Strategy 1 collapses to a set lookup + one bisect prefix
    # probe, Strategy 2 to a dict lookup. NO filesystem traversal. This is
    # what makes the function safe to call from latency-sensitive contexts
    # once the index is built (the intake hot path additionally never calls
    # it on the loop at all — see UnifiedIntakeRouter._ingest_impl).
    try:
        _idx_key = _scan_root.resolve()
    except OSError:  # noqa: PERF203 — circular symlink, keep syntactic form
        _idx_key = _scan_root
    _idx = _coverage_index_lookup(_idx_key)
    if _idx is not None:
        if exact_name in _idx.names_set:
            return True
        _i = bisect.bisect_left(_idx.names_sorted, suffix_prefix)
        if _i < len(_idx.names_sorted) and _idx.names_sorted[_i].startswith(
            suffix_prefix,
        ):
            return True  # suffix variants all end with ".py" by construction
        if _strat_ast_import_enabled():
            try:
                _fp = Path(file_path)
                if not _fp.is_absolute():
                    _fp = _scan_root / _fp
                _module_path = _strat_path_to_module(_fp, _scan_root)
                if _module_path and _idx.ast_map.get(_module_path):
                    return True
            except Exception:  # noqa: BLE001 — fail-soft, mirror Strategy 2
                pass
        return False

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
