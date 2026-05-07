"""§37 Tier 2 #13 Slice 5 — visual rendering integration spine.

Pins the additive integration of per-tool confidence into
``tool_render_view.compose()``. Existing render tests pass
through unchanged; new field is opt-in for downstream renderers.

Coverage (~22 tests):
  * ComposedToolRender gains ``confidence_band`` field with
    default-None (backward compat — frozen dataclass)
  * compose() populates field from singleton observer when
    master flag on + observer has band for stream
  * compose() returns None for confidence_band when master off
  * compose() returns None for confidence_band when no
    observation recorded yet
  * compose() returns None for confidence_band on broken
    observer (defensive)
  * confidence_band_markup helper:
      - returns "" for None / CERTAIN / HIGH (silent on safe pole)
      - returns styled glyph for MEDIUM / LOW / UNKNOWN
      - palette injection works
      - NEVER raises on malformed band
  * Composition discipline: tool_render_view.py composes
    get_default_observer (no parallel construction; AST scan)
  * Composition discipline: lazy-imports Slice 1 (no eager
    governance import at module load)
  * compose() existing fields (header / summary / body /
    expansion_hint / policy) untouched by Slice 5 addition
"""
from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import MagicMock

import pytest


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _module_path() -> Path:
    return (
        _repo_root()
        / "backend/core/ouroboros/battle_test/"
        "tool_render_view.py"
    )


@pytest.fixture(autouse=True)
def _reset_observer():
    from backend.core.ouroboros.governance.tool_confidence_warning_observer import (  # noqa: E501
        reset_default_observer_for_tests,
    )
    reset_default_observer_for_tests()
    yield
    reset_default_observer_for_tests()


# ---------------------------------------------------------------------------
# ComposedToolRender field — backward compat
# ---------------------------------------------------------------------------


def test_composed_tool_render_has_confidence_band_field():
    from backend.core.ouroboros.battle_test.tool_render_view import (
        ComposedToolRender,
    )
    fields = {f.name for f in ComposedToolRender.__dataclass_fields__.values()}
    assert "confidence_band" in fields


def test_composed_tool_render_default_none():
    """Backward compat: existing constructors that don't pass
    confidence_band still work."""
    from backend.core.ouroboros.battle_test.tool_render_view import (
        ComposedToolRender,
    )
    from backend.core.ouroboros.battle_test.tool_render_policy import (
        DensityPolicy,
    )
    r = ComposedToolRender(
        header_markup="x", summary_markup="y",
        body_lines_markup=(), expansion_hint="",
        policy=MagicMock(spec=DensityPolicy),
    )
    assert r.confidence_band is None


# ---------------------------------------------------------------------------
# compose() — confidence_band wiring
# ---------------------------------------------------------------------------


def test_compose_populates_confidence_band_when_master_on(
    monkeypatch,
):
    monkeypatch.setenv(
        "JARVIS_TOOL_CONFIDENCE_INDICATOR_ENABLED", "true",
    )
    from backend.core.ouroboros.battle_test.tool_render_view import (
        compose,
    )
    from backend.core.ouroboros.governance.tool_confidence_warning_observer import (  # noqa: E501
        ToolConfidenceBand, get_default_observer,
    )
    obs = get_default_observer()
    obs.record(
        confidence=0.10, op_id="op1", tool_name="read_file",
        publish_sse=False,
    )
    composed = compose(
        "read_file", "foo.py", "result text",
        op_id="op1", round_index=0,
    )
    assert composed.confidence_band == ToolConfidenceBand.UNKNOWN


def test_compose_returns_none_when_master_off(monkeypatch):
    monkeypatch.delenv(
        "JARVIS_TOOL_CONFIDENCE_INDICATOR_ENABLED",
        raising=False,
    )
    from backend.core.ouroboros.battle_test.tool_render_view import (
        compose,
    )
    from backend.core.ouroboros.governance.tool_confidence_warning_observer import (  # noqa: E501
        get_default_observer,
    )
    obs = get_default_observer()
    obs.record(
        confidence=0.10, op_id="op1", tool_name="read_file",
        publish_sse=False,
    )
    composed = compose(
        "read_file", "foo.py", "result text",
        op_id="op1", round_index=0,
    )
    assert composed.confidence_band is None


def test_compose_returns_none_when_no_observation(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_TOOL_CONFIDENCE_INDICATOR_ENABLED", "true",
    )
    from backend.core.ouroboros.battle_test.tool_render_view import (
        compose,
    )
    composed = compose(
        "read_file", "foo.py", "result text",
        op_id="op-no-record", round_index=0,
    )
    # No observation recorded for this op — no band.
    assert composed.confidence_band is None


def test_compose_returns_none_when_op_id_empty(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_TOOL_CONFIDENCE_INDICATOR_ENABLED", "true",
    )
    from backend.core.ouroboros.battle_test.tool_render_view import (
        compose,
    )
    composed = compose(
        "read_file", "foo.py", "result text",
        op_id="", round_index=0,
    )
    assert composed.confidence_band is None


def test_compose_returns_none_on_broken_observer(monkeypatch):
    """Defensive: observer outage → confidence_band None,
    other fields still produced."""
    monkeypatch.setenv(
        "JARVIS_TOOL_CONFIDENCE_INDICATOR_ENABLED", "true",
    )
    from backend.core.ouroboros.battle_test.tool_render_view import (
        compose,
    )
    from backend.core.ouroboros.governance import (
        tool_confidence_warning_observer as toolconf,
    )
    monkeypatch.setattr(
        toolconf, "get_default_observer",
        lambda: (_ for _ in ()).throw(
            RuntimeError("boom"),
        ),
    )
    composed = compose(
        "read_file", "foo.py", "result text",
        op_id="op1", round_index=0,
    )
    # Compose must NOT crash; band is None defensively.
    assert composed.confidence_band is None


def test_compose_existing_fields_untouched(monkeypatch):
    """Slice 5 added confidence_band; existing fields must
    behave identically."""
    monkeypatch.delenv(
        "JARVIS_TOOL_CONFIDENCE_INDICATOR_ENABLED",
        raising=False,
    )
    from backend.core.ouroboros.battle_test.tool_render_view import (
        compose,
    )
    composed = compose(
        "read_file", "foo.py", "result text",
        op_id="op1", round_index=0,
    )
    assert isinstance(composed.header_markup, str)
    assert isinstance(composed.summary_markup, str)
    assert isinstance(composed.body_lines_markup, tuple)
    assert isinstance(composed.expansion_hint, str)
    assert composed.policy is not None


# ---------------------------------------------------------------------------
# confidence_band_markup helper
# ---------------------------------------------------------------------------


def test_band_markup_silent_on_none():
    from backend.core.ouroboros.battle_test.tool_render_view import (
        confidence_band_markup,
    )
    assert confidence_band_markup(None) == ""


def test_band_markup_silent_on_certain():
    from backend.core.ouroboros.battle_test.tool_render_view import (
        confidence_band_markup,
    )
    from backend.core.ouroboros.governance.tool_confidence_warning_observer import (  # noqa: E501
        ToolConfidenceBand,
    )
    assert (
        confidence_band_markup(ToolConfidenceBand.CERTAIN) == ""
    )


def test_band_markup_silent_on_high():
    from backend.core.ouroboros.battle_test.tool_render_view import (
        confidence_band_markup,
    )
    from backend.core.ouroboros.governance.tool_confidence_warning_observer import (  # noqa: E501
        ToolConfidenceBand,
    )
    assert confidence_band_markup(ToolConfidenceBand.HIGH) == ""


@pytest.mark.parametrize(
    "band_name", ["MEDIUM", "LOW", "UNKNOWN"],
)
def test_band_markup_renders_glyph_for_unsafe_pole(band_name):
    from backend.core.ouroboros.battle_test.tool_render_view import (
        confidence_band_markup,
    )
    from backend.core.ouroboros.governance.tool_confidence_warning_observer import (  # noqa: E501
        ToolConfidenceBand,
    )
    band = getattr(ToolConfidenceBand, band_name)
    out = confidence_band_markup(band)
    assert out  # non-empty
    assert "?" in out  # the glyph
    # Rich-markup wrapped.
    assert out.startswith(" [") and out.endswith("]")


def test_band_markup_palette_injection():
    from backend.core.ouroboros.battle_test.tool_render_view import (
        confidence_band_markup,
    )
    from backend.core.ouroboros.governance.tool_confidence_warning_observer import (  # noqa: E501
        ToolConfidenceBand,
    )
    custom = {"code_del": "magenta"}
    out = confidence_band_markup(
        ToolConfidenceBand.LOW, palette=custom,
    )
    assert "magenta" in out


def test_band_markup_swallows_malformed_band():
    """Defensive: passing a non-band sentinel doesn't crash."""
    from backend.core.ouroboros.battle_test.tool_render_view import (
        confidence_band_markup,
    )

    class _Junk:
        value = 42  # non-string

    assert confidence_band_markup(_Junk()) == ""
    assert confidence_band_markup("not a band") == ""


# ---------------------------------------------------------------------------
# Composition discipline — AST scan
# ---------------------------------------------------------------------------


def test_compose_uses_singleton_observer():
    """tool_render_view.py MUST compose the canonical singleton
    via get_default_observer (no parallel construction)."""
    src = _module_path().read_text(encoding="utf-8")
    tree = ast.parse(src)
    found_get_default = False
    found_parallel_construction = False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if "tool_confidence_warning_observer" in module:
                names = {n.name for n in node.names}
                if "get_default_observer" in names:
                    found_get_default = True
        if isinstance(node, ast.Call):
            func = node.func
            if (
                isinstance(func, ast.Name)
                and func.id == "ToolConfidenceObserver"
            ):
                found_parallel_construction = True
    assert found_get_default, (
        "tool_render_view MUST lazy-import get_default_observer"
    )
    assert not found_parallel_construction, (
        "tool_render_view MUST NOT construct "
        "ToolConfidenceObserver directly — composition "
        "discipline (single source of truth)"
    )


def test_observer_lookup_lives_in_dedicated_helper():
    """The observer-touching code MUST be in a single helper
    function, not scattered through compose()."""
    src = _module_path().read_text(encoding="utf-8")
    tree = ast.parse(src)
    found_helper = False
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.FunctionDef)
            and node.name == "_read_confidence_band_for_compose"
        ):
            found_helper = True
            break
    assert found_helper, (
        "tool_render_view.py MUST expose "
        "_read_confidence_band_for_compose dedicated helper"
    )


def test_master_flag_gate_present_in_helper():
    """The helper MUST gate observer access on Slice 1's master
    flag (defense in depth + zero-overhead-when-off)."""
    src = _module_path().read_text(encoding="utf-8")
    tree = ast.parse(src)
    helper_fn = None
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.FunctionDef)
            and node.name == "_read_confidence_band_for_compose"
        ):
            helper_fn = node
            break
    assert helper_fn is not None
    found_master_call = False
    for sub in ast.walk(helper_fn):
        if isinstance(sub, ast.Call):
            func = sub.func
            if (
                isinstance(func, ast.Name)
                and func.id == "master_enabled"
            ):
                found_master_call = True
                break
    assert found_master_call, (
        "_read_confidence_band_for_compose MUST gate on "
        "master_enabled() — defense in depth"
    )


def test_module_does_not_eagerly_import_observer():
    """At module-load time, the Slice 1 module MUST NOT be in
    the top-level imports — lazy import only (composition
    discipline keeps tool_render_view substrate-pure)."""
    src = _module_path().read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in tree.body:  # top-level only
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            assert (
                "tool_confidence_warning_observer" not in module
            ), (
                f"tool_render_view.py MUST NOT eagerly import "
                f"tool_confidence_warning_observer — found at "
                f"top-level: {module!r}"
            )


# ---------------------------------------------------------------------------
# End-to-end render integration smoke
# ---------------------------------------------------------------------------


def test_end_to_end_low_confidence_renders_glyph(monkeypatch):
    """Operator workflow: tool fires at LOW confidence → compose
    populates band → renderer appends glyph via helper. The
    full visible path."""
    monkeypatch.setenv(
        "JARVIS_TOOL_CONFIDENCE_INDICATOR_ENABLED", "true",
    )
    from backend.core.ouroboros.battle_test.tool_render_view import (
        compose, confidence_band_markup,
    )
    from backend.core.ouroboros.governance.tool_confidence_warning_observer import (  # noqa: E501
        get_default_observer,
    )
    get_default_observer().record(
        confidence=0.10, op_id="op1", tool_name="bash",
        publish_sse=False,
    )
    composed = compose(
        "bash", "ls", "result text",
        op_id="op1", round_index=0,
    )
    glyph = confidence_band_markup(composed.confidence_band)
    assert glyph != ""
    assert "?" in glyph
