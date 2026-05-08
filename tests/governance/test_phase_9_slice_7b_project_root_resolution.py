"""Phase 9 Slice 7b (2026-05-07) — `_resolve_project_root` dynamic
marker-based walk regression spine.

Pre-Slice-7b bug: `live_fire_soak._resolve_project_root` used a
hardcoded 5-deep ``.parent`` chain whose comment claimed "3
parents" but actually reached ``backend/`` — off-by-one with
stale docstring drift. Resulted in cron-fired soaks emitting
``outcome=runner`` rows because every battle-test invocation
failed to find the harness script.

Slice 7's lineage waiver correctly absorbed the symptom rows;
Slice 7b is the disease fix — dynamic marker-based walk that
locates the repo root structurally (no hardcoded depth count;
immune to future module re-organization). Operator binding
"no hardcoding" honored verbatim.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Default behavior: real-repo walk locates correct root
# ---------------------------------------------------------------------------


def test_resolves_to_actual_repo_root(monkeypatch):
    """The canonical post-Slice-7b behavior: walking up from
    `governance/graduation/live_fire_soak.py` MUST land on the
    repo root containing both ``scripts/ouroboros_battle_test.py``
    AND ``backend/``."""
    monkeypatch.delenv("JARVIS_REPO_PATH", raising=False)
    from backend.core.ouroboros.governance.graduation.live_fire_soak import (  # noqa: E501
        _resolve_project_root,
        BATTLE_TEST_SCRIPT_REL,
    )
    root = _resolve_project_root()
    # The script MUST exist at the resolved root.
    script = root / BATTLE_TEST_SCRIPT_REL
    assert script.exists(), (
        f"battle-test script missing at resolved root: "
        f"{script} (root={root})"
    )
    assert (root / "backend").is_dir()


def test_env_override_wins(monkeypatch, tmp_path):
    """``JARVIS_REPO_PATH`` env override is the operator escape
    hatch — MUST short-circuit the walk."""
    target = tmp_path / "fake-repo"
    target.mkdir()
    monkeypatch.setenv("JARVIS_REPO_PATH", str(target))
    from backend.core.ouroboros.governance.graduation.live_fire_soak import (  # noqa: E501
        _resolve_project_root,
    )
    assert _resolve_project_root() == target


def test_resolves_returns_repo_root_not_backend(monkeypatch):
    """Pre-Slice-7b regression guard: the function returned
    ``backend/`` (off-by-one). Post-Slice-7b it MUST return the
    repo root — easily distinguishable because the repo root
    has ``scripts/`` AND ``backend/`` while ``backend/`` has
    only ``backend/scripts/`` (which doesn't exist)."""
    monkeypatch.delenv("JARVIS_REPO_PATH", raising=False)
    from backend.core.ouroboros.governance.graduation.live_fire_soak import (  # noqa: E501
        _resolve_project_root,
    )
    root = _resolve_project_root()
    # Pre-fix bug: would have returned `<repo>/backend`.
    # Post-fix: returns `<repo>`. Easy to distinguish:
    # the resolved root MUST contain a `backend/` SUBDIR.
    assert (root / "backend").is_dir(), (
        f"resolved root {root} appears to be `backend/` "
        f"itself (missing `backend/` subdir) — Slice 7b "
        f"regression"
    )
    # The wrong path `<root>/backend/scripts/ouroboros_battle_test.py`
    # MUST NOT exist (because the canonical path is
    # `<root>/scripts/ouroboros_battle_test.py`).
    wrong_path = root / "backend" / "scripts" / "ouroboros_battle_test.py"
    assert not wrong_path.exists(), (
        f"unexpected: {wrong_path} exists, ambiguous root"
    )


# ---------------------------------------------------------------------------
# Marker-based walk
# ---------------------------------------------------------------------------


def test_walk_finds_root_via_marker_pair(monkeypatch, tmp_path):
    """Synthetic test: simulate a re-located source file deep
    in a tree that has the canonical marker pair somewhere
    above. Walk MUST find it."""
    # Setup: create a fake repo with the canonical marker pair.
    fake_root = tmp_path / "fake_repo"
    (fake_root / "scripts").mkdir(parents=True)
    (fake_root / "scripts" / "ouroboros_battle_test.py").write_text("")
    (fake_root / "backend").mkdir()
    (fake_root / "backend" / "core" / "ouroboros" / "governance"
        / "graduation").mkdir(parents=True)
    fake_module = (
        fake_root / "backend" / "core" / "ouroboros" / "governance"
        / "graduation" / "live_fire_soak.py"
    )
    fake_module.write_text("# fake")

    monkeypatch.delenv("JARVIS_REPO_PATH", raising=False)
    # Patch __file__ on the live_fire_soak module so the walk
    # starts from the fake module path.
    from backend.core.ouroboros.governance.graduation import (
        live_fire_soak as lfs,
    )
    monkeypatch.setattr(lfs, "__file__", str(fake_module))
    root = lfs._resolve_project_root()
    assert root == fake_root


def test_walk_bails_at_filesystem_root(monkeypatch, tmp_path):
    """When neither the marker pair NOR an ancestor with markers
    exists, the walk MUST bail without infinite-looping. The
    defensive fallback returns the topmost ancestor — caller
    surfaces a clean script-not-found error."""
    isolated = tmp_path / "isolated"
    isolated.mkdir()
    fake_module = isolated / "fake.py"
    fake_module.write_text("")

    monkeypatch.delenv("JARVIS_REPO_PATH", raising=False)
    from backend.core.ouroboros.governance.graduation import (
        live_fire_soak as lfs,
    )
    monkeypatch.setattr(lfs, "__file__", str(fake_module))
    # Should return some Path (the defensive fallback) without
    # raising or hanging.
    root = lfs._resolve_project_root()
    assert isinstance(root, Path)


def test_no_hardcoded_parent_chain():
    """Operator binding "no hardcoding" — the resolver MUST NOT
    use a hardcoded ``.parent.parent.parent...`` chain on the
    canonical happy path. The dynamic marker-based walk is the
    only correct shape."""
    import ast

    src = Path(
        "backend/core/ouroboros/governance/graduation/"
        "live_fire_soak.py"
    ).read_text()
    tree = ast.parse(src)
    # Find _resolve_project_root.
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.FunctionDef)
            and node.name == "_resolve_project_root"
        ):
            # The function body MUST contain a `for` loop or
            # while-loop (the dynamic walk). Pre-fix, the body
            # was just a single return with stacked .parent
            # attribute accesses.
            has_loop = any(
                isinstance(n, (ast.For, ast.While))
                for n in ast.walk(node)
            )
            assert has_loop, (
                "_resolve_project_root MUST use a dynamic walk "
                "loop, not a hardcoded .parent chain — operator "
                "binding 'no hardcoding'"
            )
            # The function body MUST reference a marker-pair
            # check. Specifically, BOTH `scripts` AND `backend`
            # string literals must appear in the function body
            # (they're the marker-pair that defines the repo
            # root structurally).
            string_consts = [
                n.value for n in ast.walk(node)
                if isinstance(n, ast.Constant)
                and isinstance(n.value, str)
            ]
            assert "scripts" in string_consts, (
                "_resolve_project_root MUST use 'scripts' marker"
            )
            assert "backend" in string_consts, (
                "_resolve_project_root MUST use 'backend' marker"
            )
            return
    pytest.fail("_resolve_project_root not found")


# ---------------------------------------------------------------------------
# End-to-end: harness can find the battle-test script
# ---------------------------------------------------------------------------


def test_battle_test_script_found_via_harness_resolution():
    """The load-bearing assertion: post-fix, the harness can
    actually find the battle-test script. Pre-fix, every
    cron-fired soak failed here with
    `[LiveFireSoak] battle-test script not found at <wrong>`."""
    from backend.core.ouroboros.governance.graduation.live_fire_soak import (  # noqa: E501
        _resolve_project_root,
        BATTLE_TEST_SCRIPT_REL,
    )
    root = _resolve_project_root()
    script = root / BATTLE_TEST_SCRIPT_REL
    assert script.exists(), (
        f"Slice 7b regression: harness cannot find battle-test "
        f"script. resolved_root={root} script={script}"
    )
    # Sanity: the script is the actual canonical entry point.
    assert script.name == "ouroboros_battle_test.py"
    assert script.parent.name == "scripts"
