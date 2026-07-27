"""Verb output reaches whoever asked for it, without stalling the daemon.

The daemon runs detached. Its ~76 `dispatch_<verb>_command` handlers print to
`console`, which renders on a terminal nobody watches — so an operator typing
`/posture status` in an attached cockpit saw the verb dispatch, succeed, and
produce nothing. Talking to an empty room.

The handlers are untouched. The console is what changed.
"""
from __future__ import annotations

import asyncio
import io
import time
from typing import Any, List, Optional, Tuple

import pytest

from backend.core.ouroboros.battle_test.attach_session import session_scope
from backend.core.ouroboros.battle_test.spooled_console import (
    ConsoleSpooler,
    make_spooled_console,
)


class _Base:
    file = None
    is_terminal = False


def _console(sink) -> Tuple[Any, ConsoleSpooler]:
    base = _Base()
    base.file = io.StringIO()
    console, spooler = make_spooled_console(sink, base=base)
    return console, spooler


# --------------------------------------------------------------------------
# 1. it mirrors, and it addresses
# --------------------------------------------------------------------------

async def test_verb_output_reaches_the_session_that_ran_it() -> None:
    """MANDATE 4(1): the payload lands on the right session's queue."""
    seen: List[Tuple[Optional[str], str]] = []
    console, spooler = _console(lambda s, t: seen.append((s, t)))
    spooler.start()
    with session_scope("sess-A"):
        console.print("posture = CONSOLIDATE")
    await spooler.flush()
    await spooler.stop()
    assert seen == [("sess-A", "posture = CONSOLIDATE")]


async def test_ambient_output_is_broadcast_not_addressed() -> None:
    """An autonomous operation belongs to no one and is everyone's business."""
    seen: List[Tuple[Optional[str], str]] = []
    console, spooler = _console(lambda s, t: seen.append((s, t)))
    spooler.start()
    console.print("op-7 GENERATE")                 # no session scope
    await spooler.flush()
    await spooler.stop()
    assert seen == [(None, "op-7 GENERATE")]


async def test_two_sessions_do_not_cross_talk() -> None:
    """The reason addressing exists: `/posture` in one terminal must not
    paint in another."""
    seen: List[Tuple[Optional[str], str]] = []
    console, spooler = _console(lambda s, t: seen.append((s, t)))
    spooler.start()
    with session_scope("A"):
        console.print("for A")
    with session_scope("B"):
        console.print("for B")
    await spooler.flush()
    await spooler.stop()
    assert seen == [("A", "for A"), ("B", "for B")]


async def test_the_session_is_read_on_the_calling_task() -> None:
    """Reading the ContextVar in the DRAIN task would give the drainer's
    context, which belongs to nobody."""
    seen: List[Tuple[Optional[str], str]] = []
    console, spooler = _console(lambda s, t: seen.append((s, t)))
    with session_scope("sess-Z"):
        console.print("captured at print time")
    spooler.start()                                # drain starts OUTSIDE
    await spooler.flush()
    await spooler.stop()
    assert seen and seen[0][0] == "sess-Z"


async def test_the_local_render_is_untouched() -> None:
    """Additive. A daemon in the foreground must look exactly as before."""
    base = _Base()
    base.file = io.StringIO()
    console, spooler = make_spooled_console(lambda _s, _t: None, base=base)
    spooler.start()
    console.print("still on my own terminal")
    await spooler.flush()
    await spooler.stop()
    assert "still on my own terminal" in base.file.getvalue()


async def test_rich_markup_survives_to_the_client() -> None:
    seen: List[Tuple[Optional[str], str]] = []
    console, spooler = _console(lambda s, t: seen.append((s, t)))
    spooler.start()
    console.print("[bold green]healthy[/] · ouroboros")
    await spooler.flush()
    await spooler.stop()
    assert "healthy" in seen[0][1] and "ouroboros" in seen[0][1]


# --------------------------------------------------------------------------
# 2. it never stalls the daemon
# --------------------------------------------------------------------------

async def test_print_returns_instantly_against_a_blocked_sink() -> None:
    """MANDATE 4(2). `Console.print` is synchronous and runs ON the event
    loop; doing UDS I/O there would put every operation behind the slowest
    attached client."""
    async def _glacial(_s, _t):
        await asyncio.sleep(30)

    console, spooler = _console(_glacial)
    spooler.start()
    started = time.perf_counter()
    for i in range(50):
        console.print(f"line {i}")
    elapsed = time.perf_counter() - started
    await spooler.stop()
    assert elapsed < 0.5, (
        f"50 prints took {elapsed:.2f}s against a blocked sink — print is "
        f"doing network I/O on the event loop"
    )


async def test_a_wedged_client_cannot_exhaust_the_daemon() -> None:
    """Bounded queue, drop-oldest. A detached or hung cockpit must not become
    a memory leak in the organism."""
    spooler = ConsoleSpooler(lambda _s, _t: None, maxsize=8)
    for i in range(200):
        spooler.offer(None, f"line {i}")
    assert spooler.pending <= 8
    assert spooler.dropped > 0


async def test_the_newest_lines_survive_a_drop() -> None:
    """Drop-OLDEST: when a cockpit falls behind, the recent lines are the ones
    the operator is waiting for."""
    seen: List[str] = []
    spooler = ConsoleSpooler(lambda _s, t: seen.append(t), maxsize=4)
    for i in range(20):
        spooler.offer(None, f"line {i}")
    spooler.start()
    await spooler.flush()
    await spooler.stop()
    assert "line 19" in seen, f"the most recent line was dropped: {seen}"


async def test_a_failing_sink_does_not_kill_the_drain() -> None:
    """One bad frame is not fatal — the next verb must still be seen."""
    calls: List[str] = []

    def _flaky(_s, text):
        calls.append(text)
        if text == "boom":
            raise RuntimeError("client went away")

    spooler = ConsoleSpooler(_flaky)
    spooler.start()
    spooler.offer(None, "boom")
    spooler.offer(None, "after")
    await spooler.flush()
    await spooler.stop()
    assert "after" in calls


def test_printing_without_a_running_loop_never_raises() -> None:
    """Verbs run in contexts a test or a boot path may not have looped yet."""
    console, spooler = _console(lambda _s, _t: None)
    assert spooler.start() is False           # no loop
    console.print("boot-time line")           # must not raise
    assert spooler.pending >= 1               # and is not lost


async def test_a_print_that_renders_badly_cannot_break_a_verb() -> None:
    class _Hostile:
        def __rich__(self) -> Any:
            raise RuntimeError("renderable exploded")

        def __str__(self) -> str:
            return "hostile"

    console, spooler = _console(lambda _s, _t: None)
    spooler.start()
    console.print(_Hostile())                 # must not raise
    await spooler.stop()


# --------------------------------------------------------------------------
# 3. wiring
# --------------------------------------------------------------------------

def test_no_verb_handler_was_modified() -> None:
    """The whole point: the fix is one console, not 76 edits.

    Asserted as "the change touches no *_repl module", which is the actual
    invariant. An earlier version of this test picked `moltbook_repl` as an
    exemplar of a handler that prints — it does not; it RETURNS a
    MoltDispatchResult and its caller renders. Handlers vary in shape, so
    asserting on one of them tests the exemplar rather than the rule.
    """
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    import ast

    src = (repo / "backend/core/ouroboros/battle_test/"
           "spooled_console.py").read_text()
    # AST, not substring: the module's own DOCSTRING names
    # `dispatch_<verb>_command` while explaining what it deliberately does not
    # touch, and a text search cannot tell prose from code. (Use-vs-mention —
    # the same trap that has now caught me three times in this codebase.)
    imported = {
        (node.module or "")
        for node in ast.walk(ast.parse(src))
        if isinstance(node, ast.ImportFrom)
    } | {
        alias.name
        for node in ast.walk(ast.parse(src))
        if isinstance(node, ast.Import) for alias in node.names
    }
    offenders = [m for m in imported if "_repl" in m or "dispatch" in m]
    assert offenders == [], (
        f"the console layer imports verb modules — it must not know they "
        f"exist: {offenders}"
    )


def test_handlers_that_RETURN_text_are_covered_too() -> None:
    """Not every verb prints. moltbook returns text its caller renders — so
    coverage depends on that caller going through the swapped console, which
    is why the swap is at `sf.console` rather than at any handler."""
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    src = (repo / "backend/core/ouroboros/battle_test/harness.py").read_text()
    assert "sf.console = spooled" in src, (
        "the swap must be on the SerpentFlow console every renderer uses"
    )


def test_the_bridge_can_address_a_single_cockpit() -> None:
    import inspect

    from backend.core.ouroboros.battle_test.cockpit_attach import (
        CockpitAttachBridge,
    )
    params = inspect.signature(CockpitAttachBridge.publish_markup).parameters
    assert "session" in params


def test_the_harness_swaps_the_console_and_starts_the_drain() -> None:
    """Structural: an unstarted spooler queues forever and shows nothing —
    the wired-but-inert failure this session kept finding."""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[2]
           / "backend/core/ouroboros/battle_test/harness.py").read_text()
    assert "make_spooled_console(" in src
    assert "sf.console = spooled" in src
    assert "spooler.start()" in src


def test_the_harness_probes_for_addressing_rather_than_assuming() -> None:
    """Addressing arrived in #70113; an older bridge would raise on an
    unexpected kwarg and take ALL output down with it."""
    from backend.core.ouroboros.battle_test.harness import _accepts_session

    assert _accepts_session(lambda text, *, session=None: None) is True
    assert _accepts_session(lambda text: None) is False
    assert _accepts_session(None) is False
