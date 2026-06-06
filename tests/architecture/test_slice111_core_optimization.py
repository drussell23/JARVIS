"""Slice 111 — Core Optimization & Tech-Debt Eradication.

Two units under test:

1. ``FlagRegistry.register`` backward-compat adapter — the single API-boundary
   fix that lets the 1000+ legacy ``register(name=, type_=, ...)`` seed sites
   resolve into proper ``FlagSpec`` objects (string→enum mapping), eliminating
   the boot tracebacks WITHOUT touching every caller. Canonical
   ``register(FlagSpec(...))`` must still work; malformed type/category must
   degrade to safe defaults rather than abort boot.

2. ``oracle._oracle_threaded_cache_load`` gate + ``_read_and_unpickle_cache`` —
   the platform-aware switch that moves the ~1.1 GB graph ``pickle.loads`` off
   the event loop (default ON on Linux/prod, OFF on macOS ARM64 for the
   documented libmalloc-safety reason), so boot no longer blocks ~158 s.
"""

from __future__ import annotations

import pickle
import sys

import pytest

from backend.core.ouroboros.governance.flag_registry import (
    Category,
    FlagRegistry,
    FlagSpec,
    FlagType,
    Relevance,
)


# ===========================================================================
# Phase 1 — FlagRegistry legacy adapter
# ===========================================================================


class TestFlagRegistryLegacyAdapter:
    def test_legacy_kwargs_resolve_to_flagspec(self):
        r = FlagRegistry()
        r.register(
            name="JARVIS_X", type_="bool", default="false", description="d",
            category="Observability", posture_relevance="RELEVANT",
            source_file="x.py", example="JARVIS_X=true",
        )
        s = r.get_spec("JARVIS_X")
        assert isinstance(s, FlagSpec)
        assert s.type is FlagType.BOOL
        assert s.category is Category.OBSERVABILITY
        assert s.source_file == "x.py"

    def test_bare_posture_string_is_dropped(self):
        # Legacy passed a bare "RELEVANT"; the new field is Mapping[str,Relevance].
        r = FlagRegistry()
        r.register(name="JARVIS_Y", type_="int", default=1, description="d",
                   category="timing", posture_relevance="RELEVANT", source_file="y.py")
        assert dict(r.get_spec("JARVIS_Y").posture_relevance) == {}

    def test_real_mapping_posture_is_kept(self):
        r = FlagRegistry()
        r.register(name="JARVIS_Z", type_="bool", default="false", description="d",
                   category="safety", source_file="z.py",
                   posture_relevance={"HARDEN": Relevance.CRITICAL})
        assert r.get_spec("JARVIS_Z").posture_relevance["HARDEN"] is Relevance.CRITICAL

    def test_canonical_flagspec_still_works(self):
        r = FlagRegistry()
        r.register(FlagSpec(name="JARVIS_C", type=FlagType.FLOAT, default=1.5,
                            description="d", category=Category.TUNING, source_file="c.py"))
        assert r.get_spec("JARVIS_C").type is FlagType.FLOAT

    def test_unknown_type_degrades_to_str(self):
        r = FlagRegistry()
        r.register(name="JARVIS_BADT", type_="frobnicate", default="x",
                   description="d", category="tuning", source_file="b.py")
        assert r.get_spec("JARVIS_BADT").type is FlagType.STR

    def test_unknown_category_degrades_to_experimental(self):
        r = FlagRegistry()
        r.register(name="JARVIS_BADC", type_="bool", default="false",
                   description="d", category="not_a_category", source_file="b.py")
        assert r.get_spec("JARVIS_BADC").category is Category.EXPERIMENTAL

    def test_no_args_still_raises(self):
        r = FlagRegistry()
        with pytest.raises(TypeError):
            r.register()

    def test_real_legacy_caller_runs_clean(self):
        # A real ~10-flag seed module that previously threw on every call.
        from backend.core.ouroboros.governance import autonomy_command_bus_bridge as B
        r = FlagRegistry()
        B.register_flags(r)  # must not raise / must not log a traceback
        assert r.get_spec("JARVIS_COMMAND_BUS_BRIDGE_ENABLED") is not None


# ===========================================================================
# Phase 2 — Oracle threaded-load gate + cache (de)serialization
# ===========================================================================


class TestOracleThreadedLoadGate:
    def _gate(self):
        from backend.core.ouroboros import oracle as O
        return O._oracle_threaded_cache_load

    def test_explicit_true_wins(self, monkeypatch):
        monkeypatch.setenv("JARVIS_ORACLE_CACHE_THREADED_LOAD", "1")
        assert self._gate()() is True

    def test_explicit_false_wins_even_on_linux(self, monkeypatch):
        monkeypatch.setenv("JARVIS_ORACLE_CACHE_THREADED_LOAD", "0")
        monkeypatch.setattr(sys, "platform", "linux")
        assert self._gate()() is False

    def test_default_threaded_on_linux(self, monkeypatch):
        monkeypatch.delenv("JARVIS_ORACLE_CACHE_THREADED_LOAD", raising=False)
        monkeypatch.setattr(sys, "platform", "linux")
        assert self._gate()() is True

    def test_default_synchronous_on_darwin(self, monkeypatch):
        monkeypatch.delenv("JARVIS_ORACLE_CACHE_THREADED_LOAD", raising=False)
        monkeypatch.setattr(sys, "platform", "darwin")
        assert self._gate()() is False

    def test_read_and_unpickle_roundtrip(self, tmp_path):
        from backend.core.ouroboros.oracle import TheOracle
        payload = {"graph": {"n": 1}, "node_index": {}, "file_index": {},
                   "repo_index": {}, "type_index": {}, "metrics": {"total_nodes": 1}}
        p = tmp_path / "cache.pkl"
        p.write_bytes(pickle.dumps(payload))
        out = TheOracle._read_and_unpickle_cache(p)
        assert out["graph"] == {"n": 1}
        assert out["metrics"]["total_nodes"] == 1
