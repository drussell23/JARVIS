"""One env var, one default — or a collision that names both sides.

Two flags were read in two places with two different defaults today, and both
were real defects: JARVIS_PALETTE_HEIGHT (4 vs 12) made the operator's menu
height depend on which renderer mounted, and JARVIS_MEMORY_ROUTING_ENABLED
(OFF vs ON) made `/memory` report "routing: on" for the entire period routing
was disabled. A static scan then found 29 across the tree.

Nothing prevented any of them: `os.environ.get(NAME, default)` is a
decentralised declaration and every call site may invent its own default.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance import config_arbiter as ca
from backend.core.ouroboros.governance.config_arbiter import (
    ConfigurationCollisionError,
    collisions,
    resolve,
    resolve_bool,
    resolve_int,
    scan_static,
)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("JARVIS_CONFIG_ARBITER_STRICT", raising=False)
    ca.reset_for_tests()
    yield
    ca.reset_for_tests()


# ---------------------------------------------------------------------------
# The mandate: two modules, same var, different defaults -> strict raises
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_modules_declaring_different_defaults_raise_in_strict_mode(
    monkeypatch,
):
    """THE mandate case, using the real collision found in the tree:
    JARVIS_GOVERNED_TOOL_MAX_ROUNDS is 10 in governed_loop_service and 15 in
    presentation_restraint — Venom's tool-loop budget disagreeing with itself.
    """
    monkeypatch.setenv("JARVIS_CONFIG_ARBITER_STRICT", "1")
    resolve_int("JARVIS_GOVERNED_TOOL_MAX_ROUNDS", 10,
                declared_by="governed_loop_service.py")
    with pytest.raises(ConfigurationCollisionError) as exc:
        resolve_int("JARVIS_GOVERNED_TOOL_MAX_ROUNDS", 15,
                    declared_by="presentation_restraint.py")
    message = str(exc.value)
    # The error must name BOTH sides — "something collided" is not actionable.
    assert "governed_loop_service.py" in message
    assert "presentation_restraint.py" in message
    assert "10" in message and "15" in message


@pytest.mark.asyncio
async def test_identical_defaults_are_not_a_collision(monkeypatch):
    """Two call sites agreeing is the normal case and must stay silent."""
    monkeypatch.setenv("JARVIS_CONFIG_ARBITER_STRICT", "1")
    resolve_int("JARVIS_SAME", 30, declared_by="a.py")
    resolve_int("JARVIS_SAME", 30, declared_by="b.py")
    assert collisions() == []


def test_non_strict_records_without_raising():
    """Default mode must stay ALIVE. 29 collisions predate this module; an
    arbiter that raised on the default path would make the first import fatal,
    which is an outage, not a safety property."""
    resolve_int("JARVIS_X", 10, declared_by="a.py")
    resolve_int("JARVIS_X", 15, declared_by="b.py")
    found = collisions()
    assert len(found) == 1
    assert "a.py" in found[0].describe() and "b.py" in found[0].describe()


def test_the_winner_is_first_registered_not_last():
    """Last-wins would make config depend on import order, which changes when
    an unrelated module adds a lazy import — a value that moves for reasons
    invisible at the call site is worse than a wrong one."""
    assert resolve_int("JARVIS_W", 10, declared_by="a.py") == 10
    assert resolve_int("JARVIS_W", 15, declared_by="b.py") == 10


def test_the_environment_still_wins_over_any_default(monkeypatch):
    monkeypatch.setenv("JARVIS_W", "99")
    assert resolve_int("JARVIS_W", 10, declared_by="a.py") == 99


def test_empty_env_value_falls_back_to_the_default(monkeypatch):
    """`FOO=` is indistinguishable from unset in most shells."""
    monkeypatch.setenv("JARVIS_W", "")
    assert resolve_int("JARVIS_W", 7, declared_by="a.py") == 7


def test_bool_and_int_share_the_same_arbitration():
    resolve_bool("JARVIS_B", True, declared_by="a.py")
    resolve_bool("JARVIS_B", False, declared_by="b.py")
    assert len(collisions()) == 1


def test_int_is_clamped():
    assert resolve_int("JARVIS_C", 500, lo=1, hi=10, declared_by="a.py") == 10


def test_resolution_never_raises_outside_strict_mode():
    for bad in (None, object(), 1.5):
        assert resolve("JARVIS_J", bad, declared_by="a.py") is not None


def test_caller_attribution_is_derived_when_not_supplied():
    """A caller that must name itself will eventually name itself wrong."""
    resolve("JARVIS_ATTR", 1)
    resolve("JARVIS_ATTR", 2)
    assert "test_config_arbiter.py" in collisions()[0].describe()


# ---------------------------------------------------------------------------
# Static scan — catches what runtime cannot
# ---------------------------------------------------------------------------


def test_static_scan_finds_real_collisions_without_adoption():
    """Runtime arbitration only sees a collision once BOTH sites execute; for
    a pair on rarely-taken branches that may be never."""
    found = scan_static()
    assert found, "static scan found nothing — the AST walk is broken"
    names = {c.name for c in found}
    assert "JARVIS_CHANNEL_PORT" in names


def test_static_scan_ignores_computed_defaults():
    """`defaults.poll_interval_s` in two files is agreement, not collision.

    A scanner that guessed at computed values would produce false collisions,
    which is how a guard stops being read.
    """
    import ast

    from backend.core.ouroboros.governance.config_arbiter import _literal
    assert _literal(ast.parse("1").body[0].value) == "1"
    assert _literal(ast.parse("defaults.x").body[0].value) is None
    assert _literal(ast.parse("os.path.join(a, b)").body[0].value) is None
