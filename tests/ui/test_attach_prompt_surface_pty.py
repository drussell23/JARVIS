"""The `PromptSession` attach surface, pressed under real output pressure.

WHY THIS FILE EXISTS SEPARATELY FROM test_cockpit_pty_proof
-----------------------------------------------------------
`ov` has TWO interactive surfaces and they have been diverging for an entire
arc:

  * `build_bipartite_application` — a full-screen prompt_toolkit Application.
    `bipartite_layout` documents itself as the ALTERNATIVE to `patch_stdout`,
    "strictly stronger: because prompt_toolkit owns the screen".
  * `PromptSession` + `patch_stdout(raw=True)` (`ov.py`) — the print-above-
    prompt model, where every background line is written by SUSPENDING the
    application through `run_in_terminal`.

`test_cockpit_pty_proof` drives `ov demo live`, which boots the first one. Its
`test_the_slash_palette_opens_without_freezing` therefore exercises the surface
that contains no `run_in_terminal` at all — while the standing freeze report,
and its leading hypothesis (`run_in_terminal` contention under daemon load),
belong entirely to the second.

That test cannot reproduce the freeze by construction. This file covers the
surface where the bug is hypothesised to live, under the condition it is
hypothesised to need.

WHAT A NEGATIVE RESULT MEANS HERE
---------------------------------
If the escalation never wedges the cockpit, that is not proof the freeze does
not exist — it bounds it. The rate reached and the fact that liveness survived
it are recorded in the failure message either way, so a future occurrence can
be compared against a measurement instead of an impression.
"""

from __future__ import annotations

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from attach_pressure import (  # noqa: E402
    PressureBridge,
    escalate_until_unresponsive,
)
from pty_console import PtyProcess, clean  # noqa: E402

pytestmark = pytest.mark.timeout(300)

#: The prompt this surface draws when it is ready for input.
#:
#: `ov ›`, NOT the `❯` the full-screen cockpit uses. The first run of this file
#: waited for `❯` and skipped five times with the transcript showing a perfectly
#: healthy `ov ›` prompt — which is incidental confirmation of the premise: the
#: two surfaces do not even share a prompt glyph, so a test written against one
#: cannot be assumed to cover the other.
PROMPT = "ov ›"

#: Where escalation begins. Deliberately below anything suspicious: the memory
#: on this bug records 4 Hz as "likely too gentle", and starting at the last
#: known-innocent rate is how a ramp proves it climbed past it.
START_HZ = 8.0

#: How long a storm runs before liveness is probed. Long enough for the emitter
#: to reach steady state and for the client's reader to be genuinely behind;
#: short enough that twelve doublings stay inside the suite's budget.
SETTLE_S = 1.5


def _attach(socket_path, *, extra_env=None) -> PtyProcess:
    env = {
        # Point the client at OUR bridge, not at whatever the operator has
        # running. The bridge resolves this same variable.
        "JARVIS_ATTACH_IPC_SOCKET": str(socket_path),
        # THE point of this file: force the PromptSession/patch_stdout surface
        # rather than the full-screen Application.
        "JARVIS_BIPARTITE_LAYOUT_DISABLED": "1",
    }
    env.update(extra_env or {})
    return PtyProcess(
        [sys.executable, "-m", "backend.core.ouroboros.cli.ov", "attach"],
        env=env,
    )


@pytest.fixture()
def attached():
    """A real bridge and a real `ov attach` on the prompt surface."""
    from tests.pty_gate import require_pty
    require_pty("test_attach_prompt_surface_pty")

    # The bridge allocates its own socket under a root short enough to bind:
    # pytest's tmp_path is already past the AF_UNIX address limit.
    with PressureBridge() as bridge:
        session = _attach(bridge.socket_path)
        try:
            if not session.wait_for(PROMPT, timeout=45.0):
                pytest.skip(
                    "attach never reached a prompt: "
                    f"{clean(session.output)[-500:]!r}"
                )
            yield bridge, session
        finally:
            session.close()


# ---------------------------------------------------------------------------
# preconditions — nothing below means anything without these
# ---------------------------------------------------------------------------

def test_the_prompt_surface_attaches_to_a_real_bridge(attached) -> None:
    """The baseline. A dead terminal passes every liveness test trivially."""
    _bridge, session = attached
    assert session.proc.poll() is None, "attach exited during handshake"
    assert PROMPT in clean(session.output)


def test_typing_reaches_the_prompt_surface(attached) -> None:
    _bridge, session = attached
    at = session.mark()
    session.send(b"hello")
    assert session.wait_for("hello", timeout=10.0, after=at), (
        "typed text never echoed — the surface is not accepting input"
    )


def test_daemon_frames_reach_the_attached_terminal(attached) -> None:
    """The mirror, proven on this surface.

    Also the precondition for every pressure assertion below: a storm that
    never arrives is not a storm, and a liveness test under one would be
    measuring an idle terminal.
    """
    bridge, session = attached
    at = session.mark()
    bridge.publish("[dim]⎿[/] canary7391 daemon frame")
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        if "canary7391" in clean(session.since(at)):
            return
        time.sleep(0.05)
    pytest.fail(
        "a published markup frame never reached the attached terminal; "
        f"tail={clean(session.since(at))[-300:]!r}"
    )


def test_operator_input_crosses_the_bridge(attached) -> None:
    """Settles a separate open question on the same instrument.

    "`ov` verbs produce no output" was never resolved because nobody could show
    that a typed line reaches the daemon at all. The bridge records what its
    `on_input` receives, so this is decidable rather than inferred.
    """
    bridge, session = attached
    session.send(b"/status\r")
    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline:
        if any("status" in line for line in bridge.inputs):
            return
        time.sleep(0.05)
    pytest.fail(
        "the typed line never reached the bridge's on_input; "
        f"received={bridge.inputs!r}"
    )


# ---------------------------------------------------------------------------
# the freeze
# ---------------------------------------------------------------------------

def test_the_slash_palette_survives_an_output_storm(attached, record_property) -> None:
    """`/` under escalating daemon output — the reported freeze, targeted.

    Liveness is "does the NEXT keystroke still change the screen". A frozen
    application still echoes the `/` from the terminal's own line discipline
    and then goes quiet, so echoing `/` proves nothing and the following key
    proves everything.

    The rate escalates until the cockpit stops answering or the emitter
    saturates, and the outcome is reported with the rate attached either way.
    """
    bridge, session = attached

    rounds: "list[tuple[float, float, bool]]" = []

    def _probe() -> bool:
        # Fresh palette each round: an already-open one would leave the second
        # keystroke filtering a menu rather than opening it, which is a
        # different code path from the one under suspicion.
        session.send(b"\x15")          # Ctrl-U: clear the line
        time.sleep(0.15)
        session.send(b"/")
        time.sleep(0.4)
        at = session.mark()
        session.send(b"h")
        delta = session.wait_for_change(at, minimum=8, timeout=6.0)
        return len(delta) >= 8 and session.proc.poll() is None

    wedged_at = escalate_until_unresponsive(
        bridge, _probe,
        start_hz=START_HZ, settle_s=SETTLE_S,
        on_round=lambda hz, achieved, alive: rounds.append(
            (hz, achieved, alive)),
    )

    trace = "; ".join(
        f"{hz:.0f}Hz→{achieved:.0f}Hz {'alive' if alive else 'WEDGED'}"
        for hz, achieved, alive in rounds
    )
    # Recorded on the PASSING path too, not only on failure. A ramp that
    # reports nothing when it survives leaves the next person with the same
    # impression-based argument this instrument exists to replace: they need to
    # know how hard it pushed, not merely that it did.
    record_property("pressure_rounds", trace)
    record_property(
        "peak_achieved_hz",
        f"{max((a for _, a, _ in rounds), default=0.0):.1f}",
    )
    print(f"\n[storm] {trace}")
    assert wedged_at is None, (
        f"FREEZE REPRODUCED: the prompt surface stopped answering after `/` "
        f"under {wedged_at:.0f} Hz of daemon output. Rounds: {trace}"
    )
    assert session.proc.poll() is None, f"attach died during the ramp: {trace}"
    # A ramp that never actually applied pressure would pass this test while
    # proving nothing.
    assert rounds, "the escalation never ran a single round"
    assert max(a for _, a, _ in rounds) > START_HZ, (
        f"the emitter never exceeded its starting rate — no pressure was "
        f"applied, so liveness here is meaningless. Rounds: {trace}"
    )
