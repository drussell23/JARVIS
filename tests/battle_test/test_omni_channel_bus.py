"""Omni-Channel Broadcast Bus — session-addressed verb output.

The defect: 59 auto-discovered REPL verbs executed correctly in the daemon and
rendered to its LOCAL console (``serpent_flow.py`` printed
``_outcome.text`` straight to ``self._flow.console``). An attached ``ov``
cockpit sent the command, the daemon ran it, and the operator saw nothing.

Mirroring is one line. Mirroring CORRECTLY is not: the moment two cockpits are
attached, a broadcast puts terminal A's ``/moltbook`` into terminal B's
scrollback. These tests pin the routing that prevents that, the ambient path
that must still reach everyone, and the alias unmasking that recovered
``/molt``.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List

import pytest

from backend.core.ouroboros.battle_test import attach_session
from backend.core.ouroboros.battle_test.cockpit_attach import (
    CockpitAttachBridge,
)


class _Writer:
    """A StreamWriter stand-in that records what it was sent."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.frames: List[Dict[str, Any]] = []
        self.closed = False

    def is_closing(self) -> bool:
        return self.closed

    def write(self, data: bytes) -> None:
        for line in data.decode().splitlines():
            if line.strip():
                self.frames.append(json.loads(line))

    def close(self) -> None:
        self.closed = True

    def texts(self) -> List[str]:
        return [f.get("text", "") for f in self.frames]


@pytest.fixture(autouse=True)
def _clean_context():
    """Session state is a ContextVar — a leak would cross-contaminate tests."""
    with attach_session.session_scope(None):
        yield


def _bridge_with(*writers: _Writer) -> CockpitAttachBridge:
    """publish_markup dispatches through the bridge's own loop, so the tests
    that exercise it must run inside one and hand it over."""
    b = CockpitAttachBridge()
    try:
        b._loop = asyncio.get_running_loop()   # type: ignore[attr-defined]
    except RuntimeError:
        pass
    for w in writers:
        b._clients.add(w)          # type: ignore[arg-type]
        b.bind_session(f"sess-{w.name}", w)  # type: ignore[arg-type]
    return b


# --------------------------------------------------------------------------
# 1. session-addressed routing — the requirement
# --------------------------------------------------------------------------

async def test_verb_output_reaches_only_the_cockpit_that_asked() -> None:
    """Terminal A types /moltbook. Terminal B must not see the agora."""
    a, b = _Writer("A"), _Writer("B")
    bridge = _bridge_with(a, b)

    with attach_session.session_scope("sess-A"):
        bridge.publish_markup("[bold]🐍 Moltbook[/bold] [dim]— 12 posts[/dim]")

    assert len(a.frames) == 1, "the asking cockpit received nothing"
    assert "Moltbook" in a.texts()[0]
    assert b.frames == [], "terminal B received terminal A's verb output"


async def test_ambient_output_still_reaches_every_cockpit() -> None:
    """Op chrome, breadcrumbs, receipts and moltbook posts are the organism
    speaking on its own initiative — situational awareness for all."""
    a, b = _Writer("A"), _Writer("B")
    bridge = _bridge_with(a, b)

    bridge.publish_markup("[bold]⏺ Bash[/bold]")          # no session scope

    assert len(a.frames) == 1
    assert len(b.frames) == 1


async def test_rich_markup_survives_the_wire_verbatim() -> None:
    """Fidelity is preserved by transporting MARKUP, not by rendering to ANSI.

    The markup channel is width-agnostic by contract: the raw markup travels
    and each client fits it to its own canvas. Rendering to ANSI at the daemon
    would bake in this terminal's width and colour depth."""
    a = _Writer("A")
    bridge = _bridge_with(a)
    payload = (
        "  [bold]⏺ 🐍 @cassandra[/bold] [dim]· 1d ago · distress[/dim]\n"
        "  [dim]⎿[/dim]  the soak refused to launch"
    )
    with attach_session.session_scope("sess-A"):
        bridge.publish_markup(payload)

    assert a.frames[0]["type"] == "markup"
    assert a.frames[0]["text"] == payload, "markup was altered in transit"


async def test_addressed_output_for_a_departed_cockpit_is_dropped() -> None:
    """NOT broadcast as a fallback. A reconnecting operator's private output
    appearing in someone else's scrollback is the exact failure this routing
    exists to prevent — arriving through its own error path."""
    a, b = _Writer("A"), _Writer("B")
    bridge = _bridge_with(a, b)
    bridge._drop(a)                # type: ignore[arg-type]

    with attach_session.session_scope("sess-A"):
        bridge.publish_markup("private answer")

    assert b.frames == [], "a departed cockpit's output leaked to another"
    assert bridge.stats.get("addressed_undeliverable") == 1


async def test_routing_can_be_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """OFF restores pure broadcast — the only honest A/B for this change."""
    monkeypatch.setenv("JARVIS_ATTACH_SESSION_ROUTING", "0")
    a, b = _Writer("A"), _Writer("B")
    bridge = _bridge_with(a, b)

    with attach_session.session_scope("sess-A"):
        bridge.publish_markup("everyone sees this")

    assert len(a.frames) == 1 and len(b.frames) == 1


def test_session_scope_restores_on_exception() -> None:
    """A verb that raises must not leave later ambient output addressed."""
    with pytest.raises(RuntimeError):
        with attach_session.session_scope("sess-A"):
            raise RuntimeError("verb exploded")
    assert attach_session.current_session() is None


async def test_session_propagates_across_await_boundaries() -> None:
    """The whole reason for a ContextVar: the publish call is ~15 frames and
    several awaits below the dispatch site."""
    seen = {}

    async def deep() -> None:
        await asyncio.sleep(0)
        seen["sid"] = attach_session.current_session()

    async def middle() -> None:
        await deep()

    with attach_session.session_scope("sess-A"):
        await middle()
    assert seen["sid"] == "sess-A"


async def test_concurrent_sessions_do_not_bleed() -> None:
    """Two cockpits dispatching at once must not see each other's answers."""
    a, b = _Writer("A"), _Writer("B")
    bridge = _bridge_with(a, b)

    async def run(sid: str, text: str) -> None:
        with attach_session.session_scope(sid):
            await asyncio.sleep(0)
            bridge.publish_markup(text)

    await asyncio.gather(run("sess-A", "for-A"), run("sess-B", "for-B"))

    assert a.texts() == ["for-A"]
    assert b.texts() == ["for-B"]


# --------------------------------------------------------------------------
# 1b. sink arity — decided by inspection, never by trial call
# --------------------------------------------------------------------------

def test_a_legacy_one_arg_sink_is_detected_not_probed() -> None:
    """Sinks predating session routing take one argument and must keep
    working — this bridge is the only path an attached cockpit has to the
    REPL, so guessing wrong drops the operator's command."""
    from backend.core.ouroboros.battle_test.cockpit_attach import (
        _accepts_two_positional,
    )
    assert _accepts_two_positional(lambda text: None) is False
    assert _accepts_two_positional(lambda text, session=None: None) is True
    assert _accepts_two_positional(lambda *a: None) is True


def test_a_raising_sink_is_never_invoked_twice() -> None:
    """The hazard in a retry-on-TypeError compatibility shim.

    ``except TypeError: retry`` cannot distinguish 'wrong arity' from 'the
    sink raised TypeError while doing its job'. In the second case the retry
    executes the operator's command a SECOND time — a dispatch surface must
    never guess by re-running the thing it is dispatching."""
    from backend.core.ouroboros.battle_test.cockpit_attach import (
        CockpitAttachBridge,
    )
    calls: List[str] = []

    def _sink(text: str) -> None:            # one-arg AND raises TypeError
        calls.append(text)
        raise TypeError("sink's own problem")

    bridge = CockpitAttachBridge(on_input=_sink)
    assert bridge._input_takes_session is False
    try:
        bridge._on_input("do-the-thing")
    except TypeError:
        pass
    assert calls == ["do-the-thing"], "the command was executed twice"


# --------------------------------------------------------------------------
# 2. Pub/Sub — posts arrive without being asked for
# --------------------------------------------------------------------------

async def test_a_new_post_reaches_subscribers_unprompted() -> None:
    """The agora is proactive. A society you only see by typing /moltbook is
    an archive."""
    from backend.core.ouroboros.governance import moltbook

    received: List[Any] = []
    unsub = moltbook.subscribe_molts(received.append)
    try:
        moltbook._notify_subscribers({"body": "unprompted"})
        assert received == [{"body": "unprompted"}]
    finally:
        unsub()

    moltbook._notify_subscribers({"body": "after unsubscribe"})
    assert len(received) == 1, "unsubscribe did not take effect"


async def test_one_bad_subscriber_cannot_starve_the_others() -> None:
    from backend.core.ouroboros.governance import moltbook

    good: List[Any] = []

    def _boom(_post: Any) -> None:
        raise RuntimeError("sink on fire")

    unsub_bad = moltbook.subscribe_molts(_boom)
    unsub_good = moltbook.subscribe_molts(good.append)
    try:
        moltbook._notify_subscribers({"body": "x"})   # must not raise
        assert good == [{"body": "x"}]
    finally:
        unsub_bad()
        unsub_good()


def test_a_live_post_renders_as_an_escaped_block() -> None:
    """Body text is MODEL-authored. It must arrive as inert data inside our
    chrome, never as markup the model chose."""
    from backend.core.ouroboros.battle_test.harness import BattleTestHarness

    class _Post:
        handle = "@cassandra"
        glyph = "🐍"
        kind = "distress"
        body = "I warned you [bold]about this[/bold]"

    line = BattleTestHarness._render_molt_line(  # type: ignore[arg-type]
        object.__new__(BattleTestHarness), _Post(),
    )
    assert "@cassandra" in line
    assert "⏺" in line and "⎿" in line
    assert "\\[bold]" in line or "[bold]about this[/bold]" not in line


# --------------------------------------------------------------------------
# 3. alias unmasking — /molt had no caller
# --------------------------------------------------------------------------

def test_molt_is_registered_and_distinct_from_moltbook() -> None:
    """Discovery keys on the module BASENAME, so `moltbook_repl` could only
    ever expose `moltbook` — `dispatch_molt_command`, defined right beside it,
    was unreachable. Not an omission: the naming cage masked it."""
    from backend.core.ouroboros.battle_test import repl_dispatch_registry as r

    r.reset_registry_for_tests()
    report = r.prime_registry()
    assert "moltbook" in report.verbs
    assert "molt" in report.verbs, "/molt is still masked by the basename cage"

    from backend.core.ouroboros.governance import moltbook_repl
    assert "molt" in getattr(moltbook_repl, "__aliases__", ())


def test_aliases_bind_to_their_own_dispatcher_when_one_exists() -> None:
    """`/molt` posts and `/moltbook` reads — two verbs, not a synonym."""
    from backend.core.ouroboros.battle_test import repl_dispatch_registry as r
    from backend.core.ouroboros.governance import moltbook_repl

    r.reset_registry_for_tests()
    r.prime_registry()
    reg = r._VERB_TO_DISPATCHER
    assert reg["molt"] is moltbook_repl.dispatch_molt_command
    assert reg["moltbook"] is moltbook_repl.dispatch_moltbook_command


def test_a_string_aliases_declaration_is_ignored() -> None:
    """`__aliases__ = "molt"` would otherwise register one verb per letter."""
    from backend.core.ouroboros.battle_test import repl_dispatch_registry as r

    r.reset_registry_for_tests()
    report = r.prime_registry()
    for junk in ("m", "o", "l", "t"):
        assert junk not in report.verbs
