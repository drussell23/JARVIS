"""``/keys`` REPL — the operator's window into the remappable keymap.

Auto-discovered by ``repl_dispatch_registry`` via the naming cage (file ends
``_repl.py``; verb from basename; dispatcher named
``dispatch_keys_command``). Reads everything through
``battle_test.keymap.describe_keymap()`` — this module renders, it never
resolves, so the table can never drift from what the cockpit actually binds.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

KEYS_REPL_SCHEMA_VERSION: str = "keys_repl.1"

__verb_help__ = {
    "keys": "show and manage keyboard shortcuts (keybindings.json)",
}

_HELP = (
    "/keys — keyboard shortcuts, resolved from defaults + your config\n"
    "\n"
    "Subcommands:\n"
    "  /keys              effective bindings per context\n"
    "  /keys warnings     config problems (parse errors, conflicts)\n"
    "  /keys path         where the config file lives\n"
    "  /keys init         create a starter keybindings.json\n"
    "  /keys reload       force a config re-read now\n"
    "  /keys help         this text\n"
    "\n"
    "Config format matches Claude Code's keybindings.json: blocks of\n"
    '{"context": ..., "bindings": {"ctrl+e": "namespace:action", '
    '"ctrl+u": null}}.\n'
    "Edits hot-reload on cockpit surfaces; call-site bindings apply on the\n"
    "next attach. Master flag: JARVIS_KEYMAP_ENABLED (default true).\n"
)


@dataclass(frozen=True)
class KeysReplDispatchResult:
    ok: bool
    text: str
    matched: bool = True
    schema_version: str = KEYS_REPL_SCHEMA_VERSION


def _table(info: dict) -> str:
    lines = []
    header = "keymap — {} action(s)".format(len(info.get("actions", ())))
    if not info.get("enabled", True):
        header += "  [JARVIS_KEYMAP_ENABLED=false — defaults only]"
    lines.append(header)
    path = info.get("config_path")
    if path:
        state = "loaded" if info.get("config_present") else "not present yet"
        lines.append(f"config: {path} ({state})")
    current_ctx = None
    for row in info.get("actions", ()):
        if row["context"] != current_ctx:
            current_ctx = row["context"]
            lines.append(f"\n[{current_ctx}]")
        keys = ", ".join(row["keys"]) if row["keys"] else "(unbound)"
        mark = " *" if row.get("customized") else ""
        desc = row.get("description") or ""
        lines.append(f"  {row['action']:<24} {keys:<22}{mark} {desc}".rstrip())
    warn_count = len(info.get("warnings", ()))
    if warn_count:
        lines.append(
            f"\n⚠ {warn_count} warning(s) — `/keys warnings` to list them"
        )
    if any(r.get("customized") for r in info.get("actions", ())):
        lines.append("* customized in keybindings.json")
    return "\n".join(lines)


def dispatch_keys_command(line: str) -> KeysReplDispatchResult:
    """Show and manage keyboard shortcuts.

    Operator: show current keyboard shortcuts, warnings from your
    keybindings.json, or create a starter config with `/keys init`.
    """
    try:
        from backend.core.ouroboros.battle_test import keymap

        tokens = (line or "").strip().lstrip("/").split()
        sub = tokens[1].lower() if len(tokens) > 1 else ""

        if sub in ("help", "-h", "--help"):
            return KeysReplDispatchResult(ok=True, text=_HELP)

        if sub == "path":
            path = keymap.resolve_config_path()
            return KeysReplDispatchResult(
                ok=True,
                text=(f"keybindings config: {path}"
                      if path else "no config path resolvable"),
            )

        if sub == "init":
            written = keymap.write_config_template()
            if written:
                return KeysReplDispatchResult(
                    ok=True,
                    text=(f"keybindings config ready at {written} — "
                          "edits apply live on cockpit surfaces"),
                )
            return KeysReplDispatchResult(
                ok=False, text="could not create the config file",
            )

        if sub == "reload":
            keymap.get_store().maybe_reload(force=True)
            info = keymap.describe_keymap()
            warn = len(info.get("warnings", ()))
            return KeysReplDispatchResult(
                ok=True,
                text=(f"keymap reloaded — {warn} warning(s)"
                      if warn else "keymap reloaded — clean"),
            )

        if sub == "warnings":
            info = keymap.describe_keymap()
            warnings = info.get("warnings", ())
            if not warnings:
                return KeysReplDispatchResult(
                    ok=True, text="keymap: no warnings",
                )
            body = "\n".join(f"  ⚠ {w}" for w in warnings)
            return KeysReplDispatchResult(
                ok=True, text=f"keymap warnings:\n{body}",
            )

        info = keymap.describe_keymap()
        return KeysReplDispatchResult(ok=True, text=_table(info))
    except Exception:  # noqa: BLE001
        logger.debug("[KeysRepl] dispatch degraded", exc_info=True)
        return KeysReplDispatchResult(
            ok=False, text="keymap introspection unavailable",
        )


__all__ = [
    "KEYS_REPL_SCHEMA_VERSION",
    "KeysReplDispatchResult",
    "dispatch_keys_command",
]
