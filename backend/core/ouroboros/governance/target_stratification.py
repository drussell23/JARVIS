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
from typing import Union

# Defaults are module constants so tests + callers share one source of truth.
DEFAULT_PENALTY_ALPHA: float = 0.75
DEFAULT_PENALTY_MAX_LINES: int = 2000


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
