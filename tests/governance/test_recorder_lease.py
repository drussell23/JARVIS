"""The recorder waits as long as the op is entitled to run.

Replays session ``bt-2026-09-08-193049``: the pool granted op
``op-01a08280-f4f2`` a **1530 s** ceiling; the recorder expired its generation
at **900 s**; the op's real verdict (``failed / l2_stopped``) arrived at
1447.75 s with nothing left in ``_pending`` to attach it to.

With the local lane clamped to one worker, every op costs ~1450 s — so the
constant did not lose a row occasionally, it lost the entire corpus by
arithmetic.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance.observability import recorder_lease as RL

STATIC_TTL_S = 900.0          # _DEFAULT_PENDING_TTL_S
LIVE_CEILING_S = 1530.0       # the pool's adaptive ceiling for that op
LIVE_DURATION_S = 1447.75     # how long it actually ran
LIVE_OP = "op-01a08280-f4f2-7429-92cf-47d9c1ca5b07-cau"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for name in (
        RL.ENV_ENABLED, RL.ENV_BUFFER_FRACTION,
        RL.ENV_MAX_LEASE_S, RL.ENV_REGISTRY_MAX,
    ):
        monkeypatch.delenv(name, raising=False)
    RL.reset_for_tests()
    yield
    RL.reset_for_tests()


# --------------------------------------------------------------------------
# The defect
# --------------------------------------------------------------------------

def test_the_live_op_would_no_longer_expire_before_its_verdict():
    """THE regression."""
    RL.register_lease(LIVE_OP, LIVE_CEILING_S)
    ttl = RL.effective_ttl_s(LIVE_OP, STATIC_TTL_S)
    assert ttl > LIVE_DURATION_S, (
        f"lease {ttl:.0f}s still expires before the op's {LIVE_DURATION_S}s verdict"
    )


def test_the_static_ttl_was_shorter_than_the_ceiling_by_construction():
    """Not a tuning miss: the pool's ceiling is adaptive and the TTL was not,
    so past 900s EVERY long op was destined to be written as unknown."""
    assert STATIC_TTL_S < LIVE_CEILING_S


def test_the_recorder_asks_for_a_per_op_ttl():
    import inspect

    from backend.core.ouroboros.governance.observability import (
        trajectory_recorder as TR,
    )

    src = inspect.getsource(TR.TrajectoryRecorder._expire_pending)
    assert "_lease_ttl_for(op_id" in src, "the sweep still uses one global TTL"


# --------------------------------------------------------------------------
# A lease may only ever LENGTHEN the wait
# --------------------------------------------------------------------------

def test_no_lease_is_byte_identical_to_today():
    assert RL.effective_ttl_s("never-registered", STATIC_TTL_S) == STATIC_TTL_S


def test_a_short_lease_cannot_shorten_the_wait():
    """Arming this must not cause a single expiry that would not have happened
    anyway."""
    RL.register_lease("short", 60.0)
    assert RL.effective_ttl_s("short", STATIC_TTL_S) == STATIC_TTL_S


@pytest.mark.parametrize("ceiling", [1.0, 30.0, 300.0, 899.0, 901.0, 5000.0])
def test_the_answer_is_never_below_the_static_ttl(ceiling):
    RL.register_lease("op", ceiling)
    assert RL.effective_ttl_s("op", STATIC_TTL_S) >= STATIC_TTL_S


# --------------------------------------------------------------------------
# Extension is just a later deadline
# --------------------------------------------------------------------------

def test_a_restamp_only_ever_extends():
    """Mirrors restamp_pipeline_deadline_at_start: an op is never punished for
    a re-registration that happened to observe less remaining ceiling."""
    RL.register_lease("mono", 1000.0)
    first = RL.lease_remaining_s("mono")
    RL.register_lease("mono", 10.0)
    assert RL.lease_remaining_s("mono") >= first - 1.0


def test_an_fsm_extension_moves_the_deadline_out():
    RL.register_lease("op", 100.0)
    before = RL.lease_remaining_s("op")
    RL.register_lease("op", 5000.0, source="fsm_extension")
    assert RL.lease_remaining_s("op") > before


def test_the_pool_renews_the_lease_when_the_FSM_grants_time():
    """Without this a generation still legitimately in flight is swept into
    the corpus as `unknown` while the op that will label it is still running."""
    import inspect

    from backend.core.ouroboros.governance import background_agent_pool as P

    src = inspect.getsource(P.BackgroundAgentPool._worker_loop)
    assert 'source="fsm_extension"' in src


def test_the_pool_keys_the_lease_by_the_CONTEXT_op_id():
    """The recorder keys _pending by the context op id and has never seen a
    bgop id; keying by the wrong one would register a lease nothing reads."""
    import inspect

    from backend.core.ouroboros.governance import background_agent_pool as P

    src = inspect.getsource(P.BackgroundAgentPool._worker_loop)
    assert '_lease_op_id = str(getattr(_ctx_to_run, "op_id", "") or "")' in src


# --------------------------------------------------------------------------
# Bounded, and self-healing
# --------------------------------------------------------------------------

def test_the_registry_is_bounded(monkeypatch):
    monkeypatch.setenv(RL.ENV_REGISTRY_MAX, "8")
    for i in range(500):
        RL.register_lease(f"op-{i}", 100.0)
    assert RL.stats()["open"] <= 8


def test_a_missed_release_leaks_nothing(monkeypatch):
    """Release is an optimisation: a worker that dies mid-op must not pin
    generations in memory for the session."""
    import inspect

    src = inspect.getsource(RL._sweep_locked)
    assert "grace" in src
    monkeypatch.setenv(RL.ENV_MAX_LEASE_S, "1")
    RL.register_lease("stale", 1.0)
    import time as _t
    _t.sleep(0.01)
    with RL._lock:
        RL._sweep_locked(_t.monotonic() + 100.0)
    assert RL.stats()["open"] == 0


def test_release_drops_the_lease():
    RL.register_lease("op", 100.0)
    RL.release_lease("op")
    assert RL.lease_remaining_s("op") == 0.0


def test_a_single_lease_is_capped(monkeypatch):
    monkeypatch.setenv(RL.ENV_MAX_LEASE_S, "300")
    RL.register_lease("huge", 999_999.0)
    assert RL.effective_ttl_s("huge", 1.0) <= 300.0


# --------------------------------------------------------------------------
# Fail-closed means KEEP the row
# --------------------------------------------------------------------------

@pytest.mark.parametrize("hostile", [None, "", 0, object(), 3.5, b"x", -1])
def test_hostile_input_degrades_to_the_static_ttl(hostile):
    assert RL.effective_ttl_s(hostile, STATIC_TTL_S) == STATIC_TTL_S
    RL.register_lease(hostile, hostile)
    RL.release_lease(hostile)
    RL.lease_remaining_s(hostile)


def test_a_fault_is_counted_not_swallowed():
    """A degraded lease has to be VISIBLE, or it silently reverts to the
    constant this module exists to remove."""
    RL.register_lease("", 100.0)
    RL.register_lease("op", -5.0)
    assert RL.stats()["faults"] >= 2


def test_the_fault_type_is_named_but_never_raised():
    assert issubclass(RL.RecorderLeaseFault, RuntimeError)
    import inspect
    src = inspect.getsource(RL._fault)
    assert "raise" not in src, "a lease fault must never reach the op thread"


def test_the_recorder_falls_back_when_the_lease_module_is_unreadable(monkeypatch):
    from backend.core.ouroboros.governance.observability import (
        trajectory_recorder as TR,
    )

    def _boom(*_a, **_k):
        raise RuntimeError("lease module gone")

    monkeypatch.setattr(RL, "effective_ttl_s", _boom)
    assert TR._lease_ttl_for("op", STATIC_TTL_S) == STATIC_TTL_S


def test_the_kill_switch_restores_the_constant(monkeypatch):
    monkeypatch.setenv(RL.ENV_ENABLED, "false")
    RL.register_lease(LIVE_OP, LIVE_CEILING_S)
    assert RL.effective_ttl_s(LIVE_OP, STATIC_TTL_S) == STATIC_TTL_S


def test_the_expiry_log_names_which_clock_ran_out():
    """'lease' means a real stall; 'static' means no lease was ever published
    — the condition that made the constant silently wrong."""
    import inspect

    from backend.core.ouroboros.governance.observability import (
        trajectory_recorder as TR,
    )

    src = inspect.getsource(TR.TrajectoryRecorder._expire_pending)
    assert '"lease" if ttl > static_ttl else "static"' in src
