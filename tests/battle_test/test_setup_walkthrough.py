"""Regression spine for §41.3 #16 — setup walkthrough.

Substrate tests for `welcome_state.render_setup_walkthrough` +
the `/tutorial setup` dispatch wiring. Compositional — extends
the existing `/tutorial` verb without adding a new one, composes
the canonical `flag_registry.FlagRegistry` without parallel state.
Section structure auto-derives from the 8-slot Category enum —
adding a new category in flag_registry produces a new section
automatically.
"""
from __future__ import annotations

import ast
import inspect
import os
from pathlib import Path

import pytest

from backend.core.ouroboros.battle_test import (
    welcome_state as ws,
)
from backend.core.ouroboros.battle_test.welcome_state import (
    _FLAG_AT_DEFAULT_MARKER,
    _FLAG_OVERRIDDEN_MARKER,
    _FLAG_RELEVANCE_GLYPHS,
    _SETUP_SCOPES,
    _read_env_value,
    _relevance_glyph_for,
    render_setup_walkthrough,
)
from backend.core.ouroboros.governance.flag_registry import (
    Category,
    FlagRegistry,
    FlagSpec,
    FlagType,
    Relevance,
)


# ---------------------------------------------------------------------------
# Test fixtures — build a small in-memory registry
# ---------------------------------------------------------------------------


def _spec(
    name: str,
    *,
    category: Category = Category.SAFETY,
    type_: FlagType = FlagType.BOOL,
    default=True,
    description: str = "Test flag",
    example: str = "true",
    posture_relevance=None,
) -> FlagSpec:
    return FlagSpec(
        name=name,
        type=type_,
        default=default,
        description=description,
        category=category,
        source_file="test.py",
        example=example,
        posture_relevance=posture_relevance or {},
    )


@pytest.fixture
def small_registry():
    reg = FlagRegistry()
    reg.register(_spec("JARVIS_TEST_SAFETY_1", category=Category.SAFETY))
    reg.register(_spec(
        "JARVIS_TEST_SAFETY_2",
        category=Category.SAFETY,
        posture_relevance={"HARDEN": Relevance.CRITICAL},
    ))
    reg.register(_spec(
        "JARVIS_TEST_TIMING_1",
        category=Category.TIMING,
        type_=FlagType.INT,
        default=30,
    ))
    reg.register(_spec(
        "JARVIS_TEST_ROUTING_1",
        category=Category.ROUTING,
        posture_relevance={
            "EXPLORE": Relevance.RELEVANT,
            "HARDEN": Relevance.CRITICAL,
        },
    ))
    return reg


# ---------------------------------------------------------------------------
# Data-on-module tables (must remain stable for the format function)
# ---------------------------------------------------------------------------


def test_relevance_glyph_table_covers_3_values():
    keys = {k for k, _ in _FLAG_RELEVANCE_GLYPHS}
    assert keys == {"critical", "relevant", "ignored"}


def test_setup_scopes_taxonomy():
    assert set(_SETUP_SCOPES) == {"all", "critical", "relevant"}


def test_at_default_and_overridden_markers_distinct():
    assert _FLAG_AT_DEFAULT_MARKER != _FLAG_OVERRIDDEN_MARKER


# ---------------------------------------------------------------------------
# _relevance_glyph_for
# ---------------------------------------------------------------------------


def test_glyph_for_critical():
    assert _relevance_glyph_for(Relevance.CRITICAL) == "🔥"


def test_glyph_for_relevant():
    assert _relevance_glyph_for(Relevance.RELEVANT) == "📌"


def test_glyph_for_ignored():
    assert _relevance_glyph_for(Relevance.IGNORED) == "·"


def test_glyph_for_string_lookup():
    assert _relevance_glyph_for("critical") == "🔥"


def test_glyph_for_unknown_returns_dot():
    assert _relevance_glyph_for("bogus") == "·"


def test_glyph_for_none_safe():
    assert _relevance_glyph_for(None) == "·"


# ---------------------------------------------------------------------------
# _read_env_value
# ---------------------------------------------------------------------------


def test_read_env_value_default_when_unset(monkeypatch):
    monkeypatch.delenv("JARVIS_TEST_X", raising=False)
    value, overridden = _read_env_value("JARVIS_TEST_X", "fallback")
    assert value == "fallback"
    assert overridden is False


def test_read_env_value_overridden(monkeypatch):
    monkeypatch.setenv("JARVIS_TEST_X", "real")
    value, overridden = _read_env_value("JARVIS_TEST_X", "fallback")
    assert value == "real"
    assert overridden is True


def test_read_env_value_empty_string_treated_as_unset(monkeypatch):
    """An empty string in the env counts as not-set — operator
    sees the default rather than '' as the value."""
    monkeypatch.setenv("JARVIS_TEST_X", "")
    value, overridden = _read_env_value("JARVIS_TEST_X", "fallback")
    assert value == "fallback"
    assert overridden is False


def test_read_env_value_garbage_name_safe():
    value, overridden = _read_env_value(None, "x")
    assert overridden is False
    value2, overridden2 = _read_env_value(42, "x")
    assert overridden2 is False


# ---------------------------------------------------------------------------
# render_setup_walkthrough — happy path
# ---------------------------------------------------------------------------


def test_render_default_all_scope(small_registry):
    out = render_setup_walkthrough(
        small_registry, scope="all", max_per_section=0,
    )
    assert "Operator Setup" in out
    assert "JARVIS_TEST_SAFETY_1" in out
    assert "JARVIS_TEST_TIMING_1" in out
    assert "JARVIS_TEST_ROUTING_1" in out


def test_render_section_headers_from_category_enum(small_registry):
    """Section headers MUST derive from the Category enum value
    strings — no hardcoded section names."""
    out = render_setup_walkthrough(
        small_registry, scope="all", max_per_section=0,
    )
    assert "== SAFETY ==" in out
    assert "== TIMING ==" in out
    assert "== ROUTING ==" in out


def test_render_skips_empty_categories(small_registry):
    """Categories with zero flags MUST NOT produce a header."""
    out = render_setup_walkthrough(
        small_registry, scope="all", max_per_section=0,
    )
    # No flags in CAPACITY / OBSERVABILITY / INTEGRATION /
    # EXPERIMENTAL / TUNING — those headers shouldn't appear
    assert "== CAPACITY ==" not in out
    assert "== OBSERVABILITY ==" not in out


def test_render_includes_description_and_example(small_registry):
    out = render_setup_walkthrough(
        small_registry, scope="all", max_per_section=0,
    )
    assert "Test flag" in out
    assert "example: true" in out


def test_render_shows_default_marker_when_env_unset(
    small_registry, monkeypatch,
):
    monkeypatch.delenv("JARVIS_TEST_SAFETY_1", raising=False)
    out = render_setup_walkthrough(
        small_registry, scope="all", max_per_section=0,
    )
    assert _FLAG_AT_DEFAULT_MARKER in out


def test_render_shows_overridden_marker_when_env_set(
    small_registry, monkeypatch,
):
    monkeypatch.setenv("JARVIS_TEST_SAFETY_1", "false")
    out = render_setup_walkthrough(
        small_registry, scope="all", max_per_section=0,
    )
    assert _FLAG_OVERRIDDEN_MARKER in out


# ---------------------------------------------------------------------------
# Scope filtering
# ---------------------------------------------------------------------------


def test_scope_critical_requires_posture_to_filter(small_registry):
    """When scope='critical' and posture=None, the filter set
    is not built — the walkthrough degrades to scope='all'
    for the rendering layer (per the substrate's docstring)."""
    out = render_setup_walkthrough(
        small_registry, scope="critical", max_per_section=0,
    )
    # Without posture, no posture-filter applies; all flags
    # render. (The scope=critical message still surfaces in
    # the header.)
    assert "scope: critical" in out


def test_scope_critical_with_posture_filters(small_registry):
    out = render_setup_walkthrough(
        small_registry,
        scope="critical",
        posture="HARDEN",
        max_per_section=0,
    )
    # Only flags marked CRITICAL for HARDEN should appear
    assert "JARVIS_TEST_SAFETY_2" in out
    assert "JARVIS_TEST_ROUTING_1" in out
    # SAFETY_1 has no posture_relevance — filtered out
    assert "JARVIS_TEST_SAFETY_1" not in out


def test_scope_relevant_with_posture_includes_more(small_registry):
    """scope='relevant' includes CRITICAL + RELEVANT for the
    posture."""
    out = render_setup_walkthrough(
        small_registry,
        scope="relevant",
        posture="EXPLORE",
        max_per_section=0,
    )
    # ROUTING_1 is RELEVANT for EXPLORE — should appear
    assert "JARVIS_TEST_ROUTING_1" in out


def test_unknown_scope_falls_to_all(small_registry):
    """An invalid scope value gracefully falls back to 'all'."""
    out = render_setup_walkthrough(
        small_registry, scope="bogus", max_per_section=0,
    )
    # All flags should appear (no filter)
    assert "JARVIS_TEST_SAFETY_1" in out
    assert "JARVIS_TEST_TIMING_1" in out


# ---------------------------------------------------------------------------
# Category filter
# ---------------------------------------------------------------------------


def test_category_filter_restricts_to_one_section(small_registry):
    out = render_setup_walkthrough(
        small_registry,
        scope="all",
        category_filter="safety",
        max_per_section=0,
    )
    assert "== SAFETY ==" in out
    assert "== TIMING ==" not in out
    assert "== ROUTING ==" not in out


def test_category_filter_unknown_returns_message(small_registry):
    out = render_setup_walkthrough(
        small_registry, category_filter="nonexistent",
    )
    assert "no flags matched" in out


def test_category_filter_disables_cap(small_registry):
    """When operator drills into a category explicitly, the
    per-section cap doesn't apply — they want the full view."""
    # Add many flags to SAFETY
    for i in range(20):
        small_registry.register(_spec(
            f"JARVIS_TEST_BULK_{i}",
            category=Category.SAFETY,
        ))
    out = render_setup_walkthrough(
        small_registry,
        scope="critical",  # cap normally active
        category_filter="safety",
        max_per_section=5,
    )
    # When category_filter is set, cap is disabled — should
    # see all bulk flags
    for i in range(20):
        assert f"JARVIS_TEST_BULK_{i}" in out
    # No elision message
    assert "more in SAFETY" not in out


# ---------------------------------------------------------------------------
# max_per_section cap
# ---------------------------------------------------------------------------


def test_cap_applies_in_non_all_scope(small_registry):
    """Default cap should kick in for non-'all' scopes."""
    # Pad SAFETY with extras
    for i in range(15):
        small_registry.register(_spec(
            f"JARVIS_TEST_EXTRA_{i}",
            category=Category.SAFETY,
        ))
    out = render_setup_walkthrough(
        small_registry,
        scope="critical",
        max_per_section=5,
    )
    # Elision message present
    assert "more in SAFETY" in out


def test_cap_zero_disables_cap(small_registry):
    """max_per_section=0 means no cap — even non-'all' scopes
    show every flag."""
    for i in range(15):
        small_registry.register(_spec(
            f"JARVIS_TEST_EXTRA_{i}",
            category=Category.SAFETY,
        ))
    out = render_setup_walkthrough(
        small_registry,
        scope="critical",
        max_per_section=0,
    )
    # All should render
    for i in range(15):
        assert f"JARVIS_TEST_EXTRA_{i}" in out


def test_cap_bypassed_in_all_scope(small_registry):
    """scope='all' bypasses the cap regardless of value."""
    for i in range(15):
        small_registry.register(_spec(
            f"JARVIS_TEST_EXTRA_{i}",
            category=Category.SAFETY,
        ))
    out = render_setup_walkthrough(
        small_registry,
        scope="all",
        max_per_section=2,
    )
    # All 15 + the original 2 SAFETY flags = 17 — cap=2 ignored
    for i in range(15):
        assert f"JARVIS_TEST_EXTRA_{i}" in out


# ---------------------------------------------------------------------------
# Posture-relevance glyph in output
# ---------------------------------------------------------------------------


def test_posture_glyph_visible_when_posture_set(small_registry):
    out = render_setup_walkthrough(
        small_registry,
        scope="all",
        posture="HARDEN",
        max_per_section=0,
    )
    # JARVIS_TEST_SAFETY_2 is CRITICAL for HARDEN → fire glyph
    assert "🔥" in out


def test_no_glyph_when_no_posture_set(small_registry):
    out = render_setup_walkthrough(
        small_registry,
        scope="all",
        max_per_section=0,
    )
    # No posture → no relevance glyph (just space)
    assert "🔥" not in out
    assert "📌" not in out


# ---------------------------------------------------------------------------
# Error handling — NEVER raises
# ---------------------------------------------------------------------------


def test_render_with_none_registry_falls_back_to_default():
    """None means 'compose ensure_seeded()' — output should
    still be a string."""
    out = render_setup_walkthrough(None, scope="all")
    assert isinstance(out, str)


def test_render_with_garbage_scope(small_registry):
    out = render_setup_walkthrough(
        small_registry, scope=42, max_per_section=0,
    )
    assert isinstance(out, str)
    # Falls back to 'all'
    assert "JARVIS_TEST_SAFETY_1" in out


def test_render_with_garbage_posture(small_registry):
    out = render_setup_walkthrough(
        small_registry, scope="all", posture=12345, max_per_section=0,
    )
    assert isinstance(out, str)


def test_render_with_garbage_category_filter(small_registry):
    out = render_setup_walkthrough(
        small_registry, category_filter=None,
    )
    assert isinstance(out, str)


def test_render_never_raises_on_buggy_registry():
    """Buggy registry whose methods raise must NOT propagate."""

    class _BuggyRegistry:
        def list_by_category(self, _):
            raise RuntimeError("boom")

        def relevant_to_posture(self, _, **__):
            raise RuntimeError("boom")

    out = render_setup_walkthrough(_BuggyRegistry(), scope="all")
    # Should produce some defensive output, not crash
    assert isinstance(out, str)


# ---------------------------------------------------------------------------
# Composition — verify no parallel state
# ---------------------------------------------------------------------------


def test_composition_reads_canonical_flag_registry():
    """Bytes-pin: render_setup_walkthrough imports from the
    canonical flag_registry module — NO parallel registry."""
    src = Path(
        "backend/core/ouroboros/battle_test/welcome_state.py"
    ).read_text()
    idx = src.find("def render_setup_walkthrough")
    assert idx > 0
    body = src[idx:idx + 4000]
    assert "flag_registry" in body
    assert "Category" in body
    assert "ensure_seeded" in body


def test_composition_uses_list_by_category_method():
    """Bytes-pin: section structure walks via the canonical
    `list_by_category` method, NOT a parallel grouping."""
    src = Path(
        "backend/core/ouroboros/battle_test/welcome_state.py"
    ).read_text()
    idx = src.find("def render_setup_walkthrough")
    # The list_by_category call lives in the Category walk loop
    # which is deeper in the function body — widen the window.
    body = src[idx:idx + 10000]
    assert "list_by_category" in body


def test_composition_uses_relevant_to_posture_method():
    """Bytes-pin: posture filtering composes the canonical
    `relevant_to_posture` query, NOT a parallel relevance map."""
    src = Path(
        "backend/core/ouroboros/battle_test/welcome_state.py"
    ).read_text()
    idx = src.find("def render_setup_walkthrough")
    body = src[idx:idx + 4000]
    assert "relevant_to_posture" in body


def test_setup_walkthrough_exported():
    src = Path(
        "backend/core/ouroboros/battle_test/welcome_state.py"
    ).read_text()
    assert '"render_setup_walkthrough"' in src


# ---------------------------------------------------------------------------
# /tutorial setup wiring in SerpentREPL
# ---------------------------------------------------------------------------


def test_handle_tutorial_routes_setup_subcommand():
    """Bytes-pin: SerpentREPL._handle_tutorial detects the
    `setup` subcommand and dispatches to render_setup_walkthrough."""
    src = Path(
        "backend/core/ouroboros/battle_test/serpent_flow.py"
    ).read_text()
    # The setup-subcommand block must reference both:
    # - render_setup_walkthrough import
    # - a setup-token check
    assert "render_setup_walkthrough" in src


def test_handle_tutorial_setup_branch_present():
    src = Path(
        "backend/core/ouroboros/battle_test/serpent_flow.py"
    ).read_text()
    idx = src.find("def _handle_tutorial")
    assert idx > 0
    body = src[idx:idx + 5000]
    assert '"setup"' in body


def test_handle_tutorial_setup_renders_walkthrough_no_raise():
    """Smoke: invoke the full _handle_tutorial 'setup' branch via
    a minimal SerpentREPL stub — must not raise."""
    from backend.core.ouroboros.battle_test import serpent_flow

    rendered = []

    class _Console:
        def print(self, *args, **kwargs):
            if args:
                rendered.append(str(args[0]))
            else:
                rendered.append("")

    class _FakeFlow:
        def __init__(self):
            self.console = _Console()

    repl = serpent_flow.SerpentREPL(flow=_FakeFlow())
    # Invoke the setup branch
    repl._handle_tutorial("/tutorial setup")
    out = "\n".join(rendered)
    assert "Operator Setup" in out


def test_handle_tutorial_setup_category_filter():
    """Smoke: `/tutorial setup safety` narrows to one category."""
    from backend.core.ouroboros.battle_test import serpent_flow

    rendered = []

    class _Console:
        def print(self, *args, **kwargs):
            if args:
                rendered.append(str(args[0]))

    class _FakeFlow:
        def __init__(self):
            self.console = _Console()

    repl = serpent_flow.SerpentREPL(flow=_FakeFlow())
    repl._handle_tutorial("/tutorial setup safety")
    out = "\n".join(rendered)
    # Smoke: rendering succeeded and produced output
    assert len(out) > 0


def test_handle_tutorial_no_args_still_runs_verb_tour():
    """Backward compat: `/tutorial` with no args still runs the
    legacy verb-tour path, NOT the setup walkthrough."""
    from backend.core.ouroboros.battle_test import serpent_flow

    rendered = []

    class _Console:
        def print(self, *args, **kwargs):
            if args:
                rendered.append(str(args[0]))

    class _FakeFlow:
        def __init__(self):
            self.console = _Console()

    repl = serpent_flow.SerpentREPL(flow=_FakeFlow())
    repl._handle_tutorial("/tutorial")
    out = "\n".join(rendered)
    # Verb tour header (NOT setup walkthrough header)
    assert "Operator Tutorial" in out
    assert "Operator Setup" not in out


# ---------------------------------------------------------------------------
# Backward compat — legacy tutorial path unchanged
# ---------------------------------------------------------------------------


def test_legacy_tutorial_category_path_unchanged():
    """`/tutorial lifecycle` still routes to the verb-tour
    `render_tutorial(category_filter='lifecycle')` — no
    breaking change."""
    from backend.core.ouroboros.battle_test import serpent_flow

    rendered = []

    class _Console:
        def print(self, *args, **kwargs):
            if args:
                rendered.append(str(args[0]))

    class _FakeFlow:
        def __init__(self):
            self.console = _Console()

    repl = serpent_flow.SerpentREPL(flow=_FakeFlow())
    repl._handle_tutorial("/tutorial lifecycle")
    out = "\n".join(rendered)
    # Should be verb tutorial, not setup walkthrough
    assert "Operator Setup" not in out
