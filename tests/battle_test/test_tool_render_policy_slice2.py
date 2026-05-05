"""Tests for tool_render_policy (Gap #2 Slice 2).

Validates the adaptive DensityPolicy resolver:
  • 3-step precedence (override > env > table)
  • Declarative resolution table covers all (Posture × LayoutKind) pairs
  • Defensive fallbacks for unknown / non-string inputs
  • Provider Protocols accept stub implementations
  • Default providers degrade gracefully when stateful surfaces unavailable
  • DI cage: no top-level import of stateful posture/layout surfaces
"""
from __future__ import annotations

from typing import Optional
from unittest import mock

import pytest

from backend.core.ouroboros.battle_test.tool_render_policy import (
    DefaultLayoutModeProvider,
    DefaultPostureProvider,
    DensityLevel,
    DensityPolicy,
    LayoutKind,
    LayoutModeProvider,
    PostureProvider,
    TOOL_RENDER_POLICY_SCHEMA_VERSION,
    classify_layout,
    read_env_override,
    resolve_density,
    resolve_density_via_providers,
)
from backend.core.ouroboros.governance.posture import Posture


# ===========================================================================
# Schema + closed taxonomies
# ===========================================================================


def test_schema_version_pinned():
    assert TOOL_RENDER_POLICY_SCHEMA_VERSION == "tool_render_policy.v1"


def test_density_level_closed_taxonomy():
    assert {m.value for m in DensityLevel} == {
        "compact", "balanced", "verbose",
    }


def test_layout_kind_closed_taxonomy():
    assert {m.value for m in LayoutKind} == {"flow", "split", "focus"}


# ===========================================================================
# DensityLevel.coerce
# ===========================================================================


@pytest.mark.parametrize("raw,expected", [
    ("compact", DensityLevel.COMPACT),
    ("BALANCED", DensityLevel.BALANCED),
    ("  verbose  ", DensityLevel.VERBOSE),
    (DensityLevel.COMPACT, DensityLevel.COMPACT),
])
def test_coerce_accepts(raw, expected):
    assert DensityLevel.coerce(raw) is expected


@pytest.mark.parametrize("raw", [None, "", "garbage", 42, object()])
def test_coerce_rejects(raw):
    assert DensityLevel.coerce(raw) is None


# ===========================================================================
# classify_layout — 3-kind taxonomy
# ===========================================================================


@pytest.mark.parametrize("mode,expected", [
    ("flow", LayoutKind.FLOW),
    ("split", LayoutKind.SPLIT),
    ("focus:stream", LayoutKind.FOCUS),
    ("focus:dashboard", LayoutKind.FOCUS),
    ("focus:diff", LayoutKind.FOCUS),
])
def test_classify_layout_known_modes(mode: str, expected: LayoutKind):
    assert classify_layout(mode) is expected


@pytest.mark.parametrize("mode", [
    None, "", "garbage", "FLOW",  # case-sensitive
    42, object(),
])
def test_classify_layout_unknown_falls_back_to_flow(mode):
    assert classify_layout(mode) is LayoutKind.FLOW


def test_classify_layout_unknown_focus_region_still_focus():
    """Region validity is LayoutController's concern; LayoutKind only
    cares about the screen-shape family. Anything matching the
    ``focus:`` prefix is FOCUS-shaped — the region (valid or not)
    doesn't change vertical real-estate semantics."""
    assert classify_layout("focus:not_a_region") is LayoutKind.FOCUS


# ===========================================================================
# DensityPolicy — frozen + projection
# ===========================================================================


def test_density_policy_show_body_predicate():
    p = DensityPolicy(
        level=DensityLevel.COMPACT,
        max_body_lines=0, max_summary_chars=60, provenance="t",
    )
    assert p.show_body is False

    p2 = DensityPolicy(
        level=DensityLevel.BALANCED,
        max_body_lines=10, max_summary_chars=80, provenance="t",
    )
    assert p2.show_body is True


def test_density_policy_is_frozen():
    p = DensityPolicy(
        level=DensityLevel.BALANCED,
        max_body_lines=10, max_summary_chars=80, provenance="t",
    )
    with pytest.raises(Exception):
        p.max_body_lines = 99  # type: ignore[misc]


def test_density_policy_to_dict_shape():
    p = DensityPolicy(
        level=DensityLevel.VERBOSE,
        max_body_lines=30, max_summary_chars=120,
        provenance="env:verbose",
    )
    d = p.to_dict()
    assert d["level"] == "verbose"
    assert d["max_body_lines"] == 30
    assert d["max_summary_chars"] == 120
    assert d["provenance"] == "env:verbose"
    assert d["schema_version"] == TOOL_RENDER_POLICY_SCHEMA_VERSION


# ===========================================================================
# read_env_override — clean env + happy paths + bad input
# ===========================================================================


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch):
    """Remove the density env var for every test; tests opt in via setenv."""
    monkeypatch.delenv("JARVIS_TOOL_RENDER_DENSITY", raising=False)


def test_read_env_override_unset_returns_none():
    assert read_env_override() is None


def test_read_env_override_blank_returns_none(monkeypatch):
    monkeypatch.setenv("JARVIS_TOOL_RENDER_DENSITY", "   ")
    assert read_env_override() is None


@pytest.mark.parametrize("raw,expected", [
    ("compact", DensityLevel.COMPACT),
    ("balanced", DensityLevel.BALANCED),
    ("verbose", DensityLevel.VERBOSE),
    ("VERBOSE", DensityLevel.VERBOSE),
])
def test_read_env_override_recognized_levels(monkeypatch, raw, expected):
    monkeypatch.setenv("JARVIS_TOOL_RENDER_DENSITY", raw)
    assert read_env_override() is expected


@pytest.mark.parametrize("raw", ["dense", "tight", "0", "true"])
def test_read_env_override_unrecognized_returns_none(monkeypatch, raw):
    monkeypatch.setenv("JARVIS_TOOL_RENDER_DENSITY", raw)
    assert read_env_override() is None


# ===========================================================================
# Resolution table — full (Posture × LayoutKind) sweep
# ===========================================================================


_TABLE_EXPECTATIONS = [
    # (posture, layout, expected_level)
    (Posture.HARDEN, "flow", DensityLevel.COMPACT),
    (Posture.HARDEN, "split", DensityLevel.COMPACT),
    (Posture.HARDEN, "focus:stream", DensityLevel.BALANCED),
    (Posture.CONSOLIDATE, "flow", DensityLevel.BALANCED),
    (Posture.CONSOLIDATE, "split", DensityLevel.COMPACT),
    (Posture.CONSOLIDATE, "focus:diff", DensityLevel.VERBOSE),
    (Posture.MAINTAIN, "flow", DensityLevel.BALANCED),
    (Posture.MAINTAIN, "split", DensityLevel.COMPACT),
    (Posture.MAINTAIN, "focus:dashboard", DensityLevel.VERBOSE),
    (Posture.EXPLORE, "flow", DensityLevel.VERBOSE),
    (Posture.EXPLORE, "split", DensityLevel.BALANCED),
    (Posture.EXPLORE, "focus:stream", DensityLevel.VERBOSE),
]


@pytest.mark.parametrize("posture,layout,expected", _TABLE_EXPECTATIONS)
def test_resolution_table_full_sweep(
    posture: Posture, layout: str, expected: DensityLevel,
):
    policy = resolve_density(posture, layout, skip_env=True)
    assert policy.level is expected


def test_resolution_table_provenance_format():
    policy = resolve_density(Posture.HARDEN, "split", skip_env=True)
    assert policy.provenance == "table:HARDEN×split"


# ===========================================================================
# Resolution precedence — override > env > table
# ===========================================================================


def test_explicit_override_beats_env(monkeypatch):
    monkeypatch.setenv("JARVIS_TOOL_RENDER_DENSITY", "verbose")
    policy = resolve_density(
        Posture.EXPLORE, "flow",
        explicit_override=DensityLevel.COMPACT,
    )
    assert policy.level is DensityLevel.COMPACT
    assert policy.provenance == "override:compact"


def test_env_beats_table(monkeypatch):
    monkeypatch.setenv("JARVIS_TOOL_RENDER_DENSITY", "compact")
    policy = resolve_density(Posture.EXPLORE, "flow")  # would be VERBOSE
    assert policy.level is DensityLevel.COMPACT
    assert policy.provenance == "env:compact"


def test_skip_env_bypasses_env_var(monkeypatch):
    monkeypatch.setenv("JARVIS_TOOL_RENDER_DENSITY", "verbose")
    policy = resolve_density(Posture.HARDEN, "flow", skip_env=True)
    # Without env interference: HARDEN×flow → COMPACT
    assert policy.level is DensityLevel.COMPACT


# ===========================================================================
# Resolution fallbacks — defensive
# ===========================================================================


def test_none_posture_treated_as_maintain():
    policy = resolve_density(None, "flow", skip_env=True)
    # MAINTAIN×flow → BALANCED
    assert policy.level is DensityLevel.BALANCED
    assert "MAINTAIN" in policy.provenance


def test_garbage_posture_treated_as_maintain():
    policy = resolve_density("not a posture", "flow", skip_env=True)
    assert policy.level is DensityLevel.BALANCED
    assert "MAINTAIN" in policy.provenance


def test_none_layout_treated_as_flow():
    policy = resolve_density(Posture.HARDEN, None, skip_env=True)
    assert policy.level is DensityLevel.COMPACT  # HARDEN×flow
    assert "flow" in policy.provenance


def test_garbage_layout_treated_as_flow():
    policy = resolve_density(Posture.HARDEN, "garbage", skip_env=True)
    assert policy.level is DensityLevel.COMPACT
    assert "flow" in policy.provenance


# ===========================================================================
# Providers — Protocol acceptance + DI
# ===========================================================================


class _StubPostureProvider:
    def __init__(self, value: Optional[Posture]):
        self._v = value

    def current(self) -> Optional[Posture]:
        return self._v


class _StubLayoutProvider:
    def __init__(self, value: Optional[str]):
        self._v = value

    def current(self) -> Optional[str]:
        return self._v


def test_stub_providers_satisfy_protocol():
    # ``runtime_checkable`` lets isinstance check structural conformance.
    assert isinstance(_StubPostureProvider(Posture.HARDEN), PostureProvider)
    assert isinstance(_StubLayoutProvider("flow"), LayoutModeProvider)


def test_resolve_via_providers_happy_path():
    policy = resolve_density_via_providers(
        _StubPostureProvider(Posture.EXPLORE),
        _StubLayoutProvider("focus:diff"),
        skip_env=True,
    )
    # EXPLORE×focus → VERBOSE
    assert policy.level is DensityLevel.VERBOSE


def test_resolve_via_providers_handles_provider_exception():
    class _RaisingPosture:
        def current(self):
            raise RuntimeError("posture provider exploded")

    class _RaisingLayout:
        def current(self):
            raise RuntimeError("layout provider exploded")

    policy = resolve_density_via_providers(
        _RaisingPosture(), _RaisingLayout(), skip_env=True,
    )
    # Both None → MAINTAIN×flow → BALANCED (safe fallback)
    assert policy.level is DensityLevel.BALANCED


def test_resolve_via_providers_partial_failure():
    """Posture raises but layout works — should still route via the
    layout dimension, treating posture as MAINTAIN."""
    class _RaisingPosture:
        def current(self):
            raise RuntimeError("nope")

    policy = resolve_density_via_providers(
        _RaisingPosture(), _StubLayoutProvider("split"),
        skip_env=True,
    )
    # MAINTAIN×split → COMPACT
    assert policy.level is DensityLevel.COMPACT


# ===========================================================================
# Default production providers — smoke tests
# ===========================================================================


def test_default_posture_provider_does_not_raise():
    """Even with no posture state on disk, provider must return None
    (never raise) — the lazy-import + defensive try/except guarantees
    Slice 4 wiring can't crash on a cold start."""
    p = DefaultPostureProvider()
    result = p.current()
    assert result is None or isinstance(result, Posture)


def test_default_layout_provider_returns_string_or_none():
    p = DefaultLayoutModeProvider()
    result = p.current()
    assert result is None or isinstance(result, str)


def test_default_posture_provider_handles_missing_module():
    """Simulate the module being unavailable — provider degrades to None."""
    p = DefaultPostureProvider()
    with mock.patch(
        "backend.core.ouroboros.governance.posture_observer.get_default_store",
        side_effect=Exception("simulated"),
    ):
        assert p.current() is None


# ===========================================================================
# DI cage — substrate must NOT import stateful posture/layout surfaces
# at module top level
# ===========================================================================


def test_substrate_does_not_top_level_import_stateful_surfaces():
    """The Slice 5 AST pin will mechanically enforce this; this test
    is the smoke check that lives with the substrate itself."""
    from backend.core.ouroboros.battle_test import tool_render_policy
    src = open(tool_render_policy.__file__).read()
    # Find the *top-level* import block (everything before the first
    # ``def`` or ``class`` keyword at column 0). Lazy imports inside
    # methods do NOT count toward this cage.
    top_lines = []
    for line in src.splitlines():
        if line.startswith(("def ", "class ")):
            break
        top_lines.append(line)
    top = "\n".join(top_lines)
    # Stateful runtime modules — must NOT appear at top level.
    forbidden = (
        "from backend.core.ouroboros.governance.posture_observer ",
        "import backend.core.ouroboros.governance.posture_observer",
        "from backend.core.ouroboros.governance.posture_store ",
        "import backend.core.ouroboros.governance.posture_store",
        "from backend.core.ouroboros.governance.posture_health ",
        "import backend.core.ouroboros.governance.posture_health",
    )
    for needle in forbidden:
        assert needle not in top, (
            f"DI cage violation: tool_render_policy must NOT top-level "
            f"import {needle!r}; use a lazy import inside Default*Provider"
        )


def test_substrate_does_not_import_rich_or_console():
    """Layered design: policy.py is renderer-agnostic; Rich belongs
    to Slice 4's wiring layer."""
    from backend.core.ouroboros.battle_test import tool_render_policy
    src = open(tool_render_policy.__file__).read()
    for forbidden in (
        "from rich", "import rich",
        "from prompt_toolkit", "import prompt_toolkit",
    ):
        assert forbidden not in src
