"""``/orchestra`` REPL — §39 Tier-7 operator surface
(PRD v2.75 to v2.76, 2026-05-09).

Auto-discovered by §32.11 Slice 4 ``repl_dispatch_registry``
via §33.3 naming-cage convention.

Subcommands:
  /orchestra              alias for ``recent``
  /orchestra recent [N]   last N audio cues
  /orchestra status       per-intensity + per-note distribution
  /orchestra cue <phase>  manually emit a cue for testing
  /orchestra help         this text
"""
from __future__ import annotations

import logging
import shlex
from dataclasses import dataclass
from typing import Any, List

logger = logging.getLogger(__name__)


ORCHESTRA_REPL_SCHEMA_VERSION: str = "orchestra_repl.1"


_HELP = (
    "/orchestra — §39 Tier-7 #20 phase orchestra (PRD)\n"
    "\n"
    "Maps each canonical 11-phase forward-flow phase to a\n"
    "musical cue (note + intensity) for downstream audio\n"
    "consumers. Substrate is producer-only — actual audio\n"
    "playback is downstream (TUI/IDE/Karen voice).\n"
    "\n"
    "Notes (8-value solfège octave):\n"
    "  do · re · mi · fa · sol · la · ti · do2\n"
    "\n"
    "Intensity (4 dynamics):\n"
    "  ♩ whisper · ♪ soft · ♫ normal · ♬ forte\n"
    "\n"
    "Subcommands:\n"
    "  /orchestra                       alias for recent\n"
    "  /orchestra recent [N]            last N cues\n"
    "  /orchestra status                full distribution\n"
    "  /orchestra cue <phase>           manual emit (test)\n"
    "  /orchestra help                  this text\n"
    "\n"
    "Master flag: JARVIS_PHASE_ORCHESTRA_ENABLED "
    "(default false).\n"
    "ASCII-bell on cue: JARVIS_PHASE_ORCHESTRA_BELL_ENABLED "
    "(opt-in audio).\n"
)


@dataclass(frozen=True)
class OrchestraReplDispatchResult:
    ok: bool
    text: str
    matched: bool = True
    schema_version: str = ORCHESTRA_REPL_SCHEMA_VERSION


def _matches(line: str) -> bool:
    s = (line or "").strip()
    if not s:
        return False
    return (
        s == "/orchestra"
        or s == "orchestra"
        or s.startswith("/orchestra ")
        or s.startswith("orchestra ")
    )


def _master_enabled() -> bool:
    try:
        from backend.core.ouroboros.governance.phase_orchestra import (  # noqa: E501
            master_enabled,
        )
        return bool(master_enabled())
    except Exception:  # noqa: BLE001
        return False


def dispatch_orchestra_command(
    line: str,
) -> OrchestraReplDispatchResult:
    if not _matches(line):
        return OrchestraReplDispatchResult(
            ok=False, text="", matched=False,
        )
    try:
        tokens = shlex.split(line)
    except ValueError as exc:
        return OrchestraReplDispatchResult(
            ok=False,
            text=f"  /orchestra parse error: {exc}",
        )
    args = tokens[1:] if tokens else []
    head = (args[0].lower() if args else "recent")

    if head in ("help", "?"):
        return OrchestraReplDispatchResult(
            ok=True, text=_HELP,
        )

    if not _master_enabled():
        return OrchestraReplDispatchResult(
            ok=False,
            text=(
                "  /orchestra: phase orchestra disabled "
                "(default per §33.1). Set "
                "JARVIS_PHASE_ORCHESTRA_ENABLED=true."
            ),
        )

    try:
        if head == "recent":
            return _render_recent(args[1:])
        if head == "status":
            return _render_status()
        if head == "cue":
            return _render_cue(args[1:])
        return OrchestraReplDispatchResult(
            ok=False,
            text=(
                f"  /orchestra: unknown subcommand "
                f"{head!r}. Try /orchestra help."
            ),
        )
    except Exception as exc:  # noqa: BLE001
        return OrchestraReplDispatchResult(
            ok=False,
            text=(
                f"  /orchestra: internal error: "
                f"{type(exc).__name__}: {str(exc)[:200]}"
            ),
        )


def _parse_int(args: List[str], *, default: int) -> int:
    if not args:
        return default
    try:
        n = int(args[0])
        return max(1, min(n, 64))
    except (TypeError, ValueError):
        return default


def _render_recent(
    args: List[str],
) -> OrchestraReplDispatchResult:
    from backend.core.ouroboros.governance.phase_orchestra import (
        format_orchestra_recent,
    )
    n = _parse_int(args, default=12)
    out = format_orchestra_recent(limit=n)
    if not out:
        return OrchestraReplDispatchResult(
            ok=True,
            text="# /orchestra — (empty)",
        )
    return OrchestraReplDispatchResult(ok=True, text=out)


def _render_status() -> OrchestraReplDispatchResult:
    from backend.core.ouroboros.governance.phase_orchestra import (
        bell_on_cue_enabled, format_orchestra_status,
        get_default_ledger, master_enabled,
    )
    parts = ["# /orchestra status"]
    parts.append(f"  master_enabled    : {master_enabled()}")
    parts.append(
        f"  bell_on_cue       : {bell_on_cue_enabled()}"
    )
    cues = get_default_ledger().recent(limit=512)
    parts.append(f"  cues recorded     : {len(cues)}")
    if cues:
        out = format_orchestra_status()
        if out:
            parts.append("")
            parts.append(out)
    return OrchestraReplDispatchResult(
        ok=True, text="\n".join(parts),
    )


def _render_cue(
    args: List[str],
) -> OrchestraReplDispatchResult:
    if not args:
        return OrchestraReplDispatchResult(
            ok=False,
            text="  /orchestra cue: phase name required",
        )
    from backend.core.ouroboros.governance.phase_orchestra import (
        emit_cue,
    )
    phase_name = args[0]
    cue = emit_cue(phase=phase_name, op_id="repl-test")
    if cue is None:
        return OrchestraReplDispatchResult(
            ok=False,
            text=(
                f"  /orchestra cue: phase {phase_name!r} "
                "not in canonical forward-flow"
            ),
        )
    return OrchestraReplDispatchResult(
        ok=True,
        text=(
            f"# /orchestra cue — emitted\n"
            f"  phase    : {cue.phase_name}\n"
            f"  index    : {cue.phase_index}\n"
            f"  note     : {cue.note.value}\n"
            f"  intensity: {cue.intensity.value}"
        ),
    )


def register_verbs(registry: Any) -> int:  # noqa: ANN001
    if registry is None:
        return 0
    try:
        registry.register(
            verb="orchestra",
            description=(
                "Phase orchestra — audio cue events per "
                "phase transition"
            ),
            help_text=_HELP,
            source_file=(
                "backend/core/ouroboros/governance/"
                "orchestra_repl.py"
            ),
        )
        return 1
    except Exception:  # noqa: BLE001
        return 0


__all__ = [
    "ORCHESTRA_REPL_SCHEMA_VERSION",
    "OrchestraReplDispatchResult",
    "dispatch_orchestra_command",
    "register_verbs",
]
