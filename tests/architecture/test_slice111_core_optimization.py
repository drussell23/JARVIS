"""Slice 111 — FlagRegistry tech-debt eradication (Phase 1).

The ``FlagRegistry.register`` backward-compat adapter — the single API-boundary
fix that lets the 1000+ legacy ``register(name=, type_=, ...)`` seed sites
resolve into proper ``FlagSpec`` objects (string→enum mapping), eliminating the
boot tracebacks WITHOUT touching every caller. Canonical
``register(FlagSpec(...))`` must still work; malformed type/category must
degrade to safe defaults rather than abort boot.

NOTE: the Slice-111 Oracle ``to_thread`` cache-load gate was retired in Slice
112 (empirically ineffective — ``pickle.loads`` is GIL-bound, so threading it
still froze the loop ~165 s; the real fix is process isolation). Its tests were
removed with the reverted code.
"""

from __future__ import annotations

import pytest

from backend.core.ouroboros.governance.flag_registry import (
    Category,
    FlagRegistry,
    FlagSpec,
    FlagType,
    Relevance,
)


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
        from backend.core.ouroboros.governance import autonomy_command_bus_bridge as B
        r = FlagRegistry()
        B.register_flags(r)  # must not raise / must not log a traceback
        assert r.get_spec("JARVIS_COMMAND_BUS_BRIDGE_ENABLED") is not None
