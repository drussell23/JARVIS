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
    ov attach          (follow-up sprint) attach to a running organism
    ov help            usage

Everything after the verb forwards verbatim to the bootstrap, e.g.
``ov run --cost-cap 2.00 -v`` -> ``main(["--headless", "--cost-cap", "2.00", "-v"])``.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Sequence

from backend.core.ouroboros.ui.theme import build_console

_VERBS = {"cockpit", "run", "daemon", "status", "attach"}
_HELP_TOKENS = {"help", "--help", "-h"}

_ATTACH_MESSAGE = (
    "ov attach (detach / reattach to a running organism) lands in a follow-up "
    "sprint. Use `ov` for the live cockpit, or `ov daemon` to run headless."
)

_HELP_TEXT = """ov -- Ouroboros + Venom, autonomous engineering organism

  ov                  boot the organism + live cockpit (default)
  ov run [flags]      headless autonomous session
  ov daemon [flags]   alias for a headless run
  ov status           last-session digest (no boot)
  ov attach           (coming soon) attach to a running organism
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
        return Invocation("attach", message=_ATTACH_MESSAGE)
    # cockpit (explicit or defaulted)
    return Invocation("cockpit", rest)


# ---------------------------------------------------------------------------
# ov status
# ---------------------------------------------------------------------------


def _default_status_provider() -> Optional[str]:
    """Best-effort one-line digest of the most recent session.

    Reads the existing :class:`LastSessionSummary` (authority-free, read-only).
    Returns ``None`` when there is no prior session or the digest is
    unavailable -- callers render a friendly fallback. NEVER raises.
    """
    try:
        from backend.core.ouroboros.governance.last_session_summary import (
            get_default_summary,
        )

        text = get_default_summary().format_for_prompt_sync()
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
        console.print(inv.message, markup=False, highlight=False)
        return 0
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
