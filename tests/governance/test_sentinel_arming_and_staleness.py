"""Two defects that kept the operator from seeing the Mechanic work.

## The staleness invariant the envelope was violating

`harness._OP_STALE_THRESHOLD_S` states it in its own comment: *"this threshold
MUST exceed the largest single-phase budget an op can legitimately consume,
else a long-but-active phase is mis-classified stale and the session is shut
down mid-flight"*, and *"a soak with large adaptive budgets MUST set
OUROBOROS_OP_STALE_THRESHOLD_S above its per-op budget ceiling"*.

Nothing set it. Its default is 1200s while `production_envelope` hands the same
session a 3726s generation budget — the envelope violated an invariant it had
every input to satisfy. Measured in `bt-2026-09-19-194820`: a VALIDATE pytest
legitimately ran 474s, the op went 33 minutes between FSM transitions,
`all_ops_stale` fired, and the session died at 51 of its 150 planned minutes —
4 passes instead of ~20. Every measurement taken on these budgets was truncated
the same way.

## The Sentinel was unreachable from the command people type

`ov` never mentioned the two flags, `.env` leaves them unset, and the envelope
sets them for neither profile — so `ov` produced a cockpit that ran only what
the operator typed and never discovered work of its own. The capability existed
solely behind `cockpit_interactive.sh --sentinel`, a different entry point.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.cli import ov as OV
from backend.core.ouroboros.governance import production_envelope as PE


# --------------------------------------------------------------------------
# The staleness invariant
# --------------------------------------------------------------------------

def test_the_threshold_exceeds_the_largest_phase_budget():
    """THE invariant, restated as a test so it cannot drift silently."""
    env = PE.build("soak")
    threshold = float(env.as_env()["OUROBOROS_OP_STALE_THRESHOLD_S"])
    generation = float(env.as_env()["JARVIS_GENERATION_TIMEOUT_S"])
    assert threshold > generation, (
        f"threshold {threshold} must exceed the generation budget {generation}"
    )


def test_it_holds_across_every_profile_and_wall():
    for profile in sorted(PE.PROFILES):
        for wall in (600, 3600, 9000, 20000):
            env = PE.build(profile, wall_s=wall).as_env()
            assert float(env["OUROBOROS_OP_STALE_THRESHOLD_S"]) > \
                float(env["JARVIS_GENERATION_TIMEOUT_S"]), (profile, wall)


def test_the_measured_failure_would_not_recur():
    """bt-2026-09-19-194820: generation budget 3726s, threshold 1200s."""
    env = PE.build("soak", wall_s=9000).as_env()
    assert float(env["JARVIS_GENERATION_TIMEOUT_S"]) == pytest.approx(3726, abs=5)
    assert float(env["OUROBOROS_OP_STALE_THRESHOLD_S"]) > 1200


def test_force_cancel_sits_above_the_threshold():
    """Below it, the canceller reaps an op the monitor has not called stale."""
    env = PE.build("soak").as_env()
    assert float(env["OUROBOROS_OP_FORCE_CANCEL_S"]) > \
        float(env["OUROBOROS_OP_STALE_THRESHOLD_S"])


def test_an_operator_value_still_wins():
    """`hydrate` is setdefault — the envelope fills silence, never overrides."""
    environ = {"OUROBOROS_OP_STALE_THRESHOLD_S": "999"}
    applied, overridden = PE.hydrate("soak", environ=environ)
    assert environ["OUROBOROS_OP_STALE_THRESHOLD_S"] == "999"
    assert "OUROBOROS_OP_STALE_THRESHOLD_S" in overridden


# --------------------------------------------------------------------------
# Sentinel arming
# --------------------------------------------------------------------------

def test_neither_flag_is_set_by_default():
    """The composition stays deliberate: nothing arms it implicitly."""
    env = PE.build("cockpit").as_env()
    for flag in OV._SENTINEL_FLAGS:
        assert flag not in env


def test_arming_sets_BOTH_switches():
    """`_start_sentinel_loop` requires both; arming one would be a cockpit
    that looks armed and discovers nothing."""
    environ: dict = {}
    assert OV.arm_sentinel(environ) is True
    assert environ["JARVIS_SENTINEL_MODE_ENABLED"] == "true"
    assert environ["JARVIS_GOAL_DISCOVERY_ENABLED"] == "true"
    assert OV.sentinel_is_armed(environ) is True


def test_an_explicit_operator_refusal_is_not_overridden():
    """Someone who exported `false` meant it."""
    environ = {"JARVIS_SENTINEL_MODE_ENABLED": "false"}
    assert OV.arm_sentinel(environ) is False
    assert environ["JARVIS_SENTINEL_MODE_ENABLED"] == "false"
    assert OV.sentinel_is_armed(environ) is False


@pytest.mark.parametrize("flag", ["--sentinel", "--no-sentinel",
                                  "--no-production-soak"])
def test_the_flags_are_CONSUMED_not_forwarded(flag):
    """The legacy bootstrap knows none of them; forwarding one would surface
    as an argparse error at boot instead of as autonomy."""
    inv = OV.resolve([flag])
    assert flag not in inv.delegate_argv


def test_it_survives_alongside_other_flags():
    inv = OV.resolve(["--no-sentinel", "-v", "--cost-cap", "2.00"])
    assert inv.delegate_argv == ["-v", "--cost-cap", "2.00"]


def test_bare_ov_arms_the_sentinel_AND_the_production_profile():
    """Operator directive 2026-09-26: the cockpit's organism is the soak's
    organism. Before, soaks ran Sentinel-on and `ov` ran it dormant, so soak
    progress never showed in the cockpit."""
    inv = OV.resolve([])
    assert inv.sentinel is True
    assert inv.production_soak is True
    environ: dict = {}
    OV.apply_arming(inv, environ)
    assert OV.sentinel_is_armed(environ) is True
    assert environ[OV._PRODUCTION_SOAK_ENV] == "1"


def test_resolving_has_no_side_effects(monkeypatch):
    """Parsing an argv must never arm anything; only main() applies it."""
    for f in (*OV._SENTINEL_FLAGS, OV._PRODUCTION_SOAK_ENV):
        monkeypatch.delenv(f, raising=False)
    OV.resolve([])
    assert OV.sentinel_is_armed() is False
    import os
    assert OV._PRODUCTION_SOAK_ENV not in os.environ


def test_each_opt_out_is_independent():
    inv = OV.resolve(["--no-sentinel"])
    assert (inv.sentinel, inv.production_soak) == (False, True)
    inv = OV.resolve(["--no-production-soak"])
    assert (inv.sentinel, inv.production_soak) == (True, False)


def test_an_explicit_production_soak_refusal_is_not_overridden():
    environ = {OV._PRODUCTION_SOAK_ENV: "0"}
    assert OV.arm_production_soak(environ) is False
    assert environ[OV._PRODUCTION_SOAK_ENV] == "0"


def test_the_headless_verbs_are_unchanged():
    """`ov run` / `ov daemon` keep their own contract; only the cockpit drives
    by default."""
    for verb in ("run", "daemon"):
        inv = OV.resolve([verb])
        assert (inv.sentinel, inv.production_soak) == (False, False)


def test_half_armed_is_not_armed():
    """Discovery alone files goals a human approves; sentinel alone
    auto-approves goals a human writes. Only the composition drives."""
    assert OV.sentinel_is_armed({"JARVIS_SENTINEL_MODE_ENABLED": "true"}) is False
    assert OV.sentinel_is_armed({"JARVIS_GOAL_DISCOVERY_ENABLED": "true"}) is False


# --------------------------------------------------------------------------
# Production-soak limits respect the environment `ov` hands its daemon
# --------------------------------------------------------------------------

def _limits(argv, environ, cockpit_boot=False):
    from types import SimpleNamespace

    from scripts.ouroboros_battle_test import apply_production_soak_limits

    args = SimpleNamespace(cost_cap=0.71, idle_timeout=86400.0,
                           max_wall_seconds=0.0)
    apply_production_soak_limits(args, argv, environ, cockpit_boot=cockpit_boot)
    return args


def test_silence_is_scaled_to_the_profile():
    args = _limits([], {})
    assert (args.cost_cap, args.idle_timeout, args.max_wall_seconds) == \
        (25.00, 0.0, 0.0)


def test_the_cockpit_daemons_env_cap_survives():
    """Measured 2026-09-26: the cockpit read "$0.00 / $25.00" because the
    derived cap `ov` exports was overwritten by the profile."""
    args = _limits([], {"OUROBOROS_BATTLE_COST_CAP": "0.71",
                        "OUROBOROS_BATTLE_IDLE_TIMEOUT": "86400"})
    assert args.cost_cap == 0.71
    assert args.idle_timeout == 86400.0


def test_the_cockpit_cap_counts_only_on_a_cockpit_boot():
    env = {"JARVIS_COCKPIT_COST_CAP": "2.50"}
    assert _limits([], env, cockpit_boot=True).cost_cap == 0.71
    assert _limits([], env, cockpit_boot=False).cost_cap == 25.00


def test_an_argv_flag_still_wins():
    assert _limits(["--cost-cap", "0.71"], {}).cost_cap == 0.71


def test_the_boot_line_states_BOTH_modes():
    """Both states are legitimate; being unable to tell them apart is not."""
    import inspect

    src = inspect.getsource(OV.main)
    assert "ARMED" in src and "dormant" in src
    assert "--no-sentinel" in src
    assert "apply_arming(inv)" in src
