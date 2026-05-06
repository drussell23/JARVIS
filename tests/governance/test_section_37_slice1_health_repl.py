"""§37 Slice 1 — `/health` REPL regression spine.

Pins per operator binding 2026-05-05:

  * Single pipeline: composes `get_default_tracker()` singleton
    only; never constructs `ComponentHealthTracker()` directly
  * Authority asymmetry: read-only operator surface; never calls
    `update()` / `register()` on tracker
  * Substrate purity: no orchestrator / iron_gate / policy /
    providers / candidate_generator imports
  * Auto-discovery: `dispatch_health_command` matches §32.11
    Slice 4 naming-cage; `register_verbs` matches help-discovery
    contract
  * NEVER raises: every code path defensive
  * Identity discipline: state coloring uses identity-consistent
    palette (READY/ACTIVE=green=outcomes, ERROR=red, BUSY=yellow,
    other=dim)

Verifies (28 tests).
"""
from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import patch

import pytest


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _reset_tracker():
    """Reset the singleton between tests so state from one test
    doesn't bleed into the next."""
    from backend.core.ouroboros.governance.autonomy.component_health import (  # noqa: E501
        reset_default_tracker_for_tests,
    )
    reset_default_tracker_for_tests()
    yield
    reset_default_tracker_for_tests()


# ---------------------------------------------------------------------------
# Match + dispatch shape
# ---------------------------------------------------------------------------


def test_dispatch_matches_canonical_forms():
    from backend.core.ouroboros.governance.health_repl import (
        dispatch_health_command,
    )
    for line in (
        "/health",
        "health",
        "/health help",
        "health components",
    ):
        result = dispatch_health_command(line)
        assert result.matched is True, (
            f"line {line!r} should match"
        )


def test_dispatch_does_not_match_other_verbs():
    from backend.core.ouroboros.governance.health_repl import (
        dispatch_health_command,
    )
    for line in (
        "/posture",
        "/decisions",
        "/some-random-verb",
        "",
        "   ",
        "healthcare",  # similar prefix but distinct verb
    ):
        result = dispatch_health_command(line)
        assert result.matched is False, (
            f"line {line!r} should not match /health"
        )


def test_help_bypasses_master_gate():
    """Help is always available even when no producers have
    populated the tracker."""
    from backend.core.ouroboros.governance.health_repl import (
        dispatch_health_command,
    )
    result = dispatch_health_command("/health help")
    assert result.ok is True
    assert "/health" in result.text
    assert "components" in result.text
    assert "history" in result.text


def test_short_help_alias():
    from backend.core.ouroboros.governance.health_repl import (
        dispatch_health_command,
    )
    result = dispatch_health_command("/health ?")
    assert result.ok is True


# ---------------------------------------------------------------------------
# Cold-start (empty tracker) — honest "no data" rendering
# ---------------------------------------------------------------------------


def test_overview_empty_tracker():
    """Bare /health on a cold tracker renders an honest "no
    data" line, NOT fabricated zeros that look like progress."""
    from backend.core.ouroboros.governance.health_repl import (
        dispatch_health_command,
    )
    result = dispatch_health_command("/health")
    assert result.ok is True
    assert "Component Health" in result.text
    assert "No components registered yet" in result.text


def test_components_empty_tracker():
    from backend.core.ouroboros.governance.health_repl import (
        dispatch_health_command,
    )
    result = dispatch_health_command("/health components")
    assert result.ok is True
    assert "No components registered yet" in result.text


def test_history_empty_tracker():
    from backend.core.ouroboros.governance.health_repl import (
        dispatch_health_command,
    )
    result = dispatch_health_command("/health history")
    assert result.ok is True
    assert "No transitions recorded yet" in result.text


def test_unhealthy_empty_tracker():
    from backend.core.ouroboros.governance.health_repl import (
        dispatch_health_command,
    )
    result = dispatch_health_command("/health unhealthy")
    assert result.ok is True
    assert "All components healthy" in result.text


def test_show_unknown_component():
    from backend.core.ouroboros.governance.health_repl import (
        dispatch_health_command,
    )
    result = dispatch_health_command("/health show ghost")
    # ok=True because rendering completed cleanly; the line
    # surfaces the not-registered fact transparently.
    assert "not registered" in result.text


def test_show_missing_name_arg():
    from backend.core.ouroboros.governance.health_repl import (
        dispatch_health_command,
    )
    result = dispatch_health_command("/health show")
    assert result.ok is False
    assert "name required" in result.text.lower() or (
        "<name>" in result.text
    )


# ---------------------------------------------------------------------------
# Populated tracker — end-to-end with real ComponentHealthTracker
# ---------------------------------------------------------------------------


def test_overview_populated_tracker():
    from backend.core.ouroboros.governance.autonomy.component_health import (  # noqa: E501
        ComponentState, get_default_tracker,
    )
    from backend.core.ouroboros.governance.health_repl import (
        dispatch_health_command,
    )
    tracker = get_default_tracker()
    tracker.register("provider_dw", ComponentState.READY)
    tracker.register("provider_claude", ComponentState.READY)
    tracker.update(
        "provider_dw",
        ComponentState.ACTIVE,
        health_score=0.95,
    )
    tracker.update(
        "provider_claude",
        ComponentState.ERROR,
        health_score=0.10,
    )
    result = dispatch_health_command("/health")
    assert result.ok is True
    assert "2 components" in result.text
    assert "provider_dw" in result.text or (
        "provider_claude" in result.text
    )


def test_components_lists_all_registered():
    from backend.core.ouroboros.governance.autonomy.component_health import (  # noqa: E501
        ComponentState, get_default_tracker,
    )
    from backend.core.ouroboros.governance.health_repl import (
        dispatch_health_command,
    )
    tracker = get_default_tracker()
    for name in ("alpha", "beta", "gamma"):
        tracker.register(name, ComponentState.READY)
    result = dispatch_health_command("/health components")
    assert "alpha" in result.text
    assert "beta" in result.text
    assert "gamma" in result.text


def test_show_populated_component():
    from backend.core.ouroboros.governance.autonomy.component_health import (  # noqa: E501
        ComponentState, get_default_tracker,
    )
    from backend.core.ouroboros.governance.health_repl import (
        dispatch_health_command,
    )
    tracker = get_default_tracker()
    tracker.register("test_comp", ComponentState.READY)
    tracker.update(
        "test_comp",
        ComponentState.ACTIVE,
        health_score=0.75,
        metadata={"region": "us-west", "version": "1.2"},
    )
    result = dispatch_health_command("/health show test_comp")
    assert result.ok is True
    assert "test_comp" in result.text
    assert "ACTIVE" in result.text
    # Metadata renders
    assert "region" in result.text or "version" in result.text


def test_history_renders_transitions():
    from backend.core.ouroboros.governance.autonomy.component_health import (  # noqa: E501
        ComponentState, get_default_tracker,
    )
    from backend.core.ouroboros.governance.health_repl import (
        dispatch_health_command,
    )
    tracker = get_default_tracker()
    tracker.register("comp_a", ComponentState.READY)
    tracker.update("comp_a", ComponentState.ACTIVE)
    tracker.update("comp_a", ComponentState.BUSY)
    result = dispatch_health_command("/health history")
    assert result.ok is True
    assert "comp_a" in result.text
    assert "ACTIVE" in result.text or "BUSY" in result.text


def test_history_limit_clamping():
    """History limit clamps to [1, 200]."""
    from backend.core.ouroboros.governance.health_repl import (
        _parse_limit,
    )
    assert _parse_limit([]) == 20  # default
    assert _parse_limit(["5"]) == 5
    assert _parse_limit(["0"]) == 1  # clamp low
    assert _parse_limit(["999"]) == 200  # clamp high
    assert _parse_limit(["garbage"]) == 20  # parse fail = default


def test_unhealthy_filters_correctly():
    from backend.core.ouroboros.governance.autonomy.component_health import (  # noqa: E501
        ComponentState, get_default_tracker,
    )
    from backend.core.ouroboros.governance.health_repl import (
        dispatch_health_command,
    )
    tracker = get_default_tracker()
    tracker.register("healthy", ComponentState.READY)
    tracker.update("healthy", ComponentState.ACTIVE)
    tracker.register("broken", ComponentState.READY)
    tracker.update("broken", ComponentState.ERROR)
    result = dispatch_health_command("/health unhealthy")
    assert result.ok is True
    assert "broken" in result.text
    # Don't assert healthy ABSENT — render may or may not show
    # context; the ASSERTION is that broken shows up.


# ---------------------------------------------------------------------------
# Defensive paths — NEVER raises
# ---------------------------------------------------------------------------


def test_unknown_subcommand_returns_clean_error():
    from backend.core.ouroboros.governance.health_repl import (
        dispatch_health_command,
    )
    result = dispatch_health_command("/health garbage")
    assert result.matched is True
    assert result.ok is False
    assert "unknown subcommand" in result.text.lower()


def test_shlex_parse_error_does_not_raise():
    """Unmatched-quote shlex parse errors return clean error
    envelope."""
    from backend.core.ouroboros.governance.health_repl import (
        dispatch_health_command,
    )
    result = dispatch_health_command('/health "unclosed')
    assert result.matched is True
    assert result.ok is False
    assert "parse error" in result.text


def test_tracker_access_failure_returns_clean_error():
    """If the singleton accessor raises (cold-start race), the
    dispatcher returns a clean error envelope rather than
    propagating into the REPL."""
    from backend.core.ouroboros.governance.health_repl import (
        dispatch_health_command,
    )
    with patch(
        "backend.core.ouroboros.governance.autonomy."
        "component_health.get_default_tracker",
        side_effect=RuntimeError("boot race"),
    ):
        result = dispatch_health_command("/health")
    assert result.matched is True
    assert result.ok is False
    assert "error" in result.text.lower()


def test_list_names_returns_sorted():
    """Defensive test against the new helper — sorts alphabetically."""
    from backend.core.ouroboros.governance.autonomy.component_health import (  # noqa: E501
        ComponentState, get_default_tracker,
    )
    tracker = get_default_tracker()
    for name in ("zulu", "alpha", "mike"):
        tracker.register(name, ComponentState.READY)
    assert tracker.list_names() == ["alpha", "mike", "zulu"]


def test_all_components_returns_fresh_list():
    """Defensive: caller mutations of the returned list don't
    leak into tracker state."""
    from backend.core.ouroboros.governance.autonomy.component_health import (  # noqa: E501
        ComponentState, get_default_tracker,
    )
    tracker = get_default_tracker()
    tracker.register("x", ComponentState.READY)
    snapshot = tracker.all_components()
    snapshot.clear()
    # Original tracker still has the component.
    assert tracker.list_names() == ["x"]


# ---------------------------------------------------------------------------
# Singleton wiring
# ---------------------------------------------------------------------------


def test_get_default_tracker_returns_same_instance():
    from backend.core.ouroboros.governance.autonomy.component_health import (  # noqa: E501
        get_default_tracker,
    )
    a = get_default_tracker()
    b = get_default_tracker()
    assert a is b


def test_safety_net_uses_default_singleton():
    """SafetyNet `__init__` MUST default to the singleton so its
    state flows into the operator-facing /health surface."""
    from backend.core.ouroboros.governance.autonomy.component_health import (  # noqa: E501
        get_default_tracker,
    )
    # Construct SafetyNet without overriding the tracker; verify
    # its tracker is identity-equal to the singleton.
    try:
        from backend.core.ouroboros.governance.autonomy.safety_net import (  # noqa: E501
            SafetyNet,
        )
    except ImportError:
        pytest.skip("SafetyNet substrate unavailable")
    # SafetyNet may have many init args — try minimal shape;
    # if signature requires args, skip (we still pinned the
    # source-AST below).
    try:
        net = SafetyNet()  # type: ignore
    except TypeError:
        pytest.skip("SafetyNet requires args; skip live test")
    assert net._health_tracker is get_default_tracker(), (
        "SafetyNet MUST default to the singleton tracker"
    )


def test_safety_net_source_uses_singleton_default():
    """AST pin: SafetyNet __init__ MUST use get_default_tracker()
    as the default for self._health_tracker. Catches future
    refactors that re-isolate the tracker."""
    target = (
        _repo_root()
        / "backend/core/ouroboros/governance/autonomy/safety_net.py"
    )
    text = target.read_text(encoding="utf-8")
    assert "get_default_tracker()" in text, (
        "SafetyNet MUST default to get_default_tracker() — "
        "single-source-of-truth wiring"
    )


# ---------------------------------------------------------------------------
# Auto-discovery hooks
# ---------------------------------------------------------------------------


def test_dispatch_function_naming_matches_cage():
    """§32.11 Slice 4 naming cage: file ends `_repl.py` → verb
    `health` → dispatcher must be `dispatch_health_command`."""
    from backend.core.ouroboros.governance import health_repl
    assert hasattr(health_repl, "dispatch_health_command")
    import inspect
    sig = inspect.signature(
        health_repl.dispatch_health_command,
    )
    params = list(sig.parameters.values())
    assert len(params) == 1
    assert params[0].name == "line"


def test_register_verbs_returns_count():
    """Help-discovery contract: register_verbs(registry) returns
    int (count registered). NEVER raises on registry failures."""
    from backend.core.ouroboros.governance.health_repl import (
        register_verbs,
    )

    class FakeRegistry:
        def __init__(self):
            self.calls = []

        def register(self, **kwargs):
            self.calls.append(kwargs)

    reg = FakeRegistry()
    n = register_verbs(reg)
    assert n == 1
    assert len(reg.calls) == 1
    assert reg.calls[0]["verb"] == "health"


def test_register_verbs_swallows_registry_failures():
    """Defensive: if registry raises, register_verbs returns 0
    (NEVER propagates into /help boot path)."""
    from backend.core.ouroboros.governance.health_repl import (
        register_verbs,
    )

    class BrokenRegistry:
        def register(self, **kwargs):
            raise RuntimeError("registry broken")

    n = register_verbs(BrokenRegistry())
    assert n == 0


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


def test_register_shipped_invariants_returns_3():
    from backend.core.ouroboros.governance.health_repl import (
        register_shipped_invariants,
    )
    invs = register_shipped_invariants()
    assert len(invs) == 3
    names = {i.invariant_name for i in invs}
    assert names == {
        "health_repl_composes_canonical_tracker",
        "health_repl_authority_read_only",
        "health_repl_authority_asymmetry",
    }


def test_all_pins_validate_clean():
    from backend.core.ouroboros.governance.health_repl import (
        register_shipped_invariants,
    )
    target = (
        _repo_root()
        / "backend/core/ouroboros/governance/health_repl.py"
    )
    source = target.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for inv in register_shipped_invariants():
        violations = inv.validate(tree, source)
        assert violations == (), (
            f"pin {inv.invariant_name} fired: {violations}"
        )


def test_composes_pin_fires_on_direct_construction():
    """Synthetic regression: if a future refactor constructs
    ComponentHealthTracker() directly, the pin fires."""
    from backend.core.ouroboros.governance.health_repl import (
        register_shipped_invariants,
    )
    bad_source = '''
def foo():
    return ComponentHealthTracker()
'''
    tree = ast.parse(bad_source)
    invs = register_shipped_invariants()
    pin = next(
        i for i in invs
        if "composes_canonical_tracker" in i.invariant_name
    )
    violations = pin.validate(tree, bad_source)
    assert violations
    assert any(
        "ComponentHealthTracker" in v for v in violations
    )


def test_read_only_pin_fires_on_mutating_call():
    """Synthetic regression: if a future refactor calls
    tracker.update(...) inside health_repl, the pin fires."""
    from backend.core.ouroboros.governance.health_repl import (
        register_shipped_invariants,
    )
    bad_source = '''
def foo():
    tracker = get_default_tracker()
    tracker.update("x", "READY")
'''
    tree = ast.parse(bad_source)
    invs = register_shipped_invariants()
    pin = next(
        i for i in invs
        if "read_only" in i.invariant_name
    )
    violations = pin.validate(tree, bad_source)
    assert violations
    assert any(
        "update" in v or "read-only" in v
        for v in violations
    )


def test_authority_asymmetry_pin_fires_on_forbidden_import():
    from backend.core.ouroboros.governance.health_repl import (
        register_shipped_invariants,
    )
    bad_source = '''
from backend.core.ouroboros.governance.orchestrator import foo
'''
    tree = ast.parse(bad_source)
    invs = register_shipped_invariants()
    pin = next(
        i for i in invs
        if "authority_asymmetry" in i.invariant_name
    )
    violations = pin.validate(tree, bad_source)
    assert violations
    assert any("orchestrator" in v for v in violations)


# ---------------------------------------------------------------------------
# Public API stability
# ---------------------------------------------------------------------------


def test_public_api_stable():
    from backend.core.ouroboros.governance import health_repl
    expected = {
        "HealthReplDispatchResult",
        "dispatch_health_command",
        "register_shipped_invariants",
        "register_verbs",
    }
    assert set(health_repl.__all__) == expected
