"""Slice 12K — FileWatchGuard UNCONDITIONALLY-hard schedule budget.
=================================================================

Root-cause fix for the live session bt-iso-1783137488 census:

    [FileWatchGuard] candidate_roots=168 scheduled_roots=51
                     max_scheduled_roots=30

Slice 12J's schedule budget was SOFT: its coalescer only collapses
``NESTED_VENV_SPLIT`` groups (fat depth-2 splits). It has no lever for
a plan dominated by many *distinct* depth-1 recursive schedules, so a
large candidate set landed 70% OVER cap (51 > 30). Each scheduled root
is one ``PollingObserver`` snapshot-walk thread on macOS; 51 of them
aggregate-GIL-wedged the asyncio loop (34.9 s starvation → LoopDeadman
kill). The file's own Slice-12J comments document the same failure at
99 threads (bt-2026-05-22-232553).

Slice 12K makes the budget UNCONDITIONALLY hard in the scheduling
resolver: when venv-coalescing is insufficient, the collapsible
depth-1 siblings are folded into a single ``(watch_dir, True)``
recursive root (their only common recursive ancestor — the operator's
"in the limit: one recursive root schedule of the watch root"). The
legacy ``ignore_patterns`` post-event filter still drops re-included
excluded-subtree events.

ABSOLUTE CONSTRAINT (Slice 12I): ``PATTERN_DESCENT`` groups are NEVER
folded, and a ``watch_dir`` root is NEVER created while one is present
— a recursive root would drag the protected
``.jarvis/swe_bench_pro/worktrees`` subtree (56K-file element-web
clone) back into the poll walk. When the pattern-descent count alone
floors the cap, the pattern schedules are kept + a WARNING fires
(never violate 12I to satisfy 12J).

These tests unit-test the RESOLVER's output-set properties — they do
NOT spin real observers (per the existing narrow-scope test doctrine),
except one boot-telemetry test that drives ``_start_watchdog`` with a
spy Observer exactly as the Slice 12J suite does.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import List, Tuple
from unittest.mock import patch

import pytest

from backend.core.resilience.file_watch_guard import (
    FileWatchConfig,
    FileWatchGuard,
    _SCHEDULE_GROUP_KIND_HARD_COALESCED,
    _SCHEDULE_GROUP_KIND_PATTERN_DESCENT,
    _ResolvedSchedule,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _guard(tmp_path: Path, **config_overrides) -> FileWatchGuard:
    cfg = FileWatchConfig(**config_overrides)
    return FileWatchGuard(watch_dir=tmp_path, on_event=lambda _ev: None, config=cfg)


def _mk_venv_split(root: Path, name: str, grandchildren: int) -> int:
    """Create a depth-1 dir with a nested ``venv`` (→ NESTED_VENV_SPLIT)
    plus ``grandchildren`` non-excluded grandchildren. Returns the number
    of candidate entries this dir contributes: 1 non-recursive parent +
    ``grandchildren`` recursive grandchildren."""
    d = root / name
    d.mkdir()
    (d / "venv").mkdir()
    (d / "venv" / "bin").mkdir()
    for j in range(grandchildren):
        (d / f"mod_{j:03d}").mkdir()
    return 1 + grandchildren


def _mk_plain(root: Path, name: str) -> int:
    """Create a plain depth-1 dir (→ SIMPLE_RECURSIVE, 1 candidate)."""
    (root / name).mkdir()
    return 1


def _mk_pattern_descent(root: Path, name: str, worktree_rel: str) -> None:
    """Create a depth-1 dir that carries a pattern-excluded worktree
    subtree (→ PATTERN_DESCENT) plus a couple of normal children.

    ``worktree_rel`` is the repo-relative pattern that must be present in
    ``exclude_path_patterns`` (default or env-added) so the walker routes
    around it.
    """
    wt = root / worktree_rel / "instance_element-web-1234" / "src" / "deep"
    wt.mkdir(parents=True)
    (root / name / "sessions").mkdir(parents=True, exist_ok=True)
    (root / name / "logs").mkdir(parents=True, exist_ok=True)


def _live_census_layout_no_pattern(root: Path) -> int:
    """Replicate the bt-iso-1783137488 census SHAPE without any
    pattern-descent group (the real repo had ``skipped_by_pattern=0``):
    a handful of fat venv-splits + ~45 plain depth-1 siblings. After
    venv-coalescing this leaves (num_venv + num_plain) distinct depth-1
    recursive schedules — the 51-over-30 defect.

    Returns the candidate count.
    """
    total = 0
    total += _mk_venv_split(root, "backend", 40)   # 41
    total += _mk_venv_split(root, "tests", 40)     # 41
    total += _mk_venv_split(root, "scripts", 40)   # 41
    for i in range(45):
        total += _mk_plain(root, f"plain_{i:02d}")  # +45
    return total  # 168


# ---------------------------------------------------------------------------
# Test 1 — the live-census defect: fail-first vs fixed, NO pattern-descent
# ---------------------------------------------------------------------------


def test_live_census_168_no_pattern_collapses_under_cap(tmp_path: Path) -> None:
    """The exact bt-iso-1783137488 defect shape (168 candidates, no
    pattern-descent). Slice 12J venv-coalescing alone leaves 48 distinct
    depth-1 recursive schedules — OVER the cap of 30 (the defect). Slice
    12K's hard tier MUST fold them into a single recursive watch-root so
    ``scheduled <= cap``.
    """
    candidate = _live_census_layout_no_pattern(tmp_path)
    assert candidate == 168

    guard = _guard(tmp_path, max_scheduled_roots=30)
    result = guard._resolve_watch_paths(
        guard._resolve_excluded_dirs(),
        guard._resolve_excluded_path_patterns(),
        max_scheduled_roots=30,
    )
    assert isinstance(result, _ResolvedSchedule)
    assert result.candidate_count == 168
    # Venv tier coalesced all 3 fat splits ...
    assert result.coalesced_count == 3
    # ... which alone would leave 45 plain + 3 coalesced-venv = 48
    # distinct depth-1 recursive schedules. THAT is the pre-12K
    # scheduled floor, and it is > cap — the defect the hard tier fixes.
    assert result.hard_coalesced_count == 48, (
        "hard_coalesced_count reflects the collapsible groups the pre-12K "
        "resolver would have scheduled"
    )
    assert result.hard_coalesced_count > 30, (
        "pre-12K floor must exceed the cap, else the test does not "
        "reproduce the 51>30 defect"
    )
    # POST-FIX: the budget is now HARD.
    assert len(result.paths) <= 30
    assert len(result.paths) == 1


def test_hard_coalesced_root_is_watch_dir_recursive(tmp_path: Path) -> None:
    """The single hard-coalesced entry MUST be the watch root scheduled
    RECURSIVELY — that is what preserves event coverage: a file under any
    (now un-scheduled) source subtree still produces events via the
    recursive parent schedule (post-condition iv)."""
    _live_census_layout_no_pattern(tmp_path)
    guard = _guard(tmp_path, max_scheduled_roots=30)
    result = guard._resolve_watch_paths(
        guard._resolve_excluded_dirs(),
        guard._resolve_excluded_path_patterns(),
        max_scheduled_roots=30,
    )
    assert result.paths == [(guard.watch_dir, True)], (
        f"hard-coalesced plan must be a single recursive watch root; got "
        f"{result.paths}"
    )


# ---------------------------------------------------------------------------
# Test 2 — cap=0 legacy unbounded escape hatch preserved
# ---------------------------------------------------------------------------


def test_cap_zero_disables_hard_coalesce_legacy_unbounded(tmp_path: Path) -> None:
    """``max_scheduled_roots=0`` is the operator escape hatch — no
    coalescing at ALL, neither venv nor hard tier. The candidate plan is
    the final plan (legacy unbounded behaviour)."""
    candidate = _live_census_layout_no_pattern(tmp_path)
    guard = _guard(tmp_path, max_scheduled_roots=0)
    result = guard._resolve_watch_paths(
        guard._resolve_excluded_dirs(),
        guard._resolve_excluded_path_patterns(),
        max_scheduled_roots=0,
    )
    assert result.coalesced_count == 0
    assert result.hard_coalesced_count == 0
    assert len(result.paths) == candidate


# ---------------------------------------------------------------------------
# Test 3 — pattern-descent floors the cap (cap < pattern-descent count)
# ---------------------------------------------------------------------------


def test_pattern_descent_floors_cap_and_warns(
    tmp_path: Path, caplog,
) -> None:
    """When the pattern-descent-mandated schedule count alone exceeds the
    cap, Slice 12K MUST keep the pattern-descent schedules (never fold
    them, never create a watch-root that would re-walk the 56K subtree),
    exceed the cap, and log the ``schedule_budget_floored_by_pattern_descent``
    WARNING. This is the "never violate 12I to satisfy 12J" contract.
    """
    # Two pattern-descent groups, no venv, no plain siblings.
    _mk_pattern_descent(
        tmp_path, ".jarvis", ".jarvis/swe_bench_pro/worktrees",
    )
    _mk_pattern_descent(
        tmp_path, ".data", ".data/big/worktrees",
    )

    caplog.set_level(
        logging.WARNING, logger="backend.core.resilience.file_watch_guard",
    )
    with patch.dict(
        os.environ,
        {
            "JARVIS_FILE_WATCH_EXCLUDE_DIRS": "venv",
            "JARVIS_FILE_WATCH_EXCLUDE_PATH_PATTERNS": ".data/big/worktrees",
        },
        clear=False,
    ):
        guard = _guard(tmp_path, max_scheduled_roots=3)
        excluded = guard._resolve_excluded_dirs()
        patterns = guard._resolve_excluded_path_patterns()
        result = guard._resolve_watch_paths(
            excluded, patterns, max_scheduled_roots=3,
        )

    # pattern-descent entries dominate: with 2 groups × (~4 entries) the
    # pattern floor exceeds cap=3.
    pattern_floor = len(result.paths)
    assert pattern_floor > 3, "test needs the pattern floor to exceed cap"
    # No safe collapse happened; hard tier declined to fold.
    assert result.hard_coalesced_count == 0
    # Post-condition (i): scheduled <= max(cap, pattern_descent_count).
    assert len(result.paths) <= max(3, pattern_floor)
    # The protected worktree subtrees stay OUT of the schedule.
    for path, _rec in result.paths:
        rel = path.relative_to(guard.watch_dir).parts
        if len(rel) >= 3:
            assert rel[:3] != (".jarvis", "swe_bench_pro", "worktrees")
            assert rel[:3] != (".data", "big", "worktrees")
    # Both pattern parents survived.
    top = {p.relative_to(guard.watch_dir).parts[0] for p, _ in result.paths}
    assert ".jarvis" in top and ".data" in top
    # The floored WARNING fired.
    floored = [
        r.message for r in caplog.records
        if r.levelname == "WARNING"
        and "schedule_budget_floored_by_pattern_descent" in r.message
    ]
    assert floored, (
        "expected schedule_budget_floored_by_pattern_descent WARNING; "
        f"got {[r.message for r in caplog.records]}"
    )


def test_pattern_descent_present_never_folded_even_with_collapsible(
    tmp_path: Path, caplog,
) -> None:
    """Pattern-descent present AND many plain collapsible siblings, cap
    tight. Slice 12K MUST NOT fold the collapsible siblings into a
    watch-root (that would re-walk the protected subtree). The plain
    siblings stay as individual recursive schedules; the pattern-descent
    entries stay intact; the floored WARNING fires."""
    _mk_pattern_descent(
        tmp_path, ".jarvis", ".jarvis/swe_bench_pro/worktrees",
    )
    for i in range(20):
        _mk_plain(tmp_path, f"src_{i:02d}")

    caplog.set_level(
        logging.WARNING, logger="backend.core.resilience.file_watch_guard",
    )
    with patch.dict(
        os.environ,
        {"JARVIS_FILE_WATCH_EXCLUDE_DIRS": "venv"},
        clear=False,
    ):
        os.environ.pop("JARVIS_FILE_WATCH_EXCLUDE_PATH_PATTERNS", None)
        guard = _guard(tmp_path, max_scheduled_roots=5)
        result = guard._resolve_watch_paths(
            guard._resolve_excluded_dirs(),
            guard._resolve_excluded_path_patterns(),
            max_scheduled_roots=5,
        )

    # No watch-root fold happened.
    assert result.hard_coalesced_count == 0
    assert (guard.watch_dir, True) not in result.paths, (
        "a recursive watch-root must NOT be created while a "
        "pattern-descent group is present"
    )
    # The 20 plain siblings are all still individually scheduled.
    top = [p.relative_to(guard.watch_dir).parts[0] for p, _ in result.paths]
    assert sum(1 for t in top if t.startswith("src_")) == 20
    # Worktree subtree stays out.
    for path, _rec in result.paths:
        rel = path.relative_to(guard.watch_dir).parts
        if len(rel) >= 3:
            assert rel[:3] != (".jarvis", "swe_bench_pro", "worktrees")
    floored = [
        r.message for r in caplog.records
        if "schedule_budget_floored_by_pattern_descent" in r.message
    ]
    assert floored


# ---------------------------------------------------------------------------
# Test 4 — synthetic 168 WITH pattern-descent that DOES fit under cap
# ---------------------------------------------------------------------------


def test_census_168_with_pattern_descent_fits_and_stays_intact(
    tmp_path: Path,
) -> None:
    """A ~168-candidate plan mixing plain top-level splits, >=2
    pattern-descent groups, and fat venv-split groups. Here the bulk is
    venv-splits, so venv-coalescing fits the plan under cap WITHOUT
    needing a watch-root fold — proving the two tiers compose and that
    pattern-descent groups survive fully intact and under budget.
    """
    total = 0
    total += _mk_venv_split(tmp_path, "backend", 100)  # 101
    total += _mk_venv_split(tmp_path, "tests", 40)     # 41
    total += _mk_venv_split(tmp_path, "scripts", 12)   # 13
    _mk_pattern_descent(
        tmp_path, ".jarvis", ".jarvis/swe_bench_pro/worktrees",
    )  # 4 entries
    _mk_pattern_descent(
        tmp_path, ".data", ".data/big/worktrees",
    )  # 4 entries
    for i in range(3):
        total += _mk_plain(tmp_path, f"plain_{i}")     # 3

    with patch.dict(
        os.environ,
        {
            "JARVIS_FILE_WATCH_EXCLUDE_DIRS": "venv",
            "JARVIS_FILE_WATCH_EXCLUDE_PATH_PATTERNS": ".data/big/worktrees",
        },
        clear=False,
    ):
        guard = _guard(tmp_path, max_scheduled_roots=30)
        result = guard._resolve_watch_paths(
            guard._resolve_excluded_dirs(),
            guard._resolve_excluded_path_patterns(),
            max_scheduled_roots=30,
        )

    assert result.candidate_count > 150, "expected a ~168-candidate plan"
    # venv-coalescing was enough — no hard fold, so pattern-descent
    # groups keep their fine-grained routing.
    assert result.coalesced_count >= 2
    assert result.hard_coalesced_count == 0
    assert len(result.paths) <= 30, (
        f"scheduled {len(result.paths)} must be within cap 30"
    )
    # Both pattern parents present; worktrees excluded.
    top = {p.relative_to(guard.watch_dir).parts[0] for p, _ in result.paths}
    assert ".jarvis" in top and ".data" in top
    for path, _rec in result.paths:
        rel = path.relative_to(guard.watch_dir).parts
        if len(rel) >= 3:
            assert rel[:3] != (".jarvis", "swe_bench_pro", "worktrees")
            assert rel[:3] != (".data", "big", "worktrees")


# ---------------------------------------------------------------------------
# Test 5 — post-condition (i) property across caps (no pattern-descent)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("cap", [1, 5, 10, 25, 30, 47])
def test_scheduled_never_exceeds_cap_no_pattern(tmp_path: Path, cap: int) -> None:
    """Post-condition (i) for the no-pattern case: for EVERY cap >= 1,
    ``scheduled <= cap`` (== max(cap, pattern_descent_count=0))."""
    _live_census_layout_no_pattern(tmp_path)
    guard = _guard(tmp_path, max_scheduled_roots=cap)
    result = guard._resolve_watch_paths(
        guard._resolve_excluded_dirs(),
        guard._resolve_excluded_path_patterns(),
        max_scheduled_roots=cap,
    )
    assert len(result.paths) <= cap, (
        f"cap={cap} but scheduled={len(result.paths)}"
    )


# ---------------------------------------------------------------------------
# Test 6 — boot telemetry: hard_coalesced_roots census + WARNING
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_startup_telemetry_reports_hard_coalesced_roots(
    tmp_path: Path, caplog,
) -> None:
    """``_start_watchdog`` MUST extend the census INFO line with
    ``hard_coalesced_roots=`` and emit the ``schedule_budget_hard_coalesced``
    WARNING when the hard tier engages."""
    caplog.set_level(
        logging.INFO, logger="backend.core.resilience.file_watch_guard",
    )
    _live_census_layout_no_pattern(tmp_path)

    class _SpyObserver:
        def schedule(self, *a, **k):
            return None

        def start(self):
            return None

        def stop(self):
            return None

        def join(self, timeout=None):
            return None

    with patch(
        "watchdog.observers.Observer", return_value=_SpyObserver(),
    ), patch(
        "watchdog.observers.polling.PollingObserver",
        return_value=_SpyObserver(),
    ):
        with patch.dict(
            os.environ,
            {"JARVIS_FILE_WATCH_MAX_SCHEDULED_ROOTS": "30"},
            clear=False,
        ):
            os.environ.pop("JARVIS_FILE_WATCH_EXCLUDE_DIRS", None)
            os.environ.pop("JARVIS_FILE_WATCH_EXCLUDE_PATH_PATTERNS", None)
            guard = _guard(tmp_path, max_scheduled_roots=30)
            await guard._start_watchdog()

    info_msgs = [r.message for r in caplog.records if r.levelname == "INFO"]
    census = [
        m for m in info_msgs
        if "candidate_roots=" in m
        and "scheduled_roots=" in m
        and "hard_coalesced_roots=" in m
    ]
    assert census, (
        f"census INFO line missing hard_coalesced_roots=: {info_msgs[-2:]}"
    )
    warn_msgs = [r.message for r in caplog.records if r.levelname == "WARNING"]
    hard_warns = [
        m for m in warn_msgs if "schedule_budget_hard_coalesced" in m
    ]
    assert hard_warns, (
        f"schedule_budget_hard_coalesced WARNING not emitted: {warn_msgs}"
    )


@pytest.mark.asyncio
async def test_startup_quiet_when_no_hard_coalesce(
    tmp_path: Path, caplog,
) -> None:
    """A layout that fits under the cap MUST NOT emit the
    ``schedule_budget_hard_coalesced`` WARNING."""
    caplog.set_level(
        logging.INFO, logger="backend.core.resilience.file_watch_guard",
    )
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()

    class _SpyObserver:
        def schedule(self, *a, **k):
            return None

        def start(self):
            return None

        def stop(self):
            return None

        def join(self, timeout=None):
            return None

    with patch(
        "watchdog.observers.Observer", return_value=_SpyObserver(),
    ), patch(
        "watchdog.observers.polling.PollingObserver",
        return_value=_SpyObserver(),
    ):
        guard = _guard(tmp_path)
        await guard._start_watchdog()

    hard_warns = [
        r.message for r in caplog.records
        if r.levelname == "WARNING"
        and "schedule_budget_hard_coalesced" in r.message
    ]
    assert not hard_warns


# ---------------------------------------------------------------------------
# Test 7 — the new group kind + telemetry field exist
# ---------------------------------------------------------------------------


def test_hard_coalesced_group_kind_constant() -> None:
    """Slice 12K adds a fifth group kind for the terminal collapse
    target. It must be a distinct string literal."""
    assert _SCHEDULE_GROUP_KIND_HARD_COALESCED == "hard_coalesced_root"
    assert _SCHEDULE_GROUP_KIND_HARD_COALESCED != _SCHEDULE_GROUP_KIND_PATTERN_DESCENT


def test_resolved_schedule_carries_hard_coalesced_count() -> None:
    """The ``_ResolvedSchedule`` NamedTuple exposes the 12K telemetry
    field with a default of 0 (so legacy early-return sites stay valid)."""
    assert "hard_coalesced_count" in _ResolvedSchedule._fields
    assert _ResolvedSchedule._field_defaults.get("hard_coalesced_count") == 0
