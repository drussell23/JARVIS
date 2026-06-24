"""Tests for blast_radius_visualizer.render_blast_tree.

Covers: tree grouped-by-repo, cap + "+M more", ASCII-only codepoints, footer
count, empty -> "", and the authority-free invariant (source-grep: no policy /
orchestrator / change_engine imports).
"""
from __future__ import annotations

import os
import pathlib
import re

import pytest

from backend.core.ouroboros.governance.multi_repo.blast_radius_visualizer import (
    render_blast_tree,
)
from backend.core.ouroboros.governance.multi_repo.cross_repo_blast_context import (
    BlastRadiusContext,
    DependentRef,
)


def _ctx(deps, *, target_repo="reactor", target_symbol="TelemetryAdapter.emit"):
    return BlastRadiusContext(
        target_repo=target_repo,
        target_symbol=target_symbol,
        dependents=tuple(deps),
        rendered_prompt_block="",
        truncated=False,
        total_dependents=len(deps),
    )


def _dep(repo, file, symbol):
    return DependentRef(repo=repo, file=file, symbol=symbol, relevance="...")


def test_empty_dependents_returns_empty_string():
    assert render_blast_tree(_ctx([])) == ""


def test_tree_grouped_by_repo_and_has_header_and_footer():
    deps = [
        _dep("jarvis", "backend/a/foo.py", "caller_a"),
        _dep("prime", "jarvis_prime/b/baz.py", "caller_c"),
        _dep("jarvis", "backend/a/bar.py", "caller_b"),
    ]
    out = render_blast_tree(_ctx(deps))
    assert "CROSS-REPO BLAST RADIUS" in out
    assert "mutating reactor::TelemetryAdapter.emit" in out
    assert "depended on by:" in out
    # Grouped by repo: both jarvis entries appear before the prime entry
    # (alpha repo order: jarvis < prime).
    jarvis_a = out.index("backend/a/foo.py")
    jarvis_b = out.index("backend/a/bar.py")
    prime_c = out.index("jarvis_prime/b/baz.py")
    assert jarvis_a < prime_c
    assert jarvis_b < prime_c
    # Repo tags rendered.
    assert "[jarvis]" in out
    assert "[prime]" in out
    # Footer count == total dependents.
    assert "3 Body/Mind files mapped to this Nerves mutation." in out


def test_cap_and_plus_m_more(monkeypatch):
    monkeypatch.setenv("JARVIS_BLAST_TREE_MAX_NODES", "2")
    deps = [_dep("jarvis", "f%d.py" % i, "sym%d" % i) for i in range(5)]
    out = render_blast_tree(_ctx(deps))
    # Only 2 drawn, "+3 more" summarised.
    assert "... +3 more ..." in out
    # Footer still reports the true total (5), never the capped count.
    assert "5 Body/Mind files mapped" in out


def test_no_cap_when_under_limit(monkeypatch):
    monkeypatch.setenv("JARVIS_BLAST_TREE_MAX_NODES", "40")
    deps = [_dep("jarvis", "f%d.py" % i, "sym%d" % i) for i in range(3)]
    out = render_blast_tree(_ctx(deps))
    assert "more ..." not in out


def test_output_is_ascii_plus_only_box_drawing():
    deps = [
        _dep("jarvis", "backend/a/foo.py", "caller_a"),
        _dep("prime", "jarvis_prime/b/baz.py", "caller_c"),
    ]
    out = render_blast_tree(_ctx(deps))
    allowed_nonascii = set("├─└│")
    for ch in out:
        if ord(ch) > 127:
            assert ch in allowed_nonascii, "unexpected non-ASCII codepoint %r" % ch


def test_render_never_raises_on_garbage():
    class _Bad:
        @property
        def dependents(self):
            raise RuntimeError("boom")
    # Should fail-soft to "" rather than raise.
    assert render_blast_tree(_Bad()) == ""


def test_visualizer_is_authority_free_source_grep():
    """Pure render: must NOT import orchestrator / policy / change_engine / risk."""
    src = pathlib.Path(
        "backend/core/ouroboros/governance/multi_repo/blast_radius_visualizer.py"
    ).read_text(encoding="utf-8")
    forbidden = ("orchestrator", "policy", "change_engine", "risk_tier", "risk_engine")
    for needle in forbidden:
        assert (
            not re.search(r"^\s*(import|from).*%s" % re.escape(needle), src, re.MULTILINE)
        ), "authority leak: visualizer imports %s" % needle
