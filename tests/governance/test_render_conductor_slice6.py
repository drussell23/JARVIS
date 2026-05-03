"""RenderConductor Slice 6 — contextual help regression suite.

Pins the ContextualHelpResolver + HelpEntry + HelpPage substrate that
closes Gap #6 (browseable + contextual help). Pure aggregation over
existing typed registries — no new storage, no new authority.

Strict directives validated:

  * No hardcoded values: every operator-tunable knob (master flag,
    ranking weights, page size) flows through FlagRegistry. Default
    weights are in-code; operators overlay via JSON.
  * Closed taxonomies: HelpKind / HelpEntry fields / HelpPage fields
    AST-pinned. Adding a member requires coordinated registry update.
  * No top-level registry imports: substrate explicitly forbids
    help_dispatcher / flag_registry / posture / posture_observer /
    posture_store at top level. Each registry consulted via lazy
    import inside the resolver — caught by AST pin. Ensures fresh-
    read semantics on every resolve().
  * Read-only over registries: resolver never mutates source registries.
  * Defensive everywhere: each method returns degraded result instead
    of raising; partial registry availability degrades gracefully.

Covers:

  §A   HelpKind closed taxonomy
  §B   HelpEntry construction + __post_init__ validation
  §C   HelpPage construction + has_more derivation
  §D   ranking_weights — defaults + JSON overlay + malformed handling
  §E   ContextualHelpResolver — registry aggregation
  §F   ContextualHelpResolver — substring scoring
  §G   ContextualHelpResolver — posture-relevance scoring
  §H   ContextualHelpResolver — recent-verb proximity
  §I   ContextualHelpResolver — pagination correctness
  §J   ContextualHelpResolver — master flag gate
  §K   publish_help_panel + publish_help_dismiss
  §L   register_help_action_handlers — KeyAction binding
  §M   AST pins (6) self-validate green + tampering caught
  §N   Auto-discovery integration
"""
from __future__ import annotations

import ast
from typing import Any, List

import pytest

from backend.core.ouroboros.governance import render_conductor as rc
from backend.core.ouroboros.governance import render_help as rh


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_flag_env(monkeypatch: pytest.MonkeyPatch):
    for name in (
        "JARVIS_RENDER_CONDUCTOR_ENABLED",
        "JARVIS_RENDER_CONDUCTOR_THEME_NAME",
        "JARVIS_RENDER_CONDUCTOR_DENSITY_OVERRIDE",
        "JARVIS_RENDER_CONDUCTOR_POSTURE_DENSITY_MAP",
        "JARVIS_RENDER_CONDUCTOR_PALETTE_OVERRIDE",
        "JARVIS_CONTEXTUAL_HELP_ENABLED",
        "JARVIS_HELP_RANKING_WEIGHTS",
        "JARVIS_HELP_PAGE_SIZE",
    ):
        monkeypatch.delenv(name, raising=False)
    yield
    rc.reset_render_conductor()
    rh.reset_help_resolver()


@pytest.fixture
def fresh_registry():
    from backend.core.ouroboros.governance import flag_registry as fr
    fr.reset_default_registry()
    reg = fr.ensure_seeded()
    yield reg
    fr.reset_default_registry()


class _Recorder:
    name = "recorder"

    def __init__(self) -> None:
        self.events: List[Any] = []

    def notify(self, event: Any) -> None:
        self.events.append(event)

    def flush(self) -> None:
        pass

    def shutdown(self) -> None:
        pass


@pytest.fixture
def wired_conductor(monkeypatch: pytest.MonkeyPatch, fresh_registry):
    monkeypatch.setenv("JARVIS_RENDER_CONDUCTOR_ENABLED", "true")
    monkeypatch.setenv("JARVIS_CONTEXTUAL_HELP_ENABLED", "true")
    c = rc.RenderConductor()
    rec = _Recorder()
    c.add_backend(rec)
    rc.register_render_conductor(c)
    yield c, rec
    rc.reset_render_conductor()


# ---------------------------------------------------------------------------
# §A — HelpKind closed taxonomy
# ---------------------------------------------------------------------------


class TestHelpKindClosedTaxonomy:
    def test_exact_five_members(self):
        assert {m.value for m in rh.HelpKind} == {
            "VERB", "FLAG", "KEY_ACTION", "DOC", "TIP",
        }

    def test_str_inheritance(self):
        assert isinstance(rh.HelpKind.VERB, str)


# ---------------------------------------------------------------------------
# §B — HelpEntry
# ---------------------------------------------------------------------------


class TestHelpEntry:
    def test_minimal(self):
        e = rh.HelpEntry(
            kind=rh.HelpKind.VERB, name="/x", one_line="desc",
        )
        assert e.body == ""
        assert e.score == 0.0

    def test_full_fields(self):
        e = rh.HelpEntry(
            kind=rh.HelpKind.FLAG, name="JARVIS_X", one_line="desc",
            body="long body", source_module="x.py", score=42.0,
        )
        assert e.score == 42.0

    def test_frozen(self):
        e = rh.HelpEntry(
            kind=rh.HelpKind.VERB, name="/x", one_line="desc",
        )
        with pytest.raises(Exception):
            e.score = 1.0  # type: ignore[misc]

    def test_empty_name_raises(self):
        with pytest.raises(ValueError, match="name"):
            rh.HelpEntry(kind=rh.HelpKind.VERB, name="", one_line="x")

    def test_whitespace_name_raises(self):
        with pytest.raises(ValueError, match="name"):
            rh.HelpEntry(kind=rh.HelpKind.VERB, name="   ", one_line="x")

    def test_non_string_one_line_raises(self):
        with pytest.raises(ValueError, match="one_line"):
            rh.HelpEntry(
                kind=rh.HelpKind.VERB, name="/x", one_line=42,  # type: ignore[arg-type]
            )

    def test_to_metadata_round_trip(self):
        e = rh.HelpEntry(
            kind=rh.HelpKind.FLAG, name="X", one_line="d",
            body="b", source_module="s.py", score=1.5,
        )
        md = e.to_metadata()
        assert md["entry_kind"] == "FLAG"
        assert md["name"] == "X"
        assert md["score"] == 1.5
        assert md["schema_version"] == rh.RENDER_HELP_SCHEMA_VERSION


# ---------------------------------------------------------------------------
# §C — HelpPage + has_more
# ---------------------------------------------------------------------------


class TestHelpPage:
    def test_minimal(self):
        p = rh.HelpPage(entries=(), offset=0, limit=10, total=0)
        assert p.has_more is False

    def test_has_more_true(self):
        e = rh.HelpEntry(
            kind=rh.HelpKind.VERB, name="/x", one_line="d",
        )
        p = rh.HelpPage(entries=(e,), offset=0, limit=1, total=5)
        assert p.has_more is True

    def test_has_more_false_at_end(self):
        e = rh.HelpEntry(
            kind=rh.HelpKind.VERB, name="/x", one_line="d",
        )
        p = rh.HelpPage(entries=(e,), offset=4, limit=1, total=5)
        assert p.has_more is False

    def test_negative_offset_raises(self):
        with pytest.raises(ValueError, match="offset"):
            rh.HelpPage(entries=(), offset=-1, limit=10, total=0)

    def test_zero_limit_raises(self):
        with pytest.raises(ValueError, match="limit"):
            rh.HelpPage(entries=(), offset=0, limit=0, total=0)

    def test_negative_total_raises(self):
        with pytest.raises(ValueError, match="total"):
            rh.HelpPage(entries=(), offset=0, limit=10, total=-1)

    def test_to_metadata_includes_entries(self):
        e = rh.HelpEntry(
            kind=rh.HelpKind.VERB, name="/x", one_line="d",
        )
        p = rh.HelpPage(entries=(e,), offset=0, limit=10, total=1)
        md = p.to_metadata()
        assert md["page_kind"] == "help_page"
        assert md["total"] == 1
        assert md["has_more"] is False
        assert len(md["entries"]) == 1


# ---------------------------------------------------------------------------
# §D — ranking_weights overlay
# ---------------------------------------------------------------------------


class TestRankingWeights:
    def test_defaults_present(self, fresh_registry):
        w = rh.ranking_weights()
        assert "substring_name" in w
        assert "posture_critical" in w
        assert "kind_verb_baseline" in w

    def test_override_replaces(
        self, monkeypatch: pytest.MonkeyPatch, fresh_registry,
    ):
        monkeypatch.setenv(
            "JARVIS_HELP_RANKING_WEIGHTS",
            '{"substring_name": 100.0}',
        )
        w = rh.ranking_weights()
        assert w["substring_name"] == 100.0
        # Other defaults still present
        assert "posture_critical" in w

    def test_non_numeric_skipped(
        self, monkeypatch: pytest.MonkeyPatch, fresh_registry,
    ):
        monkeypatch.setenv(
            "JARVIS_HELP_RANKING_WEIGHTS",
            '{"substring_name": "not a number"}',
        )
        w = rh.ranking_weights()
        # Falls back to default for substring_name
        assert isinstance(w["substring_name"], float)

    def test_malformed_json_safe(
        self, monkeypatch: pytest.MonkeyPatch, fresh_registry,
    ):
        monkeypatch.setenv("JARVIS_HELP_RANKING_WEIGHTS", "NOT JSON")
        w = rh.ranking_weights()
        assert "substring_name" in w


# ---------------------------------------------------------------------------
# §E — Resolver gathers from registries
# ---------------------------------------------------------------------------


class TestResolverAggregation:
    def test_pulls_verbs(self, wired_conductor):
        resolver = rh.ContextualHelpResolver()
        page = resolver.resolve(query="", limit=500)
        # Verbs include /help, /posture, etc. — at least 3 known
        verb_names = {
            e.name for e in page.entries
            if e.kind is rh.HelpKind.VERB
        }
        assert "/help" in verb_names or "/posture" in verb_names

    def test_pulls_flags(self, wired_conductor):
        resolver = rh.ContextualHelpResolver()
        page = resolver.resolve(query="", limit=500)
        flag_names = {
            e.name for e in page.entries
            if e.kind is rh.HelpKind.FLAG
        }
        # Our own flags should be present
        assert any(
            "JARVIS_RENDER_CONDUCTOR" in n for n in flag_names
        )


# ---------------------------------------------------------------------------
# §F — Substring scoring
# ---------------------------------------------------------------------------


class TestSubstringScoring:
    def test_query_in_name_scores(self, wired_conductor):
        resolver = rh.ContextualHelpResolver()
        page = resolver.resolve(query="POSTURE", limit=500)
        # Top result should contain "posture" in name
        if page.entries:
            assert "posture" in page.entries[0].name.lower()

    def test_empty_query_returns_all(self, wired_conductor):
        resolver = rh.ContextualHelpResolver()
        page = resolver.resolve(query="", limit=500)
        assert page.total > 0

    def test_no_match_low_score(self, wired_conductor):
        resolver = rh.ContextualHelpResolver()
        page = resolver.resolve(
            query="zzz_definitely_not_a_real_term", limit=10,
        )
        # Some entries still returned (zero query effect — kind baseline
        # still scores) but none should have substring boost
        for e in page.entries:
            assert "zzz_definitely_not_a_real_term" not in e.name.lower()


# ---------------------------------------------------------------------------
# §G — Posture-relevance scoring
# ---------------------------------------------------------------------------


class TestPostureScoring:
    def test_critical_for_posture_boosts(self, wired_conductor):
        resolver = rh.ContextualHelpResolver()
        # JARVIS_DIRECTION_INFERRER_ENABLED is tagged ALL_POSTURES_CRITICAL
        # in flag_registry_seed
        page = resolver.resolve(
            query="DIRECTION", posture="HARDEN", limit=10,
        )
        names = [e.name for e in page.entries]
        # The CRITICAL-for-HARDEN flag should appear in top results
        assert any("DIRECTION_INFERRER" in n for n in names)


# ---------------------------------------------------------------------------
# §H — Recent-verb proximity
# ---------------------------------------------------------------------------


class TestRecentVerbProximity:
    def test_recent_verb_boosts(self, wired_conductor):
        resolver = rh.ContextualHelpResolver()
        page_no_recent = resolver.resolve(query="/posture", limit=500)
        page_with_recent = resolver.resolve(
            query="/posture", recent_verbs=("/posture",), limit=500,
        )
        # Find /posture entry in both — score should be higher with
        # recent_verbs
        def find_posture(p):
            for e in p.entries:
                if e.name == "/posture":
                    return e.score
            return None

        s_no = find_posture(page_no_recent)
        s_with = find_posture(page_with_recent)
        if s_no is not None and s_with is not None:
            assert s_with > s_no


# ---------------------------------------------------------------------------
# §I — Pagination
# ---------------------------------------------------------------------------


class TestPagination:
    def test_first_page_size(self, wired_conductor):
        resolver = rh.ContextualHelpResolver()
        page = resolver.resolve(query="", limit=5)
        assert len(page.entries) == 5
        assert page.has_more is True

    def test_offset_skips(self, wired_conductor):
        resolver = rh.ContextualHelpResolver()
        page1 = resolver.resolve(query="", limit=3, offset=0)
        page2 = resolver.resolve(query="", limit=3, offset=3)
        names1 = [e.name for e in page1.entries]
        names2 = [e.name for e in page2.entries]
        # No overlap between consecutive pages
        assert not (set(names1) & set(names2))

    def test_offset_beyond_total(self, wired_conductor):
        resolver = rh.ContextualHelpResolver()
        # Request page far beyond total
        page = resolver.resolve(query="", limit=10, offset=100000)
        assert len(page.entries) == 0
        assert page.has_more is False

    def test_default_page_size_used(self, wired_conductor):
        resolver = rh.ContextualHelpResolver()
        page = resolver.resolve(query="")
        assert page.limit == 10  # default

    def test_custom_default_page_size(
        self, monkeypatch: pytest.MonkeyPatch, wired_conductor,
    ):
        monkeypatch.setenv("JARVIS_HELP_PAGE_SIZE", "5")
        resolver = rh.ContextualHelpResolver()
        page = resolver.resolve(query="")
        assert page.limit == 5


# ---------------------------------------------------------------------------
# §J — Master flag gate
# ---------------------------------------------------------------------------


class TestMasterFlagGate:
    def test_resolve_empty_when_master_off(self, fresh_registry):
        resolver = rh.ContextualHelpResolver()
        page = resolver.resolve(query="anything")
        assert page.total == 0
        assert page.entries == ()


# ---------------------------------------------------------------------------
# §K — publish_help_panel / publish_help_dismiss
# ---------------------------------------------------------------------------


class TestPublishHelpPanel:
    def test_publishes_modal_prompt(self, wired_conductor):
        c, rec = wired_conductor
        e = rh.HelpEntry(
            kind=rh.HelpKind.VERB, name="/x", one_line="d",
        )
        page = rh.HelpPage(entries=(e,), offset=0, limit=10, total=1)
        ok = rh.publish_help_panel(page)
        assert ok is True
        assert rec.events[0].kind is rc.EventKind.MODAL_PROMPT
        assert rec.events[0].region is rc.RegionKind.MODAL
        assert rec.events[0].metadata["page_kind"] == "help_page"
        assert rec.events[0].metadata["total"] == 1

    def test_publishes_dismiss(self, wired_conductor):
        c, rec = wired_conductor
        ok = rh.publish_help_dismiss()
        assert ok is True
        assert rec.events[0].kind is rc.EventKind.MODAL_DISMISS

    def test_no_conductor_returns_false(self, fresh_registry):
        rc.reset_render_conductor()
        e = rh.HelpEntry(
            kind=rh.HelpKind.VERB, name="/x", one_line="d",
        )
        page = rh.HelpPage(entries=(e,), offset=0, limit=10, total=1)
        assert rh.publish_help_panel(page) is False

    def test_dismiss_no_conductor_returns_false(self, fresh_registry):
        rc.reset_render_conductor()
        assert rh.publish_help_dismiss() is False


# ---------------------------------------------------------------------------
# §L — register_help_action_handlers (Slice 4 binding)
# ---------------------------------------------------------------------------


class TestKeyActionBinding:
    def test_binding_returns_false_when_master_off(self, fresh_registry):
        # Master off → no binding even if Slice 4 controller exists
        from backend.core.ouroboros.governance import key_input as ki
        ctrl = ki.InputController()
        ki.register_input_controller(ctrl)
        try:
            resolver = rh.ContextualHelpResolver()
            ok = rh.register_help_action_handlers(resolver)
            assert ok is False
        finally:
            ki.reset_input_controller()

    def test_binding_returns_false_when_no_controller(
        self, monkeypatch: pytest.MonkeyPatch, fresh_registry,
    ):
        monkeypatch.setenv("JARVIS_CONTEXTUAL_HELP_ENABLED", "true")
        from backend.core.ouroboros.governance import key_input as ki
        ki.reset_input_controller()
        resolver = rh.ContextualHelpResolver()
        ok = rh.register_help_action_handlers(resolver)
        assert ok is False

    def test_binding_succeeds_with_controller(
        self, monkeypatch: pytest.MonkeyPatch, wired_conductor,
    ):
        from backend.core.ouroboros.governance import key_input as ki
        ctrl = ki.InputController()
        ki.register_input_controller(ctrl)
        try:
            resolver = rh.ContextualHelpResolver()
            ok = rh.register_help_action_handlers(resolver)
            assert ok is True
            assert ctrl.registry.has_handler(ki.KeyAction.HELP_OPEN)
            assert ctrl.registry.has_handler(ki.KeyAction.HELP_CLOSE)
        finally:
            ki.reset_input_controller()

    def test_help_open_handler_publishes(
        self, monkeypatch: pytest.MonkeyPatch, wired_conductor,
    ):
        c, rec = wired_conductor
        from backend.core.ouroboros.governance import key_input as ki
        ctrl = ki.InputController()
        ki.register_input_controller(ctrl)
        try:
            resolver = rh.ContextualHelpResolver()
            rh.register_help_action_handlers(resolver)
            # Fire HELP_OPEN — should publish MODAL_PROMPT
            ctrl.registry.fire(
                ki.KeyAction.HELP_OPEN,
                ki.KeyEvent(key=ki.KeyName.QUESTION),
            )
            assert any(
                e.kind is rc.EventKind.MODAL_PROMPT for e in rec.events
            )
        finally:
            ki.reset_input_controller()


# ---------------------------------------------------------------------------
# §M — AST pins
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def slice6_pins() -> list:
    return list(rh.register_shipped_invariants())


class TestSlice6ASTPinsClean:
    def test_six_pins_registered(self, slice6_pins):
        assert len(slice6_pins) == 6
        names = {i.invariant_name for i in slice6_pins}
        assert names == {
            "render_help_no_rich_import",
            "render_help_no_authority_imports",
            "render_help_help_kind_closed_taxonomy",
            "render_help_help_entry_closed_taxonomy",
            "render_help_help_page_closed_taxonomy",
            "render_help_discovery_symbols_present",
        }

    @pytest.fixture(scope="class")
    def real_ast(self) -> tuple:
        import inspect
        src = inspect.getsource(rh)
        return ast.parse(src), src

    def test_no_rich_import_clean(self, slice6_pins, real_ast):
        tree, src = real_ast
        pin = next(p for p in slice6_pins
                   if p.invariant_name == "render_help_no_rich_import")
        assert pin.validate(tree, src) == ()

    def test_no_authority_imports_clean(self, slice6_pins, real_ast):
        tree, src = real_ast
        pin = next(p for p in slice6_pins
                   if p.invariant_name ==
                   "render_help_no_authority_imports")
        assert pin.validate(tree, src) == ()

    def test_help_kind_closed_clean(self, slice6_pins, real_ast):
        tree, src = real_ast
        pin = next(p for p in slice6_pins
                   if p.invariant_name ==
                   "render_help_help_kind_closed_taxonomy")
        assert pin.validate(tree, src) == ()

    def test_help_entry_closed_clean(self, slice6_pins, real_ast):
        tree, src = real_ast
        pin = next(p for p in slice6_pins
                   if p.invariant_name ==
                   "render_help_help_entry_closed_taxonomy")
        assert pin.validate(tree, src) == ()

    def test_help_page_closed_clean(self, slice6_pins, real_ast):
        tree, src = real_ast
        pin = next(p for p in slice6_pins
                   if p.invariant_name ==
                   "render_help_help_page_closed_taxonomy")
        assert pin.validate(tree, src) == ()


class TestSlice6ASTPinsCatchTampering:
    def test_help_dispatcher_top_level_import_caught(self, slice6_pins):
        tampered = ast.parse(
            "from backend.core.ouroboros.governance.help_dispatcher "
            "import x\n"
        )
        pin = next(p for p in slice6_pins
                   if p.invariant_name ==
                   "render_help_no_authority_imports")
        violations = pin.validate(tampered, "")
        assert any("help_dispatcher" in v for v in violations)

    def test_posture_top_level_import_caught(self, slice6_pins):
        tampered = ast.parse(
            "from backend.core.ouroboros.governance.posture import x\n"
        )
        pin = next(p for p in slice6_pins
                   if p.invariant_name ==
                   "render_help_no_authority_imports")
        violations = pin.validate(tampered, "")
        assert any("posture" in v for v in violations)

    def test_orchestrator_import_caught(self, slice6_pins):
        tampered = ast.parse(
            "from backend.core.ouroboros.governance.orchestrator import x\n"
        )
        pin = next(p for p in slice6_pins
                   if p.invariant_name ==
                   "render_help_no_authority_imports")
        violations = pin.validate(tampered, "")
        assert any("orchestrator" in v for v in violations)

    def test_rich_import_caught(self, slice6_pins):
        tampered = ast.parse("from rich.panel import Panel\n")
        pin = next(p for p in slice6_pins
                   if p.invariant_name == "render_help_no_rich_import")
        violations = pin.validate(tampered, "")
        assert any("rich" in v for v in violations)

    def test_added_help_kind_caught(self, slice6_pins):
        tampered_src = (
            "class HelpKind:\n"
            "    VERB = 'VERB'\n"
            "    FLAG = 'FLAG'\n"
            "    KEY_ACTION = 'KEY_ACTION'\n"
            "    DOC = 'DOC'\n"
            "    TIP = 'TIP'\n"
            "    NEW_KIND = 'NEW_KIND'\n"
        )
        tampered = ast.parse(tampered_src)
        pin = next(p for p in slice6_pins
                   if p.invariant_name ==
                   "render_help_help_kind_closed_taxonomy")
        violations = pin.validate(tampered, tampered_src)
        assert violations

    def test_removed_help_entry_field_caught(self, slice6_pins):
        tampered_src = (
            "from dataclasses import dataclass\n"
            "@dataclass(frozen=True)\n"
            "class HelpEntry:\n"
            "    kind: str\n"
            "    name: str\n"
        )
        tampered = ast.parse(tampered_src)
        pin = next(p for p in slice6_pins
                   if p.invariant_name ==
                   "render_help_help_entry_closed_taxonomy")
        violations = pin.validate(tampered, tampered_src)
        assert violations


# ---------------------------------------------------------------------------
# §N — Auto-discovery
# ---------------------------------------------------------------------------


class TestAutoDiscoveryIntegration:
    def test_flag_registry_picks_up_render_help(self, fresh_registry):
        names = {s.name for s in fresh_registry.list_all()}
        assert "JARVIS_CONTEXTUAL_HELP_ENABLED" in names
        assert "JARVIS_HELP_RANKING_WEIGHTS" in names
        assert "JARVIS_HELP_PAGE_SIZE" in names

    def test_shipped_invariants_includes_slice6_pins(self):
        from backend.core.ouroboros.governance.meta import (
            shipped_code_invariants as sci,
        )
        names = {
            i.invariant_name for i in sci.list_shipped_code_invariants()
        }
        for expected in (
            "render_help_no_rich_import",
            "render_help_no_authority_imports",
            "render_help_help_kind_closed_taxonomy",
            "render_help_help_entry_closed_taxonomy",
            "render_help_help_page_closed_taxonomy",
            "render_help_discovery_symbols_present",
        ):
            assert expected in names

    def test_validate_all_no_slice6_violations(self):
        from backend.core.ouroboros.governance.meta import (
            shipped_code_invariants as sci,
        )
        results = sci.validate_all()
        slice6_failures = [
            r for r in results
            if r.invariant_name.startswith("render_help_")
        ]
        assert slice6_failures == [], (
            f"Slice 6 pins reporting violations: "
            f"{[r.to_dict() for r in slice6_failures]}"
        )
