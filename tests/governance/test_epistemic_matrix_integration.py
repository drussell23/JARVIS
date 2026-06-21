from __future__ import annotations
import asyncio
from backend.core.ouroboros.governance import epistemic_prefetch as ep
from backend.core.ouroboros.governance import context_governor as cg
from backend.core.ouroboros.governance.epistemic_prefetch import PrefetchEntry


class _FakeOracleObj:
    def __init__(self, nbh):
        self._nbh = nbh
    def is_semantic_ready(self):
        return True
    async def get_fused_neighborhood(self, files, query, k_semantic=8):
        return self._nbh


class _FakeNeighborhood:
    def __init__(self, **lists):
        for k in ("target_files", "imports", "importers", "callers", "callees",
                  "inheritors", "base_classes", "test_counterparts",
                  "semantic_support"):
            setattr(self, k, lists.get(k, []))


def test_prefetch_feeds_governor_round0_baseline(monkeypatch, tmp_path):
    # End-to-end: a prefetched excerpt becomes the governor's round-0 baseline,
    # so re-reading that same file yields LOW gain (memory worked, no inflation).
    monkeypatch.setenv("JARVIS_EPISTEMIC_PREFETCH_ENABLED", "true")
    monkeypatch.setenv("JARVIS_CONTEXT_GOVERNOR_ENABLED", "true")
    (tmp_path / "dep.py").write_text("def shared(): return 42\n", encoding="utf-8")
    nb = _FakeNeighborhood(callers=["jarvis:dep.py"])
    manifest = asyncio.run(ep.build_prefetch_manifest(
        target_files=("a.py", "b.py"), root=str(tmp_path),
        oracle=_FakeOracleObj(nb), goal_text="touch shared", is_heavy=True))
    assert manifest and manifest[0].rel_path == "dep.py"
    import types
    ctx = types.SimpleNamespace(prefetch_manifest=manifest, task_complexity="moderate",
                               target_files=("a.py", "b.py"), blast_radius=0)
    gov = cg.build_governor_for(ctx)
    assert gov is not None
    v = gov.observe_round(0, ["def shared(): return 42"], ledger=None)
    assert v.info_gain < 0.15   # already known from prefetch -> low gain


def test_truth_guard_drops_stale_between_build_and_consume(monkeypatch, tmp_path):
    # Build a manifest, then MUTATE the file on disk, then revalidate -> dropped.
    monkeypatch.setenv("JARVIS_EPISTEMIC_PREFETCH_ENABLED", "true")
    f = tmp_path / "dep.py"
    f.write_text("def shared(): return 42\n", encoding="utf-8")
    nb = _FakeNeighborhood(callers=["jarvis:dep.py"])
    manifest = asyncio.run(ep.build_prefetch_manifest(
        target_files=("a.py", "b.py"), root=str(tmp_path),
        oracle=_FakeOracleObj(nb), goal_text="g", is_heavy=True))
    assert manifest
    # file changes after prefetch hashed it
    f.write_text("def shared(): return 999  # changed\n", encoding="utf-8")
    validated = ep.revalidate_manifest(manifest, str(tmp_path), ledger=None)
    assert all(e.rel_path != "dep.py" for e in validated)  # stale dropped


def test_all_flags_off_is_noop(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_EPISTEMIC_PREFETCH_ENABLED", "false")
    monkeypatch.setenv("JARVIS_CONTEXT_GOVERNOR_ENABLED", "false")
    out = asyncio.run(ep.build_prefetch_manifest(
        target_files=("a.py", "b.py"), root=str(tmp_path),
        oracle=_FakeOracleObj(_FakeNeighborhood(callers=["jarvis:x.py"])),
        goal_text="g", is_heavy=True))
    assert out == ()
    import types
    ctx = types.SimpleNamespace(prefetch_manifest=(), task_complexity="moderate",
                               target_files=("a.py", "b.py"), blast_radius=0)
    assert cg.build_governor_for(ctx) is None   # governor off -> None


def test_governor_off_but_prefetch_on_still_safe(monkeypatch, tmp_path):
    # prefetch builds, but governor disabled -> build_governor_for None, no crash
    monkeypatch.setenv("JARVIS_EPISTEMIC_PREFETCH_ENABLED", "true")
    monkeypatch.setenv("JARVIS_CONTEXT_GOVERNOR_ENABLED", "false")
    (tmp_path / "dep.py").write_text("x = 1\n", encoding="utf-8")
    nb = _FakeNeighborhood(callers=["jarvis:dep.py"])
    manifest = asyncio.run(ep.build_prefetch_manifest(
        target_files=("a.py", "b.py"), root=str(tmp_path),
        oracle=_FakeOracleObj(nb), goal_text="g", is_heavy=True))
    assert manifest  # prefetch still works
    import types
    ctx = types.SimpleNamespace(prefetch_manifest=manifest, task_complexity="moderate",
                               target_files=("a.py", "b.py"), blast_radius=0)
    assert cg.build_governor_for(ctx) is None
