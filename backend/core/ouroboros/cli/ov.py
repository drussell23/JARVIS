"""backend/core/ouroboros/cli/ov.py -- the ``ov`` console entry point.

``ov`` is the packaged binary (PEP 621 ``[project.scripts]``). It is a thin
*dispatcher*: it translates subcommands into the legacy battle-test
bootstrap's argv and delegates, so it never re-parses the real flags -- the
single source of truth for arguments stays in
``scripts/ouroboros_battle_test.py`` (DRY, spec §4.3).

Subcommands::

    ov                 boot the organism + live cockpit (default)
    ov run [flags]     headless autonomous session  (-> --headless)
    ov daemon [flags]  alias for a headless run
    ov status          last-session digest (no boot)
    ov attach          attach to a running organism (hydrated live view + input)
    ov help            usage

Everything after the verb forwards verbatim to the bootstrap, e.g.
``ov run --cost-cap 2.00 -v`` -> ``main(["--headless", "--cost-cap", "2.00", "-v"])``.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional, Sequence

from backend.core.ouroboros.ui.theme import build_console

_VERBS = {"cockpit", "run", "daemon", "status", "attach", "system", "hive",
          "doctor", "version"}
_HELP_TOKENS = {"help", "--help", "-h"}
_VERSION_TOKENS = {"version", "--version", "-V"}

#: Milestone name — paired with the pyproject version at render time.
#: Minted per release; the number itself is NEVER duplicated here.
RELEASE_NAME = "unchained"


def resolve_version() -> str:
    """``0.1.0`` — dynamically from installed metadata, falling back to
    the repo's pyproject.toml (editable/dev checkouts), then to the
    honest ``0.0.0+unknown``. NEVER raises."""
    try:
        from importlib.metadata import version as _dist_version
        return _dist_version("ouroboros-ov")
    except Exception:
        pass
    try:
        import tomllib
        from pathlib import Path
        root = Path(__file__).resolve().parents[4]
        data = tomllib.loads((root / "pyproject.toml").read_text())
        v = str(data.get("project", {}).get("version", "")).strip()
        if v:
            return v
    except Exception:
        pass
    return "0.0.0+unknown"


def version_line() -> str:
    """``ov 0.1.0 “unchained” — ouroboros + venom``. NEVER raises."""
    try:
        return f"ov {resolve_version()} “{RELEASE_NAME}” — ouroboros + venom"
    except Exception:
        return "ov — ouroboros + venom"

def _render_markup_frame(text: str, console: Any = None) -> None:
    """Render ONE daemon-composed styled line (the typed ``markup`` frame:
    CC-style ⏺/⎿ tool blocks + numbered diffs). Unlike the untyped ``line``
    frame (always escaped — inert DATA), markup frames carry daemon-authored
    styling whose MODEL-controlled content was escaped at composition
    (tool_render_view). Fail-soft: markup that does not parse renders
    ESCAPED rather than dropped or crashing the canvas. NEVER raises."""
    try:
        from rich.text import Text as _RichText
        from rich.markup import escape as _escape
        raw = str(text)
        try:
            _RichText.from_markup(raw)          # validate before trusting
            safe = raw
        except Exception:  # noqa: BLE001 — malformed → inert fallback
            safe = _escape(raw)
        from backend.core.ouroboros.battle_test.bipartite_layout import (
            get_active_canvas,
        )
        canvas = get_active_canvas()
        if canvas is not None:
            canvas.push_raw(safe)
            return
        # NON-DESTRUCTIVE INJECTION.
        #
        # A Rich Console binds sys.stdout at CONSTRUCTION. `patch_stdout`
        # swaps sys.stdout afterwards, so a console built before the prompt
        # started writes straight past the proxy and paints over the line the
        # operator is typing. `_print_line` already documents this — "a
        # pre-bound Rich console would bypass the patch and corrupt the input
        # line" — and then this branch did precisely that.
        #
        # It mattered little while markup carried only occasional op chrome.
        # It matters now: every Moltbook post and all 60 REPL verb results
        # arrive on this channel, unprompted, while the operator types.
        #
        # The fix is to bind LATE, not to build a redraw engine. A console
        # constructed against the CURRENT sys.stdout is the patched proxy, so
        # prompt_toolkit renders the line above the prompt and redraws the
        # input buffer intact — its own machinery, reused rather than
        # reimplemented. Width is inherited from the original console so the
        # proxy (not a tty) does not collapse to 80 columns.
        _emitted = False
        try:
            import sys as _sys

            from rich.console import Console as _Console
            _kw = {"file": _sys.stdout, "highlight": False}
            _width = getattr(console, "width", None)
            if isinstance(_width, int) and _width > 0:
                _kw["width"] = _width
            _late = _Console(**{k: v for k, v in _kw.items()
                                if k != "highlight"})
            _late.print(safe, highlight=False)
            _emitted = True
        except Exception:  # noqa: BLE001 — never lose the frame
            _emitted = False
        if not _emitted:
            if console is not None:
                console.print(safe, highlight=False)
            else:
                print(raw)
    except Exception:  # noqa: BLE001
        try:
            print(str(text))
        except Exception:  # noqa: BLE001
            pass


_NO_ORGANISM_MESSAGE = (
    "no organism awake — nothing to attach to. Start one with `ov` "
    "(cockpit) or `ov daemon` (headless)."
)

_HELP_TEXT = """ov -- Ouroboros + Venom, autonomous engineering organism

  ov                  instant cockpit — attach to the organism
                      (cold-boots one in the background if needed;
                      --legacy-boot forces the old in-process boot)
  ov run [flags]      headless autonomous session (foreground)
  ov daemon [flags]   alias for a headless run
  ov daemon --install    install the resident organism (launchd agent)
  ov daemon --uninstall  remove the resident organism
  ov status           last-session digest (no boot)
  ov doctor [--live]  8-edge connectivity matrix; --live fires the
                      trace-isolated synthetic tool probe end-to-end
  ov attach           attach this terminal to the running organism
  ov version          version + milestone
  ov help             this message

All flags after the verb forward to the battle-test bootstrap, e.g.
  ov run --cost-cap 2.00 -v
See `python3 scripts/ouroboros_battle_test.py --help` for the full flag set.
"""


@dataclass
class Invocation:
    """The resolved intent of an ``ov`` command line.

    ``action`` is one of ``cockpit`` / ``headless`` / ``status`` / ``attach``
    / ``help``. ``delegate_argv`` is the argv handed to the legacy bootstrap
    for the boot actions. ``message`` carries the notice for the attach stub.
    """

    action: str
    delegate_argv: List[str] = field(default_factory=list)
    message: str = ""


def resolve(argv: Optional[Sequence[str]] = None) -> Invocation:
    """Translate an ``ov`` argv into an :class:`Invocation`.

    Pure + side-effect free so the routing is fully unit-testable without
    booting the organism. Unknown leading flags (no verb) default to the
    cockpit with the flags forwarded verbatim.
    """
    tokens = list(argv or [])

    if tokens and tokens[0] in _HELP_TOKENS:
        return Invocation("help")

    if tokens and tokens[0] in _VERSION_TOKENS:
        return Invocation("version")

    if tokens and tokens[0] in _VERBS:
        verb, rest = tokens[0], list(tokens[1:])
    else:
        verb, rest = "cockpit", list(tokens)

    if verb == "daemon" and "--install" in rest:
        return Invocation("daemon_install")
    if verb == "daemon" and "--uninstall" in rest:
        return Invocation("daemon_uninstall")
    if verb in ("run", "daemon"):
        return Invocation("headless", ["--headless", *rest])
    if verb == "status":
        return Invocation("status")
    if verb == "attach":
        return Invocation("attach")
    if verb == "system":
        return Invocation("system")
    if verb == "hive":
        return Invocation("hive")
    if verb == "doctor":
        return Invocation("doctor", list(rest))
    # cockpit (explicit or defaulted)
    return Invocation("cockpit", rest)


# ---------------------------------------------------------------------------
# ov status
# ---------------------------------------------------------------------------


def _default_status_provider() -> Optional[str]:
    """Best-effort digest of recent sessions (authority-free, read-only).

    Reads :meth:`LastSessionSummary.operator_digest_sync` — the
    OPERATOR-plane surface, deliberately ungated by
    ``JARVIS_LAST_SESSION_SUMMARY_ENABLED`` (that flag governs the
    organism's prompt-injection authority, not a human's explicit query;
    routing status through the autonomy gate was the wired-but-inert
    root cause: sessions on disk, "no prior session found" on screen).
    Returns ``None`` when no parseable prior session exists -- callers
    render a friendly fallback. NEVER raises.
    """
    try:
        from backend.core.ouroboros.governance.last_session_summary import (
            get_default_summary,
        )

        text = get_default_summary().operator_digest_sync()
        if isinstance(text, str) and text.strip():
            return text.strip()
        return None
    except Exception:
        return None


def status_digest(provider: Optional[Callable[[], Optional[str]]] = None) -> str:
    """Return a one-line status string. NEVER raises.

    ``provider`` is injectable for tests; production uses
    :func:`_default_status_provider`.
    """
    try:
        p = provider or _default_status_provider
        line = p()
        if line:
            return line
        return "ov -- no prior session found"
    except Exception:
        return "ov -- status unavailable"


# ---------------------------------------------------------------------------
# ov attach — dumb terminal over the Cockpit Attach Bridge (CLI item #6)
# ---------------------------------------------------------------------------


def _render_hydration(console: Any, payload: dict) -> None:
    """Instant-state render — the operator NEVER stares at a blank
    screen waiting for the next FSM tick. Pure presentation; the daemon
    is the single source of truth. NEVER raises."""
    try:
        status = payload.get("status") or {}
        liq = payload.get("liquidity") or {}
        ops = payload.get("ops") or []
        phase = status.get("phase", "IDLE")
        detail = status.get("phase_detail", "")
        cost = status.get("cost_spent_usd", 0.0)
        budget = status.get("cost_budget_usd", 0.0)
        console.print(
            f"⏺ attached — phase: {phase}"
            + (f" {detail}" if detail else "")
            + f" · cost: ${cost:.2f}/${budget:.2f}",
            markup=False, highlight=False,
        )
        if ops:
            console.print(
                f"⎿ active ops: {', '.join(str(o) for o in ops[:4])}",
                markup=False, highlight=False,
            )
        providers = liq.get("providers") or {}
        for name, row in list(providers.items())[:3]:
            tokens = row.get("tokens_remaining")
            tok_txt = f"{tokens:,} tokens" if tokens is not None else "undeclared"
            console.print(
                f"⎿ liquidity {name}: {tok_txt}",
                markup=False, highlight=False,
            )
        if liq.get("any_exhausted"):
            console.print("⚠ a provider runway is dry", markup=False)
        console.print(
            "⎿ type verbs or plain text · Ctrl+C detaches (the organism "
            "keeps running)", markup=False, highlight=False,
        )
    except Exception:
        pass


def _can_run_split_plane() -> bool:
    """Split-plane needs a real TTY on stdin AND prompt_toolkit — piped
    / scripted attaches degrade to the legacy pump. NEVER raises."""
    try:
        if not sys.stdin.isatty():
            return False
        import prompt_toolkit  # noqa: F401
        return True
    except Exception:
        return False


async def _reap_task(task: Any) -> None:
    """Retrieve a task's outcome on EVERY exit path — the 2026-07-18
    dirty-detach class: an abandoned prompt task finishing with its own
    KeyboardInterrupt made asyncio dump 'Task exception was never
    retrieved' over the clean goodbye. Cancel if pending, then consume
    the result/exception so nothing is left for the GC to complain
    about. NEVER raises."""
    import asyncio
    try:
        if task is None:
            return
        if not task.done():
            task.cancel()
        try:
            await task
        except BaseException:  # noqa: BLE001 — incl. KeyboardInterrupt
            pass
        # Belt: mark a completed task's exception as retrieved.
        if task.done() and not task.cancelled():
            try:
                task.exception()
            except BaseException:  # noqa: BLE001
                pass
    except Exception:
        pass


class AttachUI:
    """The rigid Footer/Header state of the attached TUI.

    Owns the audio-FSM presentation binding (operator mandate: Dynamic
    UI Morphing). ``prompt()`` and ``toolbar()`` are the dynamic
    callables handed to the ONE persistent ``PromptSession`` — prompt
    duplication died with per-iteration prompt construction; the
    session re-evaluates these on every repaint, so a state change
    repaints the footer WITHOUT touching the active keystroke buffer.
    ``on_audio_state`` is loop-safe: it mutates state then invalidates
    the app so prompt_toolkit repaints on its own schedule.
    """

    _PROMPTS = {
        "OFFLINE": "ov › ",
        "UNAVAILABLE": "ov › ",
        "HELD": "ov › ",
        "LISTENING": "🎙 Karen › ",
        "HEARING": "🎙 Karen (hearing you) › ",
        "THINKING": "💭 Karen (thinking) › ",
        "SPEAKING": "🗣 Karen (speaking) › ",
    }

    _TOOLBAR_NOTES = {
        "HELD": "voice: held by another terminal ('wake!' to take it)",
        "UNAVAILABLE": "voice: unavailable (no audio plane)",
    }

    def __init__(self) -> None:
        self.audio_state: str = "OFFLINE"
        self._app_ref: Any = None
        # Latest heartbeat frame (the CC-style pulse) + arrival clock —
        # rendered by toolbar() with client-side elapsed advance and a
        # time-driven glyph (the pt refresh_interval animates it free).
        self._heartbeat: Any = None
        self._heartbeat_arrived: float = 0.0

    def bind_app(self, app: Any) -> None:
        self._app_ref = app

    def prompt(self) -> str:
        return self._PROMPTS.get(self.audio_state, "ov › ")

    def toolbar(self) -> str:
        note = self._TOOLBAR_NOTES.get(self.audio_state)
        if note is not None:
            audio = f" · {note}"
        elif self.audio_state == "OFFLINE":
            audio = " · voice: off ('wake')"
        else:
            audio = f" · voice: {self.audio_state.lower()}"
        # CC-style live pulse while the organism works — falls back to
        # the idle line when inactive/stale. NEVER raises.
        try:
            from backend.core.ouroboros.battle_test.attach_heartbeat import (
                format_heartbeat_line,
            )
            pulse = format_heartbeat_line(
                self._heartbeat, arrival_mono=self._heartbeat_arrived,
            )
            if pulse:
                return f"{pulse}{audio} · 'detach' to leave"
        except Exception:
            pass
        return f" ov attach — organism live{audio} · 'detach' to leave"

    def on_telemetry(self, frame: Any) -> None:
        """Telemetry lane landing point — retain heartbeat frames for
        the toolbar pulse; other telemetry kinds pass untouched.
        NEVER raises."""
        try:
            if isinstance(frame, dict) and frame.get("kind") == "heartbeat":
                import time as _time
                self._heartbeat = frame
                self._heartbeat_arrived = _time.monotonic()
        except Exception:
            pass

    def should_flush_on_input(self) -> bool:
        """Ducking predicate: the operator typed a NEW command while
        Karen is composing or speaking — outbound audio yields to the
        human instantly. NEVER raises."""
        return self.audio_state in ("THINKING", "SPEAKING")

    def on_audio_state(self, state: str) -> None:
        """The synapse landing point — morph + repaint. NEVER raises."""
        try:
            state = str(state or "").strip().upper()
            if not state or state == self.audio_state:
                return
            self.audio_state = state
            app = self._app_ref
            if app is not None:
                app.invalidate()
        except Exception:
            pass


def _maybe_summon_audio_plane(client: Any, cmd: str) -> None:
    """Ensure an audio plane exists, without blocking the input loop.

    Schedules the reflex on the running loop and re-sends the arming verb once
    the supervisor is listening — the first send raced the boot and was
    answered by nobody. A no-op when a supervisor is already up (the reflex
    probes before it spawns), and entirely inert with no running loop.
    NEVER raises: a cockpit that cannot summon audio is still a cockpit."""
    try:
        # Local imports: this module imports asyncio per-function (see the
        # existing pattern) and has no module-level logger.
        import asyncio as _aio
        import logging as _logging

        from backend.core.ouroboros.cli.audio_daemon_reflex import (
            ensure_audio_daemon, reflex_enabled,
        )
        _log = _logging.getLogger(__name__)
        if not reflex_enabled():
            return
        try:
            loop = _aio.get_running_loop()
        except RuntimeError:
            return

        async def _summon() -> None:
            try:
                available, reason = await ensure_audio_daemon()
                if available and reason == "spawned":
                    # Re-arm: the original verb was sent before anything was
                    # listening for it.
                    try:
                        client.send_audio(cmd)
                    except Exception:  # noqa: BLE001
                        pass
                    _log.info("[ov] audio plane summoned (%s)", reason)
                elif not available:
                    _log.info("[ov] audio plane unavailable (%s)", reason)
            except Exception:  # noqa: BLE001
                pass

        loop.create_task(_summon())
    except Exception:  # noqa: BLE001
        pass


def _route_operator_line(client: Any, ui: Any, line: Any) -> str:
    """THE one operator-line router — shared by the legacy split-plane loop AND
    the Bipartite cockpit (DRY: verbs behave identically on both surfaces).

    Returns ``"detach"`` (leave the loop), ``"handled"`` (audio verb routed on
    the audio lane), or ``"sent"``/``"empty"``. Audio Control Plane verbs never
    travel as chat text — the daemon synapse owns the duplex. A new operator
    command while Karen is composing flushes her outbound buffer FIRST (the
    human always owns the floor). Never raises."""
    try:
        text = (line or "").strip()
        low = text.lower()
        if low in ("detach", "exit", "quit"):
            return "detach"
        audio_verbs = {
            "wake": "wake", "voice": "wake", "listen": "wake",
            "wake!": "force_wake", "force-wake": "force_wake",
            "force wake": "force_wake",
            "ptt": "ptt",
            "ptt stop": "ptt_stop", "ptt-stop": "ptt_stop", "ptt off": "ptt_stop",
            "flush": "flush", "shh": "flush", "hush": "flush",
            "mute": "sleep", "sleep": "sleep",
            "barge": "barge",
        }
        cmd = audio_verbs.get(low)
        if cmd is not None:
            # AUTO-SPAWN REFLEX. `ov` boots ouroboros_battle_test.py, which has
            # no audio pipeline; the mic lives in unified_supervisor.py. Arming
            # verbs therefore had nothing to arm unless a supervisor happened
            # to be running.
            #
            # `ov` stays a thin IPC relayer — it does NOT import the audio
            # pipeline and never touches CoreAudio. It just starts the process
            # that OWNS the hardware, then relays the verb over the existing
            # UDS. Fire-and-forget so the input loop never stalls behind a
            # 98K-line kernel boot; the verb is relayed either way, so a
            # supervisor that is already live behaves exactly as before.
            if cmd in ("wake", "force_wake"):
                _maybe_summon_audio_plane(client, cmd)
            client.send_audio(cmd)
            return "handled"
        if text:
            if ui is not None and ui.should_flush_on_input():
                client.send_audio("flush")
            client.send_input(text)
            return "sent"
        return "empty"
    except Exception:  # noqa: BLE001 — routing must never crash an input loop
        return "empty"


async def _split_plane_loop(
    client: Any, console: Any, ui: Optional["AttachUI"] = None,
) -> None:
    """The Split-Plane Multiplexer (operator mandate 2026-07-18).

    prompt_toolkit's ``PromptSession`` + ``patch_stdout`` IS the
    thread-safe split-plane mux (DRY — the same solved mechanism
    SerpentFlow's REPL trusts): the ``ov ›`` prompt permanently owns
    the bottom of the TTY on an ASYNC loop (no sleep-blockers, no
    blocking reads); a daemon telemetry line arriving MID-
    KEYSTROKE is intercepted by patch_stdout, the stdin buffer is
    hidden, the line renders on the scrolling plane above, and the
    active input buffer is restored on the bottom line — keystrokes
    can never be split or corrupted (pinned by the concurrent-I/O
    test). The prompt task races a connection watch so a daemon death
    mid-typing detaches instantly instead of hanging on the prompt.
    """
    import asyncio
    from prompt_toolkit import PromptSession
    from prompt_toolkit.patch_stdout import patch_stdout

    # Persona-host moment: the persistent interactive surface opens
    # with Karen as the host — the visual seam between the scrolling
    # daemon history above and the command plane below.
    console.print(
        "\U0001f4ad Karen ▸ attached — I'm listening. verbs or plain "
        "words both work · 'wake' arms my voice · "
        "'detach' leaves the organism running",
        markup=False, highlight=False,
    )

    ui = ui or AttachUI()
    # ONE persistent session, dynamic prompt + rigid footer toolbar:
    # both are callables re-evaluated on every repaint, so an
    # audio_state frame morphs the footer via app.invalidate() while
    # the active keystroke buffer stays untouched (mandate 4).
    session: Any = PromptSession(
        message=lambda: ui.prompt(),
        bottom_toolbar=lambda: ui.toolbar(),
    )
    ui.bind_app(session.app)

    async def _watch_disconnect() -> None:
        while client.connected:
            await asyncio.sleep(0.25)

    with patch_stdout(raw=True):
        while client.connected:
            prompt_task = asyncio.ensure_future(
                session.prompt_async(),
            )
            watch_task = asyncio.ensure_future(_watch_disconnect())
            try:
                done, _pending = await asyncio.wait(
                    {prompt_task, watch_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
            except (KeyboardInterrupt, asyncio.CancelledError):
                # Ctrl+C landed in the WAIT itself — reap BOTH tasks
                # (retrieving any KeyboardInterrupt the prompt task
                # finished with) so the goodbye stays clean: no
                # 'Task exception was never retrieved' ever again.
                await _reap_task(prompt_task)
                await _reap_task(watch_task)
                break
            if watch_task in done and prompt_task not in done:
                # Daemon died mid-typing — never hang on the prompt.
                await _reap_task(prompt_task)
                await _reap_task(watch_task)
                break
            await _reap_task(watch_task)
            try:
                line = prompt_task.result()
            except (EOFError, KeyboardInterrupt):
                await _reap_task(prompt_task)
                break
            outcome = _route_operator_line(client, ui, line)
            if outcome == "detach":
                break



async def _attach_rms_stream(scope: Any) -> Any:
    """Feed the header scope from the SUPERVISOR's amplitude stream.

    The gap this closes. `ov` and `unified_supervisor` are separate processes
    — CoreAudio hands the microphone to exactly one of them, and that one is
    the supervisor. The scope was wired only to ``audio_broadcast_tap``, an
    IN-PROCESS zero-copy broadcast, so in the cockpit process nothing ever
    captured audio, the tap never fired, and the wave sat at its flat baseline
    however loudly anyone spoke.

    The supervisor has been publishing ``rms_level`` frames on the audio-state
    socket the whole time (``MicTelemetryBridge`` → ``publish_rms``). There was
    simply no consumer: a producer with no reader is indistinguishable from a
    silent room, which is exactly why it went unnoticed.

    Subscribed DIRECTLY rather than relayed through the daemon bridge. The
    frames are lossy-by-contract 20 FPS telemetry; hopping them through a
    second socket would add a queue that must then be given its own drop
    policy, to carry samples whose whole design is that dropping them is free.

    The in-process tap subscription stays alongside this — it is still correct
    when the cockpit itself owns audio. Whichever source produces data drives
    the wave; neither knows about the other.

    Returns the connected client (so the caller can close it), or None.
    NEVER raises: no amplitude stream is survivable, a broken cockpit is not.
    """
    try:
        from backend.core.ouroboros.governance.comms.duplex.audio_state_ipc import (
            MSG_RMS_LEVEL, AudioStateClient,
        )
        from backend.core.ouroboros.ui.audio_scope import AudioPlane

        # plane wire-value → the scope's colour lane. Karen speaking is venom
        # green, the operator cyan, so a glance answers "is that me or her?"
        planes = {
            "user": AudioPlane.USER,
            "mic": AudioPlane.USER,
            "system": AudioPlane.SYSTEM,
            "karen": AudioPlane.SYSTEM,
            "tts": AudioPlane.SYSTEM,
        }

        # Edge memory for state transitions, scoped to this subscription.
        _last = {"event": ""}

        def _on_frame(msg: dict) -> None:
            try:
                # TRANSCRIPTS — what the organism actually HEARD.
                #
                # The supervisor has published these on this very socket since
                # 2026-07-18 and the cockpit never read one. So an operator who
                # spoke and got no answer had no way to tell "it never heard
                # me" from "it heard me and could not reply" — two completely
                # different faults, and days were spent guessing between them.
                #
                # Showing the transcript makes the loop legible: you see your
                # own words land, or you see nothing and know the ears are the
                # problem, not the mouth.
                if msg.get("type") == "transcript":
                    _txt = str(msg.get("chunk") or msg.get("text") or "").strip()
                    if _txt and msg.get("final", True):
                        _role = str(msg.get("role", "user")).lower()
                        _who = "you" if _role == "user" else "Karen"
                        _style = "cyan" if _role == "user" else "rgb(94,224,106)"
                        _render_markup_frame(
                            f"[{_style}]🎙 {_who}:[/{_style}] "
                            + __import__("rich.markup", fromlist=["escape"]).escape(_txt)
                        )
                    return
                # LIVE STATE — so the operator is never left in the dark.
                #
                # The supervisor has published these transitions since the IPC
                # was written; the cockpit rendered none of them. Between
                # "Hello Karen" and her reply there are 3-5 seconds of STT,
                # LLM and synthesis during which the screen said nothing at
                # all, and silence is indistinguishable from a hang.
                #
                # Each transition is announced ONCE, on the edge: the state
                # machine upstream is already edge-coalesced, and re-printing
                # a steady state would turn a status line into a scroll.
                if msg.get("type") == "event":
                    _kind = str(msg.get("kind", ""))
                    _label = _AUDIO_STATE_LABELS.get(_kind)
                    # Closure-local, NOT the caller's _audio dict: that name
                    # belongs to a different function and referencing it here
                    # raised a NameError the surrounding except swallowed, so
                    # every state line vanished silently. Exactly the failure
                    # this indicator exists to make impossible.
                    if _label and _kind != _last["event"]:
                        _last["event"] = _kind
                        _render_markup_frame(_label)
                    return
                if msg.get("type") != MSG_RMS_LEVEL:
                    return
                plane = planes.get(str(msg.get("plane", "user")).lower())
                if plane is not None and plane != scope.plane:
                    scope.set_plane(plane)
                # Already normalized upstream: the RMS + adaptive scaling ran
                # on the producer side, next to the frames. Re-normalizing a
                # normalized value here would square the curve.
                scope.push(float(msg.get("level", 0.0)), normalized=True)
            except Exception:  # noqa: BLE001 — one bad frame is not an outage
                pass

        client = AudioStateClient(on_message=_on_frame)
        return client if await client.connect() else None
    except Exception:  # noqa: BLE001
        return None


#: Audio-state transition -> the one line the cockpit shows for it.
#: Karen's own voice grammar (💭 thinking, 🗣 speaking) rather than raw event
#: names, so the operator reads a conversation rather than a state machine.
#: Keys are the EVENT_KINDS values verbatim. Written from the module's own
#: tuple rather than from memory: guessing lowercase names produced a mapping
#: that matched nothing and rendered silently — the same shape of failure as
#: a publisher with no subscriber, which this feature exists to end.
_AUDIO_STATE_LABELS = {
    "VAD_ACTIVE": "[cyan]🎙 listening…[/cyan]",
    "TTS_GENERATING": "[rgb(94,224,106)]💭 Karen is thinking…[/rgb(94,224,106)]",
    "AUDIO_PLAYING": "[rgb(94,224,106)]🗣 Karen is speaking…[/rgb(94,224,106)]",
    "AUDIO_IDLE": "[dim]· ready[/dim]",
    "SYSTEM_WARMING": "[dim]· audio plane warming…[/dim]",
    "SYSTEM_READY": "[dim]· audio plane ready[/dim]",
    "HW_FAULT": "[red]⚠ audio hardware fault[/red]",
    "SYS_TELEMETRY_DEGRADED": "[yellow]⚠ telemetry degraded[/yellow]",
    "SYS_TELEMETRY_RECOVERED": "[dim]· telemetry recovered[/dim]",
}


async def _keep_rms_stream(scope: Any, state: dict) -> None:
    """Maintain the amplitude subscription for the cockpit's whole lifetime.

    The one-shot connect this replaces encoded a boot-order assumption that
    the operator's own workflow violates: `ov` first, `wake` second. The
    cockpit subscribed ONCE at boot; if the audio host wasn't serving at that
    exact instant — it usually isn't, since `wake` is what spawns it — the
    client was None forever and the wave could never move, no matter what
    came up afterwards.

    A subscription is not an event, it is a RELATIONSHIP: the host may start
    late, restart, re-bind after losing its address, or die and be respawned
    by the reflex. So the keeper loops for the cockpit's lifetime — connect
    when absent, notice disconnection, back off with full jitter (several
    cockpits must not stampede a booting host), and reconnect. Cheap when
    idle: one failed connect per backoff tick. NEVER raises."""
    # Local imports — this module imports asyncio per-function by convention,
    # and a bare module-level name here would be a NameError swallowed by the
    # task wrapper: the keeper would die instantly and silently, recreating
    # the exact one-shot behaviour it exists to replace.
    import asyncio
    import random as _random

    delay = 0.5
    while not state.get("closing"):
        client = state.get("rms_client")
        if client is not None and getattr(client, "connected", False):
            delay = 0.5                       # healthy — re-arm the backoff
            await asyncio.sleep(1.0)
            continue
        if client is not None:                # died — release before retrying
            try:
                await client.close()
            except Exception:  # noqa: BLE001
                pass
            state["rms_client"] = None
        try:
            state["rms_client"] = await _attach_rms_stream(scope)
        except Exception:  # noqa: BLE001
            state["rms_client"] = None
        if state.get("rms_client") is None:
            await asyncio.sleep(_random.uniform(0.2, delay))
            delay = min(5.0, delay * 2)       # capped: a host can appear any time
        else:
            delay = 0.5


async def _bipartite_attach_loop(client: Any, console: Any, ui: Any) -> None:
    """The Style-Guide §06 cockpit ON THE CLIENT: Zone 1 (the Proactive Canvas,
    state-reactive border) auto-scrolls the daemon's bridge stream; Zone 2 the
    anchored ``› `` prompt reusing THE SAME verb router as the legacy loop; the
    morphing AttachUI footer rides below. The connection watcher exits the app
    the instant the daemon dies (never hangs mid-typing). Never raises out."""
    from backend.core.ouroboros.battle_test.bipartite_layout import (
        run_bipartite_repl,
    )
    from backend.core.ouroboros.ui.theme import UIState, get_reactive_theme

    # Client-side reactive accent: attached = HEALTHY (cyan). The daemon-death
    # path flips DEGRADED (red) just before the app exits — an honest mapping of
    # the CLIENT's own connection state onto the Style-Guide state ladder.
    try:
        get_reactive_theme().set_state(UIState.HEALTHY)
    except Exception:  # noqa: BLE001
        pass

    def _on_accept(text: str) -> None:
        outcome = _route_operator_line(client, ui, text)
        if outcome == "detach":
            try:
                from prompt_toolkit.application import get_app
                get_app().exit()
            except Exception:  # noqa: BLE001
                pass

    def _alive() -> bool:
        ok = bool(client.connected)
        if not ok:
            try:
                get_reactive_theme().set_state(UIState.DEGRADED)
            except Exception:  # noqa: BLE001
                pass
        return ok

    # The CC-style identity header: the mini ANIMATED crest at top-left with
    # version · state · path beside it (the reactive accent lives in the status
    # dot now that the canvas is borderless). Stateless clock-driven animation;
    # the mini ring builds progressively off-loop. Degrades to text-only on
    # tiny/incapable terminals.
    mini = None
    header_height = 0
    header_render = None
    try:
        import asyncio as _aio
        import os as _os
        import time as _time
        from backend.core.ouroboros.ui.crest_animator import (
            MiniCrest,
            render_cockpit_header,
        )
        from rich.text import Text as _Text

        mini = MiniCrest()
        if mini.available:
            _aio.ensure_future(mini.ensure_frames())
            header_height = max(3, mini.rows)
        else:
            mini = None
            header_height = 3

        def _home_path() -> str:
            try:
                cwd = _os.getcwd()
                home = _os.path.expanduser("~")
                return cwd.replace(home, "~", 1) if cwd.startswith(home) else cwd
            except Exception:
                return ""

        _STATE_DOT = {
            "HEALTHY": "rgb(67,214,208)", "DEGRADED": "rgb(248,81,73)",
            "ARMED": "rgb(227,179,65)", "SOAKING": "rgb(94,224,106)",
            "DORMANT": "rgb(108,125,119)",
        }

        def _header_lines():
            t1 = _Text()
            # The CC title grammar: "O+V v0.1.0" (bold brand + bare version),
            # exactly like "Claude Code v2.1.218". DRY: resolve_version().
            t1.append("O+V", style="bold rgb(94,224,106)")
            t1.append(f" v{resolve_version()}", style="rgb(219,230,225)")
            t2 = _Text()
            state = "HEALTHY"
            try:
                from backend.core.ouroboros.ui.theme import get_reactive_theme
                state = get_reactive_theme().state.value
            except Exception:
                pass
            t2.append("● ", style=_STATE_DOT.get(state, "rgb(67,214,208)"))
            t2.append(state.lower(), style="rgb(174,188,182)")
            t2.append(" · ouroboros + venom · the organism drives", style="rgb(108,125,119)")
            t3 = _Text(_home_path(), style="rgb(108,125,119)")
            return [t1, t2, t3]

        _hdr_width = {"w": 0}

        # ── Audio plane: Braille oscilloscope + protocol-adaptive PTT ──────
        # The scope fills the empty header real estate; the pump owns it and is
        # driven by the zero-copy tap on the EXISTING mic stream (CoreAudio
        # refuses a second handle). Wholly fail-soft: any fault here leaves the
        # cockpit exactly as it was without the visualizer.
        # Columns the crest already owns, plus the 2-space gap the header puts
        # between crest and text. Read from the crest itself rather than
        # guessed, so a different crest tier cannot silently overlap the wave.
        try:
            _CREST_RESERVE = int(getattr(mini, "cols", 0) or 0) + 2
        except Exception:  # noqa: BLE001
            _CREST_RESERVE = 2
        _scope_align = "right"
        _audio = {"pump": None, "latch": None, "mode": None, "unsub": None}
        try:
            from backend.core.ouroboros.ui.audio_pump import (
                AudioLevelPump, default_publisher,
            )
            from backend.core.ouroboros.ui.audio_scope import (
                AudioPlane, BrailleScope, scope_enabled, scope_placement,
                scope_width_for,
            )
            import shutil as _shutil_boot
            from backend.core.ouroboros.ui.ptt_router import (
                PTTLatch, resolve_ptt_mode,
            )
            _scope_align = scope_placement()
            if scope_enabled() and _scope_align != "off":
                # Boot width from the terminal, not a constant. The crest
                # column plus its 2-space gap is reserved so the scope never
                # collides with the identity text it sits beneath.
                _cols0 = _shutil_boot.get_terminal_size(fallback=(100, 30)).columns
                _scope = BrailleScope(
                    width=scope_width_for(_cols0, reserved=_CREST_RESERVE),
                )
                _pump = AudioLevelPump(
                    scope=_scope, publish=default_publisher(),
                )
                # Probe the terminal ONCE at boot: hold-to-talk where the kitty
                # keyboard protocol answers, toggle+VAD everywhere else.
                _mode, _verdict, _tel = resolve_ptt_mode()
                _latch = PTTLatch(
                    mode=_mode,
                    on_open=lambda: (
                        _scope.set_plane(AudioPlane.USER),
                        _pump.publish_mic_state("open"),
                    ),
                    on_close=lambda why: (
                        _scope.set_plane(AudioPlane.IDLE),
                        _pump.publish_mic_state("closed", reason=why),
                    ),
                )
                # Subscribe the pump to the zero-copy tap. RMS runs HERE, on the
                # consumer side — never in the capture thread.
                try:
                    from backend.voice.audio_broadcast_tap import get_default_tap

                    def _on_chunk(view, sr) -> None:
                        lvl = _pump.feed_frames(view, plane=_scope.plane)
                        if lvl is not None:
                            _latch.note_level(lvl)

                    _audio["unsub"] = get_default_tap().subscribe(_on_chunk)
                except Exception:  # noqa: BLE001 — no voice stack: scope stays idle
                    pass
                # ...and to the supervisor's stream, which is where the mic
                # actually lives. A KEEPER task, not a one-shot connect: the
                # host usually starts AFTER the cockpit (wake spawns it), and
                # may restart at any point in the session.
                _audio["rms_task"] = asyncio.get_running_loop().create_task(
                    _keep_rms_stream(_scope, _audio),
                )
                _audio.update(pump=_pump, latch=_latch, mode=_mode)
                # `ov` has no module-level logger — this scope is the only
                # record of which PTT paradigm the terminal probe chose, and a
                # bare `logger` here resolved to nothing but a swallowed
                # NameError, so the line never emitted.
                import logging as _lg
                _lg.getLogger(__name__).debug(
                    "[ov] audio scope armed mode=%s verdict=%s terminal=%s",
                    getattr(_mode, "value", "?"),
                    getattr(_verdict, "value", "?"), (_tel or {}).get("terminal"),
                )
        except Exception:  # noqa: BLE001
            _audio = {"pump": None, "latch": None, "mode": None, "unsub": None}

        def _gutter():
            """The live scope for the header — placed by ``gutter_align``.
            None when unarmed, so the header renders exactly as before."""
            _p = _audio.get("pump")
            if _p is None:
                return None
            try:
                # Follow the terminal. header_render() has already stamped the
                # live column count for this frame, so the scope re-widths on
                # a resize instead of staying pinned at its boot width.
                _w = _hdr_width.get("w") or 0
                if _w:
                    _p.scope.set_width(
                        scope_width_for(_w, reserved=_CREST_RESERVE)
                    )
                # Gravity BEFORE paint, on the render clock rather than the
                # audio clock. When heavy STT inference starves the telemetry
                # stream the wave must keep falling; a repaint that only ever
                # drew the last received frame would freeze the trace mid-spike
                # and report "loud right now" long after the sound stopped.
                _p.scope.tick()
                return _p.scope.render_rich()
            except Exception:  # noqa: BLE001
                return None

        def header_render() -> str:
            try:
                import shutil as _shutil
                w = _shutil.get_terminal_size(fallback=(100, 30)).columns
                _hdr_width["w"] = w
                return render_cockpit_header(
                    mini, _header_lines(), w, now=_time.monotonic(),
                    right_gutter=_gutter, gutter_align=_scope_align,
                )
            except Exception:
                return ""
    except Exception:
        mini, header_render, header_height = None, None, 0

    # Spacebar PTT: merged through the layout's EXISTING extra_key_bindings
    # seam, so no layout surgery. Inert unless the input buffer is empty.
    _ptt_kb = None
    try:
        _latch = _audio.get("latch") if isinstance(_audio, dict) else None
        if _latch is not None:
            from backend.core.ouroboros.ui.ptt_router import build_ptt_key_bindings
            _ptt_kb = build_ptt_key_bindings(_latch)
    except Exception:  # noqa: BLE001 — no PTT is survivable; a broken app is not
        _ptt_kb = None

    def _toolbar_with_mode() -> str:
        """Existing toolbar plus the ACTIVE PTT paradigm. Stating the real mode
        matters: a cockpit claiming 'hold' on a terminal blind to key-release
        would leave the mic latched with no obvious way out."""
        base = ""
        try:
            base = str(ui.toolbar()) if ui is not None else ""
        except Exception:  # noqa: BLE001
            base = ""
        try:
            _m = _audio.get("mode") if isinstance(_audio, dict) else None
            _l = _audio.get("latch") if isinstance(_audio, dict) else None
            if _m is not None:
                live = " ● mic" if (_l is not None and _l.is_open) else ""
                return f"{base} · {_m.hint}{live}" if base else f"{_m.hint}{live}"
        except Exception:  # noqa: BLE001
            pass
        return base

    try:
        await run_bipartite_repl(
            on_accept=_on_accept,
            title="◇ O+V · proactive canvas",
            toolbar=_toolbar_with_mode if ui is not None else None,
            watch_alive=_alive,
            header=header_render,
            header_height=header_height,
            extra_key_bindings=_ptt_kb,
            seed=[
                "[bold]💭 Karen ▸[/bold] attached — I'm listening. verbs or "
                "plain words both work · [cyan]wake[/cyan] arms my voice · "
                "[cyan]detach[/cyan] leaves the organism running",
            ],
        )
    finally:
        # Release the amplitude subscriptions. The tap unsubscribe was already
        # unreleased before the RMS client joined it; that one leaked only
        # until process death, but a live socket plus its read task deserves
        # an explicit close on the way out.
        try:
            _u = _audio.get("unsub") if isinstance(_audio, dict) else None
            if callable(_u):
                _u()
        except Exception:  # noqa: BLE001
            pass
        try:
            if isinstance(_audio, dict):
                _audio["closing"] = True
                _t = _audio.get("rms_task")
                if _t is not None:
                    _t.cancel()
                    try:
                        await _t
                    except (asyncio.CancelledError, Exception):  # noqa: BLE001
                        pass
                _c = _audio.get("rms_client")
                if _c is not None:
                    await _c.close()
        except Exception:  # noqa: BLE001
            pass


async def _legacy_pump_loop(client: Any) -> None:
    """Non-TTY fallback: the original blocking-read-off-loop pump."""
    import asyncio

    async def _stdin_pump() -> None:
        while client.connected:
            try:
                line = await asyncio.to_thread(sys.stdin.readline)
            except Exception:
                break
            if not line:                   # EOF — operator closed stdin
                break
            text = line.strip()
            if text and not client.send_input(text):
                break

    pump = asyncio.get_running_loop().create_task(_stdin_pump())
    try:
        while client.connected:
            await asyncio.sleep(0.25)
    finally:
        pump.cancel()
        try:
            await pump
        except (asyncio.CancelledError, Exception):
            pass


def run_attach(console: Any) -> int:
    """``ov attach`` — hydrate, stream, and pipe stdin upstream.

    The client is a DUMB terminal: every rendered line arrives already
    conformed by the daemon's PresentationRouter chokepoint. Detach
    (Ctrl+C / EOF / daemon exit) never touches the organism."""
    import asyncio

    async def _session() -> int:
        from backend.core.ouroboros.battle_test.cockpit_attach import (
            CockpitAttachClient,
        )

        def _print_line(text: str) -> None:
            # Cockpit mounted → the daemon's bridge stream auto-scrolls into
            # Zone 1 (the Proactive Canvas). Rich markup is escaped so a daemon
            # line can never inject styling into the canvas (inert DATA).
            try:
                from backend.core.ouroboros.battle_test.bipartite_layout import (
                    get_active_canvas,
                )
                canvas = get_active_canvas()
                if canvas is not None:
                    from rich.markup import escape
                    canvas.push_raw(escape(str(text)))
                    return
            except Exception:
                pass
            # Legacy split-plane: builtin print() resolves sys.stdout
            # DYNAMICALLY — under patch_stdout this routes daemon telemetry
            # above the pinned prompt; a pre-bound Rich console would bypass
            # the patch and corrupt the input line.
            try:
                print(text)
            except Exception:
                pass

        hydrated = asyncio.Event()
        ui = AttachUI()

        def _on_hydration(payload: dict) -> None:
            _render_hydration(console, payload)
            try:
                state = (payload.get("audio") or {}).get("state", "")
                if state:
                    ui.on_audio_state(str(state))
            except Exception:
                pass
            hydrated.set()

        client = CockpitAttachClient(
            on_hydration=_on_hydration, on_line=_print_line,
            on_markup=lambda t: _render_markup_frame(t, console),
            on_telemetry=ui.on_telemetry,
            on_audio_state=ui.on_audio_state,
        )
        if not await client.connect():
            console.print(_NO_ORGANISM_MESSAGE, markup=False, highlight=False)
            return 1

        try:
            _ran_cockpit = False
            if _can_run_split_plane():
                # Style-Guide cockpit is the default interactive surface; ANY
                # failure falls through to the proven split-plane loop (the
                # cockpit can never brick the attach). Kill-switch:
                # JARVIS_BIPARTITE_LAYOUT_DISABLED=1.
                _why = ""
                try:
                    from backend.core.ouroboros.battle_test.bipartite_layout import (
                        bipartite_enabled,
                        should_run_bipartite,
                    )
                    if should_run_bipartite():
                        await _bipartite_attach_loop(client, console, ui)
                        _ran_cockpit = True
                    elif not bipartite_enabled():
                        _why = "kill-switch (JARVIS_BIPARTITE_LAYOUT_DISABLED)"
                    else:
                        _why = "stdout is not a real TTY"
                except Exception as _exc:
                    _ran_cockpit = False
                    _why = f"{type(_exc).__name__}: {str(_exc)[:80]}"
                if not _ran_cockpit:
                    # Observability over silent reroute (operator law): say WHY
                    # the cockpit did not mount before falling back.
                    try:
                        console.print(
                            f"⎿ cockpit fallback → legacy view ({_why or 'unknown'})",
                            markup=False, highlight=False,
                        )
                    except Exception:
                        pass
                    await _split_plane_loop(client, console, ui)
            else:
                await _legacy_pump_loop(client)
            if not client.connected:
                console.print(
                    "⎿ organism went away — detached", markup=False,
                    highlight=False,
                )
        except KeyboardInterrupt:
            console.print(
                "⎿ detached (the organism keeps running)", markup=False,
                highlight=False,
            )
        finally:
            await client.close()
        return 0

    try:
        return asyncio.run(_session())
    except KeyboardInterrupt:
        try:
            console.print(
                "⎿ detached (the organism keeps running)", markup=False,
                highlight=False,
            )
        except Exception:
            pass
        return 0
    except Exception as exc:
        try:
            console.print(f"ov attach: failed ({exc})", markup=False)
        except Exception:
            pass
        return 1


# ---------------------------------------------------------------------------
# Thin-client cockpit — the sub-second `ov`
# ---------------------------------------------------------------------------


def run_cockpit_thin(console: Any) -> int:
    """The presentation-shell cockpit: instant crest, zero-trust
    probe, seamless attach — cold-booting a detached organism when
    none is home. The operator NEVER sees a traceback here."""
    import asyncio

    # The emblem law: the mark ALWAYS greets `ov`. With the Client-Side Boot
    # Animator (default on, real TTY) the mark is the ANIMATED Snake-and-Plus
    # crest — the green head + purple body chase a white `+` around the "V" — and
    # the wake logs stream into its bottom partition (a rich.live.Live managed
    # canvas, so async logs can never tear the emblem). Piped / disabled / tiny
    # terminals get the static mark exactly as before. Kill-switch:
    # JARVIS_CREST_ANIM_DISABLED=1.
    from backend.core.ouroboros.ui.crest_animator import build_animator
    _animator = build_animator(console)
    if _animator is None:
        try:
            from backend.core.ouroboros.ui.crest import print_static_crest
            print_static_crest(console)
        except Exception:
            pass
        console.print(version_line(), markup=False, highlight=False)

    async def _session() -> int:
        import asyncio as _aio
        from backend.core.ouroboros.cli.thin_client import ensure_daemon

        def _status(line: str) -> None:
            if _animator is not None:
                _animator.add_log(line)      # → the Live bottom partition
            else:
                try:
                    console.print(line, markup=False, highlight=False)
                except Exception:
                    pass

        if _animator is not None:
            # Play the chase while the daemon wakes; on daemon-up the Live exits
            # (freezing the final crest frame) and the warm attach surface prints
            # below it — seamless handoff to the interactive prompt.
            _animator.add_log(version_line())
            _stop = _aio.Event()
            _ok = {"v": False}

            async def _boot() -> None:
                try:
                    _ok["v"] = await ensure_daemon(on_status=_status)
                finally:
                    _stop.set()

            _boot_task = _aio.ensure_future(_boot())
            try:
                await _animator.play(console, stop_event=_stop)
            except Exception:
                pass
            try:
                await _boot_task
            except Exception:
                pass
            ok = _ok["v"]
        else:
            ok = await ensure_daemon(on_status=_status)

        if not ok:
            # Print below the frozen crest (add_log would land after Live exit).
            try:
                console.print(
                    "⚠ the organism did not come up — `ov daemon` in another "
                    "terminal shows the full boot, or check the daemon log.",
                    markup=False, highlight=False,
                )
            except Exception:
                pass
            return 1
        return 0

    try:
        rc = asyncio.run(_session())
    except KeyboardInterrupt:
        console.print(
            "⎿ cancelled — any background ignition continues; `ov` again "
            "to attach", markup=False, highlight=False,
        )
        return 0
    except Exception:
        return 1
    if rc != 0:
        return rc
    # Warm path from here — identical surface to `ov attach` (DRY:
    # same hydration card, same split-plane, same PresentationRouter-
    # conformed stream, same audio verbs).
    return run_attach(console)


# ---------------------------------------------------------------------------
# ov system — the System Observability Panel (Slice G)
# ---------------------------------------------------------------------------


def run_system(console: Any) -> int:
    """Attach the async System Observability cockpit to the running headless
    daemon over the Cockpit Attach UDS. Passive listener; graceful reconnect on
    daemon restart. NEVER raises out to the terminal."""
    import asyncio
    try:
        from backend.core.ouroboros.cli.ov_system_panel import run_system_panel
    except Exception as exc:  # noqa: BLE001
        try:
            console.print(f"ov system unavailable: {exc}", markup=False)
        except Exception:  # noqa: BLE001
            pass
        return 1
    try:
        return asyncio.run(run_system_panel(console=console))
    except KeyboardInterrupt:
        return 0
    except Exception:  # noqa: BLE001
        return 1


def run_hive(console: Any) -> int:
    """Attach the live Agent Hive feed to the running headless daemon over the
    Cockpit Attach UDS — a read-only, chronological projection of the real O+V
    pipeline (Trinity + IDE SSE fabrics, unified by the Hive Aggregator).
    Passive listener; graceful reconnect. NEVER raises out to the terminal."""
    import asyncio
    try:
        from backend.core.ouroboros.cli.ov_hive_panel import run_hive_panel
    except Exception as exc:  # noqa: BLE001
        try:
            console.print(f"ov hive unavailable: {exc}", markup=False)
        except Exception:  # noqa: BLE001
            pass
        return 1
    try:
        return asyncio.run(run_hive_panel(console=console))
    except KeyboardInterrupt:
        return 0
    except Exception:  # noqa: BLE001
        return 1


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: Optional[Sequence[str]] = None) -> int:
    """``ov`` entry point (the ``[project.scripts]`` target).

    Returns a process exit code. Boot actions delegate into the shared
    battle-test bootstrap; status/attach/help are handled locally without
    booting the organism.
    """
    inv = resolve(sys.argv[1:] if argv is None else list(argv))
    console = build_console()

    if inv.action == "help":
        console.print(_HELP_TEXT, markup=False, highlight=False)
        return 0
    if inv.action == "version":
        console.print(version_line(), markup=False, highlight=False)
        return 0
    if inv.action == "attach":
        return run_attach(console)
    if inv.action == "system":
        return run_system(console)
    if inv.action == "hive":
        return run_hive(console)
    if inv.action == "doctor":
        try:
            from backend.core.ouroboros.cli.ov_doctor import run_doctor
        except Exception as exc:  # noqa: BLE001
            console.print(f"ov doctor unavailable: {exc}", markup=False)
            return 1
        known = {"--live"}
        for arg in inv.delegate_argv:
            if arg not in known:
                hint = next((k for k in known
                             if k.startswith(arg) or arg.startswith(k)), None)
                console.print(
                    f"unknown flag {arg!r}"
                    + (f" — did you mean {hint!r}?" if hint else
                       f" (known: {', '.join(sorted(known))})"),
                    markup=False, highlight=False)
                return 64          # EX_USAGE — refuse, never silently ignore
        return run_doctor(console, live="--live" in inv.delegate_argv)
    if inv.action == "status":
        console.print(status_digest(), markup=False, highlight=False)
        return 0
    if inv.action in ("daemon_install", "daemon_uninstall"):
        from backend.core.ouroboros.cli.thin_client import (
            install_agent,
            uninstall_agent,
        )
        msg = (
            install_agent() if inv.action == "daemon_install"
            else uninstall_agent()
        )
        console.print(msg, markup=False, highlight=False)
        return 0

    # ── Thin-Client Split (operator-authorized 2026-07-18) ──────────
    # Bare `ov` is a PRESENTATION SHELL: crest + zero-trust probe +
    # attach. The organism runs in a separate execution boundary
    # (detached daemon), so the prompt is sub-second regardless of
    # domain-layer boot cost. `--legacy-boot` (or the env master off)
    # restores the in-process organism below.
    if inv.action == "cockpit" and "--legacy-boot" not in inv.delegate_argv:
        from backend.core.ouroboros.cli.thin_client import thin_client_enabled
        if thin_client_enabled():
            return run_cockpit_thin(console)

    # cockpit / headless -> the one shared bootstrap (DRY). The facade's ONLY
    # added responsibility: declare the presentation skin (spec §3.4).
    from backend.core.ouroboros.ui.presentation_mode import ENV_KEY, PresentationMode

    os.environ[ENV_KEY] = (
        PresentationMode.COCKPIT.value if inv.action == "cockpit"
        else PresentationMode.SOAK.value
    )

    # Cinematic Boot Mux (COCKPIT only): silence the TTY structurally
    # BEFORE the chatty bootstrap import chain runs. The awakening (or
    # the single-flight collision surface) releases it; a fatal boot
    # flushes the hidden buffer (Dead-Man's Switch) so forensics
    # survive the ambition.
    _mux_engaged = False
    if inv.action == "cockpit":
        try:
            from backend.core.ouroboros.ui.boot_mux import engage_boot_mux
            _mux_engaged = engage_boot_mux()
        except Exception:  # noqa: BLE001 — degrade to the noisy legacy boot
            _mux_engaged = False

    try:
        from scripts.ouroboros_battle_test import main as battle_main
        battle_main(inv.delegate_argv)
        return 0
    except SystemExit as exc:
        # 75 (EX_TEMPFAIL) is the single-flight collision — an EXPECTED
        # presentation outcome whose surface already released the mux
        # cleanly; only genuinely-unexpected nonzero exits flush.
        if _mux_engaged and exc.code not in (0, None, 75):
            _deadman_flush()
        raise
    except BaseException as exc:
        if _mux_engaged:
            _deadman_flush()
        console.print(
            f"ov: fatal during boot ({type(exc).__name__}: {exc}) — "
            "buffered logs flushed above",
            markup=False,
        )
        raise


def _deadman_flush() -> None:
    """Dead-Man's Switch — NEVER raises."""
    try:
        from backend.core.ouroboros.ui.boot_mux import release_boot_mux
        release_boot_mux(flush_to_tty=True)
    except Exception:  # noqa: BLE001
        pass


if __name__ == "__main__":
    raise SystemExit(main())
