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

_VERBS = {"cockpit", "run", "daemon", "status", "attach", "system", "version"}
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
        return f" ov attach — organism live{audio} · 'detach' to leave"

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
            text = (line or "").strip()
            low = text.lower()
            if low in ("detach", "exit", "quit"):
                break
            # Audio Control Plane verbs — routed on the audio lane,
            # never as chat text (the daemon synapse owns the duplex).
            if low in ("wake", "voice", "listen"):
                client.send_audio("wake")
                continue
            if low in ("wake!", "force-wake", "force wake"):
                client.send_audio("force_wake")
                continue
            if low == "ptt":
                client.send_audio("ptt")
                continue
            if low in ("ptt stop", "ptt-stop", "ptt off"):
                client.send_audio("ptt_stop")
                continue
            if low in ("flush", "shh", "hush"):
                client.send_audio("flush")
                continue
            if low in ("mute", "sleep"):
                client.send_audio("sleep")
                continue
            if low == "barge":
                client.send_audio("barge")
                continue
            if text:
                # TTS interruption (ducking): a new operator command
                # while Karen is composing/speaking flushes her
                # outbound buffer FIRST — the human always owns the
                # floor.
                if ui.should_flush_on_input():
                    client.send_audio("flush")
                client.send_input(text)


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
            # Builtin print() resolves sys.stdout DYNAMICALLY — under the
            # split-plane's patch_stdout this routes daemon telemetry
            # above the pinned prompt; the pre-bound Rich console would
            # bypass the patch and corrupt the input line.
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
            on_audio_state=ui.on_audio_state,
        )
        if not await client.connect():
            console.print(_NO_ORGANISM_MESSAGE, markup=False, highlight=False)
            return 1

        try:
            if _can_run_split_plane():
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

    # The emblem law: the mark ALWAYS greets `ov` — instantly, before
    # any daemon work.
    try:
        from backend.core.ouroboros.ui.crest import print_static_crest
        print_static_crest(console)
    except Exception:
        pass
    console.print(version_line(), markup=False, highlight=False)

    async def _session() -> int:
        from backend.core.ouroboros.cli.thin_client import ensure_daemon

        def _status(line: str) -> None:
            try:
                console.print(line, markup=False, highlight=False)
            except Exception:
                pass

        if not await ensure_daemon(on_status=_status):
            _status(
                "⚠ the organism did not come up — `ov daemon` in another "
                "terminal shows the full boot, or check the daemon log.",
            )
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
