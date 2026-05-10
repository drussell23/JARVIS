"""Karen-voice command router (§37 Tier 2 — bidirectional voice
command surface).

The operator already has a slash-REPL surface (``/voice ...``)
plus an autonomous TTS announcer (``karen_voice_announcer.py``).
What's missing is the third leg: a **voice command** path so
the operator can say "karen mute" / "karen verbose" / "karen
status" without touching the keyboard.

This router is the closed-vocabulary classifier that sits in
front of the existing :func:`voice_repl.dispatch_voice_command`
dispatcher. Composition layout::

    on_transcript(text)                       # JarvisVoiceBridge
      ↓
    is_karen_command(text)                    # this module — closed regex
      ├── True  → dispatch_karen_voice_command(text)
      │            ↓
      │          voice_repl.dispatch_voice_command("/voice <verb>")
      │            ↓
      │          KarenVoiceCommandResult (response text + handled=True)
      └── False → existing ConversationManager path (untouched)

Architectural locks (operator mandate "no hardcoding"):

  * **Closed phrase taxonomy** — exactly the verbs in
    :data:`_KAREN_VERB_TO_REPL_VERB` are recognized. Adding a
    new phrase requires extending the table AND the AST pin.

  * **Single source of truth for env mutation** — this router
    NEVER writes JARVIS_KAREN_* env vars directly. Every
    operation routes through
    :func:`voice_repl.dispatch_voice_command` so the slash-REPL
    and voice-command paths share identical mutation logic.

  * **Master-flag-gated at the entry point** — the bridge calls
    :func:`is_karen_command` first; when the master flag is off,
    that returns False and the transcript falls through to the
    conversation manager. This keeps voice-command dispatch
    additive (graduated default-FALSE → opt-in by operator).

  * **Authority asymmetry** — voice command router MUST NOT
    import orchestrator / iron_gate / providers / etc. Pure
    presentation/dispatch layer. AST-pinned by
    :func:`register_shipped_invariants`.

  * **NEVER raises** — exception isolation is the bridge's
    contract requirement.

Master flag: ``JARVIS_KAREN_VOICE_COMMAND_ENABLED`` (default
FALSE until Phase 9 cadence graduation; 3 clean soaks).

Anti-goal (per operator note): voice MUST NOT become the
primary interface. This router is a 2nd-channel observability
surface — the visual REPL stays canonical. Therefore the
recognized vocabulary is deliberately small (mute/unmute/
verbose/normal/status) and the router does NOT engage in any
generative dialogue.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


KAREN_VOICE_COMMAND_ROUTER_SCHEMA_VERSION: str = (
    "karen_voice_command_router.1"
)


# ---------------------------------------------------------------------------
# Closed phrase taxonomy — operator verbs → /voice REPL subcommands
# ---------------------------------------------------------------------------


# Each key is a normalized operator phrase; each value is the
# corresponding /voice REPL subcommand. Composing the slash-REPL
# means these aliases inherit any future /voice behavior changes
# automatically.
_KAREN_VERB_TO_REPL_VERB: Dict[str, str] = {
    # Mute family
    "mute": "off",
    "off": "off",
    "quiet": "off",
    "shush": "off",
    # Unmute family
    "unmute": "on",
    "on": "on",
    # Density family
    "verbose": "verbose",
    "normal": "on",
    "default": "on",
    # Inspection
    "status": "status",
    "state": "status",
}


# Frozen view of recognized operator verbs. AST-pinned size 11.
_KAREN_RECOGNIZED_VERBS: frozenset = frozenset(
    _KAREN_VERB_TO_REPL_VERB.keys(),
)


# Anchored regex: leading "karen" followed by exactly one verb.
# Whitespace-tolerant. Case-insensitive at match time. Non-verb
# trailing text rejected (so "karen mute the dog" doesn't match).
_KAREN_PHRASE_RE = re.compile(
    r"^\s*karen\s+([a-z]+)\s*$",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class KarenVoiceCommandResult:
    """Outcome of a voice-command dispatch attempt.

    ``handled`` distinguishes "this was a karen command and we
    routed it" from "this was not a karen command at all" so the
    bridge can fall through to the conversation manager only on
    ``handled=False``.
    """

    handled: bool
    text: str  # Operator-facing response (TTS-spoken).
    matched_verb: str = ""
    repl_verb: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "handled": self.handled,
            "text": self.text,
            "matched_verb": self.matched_verb,
            "repl_verb": self.repl_verb,
            "schema_version": (
                KAREN_VOICE_COMMAND_ROUTER_SCHEMA_VERSION
            ),
        }


# ---------------------------------------------------------------------------
# Master flag — default-FALSE (§33 graduation contract)
# ---------------------------------------------------------------------------


def voice_command_enabled() -> bool:
    """``JARVIS_KAREN_VOICE_COMMAND_ENABLED`` (default ``false``
    until Phase 9 cadence). Empty/whitespace = unset =
    default-false. NEVER raises."""
    raw = os.environ.get(
        "JARVIS_KAREN_VOICE_COMMAND_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return False  # default-false until graduation
    return raw in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Phrase recognition
# ---------------------------------------------------------------------------


def _extract_verb(text: str) -> Tuple[str, str]:
    """Parse *text* into ``(operator_verb, repl_verb)`` if it
    matches the closed phrase taxonomy, else ``("", "")``.

    Pure function. NEVER raises."""
    if not text or not isinstance(text, str):
        return ("", "")
    try:
        m = _KAREN_PHRASE_RE.match(text)
        if m is None:
            return ("", "")
        op_verb = m.group(1).lower().strip()
        repl_verb = _KAREN_VERB_TO_REPL_VERB.get(op_verb, "")
        if not repl_verb:
            return ("", "")
        return (op_verb, repl_verb)
    except Exception:  # noqa: BLE001 — defensive
        return ("", "")


def is_karen_command(text: str) -> bool:
    """Cheap classifier — returns True iff *text* matches the
    closed Karen-voice-command phrase taxonomy AND the master
    flag is on. Bridge calls this FIRST before falling through
    to the conversation manager.

    Pure function. NEVER raises."""
    if not voice_command_enabled():
        return False
    op_verb, repl_verb = _extract_verb(text)
    return bool(op_verb and repl_verb)


# ---------------------------------------------------------------------------
# Dispatcher — composes voice_repl as the single env-mutation seam
# ---------------------------------------------------------------------------


def dispatch_karen_voice_command(
    text: str,
) -> KarenVoiceCommandResult:
    """Route *text* through the closed phrase taxonomy and into
    :func:`voice_repl.dispatch_voice_command` for the actual env
    mutation. Returns a structured result.

    NEVER raises. Master-flag-off short-circuits to
    ``KarenVoiceCommandResult(handled=False, text="")`` so the
    bridge falls through naturally.

    On success, ``text`` is a short operator-facing response
    suitable for TTS playback (e.g. "Karen muted." or "Karen is
    in verbose mode."). The response is intentionally short to
    honor the anti-goal that voice MUST NOT become a primary
    interface."""
    if not voice_command_enabled():
        return KarenVoiceCommandResult(handled=False, text="")
    op_verb, repl_verb = _extract_verb(text)
    if not op_verb or not repl_verb:
        return KarenVoiceCommandResult(handled=False, text="")
    # Compose voice_repl — single source of env-mutation truth.
    try:
        from backend.core.ouroboros.governance.voice_repl import (
            dispatch_voice_command,
        )
    except Exception:  # noqa: BLE001 — defensive
        return KarenVoiceCommandResult(
            handled=True,
            text="Voice command surface unavailable.",
            matched_verb=op_verb,
            repl_verb=repl_verb,
        )
    try:
        result = dispatch_voice_command(f"/voice {repl_verb}")
        # voice_repl returns a dispatch result with a `.text`
        # attribute (Rich-markup multi-line). The voice channel
        # wants a SHORT spoken response, so we synthesize one
        # from the matched verb rather than reading the REPL
        # output aloud (which would be visually formatted).
        spoken = _spoken_response_for(op_verb)
        return KarenVoiceCommandResult(
            handled=True,
            text=spoken,
            matched_verb=op_verb,
            repl_verb=repl_verb,
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[karen_voice_command_router] dispatch raised: %s",
            exc,
        )
        return KarenVoiceCommandResult(
            handled=True,
            text="Karen voice command failed.",
            matched_verb=op_verb,
            repl_verb=repl_verb,
        )


def _spoken_response_for(op_verb: str) -> str:
    """Short TTS response per recognized operator verb. Pure
    function. The verbs that share semantics share the response
    so the operator hears consistent feedback regardless of
    which alias they used."""
    if op_verb in ("mute", "off", "quiet", "shush"):
        return "Karen muted."
    if op_verb in ("unmute", "on"):
        return "Karen unmuted."
    if op_verb == "verbose":
        return "Karen in verbose mode."
    if op_verb in ("normal", "default"):
        return "Karen in normal mode."
    if op_verb in ("status", "state"):
        # Compose canonical voice_repl status synthesis if
        # available, otherwise short fallback.
        try:
            from backend.core.ouroboros.governance.comms import (
                karen_voice as _kv,
            )
            cfg = getattr(_kv, "KarenConfig", None)
            if cfg is not None:
                # Construct a default config to read current env
                # state. This is read-only — no mutation.
                snapshot = cfg()
                enabled = bool(getattr(snapshot, "enabled", True))
                tool_voice = bool(
                    getattr(snapshot, "tool_voice_enabled", True)
                )
                if not enabled:
                    return "Karen is fully muted."
                if not tool_voice:
                    return "Karen tool voice is off."
                return "Karen is on."
        except Exception:  # noqa: BLE001 — defensive
            pass
        return "Karen status unknown."
    return "Karen command acknowledged."


# ---------------------------------------------------------------------------
# Module-owned FlagRegistry seed
# ---------------------------------------------------------------------------


def register_flags(registry: Any) -> int:
    """Module-owned FlagSpec declaration.

    Single flag: ``JARVIS_KAREN_VOICE_COMMAND_ENABLED``,
    default-FALSE. NEVER raises."""
    try:
        from backend.core.ouroboros.governance.flag_registry import (
            Category,
            FlagSpec,
            FlagType,
            Relevance,
        )
    except ImportError:
        return 0
    try:
        spec = FlagSpec(
            name="JARVIS_KAREN_VOICE_COMMAND_ENABLED",
            type=FlagType.BOOL,
            default=False,
            description=(
                "Master switch for the Karen voice-command "
                "router (§37 Tier 2). When true, the "
                "JarvisVoiceBridge.on_transcript hook checks "
                "is_karen_command(text) FIRST; matched phrases "
                "(karen mute / karen unmute / karen verbose / "
                "karen normal / karen status and aliases) route "
                "through voice_repl.dispatch_voice_command for "
                "env mutation, bypassing the conversation "
                "manager. Closed 11-alias phrase taxonomy. "
                "Default-FALSE until Phase 9 cadence "
                "graduation (3 clean soaks). Anti-goal: voice "
                "is a 2nd channel; the slash-REPL stays "
                "canonical."
            ),
            category=Category.INTEGRATION,
            source_file=(
                "backend/core/ouroboros/governance/"
                "karen_voice_command_router.py"
            ),
            example="false",
            since="§37 Tier 2 (2026-05-10)",
            posture_relevance={
                "EXPLORE": Relevance.RELEVANT,
                "CONSOLIDATE": Relevance.RELEVANT,
                "HARDEN": Relevance.IGNORED,
                "MAINTAIN": Relevance.RELEVANT,
            },
        )
        registry.register(spec, override=True)
        return 1
    except Exception:  # noqa: BLE001 — defensive
        return 0


# ---------------------------------------------------------------------------
# Shipped-code AST invariants
# ---------------------------------------------------------------------------


def register_shipped_invariants() -> list:
    """Module-owned shipped-code AST pins.

    Three invariants:
      1. ``router_default_false`` — master flag default-false.
      2. ``phrase_taxonomy_size_eleven`` — exactly 11 operator
         verbs in :data:`_KAREN_VERB_TO_REPL_VERB`. Adding a
         12th requires extending this pin.
      3. ``no_authority_imports`` — presentation/dispatch only.

    NEVER raises."""
    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    target = (
        "backend/core/ouroboros/governance/"
        "karen_voice_command_router.py"
    )

    def _validate_default_false(_tree, source) -> tuple:
        marker = (
            'os.environ.get(\n'
            '        "JARVIS_KAREN_VOICE_COMMAND_ENABLED", "",'
        )
        if marker not in source:
            return (
                "karen_voice_command_router.voice_command_enabled "
                "must read JARVIS_KAREN_VOICE_COMMAND_ENABLED "
                "env with default-false fallback (Phase 9 "
                "cadence contract).",
            )
        if "return False  # default-false until graduation" not in source:
            return (
                "karen_voice_command_router.voice_command_enabled "
                "must explicitly comment + return False on the "
                "default branch (graduation contract).",
            )
        return ()

    def _validate_taxonomy_size(tree, _source) -> tuple:
        # Walk the AST for the _KAREN_VERB_TO_REPL_VERB dict
        # literal and count its key entries.
        try:
            import ast as _ast
        except ImportError:  # pragma: no cover
            return ()
        for node in _ast.walk(tree):
            if isinstance(node, _ast.Assign):
                # Look for `_KAREN_VERB_TO_REPL_VERB: ... = {...}`
                # OR `_KAREN_VERB_TO_REPL_VERB = {...}`.
                for tgt in node.targets:
                    if (
                        isinstance(tgt, _ast.Name)
                        and tgt.id == "_KAREN_VERB_TO_REPL_VERB"
                        and isinstance(node.value, _ast.Dict)
                    ):
                        n = len(node.value.keys)
                        if n != 11:
                            return (
                                f"_KAREN_VERB_TO_REPL_VERB "
                                f"taxonomy frozen at 11 verbs; "
                                f"found {n}.",
                            )
                        return ()
            if isinstance(node, _ast.AnnAssign):
                if (
                    isinstance(node.target, _ast.Name)
                    and node.target.id == "_KAREN_VERB_TO_REPL_VERB"
                    and isinstance(node.value, _ast.Dict)
                ):
                    n = len(node.value.keys)
                    if n != 11:
                        return (
                            f"_KAREN_VERB_TO_REPL_VERB "
                            f"taxonomy frozen at 11 verbs; "
                            f"found {n}.",
                        )
                    return ()
        return (
            "_KAREN_VERB_TO_REPL_VERB dict literal not found.",
        )

    def _validate_no_authority_imports(tree, _source) -> tuple:
        try:
            import ast as _ast
        except ImportError:  # pragma: no cover
            return ()
        forbidden_modules = frozenset({
            "backend.core.ouroboros.governance.orchestrator",
            "backend.core.ouroboros.governance.iron_gate",
            "backend.core.ouroboros.governance.providers",
            "backend.core.ouroboros.governance.candidate_generator",
            "backend.core.ouroboros.governance.urgency_router",
            "backend.core.ouroboros.governance.sensor_governor",
            "backend.core.ouroboros.governance.tool_executor",
            "backend.core.ouroboros.governance.change_engine",
            "backend.core.ouroboros.governance.strategic_direction",
        })
        for node in _ast.walk(tree):
            if isinstance(node, _ast.ImportFrom):
                mod = node.module or ""
                if mod in forbidden_modules:
                    return (
                        f"karen_voice_command_router authority "
                        f"asymmetry violated: imports forbidden "
                        f"module {mod!r}.",
                    )
            elif isinstance(node, _ast.Import):
                for alias in node.names:
                    mod = alias.name or ""
                    if mod in forbidden_modules:
                        return (
                            f"karen_voice_command_router authority "
                            f"asymmetry violated: imports forbidden "
                            f"module {mod!r}.",
                        )
        return ()

    return [
        ShippedCodeInvariant(
            invariant_name="karen_voice_command_router_default_false",
            target_file=target,
            description=(
                "Master flag JARVIS_KAREN_VOICE_COMMAND_ENABLED "
                "must default-false until Phase 9 cadence."
            ),
            validate=_validate_default_false,
        ),
        ShippedCodeInvariant(
            invariant_name="karen_voice_command_phrase_taxonomy_frozen",
            target_file=target,
            description=(
                "Closed 11-alias operator-verb taxonomy frozen "
                "at the dict literal; extending requires bumping "
                "the AST pin and the regression spine."
            ),
            validate=_validate_taxonomy_size,
        ),
        ShippedCodeInvariant(
            invariant_name="karen_voice_command_no_authority_imports",
            target_file=target,
            description=(
                "Presentation/dispatch layer only: must not "
                "import orchestrator / iron_gate / providers etc."
            ),
            validate=_validate_no_authority_imports,
        ),
    ]


__all__ = [
    "KAREN_VOICE_COMMAND_ROUTER_SCHEMA_VERSION",
    "KarenVoiceCommandResult",
    "voice_command_enabled",
    "is_karen_command",
    "dispatch_karen_voice_command",
    "register_flags",
    "register_shipped_invariants",
]
