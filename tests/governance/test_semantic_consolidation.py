"""Tier 2 — Semantic Consolidation Matrix regression suite."""
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance.semantic_consolidation import (
    Lesson,
    SemanticConsolidationMatrix,
    ConsolidationResult,
    fingerprint,
)


class _FakeStore:
    """Duck-typed UserPreferenceStore: records add() calls."""

    def __init__(self):
        self.added = []

    def add(self, *, memory_type, name, description, content="", why="",
            how_to_apply="", source="user", tags=(), paths=(), apps=()):
        if not name.strip() or not description.strip():
            raise ValueError("empty name/description")  # mirror real store contract
        rec = dict(memory_type=memory_type, name=name, description=description,
                   content=content, tags=tuple(tags), paths=tuple(paths), source=source)
        self.added.append(rec)
        return rec


def _lf(msg, fp="kernel.py", ep=""):
    return Lesson(signature=msg, kind="live_fire", file_path=fp, episode_id=ep)


# --------------------------------------------------------------------------- fingerprint
def test_fingerprint_collapses_variants():
    a = fingerprint("live-fire boot failure: FrozenInstanceError: cannot assign to field 'passed'")
    b = fingerprint("live-fire boot failure: FrozenInstanceError: cannot assign to field 'status'")
    assert a == b  # different field name → same structural fingerprint


def test_fingerprint_distinguishes_classes():
    a = fingerprint("FrozenInstanceError: cannot assign")
    b = fingerprint("AttributeError: no attribute foo")
    assert a != b


# --------------------------------------------------------------------------- gating
def test_off_by_default_no_consolidation():
    store = _FakeStore()
    m = SemanticConsolidationMatrix(store=store)  # enabled resolves from env (default off)
    for _ in range(10):
        assert m.record(_lf("FrozenInstanceError: cannot assign to field 'x'")) is None
    assert store.added == []


def test_below_threshold_no_fire():
    store = _FakeStore()
    m = SemanticConsolidationMatrix(store=store, threshold=5, enabled=True)
    for i in range(4):
        assert m.record(_lf("FrozenInstanceError: cannot assign")) is None
    assert store.added == []


def test_threshold_fires_core_directive():
    store = _FakeStore()
    m = SemanticConsolidationMatrix(store=store, threshold=5, enabled=True)
    results = [m.record(_lf("FrozenInstanceError: cannot assign to field 'f%d'" % i)) for i in range(5)]
    assert results[:4] == [None, None, None, None]
    res = results[4]
    assert isinstance(res, ConsolidationResult)
    assert res.occurrences == 5
    assert "dataclasses.replace" in res.principle
    assert len(store.added) == 1
    mem = store.added[0]
    assert "core_directive" in mem["tags"] and "consolidated" in mem["tags"]
    assert mem["name"].startswith("core-directive:")


def test_consolidates_once_then_quiet():
    store = _FakeStore()
    m = SemanticConsolidationMatrix(store=store, threshold=3, enabled=True)
    for _ in range(3):
        m.record(_lf("AttributeError: 'dict' object has no attribute 'passed'"))
    assert len(store.added) == 1
    # further identical failures do NOT re-fire (already distilled)
    for _ in range(5):
        assert m.record(_lf("AttributeError: 'dict' object has no attribute 'status'")) is None
    assert len(store.added) == 1


def test_purge_called_with_episode_ids():
    store = _FakeStore()
    purged = []
    m = SemanticConsolidationMatrix(store=store, purge=lambda ids: purged.extend(ids),
                                    threshold=3, enabled=True)
    for i in range(3):
        m.record(_lf("ImportError: cannot import name x", ep=f"ep-{i}"))
    assert sorted(purged) == ["ep-0", "ep-1", "ep-2"]
    res = m.record  # noqa: just ensure no crash on extra
    assert len(store.added) == 1


def test_purge_skipped_if_store_fails():
    class _BadStore:
        def add(self, **kw):
            raise RuntimeError("disk full")
    purged = []
    m = SemanticConsolidationMatrix(store=_BadStore(), purge=lambda ids: purged.extend(ids),
                                    threshold=2, enabled=True)
    # store.add fails → not persisted → purge must NOT run (don't lose episodes on failure)
    assert m.record(_lf("TypeError", ep="a")) is None
    assert m.record(_lf("TypeError", ep="b")) is None
    assert purged == []


def test_principle_selection_by_family():
    store = _FakeStore()
    m = SemanticConsolidationMatrix(store=store, threshold=2, enabled=True)
    m.record(_lf("TypeError: 'NoneType' object has no attribute 'a'"))
    res = m.record(_lf("TypeError: 'NoneType' object has no attribute 'b'"))
    assert "Optional" in res.principle or "None" in res.principle


def test_distinct_failures_cluster_independently():
    store = _FakeStore()
    m = SemanticConsolidationMatrix(store=store, threshold=3, enabled=True)
    # two different fingerprints — neither alone reaches threshold interleaved
    seq = ["FrozenInstanceError: a", "AttributeError: b"] * 2
    for s in seq:
        assert m.record(_lf(s)) is None
    assert store.added == []  # each cluster only at 2 < 3


def test_bounded_clusters_evicts_lru():
    store = _FakeStore()
    m = SemanticConsolidationMatrix(store=store, threshold=5, max_clusters=2, enabled=True)
    for i in range(10):
        m.record(_lf("UniqueError%d: boom" % i))   # 10 distinct fingerprints
    # never more than max_clusters retained
    assert len(m._clusters) <= 2


def test_record_never_raises_on_garbage():
    m = SemanticConsolidationMatrix(store=_FakeStore(), threshold=2, enabled=True)
    assert m.record(Lesson(signature="")) is None
    assert m.record(Lesson(signature="   ")) is None


def test_no_store_still_safe():
    m = SemanticConsolidationMatrix(threshold=2, enabled=True)  # no store
    for _ in range(3):
        assert m.record(_lf("KeyError: missing")) is None  # nothing persisted, no crash


def test_env_threshold_override(monkeypatch):
    monkeypatch.setenv("JARVIS_CONSOLIDATION_THRESHOLD", "2")
    monkeypatch.setenv("JARVIS_SEMANTIC_CONSOLIDATION_ENABLED", "1")
    store = _FakeStore()
    m = SemanticConsolidationMatrix(store=store)
    m.record(_lf("ValueError: invalid literal '1'"))
    assert m.record(_lf("ValueError: invalid literal '2'")) is not None
    assert len(store.added) == 1


def test_style_memory_type_used():
    store = _FakeStore()
    m = SemanticConsolidationMatrix(store=store, threshold=2, enabled=True)
    m.record(_lf("FrozenInstanceError: cannot assign to field 'x'"))
    m.record(_lf("FrozenInstanceError: cannot assign to field 'y'"))
    mt = store.added[0]["memory_type"]
    # resolves to MemoryType.STYLE when importable, else the "style" fallback
    assert getattr(mt, "value", mt) == "style"


def test_get_default_matrix_singleton():
    from backend.core.ouroboros.governance import semantic_consolidation as sc
    sc.reset_default_matrix()
    a = sc.get_default_matrix("/tmp/x")
    b = sc.get_default_matrix()
    assert a is b
    sc.reset_default_matrix()
    c = sc.get_default_matrix()
    assert c is not a


def test_get_default_matrix_storeless_safe(monkeypatch):
    # if the store factory blows up, the matrix must still build (store-less) and record safely
    from backend.core.ouroboros.governance import semantic_consolidation as sc
    import backend.core.ouroboros.governance.user_preference_memory as upm
    sc.reset_default_matrix()
    monkeypatch.setattr(upm, "get_default_store",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no disk")))
    m = sc.get_default_matrix()        # must not raise
    assert m is not None
    assert m.record(Lesson(signature="boom")) is None  # store-less + off → safe no-op
    sc.reset_default_matrix()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
