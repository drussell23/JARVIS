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


class _FakeNeighborhood:
    def __init__(self, **lists):
        for k in ("target_files", "imports", "importers", "callers", "callees",
                  "inheritors", "base_classes", "test_counterparts",
                  "semantic_support"):
            setattr(self, k, lists.get(k, []))


class _FakeOracleObj:
    def __init__(self, nbh):
        self._nbh = nbh
    def is_semantic_ready(self):
        return True
    async def get_fused_neighborhood(self, files, query, k_semantic=8):
        return self._nbh


def test_normalize_passthrough_for_list():
    items = ep._normalize_neighborhood([{"rel_path": "a.py", "score": 0.5}])
    assert items == [{"rel_path": "a.py", "score": 0.5}]


def test_normalize_dedupes_keeping_highest_leverage():
    nb = _FakeNeighborhood(callers=["r:x.py"], imports=["r:x.py"])
    items = ep._normalize_neighborhood(nb)
    assert len(items) == 1
    assert items[0]["category_hint"] == "CALL_GRAPH"  # caller(1.0) beats import(0.7)


def test_normalizes_real_fileneighborhood(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_EPISTEMIC_PREFETCH_ENABLED", "true")
    (tmp_path / "caller.py").write_text("import dep\n", encoding="utf-8")
    (tmp_path / "sem.py").write_text("x = 1\n", encoding="utf-8")
    nb = _FakeNeighborhood(callers=["jarvis:caller.py"],
                           semantic_support=["jarvis:sem.py"])
    out = asyncio.run(ep.build_prefetch_manifest(
        target_files=("a.py", "b.py"), root=str(tmp_path),
        oracle=_FakeOracleObj(nb), goal_text="g", is_heavy=True))
    paths = {e.rel_path: e for e in out}
    assert paths["caller.py"].category_hint == "CALL_GRAPH"
    assert paths["sem.py"].category_hint == "COMPREHENSION"
    assert paths["caller.py"].relevance > paths["sem.py"].relevance


def test_target_files_excluded_from_normalized(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_EPISTEMIC_PREFETCH_ENABLED", "true")
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    # 'a.py' is BOTH a target and shows up as a caller -> must be excluded
    nb = _FakeNeighborhood(callers=["jarvis:a.py"])
    out = asyncio.run(ep.build_prefetch_manifest(
        target_files=("a.py", "b.py"), root=str(tmp_path),
        oracle=_FakeOracleObj(nb), goal_text="g", is_heavy=True))
    assert all(e.rel_path != "a.py" for e in out)
