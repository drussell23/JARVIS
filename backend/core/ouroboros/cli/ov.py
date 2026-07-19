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

_VERBS = {"cockpit", "run", "daemon", "status", "attach"}
_HELP_TOKENS = {"help", "--help", "-h"}

_NO_ORGANISM_MESSAGE = (
    "no organism awake — nothing to attach to. Start one with `ov` "
    "(cockpit) or `ov daemon` (headless)."
)

_HELP_TEXT = """ov -- Ouroboros + Venom, autonomous engineering organism

  ov                  boot the organism + live cockpit (default)
  ov run [flags]      headless autonomous session
  ov daemon [flags]   alias for a headless run
  ov status           last-session digest (no boot)
  ov attach           attach this terminal to the running organism
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

    if tokens and tokens[0] in _VERBS:
        verb, rest = tokens[0], list(tokens[1:])
    else:
        verb, rest = "cockpit", list(tokens)

    if verb in ("run", "daemon"):
        return Invocation("headless", ["--headless", *rest])
    if verb == "status":
        return Invocation("status")
    if verb == "attach":
        return Invocation("attach")
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
            try:
                console.print(text, markup=False, highlight=False)
            except Exception:
                pass

        hydrated = asyncio.Event()

        def _on_hydration(payload: dict) -> None:
            _render_hydration(console, payload)
            hydrated.set()

        client = CockpitAttachClient(
            on_hydration=_on_hydration, on_line=_print_line,
        )
        if not await client.connect():
            console.print(_NO_ORGANISM_MESSAGE, markup=False, highlight=False)
            return 1

        # stdin pump — blocking reads off-loop; EOF or detach ends it.
        async def _stdin_pump() -> None:
            while client.connected:
                try:
                    line = await asyncio.to_thread(sys.stdin.readline)
                except Exception:
                    break
                if not line:               # EOF — operator closed stdin
                    break
                text = line.strip()
                if text and not client.send_input(text):
                    break

        pump = asyncio.get_running_loop().create_task(_stdin_pump())
        try:
            while client.connected:
                await asyncio.sleep(0.25)
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
            pump.cancel()
            try:
                await pump
            except (asyncio.CancelledError, Exception):
                pass
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
    if inv.action == "attach":
        return run_attach(console)
    if inv.action == "status":
        console.print(status_digest(), markup=False, highlight=False)
        return 0

    # cockpit / headless -> the one shared bootstrap (DRY). The facade's ONLY
    # added responsibility: declare the presentation skin (spec §3.4).
    from backend.core.ouroboros.ui.presentation_mode import ENV_KEY, PresentationMode

    os.environ[ENV_KEY] = (
        PresentationMode.COCKPIT.value if inv.action == "cockpit"
        else PresentationMode.SOAK.value
    )
    try:
        from scripts.ouroboros_battle_test import main as battle_main
    except Exception as exc:  # noqa: BLE001
        console.print(f"ov: failed to load bootstrap: {exc}", markup=False)
        return 1
    battle_main(inv.delegate_argv)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
