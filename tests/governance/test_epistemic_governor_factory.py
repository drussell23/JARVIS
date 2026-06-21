from __future__ import annotations
import types
from backend.core.ouroboros.governance import context_governor as cg
from backend.core.ouroboros.governance.epistemic_prefetch import PrefetchEntry


def test_build_governor_disabled_returns_none(monkeypatch):
    monkeypatch.setenv("JARVIS_CONTEXT_GOVERNOR_ENABLED", "false")
    ctx = types.SimpleNamespace(prefetch_manifest=(), task_complexity="moderate")
    assert cg.build_governor_for(ctx) is None


def test_build_governor_returns_governor_with_prefetch_excerpts(monkeypatch):
    monkeypatch.setenv("JARVIS_CONTEXT_GOVERNOR_ENABLED", "true")
    manifest = (PrefetchEntry("a.py", "h", 0.9, "CALL_GRAPH", "def a(): pass"),)
    ctx = types.SimpleNamespace(prefetch_manifest=manifest, task_complexity="moderate",
                                target_files=("a.py", "b.py"), blast_radius=0)
    gov = cg.build_governor_for(ctx)
    assert gov is not None
    # round-0 baseline seeded from the excerpt -> re-reading it = low gain
    v = gov.observe_round(0, ["def a(): pass"], ledger=None)
    assert v.info_gain < 0.15


def test_floor_adapter_none_ledger_is_satisfied():
    ad = cg._IronGateFloorAdapter(floors=None)
    assert ad.is_satisfied(None) is True
    assert ad.missing_categories(None) == ()


def test_build_governor_failsoft_on_bad_context(monkeypatch):
    monkeypatch.setenv("JARVIS_CONTEXT_GOVERNOR_ENABLED", "true")
    # a context missing prefetch_manifest entirely must not raise
    gov = cg.build_governor_for(object())
    assert gov is None or gov is not None  # just must not raise


def test_build_governor_none_for_light_op(monkeypatch):
    monkeypatch.setenv("JARVIS_CONTEXT_GOVERNOR_ENABLED", "true")
    manifest = (PrefetchEntry("a.py", "h", 0.9, "CALL_GRAPH", "def a(): pass"),)
    # single target file, no blast radius -> NOT heavy -> None (light op untouched)
    ctx = types.SimpleNamespace(prefetch_manifest=manifest, task_complexity="simple",
                                target_files=("a.py",), blast_radius=0)
    assert cg.build_governor_for(ctx) is None


def test_build_governor_heavy_empty_manifest_still_returns_governor(monkeypatch):
    monkeypatch.setenv("JARVIS_CONTEXT_GOVERNOR_ENABLED", "true")
    # heavy (2 files) but EMPTY manifest (cold oracle) -> still a governor
    ctx = types.SimpleNamespace(prefetch_manifest=(), task_complexity="moderate",
                                target_files=("a.py", "b.py"), blast_radius=0)
    assert cg.build_governor_for(ctx) is not None
