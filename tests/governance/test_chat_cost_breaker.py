"""The Token Circuit Breaker — a chat brain that cannot run away with the bill.

The mandated contract:

  1. a normal turn routes to the REAL executor;
  2. pushing session cost past the cap trips the breaker, routes the next
     turn to the LOGGING executor, and fires the HUD notice — without
     raising, and without blocking the event loop.

Plus the properties that make it a session breaker rather than a second
per-instance counter: spend survives the executor that spent it, the cap
is read live from the environment, the projection comes from the wrapped
executor's own per-call cap, and the factory fails CLOSED — no breaker,
no brain.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from backend.core.ouroboros.governance import chat_cost_breaker as cb


class _Turn:
    def __init__(self, turn_id: str = "t-1") -> None:
        self.turn_id = turn_id
        self.session_id = "repl"


class _FakeClaude:
    """Stands in for ClaudeChatActionExecutor: spends on every call and
    exposes the same accounting surface the breaker reads."""

    _cost_cap_per_call_usd = 0.05

    def __init__(self, cost_per_call: float = 0.04) -> None:
        self.cumulative_cost_usd = 0.0
        self.calls: list = []
        self._per_call = cost_per_call

    def query_claude(self, message, turn, recent_turns):
        self.calls.append(message)
        self.cumulative_cost_usd += self._per_call
        return f"claude-answer-{turn.turn_id}"

    def dispatch_backlog(self, message, turn):
        return f"backlog-{turn.turn_id}"

    def spawn_subagent(self, message, turn):
        return f"subagent-{turn.turn_id}"

    def attach_context(self, message, turn, target_turn):
        return f"attach-{turn.turn_id}"


class _FakeLogging:
    def __init__(self) -> None:
        self.calls: list = []

    def query_claude(self, message, turn, recent_turns):
        self.calls.append(message)
        return f"logged-claude-{turn.turn_id}"

    def dispatch_backlog(self, message, turn):
        return "logged-backlog"

    def spawn_subagent(self, message, turn):
        return "logged-subagent"

    def attach_context(self, message, turn, target_turn):
        return "logged-attach"


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    """A ledger of our own — never the developer's real spend file."""
    monkeypatch.setenv(cb.LEDGER_PATH_ENV_VAR, str(tmp_path / "spend.json"))
    monkeypatch.setenv(cb.SESSION_BUDGET_ENV_VAR, "1.00")
    monkeypatch.delenv(cb.MASTER_FLAG_ENV_VAR, raising=False)
    ledger = cb.SessionCostLedger()
    yield ledger


def _mw(ledger, notices):
    return cb.CostCapMiddleware(
        _FakeClaude(), _FakeLogging(), ledger=ledger, notify=notices.append,
    )


# --------------------------------------------------------------------------
# 1. THE MANDATED CONTRACT (async — the breaker must not block the loop)
# --------------------------------------------------------------------------

def test_normal_routes_to_claude_then_trip_routes_to_logging(
    isolated,
) -> None:
    notices: list = []
    mw = _mw(isolated, notices)

    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        ticks = {"n": 0}

        async def _heartbeat() -> None:
            """Proves the loop keeps turning THROUGH the breaker."""
            while True:
                ticks["n"] += 1
                await asyncio.sleep(0.005)

        beat = asyncio.ensure_future(_heartbeat())
        try:
            # (1) a normal turn reaches the real brain
            answer = await loop.run_in_executor(
                None, mw.query_claude, "what is O+V?", _Turn("t-1"), [],
            )
            assert answer == "claude-answer-t-1"
            assert mw._primary.calls == ["what is O+V?"]
            assert mw._fallback.calls == []
            assert isolated.spent == pytest.approx(0.04)

            # (2) override the session cost past the cap
            isolated.record(1.00)

            answer2 = await loop.run_in_executor(
                None, mw.query_claude, "and again?", _Turn("t-2"), [],
            )
            assert answer2 == "logged-claude-t-2"       # degraded, not refused
            assert mw._fallback.calls == ["and again?"]
            assert mw._primary.calls == ["what is O+V?"]  # never called again
            assert any("budget exhausted" in n for n in notices)
            assert isolated.trips == 1
            await asyncio.sleep(0.02)
            assert ticks["n"] > 1                        # loop never blocked
        finally:
            beat.cancel()

    asyncio.run(scenario())


# --------------------------------------------------------------------------
# 2. session accounting — the root cause the per-instance cap misses
# --------------------------------------------------------------------------

def test_spend_survives_the_executor_that_spent_it(isolated) -> None:
    """A reconnect mints a fresh executor. Ten reconnects must not be ten
    budgets."""
    for _ in range(3):
        mw = _mw(isolated, [])
        mw.query_claude("q", _Turn(), [])
    assert isolated.spent == pytest.approx(0.12)


def test_ledger_is_file_backed_across_processes(isolated, tmp_path) -> None:
    """A daemon restart mid-runaway must not hand the next process a
    fresh allowance."""
    isolated.record(0.42)
    reborn = cb.SessionCostLedger()
    assert reborn.spent == pytest.approx(0.42)
    data = json.loads((tmp_path / "spend.json").read_text())
    assert data["spent_usd"] == pytest.approx(0.42)


def test_cap_is_read_live_from_the_environment(isolated, monkeypatch) -> None:
    monkeypatch.setenv(cb.SESSION_BUDGET_ENV_VAR, "0.10")
    assert cb.session_budget_usd() == pytest.approx(0.10)
    mw = _mw(isolated, [])
    isolated.record(0.09)
    assert mw.would_trip() is True          # 0.09 + 0.05 projected > 0.10
    monkeypatch.setenv(cb.SESSION_BUDGET_ENV_VAR, "5.00")
    assert mw.would_trip() is False         # no restart needed


def test_malformed_cap_falls_back_to_the_default(monkeypatch) -> None:
    monkeypatch.setenv(cb.SESSION_BUDGET_ENV_VAR, "not-a-number")
    assert cb.session_budget_usd() == cb.DEFAULT_SESSION_BUDGET_USD


def test_zero_cap_trips_immediately(isolated, monkeypatch) -> None:
    """A cap of zero means 'no chat spend', not 'unlimited'."""
    monkeypatch.setenv(cb.SESSION_BUDGET_ENV_VAR, "0")
    mw = _mw(isolated, [])
    assert mw.query_claude("q", _Turn("t-9"), []) == "logged-claude-t-9"


def test_projection_comes_from_the_wrapped_executor(isolated) -> None:
    """Not a constant here — tightening the executor must tighten the
    gate, or the two numbers drift."""
    mw = _mw(isolated, [])
    assert mw._projected_call_usd() == pytest.approx(0.05)
    mw._primary._cost_cap_per_call_usd = 0.25
    assert mw._projected_call_usd() == pytest.approx(0.25)


# --------------------------------------------------------------------------
# 3. degradation is total, and quiet
# --------------------------------------------------------------------------

def test_notice_is_said_once_per_streak(isolated) -> None:
    notices: list = []
    mw = _mw(isolated, notices)
    isolated.record(2.0)
    for i in range(4):
        mw.query_claude("q", _Turn(f"t-{i}"), [])
    assert len(notices) == 1                 # not a banner on every turn
    assert isolated.trips == 4               # but every trip is counted


def test_a_provider_fault_still_lands_the_turn(isolated) -> None:
    class _Exploding(_FakeClaude):
        def query_claude(self, message, turn, recent_turns):
            raise RuntimeError("provider down")

    mw = cb.CostCapMiddleware(_Exploding(), _FakeLogging(), ledger=isolated)
    assert mw.query_claude("q", _Turn("t-5"), []) == "logged-claude-t-5"


def test_a_gate_fault_degrades_safely(isolated) -> None:
    mw = _mw(isolated, [])

    def _boom() -> float:
        raise RuntimeError("ledger unreadable")

    mw._budget_fn = _boom
    # would_trip swallows and returns False, so the call proceeds; the
    # important part is that NOTHING raises into the dispatch path.
    assert mw.query_claude("q", _Turn("t-6"), []) in (
        "claude-answer-t-6", "logged-claude-t-6",
    )


def test_non_spending_legs_pass_through_untouched(isolated) -> None:
    mw = _mw(isolated, [])
    turn = _Turn("t-7")
    assert mw.dispatch_backlog("m", turn) == "backlog-t-7"
    assert mw.spawn_subagent("m", turn) == "subagent-t-7"
    assert mw.attach_context("m", turn, turn) == "attach-t-7"
    assert isolated.spent == 0.0             # they do not spend


def test_reset_rearms(isolated) -> None:
    notices: list = []
    mw = _mw(isolated, notices)
    isolated.record(2.0)
    assert mw.query_claude("q", _Turn("t-8"), []) == "logged-claude-t-8"
    isolated.reset()
    assert isolated.spent == 0.0
    assert mw.query_claude("q", _Turn("t-8b"), []) == "claude-answer-t-8b"


# --------------------------------------------------------------------------
# 4. fail-closed + telemetry
# --------------------------------------------------------------------------

def test_no_breaker_means_no_brain(isolated, monkeypatch) -> None:
    """Disabling the guardrail must not yield an UNGUARDED paid API."""
    monkeypatch.setenv(cb.MASTER_FLAG_ENV_VAR, "false")
    primary, fallback = _FakeClaude(), _FakeLogging()
    wrapped = cb.wrap_with_breaker(primary, fallback)
    assert wrapped is fallback
    wrapped.query_claude("q", _Turn("t-x"), [])
    assert primary.calls == []


def test_wrap_installs_the_breaker_by_default(isolated) -> None:
    wrapped = cb.wrap_with_breaker(_FakeClaude(), _FakeLogging())
    assert isinstance(wrapped, cb.CostCapMiddleware)


def test_chip_is_silent_at_rest_and_loud_once_spent(
    isolated, monkeypatch,
) -> None:
    monkeypatch.setattr(cb, "_LEDGER", isolated)
    assert cb.chat_budget_chip() == ""
    isolated.record(0.14)
    chip = cb.chat_budget_chip()
    assert "$0.14" in chip and "$1.00" in chip
    isolated.record(1.0)
    assert cb.chat_budget_chip().startswith("🛑")


def test_snapshot_shape(isolated) -> None:
    isolated.record(0.25)
    snap = isolated.snapshot()
    assert snap["spent_usd"] == pytest.approx(0.25)
    assert snap["cap_usd"] == pytest.approx(1.00)
    assert snap["remaining_usd"] == pytest.approx(0.75)
    assert snap["tripped"] is False
    assert snap["schema_version"] == cb.CHAT_COST_BREAKER_SCHEMA_VERSION


# --------------------------------------------------------------------------
# 5. wiring pins
# --------------------------------------------------------------------------

def test_factory_installs_the_breaker() -> None:
    import inspect
    from backend.core.ouroboros.governance import chat_repl_claude_executor
    src = inspect.getsource(
        chat_repl_claude_executor.build_chat_repl_dispatcher_with_claude,
    )
    assert "wrap_with_breaker(" in src
    # fail-closed: the except branch hands back the fallback, not the brain
    assert "wired_executor = fb" in src


def test_bridge_gives_the_breaker_a_voice() -> None:
    from backend.core.ouroboros.governance import chat_text_bridge
    src = Path(chat_text_bridge.__file__).read_text()
    assert "breaker_notify=print_sink" in src


def test_status_line_renders_the_chip() -> None:
    import inspect
    from backend.core.ouroboros.battle_test import status_line
    src = inspect.getsource(status_line.StatusLineBuilder.render_plain)
    assert "chat_budget_chip" in src


def test_harness_publishes_one_cost_surface() -> None:
    from backend.core.ouroboros.battle_test import harness
    src = Path(harness.__file__).read_text()
    assert "_set_active_cost_tracker(self._cost_tracker)" in src
    assert "def get_active_cost_tracker" in src
