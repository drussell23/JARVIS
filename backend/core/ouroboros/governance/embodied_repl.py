"""``/embodied`` REPL — §39 Tier-5 operator surface
(PRD v2.74 to v2.75, 2026-05-09).

Auto-discovered by §32.11 Slice 4 ``repl_dispatch_registry``
via §33.3 naming-cage convention.

Combined operator surface for the four Tier-5 embodied
modules — sister surfaces under one verb.

Subcommands:
  /embodied                      alias for ``help``
  /embodied arch                 8-zone architecture viz (#5)
  /embodied aura                 confidence aura summary (#15)
  /embodied attention            attention mirror (#16)
  /embodied portrait             procedural ASCII face (#17)
  /embodied all                  render all 4 surfaces stacked
  /embodied status               master flags
  /embodied help                 this text
"""
from __future__ import annotations

import logging
import shlex
from dataclasses import dataclass
from typing import Any, List

logger = logging.getLogger(__name__)


EMBODIED_REPL_SCHEMA_VERSION: str = "embodied_repl.1"


_HELP = (
    "/embodied — §39 Tier-5 embodied surfaces (PRD)\n"
    "\n"
    "Four sister read-only surfaces:\n"
    "  - 🧬 arch       : 8-zone organism viz (#5)\n"
    "  - 🌈 aura       : confidence aura (#15)\n"
    "  - 🪞 attention  : attention mirror (#16)\n"
    "  - 🎭 portrait   : procedural ASCII face (#17)\n"
    "\n"
    "Subcommands:\n"
    "  /embodied arch          8-zone architecture viz\n"
    "  /embodied aura          confidence aura summary\n"
    "  /embodied attention     attention focus mirror\n"
    "  /embodied portrait      procedural face\n"
    "  /embodied all           render all 4 surfaces stacked\n"
    "  /embodied status        master flags\n"
    "  /embodied help          this text\n"
    "\n"
    "Master flags:\n"
    "  JARVIS_ARCHITECTURE_VIZ_ENABLED         (default false)\n"
    "  JARVIS_CONFIDENCE_AURA_ENABLED          (default false)\n"
    "  JARVIS_ATTENTION_MIRROR_ENABLED         (default false)\n"
    "  JARVIS_PROCEDURAL_PORTRAIT_ENABLED      (default false)\n"
)


@dataclass(frozen=True)
class EmbodiedReplDispatchResult:
    ok: bool
    text: str
    matched: bool = True
    schema_version: str = EMBODIED_REPL_SCHEMA_VERSION


def _matches(line: str) -> bool:
    s = (line or "").strip()
    if not s:
        return False
    return (
        s == "/embodied"
        or s == "embodied"
        or s.startswith("/embodied ")
        or s.startswith("embodied ")
    )


def dispatch_embodied_command(
    line: str,
) -> EmbodiedReplDispatchResult:
    if not _matches(line):
        return EmbodiedReplDispatchResult(
            ok=False, text="", matched=False,
        )
    try:
        tokens = shlex.split(line)
    except ValueError as exc:
        return EmbodiedReplDispatchResult(
            ok=False,
            text=f"  /embodied parse error: {exc}",
        )
    args = tokens[1:] if tokens else []
    head = (args[0].lower() if args else "help")

    if head in ("help", "?", ""):
        return EmbodiedReplDispatchResult(
            ok=True, text=_HELP,
        )
    if head == "status":
        return _render_status()

    try:
        if head == "arch":
            return _render_arch()
        if head == "aura":
            return _render_aura()
        if head == "attention":
            return _render_attention()
        if head == "portrait":
            return _render_portrait()
        if head == "all":
            return _render_all()
        return EmbodiedReplDispatchResult(
            ok=False,
            text=(
                f"  /embodied: unknown subcommand "
                f"{head!r}. Try /embodied help."
            ),
        )
    except Exception as exc:  # noqa: BLE001
        return EmbodiedReplDispatchResult(
            ok=False,
            text=(
                f"  /embodied: internal error: "
                f"{type(exc).__name__}: {str(exc)[:200]}"
            ),
        )


def _render_arch() -> EmbodiedReplDispatchResult:
    try:
        from backend.core.ouroboros.governance.architecture_viz import (  # noqa: E501
            format_architecture_viz, master_enabled,
        )
    except Exception as exc:  # noqa: BLE001
        return EmbodiedReplDispatchResult(
            ok=False,
            text=(
                "  /embodied arch: substrate unavailable "
                f"({type(exc).__name__})"
            ),
        )
    if not master_enabled():
        return EmbodiedReplDispatchResult(
            ok=False,
            text=(
                "  /embodied arch: disabled. Set "
                "JARVIS_ARCHITECTURE_VIZ_ENABLED=true."
            ),
        )
    out = format_architecture_viz()
    if not out:
        return EmbodiedReplDispatchResult(
            ok=True, text="# /embodied arch — (empty)",
        )
    return EmbodiedReplDispatchResult(ok=True, text=out)


def _render_aura() -> EmbodiedReplDispatchResult:
    try:
        from backend.core.ouroboros.governance.confidence_aura import (  # noqa: E501
            format_aura_summary, master_enabled,
        )
    except Exception as exc:  # noqa: BLE001
        return EmbodiedReplDispatchResult(
            ok=False,
            text=(
                "  /embodied aura: substrate unavailable "
                f"({type(exc).__name__})"
            ),
        )
    if not master_enabled():
        return EmbodiedReplDispatchResult(
            ok=False,
            text=(
                "  /embodied aura: disabled. Set "
                "JARVIS_CONFIDENCE_AURA_ENABLED=true."
            ),
        )
    # No active ConfidenceTrace context here — the REPL
    # surface renders from a fresh empty trace, which yields
    # an empty snapshot. Tell operator how to use it.
    return EmbodiedReplDispatchResult(
        ok=True,
        text=(
            "# /embodied aura\n"
            "  [dim]aura is rendered per-op during GENERATE; "
            "compose ConfidenceCapturer in your op to "
            "see per-token tints.[/]"
        ),
    )


def _render_attention() -> EmbodiedReplDispatchResult:
    try:
        from backend.core.ouroboros.governance.attention_mirror import (  # noqa: E501
            format_attention_mirror, master_enabled,
        )
    except Exception as exc:  # noqa: BLE001
        return EmbodiedReplDispatchResult(
            ok=False,
            text=(
                "  /embodied attention: substrate "
                f"unavailable ({type(exc).__name__})"
            ),
        )
    if not master_enabled():
        return EmbodiedReplDispatchResult(
            ok=False,
            text=(
                "  /embodied attention: disabled. Set "
                "JARVIS_ATTENTION_MIRROR_ENABLED=true."
            ),
        )
    out = format_attention_mirror()
    if not out:
        return EmbodiedReplDispatchResult(
            ok=True,
            text="# /embodied attention — (empty)",
        )
    return EmbodiedReplDispatchResult(ok=True, text=out)


def _render_portrait() -> EmbodiedReplDispatchResult:
    try:
        from backend.core.ouroboros.governance.procedural_portrait import (  # noqa: E501
            format_portrait, master_enabled,
        )
    except Exception as exc:  # noqa: BLE001
        return EmbodiedReplDispatchResult(
            ok=False,
            text=(
                "  /embodied portrait: substrate "
                f"unavailable ({type(exc).__name__})"
            ),
        )
    if not master_enabled():
        return EmbodiedReplDispatchResult(
            ok=False,
            text=(
                "  /embodied portrait: disabled. Set "
                "JARVIS_PROCEDURAL_PORTRAIT_ENABLED=true."
            ),
        )
    out = format_portrait()
    if not out:
        return EmbodiedReplDispatchResult(
            ok=True,
            text="# /embodied portrait — (empty)",
        )
    return EmbodiedReplDispatchResult(ok=True, text=out)


def _render_all() -> EmbodiedReplDispatchResult:
    sections: List[str] = []
    for subcommand in (
        "arch", "attention", "portrait",
    ):
        # Skip aura because it requires per-op trace input.
        r = dispatch_embodied_command(
            f"/embodied {subcommand}",
        )
        if r.ok and r.text and not r.text.startswith("# "):
            sections.append(r.text)
    if not sections:
        return EmbodiedReplDispatchResult(
            ok=True,
            text=(
                "# /embodied all — no surfaces enabled "
                "(set the JARVIS_*_ENABLED flags)"
            ),
        )
    return EmbodiedReplDispatchResult(
        ok=True,
        text="\n\n".join(sections),
    )


def _render_status() -> EmbodiedReplDispatchResult:
    parts = ["# /embodied status"]
    surfaces = (
        ("architecture_viz", "ARCHITECTURE_VIZ"),
        ("confidence_aura", "CONFIDENCE_AURA"),
        ("attention_mirror", "ATTENTION_MIRROR"),
        ("procedural_portrait", "PROCEDURAL_PORTRAIT"),
    )
    for module_name, _ in surfaces:
        try:
            mod = __import__(
                f"backend.core.ouroboros.governance."
                f"{module_name}",
                fromlist=["master_enabled"],
            )
            enabled = mod.master_enabled()
            parts.append(
                f"  {module_name:<22} : {enabled}"
            )
        except Exception:  # noqa: BLE001
            parts.append(
                f"  {module_name:<22} : (unavailable)"
            )
    return EmbodiedReplDispatchResult(
        ok=True, text="\n".join(parts),
    )


def register_verbs(registry: Any) -> int:  # noqa: ANN001
    if registry is None:
        return 0
    try:
        registry.register(
            verb="embodied",
            description=(
                "Embodied surfaces — architecture viz + "
                "confidence aura + attention mirror + "
                "procedural portrait"
            ),
            help_text=_HELP,
            source_file=(
                "backend/core/ouroboros/governance/"
                "embodied_repl.py"
            ),
        )
        return 1
    except Exception:  # noqa: BLE001
        return 0


__all__ = [
    "EMBODIED_REPL_SCHEMA_VERSION",
    "EmbodiedReplDispatchResult",
    "dispatch_embodied_command",
    "register_verbs",
]
