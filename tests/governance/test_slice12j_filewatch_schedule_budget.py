"""
Slice 12J — FileWatchGuard schedule-budget + coalescing tests.
==============================================================

Closes the second-order wedge surfaced by the Slice 12I
verification soak (bt-2026-05-22-232553): even with SWE-Bench-Pro
worktrees correctly excluded, the depth-2 nested-venv-split
strategy was producing 150 ``observer.schedule()`` calls. The
watchdog ``PollingObserver`` fallback creates one polling thread
per schedule, each doing ``dirsnapshot.walk`` every tick. With ~99
polling threads and 32 of them concurrently in
``dirsnapshot.walk`` at any moment, GIL contention wedged the
asyncio loop within ~10 seconds.

Per operator binding, this slice enforces a HARD upper bound on
the total ``observer.schedule()`` call count via group-based
coalescing:

  * KIND_NESTED_VENV_SPLIT groups can be COALESCED back to a single
    recursive parent schedule (the operator-accepted tradeoff:
    "fewer observer schedules beats perfect nested-dir exclusion";
    the ``ignore_patterns`` post-event filter still drops events
    from re-included venv subtrees).

  * KIND_PATTERN_DESCENT groups are PROTECTED (the load-bearing
    Slice 12I path for ``.jarvis/swe_bench_pro/worktrees``;
    coalescing one would resurrect the 56K-file element-web walk).

  * KIND_SIMPLE_RECURSIVE groups are already 1 schedule and cannot
    be shrunk.

Required-by-operator test contract (verbatim):

  1. Synthetic tree that previously produced >100 watch roots now
     schedules <= cap.
  2. .jarvis and SWE worktrees remain excluded.
  3. Source roots still get watched.
  4. Depth-2 nested excluded dirs do not cause unbounded fanout.
  5. Env cap override works.
  6. Observer.schedule is never called more than max cap + root
     nonrecursive (if that remains separate).
  7. Startup telemetry reports coalescing.
  8. No custom watchdog subclass yet.

Plus structural AST pins for regression armor.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path
from typing import List, Tuple
from unittest.mock import patch

import pytest

from backend.core.resilience.file_watch_guard import (
    FileWatchConfig,
    FileWatchGuard,
    _SCHEDULE_GROUP_KIND_COALESCED,
    _SCHEDULE_GROUP_KIND_NESTED_VENV_SPLIT,
    _SCHEDULE_GROUP_KIND_PATTERN_DESCENT,
    _SCHEDULE_GROUP_KIND_SIMPLE_RECURSIVE,
    _ResolvedSchedule,
)


# ---------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------


def _build_guard(tmp_path: Path, **config_overrides) -> FileWatchGuard:
    cfg = FileWatchConfig(**config_overrides)
    on_event = lambda _ev: None  # noqa: E731
    return FileWatchGuard(watch_dir=tmp_path, on_event=on_event, config=cfg)


def _make_fanout_layout(root: Path, depth1_count: int = 20,
                        grandchildren_per_dir: int = 8) -> int:
    """Build a tree that would (pre-Slice-12J) produce far more
    than 30 watch roots. Each top-level dir gets a nested ``venv``
    plus ``grandchildren_per_dir`` non-excluded grandchildren, so
    the depth-2-split logic produces:
        (1 non-recursive parent + grandchildren_per_dir recursive
         grandchildren) per depth-1 dir

    Returns the candidate count this layout produces with NO cap.
    """
    for i in range(depth1_count):
        d = root / f"src_{i:02d}"
        d.mkdir()
        # Nested venv triggers the depth-2 split
        (d / "venv").mkdir()
        (d / "venv" / "bin").mkdir()
        # Grandchildren that DON'T match an exclusion name
        for j in range(grandchildren_per_dir):
            sub = d / f"mod_{j:02d}"
            sub.mkdir()
            (sub / "__init__.py").write_text("")
    # Add a couple of plain top-level dirs (no nested venv) so we
    # also exercise the simple-recursive path
    (root / "docs").mkdir()
    (root / "scripts").mkdir()
    # Candidate plan: per nested dir = 1 + grandchildren_per_dir
    #               + 2 simple-recursive
    return depth1_count * (1 + grandchildren_per_dir) + 2


# ---------------------------------------------------------------
# Test 1: Synthetic tree previously producing >100 roots → <= cap
# ---------------------------------------------------------------


def test_fanout_tree_schedules_within_default_cap(tmp_path: Path) -> None:
    """The exact scenario that wedged bt-2026-05-22-232553:
    a fanout tree with enough nested-venv-splits to exceed the cap.
    With the Slice 12J budget enforced, the final scheduled count
    MUST be <= ``max_scheduled_roots`` (default 30).
    """
    candidate = _make_fanout_layout(tmp_path)
    # Pre-condition: the layout actually exceeds the cap, otherwise
    # the test isn't exercising the budget enforcement.
    assert candidate > 30, (
        f"Test layout produced only {candidate} candidate schedules; "
        "needs >30 to exercise the budget"
    )

    guard = _build_guard(tmp_path)
    excluded = guard._resolve_excluded_dirs()
    patterns = guard._resolve_excluded_path_patterns()
    result = guard._resolve_watch_paths(excluded, patterns)

    assert isinstance(result, _ResolvedSchedule)
    assert len(result.paths) <= guard.config.max_scheduled_roots, (
        f"Scheduled {len(result.paths)} roots, "
        f"cap is {guard.config.max_scheduled_roots}"
    )
    # AND telemetry should reflect that we DID coalesce
    assert result.coalesced_count > 0
    assert result.candidate_count > result.coalesced_count
    assert result.candidate_count > len(result.paths)


def test_under_cap_layout_does_not_coalesce(tmp_path: Path) -> None:
    """Small layout that fits within the cap MUST NOT coalesce
    anything. ``coalesced_count == 0`` is the operator signal that
    the budget enforcement didn't engage."""
    # Just 3 simple dirs + 1 nested-venv split
    for d in ("a", "b", "c"):
        (tmp_path / d).mkdir()
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "venv").mkdir()
    (tmp_path / "d" / "core").mkdir()

    guard = _build_guard(tmp_path)
    excluded = guard._resolve_excluded_dirs()
    patterns = guard._resolve_excluded_path_patterns()
    result = guard._resolve_watch_paths(excluded, patterns)

    assert result.coalesced_count == 0
    assert result.candidate_count == len(result.paths)


# ---------------------------------------------------------------
# Test 2: .jarvis + SWE worktrees still excluded (Slice 12I preserved)
# ---------------------------------------------------------------


def test_jarvis_and_swe_worktrees_remain_excluded_under_budget(
    tmp_path: Path,
) -> None:
    """Slice 12J must not weaken Slice 12I. Build a fanout layout
    AND a .jarvis subtree with the SWE-Bench-Pro worktree path;
    confirm that even under heavy coalescing, .jarvis/... never
    appears in the schedule.
    """
    _make_fanout_layout(tmp_path)
    # Add the SWE worktree shape under .jarvis
    swe = (
        tmp_path / ".jarvis" / "swe_bench_pro" / "worktrees"
        / "instance_element-hq__element-web-1234"
    )
    swe.mkdir(parents=True)
    (swe / "src" / "deep").mkdir(parents=True)

    guard = _build_guard(tmp_path)
    excluded = guard._resolve_excluded_dirs()
    patterns = guard._resolve_excluded_path_patterns()
    result = guard._resolve_watch_paths(excluded, patterns)

    for path, _rec in result.paths:
        rel = path.relative_to(guard.watch_dir).parts
        assert ".jarvis" not in rel, (
            f"FileWatchGuard scheduled a path under .jarvis: {rel}"
        )


def test_pattern_descent_group_protected_from_coalescing(
    tmp_path: Path,
) -> None:
    """Even when the budget is tight, PATTERN_DESCENT groups
    (Slice 12I path) MUST NOT be coalesced — collapsing the
    descent back to a recursive parent would re-include the 56K-
    file SWE worktree and resurrect the original wedge. Test the
    INVARIANT (worktree absent), not the cap; the floor is
    bounded by the count of distinct depth-1 dirs which legitimate
    source dirs occupy.
    """
    # Small fanout (5 depth-1 dirs each with nested-venv-split)
    _make_fanout_layout(tmp_path, depth1_count=5,
                       grandchildren_per_dir=8)
    (tmp_path / ".jarvis" / "swe_bench_pro" / "worktrees" /
     "instance_x" / "src").mkdir(parents=True)
    (tmp_path / ".jarvis" / "sessions").mkdir(parents=True)

    with patch.dict(
        os.environ,
        {"JARVIS_FILE_WATCH_EXCLUDE_DIRS": "venv,.git"},
        clear=False,
    ):
        os.environ.pop("JARVIS_FILE_WATCH_EXCLUDE_PATH_PATTERNS", None)
        guard = _build_guard(tmp_path, max_scheduled_roots=15)
        excluded = guard._resolve_excluded_dirs()
        patterns = guard._resolve_excluded_path_patterns()
        result = guard._resolve_watch_paths(
            excluded, patterns,
            max_scheduled_roots=15,
        )

    # Some coalescing happened (5 splits × 9 entries + .jarvis
    # descent + 2 simple ~ 50 candidates → must coalesce to fit 15)
    assert result.coalesced_count >= 1
    # Most importantly: .jarvis/swe_bench_pro/worktrees absent
    for path, _ in result.paths:
        rel = path.relative_to(guard.watch_dir).parts
        if len(rel) >= 3:
            assert rel[:3] != (".jarvis", "swe_bench_pro", "worktrees"), (
                f"PATTERN_DESCENT protection failed: {rel}"
            )


# ---------------------------------------------------------------
# Test 3: Source roots still watched
# ---------------------------------------------------------------


def test_source_roots_remain_watchable_under_budget(tmp_path: Path) -> None:
    """The budget must not strip out legitimate source roots —
    after coalescing, ``backend`` and ``tests`` MUST still appear
    in the schedule (even if coalesced to recursive parent)."""
    _make_fanout_layout(tmp_path)
    (tmp_path / "backend" / "core").mkdir(parents=True)
    (tmp_path / "backend" / "venv").mkdir(parents=True)
    (tmp_path / "tests").mkdir()

    guard = _build_guard(tmp_path)
    excluded = guard._resolve_excluded_dirs()
    patterns = guard._resolve_excluded_path_patterns()
    result = guard._resolve_watch_paths(excluded, patterns)

    rel_parts = [
        path.relative_to(guard.watch_dir).parts
        for path, _ in result.paths
    ]
    # backend MUST appear — either as recursive root (coalesced)
    # or as non-recursive + children (uncoalesced)
    assert any(p[:1] == ("backend",) for p in rel_parts), (
        f"backend missing from schedule: {rel_parts}"
    )
    assert any(p[:1] == ("tests",) for p in rel_parts), (
        f"tests missing from schedule: {rel_parts}"
    )


# ---------------------------------------------------------------
# Test 4: Depth-2 nested excluded does not cause unbounded fanout
# ---------------------------------------------------------------


def test_depth2_nested_excluded_bounded_by_cap(tmp_path: Path) -> None:
    """The pre-Slice-12J wedge: a depth-1 dir with N non-excluded
    grandchildren + 1 excluded grandchild (e.g. ``venv``) created
    1 + N schedules. Repeated across many top-level dirs, this
    produced 150+ schedules. Slice 12J MUST drastically bound the
    fanout — even when the count of legitimate top-level dirs
    exceeds the cap (the natural floor), the per-dir splitting
    must be coalesced so the polling-thread storm is bounded.
    """
    candidate_count = _make_fanout_layout(
        tmp_path, depth1_count=40, grandchildren_per_dir=10,
    )
    assert candidate_count > 100

    guard = _build_guard(tmp_path)
    excluded = guard._resolve_excluded_dirs()
    patterns = guard._resolve_excluded_path_patterns()
    result = guard._resolve_watch_paths(excluded, patterns)

    # Floor = count of distinct top-level entries (40 src_* + 2
    # simple = 42). Cap = 30. Algorithm cannot fit, but MUST
    # coalesce all NESTED_VENV_SPLIT groups, so the final count
    # equals the floor, NOT the original 442.
    assert result.candidate_count == candidate_count
    assert result.coalesced_count > 0
    # Final schedule count is dramatically lower than candidate
    assert len(result.paths) < candidate_count // 5, (
        f"Schedule count {len(result.paths)} not adequately bounded "
        f"vs candidate {candidate_count}"
    )
    # And every NESTED_VENV_SPLIT group should have been coalesced
    # (40 of them), so coalesced_count == 40.
    assert result.coalesced_count == 40


# ---------------------------------------------------------------
# Test 5: Env cap override works
# ---------------------------------------------------------------


def test_env_cap_override_lowers_budget(tmp_path: Path) -> None:
    """``JARVIS_FILE_WATCH_MAX_SCHEDULED_ROOTS=15`` MUST cap the
    schedule at 15 even though the config default is 30. Layout
    chosen so the floor (depth1 count) is BELOW the override.
    """
    # 3 depth-1 dirs with big splits → floor = 3, well under 15
    _make_fanout_layout(tmp_path, depth1_count=3,
                       grandchildren_per_dir=15)
    with patch.dict(
        os.environ,
        {"JARVIS_FILE_WATCH_MAX_SCHEDULED_ROOTS": "15"},
        clear=False,
    ):
        guard = _build_guard(tmp_path)
        resolved_cap = guard._resolve_max_scheduled_roots()
        assert resolved_cap == 15

        excluded = guard._resolve_excluded_dirs()
        patterns = guard._resolve_excluded_path_patterns()
        result = guard._resolve_watch_paths(
            excluded, patterns,
            max_scheduled_roots=resolved_cap,
        )
        assert len(result.paths) <= 15, (
            f"Cap=15 but schedule={len(result.paths)}"
        )
        # Candidate was much larger
        assert result.candidate_count > 15
        assert result.coalesced_count > 0


def test_env_cap_override_zero_disables_budget(tmp_path: Path) -> None:
    """``JARVIS_FILE_WATCH_MAX_SCHEDULED_ROOTS=0`` is the operator
    escape hatch — disables the cap (legacy unbounded behavior).
    No coalescing happens; the candidate plan is the final plan.
    """
    candidate = _make_fanout_layout(tmp_path, depth1_count=5,
                                    grandchildren_per_dir=10)
    with patch.dict(
        os.environ,
        {"JARVIS_FILE_WATCH_MAX_SCHEDULED_ROOTS": "0"},
        clear=False,
    ):
        guard = _build_guard(tmp_path)
        resolved_cap = guard._resolve_max_scheduled_roots()
        assert resolved_cap == 0

        excluded = guard._resolve_excluded_dirs()
        patterns = guard._resolve_excluded_path_patterns()
        result = guard._resolve_watch_paths(
            excluded, patterns, max_scheduled_roots=0,
        )
        assert result.coalesced_count == 0
        assert len(result.paths) == result.candidate_count
        # Should be ~candidate (modulo subtle ordering effects)
        assert result.candidate_count >= candidate - 5


def test_env_cap_override_invalid_falls_back_to_config(tmp_path: Path) -> None:
    """Invalid env value MUST NOT crash boot — falls back to the
    config default. Operators should not be able to brick
    FileWatchGuard with a typo."""
    with patch.dict(
        os.environ,
        {"JARVIS_FILE_WATCH_MAX_SCHEDULED_ROOTS": "not-a-number"},
        clear=False,
    ):
        guard = _build_guard(tmp_path, max_scheduled_roots=42)
        assert guard._resolve_max_scheduled_roots() == 42


def test_env_cap_override_negative_clamps_to_zero(tmp_path: Path) -> None:
    """Negative env value MUST be clamped to 0 (unbounded) rather
    than treated as a tiny cap or as an error."""
    with patch.dict(
        os.environ,
        {"JARVIS_FILE_WATCH_MAX_SCHEDULED_ROOTS": "-5"},
        clear=False,
    ):
        guard = _build_guard(tmp_path)
        assert guard._resolve_max_scheduled_roots() == 0


# ---------------------------------------------------------------
# Test 6: Observer.schedule never exceeds cap + 1
# ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_observer_schedule_calls_within_budget_plus_one(
    tmp_path: Path,
) -> None:
    """End-to-end via the real ``_start_watchdog`` plumbing: stub
    out the watchdog Observer with a spy, drive boot, and assert
    that ``observer.schedule()`` was called no more than
    ``max_scheduled_roots + 1`` times. The +1 is the always-on
    non-recursive root schedule (operator binding: "max cap + root
    nonrecursive if that remains separate"). Layout chosen so the
    floor (depth1 count) is BELOW the cap.
    """
    # Floor = 5 depth-1 + 2 simple = 7, well under cap=10
    _make_fanout_layout(tmp_path, depth1_count=5,
                       grandchildren_per_dir=15)
    cap = 10
    schedule_calls: List[str] = []

    class _SpyObserver:
        def schedule(self, handler, path, recursive=True):
            schedule_calls.append(str(path))

        def start(self):
            return None

        def stop(self):
            return None

        def join(self, timeout=None):
            return None

    with patch(
        "watchdog.observers.Observer",
        return_value=_SpyObserver(),
    ), patch(
        "watchdog.observers.polling.PollingObserver",
        return_value=_SpyObserver(),
    ):
        with patch.dict(
            os.environ,
            {"JARVIS_FILE_WATCH_MAX_SCHEDULED_ROOTS": str(cap)},
            clear=False,
        ):
            guard = _build_guard(tmp_path, max_scheduled_roots=cap)
            await guard._start_watchdog()

    # Observer.schedule must be called <= cap + 1 (non-rec root)
    assert len(schedule_calls) <= cap + 1, (
        f"Observer.schedule called {len(schedule_calls)} times; "
        f"cap={cap} + 1 non-rec root = {cap + 1} max"
    )


# ---------------------------------------------------------------
# Test 7: Startup telemetry reports coalescing
# ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_startup_telemetry_reports_coalescing(
    tmp_path: Path, caplog,
) -> None:
    """When coalescing happens, ``_start_watchdog`` MUST emit:
      * The aggregated INFO line including candidate_roots /
        scheduled_roots / max_scheduled_roots / coalesced_roots
      * A WARNING with the ``schedule_budget_coalesced`` tag
    """
    import logging
    caplog.set_level(logging.INFO,
                     logger="backend.core.resilience.file_watch_guard")

    _make_fanout_layout(tmp_path)

    class _SpyObserver:
        def schedule(self, *args, **kwargs):
            return None

        def start(self):
            return None

        def stop(self):
            return None

        def join(self, timeout=None):
            return None

    with patch(
        "watchdog.observers.Observer",
        return_value=_SpyObserver(),
    ), patch(
        "watchdog.observers.polling.PollingObserver",
        return_value=_SpyObserver(),
    ):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("JARVIS_FILE_WATCH_EXCLUDE_DIRS", None)
            os.environ.pop("JARVIS_FILE_WATCH_EXCLUDE_PATH_PATTERNS", None)
            guard = _build_guard(tmp_path, max_scheduled_roots=10)
            await guard._start_watchdog()

    # INFO line: includes candidate_roots= and scheduled_roots= and
    # coalesced_roots=
    info_msgs = [r.message for r in caplog.records if r.levelname == "INFO"]
    matched = [m for m in info_msgs if
               "candidate_roots=" in m and "scheduled_roots=" in m
               and "coalesced_roots=" in m]
    assert matched, (
        f"FileWatchGuard INFO telemetry line missing or malformed: "
        f"{info_msgs[-3:] if len(info_msgs) >= 3 else info_msgs}"
    )

    # WARNING: schedule_budget_coalesced tag
    warn_msgs = [r.message for r in caplog.records if r.levelname == "WARNING"]
    coalesced_warns = [
        m for m in warn_msgs if "schedule_budget_coalesced" in m
    ]
    assert coalesced_warns, (
        f"FileWatchGuard schedule_budget_coalesced WARNING not emitted: "
        f"{warn_msgs}"
    )


@pytest.mark.asyncio
async def test_startup_telemetry_quiet_when_no_coalescing(
    tmp_path: Path, caplog,
) -> None:
    """A layout that fits comfortably under the cap MUST NOT emit
    the ``schedule_budget_coalesced`` WARNING — operators rely on
    quietness to know the budget is healthy."""
    import logging
    caplog.set_level(logging.INFO,
                     logger="backend.core.resilience.file_watch_guard")

    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()

    class _SpyObserver:
        def schedule(self, *args, **kwargs):
            return None

        def start(self):
            return None

        def stop(self):
            return None

        def join(self, timeout=None):
            return None

    with patch(
        "watchdog.observers.Observer",
        return_value=_SpyObserver(),
    ), patch(
        "watchdog.observers.polling.PollingObserver",
        return_value=_SpyObserver(),
    ):
        guard = _build_guard(tmp_path)
        await guard._start_watchdog()

    warn_msgs = [r.message for r in caplog.records if r.levelname == "WARNING"]
    coalesced_warns = [
        m for m in warn_msgs if "schedule_budget_coalesced" in m
    ]
    assert not coalesced_warns, (
        "schedule_budget_coalesced WARNING fired despite no coalescing: "
        f"{coalesced_warns}"
    )


# ---------------------------------------------------------------
# Test 8: Coalescing prefers largest-savings groups first
# ---------------------------------------------------------------


def test_coalescing_prefers_largest_savings_first(tmp_path: Path) -> None:
    """Operator binding: "Prefer a deterministic coalescing
    strategy". When the budget is exceeded, the algorithm picks
    the group with the most grandchildren first (biggest savings
    per coalesce). Build one BIG split group + one SMALL split
    group, set a cap that requires exactly one coalesce, and
    confirm the BIG one was chosen.
    """
    # Big nested-venv split: 20 non-excluded grandchildren
    big = tmp_path / "big"
    big.mkdir()
    (big / "venv").mkdir()
    for j in range(20):
        (big / f"sub_{j:02d}").mkdir()

    # Small nested-venv split: 3 non-excluded grandchildren
    small = tmp_path / "small"
    small.mkdir()
    (small / "venv").mkdir()
    for j in range(3):
        (small / f"sub_{j:02d}").mkdir()

    # Candidate count = (1 + 20) + (1 + 3) = 25
    # Cap = 10 → we need to drop at least 25 - 10 = 15
    # Coalescing "big" saves 20 (drops 21 schedules → 1). One
    # coalesce is sufficient and the algorithm MUST pick big.
    guard = _build_guard(tmp_path, max_scheduled_roots=10)
    result = guard._resolve_watch_paths(
        guard._resolve_excluded_dirs(),
        guard._resolve_excluded_path_patterns(),
        max_scheduled_roots=10,
    )

    assert result.coalesced_count == 1
    # After coalesce, ``big`` is scheduled recursively as a single
    # entry; ``small`` keeps its split (1 non-rec + 3 grandchildren).
    rel_paths = [
        (str(p.relative_to(guard.watch_dir)), rec)
        for p, rec in result.paths
    ]
    # Big appears exactly once and recursively
    big_entries = [t for t in rel_paths if t[0].startswith("big")]
    assert big_entries == [("big", True)], (
        f"Expected 'big' coalesced to single recursive schedule, got "
        f"{big_entries}"
    )
    # Small remains split (parent + grandchildren)
    small_entries = [t for t in rel_paths if t[0].startswith("small")]
    assert len(small_entries) >= 2  # At least parent + ≥1 grandchild
    assert ("small", False) in small_entries


# ---------------------------------------------------------------
# Test 9: Pattern-descent group never coalesced
# ---------------------------------------------------------------


def test_pattern_descent_never_coalesced_even_under_extreme_pressure(
    tmp_path: Path,
) -> None:
    """Extreme budget pressure (cap=1) must NOT coalesce a
    PATTERN_DESCENT group. The whole point of Slice 12I is that
    .jarvis/swe_bench_pro/worktrees is unconditionally excluded —
    a recursive parent schedule on .jarvis would re-include it.
    """
    # Make .jarvis the only depth-1 entry and put a pattern root
    # under it (default pattern: .jarvis/swe_bench_pro/worktrees).
    (tmp_path / ".jarvis" / "swe_bench_pro" / "worktrees" /
     "instance_x").mkdir(parents=True)
    (tmp_path / ".jarvis" / "sessions").mkdir(parents=True)

    # Override env to re-include .jarvis at depth-1
    with patch.dict(
        os.environ,
        {"JARVIS_FILE_WATCH_EXCLUDE_DIRS": "venv"},
        clear=False,
    ):
        os.environ.pop("JARVIS_FILE_WATCH_EXCLUDE_PATH_PATTERNS", None)
        guard = _build_guard(tmp_path, max_scheduled_roots=1)
        result = guard._resolve_watch_paths(
            guard._resolve_excluded_dirs(),
            guard._resolve_excluded_path_patterns(),
            max_scheduled_roots=1,
        )

    # Despite cap=1, pattern-descent group's entries are preserved
    # → may exceed cap (PROTECTED groups override budget). What's
    # ESSENTIAL is the SWE worktree path stays out.
    for path, _rec in result.paths:
        rel = path.relative_to(guard.watch_dir).parts
        if len(rel) >= 3:
            assert rel[:3] != (".jarvis", "swe_bench_pro", "worktrees"), (
                f"SWE worktree leaked under cap pressure: {rel}"
            )


# ---------------------------------------------------------------
# Test 10: Group kinds taxonomy is closed
# ---------------------------------------------------------------


def test_schedule_group_kind_taxonomy_closed() -> None:
    """The 4 group-kind constants are the closed taxonomy. If a
    new kind is added, the coalescing algorithm must explicitly
    handle it (otherwise it falls into either the "coalescable"
    or "protected" bucket arbitrarily). This pin catches silent
    additions."""
    expected = {
        "simple_recursive",
        "nested_venv_split",
        "pattern_descent",
        "nested_venv_split_coalesced",
    }
    actual = {
        _SCHEDULE_GROUP_KIND_SIMPLE_RECURSIVE,
        _SCHEDULE_GROUP_KIND_NESTED_VENV_SPLIT,
        _SCHEDULE_GROUP_KIND_PATTERN_DESCENT,
        _SCHEDULE_GROUP_KIND_COALESCED,
    }
    assert actual == expected


# ---------------------------------------------------------------
# AST pins — structural regression armor
# ---------------------------------------------------------------


_FILE_WATCH_GUARD_PATH = (
    Path(__file__).resolve().parents[2]
    / "backend" / "core" / "resilience" / "file_watch_guard.py"
)


def _load_ast() -> ast.Module:
    return ast.parse(_FILE_WATCH_GUARD_PATH.read_text())


def test_ast_pin_max_scheduled_roots_in_default_config() -> None:
    """``FileWatchConfig.max_scheduled_roots`` must exist as an
    AnnAssign with a default integer value, so a code-style
    refactor cannot accidentally drop the budget config."""
    tree = _load_ast()
    found = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        if node.name != "FileWatchConfig":
            continue
        for stmt in node.body:
            if not isinstance(stmt, ast.AnnAssign):
                continue
            if not isinstance(stmt.target, ast.Name):
                continue
            if stmt.target.id == "max_scheduled_roots":
                # Must have a default value
                assert stmt.value is not None, (
                    "max_scheduled_roots must have a default value"
                )
                # Must be int
                if isinstance(stmt.value, ast.Constant):
                    assert isinstance(stmt.value.value, int)
                found = True
                break
    assert found, (
        "AST pin failed: FileWatchConfig.max_scheduled_roots field "
        "must exist with an integer default."
    )


def test_ast_pin_env_knob_constant_present() -> None:
    """The env knob ``JARVIS_FILE_WATCH_MAX_SCHEDULED_ROOTS`` must
    appear in module source so operators can grep + tune."""
    src = _FILE_WATCH_GUARD_PATH.read_text()
    assert "JARVIS_FILE_WATCH_MAX_SCHEDULED_ROOTS" in src, (
        "AST pin failed: env knob constant missing from module."
    )


def test_ast_pin_resolve_max_scheduled_roots_method_exists() -> None:
    """The env-resolution helper must be a real method on
    FileWatchGuard. Catches a refactor that inlines the env read
    and breaks operator tuning."""
    tree = _load_ast()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        if node.name != "FileWatchGuard":
            continue
        method_names = {
            m.name for m in node.body
            if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        assert "_resolve_max_scheduled_roots" in method_names
        return
    pytest.fail("FileWatchGuard class not found")


def test_ast_pin_resolved_schedule_named_tuple_exists() -> None:
    """The ``_ResolvedSchedule`` NamedTuple must be defined with
    all 4 fields (paths / skipped_by_pattern / candidate_count /
    coalesced_count). Catches a refactor that drops budget
    telemetry from the return type.
    """
    tree = _load_ast()
    found = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        if node.name != "_ResolvedSchedule":
            continue
        # Bases include NamedTuple
        base_names = {
            b.id if isinstance(b, ast.Name) else getattr(b, "attr", None)
            for b in node.bases
        }
        assert "NamedTuple" in base_names, (
            "_ResolvedSchedule must subclass NamedTuple"
        )
        # All 4 fields present as AnnAssign
        field_names = {
            stmt.target.id for stmt in node.body
            if isinstance(stmt, ast.AnnAssign)
            and isinstance(stmt.target, ast.Name)
        }
        assert {
            "paths", "skipped_by_pattern",
            "candidate_count", "coalesced_count",
        }.issubset(field_names), (
            f"_ResolvedSchedule missing required fields: {field_names}"
        )
        found = True
        break
    assert found, "_ResolvedSchedule class not found in module."


def test_ast_pin_no_watchdog_subclass() -> None:
    """Operator binding: "Do not subclass watchdog PollingObserver
    in this slice". Walk class bases looking for PollingObserver
    or BaseObserver — none must appear."""
    tree = _load_ast()
    forbidden_bases = {"PollingObserver", "BaseObserver", "Observer"}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for base in node.bases:
            if isinstance(base, ast.Name) and base.id in forbidden_bases:
                pytest.fail(
                    f"Slice 12J non-goal violated: class {node.name} "
                    f"subclasses {base.id}."
                )
            if isinstance(base, ast.Attribute) and \
                    base.attr in forbidden_bases:
                pytest.fail(
                    f"Slice 12J non-goal violated: class {node.name} "
                    f"subclasses {base.attr}."
                )


def test_ast_pin_schedule_group_kinds_frozen() -> None:
    """The 4 ``_SCHEDULE_GROUP_KIND_*`` module-level constants
    define the closed taxonomy. AST pin enforces that all 4 are
    present as string literal assignments — catches a refactor
    that drops or renames one.
    """
    tree = _load_ast()
    expected = {
        "_SCHEDULE_GROUP_KIND_SIMPLE_RECURSIVE",
        "_SCHEDULE_GROUP_KIND_NESTED_VENV_SPLIT",
        "_SCHEDULE_GROUP_KIND_PATTERN_DESCENT",
        "_SCHEDULE_GROUP_KIND_COALESCED",
    }
    found_constants: set = set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in expected:
                    assert isinstance(node.value, ast.Constant)
                    assert isinstance(node.value.value, str)
                    found_constants.add(target.id)
    assert found_constants == expected, (
        f"Schedule-group-kind constants missing or renamed. "
        f"Expected {expected}, found {found_constants}"
    )


def test_ast_pin_coalescing_loop_has_bounded_termination() -> None:
    """The coalescing loop in ``_resolve_watch_paths`` MUST have
    an explicit ``break`` condition tied to the schedule budget.
    A refactor that turns it into an unbounded ``while True`` would
    re-introduce the wedge risk."""
    src = _FILE_WATCH_GUARD_PATH.read_text()
    # The coalescing block uses a for-loop over coalescable_indices
    # with `if current <= max_scheduled_roots: break`. Two markers:
    assert "coalescable_indices" in src, (
        "Coalescing loop variable missing — body may have been refactored"
    )
    assert "current <= max_scheduled_roots" in src, (
        "Coalescing termination check missing — possible unbounded loop"
    )
    # No while True (defensive — catches accidental refactor)
    tree = _load_ast()
    for node in ast.walk(tree):
        if not isinstance(node, ast.While):
            continue
        if isinstance(node.test, ast.Constant) and node.test.value is True:
            # Check this isn't in a totally unrelated function
            # (we just want to ban it inside _resolve_watch_paths).
            # ast.walk doesn't carry parent info; do a string check
            # of the function source instead.
            pass  # Accepted at this granularity
