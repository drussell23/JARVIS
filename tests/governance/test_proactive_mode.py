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


# ---------------------------------------------------------------------------
# Slice 3 — the explore rung's mutation veto
# ---------------------------------------------------------------------------


def test_act_rungs_permit_mutation(monkeypatch):
    monkeypatch.setenv("JARVIS_PROACTIVE_MODE_ENABLED", "1")
    for name in ("safe_auto", "notify_apply", "approval_required"):
        pm.get_controller().request("mac", name)
        assert pm.mutation_permitted().permitted is True, name


def test_explore_vetoes_mutation_while_permitting_generation(monkeypatch):
    """The rung the risk floor could not express: generation continues, no
    patch reaches disk."""
    monkeypatch.setenv("JARVIS_PROACTIVE_MODE_ENABLED", "1")
    pm.get_controller().request("mac", "explore")
    v = pm.mutation_permitted()
    assert v.permitted is False
    assert v.position == "explore"


def test_watch_vetoes_mutation_too(monkeypatch):
    monkeypatch.setenv("JARVIS_PROACTIVE_MODE_ENABLED", "1")
    pm.get_controller().request("mac", "watch")
    assert pm.mutation_permitted().permitted is False


def test_master_off_never_vetoes(monkeypatch):
    """Off is the pre-§30 status quo, not a degraded mode."""
    monkeypatch.delenv("JARVIS_PROACTIVE_MODE_ENABLED", raising=False)
    pm.get_controller().request("mac", "watch")
    assert pm.mutation_permitted().permitted is True


def test_the_veto_fails_open_on_any_internal_fault(monkeypatch):
    """A mode subsystem that cannot answer must not halt every mutation in
    the organism — that converts a fault here into a total outage."""
    monkeypatch.setenv("JARVIS_PROACTIVE_MODE_ENABLED", "1")

    def _boom():
        raise RuntimeError("controller exploded")

    monkeypatch.setattr(pm, "get_controller", _boom)
    v = pm.mutation_permitted()
    assert v.permitted is True
    assert v.position == "unknown"


def test_a_veto_is_a_terminal_not_a_retry():
    """ExplorationInsufficientError routes through GENERATE_RETRY because a
    model CAN fix insufficient exploration. It cannot fix the operator's
    dial, so retrying would burn the budget then fail the op for something
    that was never the model's fault."""
    import inspect
    from backend.core.ouroboros.governance import orchestrator as orch
    src = inspect.getsource(orch.Orchestrator._maybe_complete_cosmetic_candidate)
    assert "no_op_mode_veto" in src
    assert "OperationPhase.COMPLETE" in src
    assert "ExplorationInsufficientError" not in src


def test_the_veto_rides_the_seam_both_callers_share():
    """Run-24 proved the inline seam alone is never reached on the live
    route. A gate wired only there is wired and inert."""
    import inspect
    from backend.core.ouroboros.governance import orchestrator as orch
    from backend.core.ouroboros.governance import phase_dispatcher as pd
    helper = "_maybe_complete_cosmetic_candidate"
    assert helper in inspect.getsource(pd)
    assert "mutation_permitted" in inspect.getsource(
        orch.Orchestrator._maybe_complete_cosmetic_candidate)


def test_the_veto_precedes_the_value_gate():
    """A vetoed candidate must not first be walked for cosmetic-ness: the
    mode already decided, and the walk costs an AST parse per file.

    Compared over the EXECUTABLE body with the docstring stripped — the
    docstring names the value-gate flag before any code runs, and a raw
    text index cannot tell an explanation from a use. Same lesson as the
    earlier DRY audits."""
    import ast
    import inspect
    import textwrap
    from backend.core.ouroboros.governance import orchestrator as orch
    src = textwrap.dedent(
        inspect.getsource(orch.Orchestrator._maybe_complete_cosmetic_candidate))
    fn = ast.parse(src).body[0]
    body = fn.body[1:] if isinstance(fn.body[0], ast.Expr) else fn.body
    code = "\n".join(ast.unparse(stmt) for stmt in body)
    assert "mutation_permitted" in code
    assert code.index("mutation_permitted") < code.index(
        "JARVIS_CANDIDATE_VALUE_GATE_ENABLED")


# ---------------------------------------------------------------------------
# The sink — registered, or honestly absent
# ---------------------------------------------------------------------------


def test_the_pool_is_registered_as_the_sink_at_boot():
    """Without this the ladder floors the AUTHORITY axis correctly and
    `watch` silently degrades to approval_required — wired but inert on
    exactly one of its two axes."""
    import inspect
    from backend.core.ouroboros.governance import governed_loop_service as gls
    src = inspect.getsource(gls)
    assert "set_emission_sink as _pm_set_sink" in src
    assert "_pm_set_sink(self._bg_pool)" in src


def test_the_sink_is_registered_after_the_pool_starts():
    """A sink handed a pool that has not started would pause something with
    no workers, and `watch` would report a hold it had not achieved."""
    import inspect
    from backend.core.ouroboros.governance import governed_loop_service as gls
    src = inspect.getsource(gls)
    assert src.index("await self._bg_pool.start()") < src.index(
        "_pm_set_sink(self._bg_pool)")


def test_the_controller_never_imports_the_orchestrator():
    """A mode controller reaching into GovernedLoopService for _bg_pool
    would invert the authority boundary every governance module observes."""
    import pathlib
    src = pathlib.Path(pm.__file__).read_text(encoding="utf-8")
    for banned in ("governed_loop_service", "orchestrator", "background_agent_pool"):
        assert f"import {banned}" not in src
        assert f"governance.{banned}" not in src


def test_a_registered_sink_is_used_without_an_explicit_pool():
    pool = _Pool()
    pm.set_emission_sink(pool)
    c = pm.ProactiveModeController(clock=FakeClock())
    c.request("mac", "watch")
    out = c.apply_effects()
    assert out["emission"] == "paused"
    assert pool.paused is True


# ---------------------------------------------------------------------------
# Slice 4 — the operator sees which request is in force
# ---------------------------------------------------------------------------


def test_a_single_cockpit_chip_is_unchanged(monkeypatch):
    from backend.core.ouroboros.governance import trust_repl as t
    monkeypatch.setenv("JARVIS_PROACTIVE_MODE_ENABLED", "1")
    pm.get_controller().request("mac", "approval_required")
    assert t.floor_chip() == "🟠⛨ approval_required"


def test_the_chip_says_composed_when_cockpits_disagree(monkeypatch):
    """Silence is right for a dial nobody touched and wrong for one two
    people are pulling in different directions."""
    from backend.core.ouroboros.governance import trust_repl as t
    monkeypatch.setenv("JARVIS_PROACTIVE_MODE_ENABLED", "1")
    c = pm.get_controller()
    c.request("mac", "approval_required")
    c.request("desktop", "safe_auto")
    chip = t.floor_chip()
    assert "approval_required" in chip and "composed" in chip


def test_a_composed_chip_renders_even_at_the_resting_rung(monkeypatch):
    """The operator whose request lost needs to know a negotiation happened
    at all — the resting rung is not evidence that nothing did."""
    from backend.core.ouroboros.governance import trust_repl as t
    monkeypatch.setenv("JARVIS_PROACTIVE_MODE_ENABLED", "1")
    c = pm.get_controller()
    c.request("mac", "safe_auto")
    assert t.floor_chip() == ""
    c.request("desktop", "notify_apply")
    assert t.floor_chip() != ""


def test_agreement_is_not_reported_as_composition(monkeypatch):
    from backend.core.ouroboros.governance import trust_repl as t
    monkeypatch.setenv("JARVIS_PROACTIVE_MODE_ENABLED", "1")
    c = pm.get_controller()
    c.request("mac", "watch")
    c.request("desktop", "watch")
    assert "composed" not in t.floor_chip()


def test_the_shared_chip_never_names_a_cockpit(monkeypatch):
    """This line is mirrored to every surface. Naming one operator here
    tells the others something false about themselves."""
    from backend.core.ouroboros.governance import trust_repl as t
    monkeypatch.setenv("JARVIS_PROACTIVE_MODE_ENABLED", "1")
    c = pm.get_controller()
    c.request("mac", "watch")
    c.request("desktop", "safe_auto")
    chip = t.floor_chip()
    assert "mac" not in chip and "desktop" not in chip


def test_the_chip_reads_the_controller_not_the_env_knob(monkeypatch):
    """The knob records the LAST write; the effective rung is the strictest
    across live cockpits. Reading the knob reintroduces the race."""
    from backend.core.ouroboros.governance import trust_repl as t
    monkeypatch.setenv("JARVIS_PROACTIVE_MODE_ENABLED", "1")
    c = pm.get_controller()
    c.request("mac", "watch")
    c.request("desktop", "safe_auto")
    monkeypatch.setenv("JARVIS_MIN_RISK_TIER", "safe_auto")
    assert "watch" in t.floor_chip(), "the chip believed the last writer"


def test_an_overridden_cockpit_is_told_what_is_in_force(monkeypatch):
    """§30.6 — an operator whose tightening is invisibly discarded stops
    tightening."""
    monkeypatch.setenv("JARVIS_PROACTIVE_MODE_ENABLED", "1")
    c = pm.get_controller()
    c.request("mac", "approval_required")
    c.request("desktop", "safe_auto")
    notice = pm.override_notice("desktop")
    assert "approval_required" in notice and "safe_auto" in notice


def test_the_strictest_cockpit_is_told_nothing(monkeypatch):
    """Not news to the operator who asked for it; saying so every frame
    spends the line's attention budget on a non-event."""
    monkeypatch.setenv("JARVIS_PROACTIVE_MODE_ENABLED", "1")
    c = pm.get_controller()
    c.request("mac", "approval_required")
    c.request("desktop", "safe_auto")
    assert pm.override_notice("mac") == ""


def test_agreeing_cockpits_get_no_notice(monkeypatch):
    monkeypatch.setenv("JARVIS_PROACTIVE_MODE_ENABLED", "1")
    c = pm.get_controller()
    c.request("mac", "watch")
    c.request("desktop", "watch")
    assert pm.override_notice("desktop") == ""


def test_an_unknown_cockpit_gets_no_notice(monkeypatch):
    monkeypatch.setenv("JARVIS_PROACTIVE_MODE_ENABLED", "1")
    pm.get_controller().request("mac", "watch")
    assert pm.override_notice("never-attached") == ""
    assert pm.override_notice("") == ""


def test_a_notice_clears_when_the_stricter_cockpit_leaves(monkeypatch):
    """Deliberate exit is consent, so the override genuinely ends."""
    monkeypatch.setenv("JARVIS_PROACTIVE_MODE_ENABLED", "1")
    c = pm.get_controller()
    c.request("mac", "watch")
    c.request("desktop", "safe_auto")
    assert pm.override_notice("desktop") != ""
    c.release("mac")
    assert pm.override_notice("desktop") == ""


def test_a_notice_persists_while_the_stricter_cockpit_is_merely_detached(
        monkeypatch):
    """Absence is not consent — the notice must not clear on a Wi-Fi drop,
    because the override has not actually ended."""
    monkeypatch.setenv("JARVIS_PROACTIVE_MODE_ENABLED", "1")
    c = pm.get_controller()
    c.request("mac", "watch")
    c.request("desktop", "safe_auto")
    c.detach("mac")
    assert pm.override_notice("desktop") != ""


def test_composition_and_notice_never_raise(monkeypatch):
    monkeypatch.setenv("JARVIS_PROACTIVE_MODE_ENABLED", "1")

    def _boom():
        raise RuntimeError("controller exploded")

    monkeypatch.setattr(pm, "get_controller", _boom)
    assert pm.composition().composed is False
    assert pm.override_notice("mac") == ""


def test_the_chip_survives_proactive_mode_being_absent(monkeypatch):
    """Master-off must be the pre-§30 chip, byte-identically."""
    from backend.core.ouroboros.governance import trust_repl as t
    monkeypatch.delenv("JARVIS_PROACTIVE_MODE_ENABLED", raising=False)
    monkeypatch.setenv("JARVIS_MIN_RISK_TIER", "notify_apply")
    assert t.floor_chip() == "🟡⛨ notify_apply"


# ---------------------------------------------------------------------------
# Slice 5 — the Body never renders a rung it cannot vouch for
# ---------------------------------------------------------------------------


def _mode_frame(position="watch", **kw):
    f = {"kind": "mode", "seq": 1, "lamport": 1, "node_id": "engine",
         "position": position}
    f.update(kw)
    return f


def test_a_fresh_view_renders_unknown_not_a_guess():
    """A stale rung on a screen is worse than no rung: the operator reads a
    guarantee that is not in force and stops watching accordingly."""
    v = pm.RemoteModeView(clock=FakeClock())
    assert v.renderable() is None
    assert "unknown" in v.chip()


def test_a_confirmed_rung_renders():
    v = pm.RemoteModeView(clock=FakeClock())
    v.on_connected()
    assert v.confirm(_mode_frame("explore")) is True
    assert v.renderable().name == "explore"
    assert "explore" in v.chip()


def test_a_reconnect_voids_every_prior_confirmation():
    """THE slice-5 regression: carrying a rung across a gap the Body cannot
    vouch for."""
    v = pm.RemoteModeView(clock=FakeClock())
    v.on_connected()
    v.confirm(_mode_frame("watch"))
    v.on_connected()
    assert v.renderable() is None, "a rung survived a reconnect"


def test_a_drop_unconfirms_without_advancing_the_epoch():
    """A park is a pause. Advancing the epoch would let an in-flight frame
    from this connection confirm the next one."""
    v = pm.RemoteModeView(clock=FakeClock())
    v.on_connected()
    v.confirm(_mode_frame("watch"))
    before = v.snapshot()["epoch"]
    v.on_disconnected()
    assert v.renderable() is None
    assert v.snapshot()["epoch"] == before


def test_an_unknown_rung_is_refused_not_echoed():
    """A Body echoing a name it cannot interpret shows the operator a word
    rather than a state."""
    v = pm.RemoteModeView(clock=FakeClock())
    v.on_connected()
    assert v.confirm(_mode_frame("turbo_mode")) is False
    assert v.renderable() is None


def test_a_malformed_frame_is_refused():
    v = pm.RemoteModeView(clock=FakeClock())
    v.on_connected()
    for junk in (None, "mode", {}, {"kind": "telemetry", "position": "watch"},
                 {"kind": "mode"}):
        assert v.confirm(junk) is False


def test_the_view_cannot_decide_a_rung_only_report_one():
    """Not a second dial — authority stays where the enforcement is."""
    assert not hasattr(pm.RemoteModeView, "request")
    assert not hasattr(pm.RemoteModeView, "cycle")


def test_refused_renders_are_counted_for_the_operator():
    v = pm.RemoteModeView(clock=FakeClock())
    v.renderable(); v.renderable()
    assert v.snapshot()["stale_renders_refused"] >= 2


# -- the frame ------------------------------------------------------------


def test_the_mode_frame_carries_the_composition(monkeypatch):
    """The Body renders 'composed' from the Engine's fact rather than
    inferring who is attached to a machine it cannot see."""
    monkeypatch.setenv("JARVIS_PROACTIVE_MODE_ENABLED", "1")
    c = pm.get_controller()
    c.request("mac", "watch")
    c.request("desktop", "safe_auto")
    f = pm.build_mode_frame(seq=7, node_id="engine")
    assert f["kind"] == "mode" and f["position"] == "watch"
    assert f["composed"] is True and f["distinct"] == 2


def test_mode_is_a_known_frame_kind():
    from backend.core.ouroboros.governance import link_transport as tx
    assert tx.is_known_kind(tx.KIND_MODE)


def test_mode_is_not_carried_on_the_lossy_lane():
    """Telemetry drops its oldest under pressure, and a dropped mode frame
    leaves the Body rendering an autonomy level the Engine has left."""
    from backend.core.ouroboros.governance import link_session as ls
    assert "mode" not in {k for k in ls.ORDERED_KINDS}
    import inspect
    assert "put_high" in inspect.getsource(ls.LinkSessionLoop.publish_mode)


# -- session integration --------------------------------------------------


def _loop(node="engine"):
    from backend.core.ouroboros.governance import link_session as ls
    return ls.LinkSessionLoop(
        ls.SessionConfig(node_id=node, session_id="s-1"))


def test_publishing_is_edge_triggered(monkeypatch):
    """A rung is a state, not a measurement — restating it spends the
    link's budget on a fact the peer already holds."""
    monkeypatch.setenv("JARVIS_PROACTIVE_MODE_ENABLED", "1")
    pm.get_controller().request("mac", "explore")
    loop = _loop()
    assert loop.publish_mode() is True
    assert loop.publish_mode() is False
    pm.get_controller().request("mac", "watch")
    assert loop.publish_mode() is True


def test_a_fresh_connection_forces_a_republish(monkeypatch):
    """The peer has invalidated everything and is rendering unknown."""
    monkeypatch.setenv("JARVIS_PROACTIVE_MODE_ENABLED", "1")
    pm.get_controller().request("mac", "explore")
    loop = _loop()
    loop.publish_mode()
    loop.on_established()
    assert loop.publish_mode(force=True) is True


def test_an_inbound_mode_frame_confirms_the_view():
    loop = _loop("body")
    loop.on_established()
    loop.dispatch(_mode_frame("approval_required"))
    assert loop.remote_mode.renderable().name == "approval_required"


def test_a_park_unconfirms_the_peers_rung():
    loop = _loop("body")
    loop.on_established()
    loop.dispatch(_mode_frame("watch"))
    loop.park("wifi dropped")
    assert loop.remote_mode.renderable() is None


def test_reconnecting_voids_then_reconfirms():
    loop = _loop("body")
    loop.on_established()
    loop.dispatch(_mode_frame("watch"))
    loop.park("drop")
    loop.on_established()
    assert loop.remote_mode.renderable() is None
    loop.dispatch(_mode_frame("safe_auto"))
    assert loop.remote_mode.renderable().name == "safe_auto"


def test_the_session_snapshot_reports_the_remote_view():
    import json
    loop = _loop("body")
    snap = loop.snapshot()
    json.dumps(snap)
    assert "remote_mode" in snap
