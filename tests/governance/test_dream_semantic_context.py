"""Semantic Context Engineering — the DreamEngine payload compiler attaches the
North-Star intent + human-frontier blocks (and stays byte-identical when dark).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.core.ouroboros.consciousness.dream_engine import DreamEngine
from backend.core.ouroboros.governance import frontier_context as fc
from backend.core.ouroboros.governance import north_star_context as nsc


_LEGACY_DIRECTIVE = (
    "Analyze the repository at SHA abc123 for potential improvements in the "
    "'failure_pattern' category."
)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for v in ("JARVIS_DREAM_NORTH_STAR_CONTEXT_ENABLED",
              "JARVIS_DREAM_FRONTIER_CONTEXT_ENABLED",
              "JARVIS_PRD_DOC", "JARVIS_NORTH_STAR_DOC",
              "JARVIS_NORTH_STAR_CONTEXT_MAX_CHARS"):
        monkeypatch.delenv(v, raising=False)
    nsc._reset_cache_for_tests()
    fc._reset_cache_for_tests()
    yield
    nsc._reset_cache_for_tests()
    fc._reset_cache_for_tests()


def _prompt(candidate):
    # _build_dream_prompt touches no instance state — unbound call, no heavy init.
    return DreamEngine._build_dream_prompt(object(), candidate)


def _candidate(**extra):
    return {"repo_sha": "abc123", "prompt_family": "failure_pattern", **extra}


# ---------------------------------------------------------------------------
# A. Intent block attachment
# ---------------------------------------------------------------------------


def test_payload_attaches_north_star_intent(tmp_path, monkeypatch):
    prd = tmp_path / "prd.md"
    prd.write_text(
        "# T\n## 6. Target State (A-Level Execution from A-Level Vision)\n"
        "| Autonomous initiation | >= 3 goals |\n## 7. Next\n"
    )
    monkeypatch.setenv("JARVIS_PRD_DOC", str(prd))
    monkeypatch.setenv("JARVIS_NORTH_STAR_DOC", str(tmp_path / "none.md"))

    p = _prompt(_candidate())
    assert "ARCHITECTURAL INTENT" in p
    assert "Autonomous initiation" in p                  # §6 table attached
    assert "ADVANCES the architectural intent" in p      # alignment directive
    assert "Return JSON with keys" in p                  # schema preserved


def test_payload_attaches_frontier_digest():
    p = _prompt(_candidate(frontier_digest="## RECENT HUMAN FRONTIER\nActive scopes: governance×9"))
    assert "RECENT HUMAN FRONTIER" in p
    assert "governance×9" in p
    assert "CONTINUES the recent human frontier" in p


def test_dark_path_is_byte_identical_legacy(tmp_path, monkeypatch):
    """Both hydrations dark → the prompt is EXACTLY the legacy directive."""
    monkeypatch.setenv("JARVIS_DREAM_NORTH_STAR_CONTEXT_ENABLED", "false")
    p = _prompt(_candidate())                            # no frontier_digest key
    assert p.startswith(_LEGACY_DIRECTIVE)
    assert "ARCHITECTURAL INTENT" not in p
    assert "ADVANCES the architectural intent" not in p  # no alignment clause


def test_hydration_failure_is_failsoft(monkeypatch):
    monkeypatch.setenv("JARVIS_PRD_DOC", "/nonexistent/prd.md")
    monkeypatch.setenv("JARVIS_NORTH_STAR_DOC", "/nonexistent/ns.md")
    p = _prompt(_candidate())
    assert p.startswith(_LEGACY_DIRECTIVE)               # degraded, never raised


# ---------------------------------------------------------------------------
# B. Frontier block rendering + async hydration
# ---------------------------------------------------------------------------


def _snap(scopes, types_, subjects, n=42):
    return SimpleNamespace(
        commit_count=n,
        top_scopes=lambda k=5: scopes[:k],
        top_types=lambda k=4: types_[:k],
        latest_subjects=tuple(subjects),
        is_empty=lambda: n == 0,
    )


def test_render_frontier_block_shape():
    b = fc.render_frontier_block(_snap(
        [("governance", 30), ("providers", 8)], [("fix", 20), ("feat", 15)],
        ["fix(governance): Slice 2", "feat: masking"],
    ))
    assert "RECENT HUMAN FRONTIER" in b and "last 42 commits" in b
    assert "governance×30" in b and "fix×20" in b
    assert "- fix(governance): Slice 2" in b


def test_render_empty_snapshot_is_empty():
    assert fc.render_frontier_block(_snap([], [], [], n=0)) == ""
    assert fc.render_frontier_block(None) == ""


@pytest.mark.asyncio
async def test_frontier_async_real_repo_and_sha_cache(monkeypatch):
    """Against the REAL repo: block renders + the sha cache prevents a second
    git walk."""
    b1 = await fc.frontier_context_async(repo_sha="cachekey1")
    assert "RECENT HUMAN FRONTIER" in b1
    calls = {"n": 0}
    async def _boom(*a, **k):
        calls["n"] += 1
        raise AssertionError("must not re-walk git on cached sha")
    monkeypatch.setattr(
        "backend.core.ouroboros.governance.git_momentum.compute_recent_momentum_async",
        _boom,
    )
    b2 = await fc.frontier_context_async(repo_sha="cachekey1")
    assert b2 == b1 and calls["n"] == 0


@pytest.mark.asyncio
async def test_frontier_disabled_is_empty(monkeypatch):
    monkeypatch.setenv("JARVIS_DREAM_FRONTIER_CONTEXT_ENABLED", "false")
    assert await fc.frontier_context_async(repo_sha="x") == ""
