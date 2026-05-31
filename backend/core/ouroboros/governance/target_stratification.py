"""Slice 48 — Semantic Target Stratification scoring substrate.

Shared, policy-driven helpers for biasing autonomous target selection toward
small / test-covered files and away from massive zero-coverage core modules —
WITHOUT a hardcoded filename denylist (Manifesto §5: intelligence-driven
routing, no hardcoded tables).

Two pure functions, no class state, no I/O beyond a single ``Path.exists()``:

  * ``file_has_test_coverage`` — the canonical "does this source file have a
    sibling test" signal (``tests/test_{stem}.py`` existence). OperationAdvisor
    delegates its per-file coverage check here so the convention has a single
    definition.

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
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, Union

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


def file_has_test_coverage(
    file_path: Union[str, Path],
    repo_root: Path,
) -> bool:
    """Return True if ``file_path`` has a sibling ``tests/test_{stem}.py``.

    Canonical definition of the codebase's test-existence signal. Mirrors
    (and is delegated to by) ``OperationAdvisor._compute_test_coverage``.
    Non-``.py`` and ``test_*`` inputs are treated as covered (no penalty).
    """
    name = Path(file_path).name
    if not name.endswith(".py") or "test_" in name:
        return True
    stem = Path(file_path).stem
    return (repo_root / "tests" / f"test_{stem}.py").exists()


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
