"""Semantic Context Engineering — the North-Star/PRD hydration extractor.

Deterministic, bounded, mtime-cached, fail-soft. NOT a RAG pipeline.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance import north_star_context as nsc


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for v in ("JARVIS_DREAM_NORTH_STAR_CONTEXT_ENABLED",
              "JARVIS_NORTH_STAR_CONTEXT_MAX_CHARS",
              "JARVIS_PRD_DOC", "JARVIS_NORTH_STAR_DOC", "JARVIS_REPO_ROOT"):
        monkeypatch.delenv(v, raising=False)
    nsc._reset_cache_for_tests()
    yield
    nsc._reset_cache_for_tests()


_PRD = """# Title
## 1. Executive Summary
prose
## 6. Target State (A-Level Execution from A-Level Vision)
| Dimension | Criterion |
| Autonomous initiation | >= 3 self-formed goals |
| Reliability | >= 90% completion |
## 7. Next
more
"""

_NS = """# North Star
## S51 Galaxy
### S51.1 Mandate
### S51.2 Audit
"""


@pytest.fixture()
def docs(tmp_path, monkeypatch):
    prd = tmp_path / "prd.md"
    ns = tmp_path / "ns.md"
    prd.write_text(_PRD)
    ns.write_text(_NS)
    monkeypatch.setenv("JARVIS_PRD_DOC", str(prd))
    monkeypatch.setenv("JARVIS_NORTH_STAR_DOC", str(ns))
    return prd, ns


def test_block_carries_a_level_criteria_and_outlines(docs, tmp_path):
    block = nsc.north_star_context(str(tmp_path))
    assert "A-level definition of DONE" in block
    assert "Autonomous initiation" in block          # §6 table verbatim
    assert "## 1. Executive Summary" in block        # PRD outline
    assert "### S51.2 Audit" in block                # NS outline
    assert "alliance system" in block                # the macro framing


def test_budget_is_hard_bounded(docs, tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_NORTH_STAR_CONTEXT_MAX_CHARS", "600")
    block = nsc.north_star_context(str(tmp_path))
    assert len(block) <= 600
    assert block.endswith("[...intent truncated]")


def test_disabled_returns_empty(docs, tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_DREAM_NORTH_STAR_CONTEXT_ENABLED", "false")
    assert nsc.north_star_context(str(tmp_path)) == ""


def test_missing_docs_fail_soft_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_PRD_DOC", str(tmp_path / "nope.md"))
    monkeypatch.setenv("JARVIS_NORTH_STAR_DOC", str(tmp_path / "also-nope.md"))
    assert nsc.north_star_context(str(tmp_path)) == ""   # never raises


def test_single_doc_still_hydrates(tmp_path, monkeypatch):
    prd = tmp_path / "prd.md"
    prd.write_text(_PRD)
    monkeypatch.setenv("JARVIS_PRD_DOC", str(prd))
    monkeypatch.setenv("JARVIS_NORTH_STAR_DOC", str(tmp_path / "nope.md"))
    block = nsc.north_star_context(str(tmp_path))
    assert "Autonomous initiation" in block and "S51" not in block


def test_mtime_cache_invalidates_on_edit(docs, tmp_path):
    prd, _ = docs
    b1 = nsc.north_star_context(str(tmp_path))
    assert "NEW-SECTION-MARKER" not in b1
    import os as _os
    prd.write_text(_PRD + "## 8. NEW-SECTION-MARKER\n")
    _os.utime(prd, (9999999999, 9999999999))         # force mtime change
    b2 = nsc.north_star_context(str(tmp_path))
    assert "NEW-SECTION-MARKER" in b2


def test_real_repo_docs_extract(monkeypatch):
    """Against the ACTUAL repo docs: the block is non-empty, bounded, and
    carries the real §6 table."""
    import pathlib
    if not pathlib.Path("docs/architecture/OUROBOROS_VENOM_PRD.md").exists():
        pytest.skip("repo docs not present")
    block = nsc.north_star_context(".")
    assert block and len(block) <= 6000
    assert "Autonomous initiation" in block
