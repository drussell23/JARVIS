"""Slice 3F — Async Engine Hardening: kill the rglob loop wedge.

Closes bt-2026-05-25-050449: ``crawl_rust_subsystems`` in
``source_crawlers.py:286`` used ``Path.rglob("Cargo.toml")`` which
recursively walks the ENTIRE directory tree (descends INTO
``target/``, ``node_modules/``, ``.git/``, filters them out AFTER the
walk). On a JARVIS repo (~50,664 dirs total, only ~4,068 first-party),
the walk took ~914 seconds and wedged the asyncio event loop until
``LoopDeadman`` fired ``os._exit(75)``.

# Why Slice 3B's defenses didn't save us

Slice 3B's ``_chunk_timeout`` uses ``asyncio.wait_for`` which
schedules its timeout callback ON the event loop. When the loop is
wedged in synchronous I/O, the timer callback never fires. **Time-
based defenses are structurally defeated by event-loop wedges.** The
cure is to never block the loop in the first place — or in this case,
to make the blocking work fast enough that "blocking on the loop" is
indistinguishable from "non-blocking".

# Fix mechanism — three-part structural defense

## Part 1 — In-place dir pruning (the load-bearing fix)

Replace ``sorted(search_root.rglob("Cargo.toml"))`` with
``_walk_for_filename(search_root, "Cargo.toml", _SKIP_DIRNAMES, deadline)``
which uses ``os.walk()`` and prunes skip dirs IN-PLACE via
``dirnames[:] = [d for d in dirnames if d not in skip_dirnames]``.
``os.walk`` honors this for descent control — pruned dirs are NEVER
entered.

Empirical impact: ~860× speedup on this repo (1061ms vs 914000ms).

## Part 2 — Bounded walk deadline (defense-in-depth)

The ``_walk_for_filename`` helper accepts ``max_walk_s`` and checks
``time.monotonic() > deadline`` at every dirpath. On overrun, returns
whatever was collected so far + emits a WARNING log. Better a
truncated crate map than a multi-minute wedge.

Env: ``JARVIS_STRATEGIC_CRAWL_MAX_WALK_S`` (default 10s, floor 0.5s).

## Part 3 — Skip dirs as DIRNAMES not SUBSTRINGS

Pre-Slice-3F skip filter used ``"/target/", "/.git/"`` substrings
applied AFTER the walk. Post-Slice-3F uses bare dirnames in a
``frozenset`` matched against ``os.walk``'s ``dirnames`` list. This
is structurally pre-walk (descent prevented) instead of post-walk
(traversal complete but result discarded).

# Test surface (3 AST pins + 6 spine)
"""

from __future__ import annotations

import ast
import os
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[2]
CRAWLER_FILE = (
    REPO_ROOT / "backend" / "core" / "ouroboros" / "roadmap" / "source_crawlers.py"
)


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(), filename=str(path))


# ──────────────────────────────────────────────────────────────────────
# AST PINS — 3
# ──────────────────────────────────────────────────────────────────────


def test_ast_pin_crawl_rust_subsystems_does_not_use_rglob() -> None:
    """``crawl_rust_subsystems`` body must NOT contain the
    ``rglob("Cargo.toml")`` invocation — that was the
    bt-2026-05-25-050449 wedge. The function body should compose
    ``_walk_for_filename`` instead."""
    tree = _parse(CRAWLER_FILE)
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        if node.name != "crawl_rust_subsystems":
            continue
        body_src = ast.unparse(node)
        # The actual offending CALL was rglob("Cargo.toml").
        # Comments mentioning rglob (rationale docs) are OK.
        assert "rglob(\"Cargo.toml\")" not in body_src, (
            "crawl_rust_subsystems still calls rglob('Cargo.toml') — "
            "the bt-2026-05-25-050449 event-loop wedge trap is open."
        )
        assert "_walk_for_filename" in body_src, (
            "crawl_rust_subsystems does not compose _walk_for_filename "
            "— Slice 3F load-bearing helper not wired."
        )
        return
    raise AssertionError(
        "crawl_rust_subsystems function not found in source_crawlers.py"
    )


def test_ast_pin_walk_for_filename_uses_in_place_prune() -> None:
    """``_walk_for_filename`` body MUST contain the in-place dirnames
    slice assignment (``dirnames[:] = ...``). Without this, ``os.walk``
    still descends into skip dirs — the fix is structurally inert."""
    tree = _parse(CRAWLER_FILE)
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        if node.name != "_walk_for_filename":
            continue
        body_src = ast.unparse(node)
        # The in-place slice pattern is the ONLY way to control os.walk
        # descent. Anything else (filter-after-walk, return-list-from-fn)
        # silently fails to prune.
        assert "dirnames[:]" in body_src, (
            "_walk_for_filename does not use in-place dirnames[:] "
            "assignment — os.walk will still descend into skip dirs."
        )
        assert "os.walk" in body_src, (
            "_walk_for_filename does not use os.walk — the fix shape "
            "drifted from spec."
        )
        return
    raise AssertionError(
        "_walk_for_filename helper not found in source_crawlers.py"
    )


def test_ast_pin_walk_deadline_check_present() -> None:
    """``_walk_for_filename`` must check ``time.monotonic() > deadline``
    inside the walk loop. Without the deadline, a pathological repo
    can still wedge for arbitrarily long even with pruning."""
    src = CRAWLER_FILE.read_text()
    assert "time.monotonic() > deadline" in src, (
        "Deadline check missing from _walk_for_filename — Part 3 "
        "defense-in-depth not wired."
    )
    assert "max_walk_s" in src, "max_walk_s parameter missing"


# ──────────────────────────────────────────────────────────────────────
# Spine — 6 (pure-data tests using a temp dir)
# ──────────────────────────────────────────────────────────────────────


def test_spine_walk_prunes_skip_dirnames() -> None:
    """Build a temp tree with a Cargo.toml inside ``node_modules`` and
    one outside. The walker must skip the in-node_modules one entirely."""
    from backend.core.ouroboros.roadmap.source_crawlers import (
        _walk_for_filename,
    )
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        # First-party crate
        (root / "real_crate").mkdir()
        (root / "real_crate" / "Cargo.toml").write_text(
            "[package]\nname = \"real\"\n"
        )
        # Vendored crate inside node_modules — must NOT be returned
        (root / "node_modules" / "vendor").mkdir(parents=True)
        (root / "node_modules" / "vendor" / "Cargo.toml").write_text(
            "[package]\nname = \"vendor\"\n"
        )
        # Build artifact inside target — must NOT be returned
        (root / "target" / "debug").mkdir(parents=True)
        (root / "target" / "debug" / "Cargo.toml").write_text(
            "[package]\nname = \"build_artifact\"\n"
        )
        found = _walk_for_filename(
            root, "Cargo.toml",
            frozenset({"node_modules", "target"}),
            max_walk_s=10.0,
        )
        assert len(found) == 1, (
            f"Expected 1 first-party Cargo.toml, got {len(found)}: {found}"
        )
        assert found[0].parent.name == "real_crate"


def test_spine_walk_respects_deadline_returns_partial() -> None:
    """When the walk exceeds ``max_walk_s``, returns whatever was
    collected so far — never raises."""
    from backend.core.ouroboros.roadmap.source_crawlers import (
        _walk_for_filename,
    )
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        # Create one Cargo.toml so there's something to find
        (root / "Cargo.toml").write_text("[package]\nname = \"x\"\n")
        # Deadline of 0.001s — should overrun almost immediately
        # (or find the one Cargo.toml in the first dir; both outcomes
        # are valid). The contract is: NO EXCEPTION on overrun.
        found = _walk_for_filename(
            root, "Cargo.toml", frozenset(), max_walk_s=0.001,
        )
        # Either 0 (overrun before yield) or 1 (lucky catch on first
        # iteration). Both are valid — assertion is "no exception".
        assert len(found) <= 1


def test_spine_walk_finds_all_non_skipped_matches() -> None:
    """Multiple Cargo.toml in non-skipped subdirs are all returned,
    sorted by path."""
    from backend.core.ouroboros.roadmap.source_crawlers import (
        _walk_for_filename,
    )
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for name in ["alpha", "beta", "gamma"]:
            d = root / name
            d.mkdir()
            (d / "Cargo.toml").write_text(f"[package]\nname = \"{name}\"\n")
        found = _walk_for_filename(
            root, "Cargo.toml", frozenset({"node_modules"}),
            max_walk_s=10.0,
        )
        assert len(found) == 3
        # Sorted order
        assert [p.parent.name for p in found] == ["alpha", "beta", "gamma"]


def test_spine_walk_max_walk_s_env_default_is_10s() -> None:
    """``_crawl_max_walk_s`` default is 10s, env-overridable, floored
    at 0.5s to prevent operator-induced micro-deadlines."""
    from backend.core.ouroboros.roadmap.source_crawlers import (
        _crawl_max_walk_s,
        _DEFAULT_CRAWL_MAX_WALK_S,
    )
    assert _DEFAULT_CRAWL_MAX_WALK_S == 10.0
    # Default when env unset
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("JARVIS_STRATEGIC_CRAWL_MAX_WALK_S", None)
        assert _crawl_max_walk_s() == 10.0
    # Env override honored
    with patch.dict(os.environ, {"JARVIS_STRATEGIC_CRAWL_MAX_WALK_S": "3.5"}):
        assert _crawl_max_walk_s() == 3.5
    # Floor at 0.5s — protects against operator typos
    with patch.dict(os.environ, {"JARVIS_STRATEGIC_CRAWL_MAX_WALK_S": "0.001"}):
        assert _crawl_max_walk_s() == 0.5
    # Malformed → fall back to default
    with patch.dict(os.environ, {"JARVIS_STRATEGIC_CRAWL_MAX_WALK_S": "garbage"}):
        assert _crawl_max_walk_s() == 10.0


def test_spine_crawl_rust_subsystems_end_to_end_fast() -> None:
    """End-to-end smoke: the real ``crawl_rust_subsystems`` against the
    JARVIS repo completes in under 5s (typically <2s). This is the
    regression test for the bt-2026-05-25-050449 wedge — that soak
    took 914s for the same function call."""
    from backend.core.ouroboros.roadmap.source_crawlers import (
        crawl_rust_subsystems,
    )
    t0 = time.monotonic()
    fragments = crawl_rust_subsystems(REPO_ROOT)
    elapsed = time.monotonic() - t0
    assert elapsed < 5.0, (
        f"crawl_rust_subsystems took {elapsed:.2f}s — would risk "
        f"wedging the asyncio loop again. The pre-Slice-3F rglob "
        f"version took ~914s for this same call."
    )
    # Must still find SOME crates (this repo has multiple)
    assert len(fragments) > 0, (
        "crawl_rust_subsystems returned empty — the fix may have "
        "accidentally pruned away legitimate first-party crates."
    )


def test_spine_walk_skip_set_matches_by_dirname_not_substring() -> None:
    """Pre-Slice-3F skip used SUBSTRINGS like ``"/target/"`` which
    could false-match weird paths. Slice 3F uses bare dirnames in a
    frozenset matched against ``os.walk``'s dirnames list — exact
    match, no substring footguns. Verify a dir named ``my_target``
    (legitimate, not a build dir) is NOT skipped."""
    from backend.core.ouroboros.roadmap.source_crawlers import (
        _walk_for_filename,
    )
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        # Dir name contains "target" as substring but is NOT a build dir
        (root / "my_target_aware_crate").mkdir()
        (root / "my_target_aware_crate" / "Cargo.toml").write_text(
            "[package]\nname = \"my_target_aware\"\n"
        )
        # Actual target dir — must be skipped
        (root / "target").mkdir()
        (root / "target" / "Cargo.toml").write_text(
            "[package]\nname = \"build_artifact\"\n"
        )
        found = _walk_for_filename(
            root, "Cargo.toml",
            frozenset({"target"}),
            max_walk_s=10.0,
        )
        names = {p.parent.name for p in found}
        assert "my_target_aware_crate" in names, (
            "Substring-style skip falsely matched 'my_target_aware_crate' "
            "— Slice 3F's exact-dirname matching is broken."
        )
        assert "target" not in names, (
            "Actual 'target' dir was NOT skipped — Slice 3F prune broken."
        )
