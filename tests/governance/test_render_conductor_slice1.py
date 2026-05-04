"""RenderConductor Slice 1 — primitive regression suite.

Pins the substrate Slice 2 will wire the three existing renderers
(``stream_renderer.py``, ``serpent_flow.py``, ``live_dashboard.py``)
into, and Slices 3-6 will add typed primitive consumers on top of.

Strict directives validated:

  * No hardcoded values: every operator-tunable knob (master,
    theme name, density override, posture density map, palette
    overlay) has a FlagSpec registered in ``register_flags`` and
    is accessed exclusively through the FlagRegistry. Tests
    confirm overrides flow end-to-end.
  * Closed taxonomies: ColorRole / RegionKind / RenderDensity /
    EventKind member sets are AST-pinned. Tests confirm the pins
    pass against the shipped file AND fail against tampered AST.
  * No Rich import in the substrate: AST pin asserts no rich.*
    import. Backends own Rich; the substrate speaks roles only.
  * No authority imports: substrate stays descriptive only —
    cannot become a control-flow surface for orchestrator/policy/
    iron_gate/risk_tier/change_engine/candidate_generator/gate.
  * Async/thread safety: state mutations under ``threading.Lock``;
    backend exception in one notifier does not break others.
  * Auto-discovery: the conductor module is picked up by both
    ``flag_registry_seed._discover_module_provided_flags`` AND
    ``shipped_code_invariants._discover_module_provided_invariants``
    with zero edits to either seed file.

Covers:

  §A   Closed taxonomies: ColorRole / RegionKind / RenderDensity /
       EventKind — membership, str inheritance, immutability
  §B   Region frozen dataclass — defaults, with_density, frozen
  §C   RenderEvent frozen dataclass — defaults, monotonic_ts,
       to_dict, metadata
  §D   DefaultTheme — baseline, density modulation, overlay
  §E   ThemeRegistry — default present, register, replace, get
       fallback, names, clear
  §F   resolve_density — full precedence chain
  §G   Flag accessors — defaults, env round-trip, JSON, malformed
  §H   RenderConductor — construction, publish gate, dispatch,
       backend lifecycle, exception isolation, density resolution
  §I   Singleton triplet — get/register/reset
  §J   register_flags — count, names, types, idempotency
  §K   register_shipped_invariants — count, target file consistency
  §L   AST pins self-validate green against the real file
  §M   Auto-discovery: FlagRegistry seed + ShippedCodeInvariants
       both pick up the conductor module
"""
from __future__ import annotations

import ast
import threading
from typing import Any, Dict

import pytest

from backend.core.ouroboros.governance import render_conductor as rc


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_flag_env(monkeypatch: pytest.MonkeyPatch):
    """Strip every flag this module reads so tests start from baseline.
    Tests that need a flag set use monkeypatch.setenv inside the body."""
    for name in (
        "JARVIS_RENDER_CONDUCTOR_ENABLED",
        "JARVIS_RENDER_CONDUCTOR_THEME_NAME",
        "JARVIS_RENDER_CONDUCTOR_DENSITY_OVERRIDE",
        "JARVIS_RENDER_CONDUCTOR_POSTURE_DENSITY_MAP",
        "JARVIS_RENDER_CONDUCTOR_PALETTE_OVERRIDE",
    ):
        monkeypatch.delenv(name, raising=False)
    yield


@pytest.fixture
def fresh_registry():
    """Reset + reseed the flag registry so per-test env mutations are
    deterministic. Restored on teardown."""
    from backend.core.ouroboros.governance import flag_registry as fr
    fr.reset_default_registry()
    reg = fr.ensure_seeded()
    yield reg
    fr.reset_default_registry()


class RecordingBackend:
    """Test backend — records notify/flush/shutdown calls."""

    name = "recorder"

    def __init__(self) -> None:
        self.events: list = []
        self.flush_calls: int = 0
        self.shutdown_calls: int = 0

    def notify(self, event: rc.RenderEvent) -> None:
        self.events.append(event)

    def flush(self) -> None:
        self.flush_calls += 1

    def shutdown(self) -> None:
        self.shutdown_calls += 1


class RaisingBackend:
    """Test backend — raises on every method to verify exception isolation."""

    name = "raiser"

    def notify(self, event: rc.RenderEvent) -> None:
        del event
        raise RuntimeError("notify boom")

    def flush(self) -> None:
        raise RuntimeError("flush boom")

    def shutdown(self) -> None:
        raise RuntimeError("shutdown boom")


def _make_event(**overrides: Any) -> rc.RenderEvent:
    defaults: Dict[str, Any] = {
        "kind": rc.EventKind.PHASE_BEGIN,
        "region": rc.RegionKind.PHASE_STREAM,
        "role": rc.ColorRole.METADATA,
        "content": "x",
        "source_module": "test",
    }
    defaults.update(overrides)
    return rc.RenderEvent(**defaults)


# ---------------------------------------------------------------------------
# §A — Closed taxonomies
# ---------------------------------------------------------------------------


class TestColorRoleClosedTaxonomy:
    def test_exact_seven_members(self):
        names = sorted(m.name for m in rc.ColorRole)
        assert names == [
            "CONTENT", "EMPHASIS", "ERROR", "METADATA",
            "MUTED", "SUCCESS", "WARNING",
        ]

    def test_value_equals_name(self):
        for member in rc.ColorRole:
            assert member.value == member.name

    def test_str_inheritance(self):
        assert isinstance(rc.ColorRole.SUCCESS, str)

    def test_membership_check(self):
        assert rc.ColorRole("ERROR") is rc.ColorRole.ERROR
        with pytest.raises(ValueError):
            rc.ColorRole("NONEXISTENT")


class TestRegionKindClosedTaxonomy:
    def test_exact_seven_members(self):
        names = sorted(m.name for m in rc.RegionKind)
        assert names == [
            "HEADER", "INPUT", "MODAL", "PHASE_STREAM",
            "STATUS", "THREAD", "VIEWPORT",
        ]

    def test_value_equals_name(self):
        for member in rc.RegionKind:
            assert member.value == member.name

    def test_str_inheritance(self):
        assert isinstance(rc.RegionKind.HEADER, str)


class TestRenderDensityClosedTaxonomy:
    def test_exact_three_members(self):
        names = sorted(m.name for m in rc.RenderDensity)
        assert names == ["COMPACT", "FULL", "NORMAL"]

    def test_str_inheritance(self):
        assert isinstance(rc.RenderDensity.NORMAL, str)


class TestEventKindClosedTaxonomy:
    def test_exact_nine_members(self):
        names = sorted(m.name for m in rc.EventKind)
        assert names == [
            "BACKEND_RESET", "FILE_REF", "MODAL_DISMISS", "MODAL_PROMPT",
            "PHASE_BEGIN", "PHASE_END", "REASONING_TOKEN",
            "STATUS_TICK", "THREAD_TURN",
        ]


# ---------------------------------------------------------------------------
# §B — Region dataclass
# ---------------------------------------------------------------------------


class TestRegion:
    def test_default_construction(self):
        r = rc.Region(kind=rc.RegionKind.HEADER)
        assert r.kind is rc.RegionKind.HEADER
        assert r.capacity_lines == 0
        assert r.scroll_policy == "tail"
        assert r.density_override is None

    def test_frozen(self):
        r = rc.Region(kind=rc.RegionKind.HEADER)
        with pytest.raises(Exception):
            r.kind = rc.RegionKind.STATUS  # type: ignore[misc]

    def test_with_density(self):
        r = rc.Region(kind=rc.RegionKind.HEADER)
        r2 = r.with_density(rc.RenderDensity.COMPACT)
        assert r2.density_override is rc.RenderDensity.COMPACT
        assert r is not r2
        assert r.density_override is None  # original untouched

    def test_hashable(self):
        r = rc.Region(kind=rc.RegionKind.HEADER)
        assert hash(r) == hash(rc.Region(kind=rc.RegionKind.HEADER))


# ---------------------------------------------------------------------------
# §C — RenderEvent dataclass
# ---------------------------------------------------------------------------


class TestRenderEvent:
    def test_required_fields(self):
        e = _make_event()
        assert e.kind is rc.EventKind.PHASE_BEGIN
        assert e.region is rc.RegionKind.PHASE_STREAM
        assert e.role is rc.ColorRole.METADATA
        assert e.content == "x"
        assert e.source_module == "test"
        assert e.op_id is None

    def test_monotonic_ts_auto_populated(self):
        e1 = _make_event()
        e2 = _make_event()
        assert e2.monotonic_ts >= e1.monotonic_ts

    def test_metadata_default_empty(self):
        e = _make_event()
        assert dict(e.metadata) == {}

    def test_metadata_explicit(self):
        e = _make_event(metadata={"path": "x.py", "line": 42})
        assert e.metadata["path"] == "x.py"
        assert e.metadata["line"] == 42

    def test_frozen(self):
        e = _make_event()
        with pytest.raises(Exception):
            e.content = "y"  # type: ignore[misc]

    def test_to_dict_round_trip(self):
        e = _make_event(op_id="op-123", metadata={"k": "v"})
        d = e.to_dict()
        assert d["kind"] == "PHASE_BEGIN"
        assert d["region"] == "PHASE_STREAM"
        assert d["role"] == "METADATA"
        assert d["op_id"] == "op-123"
        assert d["metadata"] == {"k": "v"}
        assert d["schema_version"] == rc.RENDER_CONDUCTOR_SCHEMA_VERSION


# ---------------------------------------------------------------------------
# §D — DefaultTheme
# ---------------------------------------------------------------------------


class TestDefaultTheme:
    def test_baseline_palette_resolves(self):
        t = rc.DefaultTheme()
        assert t.resolve(rc.ColorRole.SUCCESS, rc.RenderDensity.NORMAL) == "green"
        assert t.resolve(rc.ColorRole.ERROR, rc.RenderDensity.NORMAL) == "red"
        assert t.resolve(rc.ColorRole.WARNING, rc.RenderDensity.NORMAL) == "yellow"

    def test_compact_suppresses_metadata(self):
        t = rc.DefaultTheme()
        assert t.resolve(rc.ColorRole.METADATA, rc.RenderDensity.COMPACT) == ""
        assert t.resolve(rc.ColorRole.MUTED, rc.RenderDensity.COMPACT) == ""

    def test_compact_preserves_content(self):
        t = rc.DefaultTheme()
        assert t.resolve(rc.ColorRole.SUCCESS, rc.RenderDensity.COMPACT) == "green"

    def test_full_adds_italic_to_metadata(self):
        t = rc.DefaultTheme()
        result = t.resolve(rc.ColorRole.METADATA, rc.RenderDensity.FULL)
        assert "italic" in result
        assert "dim" in result

    def test_full_preserves_other_roles(self):
        t = rc.DefaultTheme()
        assert t.resolve(rc.ColorRole.SUCCESS, rc.RenderDensity.FULL) == "green"

    def test_normal_passthrough(self):
        t = rc.DefaultTheme()
        assert t.resolve(rc.ColorRole.METADATA, rc.RenderDensity.NORMAL) == "dim"

    def test_overlay_overrides_baseline(self):
        t = rc.DefaultTheme(overlay={rc.ColorRole.ERROR: "bold magenta"})
        assert t.resolve(
            rc.ColorRole.ERROR, rc.RenderDensity.NORMAL,
        ) == "bold magenta"
        # Untouched roles retain baseline
        assert t.resolve(
            rc.ColorRole.SUCCESS, rc.RenderDensity.NORMAL,
        ) == "green"

    def test_from_flag_registry_picks_up_overlay(
        self, monkeypatch: pytest.MonkeyPatch, fresh_registry,
    ):
        monkeypatch.setenv(
            "JARVIS_RENDER_CONDUCTOR_PALETTE_OVERRIDE",
            '{"ERROR": "bold red on white"}',
        )
        t = rc.DefaultTheme.from_flag_registry()
        assert t.resolve(
            rc.ColorRole.ERROR, rc.RenderDensity.NORMAL,
        ) == "bold red on white"


# ---------------------------------------------------------------------------
# §E — ThemeRegistry
# ---------------------------------------------------------------------------


class TestThemeRegistry:
    def test_default_theme_always_present(self):
        tr = rc.ThemeRegistry()
        assert "default" in tr.names()

    def test_register_adds(self):
        tr = rc.ThemeRegistry()
        custom = rc.DefaultTheme()
        custom.name = "custom"  # override class attr on instance
        tr.register(custom)
        assert "custom" in tr.names()

    def test_get_unknown_falls_back_to_default(self):
        tr = rc.ThemeRegistry()
        t = tr.get("nonexistent-theme")
        assert t.name == "default"

    def test_register_empty_name_silently_rejected(self):
        tr = rc.ThemeRegistry()
        bad = rc.DefaultTheme()
        bad.name = ""
        tr.register(bad)
        assert "" not in tr.names()

    def test_names_sorted(self):
        tr = rc.ThemeRegistry()
        for n in ("zeta", "alpha", "beta"):
            t = rc.DefaultTheme()
            t.name = n
            tr.register(t)
        names = tr.names()
        assert list(names) == sorted(names)

    def test_clear_for_tests_resets(self):
        tr = rc.ThemeRegistry()
        extra = rc.DefaultTheme()
        extra.name = "extra"
        tr.register(extra)
        assert "extra" in tr.names()
        tr.clear_for_tests()
        assert tr.names() == ("default",)


# ---------------------------------------------------------------------------
# §F — resolve_density precedence chain
# ---------------------------------------------------------------------------


class TestResolveDensity:
    def test_explicit_override_wins(
        self, monkeypatch: pytest.MonkeyPatch, fresh_registry,
    ):
        monkeypatch.setenv(
            "JARVIS_RENDER_CONDUCTOR_DENSITY_OVERRIDE", "compact",
        )
        result = rc.resolve_density(
            "EXPLORE", explicit_override=rc.RenderDensity.FULL,
        )
        assert result is rc.RenderDensity.FULL  # explicit beats env

    def test_env_override_beats_posture(
        self, monkeypatch: pytest.MonkeyPatch, fresh_registry,
    ):
        monkeypatch.setenv(
            "JARVIS_RENDER_CONDUCTOR_DENSITY_OVERRIDE", "full",
        )
        assert rc.resolve_density("HARDEN") is rc.RenderDensity.FULL

    def test_posture_default_explore_to_full(self, fresh_registry):
        assert rc.resolve_density("EXPLORE") is rc.RenderDensity.FULL

    def test_posture_default_consolidate_to_normal(self, fresh_registry):
        assert rc.resolve_density("CONSOLIDATE") is rc.RenderDensity.NORMAL

    def test_posture_default_harden_to_compact(self, fresh_registry):
        assert rc.resolve_density("HARDEN") is rc.RenderDensity.COMPACT

    def test_posture_default_maintain_to_compact(self, fresh_registry):
        assert rc.resolve_density("MAINTAIN") is rc.RenderDensity.COMPACT

    def test_posture_map_env_override(
        self, monkeypatch: pytest.MonkeyPatch, fresh_registry,
    ):
        monkeypatch.setenv(
            "JARVIS_RENDER_CONDUCTOR_POSTURE_DENSITY_MAP",
            '{"HARDEN": "full"}',
        )
        assert rc.resolve_density("HARDEN") is rc.RenderDensity.FULL

    def test_unknown_posture_falls_to_normal(self, fresh_registry):
        assert rc.resolve_density("MARS") is rc.RenderDensity.NORMAL

    def test_none_posture_falls_to_normal(self, fresh_registry):
        assert rc.resolve_density(None) is rc.RenderDensity.NORMAL

    def test_malformed_posture_map_silently_skipped(
        self, monkeypatch: pytest.MonkeyPatch, fresh_registry,
    ):
        monkeypatch.setenv(
            "JARVIS_RENDER_CONDUCTOR_POSTURE_DENSITY_MAP",
            'NOT_VALID_JSON',
        )
        # Falls through to in-code default
        assert rc.resolve_density("HARDEN") is rc.RenderDensity.COMPACT

    def test_case_insensitive_posture(self, fresh_registry):
        assert rc.resolve_density("explore") is rc.RenderDensity.FULL
        assert rc.resolve_density("Harden") is rc.RenderDensity.COMPACT


# ---------------------------------------------------------------------------
# §G — Flag accessors
# ---------------------------------------------------------------------------


class TestFlagAccessors:
    def test_is_enabled_default_true_post_slice7(self, fresh_registry):
        # Graduated default true at Slice 7. Hot-revert via env still works.
        assert rc.is_enabled() is True

    def test_is_enabled_env_true(
        self, monkeypatch: pytest.MonkeyPatch, fresh_registry,
    ):
        monkeypatch.setenv("JARVIS_RENDER_CONDUCTOR_ENABLED", "true")
        assert rc.is_enabled() is True

    def test_is_enabled_env_zero(
        self, monkeypatch: pytest.MonkeyPatch, fresh_registry,
    ):
        monkeypatch.setenv("JARVIS_RENDER_CONDUCTOR_ENABLED", "0")
        assert rc.is_enabled() is False

    def test_theme_name_default(self, fresh_registry):
        assert rc.theme_name() == "default"

    def test_theme_name_env(
        self, monkeypatch: pytest.MonkeyPatch, fresh_registry,
    ):
        monkeypatch.setenv(
            "JARVIS_RENDER_CONDUCTOR_THEME_NAME", "high-contrast",
        )
        assert rc.theme_name() == "high-contrast"

    def test_theme_name_empty_falls_back_to_default(
        self, monkeypatch: pytest.MonkeyPatch, fresh_registry,
    ):
        monkeypatch.setenv("JARVIS_RENDER_CONDUCTOR_THEME_NAME", "   ")
        assert rc.theme_name() == "default"

    def test_density_override_empty_returns_none(self, fresh_registry):
        assert rc.density_override() is None

    def test_density_override_compact(
        self, monkeypatch: pytest.MonkeyPatch, fresh_registry,
    ):
        monkeypatch.setenv(
            "JARVIS_RENDER_CONDUCTOR_DENSITY_OVERRIDE", "compact",
        )
        assert rc.density_override() is rc.RenderDensity.COMPACT

    def test_density_override_unknown_returns_none(
        self, monkeypatch: pytest.MonkeyPatch, fresh_registry,
    ):
        monkeypatch.setenv(
            "JARVIS_RENDER_CONDUCTOR_DENSITY_OVERRIDE", "ultra",
        )
        assert rc.density_override() is None

    def test_posture_density_overrides_parses(
        self, monkeypatch: pytest.MonkeyPatch, fresh_registry,
    ):
        monkeypatch.setenv(
            "JARVIS_RENDER_CONDUCTOR_POSTURE_DENSITY_MAP",
            '{"EXPLORE": "compact", "HARDEN": "full"}',
        )
        m = rc.posture_density_overrides()
        assert m["EXPLORE"] is rc.RenderDensity.COMPACT
        assert m["HARDEN"] is rc.RenderDensity.FULL

    def test_posture_density_overrides_silently_skips_malformed(
        self, monkeypatch: pytest.MonkeyPatch, fresh_registry,
    ):
        monkeypatch.setenv(
            "JARVIS_RENDER_CONDUCTOR_POSTURE_DENSITY_MAP",
            '{"EXPLORE": "compact", "BAD": "ultra", "HARDEN": 42}',
        )
        m = rc.posture_density_overrides()
        assert "EXPLORE" in m
        assert "BAD" not in m  # unknown density rejected
        assert "HARDEN" not in m  # non-string rejected

    def test_palette_override_parses(
        self, monkeypatch: pytest.MonkeyPatch, fresh_registry,
    ):
        monkeypatch.setenv(
            "JARVIS_RENDER_CONDUCTOR_PALETTE_OVERRIDE",
            '{"ERROR": "bold red on white"}',
        )
        m = rc.palette_override()
        assert m[rc.ColorRole.ERROR] == "bold red on white"

    def test_palette_override_skips_unknown_role(
        self, monkeypatch: pytest.MonkeyPatch, fresh_registry,
    ):
        monkeypatch.setenv(
            "JARVIS_RENDER_CONDUCTOR_PALETTE_OVERRIDE",
            '{"NONEXISTENT": "blue", "ERROR": "red"}',
        )
        m = rc.palette_override()
        assert rc.ColorRole.ERROR in m
        assert len(m) == 1


# ---------------------------------------------------------------------------
# §H — RenderConductor
# ---------------------------------------------------------------------------


class TestRenderConductor:
    def test_default_construction_seeds_all_regions(self):
        c = rc.RenderConductor()
        kinds = set(c.regions().keys())
        assert kinds == set(rc.RegionKind)

    def test_publish_no_op_when_master_off(
        self, monkeypatch: pytest.MonkeyPatch, fresh_registry,
    ):
        # Hot-revert path: explicit env=false drops events even
        # though the default is now true.
        monkeypatch.setenv("JARVIS_RENDER_CONDUCTOR_ENABLED", "false")
        c = rc.RenderConductor()
        rec = RecordingBackend()
        c.add_backend(rec)
        c.publish(_make_event())
        assert rec.events == []

    def test_publish_dispatches_when_master_on(
        self, monkeypatch: pytest.MonkeyPatch, fresh_registry,
    ):
        monkeypatch.setenv("JARVIS_RENDER_CONDUCTOR_ENABLED", "true")
        c = rc.RenderConductor()
        rec = RecordingBackend()
        c.add_backend(rec)
        ev = _make_event()
        c.publish(ev)
        assert rec.events == [ev]

    def test_publish_fan_out_to_multiple_backends(
        self, monkeypatch: pytest.MonkeyPatch, fresh_registry,
    ):
        monkeypatch.setenv("JARVIS_RENDER_CONDUCTOR_ENABLED", "true")
        c = rc.RenderConductor()
        a, b = RecordingBackend(), RecordingBackend()
        c.add_backend(a)
        c.add_backend(b)
        c.publish(_make_event())
        assert len(a.events) == 1
        assert len(b.events) == 1

    def test_add_backend_idempotent(self):
        c = rc.RenderConductor()
        rec = RecordingBackend()
        c.add_backend(rec)
        c.add_backend(rec)
        assert len(c.backends()) == 1

    def test_remove_backend_returns_true_when_found(self):
        c = rc.RenderConductor()
        rec = RecordingBackend()
        c.add_backend(rec)
        assert c.remove_backend(rec) is True
        assert c.backends() == ()

    def test_remove_backend_returns_false_when_missing(self):
        c = rc.RenderConductor()
        assert c.remove_backend(RecordingBackend()) is False

    def test_backend_exception_does_not_block_others(
        self, monkeypatch: pytest.MonkeyPatch, fresh_registry,
    ):
        monkeypatch.setenv("JARVIS_RENDER_CONDUCTOR_ENABLED", "true")
        c = rc.RenderConductor()
        c.add_backend(RaisingBackend())
        good = RecordingBackend()
        c.add_backend(good)
        c.publish(_make_event())
        assert len(good.events) == 1

    def test_flush_calls_all_backends(self):
        c = rc.RenderConductor()
        a, b = RecordingBackend(), RecordingBackend()
        c.add_backend(a)
        c.add_backend(b)
        c.flush()
        assert a.flush_calls == 1
        assert b.flush_calls == 1

    def test_flush_swallows_backend_exceptions(self):
        c = rc.RenderConductor()
        c.add_backend(RaisingBackend())
        good = RecordingBackend()
        c.add_backend(good)
        c.flush()
        assert good.flush_calls == 1

    def test_shutdown_calls_all_backends(self):
        c = rc.RenderConductor()
        a, b = RecordingBackend(), RecordingBackend()
        c.add_backend(a)
        c.add_backend(b)
        c.shutdown()
        assert a.shutdown_calls == 1
        assert b.shutdown_calls == 1

    def test_shutdown_swallows_backend_exceptions(self):
        c = rc.RenderConductor()
        c.add_backend(RaisingBackend())
        good = RecordingBackend()
        c.add_backend(good)
        c.shutdown()
        assert good.shutdown_calls == 1

    def test_region_lookup(self):
        c = rc.RenderConductor()
        r = c.region(rc.RegionKind.STATUS)
        assert r.kind is rc.RegionKind.STATUS

    def test_regions_returns_read_only_view(self):
        c = rc.RenderConductor()
        view = c.regions()
        with pytest.raises(TypeError):
            view[rc.RegionKind.HEADER] = rc.Region(  # type: ignore[index]
                kind=rc.RegionKind.HEADER,
            )

    def test_active_theme_consults_flag(
        self, monkeypatch: pytest.MonkeyPatch, fresh_registry,
    ):
        c = rc.RenderConductor()
        assert c.active_theme().name == "default"

    def test_active_density_uses_posture_provider(self, fresh_registry):
        c = rc.RenderConductor()
        c.set_posture_provider(lambda: "EXPLORE")
        assert c.active_density() is rc.RenderDensity.FULL
        c.set_posture_provider(lambda: "HARDEN")
        assert c.active_density() is rc.RenderDensity.COMPACT

    def test_active_density_programmatic_override_wins(self, fresh_registry):
        c = rc.RenderConductor()
        c.set_posture_provider(lambda: "EXPLORE")
        c.set_density_override(rc.RenderDensity.COMPACT)
        assert c.active_density() is rc.RenderDensity.COMPACT

    def test_active_density_provider_exception_safe(self, fresh_registry):
        c = rc.RenderConductor()
        def boom() -> str:
            raise RuntimeError("provider down")
        c.set_posture_provider(boom)
        # Falls through to NORMAL when provider raises
        assert c.active_density() is rc.RenderDensity.NORMAL

    def test_thread_safety_concurrent_add(self):
        c = rc.RenderConductor()
        backends = [RecordingBackend() for _ in range(20)]
        threads = [threading.Thread(target=c.add_backend, args=(b,))
                   for b in backends]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(c.backends()) == 20


# ---------------------------------------------------------------------------
# §I — Singleton triplet
# ---------------------------------------------------------------------------


class TestSingleton:
    def test_get_returns_none_initially(self):
        rc.reset_render_conductor()
        assert rc.get_render_conductor() is None

    def test_register_sets_singleton(self):
        rc.reset_render_conductor()
        c = rc.RenderConductor()
        rc.register_render_conductor(c)
        assert rc.get_render_conductor() is c
        rc.reset_render_conductor()

    def test_reset_clears(self):
        rc.register_render_conductor(rc.RenderConductor())
        rc.reset_render_conductor()
        assert rc.get_render_conductor() is None


# ---------------------------------------------------------------------------
# §J — register_flags
# ---------------------------------------------------------------------------


class TestRegisterFlags:
    def test_returns_five(self, fresh_registry):
        from backend.core.ouroboros.governance import flag_registry as fr
        # Drop our seed entries and re-register via our function only
        reg = fr.FlagRegistry()
        count = rc.register_flags(reg)
        assert count == 5

    def test_all_flag_names_use_prefix(self, fresh_registry):
        from backend.core.ouroboros.governance import flag_registry as fr
        reg = fr.FlagRegistry()
        rc.register_flags(reg)
        names = [s.name for s in reg.list_all()]
        assert all(n.startswith("JARVIS_RENDER_CONDUCTOR_") for n in names)

    def test_idempotent(self, fresh_registry):
        from backend.core.ouroboros.governance import flag_registry as fr
        reg = fr.FlagRegistry()
        rc.register_flags(reg)
        rc.register_flags(reg)
        # Override mode means second call replaces (no duplicate)
        assert len(reg.list_all()) == 5

    def test_master_flag_is_bool_default_true_post_slice7(
        self, fresh_registry,
    ):
        from backend.core.ouroboros.governance import flag_registry as fr
        reg = fr.FlagRegistry()
        rc.register_flags(reg)
        spec = reg.get_spec("JARVIS_RENDER_CONDUCTOR_ENABLED")
        assert spec is not None
        assert spec.type is fr.FlagType.BOOL
        assert spec.default is True  # graduated at Slice 7
        assert spec.category is fr.Category.SAFETY


# ---------------------------------------------------------------------------
# §K — register_shipped_invariants
# ---------------------------------------------------------------------------


class TestRegisterShippedInvariants:
    def test_returns_seven(self):
        invs = rc.register_shipped_invariants()
        assert len(invs) == 7

    def test_all_target_render_conductor(self):
        invs = rc.register_shipped_invariants()
        for inv in invs:
            assert inv.target_file == (
                "backend/core/ouroboros/governance/render_conductor.py"
            )

    def test_invariant_names_unique_and_prefixed(self):
        invs = rc.register_shipped_invariants()
        names = [i.invariant_name for i in invs]
        assert len(set(names)) == len(names)
        assert all(n.startswith("render_conductor_") for n in names)


# ---------------------------------------------------------------------------
# §L — AST pins self-validate green against the real shipped file
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def conductor_ast() -> tuple:
    """Parse the shipped render_conductor.py once; share across pin tests."""
    import inspect
    src = inspect.getsource(rc)
    return ast.parse(src), src


class TestASTPinsSelfValidate:
    def test_no_rich_import_clean(self, conductor_ast):
        tree, src = conductor_ast
        assert rc._validate_no_rich_import(tree, src) == ()

    def test_no_authority_imports_clean(self, conductor_ast):
        tree, src = conductor_ast
        assert rc._validate_no_authority_imports(tree, src) == ()

    def test_color_role_closed_clean(self, conductor_ast):
        tree, src = conductor_ast
        assert rc._validate_color_role_closed(tree, src) == ()

    def test_region_kind_closed_clean(self, conductor_ast):
        tree, src = conductor_ast
        assert rc._validate_region_kind_closed(tree, src) == ()

    def test_render_density_closed_clean(self, conductor_ast):
        tree, src = conductor_ast
        assert rc._validate_render_density_closed(tree, src) == ()

    def test_event_kind_closed_clean(self, conductor_ast):
        tree, src = conductor_ast
        assert rc._validate_event_kind_closed(tree, src) == ()

    def test_discovery_symbols_present_clean(self, conductor_ast):
        tree, src = conductor_ast
        assert rc._validate_discovery_symbols_present(tree, src) == ()


class TestASTPinsCatchTampering:
    def test_no_rich_import_catches_tampered(self):
        tampered = ast.parse("import rich.live\n")
        violations = rc._validate_no_rich_import(tampered, "import rich.live\n")
        assert any("rich" in v for v in violations)

    def test_no_authority_imports_catches_tampered(self):
        tampered = ast.parse(
            "from backend.core.ouroboros.governance.orchestrator import x\n"
        )
        violations = rc._validate_no_authority_imports(tampered, "")
        assert any("orchestrator" in v for v in violations)

    def test_color_role_closed_catches_added_member(self):
        tampered_src = (
            'class ColorRole:\n'
            '    METADATA = "METADATA"\n'
            '    CONTENT = "CONTENT"\n'
            '    SUCCESS = "SUCCESS"\n'
            '    WARNING = "WARNING"\n'
            '    ERROR = "ERROR"\n'
            '    EMPHASIS = "EMPHASIS"\n'
            '    MUTED = "MUTED"\n'
            '    NEW_ROLE = "NEW_ROLE"\n'
        )
        tampered = ast.parse(tampered_src)
        violations = rc._validate_color_role_closed(tampered, tampered_src)
        assert violations  # should report drift

    def test_color_role_closed_catches_removed_member(self):
        tampered_src = (
            'class ColorRole:\n'
            '    METADATA = "METADATA"\n'
        )
        tampered = ast.parse(tampered_src)
        violations = rc._validate_color_role_closed(tampered, tampered_src)
        assert violations

    def test_discovery_symbols_present_catches_missing(self):
        tampered = ast.parse("def something_else(): pass\n")
        violations = rc._validate_discovery_symbols_present(tampered, "")
        assert violations
        assert any("register_flags" in v for v in violations)


# ---------------------------------------------------------------------------
# §M — Auto-discovery integration
# ---------------------------------------------------------------------------


class TestAutoDiscoveryIntegration:
    def test_flag_registry_seed_picks_up_conductor(self, fresh_registry):
        names = [s.name for s in fresh_registry.list_all()]
        assert "JARVIS_RENDER_CONDUCTOR_ENABLED" in names
        assert "JARVIS_RENDER_CONDUCTOR_THEME_NAME" in names
        assert "JARVIS_RENDER_CONDUCTOR_DENSITY_OVERRIDE" in names
        assert "JARVIS_RENDER_CONDUCTOR_POSTURE_DENSITY_MAP" in names
        assert "JARVIS_RENDER_CONDUCTOR_PALETTE_OVERRIDE" in names

    def test_shipped_invariants_includes_conductor_pins(self):
        from backend.core.ouroboros.governance.meta import (
            shipped_code_invariants as sci,
        )
        names = [i.invariant_name for i in sci.list_shipped_code_invariants()]
        assert "render_conductor_no_rich_import" in names
        assert "render_conductor_no_authority_imports" in names
        assert "render_conductor_color_role_closed_taxonomy" in names

    def test_validate_all_includes_no_conductor_violations(self):
        from backend.core.ouroboros.governance.meta import (
            shipped_code_invariants as sci,
        )
        results = sci.validate_all()
        ours = [r for r in results
                if r.invariant_name.startswith("render_conductor_")]
        assert ours == [], (
            f"Conductor pins reporting violations: "
            f"{[r.to_dict() for r in ours]}"
        )
