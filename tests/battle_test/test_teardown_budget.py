"""The shutdown watchdog must outlast the teardown it guards.

`BoundedShutdownWatchdog.arm` is first-arm-wins by design — "this avoids
accidentally extending the deadline by re-arming" — so its deadline has to
be correct at arm time. It was not. The bare `default_deadline_s()` is 30 s,
while the trajectory flush alone is allowed 30 s *before*
`_generate_report` and `_shutdown_components` have run at all, and before
the auto-train hook (whose own configured budget is an hour) is even
reached.

Soak `bt-2026-09-02-025257` paid for that arithmetic exactly:

    21:28:09  ShutdownWatchdog ARMED  reason='wall_clock_cap' deadline_s=30.0
    21:28:39  ShutdownWatchdog FIRED  elapsed=30.0s — os._exit(75)

No `session flush` line, no `[AutoTrain]` line, 0 corpus rows — while the
session held seven pairable sibling groups that had already been generated
and were sitting in the recorder's pending map.

The fix composes the budget from the SAME knobs the guarded phases spend,
so the guard cannot drift from the work. These tests pin the arithmetic,
the single-source-of-truth coupling, and the resilience of every knob.
"""

from __future__ import annotations

import pytest

from backend.core.ouroboros.battle_test import harness as h
from backend.core.ouroboros.battle_test.shutdown_watchdog import (
    default_deadline_s,
)

_FLUSH = "JARVIS_TRAJECTORY_FLUSH_TIMEOUT_S"
_CLOSE = "JARVIS_TRAJECTORY_CLOSE_TIMEOUT_S"
_AT_ON = "JARVIS_GRPO_AUTOTRAIN_ENABLED"
_AT_TIMEOUT = "JARVIS_GRPO_AUTOTRAIN_TIMEOUT_S"
_AT_EVICT = "JARVIS_GRPO_AUTOTRAIN_EVICT_WAIT_S"


def _budget() -> float:
    """Call the unbound method — it touches no instance state, which is
    itself the point: the sizing reads CONFIGURATION, never op state."""
    return h.BattleTestHarness._teardown_budget_s(object())


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch):
    for var in (_FLUSH, _CLOSE, _AT_ON, _AT_TIMEOUT, _AT_EVICT):
        monkeypatch.delenv(var, raising=False)
    yield


# ---------------------------------------------------------------------------
# The arithmetic the soak disproved
# ---------------------------------------------------------------------------


def test_the_budget_exceeds_the_flush_it_guards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE regression. A guard equal to its first phase's budget can be
    fully consumed before the remaining teardown starts."""
    monkeypatch.setenv(_AT_ON, "false")
    assert _budget() > h._trajectory_flush_timeout_s()
    assert _budget() == pytest.approx(
        default_deadline_s() + h._trajectory_flush_timeout_s(),
    )


def test_raising_the_flush_budget_raises_the_guard_with_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One source of truth: the guard and the work read the same knob, so
    an operator who grants the flush more time cannot accidentally leave
    the watchdog behind."""
    monkeypatch.setenv(_AT_ON, "false")
    monkeypatch.setenv(_FLUSH, "300")
    assert h._trajectory_flush_timeout_s() == 300.0
    assert _budget() == pytest.approx(default_deadline_s() + 300.0)
    assert _budget() > h._trajectory_flush_timeout_s()


def test_autotrain_budget_is_included_only_when_it_will_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 3600 s allowance on a session that will never train is a watchdog
    switched off by accident, so the hook's budget is conditional on the
    hook actually being enabled."""
    monkeypatch.setenv(_AT_TIMEOUT, "3600")
    monkeypatch.setenv(_AT_EVICT, "120")

    monkeypatch.setenv(_AT_ON, "false")
    off = _budget()
    monkeypatch.setenv(_AT_ON, "true")
    on = _budget()

    assert on == pytest.approx(off + 3600.0 + 120.0)
    assert off < 200.0, "a non-training session keeps a tight guard"


def test_the_inner_close_budget_stays_under_the_outer_flush(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The queue drain must lose to the wall it is measured against, not
    the other way round — otherwise `wait_for` fires while `aclose` still
    believes it has time, and the flush is reported as a timeout rather
    than completing."""
    for flush in ("30", "120", "7"):
        monkeypatch.setenv(_FLUSH, flush)
        assert h._trajectory_close_timeout_s() < h._trajectory_flush_timeout_s()


def test_close_budget_can_be_pinned_independently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(_FLUSH, "300")
    monkeypatch.setenv(_CLOSE, "42")
    assert h._trajectory_close_timeout_s() == 42.0


# ---------------------------------------------------------------------------
# Resilience — a knob is operator input, and operators typo
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("junk", ["banana", "", "  ", "1e", "None"])
def test_unparseable_knobs_fall_back_and_never_raise(
    monkeypatch: pytest.MonkeyPatch, junk: str,
) -> None:
    monkeypatch.setenv(_FLUSH, junk)
    monkeypatch.setenv(_AT_TIMEOUT, junk)
    monkeypatch.setenv(_AT_ON, "true")
    assert h._trajectory_flush_timeout_s() == 30.0
    assert _budget() > 0.0


def test_a_negative_knob_cannot_shrink_the_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Clamped at zero: a negative budget must not subtract from the
    deadline and produce a guard that fires before teardown begins."""
    monkeypatch.setenv(_AT_ON, "false")
    monkeypatch.setenv(_FLUSH, "-500")
    assert h._trajectory_flush_timeout_s() == 0.0
    assert _budget() >= default_deadline_s()


def test_budget_never_raises_even_with_the_hook_unimportable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The auto-train hook is optional; a bare checkout still arms."""
    import builtins

    real = builtins.__import__

    def _blocked(name, *a, **kw):  # noqa: ANN001
        if name.endswith("training_trigger"):
            raise ImportError("hook absent")
        return real(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", _blocked)
    assert _budget() >= default_deadline_s()


def test_sizing_reads_configuration_not_op_state() -> None:
    """The Slice-47 invariant: the wall-clock cap and its hard-kill thread
    must stay blind to the op-ledger, because a wedged phase would keep an
    extend-condition true forever. This sizing is safe under that rule
    because it consults only env configuration — it takes no instance
    state, so there is nothing live for a wedge to influence, and the
    deadline it produces is fixed and finite once armed.
    """
    import inspect

    src = inspect.getsource(h.BattleTestHarness._teardown_budget_s)
    for forbidden in ("self._active_ops", "self._orchestrator", "_op_ledger",
                      "in_flight", "self._stack"):
        assert forbidden not in src, f"budget sizing must not read {forbidden}"
    # Callable with a bare object() — proof it depends on no attribute.
    assert h.BattleTestHarness._teardown_budget_s(object()) > 0.0
