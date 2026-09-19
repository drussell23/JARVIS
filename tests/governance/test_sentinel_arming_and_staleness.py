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


def test_the_flag_is_CONSUMED_not_forwarded():
    """The legacy bootstrap has no `--sentinel`; forwarding it would surface
    as an argparse error at boot instead of as autonomy."""
    inv = OV.resolve(["--sentinel"])
    assert "--sentinel" not in inv.delegate_argv


def test_it_survives_alongside_other_flags():
    inv = OV.resolve(["--sentinel", "-v", "--cost-cap", "2.00"])
    assert inv.delegate_argv == ["-v", "--cost-cap", "2.00"]


def test_without_the_flag_nothing_is_armed(monkeypatch):
    for f in OV._SENTINEL_FLAGS:
        monkeypatch.delenv(f, raising=False)
    OV.resolve([])
    assert OV.sentinel_is_armed() is False


def test_half_armed_is_not_armed():
    """Discovery alone files goals a human approves; sentinel alone
    auto-approves goals a human writes. Only the composition drives."""
    assert OV.sentinel_is_armed({"JARVIS_SENTINEL_MODE_ENABLED": "true"}) is False
    assert OV.sentinel_is_armed({"JARVIS_GOAL_DISCOVERY_ENABLED": "true"}) is False


def test_the_boot_line_states_BOTH_modes():
    """Both states are legitimate; being unable to tell them apart is not."""
    import inspect

    src = inspect.getsource(OV.main)
    assert "ARMED" in src and "dormant" in src
    assert "ov --sentinel" in src
