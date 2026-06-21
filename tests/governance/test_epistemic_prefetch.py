# tests/governance/test_epistemic_prefetch.py
from __future__ import annotations
import asyncio
from backend.core.ouroboros.governance import epistemic_prefetch as ep


class _FakeOracle:
    def __init__(self, ready=True, neighborhood=None, raises=False):
        self._ready = ready
        self._n = neighborhood or []
        self._raises = raises
    def is_semantic_ready(self):
        return self._ready
    async def get_fused_neighborhood(self, files, query, k_semantic=8):
        if self._raises:
            raise RuntimeError("oracle boom")
        return self._n


def test_disabled_returns_empty(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_EPISTEMIC_PREFETCH_ENABLED", "false")
    out = asyncio.run(ep.build_prefetch_manifest(
        target_files=("a.py", "b.py"), root=str(tmp_path),
        oracle=_FakeOracle(), goal_text="g", is_heavy=True))
    assert out == ()


def test_not_heavy_returns_empty(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_EPISTEMIC_PREFETCH_ENABLED", "true")
    out = asyncio.run(ep.build_prefetch_manifest(
        target_files=("a.py",), root=str(tmp_path),
        oracle=_FakeOracle(), goal_text="g", is_heavy=False))
    assert out == ()


def test_oracle_cold_returns_empty(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_EPISTEMIC_PREFETCH_ENABLED", "true")
    out = asyncio.run(ep.build_prefetch_manifest(
        target_files=("a.py", "b.py"), root=str(tmp_path),
        oracle=_FakeOracle(ready=False), goal_text="g", is_heavy=True))
    assert out == ()


def test_oracle_exception_failsoft_empty(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_EPISTEMIC_PREFETCH_ENABLED", "true")
    out = asyncio.run(ep.build_prefetch_manifest(
        target_files=("a.py", "b.py"), root=str(tmp_path),
        oracle=_FakeOracle(raises=True), goal_text="g", is_heavy=True))
    assert out == ()


def test_builds_ranked_hashed_entries(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_EPISTEMIC_PREFETCH_ENABLED", "true")
    (tmp_path / "dep.py").write_text("def helper():\n    return 1\n", encoding="utf-8")
    nbh = [{"rel_path": "dep.py", "score": 0.9, "category_hint": "CALL_GRAPH"}]
    out = asyncio.run(ep.build_prefetch_manifest(
        target_files=("a.py", "b.py"), root=str(tmp_path),
        oracle=_FakeOracle(neighborhood=nbh), goal_text="g", is_heavy=True))
    assert len(out) == 1
    e = out[0]
    assert e.rel_path == "dep.py"
    assert e.sha256 != ""
    assert e.relevance == 0.9
    assert "helper" in e.content_excerpt


def test_seed_byte_budget_truncates_excerpt(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_EPISTEMIC_PREFETCH_ENABLED", "true")
    monkeypatch.setenv("JARVIS_EPISTEMIC_PREFETCH_SEED_BYTES", "10")
    big = "x = 1  # " + ("padding " * 50) + "\n"
    (tmp_path / "dep.py").write_text(big, encoding="utf-8")
    nbh = [{"rel_path": "dep.py", "score": 0.5, "category_hint": "COMPREHENSION"}]
    out = asyncio.run(ep.build_prefetch_manifest(
        target_files=("a.py", "b.py"), root=str(tmp_path),
        oracle=_FakeOracle(neighborhood=nbh), goal_text="g", is_heavy=True))
    assert out[0].content_excerpt == ""
    assert out[0].sha256 != ""
