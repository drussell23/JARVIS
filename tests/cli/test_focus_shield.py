"""A gate never interrupts a half-typed line.

The organism raises approval gates on its own schedule; the operator types on
theirs. Sooner or later a gate lands mid-sentence, and what happens next has
to be something the operator chose.

The hazard was NOT a stolen cursor — nothing in the cockpit calls `set_focus`,
so there was no focus to steal. It was on the daemon, and it was worse: every
attached line was offered to the pending gate BEFORE the REPL, and the gate
consumed whatever it got. These pin both halves of the fix.
"""
from __future__ import annotations

import asyncio
import contextlib
import io

import pytest

from backend.core.ouroboros.battle_test.focus_shield import (
    FocusShield, PendingPrompt, parse_prompt_frame,
)
from backend.core.ouroboros.battle_test.operator_prompt_bridge import (
    get_operator_prompt_bridge, is_bare_verdict, reset_bridge_for_tests,
)

with contextlib.redirect_stderr(io.StringIO()):
    from backend.core.ouroboros.cli import ov as _ov
    from backend.core.ouroboros.cli.ov import (
        AttachUI, _on_prompt_frame, _route_operator_line,
    )

_FRAME = {
    "type": "prompt", "prompt_id": "iron-gate:7759-86",
    "text": "apply candidate to test_runner.py",
    "risk": "APPROVAL_REQUIRED", "timeout_s": 300,
}


class _Client:
    def __init__(self) -> None:
        self.sent: list = []

    def send_input(self, text: str, prompt_id=None) -> bool:
        self.sent.append((text, prompt_id))
        return True

    def send_audio(self, _c: str) -> bool:
        return True


@pytest.fixture
def cockpit(monkeypatch: pytest.MonkeyPatch):
    """A cockpit whose buffer contents the test controls."""
    ui = AttachUI()
    shown: list = []
    ui.markup_sink = lambda text, addressed=False: shown.append(text)
    state = {"buffer": ""}
    monkeypatch.setattr(_ov, "_buffer_text", lambda: state["buffer"])
    return ui, _Client(), shown, state


# --------------------------------------------------------------------------
# the mandate: arriving mid-sentence defers, emptying the buffer releases
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_gate_arriving_mid_sentence_does_not_take_the_screen(
    cockpit,
) -> None:
    ui, _client, shown, state = cockpit
    state["buffer"] = "go fix the flaky "

    _on_prompt_frame(ui, _FRAME)
    await asyncio.sleep(0)

    assert shown == [], "the gate seized the screen mid-sentence"
    assert ui.shield.pending_count == 1
    assert ui.shield.showing is None


@pytest.mark.asyncio
async def test_the_operator_is_TOLD_a_gate_is_waiting(cockpit) -> None:
    """Deferring silently would be worse than interrupting: the organism is
    blocked and the operator has no way to know."""
    ui, _client, _shown, state = cockpit
    state["buffer"] = "typing"
    _on_prompt_frame(ui, _FRAME)
    await asyncio.sleep(0)

    badge = ui.shield.badge()
    assert "1 pending approval" in badge
    assert "ctrl-p" in badge
    assert badge in ui.toolbar() or badge in str(ui._key_hints())


@pytest.mark.asyncio
async def test_emptying_the_buffer_releases_the_gate(cockpit) -> None:
    """The half the operator never has to learn."""
    ui, client, shown, state = cockpit
    state["buffer"] = "go fix the flaky "
    _on_prompt_frame(ui, _FRAME)
    await asyncio.sleep(0)
    assert shown == []

    _route_operator_line(client, ui, "go fix the flaky test in test_runner")
    await asyncio.sleep(0)

    assert ui.shield.showing is not None
    assert any("Iron Gate" in line for line in shown)
    assert ui.shield.badge() == "", "badge outlived the queue"


@pytest.mark.asyncio
async def test_an_idle_operator_is_not_made_to_press_a_key(cockpit) -> None:
    """With an empty buffer there is nothing to protect — deferring then
    would be ceremony, not safety."""
    ui, _client, shown, state = cockpit
    state["buffer"] = ""
    _on_prompt_frame(ui, _FRAME)
    await asyncio.sleep(0)

    assert ui.shield.showing is not None
    assert any("Iron Gate" in line for line in shown)


# --------------------------------------------------------------------------
# the keystroke race — the reason any of this exists
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_typed_GOAL_never_becomes_a_verdict(cockpit) -> None:
    """THE bug. `go fix the flaky test` approved an unrelated op — its first
    token is "go" — and the goal never reached the REPL at all."""
    ui, client, _shown, state = cockpit
    state["buffer"] = "go fix the flaky test"
    _on_prompt_frame(ui, _FRAME)
    await asyncio.sleep(0)

    _route_operator_line(client, ui, "go fix the flaky test in test_runner")

    text, prompt_id = client.sent[-1]
    assert prompt_id is None, "a goal was submitted as an approval"
    assert text == "go fix the flaky test in test_runner"


@pytest.mark.asyncio
async def test_the_daemon_declines_a_goal_and_lets_it_reach_the_repl() -> None:
    """The daemon half, at the seam `harness._on_input` calls."""
    reset_bridge_for_tests()
    bridge = get_operator_prompt_bridge()
    bridge.begin("iron-gate:7759-86")

    for goal in ("go fix the flaky test", "stop the doc_staleness storm",
                 "let's build the streaming next"):
        assert bridge.resolve(goal) is False, f"{goal!r} was eaten by a gate"
    assert bridge.waiting is True, "the gate should still be open"


@pytest.mark.asyncio
async def test_a_bare_verdict_still_answers() -> None:
    reset_bridge_for_tests()
    bridge = get_operator_prompt_bridge()
    fut = bridge.begin("iron-gate:7759-86")
    assert bridge.resolve("y") is True
    await asyncio.sleep(0)
    assert fut.result() == "y"


@pytest.mark.asyncio
async def test_an_answer_carries_the_gate_it_was_written_for(
    cockpit,
) -> None:
    ui, client, _shown, state = cockpit
    state["buffer"] = ""
    _on_prompt_frame(ui, _FRAME)
    await asyncio.sleep(0)

    _route_operator_line(client, ui, "y")

    assert client.sent[-1] == ("y", "iron-gate:7759-86")
    assert ui.shield.showing is None


@pytest.mark.asyncio
async def test_a_deferred_answer_cannot_land_on_a_DIFFERENT_op() -> None:
    """Deferral is exactly the window in which the slot moves on. Approving
    the wrong op is the worst outcome this system can produce."""
    reset_bridge_for_tests()
    bridge = get_operator_prompt_bridge()
    bridge.begin("iron-gate:AAAA")
    bridge.begin("iron-gate:BBBB")          # supersedes while queued

    assert bridge.resolve("y", prompt_id="iron-gate:AAAA") is False
    assert bridge.resolve("y", prompt_id="iron-gate:BBBB") is True


@pytest.mark.asyncio
async def test_a_non_verdict_reply_to_a_SHOWN_gate_still_reaches_the_repl(
    cockpit,
) -> None:
    """"later, first fix the tests" is a goal, not a decision."""
    ui, client, _shown, state = cockpit
    state["buffer"] = ""
    _on_prompt_frame(ui, _FRAME)
    await asyncio.sleep(0)

    _route_operator_line(client, ui, "later, first fix the tests")

    assert client.sent[-1][1] is None
    assert ui.shield.showing is not None, "the gate was dismissed unanswered"


# --------------------------------------------------------------------------
# staleness — deferral makes it possible
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_an_expired_gate_is_never_offered() -> None:
    """Presenting a dead gate would let the operator believe they approved
    something they did not."""
    clock = {"t": 1000.0}
    shield = FocusShield(clock=lambda: clock["t"])
    shield.offer(dict(_FRAME, timeout_s=30), composing=True)
    assert shield.pending_count == 1

    clock["t"] += 31
    assert shield.pending_count == 0, "an expired gate was still advertised"
    assert shield.pop() is None
    assert shield.dropped_expired == 1


@pytest.mark.asyncio
async def test_a_gate_with_no_declared_deadline_never_expires() -> None:
    """Inventing an expiry the organism never agreed to would drop a gate it
    is still waiting on."""
    clock = {"t": 0.0}
    shield = FocusShield(clock=lambda: clock["t"])
    shield.offer(dict(_FRAME, timeout_s=0), composing=True)
    clock["t"] += 100_000
    assert shield.pending_count == 1


@pytest.mark.asyncio
async def test_the_daemon_can_withdraw_a_gate(cockpit) -> None:
    """Answered elsewhere, expired, or superseded — `prompt_resolved` purges
    it immediately rather than waiting for a deadline to notice."""
    ui, _client, _shown, state = cockpit
    state["buffer"] = "typing"
    _on_prompt_frame(ui, _FRAME)
    await asyncio.sleep(0)
    assert ui.shield.pending_count == 1

    ui.shield.dismiss("iron-gate:7759-86")
    assert ui.shield.pending_count == 0
    assert ui.shield.badge() == ""


# --------------------------------------------------------------------------
# queue behaviour
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_re_arming_the_same_gate_does_not_stack_it() -> None:
    """The bridge re-arms whenever a line turns out not to be a verdict; one
    unanswered gate must not become a queue of itself."""
    shield = FocusShield()
    for _ in range(5):
        shield.offer(_FRAME, composing=True)
    assert shield.pending_count == 1


@pytest.mark.asyncio
async def test_a_second_gate_waits_behind_the_one_on_screen() -> None:
    shield = FocusShield()
    shield.offer(_FRAME, composing=False)
    shield.offer(dict(_FRAME, prompt_id="iron-gate:BBBB"), composing=False)
    assert shield.showing.prompt_id == "iron-gate:7759-86"
    assert shield.pending_count == 1
    assert shield.pop() is None, "two gates were on screen at once"

    shield.dismiss("iron-gate:7759-86")
    assert shield.pop().prompt_id == "iron-gate:BBBB"


@pytest.mark.asyncio
async def test_the_queue_is_bounded() -> None:
    """A cockpit is not a ticket queue."""
    shield = FocusShield(max_pending=3)
    for i in range(10):
        shield.offer(dict(_FRAME, prompt_id=f"gate-{i}"), composing=True)
    assert shield.pending_count == 3
    assert shield.dropped_overflow > 0


# --------------------------------------------------------------------------
# robustness
# --------------------------------------------------------------------------

@pytest.mark.parametrize("junk", [None, {}, {"type": "prompt"}, "string", 42])
def test_a_malformed_frame_is_refused_not_crashed(junk) -> None:
    """An unanswerable prompt — no id — has nothing for the bridge to match,
    so it is refused rather than shown."""
    assert parse_prompt_frame(junk) is None
    assert FocusShield().offer(junk, composing=False) == ""


def test_a_raising_render_sink_does_not_break_the_cockpit() -> None:
    def _boom(_p):
        raise RuntimeError("render died")

    shield = FocusShield(show=_boom)
    assert shield.offer(_FRAME, composing=False) == "shown"


def test_the_op_ref_is_the_TAIL() -> None:
    """UUIDv7 is time-ordered: ops from one session share their leading bytes
    and differ only at the end."""
    assert PendingPrompt("iron-gate:0198e4c1-7f2a-7d31-9f44-7759086").ref \
        .endswith("7759086")


def test_the_kill_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Off, a gate goes straight to the screen exactly as before."""
    monkeypatch.setenv("JARVIS_FOCUS_SHIELD_ENABLED", "0")
    shield = FocusShield()
    assert shield.offer(_FRAME, composing=True) == "shown"


def test_the_verdict_vocabulary_is_shared_not_copied() -> None:
    """One definition of what "yes" means, or the surfaces drift."""
    assert is_bare_verdict("y") is True
    assert is_bare_verdict("no") is False
    assert is_bare_verdict("go fix the tests") is None
