"""``/voice`` REPL dispatcher — Karen's voice operator surface
(PRD §38 Slice 3, 2026-05-07).

Auto-discovered by §32.11 Slice 4 ``repl_dispatch_registry``
via the §33.3 naming-cage convention:

  * file ends ``_repl.py`` → verb derived from basename
  * exposes module-level ``dispatch_voice_command(line) ->
    VoiceReplDispatchResult``
  * SerpentREPL routes any line matching ``/voice`` /
    ``voice`` / ``/voice …`` / ``voice …`` here zero-edit.

## Subcommands

  * ``/voice``                 alias for ``status``
  * ``/voice status``          mute state + cooldown + tier +
                               recent count
  * ``/voice on`` / ``unmute`` clear manual mute
  * ``/voice off`` / ``mute``  set manual mute
  * ``/voice tier <name>``     set min tier
                               (critical/important/normal)
  * ``/voice cooldown <N>``    set cooldown seconds
  * ``/voice persona <name>``  switch persona
  * ``/voice recent [N]``      last N announcements
  * ``/voice interrupt``       force-stop any in-flight TTS
  * ``/voice help``            this text

## Architectural locks (operator mandate, AST-pinned)

  1. **Composes canonical announcer** — invokes
     :class:`KarenVoiceAnnouncer` singleton via
     :func:`get_default_announcer`. NO parallel mute state /
     announcement log / TTS path.
  2. **Read-only for /status + /recent** — operator-facing
     query verbs MUST NOT mutate announcer state.
  3. **Master-flag-bypass for /help** — discoverability path
     always works regardless of master flag.
  4. **Authority asymmetry** — imports stdlib +
     karen_voice_announcer ONLY. NEVER imports orchestrator /
     iron_gate / policy / providers / candidate_generator /
     change_engine / semantic_guardian.
  5. **NEVER raises** — every subcommand defensive; exceptions
     surface as a non-ok result, never propagate.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shlex
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


VOICE_REPL_SCHEMA_VERSION: str = "voice_repl.1"


_HELP = (
    "/voice — Karen's voice announcer (PRD §38 Slice 3)\n"
    "\n"
    "Karen narrates O+V's autonomous activity (sensors / "
    "graduations / cost warnings).\n"
    "Operator can interrupt Karen mid-sentence by speaking "
    "(automatic barge-in via canonical voice pipeline).\n"
    "\n"
    "Subcommands:\n"
    "  /voice                     alias for /voice status\n"
    "  /voice status              mute state + tier + recent\n"
    "  /voice on   | unmute       clear manual mute\n"
    "  /voice off  | mute         set manual mute\n"
    "  /voice tier <name>         critical | important | normal\n"
    "  /voice cooldown <N>        per-op cooldown (seconds)\n"
    "  /voice persona <name>      karen | friday | jarvis | custom\n"
    "  /voice recent [N]          last N announcements\n"
    "  /voice interrupt           force-stop in-flight TTS\n"
    "  /voice help                this text\n"
    "\n"
    "Master flag: JARVIS_KAREN_VOICE_ENABLED (default false).\n"
    "Auto-mute: headless / CI / non-TTY contexts (always).\n"
)


@dataclass(frozen=True)
class VoiceReplDispatchResult:
    """Result of a ``/voice`` dispatch. Frozen for safe
    propagation. ``matched=False`` signals the line wasn't a
    ``/voice`` invocation."""

    ok: bool
    text: str
    matched: bool = True
    schema_version: str = VOICE_REPL_SCHEMA_VERSION


def _matches(line: str) -> bool:
    s = (line or "").strip()
    if not s:
        return False
    return (
        s == "/voice"
        or s == "voice"
        or s.startswith("/voice ")
        or s.startswith("voice ")
    )


def dispatch_voice_command(
    line: str,
) -> VoiceReplDispatchResult:
    """Control Karen's voice — status, mute and announcements.

    Parse a ``/voice`` line and dispatch. NEVER raises."""
    if not _matches(line):
        return VoiceReplDispatchResult(
            ok=False, text="", matched=False,
        )
    try:
        tokens = shlex.split(line)
    except ValueError as exc:
        return VoiceReplDispatchResult(
            ok=False,
            text=f"  /voice parse error: {exc}",
        )
    args = tokens[1:] if tokens else []
    head = (args[0].lower() if args else "status")

    # /help bypasses master-flag check.
    if head in ("help", "?"):
        return VoiceReplDispatchResult(
            ok=True, text=_HELP,
        )

    try:
        if head == "status":
            return _render_status()
        if head in ("on", "unmute"):
            return _set_mute(False)
        if head in ("off", "mute"):
            return _set_mute(True)
        if head == "tier":
            return _set_tier(
                args[1] if len(args) > 1 else "",
            )
        if head == "cooldown":
            return _set_cooldown(
                args[1] if len(args) > 1 else "",
            )
        if head == "persona":
            return _set_persona(
                args[1] if len(args) > 1 else "",
            )
        if head == "recent":
            return _render_recent(
                _parse_limit(args, default=10, ceiling=200),
            )
        if head == "interrupt":
            return _force_interrupt()
        return VoiceReplDispatchResult(
            ok=False,
            text=(
                f"  /voice: unknown subcommand {head!r}. "
                f"Try /voice help."
            ),
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        return VoiceReplDispatchResult(
            ok=False,
            text=(
                f"  /voice: internal error: "
                f"{type(exc).__name__}: {str(exc)[:200]}"
            ),
        )


def _parse_limit(args, *, default, ceiling) -> int:
    if len(args) < 2:
        return default
    try:
        n = int(args[1])
        if n < 1:
            return 1
        if n > ceiling:
            return ceiling
        return n
    except (TypeError, ValueError):
        return default


def _announcer():
    from backend.core.ouroboros.governance.karen_voice_announcer import (  # noqa: E501
        get_default_announcer,
    )
    return get_default_announcer()


def _render_status() -> VoiceReplDispatchResult:
    a = _announcer()
    s = a.status()
    parts = ["# /voice status"]
    parts.append(f"  master_enabled    : {s['master_enabled']}")
    parts.append(f"  is_muted          : {s['is_muted']}")
    parts.append(f"    manual_mute     : {s['mute_manual']}")
    parts.append(
        f"    auto_mute_active: {s['auto_mute_active']}"
    )
    parts.append(f"  min_tier          : {s['min_tier']}")
    parts.append(f"  cooldown_s        : {s['cooldown_s']}")
    parts.append(f"  persona           : {s['persona']}")
    parts.append(f"  tts_voice         : {s['tts_voice']}")
    parts.append(f"  tts_rate (wpm)    : {s['tts_rate']}")
    parts.append(
        f"  recent count      : {s['recent_announcements']}"
    )
    return VoiceReplDispatchResult(
        ok=True, text="\n".join(parts),
    )


def _set_mute(on: bool) -> VoiceReplDispatchResult:
    """Silence, at the scope the operator actually meant. NEVER raises.

    This used to flip ONE announcer's in-process flag and reply "Karen
    muted." It did not touch the other six speech paths
    (`voice_orchestrator`, `shared_voice_client`, `cross_repo_voice_client`,
    the TTS engine base class), did not survive a restart, and could not
    reach any other process. So an operator typed `/voice mute`, was told
    it worked, and kept hearing her — a false confirmation, which is worse
    than having no verb at all.

    Now it does both: the announcer flag AND the durable sentinel that
    every speech path consults. The reply states what was actually
    achieved rather than asserting a result it cannot verify.
    """
    announcer_ok = False
    try:
        _announcer().set_mute(on=on)
        announcer_ok = True
    except Exception:  # noqa: BLE001
        pass

    sentinel = ""
    cleared = 0
    try:
        from backend.core.voice_mute import mute as _mute, unmute as _unmute
        if on:
            sentinel = _mute()
        else:
            cleared = _unmute()
    except Exception:  # noqa: BLE001
        pass

    if on:
        # The sentinel is what makes this durable and process-wide, so its
        # absence is the part worth reporting — an operator told "muted"
        # who is still hearing her needs to know which half failed.
        if sentinel:
            text = ("  /voice: muted — every speech path, and it survives "
                    "a restart.")
        elif announcer_ok:
            text = ("  /voice: Karen's announcer muted, but the durable "
                    "sentinel could NOT be written — other speech paths "
                    "may still talk, and this will not survive a restart.")
        else:
            text = "  /voice: could not mute — nothing changed."
        return VoiceReplDispatchResult(ok=bool(sentinel), text=text)

    freed = f" ({cleared} sentinel{'s' if cleared != 1 else ''} cleared)" if cleared else ""
    return VoiceReplDispatchResult(
        ok=True, text=f"  /voice: unmuted{freed}.",
    )


def _set_tier(name: str) -> VoiceReplDispatchResult:
    candidate = (name or "").strip().lower()
    if candidate not in ("critical", "important", "normal"):
        return VoiceReplDispatchResult(
            ok=False,
            text=(
                f"  /voice tier: invalid {name!r}. Choose "
                f"critical | important | normal."
            ),
        )
    os.environ["JARVIS_KAREN_VOICE_MIN_TIER"] = candidate
    return VoiceReplDispatchResult(
        ok=True,
        text=f"  /voice: min tier set to {candidate}.",
    )


def _set_cooldown(value: str) -> VoiceReplDispatchResult:
    try:
        n = float(value)
        if n < 0 or n > 3600:
            raise ValueError("out of range")
    except (TypeError, ValueError):
        return VoiceReplDispatchResult(
            ok=False,
            text=(
                f"  /voice cooldown: invalid {value!r}. "
                f"Choose 0..3600 seconds."
            ),
        )
    os.environ["JARVIS_KAREN_VOICE_COOLDOWN_S"] = str(n)
    return VoiceReplDispatchResult(
        ok=True,
        text=f"  /voice: cooldown set to {n:.1f}s.",
    )


def _set_persona(name: str) -> VoiceReplDispatchResult:
    candidate = (name or "").strip().lower()
    valid = ("karen", "friday", "jarvis", "custom")
    if candidate not in valid:
        return VoiceReplDispatchResult(
            ok=False,
            text=(
                f"  /voice persona: invalid {name!r}. Choose "
                f"karen | friday | jarvis | custom."
            ),
        )
    os.environ["JARVIS_KAREN_VOICE_PERSONA"] = candidate
    return VoiceReplDispatchResult(
        ok=True,
        text=f"  /voice: persona set to {candidate}.",
    )


def _render_recent(limit: int) -> VoiceReplDispatchResult:
    a = _announcer()
    items = a.recent(limit=limit)
    if not items:
        return VoiceReplDispatchResult(
            ok=True,
            text="# /voice recent — no announcements yet",
        )
    parts = [f"# /voice recent — last {len(items)}"]
    for ann in items:
        marker = " (interrupted)" if ann.interrupted else ""
        parts.append(
            f"  [{ann.tier.value}] {ann.text}{marker}"
        )
    return VoiceReplDispatchResult(
        ok=True, text="\n".join(parts),
    )


def _force_interrupt() -> VoiceReplDispatchResult:
    a = _announcer()
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(a.force_interrupt())
    except RuntimeError:
        # No running loop — fall back to sync run.
        try:
            asyncio.run(a.force_interrupt())
        except Exception:  # noqa: BLE001 — defensive
            pass
    return VoiceReplDispatchResult(
        ok=True,
        text=(
            "  /voice: interrupt signaled — Karen will "
            "stop mid-sentence."
        ),
    )


def register_verbs(registry: Any) -> int:  # noqa: ANN001
    """Register the ``/voice`` verb with the help-dispatcher."""
    if registry is None:
        return 0
    try:
        registry.register(
            verb="voice",
            description=(
                "Karen's voice announcer — mute, status, "
                "interrupt, persona, tier, cooldown"
            ),
            help_text=_HELP,
            source_file=(
                "backend/core/ouroboros/governance/voice_repl.py"
            ),
        )
        return 1
    except Exception:  # noqa: BLE001 — defensive
        try:
            logger.debug(
                "[voice_repl] register_verbs swallowed",
            )
        except Exception:  # noqa: BLE001
            pass
        return 0


__all__ = [
    "VOICE_REPL_SCHEMA_VERSION",
    "VoiceReplDispatchResult",
    "dispatch_voice_command",
    "register_verbs",
]
