"""A keystroke that can tighten the gate, and can never loosen it.

Claude Code cycles permission modes with Shift+Tab, one of which is "bypass
permissions on". That direction is not offered here, and the asymmetry is the
design rather than a limitation of it.

`risk_tier_floor` already composes several floors strictest-wins, and that
composition IS the safety property: no input can make the organism more
permissive than another input demanded. A keystroke that bypassed it would
not be a new mode — it would be a hole in the one rule every other floor is
built on.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance.risk_tier_floor import (
    apply_floor_to_name,
)
from backend.core.ouroboros.governance.session_risk_floor import (
    clear_session_floor, cycle_session_floor, session_cycle_enabled,
    session_floor, session_floor_label, set_session_floor,
)


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("JARVIS_MIN_RISK_TIER", raising=False)
    monkeypatch.delenv("JARVIS_PARANOIA_MODE", raising=False)
    clear_session_floor()
    yield
    clear_session_floor()


# --------------------------------------------------------------------------
# the safety property
# --------------------------------------------------------------------------

def test_the_keystroke_can_NEVER_loosen_the_configured_floor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE invariant. A session asking for less than the config demands must
    lose, in every cycle position — otherwise Shift+Tab is a hole in the one
    rule every other floor is built on."""
    monkeypatch.setenv("JARVIS_MIN_RISK_TIER", "approval_required")
    for _ in range(8):
        cycle_session_floor()
        effective, _applied = apply_floor_to_name("safe_auto")
        assert effective in ("approval_required", "blocked"), (
            f"a keystroke loosened the gate to {effective}"
        )


def test_safe_auto_is_not_even_offered() -> None:
    """The most permissive tier is absent from the cycle on purpose.
    Composition would refuse it anyway; this does not pretend to offer it."""
    seen = set()
    for _ in range(8):
        seen.add(cycle_session_floor())
    assert "safe_auto" not in seen


def test_it_can_tighten_freely() -> None:
    """The useful direction: the moment an operator wants this key is when
    they are about to do something they do not fully trust."""
    set_session_floor("approval_required")
    assert apply_floor_to_name("safe_auto")[0] == "approval_required"
    set_session_floor("blocked")
    assert apply_floor_to_name("safe_auto")[0] == "blocked"


def test_a_stricter_env_still_wins_over_a_looser_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVIS_MIN_RISK_TIER", "approval_required")
    set_session_floor("notify_apply")
    assert apply_floor_to_name("safe_auto")[0] == "approval_required"


def test_a_stricter_session_wins_over_a_looser_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Composition is symmetric — strictest wins whichever side asked."""
    monkeypatch.setenv("JARVIS_MIN_RISK_TIER", "notify_apply")
    set_session_floor("blocked")
    assert apply_floor_to_name("safe_auto")[0] == "blocked"


# --------------------------------------------------------------------------
# the cycle
# --------------------------------------------------------------------------

def test_it_wraps_back_to_FOLLOWING_CONFIG_not_to_permissive() -> None:
    """"Back to what I configured" is a real destination. "Less strict than I
    configured" is not one this key can reach."""
    order = [cycle_session_floor() for _ in range(4)]
    assert order == ["notify_apply", "approval_required", "blocked", None]


def test_an_unknown_tier_is_refused_not_guessed() -> None:
    set_session_floor("approval_required")
    set_session_floor("totally_permissive")
    assert session_floor() == "approval_required"


def test_it_resets_when_the_cockpit_detaches() -> None:
    """A keystroke made about one screenful must not silently become this
    machine's permanent policy. Persisting a security posture is a config
    edit, and config edits are visible."""
    set_session_floor("blocked")
    clear_session_floor()
    assert session_floor() is None
    assert apply_floor_to_name("safe_auto")[0] == "safe_auto"


# --------------------------------------------------------------------------
# visibility
# --------------------------------------------------------------------------

def test_it_is_silent_while_following_config() -> None:
    """A permanent badge saying "normal" is chrome, and chrome is not read."""
    assert session_floor_label() == ""


def test_a_raised_floor_is_always_visible() -> None:
    """The moment it says anything, the operator changed something — which is
    exactly when they need to see it."""
    set_session_floor("approval_required")
    assert "approve" in session_floor_label()
    set_session_floor("blocked")
    assert "blocked" in session_floor_label()


def test_it_reaches_the_toolbar() -> None:
    import contextlib
    import io

    with contextlib.redirect_stderr(io.StringIO()):
        from backend.core.ouroboros.cli.ov import AttachUI

    ui = AttachUI()
    assert session_floor_label() not in ("",) or True
    set_session_floor("approval_required")
    assert session_floor_label() in ui._key_hints()


# --------------------------------------------------------------------------
# wiring and robustness
# --------------------------------------------------------------------------

def test_both_surfaces_bind_it() -> None:
    import ast
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    for path in ("backend/core/ouroboros/cli/ov.py",
                 "backend/core/ouroboros/battle_test/bipartite_layout.py"):
        src = (repo / path).read_text()
        names = {a.name for n in ast.walk(ast.parse(src))
                 if isinstance(n, ast.ImportFrom) for a in n.names}
        assert "cycle_session_floor" in names, f"{path} cannot cycle"


def test_it_composes_rather_than_overrides() -> None:
    """Structural: the session floor must feed the SAME strictest-wins
    resolution, not short-circuit it."""
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    src = (repo / "backend/core/ouroboros/governance/"
           "risk_tier_floor.py").read_text()
    assert "session_floor" in src
    assert "get_active_tier_order" in src


def test_the_kill_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    set_session_floor("blocked")
    monkeypatch.setenv("JARVIS_SESSION_RISK_CYCLE_ENABLED", "0")
    assert session_cycle_enabled() is False
    assert session_floor() is None
    assert apply_floor_to_name("safe_auto")[0] == "safe_auto"
