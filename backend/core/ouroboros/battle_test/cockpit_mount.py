"""What a cockpit mount CONSISTS of — assembled once, for the surface asking.

`ov` has three surfaces that render, and all three hand-assemble their own
argument list for `build_bipartite_application`. So they drift, and the drift is
invisible: nothing compares them, so a hook added for one surface is simply
absent on the others until an operator notices a feature they were promised.

`serpent_flow` already says this out loud, about the last time it happened:

    The daemon-side cockpit is a surface the operator TYPES into, and it ran
    with zero completion, zero persistent history and zero ghost-text while the
    attach cockpit had all three wired at ov.py — the two-surfaces split, again.

That was fixed for three hooks by `repl_completion.build_completion_wiring` — one
factory, both surfaces, "so the vocabularies cannot diverge". Eleven more hooks
were still in the same state, and this module is that same fix generalised rather
than a second mechanism beside it.

The daemon was blind to state it PRODUCES
=========================================
The worst of it is not that the daemon cockpit was missing decoration. It is the
direction of the blindness:

    `pending_apply`   the daemon calls `note_pending` / `clear_pending`. It is
                      the source of the NOTIFY_APPLY countdown, and it never
                      mounted the strip that draws it.
    `panic_arbiter`   the daemon calls `arbitrate` from its own event-loop
                      exception handler. It is where the crash HAPPENS, and the
                      FATAL overlay only ever rendered on a remote client.

An operator sitting at the daemon's own terminal could not see a gate the daemon
was running or a task the daemon had just lost. The state was produced locally
and legible only from somewhere else.

Same renderer, different source
===============================
The rule this module is built on is already stated at `_local_agent_rows`:

    Same renderer as the remote cockpit, different source, which is the entire
    reason `render_roster` takes a snapshot rather than a roster: neither
    surface can drift into its own look.

So every provider here resolves an IN-PROCESS snapshot and hands it to the SHARED
renderer the attach client already calls. Nothing is drawn twice, and a
regression in a strip shows up on both surfaces at once instead of on whichever
one happened to keep its own copy.

The asymmetry that stays
------------------------
The attach client gates its strips on heartbeat STALENESS — a dead daemon must
not leave a countdown ticking toward an apply that will never happen. The daemon
has no such concept and must not grow one: it IS the source, so there is no
transport to go stale and no last-arrival to measure. Staleness is a property of
a bridge, not of the state, and importing that check here would mean the daemon
retiring its own live truth on a timer.

No new flags
------------
Every strip already owns its own master switch (`pending_apply.strip_enabled`,
`panic_arbiter.panic_arbiter_enabled`, `operator_input_queue.input_queue_enabled`,
and so on) and each returns an empty list when off. Mounting a provider therefore
cannot force a surface on, and adding a gate here would be a second answer to a
question those modules already answer — the mistake `narrative_density` refused
when it declined to absorb posture.

Width is resolved PER FRAME, never captured
-------------------------------------------
Every renderer takes a width and the canvas draws with ``wrap_lines=False``, so a
row wrapped to a stale width is clipped at the right edge rather than reflowed.
A provider closing over the width it saw at mount would be correct until the
first resize.

NEVER raises. A strip that cannot resolve returns no rows; a cockpit that cannot
draw a strip still draws the cockpit.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, List, Optional

logger = logging.getLogger("Ouroboros.CockpitMount")

COCKPIT_MOUNT_SCHEMA_VERSION = "cockpit_mount.v1"


def _terminal_width(fallback: int = 100) -> int:
    """This frame's width. Resolved on every call, deliberately — see module doc."""
    try:
        import shutil
        return max(20, int(shutil.get_terminal_size((fallback, 30)).columns))
    except Exception:  # noqa: BLE001
        return fallback


# ---------------------------------------------------------------------------
# In-process providers — local snapshot → the SHARED renderer
# ---------------------------------------------------------------------------


def daemon_pending_rows() -> List[str]:
    """The NOTIFY_APPLY countdown, for the process that owns the gate.

    `snapshot()` drops expired entries where the clock that set them lives, so
    this reader never decides whether an op has run out — it would be guessing
    from a frame that is already a second old.

    ``age_s=0.0`` and not a measured age: in-process there is no transport
    between the snapshot and the render, so the age IS zero. The attach client
    passes a real age because its snapshot crossed a bridge and the countdown has
    to keep ticking between 1 Hz heartbeats.
    """
    try:
        from backend.core.ouroboros.battle_test.pending_apply import (
            render, snapshot,
        )
        return render(snapshot(), age_s=0.0, width=_terminal_width())
    except Exception:  # noqa: BLE001
        logger.debug("[CockpitMount] pending rows unavailable", exc_info=True)
        return []


def daemon_panic_rows() -> List[str]:
    """The FATAL overlay, on the process where the task actually died.

    `recent_panics()` is the arbiter's own deduplicated record — the daemon's
    loop exception handler calls `arbitrate`, which is what fills it. Rendering
    the most recent one mirrors the client, which holds a single `self._panic`.

    The overlay is the loudest thing a cockpit draws, so it must not be summoned
    by an EMPTY record: `render_panic(None)` already answers `[]`, and passing a
    falsy payload through unchanged keeps that one decision in one place.

    The field names do NOT line up, and the mismatch is silent. `Panic` stores
    ``traceback_text``; `render_panic` reads ``"traceback"`` — the wire spelling
    the attach client receives over the bridge. Handing the dataclass's own
    ``__dict__`` straight over therefore produces a FATAL overlay with an empty
    traceback: the alarm fires, correctly, and drops the only part of it anybody
    needs. So the adapter is explicit and `_panic_payload` is pinned by a test
    that derives the expected keys from `render_panic` itself, rather than
    restating them here where they could rot apart again.
    """
    try:
        from backend.core.ouroboros.battle_test.panic_arbiter import (
            recent_panics, render_panic,
        )
        panics = recent_panics() or []
        if not panics:
            return []
        return render_panic(_panic_payload(panics[-1]),
                            width=_terminal_width())
    except Exception:  # noqa: BLE001
        logger.debug("[CockpitMount] panic rows unavailable", exc_info=True)
        return []


def _panic_payload(panic: Any) -> Optional[dict]:
    """A `Panic` in the shape `render_panic` reads. NEVER raises.

    A dict is passed through untouched — that is already the wire shape, and
    re-mapping it would corrupt the client's own payload if this were ever reused
    on that side.
    """
    if panic is None:
        return None
    if isinstance(panic, dict):
        return panic
    try:
        return {
            "exc_type": getattr(panic, "exc_type", "") or "",
            "message": getattr(panic, "message", "") or "",
            "origin": getattr(panic, "origin", "") or "",
            # The rename that would otherwise have shipped an empty overlay.
            "traceback": getattr(panic, "traceback_text", "") or "",
        }
    except Exception:  # noqa: BLE001
        return None


def daemon_queue_rows() -> List[str]:
    """The operator's own backlog — lines typed ahead of the organism."""
    try:
        from backend.core.ouroboros.battle_test.operator_input_queue import (
            active_queue_snapshot, render_queue,
        )
        return render_queue(active_queue_snapshot(), width=_terminal_width())
    except Exception:  # noqa: BLE001
        logger.debug("[CockpitMount] queue rows unavailable", exc_info=True)
        return []


def daemon_search_rows() -> Any:
    """The `/` transcript search bar, or None when the hatches are absent.

    None rather than a lambda yielding nothing: a strip whose provider can never
    produce anything should not be in the layout at all, which is the contract
    `ov.py::_transcript_search_rows` already states.
    """
    try:
        from backend.core.ouroboros.battle_test.transcript_hatches import (
            search_status,
        )
        return search_status
    except Exception:  # noqa: BLE001
        logger.debug("[CockpitMount] search rows unavailable", exc_info=True)
        return None


def daemon_serpent_active() -> bool:
    """Is the organism THINKING right now?

    Drives the serpent hairline border. Read from `build_heartbeat_payload` — the
    same pure-pull payload the turn spinner and the toolbar pulse consume — so the
    border, the verb and the token counter cannot disagree about whether work is
    happening. A border that moves while the toolbar says idle teaches an operator
    to stop believing both.

    This hook was filled by NEITHER shipping surface: the animation existed and
    only ever ran in `ov demo live`.
    """
    try:
        from backend.core.ouroboros.battle_test.attach_heartbeat import (
            build_heartbeat_payload,
        )
        payload = build_heartbeat_payload() or {}
        return bool(payload.get("active"))
    except Exception:  # noqa: BLE001
        return False


class LocalCockpitClient:
    """The daemon terminal's stand-in for the attach client's `ui`/`client`.

    `install_transcript_hatches(kb, ui, client)` needs exactly three things:
    ``ui.flash(...)``, a mutable ``ui._narrate_verbose`` and
    ``client.send_input(...)``. None of that is bridge-specific — the bridge
    exists because the cockpit is a SEPARATE PROCESS and had to ask the daemon to
    act. At the daemon's own terminal there is no second process, so the same
    three calls are local.

    This is `LocalRewindClient`'s argument applied to the hatches: the second
    ENTRANCE to one implementation, never a second implementation. It is why the
    keys are not reimplemented here — the daemon gets the identical remappable
    action set, so `keybindings.json` cannot mean two different things depending
    on which terminal an operator is sitting at.

    Serves as BOTH `ui` and `client`: the installer only ever asks for those
    three members, and splitting them into two shims would invent a distinction
    the callee does not make.

    NEVER raises. A keybinding that can break the REPL it is bound in is worse
    than an unbound key.
    """

    __slots__ = ("_send", "_flash", "_narrate_verbose")

    def __init__(self, *, send_input: Any, flash: Any = None) -> None:
        self._send = send_input
        self._flash = flash
        #: Read AND written by the hatch action, so it has to be a real
        #: attribute rather than a property — the installer toggles it.
        self._narrate_verbose = False

    def send_input(self, text: str) -> None:
        try:
            if self._send is not None:
                self._send(str(text))
        except Exception:  # noqa: BLE001
            logger.debug("[CockpitMount] local send_input degraded",
                         exc_info=True)

    def flash(self, message: str, *_args: Any, **_kwargs: Any) -> None:
        """Surface a transient notice. Falls back to the canvas.

        The attach client owns a flash region; the daemon cockpit does not, and
        the canvas is the surface an operator is already reading. Swallowing the
        message instead would make a bound key look broken.
        """
        try:
            if self._flash is not None:
                self._flash(str(message))
                return
            from backend.core.ouroboros.battle_test.bipartite_layout import (
                get_active_canvas,
            )
            canvas = get_active_canvas()
            if canvas is not None:
                canvas.push_raw(str(message))
        except Exception:  # noqa: BLE001
            logger.debug("[CockpitMount] local flash degraded", exc_info=True)


def daemon_key_bindings(repl: Any = None) -> Any:
    """The hatch/search action set, bound locally. None when nothing bound.

    Mounted for a reason that is easy to miss: the search BAR without the search
    KEY is decoration. `search_rows` gives the daemon cockpit a strip that
    renders when a search is open, and on this surface nothing could open one —
    `extra_key_bindings` was unset, so `/` was never bound. Shipping the strip
    alone would have added a row that could never appear and called the gap
    closed.

    Returns a `KeyBindings` the caller merges through the layout's existing
    `extra_key_bindings` seam, so there is no layout surgery and the daemon and
    attach surfaces stay one code path.
    """
    try:
        from prompt_toolkit.key_binding import KeyBindings

        from backend.core.ouroboros.battle_test.subagent_control import (
            install_stop_all_binding,
        )
        from backend.core.ouroboros.battle_test.transcript_hatches import (
            install_transcript_hatches,
        )
        send = getattr(repl, "_dispatch_verb", None) or getattr(
            repl, "handle_input", None)
        shim = LocalCockpitClient(send_input=send)
        kb = KeyBindings()
        hatches = install_transcript_hatches(kb, shim, shim)
        # Ctrl+X Ctrl+K, through the SAME shim: the chord sends `/stop-all`
        # and `_dispatch_repl_command` does the rest, so the daemon and the
        # attach client reach one authority by one route.
        stop_all = install_stop_all_binding(
            kb, shim,
            notify=lambda msg: _daemon_notice(repl, msg),
            running=_local_running_agents,
        )
        # Bound if EITHER cluster took. Returning None when only the hatches
        # failed would silently drop the stop-all chord along with them.
        if not (hatches or stop_all):
            return None
        return kb
    except Exception:  # noqa: BLE001
        logger.debug("[CockpitMount] daemon key bindings unavailable",
                     exc_info=True)
        return None


def _daemon_notice(repl: Any, message: str) -> None:
    """One transient line on the daemon's own console. NEVER raises.

    The client answers this with `ui.flash`, which the daemon cockpit has no
    equivalent of — its transient surface IS the console. Kept to a plain
    dim line rather than reaching for the deck: an arming prompt that has
    three seconds to live must not join a scrollback the operator will read
    later.
    """
    try:
        flow = getattr(repl, "_flow", None)
        console = getattr(flow, "console", None)
        if console is None:
            return
        console.print(f"  [dim]{message}[/dim]", highlight=False)
    except Exception:  # noqa: BLE001
        pass


def _local_running_agents() -> int:
    """Agents running in THIS process's roster. NEVER raises.

    The daemon dispatches them, so its own singleton is the truth here — no
    snapshot age, no bridge. The attach client asks its `AttachUI` instead,
    which holds the daemon's last frame.
    """
    try:
        from backend.core.ouroboros.battle_test.agent_roster import (
            get_agent_roster,
        )
        return int(get_agent_roster().running_count)   # a property
    except Exception:  # noqa: BLE001
        return 0


def daemon_toolbar() -> Any:
    """The pulse line, or None. NEVER raises.

    `format_heartbeat_line` is what every attached cockpit draws, fed by the same
    pure-pull `build_heartbeat_payload` the turn spinner and `serpent_active`
    read. The daemon had no toolbar at all: the process that knows the provider,
    the elapsed and the token count showed none of it at its own terminal.

    ``now_mono``/``arrival_mono`` are the same instant here. The client passes a
    real arrival because its payload crossed a bridge and the elapsed has to
    advance between 1 Hz frames; in-process there is no transport, so claiming an
    age would be inventing one.
    """
    try:
        from backend.core.ouroboros.battle_test.attach_heartbeat import (
            build_heartbeat_payload, format_heartbeat_line,
        )

        def _render() -> str:
            try:
                import time
                now = time.monotonic()
                return format_heartbeat_line(
                    build_heartbeat_payload(), now_mono=now, arrival_mono=now,
                ) or ""
            except Exception:  # noqa: BLE001
                return ""

        return _render
    except Exception:  # noqa: BLE001
        logger.debug("[CockpitMount] toolbar unavailable", exc_info=True)
        return None


def daemon_diff_rows() -> Any:
    """The archived-diff overlay's rows, or None. NEVER raises.

    Returns the singleton controller's bound method, so the verb that OPENS a diff
    and this hook that DRAWS it are the same object. Handing back a fresh
    controller would give `/expand d-N` a surface to fill that nothing renders.

    None when the controller cannot be built: a float whose provider can never
    yield anything should not be in the layout.
    """
    try:
        from backend.core.ouroboros.battle_test.diff_overlay import (
            get_default_controller,
        )
        return get_default_controller().rows
    except Exception:  # noqa: BLE001
        logger.debug("[CockpitMount] diff overlay unavailable", exc_info=True)
        return None


def seed_daemon_masthead(mux: Any) -> int:
    """Lay the daemon's identity block into its transcript. NEVER raises.

    The orphan from unmounting the header: the identity line had nowhere else to
    live, and an operator who cannot tell WHICH process they are typing at will
    eventually pause autonomy on the wrong one.

    Idempotent via `seed_masthead`'s own claim, so the resize storm a terminal emits
    during boot cannot stack emblems into an append-only ring. Safe to call from
    every mount path for exactly that reason.
    """
    try:
        if mux is None:
            return 0
        render, _height = daemon_header()
        if render is None:
            return 0
        return int(mux.seed_masthead(render, key="daemon"))
    except Exception:  # noqa: BLE001
        logger.debug("[CockpitMount] masthead seed degraded", exc_info=True)
        return 0


def daemon_header() -> "tuple":
    """``(render, height)`` for the crest above the deck, or ``(None, 0)``.

    The daemon cockpit had no identity surface at all: the process that IS the
    organism showed no emblem, while the attach client — a thin viewer of it — drew
    one. Rendered by `crest_animator`'s own `render_cockpit_header`, so all three
    surfaces draw the same emblem and none can drift into its own look.

    `ensure_frames` is scheduled with `ensure_future` and that is safe HERE
    specifically because the daemon builds its mount from inside an already-async
    `SerpentREPL.start`. The identical line in `ov_demo` was a bug — it ran before
    `asyncio.run`, so the coroutine was never awaited and the crest stayed on its
    unbuilt fallback. The call is not portable; its context is what makes it
    correct.
    """
    try:
        from backend.core.ouroboros.ui.crest_animator import (
            MiniCrest, render_cockpit_header,
        )
        mini = MiniCrest()
        if not mini.available:
            return (None, 0)
        try:
            import asyncio
            asyncio.get_running_loop()
            asyncio.ensure_future(mini.ensure_frames())
        except RuntimeError:
            # No loop: the emblem draws its static fallback rather than raising.
            # Scheduling onto nothing is what produced the never-awaited warning.
            pass

        def _render() -> str:
            try:
                import time
                return render_cockpit_header(
                    mini, _daemon_header_lines(), _terminal_width(),
                    now=time.monotonic(),
                )
            except Exception:  # noqa: BLE001
                return ""

        return (_render, max(3, int(getattr(mini, "rows", 3) or 3)))
    except Exception:  # noqa: BLE001
        logger.debug("[CockpitMount] crest header unavailable", exc_info=True)
        return (None, 0)


def _daemon_header_lines() -> List[Any]:
    """Rich ``Text`` beside the emblem — never markup strings.

    `render_cockpit_header` accepts "Rich renderable Texts (or plain strings)", so a
    markup string is the PLAIN case and `[dim]…[/dim]` reaches the operator
    verbatim. That shipped once already, in the demo's header.

    Says DAEMON explicitly: this cockpit and the attach client's look alike, and an
    operator who cannot tell which process they are typing at will eventually pause
    autonomy on the wrong one.
    """
    try:
        from rich.text import Text

        from backend.core.ouroboros.ui.semantic_tokens import role_palette
        palette = role_palette()
        dim = palette.get("dim") or "dim"
        first = Text()
        first.append("◇ ", style=palette.get("neural") or "cyan")
        first.append("O+V", style="bold")
        first.append(" · daemon cockpit", style=dim)
        return [
            first,
            Text("the organism itself · this process dispatches", style=dim),
            Text("/ for verbs · esc-esc rewind · ctrl+r search", style=dim),
        ]
    except Exception:  # noqa: BLE001
        return ["◇ O+V · daemon cockpit", "the organism itself",
                "/ for verbs"]


def build_daemon_mount(repl: Any = None) -> "dict":
    """Every in-process hook the daemon cockpit can fill, as a plain dict.

    Returned as VALUES for the caller to pass explicitly, and deliberately not
    splatted at the call site. `ui/capability_handoff` reads a `**kwargs` splat as
    OPAQUE — it cannot see which names a splat covers, and rightly refuses to
    guess — so a mount that spread itself would blind the very audit that found
    these eleven gaps. The composition removes the duplicated LOGIC; the call
    sites stay named so coverage remains measurable, and `divergence()` still
    flags any surface that forgets one.

    ``repl`` is the daemon REPL, consulted only for the one provider that needs a
    local action sink (`daemon_key_bindings`). Every other provider reads
    process-global state.
    """
    mount: dict = {
        "pending_rows": daemon_pending_rows,
        "panic_rows": daemon_panic_rows,
        "queue_rows": daemon_queue_rows,
        # Resolved (not a factory): a strip whose provider can never yield should
        # not be in the layout at all.
        "search_rows": daemon_search_rows(),
        "serpent_active": daemon_serpent_active,
        "toolbar": daemon_toolbar(),
        # The archived-diff overlay. The DAEMON owns the archive — it is the
        # process that files a candidate — so this is the only surface that can
        # render one locally. `rows` is the controller's pure-pull read: O(1),
        # never blocking, filled off-thread.
        "diff_rows": daemon_diff_rows(),
        # Bound here so the search bar above is REACHABLE. A strip with no key to
        # open it is a row that can never appear.
        "extra_key_bindings": daemon_key_bindings(repl),
    }
    # NO header region. The crest is TRANSCRIPT content now — see
    # `seed_daemon_masthead`, called once at cockpit mount. A fixed top region
    # stranded the emblem at row 0 while the bottom-anchored deck hugged the
    # prompt, and the band between belonged to neither.
    #
    # `header`/`header_height` are left ABSENT rather than waived here because the
    # mount is a dict of values, not a call site — `capability_handoff` reads the
    # waiver at the place the argument is passed, which is `serpent_flow`.
    # `stream_rows` is deliberately absent, and this is the reason rather than an
    # oversight: there is NO process-global in-flight text to read.
    # `live_tool_stream.make_tool_observer` creates a stream per tool CALL and
    # nothing publishes a current frame, so a provider here would have to invent
    # one — and an in-flight strip that shows the wrong sentence is worse than no
    # strip. The correct fix is a `set_active_stream` registry mirroring the
    # `set_active_canvas` / `set_active_queue` pattern this codebase already uses
    # twice, which needs a producer-side audit of every stream construction site.
    # Left UNSET rather than waived so `capability_handoff` keeps reporting it.
    return mount


__all__ = [
    "COCKPIT_MOUNT_SCHEMA_VERSION",
    "build_daemon_mount",
    "daemon_panic_rows",
    "daemon_pending_rows",
    "daemon_queue_rows",
    "daemon_search_rows",
    "daemon_serpent_active",
]
