"""Regression spine for the proactive-mode ladder (PRD §30).

Every test names the operator-visible failure it prevents. The clock is
driven, so no test sleeps.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance import proactive_mode as pm


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for knob in ("JARVIS_MIN_RISK_TIER", "JARVIS_WORKSPACE_PROMOTION_ENABLED",
                 "JARVIS_PROACTIVE_MODE_GRACE_S",
                 "JARVIS_PROACTIVE_MODE_COALESCE_S"):
        monkeypatch.delenv(knob, raising=False)
    pm.reset_controller()
    pm.set_emission_sink(None)
    yield
    pm.reset_controller()
    pm.set_emission_sink(None)


class _Pool:
    def __init__(self): self.paused = False; self.reasons = []
    def pause(self, *, reason=""): self.paused = True; self.reasons.append(reason); return True
    def resume(self, *, reason=""): self.paused = False; self.reasons.append(reason); return True


# ---------------------------------------------------------------------------
# The two axes
# ---------------------------------------------------------------------------


def test_the_ladder_is_monotone_in_both_coordinates():
    """A path trading one axis against the other would leave the operator
    unable to say whether they had just loosened or tightened."""
    init_order = [pm.Initiative.ACT, pm.Initiative.EXPLORE,
                  pm.Initiative.OBSERVE, pm.Initiative.NONE]
    auth_order = [pm.Authority.PROMOTE, pm.Authority.AUTO,
                  pm.Authority.NOTIFY, pm.Authority.PROPOSE]
    prev_i = prev_a = -1
    for pos in pm.LADDER:
        i = init_order.index(pos.initiative)
        a = auth_order.index(pos.authority)
        assert i >= prev_i and a >= prev_a, f"{pos.name} loosens an axis"
        prev_i, prev_a = i, a


def test_watch_is_the_only_rung_that_withholds_initiative():
    """The gap the shipped dial could not express: at approval_required,
    sixteen sensors still fire and tokens are still spent."""
    withholding = [p.name for p in pm.LADDER
                   if p.initiative is pm.Initiative.NONE]
    assert withholding == ["watch"]
    assert pm.position("approval_required").initiative is pm.Initiative.ACT


def test_explore_permits_initiative_but_not_authority():
    """§27.4.1.1 — a countdown buys an EXPLORE, never an APPLY."""
    p = pm.position("explore")
    assert p.initiative is pm.Initiative.EXPLORE
    assert p.authority is pm.Authority.PROPOSE


def test_the_three_shipped_rungs_keep_their_risk_floor_values():
    """The middle of the ladder must be the existing dial, unchanged."""
    assert pm.position("safe_auto").risk_floor is None
    assert pm.position("notify_apply").risk_floor == "notify_apply"
    assert pm.position("approval_required").risk_floor == "approval_required"


# ---------------------------------------------------------------------------
# Boundary — clamp, never wrap (§30.11 Q2, operator decision)
# ---------------------------------------------------------------------------


def test_the_dial_clamps_at_strictest_and_never_wraps():
    """Wrapping means one accidental keypress moves from maximum caution to
    maximum autonomy."""
    c = pm.ProactiveModeController(clock=FakeClock())
    seen = [c.cycle("mac")[0].name for _ in range(12)]
    assert seen[-1] == "watch"
    assert "safe_auto" not in seen[3:], "the dial wrapped past the boundary"


def test_each_press_moves_exactly_one_rung():
    """An accumulator that added pending steps to an already-moved position
    advanced three rungs on two presses — landing the operator somewhere
    they did not choose."""
    c = pm.ProactiveModeController(clock=FakeClock())
    order = [c.cycle("mac")[0].name for _ in range(4)]
    assert order == ["notify_apply", "approval_required", "explore", "watch"]


def test_mashing_the_boundary_settles_deterministically():
    c = pm.ProactiveModeController(clock=FakeClock())
    for _ in range(4):
        c.cycle("mac")
    results = [c.cycle("mac") for _ in range(50)]
    assert all(pos.name == "watch" for pos, _ in results)
    assert all(boundary for _, boundary in results)


def test_a_boundary_burst_produces_no_repeated_effects():
    """The expensive part is guarded on an actual change, so mashing costs
    no env writes and no pool calls."""
    pool = _Pool()
    c = pm.ProactiveModeController(clock=FakeClock())
    for _ in range(4):
        c.cycle("mac")
    c.apply_effects(pool=pool)
    before = len(pool.reasons)
    for _ in range(30):
        c.cycle("mac")
        c.apply_effects(pool=pool)
    assert c.snapshot()["transitions"] == 1
    assert len(pool.reasons) - before <= 30  # idempotent pause calls only
    assert pool.paused is True


def test_stepping_backwards_clamps_at_the_loosest_reachable_rung():
    c = pm.ProactiveModeController(clock=FakeClock())
    for _ in range(10):
        pos, _ = c.cycle("mac", steps=-1)
    assert pos.name == "safe_auto", "stepped below the reachable floor"


# ---------------------------------------------------------------------------
# Reachability — computed from live capability, never hardcoded
# ---------------------------------------------------------------------------


def test_promote_is_unreachable_until_gate_three_is_armed():
    """A dial accepting a position the host cannot honour would report an
    autonomy level that does not exist."""
    assert "promote" not in [p.name for p in pm.reachable()]


def test_promote_appears_when_the_actuator_is_armed(monkeypatch):
    monkeypatch.setenv("JARVIS_WORKSPACE_PROMOTION_ENABLED", "true")
    assert "promote" in [p.name for p in pm.reachable()]


def test_losing_a_capability_never_loosens_the_current_rung(monkeypatch):
    """A host that loses promotion mid-session must not thereby become more
    autonomous."""
    monkeypatch.setenv("JARVIS_WORKSPACE_PROMOTION_ENABLED", "true")
    c = pm.ProactiveModeController(clock=FakeClock())
    c.request("mac", "promote")
    monkeypatch.delenv("JARVIS_WORKSPACE_PROMOTION_ENABLED")
    pos, _ = c.cycle("mac", steps=0)
    assert pos.rank >= pm.position("safe_auto").rank


# ---------------------------------------------------------------------------
# Multi-cockpit — strictest wins, universally
# ---------------------------------------------------------------------------


def test_the_strictest_live_request_wins():
    """Today the last writer wins silently: an operator can tighten the
    organism and have a colleague loosen it with neither seeing."""
    c = pm.ProactiveModeController(clock=FakeClock())
    c.request("mac", "watch")
    c.request("desktop", "safe_auto")
    assert c.effective().name == "watch"


def test_a_loosening_cockpit_is_told_it_is_overridden():
    """An operator whose tightening is invisibly discarded stops
    tightening."""
    c = pm.ProactiveModeController(clock=FakeClock())
    c.request("mac", "approval_required")
    c.request("desktop", "safe_auto")
    assert c.overridden("desktop") is True
    assert c.overridden("mac") is False


def test_order_of_requests_does_not_change_the_outcome():
    a = pm.ProactiveModeController(clock=FakeClock())
    a.request("x", "watch"); a.request("y", "safe_auto")
    b = pm.ProactiveModeController(clock=FakeClock())
    b.request("y", "safe_auto"); b.request("x", "watch")
    assert a.effective().name == b.effective().name == "watch"


def test_effective_is_derived_not_cached():
    """A cached value has a window between a cockpit vanishing and the
    recomputation. A derived one has no window."""
    import inspect
    src = inspect.getsource(pm.ProactiveModeController.effective)
    assert "self._cached" not in src and "return self._effective" not in src


# ---------------------------------------------------------------------------
# Disconnect race — absence is not consent to loosen
# ---------------------------------------------------------------------------


def test_a_dropped_strict_cockpit_does_not_immediately_loosen():
    """An operator on flaky Wi-Fi who set watch would otherwise have their
    caution undone by their own network."""
    clock = FakeClock()
    c = pm.ProactiveModeController(clock=clock)
    c.request("mac", "watch")
    c.request("desktop", "safe_auto")
    c.detach("mac")
    assert c.effective().name == "watch", "a disconnect loosened the organism"


def test_a_retained_request_releases_after_the_grace_window(monkeypatch):
    monkeypatch.setenv("JARVIS_PROACTIVE_MODE_GRACE_S", "60")
    clock = FakeClock()
    c = pm.ProactiveModeController(clock=clock)
    c.request("mac", "watch")
    c.request("desktop", "safe_auto")
    c.detach("mac")
    clock.advance(120.0)
    assert c.effective().name == "safe_auto"


def test_reattaching_restores_full_weight_before_expiry(monkeypatch):
    monkeypatch.setenv("JARVIS_PROACTIVE_MODE_GRACE_S", "60")
    clock = FakeClock()
    c = pm.ProactiveModeController(clock=clock)
    c.request("mac", "watch")
    c.detach("mac")
    clock.advance(30.0)
    c.reattach("mac")
    clock.advance(120.0)
    assert c.effective().name == "watch", "a reattached request expired anyway"


def test_a_deliberate_exit_releases_immediately():
    """A clean quit is consent; an unplanned drop is not."""
    c = pm.ProactiveModeController(clock=FakeClock())
    c.request("mac", "watch")
    c.request("desktop", "safe_auto")
    c.release("mac")
    assert c.effective().name == "safe_auto"


def test_no_requests_falls_back_to_the_environment(monkeypatch):
    """Headless and attached must not disagree about where the dial rests."""
    monkeypatch.setenv("JARVIS_MIN_RISK_TIER", "approval_required")
    c = pm.ProactiveModeController(clock=FakeClock())
    assert c.effective().name == "approval_required"


# ---------------------------------------------------------------------------
# Effects — composition only
# ---------------------------------------------------------------------------


def test_watch_pauses_admission_and_says_who_did_it():
    """Hibernation pauses the same pool on provider exhaustion. Rendering
    them alike would report an outage the operator did not cause."""
    pool = _Pool()
    c = pm.ProactiveModeController(clock=FakeClock())
    c.request("mac", "watch")
    out = c.apply_effects(pool=pool)
    assert pool.paused is True
    assert out["emission"] == "paused"
    assert any("operator" in r for r in pool.reasons)


def test_leaving_watch_resumes_admission():
    pool = _Pool()
    c = pm.ProactiveModeController(clock=FakeClock())
    c.request("mac", "watch")
    c.apply_effects(pool=pool)
    c.request("mac", "notify_apply")
    c.apply_effects(pool=pool)
    assert pool.paused is False


def test_no_rung_but_watch_pauses_the_pool():
    for name in ("safe_auto", "notify_apply", "approval_required", "explore"):
        pool = _Pool()
        c = pm.ProactiveModeController(clock=FakeClock())
        c.request("mac", name)
        c.apply_effects(pool=pool)
        assert pool.paused is False, f"{name} withheld initiative"


def test_the_risk_floor_is_the_existing_env_knob(monkeypatch):
    """Every gate already re-reads it per operation — no second floor."""
    c = pm.ProactiveModeController(clock=FakeClock())
    c.request("mac", "explore")
    c.apply_effects(pool=_Pool())
    import os
    assert os.environ["JARVIS_MIN_RISK_TIER"] == "approval_required"
    c.request("mac", "safe_auto")
    c.apply_effects(pool=_Pool())
    assert "JARVIS_MIN_RISK_TIER" not in os.environ


def test_no_sink_reports_honestly_rather_than_claiming_success():
    """The authority axis still holds; only initiative is unenforceable,
    and saying so beats pretending otherwise."""
    c = pm.ProactiveModeController(clock=FakeClock())
    c.request("mac", "watch")
    out = c.apply_effects()
    assert out["emission"] == "no sink registered"


def test_a_broken_pool_degrades_rather_than_raising():
    class _Broken:
        def pause(self, **kw): raise RuntimeError("pool is gone")
        def resume(self, **kw): raise RuntimeError("pool is gone")
    c = pm.ProactiveModeController(clock=FakeClock())
    c.request("mac", "watch")
    out = c.apply_effects(pool=_Broken())
    assert "degraded" in out["emission"]


def test_apply_effects_is_idempotent():
    pool = _Pool()
    c = pm.ProactiveModeController(clock=FakeClock())
    c.request("mac", "explore")
    first = c.apply_effects(pool=pool)
    second = c.apply_effects(pool=pool)
    assert first["changed"] is True and second["changed"] is False


# ---------------------------------------------------------------------------
# Posture
# ---------------------------------------------------------------------------


def test_default_off_pending_graduation():
    assert pm.is_enabled() is False


def test_an_unknown_rung_resolves_to_the_resting_state_not_the_loosest():
    """An unparseable dial must not be able to take the organism down, and
    must not silently become maximally autonomous either."""
    assert pm.position("nonsense").name == "safe_auto"
    assert pm.position(None).name == "safe_auto"


def test_no_lock_is_held_across_a_subsystem_call():
    """A governance primitive blocking while this held the dial would
    deadlock the loop the dial exists to steer."""
    import inspect
    import re
    src = inspect.getsource(pm.ProactiveModeController)
    for block in re.findall(r"with self\._lock:(.*?)(?=\n    [a-zA-Z@]|\Z)",
                            src, re.S):
        for banned in ("pause(", "resume(", "apply_effects"):
            assert banned not in block, f"{banned} called under the lock"


def test_the_snapshot_is_serialisable():
    import json
    c = pm.ProactiveModeController(clock=FakeClock())
    c.request("mac", "explore")
    json.dumps(c.snapshot())


# ---------------------------------------------------------------------------
# trust_repl integration — one vocabulary, projected
# ---------------------------------------------------------------------------


def test_the_dial_offers_only_reachable_rungs(monkeypatch):
    from backend.core.ouroboros.governance import trust_repl as t
    monkeypatch.delenv("JARVIS_PROACTIVE_MODE_ENABLED", raising=False)
    assert t._cycle_positions() == ("safe_auto", "notify_apply",
                                    "approval_required")
    monkeypatch.setenv("JARVIS_PROACTIVE_MODE_ENABLED", "1")
    assert t._cycle_positions() == ("safe_auto", "notify_apply",
                                    "approval_required", "explore", "watch")


def test_the_dial_writes_a_valid_risk_floor_never_a_rung_name(monkeypatch):
    """THE correctness bug this pins: `explore` is a ladder position, not a
    risk tier. Writing it into JARVIS_MIN_RISK_TIER would leave the floor
    unparseable and silently resolve to NO floor — the strictest rungs
    becoming the loosest in effect."""
    import os
    from backend.core.ouroboros.governance import trust_repl as t
    monkeypatch.setenv("JARVIS_PROACTIVE_MODE_ENABLED", "1")
    for rung in ("explore", "watch"):
        t.dispatch_trust_command(f"/trust {rung}")
        assert os.environ["JARVIS_MIN_RISK_TIER"] == "approval_required"


def test_two_rungs_asserting_one_floor_stay_distinguishable(monkeypatch):
    from backend.core.ouroboros.governance import trust_repl as t
    monkeypatch.setenv("JARVIS_PROACTIVE_MODE_ENABLED", "1")
    t.dispatch_trust_command("/trust explore")
    assert t.current_floor() == "explore"
    t.dispatch_trust_command("/trust watch")
    assert t.current_floor() == "watch"


def test_the_dial_refuses_to_wrap_past_the_strictest(monkeypatch):
    from backend.core.ouroboros.governance import trust_repl as t
    monkeypatch.setenv("JARVIS_PROACTIVE_MODE_ENABLED", "1")
    t.dispatch_trust_command("/trust watch")
    for _ in range(5):
        out = t.dispatch_trust_command("/trust cycle")
    assert "does not wrap" in out.text
    assert t.current_floor() == "watch"


def test_the_glyph_table_is_not_duplicated(monkeypatch):
    """Two tables that could disagree is how the dial and the gate start
    meaning different things by the same word."""
    from backend.core.ouroboros.governance import trust_repl as t
    monkeypatch.setenv("JARVIS_PROACTIVE_MODE_ENABLED", "1")
    for rung in pm.LADDER:
        assert t._glyph_for(rung.name) == rung.glyph
